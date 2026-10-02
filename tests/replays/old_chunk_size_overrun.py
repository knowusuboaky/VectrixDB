"""Restore the chunker that broke its own size limit and repeated offsets.

``chunk_text`` tracked a running total that added a separator for every
split, including the last, and never measured the chunk it was about to
emit, so a carried overlap could push a chunk past ``chunk_size``.
``_recursive_spans`` then mapped each piece back to an offset with a search
that could start at or before the previous match, so repetitive text gave
two chunks the same start.
"""

from vectrixdb.core import document_index
from vectrixdb import ingest

_fixed_chunk_text = document_index.chunk_text
_fixed_spans = ingest._recursive_spans


def _old_chunk_text(text, chunk_size=1000, chunk_overlap=200, separators=None):
    if not text:
        return []
    if separators is None:
        separators = ["\n\n", "\n", ". ", " ", ""]

    def merge(splits, separator):
        chunks, current, size = [], [], 0
        for split in splits:
            split_size = len(split) + len(separator)
            if size + split_size > chunk_size and current:
                chunks.append(separator.join(current))
                overlap_size, overlap = 0, []
                for prev in reversed(current):
                    if overlap_size + len(prev) > chunk_overlap:
                        break
                    overlap.insert(0, prev)
                    overlap_size += len(prev) + len(separator)
                current, size = overlap, overlap_size
            current.append(split)
            size += split_size
        if current:
            chunks.append(separator.join(current))
        return chunks

    for separator in separators:
        splits = text.split(separator) if separator else list(text)
        if all(len(s) <= chunk_size for s in splits):
            return merge(splits, separator)
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size - chunk_overlap)]


def _old_recursive_spans(text, size, overlap):
    pieces = document_index.chunk_text(text, chunk_size=size, chunk_overlap=overlap)
    spans, cursor = [], 0
    for piece in pieces:
        found = text.find(piece, max(0, cursor - overlap - 1))
        if found < 0:
            found = text.find(piece)
        if found < 0:
            continue
        spans.append((found, found + len(piece)))
        cursor = found + len(piece)
    return spans


def pytest_configure(config):
    document_index.chunk_text = _old_chunk_text
    ingest._recursive_spans = _old_recursive_spans
