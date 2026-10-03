"""Where text sits on a page, and what follows from it.

Three things an OCR engine leaves to whoever calls it, and that go wrong in
silence when nobody does them:

* **Reading order.** An engine returns boxes. Sorted by the top of the box,
  a two-column page reads ``L1 R1 L2 R2``: the columns interleaved line by
  line, text nobody wrote, embedded as if somebody had. And a scan's boxes on
  one line sit a pixel apart, so the same sort put "is" before "Payment".
  :func:`reading_order` groups boxes into lines with a tolerance, finds the
  gutter between columns, and reads down one column before the next.
* **Blank pages.** A page with nothing on it costs an engine call, and a
  vision model asked to read it invents boilerplate. :func:`looks_blank`
  says so from the pixels, before anything is asked.
* **Running heads and feet.** The title at the top of every page and the
  "Page 3 of 10" at the bottom land in the middle of chunks and stop a
  sentence that runs over a page break from being joined.
  :func:`drop_running_lines` takes out the lines that repeat at the top
  or bottom of most pages, and the ones that carry the page's own printed
  number, and says which.

Nothing here needs a package, except :func:`looks_blank`, which needs Pillow
and answers False without it: a page that cannot be judged is read.
"""

from __future__ import annotations

import io
import re
import statistics
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

__all__ = ["drop_running_lines", "looks_blank", "mend_sentence_breaks", "reading_order"]


# ============================================================================
# SETTINGS: the item type
# ============================================================================
#
# A box an OCR engine found, with its text.

#: A box with its text: ((left, top, right, bottom), text).
_Item = Tuple[Tuple[float, float, float, float], str]


# ============================================================================
# READING ORDER
# ============================================================================
#
# INPUT   the boxes an OCR engine found on a page
# OUTPUT  the lines of the page in the order a person reads them: boxes
#         grouped into lines, columns told by the widest empty band, prose
#         told from a table
#
# Sorted by their tops alone, two columns read as one interleaved column; this
# is what the library did before 2.2.


def _corner(point: Any) -> Tuple[float, float]:
    """``(x, y)`` from a pair, or from ``{"x": .., "y": ..}``.

    Engines disagree about this and the disagreement is not interesting.
    RapidOCR gives pairs, Azure AI Vision gives maps, and a caller should
    not have to convert one to the other before asking what order to read
    a page in.
    """
    if isinstance(point, Mapping):
        return float(point["x"]), float(point["y"])
    return float(point[0]), float(point[1])


def _rect(box: Any) -> Tuple[float, float, float, float]:
    """(left, top, right, bottom) from four corners, or from a rectangle given as such."""
    if len(box) == 4 and not isinstance(box[0], (list, tuple, Mapping)):
        left, top, right, bottom = (float(v) for v in box)
        return min(left, right), min(top, bottom), max(left, right), max(top, bottom)
    corners = [_corner(point) for point in box]
    xs = [x for x, _ in corners]
    ys = [y for _, y in corners]
    return min(xs), min(ys), max(xs), max(ys)


def _lines(items: Sequence[_Item]) -> List[_Item]:
    """Boxes grouped into lines, top to bottom, each read left to right.

    Two boxes are on one line when their middles are within most of a line's
    height of each other. A scan is never square to the pixel, so the tops of
    the words on one line differ, and sorting on them put words out of order.
    """
    if not items:
        return []
    height = statistics.median(r[3] - r[1] for r, _ in items) or 1.0
    rows: List[List[_Item]] = []
    for item in sorted(items, key=lambda it: (it[0][1] + it[0][3]) / 2):
        middle = (item[0][1] + item[0][3]) / 2
        if rows:
            last = rows[-1]
            last_middle = statistics.fmean((r[1] + r[3]) / 2 for r, _ in last)
            if abs(middle - last_middle) <= 0.6 * height:
                last.append(item)
                continue
        rows.append([item])
    out: List[_Item] = []
    for row in rows:
        row.sort(key=lambda it: it[0][0])
        rect = (
            min(r[0] for r, _ in row),
            min(r[1] for r, _ in row),
            max(r[2] for r, _ in row),
            max(r[3] for r, _ in row),
        )
        out.append((rect, " ".join(t.strip() for _, t in row if t.strip())))
    return [line for line in out if line[1]]


