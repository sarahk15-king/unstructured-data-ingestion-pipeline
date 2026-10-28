"""Custom extraction engine: PDF or URL in, Markdown plus structured JSON out.

Every element (heading, text, table, formula, key-value pair, image, header, footer) comes out in
reading order with a confidence and a note on how it was produced.
"""
from .pipeline import extract_pdf, extract_url  # noqa: F401
from .schema import Document, Element  # noqa: F401