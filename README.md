# Unstructured Data Ingestion Pipeline

A pipeline that converts unstructured documents (PDFs and web pages) into clean Markdown and stores the results in AWS S3. Includes a FastAPI backend and a Streamlit UI for interactive use, plus a comparison of extraction tools (open-source, Docling/MarkItDown, and enterprise OCR services).

**Live app:** https://unstructured-data-ingestion-pipeline-agwzsbtb3xtfdvpuc9d7k6.streamlit.app/
**Backend API:** https://ingestion-pipeline-api.onrender.com

## What it does

1. Upload a PDF or submit a web page URL through the Streamlit app.
2. The FastAPI backend converts it to Markdown (via Docling or MarkItDown for PDFs, BeautifulSoup for web pages).
3. The result is uploaded to S3 with metadata tags (source, tool, date, type).

## Project structure
