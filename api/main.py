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

    for line in all_lines:
        if full_width(line):
            flush()
            blocks.append([line])
        else:
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


def page_to_markdown(page, words, drop_margin=()):
    margin = page.height * 0.11
    words = [w for w in words if not ((w["bottom"] < margin or w["top"] > page.height - margin)
                                      and _in_drop(page, w, drop_margin, words))]
    if not words:
        return "", False
    body_size = Counter(round(w["size"]) for w in words).most_common(1)[0][0]
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


def convert_pdf(file_path, tool):
    if tool == "docling":
        return convert_with_docling(file_path)
    elif tool == "markitdown":
        return convert_with_markitdown(file_path)
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
