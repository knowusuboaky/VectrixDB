"""Long files read in pieces, so every call to a reader finishes inside a request's time limit.

    from vectrixdb.extract import HttpExtractor
    from vectrixdb.extract.batches import batched

    reader = batched(HttpExtractor.from_environment(routes=table))          # 10 minutes, 20 pages
    reader = batched(service, minutes=5, pages=10, at_once=8)               # smaller pieces, more at once

A reader behind HTTP, such as the extraction service, answers one call at a
time, and Azure ends every HTTP request at 230 seconds, whatever the
function's own timeout says. An hour of sound, or three hundred scanned
pages, does not fit in one call, and neither does a file bigger than the
service takes. So a long file is cut into pieces that each fit, every piece
is read by the same reader as an ordinary file, and the answers are put
back together::

    sound      pieces of about ten minutes, cut at a pause, so no word is split
    video      its sound, taken out here, then the same
    PDF        pieces of twenty pages
    the rest   one call, as before

The join is exact. A recording's phrases carry their times, so each piece's
move on by where the piece starts and the minutes stay the pages; a PDF's
page numbers move on by the pages before. A citation to minute 42 or page 212
points where it did.

Pieces go several at a time, so an hour of sound takes about as long as its
slowest piece. A piece the reader failed on its side, a 5xx, a 429 or no
answer at all, is tried again alone before the file is given up on; a piece
it refused, a 4xx, is not, because it would be refused again.

Cutting needs ffmpeg for sound and video, ``pip install vectrixdb[ffmpeg]``,
and pypdf for PDFs, ``vectrixdb[documents]``. Only the cutting happens here.
The reading is still the reader's.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..exceptions import DependencyError, ExtractionError
from ..ingest import LoadedDocument

__all__ = ["Batched", "FfmpegSound", "batched", "cut_points", "parse_duration", "parse_silences"]


# ============================================================================
# SETTINGS: the most a piece may weigh, and ffmpeg's lines
# ============================================================================
#
# The size a piece is kept under, and the shapes of the lines ffmpeg prints
# for a duration and a silence.

#: A file this big is cut whatever its length: the extraction service takes
#: at most 100 MB a call, and this leaves room for the request around it.
MAX_BYTES = 90 * 1024 * 1024

_DURATION = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


# ============================================================================
# THE READER, BATCHED
# ============================================================================
#
# INPUT   a reader
# OUTPUT  the reader, with a long file cut into pieces first
#
# See the module's own words for what is cut and how.


def batched(
    reader: Optional[Callable[..., Any]],
    *,
    minutes: float = 10.0,
    pages: int = 20,
    at_once: int = 4,
    max_bytes: int = MAX_BYTES,
    sound: Optional[Any] = None,
) -> Optional["Batched"]:
    """``reader``, with a long file cut into pieces first; see the module's own words.

    None for no reader, so ``batched(HttpExtractor.from_environment(...))`` is
    None where no service is set, as the reader it wraps would be.
    """
    if reader is None:
        return None
    return Batched(
        reader, minutes=minutes, pages=pages, at_once=at_once, max_bytes=max_bytes, sound=sound
    )


# ============================================================================
# SOUND: its length, its pauses, and where to cut
# ============================================================================
#
# INPUT   what ffmpeg says of a file; a total, a piece length, and the pauses
# OUTPUT  the length in seconds, or None; every pause silencedetect found;
#         where to cut, at pauses where it can, so a word is not split
#
# A cut in a pause costs nothing; a cut in a word costs the word.


def parse_duration(said: str) -> Optional[float]:
    """The length ffmpeg reports for its input, in seconds, or None when it reports none."""
    found = _DURATION.search(said or "")
    if found is None:
        return None
    hours, minutes, seconds = found.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_silences(said: str) -> List[Tuple[float, float]]:
    """Every pause ffmpeg's silencedetect found, as its start and end in seconds."""
    found: List[Tuple[float, float]] = []
    start: Optional[float] = None
    for line in (said or "").splitlines():
        began = _SILENCE_START.search(line)
        if began:
            start = max(float(began.group(1)), 0.0)
            continue
        ended = _SILENCE_END.search(line)
        if ended and start is not None:
            found.append((start, float(ended.group(1))))
            start = None
    return found