def _gutter(items: Sequence[_Item], left: float, right: float, height: float) -> Optional[float]:
    """The middle of the widest empty vertical band between columns, if there is one.

    Boxes wider than most of the block are left out of the search: a heading
    across both columns crosses the gutter without the columns being any less
    two columns. The band has to be wider than a space between words and sit
    away from the edges, where a margin is not a gutter.
    """
    width = right - left
    if width <= 0:
        return None
    spans = sorted((r[0], r[2]) for r, _ in items if (r[2] - r[0]) <= 0.55 * width)
    if len(spans) < 4:
        return None
    best: Optional[Tuple[float, float]] = None
    reach = spans[0][1]
    for start, end in spans[1:]:
        if start > reach:
            gap, middle = start - reach, (start + reach) / 2
            if (
                gap >= max(0.02 * width, 0.8 * height)
                and left + 0.2 * width <= middle <= right - 0.2 * width
            ):
                if best is None or gap > best[0]:
                    best = (gap, middle)
        reach = max(reach, end)
    return best[1] if best else None


def _prose(lines: Sequence[_Item], room: float) -> bool:
    """Whether these lines are a column of text, and not a column of a table.

    A column of prose has a few lines and most of them run most of the room
    the column has, from the edge of the block to the gutter. A column of a
    table is short cells with room to spare: split like prose, a row's cells
    would be read a column at a time and "EMEA" would lose its "1200". The
    room is measured to the gutter and not across the cells themselves, since
    cells of one width fill their own width exactly.
    """
    if len(lines) < 3 or room <= 0:
        return False
    return statistics.median((r[2] - r[0]) / room for r, _ in lines) >= 0.5


def _read(items: Sequence[_Item], depth: int) -> List[str]:
    if not items:
        return []
    left, right = min(r[0] for r, _ in items), max(r[2] for r, _ in items)
    height = statistics.median(r[3] - r[1] for r, _ in items) or 1.0
    middle = _gutter(items, left, right, height) if depth < 3 else None
    if middle is None:
        return [text for _, text in _lines(items)]

    # What crosses the gutter, a heading or a rule of text across the page,
    # cuts the page into bands. Each band is read on its own: columns where it
    # has them, straight across where it has not.
    across = sorted((it for it in items if it[0][0] < middle < it[0][2]), key=lambda it: it[0][1])
    rest = [it for it in items if not (it[0][0] < middle < it[0][2])]
    edges = [float("-inf")] + [(it[0][1] + it[0][3]) / 2 for it in across] + [float("inf")]
    out: List[str] = []
    for n in range(len(edges) - 1):
        band = [it for it in rest if edges[n] < (it[0][1] + it[0][3]) / 2 <= edges[n + 1]]
        one = [it for it in band if it[0][2] <= middle]
        two = [it for it in band if it[0][0] >= middle]
        if (
            one
            and two
            and _prose(_lines(one), middle - left)
            and _prose(_lines(two), right - middle)
        ):
            out += _read(one, depth + 1) + _read(two, depth + 1)
        else:
            out += [text for _, text in _lines(band)]
        if n < len(across):
            out.append(across[n][1].strip())
    return [line for line in out if line]


def reading_order(items: Sequence[Tuple[Any, str]]) -> List[str]:
    """The lines of a page in the order a person reads them.

    ``items`` is what an OCR engine found: ``(box, text)``, the box as four
    corners or as ``(left, top, right, bottom)``. Words on one line are joined
    left to right; a page in columns is read down one column and then the
    next, with anything that runs across the columns read where it sits; a
    table, whose columns are short cells and not prose, is read across.
    """
    cleaned = [(_rect(box), str(text)) for box, text in items if str(text).strip()]
    return _read(cleaned, 0)


