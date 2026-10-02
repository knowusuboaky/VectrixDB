"""Ingestion: loaders, chunkers, near-duplicate detection and the embedding cache.

Retrieval quality is decided here, before any vector exists. A chunk that cuts
a sentence in half embeds as noise; a section returned whole reads as an
answer; the same paragraph indexed twice pushes a better one off the page.

The pieces, each usable on its own:

* :func:`load` turns a file (PDF, DOCX, PPTX, XLSX, CSV, HTML, Markdown,
  text) into a :class:`LoadedDocument` with page and heading positions, and
  hands any suffix somebody registered an extractor for to that extractor
  (see :mod:`vectrixdb.extract`).
* :func:`chunk` splits text by one of four strategies and returns
  :class:`Chunk` objects that know their offsets, page and heading.
* :class:`NearDuplicateIndex` is MinHash over word shingles, persisted beside
  the collection, so ``add(dedupe=0.9)`` can skip a paragraph it already has.
* :class:`EmbeddingCache` keys vectors on content and model, so re-ingesting
  a folder embeds only what changed.
* :class:`ParentStore` keeps the enclosing sections that parent-child
  retrieval returns in place of the small chunk that matched.

``Vectrix.add_document`` and ``Vectrix.search(parents=True)`` wire them
together; nothing here imports the model, so the module is cheap to load.
"""

from __future__ import annotations

import codecs
import hashlib
import html.parser
import io
import json
import re
import sqlite3
import struct
import threading
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

import numpy as np

from .exceptions import DependencyError

__all__ = [
    "Chunk",
    "LoadedDocument",
    "chunk",
    "load",
    "load_bytes",
    "markdown_document",
    "normalise_figures",
    "normalise_tables",
    "rows_to_lines",
    "split_front_matter",
    "PreparedDocument",
    "prepare_document",
    "texts_to_embed",
    "describe_figures",
    "image_size",
    "is_decorative",
    "EXTRACTED_MARKER",
    "NearDuplicateIndex",
    "EmbeddingCache",
    "ParentStore",
    "STRATEGIES",
]


# ============================================================================
# SETTINGS: the strategies
# ============================================================================
#
# The six ways a text can be cut.

STRATEGIES = ("recursive", "sentence", "semantic", "markdown", "fixed", "llm")


# ============================================================================
# LOADED DOCUMENTS
# ============================================================================
#
# INPUT   a file's text
# OUTPUT  text with the positions that give chunks their page and heading; a
#         position looked up; printed page numbers as front matter holds them
#
# Everything a chunk later says about where it came from is decided here.


@dataclass
class LoadedDocument:
    """Text with the positions that give chunks their page and heading."""

    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    # (start offset, page number), ascending. Empty when the source has no pages.
    pages: List[Tuple[int, int]] = field(default_factory=list)
    # (start offset, heading text, level), ascending.
    headings: List[Tuple[int, str, int]] = field(default_factory=list)
    # (offset of the figure's line, what is known about it), ascending. The
    # dict carries ``caption``, ``src`` when the figure came from an image,
    # and ``described`` once a description sits under the line.
    figures: List[Tuple[int, Dict[str, Any]]] = field(default_factory=list)
    # Image bytes by the ``src`` a figure names. Never written to the index;
    # a document store keeps them beside the Markdown.
    images: Dict[str, bytes] = field(default_factory=dict, repr=False)
    #: Speech, timed: ``(start, end, text)`` a phrase, in seconds, as the
    #: engine heard it. The text above groups a recording into a paragraph a
    #: minute, which is what a chunk and a citation want; these are what a
    #: person reading the transcript wants, each phrase with when it was said.
    #: A field and not metadata, because metadata is copied onto every chunk,
    #: and an hour of phrases on each of them would be most of the index.
    segments: List[Tuple[float, float, str]] = field(default_factory=list, repr=False)
    #: The number printed on each page, by its place in the file, where the
    #: PDF's own page-label table says the two differ: ``{41: "39"}`` for a
    #: report whose covers are C1 and C2. A citation's link keeps the place,
    #: which opens the right page in any viewer and is unique within the
    #: file; a person is shown the printed number, which is what the page
    #: says. Empty for a file with no such table.
    page_labels: Dict[int, str] = field(default_factory=dict, repr=False)

    def page_label(self, page: Optional[int]) -> Optional[str]:
        """The number printed on ``page``, when the file says it is not ``page`` itself."""
        if page is None or not self.page_labels:
            return None
        return self.page_labels.get(int(page))

    def figure_at(self, offset: int) -> Optional[Dict[str, Any]]:
        """The figure whose line starts exactly here, if there is one."""
        for start, info in self.figures:
            if start == offset:
                return info
            if start > offset:
                break
        return None

    def to_markdown(self, extra: Optional[Dict[str, Any]] = None) -> str:
        """The document as one Markdown file that reads back exactly.

        Front matter carries what plain Markdown cannot: where the pages
        break and the number printed on each, the headings as they were
        found, the figures and the metadata. ``extra`` adds keys for a person reading the file, a
        document id, where it came from, who extracted it; they are written
        first and ignored on the way back in. The ``vectrixdb: extracted``
        line is what stops this file ever being read as a new original.
        """
        front: Dict[str, Any] = {"vectrixdb": EXTRACTED_MARKER}
        front.update({k: v for k, v in (extra or {}).items() if k not in _RESERVED})
        front["metadata"] = _jsonable(self.metadata)
        front["pages"] = [[int(o), int(n)] for o, n in self.pages]
        if self.page_labels:
            # Only a PDF that numbers its own pages has them, and every other
            # file's front matter stays exactly as it was.
            front["page_labels"] = {
                str(int(n)): str(label) for n, label in sorted(self.page_labels.items())
            }
        front["headings"] = [[int(o), str(h), int(level)] for o, h, level in self.headings]
        front["figures"] = [[int(o), _jsonable(info)] for o, info in self.figures]
        if self.segments:
            # Only a recording has them, and every other file's front matter
            # stays exactly as it was.
            front["segments"] = [
                [round(float(a), 3), round(float(b), 3), str(t)] for a, b, t in self.segments
            ]
        return _front_matter_text(front) + self.text

    @classmethod
    def from_markdown(cls, text: str) -> "LoadedDocument":
        """Markdown, with or without front matter, as a document.

        A file this library wrote comes back as it was, offsets and all,
        and is not normalised a second time. Any other Markdown is read the
        ordinary way, and its front matter, ``doc_id``, ``audience``,
        ``client_id``, becomes the document's metadata and so every chunk's:
        that is where the fields a policy decides by can come from.
        """
        front, body = split_front_matter(text)
        if front.get("vectrixdb") == EXTRACTED_MARKER:
            length = len(body)
            return cls(
                text=body,
                metadata=dict(front.get("metadata") or {}),
                pages=[
                    (int(o), int(n)) for o, n in front.get("pages") or [] if 0 <= int(o) <= length
                ],
                headings=[
                    (int(o), str(h), int(level))
                    for o, h, level in front.get("headings") or []
                    if 0 <= int(o) <= length
                ],
                figures=[
                    (int(o), dict(info or {}))
                    for o, info in front.get("figures") or []
                    if 0 <= int(o) <= length
                ],
                segments=[(float(a), float(b), str(t)) for a, b, t in front.get("segments") or []],
                page_labels=_labels_from(front.get("page_labels")),
            )
        doc = markdown_document(body)
        doc.metadata.update({k: v for k, v in front.items() if k not in _RESERVED})
        return doc

    def page_at(self, offset: int) -> Optional[int]:
        return _position_at(self.pages, offset)

    def heading_at(self, offset: int) -> Optional[str]:
        found = _position_at([(o, (h, level)) for o, h, level in self.headings], offset)
        return found[0] if found else None

    def heading_path_at(self, offset: int) -> List[str]:
        """Every heading the offset sits under, outermost first."""
        stack: List[Tuple[int, str]] = []
        for start, heading, level in self.headings:
            if start > offset:
                break
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading))
        return [heading for _, heading in stack]

    def with_page_markers(self) -> str:
        """The text with ``--- Page N ---`` lines, for ``build_tree_from_pdf``."""
        if not self.pages:
            return self.text
        out = []
        for i, (start, page) in enumerate(self.pages):
            end = self.pages[i + 1][0] if i + 1 < len(self.pages) else len(self.text)
            out.append(f"--- Page {page} ---\n{self.text[start:end]}")
        return "\n".join(out)


def _position_at(positions: Sequence[Tuple[int, Any]], offset: int) -> Any:
    found = None
    for start, value in positions:
        if start <= offset:
            found = value
        else:
            break
    return found


def _labels_from(value: Any) -> Dict[int, str]:
    """Printed page numbers as front matter or a reply holds them, ``{"41": "39"}``, by page."""
    if not isinstance(value, Mapping):
        return {}
    return {
        int(n): str(label) for n, label in value.items() if str(n).isdigit() and str(label).strip()
    }


# ============================================================================
# FRONT MATTER
# ============================================================================
#
# INPUT   Markdown with a YAML block at the top
# OUTPUT  (front matter, body), values JSON can hold; the marker an extracted
#         document carries
#
# Front matter is how an extractor hands a document's pages and title to the
# loader.

EXTRACTED_MARKER = "extracted"
_RESERVED = ("vectrixdb", "metadata", "pages", "page_labels", "headings", "figures", "segments")
_FRONT = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_FRONT_KEY = re.compile(r"^([A-Za-z_][\w.-]*)[ \t]*:[ \t]*(.*)$")


def _jsonable(value: Any) -> Any:
    """``value`` with anything JSON cannot hold turned into its string."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _front_matter_text(front: Dict[str, Any]) -> str:
    # One key per line, each value written as JSON. JSON is also YAML's flow
    # style, so any front matter reader opens the file, and this one needs
    # no YAML package to read it back.
    lines = ["---"]
    for key, value in front.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _front_value(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        pass
    if raw.startswith("[") and raw.endswith("]"):
        return [_front_value(part) for part in raw[1:-1].split(",") if part.strip()]
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def split_front_matter(text: str) -> Tuple[Dict[str, Any], str]:
    """``(front matter, body)``. No front matter is an empty dict.

    Reads what people write at the top of a Markdown file: ``key: value``
    lines, values in JSON or bare, ``[a, b]`` lists and block lists of
    ``- item`` lines. Anything nested deeper is left out rather than
    guessed at. The body starts after the closing rule.
    """
    match = _FRONT.match(text)
    if not match:
        return {}, text
    front: Dict[str, Any] = {}
    current: Optional[str] = None
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^[ \t]+-[ \t]+(.*)$", line) or (
            re.match(r"^-[ \t]+(.*)$", line) if current else None
        )
        if item and current is not None:
            if not isinstance(front.get(current), list):
                front[current] = []
            front[current].append(_front_value(item.group(1)))
            continue
        key = _FRONT_KEY.match(line)
        if key and not line[:1].isspace():
            current = key.group(1)
            front[current] = _front_value(key.group(2))
        # else: a nested mapping or a continuation line. Left out: front
        # matter becomes every chunk's metadata, and a store with a flat
        # schema has nowhere to put a mapping.
    body = text[match.end() :]
    return front, body[1:] if body.startswith("\n") else body


# ============================================================================
# FIGURES AND TABLES IN MARKDOWN
# ============================================================================
#
# INPUT   Markdown with images and pipe tables; HTML tables
# OUTPUT  images as figure lines on their own paragraph; pipe and HTML tables
#         as one line per row, Header: value; the whole as a document with
#         pages, headings and figures
#
# A table row that reads Header: value embeds as a sentence; a row of pipes
# embeds as noise.

_HEADING = re.compile(r"^(#{1,6})[ \t]+(\S.*?)(?:[ \t]+#+)?[ \t]*$", re.MULTILINE)
#: An image's address may hold one pair of brackets: ``img/chart_(final).png``.
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(((?:[^()\s]|\([^()\s]*\))+)[^)]*\)")
FIGURE_LINE = re.compile(r"^\[Figure: (.+)\]$")
# An image, and the blockquote directly under it: only a blank line may sit
# between them. The quote is the figure's long description.
_FIGURE_BLOCK = re.compile(
    r"!\[([^\]]*)\]\(((?:[^()\s]|\([^()\s]*\))+)(?:[ \t]+\"[^\"]*\")?[ \t]*\)[ \t]*(?:\r?\n(?:[ \t]*\r?\n)?((?:[ \t]*>.*(?:\r?\n|\Z))+))?"
)
_QUOTE_MARK = re.compile(r"^[ \t]*>[ \t]?")


def figure_description(quote: Optional[str]) -> str:
    """A blockquote's lines as plain text: the quote marks and the bold
    around a ``**Figure 3.**`` label are dropped, the words are kept."""
    if not quote:
        return ""
    lines = [_QUOTE_MARK.sub("", line).rstrip() for line in quote.splitlines()]
    text = "\n".join(line for line in lines if line.strip())
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text).replace("[", "(").replace("]", ")")


def figure_line(caption: str, src: Optional[str] = None) -> str:
    """The one-line form a figure takes in the text: ``[Figure: caption]``.

    A figure is kept as text because a chunk of an image is its caption,
    which is what a reader searches for and what an answer can cite. The
    brackets are stripped from the caption so the line parses back, and a
    figure with no caption is named after its file.
    """
    text = " ".join(re.sub(r"[\[\]]", "", caption or "").split())
    if not text and src:
        text = Path(src.split("?")[0]).name
    return f"[Figure: {text or 'untitled'}]"


def _figure_text(caption: str, src: Optional[str], quote: Optional[str]) -> str:
    description = figure_description(quote)
    return figure_line(caption, src) + ("\n" + description if description else "")


def normalise_figures(text: str) -> str:
    """Markdown images as figure lines on their own paragraph, each with
    the description from the blockquote under it, when there is one."""
    return _FIGURE_BLOCK.sub(
        lambda m: "\n\n" + _figure_text(m.group(1), m.group(2), m.group(3)) + "\n\n", text
    )


_TABLE_RULE = re.compile(r"^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")


def _table_cells(line: str) -> List[Optional[str]]:
    cells = [c.strip().replace("\\|", "|") for c in _CELL_SPLIT.split(line.strip())]
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return [c or None for c in cells]


def _table_edits(text: str) -> List[Tuple[int, int, str]]:
    """``(start, end, replacement)`` for every pipe table outside a code fence."""
    lines = text.splitlines(keepends=True)
    starts: List[int] = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    edits: List[Tuple[int, int, str]] = []
    fenced = False
    i = 0
    while i < len(lines):
        bare = lines[i].rstrip("\r\n")
        if _FENCE.match(bare):
            fenced = not fenced
            i += 1
            continue
        nxt = lines[i + 1].rstrip("\r\n") if i + 1 < len(lines) else ""
        if fenced or "|" not in bare or "|" not in nxt or not _TABLE_RULE.match(nxt):
            i += 1
            continue
        j = i + 2
        while (
            j < len(lines) and "|" in lines[j] and lines[j].strip() and not _FENCE.match(lines[j])
        ):
            j += 1
        rows = [_table_cells(bare)] + [_table_cells(lines[k]) for k in range(i + 2, j)]
        # A header with fewer cells than its rows has left out the one over
        # the rows' labels, "| 2025 | 2024 |" over "| Personal banking | 14,500
        # | 13,828 |": its names belong over the figures, not moved left onto
        # the labels.
        widest = max((len(row) for row in rows[1:]), default=0)
        if len(rows[0]) < widest:
            pad: List[Optional[str]] = [None] * (widest - len(rows[0]))
            rows[0] = pad + list(rows[0])
        rendered = rows_to_lines(rows)
        if rendered:
            last = lines[j - 1]
            tail = last[len(last.rstrip("\r\n")) :]
            edits.append((starts[i], starts[j - 1] + len(last), "\n".join(rendered) + tail))
        i = j
    return edits


def _apply_edits(
    text: str, edits: Sequence[Tuple[int, int, str]], *positions: List[tuple]
) -> Tuple[str, List[List[tuple]]]:
    """Rewrite spans of ``text`` and move every recorded offset with them.

    An offset after an edit moves by what the edit added or took away; one
    inside an edit lands on the start of its replacement. That is what
    keeps a page break or a heading pointing at the same words after a
    table or an image has been rewritten in front of it.
    """
    ordered = sorted(edits)
    out: List[str] = []
    cursor = 0
    for start, end, replacement in ordered:
        if start < cursor:
            continue  # overlapping edits: the first one wins
        out.append(text[cursor:start])
        out.append(replacement)
        cursor = end

    out.append(text[cursor:])

    def moved(offset: int) -> int:
        delta = 0
        last_end = 0
        for start, end, replacement in ordered:
            if start < last_end:
                continue
            last_end = end
            if offset >= end:
                delta += len(replacement) - (end - start)
            elif offset > start:
                return start + delta
            else:
                break
        return offset + delta

    return "".join(out), [[(moved(p[0]),) + tuple(p[1:]) for p in plist] for plist in positions]


def normalise_tables(text: str) -> str:
    """Markdown pipe tables as one line per row, ``Header: value; Header: value``.

    The same rendering a spreadsheet row gets, for the same reason: a row
    of pipes is noise to a sentence embedding, and ``Region: EMEA; Revenue:
    1200`` reads like a sentence and keeps every cell an exact keyword.
    Tables inside a code fence are left alone.
    """
    return _apply_edits(text, _table_edits(text) + _html_table_edits(text))[0]


_HTML_TABLE = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.IGNORECASE | re.DOTALL)


class _TableRows(html.parser.HTMLParser):
    """The rows of one HTML table: each cell's text, the columns and rows it spans, and whether it is a heading.

    Tags inside a cell are dropped and their text kept; a table inside a
    cell is read as the words of its cells.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[Tuple[Optional[str], int, int, bool]]] = []
        self._cell: Optional[List[str]] = None
        self._span: Tuple[int, int, bool] = (1, 1, False)
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "table":
            self._depth += 1
            return
        if self._depth > 1:
            if self._cell is not None and tag in ("td", "th", "tr", "br"):
                self._cell.append(" ")
            return
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            if not self.rows:
                self.rows.append([])
            given = dict(attrs)
            self._cell = []
            self._span = (
                max(_int(given.get("colspan"), 1), 1),
                max(_int(given.get("rowspan"), 1), 1),
                tag == "th",
            )
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._depth -= 1
            return
        if self._depth > 1:
            return
        if tag in ("td", "th") and self._cell is not None:
            text = " ".join("".join(self._cell).split())
            self.rows[-1].append((text or None, *self._span))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _html_rows(
    parsed: Sequence[Sequence[Tuple[Optional[str], int, int, bool]]],
) -> List[List[Optional[str]]]:
    """An HTML table's cells laid on its grid, as rows for ``rows_to_lines``.

    A heading that spans columns names each of them, and a value that spans
    columns is written once; a cell that spans rows is carried down, so
    every row says it. The rows of headings at the top become one row, each
    column's headings joined: ``Revenue 2025`` and ``Revenue 2024`` under a
    ``Revenue`` that spans both, which is how a financial table is headed.
    """
    grid: List[List[Tuple[Optional[str], bool]]] = []
    carried: Dict[int, Tuple[Optional[str], bool, int]] = {}

    def carry_on(row: List[Tuple[Optional[str], bool]], column: int) -> int:
        """The cells carried down into ``row`` from above, from ``column`` on; the column after them."""
        while column in carried:
            text, header, left = carried[column]
            row.append((text, header))
            if left <= 1:
                del carried[column]
            else:
                carried[column] = (text, header, left - 1)
            column += 1
        return column

    for cells in parsed:
        row: List[Tuple[Optional[str], bool]] = []
        column = 0
        for text, colspan, rowspan, header in cells:
            column = carry_on(row, column)
            for k in range(max(int(colspan), 1)):
                value = text if (header or k == 0) else None
                row.append((value, header))
                if int(rowspan) > 1:
                    carried[column] = (value, header, int(rowspan) - 1)
                column += 1
        carry_on(row, column)
        grid.append(row)
    top = 0
    while (
        top < len(grid) and any(h for _t, h in grid[top]) and all(h or not t for t, h in grid[top])
    ):
        top += 1
    if top == 0:
        return [[text for text, _header in row] for row in grid]
    width = max(len(row) for row in grid)
    names: List[Optional[str]] = []
    for index in range(width):
        parts: List[str] = []
        for row in grid[:top]:
            text = row[index][0] if index < len(row) else None
            if text and (not parts or parts[-1] != text):
                parts.append(text)
        names.append(" ".join(parts) or None)
    return [names] + [[text for text, _header in row] for row in grid[top:]]


def _html_table_edits(text: str) -> List[Tuple[int, int, str]]:
    """``(start, end, replacement)`` for every HTML table outside a code fence.

    The OCR models that read a page in one pass write a table as HTML, not as
    pipes, so an extractor's reply can carry ``<table>`` in its Markdown.
    Left alone that is a run of tags in front of a sentence embedding. It gets
    the rendering every other table gets, one line per row with its headers.
    """
    if "<table" not in text.lower():
        return []
    fenced: List[Tuple[int, int]] = []
    offset, opened = 0, None
    for line in text.splitlines(keepends=True):
        if _FENCE.match(line.rstrip("\r\n")):
            if opened is None:
                opened = offset
            else:
                fenced.append((opened, offset + len(line)))
                opened = None
        offset += len(line)
    if opened is not None:
        fenced.append((opened, len(text)))
    edits: List[Tuple[int, int, str]] = []
    for found in _HTML_TABLE.finditer(text):
        if any(start <= found.start() < end for start, end in fenced):
            continue
        parser = _TableRows()
        try:
            parser.feed(found.group(0))
            parser.close()
        except Exception:  # markup too broken to read as a table is left as it was
            continue
        rows = [row for row in _html_rows(parser.rows) if any(row)]
        rendered = rows_to_lines(rows) if len(rows) >= 2 else []
        if rendered:
            edits.append((found.start(), found.end(), "\n".join(rendered)))
    return edits


