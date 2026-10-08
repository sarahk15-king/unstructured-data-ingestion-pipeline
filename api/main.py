import csv
import io
import json
import os
import re
import shutil
import tempfile
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


PDF_TOOLS = ("docling", "markitdown", "pypdf", "pdfplumber", "azure")
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
    """Replace tables that MarkItDown got wrong with the tables found by pdfplumber.

    MarkItDown sometimes merges columns ("Quarter North") or leaves a table as loose lines. For each
    pdfplumber table, find the run of lines whose words match the table's words, in order, and put a
    clean Markdown table there.
    """
    def words(text):
        return re.sub(r"[|]|(?<!\w)-{3,}(?!\w)", " ", text).split()

    lines = markdown_text.split("\n")
    for rows in tables:
        target = [w for row in rows for cell in row for w in (cell or "").split()]
        if not target:
            continue
        found = None
        for start in range(len(lines)):
            if not words(lines[start]) or words(lines[start])[0] != target[0]:
                continue
            got = []
            for end in range(start, min(len(lines), start + len(rows) * 3 + 6)):
                got += words(lines[end])
                if got == target:
                    found = (start, end + 1)
                    break
                if len(got) >= len(target) or got != target[:len(got)]:
                    break
            if found:
                break
        if found:
            lines[found[0]:found[1]] = ["", rows_to_markdown(rows), ""]
    return "\n".join(lines)


def convert_with_pypdf(file_path):
    """Text from pypdf, one section per page. pdfplumber only finds the tables, which are shown as Markdown tables."""
    import pdfplumber
    from pypdf import PdfReader

    reader = PdfReader(str(file_path))
    parts = []
    with pdfplumber.open(str(file_path)) as pdf:
        for page_num, (page, plumber_page) in enumerate(zip(reader.pages, pdf.pages), start=1):
            text = (page.extract_text() or "").strip()
            try:
                tables = extract_page_tables(plumber_page, find_figure_regions(plumber_page))
                text = put_tables_in_text(text, tables)
            except Exception:
                pass  # keep the page text even if table detection fails
            parts.append(f"## Page {page_num}\n\n{text}")
    return "\n\n".join(parts)


def convert_with_pdfplumber(file_path):
    """Text from pdfplumber, with each table shown as a Markdown table."""
    import pdfplumber

    parts = []
    with pdfplumber.open(str(file_path)) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = (page.extract_text() or "").strip()
            try:
                tables = extract_page_tables(page, find_figure_regions(page))
                text = put_tables_in_text(text, tables)
            except Exception:
                pass  # keep the page text even if table detection fails
            parts.append(f"## Page {page_num}\n\n{text}")
    return "\n\n".join(parts)


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
        caption = table.find("caption")
        title = f"{caption.get_text(' ', strip=True)}\n\n" if caption else ""
        return title + rows_to_markdown(rows)

    return re.sub(r"<table\b.*?</table>", convert, text, flags=re.IGNORECASE | re.DOTALL)


def clean_azure_markdown(text):
    """Tidy Azure's Markdown: HTML tables become Markdown tables, figures keep only their caption
    (not the chart's axis labels), and the page comments are removed."""
    def figure(match):
        caption = re.search(r"<figcaption>(.*?)</figcaption>", match.group(0), flags=re.IGNORECASE | re.DOTALL)
        return " ".join(caption.group(1).split()) if caption else ""

    text = re.sub(r"<figure\b.*?</figure>", figure, text, flags=re.IGNORECASE | re.DOTALL)
    text = html_tables_to_markdown(text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


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
    if total_pages > AZURE_MAX_PAGES:
        markdown += f"\n\n> Only the first {AZURE_MAX_PAGES} of {total_pages} pages were sent to Azure."
    return markdown


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


def convert_pdf(file_path, tool):
    if tool == "docling":
        return convert_with_docling(file_path)
    elif tool == "markitdown":
        from markitdown import MarkItDown
        converter = MarkItDown()
        result = converter.convert(str(file_path))
        return result.text_content
    elif tool == "pypdf":
        return convert_with_pypdf(file_path)
    elif tool == "pdfplumber":
        return convert_with_pdfplumber(file_path)
    elif tool == "azure":
        return convert_with_azure(file_path)
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
        return []

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
        below = [r for r in rules if caption["bottom"] - 2 <= r["top"] < limit - 2]
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
        rows = page.crop(box).extract_table({
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
