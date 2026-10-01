import time
from pathlib import Path

from docling.document_converter import DocumentConverter
from markitdown import MarkItDown

PDF_DIR = Path("data/pdfs")
OUTPUT_DIR = Path("outputs/markdown")
DOCLING_DIR = OUTPUT_DIR / "docling"
MARKITDOWN_DIR = OUTPUT_DIR / "markitdown"


def convert_with_docling(pdf_path, converter):
    start = time.time()
    result = converter.convert(str(pdf_path))
    markdown = result.document.export_to_markdown()
    elapsed = time.time() - start
    return markdown, elapsed


def convert_with_markitdown(pdf_path, md_converter):
    start = time.time()
    result = md_converter.convert(str(pdf_path))
    markdown = result.text_content
    elapsed = time.time() - start
    return markdown, elapsed


def process_pdf(pdf_path, docling_converter, markitdown_converter):
    doc_name = pdf_path.stem
    print(f"\nProcessing: {doc_name}")

    print("  Running Docling...")
    docling_md, docling_time = convert_with_docling(pdf_path, docling_converter)
    docling_out = DOCLING_DIR / f"{doc_name}.md"
    docling_out.write_text(docling_md, encoding="utf-8")
    print(f"  Docling: {len(docling_md)} characters, {docling_time:.1f}s -> {docling_out}")

    print("  Running MarkItDown...")
    markitdown_md, markitdown_time = convert_with_markitdown(pdf_path, markitdown_converter)
    markitdown_out = MARKITDOWN_DIR / f"{doc_name}.md"
    markitdown_out.write_text(markitdown_md, encoding="utf-8")
    print(f"  MarkItDown: {len(markitdown_md)} characters, {markitdown_time:.1f}s -> {markitdown_out}")


if __name__ == "__main__":
    DOCLING_DIR.mkdir(parents=True, exist_ok=True)
    MARKITDOWN_DIR.mkdir(parents=True, exist_ok=True)

    pdf_files = list(PDF_DIR.glob("*.pdf"))
    if not pdf_files:
        print(f"No PDFs found in {PDF_DIR}")
    else:
        print("Loading Docling (first run downloads models, may take a few minutes)...")
        docling_converter = DocumentConverter()
        markitdown_converter = MarkItDown()

        for pdf_file in pdf_files:
            process_pdf(pdf_file, docling_converter, markitdown_converter)