def markdown_document(
    text: str,
    pages: Optional[List[Tuple[int, int]]] = None,
    headings: Optional[List[Tuple[int, str, int]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    figures: Optional[Sequence[Tuple[int, Dict[str, Any]]]] = None,
) -> "LoadedDocument":
    """Markdown as a document: images become figure lines, pipe tables
    become rows, and any page, heading or figure offsets given move with
    the text.

    This is what a ``.md`` file, a string handed to ``add_document`` and an
    extractor's reply all go through, so a table or a figure means the same
    thing wherever the Markdown came from.
    """
    pages = list(pages or [])
    given = list(headings or [])
    # Figure lines an extractor already wrote, with what it knew about each.
    kept = [(int(o), dict(info)) for o, info in (figures or [])]
    figure_edits: List[Tuple[int, int, str]] = []
    marks: List[tuple] = []
    for m in _FIGURE_BLOCK.finditer(text):
        figure_edits.append(
            (m.start(), m.end(), "\n\n" + _figure_text(m.group(1), m.group(2), m.group(3)) + "\n\n")
        )
        line = figure_line(m.group(1), m.group(2))
        marks.append(
            (
                m.start(),
                {
                    "caption": line[9:-1],
                    "src": m.group(2),
                    "described": bool(figure_description(m.group(3))),
                },
            )
        )
    text, (pages, given, marks, kept) = _apply_edits(text, figure_edits, pages, given, marks, kept)
    # A mark sits where its edit starts, which is the blank line put in
    # front of the figure; the figure's own line is two characters on.
    marks = [(o + 2, info) for o, info in marks]
    text, (pages, given, marks, kept) = _apply_edits(
        text, _table_edits(text) + _html_table_edits(text), pages, given, marks, kept
    )
    found = given if headings else _markdown_headings(text)
    return LoadedDocument(
        text=text,
        metadata=dict(metadata or {}),
        pages=[(int(o), int(n)) for o, n in pages],
        headings=[(int(o), str(h), int(level)) for o, h, level in found],
        figures=sorted(
            ((int(o), dict(info)) for o, info in marks + kept), key=lambda figure: figure[0]
        ),
    )


def _markdown_headings(text: str) -> List[Tuple[int, str, int]]:
    """The ATX headings of Markdown, outside its fenced code: ``# install dependencies`` in a bash block is a comment."""
    fenced = (
        [(m.start(), m.end()) for m in _FENCED_BLOCK.finditer(text)]
        if "```" in text or "~~~" in text
        else []
    )
    return [
        (m.start(), m.group(2).strip(), len(m.group(1)))
        for m in _HEADING.finditer(text)
        if not any(a <= m.start() < b for a, b in fenced)
    ]


# ============================================================================
# LOAD
# ============================================================================
#
# INPUT   a file, or bytes with a name, and what to do with pictures
# OUTPUT  the document read: PDF, DOCX, DOC, PPTX, XLSX, CSV, HTML, Markdown
#         or text, or whatever an extractor was registered for; local images
#         attached; decorative pictures told from figures; every figure
#         described by a describer
#
# One entry point, and the suffix picks the reader.


# ============================================================================
# WHAT A FILE IS
# ============================================================================
#
# INPUT   a file's first bytes, and its name
# OUTPUT  the reader it needs, decided by what is in it; or a refusal that
#         says what the file is and what to do with it
#
# A name is a guess somebody made. A macro-enabled Word file named .docm, a
# workbook saved as .xls, an RTF file called .doc and a .docx renamed .doc
# were all read as plain text, zip and OLE bytes indexed as if they were
# words. The bytes a file starts with say what it is.

#: What a file of each suffix is read as, when its bytes agree.
SUFFIX_KINDS: Dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".docm": "docx",
    ".dotx": "docx",
    ".dotm": "docx",
    ".doc": "doc",
    ".rtf": "rtf",
    ".odt": "odf",
    ".ods": "odf",
    ".odp": "odf",
    ".html": "html",
    ".htm": "html",
    ".xhtml": "html",
    ".md": "markdown",
    ".markdown": "markdown",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".xltx": "xlsx",
    ".xls": "xls",
    ".pptx": "pptx",
    ".pptm": "pptx",
    ".ppsx": "pptx",
    ".csv": "csv",
    ".tsv": "csv",
    ".txt": "text",
    **{s: "image" for s in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif")},
    **{s: "audio" for s in (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac")},
    **{s: "video" for s in (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v")},
}

#: The kinds a file's bytes can prove, and a wrong suffix can be corrected to.
_PROVABLE = frozenset(
    {"pdf", "docx", "doc", "xlsx", "xls", "pptx", "rtf", "odf", "image", "audio", "video"}
)


def _refuse(message: str) -> None:
    from .exceptions import ExtractionError

    raise ExtractionError(message)


def _zip_kind(path: Path, name: str) -> Optional[str]:
    """What an Office Open XML or OpenDocument package is, from the parts it holds."""
    import zipfile

    try:
        with zipfile.ZipFile(str(path)) as archive:
            names = set(archive.namelist())
            if "word/document.xml" in names or any(n.startswith("word/") for n in names):
                return "docx"
            if "xl/workbook.xml" in names or any(n.startswith("xl/") for n in names):
                return "xlsx"
            if "ppt/presentation.xml" in names or any(n.startswith("ppt/") for n in names):
                return "pptx"
            if "mimetype" in names:
                said = archive.read("mimetype")[:100].decode("ascii", "ignore")
                if "opendocument" in said:
                    return "odf"
    except (OSError, zipfile.BadZipFile):
        _refuse(f"{name} starts like a zip package but is damaged: it could not be opened")
    _refuse(f"{name} is a zip archive, not a document: unpack it and send the files inside")
    return None


def _ole_kind(path: Path, name: str) -> Optional[str]:
    """What an OLE compound file is: a Word 97-2003 document, a workbook, a deck, or a protected Office file."""
    try:
        import olefile
    except ImportError:
        return "doc" if Path(name).suffix.lower() == ".doc" else None
    try:
        ole = olefile.OleFileIO(str(path))
    except Exception:  # noqa: BLE001 - olefile's own errors say little
        _refuse(f"{name} is a damaged Office file: it could not be opened")
        return None
    try:
        if ole.exists("EncryptionInfo") or ole.exists("EncryptedPackage"):
            _refuse(
                f"{name} is protected by a password, so its text cannot be read: open it, remove the password and save it again"
            )
        if ole.exists("WordDocument"):
            return "doc"
        if ole.exists("Workbook") or ole.exists("Book"):
            return "xls"
        if ole.exists("PowerPoint Document"):
            _refuse(f"{name} is a PowerPoint 97-2003 deck, which is not read: save it as .pptx")
        if any(entry and entry[0].startswith("__substg1.0_") for entry in ole.listdir()):
            _refuse(
                f"{name} is an Outlook message, which is not read: save it as a PDF or copy its text"
            )
    finally:
        ole.close()
    _refuse(f"{name} is an OLE compound file that holds no document this reads")
    return None


def _sniff(path: Path, name: str) -> Optional[str]:
    """What a file's first bytes say it is; None when they say nothing, as text says nothing."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
    except OSError:
        return None
    if not head:
        return None
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return _zip_kind(path, name)
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _ole_kind(path, name)
    if stripped.startswith(b"{\\rtf"):
        return "rtf"
    if head.startswith(
        (b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*", b"BM")
    ) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image"
    if (
        (head[:4] == b"RIFF" and head[8:12] == b"WAVE")
        or head.startswith((b"ID3", b"fLaC", b"OggS"))
        or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
    ):
        return "audio"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        return "audio" if brand in (b"M4A ", b"M4B ") else "video"
    if head.startswith(b"\x1a\x45\xdf\xa3") or (head[:4] == b"RIFF" and head[8:12] == b"AVI "):
        return "video"
    lowered = stripped[:64].lower()
    if lowered.startswith((b"<!doctype html", b"<html")):
        return "html"
    if not head.startswith((b"\xff\xfe", b"\xfe\xff")) and b"\x00" in head:
        _refuse(
            f"{name} is not a format this reads: its bytes are not text and not a document it knows"
        )
    return None


def media_kind_of(head: bytes) -> Optional[str]:
    """What a file's first bytes say it is, without a file to open: "pdf", "office", "rtf", "image", "audio", "video", "html" or None.

    For a route handed bytes and a content type that says nothing: an Office
    package or an OLE file is "office", and which one it is, and whether it
    can be read, is for :func:`load` to find out from the whole file.
    """
    if not head:
        return None
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "office"
    if stripped.startswith(b"{\\rtf"):
        return "rtf"
    if head.startswith(
        (b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*", b"BM")
    ) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image"
    if (
        (head[:4] == b"RIFF" and head[8:12] == b"WAVE")
        or head.startswith((b"ID3", b"fLaC", b"OggS"))
        or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
    ):
        return "audio"
    if head[4:8] == b"ftyp":
        return "audio" if head[8:12] in (b"M4A ", b"M4B ") else "video"
    if head.startswith(b"\x1a\x45\xdf\xa3") or (head[:4] == b"RIFF" and head[8:12] == b"AVI "):
        return "video"
    if stripped[:64].lower().startswith((b"<!doctype html", b"<html")):
        return "html"
    return None


def _kind_of(path: Path) -> str:
    """The reader a file needs: what its bytes prove, else what its suffix says, else text."""
    by_name = SUFFIX_KINDS.get(path.suffix.lower())
    by_bytes = _sniff(path, path.name)
    if by_bytes in _PROVABLE:
        return by_bytes
    # A PDF's own reader says plainly when a file is not one; the Office
    # packages' readers said "Package not found", so they are told here.
    if by_name in ("docx", "xlsx", "pptx", "xls", "odf") and by_bytes is None:
        # Named as a document and holding text, or nothing: the name is wrong.
        if not path.stat().st_size:
            _refuse(f"{path.name} is empty")
        _refuse(
            f"{path.name} is not the {path.suffix.lower()} file its name says: its bytes do not start the way one does"
        )
    if by_bytes == "html" and by_name in (None, "text"):
        return "html"
    return by_name or "text"


#: Control characters no author typed: everything below a space but tab, line feed, carriage return and form feed.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")


def _decode_text(data: bytes) -> str:
    """Text in the encoding it is in: a byte order mark, else UTF-8, else Windows-1252.

    Windows-1252 is what a spreadsheet or a notepad saves in on a Western
    machine, and it decodes every byte, so nothing is replaced by a question
    mark and "Café" stays "Café". Control characters are taken out.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        text = data[3:].decode("utf-8", errors="replace")
    elif data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        text = data.decode("utf-32", errors="replace")
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", errors="replace")
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", _CONTROL.sub("", text))


#: A whole fenced code block; ``_FENCE`` above is one fence line.
_FENCED_BLOCK = re.compile(
    r"^(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^(?P=fence)[ \t]*$", re.MULTILINE | re.DOTALL
)
_HTML_COMMENT = re.compile(r"<!--.*?-->\n?", re.DOTALL)


_SETEXT = re.compile(
    r"(?:(?<=\n\n)|\A)(?P<title>[^\n#>|`~ \t\-*+][^\n]*)\n(?P<rule>={2,}|-{2,})[ \t]*(?=\n|\Z)"
)
_HTML_HEADING = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1\s*>", re.IGNORECASE | re.DOTALL)
_LINKED_IMAGE = re.compile(r"\[(!\[[^\]]*\]\((?:[^()\s]|\([^()\s]*\))+[^)]*\))\]\([^)]*\)")
_REF_IMAGE = re.compile(r"!\[([^\]]*)\]\[([^\]]*)\]")
_REF_DEFINITION = re.compile(
    r"^[ \t]{0,3}\[([^\]]+)\]:[ \t]*<?(\S+?)>?(?:[ \t]+[\"'(][^\n]*)?[ \t]*$", re.MULTILINE
)
_TOML_FRONT = re.compile(r"\A\+\+\+[ \t]*\n(.*?)\n\+\+\+[ \t]*(?:\n|\Z)", re.DOTALL)


def _markdown_normalised(text: str) -> str:
    """A Markdown file's other ways of saying the same thing, said the one way this library reads.

    Outside code: a setext heading, underlined with = or -, is an ATX one;
    an HTML <h2> is a "##" heading; an image inside a link is the image; a
    reference-style image is an inline one. TOML front matter, between +++
    lines, is written as the YAML front matter the reader knows.
    """
    toml = _TOML_FRONT.match(text)
    if toml:
        lines = []
        for line in toml.group(1).splitlines():
            key = re.match(r"^\s*([A-Za-z_][\w.-]*)\s*=\s*(.*?)\s*$", line)
            if key:
                lines.append(f"{key.group(1)}: {key.group(2)}")
        text = "---\n" + "\n".join(lines) + "\n---\n" + text[toml.end() :]
    refs = {m.group(1).strip().lower(): m.group(2) for m in _REF_DEFINITION.finditer(text)}

    def plain(part: str) -> str:
        part = _SETEXT.sub(
            lambda m: ("# " if m.group("rule")[0] == "=" else "## ") + m.group("title").strip(),
            part,
        )
        part = _HTML_HEADING.sub(
            lambda m: (
                "\n"
                + "#" * int(m.group(1))
                + " "
                + " ".join(re.sub(r"<[^>]+>", "", m.group(2)).split())
                + "\n"
            ),
            part,
        )
        part = _LINKED_IMAGE.sub(lambda m: m.group(1), part)
        if refs:
            part = _REF_IMAGE.sub(
                lambda m: (
                    f"![{m.group(1)}]({refs[(m.group(2) or m.group(1)).strip().lower()]})"
                    if (m.group(2) or m.group(1)).strip().lower() in refs
                    else m.group(0)
                ),
                part,
            )
        return part

    out: List[str] = []
    at = 0
    for fence in _FENCED_BLOCK.finditer(text):
        out.append(_inline_html(plain(text[at : fence.start()])))
        out.append(fence.group(0))
        at = fence.end()
    out.append(_inline_html(plain(text[at:])))
    return "".join(out)


_INLINE_TAGS = re.compile(
    r"</?(?:b|i|em|strong|u|span|sup|sub|small|mark|font|details|summary|abbr|cite|kbd|q|s|del|ins)\b[^>]*>",
    re.IGNORECASE,
)


def _inline_html(text: str) -> str:
    """Markdown's inline HTML as the words it wraps: <b>bold</b> is bold, <br> a line; reference definitions go."""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = _INLINE_TAGS.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", _REF_DEFINITION.sub("", text))


def _markdown_without_comments(text: str) -> str:
    """Markdown with its HTML comments taken out, outside code, where a comment is only text.

    A comment is the author talking to the next author, "internal: do not
    publish the margin", and a page never shows it.
    """
    out: List[str] = []
    at = 0
    for fence in _FENCED_BLOCK.finditer(text):
        out.append(_HTML_COMMENT.sub("", text[at : fence.start()]))
        out.append(fence.group(0))
        at = fence.end()
    out.append(_HTML_COMMENT.sub("", text[at:]))
    return "".join(out)


def _load_text(path: Path) -> LoadedDocument:
    """A text file, a form feed being where a page ends, as a printer's text has it."""
    text = _decode_text(path.read_bytes())
    if "\f" in text:
        joined, pages = join_pages(text.split("\f"))
        return LoadedDocument(text=joined, pages=pages, metadata={"pages": len(pages)})
    return LoadedDocument(text=text.strip())


# ============================================================================
# RTF
# ============================================================================
#
# INPUT   an .rtf file
# OUTPUT  its text: paragraphs, tables as rows, hidden and deleted text left out
#
# RTF is text with control words in braces. The font table, the colour table,
# the style sheet, pictures and anything marked \* are not text; \v hides a
# run, \deleted marks one struck out; \'e9 is a byte in the document's code
# page and \u233 a character with a fallback after it.

_RTF_TOKEN = re.compile(
    r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|[\r\n]+|([^\\{}\r\n]+)"
)
_RTF_SKIP = frozenset(
    {
        "fonttbl",
        "colortbl",
        "stylesheet",
        "info",
        "pict",
        "object",
        "header",
        "footer",
        "headerl",
        "headerr",
        "headerf",
        "footerl",
        "footerr",
        "footerf",
        "listtable",
        "listoverridetable",
        "rsidtbl",
        "generator",
        "themedata",
        "colorschememapping",
        "datastore",
        "latentstyles",
        "xmlnstbl",
        "fldinst",
        "filetbl",
        "revtbl",
        "pgdsctbl",
        "mmathPr",
        "wgrffmtfilter",
        "comment",
        "annotation",
        "atnid",
        "atnauthor",
        "bkmkstart",
        "bkmkend",
        "shppict",
        "nonshppict",
        "blipuid",
        "operator",
        "author",
        "title",
        "subject",
        "keywords",
        "doccomm",
        "company",
    }
)
_RTF_CHARS = {
    "par": "\n",
    "line": "\n",
    "sect": "\n\n",
    "page": "\n\n",
    "tab": "\t",
    "cell": "\x1f",
    "row": "\n",
    "emdash": "\u2014",
    "endash": "\u2013",
    "bullet": "\u2022",
    "lquote": "\u2018",
    "rquote": "\u2019",
    "ldblquote": "\u201c",
    "rdblquote": "\u201d",
    "emspace": " ",
    "enspace": " ",
}


def _rtf_text(raw: str) -> Tuple[str, Optional[str]]:
    """The text of an RTF document, and its title from its info group."""
    stack: List[Tuple[bool, int, bool]] = []
    skip, uc, hidden = False, 1, False
    star = False
    pending = 0
    codepage = "cp1252"
    out: List[str] = []
    title: List[str] = []
    in_title = False
    title_depth = -1
    for match in _RTF_TOKEN.finditer(raw):
        word, param, hexed, symbol, brace, text = match.groups()
        if brace == "{":
            stack.append((skip, uc, hidden))
            star = False
            continue
        if brace == "}":
            if in_title and len(stack) <= title_depth:
                in_title = False
            skip, uc, hidden = stack.pop() if stack else (False, 1, False)
            continue
        if word is not None:
            if word == "ansicpg" and param:
                codepage = f"cp{param}"
            elif word == "uc" and param is not None:
                uc = max(0, int(param))
            elif word == "u" and param is not None:
                if not skip and not hidden:
                    out.append(chr(int(param) % 65536))
                pending = uc
                continue
            elif word == "v":
                hidden = param != "0"
            elif word == "deleted":
                hidden = True
            elif word == "title":
                in_title, title_depth = True, len(stack)
                skip = True
            elif star or word in _RTF_SKIP:
                skip = True
            elif not skip and not hidden and word in _RTF_CHARS:
                out.append(_RTF_CHARS[word])
            star = False
            pending = 0
            continue
        if hexed is not None:
            if pending:
                pending -= 1
                continue
            char = bytes([int(hexed, 16)]).decode(codepage, errors="replace")
            if in_title:
                title.append(char)
            elif not skip and not hidden:
                out.append(char)
            continue
        if symbol is not None:
            if symbol == "*":
                star = True
            elif not skip and not hidden:
                out.append(
                    {"~": " ", "-": "", "_": "-", "\\": "\\", "{": "{", "}": "}"}.get(symbol, "")
                )
            continue
        if text is not None:
            if pending:
                cut = min(pending, len(text))
                text = text[cut:]
                pending -= cut
            if in_title:
                title.append(text)
            elif not skip and not hidden:
                out.append(text)
    lines: List[str] = []
    rows: List[List[str]] = []
    for line in "".join(out).split("\n"):
        if "\x1f" in line:
            cells = [cell.strip() for cell in line.split("\x1f")]
            if cells and not cells[-1]:
                cells.pop()  # a cell mark ends every cell, the last one too
            rows.append(cells)
            continue
        if rows:
            lines += rows_to_lines(rows)
            rows = []
        lines.append(line.rstrip())
    if rows:
        lines += rows_to_lines(rows)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text, (" ".join("".join(title).split()) or None)


def _load_rtf(path: Path) -> LoadedDocument:
    raw = path.read_bytes().decode("latin-1")
    text, title = _rtf_text(raw)
    doc = (
        LoadedDocument.from_markdown(text)
        if re.search(r"^#{1,6} ", text, re.M)
        else LoadedDocument(text=text)
    )
    if title:
        doc.metadata["title"] = title
    return doc


# ============================================================================
# OPENDOCUMENT
# ============================================================================
#
# INPUT   an .odt, .ods or .odp file
# OUTPUT  its headings, paragraphs, lists and tables, the way the Office Open
#         XML readers write theirs


def _load_odf(path: Path) -> LoadedDocument:
    """An OpenDocument text, spreadsheet or presentation, read from its content.xml."""
    import xml.etree.ElementTree as ET
    import zipfile

    ns = {
        "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
        "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
        "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
        "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
        "dc": "http://purl.org/dc/elements/1.1/",
    }
    try:
        with zipfile.ZipFile(str(path)) as archive:
            root = ET.fromstring(archive.read("content.xml"))
            meta = (
                ET.fromstring(archive.read("meta.xml"))
                if "meta.xml" in archive.namelist()
                else None
            )
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        _refuse(f"{path.name} is a damaged OpenDocument file: {exc}")
        raise  # unreachable
    T, TB = "{%s}" % ns["text"], "{%s}" % ns["table"]

    def words(element: Any) -> str:
        parts: List[str] = []

        def walk(node: Any) -> None:
            if node.tag in (T + "note", T + "tracked-changes") or node.get(T + "display") == "none":
                return
            if node.text:
                parts.append(node.text)
            for child in node:
                if child.tag == T + "s":
                    parts.append(" " * int(child.get(T + "c", "1")))
                elif child.tag == T + "tab":
                    parts.append("\t")
                elif child.tag == T + "line-break":
                    parts.append("\n")
                else:
                    walk(child)
                if child.tail:
                    parts.append(child.tail)

        walk(element)
        return " ".join("".join(parts).split())

    blocks: List[str] = []

    def block(node: Any, depth: int = 0) -> None:
        if node.tag == T + "h":
            level = int(node.get(T + "outline-level", "1") or 1)
            said = words(node)
            if said:
                blocks.append("#" * min(max(level, 1), 6) + " " + said)
        elif node.tag == T + "p":
            said = words(node)
            if said:
                blocks.append(said)
        elif node.tag == T + "list":
            for item in node.findall(T + "list-item"):
                for child in item:
                    if child.tag == T + "list":
                        block(child, depth + 1)
                    else:
                        said = words(child)
                        if said:
                            blocks.append("  " * depth + "- " + said)
        elif node.tag == TB + "table":
            name = node.get(TB + "name")
            rows: List[List[str]] = []
            for row in node.iter(TB + "table-row"):
                cells: List[str] = []
                for cell in row:
                    if cell.tag not in (TB + "table-cell", TB + "covered-table-cell"):
                        continue
                    repeat = min(int(cell.get(TB + "number-columns-repeated", "1") or 1), 64)
                    cells += [words(cell)] * repeat
                while cells and not cells[-1]:
                    cells.pop()
                repeat_rows = min(int(row.get(TB + "number-rows-repeated", "1") or 1), 64)
                if cells:
                    rows += [cells] * repeat_rows
            lines = rows_to_lines(rows)
            if lines:
                blocks.append(
                    ("## " + name + "\n\n" if name and path.suffix.lower() == ".ods" else "")
                    + "\n".join(lines)
                )
        else:
            for child in node:
                block(child, depth)

    body = root.find("office:body", ns)
    if body is not None:
        block(body)
    text = "\n\n".join(blocks)
    doc = LoadedDocument.from_markdown(text) if "#" in text else LoadedDocument(text=text)
    if meta is not None:
        found = meta.find(".//dc:title", ns)
        if found is not None and (found.text or "").strip():
            doc.metadata["title"] = (found.text or "").strip()
    return doc


# ============================================================================
# EXCEL 97-2003
# ============================================================================
#
# INPUT   an .xls file
# OUTPUT  every sheet under its heading, every row a line, as .xlsx is read


def _load_xls(path: Path) -> LoadedDocument:
    try:
        import xlrd
    except ImportError as exc:
        raise DependencyError("xlrd", "documents") from exc
    try:
        book = xlrd.open_workbook(str(path))
    except Exception as exc:  # noqa: BLE001 - xlrd's errors say little a caller can act on
        _refuse(f"{path.name} could not be read as an Excel 97-2003 workbook: {exc}")
        raise
    parts: List[str] = []
    for sheet in book.sheets():
        if getattr(sheet, "visibility", 0):
            continue  # a hidden sheet is scratch, not what the workbook says
        rows: List[List[Any]] = []
        for r in range(sheet.nrows):
            row: List[Any] = []
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        row.append(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode))
                    except Exception:  # noqa: BLE001
                        row.append(cell.value)
                elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    row.append(None)
                else:
                    row.append(cell.value)
            rows.append(row)
        lines = rows_to_lines(rows)
        if lines:
            parts.append(f"# {sheet.name}\n\n" + "\n".join(lines))
    text = "\n\n".join(parts)
    return LoadedDocument.from_markdown(text) if text else LoadedDocument(text="")


def load(
    source: Union[str, Path],
    kind: Optional[str] = None,
    extractors: Any = None,
    images: bool = False,
    ocr: Optional[Callable[[bytes], Sequence[str]]] = None,
    page_reader: Optional[Callable[..., Any]] = None,
    every_page: bool = False,
) -> LoadedDocument:
    """Read a file into text with page and heading positions.

    ``kind`` overrides the extension: one of pdf, docx, doc, pptx, xlsx, csv,
    html, markdown, text, image, audio, video. The last three are read by the local
    engines in :mod:`vectrixdb.extract.engines` and need an extra each.
    PDF needs ``pypdf`` (or PyMuPDF as ``fitz``), DOCX needs ``python-docx``;
    the others use the standard library. A missing parser is a
    :class:`DependencyError` naming the pip install, not an ImportError from
    three frames down.

    ``extractors`` is a mapping of suffix to callable, an
    :class:`~vectrixdb.extract.ExtractorRegistry` or an
    :class:`~vectrixdb.extract.HttpExtractor`. A suffix somebody registered
    an extractor for goes to that extractor, including a suffix the
    built-in readers know; passing ``kind`` asks for a built-in reader by
    name and skips them.

    ``images=True`` asks for the pictures in a PDF, a Word document, a
    PowerPoint deck and an Excel workbook as well. Each becomes a figure
    where it sits, with its bytes in ``doc.images``: at the end of its page
    in a PDF, ``p4-fig1``; after its paragraph in Word, ``fig3``; at the end
    of its slide in PowerPoint, ``s2-fig1``; after its sheet's rows in Excel,
    ``sheet2-fig1``. No caption yet, which is why this is for a caller that
    has something to describe them with.

    ``ocr`` reads the pages of a PDF that have no text layer: a callable of
    one page drawn as a PNG, returning its lines in the order a person reads
    them, which is what
    :func:`~vectrixdb.extract.layout.reading_order` is for. Only pages under
    ``min_chars`` are drawn and only those are sent, so a report that is
    text throughout costs nothing and never calls it; a page with no ink is
    not sent either, because a reader shown an empty page invents text for
    it. This is the one way to have both halves of a PDF: registering an
    extractor for ``.pdf`` replaces this reader and takes the figures with
    it, and ``ocr`` leaves the figures where they were.

    ``page_reader`` reads a PDF's pages by sight, the way a person does: a
    :class:`~vectrixdb.extract.page_reader.PageReader`, given a picture of
    each page the rules are unsure of, a chart, big figures set apart from
    their labels, a page whose file draws its parts out of order, with the
    page's own words, and answering the page in Markdown. Every number it
    writes is held to the page: one the page does not print is taken out,
    and a reading with too many is not taken. ``every_page`` gives it every
    page with words on it rather than the unsure ones alone.
    """
    path = Path(source)
    if kind is None:
        from .extract import resolve

        registry = resolve(extractors)
        if registry.get(path.name) is not None:
            data = path.read_bytes()
            extracted = registry.extract(data, path.name, source=str(path))
            assert extracted is not None
            return _as_read(extracted, data, path.name, str(path))
    if kind is None:
        kind = _kind_of(path)
    elif kind in ("pdf", "docx", "doc", "xlsx", "pptx"):
        # Asked for by name, and still read as what it is: a .docx sent to
        # the .doc route is a .docx.
        proven = _sniff(path, path.name)
        if proven in _PROVABLE and proven != kind:
            kind = proven
    meta = {"source": str(path), "filename": path.name, "kind": kind}

    if kind == "pdf":
        doc = _load_pdf(
            path, images=images, ocr=ocr, page_reader=page_reader, every_page=every_page
        )
    elif kind == "docx":
        doc = _load_docx(path, images=images)
    elif kind == "doc":
        doc = _load_doc(path)
    elif kind == "rtf":
        doc = _load_rtf(path)
    elif kind == "odf":
        doc = _load_odf(path)
    elif kind == "xls":
        doc = _load_xls(path)
    elif kind == "xlsx":
        doc = _load_xlsx(path, images=images)
    elif kind == "pptx":
        doc = _load_pptx(path, images=images)
    elif kind == "csv":
        doc = _load_csv(path)
    elif kind in ("image", "audio", "video"):
        # Not text yet. The local engine for the job reads it, and says
        # which extra it needs when that is not installed, rather than this
        # function decoding a PNG as UTF-8 and indexing the result.
        doc = _local_engine(kind)(path.read_bytes(), path.name)
    elif kind == "html":
        doc = _load_html(_decode_html(path.read_bytes()))
    elif kind == "markdown":
        raw = _markdown_normalised(_markdown_without_comments(_decode_text(path.read_bytes())))
        doc = LoadedDocument.from_markdown(raw)
        front, _body = split_front_matter(raw)
        if front.get("vectrixdb") == EXTRACTED_MARKER and isinstance(front.get("doc_id"), str):
            # A kept document that found its way among the originals is
            # still the document it was kept from, under the id it had.
            doc.metadata.setdefault("doc_id", front["doc_id"])
        if path.parent.is_dir():
            _attach_local_images(doc, path.parent)
        if front.get("vectrixdb") != EXTRACTED_MARKER and not doc.metadata.get("title"):
            # Its front matter's title, else its first top-level heading.
            first = next((h for _o, h, level in doc.headings if level == 1), None)
            if first:
                doc.metadata["title"] = first
    elif kind == "text":
        doc = _load_text(path)
    else:
        raise ValueError(f"unknown document kind {kind!r}")
    doc.metadata = {**meta, **doc.metadata}
    return doc


def _attach_local_images(doc: "LoadedDocument", folder: Path) -> None:
    """Read the image files a Markdown document's figures name, when they
    are files beside it. A remote address, or a path that leaves the
    document's folder, is not followed."""
    root = folder.resolve()
    for _offset, info in doc.figures:
        src = str(info.get("src") or "")
        if not src or "://" in src or src.startswith(("data:", "/", "\\")) or src in doc.images:
            continue
        try:
            target = (root / src).resolve()
            target.relative_to(root)
            if target.is_file() and target.stat().st_size <= 20 * 1024 * 1024:
                doc.images[src] = target.read_bytes()
        except (OSError, ValueError):
            continue


def image_size(data: bytes) -> Optional[Tuple[int, int]]:
    """``(width, height)`` of a PNG, JPEG, GIF, BMP or WebP, read from its
    header with nothing but ``struct``; None for anything else."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:2] == b"BM":
            width, height = struct.unpack("<ii", data[18:26])
            return width, abs(height)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            if data[12:16] == b"VP8X":
                return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(
                    data[27:30], "little"
                )
            if data[12:16] == b"VP8 ":
                width, height = struct.unpack("<HH", data[26:30])
                return width & 0x3FFF, height & 0x3FFF
            if data[12:16] == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (
                    0xC0,
                    0xC1,
                    0xC2,
                    0xC3,
                    0xC5,
                    0xC6,
                    0xC7,
                    0xC9,
                    0xCA,
                    0xCB,
                    0xCD,
                    0xCE,
                    0xCF,
                ):
                    height, width = struct.unpack(">HH", data[i + 5 : i + 9])
                    return width, height
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                i += 2 + int.from_bytes(data[i + 2 : i + 4], "big")
    except (struct.error, IndexError):
        return None
    return None


def is_decorative(
    data: bytes, min_bytes: int = 1500, min_side: int = 48, max_aspect: float = 8.0
) -> bool:
    """A logo, an icon, a rule, a signature: too small to be a figure, or
    far too long and thin. Costs nothing to tell, which matters because the
    alternative is paying a vision model to describe a letterhead."""
    if len(data) < min_bytes:
        return True
    size = image_size(data)
    if size is None:
        return False
    width, height = size
    if width <= 0 or height <= 0 or min(width, height) < min_side:
        return True
    return max(width, height) / min(width, height) > max_aspect


def _blank_image(data: bytes) -> bool:
    from .extract.layout import looks_blank

    return looks_blank(data)


def _printed_words(block: str) -> str:
    """What a figure left out as decoration keeps: the words a chart printed, which were the page's own."""
    return "\n".join(
        line for line in block.split("\n")[1:] if line.startswith("Words in the picture:")
    )


def _printed_rows(rows: Any, block: str) -> Any:
    """A chart's rows held to the words the chart prints, when its figure line has them under it.

    A chart a PDF draws keeps the words it prints on the line under its
    figure line, ``Words in the picture: ...``. A value in the describer's
    rows that is not among them, or is only a tick on the chart's scale, was
    read off a bar, and goes; a row left with its label alone goes with it.
    A picture with no such line, a photograph or a picture pasted in, keeps
    its rows as the describer gave them.
    """
    words = next(
        (
            line[len("Words in the picture: ") :]
            for line in block.split("\n")[1:]
            if line.startswith("Words in the picture: ")
        ),
        None,
    )
    if words is None or not isinstance(rows, list) or len(rows) < 2:
        return rows
    from .extract.page_reader import numbers_in, scale_in

    printed, ticks = set(numbers_in(words)), scale_in(words)

    def shown(cell: Any) -> bool:
        found = numbers_in(str(cell or ""))
        return not found or (
            all(n in printed for n in found) and not (ticks and all(n in ticks for n in found))
        )

    kept = [list(rows[0])]
    for row in rows[1:]:
        cells = [row[0], *(cell if shown(cell) else "" for cell in row[1:])] if row else []
        if any(str(cell or "").strip() for cell in cells[1:]):
            kept.append(cells)
    return kept if len(kept) >= 2 else None


def describe_figures(
    doc: "LoadedDocument",
    describer: Optional[Callable[..., Any]],
    *,
    name: str = "",
    skip_decorative: bool = True,
    repeated_on: int = 3,
) -> "LoadedDocument":
    """Put a description under every figure whose image is at hand.

    ``describer`` takes the image bytes and a context, ``caption``, ``name``,
    ``page``, ``before`` and ``after`` (the text either side), and returns
    the description as a string, or a mapping with any of ``caption``,
    ``description``, ``table`` (rows, for a picture of a table, rendered the
    way a spreadsheet row is) and ``decorative``. None leaves the figure as
    it is. A figure that already has a description is not described again,
    which is what makes this free on a kept document.

    Decorative images are taken out of the text altogether: small ones,
    long thin ones, and an image that turns up ``repeated_on`` times or more,
    which is a letterhead and not a figure.
    """
    if not doc.figures:
        return doc
    counts: Dict[str, int] = {}
    digests: Dict[str, str] = {}
    for _offset, info in doc.figures:
        src = str(info.get("src") or "")
        if src in doc.images and src not in digests:
            digests[src] = hashlib.sha256(doc.images[src]).hexdigest()
    for _offset, info in doc.figures:
        digest = digests.get(str(info.get("src") or ""))
        if digest:
            counts[digest] = counts.get(digest, 0) + 1

    blocks = {m.start(): m for m in _FIGURE_IN_TEXT.finditer(doc.text)}
    edits: List[Tuple[int, int, str]] = []
    marks: List[tuple] = []
    dropped: List[str] = []
    # Who described how many, and how many nobody could.
    said: Dict[str, int] = {}
    for offset, info in doc.figures:
        info = dict(info)
        src = str(info.get("src") or "")
        block = blocks.get(offset)
        data = doc.images.get(src)
        if block is None or data is None:
            marks.append((offset, info))
            continue
        # An image with nothing in it is asked about least of all: a vision model
        # shown an empty picture describes one it made up.
        if skip_decorative and (
            is_decorative(data) or counts.get(digests[src], 0) >= repeated_on or _blank_image(data)
        ):
            edits.append((block.start(), block.end(), _printed_words(block.group(0))))
            dropped.append(src)
            continue
        if describer is None or info.get("described"):
            marks.append((offset, info))
            continue
        own = str(info.get("caption") or "")
        page = doc.page_at(offset)
        context = {
            # A caption that is only the picture's file name is no caption.
            # Asked with none, a describer says what the picture shows, and
            # the figure is found and cited by that, not by p4-fig1.jpg.
            "caption": "" if own == src else own,
            "name": name,
            "src": src,
            "page": page,
            "page_label": doc.page_label(page),
            "heading": doc.heading_at(offset),
            "before": doc.text[max(0, block.start() - 400) : block.start()].strip(),
            "after": doc.text[block.end() : block.end() + 400].strip(),
        }
        try:
            answer = describer(data, context)
        except Exception as exc:
            from .exceptions import ExtractionError

            if isinstance(exc, ExtractionError):
                raise
            raise ExtractionError(
                f"describing the figure {src} in {name or 'a document'} failed: {exc}"
            ) from exc
        if answer is None:
            said["undescribed"] = said.get("undescribed", 0) + 1
            marks.append((offset, info))
            continue
        if isinstance(answer, str):
            answer = {"description": answer}
        if answer.get("decorative"):
            edits.append((block.start(), block.end(), _printed_words(block.group(0))))
            dropped.append(src)
            continue
        caption = " ".join(str(answer.get("caption") or info.get("caption") or "").split())
        lines = [str(answer.get("description") or "").strip()]
        table = _printed_rows(answer.get("table"), block.group(0)) if answer.get("table") else None
        if table:
            lines.extend(rows_to_lines(table))
        description = figure_description("\n".join(line for line in lines if line))
        description = "\n".join(part.strip() for part in description.splitlines() if part.strip())
        info.update(caption=figure_line(caption, src)[9:-1], described=bool(description))
        if description:
            # Who wrote it, so a plainer description can be found and done
            # again once a better describer is to hand.
            by = str(answer.get("by") or getattr(describer, "label", "") or "describer")
            info["described_by"] = by
            said[by] = said.get(by, 0) + 1
        edits.append(
            (
                block.start(),
                block.end(),
                figure_line(caption, src) + ("\n" + description if description else ""),
            )
        )
        marks.append((offset, info))
    metadata = dict(doc.metadata)
    if said:
        # About the document, and so it travels in an extraction service's
        # reply and is kept in the front matter, where the figures may not be.
        counts = dict(metadata.get("figures_described_by") or {})
        for by, n in said.items():
            counts[by] = int(counts.get(by, 0)) + n
        metadata["figures_described_by"] = counts
    if not edits:
        return replace(doc, metadata=metadata) if said else doc
    text, (pages, headings, marks) = _apply_edits(doc.text, edits, doc.pages, doc.headings, marks)
    images = {k: v for k, v in doc.images.items() if k not in dropped}
    return LoadedDocument(
        text=text,
        metadata=metadata,
        pages=[(int(o), int(n)) for o, n in pages],
        headings=[(int(o), str(h), int(level)) for o, h, level in headings],
        figures=[(int(o), dict(i)) for o, i in marks],
        images=images,
        # Times, not offsets, so a figure's description moving the text does
        # not move when anything was said.
        segments=list(doc.segments),
        # By page, not offset, and so the same holds for what each page is numbered.
        page_labels=dict(doc.page_labels),
    )


_LOCAL_ENGINES: Dict[str, Any] = {}


def _local_engine(kind: str) -> Any:
    """One engine per kind for the life of the process: a speech model is
    loaded once, not once per file."""
    if kind not in _LOCAL_ENGINES:
        from .extract import engines

        _LOCAL_ENGINES[kind] = {
            "image": engines.RapidOcr,
            "audio": engines.Whisper,
            "video": engines.Video,
        }[kind]()
    return _LOCAL_ENGINES[kind]


def _as_read(doc: LoadedDocument, data: bytes, name: str, source: str) -> LoadedDocument:
    """What an extractor read, with what only the caller knows put back.

    Where the file came from and what it is called are the caller's. An
    extractor behind a network call knows a file only by the name it was
    sent, which for a long file read in pieces is a piece's name,
    ``report.pages-1.pdf``, and a document kept under that name could not be
    read again from its source. A PDF's printed page numbers, its bookmarks
    and its title are read from the original for the same reason: a piece
    cut from it has none of the three of its own.
    """
    given = {k: v for k, v in doc.metadata.items() if k not in ("source", "filename")}
    doc.metadata = {
        "source": source,
        "filename": name,
        "kind": Path(name).suffix.lower().lstrip(".") or "text",
        **given,
    }
    if Path(name).suffix.lower() == ".pdf":
        _pdf_extras(data, doc)
    return doc


def load_bytes(
    data: bytes,
    name: str,
    kind: Optional[str] = None,
    extractors: Any = None,
    source: Optional[str] = None,
    images: bool = False,
    ocr: Optional[Callable[[bytes], Sequence[str]]] = None,
    page_reader: Optional[Callable[..., Any]] = None,
    every_page: bool = False,
) -> LoadedDocument:
    """:func:`load` for bytes that are not a file here: an object fetched
    from a bucket, an upload. ``name`` picks the reader by its suffix and is
    what the document is called; ``source`` is where it came from, a URI.

    An extractor is handed the bytes as they are. A built-in reader wants a
    file, so the bytes go to a temporary one that is removed afterwards.

    ``images`` keeps the pictures inside the document, which is what a
    describer and an image embedder need. It costs nothing to leave off and
    a decode of every picture to turn on, so it is off unless asked for.
    Without it a PDF, a Word document, a deck or a workbook has no figures
    at all, not merely figures without descriptions: a document fetched from
    a bucket used to lose them in silence, whatever the collection was
    opened with.

    ``ocr`` reads a PDF's pages that have no text layer, and ``page_reader``
    the pages the rules are unsure of, by sight; see :func:`load`.
    """
    import os
    import tempfile

    if kind is None:
        from .extract import resolve

        extracted = resolve(extractors).extract(data, name, source=source)
        if extracted is not None:
            return _as_read(extracted, data, name, source or name)
    fd, tmp = tempfile.mkstemp(suffix=Path(name).suffix or ".txt")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        doc = load(
            tmp,
            kind=kind,
            extractors={},
            images=images,
            ocr=ocr,
            page_reader=page_reader,
            every_page=every_page,
        )
    finally:
        try:
            os.unlink(tmp)
        except OSError:  # pragma: no cover - a temp file already gone
            pass
    doc.metadata["source"] = source or name
    doc.metadata["filename"] = name
    return doc


# ============================================================================
# PAGES, AND THE PICTURES OFFICE FILES HOLD
# ============================================================================
#
# INPUT   page texts; a PDF; an Office archive
# OUTPUT  one text with the offset each page starts at; pages drawn as PNGs;
#         the figure lines a reader kept; each part's pictures from the
#         relationships it names
#
# A sentence cut by a page break is joined, so a chunk never starts mid-
# sentence because a page did.

_SENTENCE_CLOSE = re.compile(r"[.!?:;\"')\]]\s*$")
_CONTINUES = re.compile(r"^[a-z0-9(\"']")
_PAGE_NOISE = re.compile(r"^\s*(?:page\s+)?\d{1,4}\s*(?:of\s+\d{1,4})?\s*$", re.IGNORECASE)


def join_pages(pages_text: Sequence[str]) -> Tuple[str, List[Tuple[int, int]]]:
    """One text from a list of page texts, with the offset each page starts at.

    A page break is a paragraph break unless the sentence plainly runs on:
    the previous page ends without closing punctuation and the next page
    starts with a lowercase letter, a digit or an opening quote. Then the
    two are joined with a space, so a sentence a printer split across pages
    reaches the chunker whole and lands in one chunk instead of two halves
    that each mean less. A page consisting of nothing but a page number is
    dropped from the text and still counted, so page numbers stay right.
    """
    parts: List[str] = []
    pages: List[Tuple[int, int]] = []
    offset = 0
    last = ""
    for number, page_text in enumerate(pages_text, start=1):
        cleaned = page_text.strip()
        if _PAGE_NOISE.match(cleaned):
            cleaned = ""
        if cleaned and last:
            runs_on = not _SENTENCE_CLOSE.search(last) and bool(_CONTINUES.match(cleaned))
            sep = " " if runs_on else "\n\n"
            parts.append(sep)
            offset += len(sep)
        pages.append((offset, number))
        if cleaned:
            parts.append(cleaned)
            offset += len(cleaned)
            last = cleaned
    return "".join(parts), pages


def _pdf_page_images(path: Path, wanted: Sequence[int]) -> Iterable[Tuple[int, bytes]]:
    """Just the pages asked for, drawn as PNGs.

    Only the ones asked for, because drawing a page is the slow half and
    sending it to a paid reader is the expensive half. A report of three
    hundred pages with two scanned inserts costs two pages, not three
    hundred.
    """
    try:
        import io

        import pypdfium2
    except ImportError as exc:
        raise DependencyError("pypdfium2", "ocr") from exc
    from .extract.pdf_text import PDFIUM_LOCK

    # Every PDFium call under the one lock, and none held while the caller
    # reads the page it was handed: that can take a paid service a second.
    with PDFIUM_LOCK:
        document = pypdfium2.PdfDocument(str(path))
    try:
        for index in wanted:
            buffer = io.BytesIO()
            with PDFIUM_LOCK:
                page = document[index]
                try:
                    bitmap = page.render(scale=2.0)
                    picture = bitmap.to_pil()
                finally:
                    page.close()
            picture.save(buffer, format="PNG")
            yield index, buffer.getvalue()
    finally:
        with PDFIUM_LOCK:
            document.close()


def _marked_figures(text: str, found: Mapping[str, bytes]) -> List[Tuple[int, Dict[str, Any]]]:
    """The ``[Figure: name]`` lines a reader wrote, as figures, for the pictures it kept.

    The name is on the figure line itself; a line under it, the words a
    chart prints, is part of the figure and not of its name.
    """
    marked = []
    for m in _FIGURE_IN_TEXT.finditer(text):
        name = m.group(0).split("\n", 1)[0][9:-1]
        if name in found:
            marked.append((m.start(), {"caption": name, "src": name, "described": False}))
    return marked


#: The parts of an Office file that say where its pictures are.
_OOXML_NS = {
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}


def _ooxml_rels(archive: Any, part: str) -> Dict[str, str]:
    """A part's relationships inside an Office file: each id, and the part it points at.

    A picture linked from the web rather than kept in the file is left out:
    it is not in the file, and nothing here fetches an address.
    """
    import posixpath
    import xml.etree.ElementTree as ET

    folder, _, name = part.rpartition("/")
    try:
        root = ET.fromstring(
            archive.read(f"{folder}/_rels/{name}.rels" if folder else f"_rels/{name}.rels")
        )
    except (KeyError, ET.ParseError):
        return {}
    found: Dict[str, str] = {}
    for rel in root.findall("rel:Relationship", _OOXML_NS):
        target = rel.get("Target") or ""
        if not target or rel.get("TargetMode") == "External":
            continue
        found[str(rel.get("Id"))] = (
            target.lstrip("/")
            if target.startswith("/")
            else posixpath.normpath(posixpath.join(folder, target))
        )
    return found


def _ooxml_pictures(archive: Any, rels: Mapping[str, str], element: Any) -> List[Tuple[str, bytes]]:
    """The pictures ``element`` shows, in the order it shows them, as their suffix and bytes."""
    import posixpath

    found: List[Tuple[str, bytes]] = []
    for blip in element.iter("{%s}blip" % _OOXML_NS["a"]):
        target = rels.get(blip.get("{%s}embed" % _OOXML_NS["r"]) or "")
        if not target:
            continue
        try:
            data = archive.read(target)
        except KeyError:
            continue
        found.append((posixpath.splitext(target)[1].lower() or ".png", data))
    return found


def _docx_pictures(document: Any, paragraph: Any) -> List[Tuple[str, bytes]]:
    """The pictures a Word paragraph shows, in order, as their suffix and bytes."""
    import posixpath

    from docx.oxml.ns import qn

    found: List[Tuple[str, bytes]] = []
    for blip in paragraph._p.iter(qn("a:blip")):
        part = document.part.related_parts.get(blip.get(qn("r:embed")))
        blob = getattr(part, "blob", None)
        if blob:
            found.append((posixpath.splitext(str(part.partname))[1].lower() or ".png", bytes(blob)))
    return found


# ============================================================================
# CHARTS AND SMARTART: what an Office drawing says in numbers and words
# ============================================================================
#
# INPUT   a chart part, or a SmartArt data part
# OUTPUT  the chart as its title and one row a category, "Region: EMEA;
#         2024: 41.7; 2025: 45.2", from the values the file keeps with it;
#         SmartArt as its items, one a line
#
# A chart in an Office file carries the numbers it was drawn from, in a
# cache beside the drawing. A reader that skips the drawing skips the
# numbers, and a question about them has nothing to find.

_CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_DGM_NS = "http://schemas.openxmlformats.org/drawingml/2006/diagram"


def _format_number(value: str, code: str) -> str:
    """A cached chart value as the chart shows it: a percent, a thousands separator, its decimals."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    code = (code or "").split(";")[0]
    decimals = (
        len(code.split(".")[1].rstrip('%)]\\"_ '))
        if "." in code
        else (0 if code and code != "General" else None)
    )
    if "%" in code:
        return f"{number * 100:.{decimals or 0}f}%"
    if decimals is None:
        return str(int(number)) if number.is_integer() else f"{number:g}"
    shown = f"{number:,.{decimals}f}" if "," in code else f"{number:.{decimals}f}"
    return shown


def _chart_points(element: Any) -> Dict[int, str]:
    """A series' cached values or categories, by their index."""
    if element is None:
        return {}
    c = "{%s}" % _CHART_NS
    found: Dict[int, str] = {}
    for cache in (
        list(element.iter(c + "numCache"))
        + list(element.iter(c + "strCache"))
        + list(element.iter(c + "multiLvlStrCache"))
    ):
        code_el = cache.find(c + "formatCode")
        code = code_el.text if code_el is not None else ""
        for pt in cache.iter(c + "pt"):
            v = pt.find(c + "v")
            if v is None or v.text is None:
                continue
            index = _int(pt.get("idx"), len(found))
            found.setdefault(
                index,
                _format_number(v.text, code)
                if cache.tag.endswith("numCache")
                else " ".join(v.text.split()),
            )
    if not found:
        for index, v in enumerate(element.iter(c + "v")):
            if v.text:
                found[index] = " ".join(v.text.split())
    return found


def _chart_title(element: Any) -> str:
    if element is None:
        return ""
    words = [t.text or "" for t in element.iter("{%s}t" % _OOXML_NS["a"])]
    if not words:
        words = [v.text or "" for v in element.iter("{%s}v" % _CHART_NS)]
    return " ".join("".join(words).split())


def _chart_lines(data: bytes) -> List[str]:
    """A chart part as text: "Chart: its title", then a line a category with each series' value."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    c = "{%s}" % _CHART_NS
    chart = root.find(c + "chart")
    if chart is None:
        return []
    title = _chart_title(chart.find(c + "title"))
    plot = chart.find(c + "plotArea")
    if plot is None:
        return [f"Chart: {title}"] if title else []
    axis = next(
        (
            _chart_title(ax.find(c + "title"))
            for ax in plot
            if ax.tag in (c + "catAx", c + "dateAx") and ax.find(c + "title") is not None
        ),
        "",
    )
    kinds: List[str] = []
    categories: Dict[int, str] = {}
    series: List[Tuple[str, Dict[int, str]]] = []
    for group in plot:
        name = group.tag.split("}")[-1]
        if not name.endswith("Chart"):
            continue
        kinds.append(name[:-5].replace("3D", " 3D").lower())
        for number, ser in enumerate(group.findall(c + "ser"), start=1):
            label = _chart_title(ser.find(c + "tx")) or f"Series {len(series) + 1}"
            cats = _chart_points(ser.find(c + "cat")) or _chart_points(ser.find(c + "xVal"))
            vals = _chart_points(ser.find(c + "val")) or _chart_points(ser.find(c + "yVal"))
            for index, value in cats.items():
                categories.setdefault(index, value)
            series.append((label, vals))
    kind = "/".join(dict.fromkeys(kinds))
    lines = [
        f"Chart: {title}" + (f" ({kind} chart)" if kind else "")
        if title
        else f"Chart ({kind or 'untitled'})"
    ]
    if not series:
        return lines
    indexes = sorted(set(categories) | {i for _l, vals in series for i in vals})
    head = axis or "Category"
    for index in indexes:
        cells = [f"{head}: {categories.get(index, str(index + 1))}"]
        cells += [f"{label}: {vals[index]}" for label, vals in series if index in vals]
        lines.append("; ".join(cells))
    return lines


def _smartart_lines(data: bytes) -> List[str]:
    """A SmartArt diagram's items, one a line, in the order the author entered them."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    d = "{%s}" % _DGM_NS
    lines: List[str] = []
    for point in root.iter(d + "pt"):
        if point.get("type") in ("parTrans", "sibTrans", "pres", "doc"):
            continue
        words = " ".join(
            "".join(t.text or "" for t in point.iter("{%s}t" % _OOXML_NS["a"])).split()
        )
        if words:
            lines.append("- " + words)
    return lines


def _drawing_extras(archive: Any, rels: Mapping[str, str], element: Any) -> List[str]:
    """The charts and SmartArt an Office drawing element shows, as lines, from the parts its relationships name."""
    lines: List[str] = []
    for found in element.iter():
        tag = found.tag if isinstance(found.tag, str) else ""
        if tag == "{%s}chart" % _CHART_NS:
            target = rels.get(found.get("{%s}id" % _OOXML_NS["r"]) or "")
            if target:
                try:
                    lines += _chart_lines(archive.read(target))
                except KeyError:
                    pass
        elif tag == "{%s}relIds" % _DGM_NS:
            target = rels.get(found.get("{%s}dm" % _OOXML_NS["r"]) or "")
            if target:
                try:
                    lines += _smartart_lines(archive.read(target))
                except KeyError:
                    pass
    return lines


def _xlsx_pictures(path: Path) -> Dict[str, List[Tuple[str, bytes]]]:
    """Each sheet's pictures, by the sheet's name, from the drawings it names.

    Read from the Open XML directly, because openpyxl reads no pictures in
    the read-only mode that keeps a large workbook out of memory.
    """
    import xml.etree.ElementTree as ET
    import zipfile

    found: Dict[str, List[Tuple[str, bytes]]] = {}
    with zipfile.ZipFile(str(path)) as archive:
        try:
            book = ET.fromstring(archive.read("xl/workbook.xml"))
        except (KeyError, ET.ParseError):
            return found
        sheets = _ooxml_rels(archive, "xl/workbook.xml")
        for sheet in book.iter("{%s}sheet" % _OOXML_NS["s"]):
            part = sheets.get(sheet.get("{%s}id" % _OOXML_NS["r"]) or "")
            try:
                root = ET.fromstring(archive.read(part)) if part else None
            except (KeyError, ET.ParseError):
                root = None
            if root is None:
                continue
            rels = _ooxml_rels(archive, part or "")
            shown: List[Tuple[str, bytes]] = []
            for drawing in root.iter("{%s}drawing" % _OOXML_NS["s"]):
                drawn = rels.get(drawing.get("{%s}id" % _OOXML_NS["r"]) or "")
                try:
                    shapes = ET.fromstring(archive.read(drawn)) if drawn else None
                except (KeyError, ET.ParseError):
                    shapes = None
                if shapes is not None:
                    shown.extend(
                        _ooxml_pictures(archive, _ooxml_rels(archive, drawn or ""), shapes)
                    )
            if shown:
                found[str(sheet.get("name") or "")] = shown
    return found


# ============================================================================
# PDF
# ============================================================================
#
# INPUT   a PDF's bytes
# OUTPUT  its printed page numbers from its own label table; its title when a
#         person would recognise it; its bookmarks as headings, each where its
#         title is written on its page; the document read, with OCR when a
#         page has too little text
#
# A PDF says more about itself than its text: the labels, the outline and the
# title are read from the original.


def _page_labels_of(reader: Any) -> Dict[int, str]:
    """A pypdf reader's page-label table by page, or nothing when it numbers its pages 1, 2, 3."""
    try:
        labels = [str(label) for label in reader.page_labels]
    except Exception:  # noqa: BLE001 - a broken table is no table, not a broken file
        return {}
    if all(label == str(number) for number, label in enumerate(labels, start=1)):
        return {}
    # A page printed with no number at all, a cover say, keeps its place.
    return {number: label for number, label in enumerate(labels, start=1) if label.strip()}


def pdf_page_labels(data: Union[bytes, str, Path]) -> Dict[int, str]:
    """The number printed on each page of a PDF, from its own page-label table.

    ``{1: "C1", 2: "C2", 3: "1", ...}``: every page, when the file numbers
    any of them other than by its place in the file, and nothing otherwise,
    for a file with no table, one that will not open, or no ``pypdf``. Read
    from the table and never guessed from a footer: the footer is taken out
    of the text as a running line, and a guess sends a reader to the wrong
    page.
    """
    try:
        import pypdf

        reader = pypdf.PdfReader(
            io.BytesIO(bytes(data)) if isinstance(data, (bytes, bytearray)) else str(data)
        )
    except Exception:  # noqa: BLE001 - no pypdf, or not a PDF it can open
        return {}
    return _page_labels_of(reader)


#: What a template or a printer driver calls a document when nobody named it.
_PLACEHOLDER_TITLES = {
    "untitled",
    "title",
    "document",
    "word document",
    "presentation",
    "powerpoint presentation",
    "slide 1",
    "book1",
    "sheet1",
    "workbook",
}


def _useful_title(value: Any) -> Optional[str]:
    """A title a person would recognise, or None.

    Not the file name a printer driver wrote, ``Microsoft Word - report.docx``
    is ``report``, and not a template's placeholder, ``Untitled`` or
    ``PowerPoint Presentation``.
    """
    text = " ".join(str(value or "").split())
    text = re.sub(
        r"^microsoft\s+(?:office\s+)?(?:word|powerpoint|excel)\s*-\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\.(?:docx?|pptx?|xlsx?|pdf|indd|rtf|odt|txt)$", "", text, flags=re.IGNORECASE
    ).strip()
    if not text or len(text) > 300 or text.lower() in _PLACEHOLDER_TITLES:
        return None
    return text


def _pdf_title(reader: Any) -> Optional[str]:
    """The title a PDF keeps in its properties, when it is one a person would recognise."""
    try:
        properties = reader.metadata
        return _useful_title(properties.get("/Title") if properties is not None else None)
    except Exception:  # noqa: BLE001 - properties that will not read are no title
        return None


def _find_heading(text: str, start: int, end: int, title: str) -> Optional[int]:
    """Where a bookmark's title is written on its page, at the start of a line if it can be; None when it is not there."""
    words = re.findall(r"\w+", title)
    if not words:
        return None
    pattern = r"\W+".join(re.escape(word) for word in words)
    page = text[start:end]
    for rx in (
        re.compile(r"(?:^|\n)[ \t]*(" + pattern + r")", re.IGNORECASE),
        re.compile(r"\b(" + pattern + r")\b", re.IGNORECASE),
    ):
        found = rx.search(page)
        if found:
            return start + found.start(1)
    return None


def _pdf_bookmark_headings(
    reader: Any, text: str, pages: Sequence[Tuple[int, int]]
) -> List[Tuple[int, str, int]]:
    """A PDF's own bookmarks as its headings: each where its title is written on its page, else where the page starts.

    The bookmarks are the section list the author made, so they are what a
    person reads as the document's structure; nothing is guessed from the
    size of a font. A bookmark's depth in the list is its level. A PDF
    without bookmarks has no headings, as before.
    """
    marks: List[Tuple[int, str, int]] = []

    def walk(items: Any, depth: int) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item, depth + 1)
                continue
            title = " ".join(str(getattr(item, "title", "") or "").split())
            if not title:
                continue
            try:
                page = reader.get_destination_page_number(item) + 1
            except Exception:  # noqa: BLE001 - a bookmark to nowhere is not a heading
                continue
            if page >= 1:
                marks.append((depth, title, page))

    try:
        walk(reader.outline, 0)
    except Exception:  # noqa: BLE001 - an outline that will not read is no outline
        return []
    if not marks or not pages:
        return []
    by_page = sorted(pages, key=lambda entry: entry[1])
    offsets = sorted({offset for offset, _page in pages})
    headings: List[Tuple[int, str, int]] = []
    for depth, title, page in marks:
        entry = next(((offset, number) for offset, number in by_page if number >= page), None)
        if entry is None:
            continue
        start = entry[0]
        end = next((offset for offset in offsets if offset > start), len(text))
        at = _find_heading(text, start, end, title) if entry[1] == page else None
        headings.append((start if at is None else at, title, min(depth + 1, 6)))
    # Stable, so a section and the first of its parts at one place stay in that order.
    headings.sort(key=lambda heading: heading[0])
    seen: set = set()
    unique: List[Tuple[int, str, int]] = []
    for heading in headings:
        if (heading[0], heading[1]) not in seen:
            seen.add((heading[0], heading[1]))
            unique.append(heading)
    return unique


def _pdf_extras(data: bytes, doc: "LoadedDocument") -> None:
    """What a PDF says about itself, read from the original: its printed page numbers, its bookmarks, its title.

    For a document read somewhere else, and above all one read in pieces:
    a piece cut from a PDF has none of the three of its own. Only what the
    document is missing is filled in.
    """
    if doc.page_labels and doc.headings and doc.metadata.get("title"):
        return
    try:
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(bytes(data)))
    except Exception:  # noqa: BLE001 - no pypdf, or not a PDF it can open
        return
    if not doc.page_labels:
        doc.page_labels = _page_labels_of(reader)
    if not doc.headings and doc.pages:
        doc.headings = _pdf_bookmark_headings(reader, doc.text, doc.pages)
    if not doc.metadata.get("title"):
        title = _pdf_title(reader)
        if title:
            doc.metadata["title"] = title


def _pdf_headings(
    reader: Any, text: str, pages: Sequence[Tuple[int, int]]
) -> List[Tuple[int, str, int]]:
    """A PDF's headings: its bookmarks, and under them the headings its type sets apart.

    The engine writes a line set larger, or bold and alone, as a Markdown
    heading. A PDF with no bookmarks has those as its headings. One with
    bookmarks keeps them on top, since its author chose them, and the
    headings its type shows nest one level under the deepest bookmark, so a
    report whose bookmarks stop at its chapters still has its sections.
    """
    typed = _markdown_headings(text)
    marks = _pdf_bookmark_headings(reader, text, pages) if reader is not None else []
    if not marks:
        return typed
    deepest = max(level for _o, _t, level in marks)
    key = lambda title: re.sub(r"\W+", " ", title).strip().lower()  # noqa: E731
    near = {(key(title), offset) for offset, title, _l in marks}
    merged = list(marks)
    for offset, title, level in typed:
        line_end = text.find("\n", offset)
        line_end = len(text) if line_end < 0 else line_end
        if any(k == key(title) and offset <= at <= line_end for k, at in near):
            continue
        merged.append((offset, title, min(6, deepest + level)))
    merged.sort(key=lambda heading: heading[0])
    return merged


def _pipe_tables_as_rows(text: str) -> str:
    """A text's Markdown tables written the way every reader writes a table's rows."""
    edits = _table_edits(text)
    if not edits:
        return text
    out: List[str] = []
    cursor = 0
    for start, end, replacement in sorted(edits):
        out.append(text[cursor:start])
        out.append(replacement)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _pages_by_sight(
    path: Path, engine: Any, page_reader: Callable[..., Any], every_page: bool, workers: int = 4
) -> Tuple[Dict[int, str], Dict[int, str], Dict[str, int]]:
    """The pages a reader that sees reads, each held to its own words.

    Which pages: the ones the rules were unsure of, or every page with words
    on it when ``every_page``. What comes back: each page's text by index, as
    every reader writes a page; the pages that stay with the rules, and why;
    and how many numbers were taken out: the ones the page does not print,
    and the guesses, values read off a chart or said to be guessed.
    A reader that fails, or a reading that is not taken, leaves its page as
    the rules read it: nothing is lost by asking.
    """
    import logging
    from concurrent.futures import ThreadPoolExecutor

    from .extract.page_reader import held_to_the_page

    wanted = [
        i
        for i, layer in enumerate(engine.layers)
        if layer.strip() and (every_page or (i + 1) in engine.hard)
    ]
    if not wanted:
        return {}, {}, {}
    drawn = dict(_pdf_page_images(path, wanted))

    def read(index: int) -> Tuple[int, Optional[str]]:
        try:
            return index, page_reader(
                drawn[index], engine.layers[index], {"name": path.name, "page": index + 1}
            )
        except Exception as exc:  # noqa: BLE001 - a page the reader fails on keeps its reading by the rules
            logging.getLogger("vectrixdb.ingest").warning(
                "page %d of %s was not read by sight: %s", index + 1, path.name, exc
            )
            return index, None

    seen: Dict[int, str] = {}
    kept: Dict[int, str] = {}
    dropped: Dict[str, int] = {"numbers_dropped": 0, "guesses_dropped": 0}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(wanted)))) as pool:
        for index, answer in pool.map(read, wanted):
            if not answer:
                kept[index + 1] = "no reading"
                continue
            held = held_to_the_page(answer, engine.layers[index], engine.ticks.get(index + 1, ()))
            if held is None:
                kept[index + 1] = "numbers or words the page does not hold"
                continue
            text, counts = held
            seen[index] = _pipe_tables_as_rows(text)
            for name in dropped:
                dropped[name] += counts.get(name, 0)
    return seen, kept, dropped


