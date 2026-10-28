"""The engine's output: a list of typed elements in reading order, each with a confidence."""
from dataclasses import dataclass, field, asdict

ELEMENT_TYPES = ("heading", "text", "table", "formula", "key_value", "image", "header", "footer")


@dataclass
class Element:
    type: str                      # one of ELEMENT_TYPES
    page: int                      # 1-based page number (1 for web pages)
    content: str                   # heading/text: text, formula: LaTeX, table: Markdown, image: link
    confidence: float              # 0 to 1, how sure the engine is that this element is right
    source: str                    # how it was produced, e.g. "reader", "pdfplumber", "ai:<model>", "azure"
    flags: list = field(default_factory=list)   # e.g. "low_confidence", "formula_not_rebuilt", "ai_repaired"
    data: dict = field(default_factory=dict)    # type-specific extras (heading level, table rows, key/value)
    id: str = ""
    in_markdown: bool = True       # key_value elements are derived from text and are not printed twice

    def to_dict(self):
        d = asdict(self)
        d["confidence"] = round(self.confidence, 3)
        return d


@dataclass
class Document:
    source: str
    level: str
    elements: list = field(default_factory=list)
    pages: int = 0
    cost: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def number(self):
        for i, element in enumerate(self.elements, start=1):
            element.id = f"e{i}"

    def to_markdown(self):
        out = []
        for e in self.elements:
            if not e.in_markdown:
                continue
            if e.type == "heading":
                out.append("#" * int(e.data.get("level", 2)) + " " + e.content)
            elif e.type == "formula":
                number = e.data.get("number")
                out.append(f"$$ {e.content} $$" + (f"  *({number})*" if number else ""))
            elif e.type in ("header", "footer"):
                out.append("*" + e.content.replace("*", "\\*") + "*")
            else:
                out.append(e.content)
        return "\n\n".join(out) + "\n"

    def stats(self):
        scored = [e for e in self.elements if e.in_markdown]
        by_type = {}
        for e in scored:
            by_type.setdefault(e.type, []).append(e.confidence)
        return {
            "elements": len(self.elements),
            "average_confidence": round(sum(e.confidence for e in scored) / len(scored), 3) if scored else 0,
            "low_confidence": sum(1 for e in scored if "low_confidence" in e.flags),
            "ai_repaired": sum(1 for e in scored if "ai_repaired" in e.flags),
            "by_type": {t: {"count": len(v), "average_confidence": round(sum(v) / len(v), 3)} for t, v in by_type.items()},
        }

    def to_json(self):
        self.number()
        return {
            "source": self.source,
            "level": self.level,
            "pages": self.pages,
            "stats": self.stats(),
            "cost": self.cost,
            "timings": self.timings,
            "warnings": self.warnings,
            "elements": [e.to_dict() for e in self.elements],
        }