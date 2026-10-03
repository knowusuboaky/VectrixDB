"""A PDF's text as a person reads it, from where every run of text sits on its page.

A PDF has no paragraphs, no columns, no tables and no headings. It has runs of
glyphs placed at points on a page. A reader that asks for "the text" gets the
runs in whatever order the file happened to write them, joined with guesses:
words broken in the middle, two columns read across, the numbers of a table
without the years above them, and a page's running title in the middle of
every chunk. This module reads the runs with their places, sizes and weights
from PDFium, the engine inside Chrome, and rebuilds what a person sees:

* **Reading order.** Columns are found by the empty band between them and read
  one after the other, with :mod:`vectrixdb.extract.layout`'s rules.
* **Running heads and feet.** A line is taken out only when it sits in the top
  or bottom margin and repeats there on many pages. A table's "Total" row at
  the same place on every page is not in the margin, and stays.
* **Paragraphs.** Lines join into paragraphs by their spacing, indents and
  sentence ends; a word hyphenated across lines is joined, keeping its hyphen
  only when the document writes it that way elsewhere; a raised footnote
  number is written ``[^3]`` and not glued onto the word before it.
* **Headings.** A line set larger, or bold and alone, is a heading, its level
  by how large it is among the document's headings.
* **Tables.** Rows whose numbers line up in columns are a table. The lines
  above them that sit over the columns are its header, joined per column, so
  every row says what each number is: ``Net interest income; 2025 Oct. 31:
  $ 8,545``.
* **What is not there to be read.** White text on a white page, invisible text
  that no picture lies under, and text too small to see are left out and
  counted: they are how a document hides instructions from its reader.
* **Charts it draws.** Asked with ``charts=True``, a chart drawn with shapes
  rather than pasted in as a picture is drawn as a picture of its own
  region, its title, scales and legend taken in, for a describer: bars
  standing on one line, a line through its values, a panel of charts, a
  diagram of boxes. A table, text with shapes about it and boxes of
  sentences are not taken for charts. Its ``[Figure: ...]`` line stands
  where it sits, with the words it prints on one line under it, taken out
  of the page's text.

Nothing here is required. :func:`read_pdf` raises ImportError without
``pypdfium2``, and the caller falls back to the plain text reader.
"""

from __future__ import annotations

import ctypes
import math
import re
import statistics
import threading
from collections import Counter
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .layout import _PAGE_NUMBER, _gutter, _lines, _prose

__all__ = ["PDFIUM_LOCK", "PdfRead", "read_pdf"]

#: Held for every call into PDFium, by this module and by whatever else in the
#: library draws a page with it. PDFium keeps global state and is not
#: thread-safe, across documents as much as within one: two ingest threads
#: reading two PDFs at once corrupted memory that failed much later, in a
#: different library. A reentrant lock, so a caller holding it may call in.
PDFIUM_LOCK = threading.RLock()


# ============================================================================
# SETTINGS: what a run is, and the numbers the rules are made of
# ============================================================================
#
# Positions are in points from the top-left of the page, as a person reads it.

#: The share of a page's height at its top and at its bottom where a running
#: head or foot is printed. A body set one inch from the edge starts at 9% of
#: a letter page, so 8% keeps a first line of body out of it.
MARGIN = 0.08
#: A little further in, a line that repeats word for word is still a running
#: line: a report with deep margins prints its title lower.
MARGIN_EXACT = 0.14
#: The share of a document's pages a running line has to be found on. Low,
#: because a report prints each section's own foot on that section's pages,
#: and safe, because only a line in the margin is ever counted.
RUNNING_SHARE = 0.1
#: A heading is at least this much larger than the body text.
HEADING_SCALE = 1.2
#: Two runs further apart than this many font sizes are two cells of a table.
CELL_GAP = 1.3

#: A figure: optional sign or currency, digits with separators, optional
#: percent, or the dash a table prints for nothing.
_NUMBER = re.compile(
    r"^[(\-−–+]?\s*[$€£¥]?\s*\(?\d[\d,.\s]*\)?\s*%?\)?\*?$|^[–—\-]$|^n/?a$", re.IGNORECASE
)
#: What a cell holding a year or a date looks like; it can head a column.
_DATEISH = re.compile(
    r"^(?:19|20)\d\d$|^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{1,2},?(?:\s*(?:19|20)\d\d)?$",
    re.IGNORECASE,
)
#: Characters a PDF sets as one glyph for two or three letters.
_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
#: How PDFium writes a hyphen that is there only because the line broke.
_SOFT = ("\x02", "\xad", "￾")
_SENTENCE_END = re.compile(r"[.!?:;”\"')\]]$")
#: What opens a list item: a bullet glyph, a dash, or a short number or letter
#: with its dot or bracket, followed by the item's words.
_LIST_MARK = re.compile(
    r"^(?P<mark>[•◦▪▫‣⁃●○■□–—\-*·]|\(?\d{1,2}[.)]|\(?[a-zA-Z][.)]|\(?[ivxIVX]{1,4}[.)]|\d{1,2})\s+(?=\S)"
)
#: Bullet glyphs, written as Markdown's dash so every reader's lists look alike.
_BULLETS = "•◦▪▫‣⁃●○■□·*–—-"
#: Marks a line of prose can start with too: believed only inside a list.
_WEAK_MARKS = re.compile(r"^(?:\d{1,2}|[–—\-])$")


def _weak(mark: str) -> bool:
    return _WEAK_MARKS.match(mark) is not None