def _load_pdf(
    path: Path,
    images: bool = False,
    ocr: Optional[Callable[[bytes], Sequence[str]]] = None,
    min_chars: int = 24,
    page_reader: Optional[Callable[..., Any]] = None,
    every_page: bool = False,
) -> LoadedDocument:
    """A PDF's text, a page at a time, as a person reads it.

    With PDFium installed, :func:`vectrixdb.extract.pdf_text.read_pdf` rebuilds
    each page from where its text sits: columns, paragraphs, headings, tables
    with their headers, running heads and feet left out by their place in the
    margin, and text nobody can see left out and counted. Without it, pypdf's
    text is read and running lines are found by their repetition alone.

    ``page_reader`` reads a page by sight, a
    :class:`~vectrixdb.extract.page_reader.PageReader` or any callable of a
    page drawn as a PNG, its own words and where it is from, answering the
    page in Markdown. It is given the pages the rules are unsure of, or every
    page when ``every_page``, and what it writes is held to the page's own
    words before it takes the page's place.
    """
    from .exceptions import ExtractionError

    bodies: List[str] = []
    # The [Figure: ...] lines a page carries, kept apart from its text so
    # that reading the page again by OCR does not throw its figures away.
    marks: List[str] = []
    found: Dict[str, bytes] = {}
    labels: Dict[int, str] = {}
    reader: Any = None
    engine: Any = None
    refused: Optional[Exception] = None
    try:
        from .extract.pdf_text import read_pdf

        # With images asked for, a chart drawn with shapes is drawn as a picture
        # too; with a page reader, the pages the rules are unsure of are named.
        engine = read_pdf(path, charts=images, judge=page_reader is not None)
    except ImportError:
        engine = None
    except ExtractionError as exc:
        # PDFium is strict about a file's structure and pypdf forgives more:
        # a file one cannot open is handed to the other before it is refused.
        engine, refused = None, exc
    try:
        import pypdf

        reader = pypdf.PdfReader(str(path))
        labels = _page_labels_of(reader)
    except ImportError:
        if engine is None:
            try:
                import fitz  # PyMuPDF
            except ImportError as exc:
                raise DependencyError("pypdf", "documents") from exc
            with fitz.open(str(path)) as pdf:
                bodies = [page.get_text() for page in pdf]
    except Exception as exc:  # noqa: BLE001 - pypdf's own errors say little a caller can act on
        if engine is None:
            if refused is not None:
                raise refused from exc
            raise ExtractionError(
                f"{path.name} could not be read as a PDF: it is damaged, protected by a password, or not a PDF ({exc})"
            ) from exc
        reader = None

    if engine is not None:
        bodies = list(engine.pages)
    elif reader is not None and not bodies:
        if getattr(reader, "is_encrypted", False):
            try:
                opened = reader.decrypt("")
            except Exception:  # noqa: BLE001
                opened = 0
            if not opened:
                raise ExtractionError(
                    f"{path.name} is protected by a password, so its text cannot be read"
                )
        try:
            bodies = [page.extract_text() or "" for page in reader.pages]
        except Exception as exc:  # noqa: BLE001
            raise ExtractionError(f"{path.name} could not be read as a PDF: {exc}") from exc
    marks = [""] * len(bodies)
    if images and reader is not None:
        for number, page in enumerate(reader.pages, start=1):
            if number > len(marks):
                break
            try:
                page_images = list(page.images)
            except Exception:  # a broken image stream is not a broken page
                page_images = []
            for index, image in enumerate(page_images, start=1):
                suffix = Path(str(getattr(image, "name", "") or "")).suffix.lower() or ".png"
                name = f"p{number}-fig{index}{suffix}"
                found[name] = bytes(image.data)
                marks[number - 1] += f"\n\n[Figure: {name}]"
    charted: Set[int] = set()
    if images and engine is not None:
        # A chart's figure line is in its page's text already, where the chart
        # sits, with the words it prints under it; here is its picture.
        for number, _top, picture, name in engine.charts:
            found[name] = picture
            charted.add(number)

    sight: Dict[int, str] = {}
    left_to_rules: Dict[int, str] = {}
    unprinted: Dict[str, int] = {}
    if page_reader is not None and engine is not None:
        sight, left_to_rules, unprinted = _pages_by_sight(path, engine, page_reader, every_page)
        for index, text in sight.items():
            # What the reader saw on the page, pictures and charts included,
            # takes the page's place; its pictures are not described again.
            bodies[index] = text
            marks[index] = ""
            gone = f"p{index + 1}-"
            found = {name: data for name, data in found.items() if not name.startswith(gone)}

    by_ocr: List[int] = []
    blank = 0
    if ocr is not None:
        from .extract.layout import looks_blank

        # A page with next to nothing on it is a scan, or it is empty. Both
        # are worth telling apart before paying to read one. A page a picture
        # covers is a scan too, whatever caption was typed over it.
        pictured = set(engine.pictured) if engine is not None else set()
        # A page that draws a chart was made by a program, not a scanner,
        # however little of its text is left once the chart's words are its.
        thin = [
            i
            for i, body in enumerate(bodies)
            if (len(body.strip()) < min_chars and (i + 1) not in charted) or (i + 1) in pictured
        ]
        for index, drawn in _pdf_page_images(path, thin) if thin else ():
            if looks_blank(drawn):
                blank += 1
                continue
            lines = list(ocr(drawn) or [])
            if lines:
                from .extract.layout import paragraphs_of

                # A line of text a line of print, joined back into paragraphs.
                read = paragraphs_of(lines)
                kept = bodies[index].strip()
                bodies[index] = read if not kept or kept in read else f"{kept}\n\n{read}"
                by_ocr.append(index + 1)

    running: List[str] = list(engine.running) if engine is not None else []
    if engine is None:
        # The title at the top of every page and the "Page 3 of 10" at the
        # bottom are nobody's content. Without positions they are found by
        # repeating at the edges of most pages.
        from .extract.layout import drop_running_lines

        bodies, running = drop_running_lines(bodies)
    pages_text = [body + mark for body, mark in zip(bodies, marks)]
    text, pages = join_pages(pages_text)
    figures = _marked_figures(text, found)
    metadata: Dict[str, Any] = {"pages": len(pages)}
    headings = _pdf_headings(reader, text, pages)
    title = _pdf_title(reader) if reader is not None else None
    if title:
        metadata["title"] = title
    if running:
        metadata["running_lines"] = running
    if by_ocr:
        # Which pages were read by machine, so a chunk can say whether its own was.
        metadata["ocr"] = True
        metadata["pages_ocr"] = len(by_ocr)
        metadata["ocr_pages"] = by_ocr
    if blank:
        metadata["pages_blank"] = blank
    if engine is not None:
        # A page with no text that nothing read is a scan nobody paid to read,
        # and says so, rather than being an empty page in silence.
        unread = [n for n in engine.textless if n not in by_ocr]
        if unread:
            metadata["pages_without_text"] = unread
        if engine.hidden:
            metadata["hidden_text_left_out"] = engine.hidden
    if page_reader is not None:
        # Which pages were read by sight, and which stayed with the rules and why.
        metadata["page_reader"] = str(
            getattr(page_reader, "label", "") or type(page_reader).__name__
        )
        metadata["pages_read_by_sight"] = sorted(index + 1 for index in sight)
        if left_to_rules:
            metadata["pages_kept_by_rules"] = {
                str(number): why for number, why in sorted(left_to_rules.items())
            }
        if unprinted.get("numbers_dropped"):
            metadata["numbers_not_on_page"] = unprinted["numbers_dropped"]
        if unprinted.get("guesses_dropped"):
            # Values read off a chart's bars, or said to be guessed: taken out, not held against the reading.
            metadata["guesses_left_out"] = unprinted["guesses_dropped"]
    return LoadedDocument(
        text=text,
        pages=pages,
        headings=headings,
        metadata=metadata,
        figures=figures,
        images=found,
        page_labels=labels,
    )


