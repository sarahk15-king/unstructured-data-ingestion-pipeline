import csv
from collections import Counter
import io
import json
import os
import re
import shutil
import tempfile
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

import boto3
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

app = FastAPI(title="Unstructured Data Ingestion Pipeline API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

session = boto3.Session(
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    region_name=os.getenv("AWS_REGION"),
)
s3 = session.client("s3")
BUCKET = os.getenv("S3_BUCKET")

# Keeps one request fast enough for a free-tier server.
MAX_IMAGES = 25
RENDER_DPI = 150
FIGURE_CAPTION = re.compile(r"^(Figure|Fig\.)\s*\d+\s*[:.]", re.IGNORECASE)
TABLE_CAPTION = re.compile(r"^(Table\s*\d+\s*[:.]|TABLE\s*[IVX]+\s*([:.]|$))")
PANEL_LABEL = re.compile(r"^\(?([a-h])\)$", re.IGNORECASE)
HEADERS = {"User-Agent": "Mozilla/5.0 (educational data ingestion project)"}


def safe_name(name):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return cleaned or "document"


def put_object(key, body, content_type, metadata):
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=body,
        ContentType=content_type,
        Metadata={k: str(v) for k, v in metadata.items()},
    )
    return key


def put_json(key, data, metadata):
    body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    return put_object(key, body, "application/json", metadata)


def upload_table(key, name, rows, metadata):
    """Upload one table as its own CSV file and return a short summary of it."""
    clean_rows = [["" if cell is None else str(cell).replace("\n", " ") for cell in row] for row in rows]
    buffer = io.StringIO()
    csv.writer(buffer).writerows(clean_rows)
    # utf-8-sig so Excel opens the file with the right characters
    put_object(key, buffer.getvalue().encode("utf-8-sig"), "text/csv", {**metadata, "type": "table"})
    return {
        "name": name,
        "key": key,
        "row_count": len(clean_rows),
        "preview": [[cell[:120] for cell in row] for row in clean_rows[:8]],
    }


PDF_TOOLS = ("docling", "markitdown", "pypdf", "pdfplumber", "azure", "textract")
URL_TOOLS = ("beautifulsoup", "azure", "pypdf", "pdfplumber")


