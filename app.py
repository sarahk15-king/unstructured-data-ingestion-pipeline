import streamlit as st
import requests

API_URL = "https://ingestion-pipeline-api.onrender.com"

st.set_page_config(page_title="Unstructured Data Ingestion Pipeline", layout="wide")
st.title("Unstructured Data Ingestion Pipeline")
st.caption("Upload a PDF or submit a URL to convert it to Markdown and store it in S3.")

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
                result = response.json()

                st.success(f"Done! Extracted {result['markdown_length']} characters")
                st.write(f"**S3 location:** `{result['s3_url']}`")
                st.markdown("**Preview:**")
                st.text_area("Markdown preview", result["markdown_preview"], height=300)
            except Exception as e:
                st.error(f"Error: {e}")

with tab2:
    url = st.text_input("Enter a web page URL", placeholder="https://en.wikipedia.org/wiki/...")

    if st.button("Process URL", disabled=not url):
        with st.spinner("Processing URL..."):
            try:
                response = requests.post(f"{API_URL}/process-url", data={"url": url}, timeout=60)
                response.raise_for_status()
                result = response.json()

                st.success(f"Done! Extracted {result['markdown_length']} characters")
                st.write(f"**S3 location:** `{result['s3_url']}`")
                st.markdown("**Preview:**")
                st.text_area("Markdown preview", result["markdown_preview"], height=300)
            except Exception as e:
                st.error(f"Error: {e}")