# ============================================================================
# WORD 97-2003
# ============================================================================
#
# INPUT   a .doc file
# OUTPUT  the body read in pure Python from the WordDocument and table
#         streams, control characters made text
#
# No converter to install.

#: The first two bytes of a Word 97-2003 document's WordDocument stream.
_DOC_MAGIC = 0xA5EC
#: A field is an instruction and its result, both inside the text: 0x13 opens
#: one, 0x14 separates the instruction from what it shows, 0x15 closes it.
_FIELD_OPEN, _FIELD_SEPARATE, _FIELD_CLOSE = "\x13", "\x14", "\x15"
#: Word's own marks, as what a reader should see instead. A cell and a row both
#: end in 0x07, and without the table's properties the two cannot be told
#: apart, so cells come out tab separated rather than guessed into rows.
_DOC_MARKS = {
    "\r": "\n",  # paragraph
    "\x0b": "\n",  # line break inside a paragraph
    "\x0c": "\n",  # page or section break
    "\x07": "\t",  # end of a table cell
    "\x1e": "-",  # non-breaking hyphen
    "\x1f": "",  # optional hyphen, shown only where a line breaks
    "\xa0": " ",  # non-breaking space
}


def _doc_text(word: bytes, table: bytes) -> str:
    """The body of a Word 97-2003 document, from its WordDocument and table streams.

    The text is not stored in order. It is in pieces, and the piece table in
    the table stream says where each one is and how it is encoded: eight bit
    code page 1252, or UTF-16. The File Information Block at the start of
    WordDocument says where the piece table is and how long the body is.
    Everything after the body, headers, footers, footnotes and comments, is
    left out, because a running header on every chunk is noise and not
    content.
    """
    from .exceptions import ExtractionError

    if len(word) < 0x22 or struct.unpack_from("<H", word, 0)[0] != _DOC_MAGIC:
        raise ExtractionError("not a Word 97-2003 document: it does not start the way one does")
    flags = struct.unpack_from("<H", word, 0x0A)[0]
    if flags & 0x0100:
        raise ExtractionError("the document is password-protected, so its text cannot be read")
    csw = struct.unpack_from("<H", word, 0x20)[0]
    at_cslw = 0x22 + csw * 2
    cslw = struct.unpack_from("<H", word, at_cslw)[0]
    rg_lw = at_cslw + 2
    ccp_text = struct.unpack_from("<i", word, rg_lw + 12)[0]
    at_count = rg_lw + cslw * 4
    pairs = struct.unpack_from("<H", word, at_count)[0]
    if pairs < 34:
        raise ExtractionError(
            "the document's file information block is too short to hold a piece table"
        )
    fc_clx, lcb_clx = struct.unpack_from("<II", word, at_count + 2 + 33 * 8)
    clx = table[fc_clx : fc_clx + lcb_clx]

    # Formatting runs come first and are skipped: clxt 1, a length, its bytes.
    i = 0
    while i < len(clx) and clx[i] == 0x01:
        i += 3 + struct.unpack_from("<h", clx, i + 1)[0]
    if i + 5 > len(clx) or clx[i] != 0x02:
        raise ExtractionError("the document has no piece table where its header says one is")
    lcb = struct.unpack_from("<I", clx, i + 1)[0]
    plc = clx[i + 5 : i + 5 + lcb]
    count = (lcb - 4) // 12
    if count <= 0:
        return ""
    cps = struct.unpack_from(f"<{count + 1}i", plc, 0)
    pieces: List[str] = []
    for k in range(count):
        raw = struct.unpack_from("<I", plc, 4 * (count + 1) + 8 * k + 2)[0]
        length = cps[k + 1] - cps[k]
        if length <= 0:
            continue
        if raw & 0x40000000:
            start = (raw & 0x3FFFFFFF) // 2
            pieces.append(word[start : start + length].decode("cp1252", errors="replace"))
        else:
            start = raw & 0x3FFFFFFF
            pieces.append(word[start : start + 2 * length].decode("utf-16-le", errors="replace"))
    text = "".join(pieces)
    if ccp_text > 0:
        text = text[:ccp_text]
    return _clean_doc(text)


