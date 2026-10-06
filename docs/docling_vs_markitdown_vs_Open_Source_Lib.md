# Docling vs MarkItDown vs Open-Source Libraries (Task 4)

Tested on 3 PDFs: ACE (19 pages, table-heavy), graph-CNN (5 pages, equation and figure heavy),
LSC (8 pages, moderate tables/figures).

The three open-source options compared here:
- **Docling** (IBM): AI-based layout and table detection, outputs structured Markdown.
- **MarkItDown** (Microsoft): fast converter that outputs Markdown straight from the PDF text.
- **pypdf / pdfplumber**: plain Python libraries (Task 1). They return raw text, and pdfplumber can also pull tables and images, but any structure has to be built by hand.

## Speed

| PDF        | Docling | MarkItDown | pypdf (text) | pdfplumber (text + tables) |
|------------|---------|------------|--------------|----------------------------|
| ACE        | 195.0s  | 1.6s       | 0.5s         | 2.3s                       |
| graph-CNN  | 23.0s   | 3.0s       | 0.9s         | 5.4s                       |
| LSC        | 44.6s   | ~2s        | 0.2s         | 0.9s                       |

pypdf is the fastest because it only reads the text layer. MarkItDown is close behind.
Docling is far slower because it runs layout detection and OCR models.

## What each one extracts

| | Docling | MarkItDown | pypdf | pdfplumber |
|---|---|---|---|---|
| Output format | Markdown | Markdown | Plain text | Plain text, tables as lists |
| Headings / structure | Yes | Partly | No | No |
| Tables | Found 13 in ACE, rendered as Markdown tables | Not detected, shredded into fragments | None | Found 13 in ACE, 3 in graph-CNN, 0 in LSC (some on ACE are false positives) |
| Images / figures | Marked in the output | Shredded into table fragments | None | Embedded pictures only; vector figures need extra code |
| Setup | Heavy (models, ~10-20 min) | Light | Very light | Very light |

## Output quality (tested on ACE, Table 1 and surrounding text)

**Docling:**
- Preserved word spacing and paragraph structure correctly
- Rendered Table 1 as a clean, proper Markdown table with aligned columns
- Struggled with complex multi-line table cells (Table 2), merging
  content from adjacent rows incorrectly
- Formulas it could not convert were flagged with a placeholder
  instead of being corrupted

**MarkItDown:**
- Lost spacing between words throughout the entire document
  (e.g. "ZhendongMi1,ZhenglunKong2" with no spaces)
- Figures and diagrams were shredded into meaningless tiny table fragments
- Mathematical formulas were mangled into broken table cells, unreadable

**pypdf:**
- Text came out complete and readable, but as one flat stream with no headings,
  no table structure and two-column pages read in a mixed order
- Extracted about 61k characters from ACE, 35k from LSC and 26k from graph-CNN
- Useful as a quick, reliable text layer, not as a Markdown converter

**pdfplumber:**
- Text matched pypdf closely (slightly fewer characters because of whitespace handling)
- Its table finder is the only open-source-library feature that gets close to Docling,
  but it relies on ruled lines. It found 0 of the tables in LSC, and several of the
  "tables" it reported on ACE were really parts of figures
- Needs custom code to get figures out (the embedded-image list misses vector drawings),
  which is what the final pipeline does
- On an earlier run it hit "Exceeded 5000 form XObject invocations" on graph-CNN
  and stopped early, so it needs fallback handling

## Conclusion

- **Docling** is slower but far more reliable on complex, multimodal academic PDFs.
  It is the best choice when tables and structure matter.
- **MarkItDown** is fast and gives Markdown directly, but is not trustworthy beyond
  simple, text-only documents.
- **pypdf / pdfplumber** are the fastest and lightest, with no Markdown or layout
  understanding. They work well as building blocks (this project uses pdfplumber for
  the separate tables and images), but would need a lot of custom code to match Docling.
- In the deployed app, MarkItDown is used for the Markdown because Docling runs out of
  memory on Render's free tier, and pdfplumber handles the separate text, table and image files.