@dataclass
class Run:
    """A stretch of text on one line, with where it sits and how it is set."""

    left: float
    top: float
    right: float
    bottom: float
    text: str
    size: float
    bold: bool

    @property
    def middle(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class Line:
    """Runs on one line, left to right, and what the reader decided the line is."""

    runs: List[Run]
    kind: str = "text"  # text, heading, row, header, caption, figure
    level: int = 0
    cells: List[Tuple[float, float, str]] = field(default_factory=list)

    @property
    def left(self) -> float:
        return min(r.left for r in self.runs)

    @property
    def right(self) -> float:
        return max(r.right for r in self.runs)

    @property
    def top(self) -> float:
        return min(r.top for r in self.runs)

    @property
    def bottom(self) -> float:
        return max(r.bottom for r in self.runs)

    @cached_property
    def size(self) -> float:
        weights: Counter = Counter()
        for r in self.runs:
            weights[round(r.size * 2) / 2] += len(r.text)
        return weights.most_common(1)[0][0] if weights else 0.0

    @cached_property
    def bold(self) -> bool:
        letters = sum(len(r.text) for r in self.runs)
        return letters > 0 and sum(len(r.text) for r in self.runs if r.bold) >= 0.8 * letters

    @cached_property
    def text(self) -> str:
        return _join_runs(self.runs, self.size)


@dataclass
class PdfRead:
    """What :func:`read_pdf` found: a text a page, and what it left out, and why."""

    pages: List[str]
    running: List[str] = field(default_factory=list)
    hidden: int = 0
    textless: List[int] = field(default_factory=list)
    pictured: List[int] = field(default_factory=list)
    tables: int = 0
    fields: int = 0
    comments: int = 0
    #: Each chart drawn with shapes, as ``(page, top, png, name)``: its region as
    #: a picture, and the name its ``[Figure: name]`` line carries in the page's text.
    charts: List[Tuple[int, float, bytes, str]] = field(default_factory=list)
    #: Each page's own words, a line at a time in reading order, running lines
    #: left out: what a reading of the page by anything else is held to.
    layers: List[str] = field(default_factory=list)
    #: The pages the rules are unsure of, and why: page number to "chart",
    #: "tiles", "order" or "rotated". Judged only when asked.
    hard: Dict[int, str] = field(default_factory=dict)
    #: Each hard page's figures printed only on a chart's scale, by value: no
    #: value a reading writes may be one of them.
    ticks: Dict[int, List[float]] = field(default_factory=dict)


# ============================================================================
# RUNS: what PDFium says is on a page
# ============================================================================
#
# INPUT   a page and its text page
# OUTPUT  its runs, top-down coordinates, each with its size and weight; and
#         how many runs were left out because nobody can see them


def _clean(text: str) -> str:
    """A run's text with ligatures spelled out and PDFium's own marks made plain."""
    for glyph, letters in _LIGATURES.items():
        if glyph in text:
            text = text.replace(glyph, letters)
    text = text.replace("\r", " ").replace("\n", " ").replace("\x00", "").replace("\xa0", " ")
    return text


class _Styles:
    """Each text object's size, weight, colour and render mode, asked for once."""

    def __init__(self, raw: Any, textpage: Any) -> None:
        self.raw = raw
        self.tp = textpage
        self.seen: Dict[int, Tuple[float, bool, Tuple[int, int, int, int], int]] = {}
        self._rgba = [ctypes.c_uint() for _ in range(4)]
        self._matrix = raw.FS_MATRIX()

    def of(self, index: int) -> Tuple[float, bool, Tuple[int, int, int, int], int]:
        raw, tp = self.raw, self.tp
        obj = raw.FPDFText_GetTextObject(tp, index)
        key = ctypes.cast(obj, ctypes.c_void_p).value or -index - 1
        if key in self.seen:
            return self.seen[key]
        size = float(raw.FPDFText_GetFontSize(tp, index) or 0.0)
        if raw.FPDFText_GetMatrix(tp, index, ctypes.byref(self._matrix)):
            scale = math.hypot(self._matrix.c, self._matrix.d)
            if scale > 0:
                size *= scale
        weight = int(raw.FPDFText_GetFontWeight(tp, index))
        bold = weight >= 600
        if weight < 0:
            buffer = ctypes.create_string_buffer(128)
            flags = ctypes.c_int()
            raw.FPDFText_GetFontInfo(tp, index, buffer, 128, ctypes.byref(flags))
            name = buffer.value.decode("latin-1", "replace").lower()
            bold = any(word in name for word in ("bold", "black", "heavy", "semibold", "demi"))
        r, g, b, a = self._rgba
        if raw.FPDFText_GetFillColor(
            tp, index, ctypes.byref(r), ctypes.byref(g), ctypes.byref(b), ctypes.byref(a)
        ):
            rgba = (r.value, g.value, b.value, a.value)
        else:
            rgba = (0, 0, 0, 255)
        mode = int(raw.FPDFTextObj_GetTextRenderMode(obj)) if obj else 0
        found = (size, bold, rgba, mode)
        self.seen[key] = found
        return found


def _backgrounds(
    page: Any, raw: Any, height: float
) -> List[Tuple[float, float, float, float, bool]]:
    """What is drawn on a page that text can sit on: pictures, and shapes filled with a colour that is not white.

    ``(left, top, right, bottom, is_picture)``. Asked for only on a page with
    text that might be hidden, since walking every object costs time.
    """
    out: List[Tuple[float, float, float, float, bool]] = []
    kinds = (raw.FPDF_PAGEOBJ_IMAGE, raw.FPDF_PAGEOBJ_PATH, raw.FPDF_PAGEOBJ_SHADING)
    rgba = [ctypes.c_uint() for _ in range(4)]
    mode, stroke = ctypes.c_int(), ctypes.c_int()
    try:
        objects = list(page.get_objects(filter=kinds, max_depth=4))
    except Exception:  # noqa: BLE001 - a page whose objects will not list hides nothing we can prove
        return out
    for obj in objects:
        try:
            # get_bounds since pypdfium2 5, get_pos before it.
            left, bottom, right, top = (
                obj.get_bounds() if hasattr(obj, "get_bounds") else obj.get_pos()
            )
        except Exception:  # noqa: BLE001
            continue
        picture = obj.type == raw.FPDF_PAGEOBJ_IMAGE
        if obj.type == raw.FPDF_PAGEOBJ_PATH:
            if (
                not raw.FPDFPath_GetDrawMode(obj.raw, ctypes.byref(mode), ctypes.byref(stroke))
                or mode.value == 0
            ):
                continue
            r, g, b, a = rgba
            if raw.FPDFPageObj_GetFillColor(
                obj.raw, ctypes.byref(r), ctypes.byref(g), ctypes.byref(b), ctypes.byref(a)
            ):
                if a.value == 0 or min(r.value, g.value, b.value) >= 245:
                    continue
        out.append((left, height - top, right, height - bottom, picture))
    return out


def _over(
    run: Run, grounds: Sequence[Tuple[float, float, float, float, bool]], pictures_only: bool
) -> bool:
    x, y = (run.left + run.right) / 2, run.middle
    return any(
        l <= x <= r and t <= y <= b and (picture or not pictures_only)
        for l, t, r, b, picture in grounds
    )


def _runs(page: Any, textpage: Any, raw: Any, height: float) -> Tuple[List[Run], int]:
    """The runs PDFium finds on a page, and how many it left out as unseen."""
    tp = textpage
    count = raw.FPDFText_CountChars(tp)
    if count <= 0:
        return [], 0
    rects = raw.FPDFText_CountRects(tp, 0, count)
    styles = _Styles(raw, tp)
    # The page's text once, and each run cut from it by where its first and
    # last characters are: asking PDFium for the text inside a box searches
    # the whole page each time, and a dense page has hundreds of runs.
    whole = textpage.get_text_range(0, count)
    l, t, r, b = (ctypes.c_double() for _ in range(4))
    runs: List[Run] = []
    doubtful: List[Tuple[Run, bool]] = []
    hidden = 0
    for k in range(rects):
        if not raw.FPDFText_GetRect(
            tp, k, ctypes.byref(l), ctypes.byref(t), ctypes.byref(r), ctypes.byref(b)
        ):
            continue
        middle = (b.value + t.value) / 2
        first = raw.FPDFText_GetCharIndexAtPos(tp, l.value + 0.3, middle, 1.0, 1.0)
        last = raw.FPDFText_GetCharIndexAtPos(tp, r.value - 0.3, middle, 1.0, 1.0)
        if 0 <= first <= last and last - first < 5000:
            words = _clean(whole[first : last + 1])
        else:
            words = _clean(textpage.get_text_bounded(l.value, b.value, r.value, t.value))
        if not words.strip():
            continue
        at = first
        if at < 0:
            at = raw.FPDFText_GetCharIndexAtPos(
                tp, l.value + min(1.0, (r.value - l.value) / 2), middle, 2.0, 2.0
            )
        if at < 0:
            at = raw.FPDFText_GetCharIndexAtPos(tp, (l.value + r.value) / 2, middle, 4.0, 4.0)
        size, bold, rgba, mode = (
            styles.of(at) if at >= 0 else (t.value - b.value, False, (0, 0, 0, 255), 0)
        )
        if size <= 0:
            size = t.value - b.value
        run = Run(l.value, height - t.value, r.value, height - b.value, words, size, bold)
        if size < 1.0 or rgba[3] == 0:
            hidden += 1
            continue
        if mode == 3:
            doubtful.append(
                (run, True)
            )  # invisible: kept only over a picture, the text layer of a scan
            continue
        if min(rgba[:3]) >= 245:
            doubtful.append((run, False))  # white: kept only over something drawn
            continue
        runs.append(run)
    if doubtful:
        grounds = _backgrounds(page, raw, height)
        for run, invisible in doubtful:
            if _over(run, grounds, pictures_only=invisible):
                runs.append(run)
            else:
                hidden += 1
    return runs, hidden


# ============================================================================
# LINES: runs grouped, in the order a person reads them
# ============================================================================
#
# INPUT   a page's runs
# OUTPUT  its lines in reading order: columns one after the other, and what
#         crosses them where it sits


def _same_line(run: Run, line: List[Run]) -> bool:
    top, bottom = min(r.top for r in line), max(r.bottom for r in line)
    overlap = min(bottom, run.bottom) - max(top, run.top)
    if overlap >= 0.45 * min(run.bottom - run.top, bottom - top):
        return True
    # A footnote mark is set small and raised, so little of it overlaps the
    # line it belongs to; it still ends inside that line's height, whichever
    # of the two came first.
    small, big = (run, line) if run.bottom - run.top < 0.8 * (bottom - top) else (line, run)
    if small is run:
        return overlap > 0 and top < run.bottom <= bottom
    big_run = run
    small_bottom = bottom
    return (
        overlap > 0
        and (bottom - top) < 0.8 * (big_run.bottom - big_run.top)
        and big_run.top < small_bottom <= big_run.bottom
    )


def _group(runs: Sequence[Run]) -> List[Line]:
    """Runs on one line together, top to bottom, left to right; a raised footnote number stays on its line."""
    lines: List[List[Run]] = []
    for run in sorted(runs, key=lambda r: (r.top, r.left)):
        for line in reversed(lines[-3:]):
            if _same_line(run, line):
                line.append(run)
                break
        else:
            lines.append([run])
    grouped = [Line(sorted(line, key=lambda r: r.left)) for line in lines]
    grouped.sort(key=lambda line: (line.bottom, line.left))
    return grouped


def _stream_lines(runs: Sequence[Run]) -> List[Line]:
    """Runs in the order the file wrote them, a new line wherever the next run is not further along the same line.

    A report laid out in a design tool writes its text in reading order,
    story by story, whatever its columns and boxes look like; reordering it
    by position reads a three-column page with headlines across it worse
    than the file already does.
    """
    lines: List[Line] = []
    for run in runs:
        if lines:
            current = lines[-1]
            last = current.runs[-1]
            if _same_line(run, current.runs) and run.left >= last.right - 0.5 * min(
                run.size, last.size
            ):
                current.runs.append(run)
                continue
        lines.append(Line([run]))
    return lines


def _prose_run(run: Run) -> bool:
    words = run.text.strip()
    return len(words) >= 12 and sum(c.isalpha() for c in words) >= 0.6 * len(words)


def _coherent(lines: Sequence[Line]) -> bool:
    """Whether the file wrote a page in an order a person reads it.

    Not when most of its lines climb the page, a file that writes from the
    bottom up; and not when many of its lines are two stretches of prose with
    a column's gutter between them, a file that writes across two columns a
    line at a time.
    """
    ups = sum(1 for a, b in zip(lines, lines[1:]) if b.top < a.top - 0.3 * max(a.size, 1.0))
    downs = sum(1 for a, b in zip(lines, lines[1:]) if b.top > a.top + 0.3 * max(a.size, 1.0))
    if ups >= 2 and ups > 1.5 * downs:
        return False
    if len(lines) < 6:
        return True
    prose = crossing = 0
    for line in lines:
        if len(line.text) < 20:
            continue
        prose += 1
        size = max(line.size, 1.0)
        if any(
            b.left - a.right > 2.0 * size and _prose_run(a) and _prose_run(b)
            for a, b in zip(line.runs, line.runs[1:])
        ):
            crossing += 1
    return not (crossing >= 3 and crossing >= 0.25 * prose)


def _split_rows(lines: Sequence[Line]) -> bool:
    """Whether the file wrote a table a column at a time: figures on a line of their own that share a row with other lines.

    Read in the file's order such a table is all its labels and then each
    column of figures; the rows exist only by where the runs sit.
    """
    alone = [line for line in lines if len(line.runs) == 1 and _numeric(line.runs[0].text)]
    if len(alone) < 4:
        return False
    shared = 0
    for line in alone:
        run = line.runs[0]
        if any(
            other is not line and other.runs[0].right <= run.left and _same_line(run, other.runs)
            for other in lines
        ):
            shared += 1
    return shared >= 4 and shared >= 0.5 * len(alone)


def _place_values(lines: List[Line], values: Sequence[Run]) -> None:
    """What was typed into a form's fields, each on the line of the label beside it, or a line of its own below."""
    for value in values:
        for line in lines:
            if _same_line(value, line.runs) and line.right <= value.left + 1.0:
                line.runs.append(value)
                line.runs.sort(key=lambda r: r.left)
                for cached in ("size", "text", "bold"):
                    line.__dict__.pop(cached, None)
                break
        else:
            lines.append(Line([value]))


def _page_lines(runs: Sequence[Run]) -> List[Line]:
    """A page's lines: in the file's own order when that order reads, rebuilt from positions when it does not."""
    lines = _stream_lines(runs)
    return lines if _coherent(lines) and not _split_rows(lines) else _order(runs)


def _items(runs: Sequence[Run]) -> List[Tuple[Tuple[float, float, float, float], str]]:
    return [((r.left, r.top, r.right, r.bottom), r.text) for r in runs]


def _order(runs: Sequence[Run], depth: int = 0) -> List[Line]:
    """The lines of a page, a column at a time, as :func:`vectrixdb.extract.layout.reading_order` reads boxes."""
    if not runs:
        return []
    left, right = min(r.left for r in runs), max(r.right for r in runs)
    height = statistics.median(r.bottom - r.top for r in runs) or 1.0
    middle = _gutter(_items(runs), left, right, height) if depth < 3 else None
    if middle is None:
        return _group(runs)
    across = sorted((r for r in runs if r.left < middle < r.right), key=lambda r: r.top)
    rest = [r for r in runs if not (r.left < middle < r.right)]
    edges = [float("-inf")] + [r.middle for r in across] + [float("inf")]
    out: List[Line] = []
    for n in range(len(edges) - 1):
        band = [r for r in rest if edges[n] < r.middle <= edges[n + 1]]
        one = [r for r in band if r.right <= middle]
        two = [r for r in band if r.left >= middle]
        figures = lambda side: sum(1 for r in side if _numeric(r.text)) >= 0.4 * len(side)  # noqa: E731
        if (
            one
            and two
            and not figures(one)
            and not figures(two)
            and _prose(_lines(_items(one)), middle - left)
            and _prose(_lines(_items(two)), right - middle)
        ):
            out += _order(one, depth + 1) + _order(two, depth + 1)
        else:
            out += _group(band)
        if n < len(across):
            out += _group([across[n]])
    return out


def _join_runs(runs: Sequence[Run], size: float) -> str:
    """A line's runs as words: close runs are one word, a raised small one is a footnote mark."""
    out = ""
    base = max(
        (r.bottom for r in runs if abs(r.size - size) <= 0.5), default=max(r.bottom for r in runs)
    )
    previous: Optional[Run] = None
    for run in runs:
        words = run.text.strip()
        if not words:
            continue
        raised = (
            size > 0
            and run.size < 0.9 * size
            and run.bottom < base - 0.2 * size
            and len(words) <= 4
        )
        if raised and re.fullmatch(r"[0-9a-z*†‡,]+", words):
            # Raised after a word, the mark that points at a footnote; raised
            # at the start of a line, the footnote's own number.
            out += "".join(f"[^{mark}]" for mark in words.split(",") if mark)
            previous = run
            continue
        if not out:
            out = words
        elif (
            previous is not None
            and run.left - previous.right < 0.12 * size
            and not out.endswith(" ")
        ):
            out += words
        else:
            out += " " + words
        previous = run
    return out


def _figure(text: str) -> bool:
    """A whole figure, "$ 170,594" or "(12)", and not a lone sign, currency mark or dash."""
    return _numeric(text) and any(c.isdigit() for c in text)


def _cells(line: Line) -> List[Tuple[float, float, str]]:
    """A line cut where the space between two runs is wide: ``(left, right, text)`` a cell.

    Two whole figures with a space between them are two cells however
    narrow the space: a ten-year table sets its columns a figure's height
    apart, and no figure is written "$ 170,594 $ 30,446".
    """
    size = line.size or 1.0
    cells: List[List[Run]] = []
    for run in line.runs:
        if not run.text.strip():
            continue
        if cells:
            last = cells[-1][-1]
            gap = run.left - last.right
            if gap < CELL_GAP * size and not (
                gap >= 0.3 * min(run.size, last.size, size)
                and _figure(run.text)
                and _figure(last.text)
            ):
                cells[-1].append(run)
                continue
        cells.append([run])
    return [(c[0].left, c[-1].right, _join_runs(c, size)) for c in cells]


def _numeric(text: str) -> bool:
    return bool(_NUMBER.match(text.replace(" ", " ").strip()))


# ============================================================================
# RUNNING LINES: what is printed in the margin of most pages
# ============================================================================
#
# INPUT   every page's lines, and each page's height
# OUTPUT  the same lines without the running heads and feet, and which went


def _margin_shape(text: str) -> str:
    """What makes two lines in a margin the same running line: their words, whatever their numbers.

    In the body a line that differs only by a number can be a list; in the
    margin it is the page's own number beside the same title, however long
    the title is, "TD Annual Report 2025 Management's Discussion 37" on one
    page and "38 TD Annual Report 2025 ..." on the next.
    """
    flat = re.sub(r"\d+", "#", " ".join(text.lower().split()))
    return re.sub(r"^[\W_#]+|[\W_#]+$", "", flat) or "#"


def _shared(one: str, two: str) -> int:
    """How many characters two lines share at their start or at their end."""
    start = 0
    while start < min(len(one), len(two)) and one[start] == two[start]:
        start += 1
    end = 0
    while end < min(len(one), len(two)) and one[-1 - end] == two[-1 - end]:
        end += 1
    return max(start, end)


def _drop_running(pages: List[List[Line]], heights: Sequence[float]) -> List[str]:
    written = [i for i, lines in enumerate(pages) if lines]
    if len(written) < 2:
        return []
    # On a document of two pages a running line is in the margin of both; a
    # line that repeats lower down is believed only on three pages or more.
    needed = min(len(written), max(3, math.ceil(RUNNING_SHARE * len(written))))
    needed_exact = max(3, math.ceil(RUNNING_SHARE * len(written)))
    seen: Dict[str, Set[int]] = {}
    exact: Dict[str, Set[int]] = {}
    first: Dict[str, str] = {}
    for index in written:
        height = heights[index]
        if len(pages[index]) < 2:
            # A page's only line runs over nothing.
            continue
        for line in pages[index]:
            text = line.text.strip()
            if not text or len(text) > 120 or line.kind == "figure":
                continue
            inner = line.bottom <= MARGIN * height or line.top >= (1 - MARGIN) * height
            outer = line.bottom <= MARGIN_EXACT * height or line.top >= (1 - MARGIN_EXACT) * height
            if inner:
                shape = _margin_shape(text)
                seen.setdefault(shape, set()).add(index)
                first.setdefault(shape, text)
            if outer:
                flat = " ".join(text.lower().split())
                exact.setdefault(flat, set()).add(index)
                first.setdefault(flat, text)
    # A figure alone is a page number only when this document could have a page with that number.
    # A running line has words: "2025 2024" at the top of a table is its header, whatever it repeats.
    worded_line = lambda key: sum(c.isalpha() for c in key) >= 3  # noqa: E731
    going = {
        shape
        for shape, on in seen.items()
        if len(on) >= needed and shape != "#" and worded_line(shape)
    }
    most = 2 * len(pages) + 20
    going_exact = {
        flat for flat, on in exact.items() if len(on) >= needed_exact and worded_line(flat)
    }

    # A report prints each section's own foot, "Annual Report 2025 Financial
    # Results" on some pages and "Annual Report 2025 Management's Discussion"
    # on others. One that shares a long stretch of words with a running line
    # already found is running too, on as few as three pages.
    def kin(shape: str) -> bool:
        return any(_shared(shape, known) >= 16 for known in going)

    going |= {
        shape
        for shape, on in seen.items()
        if len(on) >= 3 and shape not in going and worded_line(shape) and kin(shape)
    }
    dropped: List[str] = []
    for index in written:
        height = heights[index]
        kept: List[Line] = []
        for line in pages[index]:
            if line.kind == "figure":
                kept.append(line)
                continue
            text = line.text.strip()
            inner = line.bottom <= MARGIN * height or line.top >= (1 - MARGIN) * height
            outer = line.bottom <= MARGIN_EXACT * height or line.top >= (1 - MARGIN_EXACT) * height
            shape: Optional[str] = _margin_shape(text) if inner else None  # type: ignore[no-redef]
            flat: Optional[str] = " ".join(text.lower().split()) if outer else None  # type: ignore[no-redef]
            numbered = shape == "#" and text.strip().isdigit() and int(text.strip()) <= most
            worded = shape != "#" and bool(_PAGE_NUMBER.match(shape or ""))
            if inner and (shape in going or numbered or worded):
                key = shape
            elif outer and flat in going_exact:
                key = flat
            else:
                kept.append(line)
                continue
            said = "page numbers" if numbered and shape not in going else first.get(key or "", text)
            if said not in dropped:
                dropped.append(said)
        pages[index] = kept
    return dropped


# ============================================================================
# TABLES: rows whose figures line up, and the header over them
# ============================================================================
#
# INPUT   a page's lines in reading order
# OUTPUT  the same lines, a table's rows marked as rows with their cells in
#         columns, and its header lines folded into those columns


def _row_like(line: Line) -> bool:
    cells = _cells(line)
    line.cells = cells
    if cells and all(_DATEISH.match(text.strip()) for _l, _r, text in cells):
        return False  # "2025 2024": the years over a table's columns
    # "2025 2024 Change": a year is a figure only in a row that has other
    # figures; among words alone it heads a column.
    figures = sum(
        1 for _l, _r, text in cells if _numeric(text) and not _DATEISH.match(text.strip())
    )
    if figures == 0:
        figures = sum(1 for _l, _r, text in cells if _numeric(text))
        if figures and any(not _numeric(text) for _l, _r, text in cells):
            return False
    return (
        len(cells) >= 2
        and figures >= 1
        and (figures >= 2 or len(cells) >= 3 or _numeric(cells[-1][2]))
    )


def _anchors(rows: Sequence[Line], size: float) -> List[Tuple[float, float]]:
    """Each column of figures as ``(left, right)``: the figures of the rows, merged where they overlap.

    Figures in one column overlap from row to row whether the column sets
    them flush right, flush left or centred; two columns do not, or a reader
    could not tell them apart either.
    """
    spans = sorted(
        (left, right) for line in rows for left, right, text in line.cells if _numeric(text)
    )
    columns: List[List[float]] = []
    for left, right in spans:
        if columns and left <= columns[-1][1] + 0.5:
            columns[-1][1] = max(columns[-1][1], right)
        else:
            columns.append([left, right])
    return [(left, right) for left, right in columns]


def _column_of(
    left: float, right: float, anchors: Sequence[Tuple[float, float]], size: float
) -> Optional[int]:
    """The column a cell sits in: the one it overlaps most, else the nearest within a character or two."""
    best, most = None, 0.0
    for index, (a_left, a_right) in enumerate(anchors):
        shared = min(right, a_right) - max(left, a_left)
        if shared > most:
            best, most = index, shared
    if best is not None:
        return best
    middle = (left + right) / 2
    nearest = min(
        range(len(anchors)),
        key=lambda i: abs((anchors[i][0] + anchors[i][1]) / 2 - middle),
        default=None,
    )
    if (
        nearest is not None
        and abs((anchors[nearest][0] + anchors[nearest][1]) / 2 - middle)
        <= max(6.0, 2.0 * size) + (anchors[nearest][1] - anchors[nearest][0]) / 2
    ):
        return nearest
    return None


def _place_header(
    cells: Sequence[Tuple[float, float, str]],
    spans: Sequence[Tuple[float, float]],
    anchors: Sequence[Tuple[float, float]],
    size: float,
    groups: bool = True,
) -> List[Tuple[int, str]]:
    """Which columns each cell of a header line heads: ``(column, text)`` pairs.

    A line with a cell for every column heads each column with the cell over
    it. A line with fewer cells than columns names groups of columns: a year
    printed once over the quarters of that year, when a line below it already
    heads the columns one by one; without such a line a lone "2024" over one
    column heads that column and no other. Such a cell heads every
    column from the one after the previous cell's group up to its own: a
    cell set flush right over a column ends its group there, and a cell set
    over the middle of its columns reaches halfway to its neighbours.
    """
    over: List[List[int]] = []
    for left, right, _text in cells:
        over.append(
            [i for i, (a_left, a_right) in enumerate(spans) if left <= a_right and right >= a_left]
        )
    if not groups or len(cells) >= len(spans):
        return [(i, text) for (left, right, text), columns in zip(cells, over) for i in columns]
    centres = [(a_left + a_right) / 2 for a_left, a_right in anchors]
    # A column's name wrapped over several lines, "Computer" over "equipment":
    # each word sits over its one column and no wider, and heads that column
    # alone. A year or a date over one column may still name a group of them.
    if all(
        len(columns) == 1
        and right - left <= spans[columns[0]][1] - spans[columns[0]][0] + size
        and not _DATEISH.match(text)
        for (left, right, text), columns in zip(cells, over)
    ):
        return [(columns[0], text) for (_l, _r, text), columns in zip(cells, over)]
    # A group's label centred between its columns may sit over none of them.
    flush = [
        bool(columns) and abs(right - anchors[max(columns)][1]) <= max(3.0, 0.5 * size)
        for (left, right, _t), columns in zip(cells, over)
    ]
    if not all(flush):
        # Set over the middle of its columns: each column goes to the label nearest it.
        middles = [(left + right) / 2 for left, right, _t in cells]
        return [
            (i, cells[min(range(len(cells)), key=lambda n: abs(middles[n] - c))][2])
            for i, c in enumerate(centres)
        ]
    out: List[Tuple[int, str]] = []
    start = 0
    for n, ((left, right, text), columns) in enumerate(zip(cells, over)):
        last = max(columns)
        flush_right = abs(right - anchors[last][1]) <= max(3.0, 0.5 * size)
        if n + 1 < len(cells):
            if flush_right:
                end = last
            else:
                boundary = (right + cells[n + 1][0]) / 2
                end = max([i for i, c in enumerate(centres) if c < boundary] + [last])
        else:
            end = len(spans) - 1
        out += [(i, text) for i in range(start, end + 1)]
        start = end + 1
    return out


def _headers(
    lines: List[Line],
    start: int,
    anchors: Sequence[Tuple[float, float]],
    label_right: float,
    size: float,
) -> Tuple[int, List[str]]:
    """The lines just above a table that head its columns, as one header per column.

    A line counts when every one of its cells sits over the columns of
    figures, and it holds words, years or dates rather than figures. A cell
    over several columns, "For the three months ended", heads each of them.

    The lines are read by where they sit, not as the file wrote them: a
    header stacked two or three words deep is often written a column at a
    time, "Land" and "Buildings" on one line of the file, "Computer" and
    "equipment" on the next, and read as the file wrote it each column's
    words would be a line of its own over every column.
    """
    heads: List[List[Tuple[float, str]]] = [[] for _ in anchors]
    spans = [(a_left - 0.5 * size, a_right + 0.5 * size) for a_left, a_right in anchors]
    above: List[int] = []
    # Up to thirty of the file's lines, since a header three words deep over
    # six columns is written as a dozen or more; the lines over the table end
    # where a line sits beside the columns rather than over them.
    for back in range(start - 1, max(start - 30, -1), -1):
        line = lines[back]
        if line.kind != "text" or not (line.cells or _cells(line)):
            break
        above.append(back)
    if not above:
        return start, [""] * len(anchors)
    placed: Set[int] = set()
    finest = 0
    labels = 0
    for line in reversed(_group([run for back in above for run in lines[back].runs])):
        cells = _cells(line)
        if not cells:
            break
        beside = [
            (left, right, text)
            for left, right, text in cells
            if left < label_right - 0.5 * size or right <= label_right + 0.5 * size
        ]
        if beside:
            # "Assets" between the header and the rows is a label of the rows
            # under it, and "(millions of dollars)" beside the header's first
            # line says what the figures are in: look past one or two.
            if (
                len(beside) == 1
                and labels < 2
                and len(beside[0][2]) <= 40
                and not _numeric(beside[0][2])
            ):
                labels += 1
                cells = [cell for cell in cells if cell not in beside]
                if not cells:
                    continue
            else:
                break
        if any(_numeric(text) and not _DATEISH.match(text) for _l, _r, text in cells):
            break
        found = _place_header(cells, spans, anchors, size, groups=finest > len(cells))
        if not found:
            break
        for index, text in found:
            heads[index].append((line.top, text))
        finest = max(finest, len(cells))
        placed.update(
            id(run)
            for run in line.runs
            if run.text.strip() and (run.left, run.right) not in {(l, r) for l, r, _t in beside}
        )
    first = start
    for back in above:
        line = lines[back]
        if any(id(run) in placed for run in line.runs if run.text.strip()):
            line.kind = "header"
            first = back
    return first, [
        " ".join(text for _top, text in sorted(parts, key=lambda part: part[0])) for parts in heads
    ]


def _tables(lines: List[Line]) -> int:
    """Mark the tables on a page; how many were found."""
    found = 0
    index = 0
    while index < len(lines):
        if lines[index].kind != "text" or not _row_like(lines[index]):
            index += 1
            continue
        end = index
        while (
            end + 1 < len(lines)
            and lines[end + 1].kind == "text"
            and (
                _row_like(lines[end + 1])
                # A label alone between rows, "Average earning assets", belongs to the table.
                or (
                    end + 2 < len(lines)
                    and len(_cells(lines[end + 1])) == 1
                    and len(lines[end + 1].text) <= 60
                    and _row_like(lines[end + 2])
                )
            )
        ):
            end += 1
        block = lines[index : end + 1]
        rows = [line for line in block if len(line.cells or _cells(line)) >= 2]
        if len(rows) < 2:
            index = end + 1
            continue
        size = statistics.median(line.size for line in rows) or 10.0
        anchors = _anchors(rows, size)
        if len(anchors) < 1:
            index = end + 1
            continue
        first_column = min(a_left for a_left, _a in anchors)
        label_right = max(
            (
                right
                for line in rows
                for left, right, text in line.cells
                if right < first_column - 0.5 * size
            ),
            default=first_column - size,
        )
        top, heads = _headers(lines, index, anchors, label_right, size)
        for line in block:
            cells = line.cells or _cells(line)
            values: List[Optional[str]] = [None] * (len(anchors) + 1)
            for left, right, text in cells:
                column = (
                    None
                    if right < first_column - 0.5 * size
                    else _column_of(left, right, anchors, size)
                )
                if column is None:
                    values[0] = text if values[0] is None else f"{values[0]} {text}"
                else:
                    values[column + 1] = (
                        text if values[column + 1] is None else f"{values[column + 1]} {text}"
                    )
            line.kind = "row"
            line.cells = [(0.0, 0.0, "" if v is None else v) for v in values]
        lines[index].level = 1  # the first row of a table carries its header
        lines[index].cells.append((-1.0, -1.0, "\x1f".join([""] + heads)))
        found += 1
        index = end + 1
    return found


# ============================================================================
# HEADINGS, PARAGRAPHS: the page written out
# ============================================================================
#
# INPUT   a page's lines, the document's body size, its heading sizes, and the
#         words it uses
# OUTPUT  the page as text: headings as Markdown lines, paragraphs joined,
#         tables as one row a line


def _body_size(pages: Iterable[List[Line]]) -> float:
    weights: Counter = Counter()
    for lines in pages:
        for line in lines:
            if line.kind != "figure":
                weights[line.size] += len(line.text)
    return weights.most_common(1)[0][0] if weights else 10.0


#: "1. ", "2.1 ", "III. ", "Chapter 4", "Part B", "Section 2": a line that opens a section of its own.
_NUMBERED_HEADING = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?|[IVXLC]+\.|[A-Z]\.|(?:chapter|part|section|appendix|article)\s+\S+)\s+\S",
    re.IGNORECASE,
)


