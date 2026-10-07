# S3 Storage Layout (Task 5)

Bucket: sarah-unstructed-ingestion-2026 (private, SSE-S3 encrypted,
Block Public Access enabled)

## Naming scheme

**PDFs**

- `raw/pdf/<source>/<date>/<file>.pdf` — original PDFs, by source and date.
- `processed/markdown/<tool>/<source>/<file>.md` — standardized Markdown
  output, separated by which tool produced it (docling or markitdown).
  The Markdown ends with an "Extracted images" section linking to each image.
- `processed/extracted/pdf/<document>/text.json` — the extracted text.
- `processed/extracted/pdf/<document>/tables/page<p>_table<n>.csv` — each
  table as its own CSV, named by page and table number.
- `assets/images/<source>/<document>/page<p>_img<i>.png` — embedded pictures.
- `assets/images/<source>/<document>/page<p>_figure<k>.png` — figures drawn
  as vector graphics, found from their "Figure N:" caption.
- `assets/images/<source>/<document>/page<p>_figure<k>_<letter>.png` — each
  labelled panel (a), (b), (c) of a figure, saved as its own image.

**Web pages**

- `raw/web/<document>/<date>/text.json` — the scraped text.
- `raw/web/<document>/<date>/tables/table_<n>.csv` — each table as its own CSV.
- `assets/images/web/<document>/img<i>.<ext>` — images from the page.
- `processed/markdown/web/<source>/<document>.md` — the page as Markdown.

`<source>` is `api-uploads` for files sent through the app.

## Example (ACE.pdf uploaded through the app)
