# S3 Storage Layout (Task 5)

Bucket: sarah-unstructed-ingestion-2026 (private, SSE-S3 encrypted,
Block Public Access enabled)

## Naming scheme

- `raw/pdf/<source>/<date>/<file>.pdf` — original PDFs, by source and date.
- `raw/web/<document>/<date>/<text|tables>.json` — raw scraped web data.
- `processed/markdown/<tool>/<source>/<file>.md` — standardized Markdown
  output, separated by which tool produced it (docling or markitdown).
- `assets/images/<source>/<document>/<image>` — extracted images, grouped
  by source and document.

Every uploaded file carries metadata tags (source, tool, date, type,
document name), so files can be filtered without parsing the key path.