def _aligned(one: Line, two: Line) -> bool:
    """Whether two lines share a left edge, a right edge or a centre, within a character."""
    within = max(one.size, two.size, 1.0)
    return (
        abs(one.left - two.left) <= within
        or abs(one.right - two.right) <= within
        or abs((one.left + one.right) - (two.left + two.right)) <= 2 * within
    )


def _heading_like(line: Line, body: float) -> bool:
    text = line.text.strip()
    if line.kind != "text" or not text or len(text) > 120 or not re.search(r"[A-Za-zÀ-ɏ]", text):
        return False
    if text.endswith((".", ",", ";")) and len(text.split()) > 3:
        return False
    if line.size >= HEADING_SCALE * body:
        return True
    return (
        line.bold
        and line.size >= 0.95 * body
        and len(text.split()) <= 12
        and text[:1].isupper() is not False
    )


def _mark_headings(pages: List[List[Line]], body: float) -> None:
    sizes = sorted(
        {
            line.size
            for lines in pages
            for line in lines
            if _heading_like(line, body) and line.size >= HEADING_SCALE * body
        },
        reverse=True,
    )
    rank = {size: min(n + 1, 3) for n, size in enumerate(sizes)}
    for lines in pages:
        for n, line in enumerate(lines):
            if not _heading_like(line, body):
                continue
            if line.size < HEADING_SCALE * body:
                # Bold at body size is a heading only when it stands alone.
                before = lines[n - 1] if n else None
                after = lines[n + 1] if n + 1 < len(lines) else None
                gap_before = (
                    before is None or line.top - before.bottom > 0.5 * body or before.kind != "text"
                )
                ends = after is None or after.top - line.bottom > 0.3 * body or not after.bold
                if not (gap_before and ends) or (
                    after is not None
                    and after.bold
                    and after.size == line.size
                    and abs(after.top - line.bottom) < 0.5 * body
                ):
                    continue
                if (
                    before is not None
                    and before.kind == "text"
                    and not _SENTENCE_END.search(before.text.strip())
                    and line.top - before.bottom < 0.5 * body
                ):
                    continue
                if after is not None and after.kind == "text" and after.text.strip()[:1].islower():
                    continue
                line.level = min(len(rank) + 1, 4)
            else:
                line.level = rank.get(line.size, 3)
            line.kind = "heading"


