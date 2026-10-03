"""A page read the way a person reads it: a picture of the page and its own words, given to a model that can see.

The rules in :mod:`vectrixdb.extract.pdf_text` rebuild a page from where its
text sits, and on most pages that is exact and free. On a page laid out to
be looked at, a report's snapshot of big figures, a panel of charts, a page
whose file draws its parts in no order, the rules lose what goes with what:
"4.6%" in one paragraph and "2025 Dividend Yield" in another. A person
looking at the page has no such trouble, and neither has a model that can
see it. :class:`PageReader` hands one a picture of the page and the words the
page holds, and asks for the page written out in Markdown.

A model that can see can also misread, and a number it misreads looks like
any other. So every number in what it writes is held to the page's own
text: :func:`held_to_the_page` takes out each sentence, cell or value that
holds a number the page does not print, and a page with too many of them,
or one that leaves out too much of what the page says, is not taken at all
and keeps its reading by the rules.
"""

from __future__ import annotations

import base64
import logging
import math
import os
import re
import unicodedata
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from .._chat import ChatRoute, Transport
from .._chat import json_in as _json_in
from .describers import _picture

logger = logging.getLogger(__name__)

__all__ = ["PageReader", "held_to_the_page", "numbers_in"]


# ============================================================================
# SETTINGS: what the model is asked, and the limits a reading is held to
# ============================================================================
#
# The instruction, and how far an answer may stray from the page before it
# is not taken.

#: What a model that can see is asked, with the page's picture and its words.
INSTRUCTION = """You write out one page of a document in Markdown, so that it can be searched and cited exactly.

You are given a picture of the page and its text layer: every word and number printed on the page, exactly as the file holds them, in an order that may be wrong and with no layout.

Use the picture for what goes with what: the reading order, the headings, which label belongs to which number, the rows and columns of each table, and what each chart or picture shows.
Use the text layer for every word and number you write, copied exactly. Never write a number that is not in the text layer: not a total you worked out, not a value you read off a chart's bars or lines, not a date you inferred.

Write:
- headings with #, ## and ###, ranked as the page ranks them;
- paragraphs whole and in the order a person reads them, with a word broken across lines joined;
- a figure set apart on the page, a big number with its label, as one list item that keeps the number with what it measures, even when the label is printed as a heading over it: "- 4.6% 2025 dividend yield", "- $68 billion FY25 total reported revenue";
- each table as a Markdown table whose first row is its header: the column names the page prints, or, when it prints none, short names for what each column holds, such as "Entry | Page". One table row for each row on the page, and a header printed over several columns repeated in each column it covers;
- each chart, diagram or photograph that carries information as a line "[Figure: what it shows, in at most 15 words]", then, on the lines directly under it with no blank line between, a description in plain sentences, then for a chart one line for each value printed on the chart as a label, naming everything that says what the value is: "Net income ($ billions); 2025 Reported: 20.0". A value printed on the chart is written only with its year, its series and its category, never alone and never as a list item;
- footnote marks as [^1], and the footnotes at the end as "[^1]: text".
A chart's values are only the ones printed on it as labels. The numbers on its axes are its scale, never a value: a bar that reaches the line marked 7,000 does not have the value 7,000, and a chart that prints no values gets a description and no value lines. Say which of two things is larger only when the picture plainly shows it.
Write each thing once. Use - for list items, and numbers only where the page numbers them itself.
Leave out the running header, the running footer, the page number, and decoration: logos, backgrounds, borders.

Answer with one JSON object and nothing else: {"markdown": "the page in Markdown"}.
Write in the language of the page."""

#: Numbers the page does not print, over this share of the numbers written,
#: and a reading is not taken; three at least, so one slip on a page of two
#: figures does not throw it away.
MOST_UNPRINTED = 0.1

#: The share of the page's own words a reading must keep, or it left out
#: what the page says: a summary, or a page half read.
LEAST_KEPT = 0.7


# ============================================================================
# NUMBERS: what a page prints, and what a reading says
# ============================================================================
#
# INPUT   a page's text layer; a reading of it
# OUTPUT  the numbers each holds; the reading with every sentence, cell or
#         value that holds a number the page does not print taken out
#
# A number is compared by its value: 10.0 and 10, 1,200 and 1200, (35) and
# -35 are the same number. A digit inside a word, the 1 of CET1 or the 3 of
# Q3, is part of the word on both sides and counts on neither.