def cut_points(
    total: float,
    piece: float,
    pauses: Sequence[Tuple[float, float]],
    window: Optional[float] = None,
) -> List[float]:
    """Where to cut ``total`` seconds of sound into pieces of about ``piece`` seconds each.

    At the longest pause within ``window`` of each mark, a fifth of a piece by
    default, so a cut falls between sentences rather than inside a word. With
    no pause near a mark, the cut is the mark itself. No piece is ever longer
    than ``piece`` and its window.
    """
    window = piece * 0.2 if window is None else window
    cuts: List[float] = []
    last = 0.0
    while total - last > piece:
        mark = last + piece
        near = [
            (end - start, (start + end) / 2)
            for start, end in pauses
            if abs((start + end) / 2 - mark) <= window and (start + end) / 2 > last + piece / 2
        ]
        cut = max(near)[1] if near else mark
        cuts.append(cut)
        last = cut
    return cuts


class FfmpegSound:
    """What cutting sound takes, from ffmpeg: a file's length, its sound as a WAV, and its pauses.

    ``binary`` is the ffmpeg to run; left out, the one imageio-ffmpeg ships,
    or one on the path. ``noise`` is how quiet a pause is and ``pause`` how
    long it lasts at least, in seconds.
    """

    def __init__(
        self, binary: Optional[str] = None, *, noise: str = "-35dB", pause: float = 0.4
    ) -> None:
        self.binary = binary
        self.noise = noise
        self.pause = pause

    def _ffmpeg(self) -> str:
        if self.binary:
            return self.binary
        try:
            import imageio_ffmpeg
        except ImportError as exc:
            on_path = shutil.which("ffmpeg")
            if on_path:
                return on_path
            raise DependencyError("imageio-ffmpeg", "ffmpeg") from exc
        return imageio_ffmpeg.get_ffmpeg_exe()

    def _run(self, *args: str) -> Tuple[int, str]:
        done = subprocess.run(
            [self._ffmpeg(), "-hide_banner", "-nostdin", *args], capture_output=True
        )  # noqa: S603 - fixed argv, no shell
        return done.returncode, done.stderr.decode("utf-8", errors="replace")

    def duration(self, path: str) -> Optional[float]:
        # With no output named, ffmpeg says what its input is and stops.
        return parse_duration(self._run("-i", path)[1])

    def to_wav(self, path: str, wav: str) -> None:
        code, said = self._run("-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000", wav)
        if code != 0:
            tail = said.strip().splitlines()[-1:] or ["no output"]
            raise ExtractionError(f"ffmpeg could not take the sound out: {tail[0]}")

    def silences(self, wav: str) -> List[Tuple[float, float]]:
        said = self._run(
            "-nostats",
            "-i",
            wav,
            "-af",
            f"silencedetect=noise={self.noise}:d={self.pause}",
            "-f",
            "null",
            "-",
        )[1]
        return parse_silences(said)


# ============================================================================
# THE PIECES, AND THE WHOLE
# ============================================================================
#
# INPUT   a WAV and its bounds; each piece's metadata
# OUTPUT  the WAV between each pair of bounds as its own, with where it
#         starts; a failure worth a second try, told from a refusal that would
#         be met again; one piece's metadata folded into the whole file's,
#         counts added and page numbers moved on; the batched reader itself
#
# So every call to a reader finishes inside a request's time limit, and a long
# recording is still one document.


def _wav_seconds(path: Path) -> float:
    import wave

    with wave.open(str(path), "rb") as sound:
        return sound.getnframes() / float(sound.getframerate() or 1)


