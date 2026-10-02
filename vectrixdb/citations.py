"""Citations: a name for every chunk, and a check on what an answer cites.

The pattern is the one the Azure search sample settled on and it needs no
Azure. Every chunk carries a short citation string built at ingestion,
``report.pdf#page=3`` for a paged source and ``guide#Offline`` for a
markdown section. The prompt lists the sources as ``[citation]: text`` and
tells the model to cite in square brackets. The answer is then checked: a
bracket that names a source the model was given is a citation, and one
that names anything else is text, because a model will invent a plausible
looking reference when it has nothing to cite and the reader must never be
shown one as if it were real.

A citation names a place in the unit of the file: ``#page=`` for a PDF,
``#slide=`` for a slide deck, ``#t=`` and a second for a recording, which is
how a media player is told where to start. The link keeps a PDF page's
place in the file, which opens the right page in any viewer. A person is
shown the readable form beside it, ``report.pdf, pp. 39-40``, with the
number printed on the page where the PDF numbers its own pages, a range
where the chunk runs onto the next, ``slide 3``, or ``11:05``.

A citation is a pointer a person can follow, not proof. The proof is the
chunk's provenance: its build, its document version and its quality score,
which the lineage pages carry and a citation string does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "CITATION_INSTRUCTION",
    "Cited",
    "citation_for",
    "parse_citations",
    "readable_citation_for",
    "validate_answer",
]


# ============================================================================
# SETTINGS: the instruction a model is given, and the bracket and slides patterns
# ============================================================================
#
# The sentence that asks a model to cite in brackets, and the shapes a bracket
# and a slide reference take.

#: The sentence to put in a system prompt beside the sources. Short on
#: purpose: the check afterwards is what makes it safe, not the wording.
CITATION_INSTRUCTION = (
    "Answer from the sources below and nothing else. Each source starts with its "
    "name in square brackets. Cite a source by repeating its name in square "
    "brackets after the sentence it supports, for example [report.pdf#page=3]. "
    "Cite one source per bracket. If the sources do not answer the question, say so."
)

_BRACKET = re.compile(r"\[([^\[\]]+)\]")

#: Files whose pages are slides, and are cited as slides.
_SLIDES = (".pptx", ".ppt", ".odp")


# ============================================================================
# A NAME FOR EVERY CHUNK
# ============================================================================
#
# INPUT   a chunk's source and its metadata
# OUTPUT  the citation string; the readable one, report.pdf, pp. 39-40; either
#         built from what the chunk carries when it has none
#
# The pattern is the one the Azure search sample settled on, and it needs no
# Azure.


def _clock(seconds: Any) -> str:
    """``m:ss``, or ``h:mm:ss`` from an hour on, as a player shows it."""
    total = int(max(float(seconds or 0), 0.0))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _slides(name: str) -> bool:
    return PurePath(name).suffix.lower() in _SLIDES


def _source_name(source: Any, fallback: str) -> str:
    """A short name for a source: a file's basename, a label as given, else the id."""
    if isinstance(source, PurePath):
        return source.name
    if isinstance(source, str) and source.strip():
        text = source.strip()
        looks_like_path = ("/" in text or "\\" in text) or (
            "." in text and " " not in text and len(text) < 260
        )
        return PurePath(text).name if looks_like_path else text
    return fallback


def citation_for(
    source: Any,
    fallback: str,
    *,
    page: Optional[int] = None,
    heading: Optional[str] = None,
    figure: Optional[str] = None,
    seconds: Optional[float] = None,
) -> str:
    """The citation string for one chunk.

    ``name#t=S`` for a recording, the second the chunk starts at;
    ``name#page=N`` when the source has pages, ``name#slide=N`` when they
    are a deck's slides; ``name#Heading`` when it has headings and no
    pages, and the bare name otherwise. Square brackets are dropped from a
    heading, since the citation has to survive being written inside a pair
    of them.
    """
    name = _source_name(source, fallback)
    base = name
    if seconds is not None:
        base = f"{name}#t={int(max(float(seconds), 0.0))}"
    elif page is not None:
        base = f"{name}#{'slide' if _slides(name) else 'page'}={int(page)}"
    elif heading:
        clean = re.sub(r"[\[\]]", "", str(heading)).strip()
        if clean:
            base = f"{name}#{clean}"
    if figure:
        # A figure is cited as the page it sits on plus its caption, the
        # way the reader will look for it: the figure on page three about
        # revenue by region.
        caption = re.sub(r"[\[\]()]", "", str(figure)).strip()
        if caption:
            return f"{base}({caption})"
    return base


def citation_of(metadata: Mapping[str, Any], fallback: str) -> str:
    """The citation a chunk carries, or one built from what else it carries."""
    stamped = metadata.get("_vx_citation")
    if stamped:
        return str(stamped)
    source = metadata.get("filename") or metadata.get("source")
    return citation_for(
        source,
        fallback,
        page=metadata.get("page"),
        heading=metadata.get("heading"),
        figure=metadata.get("figure"),
        seconds=metadata.get("start_seconds"),
    )


