import os
from pathlib import Path
from pypdf import PdfReader
import pdfplumber

PDF_DIR = Path("data/pdfs")
OUTPUT_DIR = Path("outputs")
IMAGES_DIR = OUTPUT_DIR / "images"


def extract_text_pypdf(pdf_path):
    """Extract raw text page by page using pypdf."""
    reader = PdfReader(pdf_path)
    pages_text = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        pages_text.append({"page": i + 1, "text": text})
    return pages_text


def extract_tables_and_images_pdfplumber(pdf_path, doc_name):
    """Extract tables and embedded images using pdfplumber."""
    doc_image_dir = IMAGES_DIR / doc_name
    doc_image_dir.mkdir(parents=True, exist_ok=True)

    all_tables = []
    image_files = []

    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            page_num = i + 1

            tables = page.extract_tables()
            for t_idx, table in enumerate(tables):
                all_tables.append({
                    "page": page_num,
                    "table_index": t_idx,
                    "rows": table,
                })

            for img_idx, img in enumerate(page.images):
                try:
                    x0, top, x1, bottom = img["x0"], img["top"], img["x1"], img["bottom"]
                    cropped = page.within_bbox((x0, top, x1, bottom))
                    im = cropped.to_image(resolution=150)
                    filename = f"page{page_num}_img{img_idx}.png"
                    filepath = doc_image_dir / filename
                    im.save(str(filepath))
                    image_files.append(str(filepath))
                except Exception as e:
                    print(f"  Skipped an image on page {page_num}: {e}")

    return all_tables, image_files


def process_pdf(pdf_path):
    doc_name = Path(pdf_path).stem
    print(f"\nProcessing: {doc_name}")

    pages_text = extract_text_pypdf(pdf_path)
    total_chars = sum(len(p["text"]) for p in pages_text)
    print(f"  pypdf: extracted text from {len(pages_text)} pages ({total_chars} characters)")

    tables, images = extract_tables_and_images_pdfplumber(pdf_path, doc_name)
    print(f"  pdfplumber: found {len(tables)} tables, saved {len(images)} images")

    return {
        "doc_name": doc_name,
        "pages_text": pages_text,
        "tables": tables,
        "images": images,
    }


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(exist_ok=True)
    pdf_files = list(PDF_DIR.glob("*.pdf"))

    if not pdf_files:
        print(f"No PDFs found in {PDF_DIR}. Add your test PDFs there first.")
    else:
        for pdf_file in pdf_files:
            process_pdf(pdf_file)