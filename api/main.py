import os
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import boto3
from dotenv import load_dotenv

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
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=markdown_text.encode("utf-8"),
        Metadata={"tool": tool, "source": "api-upload"},
    )
    return key


@app.get("/")
def root():
    return {"status": "ok", "message": "Ingestion pipeline API is running"}


@app.post("/process-pdf")
async def process_pdf(file: UploadFile = File(...), tool: str = Form("docling")):
    if tool not in ("docling", "markitdown"):
        raise HTTPException(status_code=400, detail="tool must be 'docling' or 'markitdown'")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        doc_name = Path(file.filename).stem
        markdown_text = convert_pdf(tmp_path, tool)
        s3_key = upload_result(doc_name, tool, markdown_text)

        return {
            "filename": file.filename,
            "tool": tool,
            "s3_key": s3_key,
            "s3_url": f"s3://{BUCKET}/{s3_key}",
            "markdown_preview": markdown_text[:1000],
            "markdown_length": len(markdown_text),
        }
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/process-url")
async def process_url(url: str = Form(...)):
    import requests
    from bs4 import BeautifulSoup
    import re
    from urllib.parse import urlparse

    headers = {"User-Agent": "Mozilla/5.0 (educational data ingestion project)"}
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")

    content = soup.find(id="mw-content-text") or soup.body
    lines = []
    for tag in content.find_all(["h1", "h2", "h3", "p"]):
        text = tag.get_text(strip=True)
        if text:
            prefix = "#" * int(tag.name[1]) if tag.name.startswith("h") else ""
            lines.append(f"{prefix} {text}" if prefix else text)
    markdown_text = "\n\n".join(lines)

    doc_name = re.sub(r"[^a-zA-Z0-9]+", "_", urlparse(url).path).strip("_") or "page"
    s3_key = f"processed/markdown/web/api-uploads/{doc_name}.md"
    s3.put_object(
        Bucket=BUCKET,
        Key=s3_key,
        Body=markdown_text.encode("utf-8"),
        Metadata={"source": "api-upload-url"},
    )

    return {
        "url": url,
        "s3_key": s3_key,
        "s3_url": f"s3://{BUCKET}/{s3_key}",
        "markdown_preview": markdown_text[:1000],
        "markdown_length": len(markdown_text),
    }