"""Turns Markdown (from the column reader, Azure or an AI answer) into typed elements."""
import re

from . import confidence as conf
from . import kv
from .schema import Element

FORMULA = re.compile(r"^\$\$\s*(?P<latex>.*?)\s*\$\$(?:\s*\*\((?P<number>[^)]*)\)\*)?\s*$", re.S)
HEADING = re.compile(r"^(#{1,6})\s+(?P<text>.+)$")
ITALIC_LINE = re.compile(r"^\*(?!\*)(.+?)\*$")
TABLE_LINE = re.compile(r"^\|.*\|\s*$")
MATH_WORDS = {"diag", "argmin", "argmax", "softmax", "sigma", "lambda", "theta", "exp", "sin", "cos", "tan", "log", "lim", "max", "min"}


def rows_from_markdown_table(block):
    rows = []
    for line in block.strip().splitlines():
        cells = [c.strip().replace("\\|", "|") for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        rows.append(cells)
    return rows


def table_markdown(rows):
    """Rows (first row = header) as a Markdown pipe table."""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [[(c or "").replace("\n", " ").replace("|", "\\|").strip() for c in r] + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


ROMAN = re.compile(r"^(?P<num>[IVX]{1,5})\.\s+(?P<title>[A-Z][A-Z0-9\-&,]*(?:\s+[A-Z][A-Z0-9\-&,]*)*)(?P<rest>\s+\S.*)?$", re.S)
LETTER = re.compile(r"^(?P<num>[A-H])\.\s+(?P<title>[A-Z][\w\-]*(?:\s+[A-Za-z(][\w\-()]*){0,9})$")
LETTER_RUN_IN = re.compile(r"^(?P<num>[A-H])\.\s+(?P<words>(?:[A-Z][\w\-()]*|and|of|the|for|in|to|on|with|a|vs\.?)(?:\s+(?:[A-Z][\w\-()]*|and|of|the|for|in|to|on|with|a|vs\.?)){0,7})\s+(?P<rest>[a-z]\S*.*)$", re.S)
STARTERS = {"The", "We", "In", "A", "An", "This", "To", "As", "For", "Our", "It", "Let", "Given", "These", "There", "One", "When", "If", "Each", "All", "Since", "Here", "At", "On", "Although", "Traditional", "Several", "Many", "Most", "Recent", "Existing", "Some", "Such", "Both", "Despite", "While", "Previous", "Another", "Other", "Different", "Typically"}
RUN_IN = re.compile(r"^(?P<title>(?:Abstract|Index Terms|Keywords|Summary))\s*[—–-]+\s*(?P<rest>\S.*)$", re.S)


def split_heading(text):
    """IEEE-style headings ("I. INTRODUCTION Text...", "B. Problem Formulation", "Abstract—Text...").
    Returns (heading, level, rest) or None."""
    flat = " ".join(text.split())
    m = RUN_IN.match(flat)
    if m:
        return m.group("title"), 2, m.group("rest")
    m = ROMAN.match(flat)
    if m and len(m.group("title")) >= 4:
        return f"{m.group('num')}. {m.group('title')}", 2, (m.group("rest") or "").strip()
    m = LETTER.match(flat)
    if m and len(flat) <= 90 and not flat.endswith("."):
        return flat, 3, ""
    m = LETTER_RUN_IN.match(flat)
    if m:
        words = m.group("words").split()
        rest = m.group("rest")
        if words and words[-1] in STARTERS:
            rest = words.pop() + " " + rest
        if words:
            return f"{m.group('num')}. {' '.join(words)}", 3, rest
    return None


EMBEDDED = re.compile(r"(?:(?<=[.!?])|(?<=\w))\s+(?P<h>(?:[IVX]{1,5})\.\s+[A-Z][A-Z\-&]{2,}(?:\s+[A-Z][A-Z\-&]{2,}){1,6})(?=\s+(?:[A-Z][a-z]|[A-H]\.\s)|\s*$)|(?<=[.!?])\s+(?P<h2>(?:[IVX]{1,5})\.\s+[A-Z][A-Z\-&]{2,})(?=\s+(?:[A-Z][a-z]|[A-H]\.\s)|\s*$)|(?<=[.!?])\s+(?P<h3>[A-H]\.\s+[A-Z][a-z]+(?:\s+(?:[A-Z][a-z]+|and|of|the|for|in)){1,5})(?=\s+(?:TABLE|Table|FIG|Fig|Figure|Algorithm)\b)")


END_HEADING = re.compile(r"^(?P<before>.*\S)\s+(?P<h>[A-H]\.\s+[A-Z][a-z]+(?:\s+(?:[A-Z][a-z]+|and|of|the|for|in)){1,4})$", re.S)


def heading_level(text):
    """Lettered sub-headings ("B. Evaluation Metrics") are one level below numbered sections ("III. METHODS")."""
    return 3 if re.match(r"^[A-H]\.\s", text) else 2


def split_embedded(text):
    """A section heading glued to the end of a paragraph: "... end. III. PROPOSED METHOD Text". Returns parts."""
    parts, last = [], 0
    for m in EMBEDDED.finditer(text):
        before = text[last:m.start()].strip()
        if before:
            parts.append(("text", before))
        parts.append(("heading", m.group("h") or m.group("h2") or m.group("h3")))
        last = m.end()
    rest = text[last:].strip()
    # a sub-heading stuck to the very end of a block ("... cluster centers C B. Evaluation Metrics")
    m = END_HEADING.match(rest)
    if m:
        parts.append(("text", m.group("before")))
        parts.append(("heading", m.group("h")))
    elif rest:
        parts.append(("text", rest))
    return parts


def text_elements(text, page, source):
    """Text after a heading may start with another heading ("III. METHODOLOGY A. Line Space ..."), so split again."""
    out = []
    split = split_heading(text)
    if split:
        title, level, rest = split
        out.append(Element("heading", page, title, 0.88, source, data={"level": level}))
        if rest:
            out += text_elements(rest, page, source)
        return out
    for kind, part in split_embedded(text):
        if kind == "heading":
            out.append(Element("heading", page, part, 0.85, source, data={"level": heading_level(part)}))
        else:
            out.append(Element("text", page, part, conf.text_confidence(part), source))
    return out


def blocks_to_elements(markdown, page, source, first_is_header=True):
    """One element per Markdown block. Italic blocks at the top or bottom of a page are header/footer."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", markdown) if b.strip()]
    elements = []
    for index, block in enumerate(blocks):
        lines = [l.strip() for l in re.split(r"\n", block) if l.strip()]
        m = HEADING.match(block)
        if m and "\n" not in block:
            text = m.group("text").replace("**", "").strip()
            split = RUN_IN.match(text)
            if split:
                elements.append(Element("heading", page, split.group("title"), 0.9, source, data={"level": 2}))
                rest = split.group("rest")
                elements.append(Element("text", page, rest, conf.text_confidence(rest), source))
                continue
            elements.append(Element("heading", page, text, conf.heading_confidence(text), source,
                                    data={"level": min(len(m.group(1)), 4)}))
            continue
        m = FORMULA.match(block)
        if m:
            latex = m.group("latex")
            score, flags = conf.formula_confidence(latex)
            data = {"number": m.group("number")} if m.group("number") else {}
            words = [w for w in re.findall(r"[A-Za-z]{4,}", latex) if w.lower() not in MATH_WORDS]
            if flags and (len(words) >= 2 or latex.lstrip().startswith(("•", "-", "–"))):
                # not a formula: a sentence or list item that contains symbols
                elements.append(Element("text", page, latex, conf.text_confidence(latex), source, flags=["math_in_text"]))
                continue
            elements.append(Element("formula", page, latex, score, source, flags=list(flags), data=data))
            continue
        if all(ITALIC_LINE.match(l.rstrip(" ")) for l in lines) and not lines[0].startswith("**"):
            text = "  ".join(ITALIC_LINE.match(l.rstrip(" ")).group(1).replace("\\*", "*") for l in lines)
            kind = "header" if index == 0 and first_is_header and len(blocks) > 1 else "footer" if index > 0 else "header"
            elements.append(Element(kind, page, text, 0.9, source))
            continue
        if all(TABLE_LINE.match(l) for l in lines) and len(lines) >= 2:
            rows = rows_from_markdown_table(block)
            elements.append(Element("table", page, block, conf.table_confidence(rows), source,
                                    data={"rows": rows}))
            continue
        if not any(l.startswith(("- ", "* ", "|")) for l in lines):
            split = split_heading(block)
            if split:
                title, level, rest = split
                elements.append(Element("heading", page, title, 0.88, source, flags=[], data={"level": level}))
                if rest:
                    elements += text_elements(rest, page, source)
                continue
        flat = " ".join(lines) if len(lines) > 1 and not any(l.startswith(("- ", "* ")) for l in lines) else block
        for kind, part in split_embedded(flat):
            if kind == "heading":
                elements.append(Element("heading", page, part, 0.85, source, data={"level": heading_level(part)}))
            else:
                elements.append(Element("text", page, part, conf.text_confidence(part), source))
    return elements


def add_key_values(elements):
    """Derived key-value elements, placed right after the text they were found in."""
    out = []
    for e in elements:
        out.append(e)
        if e.type != "text":
            continue
        for key, value in kv.find_pairs(e.content):
            out.append(Element("key_value", e.page, f"{key}: {value}", kv.pair_confidence(key, value), "pattern",
                               data={"key": key, "value": value}, in_markdown=False))
    return out


def demote_repeated_headings(elements):
    """A short unnumbered "heading" that repeats on many pages is a table column name, not a section."""
    from collections import Counter
    count = Counter(e.content for e in elements if e.type == "heading" and len(e.content.split()) <= 2
                    and not e.content[:1].isdigit())
    for e in elements:
        if e.type == "heading" and count.get(e.content, 0) >= 3:
            e.type = "text"
            e.data = {}
            e.flags.append("demoted_heading")
    return elements


def finish_flags(elements):
    demote_repeated_headings(elements)
    for e in elements:
        if e.confidence < conf.LOW and "low_confidence" not in e.flags:
            e.flags.append("low_confidence")
    return elements