def rows_to_markdown(rows):
    """Turn a list of table rows into a Markdown table."""
    width = max(len(row) for row in rows)
    padded = [[(cell or "").replace("|", "/").replace("\n", " ") for cell in row] + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(padded[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(row) + " |" for row in padded[1:]]
    return "\n".join(lines)


def put_tables_in_text(text, tables):
    """Swap each table's loose text lines for a Markdown table.

    pypdf and pdfplumber give a table as plain lines (one cell or one row per line). For each
    table, find that run of lines in the page text and replace it with a Markdown table. If the
    run cannot be found, the table is added at the end of the page instead.
    """
    lines = text.split("\n")
    leftover = []
    for rows in tables:
        known = set()
        for row in rows:
            cells = [(cell or "").replace("\n", " ").strip() for cell in row]
            known.update(c for c in cells if c)
            known.add(" ".join(c for c in cells if c))
        best = None  # (start, end) of the longest run of lines that belong to the table
        start = None
        for n, line in enumerate(lines + [None]):
            if line is not None and line.strip() in known:
                if start is None:
                    start = n
            elif start is not None:
                if best is None or n - start > best[1] - best[0]:
                    best = (start, n)
                start = None
        cell_count = sum(1 for row in rows for cell in row if (cell or "").strip())
        if best and (best[1] - best[0]) >= max(2, len(rows)):
            lines[best[0]:best[1]] = ["", rows_to_markdown(rows), ""]
        else:
            leftover.append(rows)
    result = "\n".join(lines)
    for rows in leftover:
        result += "\n\n" + rows_to_markdown(rows)
    return result


def fix_markdown_tables(markdown_text, tables):
    """Replace tables that the converter got wrong with the tables found by pdfplumber.

    MarkItDown sometimes merges columns ("Quarter North"), and column-by-column reading leaves a
    table as loose lines. For each pdfplumber table, find where its text sits in the Markdown, as
    one run of characters whatever the spacing and line breaks, and put a clean Markdown table there.
    """
    for rows in tables:
        target = "".join(ch for row in rows for cell in row for ch in (cell or "") if not ch.isspace())
        if len(target) < 6:
            continue
        skip = set()
        for dashes in re.finditer(r"-{3,}", markdown_text):
            skip.update(range(dashes.start(), dashes.end()))
        positions = [i for i, ch in enumerate(markdown_text) if not ch.isspace() and ch != "|" and i not in skip]
        squeezed = "".join(markdown_text[i] for i in positions)
        # also ignore the dashes of a Markdown table's separator row when matching
        found = squeezed.find(target)
        if found < 0:
            continue
        start = positions[found]
        end = positions[found + len(target) - 1] + 1
        # take the whole line when the run starts or ends inside a Markdown table row
        while start > 0 and markdown_text[start - 1] in "| ":
            start -= 1
        while end < len(markdown_text) and markdown_text[end] in "| ":
            end += 1
        markdown_text = markdown_text[:start] + "\n\n" + rows_to_markdown(rows) + "\n\n" + markdown_text[end:]
    return re.sub(r"\n{3,}", "\n\n", markdown_text)


def page_margin_texts(page, words, repeated):
    """Header lines and footer lines of one page, as plain text (the same rules the column reader uses)."""
    if not words:
        return [], []
    body_size = Counter(round(w["size"]) for w in words).most_common(1)[0][0]
    footer_ids = footer_word_ids(page, words, body_size, repeated)
    margin = page.height * 0.11
    header_words = []
    for line in group_lines([w for w in words if w["bottom"] < margin and id(w) not in footer_ids]):
        text = line_text(line)
        if re.fullmatch(r"(page\s+)?\d{1,4}", text, re.I) or _in_drop(page, line[0], repeated, words):
            header_words += line
    footer_words = [w for w in words if id(w) in footer_ids]
    return ([line_text(l) for l in group_lines(header_words)] if header_words else [],
            [line_text(l) for l in group_lines(footer_words)] if footer_words else [])


def pypdf_page_markdown(text, page, repeated):
    """pypdf's text for one page, laid out like the PDF.

    pypdf gives the words but no formatting, so each line is looked up in the page layout (font size and
    weight come from pdfplumber) to find headings, which are printed in bold, and headers and footers,
    which are printed in italics at the top and bottom. Lines broken in the middle of a word are joined
    again, and lines that belong to one paragraph are put together."""
    try:
        figure_boxes = find_figure_regions(page)
    except Exception:
        figure_boxes = []
    words = page_words(page, [tuple(b) for b in figure_boxes])  # words inside figures stay in the image
    if not words:
        return text
    body_size = Counter(round(w["size"]) for w in words).most_common(1)[0][0]
    headers, footers = page_margin_texts(page, words, repeated)
    header_keys = {_letters(t) for t in headers}
    footer_keys = {_letters(t) for t in footers}
    layout = {}
    blocks, _ = column_blocks(page, words)
    for lines in blocks:
        for line in lines:
            fact = _line_facts(line)
            if fact["text"]:
                layout.setdefault(_letters(fact["text"]), []).append(fact)

    out, paragraph, last_bottom, last_x0 = [], [], None, None
    top_lines, bottom_lines = [], []

    def end_paragraph():
        nonlocal paragraph
        if paragraph:
            joined = paragraph[0]
            for nxt in paragraph[1:]:
                if joined.endswith("-") and nxt[:1].islower() and len(joined) > 2 and joined[-2].isalpha():
                    joined = joined[:-1] + nxt  # a word split across two lines becomes one word
                else:
                    joined += " " + nxt
            out.append(joined)
        paragraph = []

    def split_glued(key, depth=0):
        """pypdf sometimes runs two printed lines together ('CanadaKun Zhang'); cut them where the layout has line ends."""
        if key in layout and layout[key]:
            return [key]
        if depth >= 3:
            return None
        for first in sorted((k for k in layout if layout[k] and len(k) >= 6 and key.startswith(k)), key=len, reverse=True):
            rest = split_glued(key[len(first):], depth + 1) if key[len(first):] else []
            if rest is not None:
                return [first] + rest
        return None

    # words that sit inside a figure: pypdf prints them as loose lines, but they belong to the image
    figure_tokens, shared_tokens = set(), set()
    if figure_boxes:
        page_tokens = {_letters(w["text"]) for w in words}
        for w in page.extract_words():
            if char_in_boxes(w, figure_boxes):
                figure_tokens.add(_letters(w["text"]))
        figure_tokens.discard("")
        # a word that also appears in the running text (such as "the") is only figure text when most of its line is
        shared_tokens = figure_tokens & page_tokens

    def strip_figure_text(line):
        """Remove the figure's own words from a line; returns '' when nothing else is left."""
        tokens = [t for t in re.split(r"\s+", line) if _letters(t)]
        inside = [t for t in tokens if _letters(t) in figure_tokens]
        if tokens and len(inside) >= 0.6 * len(tokens):
            return ""  # a line made mostly of the figure's words is the figure's text
        # pypdf also glues the figure's words together without spaces: count the letters they cover
        letters = _letters(line)
        covered = [False] * len(letters)
        for token in figure_tokens:
            if len(token) < 3:
                continue
            start = letters.find(token)
            while start >= 0:
                for k in range(start, start + len(token)):
                    covered[k] = True
                start = letters.find(token, start + 1)
        if letters and sum(covered) >= 0.7 * len(letters):
            return ""
        kept = [t for t in re.split(r"\s+", line) if _letters(t) not in figure_tokens or _letters(t) in shared_tokens]
        rest = " ".join(kept).strip()
        return rest if _letters(rest) else ""

    todo = []
    for raw in text.split("\n"):
        line = raw.strip()
        key = _letters(line)
        if not key:
            continue
        if figure_tokens and key not in layout:
            line = strip_figure_text(line)
            key = _letters(line)
            if not key:
                continue
        if key not in layout and key not in header_keys and key not in footer_keys:
            pieces = split_glued(key)
            if pieces and len(pieces) > 1:
                for piece in pieces:
                    fact = layout[piece][0]
                    todo.append((fact["text"], piece))
                continue
        todo.append((line, key))

    for line, key in todo:
        if key in header_keys:
            top_lines.append(line)
            continue
        if key in footer_keys:
            bottom_lines.append(line)
            continue
        matches = layout.get(key)
        fact = matches.pop(0) if matches else None
        if fact is None:
            paragraph.append(line)
            continue
        line = fact["text"]  # same letters as pypdf's line, with the spaces between words restored
        size, bold = fact["size"], fact["bold"]
        height = fact["bottom"] - fact["top"]
        gap = (fact["top"] - last_bottom) if last_bottom is not None else 0
        new_column = last_x0 is not None and abs(fact["x0"] - last_x0) > 60 and gap < 0
        if re.match(r"^(Figure|Fig\.|Table|Algorithm)\s*\d+", line):
            end_paragraph()
            out.append(line)
        elif size > body_size + 3 and len(line) < 140:
            end_paragraph()
            mark = "# " if size > body_size + 6 else "## "
            if out and out[-1].startswith(mark) and last_bottom is not None and 0 <= gap < height * 1.6:
                out[-1] = out[-1][:-2] + " " + line + "**"  # a heading that wraps onto a second line
            else:
                out.append(f"{mark}**{line}**")
        elif bold and len(line) < 80 and re.match(r"^(\d+(\.\d+)*\s+)?[A-Z]", line):
            end_paragraph()
            out.append(("### " if re.match(r"^\d+\.\d+", line) else "## ") + f"**{line}**")
        elif re.match(r"^(\d{1,2}[.)]|[\u2022\u25aa\u25e6\u25cf\-\u2013])\s+\S", line):
            end_paragraph()
            paragraph.append(line)
        else:
            if paragraph and (new_column or gap > height * 0.9):
                end_paragraph()
            paragraph.append(line)
        last_bottom, last_x0 = fact["bottom"], fact["x0"]
    end_paragraph()
    italic = lambda lines: "  \n".join("*" + l.replace("*", "\\*") + "*" for l in lines)
    if top_lines or headers:
        out.insert(0, italic(top_lines or headers))
    if bottom_lines or footers:
        out.append(italic(bottom_lines or footers))
    return "\n\n".join(out)


def pypdf_page_text(pypdf_page, plumber_page, figures):
    """pypdf's text for one page, without the text that is drawn inside a figure.

    pypdf reports each piece of text with its position; the pieces that start inside a figure box are
    skipped, so the figure's labels never reach the Markdown (the figure itself is saved as an image)."""
    if not figures:
        return (pypdf_page.extract_text() or "").strip()
    height, width = float(plumber_page.height), float(plumber_page.width)
    pieces, last_y = [], [None]
    # the words pdfplumber finds inside the figures: a piece made only of these words is figure text
    figure_words = {_letters(w["text"]) for w in plumber_page.extract_words() if char_in_boxes(w, figures)}
    figure_words.discard("")
    body_words = {_letters(w["text"]) for w in plumber_page.extract_words() if not char_in_boxes(w, figures)}

    def visit(text, cm, tm, font_dict, font_size):
        if not text or not text.strip("\n"):
            return
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        top = height - y
        if 0 <= x <= width and 0 <= top <= height and any(b[0] - 1 <= x <= b[2] + 1 and b[1] - 2 <= top <= b[3] + 2 for b in figures):
            return
        # figures drawn inside a scaled graphic report positions that are not reliable: go by the words as well
        tokens = [_letters(t) for t in text.split() if _letters(t)]
        if tokens and all(t in figure_words for t in tokens) and (len(tokens) >= 3 or any(t not in body_words for t in tokens)):
            return
        if last_y[0] is not None and abs(top - last_y[0]) > 2:
            pieces.append("\n")
        pieces.append(text)
        last_y[0] = top

    try:
        pypdf_page.extract_text(visitor_text=visit)
        text = "".join(pieces).strip()
        if text:
            return text
    except Exception:
        pass
    return (pypdf_page.extract_text() or "").strip()


def convert_with_pypdf(file_path):
    """Text from pypdf, one section per page, laid out like the PDF: bold headings, italic headers and
    footers, whole words and paragraphs. pdfplumber only supplies the layout and finds the tables."""
    import pdfplumber
    from pypdf import PdfReader

    reader = PdfReader(str(file_path))
    parts = []
    with pdfplumber.open(str(file_path)) as pdf:
        plumber_pages = list(pdf.pages)
        try:
            page_list = [page_words(p) for p in plumber_pages[:40]]
            counts = Counter(t for p, w in zip(plumber_pages, page_list) for t in set(margin_lines(p, w)))
            repeated = {t for t, n in counts.items() if n >= 2 and len(plumber_pages) > 1}
        except Exception:
            repeated = set()
        for page_num, (page, plumber_page) in enumerate(zip(reader.pages, plumber_pages), start=1):
            try:
                figures = find_figure_regions(plumber_page)
            except Exception:
                figures = []
            text = pypdf_page_text(page, plumber_page, figures)
            try:
                text = pypdf_page_markdown(text, plumber_page, repeated)
            except Exception:
                pass  # keep pypdf's plain text if the layout lookup fails
            try:
                tables = extract_page_tables(plumber_page, find_figure_regions(plumber_page))
                text = put_tables_in_text(text, tables)
            except Exception:
                pass  # keep the page text even if table detection fails
            parts.append(f"## Page {page_num}\n\n{text}")
    result = "\n\n".join(parts)
    try:
        # pypdf prints formulas as loose symbols: put the LaTeX the column reader found in their place
        result = recognize_docling_formulas(result, file_path)
    except Exception:
        pass
    return result


def char_in_boxes(obj, boxes):
    """True when the middle of a character or word lies inside one of the boxes (x0, top, x1, bottom)."""
    cx, cy = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
    return any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes)


def pdfplumber_page_text(page, figures):
    """pdfplumber's words for one page in reading order (column by column), one printed line per line,
    without the words inside figures. Words keep the spaces between them, so no word is run together."""
    words = page_words(page, [tuple(b) for b in figures])
    if not words:
        return ""
    blocks, _ = column_blocks(page, words)
    return "\n".join(line_text(line) for block in blocks for line in block if line)


def convert_with_pdfplumber(file_path):
    """Text from pdfplumber, one section per page, laid out like the PDF: bold headings, italic headers and
    footers, whole words and paragraphs, formulas as LaTeX, and each table shown as a Markdown table."""
    import pdfplumber

    parts = []
    with pdfplumber.open(str(file_path)) as pdf:
        pages = list(pdf.pages)
        figure_list = []
        for page in pages:
            try:
                figure_list.append(find_figure_regions(page))
            except Exception:
                figure_list.append([])
        try:
            page_list = [page_words(p) for p in pages[:40]]
            counts = Counter(t for p, w in zip(pages, page_list) for t in set(margin_lines(p, w)))
            repeated = {t for t, n in counts.items() if n >= 2 and len(pages) > 1}
        except Exception:
            repeated = set()
        for page_num, (page, figures) in enumerate(zip(pages, figure_list), start=1):
            # text printed inside a figure (axis labels, legends) stays in the image; only the image link is wanted
            visible = page.filter(lambda obj: not (obj.get("object_type") == "char" and char_in_boxes(obj, figures))) if figures else page
            text = (visible.extract_text() or "").strip()
            try:
                ordered = pdfplumber_page_text(page, figures)
                if ordered.strip():
                    text = pypdf_page_markdown(ordered, page, repeated)
            except Exception:
                pass  # keep pdfplumber's plain text if the layout step fails
            try:
                tables = extract_page_tables(page, figures)
                text = put_tables_in_text(text, tables)
            except Exception:
                pass  # keep the page text even if table detection fails
            parts.append(f"## Page {page_num}\n\n{text}")
    result = "\n\n".join(parts)
    try:
        result = recognize_docling_formulas(result, file_path)
    except Exception:
        pass
    return result


def looks_like_text_box(rows):
    """True for a ruled box around text rather than a real table: an algorithm listing (a title, then
    'Require:' or numbered lines), or any box where every column of a row repeats the same text."""
    flat = [c for row in rows for c in row if c]
    if not flat:
        return False
    first = " ".join(c for c in rows[0] if c)
    keywords = re.compile(r"^(Require|Input|Output|Ensure|Initialize|Procedure)\b", re.I)
    if re.match(r"^(Algorithm|Listing|Pseudocode)\b", first, re.I) or any(keywords.match(c) for c in flat):
        return True
    numbered = sum(1 for row in rows if row and re.match(r"^\d{1,2}[:.]?(\s|$)", row[0] or ""))
    if numbered >= 3 and numbered >= 0.6 * len(rows):
        return True
    single = 0
    for row in rows:
        values = [v for v in dict.fromkeys(row) if v]
        single += 1 if len(values) <= 1 else 0
    return single == len(rows)


def html_tables_to_markdown(text):
    """Azure writes tables as HTML. Turn each <table> into a Markdown table (the caption goes above it)."""
    if "<table" not in text.lower():
        return text

    def convert(match):
        soup = BeautifulSoup(match.group(0), "lxml")
        table = soup.find("table")
        rows = []
        spans = {}  # column -> (cell text, rows still to fill) for cells that span several rows
        for tr in table.find_all("tr"):
            row, col = [], 0
            cells = tr.find_all(["th", "td"])
            cell_iter = iter(cells)
            while True:
                if col in spans:
                    value, left = spans[col]
                    row.append(value)
                    spans[col] = (value, left - 1) if left > 1 else None
                    if spans[col] is None:
                        del spans[col]
                    col += 1
                    continue
                cell = next(cell_iter, None)
                if cell is None:
                    break
                value = " ".join(cell.get_text(" ", strip=True).split())
                colspan = int(cell.get("colspan", 1) or 1)
                rowspan = int(cell.get("rowspan", 1) or 1)
                for _ in range(colspan):
                    row.append(value)
                    if rowspan > 1:
                        spans[col] = (value, rowspan - 1)
                    col += 1
            if row:
                rows.append(row)
        if not rows:
            return match.group(0)
        # a box drawn around text (an algorithm listing, say) is not a table: print its lines as text
        if looks_like_text_box(rows):
            return "  \n".join(" ".join(dict.fromkeys(c for c in row if c)) for row in rows if any(row))
        caption = table.find("caption")
        title = f"{caption.get_text(' ', strip=True)}\n\n" if caption else ""
        return title + rows_to_markdown(rows)

    return re.sub(r"<table\b.*?</table>", convert, text, flags=re.IGNORECASE | re.DOTALL)


def pipe_text_boxes_to_text(text):
    """A Markdown table that is really a box of text (an algorithm listing) is printed as lines."""
    def convert(match):
        rows = []
        for line in match.group(0).strip().split("\n"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{3,}:?", c) or not c for c in cells):
                continue
            rows.append(cells)
        if rows and looks_like_text_box(rows):
            return "  \n".join(" ".join(dict.fromkeys(c for c in row if c)) for row in rows if any(row)) + "\n"
        return match.group(0)

    return re.sub(r"(?m)(?:^\|.*\|[ \t]*\n?)+", convert, text)


def clean_azure_markdown(text):
    """Tidy Azure's Markdown: HTML tables become Markdown tables, figures keep only their caption
    (not the chart's axis labels), and the page comments are removed."""
    def figure(match):
        caption = re.search(r"<figcaption>(.*?)</figcaption>", match.group(0), flags=re.IGNORECASE | re.DOTALL)
        return " ".join(caption.group(1).split()) if caption else ""

    text = re.sub(r"<figure\b.*?</figure>", figure, text, flags=re.IGNORECASE | re.DOTALL)
    text = html_tables_to_markdown(text)
    text = pipe_text_boxes_to_text(text)
    text = italic_page_margins(text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def reflow_paragraphs(text):
    """Azure keeps the printed line breaks. Lines of one paragraph are joined again, and a word split
    across two lines by a hyphen is made whole."""
    blocks = []
    for block in re.split(r"\n\s*\n", text):
        lines = [l for l in block.split("\n") if l.strip()]
        plain = lines and len(lines) > 1 and not any(
            re.match(r"^\s*(#|\||<|!\[|>|\$\$|\*|[-+]\s|\d+[.)]\s|[•▪◦●])", l) or l.endswith("  ") for l in lines)
        if not plain:
            blocks.append(block.strip("\n"))
            continue
        joined = lines[0].strip()
        for nxt in lines[1:]:
            nxt = nxt.strip()
            if joined.endswith("-") and nxt[:1].islower() and len(joined) > 2 and joined[-2].isalpha():
                joined = joined[:-1] + nxt  # a word split across two lines becomes one word
            else:
                joined += " " + nxt
        blocks.append(joined)
    cleaned = []
    for block in blocks:
        if not block:
            continue
        # a caption printed twice (once as text, once as the first line of the box under it)
        if cleaned and block.startswith(cleaned[-1].strip() + "  \n"):
            block = block[len(cleaned[-1].strip()) + 3:]
        cleaned.append(block)
    return "\n\n".join(cleaned)


def bold_headings(text):
    """Headings are printed in bold: '## Abstract' becomes '## **Abstract**'."""
    def bold(match):
        title = match.group(2).strip()
        if not title or title.startswith("**"):
            return match.group(0)
        return f"{match.group(1)} **{title}**"
    return re.sub(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$", bold, text)


def italicize_margin_lines(text, reader_text):
    """Headers, footers and page numbers that Azure printed as ordinary text are set in italics.
    They are recognised by matching the lines the column reader found in the margins."""
    margin_keys = set()
    for line in re.findall(r"(?m)^\*(?!\*)(.+?)\*\s*$", reader_text):
        key = _letters(line.replace("\\*", "*"))
        if len(key) >= 3:
            margin_keys.add(key)
    if not margin_keys:
        return text
    lines = text.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("*") or stripped.startswith("#") or not stripped:
            continue
        if _letters(stripped) in margin_keys:
            lines[i] = "*" + stripped.replace("*", "\\*") + "*"
    return "\n".join(lines)


AZURE_MARGIN = re.compile(r'<!--\s*(PageHeader|PageFooter|PageNumber)\s*=\s*"(.*?)"\s*-->', re.DOTALL)


def italic_page_margins(text):
    """Azure marks running headers, footers and page numbers with comments. Each page gets its headers in
    italics at the top and its footers and page number in italics at the bottom."""
    pages = re.split(r"<!--\s*PageBreak\s*-->", text)
    result = []
    for page in pages:
        headers, footers = [], []
        for kind, value in AZURE_MARGIN.findall(page):
            value = " ".join(value.split())
            if value:
                (headers if kind == "PageHeader" else footers).append("*" + value.replace("*", "\\*") + "*")
        body = AZURE_MARGIN.sub("", page).strip()
        pieces = ["  \n".join(headers)] if headers else []
        pieces.append(body)
        if footers:
            pieces.append("  \n".join(footers))
        result.append("\n\n".join(p for p in pieces if p))
    return "\n\n".join(result)


def author_block(reader_text):
    """The title paragraphs and author paragraphs at the start of the column reader's text."""
    paragraphs = [p.strip() for p in reader_text.split("\n\n") if p.strip()]
    for index, paragraph in enumerate(paragraphs):
        if re.match(r"^#*\s*\**abstract\b", paragraph, re.I) and index > 0:
            front = paragraphs[:index]
            authors = [p for p in front if not p.startswith("#") and not p.startswith("*")]
            return authors
    return []


def _is_math_symbol(ch):
    """Greek letters, maths italic letters and maths operators."""
    code = ord(ch)
    return 0x370 <= code <= 0x3FF or 0x1D400 <= code <= 0x1D7FF or 0x2190 <= code <= 0x22FF or ch in "ℝℕℤℚℂ"


def _word_spans(text):
    """Words with their positions, skipping formula blocks and Markdown marks."""
    words, offset = [], 0
    for line in text.split("\n"):
        if not line.lstrip().startswith("$$"):
            for match in re.finditer(r"\S+", line):
                token = match.group(0).strip("*#|`")
                if token and not set(token) <= set("-:"):
                    words.append((token, offset + match.start(), offset + match.end()))
        offset += len(line) + 1
    return words


def repair_symbols(text, reader_text):
    """Azure often reads a Greek letter or maths symbol as a look-alike letter (tau as 't', xi as '5').
    The words of Azure's text are lined up with the words the column reader read from the PDF's own
    text layer, and where they differ at a symbol, the paper's own symbols are put back."""
    import difflib

    reader_lines = [l for l in reader_text.split("\n") if not l.lstrip().startswith("*")]
    azure_words = _word_spans(text)
    reader_words = [w for w in _word_spans("\n".join(reader_lines))]
    if not azure_words or not reader_words:
        return text
    matcher = difflib.SequenceMatcher(None, [w[0] for w in azure_words], [w[0] for w in reader_words], autojunk=False)
    edits = []
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag != "replace" or a1 - a0 > 4 or b1 - b0 > 4:
            continue
        reader_span = [w[0] for w in reader_words[b0:b1]]
        azure_span = [w[0] for w in azure_words[a0:a1]]
        has_symbol = any(_is_math_symbol(ch) for token in reader_span for ch in token)
        short = len("".join(azure_span)) <= 14 and not any(_is_math_symbol(ch) for token in azure_span for ch in token)
        if has_symbol and short:
            edits.append((azure_words[a0][1], azure_words[a1 - 1][2], " ".join(reader_span)))
    for start, end, replacement in sorted(edits, reverse=True):
        # keep punctuation that follows the word in Azure's text
        text = text[:start] + replacement + text[end:]
    return text


def fix_azure_authors(text, file_path):
    """Azure reads a two-column title block column by column, so the second author lands after the
    Introduction. All authors are put right under the title, as printed, and the stray copies are removed."""
    reader = read_pdf_with_reader(file_path)
    authors = author_block(reader) if reader else []
    if not authors:
        return text
    paragraphs = [p for p in re.split(r"\n\s*\n", text)]
    abstract = next((i for i, p in enumerate(paragraphs) if re.match(r"^#*\s*\**abstract\**\s*$", p.strip(), re.I)), None)
    if abstract is None:
        return text
    title_end = max([i for i, p in enumerate(paragraphs[:abstract]) if p.lstrip().startswith("#")], default=-1)
    block_letters = _letters(" ".join(authors))
    keep_front = [p for p in paragraphs[:title_end + 1]]
    # anything between the title and "Abstract" that is not an author line stays (such as a venue line)
    middle = [p for p in paragraphs[title_end + 1:abstract] if _letters(p) and _letters(p) not in block_letters]
    rest = []
    for i, p in enumerate(paragraphs[abstract:], start=abstract):
        key = _letters(p)
        if i > abstract and len(key) >= 8 and key in block_letters and i < len(paragraphs) * 0.6:
            continue  # the second author, printed after the Introduction
        rest.append(p)
    return "\n\n".join(keep_front + authors + middle + rest)


def azure_markdown(body, content_type=None, pages=None):
    """Markdown from Azure Document Intelligence (prebuilt-layout model).

    The free tier reads only the first two pages of a PDF and files up to 4 MB.
    """
    endpoint = os.getenv("AZURE_DOCINTEL_ENDPOINT")
    key = os.getenv("AZURE_DOCINTEL_KEY")
    if not endpoint or not key:
        raise HTTPException(
            status_code=503,
            detail="Azure is not set up on this server. Add AZURE_DOCINTEL_ENDPOINT and AZURE_DOCINTEL_KEY.",
        )

    try:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.core.credentials import AzureKeyCredential
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="The Azure library is not installed on this server. Add azure-ai-documentintelligence "
                   "to requirements.txt and redeploy.",
        )

    client = DocumentIntelligenceClient(endpoint=endpoint, credential=AzureKeyCredential(key))
    options = {"content_type": content_type} if content_type else {}
    if pages:
        options["pages"] = pages
    poller = client.begin_analyze_document("prebuilt-layout", body=body, output_content_format="markdown", **options)
    return clean_azure_markdown(poller.result().content)


AZURE_PAGES_PER_CALL = 2   # the free tier reads at most 2 pages per request
AZURE_MAX_PAGES = 12       # keeps one upload from running too long on a free server


def convert_with_azure(file_path):
    """Send a PDF to Azure two pages at a time, so the free tier still covers the whole document."""
    from pypdf import PdfReader

    data = Path(file_path).read_bytes()
    total_pages = len(PdfReader(str(file_path)).pages)
    last_page = min(total_pages, AZURE_MAX_PAGES)

    parts = []
    for first in range(1, last_page + 1, AZURE_PAGES_PER_CALL):
        last = min(first + AZURE_PAGES_PER_CALL - 1, last_page)
        try:
            parts.append(azure_markdown(data, content_type="application/pdf", pages=f"{first}-{last}"))
        except HTTPException:
            raise
        except Exception as e:
            if not parts:
                raise
            parts.append(f"> Azure stopped before page {first}: {e}")
            break

    markdown = "\n\n".join(parts)
    try:
        markdown = fix_azure_authors(markdown, file_path)
        markdown = italicize_margin_lines(markdown, read_pdf_with_reader(file_path))
        markdown = recognize_docling_formulas(markdown, file_path)
        markdown = repair_symbols(markdown, read_pdf_with_reader(file_path))
    except Exception:
        pass  # keep Azure's own text if the extra steps fail
    markdown = bold_headings(reflow_paragraphs(markdown))
    if total_pages > AZURE_MAX_PAGES:
        markdown += f"\n\n> Only the first {AZURE_MAX_PAGES} of {total_pages} pages were sent to Azure."
    return markdown


TEXTRACT_MAX_WAIT = 300  # seconds to wait for Textract before giving up


def convert_with_textract(file_path):
    """Amazon Textract (tables feature): the PDF goes to S3, Textract reads it, the blocks become Markdown.

    Textract returns lines and table cells only, so the Markdown has no headings.
    """
    import time

    try:
        from api.textract_to_md import textract_to_markdown
    except ImportError:  # the server was started from inside the api folder
        from textract_to_md import textract_to_markdown

    if not BUCKET:
        raise HTTPException(status_code=503, detail="Textract needs the S3_BUCKET setting.")
    client = session.client("textract")
    key = f"textract-input/{date.today().isoformat()}/{safe_name(Path(file_path).name)}"
    s3.upload_file(str(file_path), BUCKET, key)
    try:
        job = client.start_document_analysis(
            DocumentLocation={"S3Object": {"Bucket": BUCKET, "Name": key}}, FeatureTypes=["TABLES"]
        )["JobId"]
        waited = 0
        while True:
            result = client.get_document_analysis(JobId=job)
            if result["JobStatus"] != "IN_PROGRESS":
                break
            if waited >= TEXTRACT_MAX_WAIT:
                raise HTTPException(status_code=504, detail="Textract took too long on this PDF.")
            time.sleep(2)
            waited += 2
        if result["JobStatus"] != "SUCCEEDED":
            raise HTTPException(status_code=500, detail=f"Textract failed: {result.get('StatusMessage', result['JobStatus'])}")
        blocks = list(result["Blocks"])
        while result.get("NextToken"):
            result = client.get_document_analysis(JobId=job, NextToken=result["NextToken"])
            blocks.extend(result["Blocks"])
    finally:
        s3.delete_object(Bucket=BUCKET, Key=key)
    markdown = textract_to_markdown(blocks)
    try:
        # Textract cannot read equations, so the column reader's LaTeX formulas are put in their places
        markdown = recognize_docling_formulas(markdown, file_path)
    except Exception:
        pass  # keep Textract's own text if this step fails
    return bold_headings(markdown)


_docling_converter = None


def light_docling_converter(DocumentConverter):
    """Docling with its lighter settings: no OCR (the PDFs have a text layer) and the fast table model.

    This uses less memory and time than the default setup. If these settings are not available in the
    installed Docling version, the default converter is used instead.
    """
    global _docling_converter
    if _docling_converter is None:
        try:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
            from docling.document_converter import PdfFormatOption

            options = PdfPipelineOptions()
            options.do_ocr = False
            options.table_structure_options.mode = TableFormerMode.FAST
            _docling_converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
            )
        except Exception:
            _docling_converter = DocumentConverter()
    return _docling_converter


def convert_with_docling(file_path):
    """Docling needs more memory than Render's free tier has.

    When DOCLING_SERVICE_URL is set, the PDF is sent to a separate Docling service (for example a
    Hugging Face Space). Otherwise Docling runs inside this server, which works on a local machine.
    """
    service_url = os.getenv("DOCLING_SERVICE_URL")
    if service_url:
        headers = {"X-Token": os.getenv("DOCLING_SERVICE_TOKEN", "")}
        with open(file_path, "rb") as f:
            try:
                response = requests.post(
                    service_url.rstrip("/") + "/convert",
                    files={"file": ("document.pdf", f, "application/pdf")},
                    headers=headers,
                    timeout=900,
                )
            except requests.RequestException as e:
                raise HTTPException(status_code=502, detail=f"Could not reach the Docling service: {e}")
        if response.status_code != 200:
            try:
                detail = response.json().get("detail", response.text[:200])
            except ValueError:
                detail = response.text[:200]
            raise HTTPException(status_code=502, detail=f"Docling service error ({response.status_code}): {detail}")
        return response.json()["markdown"]

    try:
        from docling.document_converter import DocumentConverter
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="Docling is not set up on this server. Set DOCLING_SERVICE_URL, or run the backend locally.",
        )
    return light_docling_converter(DocumentConverter).convert(str(file_path)).document.export_to_markdown()


MATH_FONT_HINTS = ("math", "cmmi", "cmsy", "cmex", "msam", "msbm", "txsys", "txexs", "txmia", "txsym", "symbol", "stix", "mi7", "mtmi", "mtsy", "mtextra", "euler", "lmmath", "standardsym")


def _is_math_char(char):
    name = char.get("fontname", "").lower()
    return any(hint in name for hint in MATH_FONT_HINTS) or ord(char["text"][0]) > 0x2000


def find_gutters(words, page_width):
    """x ranges of the empty strips between text columns (one strip for 2 columns, two for 3), or [] for one column."""
    if len(words) < 40:
        return []
    lo, hi = page_width * 0.12, page_width * 0.88
    xs = [lo + i for i in range(int(hi - lo) + 1)]
    covered = [0] * len(xs)
    for w in words:
        for i, x in enumerate(xs):
            if w["x0"] <= x <= w["x1"]:
                covered[i] += 1
    # a column gap is a run of x positions that almost no word crosses
    limit = max(2, len(words) * 0.02)
    runs, start = [], None
    for i, c in enumerate(covered + [10 ** 9]):
        if c <= limit and start is None:
            start = i
        elif c > limit and start is not None:
            if i - start >= 6:
                runs.append((xs[start], xs[i - 1]))
            start = None
    gutters = []
    for g0, g1 in runs:
        left = sum(1 for w in words if w["x1"] <= g0 + 1)
        right = sum(1 for w in words if w["x0"] >= g1 - 1)
        if left >= len(words) * 0.15 and right >= len(words) * 0.15:
            gutters.append((g0, g1))
    return gutters[:2]


def find_gutter(words, page_width):
    """The central gutter of a two-column page, or None."""
    for g0, g1 in find_gutters(words, page_width):
        if page_width * 0.38 <= (g0 + g1) / 2 <= page_width * 0.62:
            return g0, g1
    return None


def group_lines(words, tolerance=3.5):
    """Words -> lines (lists of words, left to right), by vertical position."""
    lines = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        mid = (w["top"] + w["bottom"]) / 2
        for line in lines:
            if abs(line["mid"] - mid) <= tolerance:
                line["words"].append(w)
                line["mid"] = sum((x["top"] + x["bottom"]) / 2 for x in line["words"]) / len(line["words"])
                break
        else:
            lines.append({"mid": mid, "words": [w]})
    out = []
    for line in sorted(lines, key=lambda l: l["mid"]):
        out.append(sorted(line["words"], key=lambda w: w["x0"]))
    return out


def line_text(words):
    text = ""
    previous = None
    for w in words:
        if previous is not None:
            gap = w["x0"] - previous["x1"]
            size = max(w.get("size", 10), 1)
            if gap > size * 0.12:
                text += " "
        text += w["text"]
        previous = w
    return text.strip()


def column_blocks(page, words):
    """Reading order: full-width lines stay where they are, each band of columns reads left to right,
    one column after the other. Works for two or three columns.

    Returns a list of blocks (each a list of lines) and whether the page has columns."""
    gutters = find_gutters(words, page.width)
    all_lines = group_lines(words)
    if not gutters:
        return [all_lines], False
    middles = [(g0 + g1) / 2 for g0, g1 in gutters]
    gutter_width = min(g1 - g0 for g0, g1 in gutters)
    blocks, bands = [], [[] for _ in range(len(gutters) + 1)]

    def flush():
        nonlocal bands
        for side in bands:
            if side:
                blocks.append(group_lines(side))
        bands = [[] for _ in range(len(gutters) + 1)]

    def column_of(w):
        centre = (w["x0"] + w["x1"]) / 2
        return sum(1 for mid in middles if centre > mid)

    def full_width(line):
        if any(w["x0"] < mid < w["x1"] for w in line for mid in middles):
            return True
        used = {column_of(w) for w in line}
        if len(used) > 1:
            # one line of text that happens to break at a gutter: the gap is a normal word space
            for mid in middles:
                left = [w for w in line if w["x1"] <= mid]
                right = [w for w in line if w["x0"] >= mid]
                if left and right and min(w["x0"] for w in right) - max(w["x1"] for w in left) < gutter_width * 0.6:
                    return True
        return False

    column_width = (page.width - 2 * min(w["x0"] for w in words)) / (len(gutters) + 1.2)
    # on the first page the author block sits above the body: names, e-mails and affiliations side by side.
    # Those rows are kept together (left author, then right author) instead of waiting for the end of the column.
    banner = page.page_number == 1

    for line in all_lines:
        if full_width(line):
            flush()
            blocks.append([line])
            continue
        if banner:
            sides = {}
            for w in line:
                sides.setdefault(column_of(w), []).append(w)
            wide = any(max(x["x1"] for x in ws) - min(x["x0"] for x in ws) > 0.8 * column_width for ws in sides.values())
            heading = any(re.match(r"^(abstract\b|keywords?\b|index terms|\d{1,2}\.?\s+[A-Z]|[IVX]+\.\s+[A-Z])", line_text(ws), re.I) for ws in sides.values())
            if len(sides) < len(gutters) + 1 or wide or heading:
                flush()
                banner = False
        for w in line:
            bands[column_of(w)].append(w)
    flush()
    return blocks, True


# ---- formulas to LaTeX ----
GREEK = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "varepsilon", "ϵ": "epsilon", "ζ": "zeta",
    "η": "eta", "θ": "theta", "ϑ": "vartheta", "ι": "iota", "κ": "kappa", "λ": "lambda", "μ": "mu", "µ": "mu",
    "ν": "nu", "ξ": "xi", "ο": "o", "π": "pi", "ϖ": "varpi", "ρ": "rho", "ϱ": "varrho", "σ": "sigma",
    "ς": "varsigma", "τ": "tau", "υ": "upsilon", "φ": "varphi", "ϕ": "phi", "χ": "chi", "ψ": "psi", "ω": "omega",
    "Γ": "Gamma", "Δ": "Delta", "Θ": "Theta", "Λ": "Lambda", "Ξ": "Xi", "Π": "Pi", "Σ": "Sigma",
    "Υ": "Upsilon", "Φ": "Phi", "Ψ": "Psi", "Ω": "Omega",
}
SYMBOLS = {
    "−": "-", "–": "-", "—": "-", "∥": r"\|", "∈": r"\in", "∉": r"\notin", "≤": r"\leq", "≥": r"\geq", "×": r"\times",
    "·": r"\cdot", "∙": r"\cdot", "⋅": r"\cdot", "∑": r"\sum", "∏": r"\prod", "∫": r"\int", "∞": r"\infty",
    "→": r"\to", "←": r"\leftarrow", "⇒": r"\Rightarrow", "⇔": r"\Leftrightarrow", "∂": r"\partial", "∇": r"\nabla",
    "±": r"\pm", "∓": r"\mp", "≈": r"\approx", "≠": r"\neq", "∝": r"\propto", "∼": r"\sim", "≡": r"\equiv",
    "⊂": r"\subset", "⊆": r"\subseteq", "∪": r"\cup", "∩": r"\cap", "∀": r"\forall", "∃": r"\exists", "∅": r"\emptyset",
    "′": "'", "∆": r"\Delta", "̸": "", "⊤": r"^{\top}", "ℝ": r"\mathbb{R}", "ℕ": r"\mathbb{N}", "ℤ": r"\mathbb{Z}", "ℂ": r"\mathbb{C}",
    "𝔼": r"\mathbb{E}", "ℙ": r"\mathbb{P}", "{": r"\{", "}": r"\}", "%": r"\%", "&": r"\&", "#": r"\#", "_": r"\_",
    "⟨": r"\langle", "⟩": r"\rangle", "⌊": r"\lfloor", "⌋": r"\rfloor", "⌈": r"\lceil", "⌉": r"\rceil", "‖": r"\|",
    "∘": r"\circ", "⊙": r"\odot", "⊗": r"\otimes", "⊕": r"\oplus", "…": r"\ldots", "⋯": r"\cdots", "<": "<", ">": ">",
    "∼": r"\sim", "≪": r"\ll", "≫": r"\gg", "∥": r"\|", "√": r"\surd", "−": "-",
}
FUNCTIONS = {"exp", "log", "ln", "min", "max", "sin", "cos", "tan", "tanh", "sinh", "cosh", "lim", "sup", "inf",
             "det", "arg", "argmax", "argmin", "softmax", "KL", "Var", "Cov", "E", "Pr", "dim", "rank", "tr", "diag", "sign"}