def _wav_pieces(path: Path, bounds: Sequence[float]) -> List[Tuple[float, bytes]]:
    """The WAV between each pair of bounds, as its own WAV, with where it starts."""
    import wave

    pieces: List[Tuple[float, bytes]] = []
    with wave.open(str(path), "rb") as sound:
        rate, channels, width = sound.getframerate(), sound.getnchannels(), sound.getsampwidth()
        for start, end in zip(bounds, bounds[1:]):
            first, last = int(start * rate), int(end * rate)
            sound.setpos(first)
            frames = sound.readframes(max(last - first, 0))
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as out:
                out.setnchannels(channels)
                out.setsampwidth(width)
                out.setframerate(rate)
                out.writeframes(frames)
            pieces.append((float(start), buffer.getvalue()))
    return pieces


def _worth_again(exc: ExtractionError) -> bool:
    """A failure on the reader's side, which a second try may not meet; a refusal would be met again."""
    status = getattr(exc, "status", None)
    return status is None or status >= 500 or status == 429


#: Page numbers a piece lists, moved on by the pages before it. A 244-page
#: report read in twenty-page pieces said which pages of its first piece were
#: read by sight and nothing of the other eleven.
_PAGE_LISTS = ("ocr_pages", "pages_read_by_sight", "pages_without_text")
#: Counts a piece gives, added up.
_COUNTS = (
    "pages_ocr",
    "pages_blank",
    "pages_not_read_by_sight",
    "numbers_not_on_page",
    "guesses_left_out",
    "hidden_text_left_out",
)


def _merge(into: Dict[str, Any], given: Dict[str, Any], first_page: int) -> None:
    """One piece's metadata into the whole file's: counts added, page numbers moved on."""
    for key, value in (given or {}).items():
        if key in _PAGE_LISTS and isinstance(value, (list, tuple)):
            into.setdefault(key, []).extend(int(n) + first_page for n in value)
        elif key == "pages_kept_by_rules" and isinstance(value, Mapping):
            kept = into.setdefault(key, {})
            for number, why in value.items():
                kept[str(int(number) + first_page)] = why
        elif key in _COUNTS and isinstance(value, (int, float)):
            into[key] = into.get(key, 0) + value
        elif key == "ocr":
            into[key] = bool(into.get(key)) or bool(value)
        elif key == "running_lines" and isinstance(value, (list, tuple)):
            into[key] = sorted({str(line) for line in [*into.get(key, []), *value]})
        elif key == "figures_described_by" and isinstance(value, Mapping):
            counts = into.setdefault(key, {})
            for by, n in value.items():
                counts[str(by)] = int(counts.get(str(by), 0)) + int(n)
        else:
            into.setdefault(key, value)


