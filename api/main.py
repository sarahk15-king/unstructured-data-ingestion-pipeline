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


def convert_pdf(file_path, tool):
    if tool == "docling":
        from docling.document_converter import DocumentConverter
        converter = DocumentConverter()
        result = converter.convert(str(file_path))
        return result.document.export_to_markdown()
    elif tool == "markitdown":
        from markitdown import MarkItDown
        converter = MarkItDown()
        result = converter.convert(str(file_path))
        return result.text_content
    else:
        raise ValueError("tool must be 'docling' or 'markitdown'")


def add_image_links(markdown_text, image_keys):
    """Append links to the images saved in S3 so the Markdown points to where they are stored."""
    if not image_keys:
        return markdown_text
    lines = ["", "## Extracted images", ""]
    for key in image_keys:
        name = key.rsplit("/", 1)[-1]
        lines.append(f"![{name}](s3://{BUCKET}/{key})")
        lines.append("")
    return markdown_text.rstrip() + "\n" + "\n".join(lines)


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


def extract_and_upload_pdf_parts(pdf_path, doc_name, today):
    """Pull text, tables and images out of a PDF and upload each one to S3 on its own."""
    import pdfplumber

    pages_text = []
    table_summaries = []
    image_keys = []
    warnings = []
    table_meta = {"source": "api-upload", "document": doc_name, "date": today}

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            pages_text.append({"page": page_num, "text": page.extract_text() or ""})

            # Each table becomes its own CSV file
            for t_idx, rows in enumerate(page.extract_tables()):
                if not rows:
                    continue
                name = f"page{page_num}_table{t_idx}"
                key = f"processed/extracted/pdf/{doc_name}/tables/{name}.csv"
                table_summaries.append(upload_table(key, name, rows, table_meta))

            # Images: figures (including vector drawings) first, then embedded pictures
            if len(image_keys) >= MAX_IMAGES:
                continue
            try:
                figures = find_figure_regions(page)
            except Exception as e:
                figures = []
                warnings.append(f"Could not look for figures on page {page_num}: {e}")

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

            to_save = [(f"page{page_num}_figure{i}", box) for i, box in enumerate(figures)] + pictures
            if not to_save:
                continue

            try:
                page_image = page.to_image(resolution=RENDER_DPI).original
            except Exception as e:
                warnings.append(f"Could not render page {page_num} to save its images: {e}")
                continue
            scale = RENDER_DPI / 72
            for name, box in to_save:
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
    return parts, table_summaries, warnings


@app.get("/")
def root():
    return {"status": "ok", "message": "Ingestion pipeline API is running"}


@app.post("/process-pdf")
def process_pdf(file: UploadFile = File(...), tool: str = Form("docling")):
    if tool not in ("docling", "markitdown"):
        raise HTTPException(status_code=400, detail="tool must be 'docling' or 'markitdown'")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        doc_name = safe_name(Path(file.filename).stem)
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
        try:
            parts, table_summaries, warnings = extract_and_upload_pdf_parts(tmp_path, doc_name, today)
            uploaded.update(parts)
            image_keys = parts["image_keys"]
        except Exception as e:
            warnings.append(f"Text, table and image extraction failed: {e}")

        # 3. Whole document as Markdown, with links to the images saved in S3
        markdown_text = add_image_links(convert_pdf(tmp_path, tool), image_keys)
        markdown_key = upload_result(doc_name, tool, markdown_text)
        uploaded["markdown"] = markdown_key

        return {
            "filename": file.filename,
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
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/process-url")
def process_url(url: str = Form(...)):
    today = date.today().isoformat()

    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch the URL: {e}")

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
