import os
from pathlib import Path
from urllib.parse import urlparse

import requests
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
from dotenv import load_dotenv

load_dotenv()

ENDPOINT = os.getenv("AZURE_DOCINTEL_ENDPOINT")
KEY = os.getenv("AZURE_DOCINTEL_KEY")

URLS = [
    "https://en.wikipedia.org/wiki/Extract,_transform,_load",
    "https://en.wikipedia.org/wiki/List_of_tallest_buildings",
    "https://en.wikipedia.org/wiki/Periodic_table",
]
HEADERS = {"User-Agent": "ingestion-pipeline-demo/1.0 (learning project)"}

OUTPUT_DIR = Path("outputs/azure_web")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

client = DocumentIntelligenceClient(endpoint=ENDPOINT, credential=AzureKeyCredential(KEY))


def process_url(url):
    doc_name = urlparse(url).path.strip("/").split("/")[-1].replace(",", "")
    print(f"\nProcessing with Azure: {doc_name}")

    # Download the page, then send its HTML to Azure's layout model
    html = requests.get(url, headers=HEADERS, timeout=30)
    html.raise_for_status()

    poller = client.begin_analyze_document(
        "prebuilt-layout",
        body=html.content,
        content_type="text/html",
        output_content_format="markdown",
    )
    result = poller.result()

    markdown_text = result.content
    output_file = OUTPUT_DIR / f"{doc_name}.md"
    output_file.write_text(markdown_text, encoding="utf-8")

    print(f"  Extracted {len(markdown_text)} characters -> {output_file}")
    print(f"  Tables found: {len(result.tables) if result.tables else 0}")


if __name__ == "__main__":
    for page_url in URLS:
        try:
            process_url(page_url)
        except Exception as e:
            print(f"  Failed: {e}")
