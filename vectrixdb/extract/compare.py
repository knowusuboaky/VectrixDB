"""Several readers over your own pages, side by side.

A benchmark says how an OCR model does on somebody else's documents. The only
way to choose one is to run the candidates over fifty or a hundred of yours,
the awkward ones included, and look at where they disagree. The registry
already lets any callable or endpoint be a reader, and the quality score
already says when text reads as a failed extraction; this puts the two
together::

    from vectrixdb.extract.compare import compare_extractors

    found = compare_extractors(
        ["samples/scan-01.pdf", "samples/two-columns.pdf"],
        {"built in": None, "rapidocr": RapidOcr(), "our service": HttpExtractor({".pdf": URL})},
    )
    print(found.to_markdown())
    found.disagreements()          # the files worth opening, most different first

It runs the readers; it does not judge which text is right. Two readers that
agree can both be wrong, and the one that disagrees can be the one that read
the second column. What it gives is where to look.
"""

from __future__ import annotations

import difflib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from ..ingest import LoadedDocument, load_bytes
from ..quality import DEFAULT_THRESHOLD, extraction_quality
from . import coerce

__all__ = ["Comparison", "Reading", "compare_extractors"]


# ============================================================================
# SETTINGS: what is compared
# ============================================================================
#
# The measures taken of each reading.

#: How much of each text the pairwise comparison reads. difflib is quadratic
#: at worst, and the first pages say whether two readers agree.
_COMPARED = 20_000


# ============================================================================
# ONE READING, AND THE COMPARISON
# ============================================================================
#
# INPUT   your own files, and the readers to try
# OUTPUT  what one reader made of one file; every reader run over every file,
#         each text measured, and how far the readers agree
#
# A benchmark says how an OCR model does on somebody else's documents; the
# only way to choose one is to run the candidates on yours.


@dataclass
class Reading:
    """What one reader made of one file."""

    file: str
    reader: str
    seconds: float
    characters: int = 0
    pages: int = 0
    pages_ocr: int = 0
    quality: float = 0.0
    usable: bool = False
    error: Optional[str] = None
    text: str = field(default="", repr=False)

    def to_dict(self) -> dict:
        out = {k: v for k, v in self.__dict__.items() if k != "text"}
        out["seconds"] = round(self.seconds, 3)
        return out


@dataclass
class Comparison:
    readings: List[Reading]
    #: (file, reader, other reader) -> how alike their texts are, 0 to 1.
    alike: Dict[Tuple[str, str, str], float]

    def of(self, file: str, reader: str) -> Optional[Reading]:
        return next((r for r in self.readings if r.file == file and r.reader == reader), None)

    def disagreements(self, below: float = 0.9) -> List[Tuple[str, str, str, float]]:
        """``(file, reader, other reader, how alike)`` under ``below``, the least alike first: the files worth opening."""
        found = [(f, a, b, score) for (f, a, b), score in self.alike.items() if score < below]
        return sorted(found, key=lambda row: row[3])

    def to_dict(self) -> dict:
        return {
            "readings": [r.to_dict() for r in self.readings],
            "alike": [
                {"file": f, "reader": a, "other": b, "alike": round(score, 4)}
                for (f, a, b), score in self.alike.items()
            ],
        }

    def to_markdown(self) -> str:
        """A table a person can read: one row per file and reader, then where the readers disagree."""
        lines = [
            "| File | Reader | Quality | Usable | Characters | Pages | By OCR | Seconds |",
            "| --- | --- | ---: | --- | ---: | ---: | ---: | ---: |",
        ]
        for r in self.readings:
            if r.error:
                lines.append(
                    f"| {r.file} | {r.reader} | | failed: {r.error} | | | | {r.seconds:.2f} |"
                )
            else:
                lines.append(
                    f"| {r.file} | {r.reader} | {r.quality:.2f} | {'yes' if r.usable else 'no'} | {r.characters:,} | {r.pages} | {r.pages_ocr} | {r.seconds:.2f} |"
                )
        apart = self.disagreements()
        if apart:
            lines += ["", "Where the readers disagree, least alike first:", ""]
            lines += [f"- {f}: {a} and {b} are {score:.0%} alike" for f, a, b, score in apart]
        return "\n".join(lines)


def _words(text: str) -> List[str]:
    return text[:_COMPARED].split()


def compare_extractors(
    files: Sequence[Union[str, Path, Tuple[str, bytes]]],
    extractors: Mapping[str, Optional[Callable[..., Any]]],
    threshold: float = DEFAULT_THRESHOLD,
) -> Comparison:
    """Run every reader over every file, and measure each text and how far the readers agree.

    ``files`` are paths, or ``(name, bytes)`` for something that is not a file
    here. ``extractors`` maps a name to a reader, anything the registry takes:
    a callable given the bytes and the name, returning Markdown, a mapping or
    a :class:`~vectrixdb.ingest.LoadedDocument`. ``None`` is the built-in
    reader for that file type. A reader that raises is recorded as having
    failed on that file, with what it said, and the rest still run.
    """
    if not extractors:
        raise ValueError("compare_extractors needs at least one reader to run")
    readings: List[Reading] = []
    alike: Dict[Tuple[str, str, str], float] = {}
    for entry in files:
        if isinstance(entry, tuple):
            name, data = str(entry[0]), bytes(entry[1])
        else:
            name, data = Path(entry).name, Path(entry).read_bytes()
        done: List[Reading] = []
        for reader, extractor in extractors.items():
            started = time.perf_counter()
            try:
                doc: LoadedDocument = (
                    load_bytes(data, name)
                    if extractor is None
                    else coerce(extractor(data, name), name)
                )
            except (
                Exception
            ) as exc:  # one reader failing on one file is a finding, not the end of the run
                done.append(
                    Reading(
                        file=name,
                        reader=reader,
                        seconds=time.perf_counter() - started,
                        error=f"{type(exc).__name__}: {exc}"[:200],
                    )
                )
                continue
            took = time.perf_counter() - started
            scored = extraction_quality(doc.text, threshold)
            done.append(
                Reading(
                    file=name,
                    reader=reader,
                    seconds=took,
                    characters=len(doc.text),
                    pages=len(doc.pages) or int(doc.metadata.get("pages") or 0),
                    pages_ocr=int(doc.metadata.get("pages_ocr") or 0),
                    quality=scored.score,
                    usable=scored.usable,
                    text=doc.text,
                )
            )
        read = [r for r in done if r.error is None]
        for n, one in enumerate(read):
            for other in read[n + 1 :]:
                alike[(name, one.reader, other.reader)] = difflib.SequenceMatcher(
                    None, _words(one.text), _words(other.text), autojunk=False
                ).ratio()
        readings += done
    return Comparison(readings=readings, alike=alike)
