"""Key-value pairs: "Label: value" lines such as Keywords, Received, DOI or Affiliation."""
import re

PAIR = re.compile(r"^\s*(?P<key>[A-Z][A-Za-z0-9 /&()'’.-]{1,40}?)\s*[:∶]\s+(?P<value>\S.{0,300}?)\s*$")
# first words that start a sentence, not a label ("Note: ..." is a label, "However: ..." is not)
NOT_LABELS = {"figure", "fig", "table", "algorithm", "equation", "eq", "section", "where", "then", "thus",
              "however", "therefore", "note", "e.g", "i.e", "step", "proof", "require", "ensure", "task", "part", "chapter", "phase", "example", "challenge"}


STOP = {"of", "the", "for", "a", "an", "and", "to", "in", "on", "is", "this", "who", "what", "when", "how", "by", "as", "at", "or"}


def find_pairs(text):
    """Pairs found in one block of text. The block is split on line breaks first."""
    pairs = []
    for line in re.split(r"\s{2,}\n|\n", text):
        m = PAIR.match(line.strip(" *"))
        if not m:
            continue
        key, value = m.group("key").strip(), m.group("value").strip()
        words = key.split()
        if len(words) > 5 or words[0].lower().rstrip(".") in NOT_LABELS:
            continue
        if key.lower().startswith(("http", "https")) or value.startswith("//"):
            continue
        if any(w[:1].islower() and w.lower() not in STOP for w in words) or len(value) > 200:
            continue  # a sentence start or a paragraph, not a label
        pairs.append((key, value))
    return pairs


def pair_confidence(key, value):
    """A short label followed by a short value is a clear pair. A long value is less sure."""
    score = 0.9
    if len(key.split()) > 3:
        score -= 0.1
    if len(value) > 160:
        score -= 0.15
    if key.islower():
        score -= 0.2
    # a real label is mostly capitalized words; "Extraction gives you pieces" is the start of a sentence
    plain = [w for w in key.split() if w[:1].islower() and w.lower() not in STOP]
    score -= 0.2 * len(plain)
    return max(0.3, score)