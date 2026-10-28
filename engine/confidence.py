"""Confidence scores. Each one is a plain rule that can be read and tested, not a model output.

Text: does it look like real words? Formula: did the LaTeX pass the quality gate? Table: are the
rows and columns consistent? Image: was it found from a caption? Scores below LOW (0.6) are flagged.
"""
import re

LOW = 0.6

COMMON = set("""the of and to in a is that for it as with was on are by this be at or from an have not but
we our can which their these has were also been more such than when using used use each between into
may would should results method model data paper show shown figure table section where while however
therefore thus then if all one two both only other over under after before based given""".split())

WORD = re.compile(r"^[A-Za-z][A-Za-z'’\-]*[.,;:!?)]*$")
# emails, links, numbers and citations are fine inside text, they are not scrambled words
SPECIAL = re.compile(r"@|://|^www\.|^\(?\d|^\[\d|^[\d.,%()\[\]-]+$|^\W+$")


def text_confidence(text):
    """Near 1 for ordinary prose, lower for scrambled, run-together or symbol-heavy text."""
    tokens = text.split()
    if not tokens:
        return 0.0
    n = len(tokens)
    score = 0.97
    if "�" in text:
        score -= 0.4
    # characters that are not letters, digits, spaces or common punctuation
    odd = sum(1 for c in text if not (c.isalnum() or c.isspace() or c in ".,;:!?()[]{}'\"’“”-–—/%&+=<>*_#@$^|\\"))
    score -= min(0.4, 2.0 * odd / max(len(text), 1))
    # words run together ("Wecompare", "withper-zoneMPC")
    glued = sum(1 for t in tokens if len(re.sub(r"[^A-Za-z]", "", t)) > 22 and "@" not in t and "://" not in t)
    score -= min(0.3, 0.1 * glued)
    if n >= 8:
        wordlike = sum(1 for t in tokens if WORD.match(t) or SPECIAL.search(t)) / n
        score -= max(0.0, 0.8 - wordlike) * 0.8
        singles = sum(1 for t in tokens if len(t) == 1 and t.isalpha() and t.lower() not in "ai") / n
        score -= max(0.0, singles - 0.12) * 1.5
        common = sum(1 for t in tokens if t.lower().strip(".,;:()") in COMMON) / n
        if common < 0.04 and n >= 15:
            score -= 0.15  # prose almost always contains some common words
    return max(0.05, min(0.99, score))


def heading_confidence(text):
    score = 0.93
    if len(text) > 120:
        score -= 0.25
    if text.endswith((".", ",")) and len(text.split()) > 8:
        score -= 0.2  # looks like a sentence
    return max(0.2, score)


def formula_confidence(latex):
    """LaTeX that passed the reader's quality gate is ASCII only; anything else is the plain-symbol fallback."""
    if any(ord(c) > 127 for c in latex):
        return 0.4, ["formula_not_rebuilt"]
    score = 0.9
    if len(latex) > 300:
        score -= 0.1
    if latex.count("{") != latex.count("}"):
        return 0.3, ["formula_not_rebuilt"]
    return score, []


def table_confidence(rows):
    """Consistent column counts, filled cells and a text header row give a high score."""
    if not rows or len(rows) < 2:
        return 0.2
    width = max(len(r) for r in rows)
    cells = [c for r in rows for c in r]
    fill = sum(1 for c in cells if c and c.strip()) / max(len(cells), 1)
    consistent = sum(1 for r in rows if len(r) == width) / len(rows)
    header = rows[0]
    header_ok = sum(1 for c in header if c and re.search(r"[A-Za-z]", c)) / max(len(header), 1)
    glued = sum(1 for c in cells if c and len(re.sub(r"[^A-Za-z]", "", c)) > 25)
    score = 0.35 + 0.3 * fill + 0.15 * consistent + 0.15 * header_ok - min(0.2, 0.05 * glued)
    return max(0.1, min(0.97, score))


def image_confidence(has_caption):
    return 0.95 if has_caption else 0.7