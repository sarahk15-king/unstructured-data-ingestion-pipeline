"""PDF to elements: the column reader for text, headings and formulas, pdfplumber for tables and images."""
import io
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

from . import confidence as conf
from . import mdparse
from .schema import Element

RENDER_DPI = 150
MAX_IMAGES = int(os.getenv("ENGINE_MAX_IMAGES", "40"))


def _main():
    """The reader functions live in api/main.py (the Scope 1 backend)."""
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    from api import main
    return main


def rows_from_words(page, box, gap=6):
    """Fallback for a small table the grid finder gives up on: group the words into lines, then join
    words that are close together into one cell. Returns rows, or [] if the lines do not line up."""
    words = page.within_bbox(box).extract_words(x_tolerance=1.5)
    lines = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) < 3:
            lines[-1].append(w)
        else:
            lines.append([w])
    rows = []
    for line in lines:
        cells = []
        for w in sorted(line, key=lambda w: w["x0"]):
            if cells and w["x0"] - cells[-1]["x1"] < gap:
                cells[-1]["text"] += " " + w["text"]
                cells[-1]["x1"] = w["x1"]
            else:
                cells.append({"text": w["text"], "x1": w["x1"]})
        rows.append([c["text"] for c in cells])
    widths = {len(r) for r in rows}
    return rows if len(rows) >= 2 and len(widths) == 1 and min(widths) >= 2 else []


def tables_with_boxes(m, page, figures):
    """Like Scope 1's extract_page_tables, but also returns where each table sits on the page."""
    found, boxes = [], []
    for table in page.find_tables():
        rows = table.extract()
        if m.looks_like_table(table.bbox, rows, figures):
            found.append((m.clean_rows(rows), tuple(table.bbox)))
            boxes.append(table.bbox)
    for box in m.find_caption_tables(page):
        if any(m.overlap_share(box, b) > 0.3 for b in boxes):
            continue
        if any(m.overlap_share(box, f) > 0.5 for f in figures):
            continue
        rows = page.within_bbox(box).extract_table(
            {"vertical_strategy": "text", "horizontal_strategy": "text", "text_x_tolerance": 3})
        rows = m.clean_rows(rows or [])
        if len(rows) < 2:
            rows = rows_from_words(page, box)
        if m.looks_like_table(box, rows, figures):
            found.append((rows, tuple(box)))
            boxes.append(box)
    return found


def figure_images(m, page, figures, page_no):
    """(name, box, label) for each figure, its labelled panels and each picture outside a figure."""
    out = []
    for i, box in enumerate(figures):
        try:
            label = m.figure_label(page, box)
        except Exception:
            label = None
        out.append((f"page{page_no}_figure{i}", box, label))
        try:
            for letter, panel in m.split_panels(page, box):
                out.append((f"page{page_no}_figure{i}_{letter}", panel, label))
        except Exception:
            pass
    for j, img in enumerate(page.images):
        box = (max(img["x0"], page.bbox[0]), max(img["top"], page.bbox[1]),
               min(img["x1"], page.bbox[2]), min(img["bottom"], page.bbox[3]))
        if box[2] - box[0] < 20 or box[3] - box[1] < 20:
            continue
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if not any(f[0] <= cx <= f[2] and f[1] <= cy <= f[3] for f in figures):
            out.append((f"page{page_no}_img{j}", box, None))
    return out


def render_page(page, dpi=110):
    buffer = io.BytesIO()
    page.to_image(resolution=dpi).original.save(buffer, format="PNG")
    return buffer.getvalue()


def extract_pdf_elements(path, image_store, warnings, max_pages=60):
    """Returns (elements, page count, scanned page numbers)."""
    import pdfplumber
    m = _main()
    elements, scanned = [], []
    saved = 0
    with pdfplumber.open(str(path)) as pdf:
        pages = pdf.pages[:max_pages]
        if len(pdf.pages) > max_pages:
            warnings.append(f"Only the first {max_pages} of {len(pdf.pages)} pages were read.")
        per_page = []
        for page in pages:
            try:
                figures = [tuple(b) for b in m.find_figure_regions(page)]
            except Exception as e:
                figures = []
                warnings.append(f"Figure search failed on page {page.page_number}: {e}")
            try:
                tables = tables_with_boxes(m, page, figures)
            except Exception as e:
                tables = []
                warnings.append(f"Table search failed on page {page.page_number}: {e}")
            skip = list(figures) + [box for _, box in tables]
            words = m.page_words(page, skip)
            per_page.append((page, figures, tables, words))
        counts = Counter(t for page, _, _, words in per_page for t in set(m.margin_lines(page, words)))
        repeated = {t for t, n in counts.items() if n >= 2 and len(pages) > 1}

        for page, figures, tables, words in per_page:
            n = page.page_number
            has_images = bool(page.images)
            if len(words) < 8 and (has_images or not page.chars):
                scanned.append(n)
                elements.append(Element("text", n, "", 0.0, "reader", flags=["no_text_layer", "low_confidence"],
                                        data={"scanned": True}))
                continue
            try:
                markdown, _ = m.page_to_markdown(page, words, repeated)
            except Exception as e:
                warnings.append(f"Reader failed on page {n}: {e}")
                markdown = " ".join(w["text"] for w in words)
            page_elements = mdparse.blocks_to_elements(markdown, n, "reader")

            # tables: after the caption "Table N", or at the end of the page
            for rows, box in tables:
                el = Element("table", n, mdparse.table_markdown(rows), conf.table_confidence(rows), "pdfplumber",
                             data={"rows": rows, "box": [round(v, 1) for v in box]})
                at = next((i for i, e in enumerate(page_elements)
                           if e.type == "text" and re.match(r"^(Table|TABLE)\s*[\dIVX]+", e.content)), None)
                if at is not None:
                    page_elements.insert(at + 1, el)
                else:
                    page_elements.append(el)

            # images: before the caption "Figure N", or at the end of the page
            to_save = figure_images(m, page, figures, n) if saved < MAX_IMAGES else []
            if to_save:
                try:
                    sheet = page.to_image(resolution=RENDER_DPI).original
                except Exception as e:
                    warnings.append(f"Could not render page {n}: {e}")
                    sheet = None
                scale = RENDER_DPI / 72
                for name, box, label in to_save if sheet else []:
                    if saved >= MAX_IMAGES:
                        warnings.append(f"Stopped saving images after {MAX_IMAGES}.")
                        break
                    crop = sheet.crop((int((box[0] - page.bbox[0]) * scale), int((box[1] - page.bbox[1]) * scale),
                                       int((box[2] - page.bbox[0]) * scale), int((box[3] - page.bbox[1]) * scale)))
                    buffer = io.BytesIO()
                    crop.save(buffer, format="PNG")
                    ref = image_store(name, buffer.getvalue())
                    saved += 1
                    el = Element("image", n, f"![{name}]({ref})", conf.image_confidence(bool(label)), "pdfplumber",
                                 data={"name": name, "label": label, "box": [round(v, 1) for v in box]})
                    caption = None
                    if label:
                        number = re.search(r"\d+", label)
                        if number:
                            caption = next((e for e in page_elements if e.type == "text" and
                                            re.match(rf"^(Figure|Fig\.)\s*{number.group()}\b", e.content)), None)
                    if caption is not None:
                        page_elements.insert(page_elements.index(caption), el)  # keeps figure, then its panels
                    else:
                        page_elements.append(el)
            elements += page_elements
    return elements, len(pages), scanned