def _vocabulary(pages: Iterable[List[Line]]) -> Set[str]:
    """The words the document uses, the two halves of a word a line broke left out.

    "obliga-" at the end of a line and "tions" at the start of the next are
    not words, and counting them made every broken word look like a compound
    the document writes with its hyphen.
    """
    words: Set[str] = set()
    for lines in pages:
        after_break = False
        for line in lines:
            if line.kind == "figure":
                continue
            text = line.text
            found = re.findall(r"[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'-]+", text)
            broken = text.rstrip().endswith(_SOFT + ("-",)) and len(text.rstrip()) > 1
            if broken and found:
                found = found[:-1]
            if after_break and found:
                found = found[1:]
            words.update(w.lower() for w in found)
            after_break = broken
    return words


def _hyphen_join(head: str, tail: str, words: Set[str]) -> str:
    """Two halves of a word the line broke: joined, with the hyphen only when the document spells it so."""
    soft = head.endswith("­")
    stem = head.rstrip("".join(_SOFT)).rstrip("-")
    first = re.search(r"([A-Za-zÀ-ɏ]+)$", stem)
    rest = re.match(r"([A-Za-zÀ-ɏ]+)", tail)
    if soft or not (first and rest):
        return stem + tail
    joined = (first.group(1) + rest.group(1)).lower()
    hyphened = f"{first.group(1)}-{rest.group(1)}".lower()
    if joined in words:
        return stem + tail
    if hyphened in words:
        return stem + "-" + tail
    # A printer breaks a word between syllables, which are rarely words:
    # "re-sults", "interna-tional". Two halves that are both words the
    # document uses are a compound written with its hyphen: "industry-leading".
    head_word, tail_word = first.group(1).lower(), rest.group(1).lower()
    if len(head_word) >= 3 and len(tail_word) >= 3 and head_word in words and tail_word in words:
        return stem + "-" + tail
    return stem + tail