#: One word run into the next across a sentence end, and nothing else in the
#: token: ``fees.Interest``, ``due?Yes,``. A second stop in the token makes it
#: something else, an address or a file name, and it does not match.
_LOST_SPACE = re.compile(r"^([(\"']?[a-z]{2,}[.!?])([A-Z][a-z]+[,;:.!?)\"']?)$")


def mend_sentence_breaks(line: str) -> str:
    """Put back the space an engine drops between sentences: ``fees.Interest`` to ``fees. Interest``.

    Seen on the real engine's output: a full stop, then a capital with no
    space, wherever a sentence ends mid line. Left alone, the sentence
    splitter sees one sentence and a keyword search for "fees" misses
    "fees.Interest". A token is mended only when it is a lower-case word of
    two letters or more, its stop, and a capitalised word, and nothing more,
    so ``e.g.This``, ``U.S.Army``, ``v2.Final`` and ``www.Example.com`` are
    left as they are.
    """
    return " ".join(_LOST_SPACE.sub(r"\1 \2", token) for token in line.split(" "))


# ============================================================================
# SENTENCE BREAKS, AND BLANK PAGES
# ============================================================================
#
# INPUT   a line; a page image
# OUTPUT  the space an engine drops between sentences put back; whether a page
#         has next to nothing on it
#
# A blank page read is a page of noise indexed.


#: What opens a list item on a page read by an engine: a bullet, or a short number with its dot or bracket.
_ITEM = re.compile(r"^(?:[•◦▪▫‣⁃●○■□·*–—-]|\(?\d{1,2}[.)]|\(?[a-z][.)])\s+\S")
_ENDS_SENTENCE = re.compile(r"[.!?:;”\"')\]]$")