def readable_citation_for(
    source: Any,
    fallback: str,
    *,
    page: Optional[int] = None,
    page_end: Optional[int] = None,
    page_label: Optional[str] = None,
    page_label_end: Optional[str] = None,
    heading: Optional[str] = None,
    figure: Optional[str] = None,
    seconds: Optional[float] = None,
) -> str:
    """The citation as a person reads it: ``report.pdf, pp. 39-40``.

    The number printed on the page where the file numbers its own pages,
    and the page's place in the file where it does not; a range when the
    chunk runs onto the next page; ``slide 3`` for a deck; ``11:05`` in a
    recording; the heading when there are no pages; a figure's caption in
    brackets. What to show beside an answer: the answer cites
    :func:`citation_for`'s string, which is also the link that opens the
    page.
    """
    name = _source_name(source, fallback)
    where = ""
    if seconds is not None:
        where = _clock(seconds)
    elif page is not None:
        runs_on = page_end is not None and int(page_end) != int(page)
        if page_label and (not runs_on or page_label_end):
            # Printed numbers for both ends or for neither: a range from a
            # printed number to a place in the file would mean nothing.
            first, last = str(page_label), str(page_label_end) if runs_on else ""
        else:
            first, last = str(int(page)), str(int(page_end)) if runs_on and page_end is not None else ""
        if _slides(name):
            where = f"slides {first}-{last}" if last else f"slide {first}"
        else:
            where = f"pp. {first}-{last}" if last else f"p. {first}"
    elif heading:
        where = re.sub(r"[\[\]]", "", str(heading)).strip()
    text = f"{name}, {where}" if where else name
    if figure:
        caption = re.sub(r"[\[\]()]", "", str(figure)).strip()
        if caption:
            text = f"{text} ({caption})"
    return text


def readable_citation_of(metadata: Mapping[str, Any], fallback: str) -> str:
    """The readable citation a chunk carries, or one built from what else it carries."""
    stamped = metadata.get("_vx_readable_citation")
    if stamped:
        return str(stamped)
    return readable_citation_for(
        metadata.get("filename") or metadata.get("source"),
        fallback,
        page=metadata.get("page"),
        page_end=metadata.get("page_end"),
        page_label=metadata.get("page_label"),
        page_label_end=metadata.get("page_label_end"),
        heading=metadata.get("heading"),
        figure=metadata.get("figure"),
        seconds=metadata.get("start_seconds"),
    )


# ============================================================================
# WHAT AN ANSWER CITES
# ============================================================================
#
# INPUT   an answer, and the sources the model was given
# OUTPUT  every bracketed reference in order, duplicates kept; each resolved
#         to an allowed citation, or None; the answer with its citations
#         checked
#
# A citation of a source the model was not given is caught, not trusted.


def parse_citations(answer: str) -> List[str]:
    """Every bracketed reference in an answer, in order, duplicates kept."""
    return [m.group(1).strip() for m in _BRACKET.finditer(answer)]


def _resolve(part: str, allowed: Sequence[str]) -> Optional[str]:
    """The allowed citation a bracket refers to, or None.

    Exact first. Then a suffix match, because a model handed
    ``reports/q3.pdf#page=2`` will sometimes write ``q3.pdf#page=2``, and
    that is the same source; the match runs on a boundary so ``3.pdf``
    cannot claim ``q3.pdf``.
    """
    if part in allowed:
        return part
    for candidate in allowed:
        if candidate.endswith(part) and (
            len(candidate) == len(part) or candidate[-len(part) - 1] in "/\\ "
        ):
            return candidate
    return None


@dataclass
class Cited:
    """An answer with its citations checked against the sources it was given.

    ``text`` keeps every valid citation as written and turns every other
    bracket into plain text. ``cited`` lists the sources actually cited, in
    first-use order; ``rejected`` lists what the model wrote in brackets
    that named nothing it was given.
    """

    text: str
    cited: List[str] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """True when every bracket in the answer named a real source."""
        return not self.rejected

    def numbered(self) -> Tuple[str, List[str]]:
        """The answer with ``[1]``, ``[2]`` in place of the citations, and the list they index.

        For a footnote rendering. The list is 1-based in the text and
        0-based in Python, which is what a footnote list is.
        """
        order = list(self.cited)

        def swap(m: "re.Match[str]") -> str:
            part = m.group(1).strip()
            hit = _resolve(part, order)
            return f"[{order.index(hit) + 1}]" if hit is not None else m.group(0)

        return _BRACKET.sub(swap, self.text), order


def validate_answer(answer: str, sources: Iterable[Any]) -> Cited:
    """Check an answer's brackets against the sources the model was given.

    ``sources`` is whatever carries citations: a ``Results``, a list of
    result objects with a ``citation`` attribute, or plain strings.
    """
    allowed: List[str] = []
    for s in sources:
        cite = getattr(s, "citation", None)
        if cite is None and isinstance(s, str):
            cite = s
        if cite is None and isinstance(s, Mapping):
            cite = citation_of(s, str(s.get("id", "")))
        if cite and cite not in allowed:
            allowed.append(str(cite))

    cited: List[str] = []
    rejected: List[str] = []

    def keep_or_flatten(m: "re.Match[str]") -> str:
        part = m.group(1).strip()
        hit = _resolve(part, allowed)
        if hit is None:
            if part not in rejected:
                rejected.append(part)
            return part
        if hit not in cited:
            cited.append(hit)
        return f"[{hit}]"

    text = _BRACKET.sub(keep_or_flatten, answer)
    return Cited(text=text, cited=cited, rejected=rejected)