def _leading(lines: Sequence[Line]) -> float:
    """How far one line of text sits below the one before, as the page sets most of them; 0 when it cannot tell."""
    advances = [
        b.top - a.top
        for a, b in zip(lines, lines[1:])
        if a.kind == "text"
        and b.kind == "text"
        and abs(a.size - b.size) <= 0.5
        and 0.8 * a.size < b.top - a.top < 3.0 * a.size
    ]
    if len(advances) < 2:
        return 0.0
    return statistics.median(advances)


def _page_text(lines: Sequence[Line], body: float, words: Set[str]) -> str:
    """A page's lines as text: headings on their own, paragraphs joined, a table a row a line."""
    blocks: List[str] = []
    paragraph = ""
    previous: Optional[Line] = None
    headers: List[str] = []
    # The left edge of the list item the paragraph is, when it is one: its
    # lines hang in from the marker, and a line back at the marker's edge,
    # or a new marker, ends it.
    item_left: Optional[float] = None
    # Whether the block just closed was a list item: a bare number or a dash
    # opens another only then.
    in_list = False

    def close() -> None:
        nonlocal paragraph, item_left, in_list
        if paragraph.strip():
            blocks.append(paragraph.strip())
            in_list = item_left is not None
        paragraph = ""
        item_left = None

    table: List[List[str]] = []
    leading = _leading(lines)

    def flush_table() -> None:
        nonlocal table, headers
        if table:
            written = []
            for row in table:
                parts = [row[0]] if row and row[0] else []
                for column, value in enumerate(row[1:], start=1):
                    if not value:
                        continue
                    name = headers[column] if column < len(headers) else ""
                    parts.append(f"{name}: {value}" if name else value)
                if parts:
                    written.append("; ".join(parts))
            if written:
                blocks.append("\n".join(written))
        table, headers = [], []

    for line in lines:
        if line.kind == "figure":
            # A chart drawn as a picture, where it sits, with the words it
            # prints on the line under it: a block of its own.
            close()
            flush_table()
            blocks.append(line.runs[0].text)
            previous = line
            continue
        text = line.text.strip()
        if not text or line.kind == "header":
            continue
        if line.kind == "row":
            close()
            cells = [c[2] for c in line.cells if c[0] >= 0]
            carried = [c[2] for c in line.cells if c[0] < 0]
            if carried:
                flush_table()
                headers = carried[0].split("\x1f")
            last = table[-1] if table else None
            if last is not None and not any(last[1:]) and cells and cells[0][:1].islower():
                # A label too long for its column, carried onto the next line.
                cells = [f"{last[0]} {cells[0]}".strip()] + cells[1:]
                table[-1] = cells
                previous = line
                continue
            table.append(cells)
            previous = line
            continue
        flush_table()
        if line.kind == "heading":
            close()
            if (
                previous is not None
                and previous.kind == "heading"
                and previous.level == line.level
                and abs(previous.size - line.size) <= 0.5
                and 0 <= line.top - previous.bottom < 0.8 * max(line.size, 1.0)
                and _aligned(previous, line)
                and not _NUMBERED_HEADING.match(text)
                and blocks
            ):
                # One heading set over several lines is one heading: its lines
                # share an edge or a centre, and the second does not open a
                # numbered section of its own under a title.
                blocks[-1] += " " + text
            else:
                blocks.append("#" * max(1, line.level) + " " + text)
            previous = line
            continue
        marked = _LIST_MARK.match(text)
        if marked and _weak(marked.group("mark")) and not (item_left is not None or in_list):
            # In prose a line can start with "12 markets" or "– which": a bare
            # number or a dash opens an item only after another item.
            marked = None
        if marked and paragraph:
            # A list item is a block of its own, whatever the spacing.
            close()
        if previous is not None and previous.kind == "text" and paragraph:
            if leading and abs(line.size - previous.size) <= 0.5:
                # Measured from the tops of the lines, which a line's letters
                # do not move: a line with no descender ends higher, and a
                # gap measured from its bottom opened a paragraph mid-sentence.
                advance = line.top - previous.top
                gap = advance - leading if advance > leading * 1.3 + 1.0 else 0.0
            else:
                gap = line.top - previous.bottom
            moved_up = line.top < previous.top - 0.5 * body
            indented = line.left > previous.left + 1.2 * body and not moved_up
            if item_left is not None:
                # The lines of a list item hang in from its marker; one back
                # at the marker's edge is the paragraph after the list.
                indented = False
                if line.left < item_left + 0.5 * body:
                    close()
            short_before = _SENTENCE_END.search(
                paragraph
            ) is not None and previous.right < line.right - 0.15 * max(line.right - line.left, 1.0)
            new_size = abs(line.size - previous.size) > 0.15 * max(previous.size, 1.0)
            runs_on = moved_up and not _SENTENCE_END.search(paragraph) and text[:1].islower()
            if not runs_on and (
                gap > (0.0 if leading else 0.6 * max(body, previous.size))
                or moved_up
                or indented
                or short_before
                or new_size
            ):
                close()
        if marked and not paragraph:
            item_left = line.left
            if marked.group("mark") in _BULLETS:
                text = "- " + text[marked.end() :]
        if paragraph:
            ends_hyphen = (
                paragraph.endswith(("-", "", "￾"))
                and len(paragraph) > 1
                and paragraph[-2].isalpha()
                and text[:1].islower()
            )
            if paragraph.endswith("­") or ends_hyphen:
                paragraph = _hyphen_join(paragraph, text, words)
            else:
                paragraph += " " + text
        else:
            paragraph = text
        previous = line
    close()
    flush_table()
    joined = "\n\n".join(blocks)
    for mark in _SOFT:
        joined = joined.replace(mark, "")
    return joined


# ============================================================================
# CHARTS: what a PDF draws rather than writes
# ============================================================================
#
# INPUT   a page, its size and its runs
# OUTPUT  the regions where it draws a chart: shapes gathered together, with
#         figures by them; each drawn as a picture
#
# A chart drawn with vectors has no picture to hand a describer and no
# numbers in its text: bars are rectangles, and their values are only their
# heights. Drawn as a picture, a model that can see reads it as a person does.


Box = Tuple[float, float, float, float]


def _chart_regions(
    page: Any, raw: Any, width: float, height: float, runs: Sequence[Run]
) -> List[Box]:
    """``(left, top, right, bottom)`` of each chart on a page, labels included."""
    shapes: List[Box] = []
    plotted: Set[int] = set()
    try:
        objects = page.get_objects(filter=(raw.FPDF_PAGEOBJ_PATH,), max_depth=4)
        for obj in objects:
            left, bottom, right, top = (
                obj.get_bounds() if hasattr(obj, "get_bounds") else obj.get_pos()
            )
            w, h = right - left, top - bottom
            if w < 3 or h < 3 or w * h > 0.6 * width * height:
                continue  # a rule, a tick, or the page's background
            if h <= 5 and w >= 8 * h:
                continue  # a table's rule under a total, drawn double
            if w >= 60 and _plotted(raw, obj.raw):
                plotted.add(len(shapes))
            shapes.append((left, height - top, right, height - bottom))
    except Exception:  # noqa: BLE001 - a page whose shapes will not list draws no chart we can find
        return []
    if len(shapes) < 4 and not plotted:
        return []
    found: List[Box] = []
    for members in _gather(shapes, runs):
        group = [shapes[i] for i in members]
        lines = [shapes[i] for i in members if i in plotted]
        drawn = (
            min(b[0] for b in group),
            min(b[1] for b in group),
            max(b[2] for b in group),
            max(b[3] for b in group),
        )
        # A chart in a panel of its own holds its scales in the panel.
        framed = _frame(group, drawn, lines) is not None
        left, top, right, bottom = (
            _scales(drawn, runs) if not framed and (len(group) >= 4 or lines) else drawn
        )
        area = (right - left) * (bottom - top)
        if (
            (len(group) < 4 and not lines)
            or area < 0.02 * width * height
            or area > 0.85 * width * height
        ):
            continue
        edges = Counter((round(b[0]), round(b[2])) for b in group)
        if not lines and edges.most_common(1)[0][1] >= 0.8 * len(group):
            continue  # shaded rows of a table, not bars
        near = [
            r
            for r in runs
            if r.left >= left - 24
            and r.right <= right + 24
            and r.top >= top - 24
            and r.bottom <= bottom + 24
        ]
        figures = sum(1 for r in near if _numeric(r.text))
        if figures < (2 if len(group) >= 4 else 3) and len(group) < 8:
            continue
        region = _chart_bounds(group, (left, top, right, bottom), runs, width, height, lines)
        if not _words_not_chart(region, runs):
            found.append(region)
    return _merged(found)


