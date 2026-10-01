import os
import time
import json
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
textract = session.client("textract")

BUCKET = os.getenv("S3_BUCKET")
PDF_DIR = Path("data/pdfs")
OUTPUT_DIR = Path("outputs/textract")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def upload_to_s3(pdf_path):
    key = f"raw/pdf/textract-test/{pdf_path.name}"
    s3.upload_file(str(pdf_path), BUCKET, key)
    return key


def start_analysis(key):
    response = textract.start_document_analysis(
        DocumentLocation={"S3Object": {"Bucket": BUCKET, "Name": key}},
        FeatureTypes=["TABLES", "FORMS"],
    )
    return response["JobId"]


def wait_for_completion(job_id, poll_seconds=5):
    while True:
        response = textract.get_document_analysis(JobId=job_id)
        status = response["JobStatus"]
        if status in ("SUCCEEDED", "FAILED"):
            return status
        print(f"  Job status: {status}, waiting...")
        time.sleep(poll_seconds)


def get_all_blocks(job_id):
    blocks = []
    next_token = None
    while True:
        if next_token:
            response = textract.get_document_analysis(JobId=job_id, NextToken=next_token)
        else:
            response = textract.get_document_analysis(JobId=job_id)
        blocks.extend(response["Blocks"])
        next_token = response.get("NextToken")
        if not next_token:
            break
    return blocks


def process_pdf(pdf_path):
    doc_name = pdf_path.stem
    print(f"\nProcessing with Textract: {doc_name}")

    key = upload_to_s3(pdf_path)
    print(f"  Uploaded to s3://{BUCKET}/{key}")

    job_id = start_analysis(key)
    print(f"  Started job: {job_id}")

    status = wait_for_completion(job_id)
    if status != "SUCCEEDED":
        print(f"  Job failed with status: {status}")
        return

    blocks = get_all_blocks(job_id)
    lines = [b["Text"] for b in blocks if b["BlockType"] == "LINE"]
    tables = [b for b in blocks if b["BlockType"] == "TABLE"]

    print(f"  Extracted {len(lines)} lines of text, found {len(tables)} tables")

    output_file = OUTPUT_DIR / f"{doc_name}_textract.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(blocks, f, indent=2)
    print(f"  Saved raw output to {output_file}")


if __name__ == "__main__":
    pdf_files = list(PDF_DIR.glob("*.pdf"))
    if not pdf_files:
        print(f"No PDFs found in {PDF_DIR}")
    else:
        for pdf_file in pdf_files:
            process_pdf(pdf_file)