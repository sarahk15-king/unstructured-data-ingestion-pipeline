"""python -m engine.cli paper.pdf --out result/      (also accepts a URL)"""
import argparse
import json
from pathlib import Path

from .pipeline import extract_pdf, extract_url


def main():
    parser = argparse.ArgumentParser(description="Extract a PDF or URL to Markdown and JSON")
    parser.add_argument("source")
    parser.add_argument("--out", default="result")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.source.startswith(("http://", "https://")):
        doc = extract_url(args.source, out_dir=out)
    else:
        doc = extract_pdf(args.source, out_dir=out)
    (out / "output.md").write_text(doc.to_markdown(), encoding="utf-8")
    (out / "output.json").write_text(json.dumps(doc.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")
    s = doc.stats()
    print(f"{doc.pages} pages, {s['elements']} elements, average confidence {s['average_confidence']}, "
          f"{s['low_confidence']} low confidence, {doc.timings.get('total_s')} s")
    for w in doc.warnings:
        print("warning:", w)


if __name__ == "__main__":
    main()