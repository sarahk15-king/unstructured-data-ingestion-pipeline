# Docling vs MarkItDown Comparison (Task 4)

Tested on 3 PDFs: ACE (19 pages, table-heavy), graph-CNN (equation-heavy),
LSC (8 pages, moderate tables/figures).

## Speed

| PDF        | Docling | MarkItDown |
|------------|---------|------------|
| ACE        | 195.0s  | 1.6s       |
| graph-CNN  | 23.0s   | 3.0s       |
| LSC        | 44.6s   | ~2s        |

MarkItDown is dramatically faster, especially on table/image-heavy PDFs
where Docling runs layout detection and OCR models.

## Output quality (tested on ACE.md, Table 1 and surrounding text)

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

## Conclusion

Docling is slower but far more reliable, especially on complex,
multimodal academic PDFs. MarkItDown is fast but not trustworthy for
anything beyond simple, text-only documents.