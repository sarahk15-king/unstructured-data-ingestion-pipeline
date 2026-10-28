"""The engine: one entry point for a PDF or a URL.

PDF steps
  1. Triage each page: does it have a text layer? (pdf.extract_pdf_elements)
  2. Text layer: the column reader gives headings, paragraphs, formulas, headers and footers in
     reading order; pdfplumber gives tables; the figure finder gives images.
  3. No text layer (a scan): Azure OCR when keys are set, otherwise the page is flagged, not guessed.
  4. Key-value pairs are derived from the text, and every element gets a confidence.
URL steps
  A link to a PDF goes through the PDF steps; a web page is read from its HTML structure.
"""
import os
import tempfile
import time
from pathlib import Path

from . import mdparse
from .pdf import extract_pdf_elements
from .schema import Document


def default_image_store(out_dir):
    """Saves each image as a PNG under out_dir/images and returns the relative link."""
    out = Path(out_dir) / "images"
    out.mkdir(parents=True, exist_ok=True)

    def store(name, png):
        (out / f"{name}.png").write_bytes(png)
        return f"images/{name}.png"

    return store


def azure_page_elements(pdf_path, page_no):
    """OCR one scanned page with Azure Document Intelligence when keys are set. Returns elements or None."""
    if not (os.getenv("AZURE_DOCINTEL_ENDPOINT") and os.getenv("AZURE_DOCINTEL_KEY")):
        return None
    try:
        from pypdf import PdfReader, PdfWriter
        from .pdf import _main
        writer = PdfWriter()
        writer.add_page(PdfReader(str(pdf_path)).pages[page_no - 1])
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            writer.write(tmp)
        try:
            markdown = _main().convert_with_azure(tmp.name)
        finally:
            os.unlink(tmp.name)
        return mdparse.blocks_to_elements(markdown, page_no, "azure", first_is_header=False)
    except Exception:
        return None


def extract_pdf(path, image_store=None, out_dir=None, max_pages=60):
    """PDF in, Document out (Document.to_markdown() and Document.to_json())."""
    started = time.time()
    doc = Document(source=str(path), level="engine")
    if image_store is None:
        image_store = default_image_store(out_dir or tempfile.mkdtemp(prefix="engine_"))

    elements, pages, scanned = extract_pdf_elements(path, image_store, doc.warnings, max_pages=max_pages)
    doc.pages = pages

    ocr_pages = 0
    if scanned:
        rebuilt = []
        for e in elements:
            if not e.data.get("scanned"):
                rebuilt.append(e)
                continue
            new = azure_page_elements(path, e.page)
            if new:
                ocr_pages += 1
                for n in new:
                    n.flags.append("ocr")
                rebuilt += new
            else:
                doc.warnings.append(f"Page {e.page} has no text layer and no OCR is set up, so it was not read.")
                rebuilt.append(e)
        elements = rebuilt

    doc.elements = mdparse.add_key_values([e for e in elements if e.type != "key_value"])
    mdparse.finish_flags(doc.elements)
    doc.number()
    doc.timings["total_s"] = round(time.time() - started, 2)
    doc.cost = {"ocr_pages": ocr_pages}
    return doc


def extract_url(url, image_store=None, out_dir=None):
    """URL in, Document out. A link to a PDF is downloaded and handled as a PDF."""
    from . import web
    started = time.time()
    body, content_type, final_url = web.fetch(url)
    if web.is_pdf(body, content_type, final_url):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(body)
        try:
            doc = extract_pdf(tmp.name, image_store, out_dir)
        finally:
            os.unlink(tmp.name)
        doc.source = url
        return doc
    doc = Document(source=url, level="engine", pages=1)
    doc.elements = web.html_elements(body.decode("utf-8", "replace"), final_url)
    mdparse.finish_flags(doc.elements)
    doc.number()
    doc.timings["total_s"] = round(time.time() - started, 2)
    return doc