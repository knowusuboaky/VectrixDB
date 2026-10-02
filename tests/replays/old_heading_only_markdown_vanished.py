"""Restore the Markdown chunker that indexed a document of headings as nothing.

Each section's heading line is dropped from its body, since it rides on the
chunk's metadata, and a section left with no body was skipped. A document
that is only headings, an outline say, had every section skipped and came
back with no chunks at all, so it was never indexed and nothing said so.
"""

from vectrixdb import ingest

_fixed = ingest._markdown_spans


def _old_markdown_spans(doc, size, overlap):
    text = doc.text
    headings = doc.headings or ingest._markdown_headings(text)
    bounds = [0] + [h[0] for h in headings if h[0] > 0] + [len(text)]
    out = []
    for a, b in zip(bounds, bounds[1:]):
        section = text[a:b]
        if not section.strip():
            continue
        m = ingest._HEADING.match(section)
        body_start = a + (m.end() if m else 0)
        if body_start >= b or not text[body_start:b].strip():
            continue
        if b - body_start <= size:
            out.append((body_start, b))
        else:
            out.extend(ingest._recursive_spans_offset(text, (body_start, b), size))
    return out


def pytest_configure(config):
    ingest._markdown_spans = _old_markdown_spans