class Batched:
    """``reader``, with a long file cut into pieces first. Everything else passes straight through.

    An extractor like the reader it wraps: called with a file's bytes and
    name, it answers a :class:`~vectrixdb.ingest.LoadedDocument`, and it
    reads the suffixes the reader reads. ``sound`` cuts sound;
    :class:`FfmpegSound` when left out.
    """

    def __init__(
        self,
        reader: Callable[..., Any],
        *,
        minutes: float = 10.0,
        pages: int = 20,
        at_once: int = 4,
        max_bytes: int = MAX_BYTES,
        sound: Optional[Any] = None,
        tries: int = 3,
        pause: float = 2.0,
    ) -> None:
        if minutes <= 0 or pages <= 0 or at_once <= 0 or tries <= 0:
            raise ValueError("minutes, pages, at_once and tries are all more than nothing")
        self.reader = reader
        self.minutes = float(minutes)
        self.pages = int(pages)
        self.at_once = int(at_once)
        self.max_bytes = int(max_bytes)
        self.tries = int(tries)
        self._pause = float(pause)
        self._sound = sound
        self.label = f"{getattr(reader, 'label', None) or type(reader).__name__}, in pieces"

    def suffixes(self) -> List[str]:
        found = getattr(self.reader, "suffixes", None)
        return sorted(found() if callable(found) else found or [])

    def __call__(self, data: bytes, name: str, source: Optional[str] = None) -> LoadedDocument:
        from .engines import AUDIO_SUFFIXES, VIDEO_SUFFIXES

        suffix = PurePosixPath(name).suffix.lower()
        if suffix == ".pdf":
            return self._pdf(data, name, source)
        if suffix in AUDIO_SUFFIXES or suffix in VIDEO_SUFFIXES:
            return self._sound_file(data, name, source, video=suffix in VIDEO_SUFFIXES)
        return self._read(data, name, source)

    # -- reading one piece

    def _read(self, data: bytes, name: str, source: Optional[str]) -> LoadedDocument:
        from . import _takes_source, coerce

        for attempt in range(1, self.tries + 1):
            try:
                answer = (
                    self.reader(data, name, source=source)
                    if _takes_source(self.reader)
                    else self.reader(data, name)
                )
                return coerce(answer, name)
            except ExtractionError as exc:
                if attempt == self.tries or not _worth_again(exc):
                    raise
                time.sleep(self._pause * attempt)
        raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises

    def _each(
        self, pieces: Sequence[Tuple[bytes, str]], whole: str, source: Optional[str]
    ) -> List[LoadedDocument]:
        """Every piece read, several at a time, in the order they were cut."""

        def read(numbered: Tuple[int, Tuple[bytes, str]]) -> LoadedDocument:
            index, (data, name) = numbered
            try:
                return self._read(data, name, source)
            except ExtractionError as exc:
                raise ExtractionError(
                    f"piece {index} of {len(pieces)} of {whole} did not read: {exc}",
                    route=exc.route,
                    status=exc.status,
                ) from exc

        numbered = list(enumerate(pieces, start=1))
        if len(numbered) == 1 or self.at_once == 1:
            return [read(one) for one in numbered]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(self.at_once, len(numbered))) as pool:
            return list(pool.map(read, numbered))

    # -- PDFs

    def _pdf(self, data: bytes, name: str, source: Optional[str]) -> LoadedDocument:
        try:
            import pypdf
        except ImportError as exc:
            raise DependencyError("pypdf", "documents") from exc
        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
            total = 0 if reader.is_encrypted else len(reader.pages)
        except Exception:  # noqa: BLE001 - a PDF not read here is still the reader's to try
            total = 0
        if total == 0 or (total <= self.pages and len(data) <= self.max_bytes):
            return self._read(data, name, source)
        cut: List[Tuple[int, bytes]] = []
        for start in range(0, total, self.pages):
            cut.extend(self._pdf_pieces(reader, start, min(start + self.pages, total)))
        stem = Path(name).stem
        docs = self._each(
            [(piece, f"{stem}.pages-{first + 1}.pdf") for first, piece in cut], name, source
        )
        return self._join_pages(
            name, total, [(first, doc) for (first, _piece), doc in zip(cut, docs)], source
        )

    def _pdf_pieces(self, reader: Any, start: int, end: int) -> List[Tuple[int, bytes]]:
        """Pages ``start`` to ``end`` as a PDF of their own, halved again while it is too big to send."""
        import pypdf

        writer = pypdf.PdfWriter()
        for index in range(start, end):
            writer.add_page(reader.pages[index])
        buffer = io.BytesIO()
        writer.write(buffer)
        data = buffer.getvalue()
        if len(data) > self.max_bytes and end - start > 1:
            middle = (start + end) // 2
            return self._pdf_pieces(reader, start, middle) + self._pdf_pieces(reader, middle, end)
        return [(start, data)]

    def _join_pages(
        self,
        name: str,
        total: int,
        parts: Sequence[Tuple[int, LoadedDocument]],
        source: Optional[str] = None,
    ) -> LoadedDocument:
        texts: List[str] = []
        pages: List[Tuple[int, int]] = []
        headings: List[Tuple[int, str, int]] = []
        figures: List[Tuple[int, Dict[str, Any]]] = []
        labels: Dict[int, str] = {}
        metadata: Dict[str, Any] = {}
        offset = 0
        for first, doc in parts:
            if texts:
                offset += 2
            pages.extend((offset + int(o), first + int(n)) for o, n in doc.pages)
            headings.extend((offset + int(o), str(h), int(level)) for o, h, level in doc.headings)
            figures.extend((offset + int(o), dict(info)) for o, info in doc.figures)
            labels.update({first + int(n): str(label) for n, label in doc.page_labels.items()})
            texts.append(doc.text)
            offset += len(doc.text)
            _merge(metadata, doc.metadata, first)
        # The whole file's name and address, not the first piece's.
        metadata.update(source=source or name, filename=name, pages=total, pieces=len(parts))
        # A printed number that is the page's own place says nothing a citation needs.
        labels = {n: label for n, label in labels.items() if label != str(n)}
        from ..ingest import _footnotes_unglued

        # The whole file, held to each page's notes once more: whatever a
        # piece's reader wrote last, a footnote mark left glued to its word
        # is put right here, where every page is as it will be kept.
        return _footnotes_unglued(
            LoadedDocument(
                text="\n\n".join(texts),
                pages=pages,
                headings=headings,
                metadata=metadata,
                figures=figures,
                page_labels=labels,
            )
        )

    # -- sound and video

    def _sound_file(
        self, data: bytes, name: str, source: Optional[str], *, video: bool
    ) -> LoadedDocument:
        sound = self._sound if self._sound is not None else FfmpegSound()
        piece = self.minutes * 60.0
        suffix = PurePosixPath(name).suffix.lower()
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / f"original{suffix}"
            original.write_bytes(data)
            seconds = sound.duration(str(original))
            if seconds is not None and seconds <= piece and len(data) <= self.max_bytes:
                return self._read(data, name, source)
            wav = Path(folder) / "sound.wav"
            sound.to_wav(str(original), str(wav))
            total = _wav_seconds(wav)
            cuts = cut_points(total, piece, sound.silences(str(wav))) if total > piece else []
            cut = _wav_pieces(wav, [0.0, *cuts, total])
        stem = Path(name).stem
        docs = self._each(
            [
                (piece_data, f"{stem}.part{index}.wav")
                for index, (_start, piece_data) in enumerate(cut, start=1)
            ],
            name,
            source,
        )
        return self._join_sound(
            name,
            [(start, doc) for (start, _data), doc in zip(cut, docs)],
            total,
            video=video,
            source=source,
        )

    def _join_sound(
        self,
        name: str,
        parts: Sequence[Tuple[float, LoadedDocument]],
        total: float,
        *,
        video: bool,
        source: Optional[str] = None,
    ) -> LoadedDocument:
        from .engines import segments_to_document

        timed: List[Tuple[float, float, str]] = []
        untimed: List[str] = []
        metadata: Dict[str, Any] = {}
        for start, doc in parts:
            if doc.segments:
                timed.extend(
                    (float(a) + start, float(b) + start, str(text)) for a, b, text in doc.segments
                )
            elif doc.text.strip():
                # A reader that sent no times: the words are kept, in order.
                untimed.append(doc.text.strip())
            _merge(metadata, doc.metadata, 0)
        joined = segments_to_document(timed) if timed else LoadedDocument(text="\n\n".join(untimed))
        joined.metadata = {**metadata, **joined.metadata}
        joined.metadata.update(
            source=source or name,
            filename=name,
            pieces=len(parts),
            duration_seconds=round(total, 2),
        )
        if video:
            joined.metadata["video"] = True
        return joined