def _merged(regions: Sequence[Box]) -> List[Box]:
    """Regions that overlap as one: a diagram in bands, each band found apart, is one picture."""
    merged = [list(r) for r in regions]
    joined = True
    while joined:
        joined = False
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                a, b = merged[i], merged[j]
                across, down = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
                smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
                if across > 0 and down > 0 and across * down >= 0.05 * smaller:
                    merged[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del merged[j]
                    joined = True
                    break
            if joined:
                break
    return sorted((tuple(r) for r in merged), key=lambda r: (r[1], r[0]))  # type: ignore[misc]


def _scales(box: Box, runs: Sequence[Run]) -> Box:
    """A chart's box widened to the scales printed along it.

    Its marks reach only as far as its values: a line may never come near
    its axis, and the top tick stands above the tallest bar. Its scales mark
    the whole plot. A column of three figures or more at even steps beside
    it is its value axis, and the plot runs from its first figure to its
    last; a row of three short labels or more under it is its category
    axis, years or dates or names.
    """
    left, top, right, bottom = box
    tall = bottom - top
    ticks: Set[int] = set()
    for side in (-1, 1):
        if side < 0:
            near = [r for r in runs if _numeric(r.text) and left - 80 <= r.right <= left + 4]
        else:
            near = [r for r in runs if _numeric(r.text) and right - 4 <= r.left <= right + 80]
        near = [r for r in near if r.bottom >= top - tall and r.top <= bottom + tall]
        best: List[Run] = []
        # Set flush right against the plot, flush left, or centred.
        for edge in (lambda r: r.right, lambda r: r.left, lambda r: (r.left + r.right) / 2):
            for column in _clusters(near, edge, 3.0):
                column = sorted(column, key=lambda r: r.middle)
                steps = [b.middle - a.middle for a, b in zip(column, column[1:])]
                if (
                    len(column) >= 3
                    and min(steps) > 2
                    and max(steps) - min(steps) <= 0.25 * statistics.median(steps)
                ):
                    best = max(best, column, key=len)
        if best:
            ticks.update(id(r) for r in best)
            top, bottom = min(top, best[0].top), max(bottom, best[-1].bottom)
            left, right = (
                (min(left, *(r.left for r in best)), right)
                if side < 0
                else (left, max(right, *(r.right for r in best)))
            )
    under = [
        r
        for r in runs
        if id(r) not in ticks
        and bottom - 4 <= r.top <= bottom + 60
        and r.right >= left
        and r.left <= right
        and len(r.text.strip()) <= 20
    ]
    rows = sorted(
        _clusters(under, lambda r: r.middle, 2.0), key=lambda row: min(r.top for r in row)
    )
    if rows and len(rows[0]) >= 3:
        bottom = max(
            bottom, *(r.bottom for r in rows[0])
        )  # the row nearest under it, and only that one
    return (left, top, right, bottom)


def _clusters(runs: Sequence[Run], key: Callable[[Run], float], within: float) -> List[List[Run]]:
    """Runs in groups whose ``key`` lies within ``within`` of the group's first."""
    groups: List[List[Run]] = []
    for r in sorted(runs, key=key):
        if groups and key(r) - key(groups[-1][0]) <= within:
            groups[-1].append(r)
        else:
            groups.append([r])
    return groups


def _plotted(raw: Any, handle: Any) -> bool:
    """Whether a path runs through its points from left to right, as a line chart's line does.

    A line chart draws each series as one path through its values in order,
    so the path moves one way across the page and rises and falls as it goes.
    A logo or an icon turns back on itself; a table's rules are straight and
    start over at every rule. An area chart's shape runs out along its values
    and back along its base, so two turns are allowed.
    """
    count = raw.FPDFPath_CountSegments(handle)
    if count < 6 or count > 50000:
        return False
    x, y = ctypes.c_float(), ctypes.c_float()
    moves = turns = sloped = heading = 0
    last: Optional[Tuple[float, float]] = None
    for index in range(count):
        segment = raw.FPDFPath_GetPathSegment(handle, index)
        if not segment or not raw.FPDFPathSegment_GetPoint(
            segment, ctypes.byref(x), ctypes.byref(y)
        ):
            return False
        point = (x.value, y.value)
        if raw.FPDFPathSegment_GetType(segment) == raw.FPDF_SEGMENT_MOVETO:
            moves += 1
            if moves > 3:
                return False
        elif last is not None:
            dx, dy = point[0] - last[0], point[1] - last[1]
            sloped += abs(dy) > 0.01
            step = 1 if dx > 0.5 else -1 if dx < -0.5 else 0
            if step and heading and step != heading:
                turns += 1
                if turns > 2:
                    return False
            heading = step or heading
        last = point
    return sloped >= 0.3 * (count - moves)


def _gather(shapes: Sequence[Box], runs: Sequence[Run]) -> List[List[int]]:
    """The shapes of a page in groups that belong together, as indexes.

    Shapes within a few points of each other are one group: the bars of a
    stack, a panel and what it holds. Bars also stand on one line with room
    between them and never touch, so shapes of one width standing on one
    edge are one group too, across gaps up to three bars wide; and the same
    turned on its side for bars that run across. A shape that holds more
    than one run of text is a box of words, not a bar, and joins nothing
    that way.
    """
    parent = list(range(len(shapes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    order = sorted(range(len(shapes)), key=lambda i: shapes[i][0])
    for at, i in enumerate(order):
        _, top, right, bottom = shapes[i]
        for j in order[at + 1 :]:
            if shapes[j][0] > right + 8:
                break
            if shapes[j][1] <= bottom + 8 and shapes[j][3] >= top - 8:
                join(i, j)

    bare: Dict[int, bool] = {}

    def bar(i: int) -> bool:
        if i not in bare:
            left, top, right, bottom = shapes[i]
            inside = sum(
                1
                for r in runs
                if r.left >= left - 1
                and r.right <= right + 1
                and r.top >= top - 1
                and r.bottom <= bottom + 1
            )
            bare[i] = inside <= 1  # a value printed on a bar, at most
        return bare[i]

    for standing in (True, False):
        # Standing bars share a top or a bottom and are laid out left to
        # right; bars that run across share a left or a right edge.
        on: Dict[Tuple[int, int], Set[int]] = {}
        for i, (left, top, right, bottom) in enumerate(shapes):
            if not standing and right - left < bottom - top:
                continue  # a bar that runs across is longer than it is thick
            for edge in (top, bottom) if standing else (left, right):
                for offset in (0, 1):  # two grids half a point apart: edges a hair apart share one
                    on.setdefault((math.floor(edge + offset / 2), offset), set()).add(i)
        along, start, end = (0, 0, 2) if standing else (1, 1, 3)
        for members in on.values():
            if len(members) < 2:
                continue
            ordered = sorted(members, key=lambda i: shapes[i][along])
            for i, j in zip(ordered, ordered[1:]):
                size_i, size_j = (
                    shapes[i][end] - shapes[i][start],
                    shapes[j][end] - shapes[j][start],
                )
                gap = shapes[j][start] - shapes[i][end]
                # The room allowed between bars is measured by how thick they
                # are, never how long: two charts one above the other have
                # bars that share a left edge, a chart's height apart.
                thick = max(min(b[2] - b[0], b[3] - b[1]) for b in (shapes[i], shapes[j]))
                if (
                    abs(size_i - size_j) <= 0.25 * max(size_i, size_j)
                    and -1 <= gap <= max(3 * thick, 40)
                    and bar(i)
                    and bar(j)
                ):
                    join(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(len(shapes)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _words_not_chart(region: Box, runs: Sequence[Run]) -> bool:
    """Whether a region is words with shapes about them, or a table, and no chart.

    A chart leaves most of its room to its marks and labels them in a word
    or a figure; text set over a quarter of the region is a page or a column
    of words, and three lines of eight words or more are sentences. Six rows
    that each name what they hold and then give three figures or more are a
    table, which the table reader already reads row by row; the ticks of
    charts side by side on one scale line up in rows too, and name nothing.
    """
    left, top, right, bottom = region
    inside = [
        r
        for r in runs
        if r.left >= left - 1
        and r.right <= right + 1
        and r.top >= top - 1
        and r.bottom <= bottom + 1
    ]
    area = max(1.0, (right - left) * (bottom - top))
    if sum((r.right - r.left) * (r.bottom - r.top) for r in inside) >= 0.24 * area:
        return True
    if sum(1 for r in inside if len(r.text.split()) >= 8) >= 3:
        return True
    rows: List[List[Run]] = []
    for r in sorted(inside, key=lambda r: r.middle):
        if rows and abs(r.middle - rows[-1][0].middle) <= 2:
            rows[-1].append(r)
        else:
            rows.append([r])
    named = 0
    for row in rows:
        figures = [r for r in row if _numeric(r.text)]
        if len(figures) >= 3:
            first = min(r.left for r in figures)
            named += any(
                r.right <= first and not _numeric(r.text) and any(c.isalpha() for c in r.text)
                for r in row
            )
    return named >= 6


def _frame(group: Sequence[Box], box: Box, lines: Sequence[Box] = ()) -> Optional[Box]:
    """The panel or border drawn round a chart: its largest shape, holding two others and nearly all of it.

    A line chart's line spans its plot and is no frame.
    """
    framing = [b for b in group if b not in lines]
    if not framing:
        return None
    frame = max(framing, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
    held = sum(
        1
        for b in group
        if b is not frame
        and b[0] >= frame[0] - 1
        and b[2] <= frame[2] + 1
        and b[1] >= frame[1] - 1
        and b[3] <= frame[3] + 1
    )
    left, top, right, bottom = box
    if held >= 2 and (frame[2] - frame[0]) * (frame[3] - frame[1]) >= 0.9 * (right - left) * (
        bottom - top
    ):
        return frame
    return None


def _chart_bounds(
    group: Sequence[Box],
    box: Box,
    runs: Sequence[Run],
    width: float,
    height: float,
    lines: Sequence[Box] = (),
) -> Box:
    """A chart's shapes widened to its own words, padded and kept on the page."""
    left, top, right, bottom = box
    pad = 8.0
    # A panel or a border drawn round the chart already holds its title and
    # labels: the picture is the panel, and what lies outside it is the page.
    if _frame(group, box, lines) is not None:
        return (
            max(0.0, left - 2),
            max(0.0, top - 2),
            min(width, right + 2),
            min(height, bottom + 2),
        )
    # The chart's own words around its shapes, each measured from the shapes
    # and never from words already taken, so a table or a paragraph under a
    # chart is not drawn into it one line at a time: axis values and end
    # labels beside it, a title and its subtitle above, year labels and a
    # legend below in the labels' own size.
    inside = [
        r.size
        for r in runs
        if r.left >= left - 2
        and r.right <= right + 2
        and r.top >= top - 2
        and r.bottom <= bottom + 2
    ]
    label = sorted(inside)[len(inside) // 2] if inside else 0.0
    grown = [left, top, right, bottom]
    for r in runs:
        if len(r.text.strip()) > 60 or r.right < left - 40 or r.left > right + 40:
            continue
        beside = r.bottom >= top and r.top <= bottom
        above = r.bottom < top and r.bottom >= top - 48 and r.right >= left and r.left <= right
        below = (
            r.top > bottom
            and r.top <= bottom + 30
            and r.right >= left
            and r.left <= right
            and (not label or r.size <= label * 1.25)
        )
        if beside or above or below:
            grown = [
                min(grown[0], r.left),
                min(grown[1], r.top),
                max(grown[2], r.right),
                max(grown[3], r.bottom),
            ]
    # A title set over two or three lines climbs higher: each line sits just
    # above the last one taken, a line's own height or less, and is short
    # and no sentence.
    for _ in range(3):
        over = [
            r
            for r in runs
            if grown[1] - max(8.0, 1.2 * r.size) <= r.bottom <= grown[1] + 1
            and r.top < grown[1] - 1
            and r.right >= left
            and r.left <= right
            and len(r.text.strip()) <= 60
            and len(r.text.split()) < 8
        ]
        if not over:
            break
        grown = [
            min(grown[0], *(r.left for r in over)),
            min(r.top for r in over),
            max(grown[2], *(r.right for r in over)),
            grown[3],
        ]
    left, top, right, bottom = grown
    return (
        max(0.0, left - pad),
        max(0.0, top - pad),
        min(width, right + pad),
        min(height, bottom + pad),
    )


def _draw_region(
    page: Any,
    width: float,
    height: float,
    region: Tuple[float, float, float, float],
    scale: float = 2.0,
) -> Optional[bytes]:
    """One region of a page as a PNG."""
    import io

    left, top, right, bottom = region
    try:
        bitmap = page.render(scale=scale, crop=(left, height - bottom, width - right, top))
        buffer = io.BytesIO()
        bitmap.to_pil().save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 - a region that will not draw is not a chart we can show
        return None


def _figure_in_place(lines: Sequence[Line], region: Box, name: str, height: float) -> List[Line]:
    """A page's lines with a chart's own words taken out, and its figure line where the chart sits.

    What a chart prints, its title, its scales, a value over each bar, is in
    its picture; left in the page's text it is a column of loose figures
    that says nothing, and a reader cutting the text into pieces makes a
    piece of it. The words go on one line under the figure line instead,
    ``Words in the picture: ...``, in the order they are read, so a figure
    nothing describes still says what it printed, and a description takes
    the place of both. A long line inside the region is a sentence and stays.

    The figure line stands where the chart sits, so what is said before and
    after it is what is around the chart: after the last line, in reading
    order, that ends above the chart and runs across it; with none, before
    the first line under its top. Lines in the margins, a running head or
    foot that a file often draws last of all, do not count. A file may draw
    its charts after everything else too, so where the chart's words come in
    the file is only the fallback, and the page's end the last one.
    """
    left, top, right, bottom = region

    def its_own(run: Run) -> bool:
        inside = (
            run.left >= left - 1
            and run.right <= right + 1
            and run.top >= top - 1
            and run.bottom <= bottom + 1
        )
        return inside and len(run.text.strip()) <= 60 and len(run.text.split()) < 8

    kept: List[Line] = []
    said: List[str] = []
    at: Optional[int] = None
    for line in lines:
        if line.kind == "figure":
            kept.append(line)
            continue
        own = [r for r in line.runs if its_own(r)]
        if not own:
            kept.append(line)
            continue
        if at is None:
            at = len(kept)
        words = _join_runs(own, line.size)
        if words:
            said.append(words)
        rest = [r for r in line.runs if not its_own(r)]
        if rest:
            # A new line, not the old one cut down: what it says and how
            # large it is are worked out once and kept.
            kept.append(Line(rest, kind=line.kind, level=line.level))

    def across(line: Line) -> bool:
        body = MARGIN * height < line.bottom and line.top < (1 - MARGIN) * height
        return body and line.right >= left and line.left <= right

    above = [i for i, line in enumerate(kept) if across(line) and line.bottom <= top + 1]
    under = next(
        (
            i
            for i, line in enumerate(kept)
            if line.kind != "figure" and across(line) and line.top >= top - 1
        ),
        None,
    )
    if above:
        at = max(above) + 1
    elif under is not None:
        at = under
    elif at is None:
        at = len(kept)
    block = f"[Figure: {name}]" + (f"\nWords in the picture: {' '.join(said)}" if said else "")
    kept.insert(at, Line([Run(left, top, right, bottom, block, 0.0, False)], kind="figure"))
    return kept


# ============================================================================
# HARD PAGES: where the rules are unsure of what they read
# ============================================================================
#
# INPUT   a page's lines in reading order, its runs, its height, and whether
#         it draws a chart
# OUTPUT  why the page wants reading by something that sees it, or None;
#         the page's own words, for that reading to be held to
#
# The rules are exact on prose and on tables whose figures line up. They are
# unsure where a page is laid out to be looked at: a chart, big figures set
# apart with their labels under them, a file that draws its parts out of
# order, text turned on its side.

#: A figure standing alone on its line: "4.6%", "$2.1 trillion", "169-year", "(35)".
_LONE_FIGURE = re.compile(r"[(\-+]?[$€£¥]?\s?\d[\d,.]*\s?(?:%|x|[A-Za-z]+)?(?:-[A-Za-z]+)?\)?")


def _lone_figure(text: str) -> bool:
    plain = re.sub(r"\[\^[^\]]*\]", "", text).strip()
    return len(plain.split()) <= 3 and bool(_LONE_FIGURE.fullmatch(plain))


def _rotated(runs: Sequence[Run]) -> bool:
    """Text set up the page rather than across it: its letters one over the other, in two places or more."""
    singles = sorted(
        (r for r in runs if len(r.text.strip()) == 1), key=lambda r: (round(r.left), r.top)
    )
    stacks = 0
    stacked = 1
    for a, b in zip(singles, singles[1:]):
        if abs(a.left - b.left) <= 1 and -0.2 * max(a.size, 1.0) <= b.top - a.bottom <= 0.6 * max(
            a.size, 1.0
        ):
            stacked += 1
            if stacked == 4:
                stacks += 1
        else:
            stacked = 1
    return stacks >= 2


def _hard(
    lines: Sequence[Line], runs: Sequence[Run], height: float, charted: bool
) -> Optional[str]:
    """Why a page wants reading by something that sees it: "chart", "tiles", "order" or "rotated"; None when the rules read it well."""
    if charted:
        return "chart"
    body = [line for line in lines if line.text.strip()]
    if not body:
        return None
    sizes = sorted(line.size for line in body)
    typical = sizes[len(sizes) // 2]
    # Figures set large and alone, their labels on other lines: a snapshot's tiles.
    if sum(1 for line in body if line.size >= 1.3 * typical and _lone_figure(line.text)) >= 2:
        return "tiles"
    # A reading that starts at the foot of the page, or goes back up it twice
    # other than to a column beside: the file drew its parts out of order.
    ups = sum(
        1 for a, b in zip(body, body[1:]) if b.top < a.top - 0.2 * height and b.left < a.right - 5
    )
    foot = (1 - MARGIN) * height
    if (body[0].top >= foot and any(line.top < foot for line in body[1:])) or ups >= 2:
        return "order"
    if _rotated(runs):
        return "rotated"
    return None


#: A run that is one figure and nothing else, as a scale prints its ticks: "$8,000", "(20)", "45%".
_TICK_LABEL = re.compile(r"[(\-]?[$€£¥]?\s?\d[\d,]*(?:\.\d+)?\s?%?\)?")


def _tick_value(text: str) -> Optional[float]:
    plain = text.strip()
    if not _TICK_LABEL.fullmatch(plain):
        return None
    try:
        number = float(re.sub(r"[^\d.]", "", plain))
    except ValueError:
        return None
    return -number if plain.startswith(("(", "-")) else number


def _scale_ticks(runs: Sequence[Run]) -> Set[float]:
    """The figures a page prints only on a chart's scale, by value.

    A scale is four figures or more set at even steps up the page or along
    it, going up by a round step from zero: 0, 2,000, 4,000, 6,000. A bar
    that reaches the line marked 6,000 does not have the value 6,000, so a
    reading that writes it as one read it off the bar. A figure the page
    also prints anywhere else is not only a tick, and is left out.
    """
    from .page_reader import numbers_in, round_step

    labelled = [(r, v) for r in runs if (v := _tick_value(r.text)) is not None]
    if len(labelled) < 4:
        return set()
    value_of = {id(r): v for r, v in labelled}
    marks = [r for r, _v in labelled]
    ways = [
        (group, "down")
        for edge in (lambda r: r.right, lambda r: r.left, lambda r: (r.left + r.right) / 2)
        for group in _clusters(marks, edge, 3.0)
    ]
    ways += [(group, "across") for group in _clusters(marks, lambda r: r.middle, 2.0)]
    on_scale: Set[int] = set()
    for group, way in ways:
        if len(group) < 4:
            continue
        where = (lambda r: r.middle) if way == "down" else (lambda r: (r.left + r.right) / 2)
        group = sorted(group, key=where)
        steps = [where(b) - where(a) for a, b in zip(group, group[1:])]
        if min(steps) <= 2 or max(steps) - min(steps) > 0.25 * statistics.median(steps):
            continue
        values = [value_of[id(r)] for r in group]
        rises = [b - a for a, b in zip(values, values[1:])]
        if (
            any(abs(rise - rises[0]) > 1e-6 * max(1.0, abs(rises[0])) for rise in rises)
            or 0.0 not in values
        ):
            continue
        if round_step(abs(rises[0])):
            on_scale.update(id(r) for r in group)
    ticks = {abs(value_of[id(r)]) for r in marks if id(r) in on_scale}
    elsewhere = {abs(v) for r, v in labelled if id(r) not in on_scale}
    elsewhere.update(n for r in runs if _tick_value(r.text) is None for n in numbers_in(r.text))
    return {round(t, 6) for t in ticks - elsewhere}


def _layer(lines: Sequence[Line]) -> str:
    """A page's own words, a line at a time: its lines, and the words each chart on it prints."""
    out: List[str] = []
    for line in lines:
        if line.kind == "figure":
            _mark, _, words = line.runs[0].text.partition("\n")
            out.append(words.replace("Words in the picture: ", "", 1))
        else:
            out.append(line.text)
    return "\n".join(text for text in out if text.strip())


# ============================================================================
# FORMS AND COMMENTS: what a person typed into the PDF, or wrote beside it
# ============================================================================
#
# INPUT   a pypdf page
# OUTPUT  its filled fields as "name: value", and its comments


def _pdfium_string(getter: Any, *args: Any) -> str:
    """A UTF-16 string PDFium hands back through a length call and a fill call."""
    length = getter(*args, None, 0)
    if length <= 2:
        return ""
    buffer = ctypes.create_string_buffer(length)
    getter(*args, ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ushort)), length)
    return buffer.raw[: length - 2].decode("utf-16-le", errors="replace")


#: The annotation kinds a person writes a comment in.
_COMMENTS = {
    "FPDF_ANNOT_TEXT",
    "FPDF_ANNOT_FREETEXT",
    "FPDF_ANNOT_HIGHLIGHT",
    "FPDF_ANNOT_UNDERLINE",
    "FPDF_ANNOT_STRIKEOUT",
    "FPDF_ANNOT_SQUIGGLY",
    "FPDF_ANNOT_STAMP",
    "FPDF_ANNOT_CARET",
}


def _pdfium_annotations(
    page: Any, form: Any, raw: Any
) -> Tuple[List[Tuple[Optional[Tuple[float, float, float, float]], str, str]], List[str]]:
    """A page's filled fields as ``(rect, name, value)`` in PDF space, and its comments, read by PDFium."""
    fields: List[Tuple[Optional[Tuple[float, float, float, float]], str, str]] = []
    comments: List[str] = []
    kinds = {getattr(raw, name): name for name in _COMMENTS if hasattr(raw, name)}
    widget = getattr(raw, "FPDF_ANNOT_WIDGET", 20)
    rect = raw.FS_RECTF()
    try:
        count = raw.FPDFPage_GetAnnotCount(page.raw)
    except Exception:  # noqa: BLE001
        return fields, comments
    for i in range(count):
        annot = raw.FPDFPage_GetAnnot(page.raw, i)
        if not annot:
            continue
        try:
            kind = raw.FPDFAnnot_GetSubtype(annot)
            if kind == widget and form is not None:
                name = _pdfium_string(
                    raw.FPDFAnnot_GetFormFieldAlternateName, form, annot
                ) or _pdfium_string(raw.FPDFAnnot_GetFormFieldName, form, annot)
                value = _pdfium_string(raw.FPDFAnnot_GetFormFieldValue, form, annot).strip()
                kind_of_field = raw.FPDFAnnot_GetFormFieldType(form, annot)
                if kind_of_field in (
                    getattr(raw, "FPDF_FORMFIELD_CHECKBOX", 2),
                    getattr(raw, "FPDF_FORMFIELD_RADIOBUTTON", 3),
                ):
                    value = "yes" if raw.FPDFAnnot_IsChecked(form, annot) else ""
                if not value or value in ("Off",):
                    continue
                place = None
                if raw.FPDFAnnot_GetRect(annot, ctypes.byref(rect)):
                    place = (
                        min(rect.left, rect.right),
                        min(rect.bottom, rect.top),
                        max(rect.left, rect.right),
                        max(rect.bottom, rect.top),
                    )
                fields.append((place, name.strip(), value))
            elif kind in kinds:
                said = _pdfium_string(raw.FPDFAnnot_GetStringValue, annot, b"Contents").strip()
                if said:
                    comments.append(" ".join(said.split()))
        except Exception:  # noqa: BLE001 - one broken annotation is not a broken page
            continue
        finally:
            raw.FPDFPage_CloseAnnot(annot)
    return fields, comments


def _annotations(
    page: Any,
) -> Tuple[List[Tuple[Optional[Tuple[float, float, float, float]], str, str]], List[str]]:
    """A page's filled fields as ``(rect, name, value)``, the rect in PDF space when it has one, and its comments."""
    fields: List[Tuple[Optional[Tuple[float, float, float, float]], str, str]] = []
    comments: List[str] = []
    try:
        annots = page.get("/Annots") or []
    except Exception:  # noqa: BLE001
        return fields, comments
    for ref in annots:
        try:
            annot = ref.get_object()
            kind = str(annot.get("/Subtype", ""))
            if kind == "/Widget":
                holder = annot
                name = holder.get("/TU") or holder.get("/T")
                value = holder.get("/V")
                parent = holder.get("/Parent")
                if parent is not None:
                    parent = parent.get_object()
                    name = name or parent.get("/TU") or parent.get("/T")
                    value = value if value is not None else parent.get("/V")
                if value is None or name is None:
                    continue
                shown = str(value).lstrip("/")
                if shown in ("Off", ""):
                    continue
                if shown in ("Yes", "On"):
                    shown = "yes"
                rect = None
                try:
                    corners = [float(v) for v in holder.get("/Rect")]
                    rect = (
                        min(corners[0], corners[2]),
                        min(corners[1], corners[3]),
                        max(corners[0], corners[2]),
                        max(corners[1], corners[3]),
                    )
                except Exception:  # noqa: BLE001 - a field with no place is listed at the end of its page
                    rect = None
                fields.append((rect, str(name).strip(), shown.strip()))
            elif kind in (
                "/Text",
                "/FreeText",
                "/Highlight",
                "/Underline",
                "/StrikeOut",
                "/Squiggly",
                "/Stamp",
                "/Caret",
            ):
                said = str(annot.get("/Contents") or "").strip()
                if said:
                    comments.append(" ".join(said.split()))
        except Exception:  # noqa: BLE001 - one broken annotation is not a broken page
            continue
    return fields, comments


# ============================================================================
# THE WHOLE DOCUMENT
# ============================================================================
#
# INPUT   a PDF on disk
# OUTPUT  a text a page, and what was left out


def read_pdf(
    path: Path, password: Optional[str] = None, charts: bool = False, judge: bool = False
) -> PdfRead:
    """Every page's text, rebuilt from where its runs sit. Raises ImportError without pypdfium2.

    A file PDFium cannot open raises :class:`vectrixdb.exceptions.ExtractionError`
    saying why: protected by a password, or not a PDF it can read. With
    ``charts``, each chart a page draws with shapes is drawn as a PNG of its
    region, in :attr:`PdfRead.charts` as ``(page, top, png, name)``. With
    ``judge``, the pages the rules are unsure of are named in
    :attr:`PdfRead.hard`, for something that sees the page to read.
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    with PDFIUM_LOCK:
        return _read_pdf(pdfium, raw, path, password, charts, judge)


def _read_pdf(
    pdfium: Any,
    raw: Any,
    path: Path,
    password: Optional[str],
    charts: bool = False,
    judge: bool = False,
) -> PdfRead:
    from ..exceptions import ExtractionError

    try:
        document = pdfium.PdfDocument(str(path), password=password)
    except pdfium.PdfiumError as exc:
        said = str(exc).lower()
        if "password" in said:
            raise ExtractionError(
                f"{Path(path).name} is protected by a password, so its text cannot be read"
            ) from exc
        raise ExtractionError(
            f"{Path(path).name} could not be opened as a PDF: it is damaged or not a PDF ({exc})"
        ) from exc
    try:
        form = None
        try:
            document.init_forms()
            form = document.formenv.raw if getattr(document, "formenv", None) is not None else None
        except Exception:  # noqa: BLE001 - forms are extra; the text does not wait on them
            form = None
        forms: List[Tuple[list, List[str]]] = []
        pages: List[List[Line]] = []
        heights: List[float] = []
        hidden = 0
        pictured: List[int] = []
        drawn: List[Tuple[int, float, bytes, str]] = []
        hard: Dict[int, str] = {}
        ticks: Dict[int, List[float]] = {}
        for index in range(len(document)):
            page = document[index]
            try:
                # The page's own space, as the text is placed in it; a page
                # turned for display is still written the way it was written.
                box = page.get_mediabox()
                height = float(box[3])
                width = float(box[2] - box[0]) or float(page.get_width())
                textpage = page.get_textpage()
                try:
                    runs, left_out = _runs(page, textpage, raw, height)
                finally:
                    textpage.close()
                forms.append(_pdfium_annotations(page, form, raw))
                typed = forms[index][0] if index < len(forms) else []
                values = [
                    Run(
                        l,
                        height - tp_,
                        r,
                        height - b,
                        value,
                        max(4.0, min(12.0, 0.7 * (tp_ - b))),
                        False,
                    )
                    for (l, b, r, tp_), _name, value in (
                        (rect, name, value) for rect, name, value in typed if rect is not None
                    )
                ]
                hidden += left_out
                if sum(len(r.text.strip()) for r in runs) < 200:
                    area = width * (height - float(box[1])) or 1.0
                    covered = sum(
                        (r - l) * (b - t)
                        for l, t, r, b, picture in _backgrounds(page, raw, height)
                        if picture
                    )
                    if covered >= 0.5 * area:
                        pictured.append(index + 1)
                lines = _page_lines(runs)
                _place_values(lines, values)
                regions = _chart_regions(page, raw, width, height, runs) if charts or judge else []
                if judge:
                    why = _hard(lines, runs, height, bool(regions))
                    if why:
                        hard[index + 1] = why
                        scale = _scale_ticks(runs)
                        if scale:
                            ticks[index + 1] = sorted(scale)
                if charts:
                    # Each chart drawn as a picture, named on its page as a
                    # page's pictures are, and its figure line set where it sits.
                    shown = 0
                    for region in regions:
                        picture = _draw_region(page, width, height, region)
                        if picture:
                            shown += 1
                            name = f"p{index + 1}-chart{shown}.png"
                            lines = _figure_in_place(lines, region, name, height)
                            drawn.append((index + 1, region[1], picture, name))
                pages.append(lines)
                heights.append(height)
            finally:
                page.close()
    finally:
        document.close()

    running = _drop_running(pages, heights)
    layers = [_layer(lines) for lines in pages]
    tables = sum(_tables(lines) for lines in pages)
    body = _body_size(pages)
    _mark_headings(pages, body)
    words = _vocabulary(pages)
    texts: List[str] = []
    fields = comments = 0
    textless: List[int] = []
    for index, lines in enumerate(pages):
        text = _page_text(lines, body, words)
        if index < len(forms):
            typed, remarks = forms[index]
            fields += len(typed)
            unplaced = [f"{name}: {value}" for rect, name, value in typed if rect is None]
            if unplaced:
                text += (
                    ("\n\n" if text else "")
                    + "Form fields:\n"
                    + "\n".join(f"- {item}" for item in unplaced)
                )
            if remarks:
                text += ("\n\n" if text else "") + "\n".join(
                    f"[Comment: {item}]" for item in remarks
                )
                comments += len(remarks)
        if not text.strip():
            textless.append(index + 1)
        texts.append(text)
    return PdfRead(
        texts,
        running,
        hidden,
        textless,
        pictured,
        tables,
        fields,
        comments,
        drawn,
        layers,
        hard,
        ticks,
    )
