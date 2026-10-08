import os

import pandas as pd
import requests
import streamlit as st

# The backend. It defaults to the deployed Render service. To run everything on your own computer,
# set the API_URL environment variable to http://localhost:8000 before starting the app.
API_URL = os.getenv("API_URL", "https://ingestion-pipeline-api.onrender.com")

# Optional: a separate backend used only for Docling, for example a local backend shared
# through a tunnel. Leave it unset to use API_URL for everything.
DOCLING_API_URL = os.getenv("DOCLING_API_URL", "")

# Docling needs more memory than Render's free tier has, so it only works against a local backend.
DOCLING_AVAILABLE = bool(DOCLING_API_URL) or "localhost" in API_URL or "127.0.0.1" in API_URL

TOOL_NAMES = {"markitdown": "MarkItDown", "pypdf": "pypdf", "pdfplumber": "pdfplumber", "azure": "Azure Document Intelligence", "docling": "Docling"}

st.set_page_config(page_title="Unstructured Data Ingestion Pipeline", layout="wide")
st.title("Unstructured Data Ingestion Pipeline")
st.caption("Upload a PDF or submit a URL to convert it to Markdown and store the text, tables and images in S3.")


def show_result(result):
    uploaded = result.get("uploaded", {})

    st.success(f"Done! Extracted {result['markdown_length']} characters")
    st.write(f"**S3 location (Markdown):** `{result['s3_url']}`")
    st.write(
        f"**Tables found:** {uploaded.get('table_count', 0)}  |  "
        f"**Images uploaded:** {uploaded.get('image_count', 0)}"
    )

    with st.expander("Everything uploaded to S3"):
        st.json(uploaded)

    tables = result.get("tables", [])
    if tables:
        st.markdown(f"**Tables ({len(tables)}), each saved as its own CSV file:**")
        for table in tables:
            with st.expander(f"{table['name']}  ({table['row_count']} rows)"):
                st.caption(f"S3 key: `{table['key']}`")
                preview = table["preview"]
                width = max(len(row) for row in preview)
                padded = [row + [""] * (width - len(row)) for row in preview]
                st.dataframe(pd.DataFrame(padded))
                if table["row_count"] > len(preview):
                    st.caption(f"Showing the first {len(preview)} of {table['row_count']} rows.")

    for warning in result.get("warnings", []):
        st.warning(warning)

    markdown_text = result.get("markdown_text", result["markdown_preview"])
    file_name = result["s3_key"].rsplit("/", 1)[-1]
    st.download_button(
        "Download Markdown",
        data=markdown_text,
        file_name=file_name,
        mime="text/markdown",
        key=f"download_{result['s3_key']}",
    )

    st.markdown("**Preview:**")
    st.text_area("Markdown preview", markdown_text[:3000], height=300)


tab1, tab2 = st.tabs(["Upload PDF", "Submit URL"])

with tab1:
    uploaded_file = st.file_uploader("Choose a PDF", type=["pdf"])
    tool = st.selectbox("Conversion tool", ["markitdown", "pypdf", "pdfplumber", "azure", "docling"])

    if tool == "azure":
        st.info(
            "Azure Document Intelligence is an enterprise service. On the free tier it reads only "
            "the first 2 pages of a PDF and files up to 4 MB."
        )

    docling_unavailable = tool == "docling" and not DOCLING_AVAILABLE
    if docling_unavailable:
        st.warning(
            "Docling is currently unavailable on this deployed app because the Render free tier "
            "does not have enough memory to run it. To use it, run the backend and the app on your own "
            "computer (see the README), or pick MarkItDown, pypdf or pdfplumber."
        )

    if st.button("Process PDF", disabled=uploaded_file is None or docling_unavailable):
        with st.spinner(f"Processing with {TOOL_NAMES[tool]}..."):
            files = {"file": (uploaded_file.name, uploaded_file.getvalue(), "application/pdf")}
            data = {"tool": tool}
            try:
                backend = DOCLING_API_URL if tool == "docling" and DOCLING_API_URL else API_URL
                response = requests.post(f"{backend.rstrip('/')}/process-pdf", files=files, data=data, timeout=600)
                response.raise_for_status()
                st.session_state["pdf_result"] = response.json()
            except Exception as e:
                st.session_state.pop("pdf_result", None)
                detail = ""
                try:
                    detail = response.json().get("detail", "")
                except Exception:
                    pass
                st.error(f"Error: {detail or e}")

    if "pdf_result" in st.session_state:
        show_result(st.session_state["pdf_result"])

with tab2:
    url = st.text_input("Enter a web page URL", placeholder="https://en.wikipedia.org/wiki/...")
    url_tool = st.selectbox(
        "Conversion tool",
        ["beautifulsoup", "azure", "pypdf", "pdfplumber"],
        format_func=lambda t: {"beautifulsoup": "BeautifulSoup (web pages)", **TOOL_NAMES}[t],
        key="url_tool",
    )
    if url_tool in ("pypdf", "pdfplumber"):
        st.info("pypdf and pdfplumber read PDF files. Use them with a link that points to a PDF.")
    elif url_tool == "azure":
        st.info(
            "Azure Document Intelligence works on web pages and PDF links. On the free tier it reads "
            "only the first 2 pages of a PDF."
        )
    else:
        st.caption("BeautifulSoup reads web pages. For a link to a PDF, choose Azure, pypdf or pdfplumber.")

    if st.button("Process URL", disabled=not url):
        with st.spinner(f"Processing with {'BeautifulSoup' if url_tool == 'beautifulsoup' else TOOL_NAMES[url_tool]}..."):
            try:
                response = requests.post(
                    f"{API_URL.rstrip('/')}/process-url", data={"url": url, "tool": url_tool}, timeout=300
                )
                response.raise_for_status()
                st.session_state["url_result"] = response.json()
            except Exception as e:
                st.session_state.pop("url_result", None)
                detail = ""
                try:
                    detail = response.json().get("detail", "")
                except Exception:
                    pass
                st.error(f"Error: {detail or e}")

    if "url_result" in st.session_state:
        show_result(st.session_state["url_result"])
