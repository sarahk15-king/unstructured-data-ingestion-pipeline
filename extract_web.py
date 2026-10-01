import os
import re
import json
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

URLS = [
    "https://en.wikipedia.org/wiki/Extract,_transform,_load",
    "https://en.wikipedia.org/wiki/List_of_tallest_buildings",
    "https://en.wikipedia.org/wiki/Periodic_table",
]

OUTPUT_DIR = Path("outputs/web")
HEADERS = {"User-Agent": "Mozilla/5.0 (educational data ingestion project)"}


def slugify(url):
    return re.sub(r"[^a-zA-Z0-9]+", "_", urlparse(url).path).strip("_")


def fetch_page(url):
    response = requests.get(url, headers=HEADERS, timeout=15)
    response.raise_for_status()
    return BeautifulSoup(response.text, "lxml")


def extract_text(soup):
    """Pull headings and paragraph text in order."""
    content = soup.find(id="mw-content-text") or soup.body
    elements = []
    for tag in content.find_all(["h1", "h2", "h3", "p"]):
        text = tag.get_text(strip=True)
        if text:
            elements.append({"tag": tag.name, "text": text})
    return elements


def extract_tables(soup):
    """Pull HTML tables as lists of rows."""
    tables = []
    for t_idx, table in enumerate(soup.find_all("table")):
        rows = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if cells:
                rows.append(cells)
        if rows:
            tables.append({"table_index": t_idx, "rows": rows})
    return tables


def extract_images(soup, base_url, doc_name):
    """Download images referenced on the page."""
    doc_image_dir = OUTPUT_DIR / doc_name / "images"
    doc_image_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for img_idx, img in enumerate(soup.find_all("img")):
        src = img.get("src")
        if not src:
            continue
        full_url = urljoin(base_url, src)
        try:
            resp = requests.get(full_url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            ext = os.path.splitext(urlparse(full_url).path)[1] or ".jpg"
            filename = f"img{img_idx}{ext}"
            filepath = doc_image_dir / filename
            with open(filepath, "wb") as f:
                f.write(resp.content)
            saved.append({"url": full_url, "file": str(filepath)})
        except Exception as e:
            print(f"  Skipped an image ({full_url}): {e}")

    return saved


def process_url(url):
    doc_name = slugify(url)
    print(f"\nProcessing: {url}")

    soup = fetch_page(url)

    text_elements = extract_text(soup)
    print(f"  Extracted {len(text_elements)} text blocks (headings + paragraphs)")

    tables = extract_tables(soup)
    print(f"  Found {len(tables)} tables")

    images = extract_images(soup, url, doc_name)
    print(f"  Downloaded {len(images)} images")

    doc_dir = OUTPUT_DIR / doc_name
    doc_dir.mkdir(parents=True, exist_ok=True)

    with open(doc_dir / "text.json", "w", encoding="utf-8") as f:
        json.dump(text_elements, f, indent=2, ensure_ascii=False)

    with open(doc_dir / "tables.json", "w", encoding="utf-8") as f:
        json.dump(tables, f, indent=2, ensure_ascii=False)

    return {"doc_name": doc_name, "text_elements": text_elements, "tables": tables, "images": images}


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for url in URLS:
        process_url(url)