"""Turns raw Textract blocks into Markdown. Used by the API and by eval/textract_to_md.py.

Textract gives lines, words and table cells with no headings, so none are invented here:
each line is a paragraph, and each table becomes a Markdown table at the place where its
first line appears in Textract's own order.
"""
import json
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


def textract_to_markdown(blocks):
    by_id = {b["Id"]: b for b in blocks if "Id" in b}
    # which table does each word belong to?
    word_table = {}
    for block in blocks:
        if block.get("BlockType") == "TABLE":
            for cell in children(block, by_id):
                for word in children(cell, by_id):
                    word_table[word["Id"]] = block["Id"]
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
            out.append(text)
    # tables whose words never showed up as a line still count
    for block in blocks:
        if block.get("BlockType") == "TABLE" and block["Id"] not in done:
            rows = table_rows(block, by_id)
            if rows:
                out.append(markdown_table(rows))
    return "\n\n".join(out) + "\n"