def _clean_doc(text: str) -> str:
    """Word's control characters as text a person reads.

    A field shows its result and hides its instruction: a hyperlink reads as
    its words and not as ``HYPERLINK "https://..."``. Fields nest, so this
    keeps a stack rather than looking for the next close.
    """
    out: List[str] = []
    # One entry a field that is open: True while its instruction is being read.
    stack: List[bool] = []
    for ch in text:
        if ch == _FIELD_OPEN:
            stack.append(True)
            continue
        if ch == _FIELD_SEPARATE:
            if stack:
                stack[-1] = False
            continue
        if ch == _FIELD_CLOSE:
            if stack:
                stack.pop()
            continue
        if any(stack):
            continue
        out.append(_DOC_MARKS.get(ch, ch))
    joined = "".join(out)
    # Anything else below a space is a placeholder: a picture, a drawing, the
    # anchor of a footnote. None of them is a character anybody typed.
    joined = "".join(c for c in joined if c >= " " or c in "\n\t")
    lines = [line.rstrip(" \t") for line in joined.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _doc_streams(path: Path) -> Tuple[bytes, bytes]:
    """The WordDocument stream and whichever table stream it uses."""
    from .exceptions import ExtractionError

    try:
        import olefile
    except ImportError as exc:
        raise DependencyError("olefile", "documents") from exc
    if not olefile.isOleFile(str(path)):
        raise ExtractionError(
            f"{path.name} is not a Word 97-2003 document. A .doc that is really RTF or HTML "
            "has been renamed; save it as .docx, or give it the extension it has"
        )
    ole = olefile.OleFileIO(str(path))
    try:
        if not ole.exists("WordDocument"):
            raise ExtractionError(
                f"{path.name} is an OLE file but not a Word document: it has no WordDocument stream"
            )
        word = ole.openstream("WordDocument").read()
        which = (
            "1Table"
            if len(word) > 0x0B and struct.unpack_from("<H", word, 0x0A)[0] & 0x0200
            else "0Table"
        )
        if not ole.exists(which):
            raise ExtractionError(f"{path.name} names a {which} stream it does not have")
        table = ole.openstream(which).read()
    finally:
        ole.close()
    return word, table


def _load_doc(path: Path) -> LoadedDocument:
    """A Word 97-2003 document, read in pure Python.

    Pure Python on purpose: ``antiword`` and LibreOffice read these better
    but are programs to install, and a function app on a Flex plan has no
    way to install one. So this is text and paragraphs, and not the
    headings and tables a ``.docx`` gives: a .doc keeps those in property
    tables this does not interpret. Table cells come out tab separated.
    """
    word, table = _doc_streams(path)
    return LoadedDocument(text=_doc_text(word, table))


# ============================================================================
# WORD
# ============================================================================
#
# INPUT   a .docx file
# OUTPUT  everything in its body in order: headings, lists with their markers
#         as Word shows them, footnotes and endnotes, page breaks, page
#         numbers by section, and pictures
#
# Read the way the document reads, not the way its XML is arranged.

#: WordprocessingML; the wrapper Word puts round a text box so that an older
#: reader gets a copy of it, which is not read a second time; Office's maths.
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
#: What holds words inside a paragraph, and so is walked into. A deletion, a
#: move away and a field's instructions are not: none of them is shown.
_W_INSIDE = {
    "r",
    "hyperlink",
    "ins",
    "moveTo",
    "smartTag",
    "sdt",
    "sdtContent",
    "fldSimple",
    "customXml",
    "dir",
    "bdo",
}


def _w(tag: str) -> str:
    return "{%s}%s" % (_W, tag)


def _w_on(properties: Any, tag: str) -> bool:
    """Whether a run or paragraph property is switched on: present, and not val="false" or "0"."""
    if properties is None:
        return False
    found = properties.find(_w(tag))
    return found is not None and str(found.get(_w("val"), "true")).lower() not in (
        "false",
        "0",
        "off",
    )


def _w_unseen(element: Any) -> bool:
    """Whether a run is hidden text, or a content control shows only its placeholder.

    Text marked hidden is how a draft keeps its notes, "remove before
    publishing", and a placeholder is Word's own "Click or tap here to enter
    text". Neither is what the document says.
    """
    space, name = _local(element)
    if space != _W:
        return False
    if name == "r":
        return _w_on(element.find(_w("rPr")), "vanish")
    if name == "sdt":
        return _w_on(element.find(_w("sdtPr")), "showingPlcHdr")
    return False


def _local(element: Any) -> Tuple[str, str]:
    """An element's namespace and name; two empty strings for a comment or an instruction."""
    tag = element.tag
    if not isinstance(tag, str):
        return "", ""
    if tag.startswith("{"):
        space, _, name = tag[1:].partition("}")
        return space, name
    return "", tag


def _int(value: Any, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _roman(n: int) -> str:
    out: List[str] = []
    for value, letters in (
        (1000, "m"),
        (900, "cm"),
        (500, "d"),
        (400, "cd"),
        (100, "c"),
        (90, "xc"),
        (50, "l"),
        (40, "xl"),
        (10, "x"),
        (9, "ix"),
        (5, "v"),
        (4, "iv"),
        (1, "i"),
    ):
        while n >= value:
            out.append(letters)
            n -= value
    return "".join(out)


def _word_number(n: int, fmt: str) -> str:
    """A number the way Word shows it in a list or on a page: 3, iii, III, c, C."""
    if n < 1:
        return str(n)
    if fmt == "lowerRoman":
        return _roman(n)
    if fmt == "upperRoman":
        return _roman(n).upper()
    if fmt in ("lowerLetter", "upperLetter"):
        letters = chr(ord("a") + (n - 1) % 26) * ((n - 1) // 26 + 1)
        return letters if fmt == "lowerLetter" else letters.upper()
    if fmt == "decimalZero":
        return f"{n:02d}"
    return str(n)


def _w_children(element: Any, names: Sequence[str]) -> Iterable[Any]:
    """The children with these names, looking through the content controls and custom XML wrapped round them."""
    for child in element:
        space, name = _local(child)
        if space != _W:
            continue
        if name in names:
            yield child
        elif name == "sdt":
            content = child.find(_w("sdtContent"))
            if content is not None:
                yield from _w_children(content, names)
        elif name == "customXml":
            yield from _w_children(child, names)


class _WordNotes:
    """A document's footnotes and endnotes, numbered in the order the text first calls them, as Word shows them."""

    def __init__(self, document: Any) -> None:
        from docx.oxml import parse_xml

        self._words: Dict[Tuple[str, str], str] = {}
        self._marker: Dict[Tuple[str, str], str] = {}
        self._count = {"f": 0, "e": 0}
        self.written: set = set()
        for relationship in document.part.rels.values():
            reltype = str(relationship.reltype)
            kind = (
                "f"
                if reltype.endswith("/footnotes")
                else "e"
                if reltype.endswith("/endnotes")
                else ""
            )
            if not kind or relationship.is_external:
                continue
            try:
                root = parse_xml(relationship.target_part.blob)
            except Exception:  # noqa: BLE001 - notes that will not parse are notes not read
                continue
            for note in root:
                space, name = _local(note)
                # The separator lines Word keeps among the notes are not notes.
                if (
                    space != _W
                    or name not in ("footnote", "endnote")
                    or note.get(_w("type")) not in (None, "normal")
                ):
                    continue
                paragraphs = [
                    " ".join("".join(t.text or "" for t in p.iter(_w("t"))).split())
                    for p in note.iter(_w("p"))
                ]
                words = " ".join(p for p in paragraphs if p)
                if words:
                    self._words[(kind, str(note.get(_w("id"))))] = words

    def call(self, kind: str, note_id: Any) -> Optional[str]:
        """The marker for a note the text calls, numbered on its first call; None for a note with no words."""
        key = (kind, str(note_id))
        if key not in self._words:
            return None
        if key not in self._marker:
            self._count[kind] += 1
            self._marker[key] = str(self._count[kind]) if kind == "f" else f"e{self._count[kind]}"
        return self._marker[key]

    def words(self, marker: str) -> str:
        for key, given in self._marker.items():
            if given == marker:
                return self._words[key]
        return ""


def _omml_text(element: Any) -> str:
    """A Word equation as a line of text: a/(b+c) = x^2, not "ab+c=x2"."""
    m = "{%s}" % _M
    name = _local(element)[1]

    def inner(tag: str) -> str:
        found = element.find(m + tag)
        return "".join(_omml_text(c) for c in found) if found is not None else ""

    def grouped(text: str) -> str:
        return f"({text})" if len(text) > 1 and re.search(r"[+\-*/=\s]", text) else text

    if name == "t":
        return element.text or ""
    if name == "f":
        return f"{grouped(inner('num'))}/{grouped(inner('den'))}"
    if name == "sSup":
        return f"{inner('e')}^{grouped(inner('sup'))}"
    if name == "sSub":
        return f"{inner('e')}_{grouped(inner('sub'))}"
    if name == "sSubSup":
        return f"{inner('e')}_{grouped(inner('sub'))}^{grouped(inner('sup'))}"
    if name == "rad":
        degree = inner("deg")
        return (f"root[{degree}]" if degree else "sqrt") + f"({inner('e')})"
    if name == "d":
        props = element.find(m + "dPr")
        begin = props.find(m + "begChr") if props is not None else None
        end = props.find(m + "endChr") if props is not None else None
        opening = begin.get(m + "val") if begin is not None else "("
        closing = end.get(m + "val") if end is not None else ")"
        return f"{opening}{', '.join(''.join(_omml_text(c) for c in e) for e in element.findall(m + 'e'))}{closing}"
    if name == "nary":
        props = element.find(m + "naryPr")
        sign = props.find(m + "chr") if props is not None else None
        symbol = sign.get(m + "val") if sign is not None else "\u222b"
        lower, upper = inner("sub"), inner("sup")
        return f"{symbol}{('_' + grouped(lower)) if lower else ''}{('^' + grouped(upper)) if upper else ''} {inner('e')}"
    if name.endswith("Pr"):
        return ""
    return "".join(_omml_text(c) for c in element)


class _WordRun:
    """One paragraph's words in document order, where pages begin in them, and what they call."""

    def __init__(self, notes: _WordNotes) -> None:
        self.parts: List[str] = []
        self.length = 0
        self.breaks: List[int] = []
        self.rendered = False
        self.called: List[str] = []
        self.boxes: List[str] = []
        self.comments: List[str] = []
        self._notes = notes

    @property
    def text(self) -> str:
        return "".join(self.parts)

    def add(self, words: str) -> None:
        if words:
            self.parts.append(words)
            self.length += len(words)

    def walk(self, element: Any) -> None:
        for child in element:
            space, name = _local(child)
            if space == _MC and name == "AlternateContent":
                # Word's own version; the fallback is the same thing again for an older reader.
                chosen = child.find("{%s}Choice" % _MC)
                if chosen is None:
                    chosen = child.find("{%s}Fallback" % _MC)
                if chosen is not None:
                    self.walk(chosen)
            elif space == _M and name in ("oMath", "oMathPara"):
                self.add(_omml_text(child))
            elif space != _W:
                continue
            elif name == "t":
                self.add(child.text or "")
            elif name in ("tab", "ptab"):
                self.add("\t")
            elif name == "cr":
                self.add("\n")
            elif name == "br":
                kind = child.get(_w("type"))
                if kind == "page":
                    self.breaks.append(self.length)
                elif kind != "column":
                    self.add("\n")
            elif name == "lastRenderedPageBreak":
                # Where Word began a page the last time it laid the document out.
                self.breaks.append(self.length)
                self.rendered = True
            elif name == "noBreakHyphen":
                self.add("-")
            elif name == "commentReference":
                self.comments.append(str(child.get(_w("id"))))
            elif name in ("footnoteReference", "endnoteReference"):
                marker = self._notes.call(
                    "f" if name == "footnoteReference" else "e", child.get(_w("id"))
                )
                if marker:
                    self.called.append(marker)
                    self.add(f"[^{marker}]")
            elif name in ("drawing", "pict", "object"):
                for box in child.iter(_w("txbxContent")):
                    for inner in box.iter(_w("p")):
                        words = " ".join("".join(t.text or "" for t in inner.iter(_w("t"))).split())
                        if words:
                            self.boxes.append(words)
            elif name in _W_INSIDE:
                if _w_unseen(child):
                    continue
                self.walk(child)


def _docx_comments(document: Any) -> Dict[str, str]:
    """A document's comments by id, each as its author and words: "Ada Lovelace: check the figure"."""
    from docx.oxml import parse_xml

    found: Dict[str, str] = {}
    try:
        relationships = list(document.part.rels.values())
    except Exception:  # noqa: BLE001
        return found
    for relationship in relationships:
        if (
            not str(getattr(relationship, "reltype", "")).endswith("/comments")
            or relationship.is_external
        ):
            continue
        try:
            root = parse_xml(relationship.target_part.blob)
        except Exception:  # noqa: BLE001 - comments that will not parse are not read
            continue
        for comment in root.iter(_w("comment")):
            words = " ".join(" ".join(t.text or "" for t in comment.iter(_w("t"))).split())
            if words:
                author = comment.get(_w("author"))
                found[str(comment.get(_w("id")))] = f"{author}: {words}" if author else words
    return found


def _docx_running(document: Any, notes: Any) -> Dict[str, List[str]]:
    """What the headers and footers of a document's sections say, each once, page numbers left out."""
    out: Dict[str, List[str]] = {"page_header": [], "page_footer": []}
    try:
        sections = list(document.sections)
    except Exception:  # noqa: BLE001
        return out
    for section in sections:
        for key, parts in (
            ("page_header", ("header", "first_page_header", "even_page_header")),
            ("page_footer", ("footer", "first_page_footer", "even_page_footer")),
        ):
            for attribute in parts:
                try:
                    part = getattr(section, attribute)
                    if part.is_linked_to_previous:
                        continue
                    element = part._element
                except Exception:  # noqa: BLE001 - a section without that part
                    continue
                for paragraph in element.iter(_w("p")):
                    run = _WordRun(notes)
                    run.walk(paragraph)
                    words = " ".join(run.text.split())
                    if (
                        words
                        and not re.fullmatch(r"(?:page\s*)?[\d\s/of-]+", words, re.IGNORECASE)
                        and words not in out[key]
                    ):
                        out[key].append(words)
    return out


def _docx_numbering(document: Any) -> Dict[str, Dict[int, Tuple[Any, ...]]]:
    """Every list's format, first number, text and legal flag by level, ``{numId: {level: (format, start, text, legal)}}``.

    The text is Word's own pattern for the number, ``%1.%2`` or ``(%1)`` or
    ``Article %1 -``, so "1.2", "(a)" and "Article I -" come out as printed.
    """
    try:
        root = document.part.numbering_part.element
    except Exception:  # noqa: BLE001 - a document with no lists
        return {}
    abstract: Dict[str, Dict[int, Tuple[Any, ...]]] = {}
    for definition in root.findall(_w("abstractNum")):
        levels: Dict[int, Tuple[Any, ...]] = {}
        for level in definition.findall(_w("lvl")):
            fmt = level.find(_w("numFmt"))
            start = level.find(_w("start"))
            pattern = level.find(_w("lvlText"))
            levels[_int(level.get(_w("ilvl")), 0)] = (
                (fmt.get(_w("val")) if fmt is not None else None) or "decimal",
                _int(start.get(_w("val")) if start is not None else None, 1),
                pattern.get(_w("val")) if pattern is not None else None,
                level.find(_w("isLgl")) is not None,
            )
        abstract[str(definition.get(_w("abstractNumId")))] = levels
    lists: Dict[str, Dict[int, Tuple[Any, ...]]] = {}
    for num in root.findall(_w("num")):
        ref = num.find(_w("abstractNumId"))
        levels = dict(abstract.get(str(ref.get(_w("val"))) if ref is not None else "", {}))
        for override in num.findall(_w("lvlOverride")):
            level_index = _int(override.get(_w("ilvl")), 0)
            start = override.find(_w("startOverride"))
            if start is not None and level_index in levels:
                levels[level_index] = (
                    levels[level_index][0],
                    _int(start.get(_w("val")), 1),
                ) + tuple(levels[level_index][2:])
        lists[str(num.get(_w("numId")))] = levels
    return lists


def _docx_find(p: Any, style: Any, tag: str) -> Any:
    """A paragraph property, from the paragraph, else from its style and the styles that style is based on."""
    properties = p.find(_w("pPr"))
    found = properties.find(_w(tag)) if properties is not None else None
    hops = 0
    while found is None and style is not None and hops < 20:
        own = style.element.find(_w("pPr"))
        found = own.find(_w(tag)) if own is not None else None
        style = style.base_style
        hops += 1
    return found


def _docx_styles(document: Any) -> Tuple[Dict[str, Any], Any]:
    """Every paragraph style by its id, and the default one, looked up once.

    Asking python-docx for a paragraph's style searches every style every
    time: most of the time a large document took, 23 seconds for 20,000
    paragraphs, went there.
    """
    by_id: Dict[str, Any] = {}
    default = None
    try:
        for style in document.styles:
            by_id[str(style.style_id)] = style
        from docx.enum.style import WD_STYLE_TYPE

        default = document.styles.default(WD_STYLE_TYPE.PARAGRAPH)
    except Exception:  # noqa: BLE001 - a document whose styles will not list is read with python-docx's own lookups
        return {}, None
    return by_id, default


def _docx_style_of(p: Any, styles: Tuple[Dict[str, Any], Any]) -> Any:
    properties = p.find(_w("pPr"))
    named = properties.find(_w("pStyle")) if properties is not None else None
    wanted = named.get(_w("val")) if named is not None else None
    return styles[0].get(str(wanted), styles[1]) if wanted else styles[1]


_HEADING_STYLE = re.compile(r"^heading\s*([1-9])$", re.IGNORECASE)
_HEADING_ID = re.compile(r"^Heading([1-9])$")


def _docx_kind(
    paragraph: Any, p: Any, styles: Optional[Tuple[Dict[str, Any], Any]] = None
) -> Tuple[str, int, Optional[Tuple[str, int]]]:
    """A paragraph's style name, its heading level, 0 for none, and its list, ``(numId, level)``, or None.

    A heading is a style named "Heading 1" to "Heading 9", or with that id
    whatever the language calls it, or a paragraph with an outline level; a
    style that only starts with the word, "Heading Caption", is not one.
    """
    style = _docx_style_of(p, styles) if styles and styles[0] else paragraph.style
    name = (style.name or "") if style is not None else ""
    style_id = str(getattr(style, "style_id", "") or "")
    lowered = name.lower()
    level = 0
    named = _HEADING_STYLE.match(name.strip()) or _HEADING_ID.match(style_id)
    if named:
        level = int(named.group(1))
    elif lowered == "title" or style_id == "Title":
        level = 1
    else:
        outline = _docx_find(p, style, "outlineLvl")
        value = _int(outline.get(_w("val")), 9) if outline is not None else 9
        if value < 9:
            level = value + 1
    listed: Optional[Tuple[str, int]] = None
    numbering = _docx_find(p, style, "numPr")
    if numbering is not None:
        num = numbering.find(_w("numId"))
        depth = numbering.find(_w("ilvl"))
        num_id = str(num.get(_w("val"))) if num is not None else ""
        if num_id and num_id != "0":
            listed = (num_id, _int(depth.get(_w("val")) if depth is not None else None, 0))
    return name, min(level, 6), listed


def _list_marker(
    listed: Tuple[str, int],
    lists: Mapping[str, Dict[int, Tuple[Any, ...]]],
    counters: Dict[Tuple[str, int], int],
) -> str:
    """A list item's marker as Word shows it, ``- `` for a bullet, ``3. `` or ``iii. `` for a number, indented by its level."""
    num_id, level = listed
    spec: Tuple[Any, ...] = tuple(lists.get(num_id, {}).get(level, ("bullet", 1))) + (None, False)
    fmt, start, pattern, legal = spec[0], spec[1], spec[2], spec[3]
    for key in [k for k in counters if k[0] == num_id and k[1] > level]:
        # A list under an item starts again under the next one.
        del counters[key]
    indent = "  " * level
    if fmt == "bullet":
        return indent + "- "
    if fmt == "none":
        return indent
    counters[(num_id, level)] = counters.get((num_id, level), start - 1) + 1
    if pattern and "%" in pattern:
        # Word's own pattern: every level's number where it says %1, %2.
        def number(match: Any) -> str:
            at = int(match.group(1)) - 1
            other: Tuple[Any, ...] = tuple(lists.get(num_id, {}).get(at, ("decimal", 1))) + (
                None,
                False,
            )
            value = counters.get((num_id, at), other[1])
            return _word_number(value, "decimal" if legal and at < level else other[0])

        shown = re.sub(r"%([1-9])", number, pattern).strip()
        if shown:
            return indent + shown + " "
    return indent + _word_number(counters[(num_id, level)], fmt) + ". "


def _docx_page_labels(
    pages: Sequence[Tuple[int, int]], sections: Sequence[Tuple[Optional[int], Any]]
) -> Dict[int, str]:
    """The number printed on each page, where a section numbers its own pages: from 1 again, or as i, ii, iii.

    Each section's ``pgNumType`` says how, and a section that says nothing
    carries on from the one before. Nothing comes back when every page is
    printed with its place in the file, which is the usual Word document.
    """
    bounds: List[Tuple[int, str, Optional[int]]] = []
    first = 0
    for end, properties in sections:
        numbering = properties.find(_w("pgNumType"))
        fmt = (numbering.get(_w("fmt")) if numbering is not None else None) or "decimal"
        start = numbering.get(_w("start")) if numbering is not None else None
        bounds.append((first, fmt, _int(start, 1) if start is not None else None))
        if end is not None:
            first = end + 1
    if not bounds:
        return {}
    labels: Dict[int, str] = {}
    counter, current = 0, -1
    for offset, number in pages:
        index = max(i for i, (begin, _fmt, _start) in enumerate(bounds) if begin <= offset)
        _begin, fmt, start = bounds[index]
        counter = start if index != current and start is not None else counter + 1
        current = index
        labels[number] = _word_number(counter, fmt)
    if all(label == str(number) for number, label in labels.items()):
        return {}
    return labels


def _load_docx(path: Path, images: bool = False) -> LoadedDocument:
    """A Word document the way it reads: everything in its body, in order.

    Headings become Markdown headings, a "Title" paragraph a top-level one;
    a list item keeps its bullet, or its number as Word shows it; a table is
    one line per row, ``Header: value; Header: value``, where it sits, with a
    merged cell's value in every row it spans; a footnote or an endnote
    follows the paragraph that calls it, ``[^1]: ...``; a text box follows
    the paragraph it is anchored to; a content control's words are read, a
    tracked deletion's are not. With ``images``, each picture is a figure
    after the paragraph that shows it.

    A Word file keeps no pages, but Word marks where each page began the
    last time it laid the document out and saved it. Where there are such
    marks, every part of the text has its page, and a section that numbers
    its own pages, from 1 again or in roman numerals, gives the number
    printed on each. A file saved by software that lays nothing out has no
    marks and no pages, and is cited by its headings: pages counted from
    manual breaks alone would be wrong wherever text ran onto a new page by
    itself. The title is the file's own, else its "Title" paragraph.
    """
    try:
        import docx
        from docx.text.paragraph import Paragraph
    except ImportError as exc:
        raise DependencyError("python-docx", "documents") from exc
    try:
        document = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001 - python-docx says "Package not found" for a file it cannot parse
        _refuse(
            f"{path.name} could not be read as a Word document: it is damaged or not a .docx ({exc})"
        )
        raise
    lists = _docx_numbering(document)
    notes = _WordNotes(document)
    styles = _docx_styles(document)
    comments = _docx_comments(document)
    import zipfile

    try:
        archive: Any = zipfile.ZipFile(str(path))
        drawing_rels = _ooxml_rels(archive, "word/document.xml")
    except (OSError, zipfile.BadZipFile):
        archive, drawing_rels = None, {}
    counters: Dict[Tuple[str, int], int] = {}
    parts: List[str] = []
    headings: List[Tuple[int, str, int]] = []
    found: Dict[str, bytes] = {}
    starts: List[int] = []
    sections: List[Tuple[Optional[int], Any]] = []
    state: Dict[str, Any] = {
        "offset": 0,
        "pending": False,
        "rendered": False,
        "title": None,
        "first_heading": None,
    }

    def emit(line: str, breaks: Sequence[int] = ()) -> int:
        # One line of the text; ``breaks`` are where in it a new page begins.
        if parts:
            state["offset"] += 2
        begin = state["offset"]
        if state["pending"]:
            starts.append(begin)
            state["pending"] = False
        for at in breaks:
            if at >= len(line):
                state["pending"] = True
            else:
                starts.append(begin + max(at, 0))
        parts.append(line)
        state["offset"] += len(line)
        return begin

    def after(
        called: Sequence[str],
        boxes: Sequence[str],
        pictures: Sequence[Tuple[str, bytes]],
        said: Sequence[str] = (),
        drawn: Sequence[str] = (),
    ) -> None:
        # What follows a paragraph or a table: the notes it calls, its text
        # boxes, its charts, SmartArt and alt text, its comments, its pictures.
        for marker in called:
            if marker not in notes.written:
                notes.written.add(marker)
                emit(f"[^{marker}]: {notes.words(marker)}")
        for box in boxes:
            emit(box)
        for line in drawn:
            emit(line)
        for comment in said:
            if comment in comments:
                emit(f"[Comment: {comments[comment]}]")
        for suffix, data in pictures:
            name = f"fig{len(found) + 1}{suffix}"
            found[name] = data
            emit(f"[Figure: {name}]")

    def paragraph(p: Any) -> None:
        para = Paragraph(p, document)
        run = _WordRun(notes)
        run.walk(p)
        state["rendered"] = state["rendered"] or run.rendered
        breaks = list(run.breaks)
        properties = p.find(_w("pPr"))
        before = properties.find(_w("pageBreakBefore")) if properties is not None else None
        if before is not None and before.get(_w("val")) not in ("0", "false", "off"):
            breaks.insert(0, 0)
        raw = unicodedata.normalize("NFC", run.text)
        words = raw.strip()
        style, level, listed = _docx_kind(para, p, styles)
        if style.lower().startswith("toc") or style.lower() in ("table of contents", "contents"):
            # A table of contents is the headings again, with page numbers:
            # read, it is the first chunk of every document and cites nothing.
            return
        if level:
            # A heading is one line, and its footnote mark is not its title.
            words = re.sub(r"\[\^[^\]]+\]", "", " ".join(words.split())).strip()
        prefix = ""
        if words and level:
            number = _list_marker(listed, lists, counters).strip() if listed is not None else ""
            prefix = "#" * level + " " + (number + " " if number and number != "-" else "")
            if style.lower() == "title" and state["title"] is None:
                state["title"] = words
            if state["first_heading"] is None and level == 1:
                state["first_heading"] = words
        elif words and listed is not None:
            prefix = _list_marker(listed, lists, counters)
        pictures = _docx_pictures(document, para) if images else []
        if words:
            line = prefix + words
            lead = len(raw) - len(raw.lstrip())
            at: List[int] = []
            for b in breaks:
                inside = b - lead
                if level or inside <= 0:
                    at.append(0)
                elif inside >= len(words):
                    at.append(len(line))
                else:
                    at.append(len(prefix) + inside)
            begin = emit(line, at)
            if level:
                headings.append((begin, words, level))
        elif breaks:
            state["pending"] = True
        drawn: List[str] = []
        for drawing in p.iter(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}drawing"
        ):
            if archive is not None:
                drawn += _drawing_extras(archive, drawing_rels, drawing)
            if not images:
                # A picture's alt text is what it shows, in the author's words, and costs nothing to keep.
                for described in drawing.iter(
                    "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}docPr"
                ):
                    alt = " ".join(
                        str(described.get("descr") or described.get("title") or "").split()
                    )
                    if (
                        alt
                        and not _DEFAULT_ALT.match(alt)
                        and not any(True for _ in drawing.iter("{%s}chart" % _CHART_NS))
                    ):
                        drawn.append(figure_line(alt))
        after(run.called, run.boxes, pictures, run.comments, drawn)
        if properties is not None and properties.find(_w("sectPr")) is not None:
            sections.append((state["offset"], properties.find(_w("sectPr"))))

    def cell(tc: Any) -> Tuple[str, bool, List[str], List[str], List[Tuple[str, bytes]]]:
        # A cell's words on one line, whether a page began in it, and what follows it.
        words: List[str] = []
        broke = False
        called: List[str] = []
        boxes: List[str] = []
        pictures: List[Tuple[str, bytes]] = []
        for child in _w_children(tc, ("p", "tbl")):
            if _local(child)[1] == "p":
                run = _WordRun(notes)
                run.walk(child)
                state["rendered"] = state["rendered"] or run.rendered
                broke = broke or bool(run.breaks)
                words.append(" ".join(run.text.split()))
                called.extend(run.called)
                boxes.extend(run.boxes)
                if images:
                    pictures.extend(_docx_pictures(document, Paragraph(child, document)))
                continue
            # A table inside a cell is read as the words of its cells.
            for row in _w_children(child, ("tr",)):
                for inner in _w_children(row, ("tc",)):
                    text, inner_broke, inner_called, inner_boxes, inner_pictures = cell(inner)
                    words.append(text)
                    broke = broke or inner_broke
                    called.extend(inner_called)
                    boxes.extend(inner_boxes)
                    pictures.extend(inner_pictures)
        return " ".join(w for w in words if w), broke, called, boxes, pictures

    def table(tbl: Any) -> None:
        rows: List[List[Optional[str]]] = []
        broken: List[bool] = []
        called: List[str] = []
        boxes: List[str] = []
        pictures: List[Tuple[str, bytes]] = []
        merged: Dict[int, str] = {}
        marked = 0
        counting = True
        for tr in _w_children(tbl, ("tr",)):
            cells: List[Optional[str]] = []
            column = 0
            row_broke = False
            row_props = tr.find(_w("trPr"))
            if counting and row_props is not None and _w_on(row_props, "tblHeader"):
                marked += 1  # a row Word repeats at the top of every page: the header
            else:
                counting = False
            skipped = row_props.find(_w("gridBefore")) if row_props is not None else None
            if skipped is not None:
                # Columns a row leaves out at its start: its cells sit further right.
                column = max(_int(skipped.get(_w("val")), 0), 0)
                cells.extend([None] * column)
            for tc in _w_children(tr, ("tc",)):
                properties = tc.find(_w("tcPr"))
                span_of = properties.find(_w("gridSpan")) if properties is not None else None
                span = max(_int(span_of.get(_w("val")) if span_of is not None else None, 1), 1)
                vmerge = properties.find(_w("vMerge")) if properties is not None else None
                text, cell_broke, cell_called, cell_boxes, cell_pictures = cell(tc)
                if vmerge is not None and vmerge.get(_w("val")) != "restart":
                    # The rest of a cell merged down the rows: its value again, so each row says it.
                    text = merged.get(column, "")
                elif vmerge is not None:
                    merged[column] = text
                else:
                    merged.pop(column, None)
                cells.append(text or None)
                cells.extend([None] * (span - 1))
                column += span
                row_broke = row_broke or cell_broke
                called.extend(cell_called)
                boxes.extend(cell_boxes)
                pictures.extend(cell_pictures)
            rows.append(cells)
            broken.append(row_broke)
        shown = _table_row_lines(rows, marked or None)
        if shown:
            written = {index for index, _line in shown}
            # A page that began in the header row, or in a row with nothing in it, begins with the table.
            at: List[int] = [0] if any(b and i not in written for i, b in enumerate(broken)) else []
            position = 0
            for index, line in shown:
                if broken[index]:
                    at.append(position)
                position += len(line) + 1
            emit("\n".join(line for _index, line in shown), at)
        elif any(broken):
            state["pending"] = True
        after(called, boxes, pictures)

    def block(container: Any) -> None:
        for child in container:
            space, name = _local(child)
            if space != _W:
                continue
            if name == "p":
                paragraph(child)
            elif name == "tbl":
                table(child)
            elif name == "sdt":
                content = child.find(_w("sdtContent"))
                gallery = child.find(
                    _w("sdtPr") + "/" + _w("docPartObj") + "/" + _w("docPartGallery")
                )
                if gallery is not None and "contents" in str(gallery.get(_w("val")) or "").lower():
                    continue  # a table of contents: the headings again, with page numbers
                if content is not None and not _w_unseen(child):
                    block(content)
            elif name == "customXml":
                block(child)
            elif name == "sectPr":
                sections.append((None, child))

    block(document.element.body)
    if archive is not None:
        archive.close()
    text = "\n\n".join(parts)
    pages: List[Tuple[int, int]] = []
    labels: Dict[int, str] = {}
    if state["rendered"]:
        begun: List[int] = []
        for at in sorted({offset for offset in starts if 0 < offset < len(text)}):
            # A manual break and the mark Word wrote after it are one new page.
            if begun and at - begun[-1] <= 2:
                begun[-1] = at
            else:
                begun.append(at)
        pages = [(0, 1)] + [(at, number) for number, at in enumerate(begun, start=2)]
        labels = _docx_page_labels(pages, sections)
    metadata: Dict[str, Any] = {}
    if pages:
        metadata["pages"] = len(pages)
    running = _docx_running(document, notes)
    for key, found_lines in running.items():
        if found_lines:
            # What every page's header and footer say: the document's name and
            # its marking, "CONFIDENTIAL", once, not on every chunk's text.
            metadata[key] = " | ".join(found_lines)
    title = (
        _useful_title(document.core_properties.title)
        or state["title"]
        or _useful_title(state["first_heading"])
        or _useful_title(next(iter(running.get("page_header") or []), None))
    )
    if title:
        metadata["title"] = title
    return LoadedDocument(
        text=text,
        pages=pages,
        headings=headings,
        metadata=metadata,
        figures=_marked_figures(text, found),
        images=found,
        page_labels=labels,
    )


# ============================================================================
# SPREADSHEETS AND CSV
# ============================================================================
#
# INPUT   an .xlsx or .csv file
# OUTPUT  every sheet under its own heading, every row as one line, Header:
#         value
#
# A row a line, so a chunk holds whole rows.


def _cell_text(value: Any) -> str:
    """A cell as a reader would say it: whole numbers without a trailing .0,
    dates as dates, everything else as its string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if hasattr(value, "isoformat"):
        iso = value.isoformat()
        return iso[:10] if iso.endswith("T00:00:00") else iso
    return " ".join(str(value).split())


def rows_to_lines(rows: Sequence[Sequence[Any]]) -> List[str]:
    """Tabular rows as one line each, ``Header: value; Header: value``.

    The first row is the header when every filled cell in it is text. A
    header turns a row into something a sentence embedding can read and a
    keyword search can hit: ``Region: EMEA; Revenue: 1200`` rather than
    ``EMEA 1200``. Empty cells are left out, so a sparse row says what it
    holds and nothing about what it does not.
    """
    return [line for _row, line in _row_lines(rows)]


def _row_lines(rows: Sequence[Sequence[Any]]) -> List[Tuple[int, str]]:
    """``rows_to_lines`` with the row each line came from, for a reader that has to say where a row sits."""
    kept = [
        (index, list(r))
        for index, r in enumerate(rows)
        if any(c is not None and str(c).strip() for c in r)
    ]
    if not kept:
        return []
    first = kept[0][1]
    filled = [c for c in first if c is not None and str(c).strip()]
    # A header needs something under it: a sheet holding one line of text
    # is a line of text, not a header with no rows.
    has_header = len(kept) > 1 and bool(filled) and all(isinstance(c, str) for c in filled)
    header = [(_cell_text(c) if c is not None else "") for c in first] if has_header else []
    body = kept[1:] if has_header else kept
    lines: List[Tuple[int, str]] = []
    for index, row in body:
        cells: List[str] = []
        for i, value in enumerate(row):
            if value is None or str(value).strip() == "":
                continue
            text = _cell_text(value)
            name = header[i] if i < len(header) and header[i] else ""
            cells.append(f"{name}: {text}" if name else text)
        if cells:
            lines.append((index, "; ".join(cells)))
    return lines


# ============================================================================
# TABLES: where a table starts, what heads it, what each cell says
# ============================================================================
#
# INPUT   the rows of a sheet, a CSV, a slide's or a document's table
# OUTPUT  a line a row, "Region: EMEA; 2024 H1: 580", with the title lines
#         above a table kept as text and every table in a sheet on its own
#
# A sheet is often a report: a title, a note on units, a table, a blank row,
# another table with its own header. Taking the first row of the sheet as
# the header of everything gave "Quarterly Sales Report: EMEA", and a CSV,
# whose cells are all strings, took its first row of data for a header.

_YEARISH = re.compile(r"^(?:19|20)\d{2}$")
_NUMBERISH = re.compile(
    r"^[(\-\u2212+]?\s*[$\u20ac\u00a3\u00a5]?\s*\(?\d[\d,.\s']*\)?\s*%?\)?\s*[kKmMbB]?$"
)
_DATEISH_TEXT = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}(?:[T ][\d:]+)?|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,4}(?:,?\s*\d{4})?)$",
    re.IGNORECASE,
)


def _cell_kind(value: Any) -> str:
    """ "empty", "text", "number", "year" or "date": what a cell holds, whatever type it came as."""
    if value is None:
        return "empty"
    if isinstance(value, bool):
        return "text"
    if isinstance(value, (int, float)):
        return "year" if float(value).is_integer() and 1900 <= value <= 2100 else "number"
    if hasattr(value, "isoformat"):
        return "date"
    words = str(value).strip()
    if not words:
        return "empty"
    if _YEARISH.match(words):
        return "year"
    if _NUMBERISH.match(words):
        return "number"
    if _DATEISH_TEXT.match(words):
        return "date"
    return "text"


def _plain_rows(rows: Sequence[Sequence[Any]]) -> List[str]:
    """Rows with no header, each a line of its filled cells."""
    lines = []
    for row in rows:
        cells = [_cell_text(c) for c in row if c is not None and str(c).strip()]
        if cells:
            lines.append("; ".join(cells))
    return lines


def _header_rows_lines(
    rows: Sequence[Sequence[Any]], header_rows: Optional[int] = None
) -> List[str]:
    """One table's rows as lines; see :func:`_table_row_lines`."""
    return [line for _index, line in _table_row_lines(rows, header_rows)]


def _table_row_lines(
    given: Sequence[Sequence[Any]], forced: Optional[int] = None
) -> List[Tuple[int, str]]:
    """One table's rows as lines, each with the row it came from, its header found by what its cells hold.

    A header is a row of words, years or dates over rows that hold numbers,
    or the first row of a table that holds no numbers at all. Two such rows
    over the numbers are one header joined per column, "2024 H1", a label
    printed once over its columns heading each of them. A table of two
    columns whose second column holds values of every kind is a form,
    "Applicant name: Jane Doe", not a header over one row. Rows under no
    header are their cells, in order. ``forced`` is how many rows the file
    itself marks as the header, a Word table's repeated header rows.
    """
    kept = [
        (i, list(r))
        for i, r in enumerate(given)
        if any(c is not None and str(c).strip() for c in r)
    ]
    if not kept:
        return []
    origin = [i for i, _r in kept]
    rows = [r for _i, r in kept]
    width = max(len(r) for r in rows)
    rows = [r + [None] * (width - len(r)) for r in rows]
    kinds = [[_cell_kind(c) for c in r] for r in rows]

    def filled(i: int) -> List[str]:
        return [k for k in kinds[i] if k != "empty"]

    def worded(i: int) -> bool:
        found = filled(i)
        return bool(found) and all(k in ("text", "year", "date") for k in found)

    def figures(i: int) -> bool:
        return any(k == "number" for k in kinds[i][1:])

    columns = [j for j in range(width) if any(kinds[i][j] != "empty" for i in range(len(rows)))]
    if forced is None and len(columns) == 2 and len(rows) >= 3:
        first, second = columns
        keys = [kinds[i][first] for i in range(len(rows))]
        values = {kinds[i][second] for i in range(len(rows)) if kinds[i][second] != "empty"}
        labelled = all(k == "text" for k in keys) and len(
            {str(rows[i][first]).strip().lower() for i in range(len(rows))}
        ) == len(rows)
        colons = all(str(rows[i][first]).strip().endswith(":") for i in range(len(rows)))
        body_values = {kinds[i][second] for i in range(1, len(rows)) if kinds[i][second] != "empty"}
        if labelled and (
            colons
            or (len(values) >= 2 and not (kinds[0][second] == "text" and len(body_values) == 1))
        ):
            pairs: List[Tuple[int, str]] = []
            for i in range(len(rows)):
                key = str(rows[i][first]).strip().rstrip(":").strip()
                value = rows[i][second]
                pairs.append(
                    (
                        origin[i],
                        f"{key}: {_cell_text(value)}"
                        if value is not None and str(value).strip()
                        else key,
                    )
                )
            return pairs

    any_figures = any(figures(i) for i in range(len(rows)))
    header_rows = 0
    if forced is not None:
        header_rows = max(0, min(int(forced), len(rows) - 1))
    elif len(rows) > 1 and worded(0) and (figures(1) or not any_figures or worded(1)):
        header_rows = 1
        if (
            len(rows) > 2
            and worded(1)
            and not figures(1)
            and any(figures(i) for i in range(2, len(rows)))
            and not figures(0)
        ):
            header_rows = 2
    if (
        forced is None
        and len(rows) > 1
        and not header_rows
        and all(k in ("year", "text", "empty") for k in kinds[0])
        and kinds[0].count("year") >= 2
        and figures(1)
    ):
        header_rows = 1  # years as numbers over the columns: 2023, 2024
    if not header_rows:
        return [(origin[i], line) for i, row in enumerate(rows) for line in _plain_rows([row])]
    names: List[str] = []
    for j in range(width):
        parts: List[str] = []
        for i in range(header_rows):
            value = rows[i][j]
            if (value is None or not str(value).strip()) and i < header_rows - 1:
                # A label printed once over the columns after it: carried along, under the row below it.
                back = j - 1
                while (
                    back >= 0
                    and (rows[i][back] is None or not str(rows[i][back]).strip())
                    and rows[header_rows - 1][back] is not None
                ):
                    back -= 1
                if (
                    back >= 0
                    and rows[header_rows - 1][j] is not None
                    and str(rows[header_rows - 1][j]).strip()
                ):
                    value = rows[i][back]
            if value is not None and str(value).strip():
                said = _cell_text(value)
                if not parts or parts[-1] != said:
                    parts.append(said)
        names.append(" ".join(parts))
    written: List[Tuple[int, str]] = []
    for i in range(header_rows, len(rows)):
        cells = []
        for j, value in enumerate(rows[i]):
            if value is None or not str(value).strip():
                continue
            shown = _cell_text(value)
            cells.append(f"{names[j]}: {shown}" if names[j] else shown)
        if cells:
            written.append((origin[i], "; ".join(cells)))
    return written


def _sheet_lines(rows: Sequence[Sequence[Any]]) -> List[str]:
    """A sheet as text: each table on it read on its own, the lines around them as text.

    Tables are cut apart where a whole row is empty, and where a whole column
    is empty between two tables side by side. A line with one cell in it,
    above or below a table, is a title, a note on units or a source.
    """
    lines: List[str] = []
    block: List[List[Any]] = []
    held: List[List[Any]] = []

    def worded(row: Sequence[Any]) -> bool:
        kinds = [_cell_kind(c) for c in row if c is not None and str(c).strip()]
        return len(kinds) >= 2 and all(k in ("text", "year", "date") for k in kinds)

    def filled(row: Sequence[Any]) -> int:
        # A title merged across the columns is one cell, however many it covers.
        values = [str(c).strip() for c in row if c is not None and str(c).strip()]
        return 1 if values and len(set(values)) == 1 else len(values)

    last: Dict[str, Any] = {"head": None, "width": 0}

    def flush() -> None:
        if not block:
            return
        width = max(len(r) for r in block)
        grid = [list(r) + [None] * (width - len(r)) for r in block]
        widest = max(filled(r) for r in grid)
        if widest <= 1:
            # Lines of one cell each, a title and a note: text, not a table.
            lines.extend(
                next(_cell_text(c) for c in r if c is not None and str(c).strip()) for r in grid
            )
            block.clear()
            return
        if last["head"] is not None and not worded(grid[0]) and filled(grid[0]) == last["width"]:
            # The table above goes on after a blank line: its header heads these rows too.
            grid.insert(0, list(last["head"]) + [None] * max(0, width - len(last["head"])))
        elif worded(grid[0]) and len(grid) > 1:
            last["head"], last["width"] = grid[0], filled(grid[0])
        top = 0
        while top < len(grid) - 1 and widest >= 2 and filled(grid[top]) == 1:
            lines.append(next(_cell_text(c) for c in grid[top] if c is not None and str(c).strip()))
            top += 1
        bottom = len(grid)
        tail: List[str] = []
        while bottom - 1 > top and widest >= 2 and filled(grid[bottom - 1]) == 1:
            tail.insert(
                0, next(_cell_text(c) for c in grid[bottom - 1] if c is not None and str(c).strip())
            )
            bottom -= 1
        body = grid[top:bottom]
        empty = [
            j for j in range(width) if all(r[j] is None or not str(r[j]).strip() for r in body)
        ]
        spans: List[Tuple[int, int]] = []
        start = None
        for j in range(width + 1):
            if j < width and j not in empty:
                start = j if start is None else start
            elif start is not None:
                spans.append((start, j))
                start = None
        for left, right in spans or [(0, width)]:
            lines.extend(_header_rows_lines([r[left:right] for r in body]))
        lines.extend(tail)
        block.clear()

    for row in rows:
        if filled(row):
            if held and not block:
                # A header, a blank line, then its rows: the header still heads them.
                if not worded(row) and filled(row) >= 2:
                    block.extend(held)
                else:
                    block.append(held[0])
                    flush()
                held.clear()
            block.append(list(row))
        else:
            if len(block) == 1 and worded(block[0]):
                held.append(block[0])
                block.clear()
            else:
                flush()
    if held:
        block.extend(held)
    flush()
    return lines


_XL_SYMBOL = re.compile(r'\[\$([^\-\]]+)(?:-[^\]]*)?\]|"([^"]*)"|([$\u20ac\u00a3\u00a5])')


def _format_cell(value: Any, code: Optional[str]) -> Any:
    """A cell as the workbook shows it: 4.2%, $1,234.50, (250), Mar 2024, 36:00.

    Excel keeps 0.042 and shows 4.2%; a search for "4.2%" should find it,
    and a reader should read what the author saw. A format this does not
    know leaves the value as it is.
    """
    import datetime as _dt

    if value is None or isinstance(value, bool):
        return value
    code = str(code or "General")
    if isinstance(value, _dt.timedelta):
        hours = value.total_seconds() / 3600
        return f"{int(hours)}:{int(round((hours % 1) * 60)):02d}"
    if isinstance(value, (_dt.datetime, _dt.date)):
        lowered = code.lower()
        if "mmm" in lowered and "d" not in lowered.replace("mmm", ""):
            return value.strftime("%B %Y" if "mmmm" in lowered else "%b %Y")
        return value
    if not isinstance(value, (int, float)) or code == "General" or code == "@":
        return value
    sections = code.split(";")
    section = sections[1] if value < 0 and len(sections) > 1 else sections[0]
    body = re.sub(
        r"\[[^\]]*\]", lambda m: m.group(0) if m.group(0).startswith("[$") else "", section
    )
    if not re.search(r"[0#]", body):
        return value
    decimals = 0
    point = re.search(r"\.([0#]+)", body)
    if point:
        decimals = len(point.group(1))
    number = abs(value) * (100 if "%" in body else 1)
    if re.search(r"E[+-]", body):
        return ("-" if value < 0 else "") + f"{number:.{decimals}E}"
    shown = f"{number:,.{decimals}f}" if "," in body else f"{number:.{decimals}f}"
    symbol = ""
    for m in _XL_SYMBOL.finditer(body):
        found = m.group(1) or m.group(2) or m.group(3) or ""  # [$€-x], "$" in quotes, or a bare $
        if found.strip():
            symbol = found.strip()
            break
    if "%" in body:
        shown += "%"
    if symbol:
        digit = re.search(r"[0#]", body)
        shown = (
            (symbol + shown)
            if body.find(symbol) <= (digit.start() if digit else 0)
            else (shown + " " + symbol)
        )
    if value < 0:
        shown = f"({shown})" if "(" in section and len(sections) > 1 else "-" + shown
    return shown


#: What a program calls a picture when nobody described it: "Picture 3", "Image", "Chart 1". Not alt text.
_DEFAULT_ALT = re.compile(
    r"^(?:picture|image|graphic|chart|shape|object|diagram|photo|figure|grafik|bild|imagen)\s*\d*$",
    re.IGNORECASE,
)


def _xlsx_sheet_extras(path: Path) -> Dict[str, Dict[str, Any]]:
    """What openpyxl's read-only mode does not say about each sheet, read from its XML.

    Its merged ranges, its hidden rows and columns, whether it holds formulas
    with no value saved, and the charts its drawings show.
    """
    import xml.etree.ElementTree as ET
    import zipfile

    found: Dict[str, Dict[str, Any]] = {}
    try:
        archive = zipfile.ZipFile(str(path))
    except (OSError, zipfile.BadZipFile):
        return found
    with archive:
        try:
            book = ET.fromstring(archive.read("xl/workbook.xml"))
        except (KeyError, ET.ParseError):
            return found
        sheets = _ooxml_rels(archive, "xl/workbook.xml")
        for sheet in book.iter("{%s}sheet" % _OOXML_NS["s"]):
            part = sheets.get(sheet.get("{%s}id" % _OOXML_NS["r"]) or "")
            if not part:
                continue
            try:
                data = archive.read(part)
            except KeyError:
                continue
            merged = [
                m.decode() for m in re.findall(rb'<mergeCell\s+ref="([A-Z]+\d+:[A-Z]+\d+)"', data)
            ]
            hidden_rows = {
                int(r)
                for r in re.findall(rb'<row\s[^>]*?r="(\d+)"[^>]*?\bhidden="(?:1|true)"', data)
            }
            hidden_rows |= {
                int(r)
                for r in re.findall(rb'<row\s[^>]*?\bhidden="(?:1|true)"[^>]*?r="(\d+)"', data)
            }
            hidden_cols: Set[int] = set()
            for tag in re.findall(rb"<col\s[^>]*>", data):
                # Attributes in any order: openpyxl writes hidden before min and max.
                attributes = dict(re.findall(rb'(\w+)="([^"]*)"', tag))
                if attributes.get(b"hidden") in (b"1", b"true"):
                    hidden_cols.update(
                        range(
                            int(attributes.get(b"min", b"0")), int(attributes.get(b"max", b"0")) + 1
                        )
                    )
            # A formula nobody recalculated is saved with no value, or an empty one.
            uncached = (
                re.search(
                    rb"<f[^>]*>[^<]*</f>\s*(?:<v\s*/>|<v>\s*</v>)?\s*</c>|<f[^>]*/>\s*(?:<v\s*/>|<v>\s*</v>)?\s*</c>",
                    data,
                )
                is not None
            )
            charts: List[str] = []
            rels = _ooxml_rels(archive, part or "")
            for drawing in re.findall(rb'<drawing\s[^>]*?r:id="([^"]+)"', data):
                drawn = rels.get(drawing.decode())
                if not drawn:
                    continue
                try:
                    shapes = ET.fromstring(archive.read(drawn))
                except (KeyError, ET.ParseError):
                    continue
                charts += _drawing_extras(archive, _ooxml_rels(archive, drawn), shapes)
                for described in shapes.iter(
                    "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}cNvPr"
                ):
                    alt = " ".join(str(described.get("descr") or "").split())
                    if alt and not _DEFAULT_ALT.match(alt):
                        charts.append(figure_line(alt))
            found[str(sheet.get("name") or "")] = {
                "merged": merged,
                "hidden_rows": hidden_rows,
                "hidden_cols": hidden_cols,
                "uncached": uncached,
                "charts": charts,
            }
    return found


def _cell_ref(ref: str) -> Tuple[int, int]:
    """``B3`` as its row and column, both from 1."""
    letters = re.match(r"([A-Z]+)(\d+)", ref)
    if not letters:
        return 0, 0
    column = 0
    for ch in letters.group(1):
        column = column * 26 + (ord(ch) - 64)
    return int(letters.group(2)), column


def _xlsx_rows(sheet: Any, extras: Mapping[str, Any], formulas: Any = None) -> List[List[Any]]:
    """A sheet's rows as the workbook shows them, merged cells filled and hidden rows and columns left out."""
    rows: List[List[Any]] = []
    numbers: List[int] = []
    formula_rows = iter(formulas.iter_rows(values_only=True)) if formulas is not None else None
    for row in sheet.iter_rows():
        cells = list(row)
        written = next(formula_rows, None) if formula_rows is not None else None
        values: List[Any] = []
        for index, cell in enumerate(cells):
            value = getattr(cell, "value", None)
            if value is None and written is not None and index < len(written):
                raw = written[index]
                if isinstance(raw, str) and raw.startswith("="):
                    value = raw  # a formula with no value saved: its formula, rather than nothing
            values.append(_format_cell(value, getattr(cell, "number_format", None)))
        first = next((getattr(c, "row", None) for c in cells if getattr(c, "row", None)), None)
        numbers.append(first or (numbers[-1] + 1 if numbers else 1))
        rows.append(values)
    for ref in extras.get("merged", []):
        start, end = ref.split(":")
        top, left = _cell_ref(start)
        bottom, right = _cell_ref(end)
        try:
            at = numbers.index(top)
        except ValueError:
            continue
        value = rows[at][left - 1] if left - 1 < len(rows[at]) else None
        for r in range(top, bottom + 1):
            if r not in numbers:
                continue
            i = numbers.index(r)
            while len(rows[i]) < right:
                rows[i].append(None)
            for c in range(left, right + 1):
                if (r, c) != (top, left):
                    rows[i][c - 1] = value
    hidden_rows, hidden_cols = extras.get("hidden_rows", set()), extras.get("hidden_cols", set())
    if hidden_rows or hidden_cols:
        rows = [
            [v for c, v in enumerate(row, start=1) if c not in hidden_cols]
            for n, row in zip(numbers, rows)
            if n not in hidden_rows
        ]
    return rows


def _load_xlsx(path: Path, images: bool = False) -> LoadedDocument:
    """Every sheet under its own heading, every row as one line; with
    ``images``, each picture as a figure after its sheet's rows."""
    try:
        import openpyxl
    except ImportError as exc:
        raise DependencyError("openpyxl", "documents") from exc
    import zipfile

    pictures = _xlsx_pictures(path) if images else {}
    extras = _xlsx_sheet_extras(path)
    formulas_book: Any = None
    try:
        workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - openpyxl raises zip, key, value and XML errors for a damaged file
        _refuse(
            f"{path.name} could not be read as an Excel workbook: it is damaged or protected by a password ({exc})"
        )
        raise
    parts: List[str] = []
    headings: List[Tuple[int, str, int]] = []
    found: Dict[str, bytes] = {}
    offset = 0
    sheets = 0
    hidden: List[str] = []
    try:
        for index, sheet in enumerate(workbook.worksheets, start=1):
            if getattr(sheet, "sheet_state", "visible") != "visible":
                # A hidden sheet is scratch or lookups, not what the workbook says: named, not read.
                hidden.append(sheet.title)
                continue
            if hasattr(sheet, "reset_dimensions"):
                # The size a sheet says it is can be wrong: some tools write A1
                # over a whole table, and a reader that believed it read one cell.
                sheet.reset_dimensions()
            about = extras.get(sheet.title, {})
            formulas = None
            if about.get("uncached"):
                # A formula nobody recalculated has no value saved; its formula is read instead.
                if formulas_book is None:
                    formulas_book = openpyxl.load_workbook(
                        str(path), read_only=True, data_only=False
                    )
                formulas = formulas_book[sheet.title]
                if hasattr(formulas, "reset_dimensions"):
                    formulas.reset_dimensions()
            lines = _sheet_lines(_xlsx_rows(sheet, about, formulas)) + list(about.get("charts", []))
            shown = pictures.get(sheet.title, [])
            # A sheet that is only a picture, a chart pasted in, is still read.
            if not lines and not shown:
                continue
            sheets += 1
            title = "# " + sheet.title
            headings.append((offset, sheet.title, 1))
            parts.append(title)
            offset += len(title) + 2
            if lines:
                block = "\n".join(lines)
                parts.append(block)
                offset += len(block) + 2
            for number, (suffix, data) in enumerate(shown, start=1):
                name = f"sheet{index}-fig{number}{suffix}"
                found[name] = data
                line = f"[Figure: {name}]"
                parts.append(line)
                offset += len(line) + 2
        for chartsheet in getattr(workbook, "chartsheets", []):
            # A sheet that is only a chart: its numbers, under its name.
            drawn = extras.get(chartsheet.title, {}).get("charts", [])
            if drawn:
                sheets += 1
                title = "# " + chartsheet.title
                headings.append((offset, chartsheet.title, 1))
                parts.append(title)
                offset += len(title) + 2
                block = "\n".join(drawn)
                parts.append(block)
                offset += len(block) + 2
    finally:
        workbook.close()
        if formulas_book is not None:
            formulas_book.close()
    text = "\n\n".join(parts)
    metadata: Dict[str, Any] = {"sheets": sheets}
    if hidden:
        metadata["sheets_hidden"] = hidden
    try:
        with zipfile.ZipFile(str(path)) as archive:
            title = _ooxml_core_title(archive)
    except (OSError, zipfile.BadZipFile):
        title = None
    if title:
        metadata["title"] = title
    return LoadedDocument(
        text=text,
        headings=headings,
        metadata=metadata,
        figures=_marked_figures(text, found),
        images=found,
    )


def _csv_dialect(text: str, suffix: str) -> Tuple[str, str]:
    """The delimiter a delimited file uses, and its text without Excel's ``sep=`` line.

    Excel writes ``sep=;`` on the first line of a file it saves for a
    locale whose decimal mark is a comma; a .tsv is tab separated; anything
    else is sniffed from its first lines among comma, semicolon, tab and
    pipe, and is a comma when nothing stands out.
    """
    import csv

    first, _, rest = text.partition("\n")
    said = re.fullmatch(r"\s*sep=(.)\s*", first)
    if said:
        return said.group(1), rest
    if suffix == ".tsv":
        return "\t", text
    sample = "\n".join(text.split("\n", 50)[:50])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter, text
    except csv.Error:
        counts = {d: sample.count(d) for d in ",;\t|"}
        best = max(counts, key=lambda d: counts[d])
        return (best if counts[best] else ","), text


def _load_csv(path: Path) -> LoadedDocument:
    """A delimited file as one line per row, headed by its first row, in whatever encoding and delimiter it uses."""
    import csv
    import io

    text = _decode_text(path.read_bytes())
    delimiter, text = _csv_dialect(text, path.suffix.lower())
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    lines = _sheet_lines(rows)
    return LoadedDocument(text="\n".join(lines), metadata={"rows": len(lines)})


# ============================================================================
# POWERPOINT
# ============================================================================
#
# INPUT   a .pptx file
# OUTPUT  every slide as a page with its title as a heading, its text and its
#         speaker notes
#
# Slides in presentation order, from the relationship list.

_PPTX_NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def _pptx_slide_paths(archive: Any) -> List[str]:
    """Slide parts in presentation order, from the relationship list, or by
    number when the deck has no usable list."""
    import xml.etree.ElementTree as ET

    names = set(archive.namelist())
    try:
        pres = ET.fromstring(archive.read("ppt/presentation.xml"))
        rels = ET.fromstring(archive.read("ppt/_rels/presentation.xml.rels"))
        targets = {
            rel.get("Id"): rel.get("Target") for rel in rels.findall("rel:Relationship", _PPTX_NS)
        }
        ordered = []
        for sld in pres.findall("p:sldIdLst/p:sldId", _PPTX_NS):
            target = targets.get(sld.get("{%s}id" % _PPTX_NS["r"]))
            if target:
                part = target if target.startswith("ppt/") else "ppt/" + target.lstrip("/")
                if part in names:
                    ordered.append(part)
        if ordered:
            return ordered
    except (KeyError, ET.ParseError):
        pass
    slides = [n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
    return sorted(slides, key=lambda n: int(re.findall(r"\d+", n)[-1]))


_PPTX_SKIPPED = frozenset({"dt", "ftr", "sldNum", "hdr"})
_P14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"


def _pptx_offset(xfrm: Any) -> Tuple[float, float]:
    off = xfrm.find("a:off", _PPTX_NS) if xfrm is not None else None
    return (float(off.get("x", 0)), float(off.get("y", 0))) if off is not None else (0.0, 0.0)


def _pptx_paragraphs(body: Any, bulleted: bool) -> List[str]:
    """A text body's paragraphs, a line each, a line break a new line, a bullet by its level."""
    a = "{%s}" % _PPTX_NS["a"]
    lines: List[str] = []
    for para in body.findall("a:p", _PPTX_NS):
        pieces: List[str] = []
        for child in para:
            if child.tag == a + "r" or child.tag == a + "fld":
                if child.tag == a + "fld" and str(child.get("type") or "").startswith(
                    ("slidenum", "datetime")
                ):
                    continue
                pieces.append("".join(t.text or "" for t in child.findall("a:t", _PPTX_NS)))
            elif child.tag == a + "br":
                pieces.append("\n")
        props = para.find("a:pPr", _PPTX_NS)
        level = _int(props.get("lvl") if props is not None else None, 0)
        marked = props is not None and (
            props.find("a:buChar", _PPTX_NS) is not None
            or props.find("a:buAutoNum", _PPTX_NS) is not None
        )
        unmarked = props is not None and props.find("a:buNone", _PPTX_NS) is not None
        for n, line in enumerate("".join(pieces).split("\n")):
            words = " ".join(line.split())
            if not words:
                continue
            if (bulleted or marked) and not unmarked:
                lines.append("  " * level + "- " + words)
            else:
                lines.append(words)
    return lines


def _pptx_items(
    tree: Any, archive: Any, rels: Mapping[str, str], shift: Tuple[float, float, float, float]
) -> List[Tuple[float, float, str, List[str]]]:
    """A shape tree's items as ``(top, left, role, lines)``: role is "title" or "body", placed on the slide."""
    ox, oy, sx, sy = shift
    items: List[Tuple[float, float, str, List[str]]] = []
    for shape in tree:
        tag = shape.tag.split("}")[-1]
        if tag == "AlternateContent":
            # Read once: the choice, else the fallback, never both.
            chosen = shape.find("{%s}Choice" % _MC) or shape.find("{%s}Fallback" % _MC)
            if chosen is not None:
                items += _pptx_items(chosen, archive, rels, shift)
            continue
        if tag == "grpSp":
            xfrm = shape.find("p:grpSpPr/a:xfrm", _PPTX_NS)
            if xfrm is not None:
                off, ext = xfrm.find("a:off", _PPTX_NS), xfrm.find("a:ext", _PPTX_NS)
                choff, chext = xfrm.find("a:chOff", _PPTX_NS), xfrm.find("a:chExt", _PPTX_NS)
                if off is not None and choff is not None and ext is not None and chext is not None:
                    scale_x = float(ext.get("cx", 1)) / max(float(chext.get("cx", 1)), 1.0)
                    scale_y = float(ext.get("cy", 1)) / max(float(chext.get("cy", 1)), 1.0)
                    inner = (
                        float(off.get("x", 0)) - float(choff.get("x", 0)) * scale_x,
                        float(off.get("y", 0)) - float(choff.get("y", 0)) * scale_y,
                        scale_x,
                        scale_y,
                    )
                    inner = (ox + inner[0] * sx, oy + inner[1] * sy, sx * inner[2], sy * inner[3])
                    items += _pptx_items(shape, archive, rels, inner)
                    continue
            items += _pptx_items(shape, archive, rels, shift)
            continue
        if tag == "sp":
            placeholder = shape.find("p:nvSpPr/p:nvPr/p:ph", _PPTX_NS)
            kind = placeholder.get("type") if placeholder is not None else None
            if kind in _PPTX_SKIPPED:
                continue  # the footer, the date and the slide number are the template's, not the slide's
            body = shape.find("p:txBody", _PPTX_NS)
            if body is None:
                continue
            x, y = _pptx_offset(shape.find("p:spPr/a:xfrm", _PPTX_NS))
            role = "title" if kind in ("title", "ctrTitle") else "body"
            bulleted = placeholder is not None and kind in (None, "body", "obj")
            lines = _pptx_paragraphs(body, bulleted and role == "body")
            if lines:
                items.append((oy + y * sy, ox + x * sx, role, lines))
        elif tag == "graphicFrame":
            x, y = _pptx_offset(shape.find("p:xfrm", _PPTX_NS))
            lines = []
            table = shape.find(".//a:tbl", _PPTX_NS)
            if table is not None:
                lines += _pptx_table(table)
            if archive is not None:
                lines += _drawing_extras(archive, rels, shape)
            if lines:
                items.append((oy + y * sy, ox + x * sx, "body", lines))
        elif tag == "pic":
            described = shape.find("p:nvPicPr/p:cNvPr", _PPTX_NS)
            alt = " ".join(
                str(
                    (described.get("descr") or described.get("title") or "")
                    if described is not None
                    else ""
                ).split()
            )
            if alt and not _DEFAULT_ALT.match(alt):
                x, y = _pptx_offset(shape.find("p:spPr/a:xfrm", _PPTX_NS))
                items.append((oy + y * sy, ox + x * sx, "picture", [figure_line(alt)]))
    return items


def _pptx_table(table: Any) -> List[str]:
    """A slide's table, a row a line; a cell merged across or down gives its value to each cell it covers."""
    rows: List[List[Optional[str]]] = []
    below: Dict[int, str] = {}
    for row in table.findall("a:tr", _PPTX_NS):
        cells: List[Optional[str]] = []
        carried: Optional[str] = None
        for index, cell in enumerate(row.findall("a:tc", _PPTX_NS)):
            text = (
                " ".join("".join(t.text or "" for t in cell.findall(".//a:t", _PPTX_NS)).split())
                or None
            )
            if cell.get("vMerge") in ("1", "true"):
                text = below.get(index)
            elif cell.get("hMerge") in ("1", "true"):
                text = carried
            if _int(cell.get("rowSpan"), 1) > 1 and text:
                below[index] = text
            carried = (
                text
                if _int(cell.get("gridSpan"), 1) > 1 or cell.get("hMerge") in ("1", "true")
                else None
            )
            cells.append(text)
        rows.append(cells)
    return _header_rows_lines(rows)


def _pptx_slide_text(
    root: Any, archive: Any = None, part: Optional[str] = None
) -> Tuple[str, List[str]]:
    """The slide's title and its text, read top to bottom and left to right, one line per paragraph or table row.

    A shape's place on the slide is its reading order, not where the file
    lists it: a deck whose boxes were added out of order read "4. Conclusion,
    1. Introduction". The footer, date and slide number placeholders are the
    template's and are left out. A chart gives its numbers, SmartArt its
    items, a picture its alt text. With no title placeholder, the first short
    line of the topmost box is the title.
    """
    rels = _ooxml_rels(archive, part) if archive is not None and part else {}
    tree = root.find("p:cSld/p:spTree", _PPTX_NS)
    if tree is None:
        return "", []
    items = _pptx_items(tree, archive, rels, (0.0, 0.0, 1.0, 1.0))
    # A row of boxes side by side is read left to right; a box lower down after them.
    items.sort(key=lambda item: (round(item[0] / 457200.0), item[1]))
    title = ""
    lines: List[str] = []
    for top, left, role, found in items:
        if role == "title" and not title:
            title = " ".join(" ".join(found).replace("- ", "").split())
        else:
            lines.extend(found)
    if (
        not title
        and lines
        and len(lines[0]) <= 100
        and not lines[0].startswith(("- ", "[Figure:", "Chart"))
    ):
        title = lines.pop(0)
    return title, lines


def _ooxml_core_title(archive: Any) -> Optional[str]:
    """The title an Office file keeps in its properties, when it is one a person would recognise."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(archive.read("docProps/core.xml"))
    except (KeyError, ET.ParseError):
        return None
    found = root.find("{http://purl.org/dc/elements/1.1/}title")
    return _useful_title(found.text if found is not None else None)


def _pptx_notes(archive: Any, slide_part: str) -> List[str]:
    """A slide's speaker notes, a line a paragraph; the notes page's picture of the slide and its number are not notes."""
    import posixpath
    import xml.etree.ElementTree as ET

    folder, _, name = slide_part.rpartition("/")
    try:
        rels = ET.fromstring(archive.read(f"{folder}/_rels/{name}.rels"))
    except (KeyError, ET.ParseError):
        return []
    target = None
    for rel in rels.findall("rel:Relationship", _PPTX_NS):
        if (
            str(rel.get("Type") or "").endswith("/notesSlide")
            and rel.get("TargetMode") != "External"
        ):
            target = posixpath.normpath(posixpath.join(folder, rel.get("Target") or ""))
            break
    if not target:
        return []
    try:
        root = ET.fromstring(archive.read(target))
    except (KeyError, ET.ParseError):
        return []
    lines: List[str] = []
    for shape in root.iter("{%s}sp" % _PPTX_NS["p"]):
        placeholder = shape.find("p:nvSpPr/p:nvPr/p:ph", _PPTX_NS)
        if placeholder is None or placeholder.get("type") != "body":
            continue
        for para in shape.findall("p:txBody/a:p", _PPTX_NS):
            words = " ".join(
                "".join(t.text or "" for t in para.findall(".//a:t", _PPTX_NS)).split()
            )
            if words:
                lines.append(words)
    return lines


def _load_pptx(path: Path, images: bool = False) -> LoadedDocument:
    """Every slide as a page with its title as a heading and its speaker
    notes after it, read from the Open XML directly, so no package is
    needed; with ``images``, each picture as a figure at the end of its
    slide. The title is the deck's own, else its first slide's."""
    import xml.etree.ElementTree as ET
    import zipfile

    parts: List[str] = []
    pages: List[Tuple[int, int]] = []
    headings: List[Tuple[int, str, int]] = []
    found: Dict[str, bytes] = {}
    offset = 0
    first_title: Optional[str] = None
    hidden_slides: List[int] = []
    with zipfile.ZipFile(str(path)) as archive:
        slide_paths = _pptx_slide_paths(archive)
        for number, part in enumerate(slide_paths, start=1):
            try:
                root = ET.fromstring(archive.read(part))
            except ET.ParseError:
                continue
            if root.get("show") in ("0", "false"):
                hidden_slides.append(number)
                continue
            title, lines = _pptx_slide_text(root, archive, part)
            if title and first_title is None:
                first_title = title
            heading = f"Slide {number}" + (f": {title}" if title else "")
            block = "# " + heading + ("\n\n" + "\n".join(lines) if lines else "")
            notes = _pptx_notes(archive, part)
            if notes:
                # What the speaker says over the slide, which is often what it means.
                block += "\n\nSpeaker notes: " + "\n".join(notes)
            if images:
                for index, (suffix, data) in enumerate(
                    _ooxml_pictures(archive, _ooxml_rels(archive, part), root), start=1
                ):
                    name = f"s{number}-fig{index}{suffix}"
                    found[name] = data
                    block += f"\n\n[Figure: {name}]"
            if parts:
                offset += 2
            pages.append((offset, number))
            headings.append((offset, heading, 1))
            parts.append(block)
            offset += len(block)
        deck_title = _ooxml_core_title(archive) or _useful_title(first_title)
    text = "\n\n".join(parts)
    return LoadedDocument(
        text=text,
        pages=pages,
        headings=headings,
        metadata={
            "pages": len(pages),
            **({"title": deck_title} if deck_title else {}),
            **({"slides_hidden": hidden_slides} if hidden_slides else {}),
        },
        figures=_marked_figures(text, found),
        images=found,
    )


# ============================================================================
# HTML
# ============================================================================
#
# INPUT   a web page, as bytes or as markup
# OUTPUT  the page's own text: headings as Markdown lines, lists with their
#         markers, tables as rows, code as fenced blocks, and the page's title
#
# A page is mostly not its content. The menus, the banner, the footer, the
# cookie notice and the site index come before and after it, and on a large
# site they are most of the markup. So the reader keeps to <main> when the
# page has one, or to its one <article>, and everywhere leaves out what a
# page marks as around its content rather than in it: navigation, banners,
# footers, dialogs, form controls, and anything hidden. A list that is all
# links, outside the content, is a menu whatever its markup says.


#: A page's own <meta charset>, looked for before anything is decoded.
_META_CHARSET = re.compile(
    rb"""<meta[^>]{0,200}?charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE
)


def _decode_html(data: bytes) -> str:
    """A page's markup as text, in the encoding it says it is in.

    A byte order mark first, then the page's own <meta charset>, then UTF-8,
    and Windows-1252 when the bytes are not UTF-8, which is what a browser
    falls back to for a Western page that says nothing. A page that says
    Latin-1 or ASCII is read as Windows-1252, as browsers do. One that says
    UTF-16 in a <meta> is not UTF-16, or the <meta> could not have been read.
    """
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8) :].decode("utf-8", errors="replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    said = _META_CHARSET.search(data[:8192])
    if said:
        try:
            name = codecs.lookup(said.group(1).decode("ascii", "ignore")).name
        except LookupError:
            name = ""
        if name in ("latin-1", "iso8859-1", "ascii", "cp1252"):
            return data.decode("cp1252", errors="replace")
        if name and name != "utf-8" and not name.startswith(("utf-16", "utf-32")):
            return data.decode(name, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


#: Elements with no end tag: never on the stack of open elements.
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

#: Elements that hold no text a reader wants: nothing inside them is read.
_NOT_TEXT = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "head",
        "title",
        "svg",
        "math",
        "canvas",
        "iframe",
        "object",
        "embed",
        "audio",
        "video",
        "map",
    }
)