_NUMBER = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?!\d)|(?<![\w.])\d+(?:\.\d+)?(?!\d)")
_FOOTNOTE = re.compile(r"\[\^[^\]]*\]")
_LIST_NUMBER = re.compile(r"^\s*\d+[.)]\s+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _plain(text: str) -> str:
    """Text with footnote marks out and raised figures made plain, the same way for the page and the reading."""
    return _FOOTNOTE.sub(" ", unicodedata.normalize("NFKC", text))


def numbers_in(text: str) -> List[float]:
    """Every number in a text, by value."""
    found: List[float] = []
    for match in _NUMBER.finditer(_plain(text)):
        try:
            found.append(round(float(match.group(0).replace(",", "")), 6))
        except ValueError:  # pragma: no cover - the pattern holds only digits, commas and one point
            continue
    return found


def _words(text: str) -> Set[str]:
    return {w.lower() for w in re.findall(r"[^\W\d_]{3,}", _plain(text))}


#: A number said to be a guess, in words or by a sign: "about 7,100", "≈450", "~300".
_GUESSED = re.compile(
    r"(?:\b(?:about|approximately|approx\.?|around|roughly|nearly|almost|circa|estimated|est\.)|≈|~)\s*[$€£¥]?\s*\(?(\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)


def guessed(text: str) -> Set[float]:
    """The numbers a text says it guessed."""
    found: Set[float] = set()
    for match in _GUESSED.finditer(_plain(text)):
        try:
            found.add(round(float(match.group(1).replace(",", "")), 6))
        except ValueError:  # pragma: no cover - the pattern holds only digits, commas and one point
            continue
    return found


def round_step(step: float) -> bool:
    """1, 2, 2.5, 3, 4 or 5 times a power of ten: the steps a chart's scale is drawn in."""
    if step <= 0:
        return False
    scale = 10 ** math.floor(math.log10(step))
    return any(abs(step - m * scale) <= 1e-9 * scale for m in (1, 2, 2.5, 3, 4, 5, 10))


def scale_in(text: str) -> Set[float]:
    """The figures in a chart's printed words that are only its scale.

    Its words are read in order, and a scale's ticks come one after another:
    four or more figures going up or down by one round step, through zero,
    "8,000 7,000 6,000 5,000 4,000 3,000 2,000 1,000 0". A figure the words
    hold anywhere else as well is a label too, and not only a tick.
    """
    values = numbers_in(text)
    on_scale = [False] * len(values)
    start = 0
    while start < len(values) - 3:
        step = values[start + 1] - values[start]
        end = start + 1
        while end + 1 < len(values) and abs((values[end + 1] - values[end]) - step) <= 1e-9 * max(
            1.0, abs(step)
        ):
            end += 1
        run = values[start : end + 1]
        if len(run) >= 4 and 0.0 in run and round_step(abs(step)):
            for at in range(start, end + 1):
                on_scale[at] = True
            start = end
        else:
            start += 1
    ticks = {v for v, tick in zip(values, on_scale) if tick}
    elsewhere = {v for v, tick in zip(values, on_scale) if not tick}
    return ticks - elsewhere


def _cells(line: str) -> List[str]:
    parts = re.split(r"(?<!\\)\|", line.strip())
    if parts and not parts[0].strip():
        parts = parts[1:]
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    return parts


#: A run of HTML line breaks a model writes inside a table cell, as tags or as entities.
_BREAKS = re.compile(r"\s*(?:(?:<br\s*/?>|&lt;br\s*/?&gt;)\s*)+", re.IGNORECASE)
#: A footnote's definition line, ``[^3]: ...``.
_NOTE_LINE = re.compile(r"^\[\^(\d{1,2})\]:", re.MULTILINE)


def _unbroken(text: str) -> str:
    """Line breaks inside cells as the words they separate: "; " between items, a space before a bullet or a bracket."""

    def joined(match: "re.Match[str]") -> str:
        before = text[: match.start()].rstrip()
        after = text[match.end() :]
        if (
            not before
            or not after.strip()
            or before.endswith("|")
            or after.lstrip().startswith("|")
        ):
            return " "
        if after.lstrip()[:1] in "•·-–*(":
            return " "
        return "; "

    return _BREAKS.sub(joined, text)


def glued_marks(text: str) -> List[Tuple[int, int, str]]:
    """Where a text has footnote marks glued to their words, "CET1 Ratio5", as edits: (start, end, mark).

    Only for a footnote the text writes out, "[^5]: ...". A word with that
    number glued on points at it, however many times it does and whether or
    not the text marks it properly elsewhere: one annual report's page had
    "Adjusted1" eight times beside one "Adjusted[^1]". A short capital code
    with a digit, the CET1 or Q4 or H1 of a report, is a name and never taken
    for a mark. Each edit replaces the number with its mark, so a caller
    holding offsets into the text can move them with it.
    """
    defined = {int(n) for n in _NOTE_LINE.findall(text)}
    if not defined:
        return []
    notes = [(m.start(), text.find("\n", m.start())) for m in _NOTE_LINE.finditer(text)]
    notes = [(start, len(text) if end == -1 else end) for start, end in notes]
    edits: List[Tuple[int, int, str]] = []
    for number in defined:
        glued = re.compile(rf"\b([^\W\d_]+){number}(?![\w.,%])")
        for found in glued.finditer(text):
            word = found.group(1)
            if word.isupper() and len(word) <= 3:
                continue  # CET1, Q4: a name with a number in it
            if any(start <= found.start() < end for start, end in notes):
                continue  # the footnote's own words
            edits.append((found.end(1), found.end(), f"[^{number}]"))
    return sorted(edits)


def _marks_unglued(text: str) -> str:
    """A text with the footnote marks :func:`glued_marks` finds written as marks."""
    out: List[str] = []
    cursor = 0
    for start, end, mark in glued_marks(text):
        out.append(text[cursor:start])
        out.append(mark)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def tidied(reading: str) -> str:
    """A model's markup habits put right before the reading is held to the page: line breaks in cells, glued footnote marks."""
    return _marks_unglued(_unbroken(reading))


def held_to_the_page(
    reading: str, layer: str, ticks: Iterable[float] = ()
) -> Optional[Tuple[str, Dict[str, int]]]:
    """A reading with what the page does not print taken out, and what was checked; None when it is not to be taken.

    A sentence holding a number the page does not print goes; in a table, the
    cell goes and the row stays; in a row of values, ``Label; 2025: 4.2``, the
    value goes. ``ticks`` are the figures the page prints only on a chart's
    scale: a value in a row or a table cell that is one of them was read off
    a bar against the scale, and goes too.

    What goes is one of two things. A guess is a value the reading says it
    guessed, "about 7,100", or one read off a scale: a habit of models shown
    a chart, taken out and no more. An invention is a number the page never
    prints and the reading gives as fact: a sign the page was misread. The
    reading is not taken at all when more than :data:`MOST_UNPRINTED` of its
    numbers are inventions, or when it keeps less than :data:`LEAST_KEPT` of
    the words the page holds.
    """
    reading = tidied(reading)
    printed = set(numbers_in(layer))
    scale = {round(float(t), 6) for t in ticks}

    def flaws(text: str, value: Optional[str] = None) -> Tuple[int, int]:
        """(inventions, guesses) in a piece of a reading; ``value``, when a piece has one, is held to the scale too."""
        missing = [n for n in numbers_in(text) if n not in printed]
        if missing:
            said = guessed(text)
            guesses = sum(1 for n in missing if n in said)
            return len(missing) - guesses, guesses
        found = numbers_in(value) if value is not None else []
        if found and scale and all(n in scale for n in found):
            return 0, len(found)
        return 0, 0

    kept: List[str] = []
    checked = invented = guesses = 0
    for line in reading.splitlines():
        # A list the reading numbered itself: its numbers are the reading's, not the page's.
        bare = _LIST_NUMBER.sub("- ", line, count=1) if _LIST_NUMBER.match(line) else line
        checked += len(numbers_in(bare))
        stripped = bare.strip()
        if stripped.startswith("|") and stripped.count("|") >= 2:
            cells = _cells(bare)
            found = [flaws(cell) if i == 0 else flaws(cell, cell) for i, cell in enumerate(cells)]
            if not any(sum(f) for f in found):
                kept.append(bare)
                continue
            invented += sum(f[0] for f in found)
            guesses += sum(f[1] for f in found)
            cells = [" " if sum(f) else cell for cell, f in zip(cells, found)]
            if any(cell.strip() for cell in cells[1:]):
                kept.append("| " + " | ".join(cell.strip() for cell in cells) + " |")
            continue
        if "; " in stripped and ": " in stripped:
            # A row of values: the ones the page does not print go, and a
            # row left with its label alone goes with them.
            parts = stripped.split("; ")
            found = [
                flaws(part, part.rsplit(": ", 1)[1] if ": " in part else None) for part in parts
            ]
            if not any(sum(f) for f in found):
                kept.append(bare)
                continue
            invented += sum(f[0] for f in found)
            guesses += sum(f[1] for f in found)
            left = [part for part, f in zip(parts, found) if not sum(f)]
            if len(left) >= 2:
                kept.append("; ".join(left))
            continue
        made_up, guess = flaws(bare)
        if not (made_up or guess):
            kept.append(bare)
            continue
        invented += made_up
        guesses += guess
        sentences = [s for s in _SENTENCE.split(stripped) if not sum(flaws(s))]
        if sentences:
            kept.append(" ".join(sentences))
    if invented >= 3 and invented > MOST_UNPRINTED * max(checked, 1):
        logger.info(
            "a reading was not taken: %d of its %d numbers are not printed on the page",
            invented,
            checked,
        )
        return None
    page_words = _words(layer)
    written = "\n".join(kept)
    if page_words and len(page_words & _words(written)) < LEAST_KEPT * len(page_words):
        logger.info("a reading was not taken: it keeps too few of the page's own words")
        return None
    return written.strip(), {
        "numbers_checked": checked,
        "numbers_dropped": invented,
        "guesses_dropped": guesses,
    }


# ============================================================================
# THE READER: a chat model that can see, asked for one page
# ============================================================================
#
# INPUT   a page drawn as a picture, its text layer, and where it is from
# OUTPUT  the page written out in Markdown, or None


class PageReader(ChatRoute):
    """One page read by a chat model that can see, from a picture of it and its own words.

    Any service with an OpenAI style chat completions route takes it, the
    same as :class:`~vectrixdb.extract.describers.ChatDescriber`: by default
    the deployment that describes pictures, ``AZURE_OPENAI_VISION_DEPLOYMENT``,
    or another named by ``AZURE_OPENAI_PAGE_DEPLOYMENT``; otherwise the route
    ``VECTRIXDB_DESCRIBER_URL`` names. What it answers is the page in Markdown;
    hold it to the page with :func:`held_to_the_page` before it is used. Like
    the describer, it never fails a document: a refusal, a service still
    throttled after ``tries``, or an answer with nothing in it is None, and
    the page keeps its reading by the rules.
    """

    label = "page-reader"
    _AZURE_DEPLOYMENT = "AZURE_OPENAI_VISION_DEPLOYMENT"
    _ROUTE = (
        "VECTRIXDB_DESCRIBER_URL",
        "VECTRIXDB_DESCRIBER_KEY",
        "VECTRIXDB_DESCRIBER_MODEL",
        "VECTRIXDB_DESCRIBER_KEY_HEADER",
    )
    _FAILED = "did not read a page"
    _WITHOUT = "pages are read by the rules alone"

    def __init__(
        self,
        url: str,
        *,
        key: Optional[str] = None,
        token: Optional[Callable[[], str]] = None,
        model: Optional[str] = None,
        key_header: str = "Authorization",
        label: Optional[str] = None,
        max_tokens: int = 4000,
        max_side: int = 2048,
        detail: str = "high",
        timeout: float = 120.0,
        # A page the reader gives up on loses its tables, figures and
        # footnotes to the rules, so a busy deployment is waited out longer
        # than for a picture's description.
        tries: int = 6,
        max_wait: float = 60.0,
        transport: Optional[Transport] = None,
    ) -> None:
        super().__init__(
            url,
            key=key,
            token=token,
            model=model,
            key_header=key_header,
            label=label,
            max_tokens=max_tokens,
            timeout=timeout,
            tries=tries,
            max_wait=max_wait,
            transport=transport,
        )
        self.max_side = int(max_side)
        self.detail = detail
        self.asked = 0

    @classmethod
    def from_environment(
        cls, env: Optional[Mapping[str, str]] = None, **kwargs: Any
    ) -> Optional["PageReader"]:
        """One from the settings, or None when they name none.

        ``AZURE_OPENAI_PAGE_DEPLOYMENT`` names a deployment for pages alone, a
        stronger model than the one that describes pictures, say; without it
        the picture describer's own, ``AZURE_OPENAI_VISION_DEPLOYMENT``, reads
        the pages too. Otherwise ``VECTRIXDB_DESCRIBER_URL``, as for the
        describer.
        """
        found = dict(os.environ if env is None else env)
        own = str(found.get("AZURE_OPENAI_PAGE_DEPLOYMENT") or "").strip()
        if own:
            found["AZURE_OPENAI_VISION_DEPLOYMENT"] = own
        return super().from_environment(found, **kwargs)

    def __call__(self, image: bytes, layer: str, context: Mapping[str, Any]) -> Optional[str]:
        """The page in Markdown, or None."""
        ready = _picture(image, self.max_side)
        if ready is None:
            logger.info(
                "%s was not sent page %s: not a picture it can read",
                self.label,
                context.get("page"),
            )
            return None
        data, mime = ready
        about = [
            f"File: {context['name']}" if context.get("name") else "",
            f"Page: {context['page']}" if context.get("page") else "",
        ]
        text = "\n".join(line for line in about if line) + f'\n\nText layer:\n"""\n{layer}\n"""'
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": INSTRUCTION},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text.strip()},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
                            "detail": self.detail,
                        },
                    },
                ],
            },
        ]
        answer = self._ask(messages)
        if answer is None:
            return None
        self.asked += 1
        parsed = _json_in(answer)
        markdown = str(parsed.get("markdown") or "") if parsed is not None else answer
        return markdown.strip() or None