NAMED_FUNCTIONS = {"exp", "log", "ln", "min", "max", "sin", "cos", "tan", "tanh", "sinh", "cosh", "lim", "sup", "inf", "det", "dim", "arg"}
BIG_OPERATORS = {"∑", "∏", "∫", "⋃", "⋂"}
ACCENTS = {"ˆ": r"\hat", "^": r"\hat", "˜": r"\tilde", "~": r"\tilde", "¯": r"\bar", "‾": r"\bar", "˙": r"\dot", "¨": r"\ddot", "→": r"\vec"}
ITALIC_FONT_HINTS = ("ital", "cmmi", "oblique", "slant", "mathit", "mmi", "-mi", "mi7", "math italic")


def _is_extension_font(char):
    name = char.get("fontname", "").lower()
    return "cmex" in name or "exs" in name.split("+")[-1] or "mex" in name


def _is_italic(char):
    name = char.get("fontname", "").lower()
    return any(h in name for h in ITALIC_FONT_HINTS) or name.endswith("mi") or "mathmi" in name.replace("-", "").replace("_", "")


def _normalise(text):
    """Math italic / bold letters become plain letters; returns (plain text, was_math_alphanumeric)."""
    plain = unicodedata.normalize("NFKC", text)
    return plain, plain != text


def _symbol(text):
    if text in SYMBOLS:
        return SYMBOLS[text]
    if text in GREEK:
        return "\\" + GREEK[text]
    return text


