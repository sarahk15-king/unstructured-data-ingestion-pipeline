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
MAX_IMAGES = 15
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


def upload_result(doc_name, tool, markdown_text):
    key = f"processed/markdown/{tool}/api-uploads/{doc_name}.md"
    put_object(
        key,
        markdown_text.encode("utf-8"),
        "text/markdown",
        {"tool": tool, "source": "api-upload", "type": "markdown", "document": doc_name},
    )
    return key


def extract_and_upload_pdf_parts(pdf_path, doc_name, today):
    """Pull text, tables and images out of a PDF and upload each one to S3 on its own."""
    import pdfplumber

    pages_text = []
    tables = []
    image_keys = []
    warnings = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            pages_text.append({"page": page_num, "text": page.extract_text() or ""})

            for t_idx, rows in enumerate(page.extract_tables()):
                tables.append({"page": page_num, "table_index": t_idx, "rows": rows})

            for img_idx, img in enumerate(page.images):
                if len(image_keys) >= MAX_IMAGES:
                    break
                try:
                    # Clamp to the page so images that overhang the edge are still saved.
                    x0 = max(img["x0"], page.bbox[0])
                    top = max(img["top"], page.bbox[1])
                    x1 = min(img["x1"], page.bbox[2])
                    bottom = min(img["bottom"], page.bbox[3])
                    if x1 - x0 < 20 or bottom - top < 20:
                        continue
                    buffer = io.BytesIO()
                    page.within_bbox((x0, top, x1, bottom)).to_image(resolution=120).save(buffer, format="PNG")
                    key = f"assets/images/api-uploads/{doc_name}/page{page_num}_img{img_idx}.png"
                    put_object(
                        key,
                        buffer.getvalue(),
                        "image/png",
                        {"source": "api-upload", "document": doc_name, "date": today, "type": "image"},
                    )
                    image_keys.append(key)
                except Exception as e:
                    warnings.append(f"Skipped an image on page {page_num}: {e}")

    base = f"processed/extracted/pdf/{doc_name}"
    text_key = put_json(
        f"{base}/text.json",
        pages_text,
        {"source": "api-upload", "document": doc_name, "date": today, "type": "text"},
    )
    tables_key = put_json(
        f"{base}/tables.json",
        tables,
        {"source": "api-upload", "document": doc_name, "date": today, "type": "tables"},
    )

    return {
        "text": text_key,
        "tables": tables_key,
        "table_count": len(tables),
        "image_count": len(image_keys),
        "image_keys": image_keys,
    }, warnings


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

        # 2. Whole document as Markdown
        markdown_text = convert_pdf(tmp_path, tool)
        markdown_key = upload_result(doc_name, tool, markdown_text)

        # 3. Text, tables and images as separate files
        uploaded = {"original_pdf": original_key, "markdown": markdown_key}
        warnings = []
        try:
            parts, warnings = extract_and_upload_pdf_parts(tmp_path, doc_name, today)
            uploaded.update(parts)
        except Exception as e:
            warnings.append(f"Text, table and image extraction failed: {e}")

        return {
            "filename": file.filename,
            "tool": tool,
            "s3_key": markdown_key,
            "s3_url": f"s3://{BUCKET}/{markdown_key}",
            "markdown_preview": markdown_text[:1000],
            "markdown_length": len(markdown_text),
            "uploaded": uploaded,
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

    # Tables
    tables = []
    for t_idx, table in enumerate(soup.find_all("table")):
        rows = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if cells:
                rows.append(cells)
        if rows:
            tables.append({"table_index": t_idx, "rows": rows})

    meta = {"source": "api-upload-url", "document": doc_name, "date": today}

    markdown_key = f"processed/markdown/web/api-uploads/{doc_name}.md"
    put_object(markdown_key, markdown_text.encode("utf-8"), "text/markdown", {**meta, "type": "markdown"})
    text_key = put_json(f"raw/web/{doc_name}/{today}/text.json", text_elements, {**meta, "type": "text"})
    tables_key = put_json(f"raw/web/{doc_name}/{today}/tables.json", tables, {**meta, "type": "tables"})

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

    return {
        "url": url,
        "s3_key": markdown_key,
        "s3_url": f"s3://{BUCKET}/{markdown_key}",
        "markdown_preview": markdown_text[:1000],
        "markdown_length": len(markdown_text),
        "uploaded": {
            "markdown": markdown_key,
            "text": text_key,
            "tables": tables_key,
            "table_count": len(tables),
            "image_count": len(image_keys),
            "image_keys": image_keys,
        },
        "warnings": warnings,
    }