#: Elements that are around a page's content and not in it.
_AROUND = frozenset({"nav", "dialog", "menu", "button", "select", "textarea", "datalist", "option"})

#: Roles that say the same of any element.
_AROUND_ROLES = frozenset(
    {
        "navigation",
        "banner",
        "contentinfo",
        "search",
        "dialog",
        "alertdialog",
        "menu",
        "menubar",
        "toolbar",
        "tooltip",
    }
)

#: Words in a class or an id that name something around the content, matched whole.
_AROUND_WORDS = frozenset(
    {
        "cookie",
        "cookies",
        "consent",
        "gdpr",
        "breadcrumb",
        "breadcrumbs",
        "skip",
        "skiplink",
        "modal",
        "popup",
    }
)

#: Class names that hide an element, unless another class shows it again at some width.
_HIDING_CLASSES = frozenset({"hidden", "is-hidden", "u-hidden"})
_SHOWING_CLASS = re.compile(
    r"(?:^|[:-])(?:block|flex|grid|inline|inline-block|inline-flex|table|contents)$"
)

#: Class names for text only a screen reader is given: "opens in a new window".
_READER_ONLY = frozenset(
    {"sr-only", "visually-hidden", "visuallyhidden", "screen-reader-text", "screenreader-only"}
)

#: Elements a header, a footer or an aside can be part of the content inside.
_SECTIONING = frozenset({"article", "main", "section"})

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

