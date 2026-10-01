import os
import json
from datetime import date
from pathlib import Path

import boto3
from dotenv import load_dotenv

load_dotenv()

session = boto3.Session(
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    region_name=os.getenv("AWS_REGION"),
)
s3 = session.client("s3")
BUCKET = os.getenv("S3_BUCKET")
TODAY = date.today().isoformat()

uploaded_count = 0


def upload_file(local_path, s3_key, metadata):
    global uploaded_count
    s3.upload_file(
        str(local_path),
        BUCKET,
        s3_key,
        ExtraArgs={"Metadata": {k: str(v) for k, v in metadata.items()}},
    )
    uploaded_count += 1
    print(f"  Uploaded: {s3_key}")


def upload_raw_pdfs():
    print("\nUploading raw PDFs...")
    for pdf_path in Path("data/pdfs").glob("*.pdf"):
        s3_key = f"raw/pdf/arxiv/{TODAY}/{pdf_path.name}"
        upload_file(pdf_path, s3_key, {
            "source": "arxiv",
            "type": "pdf",
            "date": TODAY,
        })


def upload_markdown(tool_name):
    print(f"\nUploading {tool_name} markdown...")
    folder = Path("outputs/markdown") / tool_name
    if not folder.exists():
        return
    for md_path in folder.glob("*.md"):
        s3_key = f"processed/markdown/{tool_name}/arxiv/{md_path.name}"
        upload_file(md_path, s3_key, {
            "tool": tool_name,
            "source": "arxiv",
            "date": TODAY,
            "type": "markdown",
        })


def upload_pdf_images():
    print("\nUploading PDF-extracted images...")
    images_root = Path("outputs/images")
    if not images_root.exists():
        return
    for doc_dir in images_root.iterdir():
        if not doc_dir.is_dir():
            continue
        for img_path in doc_dir.glob("*"):
            s3_key = f"assets/images/arxiv/{doc_dir.name}/{img_path.name}"
            upload_file(img_path, s3_key, {
                "source": "arxiv",
                "document": doc_dir.name,
                "date": TODAY,
                "type": "image",
            })


def upload_web_outputs():
    print("\nUploading web page outputs...")
    web_root = Path("outputs/web")
    if not web_root.exists():
        return
    for doc_dir in web_root.iterdir():
        if not doc_dir.is_dir():
            continue

        for json_name in ["text.json", "tables.json"]:
            json_path = doc_dir / json_name
            if json_path.exists():
                s3_key = f"raw/web/{doc_dir.name}/{TODAY}/{json_name}"
                upload_file(json_path, s3_key, {
                    "source": "web",
                    "document": doc_dir.name,
                    "date": TODAY,
                    "type": json_name.replace(".json", ""),
                })

        images_dir = doc_dir / "images"
        if images_dir.exists():
            for img_path in images_dir.glob("*"):
                s3_key = f"assets/images/web/{doc_dir.name}/{img_path.name}"
                upload_file(img_path, s3_key, {
                    "source": "web",
                    "document": doc_dir.name,
                    "date": TODAY,
                    "type": "image",
                })


if __name__ == "__main__":
    upload_raw_pdfs()
    upload_markdown("docling")
    upload_markdown("markitdown")
    upload_pdf_images()
    upload_web_outputs()
    print(f"\nDone. Uploaded {uploaded_count} files to s3://{BUCKET}")