import os
import json
from pathlib import Path

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
from dotenv import load_dotenv

load_dotenv()

ENDPOINT = os.getenv("AZURE_DOCINTEL_ENDPOINT")
KEY = os.getenv("AZURE_DOCINTEL_KEY")

PDF_DIR = Path("data/pdfs")
OUTPUT_DIR = Path("outputs/azure")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

client = DocumentIntelligenceClient(endpoint=ENDPOINT, credential=AzureKeyCredential(KEY))


def process_pdf(pdf_path):
    doc_name = pdf_path.stem
    print(f"\nProcessing with Azure: {doc_name}")

    with open(pdf_path, "rb") as f:
        poller = client.begin_analyze_document(
            "prebuilt-layout",
            body=f,
            output_content_format="markdown",
        )
    result = poller.result()

    markdown_text = result.content
    output_file = OUTPUT_DIR / f"{doc_name}.md"
    output_file.write_text(markdown_text, encoding="utf-8")

    print(f"  Extracted {len(markdown_text)} characters -> {output_file}")
    print(f"  Tables found: {len(result.tables) if result.tables else 0}")


if __name__ == "__main__":
    pdf_files = list(PDF_DIR.glob("*.pdf"))
    if not pdf_files:
        print(f"No PDFs found in {PDF_DIR}")
    else:
        for pdf_file in pdf_files:
            process_pdf(pdf_file)