#: What closes an open <p> by beginning, as a browser reads it.
_CLOSES_P = frozenset(
    {
        "p",
        "div",
        "ul",
        "ol",
        "dl",
        "table",
        "pre",
        "blockquote",
        "section",
        "article",
        "header",
        "footer",
        "aside",
        "nav",
        "main",
        "figure",
        "form",
        "hr",
        "address",
        "fieldset",
        "details",
        "menu",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)

#: A line the reader wrote as a list item: its indent, its marker, its words.
_LIST_LINE = re.compile(r"^( *)(-|\d+\.)(?:\s+(.*))?$")

#: What starts a line that goes on an open list item, once for each list it is in.
_GOES_ON = "\x01"

#: Under this many characters a <main> may be only a shell a script fills in.
_SHELL = 200


def _named_around(tag: str, given: Mapping[str, Optional[str]]) -> bool:
    """Whether an element's class or id says it is around the content, or hidden."""
    if tag in ("main", "article"):
        return False
    names = " ".join(v for v in (given.get("class"), given.get("id")) if v).lower()
    if not names:
        return False
    whole = set(names.split())
    if whole & _READER_ONLY and not any("not-sr-only" in n for n in whole):
        return True
    if whole & _HIDING_CLASSES and not any(
        _SHOWING_CLASS.search(n) for n in whole - _HIDING_CLASSES
    ):
        return True
    return bool(set(re.split(r"[^a-z0-9]+", names)) & _AROUND_WORDS)


class _HTMLText(html.parser.HTMLParser):
    """A page's own text: headings as Markdown lines, list items with their markers, tables as rows, code fenced.

    ``scope`` is "main" or "article" to read only inside that element, or
    None to read the whole page. Either way what is around the content is
    left out, and ``left_out`` counts the pieces.
    """

    _block = {
        "p",
        "div",
        "tr",
        "section",
        "article",
        "blockquote",
        "header",
        "footer",
        "main",
        "aside",
        "nav",
        "dd",
        "dt",
        "hr",
        "address",
        "form",
        "fieldset",
        "dl",
        "details",
        "summary",
    }

    def __init__(self, scope: Optional[str] = None) -> None:
        super().__init__()
        self.scope = scope
        self.parts: List[str] = []
        self.headings: List[Tuple[int, str, int]] = []
        self.title: List[str] = []
        self.pre_blocks: List[str] = []
        self.left_out = 0
        self._in_title = False
        self._heading_level = 0
        self._heading_buffer: List[str] = []
        self._length = 0
        self._in_figure = False
        self._figure_alt: Optional[str] = None
        self._in_caption = False
        self._caption: List[str] = []
        # Every element open now, the innermost last, and how many of each, so
        # asking whether one is open does not read the whole stack.
        self._open: List[str] = []
        self._counts: Dict[str, int] = {}
        # Where a left-out element began, and where the scope element did.
        self._hidden_at: Optional[int] = None
        self._scope_at: Optional[int] = None
        # A <pre> being read, word for word.
        self._pre: Optional[List[str]] = None
        # Just after a list marker, before the item has said anything.
        self._li_fresh = False
        # Each open list: numbered, its count, where it began, and how much of it is links.
        self._lists: List[Dict[str, Any]] = []
        # Each open table, the innermost last: its rows, the cell being read, its caption.
        self._tables: List[Dict[str, Any]] = []

    def _emit(self, text: str) -> None:
        self.parts.append(text)
        self._length += len(text)
        if text.strip():
            self._li_fresh = False

    def _break(self) -> str:
        """Between two blocks: a blank line, or inside a list item a new line that still belongs to it."""
        return "\n" + _GOES_ON * len(self._lists) if self._lists else "\n\n"

    def _quiet(self) -> bool:
        return self._hidden_at is not None or (self.scope is not None and self._scope_at is None)

    def _within(self, *tags: str) -> bool:
        return any(self._counts.get(tag) for tag in tags)

    def _push(self, tag: str) -> None:
        self._open.append(tag)
        self._counts[tag] = self._counts.get(tag, 0) + 1

    def _pop_to(self, at: int) -> None:
        for tag in self._open[at:]:
            self._counts[tag] -= 1
        del self._open[at:]

    def _close_implied(self, tag: str) -> None:
        """Close what beginning ``tag`` closes without saying so, as a browser does.

        An old page writes <p>one<p>two, <li>a<li>b and <td>1<td>2 without an
        end tag. Left open, each paragraph sits inside the one before, the
        stack grows with the page, and a cell's words are lost to the next.
        """
        top = self._open[-1] if self._open else ""
        if top == "p" and tag in _CLOSES_P:
            self.handle_endtag("p")
        elif top == "li" and tag == "li":
            self.handle_endtag("li")
        elif top in ("dt", "dd") and tag in ("dt", "dd"):
            self.handle_endtag(top)
        elif top in ("td", "th") and tag in ("td", "th", "tr"):
            self.handle_endtag(top)
            if tag == "tr" and self._open and self._open[-1] == "tr":
                self.handle_endtag("tr")
        elif top == "tr" and tag == "tr":
            self.handle_endtag("tr")

    def _left_out(self, tag: str, given: Mapping[str, Optional[str]]) -> bool:
        """Whether an element is around the content, or hidden, and so not read at all."""
        if tag in ("html", "body"):
            return False
        if tag in _NOT_TEXT or tag in _AROUND:
            return True
        if "hidden" in given and (given.get("hidden") or "").strip().lower() != "until-found":
            return True
        if (given.get("aria-hidden") or "").strip().lower() == "true" or (
            given.get("aria-modal") or ""
        ).strip().lower() == "true":
            return True
        style = (given.get("style") or "").lower().replace(" ", "")
        if "display:none" in style or "visibility:hidden" in style:
            return True
        role = (given.get("role") or "").strip().lower()
        if role in _AROUND_ROLES:
            return True
        if tag in ("header", "footer") and not self._within(*_SECTIONING):
            return True
        if (tag == "aside" or role == "complementary") and not self._within("article"):
            return True
        return _named_around(tag, given)

    @staticmethod
    def _new_table() -> Dict[str, Any]:
        return {"rows": [], "cell": None, "span": (1, 1, False), "caption": [], "in_caption": False}

    def _table_start(self, tag: str, attrs: Any) -> None:
        table = self._tables[-1]
        if tag == "table":
            self._tables.append(self._new_table())
        elif tag == "tr":
            table["rows"].append([])
        elif tag in ("td", "th"):
            if not table["rows"]:
                table["rows"].append([])
            given = dict(attrs)
            table["cell"] = []
            table["span"] = (
                max(_int(given.get("colspan"), 1), 1),
                max(_int(given.get("rowspan"), 1), 1),
                tag == "th",
            )
        elif tag == "caption":
            table["in_caption"] = True
        elif table["cell"] is not None:
            if tag == "img":
                alt = dict(attrs).get("alt") or ""
                table["cell"].append(f" {alt} " if alt else " ")
            elif tag in self._block or tag in ("li", "br"):
                table["cell"].append(" ")

    def _table_end(self, tag: str) -> None:
        table = self._tables[-1]
        if tag in ("td", "th") and table["cell"] is not None:
            words = " ".join("".join(table["cell"]).split())
            table["rows"][-1].append((words or None, *table["span"]))
            table["cell"] = None
        elif tag == "caption":
            table["in_caption"] = False
        elif tag == "table":
            if table["cell"] is not None:
                # </table> with a cell still open: the cell ends with it, and keeps its words.
                self._table_end("td")
            self._tables.pop()
            rows = [row for row in _html_rows(table["rows"]) if any(row)]
            caption = " ".join("".join(table["caption"]).split())
            if self._tables:
                # A table inside a cell is read as the words of its cells, in the cell.
                outer = self._tables[-1]
                if outer["cell"] is not None:
                    words = " ".join(value for row in rows for value in row if value)
                    outer["cell"].append(" " + " ".join(w for w in (caption, words) if w) + " ")
                return
            lines = ([caption] if caption else []) + rows_to_lines(rows)
            if lines:
                self._emit("\n\n" + "\n".join(lines) + "\n\n")

    # -- where an element begins

    def handle_starttag(self, tag, attrs):
        if self._open:
            self._close_implied(tag)
        if tag == "title" and not self._within("svg", "math"):
            self._in_title = True
        given = dict(attrs)
        void = tag in _VOID
        if self._hidden_at is not None:
            if not void:
                self._push(tag)
            return
        if self._left_out(tag, given):
            self.left_out += 1
            if not void:
                self._hidden_at = len(self._open)
                self._push(tag)
            return
        if self.scope is not None and self._scope_at is None:
            if tag == self.scope or (
                self.scope == "main" and (given.get("role") or "").strip().lower() == "main"
            ):
                self._scope_at = len(self._open)
            else:
                if not void:
                    self._push(tag)
                return
        if not void:
            self._push(tag)
        if self._pre is not None:
            if tag == "br":
                self._pre.append("\n")
            return
        self._start(tag, given, attrs)

    def _start(self, tag: str, given: Mapping[str, Optional[str]], attrs: Any) -> None:
        if self._tables:
            self._table_start(tag, attrs)
            return
        if tag == "table":
            self._tables.append(self._new_table())
        elif tag in _HEADING_TAGS:
            self._heading_level = int(tag[1])
            self._heading_buffer = []
        elif tag in ("ul", "ol"):
            if not self._lists:
                self._emit("\n\n")
            start = _int(given.get("start"), 1)
            self._lists.append(
                {
                    "numbered": tag == "ol",
                    "count": start - 1,
                    "at": len(self.parts),
                    "words": 0,
                    "links": 0,
                    "items": 0,
                }
            )
        elif tag == "li":
            marker = "- "
            if self._lists:
                top = self._lists[-1]
                top["items"] += 1
                if top["numbered"]:
                    top["count"] += 1
                    marker = f"{top['count']}. "
            self._emit("\n" + "  " * max(len(self._lists) - 1, 0) + marker)
            self._li_fresh = True
        elif tag == "figure":
            self._in_figure = True
            self._figure_alt = None
            self._caption = []
        elif tag == "figcaption":
            self._in_caption = True
        elif tag == "img":
            self._image(given)
        elif tag == "br":
            if self._heading_level:
                self._heading_buffer.append(" ")
            else:
                self._emit("\n")
        elif tag == "pre":
            self._emit("\n\n")
            self._pre = []
        elif tag in self._block and not self._li_fresh:
            self._emit(self._break())

    def _image(self, given: Mapping[str, Optional[str]]) -> None:
        """A picture as a figure line, unless the page says it is only decoration."""
        alt = given.get("alt")
        src = given.get("src") or ""
        if src.startswith("data:"):
            src = ""
        if (given.get("width") or "").strip() in ("0", "1") and (
            given.get("height") or ""
        ).strip() in ("0", "1"):
            return  # a counter, not a picture
        if self._in_figure:
            self._figure_alt = (alt or "").strip() or src or self._figure_alt
        elif alt is not None and not alt.strip():
            return  # alt="" is how a page says a picture is decoration
        elif (alt or "").strip() or src:
            self._emit("\n\n" + figure_line(alt or "", src) + "\n\n")

    # -- where an element ends

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in _VOID or not self._counts.get(tag):
            return
        was_quiet = self._quiet()
        at = len(self._open) - 1 - self._open[::-1].index(tag)
        self._pop_to(at)
        if self._hidden_at is not None:
            if len(self._open) <= self._hidden_at:
                self._hidden_at = None
            return
        if was_quiet:
            return
        if self._pre is not None:
            if self._within("pre"):
                return
            self._finish_pre()
            if tag == "pre":
                self._leave_scope()
                return
        self._end(tag)
        self._close_orphans()
        self._leave_scope()

    def _leave_scope(self) -> None:
        if self._scope_at is not None and len(self._open) <= self._scope_at:
            self._scope_at = None

    def _end(self, tag: str) -> None:
        if self._tables:
            self._table_end(tag)
            return
        if tag in _HEADING_TAGS and self._heading_level:
            heading = " ".join("".join(self._heading_buffer).split())
            if heading:
                self._emit("\n\n")
                self.headings.append((self._length, heading, self._heading_level))
                self._emit("#" * self._heading_level + " " + heading + "\n\n")
            self._heading_level = 0
        elif tag in ("ul", "ol"):
            self._end_list()
        elif tag == "figcaption":
            self._in_caption = False
        elif tag == "figure":
            caption = " ".join("".join(self._caption).split())
            self._emit(
                "\n\n" + figure_line(caption or (self._figure_alt or ""), self._figure_alt) + "\n\n"
            )
            self._in_figure = False
        elif tag in self._block and not self._li_fresh:
            self._emit(self._break())

    def _end_list(self) -> None:
        """Close the innermost list, and take it back out when it was a menu of links outside the content."""
        if not self._lists:
            return
        top = self._lists.pop()
        menu = top["items"] >= 3 and top["words"] > 0 and top["links"] >= 0.9 * top["words"]
        if menu and self._scope_at is None and not self._within("main", "article"):
            del self.parts[top["at"] :]
            self._length = sum(len(part) for part in self.parts)
            self.headings = [h for h in self.headings if h[0] < self._length]
            self.left_out += 1
            for outer in self._lists:
                outer["words"] -= top["words"]
                outer["links"] -= top["links"]
        if not self._lists:
            self._emit("\n\n")
        self._li_fresh = False

    def _close_orphans(self) -> None:
        """Finish what an end tag closed without naming it, as a browser would."""
        while len(self._tables) > self._counts.get("table", 0):
            if self._tables[-1]["cell"] is not None:
                self._table_end("td")
            self._table_end("table")
        if self._heading_level and not self._within(*_HEADING_TAGS):
            self._end(f"h{self._heading_level}")
        while len(self._lists) > self._counts.get("ul", 0) + self._counts.get("ol", 0):
            self._end_list()
        if self._in_caption and not self._within("figcaption"):
            self._in_caption = False
        if self._in_figure and not self._within("figure"):
            self._end("figure")

    def _finish_pre(self) -> None:
        code = "".join(self._pre or []).strip("\n")
        self._pre = None
        if code.strip():
            self.pre_blocks.append(code)
            self._emit(f"\x00{len(self.pre_blocks) - 1}\x00\n\n")

    # -- the words

    def handle_data(self, data):
        if self._in_title:
            self.title.append(data)
            return
        if self._quiet():
            return
        if self._pre is not None:
            self._pre.append(data)
            return
        if self._tables:
            table = self._tables[-1]
            if table["in_caption"]:
                table["caption"].append(data)
            elif table["cell"] is not None:
                table["cell"].append(data)
            return
        if self._in_caption:
            self._caption.append(data)
        elif self._heading_level:
            self._heading_buffer.append(data)
        elif self._in_figure:
            return
        else:
            text = re.sub(r"\s+", " ", data).replace("\x00", "").replace(_GOES_ON, "")
            if not text.strip():
                if not self._li_fresh:
                    self._emit(" ")
                return
            self._emit(text)
            size, linked = len(text.strip()), self._within("a")
            for entry in self._lists:
                entry["words"] += size
                if linked:
                    entry["links"] += size


def _html_lines(raw: str, blocks: Sequence[str]) -> str:
    """The reader's output made tidy: one space between words, no empty items, no runs of blank lines, code put back."""
    lines: List[str] = []
    # Whether the last line written is a list item, or more of one.
    in_item = False
    for line in raw.split("\n"):
        depth = len(line) - len(line.lstrip(_GOES_ON))
        if depth:
            # More of an open list item, under its marker; with no words it is nothing, not a blank line.
            words = " ".join(line[depth:].split())
            if not words:
                continue
            if in_item:
                lines.append("  " * depth + words)
            else:
                # The item began with a heading, so there is no marker to sit under: a paragraph.
                if lines and lines[-1]:
                    lines.append("")
                lines.append(words)
            continue
        line = line.rstrip()
        item = _LIST_LINE.match(line)
        if item:
            words = " ".join((item.group(3) or "").split())
            lines.append(f"{item.group(1)}{item.group(2)} {words}" if words else "")
            in_item = bool(words)
        else:
            lines.append(" ".join(line.split()))
            in_item = False
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    for n, code in enumerate(blocks):
        fence = "```"
        while fence in code:
            fence += "`"
        text = text.replace(f"\x00{n}\x00", f"{fence}\n{code}\n{fence}")
    return text


def _read_html(markup: str, scope: Optional[str]) -> Tuple["_HTMLText", str]:
    parser = _HTMLText(scope=scope)
    parser.feed(markup)
    parser.close()
    if parser._pre is not None:
        parser._finish_pre()
    return parser, _html_lines("".join(parser.parts), parser.pre_blocks)


def _landmark(markup: str) -> Optional[str]:
    """The element a page keeps its content in: "main", its one "article", or None."""
    if re.search(r"<main[\s>/]|\brole\s*=\s*[\"']?main\b", markup, re.IGNORECASE):
        return "main"
    if len(re.findall(r"<article[\s>/]", markup, re.IGNORECASE)) == 1:
        return "article"
    return None


def _load_html(markup: str) -> LoadedDocument:
    """A web page's own text, its tables as rows and its lists with their markers.

    Only what is inside <main>, or the page's one <article>, when it has
    one. A <main> that holds almost nothing is a shell a script fills in, so
    then the whole page is read, still without what is around its content.
    The title is the page's ``<title>``, else its first top-level heading.
    """
    scope = _landmark(markup)
    parser, text = _read_html(markup, scope)
    if scope is not None and len(text) < _SHELL:
        whole_parser, whole = _read_html(markup, None)
        if len(whole) >= 5 * max(len(text), 1):
            parser, text = whole_parser, whole
    headings = _markdown_headings(text)
    metadata: Dict[str, Any] = {}
    title = _useful_title("".join(parser.title)) or next(
        (h for _o, h, level in headings if level == 1), None
    )
    if title:
        metadata["title"] = title
    return LoadedDocument(text=text, headings=headings, metadata=metadata)


# ============================================================================
# CHUNKING
# ============================================================================
#
# INPUT   a document, a strategy, a size and an overlap
# OUTPUT  chunks with offsets, page and heading: recursive, by sentence,
#         semantic, by Markdown section, fixed, or where a model says a topic
#         starts
#
# A chunk that cuts a sentence in half embeds as noise; a section returned
# whole reads as an answer.


@dataclass
class Chunk:
    """A piece of a document that knows where it came from."""

    text: str
    index: int
    start: int
    end: int
    page: Optional[int] = None
    heading: Optional[str] = None
    # The page the chunk ends on, when that is a different page.
    page_end: Optional[int] = None
    # The caption, when the chunk is a figure and nothing else.
    figure: Optional[str] = None
    # The image a figure came from, as its document named it.
    figure_src: Optional[str] = None
    # Every heading the chunk sits under, outermost first. Not metadata:
    # it is what embed_heading puts in front of the text it embeds.
    heading_path: Optional[List[str]] = None

    def metadata(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "_vx_chunk": self.index,
            "_vx_start": self.start,
            "_vx_end": self.end,
        }
        if self.page is not None:
            out["page"] = self.page
        if self.heading is not None:
            out["heading"] = self.heading
        if self.page_end is not None and self.page_end != self.page:
            out["page_end"] = self.page_end
        if self.figure is not None:
            out["figure"] = self.figure
        if self.figure_src:
            out["figure_src"] = self.figure_src
        return out


_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[A-Z0-9\"'(\[])|\n{2,}")


def split_sentences(text: str) -> List[Tuple[int, int]]:
    """(start, end) offsets of sentences. Cheap, regex-based, good enough for
    chunk boundaries; not a linguistic tokenizer."""
    spans: List[Tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        end = m.start()
        if end > start and text[start:end].strip():
            spans.append((start, end))
        start = m.end()
    if start < len(text) and text[start:].strip():
        spans.append((start, len(text)))
    return spans


def chunk(
    text: Union[str, LoadedDocument],
    strategy: str = "recursive",
    size: int = 1000,
    overlap: int = 200,
    *,
    embed: Optional[Callable[[List[str]], np.ndarray]] = None,
    threshold: float = 0.1,
    cut_with: Optional[Callable[[str, List[Tuple[int, int]]], Sequence[int]]] = None,
) -> List[Chunk]:
    """Split text into chunks by strategy, each with offsets, page and heading.

    Strategies:

    * ``recursive``: split on paragraphs, then lines, then sentences, then
      words, merging pieces up to ``size`` characters with ``overlap``
      characters carried between neighbours. The general-purpose default.
    * ``sentence``: whole sentences packed up to ``size``, overlapping by the
      last sentence of the previous chunk. Never cuts mid-sentence.
    * ``semantic``: sentences grouped where they are about the same thing.
      Needs ``embed``. Every sentence is embedded, and the ``threshold``
      fraction of boundaries with the lowest similarity between neighbours
      start a new chunk (0.1 splits at the ten percent sharpest topic
      changes); ``size`` is the hard cap. Costs one embedding per sentence.
    * ``markdown``: every heading starts a new chunk and the heading text
      rides along on each chunk under it; long sections fall back to
      ``recursive`` within the section.
    * ``fixed``: every ``size`` characters, ``overlap`` carried over, with
      no regard for paragraphs or sentences; only a word is never cut, the
      window ending at the space before it.
    * ``llm``: a model reads the paragraphs in order and says where a new
      topic starts, and each topic is a chunk, split to ``size`` where it
      runs longer. Needs ``cut_with``, a callable from the text and its
      paragraphs' ``(start, end)`` offsets to the paragraph numbers that
      start a chunk; :func:`vectrixdb.chunk_models.llm_cutter` makes one
      from a chat model.

    Pass a :class:`LoadedDocument` and every chunk carries its page and
    heading; pass a string and only offsets are known.
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"strategy must be one of {STRATEGIES}, got {strategy!r}")
    if strategy == "llm" and cut_with is None:
        raise ValueError(
            "llm chunking needs cut_with=, a model that says where each topic starts: see vectrixdb.chunk_models.llm_cutter"
        )
    if size <= 0:
        raise ValueError("size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must be non-negative and smaller than size")

    if isinstance(text, LoadedDocument):
        doc = text
    else:
        doc = LoadedDocument(text=text, headings=_markdown_headings(text))
    if not doc.text.strip():
        return []
    if strategy == "semantic" and embed is None:
        raise ValueError("semantic chunking needs embed=, a callable from texts to vectors")

    # A figure is one unit. Its line is cut out before the strategy runs
    # and put back as a span of its own, so no strategy ever splits a
    # caption or glues it to the paragraph before it.
    spans: List[Tuple[int, int]] = []
    cursor = 0
    figures = [(m.start(), m.end()) for m in _FIGURE_IN_TEXT.finditer(doc.text)]
    for fs, fe in figures + [(len(doc.text), len(doc.text))]:
        if fs > cursor:
            spans.extend(
                _strategy_spans(
                    doc, cursor, fs, strategy, size, overlap, embed, threshold, cut_with
                )
            )
        if fe > fs:
            spans.append((fs, fe))
        cursor = fe

    chunks: List[Chunk] = []
    for i, (start, end) in enumerate(spans):
        piece = doc.text[start:end]
        lead = len(piece) - len(piece.lstrip())
        trail = len(piece) - len(piece.rstrip())
        s, e = start + lead, end - trail
        if e <= s:
            continue
        body = doc.text[s:e]
        fig = FIGURE_LINE.match(body.strip().split("\n", 1)[0])
        info = doc.figure_at(s) if fig else None
        chunks.append(
            Chunk(
                text=body,
                index=len(chunks),
                start=s,
                end=e,
                page=doc.page_at(s),
                heading=doc.heading_at(s) if doc.headings else None,
                page_end=doc.page_at(e - 1) if doc.pages else None,
                figure=fig.group(1) if fig else None,
                figure_src=(info or {}).get("src"),
                heading_path=doc.heading_path_at(s) if doc.headings else None,
            )
        )
    return chunks


_FIGURE_IN_TEXT = re.compile(r"^\[Figure: .+\]$(?:\n(?![ \t]*$).+)*", re.MULTILINE)


def _strategy_spans(
    doc: LoadedDocument,
    a: int,
    b: int,
    strategy: str,
    size: int,
    overlap: int,
    embed: Optional[Callable[[List[str]], np.ndarray]],
    threshold: float,
    cut_with: Optional[Callable[[str, List[Tuple[int, int]]], Sequence[int]]] = None,
) -> List[Tuple[int, int]]:
    """The strategy run over ``doc.text[a:b]``, with spans in the document's offsets."""
    sub = doc.text[a:b]
    if not sub.strip():
        return []
    if strategy == "recursive":
        local = _recursive_spans(sub, size, overlap)
    elif strategy == "sentence":
        local = _sentence_spans(sub, size, overlap)
    elif strategy == "semantic":
        assert embed is not None
        local = _semantic_spans(sub, size, embed, threshold)
    elif strategy == "fixed":
        local = _fixed_spans(sub, size, overlap)
    elif strategy == "llm":
        assert cut_with is not None
        local = _llm_spans(sub, size, cut_with)
    else:
        headings = [(o - a, h, level) for o, h, level in doc.headings if a <= o < b]
        local = _markdown_spans(LoadedDocument(text=sub, headings=headings), size, overlap)
    return [(s + a, e + a) for s, e in local]


def _recursive_spans(text: str, size: int, overlap: int) -> List[Tuple[int, int]]:
    """Offsets of the same chunks ``document_index.chunk_text`` produces."""
    from .core.document_index import chunk_text

    pieces = chunk_text(text, chunk_size=size, chunk_overlap=overlap)
    # Searching for each piece is ambiguous on repeated words: an earlier
    # repeat inside the previous chunk matches as well, and taking it loses
    # the text the piece really covers. Splitting the same way with offsets
    # is exact; the search remains for a chunk_text this no longer mirrors.
    exact = _chunk_text_spans(text, size, overlap)
    if exact is not None and [text[a:b] for a, b in exact] == pieces:
        return [(a, b) for a, b in exact if b > a]

    spans: List[Tuple[int, int]] = []
    cursor = 0
    for piece in pieces:
        if not piece:
            continue
        # Search back by the overlap so a carried tail is found where it was
        # carried from, but never at or before the previous chunk's start:
        # on repetitive text ``find`` would return that same occurrence again
        # and two chunks would claim one offset.
        floor = spans[-1][0] + 1 if spans else 0
        found = text.find(piece, max(floor, cursor - overlap - 1))
        if found < 0:
            found = text.find(piece, floor)
        if found < 0:  # pragma: no cover - chunk_text only returns substrings
            continue
        spans.append((found, found + len(piece)))
        cursor = found + len(piece)
    return spans


def _chunk_text_spans(text: str, size: int, overlap: int) -> Optional[List[Tuple[int, int]]]:
    """``document_index.chunk_text`` step for step, keeping offsets instead of strings."""
    if not text:
        return []
    for sep in ("\n\n", "\n", ". ", " ", ""):
        splits: List[Tuple[int, int]] = []
        if sep:
            pos = 0
            for part in text.split(sep):
                splits.append((pos, pos + len(part)))
                pos += len(part) + len(sep)
        else:
            splits = [(i, i + 1) for i in range(len(text))]
        if all(b - a <= size for a, b in splits):
            break
    else:
        if size - overlap <= 0:
            return None
        return [(i, min(i + size, len(text))) for i in range(0, len(text), size - overlap)]

    # Splits are contiguous with one separator between them, so a run of
    # them joined is the text from the first's start to the last's end.
    chunks: List[Tuple[int, int]] = []
    current: List[Tuple[int, int]] = []
    for split in splits:
        if current and split[1] - current[0][0] > size:
            chunks.append((current[0][0], current[-1][1]))
            carried: List[Tuple[int, int]] = []
            carried_size = 0
            for prev in reversed(current):
                if carried_size + (prev[1] - prev[0]) > overlap:
                    break
                carried.insert(0, prev)
                carried_size += prev[1] - prev[0] + len(sep)
            current = carried
            if current and split[1] - current[0][0] > size:
                current = []
        current.append(split)
    if current:
        chunks.append((current[0][0], current[-1][1]))
    return chunks


def _pack(
    spans: List[Tuple[int, int]], size: int, overlap_units: int, text: str
) -> List[Tuple[int, int]]:
    """Pack unit spans into chunks up to ``size`` characters, carrying the
    last ``overlap_units`` units into the next chunk."""
    out: List[Tuple[int, int]] = []
    current: List[Tuple[int, int]] = []

    def length(units: List[Tuple[int, int]]) -> int:
        return units[-1][1] - units[0][0] if units else 0

    for span in spans:
        if span[1] - span[0] > size:
            # A single unit longer than size: flush and split it by words.
            if current:
                out.append((current[0][0], current[-1][1]))
                current = []
            out.extend(_recursive_spans_offset(text, span, size))
            continue
        if current and length(current + [span]) > size:
            out.append((current[0][0], current[-1][1]))
            current = current[-overlap_units:] if overlap_units else []
            if current and length(current + [span]) > size:
                current = []
        current.append(span)
    if current:
        out.append((current[0][0], current[-1][1]))
    return out


def _recursive_spans_offset(text: str, span: Tuple[int, int], size: int) -> List[Tuple[int, int]]:
    start, end = span
    return [
        (start + s, start + e)
        for s, e in _recursive_spans(text[start:end], size, min(size // 5, 200))
    ]


def _sentence_spans(text: str, size: int, overlap: int) -> List[Tuple[int, int]]:
    sentences = split_sentences(text)
    if not sentences:
        return [(0, len(text))]
    # Overlap in sentences: as many trailing sentences as fit in ``overlap`` chars, at least one when asked.
    avg = max(1, sum(e - s for s, e in sentences) // len(sentences))
    overlap_units = max(1, overlap // avg) if overlap else 0
    return _pack(sentences, size, overlap_units, text)


def _semantic_spans(
    text: str, size: int, embed: Callable[[List[str]], np.ndarray], threshold: float
) -> List[Tuple[int, int]]:
    sentences = split_sentences(text)
    if len(sentences) < 3:
        return _pack(sentences, size, 0, text) if sentences else [(0, len(text))]
    vectors = np.asarray(embed([text[s:e] for s, e in sentences]), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9
    # Similarity between each sentence and the next; a low value is a topic shift.
    sims = np.sum(vectors[:-1] * vectors[1:], axis=1)
    cut = float(np.quantile(sims, threshold))
    out: List[Tuple[int, int]] = []
    current_start, current_end = sentences[0]
    for i in range(1, len(sentences)):
        s, e = sentences[i]
        shift = sims[i - 1] <= cut
        too_long = e - current_start > size
        if shift or too_long:
            out.append((current_start, current_end))
            current_start = s
        current_end = e
    out.append((current_start, current_end))
    # Sentences longer than size on their own are split by words.
    final: List[Tuple[int, int]] = []
    for span in out:
        if span[1] - span[0] > size:
            final.extend(_recursive_spans_offset(text, span, size))
        else:
            final.append(span)
    return final


def _fixed_spans(text: str, size: int, overlap: int) -> List[Tuple[int, int]]:
    """Every ``size`` characters, ``overlap`` carried over: the cut goes back to the last space so no word is split, unless a word fills the window."""
    out: List[Tuple[int, int]] = []
    start, n = 0, len(text)
    while start < n:
        while start < n and text[start].isspace():
            start += 1
        if start >= n:
            break
        end = min(n, start + size)
        if end < n and not text[end].isspace():
            space = max(text.rfind(" ", start, end), text.rfind("\n", start, end))
            if space > start:
                end = space
        out.append((start, end))
        if end >= n:
            break
        start = max(start + 1, end - overlap)
        # Carried over from a word boundary, not from the middle of one.
        if overlap and start > 0 and not text[start - 1].isspace():
            nxt = min(
                (i for i in (text.find(" ", start, end), text.find("\n", start, end)) if i >= 0),
                default=-1,
            )
            if nxt >= 0:
                start = nxt + 1
    return out


def _paragraphs(text: str, size: int) -> List[Tuple[int, int]]:
    """The paragraphs of a text, blank lines between them; one longer than ``size`` in its sentences."""
    units: List[Tuple[int, int]] = []
    for m in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\s*\Z)", text, re.DOTALL):
        s, e = m.start(), m.end()
        if e - s > size:
            units.extend((s + a, s + b) for a, b in split_sentences(text[s:e]))
        else:
            units.append((s, e))
    return units


def _llm_spans(
    text: str, size: int, cut_with: Callable[[str, List[Tuple[int, int]]], Sequence[int]]
) -> List[Tuple[int, int]]:
    """Paragraphs grouped where a model says a new topic starts; a group longer than ``size`` is packed to it."""
    units = _paragraphs(text, size)
    if not units:
        return [(0, len(text))]
    starts = {0} | {int(i) for i in cut_with(text, units) if 0 < int(i) < len(units)}
    out: List[Tuple[int, int]] = []
    group: List[Tuple[int, int]] = []
    for i, unit in enumerate(units):
        if i in starts and group:
            out.extend(_pack(group, size, 0, text))
            group = []
        group.append(unit)
    if group:
        out.extend(_pack(group, size, 0, text))
    return out


def _markdown_spans(doc: LoadedDocument, size: int, overlap: int) -> List[Tuple[int, int]]:
    text = doc.text
    headings = doc.headings or _markdown_headings(text)
    bounds = [0] + [h[0] for h in headings if h[0] > 0] + [len(text)]
    out: List[Tuple[int, int]] = []
    for a, b in zip(bounds, bounds[1:]):
        section = text[a:b]
        if not section.strip():
            continue
        # Drop the heading line itself from the body; it rides on metadata.
        m = _HEADING.match(section)
        body_start = a + (m.end() if m else 0)
        if body_start >= b or not text[body_start:b].strip():
            continue
        if b - body_start <= size:
            out.append((body_start, b))
        else:
            out.extend(_recursive_spans_offset(text, (body_start, b), size))
    # A document of headings and nothing else, an outline say, had every section
    # dropped and was indexed as nothing at all. Its headings are its text.
    if not out and text.strip():
        return (
            [(0, len(text))]
            if len(text) <= size
            else _recursive_spans_offset(text, (0, len(text)), size)
        )
    return out


# ============================================================================
# NEAR-DUPLICATES: MinHash over word shingles
# ============================================================================
#
# INPUT   texts
# OUTPUT  signatures with LSH bands, saved as one JSON file; the near-
#         duplicates of a new text
#
# The same paragraph indexed twice pushes a better one off the page.

_WORD = re.compile(r"\w+", re.UNICODE)
_MERSENNE = (1 << 61) - 1


class NearDuplicateIndex:
    """MinHash signatures with LSH bands, saved as one JSON file.

    Jaccard similarity of word 3-shingles, estimated from ``permutations``
    hashes. Banding (``bands`` bands of ``permutations // bands`` rows) makes
    the candidate lookup a dict hit rather than a scan, and the estimate is
    then checked against every candidate. Sized for a single node: a hundred
    thousand signatures of 128 hashes is a few tens of megabytes on disk.
    """

    def __init__(
        self,
        path: Optional[Union[str, Path]] = None,
        permutations: int = 128,
        bands: int = 16,
        shingle: int = 3,
        seed: int = 20260911,
    ) -> None:
        if permutations % bands:
            raise ValueError("bands must divide permutations")
        self.path = Path(path) if path else None
        self.permutations = permutations
        self.bands = bands
        self.rows = permutations // bands
        self.shingle = shingle
        rng = np.random.default_rng(seed)
        self._a = rng.integers(1, _MERSENNE, size=permutations, dtype=np.int64)
        self._b = rng.integers(0, _MERSENNE, size=permutations, dtype=np.int64)
        self._signatures: Dict[str, np.ndarray] = {}
        self._buckets: List[Dict[bytes, List[str]]] = [{} for _ in range(bands)]
        self._lock = threading.Lock()
        if self.path and self.path.exists():
            self._load()

    # -- signatures -------------------------------------------------------

    def _shingles(self, text: str) -> List[int]:
        words = [w.lower() for w in _WORD.findall(text)]
        if len(words) < self.shingle:
            words = words + [""] * (self.shingle - len(words))
        out = set()
        for i in range(len(words) - self.shingle + 1):
            token = " ".join(words[i : i + self.shingle]).encode("utf-8")
            out.add(int.from_bytes(hashlib.blake2b(token, digest_size=8).digest(), "big"))
        return sorted(out)

    def signature(self, text: str) -> np.ndarray:
        shingles = np.asarray(self._shingles(text), dtype=np.uint64)
        if shingles.size == 0:
            return np.full(self.permutations, _MERSENNE, dtype=np.int64)
        # (a * x + b) mod p for every permutation, min over shingles.
        x = shingles.astype(np.object_)
        mins = np.empty(self.permutations, dtype=np.int64)
        for i in range(self.permutations):
            a, b = int(self._a[i]), int(self._b[i])
            values = [(a * int(v) + b) % _MERSENNE for v in x]
            mins[i] = min(values)
        return mins

    @staticmethod
    def similarity(sig_a: np.ndarray, sig_b: np.ndarray) -> float:
        return float(np.mean(sig_a == sig_b))

    # -- index ------------------------------------------------------------

    def _band_keys(self, sig: np.ndarray) -> List[bytes]:
        return [sig[i * self.rows : (i + 1) * self.rows].tobytes() for i in range(self.bands)]

    def add(self, doc_id: str, text: str, signature: Optional[np.ndarray] = None) -> np.ndarray:
        sig = signature if signature is not None else self.signature(text)
        with self._lock:
            self._signatures[doc_id] = sig
            for band, key in zip(self._buckets, self._band_keys(sig)):
                band.setdefault(key, []).append(doc_id)
        return sig

    def remove(self, doc_id: str) -> None:
        with self._lock:
            sig = self._signatures.pop(doc_id, None)
            if sig is None:
                return
            for band, key in zip(self._buckets, self._band_keys(sig)):
                ids = band.get(key)
                if ids:
                    band[key] = [d for d in ids if d != doc_id]
                    if not band[key]:
                        del band[key]

    def query(
        self, text: str, threshold: float = 0.9, signature: Optional[np.ndarray] = None
    ) -> Optional[Tuple[str, float]]:
        """The most similar stored document at or above ``threshold``, or None."""
        sig = signature if signature is not None else self.signature(text)
        with self._lock:
            candidates: set = set()
            for band, key in zip(self._buckets, self._band_keys(sig)):
                candidates.update(band.get(key, ()))
            best: Optional[Tuple[str, float]] = None
            for doc_id in candidates:
                score = self.similarity(sig, self._signatures[doc_id])
                if score >= threshold and (best is None or score > best[1]):
                    best = (doc_id, score)
        return best

    def __len__(self) -> int:
        return len(self._signatures)

    # -- persistence ------------------------------------------------------

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            payload = {
                "permutations": self.permutations,
                "bands": self.bands,
                "shingle": self.shingle,
                "signatures": {k: v.tolist() for k, v in self._signatures.items()},
            }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self.path)

    def _load(self) -> None:
        assert self.path is not None
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("permutations") != self.permutations or payload.get("bands") != self.bands:
            raise ValueError(f"{self.path} was written with different MinHash settings")
        for doc_id, values in payload.get("signatures", {}).items():
            self.add(doc_id, "", signature=np.asarray(values, dtype=np.int64))


# ============================================================================
# THE EMBEDDING CACHE
# ============================================================================
#
# INPUT   a model and a text
# OUTPUT  its vector, keyed on the model and the content hash, in one SQLite
#         file
#
# A re-index after a chunking change embeds only what changed.


class EmbeddingCache:
    """Vectors keyed on (model, content hash) in one SQLite file.

    Re-ingesting a folder where three files changed embeds three files. The
    key includes the model name, so switching models never serves a stale
    vector, and the dimension is stored with each row for safety.
    """

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)
        # Its folder may be the first thing on this disk: a collection kept in
        # a search service, Cosmos and Blob writes nothing of its own here,
        # and SQLite makes a file but never the folder it goes in.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS vectors ("
            "model TEXT NOT NULL, content_hash TEXT NOT NULL, dimension INTEGER NOT NULL, "
            "vector BLOB NOT NULL, PRIMARY KEY (model, content_hash))"
        )
        self._conn.commit()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()  # noqa: S324 - not security

    def get_many(self, model: str, texts: Sequence[str]) -> List[Optional[np.ndarray]]:
        keys = [self.key(t) for t in texts]
        found: Dict[str, np.ndarray] = {}
        with self._lock:
            for start in range(0, len(keys), 500):
                chunk_keys = keys[start : start + 500]
                marks = ",".join("?" * len(chunk_keys))
                rows = self._conn.execute(
                    f"SELECT content_hash, dimension, vector FROM vectors WHERE model = ? AND content_hash IN ({marks})",
                    [model, *chunk_keys],
                ).fetchall()
                for content_hash, dimension, blob in rows:
                    found[content_hash] = np.frombuffer(blob, dtype=np.float32).reshape(dimension)
        out = [found.get(k) for k in keys]
        self.hits += sum(1 for v in out if v is not None)
        self.misses += sum(1 for v in out if v is None)
        return out

    def put_many(self, model: str, texts: Sequence[str], vectors: np.ndarray) -> None:
        vectors = np.asarray(vectors, dtype=np.float32)
        rows = [
            (model, self.key(t), int(vectors.shape[1]), vectors[i].tobytes())
            for i, t in enumerate(texts)
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO vectors (model, content_hash, dimension, vector) VALUES (?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def __len__(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ============================================================================
# PARENT SECTIONS
# ============================================================================
#
# INPUT   a document and a parent size
# OUTPUT  the enclosing sections small chunks point back to, in a folder or in
#         a Cosmos container
#
# Small chunks retrieve well and read badly; the parent is what is shown.


class _CosmosParents:
    """Parents in one Cosmos DB container, partitioned by the document they came from.

    Partitioned by document and not by parent, because the only bulk
    operation is forgetting a document, and that is then one partition
    rather than a scan of the container.
    """

    def __init__(self, container: Any) -> None:
        self._container = container

    @staticmethod
    def _safe(text: str) -> str:
        """An id Cosmos will take. A document id holds slashes; an item id may not."""
        from urllib.parse import quote

        return quote(text, safe="")

    @classmethod
    def open(cls, url: str, key: Optional[str] = None) -> "_CosmosParents":
        from urllib.parse import urlsplit

        from .exceptions import ConfigurationError

        try:
            from azure.cosmos import CosmosClient, PartitionKey
        except ImportError as exc:
            raise DependencyError("azure-cosmos", "azure") from exc
        parts = urlsplit(url)
        names = [part for part in parts.path.split("/") if part]
        if not parts.hostname or len(names) != 2:
            raise ConfigurationError(
                "A parent store in Cosmos DB reads "
                "cosmos://<account>.documents.azure.com/<database>/<container>"
            )
        credential: Any = key
        if not credential:
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError as exc:
                raise DependencyError("azure-identity", "azure") from exc
            credential = DefaultAzureCredential()
        client = CosmosClient(
            f"https://{parts.hostname}:{parts.port or 443}/", credential=credential
        )
        database_name, container_name = names
        container = client.get_database_client(database_name).get_container_client(container_name)
        try:
            container.read()
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
            try:
                database = client.create_database_if_not_exists(database_name)
                container = database.create_container_if_not_exists(
                    id=container_name, partition_key=PartitionKey(path="/doc")
                )
            except Exception as refused:
                raise ConfigurationError(
                    f"The Cosmos DB container {database_name}/{container_name} is not there, and this "
                    "may not make it. Make it with the partition key /doc, then start again"
                ) from refused
        return cls(container)

    def put(self, parent_id: str, doc_id: str, text: str, metadata: Dict[str, Any]) -> None:
        self._container.upsert_item(
            {
                "id": self._safe(parent_id),
                "doc": self._safe(doc_id),
                "parent": parent_id,
                "text": text,
                "metadata": metadata,
            }
        )

    def get(self, parent_id: str, doc_id: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        try:
            item = self._container.read_item(
                item=self._safe(parent_id), partition_key=self._safe(doc_id)
            )
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise
        return str(item.get("text") or ""), dict(item.get("metadata") or {})

    def delete_document(self, doc_id: str) -> int:
        where = self._safe(doc_id)
        found = list(
            self._container.query_items(
                query="SELECT c.id FROM c WHERE c.doc = @doc",
                parameters=[{"name": "@doc", "value": where}],
                partition_key=where,
            )
        )
        for item in found:
            self._container.delete_item(item=item["id"], partition_key=where)
        return len(found)


def _doc_of(parent_id: str) -> str:
    """The document a parent belongs to. Parent ids are ``<doc_id>:parent:<n>``."""
    return parent_id.rsplit(":parent:", 1)[0] if ":parent:" in parent_id else parent_id


class ParentStore:
    """The enclosing sections that small chunks point back to.

    Parents are never searched; they are returned. Three places they can
    live, and which one is right depends on who reads them:

    * a **SQLite file** beside the collection, the default, so they survive
      restarts and travel with ``export()``;
    * **Cosmos DB**, given ``cosmos://<account>.documents.azure.com/<database>/<container>``,
      when more than one process reads them. That is not a nicety on a
      deployment that scales out: parents written by the instance that
      ingested a document are invisible to the instance serving the search
      if they sit on a local disk, and the symptom is parent-child
      retrieval quietly returning nothing rather than an error anywhere;
    * **in memory**, when there is nowhere given, which is fine for one
      process that does not restart and wrong for anything else.

    The SQLite one makes its own folder. It used not to, and a collection
    opened on a remote backend never built that folder itself, so the first
    document into a fresh deployment died on ``unable to open database
    file`` from three frames inside an unrelated-looking call.
    """

    def __init__(
        self, path: Optional[Union[str, Path]] = None, *, key: Optional[str] = None
    ) -> None:
        self.path: Optional[Path] = None
        self._memory: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        self._conn: Optional[sqlite3.Connection] = None
        self._cosmos: Optional[_CosmosParents] = None
        self._lock = threading.Lock()
        if not path:
            return
        text = str(path)
        if "://" in text:
            from urllib.parse import urlsplit

            from .exceptions import ConfigurationError

            scheme = urlsplit(text).scheme.lower()
            if scheme != "cosmos":
                raise ConfigurationError(
                    f"A parent store is a path or cosmos://<account>.documents.azure.com/<database>/<container>, not {scheme}://"
                )
            self._cosmos = _CosmosParents.open(text, key=key)
            return
        self.path = Path(path)
        # The folder, because nothing else makes it when the collection
        # itself lives on a service rather than on this disk.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS parents (id TEXT PRIMARY KEY, text TEXT NOT NULL, metadata TEXT)"
        )
        self._conn.commit()

    @property
    def where(self) -> str:
        """Where the parents are, for a log line on a cold start."""
        if self._cosmos is not None:
            return "Cosmos DB"
        return str(self.path) if self.path else "memory"

    def put(self, parent_id: str, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        if self._cosmos is not None:
            self._cosmos.put(parent_id, _doc_of(parent_id), text, dict(metadata or {}))
            return
        with self._lock:
            if self._conn is None:
                self._memory[parent_id] = (text, dict(metadata or {}))
                return
            self._conn.execute(
                "INSERT OR REPLACE INTO parents (id, text, metadata) VALUES (?, ?, ?)",
                (parent_id, text, json.dumps(metadata or {})),
            )
            self._conn.commit()

    def get(self, parent_id: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        if self._cosmos is not None:
            return self._cosmos.get(parent_id, _doc_of(parent_id))
        with self._lock:
            if self._conn is None:
                return self._memory.get(parent_id)
            row = self._conn.execute(
                "SELECT text, metadata FROM parents WHERE id = ?", (parent_id,)
            ).fetchone()
        if row is None:
            return None
        return row[0], (json.loads(row[1]) if row[1] else {})

    def delete_document(self, doc_id: str) -> int:
        if self._cosmos is not None:
            return self._cosmos.delete_document(doc_id)
        prefix = f"{doc_id}:parent:"
        with self._lock:
            if self._conn is None:
                gone = [k for k in self._memory if k.startswith(prefix)]
                for k in gone:
                    del self._memory[k]
                return len(gone)
            cur = self._conn.execute(
                "DELETE FROM parents WHERE id LIKE ?", (prefix.replace("%", "%%") + "%",)
            )
            self._conn.commit()
            return int(cur.rowcount)

    def close(self) -> None:
        # Nothing to close on Cosmos: the client holds no connection of ours.
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


def parent_spans(doc: LoadedDocument, parent_size: int) -> List[Tuple[int, int]]:
    """Sections a document is divided into for parent-child retrieval:
    heading sections when the document has headings, else ``parent_size``
    windows on sentence boundaries."""
    if doc.headings:
        spans = _markdown_spans(doc, max(parent_size, 1) * 4, 0)
        # _markdown_spans splits long sections; merge nothing, just return.
        return spans or [(0, len(doc.text))]
    return _sentence_spans(doc.text, parent_size, 0) or [(0, len(doc.text))]


# ============================================================================
# A DOCUMENT, PREPARED
# ============================================================================
#
# INPUT   a loaded document and its id
# OUTPUT  everything decided before it is written: its chunks, what each
#         carries, the strings the dense model sees, figures tied to the text
#         that mentions them, and when a recording's chunk was said
#
# One pass, so the collection writes what it is handed and decides nothing.

# prepare_document takes the strategy as ``chunk``, which is also the name of
# this module's chunker.
_cut = chunk

_FIGURE_LABEL = re.compile(
    r"^\s*(figure|fig\.?|chart|table|exhibit|diagram)\s+(\d+[a-z]?(?:\.\d+)?)", re.IGNORECASE
)


@dataclass
class PreparedDocument:
    """Everything about a document that is decided before it is written:
    its chunks, their ids and their metadata, the sections parent retrieval
    returns, its version and how it scored."""

    doc: LoadedDocument
    doc_id: str
    texts: List[str]
    ids: List[str]
    metadata: List[Dict[str, Any]]
    version: str
    quality: float
    chunking: Dict[str, Any]
    parents: List[Tuple[str, str, Dict[str, Any]]] = field(default_factory=list)


def texts_to_embed(texts: Sequence[str], metadata: Any) -> List[str]:
    """The strings the dense model sees: a chunk's text, with whatever
    context its metadata says belongs in front of it or after it. The
    stored text never carries that context."""
    if not isinstance(metadata, list) or len(metadata) != len(texts):
        return list(texts)
    out: List[str] = []
    for text, meta in zip(texts, metadata):
        prefix = (meta or {}).get("_vx_embed_prefix")
        suffix = (meta or {}).get("_vx_embed_suffix")
        if prefix:
            text = f"{prefix}: {text}"
        if suffix:
            text = f"{text}\n{suffix}"
        out.append(text)
    return out


def link_figures(
    doc_id: str, chunks: Sequence[Chunk], metas: List[Dict[str, Any]], ids: Sequence[str]
) -> None:
    """Give every figure a stable id, and tie it to the text that mentions it.

    The id is the document, the page and the figure's place on it,
    ``acme-q3.pdf#p4-fig1``. A figure whose caption starts ``Figure 3`` is
    looked for in the other chunks: the one that says ``Figure 3`` gets
    ``refers_to``, the figure gets ``referenced_by``, and the sentence that
    mentions it goes to the embedder with the figure, so a chart is found by
    what the report says about it.
    """
    on_page: Dict[Any, int] = {}
    for c, m, chunk_id in zip(chunks, metas, ids):
        if not c.figure:
            continue
        on_page[c.page] = on_page.get(c.page, 0) + 1
        place = f"p{c.page}-fig{on_page[c.page]}" if c.page is not None else f"fig{on_page[c.page]}"
        m["figure_id"] = f"{doc_id}#{place}"
        label = _FIGURE_LABEL.match(c.figure) or _FIGURE_LABEL.match(c.text.split("\n", 1)[-1])
        if not label:
            continue
        kind = (
            "(?:figure|fig\\.?)"
            if label.group(1).lower().startswith("fig")
            else re.escape(label.group(1))
        )
        mention = re.compile(rf"\b{kind}\s*{re.escape(label.group(2))}(?![\w.]*\d)", re.IGNORECASE)
        for other, other_meta, other_id in zip(chunks, metas, ids):
            if other.figure:
                continue
            found = mention.search(other.text)
            if not found:
                continue
            other_meta.setdefault("refers_to", []).append(m["figure_id"])
            other_meta.setdefault("_vx_refers_to", []).append(chunk_id)
            m.setdefault("referenced_by", []).append(other_id)
            if "_vx_embed_suffix" not in m:
                start = (
                    max(
                        other.text.rfind(". ", 0, found.start()),
                        other.text.rfind("\n", 0, found.start()),
                    )
                    + 1
                )
                stop = other.text.find(". ", found.end())
                sentence = " ".join(
                    other.text[start : stop + 1 if stop != -1 else len(other.text)].split()
                )
                m["_vx_embed_suffix"] = f"Mentioned as: {sentence[:400]}"


def _timeline(doc: LoadedDocument) -> List[Tuple[int, float, float]]:
    """Where each of a recording's timed phrases sits in its text: ``(offset, start, end)``, in order.

    The text is the phrases a minute to a paragraph, so each is found by
    walking on from the last. A phrase that is not found, in a transcript
    edited by hand say, is left out, and the phrases either side of it still
    place the chunks around it.
    """
    placed: List[Tuple[int, float, float]] = []
    cursor = 0
    for start, end, phrase in doc.segments:
        words = " ".join(str(phrase).split())[:80]
        if not words:
            continue
        at = doc.text.find(words, cursor)
        if at < 0:
            continue
        placed.append((at, float(start), float(end)))
        cursor = at + len(words)
    return placed


def _times_of(
    timeline: Sequence[Tuple[int, float, float]], offsets: Sequence[int], start: int, end: int
) -> Tuple[Optional[float], Optional[float]]:
    """When the text from ``start`` to ``end`` was said: from the start of the
    phrase it opens in to the end of the last phrase it holds."""
    import bisect

    first = bisect.bisect_right(offsets, start) - 1
    last = bisect.bisect_left(offsets, end) - 1
    if first < 0 and offsets and offsets[0] < end:
        # The chunk opens before the first phrase, on a heading say.
        first = 0
    if first < 0 or last < first:
        return None, None
    return round(timeline[first][1], 1), round(max(timeline[last][2], timeline[first][1]), 1)


def prepare_document(
    doc: LoadedDocument,
    doc_id: str,
    *,
    chunk: str = "recursive",
    chunk_size: int = 1000,
    overlap: int = 200,
    parent_size: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
    embed_heading: bool = False,
    threshold: float = 0.1,
    embed: Optional[Callable[[List[str]], np.ndarray]] = None,
    quality_threshold: Optional[float] = None,
    cut_with: Optional[Callable[[str, List[Tuple[int, int]]], Sequence[int]]] = None,
    context_with: Optional[Callable[[str, str, int, int], Optional[str]]] = None,
) -> Optional[PreparedDocument]:
    """Cut a document and work out what every chunk carries. None when
    there is nothing to cut.

    Nothing is written and nothing is embedded, apart from what semantic
    chunking asks ``embed`` for. ``Vectrix.add_document`` and the REST
    server's documents route both call this, so a chunk means the same
    thing whichever way it came in: the same ids, the same citation, the
    same lineage stamps, the same figure links.

    ``cut_with`` is what ``chunk="llm"`` asks where each topic starts.
    ``context_with(document, chunk, start, end)`` writes a note that places
    a chunk in its document, which goes in front of the chunk for the
    embedder, and is kept as ``_vx_context``; the stored text is the chunk
    as written. :mod:`vectrixdb.chunk_models` makes both from a chat model.
    """
    from .citations import citation_for, readable_citation_for
    from .quality import DEFAULT_THRESHOLD, extraction_quality

    if not doc.text.strip():
        return None
    if overlap >= chunk_size:
        # A small chunk_size with the default overlap is a request for
        # small chunks, not for an error; keep a fifth as overlap.
        overlap = chunk_size // 5
    chunks = _cut(
        doc, chunk, chunk_size, overlap, embed=embed, threshold=threshold, cut_with=cut_with
    )
    if not chunks:
        return None
    cutoff = DEFAULT_THRESHOLD if quality_threshold is None else quality_threshold
    # Which pages OCR read, and the lines taken out of every page, are about
    # the document. A chunk is told the one thing it can use: whether the
    # page it came from was read by OCR.
    about_the_document = ("pages", "doc_id", "ocr_pages", "running_lines", "figures_described_by")
    base_meta = {
        **{k: v for k, v in doc.metadata.items() if k not in about_the_document},
        **(metadata or {}),
    }
    ocr_pages = {int(n) for n in doc.metadata.get("ocr_pages") or ()}

    parents: List[Tuple[str, str, Dict[str, Any]]] = []
    parents_of: Dict[int, str] = {}
    if parent_size:
        spans = parent_spans(doc, parent_size)
        for j, (start, end) in enumerate(spans):
            parents.append(
                (
                    f"{doc_id}:parent:{j}",
                    doc.text[start:end].strip(),
                    {**base_meta, "_vx_doc": doc_id, "_vx_start": start, "_vx_end": end},
                )
            )
        for c in chunks:
            for j, (start, end) in enumerate(spans):
                if start <= c.start < end or j == len(spans) - 1:
                    parents_of[c.index] = f"{doc_id}:parent:{j}"
                    break

    # Lineage. The version is a hash of the text as loaded, so two
    # ingestions of the same file can be told apart from two of a changed
    # one, and the chunking is stamped because the same text cut
    # differently is a different set of chunks.
    version = hashlib.sha256(doc.text.encode()).hexdigest()[:16]
    stamps = {
        "_vx_doc_version": version,
        "_vx_chunk_strategy": chunk,
        "_vx_chunk_size": chunk_size,
        "_vx_chunk_overlap": overlap,
    }
    scores = [extraction_quality(c.text, cutoff) for c in chunks]
    # What an answer cites. Built once here rather than at read time, so the
    # name a reader follows is the name the chunk was written under,
    # whatever the file is called by the time they follow it.
    cite_source = base_meta.get("filename") or base_meta.get("source")
    ids = [f"{doc_id}:{c.index}" for c in chunks]
    # A recording's phrases placed in its text, so a chunk can say when it was said.
    timeline = _timeline(doc) if doc.segments else []
    offsets = [at for at, _start, _end in timeline]
    metas: List[Dict[str, Any]] = []
    for c, quality in zip(chunks, scores):
        m = {**base_meta, **c.metadata(), **stamps, "_vx_doc": doc_id}
        if ocr_pages and c.page is not None:
            # A scan with a few typed pages in it: only the scanned pages' chunks are OCR's.
            m["ocr"] = any(n in ocr_pages for n in range(c.page, (c.page_end or c.page) + 1))
        if doc.page_labels and c.page is not None:
            # What the page says it is, beside where it is in the file.
            first, last = doc.page_label(c.page), doc.page_label(c.page_end)
            if first is not None:
                m["page_label"] = first
            if last is not None and c.page_end != c.page:
                m["page_label_end"] = last
        if timeline:
            said_from, said_to = _times_of(timeline, offsets, c.start, c.end)
            if said_from is not None:
                m["start_seconds"], m["end_seconds"] = said_from, said_to
        m["_vx_quality"] = quality.score
        m["_vx_citation"] = citation_for(
            cite_source,
            doc_id,
            page=m.get("page"),
            heading=m.get("heading"),
            figure=m.get("figure"),
            seconds=m.get("start_seconds"),
        )
        # The same place as a person reads it, for showing beside an answer.
        m["_vx_readable_citation"] = readable_citation_for(
            cite_source,
            doc_id,
            page=m.get("page"),
            page_end=m.get("page_end"),
            page_label=m.get("page_label"),
            page_label_end=m.get("page_label_end"),
            heading=m.get("heading"),
            figure=m.get("figure"),
            seconds=m.get("start_seconds"),
        )
        if c.index in parents_of:
            m["_vx_parent"] = parents_of[c.index]
        # A figure always carries its headings to the embedder: a bar
        # chart's numbers say nothing about which section they belong to.
        if c.heading_path and (embed_heading or c.figure):
            m["_vx_embed_prefix"] = " > ".join(c.heading_path)
        if context_with is not None and not c.figure:
            # A note on where the chunk sits in its document, for the embedder
            # alone: after the heading path when both are asked for.
            note = " ".join(str(context_with(doc.text, c.text, c.start, c.end) or "").split())
            if note:
                m["_vx_context"] = note
                m["_vx_embed_prefix"] = (
                    f"{m['_vx_embed_prefix']}. {note}" if m.get("_vx_embed_prefix") else note
                )
        metas.append(m)
    link_figures(doc_id, chunks, metas, ids)
    chunking: Dict[str, Any] = {"strategy": chunk, "chunk_size": chunk_size, "overlap": overlap}
    if context_with is not None:
        chunking["context"] = True
    return PreparedDocument(
        doc=doc,
        doc_id=doc_id,
        texts=[c.text for c in chunks],
        ids=ids,
        metadata=metas,
        version=version,
        quality=round(sum(s.score for s in scores) / len(scores), 4),
        chunking=chunking,
        parents=parents,
    )


# ============================================================================
# VECTORS PACKED, AND BATCHES
# ============================================================================
#
# INPUT   a vector; items and a size
# OUTPUT  float32 bytes with a length prefix; the items in batches
#
# For callers who store vectors raw.


def pack_vector(vector: np.ndarray) -> bytes:
    """float32 bytes with a length prefix, for callers who store vectors raw."""
    v = np.asarray(vector, dtype=np.float32)
    return struct.pack("<I", v.size) + v.tobytes()


def iter_batches(items: Iterable[Any], size: int) -> Iterable[List[Any]]:
    batch: List[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
