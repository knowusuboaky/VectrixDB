"""The reading order, on what the real OCR engine finds.

    pip install "vectrixdb[ocr]"
    python scripts/ocr_order_check.py                 # pages drawn here
    python scripts/ocr_order_check.py scan1.png ...   # your own page images

With no arguments it draws pages the way a scanner gives them, real prose set
in two and three columns, a heading across the columns, a table, a page turned
a little and speckled, and reads each with RapidOCR. It prints how alike the
text is to what a person reads, word for word, twice: with the engine's boxes
sorted by their tops, which is all the library did before 2.2, and in the
order :func:`vectrixdb.extract.layout.reading_order` gives. What is left under
1.0 in the second column is the engine's own reading, a dropped space, a
misread letter, and not the order.

Given image files, it prints each page as the library now reads it, so the
order can be checked by eye against the page. Not run by the suite, which
installs no OCR engine.
"""

from __future__ import annotations

import difflib
import io
import random
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vectrixdb.extract.layout import looks_blank, mend_sentence_breaks, reading_order  # noqa: E402


# ============================================================================
# SETTINGS: the columns of text the pages are drawn from
# ============================================================================
#
# Real prose, in the columns a scanned page would carry: a left, a right and a
# third.

W, H = 1700, 2200
LEFT = (
    "Customers in a declared disaster area can defer loan payments for up to ninety days with no late fees. "
    "Interest still accrues during the deferral, and an adviser calls within a day to confirm the new schedule. "
    "Displaced customers get expedited access to their funds and waived withdrawal fees for thirty days."
)
RIGHT = (
    "Anyone who needs to sell investments before maturity is shown the penalty before they confirm the sale. "
    "Statements are issued at the end of each quarter and kept for seven years. "
    "Disputes are raised in writing and answered within ten working days of the letter arriving."
)
THIRD = (
    "Refunds go back to the account the payment came from. A reminder goes out on the fifth day, "
    "and a second one a fortnight later. Nothing in these terms limits a right the law gives."
)


# ============================================================================
# DRAWING THE PAGES
# ============================================================================
#
# INPUT   nothing
# OUTPUT  (name, PNG bytes, what a person reads) for each page: two and three
#         columns, a heading across them, a table, and a page turned a little
#         and speckled
#
# Pages the way a scanner gives them, drawn here, so the check needs no
# fixture files and no scanner.


def _fonts():
    from PIL import ImageFont

    for body, bold in (
        (r"C:\Windows\Fonts\georgia.ttf", r"C:\Windows\Fonts\georgiab.ttf"),
        ("DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf"),
    ):
        try:
            return ImageFont.truetype(body, 26), ImageFont.truetype(bold, 36)
        except OSError:
            continue
    return ImageFont.load_default(26), ImageFont.load_default(36)


def _column(draw, font, text, x, y, chars):
    lines = textwrap.wrap(text, chars)
    for n, line in enumerate(lines):
        draw.text((x, y + n * 40), line, font=font, fill=20)
    return " ".join(lines)


def _png(image, turn=0.0, speckle=0):
    from PIL import Image

    if turn:
        image = image.rotate(turn, resample=Image.Resampling.BICUBIC, fillcolor=250)
    if speckle:
        rng, pixels = random.Random(5), image.load()
        for _ in range(speckle):
            pixels[rng.randrange(W), rng.randrange(H)] = rng.randrange(0, 90)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def drawn_pages():
    """``(name, PNG bytes, what a person reads)`` for each page."""
    from PIL import Image, ImageDraw

    body, head = _fonts()

    def fresh():
        image = Image.new("L", (W, H), 250)
        return image, ImageDraw.Draw(image)

    image, d = fresh()
    wanted = _column(d, body, LEFT, 120, 300, 48) + " " + _column(d, body, RIGHT, 920, 300, 48)
    yield "two columns", _png(image), wanted
    yield "two columns, turned 1.2 degrees, speckled", _png(image, turn=1.2, speckle=900), wanted

    image, d = fresh()
    title = "Terms for customers in a declared disaster area"
    d.text((120, 180), title, font=head, fill=20)
    wanted = (
        title
        + " "
        + _column(d, body, LEFT, 120, 300, 48)
        + " "
        + _column(d, body, RIGHT, 920, 300, 48)
    )
    yield "a heading across two columns", _png(image), wanted

    image, d = fresh()
    wanted = " ".join(
        _column(d, body, t, x, 300, 30) for t, x in ((LEFT, 90), (RIGHT, 630), (THIRD, 1170))
    )
    yield "three columns", _png(image), wanted

    image, d = fresh()
    rows = [
        ("Region", "Revenue", "Owner"),
        ("EMEA", "1200", "Ama"),
        ("APAC", "900", "Olu"),
        ("LATAM", "400", "Vi"),
        ("NORTH", "750", "Kofi"),
    ]
    for n, row in enumerate(rows):
        for m, cell in enumerate(row):
            d.text((160 + 520 * m, 300 + 70 * n), cell, font=body, fill=20)
    yield "a table, read across", _png(image), " ".join(" ".join(r) for r in rows)

    image, d = fresh()
    wanted = _column(d, body, LEFT + " " + RIGHT, 120, 300, 100)
    yield "one column", _png(image), wanted


# ============================================================================
# COMPARING: what was read against what was drawn
# ============================================================================
#
# INPUT   what the engine read, and what was wanted
# OUTPUT  how alike they are, word for word, from 0 to 1
#
# Word for word, so a dropped space or a misread letter costs a little and a
# wrong order costs a lot.


def _alike(got: str, wanted: str) -> float:
    return difflib.SequenceMatcher(
        None, got.lower().split(), wanted.lower().split(), autojunk=False
    ).ratio()


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   nothing, or your own page images
# OUTPUT  each drawn page's likeness twice, with the engine's boxes sorted by
#         their tops and in reading order; or each given page as the library
#         now reads it
#
# What is left under 1.0 in the second column is the engine's own reading and
# not the order. Not run by the suite, which installs no OCR engine.


def main(argv: list) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("pages", nargs="*", help="page images to read; none draws the pages here")
    args = parser.parse_args(argv)

    # After parsing, so --help answers on a machine without the extra.
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        print('this needs the OCR engine: pip install "vectrixdb[ocr]"')
        return 2
    model = RapidOCR()

    if args.pages:
        for name in args.pages:
            data = Path(name).read_bytes()
            print(
                f"== {name}"
                + ("  (judged blank: not sent to the engine)" if looks_blank(data) else "")
            )
            found, _ = model(data)
            for line in reading_order([(r[0], str(r[1])) for r in found or []]):
                print("  ", mend_sentence_breaks(line))
        return 0

    print(f"{'page':44} {'boxes':>5} {'by their tops':>14} {'reading order':>14}")
    for name, image, wanted in drawn_pages():
        found, _ = model(image)
        found = found or []
        by_tops = " ".join(
            str(r[1])
            for r in sorted(found, key=lambda r: (min(p[1] for p in r[0]), min(p[0] for p in r[0])))
        )
        in_order = " ".join(
            mend_sentence_breaks(line) for line in reading_order([(r[0], str(r[1])) for r in found])
        )
        print(
            f"{name:44} {len(found):5d} {_alike(by_tops, wanted):14.3f} {_alike(in_order, wanted):14.3f}"
        )
    from PIL import Image

    blank = _png(Image.new("L", (W, H), 250), speckle=900)
    print(f"\nan empty, speckled page is judged blank: {looks_blank(blank)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