def _items_from_chars(chars):
    items = []
    for ch in chars:
        text = ch["text"]
        if text.startswith("(cid:"):
            n = int(re.sub(r"\D", "", text) or -1)
            if _is_extension_font(ch):
                text = {88: "∑", 80: "∑", 89: "∏", 81: "∏", 90: "∫", 82: "∫",
                        0: "(", 1: ")", 112: "√", 113: "√", 114: "√", 115: "√", 116: "√", 16: "(", 18: "(", 32: "(", 17: ")", 19: ")", 33: ")"}.get(n, "")
            else:
                text = "(" if n == 0 else ")" if n == 1 else ""
        text = re.sub("[︀-️​]", "", text)
        if not text.strip():
            continue
        plain, math_alnum = _normalise(text)
        items.append({
            "text": plain, "x0": ch["x0"], "x1": ch["x1"], "top": ch["top"], "bottom": ch["bottom"],
            "size": ch.get("size", 10), "italic": _is_italic(ch) or math_alnum,
            "cy": (ch["top"] + ch["bottom"]) / 2, "latex": None,
            "base": -ch["matrix"][5] if ch.get("matrix") else ch["bottom"] - 0.22 * ch.get("size", 10),
            "big": _is_extension_font(ch) and text in "()",
        })
    return items


def _span(items):
    return min(i["x0"] for i in items), max(i["x1"] for i in items), min(i["top"] for i in items), max(i["bottom"] for i in items)


def _placeholder(latex, region):
    x0, x1, top, bottom = region
    return {"text": "", "x0": x0, "x1": x1, "top": top, "bottom": bottom, "size": 10, "italic": False,
            "cy": (top + bottom) / 2, "latex": latex}


def _barless_fraction_rules(items, base_size):
    """A fraction drawn inside tall brackets with no bar: two rows of text stacked between '(' and ')'.
    A bar is invented between the rows so the normal fraction code handles it."""
    opens = sorted([i for i in items if i.get("big") and i["text"] == "("], key=lambda i: i["x0"])
    closes = sorted([i for i in items if i.get("big") and i["text"] == ")"], key=lambda i: i["x0"])
    found = []
    for o in opens:
        c = next((c for c in closes if c["x0"] > o["x1"]), None)
        if c is None:
            continue
        inner = [i for i in items if o["x1"] <= (i["x0"] + i["x1"]) / 2 <= c["x0"] and i["latex"] is None and not i.get("big")]
        if len(inner) < 2:
            continue
        rows = []
        for i in sorted(inner, key=lambda i: i["cy"]):
            if rows and abs(i["cy"] - rows[-1][-1]["cy"]) < base_size * 0.45:
                rows[-1].append(i)
            else:
                rows.append([i])
        if len(rows) != 2:
            continue
        top_row, bottom_row = rows
        mid = (max(i["bottom"] for i in top_row) + min(i["top"] for i in bottom_row)) / 2
        found.append({"x0": o["x1"], "x1": c["x0"], "top": mid - 0.3, "bottom": mid + 0.3, "parens": [o, c]})
    return found


def parse_layout(items, rules, base_size=None):
    """LaTeX for a group of positioned characters (items) and thin horizontal rules (fraction bars)."""
    items = [i for i in items]
    if not items:
        return ""
    if base_size is None:
        sizes = [round(i["size"], 1) for i in items if i["text"].strip()]
        base_size = max(set(sizes), key=sizes.count) if sizes else 10

    rules = list(rules)
    for synthetic in _barless_fraction_rules(items, base_size):
        overlapped = any(min(r["x1"], synthetic["x1"]) - max(r["x0"], synthetic["x0"]) > 0.5 * (synthetic["x1"] - synthetic["x0"]) and
                         abs((r["top"] + r["bottom"]) / 2 - (synthetic["top"] + synthetic["bottom"]) / 2) < base_size * 2 for r in rules)
        if not overlapped:
            rules.append(synthetic)
    rules = sorted(rules, key=lambda r: -(r["x1"] - r["x0"]))
    used = set()
    # fractions and roots: the widest bar first, so a bar inside a numerator is handled inside it
    for index, rule in enumerate(rules):
        if index in used:
            continue
        cy = (rule["top"] + rule["bottom"]) / 2
        inside = [i for i in items if rule["x0"] - 2 <= (i["x0"] + i["x1"]) / 2 <= rule["x1"] + 2
                  and abs(i["cy"] - cy) < base_size * 3.2 and i["latex"] is None and not i.get("big")]
        above = [i for i in inside if i["cy"] < cy - 0.5]
        below = [i for i in inside if i["cy"] > cy + 0.5]
        inner = [(k, r) for k, r in enumerate(rules) if k != index and k not in used
                 and r["x0"] >= rule["x0"] - 2 and r["x1"] <= rule["x1"] + 2 and abs((r["top"] + r["bottom"]) / 2 - cy) < base_size * 3.2]
        if not above and not below:
            continue
        used.add(index)
        for k, _ in inner:
            used.add(k)
        num_rules = [r for k, r in inner if (r["top"] + r["bottom"]) / 2 < cy]
        den_rules = [r for k, r in inner if (r["top"] + r["bottom"]) / 2 >= cy]
        if above and below:
            latex = r"\frac{%s}{%s}" % (parse_layout(above, num_rules, base_size), parse_layout(below, den_rules, base_size))
            members = above + below
        elif below and not above:
            latex = r"\overline{%s}" % parse_layout(below, [], base_size)
            members = below
        else:
            continue
        # a radical sign just left of the bar turns an overline into a square root
        radicals = [i for i in items if i["text"] == "√" and abs(i["x1"] - rule["x0"]) < base_size * 0.8]
        if radicals and latex.startswith(r"\overline"):
            latex = r"\sqrt{%s}" % latex[len(r"\overline{"):-1]
            members = members + radicals
        parens = rule.get("parens")
        if not parens:
            left = [i for i in items if i.get("big") and i["text"] == "(" and 0 <= rule["x0"] - i["x1"] < base_size * 0.9]
            right = [i for i in items if i.get("big") and i["text"] == ")" and 0 <= i["x0"] - rule["x1"] < base_size * 0.9]
            parens = [left[-1], right[0]] if left and right else None
        if parens:
            latex = r"\left( %s \right)" % latex
            members = members + parens
        ids = {id(m) for m in members}
        region = _span(members + [{"x0": rule["x0"], "x1": rule["x1"], "top": rule["top"], "bottom": rule["bottom"]}])
        items = [i for i in items if id(i) not in ids]
        # the fraction sits on the main baseline, not at its own height
        items.append(_placeholder(latex, region))

    return _parse_line(items, base_size)


