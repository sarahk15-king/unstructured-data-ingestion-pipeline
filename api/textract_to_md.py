"""Turns raw Textract output into Markdown. The API imports it, and it also runs on its own:

python api/textract_to_md.py outputs/textract eval/external/textract

The command reads every *_textract.json in the first folder and writes <paper>.md files into the second,
so the benchmark can score Textract.

Textract gives lines, words and table cells with no headings, so headings are found here from
the line's size (Textract's box height) and its numbering. Every other line is a paragraph, and
each table becomes a Markdown table at the place where its first line appears.
"""
import json
import re
import statistics
import sys
from pathlib import Path


def load_blocks(path):
    """The saved file can be a list of blocks, {"Blocks": [...]}, or a list of responses."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("Blocks", data.get("blocks", []))
    blocks = []
    for item in data:
        if isinstance(item, dict) and "Blocks" in item:
            blocks.extend(item["Blocks"])
        else:
            blocks.append(item)
    return blocks


def children(block, by_id, kind="CHILD"):
    for rel in block.get("Relationships", []) or []:
        if rel.get("Type") == kind:
            for cid in rel.get("Ids", []):
                if cid in by_id:
                    yield by_id[cid]


def cell_text(cell, by_id):
    parts = []
    for child in children(cell, by_id):
        if child["BlockType"] == "WORD":
            parts.append(child.get("Text", ""))
        elif child["BlockType"] == "SELECTION_ELEMENT":
            parts.append("[x]" if child.get("SelectionStatus") == "SELECTED" else "[ ]")
    return " ".join(parts).replace("|", "\\|").strip()


def table_rows(table, by_id):
    cells = {}
    for cell in children(table, by_id):
        if cell["BlockType"] == "CELL":
            cells[(cell["RowIndex"], cell["ColumnIndex"])] = cell_text(cell, by_id)
    if not cells:
        return []
    n_rows = max(r for r, _ in cells)
    n_cols = max(c for _, c in cells)
    return [[cells.get((r, c), "") for c in range(1, n_cols + 1)] for r in range(1, n_rows + 1)]


def markdown_table(rows):
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


NUMBERED = re.compile(r"^(?:(\d+(?:\.\d+)*)\.?|([IVX]+)\.|([A-H])\.)\s+[A-Z]")
NAMED = {"abstract", "references", "acknowledgments", "acknowledgements", "conclusion", "conclusions",
         "introduction", "appendix", "bibliography"}


def heading_level(text, height, body_height):
    """1-6 if the line looks like a heading, else 0. Needs a short line that does not end like a sentence."""
    words = text.split()
    if not 1 <= len(words) <= 12 or text.endswith((".", ",", ";", ":")) or text.startswith(("Table", "Figure", "Fig.")):
        return 0
    bigger = body_height and height >= body_height * 1.15
    m = NUMBERED.match(text)
    if m and height >= body_height * 0.97:
        if m.group(1):
            return min(6, m.group(1).count(".") + 2)
        return 2 if m.group(2) else 3
    if text.lower().rstrip(":") in NAMED and height >= body_height * 0.97:
        return 2
    if bigger and len(words) >= 2 and not text.islower():
        return 1
    return 0


def textract_to_markdown(blocks):
    by_id = {b["Id"]: b for b in blocks if "Id" in b}
    # which table does each word belong to?
    word_table = {}
    for block in blocks:
        if block.get("BlockType") == "TABLE":
            for cell in children(block, by_id):
                for word in children(cell, by_id):
                    word_table[word["Id"]] = block["Id"]
    heights = [b["Geometry"]["BoundingBox"]["Height"] for b in blocks
               if b.get("BlockType") == "LINE" and "Geometry" in b]
    body_height = statistics.median(heights) if heights else 0
    out, done = [], set()
    for block in blocks:
        if block.get("BlockType") != "LINE":
            continue
        words = [w["Id"] for w in children(block, by_id) if w["BlockType"] == "WORD"]
        tables = {word_table[w] for w in words if w in word_table}
        if tables and len(tables) == 1 and all(w in word_table for w in words):
            tid = tables.pop()
            if tid not in done:
                done.add(tid)
                rows = table_rows(by_id[tid], by_id)
                if rows:
                    out.append(markdown_table(rows))
            continue
        text = block.get("Text", "").strip()
        if text:
            height = block.get("Geometry", {}).get("BoundingBox", {}).get("Height", 0)
            level = heading_level(text, height, body_height) if body_height else 0
            out.append(f"{'#' * level} {text}" if level else text)
    # tables whose words never showed up as a line still count
    for block in blocks:
        if block.get("BlockType") == "TABLE" and block["Id"] not in done:
            rows = table_rows(block, by_id)
            if rows:
                out.append(markdown_table(rows))
    return "\n\n".join(out) + "\n"


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.glob("*_textract.json")):
        paper = path.name[: -len("_textract.json")]
        md = textract_to_markdown(load_blocks(path))
        (dst / f"{paper}.md").write_text(md, encoding="utf-8")
        print(f"{paper}: {md.count(chr(10))} lines written to {dst / (paper + '.md')}")


if __name__ == "__main__":
    main()
