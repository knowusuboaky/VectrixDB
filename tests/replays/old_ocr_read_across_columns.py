"""Restore OCR results sorted by the top of each box, then its left edge.

That was the whole of the reading order. A page in two columns came out
``L1 R1 L2 R2``, the columns interleaved line by line, and the words of one
line came out of order whenever a scan put their tops a pixel apart: "is
Payment due". Every chunk from such a page was text nobody wrote, and it was
chunked and embedded as if somebody had. Nothing failed.
"""

from vectrixdb.extract import engines, layout


def _by_the_top_of_the_box(items):
    rows = sorted(
        ((layout._rect(box), str(text)) for box, text in items if str(text).strip()),
        key=lambda row: (row[0][1], row[0][0]),
    )
    return [text for _, text in rows]


def pytest_configure(config):
    layout.reading_order = _by_the_top_of_the_box
    engines.reading_order = _by_the_top_of_the_box
    import sys

    module = sys.modules.get("unit.test_extract_layout") or sys.modules.get("test_extract_layout")
    if module is not None:  # pragma: no cover - only when the test module was imported first
        module.reading_order = _by_the_top_of_the_box
