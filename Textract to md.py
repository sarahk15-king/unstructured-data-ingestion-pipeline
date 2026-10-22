"""Turns saved Textract JSON files into Markdown so the benchmark can score them.

python eval/textract_to_md.py outputs/textract eval/external/textract

Reads every *_textract.json in the first folder and writes <paper>.md files into the second.
The conversion itself lives in api/textract_md.py, so the app and the benchmark use the same code.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api.textract_md import load_blocks, textract_to_markdown


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