def _parse_line(items, base_size):
    items = sorted(items, key=lambda i: (i["x0"] + i["x1"]) / 2)
    normal = [i for i in items if i["latex"] is None and i["size"] >= base_size * 0.88 and i["text"] not in BIG_OPERATORS]
    if normal:
        ordered = sorted(i["cy"] for i in normal)
        baseline = ordered[len(ordered) // 2]
    else:
        baseline = sum(i["cy"] for i in items) / len(items)

    def foot(i):
        return i.get("base", i["bottom"] - 0.22 * i["size"])

    base_foot = sorted(foot(i) for i in normal)[len(normal) // 2] if normal else baseline

    def role(i):
        if i["latex"] is not None or i["text"] in BIG_OPERATORS:
            return "main"
        if i["size"] < base_size * 0.88:
            if foot(i) < base_foot - base_size * 0.22:
                return "sup"
            if foot(i) > base_foot + base_size * 0.08:
                return "sub"
        return "main"

    nodes = []  # each: {"item": ..., "sup": [], "sub": [], "limits": bool}
    mains = [i for i in items if role(i) == "main"]
    scripts = [i for i in items if role(i) != "main"]
    # scripts attach to the nearest main item to their left (or above/below a big operator)
    for m in mains:
        nodes.append({"item": m, "sup": [], "sub": []})
    for s in scripts:
        centre = (s["x0"] + s["x1"]) / 2
        target = None
        for node in nodes:
            m = node["item"]
            if m["text"] in BIG_OPERATORS and m["x0"] - 4 <= centre <= m["x1"] + 4:
                target = node
                break
        if target is None:
            candidates = [n for n in nodes if n["item"]["x1"] <= s["x1"] + 1.5 and (n["item"]["x0"] + n["item"]["x1"]) / 2 <= centre]
            target = candidates[-1] if candidates else (nodes[0] if nodes else None)
        if target is None:
            continue
        target["sup" if role(s) == "sup" else "sub"].append(s)

    pieces = _tokens([n["item"] for n in nodes], nodes)
    return " ".join(p for p in pieces if p).strip()


def _function_at(nodes, i):
    """If the letters starting at node i spell a known function name (min, argmax, softmax...), return (name, number of nodes)."""
    letters, owners = "", []
    for k in range(i, len(nodes)):
        it = nodes[k]["item"]
        if it["latex"] is not None or not it["text"].isalpha() or not it["text"].isascii():
            break
        letters += it["text"]
        owners.append(k)
    for name in sorted(FUNCTIONS, key=len, reverse=True):
        if letters.startswith(name) and len(name) >= 2:
            count = 0
            used = ""
            for k in owners:
                used += nodes[k]["item"]["text"]
                count += 1
                if len(used) >= len(name):
                    break
            if used == name:
                return name, count
    return None


def _script_text(parts):
    parts = sorted(parts, key=lambda i: i["x0"])
    out = _tokens(parts, [{"item": p, "sup": [], "sub": []} for p in parts])
    # three or more plain letters in a row are a word (old, phys): set them upright
    if len(parts) >= 3 and all(p["text"].isalpha() and p["text"].isascii() and p["latex"] is None for p in parts):
        return [r"\mathrm{%s}" % "".join(p["text"] for p in parts)]
    return out


def _tokens(mains, nodes):
    out = []
    i = 0
    while i < len(nodes):
        node = nodes[i]
        item = node["item"]
        text = item["text"]
        # accents sit over the previous letter: yˆ -> \hat{y}
        if out and text in ACCENTS and nodes[i - 1]["item"]["x0"] - 2 <= (item["x0"] + item["x1"]) / 2 <= nodes[i - 1]["item"]["x1"] + 4 and item["latex"] is None:
            out[-1] = ACCENTS[text] + "{" + out[-1] + "}"
            i += 1
            continue
        func = _function_at(nodes, i)
        if item["latex"] is not None:
            token = item["latex"]
        elif func:
            name, count = func
            last = i + count - 1
            for k in range(i, last):
                nodes[last]["sub"] += nodes[k]["sub"]
                nodes[last]["sup"] += nodes[k]["sup"]
            i = last
            node = nodes[i]
            token = ("\\" + name) if name in NAMED_FUNCTIONS else (r"\operatorname{%s}" % name)
        elif text.isalpha() and text.isascii() and not item["italic"]:
            # a run of upright letters is a word: exp, log, softmax
            j, word = i, ""
            while j < len(nodes):
                it = nodes[j]["item"]
                if it["latex"] is None and it["text"].isascii() and it["text"].isalpha() and not it["italic"]:
                    word += it["text"]
                    j += 1
                    if nodes[j - 1]["sup"] or nodes[j - 1]["sub"]:
                        break
                else:
                    break
            node = nodes[j - 1]
            i = j - 1
            if len(word) >= 2:
                token = ("\\" + word) if word in NAMED_FUNCTIONS else (r"\operatorname{%s}" % word)
            else:
                token = word
        else:
            token = _symbol(text)
        if node["item"]["text"] in BIG_OPERATORS or node["sub"] or node["sup"]:
            if node["sub"]:
                token += "_{%s}" % " ".join(_script_text(node["sub"]))
            if node["sup"]:
                token += "^{%s}" % " ".join(_script_text(node["sup"]))
        out.append(token)
        i += 1
    return out


def equation_latex(page, words, skip_words=()):
    """LaTeX for one display equation, from the words (and their characters and fraction bars) on the page."""
    x0 = min(w["x0"] for w in words) - 3
    x1 = max(w["x1"] for w in words) + 3
    top = min(w["top"] for w in words) - 2
    bottom = max(w["bottom"] for w in words) + 2
    skip = [(w["x0"] - 0.5, w["top"] - 1, w["x1"] + 0.5, w["bottom"] + 1) for w in skip_words]
    chars = []
    for c in page.chars:
        if not (x0 <= (c["x0"] + c["x1"]) / 2 <= x1 and top <= (c["top"] + c["bottom"]) / 2 <= bottom):
            continue
        if any(b[0] <= (c["x0"] + c["x1"]) / 2 <= b[2] and b[1] <= (c["top"] + c["bottom"]) / 2 <= b[3] for b in skip):
            continue
        chars.append(c)
    rules = []
    for obj in list(page.lines) + list(page.rects):
        height = obj["bottom"] - obj["top"]
        width = obj["x1"] - obj["x0"]
        if height < 1.8 and width > 5 and obj["x0"] >= x0 - 2 and obj["x1"] <= x1 + 2 and top - 1 <= obj["top"] <= bottom + 1:
            rules.append({"x0": obj["x0"], "x1": obj["x1"], "top": obj["top"], "bottom": obj["bottom"]})
    items = _items_from_chars(chars)
    sizes = [round(c["size"], 1) for c in page.chars if c["text"].strip()]
    body = max(set(sizes), key=sizes.count) if sizes else None
    return parse_layout(items, rules, body)


def classify(line, body_size):
    """paragraph / heading / equation for one line of words."""
    text = line_text(line)
    chars = [c for w in line for c in w.get("chars", [])]
    size = Counter(round(w.get("size", body_size)) for w in line).most_common(1)[0][0]
    bold = all("bold" in w.get("fontname", "").lower() or w.get("fontname", "").lower().endswith(("b", "bd")) for w in line)
    math_share = (sum(1 for c in chars if _is_math_char(c)) / len(chars)) if chars else 0
    return text, size, bold, math_share


def clean_math(text):
    text = re.sub("[\ufe00-\ufe0f]", "", text)
    text = text.replace("(cid:0)", "(").replace("(cid:1)", ")")
    return re.sub(r"\(cid:\d+\)", "", text)


def margin_lines(page, words, fraction=0.11):
    """Text of the lines in the top and bottom margin of the page (running headers, page numbers)."""
    found = []
    for line in group_lines([w for w in words if w["bottom"] < page.height * fraction or w["top"] > page.height * (1 - fraction)]):
        found.append(" ".join(re.sub(r"\d+", "#", line_text(line)).split()[:4]))
    return found


def page_words(page, skip_boxes=()):
    words = page.extract_words(x_tolerance=1.5, extra_attrs=["fontname", "size", "upright"])
    words = [w for w in words if w.get("upright", True)]

    def inside(w):
        cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
        return any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in skip_boxes)

    return [w for w in words if not inside(w)]


MATH_SYMBOLS = set("=<>≤≥≈≠∑∏∫√∥∈∉⊂⊆∀∃∂∇±×·∝→←⇒⇔∞ˆ˜")


def _line_facts(line):
    text = clean_math(line_text(line)).strip()
    chars = sum(len(w["text"]) for w in line) or 1
    math = sum(len(w["text"]) for w in line if any(h in w["fontname"].lower() for h in MATH_FONT_HINTS))
    sizes = Counter(round(w["size"]) for w in line)
    return {
        "text": text, "words": line,
        "x0": min(w["x0"] for w in line), "x1": max(w["x1"] for w in line),
        "top": min(w["top"] for w in line), "bottom": max(w["bottom"] for w in line),
        "size": sizes.most_common(1)[0][0],
        "share": math / chars,
        "bold": all(any(k in w["fontname"].lower() for k in ("bold", "medi", "black", "heavy")) or w["fontname"].lower().endswith(("tb", "-bd")) for w in line),
    }


def _equation_groups(facts, body_size):
    """Indexes of the lines of each display equation in one column, including sum limits and fractions."""
    if not facts:
        return []
    x0 = min(f["x0"] for f in facts)
    x1 = max(f["x1"] for f in facts)
    width = max(x1 - x0, 1)
    seeds, fragments = [], set()
    relations = set("=<>≤≥≈≠∈∝→⇒⇔")
    for i, f in enumerate(facts):
        text = f["text"]
        words = len(text.split())
        number_word = any(re.fullmatch(r"\(\d{1,3}\)", w["text"]) and w["x0"] > x0 + 0.55 * width for w in f["words"])
        relation = any(ch in relations for ch in text)
        symbols = sum(1 for ch in text if ch in MATH_SYMBOLS)
        left_gap, right_gap = f["x0"] - x0, x1 - f["x1"]
        # the number sits at the right edge, so only the left gap tells whether a numbered line is centred
        centred = left_gap > 0.06 * width if number_word else (left_gap > 0.06 * width and right_gap > 0.06 * width)
        display = len(text) < 140 and words <= 26 and (
            (number_word and (relation or f["share"] > 0.1 or centred))
            or (relation and (f["share"] > 0.3 or centred) and not text.endswith((".", ":")) and len(text) > 5)
            or (f["share"] > 0.5 and len(text) > 25)
        )
        if display:
            seeds.append(i)
        elif len(text) <= 24 and (f["share"] > 0.4 or f["size"] < body_size - 1 or symbols or all(len(w["text"]) <= 2 for w in f["words"])):
            fragments.add(i)
    groups = {i: [i] for i in seeds}
    for i in fragments:
        best, distance = None, None
        for j in seeds:
            gap = (facts[i]["top"] - facts[j]["bottom"]) if i > j else (facts[j]["top"] - facts[i]["bottom"])
            line_height = max(facts[j]["bottom"] - facts[j]["top"], 8)
            horizontal = facts[i]["x1"] > facts[j]["x0"] - 10 and facts[i]["x0"] < facts[j]["x1"] + 10
            if gap < line_height * 1.8 and horizontal and (distance is None or gap < distance):
                best, distance = j, gap
        if best is not None:
            groups[best].append(i)
    return [sorted(members) for members in groups.values()]


def looks_like_prose(text):
    """Three or more long ordinary words in a row of 'equation' text means it is a sentence, not a formula."""
    long_words = [w for w in re.findall(r"[A-Za-z]{5,}", text) if w.lower() not in ("softmax", "argmax", "argmin", "sigma", "lambda", "theta")]
    return len(long_words) >= 3


def formula_to_latex(page, words, number_words):
    """LaTeX for a display formula, or None when the result would be unreliable (the caller then keeps the plain symbols)."""
    try:
        latex = equation_latex(page, words, number_words).strip()
    except Exception:
        return None
    if not latex or not latex.isascii() or latex.count("{") != latex.count("}") or "_{}" in latex or "^{}" in latex:
        return None
    if any(len(name) > 12 for name in re.findall(r"\\operatorname\{([^}]*)\}", latex)):
        return None  # a long upright run is prose that was mistaken for a formula
    if re.search(r"\\frac\{\}|\{\}", latex):
        return None
    if re.search(r"(?:(?<![\\\w])[A-Za-z] ){5,}", latex + " ") or re.search(r"[_^]\{ ?[=+\-]", latex):
        return None  # scattered single letters or a script that starts with an operator: the layout was misread
    return latex


def footer_word_ids(page, words, body_size, drop_margin):
    """Footer text: lone page numbers, running footers repeated on several pages, and the block of small print
    at the very bottom of the page (licence and copyright notes, 'Preprint' lines) that stands apart from the body."""
    ids = set()
    low = page.height * 0.82
    # page numbers and repeated running footers in the bottom margin
    for line in group_lines([w for w in words if w["top"] > page.height * 0.89]):
        text = line_text(line)
        if re.fullmatch(r"(page\s+)?\d{1,4}", text, re.I) or (line and _in_drop(page, line[0], drop_margin, words)):
            ids.update(id(w) for w in line)
    # small print: the lowest lines, smaller than the body, with clear space above them
    candidates = [w for w in words if w["top"] > low and w["size"] <= body_size - 1
                  and not any(h in w["fontname"].lower() for h in MATH_FONT_HINTS)]
    lines = group_lines([w for w in candidates if id(w) not in ids])
    if lines:
        cluster = [lines[-1]]
        for line in reversed(lines[:-1]):
            height = max(w["bottom"] - w["top"] for w in line)
            if min(w["top"] for w in cluster[-1]) - max(w["bottom"] for w in line) <= height * 1.8:
                cluster.append(line)
            else:
                break
        cluster_words = [w for line in cluster for w in line]
        x0, x1 = min(w["x0"] for w in cluster_words), max(w["x1"] for w in cluster_words)
        top = min(w["top"] for w in cluster_words)
        height = max(w["bottom"] - w["top"] for w in cluster_words)
        above = [w for w in words if id(w) not in {id(c) for c in cluster_words} and w["bottom"] <= top + 1
                 and w["x1"] > x0 and w["x0"] < x1]
        nearest = max((w["bottom"] for w in above), default=0)
        if top - nearest >= height * 2.2 and len(cluster_words) >= 3:
            ids.update(id(w) for w in cluster_words)
    return ids


def page_to_markdown(page, words, drop_margin=()):
    margin = page.height * 0.11
    if not words:
        return "", False
    body_size = Counter(round(w["size"]) for w in words).most_common(1)[0][0]
    footer_ids = footer_word_ids(page, words, body_size, drop_margin)
    footer_words = [w for w in words if id(w) in footer_ids]
    # running headers are dropped; footers are kept and printed in italics at the bottom of the page
    header_words = []
    for line in group_lines([w for w in words if w["bottom"] < margin and id(w) not in footer_ids]):
        text = line_text(line)
        if re.fullmatch(r"(page\s+)?\d{1,4}", text, re.I) or _in_drop(page, line[0], drop_margin, words):
            header_words += line
    header_ids = {id(w) for w in header_words}
    header_lines = [line_text(line) for line in group_lines(header_words)] if header_words else []
    words = [w for w in words if id(w) not in footer_ids and id(w) not in header_ids]
    footer_lines = [line_text(line) for line in group_lines(footer_words)] if footer_words else []
    if not words:
        return "", False
    blocks, two_col = column_blocks(page, words)
    out = []

    for lines in blocks:
        facts = [_line_facts(line) for line in lines]
        facts = [f for f in facts if f["text"]]
        groups = _equation_groups(facts, body_size)
        start_of = {g[0]: g for g in groups}
        in_equation = {i for g in groups for i in g}
        paragraph = []

        def end_paragraph():
            nonlocal paragraph
            if paragraph:
                text = paragraph[0]
                for nxt in paragraph[1:]:
                    text = text[:-1] + nxt if (text.endswith("-") and nxt[:1].islower() and len(text) > 2 and text[-2].isalpha()) else text + " " + nxt
                out.append(text)
            paragraph = []

        last_bottom = None
        for i, f in enumerate(facts):
            if i in start_of:
                end_paragraph()
                members = start_of[i]
                column_x0 = min(g["x0"] for g in facts)
                column_width = max(max(g["x1"] for g in facts) - column_x0, 1)
                number, parts, kept_words, number_words = None, [], [], []
                for k in members:
                    kept = []
                    for w in facts[k]["words"]:
                        if re.fullmatch(r"\(\d{1,3}\)", w["text"]) and w["x0"] > column_x0 + 0.55 * column_width and number is None:
                            number = w["text"][1:-1]
                            number_words.append(w)
                        else:
                            kept.append(w)
                    kept_words += kept
                    if kept:
                        parts.append(clean_math(line_text(kept)).strip())
                body = " ".join(p for p in parts if p)
                if looks_like_prose(body):
                    # not a formula after all: ordinary text that happens to contain symbols
                    for p in parts:
                        paragraph.append(p)
                    last_bottom = facts[members[-1]]["bottom"]
                    continue
                latex = formula_to_latex(page, kept_words, number_words)
                out.append(f"$$ {latex or body} $$" + (f"  *({number})*" if number else ""))
                last_bottom = facts[members[-1]]["bottom"]
                continue
            if i in in_equation:
                continue
            text, size, bold = f["text"], f["size"], f["bold"]
            height = f["bottom"] - f["top"]
            gap = (f["top"] - last_bottom) if last_bottom is not None else 0
            if re.match(r"^(Figure|Fig\.|Table|Algorithm)\s*\d+", text):
                end_paragraph()
                out.append(text)
            elif size > body_size + 3 and len(text) < 140:
                end_paragraph()
                mark = "# " if size > body_size + 6 else "## "
                if out and out[-1].startswith(mark) and last_bottom is not None and gap < height * 1.6:
                    out[-1] += " " + text
                else:
                    out.append(mark + text)
            elif re.match(r"^(\d{1,2}[.)]|[•▪◦●\-–])\s+\S", text):
                end_paragraph()
                paragraph.append(text)  # a list item starts its own paragraph
            elif bold and len(text) < 80 and re.match(r"^(\d+(\.\d+)*\s+)?[A-Z]", text):
                end_paragraph()
                mark = "### " if re.match(r"^\d+\.\d+", text) else "## "
                out.append(mark + text)
            else:
                if paragraph and gap > height * 0.9:
                    end_paragraph()
                paragraph.append(text)
            last_bottom = f["bottom"]
        end_paragraph()
    if header_lines:
        out.insert(0, "  \n".join("*" + line.replace("*", "\\*") + "*" for line in header_lines if line))
    if footer_lines:
        out.append("  \n".join("*" + line.replace("*", "\\*") + "*" for line in footer_lines if line))
    return "\n\n".join(out), two_col


def _in_drop(page, word, drop_margin, words):
    line_words = [w for w in words if abs((w["top"] + w["bottom"]) / 2 - (word["top"] + word["bottom"]) / 2) <= 3.5]
    text = " ".join(re.sub(r"\d+", "#", line_text(sorted(line_words, key=lambda w: w["x0"]))).split()[:4])
    return text in drop_margin


def convert_with_markitdown(file_path):
    """Markdown from MarkItDown. For two-column papers MarkItDown mixes the columns and loses word
    spacing, so those PDFs are read column by column instead, with formulas as $$ blocks."""
    from markitdown import MarkItDown
    import pdfplumber

    try:
        with pdfplumber.open(str(file_path)) as pdf:
            pages = pdf.pages[:40]
            page_list = [page_words(p, [tuple(b) for b in find_figure_regions(p)]) for p in pages]
            two_col = sum(1 for p, w in zip(pages, page_list) if find_gutters(w, p.width))
            if two_col and two_col * 2 >= sum(1 for w in page_list if w):
                counts = Counter(t for p, w in zip(pages, page_list) for t in set(margin_lines(p, w)))
                repeated = {t for t, n in counts.items() if n >= 2 and len(pages) > 1}
                parts = [page_to_markdown(p, w, repeated)[0] for p, w in zip(pages, page_list)]
                text = "\n\n".join(part for part in parts if part)
                if text.strip():
                    return text
            else:
                # one column: the reader is only worth using when the paper has display formulas,
                # which plain MarkItDown turns into scrambled symbols
                counts = Counter(t for p, w in zip(pages, page_list) for t in set(margin_lines(p, w)))
                repeated = {t for t, n in counts.items() if n >= 2 and len(pages) > 1}
                parts = [page_to_markdown(p, w, repeated)[0] for p, w in zip(pages, page_list)]
                text = "\n\n".join(part for part in parts if part)
                if text.count("$$") >= 4:
                    return text
    except Exception:
        pass  # fall back to MarkItDown's own output
    return MarkItDown().convert(str(file_path)).text_content


def read_pdf_with_reader(file_path):
    """The column reader's Markdown for the whole PDF (formulas as LaTeX), or '' if it fails."""
    import pdfplumber

    try:
        with pdfplumber.open(str(file_path)) as pdf:
            pages = pdf.pages[:40]
            page_list = [page_words(p, [tuple(b) for b in find_figure_regions(p)]) for p in pages]
            counts = Counter(t for p, w in zip(pages, page_list) for t in set(margin_lines(p, w)))
            repeated = {t for t, n in counts.items() if n >= 2 and len(pages) > 1}
            parts = [page_to_markdown(p, w, repeated)[0] for p, w in zip(pages, page_list)]
            return "\n\n".join(part for part in parts if part)
    except Exception:
        return ""


FORMULA_PLACEHOLDER = re.compile(r"[ \t]*<!--\s*formula-not-decoded\s*-->[ \t]*")
FORMULA_LINE = re.compile(r"(?m)^(\$\$ .*? \$\$.*)$")


def _letters(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def reader_formulas(reader_text):
    """The formulas in the column reader's text, each with the few words just before and just after it.

    Formulas that follow each other directly are one group, placed together.
    Returns a list of (LaTeX blocks, text before, text after), the texts lower-cased letters and digits only."""
    # running headers and footers are italic lines; they are not part of the running text
    reader_text = re.sub(r"(?m)^\*[^\n]*\*\s*$", "", reader_text)
    pieces = FORMULA_LINE.split(reader_text)
    plain = [_letters(piece) for index, piece in enumerate(pieces) if index % 2 == 0]
    blocks = [piece.strip() for index, piece in enumerate(pieces) if index % 2 == 1]
    groups = []
    for i, block in enumerate(blocks):
        if groups and len(plain[i]) < 10:
            groups[-1]["blocks"].append(block)
            groups[-1]["after"] = plain[i + 1][:40]
        else:
            groups.append({"blocks": [block], "before": plain[i][-40:], "after": plain[i + 1][:40]})
    return [("\n\n".join(g["blocks"]), g["before"], g["after"]) for g in groups]


MATH_GARBLE = re.compile(r"[()=/+\-*^_|∑∏√∫<>≤≥≈{}\[\]]")


def find_formula_region(raw, letters, positions, before, after, start_at):
    """Where in Docling's (or pypdf's, or Azure's) text a formula belongs: the stretch between the words
    before it and the words after it. The few letters next to the formula may be its own scrambled
    symbols, so shorter versions of both are tried as well; letters that were skipped that way are only
    removed when they look like scrambled maths, so real words next to the formula are never lost.

    Returns (start, end, next search position), or None."""
    for trim_before in (0, 6, 12, 18):
        context = before[:len(before) - trim_before] if trim_before else before
        context = context[-30:]
        if len(context) < 10:
            break
        a = letters.find(context, start_at)
        if a < 0:
            continue
        a_end = a + len(context)
        start = positions[a_end - 1] + 1
        if trim_before:
            # the skipped letters follow the matched words: keep them unless they look like maths
            skipped_end = min(a_end + trim_before, len(positions)) - 1
            span = raw[start:positions[skipped_end] + 1]
            if not MATH_GARBLE.search(span):
                start = positions[skipped_end] + 1
                while start < len(raw) and raw[start].isalnum():
                    start += 1  # finish the word, so no word is cut in the middle
        for trim_after in (0, 6, 12, 18):
            following = after[trim_after:trim_after + 30]
            if len(following) < 10:
                break
            b = letters.find(following, a_end)
            if b >= 0 and b - a_end <= 250 + trim_before + trim_after:
                end = positions[b]
                if trim_after and b - trim_after >= a_end:
                    span = raw[positions[b - trim_after]:end]
                    if not MATH_GARBLE.search(span):
                        end = positions[b - trim_after]  # the skipped words are real text: keep them
                        while end > start and raw[end - 1].isalnum():
                            end -= 1  # back up to the start of the word, so no word loses its first letters
                if end < start:
                    continue
                return start, end, a_end
    return None


def recognize_docling_formulas(text, file_path):
    """Put a LaTeX formula wherever the paper has one, whatever Docling printed there.

    Docling on the light settings either leaves '<!-- formula-not-decoded -->' or prints the formula as
    scrambled symbols (or skips it). The column reader finds the paper's display formulas as LaTeX, and
    each one is placed by matching the words around it with Docling's own text. Whatever Docling printed
    between those words (marker, scrambled symbols or nothing) is replaced by the formula."""
    reader = read_pdf_with_reader(file_path)
    formulas = reader_formulas(reader) if reader else []
    positions, chars = [], []
    for index, ch in enumerate(text):
        if ch.isalnum() and ch.isascii():
            chars.append(ch.lower())
            positions.append(index)
    letters = "".join(chars)
    edits, cursor = [], 0
    for block, before, after in formulas:
        region = find_formula_region(text, letters, positions, before, after, cursor)
        if region:
            start, end, cursor = region[0], region[1], region[2]
            edits.append((start, end, "\n\n" + block + "\n\n"))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    # any marker that is still left has no formula to take its place
    return FORMULA_PLACEHOLDER.sub("", text)


def _anchor_position(block_letters, reader_letters):
    """Where a block of Docling's text sits in the column reader's text, or -1 when it cannot be found."""
    n = len(block_letters)
    if n < 12:
        return -1
    for start in (0, max(0, n // 2 - 12), max(0, n - 24)):
        piece = block_letters[start:start + 24]
        if len(piece) < 12:
            continue
        found = reader_letters.find(piece)
        if found >= 0:
            return found - start
    return -1


def order_docling_like_reader(text, reader):
    """Put Docling's blocks in the column reader's order (left column, then right column), put each
    heading where the reader has it, list every author on a line of their own, and italicize the page
    headers and footers.

    Docling sometimes reads the right column before the left one, misplaces headings (Abstract,
    Introduction) and runs the authors together in one paragraph. The column reader reads the same page
    by its true layout, so each Docling block is found in the reader's text and the blocks are sorted by
    where they sit there."""
    if not reader or not text.strip():
        return text
    italic_words = set()
    for line in re.findall(r"(?m)^\*([^\n]*)\*\s*$", reader):
        italic_words.update(w.lower() for w in re.findall(r"[A-Za-z0-9]{3,}", line))
    body = re.sub(r"(?m)^\*[^\n]*\*\s*$", "", reader)
    reader_blocks = [b for b in re.split(r"\n\s*\n", body) if b.strip()]
    reader_letters, exact, offset = "", {}, 0
    for block in reader_blocks:
        letters = _letters(block)
        exact.setdefault(letters, offset)
        reader_letters += letters
        offset += len(letters)

    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    keys, last = [], 0.0
    for index, block in enumerate(blocks):
        letters = _letters(block)
        if (block.lstrip().startswith("#") or len(letters) < 12) and letters in exact:
            pos = exact[letters]  # a heading goes where the reader has the same heading
        else:
            pos = _anchor_position(letters, reader_letters)
        if pos >= 0:
            last = float(pos)
            keys.append((last, 0, index))
        else:
            keys.append((last, 1, index))  # no match: stays right behind the block before it
    blocks = [blocks[k[2]] for k in sorted(keys)]

    # authors: one paragraph each, as the reader has them, right under the title
    author_blocks = []
    for block in reader_blocks:
        if re.match(r"(?i)\s*(#+\s*)?(\*\*)?abstract", block):
            break
        if not block.lstrip().startswith("#"):
            author_blocks.append(block.strip())
        if len(author_blocks) > 12:
            break
    else:
        author_blocks = []
    if author_blocks and len(author_blocks) <= 12:
        blob = "".join(_letters(b) for b in author_blocks)
        blocks = [b for b in blocks if not (6 <= len(_letters(b)) and _letters(b) in blob)]
        title_at = next((i for i, b in enumerate(blocks) if b.lstrip().startswith("#")), -1)
        blocks[title_at + 1:title_at + 1] = author_blocks

    # running headers and footers: short blocks whose words all come from the reader's italic lines
    if italic_words:
        for i, block in enumerate(blocks):
            plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", block)
            words = [w.lower() for w in re.findall(r"[A-Za-z0-9]{3,}", plain)]
            if (block.lstrip().startswith(("#", "!", "$", "*", "|", "-")) or len(words) < 2 or len(words) > 40):
                continue
            if sum(w in italic_words for w in words) >= 0.9 * len(words):
                blocks[i] = "*" + block.strip().replace("\n", " ") + "*"

    # Docling drops running headers and footers on most pages; the reader has them, so the ones Docling
    # left out are put back, each at the top (header) or bottom (footer) of its page
    def words_of(line):
        return {w.lower() for w in re.findall(r"[A-Za-z0-9]{3,}", re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line))}

    have = [(i, words_of(b)) for i, b in enumerate(blocks)
            if len(b) > 2 and b.startswith("*") and b.endswith("*") and not b.startswith("**")]
    used = set()
    dl_letters = [_letters(b) for b in blocks]
    inserts, pending = {}, []
    for block in re.split(r"\n\s*\n", reader):
        if not block.strip():
            continue
        if block.lstrip().startswith("*") and not block.lstrip().startswith("**"):
            for line in block.splitlines():
                line = line.strip()
                if len(line) < 4 or not (line.startswith("*") and line.rstrip("*") != line):
                    continue
                words = words_of(line)
                match = None
                for i, w in have:
                    if i in used or not w or not words:
                        continue
                    if len(w & words) / len(w | words) >= 0.8:
                        match = i
                        break
                if match is not None:
                    used.add(match)
                else:
                    pending.append(line)
            continue
        if pending:
            letters = _letters(block)
            if len(letters) >= 12:
                at = next((i for i, d in enumerate(dl_letters) if letters[:24] in d), None)
                if at is not None:
                    inserts.setdefault(at, []).extend(pending)
                    pending = []
    if inserts or pending:
        result = []
        for i, b in enumerate(blocks):
            result.extend(inserts.get(i, []))
            result.append(b)
        result.extend(pending)
        blocks = result
    return "\n\n".join(blocks)


def convert_pdf(file_path, tool):
    if tool == "docling":
        text = convert_with_docling(file_path)
        # Docling leaves "<!-- image -->" where a picture was; the real image links are placed separately
        text = re.sub(r"[ \t]*<!--\s*image\s*-->[ \t]*\n?", "", text)
        try:
            text = order_docling_like_reader(text, read_pdf_with_reader(file_path))
        except Exception:
            pass  # keep Docling's own order
        text = recognize_docling_formulas(text, file_path)
        return re.sub(r"\n{3,}", "\n\n", text)
    elif tool == "markitdown":
        return convert_with_markitdown(file_path)
    elif tool == "pypdf":
        return convert_with_pypdf(file_path)
    elif tool == "pdfplumber":
        return convert_with_pdfplumber(file_path)
    elif tool == "azure":
        return convert_with_azure(file_path)
    elif tool == "textract":
        return convert_with_textract(file_path)
    else:
        raise ValueError("tool must be one of: " + ", ".join(PDF_TOOLS))


def image_link(key):
    name = key.rsplit("/", 1)[-1]
    return f"![{name}](s3://{BUCKET}/{key})"


def add_image_links(markdown_text, image_keys):
    """Append links for images that could not be placed next to their figure, at the end of the Markdown."""
    if not image_keys:
        return markdown_text
    lines = ["", "## Extracted images", ""]
    for key in image_keys:
        lines.append(image_link(key))
        lines.append("")
    return markdown_text.rstrip() + "\n" + "\n".join(lines)


def place_images(markdown_text, places):
    """Put each image link where its figure is in the Markdown.

    A figure is found by its "Figure N:" caption and its images go just above it, where the picture
    sits on the page. A picture without a caption goes at the end of its page when the Markdown has
    "## Page N" sections. Anything that cannot be placed is listed at the end.
    """
    if not places:
        return markdown_text

    page_marks = [(int(m.group(1)), m.start()) for m in re.finditer(r"(?m)^## Page (\d+)\s*$", markdown_text)]

    def page_span(page):
        for n, (number, start) in enumerate(page_marks):
            if number == page:
                end = page_marks[n + 1][1] if n + 1 < len(page_marks) else len(markdown_text)
                return start, end
        return None

    groups = {}  # figure label -> {"page": ..., "keys": [...]}
    loose = []   # images without a caption
    for place in places:
        if place.get("label"):
            group = groups.setdefault(place["label"], {"page": place["page"], "keys": []})
            group["keys"].append(place["key"])
        else:
            loose.append(place)

    inserts = []  # (position, text)
    unplaced = []

    for label, group in groups.items():
        number = label.split()[-1]
        pattern = re.compile(r"(?m)^[ \t>*_#]{0,6}(?:Figure|Fig\.)[ \t]*" + number + r"[ \t]*[:.]")
        span = page_span(group["page"])
        match = pattern.search(markdown_text, span[0], span[1]) if span else pattern.search(markdown_text)
        block = "\n\n".join(image_link(k) for k in group["keys"])
        if match:
            inserts.append((match.start(), block + "\n\n"))
        elif span:
            inserts.append((span[1], "\n\n" + block + "\n\n"))
        else:
            unplaced.extend(group["keys"])

    by_page = {}
    for place in loose:
        by_page.setdefault(place["page"], []).append(place["key"])
    for page, keys in by_page.items():
        span = page_span(page)
        if span:
            inserts.append((span[1], "\n\n" + "\n\n".join(image_link(k) for k in keys) + "\n\n"))
        else:
            unplaced.extend(keys)

    for position, text in sorted(inserts, key=lambda item: item[0], reverse=True):
        markdown_text = markdown_text[:position] + text + markdown_text[position:]
    markdown_text = re.sub(r"\n{3,}", "\n\n", markdown_text)
    return add_image_links(markdown_text, unplaced)


def figure_label(page, box):
    """The label of the figure in this box ("Figure 2"), from the caption just below it."""
    best = None
    pattern = re.compile(r"^(?:Figure|Fig\.)\s*(\d+)\s*[:.]", re.IGNORECASE)
    candidates = [(line["x0"], line["x1"], line["top"], line["text"].strip()) for line in page.extract_text_lines()]
    words = page.extract_words()
    for i, w in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else None
        text = w["text"]
        if nxt is not None and abs(nxt["top"] - w["top"]) < 3 and re.fullmatch(r"(Figure|Fig\.)", text, re.IGNORECASE):
            text += nxt["text"]
        candidates.append((w["x0"], w["x1"], w["top"], text))
    for x0, x1, top, text in candidates:
        found = pattern.match(text)
        if not found or x1 < box[0] - 5 or x0 > box[2] + 5:
            continue
        distance = top - box[3]
        if -10 <= distance <= 90 and (best is None or distance < best[0]):
            best = (distance, f"Figure {found.group(1)}")
    return best[1] if best else None


def upload_result(doc_name, tool, markdown_text):
    key = f"processed/markdown/{tool}/api-uploads/{doc_name}.md"
    put_object(
        key,
        markdown_text.encode("utf-8"),
        "text/markdown",
        {"tool": tool, "source": "api-upload", "type": "markdown", "document": doc_name},
    )
    return key


def merge_boxes(boxes, gap):
    """Merge boxes that touch or sit within `gap` points of each other."""
    clusters = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        merged = []
        while clusters:
            current = clusters.pop()
            i = 0
            while i < len(clusters):
                other = clusters[i]
                near = (
                    current[0] - gap <= other[2] and other[0] - gap <= current[2]
                    and current[1] - gap <= other[3] and other[1] - gap <= current[3]
                )
                if near:
                    current = [
                        min(current[0], other[0]), min(current[1], other[1]),
                        max(current[2], other[2]), max(current[3], other[3]),
                    ]
                    clusters.pop(i)
                    changed = True
                    i = 0
                else:
                    i += 1
            merged.append(current)
        clusters = merged
    return clusters


def find_figure_regions(page):
    """Find figures on a page, including ones drawn with vector graphics.

    Papers usually put the caption ("Figure 2: ...") under the figure, so each caption
    is used to locate the drawing just above it. Returns boxes as (x0, top, x1, bottom).
    """
    captions = [
        line for line in page.extract_text_lines()
        if FIGURE_CAPTION.match(line["text"].strip())
    ]
    if not captions:
        return []

    page_area = page.width * page.height
    boxes = []
    for kind in ("curve", "line", "rect", "image"):
        for obj in page.objects.get(kind, []):
            width = obj["x1"] - obj["x0"]
            height = obj["bottom"] - obj["top"]
            if width * height > 0.7 * page_area:
                continue  # page background
            boxes.append((obj["x0"], obj["top"], obj["x1"], obj["bottom"]))
    if not boxes or len(boxes) > 3000:
        return []

    words = page.extract_words()
    regions = []
    for caption in captions:
        above = [b for b in boxes if b[3] <= caption["top"] + 2]
        candidates = [
            c for c in merge_boxes(above, gap=20)
            if caption["top"] - 80 <= c[3] <= caption["top"] + 3
            and (c[2] - c[0]) >= 80 and (c[3] - c[1]) >= 50
        ]
        if not candidates:
            continue
        x0, top, x1, bottom = max(candidates, key=lambda c: (c[2] - c[0]) * (c[3] - c[1]))

        # Pull in the labels that sit on or around the drawing
        for w in words:
            cx = (w["x0"] + w["x1"]) / 2
            cy = (w["top"] + w["bottom"]) / 2
            if x0 - 10 <= cx <= x1 + 10 and top - 10 <= cy <= bottom + 10 and w["bottom"] <= caption["top"]:
                x0, top = min(x0, w["x0"]), min(top, w["top"])
                x1, bottom = max(x1, w["x1"]), max(bottom, w["bottom"])

        pad = 6
        regions.append((
            max(x0 - pad, page.bbox[0]),
            max(top - pad, page.bbox[1]),
            min(x1 + pad, page.bbox[2]),
            min(bottom + pad, caption["top"] - 1),
        ))
    return regions


def split_unlabeled_panels(page, box):
    """Panels with no (a), (b) labels: split the figure where a clear empty vertical strip runs through all of it.

    Returns a list of (letter, box), or [] when the figure is one piece."""
    x0, top, x1, bottom = box
    width = x1 - x0
    spans = []
    for kind in ("rect", "curve", "line", "image", "char"):
        for obj in page.objects.get(kind, []):
            if obj["bottom"] >= top and obj["top"] <= bottom and obj["x1"] > x0 and obj["x0"] < x1:
                if obj["x1"] - obj["x0"] > 0.6 * width:
                    continue  # a frame or background behind the whole figure
                spans.append((max(obj["x0"], x0), min(obj["x1"], x1)))
    if len(spans) < 10:
        return split_raster_panels(page, box)
    spans.sort()
    gaps, edge = [], x0
    for s0, s1 in spans:
        if s0 > edge:
            gaps.append((edge, s0))
        edge = max(edge, s1)
    # a split needs a real gap with a substantial drawing on both sides
    cuts = [(g1 - g0, (g0 + g1) / 2) for g0, g1 in gaps
            if g1 - g0 >= 8 and (g0 - x0) >= 0.25 * width and (x1 - g1) >= 0.25 * width]
    if not cuts:
        return split_raster_panels(page, box)
    cut = max(cuts)[1]
    return [("a", (x0, top, cut, bottom)), ("b", (cut, top, x1, bottom))]


def split_raster_panels(page, box):
    """A figure stored as one picture that holds two plots side by side: look for a clear white vertical strip."""
    try:
        x0, top, x1, bottom = box
        scale = 100 / 72
        image = page.to_image(resolution=100).original.convert("L")
        crop = image.crop((int(x0 * scale), int(top * scale), int(x1 * scale), int(bottom * scale)))
        width, height = crop.size
        if width < 200 or height < 60:
            return []
        pixels = crop.load()
        rows = range(0, height, 2)
        white = []
        for x in range(width):
            dark = sum(1 for y in rows if pixels[x, y] < 235)
            white.append(dark <= 0.01 * len(rows))
        best, start = None, None
        for x, is_white in enumerate(white + [False]):
            if is_white and start is None:
                start = x
            elif not is_white and start is not None:
                run = x - start
                if run >= 5 and start >= 0.25 * width and x <= 0.75 * width and (best is None or run > best[0]):
                    best = (run, (start + x) / 2)
                start = None
        if not best:
            return []
        cut = x0 + best[1] / scale
        return [("a", (x0, top, cut, bottom)), ("b", (cut, top, x1, bottom))]
    except Exception:
        return []


def split_panels(page, box):
    """Split a figure into its labelled panels, such as (a), (b), (c) printed under it.

    Returns a list of (letter, box). Returns an empty list if fewer than two labels are found.
    """
    x0, top, x1, bottom = box
    labels = []
    for w in page.extract_words():
        match = PANEL_LABEL.match(w["text"].strip())
        cx = (w["x0"] + w["x1"]) / 2
        cy = (w["top"] + w["bottom"]) / 2
        if match and x0 <= cx <= x1 and bottom - 30 <= cy <= bottom + 25:
            labels.append((cx, match.group(1).lower(), w["bottom"]))
    labels.sort()
    if len(labels) < 2 or len({letter for _, letter, _ in labels}) != len(labels):
        return split_unlabeled_panels(page, box)

    # Include the (a), (b), (c) labels themselves at the bottom of each panel
    bottom = max(bottom, max(label_bottom for _, _, label_bottom in labels) + 4)
    # Cut each pair of panels in the widest empty gap between their labels
    spans = []
    for kind in ("rect", "curve", "line", "image", "char"):
        for obj in page.objects.get(kind, []):
            if obj["bottom"] >= top and obj["top"] <= bottom and obj["x1"] > x0 and obj["x0"] < x1:
                if obj["x1"] - obj["x0"] > 0.9 * (x1 - x0):
                    continue  # a box behind the whole figure does not separate panels
                spans.append((obj["x0"], obj["x1"]))
    spans.sort()

    # Panels often sit on their own coloured box, so a large box that starts between two
    # labels marks where the next panel begins
    backgrounds = []
    for kind in ("rect", "curve"):
        for obj in page.objects.get(kind, []):
            width, height = obj["x1"] - obj["x0"], obj["bottom"] - obj["top"]
            if 0.1 * (x1 - x0) < width <= 0.9 * (x1 - x0) and height > 0.5 * (bottom - top) \
                    and obj["top"] >= top - 5 and obj["bottom"] <= bottom + 5:
                backgrounds.append(obj["x0"])

    def best_cut(left, right):
        starts = [b for b in backgrounds if left < b < right]
        if starts:
            return min(starts) - 1.5
        gaps, edge = [], left
        for s0, s1 in spans:
            if s1 <= left or s0 >= right:
                continue
            if s0 > edge:
                gaps.append((s0 - edge, (edge + s0) / 2))
            edge = max(edge, s1)
        if right > edge:
            gaps.append((right - edge, (edge + right) / 2))
        widest = max(gaps) if gaps else None
        return widest[1] if widest and widest[0] >= 4 else (left + right) / 2

    edges = [x0] + [best_cut(labels[i][0], labels[i + 1][0]) for i in range(len(labels) - 1)] + [x1]
    return [(letter, (edges[i], top, edges[i + 1], bottom)) for i, (_, letter, _) in enumerate(labels)]


def find_caption_tables(page, max_rule_gap=70):
    """Find tables that have no ruled grid, using their "Table N:" caption and horizontal rules.

    Papers usually draw only a few horizontal lines (top, header, bottom) and put the caption
    just above. The table is the area from the caption down to the last of those lines.
    Returns a list of boxes as (x0, top, x1, bottom).
    """
    captions = [
        line for line in page.extract_text_lines()
        if TABLE_CAPTION.match(line["text"].strip())
    ]
    rules = sorted(
        (
            line for line in page.objects.get("line", [])
            if abs(line["bottom"] - line["top"]) < 2 and line["x1"] - line["x0"] > 40
        ),
        key=lambda line: line["top"],
    )
    boxes = []
    for i, caption in enumerate(captions):
        limit = captions[i + 1]["top"] if i + 1 < len(captions) else page.height
        below = [r for r in rules if caption["bottom"] - 6 <= r["top"] < limit - 2]
        if not below or below[0]["top"] - caption["bottom"] > 45:
            continue
        group = [below[0]]
        for rule in below[1:]:
            if rule["top"] - group[-1]["top"] <= max_rule_gap:
                group.append(rule)
            else:
                break
        if len(group) < 2:
            continue
        boxes.append((
            max(min(r["x0"] for r in group) - 2, page.bbox[0]),
            group[0]["top"] - 2,
            min(max(r["x1"] for r in group) + 2, page.bbox[2]),
            group[-1]["top"] + 2,
        ))
    return boxes


def clean_rows(rows):
    """Remove empty rows and empty columns from a table."""
    rows = [[(cell or "").strip() for cell in row] for row in rows]
    rows = [row for row in rows if any(row)]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    keep = [c for c in range(width) if any(row[c] for row in rows)]
    return [[row[c] for c in keep] for row in rows]


def overlap_share(box, other):
    """How much of `box` is covered by `other` (0 to 1)."""
    w = min(box[2], other[2]) - max(box[0], other[0])
    h = min(box[3], other[3]) - max(box[1], other[1])
    if w <= 0 or h <= 0:
        return 0
    return (w * h) / ((box[2] - box[0]) * (box[3] - box[1]))


def extract_page_tables(page, figures):
    """Return the real tables on a page as lists of rows.

    First the tables with a ruled grid, then tables found from their caption and rules.
    """
    found = []
    boxes = []
    for table in page.find_tables():
        rows = table.extract()
        if looks_like_table(table.bbox, rows, figures):
            found.append(clean_rows(rows))
            boxes.append(table.bbox)

    for box in find_caption_tables(page):
        if any(overlap_share(box, b) > 0.3 for b in boxes):
            continue  # already found as a ruled table
        if any(overlap_share(box, f) > 0.5 for f in figures):
            continue
        rows = page.within_bbox(box).extract_table({
            "vertical_strategy": "text",
            "horizontal_strategy": "text",
            "text_x_tolerance": 3,
        })
        rows = clean_rows(rows or [])
        if looks_like_table(box, rows, figures):
            found.append(rows)
            boxes.append(box)
    return found


def looks_like_table(bbox, rows, figures):
    """Reject boxes that pdfplumber found but that are not real tables.

    A real table has at least two rows and two columns with most cells filled in, and it does
    not sit inside a figure (such as the number grids drawn in a diagram).
    """
    if not rows or len(rows) < 2:
        return False
    columns = max(len(row) for row in rows)
    if columns < 2:
        return False
    cells = [cell for row in rows for cell in row]
    filled = sum(1 for cell in cells if cell and cell.strip())
    if filled < 4 or filled / len(cells) < 0.5:
        return False
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    if any(f[0] <= cx <= f[2] and f[1] <= cy <= f[3] for f in figures):
        return False
    return True


def extract_and_upload_pdf_parts(pdf_path, doc_name, today):
    """Pull text, tables and images out of a PDF and upload each one to S3 on its own."""
    import pdfplumber

    pages_text = []
    table_summaries = []
    image_keys = []
    image_places = []
    table_rows = []
    warnings = []
    table_meta = {"source": "api-upload", "document": doc_name, "date": today}

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            pages_text.append({"page": page_num, "text": page.extract_text() or ""})

            # Figures are found first, so numbers inside a figure are not mistaken for a table
            try:
                figures = find_figure_regions(page)
            except Exception as e:
                figures = []
                warnings.append(f"Could not look for figures on page {page_num}: {e}")

            # Each real table becomes its own CSV file
            for kept, rows in enumerate(extract_page_tables(page, figures)):
                name = f"page{page_num}_table{kept}"
                key = f"processed/extracted/pdf/{doc_name}/tables/{name}.csv"
                table_summaries.append(upload_table(key, name, rows, table_meta))
                table_rows.append(rows)

            # Images: figures (including vector drawings) first, then embedded pictures
            if len(image_keys) >= MAX_IMAGES:
                continue

            pictures = []
            for img_idx, img in enumerate(page.images):
                # Clamp to the page so images that overhang the edge are still saved.
                box = (
                    max(img["x0"], page.bbox[0]), max(img["top"], page.bbox[1]),
                    min(img["x1"], page.bbox[2]), min(img["bottom"], page.bbox[3]),
                )
                if box[2] - box[0] < 20 or box[3] - box[1] < 20:
                    continue
                cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                inside_figure = any(f[0] <= cx <= f[2] and f[1] <= cy <= f[3] for f in figures)
                if not inside_figure:  # pictures inside a saved figure are already part of it
                    pictures.append((f"page{page_num}_img{img_idx}", box))

            to_save = []
            for i, box in enumerate(figures):
                try:
                    label = figure_label(page, box)
                except Exception:
                    label = None
                to_save.append((f"page{page_num}_figure{i}", box, label))
                # Also save each labelled panel (a), (b), (c) as its own image
                for letter, panel_box in split_panels(page, box):
                    to_save.append((f"page{page_num}_figure{i}_{letter}", panel_box, label))
            to_save += [(name, box, None) for name, box in pictures]
            if not to_save:
                continue

            try:
                page_image = page.to_image(resolution=RENDER_DPI).original
            except Exception as e:
                warnings.append(f"Could not render page {page_num} to save its images: {e}")
                continue
            scale = RENDER_DPI / 72
            for name, box, label in to_save:
                if len(image_keys) >= MAX_IMAGES:
                    break
                try:
                    crop = page_image.crop((
                        int((box[0] - page.bbox[0]) * scale), int((box[1] - page.bbox[1]) * scale),
                        int((box[2] - page.bbox[0]) * scale), int((box[3] - page.bbox[1]) * scale),
                    ))
                    buffer = io.BytesIO()
                    crop.save(buffer, format="PNG")
                    key = f"assets/images/api-uploads/{doc_name}/{name}.png"
                    put_object(
                        key,
                        buffer.getvalue(),
                        "image/png",
                        {"source": "api-upload", "document": doc_name, "date": today, "type": "image"},
                    )
                    image_keys.append(key)
                    image_places.append({"key": key, "page": page_num, "label": label})
                except Exception as e:
                    warnings.append(f"Skipped {name}: {e}")

    text_key = put_json(
        f"processed/extracted/pdf/{doc_name}/text.json",
        pages_text,
        {"source": "api-upload", "document": doc_name, "date": today, "type": "text"},
    )

    parts = {
        "text": text_key,
        "table_count": len(table_summaries),
        "table_keys": [t["key"] for t in table_summaries],
        "image_count": len(image_keys),
        "image_keys": image_keys,
    }
    parts["image_places"] = image_places
    parts["table_rows"] = table_rows
    return parts, table_summaries, warnings


@app.get("/")
def root():
    return {"status": "ok", "message": "Ingestion pipeline API is running"}


def run_pdf_pipeline(tmp_path, filename, tool, extra=None):
    """Upload a PDF, extract its parts and convert it to Markdown. Used for uploads and PDF links."""
    doc_name = safe_name(Path(filename).stem)
    today = date.today().isoformat()

    # 1. Original PDF
    original_key = f"raw/pdf/api-uploads/{today}/{doc_name}.pdf"
    s3.upload_file(
        str(tmp_path),
        BUCKET,
        original_key,
        ExtraArgs={
            "ContentType": "application/pdf",
            "Metadata": {"source": "api-upload", "document": doc_name, "date": today, "type": "pdf"},
        },
    )

    # 2. Text, tables and images as separate files
    uploaded = {"original_pdf": original_key}
    table_summaries = []
    warnings = []
    image_keys = []
    image_places = []
    table_rows = []
    try:
        parts, table_summaries, warnings = extract_and_upload_pdf_parts(tmp_path, doc_name, today)
        image_places = parts.pop("image_places", [])
        table_rows = parts.pop("table_rows", [])
        uploaded.update(parts)
        image_keys = parts["image_keys"]
    except Exception as e:
        warnings.append(f"Text, table and image extraction failed: {e}")

    # 3. Whole document as Markdown, with links to the images saved in S3
    try:
        converted = convert_pdf(tmp_path, tool)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{tool} could not convert this PDF: {e}")
    if tool == "markitdown":
        converted = fix_markdown_tables(converted, table_rows)
    markdown_text = place_images(converted, image_places)
    markdown_key = upload_result(doc_name, tool, markdown_text)
    uploaded["markdown"] = markdown_key

    return {
        **(extra or {}),
        "filename": filename,
        "tool": tool,
        "s3_key": markdown_key,
        "s3_url": f"s3://{BUCKET}/{markdown_key}",
        "markdown_preview": markdown_text[:1000],
        "markdown_text": markdown_text,
        "markdown_length": len(markdown_text),
        "uploaded": uploaded,
        "tables": table_summaries,
        "warnings": warnings,
    }


@app.post("/process-pdf")
def process_pdf(file: UploadFile = File(...), tool: str = Form("docling")):
    if tool not in PDF_TOOLS:
        raise HTTPException(status_code=400, detail="tool must be one of: " + ", ".join(PDF_TOOLS))

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)
    try:
        return run_pdf_pipeline(tmp_path, file.filename, tool)
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/process-url")
def process_url(url: str = Form(...), tool: str = Form("beautifulsoup")):
    if tool not in URL_TOOLS:
        raise HTTPException(status_code=400, detail="tool must be one of: " + ", ".join(URL_TOOLS))
    today = date.today().isoformat()

    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch the URL: {e}")

    # A link to a PDF file goes through the PDF pipeline with the chosen tool
    is_pdf = (
        "pdf" in response.headers.get("Content-Type", "").lower()
        or urlparse(url).path.lower().endswith(".pdf")
        or response.content[:5] == b"%PDF-"
    )
    if is_pdf:
        if tool == "beautifulsoup":
            raise HTTPException(
                status_code=400,
                detail="This link is a PDF. BeautifulSoup reads web pages, so choose Azure, pypdf or pdfplumber.",
            )
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(response.content)
            tmp_path = Path(tmp.name)
        try:
            file_name = Path(urlparse(url).path).name or "document.pdf"
            return run_pdf_pipeline(tmp_path, file_name, tool, extra={"url": url})
        finally:
            tmp_path.unlink(missing_ok=True)

    if tool in ("pypdf", "pdfplumber"):
        raise HTTPException(
            status_code=400,
            detail=f"{tool} reads PDF files, and this link is a web page. Choose BeautifulSoup or Azure, or use a link to a PDF.",
        )

    soup = BeautifulSoup(response.text, "lxml")
    doc_name = safe_name(urlparse(url).path) if urlparse(url).path.strip("/") else "page"

    # Text
    content = soup.find(id="mw-content-text") or soup.body
    text_elements = []
    lines = []
    for tag in content.find_all(["h1", "h2", "h3", "p"]):
        text = tag.get_text(strip=True)
        if text:
            text_elements.append({"tag": tag.name, "text": text})
            prefix = "#" * int(tag.name[1]) if tag.name.startswith("h") else ""
            lines.append(f"{prefix} {text}" if prefix else text)
    markdown_text = "\n\n".join(lines)

    meta = {"source": "api-upload-url", "document": doc_name, "date": today}

    if tool == "azure":
        try:
            markdown_text = azure_markdown(response.content, content_type="text/html")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"azure could not convert this page: {e}")
        markdown_key = f"processed/markdown/azure/web/{doc_name}.md"
    else:
        markdown_key = f"processed/markdown/web/api-uploads/{doc_name}.md"
    text_key = put_json(f"raw/web/{doc_name}/{today}/text.json", text_elements, {**meta, "type": "text"})

    # Tables: each one becomes its own CSV file
    table_summaries = []
    for t_idx, table in enumerate(soup.find_all("table")):
        rows = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if cells:
                rows.append(cells)
        if rows:
            name = f"table_{t_idx}"
            key = f"raw/web/{doc_name}/{today}/tables/{name}.csv"
            table_summaries.append(upload_table(key, name, rows, meta))

    # Images (the first few real ones, skipping tiny icons)
    image_keys = []
    warnings = []
    for img_idx, img in enumerate(soup.find_all("img")):
        if len(image_keys) >= MAX_IMAGES:
            break
        src = img.get("src")
        if not src or src.startswith("data:"):
            continue
        try:
            width = int(img.get("width") or 0)
            height = int(img.get("height") or 0)
        except ValueError:
            width = height = 0
        if (width and width < 50) or (height and height < 50):
            continue
        full_url = urljoin(url, src)
        try:
            img_response = requests.get(full_url, headers=HEADERS, timeout=10)
            img_response.raise_for_status()
            ext = os.path.splitext(urlparse(full_url).path)[1] or ".jpg"
            key = f"assets/images/web/{doc_name}/img{img_idx}{ext}"
            put_object(
                key,
                img_response.content,
                img_response.headers.get("Content-Type", "application/octet-stream"),
                {**meta, "type": "image"},
            )
            image_keys.append(key)
        except Exception as e:
            warnings.append(f"Skipped an image ({full_url}): {e}")

    # Markdown is uploaded last so it can link to the images saved above
    markdown_text = add_image_links(markdown_text, image_keys)
    put_object(markdown_key, markdown_text.encode("utf-8"), "text/markdown", {**meta, "type": "markdown"})

    return {
        "url": url,
        "tool": tool,
        "s3_key": markdown_key,
        "s3_url": f"s3://{BUCKET}/{markdown_key}",
        "markdown_preview": markdown_text[:1000],
        "markdown_text": markdown_text,
        "markdown_length": len(markdown_text),
        "uploaded": {
            "markdown": markdown_key,
            "text": text_key,
            "table_count": len(table_summaries),
            "table_keys": [t["key"] for t in table_summaries],
            "image_count": len(image_keys),
            "image_keys": image_keys,
        },
        "tables": table_summaries,
        "warnings": warnings,
    }
