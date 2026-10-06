import pandas as pd
import requests
import streamlit as st

API_URL = "https://ingestion-pipeline-api.onrender.com"

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

    st.markdown("**Preview:**")
    st.text_area("Markdown preview", result["markdown_preview"], height=300)


tab1, tab2 = st.tabs(["Upload PDF", "Submit URL"])

with tab1:
    uploaded_file = st.file_uploader("Choose a PDF", type=["pdf"])
    tool = st.selectbox("Conversion tool", ["docling", "markitdown"])

    if st.button("Process PDF", disabled=uploaded_file is None):
        with st.spinner(f"Processing with {tool}... this may take a while for Docling"):
            files = {"file": (uploaded_file.name, uploaded_file.getvalue(), "application/pdf")}
            data = {"tool": tool}
            try:
                response = requests.post(f"{API_URL}/process-pdf", files=files, data=data, timeout=300)
                response.raise_for_status()
                show_result(response.json())
            except Exception as e:
                st.error(f"Error: {e}")

with tab2:
    url = st.text_input("Enter a web page URL", placeholder="https://en.wikipedia.org/wiki/...")

    if st.button("Process URL", disabled=not url):
        with st.spinner("Processing URL..."):
            try:
                response = requests.post(f"{API_URL}/process-url", data={"url": url}, timeout=120)
                response.raise_for_status()
                show_result(response.json())
            except Exception as e:
                st.error(f"Error: {e}")
