import pandas as pd
import requests
import streamlit as st

API_URL = "https://ingestion-pipeline-api.onrender.com"

# Docling needs more memory than Render's free tier has, so it is only offered with a local backend.
DOCLING_AVAILABLE = "localhost" in API_URL or "127.0.0.1" in API_URL

TOOL_NAMES = {"markitdown": "MarkItDown", "pypdf": "pypdf", "pdfplumber": "pdfplumber", "docling": "Docling"}

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
    tool_options = ["markitdown", "pypdf", "pdfplumber"] + (["docling"] if DOCLING_AVAILABLE else [])
    tool = st.selectbox("Conversion tool", tool_options)

    if st.button("Process PDF", disabled=uploaded_file is None):
        with st.spinner(f"Processing with {TOOL_NAMES[tool]}..."):
            files = {"file": (uploaded_file.name, uploaded_file.getvalue(), "application/pdf")}
            data = {"tool": tool}
            try:
                response = requests.post(f"{API_URL}/process-pdf", files=files, data=data, timeout=300)
                response.raise_for_status()
                st.session_state["pdf_result"] = response.json()
            except Exception as e:
                st.session_state.pop("pdf_result", None)
                st.error(f"Error: {e}")

    if "pdf_result" in st.session_state:
        show_result(st.session_state["pdf_result"])

with tab2:
    url = st.text_input("Enter a web page URL", placeholder="https://en.wikipedia.org/wiki/...")

    if st.button("Process URL", disabled=not url):
        with st.spinner("Processing URL..."):
            try:
                response = requests.post(f"{API_URL}/process-url", data={"url": url}, timeout=120)
                response.raise_for_status()
                st.session_state["url_result"] = response.json()
            except Exception as e:
                st.session_state.pop("url_result", None)
                st.error(f"Error: {e}")

    if "url_result" in st.session_state:
        show_result(st.session_state["url_result"])