def paragraphs_of(lines: Sequence[str], full: float = 0.8) -> str:
    """The lines an engine read, joined into the paragraphs they were printed as.

    An OCR engine gives a line of print a line of text, and a page read that
    way is a paragraph cut at every line: a chunk ends mid-sentence, and a
    word the printer broke stays broken. A line that filled its measure, at
    least ``full`` of the longest lines on the page, runs on into the next;
    one that stopped short ended its paragraph. A word broken by a hyphen at
    the end of a line is joined to its other half, and a list item or a
    heading starts a block of its own. A page of short lines, an invoice or
    a form, has no line that fills a measure, and is left a line a line.
    """
    kept = [str(line).strip() for line in lines]
    kept = [line for line in kept if line]
    if len(kept) < 3:
        return "\n".join(kept)
    lengths = sorted(len(line) for line in kept)
    measure = statistics.median(lengths[len(lengths) // 2 :])
    if measure < 30:
        return "\n".join(kept)
    blocks: List[str] = []
    paragraph = ""
    runs_on = False
    for line in kept:
        opens = _ITEM.match(line) is not None or line.startswith("#")
        if paragraph and (not runs_on or opens):
            blocks.append(paragraph)
            paragraph = ""
        if paragraph:
            if (
                paragraph.endswith("-")
                and len(paragraph) > 1
                and paragraph[-2].isalpha()
                and line[:1].islower()
            ):
                paragraph = paragraph[:-1] + line
            else:
                paragraph += " " + line
        else:
            paragraph = line
        ends = _ENDS_SENTENCE.search(line) is not None
        # A line that filled its measure runs on, unless it ended a sentence
        # a little short of it; an item that ended a sentence is complete.
        runs_on = len(line) >= full * measure and not (
            ends and (opens or len(line) < 0.9 * measure)
        )
    if paragraph:
        blocks.append(paragraph)
    return "\n\n".join(blocks)


def looks_blank(image: bytes, ink: float = 0.002) -> bool:
    """Whether a page image has next to nothing on it.

    The page is shrunk first, which averages a scan's speckle into the paper,
    and then counted: a pixel is ink when it is far darker than the paper,
    taken as the page's median. Under ``ink`` of the page, two pixels in a
    thousand, is blank; a single line of text is several times that. False
    when Pillow is missing or the bytes are not an image, because a page that
    cannot be judged should be read.
    """
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(io.BytesIO(image)) as opened:
            grey = opened.convert("L")
            grey.thumbnail((256, 256), Image.Resampling.BOX)
            # The bytes of an 8-bit grey image are its pixels. Not getdata(),
            # which Pillow 12 deprecates, and a deprecation raised as an error
            # read every page as not blank.
            pixels = grey.tobytes()
    except Exception:
        return False
    if not pixels:
        return False
    paper = statistics.median(pixels)
    dark = sum(1 for p in pixels if p < paper - 60)
    # A page printed white on black is as blank when nothing is light on it.
    light = sum(1 for p in pixels if p > paper + 60)
    return max(dark, light) / len(pixels) < ink


# ============================================================================
# RUNNING LINES: the headers and footers that repeat
# ============================================================================
#
# INPUT   every page's lines
# OUTPUT  the page texts without the lines that repeat at the top or bottom of
#         most pages, and the printed page numbers told from them
#
# Three things an OCR engine leaves to whoever calls it, and that go wrong in
# silence when nobody does them: the order, the breaks, and the lines that are
# not the document.

_DIGITS = re.compile(r"\d+")


#: "page # of #", "p. #", "- # -", "#/#", "#": a page number and nothing else.
_PAGE_NUMBER = re.compile(r"^\W*(?:page|pg|p)?\.?\s*#(?:\s*(?:of|/)\s*#)?\W*$")
#: What a short line starts and ends with besides words, once its digits are "#": a page number and its "|" or "-".
_EDGES = re.compile(r"^[\W_]+|[\W_]+$")


def _shape(line: str, outermost: bool = True) -> str:
    """What makes two lines the same running line.

    A short line has its numbers taken out, so "Page 3 of 10" and "Page 4
    of 10" are one line, and so is what is at either end of it besides
    words, since that is where a page's own number is printed: "Acme Report
    | 3" on a right-hand page and "4 | Acme Report" on a left-hand one are
    one line. Short is counted without those ends. A longer line has to
    repeat to the letter: "Paragraph 3 opens here and says something" is a
    sentence with a number in it, and sentences that differ only by a
    number are a numbered list, not a header.
    """
    flat = " ".join(line.split()).lower()
    bare = _DIGITS.sub("#", flat)
    if not outermost:
        # Past the outermost line, only what reads as a page number has its
        # numbers taken out: "page 3 of 10", "- 3 -", "3". Once the footer is
        # gone the next line up is the body, and "See note 4." is not a footer.
        return bare if len(flat.split()) <= 6 and _PAGE_NUMBER.match(bare) else flat
    words = _EDGES.sub("", bare)
    if len(words.split()) > 6:
        return flat
    return words or "#"


def _edges(lines: Sequence[str]) -> List[int]:
    """Where a page's first and last lines with anything on them are: one place on a page of one line, none on a blank page."""
    filled = [i for i, line in enumerate(lines) if line.strip()]
    return sorted({filled[0], filled[-1]}) if filled else []


def _edge_numbers(line: str) -> Tuple[Set[int], Set[int]]:
    """What the page's printed number could be, from the digits a line starts or ends with: whole, and run into another.

    A page number printed hard against another number is read as one with
    it, "ANNUAL REPORT 20253" for the year and page 3, so the last few
    digits of a run a line ends with are candidates too, and the first few
    of one it starts with. They come second: the 1 that "41" ends in fits
    as well as the 41 does. Four digits at most, since no page number is
    longer.
    """
    flat = " ".join(line.split())
    whole: Set[int] = set()
    run_in: Set[int] = set()
    head = re.match(r"\d+", flat)
    if head:
        run = head.group()
        if len(run) <= 4:
            whole.add(int(run))
        run_in.update(int(run[:k]) for k in range(1, min(len(run), 5)))
    tail = re.search(r"\d+$", flat)
    if tail:
        run = tail.group()
        if len(run) <= 4:
            whole.add(int(run))
        run_in.update(int(run[-k:]) for k in range(1, min(len(run), 5)) if run[-k] != "0")
    return whole, run_in - whole


def _without_number(line: str, number: int) -> Optional[str]:
    """What a line says with ``number``, its page's printed number, taken off the end it is at; None when it is at neither."""
    flat = " ".join(line.split()).lower()
    mark = str(number)
    head, tail = re.match(r"\d+", flat), re.search(r"\d+$", flat)
    if head and head.group() == mark:
        return flat[head.end() :].strip()
    if tail and tail.group() == mark:
        return flat[: tail.start()].strip()
    # Printed hard against another number: "report 20253" is the year 2025 and page 3.
    if tail and tail.group().endswith(mark):
        return flat[: len(flat) - len(mark)].strip()
    if head and head.group().startswith(mark):
        return flat[len(mark) :].strip()
    return None


def _unstuck(line: str, key: str, number: int) -> Optional[str]:
    """The text a running line was read onto: "...payments.ACME HOLDINGS ... ANALYSIS122" is "...payments.".

    A reader joins two runs of text it takes for one line, and a footer can
    end up on the end of the body's last line, or a header on the start of
    its first. The running line is looked for at either end of the line,
    with its page's number beside it, and only then taken off. None when it
    is not there.
    """
    words = r"\s*".join(re.escape(word) for word in key.split())
    for pattern in (
        rf"(?<!\d){number}\s*{words}\s*$",
        rf"{words}\s*{number}\s*$",
        rf"^\s*{number}\s*{words}",
        rf"^\s*{words}\s*{number}(?!\d)",
    ):
        found = re.search(pattern, line, re.IGNORECASE)
        if found:
            return (line[: found.start()] + line[found.end() :]).strip()
    return None


def _alike(key: str, known: str) -> bool:
    """Whether a line is a running line with another part of the document named in it.

    "acme holdings annual report 2025 glossary" is "acme holdings annual
    report 2025 financial results" in another part of the report: the same
    words at one end, two thirds of the running line's and four at least.
    """
    mine, theirs = key.split(), known.split()
    enough = max(4, len(theirs) * 2 // 3)
    for a, b in ((mine, theirs), (mine[::-1], theirs[::-1])):
        same = 0
        while same < min(len(a), len(b)) and a[same] == b[same]:
            same += 1
        if same >= enough:
            return True
    return False


def _misplaced(
    lines: List[str], known: Sequence[str], number: int, longest: int
) -> Optional[Tuple[int, str, bool]]:
    """A running line on a page where it is not a line of its own at the top or bottom.

    Each time with the page's own number beside it: inside the page, where
    the reader put it in the order it was drawn; at an edge, naming a part
    of the document that is on this page alone, the rest of its words a
    running line's; or at an edge, read onto the text. Returned as where it
    is, what to leave of that line, and whether it is a line not seen
    before; None when there is none.
    """
    for i, line in enumerate(lines):
        if len(line.strip()) <= longest and _without_number(line, number) in known:
            return i, "", False
    edges = _edges(lines)
    for i in edges:
        key = _without_number(lines[i], number) if len(lines[i].strip()) <= longest else None
        if key is not None and any(_alike(key, other) for other in known):
            return i, "", True
    for i in edges:
        for other in known:
            rest = _unstuck(lines[i], other, number)
            if rest is not None:
                return i, rest, False
    return None


def _numbered(
    split: Sequence[List[str]], needed: int, longest: int
) -> Tuple[Dict[int, Dict[int, str]], List[str]]:
    """The lines that carry their own page's printed number.

    The number printed on a page is its place in the file less a constant,
    the count of pages before the one printed 1, so the constant that the
    top and bottom lines of most pages agree on is the numbering. A line that
    carries its own page's number, and says the same as such a line on
    another page, is a running line whatever its length, whichever end it is
    at, and whichever part of the document it names: a report's footer
    changes from one part's name to the next inside a few pages. A page has
    one printed number, so one such line goes from a page at a time, the
    one whose words are on the most pages: a page with a second loses it
    at the next look, once it is the only one. On a page with none,
    :func:`_misplaced` looks for one.

    Returned as what to leave of each line, by page and place, "" for a line
    that goes, with the lines as they first appeared.
    """
    whole: Dict[int, Set[int]] = {}
    votes: Dict[int, Set[int]] = {}
    for index, lines in enumerate(split):
        for i in _edges(lines):
            numbers, run_in = _edge_numbers(lines[i])
            for number in numbers:
                whole.setdefault(number - index, set()).add(index)
            for number in numbers | run_in:
                votes.setdefault(number - index, set()).add(index)

    def best(tally: Dict[int, Set[int]]) -> Optional[int]:
        found = max(
            tally, key=lambda o: (len(tally[o]), len(whole.get(o, ())), -abs(o)), default=None
        )
        return found if found is not None and len(tally[found]) >= needed else None

    offset = best(whole)
    if offset is None:
        offset = best(votes)
    if offset is None:
        return {}, []
    seen: Dict[str, Dict[int, Tuple[int, str]]] = {}
    for index, lines in enumerate(split):
        if index + offset < 1:
            continue
        for i in _edges(lines):
            line = lines[i].strip()
            key = _without_number(line, index + offset) if len(line) <= longest else None
            if key is not None:
                seen.setdefault(key, {}).setdefault(index, (i, line))
    repeated = {key: on for key, on in seen.items() if len(on) >= 2}
    cut: Dict[int, Dict[int, str]] = {}
    used: List[str] = []
    for index in range(len(split)):
        here = [key for key, on in repeated.items() if index in on]
        if here:
            key = max(here, key=lambda k: len(repeated[k]))
            cut[index] = {repeated[key][index][0]: ""}
            if key not in used:
                used.append(key)
    first = [repeated[key][min(repeated[key])][1] for key in used]
    # Three words at least: a running line of one word could be any word.
    known = [key for key in used if len(key.split()) >= 3]
    for index, lines in enumerate(split):
        if not known or index + offset < 1 or index in cut:
            continue
        found = _misplaced(lines, known, index + offset, longest)
        if found is not None:
            i, rest, new = found
            if new:
                first.append(lines[i].strip())
            cut[index] = {i: rest}
    return cut, first


def drop_running_lines(
    pages: Sequence[str], share: float = 0.6, longest: int = 90
) -> Tuple[List[str], List[str]]:
    """Page texts without the lines that repeat at the top or bottom of most pages.

    A line counts when, numbers aside, it is the first or the last line of at
    least ``share`` of the pages that have text, and of at least three. The
    two ends are counted together: a reader can put a left-hand page's
    header after its text. Only short lines are looked at that way, since a
    paragraph that happens to repeat is not a header; a longer one counts
    when it carries its own page's printed number, see :func:`_numbered`.
    Documents of fewer than four pages are left alone: there is too little
    to tell a header from a coincidence. Returned with the lines that were
    taken out, as they first appeared, so nothing is dropped in secret.
    """
    texts = [str(p) for p in pages]
    split = [t.splitlines() for t in texts]
    written = sum(1 for lines in split if _edges(lines))
    if written < 4:
        return texts, []
    needed = max(3, int(share * written + 0.999))
    dropped: List[str] = []
    for look in range(
        3
    ):  # a header of two lines is two running lines, so look again, and not for ever
        cut, numbered = _numbered(split, needed, longest)
        seen: Dict[str, Dict[int, List[int]]] = {}
        first: Dict[str, str] = {}
        for index, lines in enumerate(split):
            for i in _edges(lines):
                line = lines[i].strip()
                if len(line) <= longest:
                    shape = _shape(line, look == 0)
                    seen.setdefault(shape, {}).setdefault(index, []).append(i)
                    first.setdefault(shape, line)
        running = [shape for shape, on in seen.items() if len(on) >= needed]
        for shape in running:
            for index, places in seen[shape].items():
                for i in places:
                    cut.setdefault(index, {})[i] = ""
        if not cut:
            break
        for line in [first[shape] for shape in running] + numbered:
            if line not in dropped:
                dropped.append(line)
        for index, gone in cut.items():
            lines = split[index]
            for i in sorted(gone, reverse=True):
                if gone[i]:
                    lines[i] = gone[i]
                else:
                    del lines[i]
    if not dropped:
        return texts, []
    # Paragraph breaks inside a page are kept: only the running lines went.
    return ["\n".join(lines).strip("\n") for lines in split], dropped
