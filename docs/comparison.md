# Three-Way Comparison: Open-Source vs Docling/MarkItDown vs Enterprise (Task 3)

Tested on 3 PDFs (ACE, graph-CNN, LSC) and 3 web pages (ETL,
List of tallest buildings, Periodic table).

## Accuracy

**Open-source (pypdf/pdfplumber, requests/BeautifulSoup):**
- pypdf extracted text reliably except on graph-CNN, where pdfplumber
  hit a hard limit ("Exceeded 5000 form XObject invocations") and
  only processed 5 of the PDF's pages.
- pdfplumber missed all 3 tables in the LSC paper (found 0).
- Web scraping with BeautifulSoup was reliable across all 3 pages,
  though it picked up boilerplate tables/images alongside real content.

**Docling / MarkItDown:** see docling_vs_markitdown_vs_Open_Source_Lib.md for full detail
Docling preserved structure and rendered simple tables cleanly;
MarkItDown lost word spacing and mangled complex layouts/formulas.

**Enterprise (Azure AI Document Intelligence):**
- AWS Textract could not be tested; the AWS account remained blocked
  with a "SubscriptionRequiredException" throughout the project.
- Azure AI Document Intelligence (prebuilt-layout model) was
  successfully tested as the enterprise alternative. It processed
  ACE (8,651 characters extracted) and graph-CNN (11,983 characters)
  successfully. The LSC PDF failed with "InvalidContentLength", a
  free-tier (F0) file size limit.
- Azure's output was clean prose-style Markdown but found 0 tables in
  either PDF, while Docling found and rendered 13 tables in ACE.
  Azure's free-tier layout model appears tuned more for text/form
  extraction than complex academic table structures.

## Performance (processing time per document)

| PDF | Docling | MarkItDown |
|---|---|---|
| ACE (19p, table-heavy) | 195.0s | 1.6s |
| graph-CNN (diagram-heavy) | 23.0s | 3.0s |
| LSC (8p, moderate) | 44.6s | ~2s |

## Ease of use

- **Open-source libraries**: quick to install, require custom logic
  for tables/images, behavior varies a lot by document format.
- **Docling**: heavier install (~10-20 min with model downloads), but
  a simple API call once set up.
- **MarkItDown**: very light install, fast, simplest API of the three.
- **Enterprise (Azure/Textract)**: setup friction was the single
  biggest cost in this project; AWS subscription activation never
  completed, and Azure required multiple signup attempts.

## Cost

All testing in this project used free tiers or free tools, so the actual cost was $0.

| Approach | What I used | Minimum cost |
|---|---|---|
| Open-source libraries (pypdf, pdfplumber, BeautifulSoup) | Free libraries on my own machine | $0 |
| Docling / MarkItDown | Free libraries on my own machine; the deployed app runs on Render's free tier | $0 (only compute) |
| Azure Document Intelligence | Free tier (F0): 500 pages per month, 4 MB file limit, first 2 pages of each document only | $0 on the free tier; the paid layout model is roughly $10 per 1,000 pages |
| AWS Textract | Could not run on my free AWS plan; it needs an upgrade to a paid plan | Text only: $1.50 per 1,000 pages. Tables and forms: $65 per 1,000 pages |

- At the scale tested here (3 PDFs, about 32 pages), the paid options would cost very little:
  roughly $0.32 for Azure's layout model and about $2.08 for Textract with tables and forms,
  as in my script.
- Textract's own free tier (for new AWS customers, 3 months) covers 1,000 pages of text detection
  and 100 pages of table and form analysis per month, but the service still needs the paid plan.
- Azure's price is approximate, because the sources I found disagree. Confirm it on Azure's
  pricing page before relying on it.

## Scalability and integration

- Open-source tools run anywhere with no external dependency, but
  need manual fallback handling for failures like the graph-CNN case.
- Docling produced the most reliable output but is slow (195s for one
  19-page PDF), needing parallelization for real volume.
- MarkItDown scales easily on speed but its accuracy issues make it
  risky for production use on complex documents.
- Enterprise services are designed for scale and offer managed
  infrastructure, but introduce external cost and setup dependency.

## Overall recommendation

For a pipeline handling dense, multimodal PDFs (tables, equations,
figures), Docling is the strongest default: most faithful output of
the tools actually tested, at the cost of speed. MarkItDown works
only as a fast fallback for simple, text-only documents. Azure's
free-tier model handled text well but missed tables entirely,
suggesting a paid tier or different feature configuration would be
needed for full enterprise-grade table extraction in production.
