"""Facts a reader of a PDF keeps with what they measure: the rules, and the rules with pages read by sight.

    python scripts/extraction_facts.py report.pdf scripts/facts_td_annual_report_2025.json
    python scripts/extraction_facts.py report.pdf facts.json --sight     # a paid model reads the unsure pages

A fact is a number printed on a page and what it measures, "4.6%" and
"dividend yield". It is read when both are in one paragraph of that page's
text, because a paragraph is the least a search result or a chunk holds: a
figure in one paragraph and its label in the next is a figure nobody finds
by what it means. Only the pages the facts are on are read, cut out of the
document first, so a reading by sight costs those pages and no more.

With --sight, the pages the rules are unsure of are read by the chat model
the settings name, as the extraction service reads them with
VECTRIXDB_EXTRACT_PDF=vision: AZURE_OPENAI_VISION_DEPLOYMENT with
AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_KEY, or AZURE_OPENAI_PAGE_DEPLOYMENT
for pages alone. Each page is a paid call. Not run by the suite.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vectrixdb.ingest import load  # noqa: E402


# ============================================================================
# SETTINGS: the command line
# ============================================================================
#
# The PDF, the facts, and whether a model reads the unsure pages.


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("pdf", type=Path, help="the document the facts are printed in")
    parser.add_argument("facts", type=Path, help="a JSON file of facts: page, number, label")
    parser.add_argument("--sight", action="store_true", help="read the unsure pages with the chat model the settings name (paid)")
    parser.add_argument("--save", type=Path, help="a folder to write each reading's text to, rules.md and sight.md, to read them side by side")
    return parser.parse_args()


# ============================================================================
# THE PAGES AND THE FACTS
# ============================================================================
#
# INPUT   a PDF and the pages wanted; a reading of them; a fact
# OUTPUT  the pages as a PDF of their own; each page's paragraphs; whether a
#         fact's number and label are in one paragraph


def cut(pdf: Path, pages: List[int], folder: Path) -> Path:
    """The pages wanted, in order, as a PDF of their own."""
    from pypdf import PdfReader, PdfWriter

    reader, writer = PdfReader(str(pdf)), PdfWriter()
    for number in pages:
        writer.add_page(reader.pages[number - 1])
    out = folder / pdf.name
    with open(out, "wb") as fh:
        writer.write(fh)
    return out


def paragraphs(doc: Any, index: int) -> List[str]:
    """One page's text, a paragraph at a time, in lower case."""
    starts = [offset for offset, _number in doc.pages] + [len(doc.text)]
    text = doc.text[starts[index] : starts[index + 1]] if index < len(doc.pages) else ""
    return [" ".join(block.split()).lower() for block in text.split("\n\n") if block.strip()]


def read(fact: Dict[str, Any], blocks: List[str]) -> bool:
    number, label = str(fact["number"]).lower(), " ".join(str(fact["label"]).split()).lower()
    return any(number in block and label in block for block in blocks)


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   a PDF, its facts, --sight
# OUTPUT  each fact read or not, by the rules and by sight, and how many of
#         them each kept; which pages the model read and which it left


def main() -> int:
    args = arguments()
    wanted = json.loads(args.facts.read_text(encoding="utf-8"))["facts"]
    pages = sorted({int(f["page"]) for f in wanted})
    where = {number: index for index, number in enumerate(pages)}
    reader: Optional[Any] = None
    if args.sight:
        from vectrixdb.extract.page_reader import PageReader

        reader = PageReader.from_environment()
        if reader is None:
            print("--sight needs AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY and AZURE_OPENAI_VISION_DEPLOYMENT in the environment")
            return 2
    with tempfile.TemporaryDirectory() as folder:
        part = cut(args.pdf, pages, Path(folder))
        readings: List[Tuple[str, Any]] = [("rules", load(part))]
        if reader is not None:
            readings.append(("sight", load(part, page_reader=reader)))
    if args.save:
        args.save.mkdir(parents=True, exist_ok=True)
        for name, doc in readings:
            starts = [offset for offset, _n in doc.pages] + [len(doc.text)]
            written = "\n\n".join(
                f"<!-- page {pages[i]} -->\n\n{doc.text[starts[i]:starts[i + 1]].strip()}" for i in range(len(doc.pages))
            )
            (args.save / f"{name}.md").write_text(written + "\n", encoding="utf-8")
    print(f"{len(wanted)} facts on pages {', '.join(str(p) for p in pages)} of {args.pdf.name}\n")
    print(f"  {'page':>4}  {'number':<14} {'label':<40} " + "  ".join(f"{name:>5}" for name, _doc in readings))
    kept = {name: 0 for name, _doc in readings}
    for fact in wanted:
        marks = []
        for name, doc in readings:
            ok = read(fact, paragraphs(doc, where[int(fact["page"])]))
            kept[name] += ok
            marks.append(f"{'yes' if ok else 'no':>5}")
        print(f"  {fact['page']:>4}  {fact['number']:<14} {fact['label'][:40]:<40} " + "  ".join(marks))
    print()
    for name, doc in readings:
        print(f"  {name:<6} {kept[name]} of {len(wanted)} facts kept with what they measure")
        if name == "sight":
            meta = doc.metadata
            read_pages = [pages[n - 1] for n in meta.get("pages_read_by_sight", [])]
            left = {pages[int(n) - 1]: why for n, why in (meta.get("pages_kept_by_rules") or {}).items()}
            print(f"         read by sight: pages {read_pages or 'none'}; left to the rules: {left or 'none'}; "
                  f"numbers taken out as not on the page: {meta.get('numbers_not_on_page', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
