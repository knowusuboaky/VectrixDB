"""Extractors for what is not text yet: pictures of pages, speech, video.

Local engines first. Each needs one optional package and says which when it
is missing, the way the PDF and DOCX readers do:

* :class:`RapidOcr` reads images, and the scanned pages of a PDF, with
  ``pip install vectrixdb[ocr]``.
* :class:`Whisper` transcribes audio with ``pip install vectrixdb[asr]``.
* :class:`Video` takes the sound out of a video and hands it to an audio
  extractor, with ``pip install vectrixdb[video]``; ``vectrixdb[ffmpeg]``
  alone when the audio extractor is a service.

Then the same three jobs done by a cloud service the host already has:
:class:`AzureDocumentIntelligence`, :class:`AzureSpeech`, :class:`Textract`
and :class:`Transcribe`. Each is built around a client, or a key, the host
made; nothing here reads a credential from the environment.

Speech becomes pages by the minute, and every phrase keeps its time, so a
hit in a transcript is cited ``call.wav#t=665``, the second it starts at,
and a person is shown ``call.wav, 11:05``.
"""

from __future__ import annotations

import html
import json
import os
import re
import logging
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

from ..citations import _clock
from ..exceptions import DependencyError, ExtractionError
from ..ingest import LoadedDocument, join_pages
from .layout import drop_running_lines, looks_blank, mend_sentence_breaks, reading_order

__all__ = [
    "AUDIO_SUFFIXES",
    "IMAGE_SUFFIXES",
    "VIDEO_SUFFIXES",
    "AzureDocumentIntelligence",
    "AzureImageAnalysis",
    "AzureSpeech",
    "RapidOcr",
    "Textract",
    "Transcribe",
    "Video",
    "Whisper",
    "segments_to_document",
    "transcript_markdown",
]


logger = logging.getLogger("vectrixdb.extract")

# ============================================================================
# SETTINGS: the suffixes of pictures, sound and video, and the segment type
# ============================================================================
#
# Which suffixes each kind of engine takes, and what a timed segment of speech
# is.

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")
AUDIO_SUFFIXES = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac")
VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv", ".avi")

Segment = Tuple[float, float, str]

#: A phrase that starts a line of its own in a transcript: a voice told apart, or the screen.
_OWN_LINE = re.compile(r"^(?:Speaker \d+|On screen):")


# ============================================================================
# TIMED SPEECH AS A DOCUMENT
# ============================================================================
#
# INPUT   segments of timed speech
# OUTPUT  a document whose pages are minutes; a transcript written for a
#         person to read, what it is, the whole text, then every phrase timed
#
# Minutes as pages, so a citation into a recording reads like one into a book.


def segments_to_document(segments: Iterable[Segment], seconds_per_page: float = 60.0) -> LoadedDocument:
    """Timed speech as a document whose pages are minutes.

    Everything said within one minute is one paragraph, and minute ``n``
    starts page ``n + 1``. A silent minute has no text and still counts, so
    page twelve is always the twelfth minute.
    """
    segments = list(segments)
    buckets: Dict[int, List[str]] = {}
    for start, _end, text in segments:
        cleaned = " ".join(str(text).split())
        if cleaned:
            buckets.setdefault(int(max(float(start), 0.0) // seconds_per_page), []).append(cleaned)
    parts: List[str] = []
    pages: List[Tuple[int, int]] = []
    offset = 0
    for minute in sorted(buckets):
        if parts:
            offset += 2
        pages.append((offset, minute + 1))
        paragraph = previous = ""
        for piece in buckets[minute]:
            if paragraph:
                # Each voice's turn, and what the screen shows, on a line of its own.
                paragraph += "\n" if _OWN_LINE.match(piece) or previous.startswith("On screen:") else " "
            paragraph += piece
            previous = piece
        parts.append(paragraph)
        offset += len(paragraph)
    duration = max((float(end) for _, end, _ in segments), default=0.0)
    metadata: Dict[str, Any] = {"transcript": True, "seconds_per_page": seconds_per_page}
    if duration:
        metadata["duration_seconds"] = round(duration, 2)
    timed = [
        (round(max(float(a), 0.0), 3), round(max(float(b), 0.0), 3), " ".join(str(t).split()))
        for a, b, t in segments
        if str(t).strip()
    ]
    return LoadedDocument(text="\n\n".join(parts), pages=pages, metadata=metadata, segments=timed)


def transcript_markdown(doc: LoadedDocument) -> str:
    """A transcript written for a person to read: what it is, the whole text, then every phrase timed.

    ::

        # YouTube: Quarterly results, explained

        - URL: https://www.youtube.com/watch?v=...
        - Channel: Northwind Bank
        - Duration: 3:32
        - Language: en-US

        ---

        ## Full Text

        Welcome to the quarterly results. Revenue grew in every region.

        ## Segments

        **[0:00 → 0:04]** Welcome to the quarterly results.

        **[0:04 → 0:09]** Revenue grew in every region.

    A YouTube video is headed ``YouTube:`` and its title; any other
    recording ``Transcript:`` and its file name. A line whose value is not
    known is left out rather than written as "Unknown". This is for people:
    what a collection indexes is the document itself, whose paragraphs and
    minute pages are what a chunk and a citation want.
    """
    about = doc.metadata or {}
    if about.get("kind") == "youtube":
        heading = f"YouTube: {about.get('title') or about.get('youtube_id') or 'video'}"
    else:
        heading = f"Transcript: {about.get('filename') or 'recording'}"
    details: List[str] = []
    source = str(about.get("source") or "")
    if source.startswith(("http://", "https://")):
        details.append(f"- URL: {source}")
    if about.get("channel"):
        details.append(f"- Channel: {about['channel']}")
    duration = about.get("duration") or about.get("duration_seconds")
    if duration:
        details.append(f"- Duration: {_clock(duration)}")
    if about.get("language"):
        details.append(f"- Language: {about['language']}")

    lines: List[str] = [f"# {heading}", ""]
    if details:
        lines += details + [""]
    lines += ["---", "", "## Full Text", "", doc.text.strip() or "_(no speech detected)_", ""]
    if doc.segments:
        lines += ["## Segments", ""]
        for start, end, text in doc.segments:
            lines += [f"**[{_clock(start)} → {_clock(end)}]** {text}", ""]
    return "\n".join(lines).rstrip() + "\n"


def _temp_file(data: bytes, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:  # pragma: no cover - already gone
        pass


# ============================================================================
# ON THIS MACHINE: pictures of text, speech, and video
# ============================================================================
#
# INPUT   a picture, a recording, a video
# OUTPUT  text read by RapidOCR; speech to text with faster-whisper; a video's
#         sound handed to an audio extractor
#
# Local engines first. Each needs one optional package and says which when it
# is missing.


class RapidOcr:
    """Pictures of text, read on this machine.

    An image is read whole. A PDF is read page by page: a page that already
    has text keeps it, and a page that has next to none, a scan, is drawn
    and read. ``engine`` takes image bytes and returns lines of text, top to
    bottom; left out, it is RapidOCR. ``rasterize`` takes PDF bytes and
    yields one PNG per page; left out, it is pypdfium2.

    What RapidOCR finds is put in the order a person reads it, by
    :func:`~vectrixdb.extract.layout.reading_order`: a page in columns is read
    down one column and then the next. A page with nothing on it is not sent
    to the engine at all (``skip_blank``), which costs nothing here and
    matters when ``engine`` is a vision model, since those invent text for an
    empty page. Running headers and footers are taken out by
    :func:`~vectrixdb.extract.layout.drop_running_lines` and named in
    ``running_lines``; ``ocr_pages`` says which pages were read by OCR, so a
    chunk knows whether its own page was.
    """

    label = "rapidocr"

    def __init__(
        self,
        engine: Optional[Callable[[bytes], Sequence[str]]] = None,
        rasterize: Optional[Callable[[bytes], Iterable[bytes]]] = None,
        page_text: Optional[Callable[[bytes], List[str]]] = None,
        min_chars: int = 24,
        skip_blank: bool = True,
        drop_running: bool = True,
    ) -> None:
        self._engine = engine
        self._rasterize = rasterize
        self._page_text = page_text
        self.min_chars = int(min_chars)
        self.skip_blank = bool(skip_blank)
        self.drop_running = bool(drop_running)

    def suffixes(self) -> List[str]:
        return sorted(IMAGE_SUFFIXES + (".pdf",))

    # -- the parts that need a package

    def _lines(self, image: bytes) -> Sequence[str]:
        if self._engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as exc:
                raise DependencyError("rapidocr-onnxruntime", "ocr") from exc
            model = RapidOCR()

            def run(data: bytes) -> Sequence[str]:
                result, _elapsed = model(data)
                # Sorted by the top of each box, a page in two columns read
                # L1 R1 L2 R2, and the words of one line came out of order.
                return [mend_sentence_breaks(line) for line in reading_order([(r[0], str(r[1])) for r in result or []])]

            self._engine = run
        return self._engine(image)

    def _pages_as_images(self, pdf: bytes) -> Iterable[bytes]:
        if self._rasterize is not None:
            return self._rasterize(pdf)
        try:
            import io

            import pypdfium2
        except ImportError as exc:
            raise DependencyError("pypdfium2", "ocr") from exc

        from .pdf_text import PDFIUM_LOCK

        def draw() -> Iterable[bytes]:
            # Every PDFium call under the one lock; none held while a page is read.
            with PDFIUM_LOCK:
                document = pypdfium2.PdfDocument(pdf)
                count = len(document)
            try:
                for index in range(count):
                    buffer = io.BytesIO()
                    with PDFIUM_LOCK:
                        page = document[index]
                        try:
                            picture = page.render(scale=2.0).to_pil()
                        finally:
                            page.close()
                    picture.save(buffer, format="PNG")
                    yield buffer.getvalue()
            finally:
                with PDFIUM_LOCK:
                    document.close()

        return draw()

    def _text_layer(self, pdf: bytes) -> List[str]:
        if self._page_text is not None:
            return self._page_text(pdf)
        try:
            import io

            import pypdf
        except ImportError:
            return []
        try:
            return [(page.extract_text() or "") for page in pypdf.PdfReader(io.BytesIO(pdf)).pages]
        except Exception:
            return []

    # -- the extractor

    def __call__(self, data: bytes, name: str) -> LoadedDocument:
        if Path(name).suffix.lower() != ".pdf":
            if self.skip_blank and looks_blank(data):
                return LoadedDocument(text="", metadata={"ocr": True, "pages_ocr": 0, "pages_blank": 1})
            text = "\n".join(self._lines(data))
            return LoadedDocument(text=text, metadata={"ocr": True, "pages_ocr": 1})
        layer = self._text_layer(data)
        needs = [i for i, t in enumerate(layer) if len(t.strip()) < self.min_chars] if layer else None
        if needs == []:
            return self._joined(layer, [], 0)
        read: List[str] = []
        by_ocr: List[int] = []
        blank = 0
        for index, image in enumerate(self._pages_as_images(data)):
            if needs is not None and index not in needs and index < len(layer):
                read.append(layer[index])
            elif self.skip_blank and looks_blank(image):
                read.append("")
                blank += 1
            else:
                read.append("\n".join(self._lines(image)))
                by_ocr.append(index + 1)
        return self._joined(read, by_ocr, blank)

    def _joined(self, page_texts: Sequence[str], by_ocr: List[int], blank: int) -> LoadedDocument:
        dropped: List[str] = []
        if self.drop_running:
            page_texts, dropped = drop_running_lines(page_texts)
        text, pages = join_pages(page_texts)
        metadata: Dict[str, Any] = {"pages": len(pages), "ocr": bool(by_ocr), "pages_ocr": len(by_ocr)}
        if by_ocr:
            metadata["ocr_pages"] = by_ocr
        if blank:
            metadata["pages_blank"] = blank
        if dropped:
            metadata["running_lines"] = dropped
        return LoadedDocument(text=text, pages=pages, metadata=metadata)




class Whisper:
    """Speech to text on this machine, with faster-whisper.

    ``engine`` takes a path to an audio file and returns ``(start, end,
    text)`` segments in seconds; left out, it is a ``WhisperModel`` of the
    size named, loaded once and kept.
    """

    label = "faster-whisper"

    def __init__(
        self,
        model: str = "base",
        language: Optional[str] = None,
        engine: Optional[Callable[[str], Iterable[Segment]]] = None,
        seconds_per_page: float = 60.0,
    ) -> None:
        self.model = model
        self.language = language
        self._engine = engine
        self._loaded: Any = None
        self.seconds_per_page = float(seconds_per_page)

    def suffixes(self) -> List[str]:
        return sorted(AUDIO_SUFFIXES)

    def transcribe_file(self, path: str) -> List[Segment]:
        if self._engine is not None:
            return [(float(a), float(b), str(t)) for a, b, t in self._engine(path)]
        # The model on the instance and the language read at the call, not
        # captured when the model loaded. It used to be captured, so a copy
        # made to listen in French shared the loaded model and went on
        # transcribing in whatever the original was set to, with no error.
        if self._loaded is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise DependencyError("faster-whisper", "asr") from exc
            self._loaded = WhisperModel(self.model)
        found, _info = self._loaded.transcribe(path, language=self.language)
        return [(float(s.start), float(s.end), str(s.text)) for s in found]

    def __call__(self, data: bytes, name: str) -> LoadedDocument:
        path = _temp_file(data, Path(name).suffix or ".wav")
        try:
            segments = self.transcribe_file(path)
        finally:
            _remove(path)
        return segments_to_document(segments, self.seconds_per_page)


class _NoSound(Exception):
    """A video with no sound track: nothing was said, which is not a broken file."""


def _moments(changes: Sequence[Tuple[float, float]], duration: float, most: int, scene: float = 0.02) -> List[float]:
    """The seconds of a video worth a frame, from how much each look at it changed, at most ``most``.

    ``changes`` is ``(seconds, score)``, a score from 0 for no change to 1
    for a new picture. A slide whose words change scores about 0.03 and a
    cut in filmed footage 0.3 or more; ``scene`` is the least that counts.
    The start always counts. Changes within two seconds of each other are
    one moment, a transition or a build, at its largest. When there are
    more moments than ``most``, the video is cut into ``most`` stretches and
    the largest change in each is kept, then the largest of the rest, so
    the frames cover the whole of it. Each frame is taken half a second
    after its change, once the new picture has settled.
    """
    if most <= 0:
        return []
    found: List[Tuple[float, float]] = [(0.0, 1.0)]
    for seconds, score in sorted(changes):
        if score < scene:
            continue
        if seconds - found[-1][0] < 2.0:
            if score > found[-1][1] and found[-1][0] > 0.0:
                found[-1] = (seconds, score)
            continue
        found.append((seconds, score))
    if len(found) > most:
        size = max(duration, found[-1][0] + 1.0) / most
        kept = []
        for stretch in range(most):
            inside = [m for m in found if stretch * size <= m[0] < (stretch + 1) * size]
            if inside:
                kept.append(max(inside, key=lambda m: m[1]))
        rest = sorted((m for m in found if m not in kept), key=lambda m: -m[1])
        found = sorted(kept + rest[: most - len(kept)])
    last = max(0.0, duration - 0.1) if duration else float("inf")
    return [round(min(seconds + 0.5, last), 3) for seconds, _score in found]


class Video:
    """The sound of a video, handed to an audio extractor, and what its screen shows.

    ``audio`` is any extractor that takes audio bytes, :class:`Whisper` by
    default. ``ffmpeg`` takes the path of a video and the path to write a WAV
    to; left out, it runs the ffmpeg binary that imageio-ffmpeg ships.

    ``frames`` is a picture reader, called as ``frames(png, name)``. Given
    one, a frame is taken at the start and at each moment the picture
    changes, at most ``max_frames`` spread over the video, and what it says
    joins the transcript at its second as an ``On screen:`` line; a slide
    still on screen is not said twice. ``scene`` is the least change that
    counts, 0 to 1: a slide whose words change scores about 0.03, a cut in
    filmed footage 0.3 or more (see :func:`_moments`). ``grabber`` stands in
    for ffmpeg there: it takes the video's path and returns ``(seconds,
    png)`` pairs. A video with no sound track is read for its screen alone.
    """

    label = "video"

    def __init__(
        self,
        audio: Optional[Callable[..., Any]] = None,
        ffmpeg: Optional[Callable[[str, str], None]] = None,
        frames: Optional[Callable[..., Any]] = None,
        max_frames: int = 12,
        scene: float = 0.02,
        grabber: Optional[Callable[[str], List[Tuple[float, bytes]]]] = None,
    ) -> None:
        self.audio = audio if audio is not None else Whisper()
        self._ffmpeg = ffmpeg
        #: What reads a frame's words: a picture reader, Document Intelligence say. None reads no frames.
        self.frames = frames
        self.max_frames = max(0, int(max_frames))
        self.scene = float(scene)
        self._grabber = grabber

    def suffixes(self) -> List[str]:
        return sorted(VIDEO_SUFFIXES)

    def _extract_audio(self, video: str, wav: str) -> None:
        if self._ffmpeg is not None:
            self._ffmpeg(video, wav)
            return
        try:
            import imageio_ffmpeg
        except ImportError as exc:
            raise DependencyError("imageio-ffmpeg", "ffmpeg") from exc
        command = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000", wav]
        done = subprocess.run(command, capture_output=True)  # noqa: S603 - fixed argv, no shell
        if done.returncode != 0:
            said = done.stderr.decode("utf-8", errors="replace")
            if "Audio:" not in said and ("Video:" in said or "does not contain any stream" in said):
                raise _NoSound()
            tail = said.strip().splitlines()[-1:] or ["no output"]
            raise ExtractionError(f"ffmpeg could not read the audio of the video: {tail[0]}")

    def _grab(self, video: str) -> List[Tuple[float, bytes]]:
        """A frame at the start and at each moment the picture changes, as ``(seconds, png)``, at most ``max_frames``.

        Two passes of ffmpeg. The first looks at the whole video twice a
        second, small, and scores how much each look differs from the one
        before; :func:`_moments` picks the seconds worth a frame from those
        scores. The second takes each of those frames at full size, half a
        second after its change, once a slide has finished coming in.
        """
        if self._grabber is not None:
            return list(self._grabber(video))[: self.max_frames]
        try:
            import imageio_ffmpeg
        except ImportError as exc:
            raise DependencyError("imageio-ffmpeg", "ffmpeg") from exc
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        looked = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [exe, "-hide_banner", "-nostats", "-i", video, "-an", "-sn", "-dn",
             "-vf", "fps=2,scale=320:-2,select='gt(scene\\,0)',metadata=print:key=lavfi.scene_score", "-f", "null", "-"],
            capture_output=True,
        )
        said = looked.stderr.decode("utf-8", errors="replace")
        changes = [(float(t), float(s)) for t, s in re.findall(r"pts_time:([0-9.]+)[^\n]*\n[^\n]*lavfi\.scene_score=([0-9.]+)", said)]
        length = re.search(r"Duration: (\d+):(\d+):([0-9.]+)", said)
        duration = int(length.group(1)) * 3600 + int(length.group(2)) * 60 + float(length.group(3)) if length else 0.0
        duration = duration or max((t for t, _ in changes), default=0.0)
        taken: List[Tuple[float, bytes]] = []
        for seconds in _moments(changes, duration, self.max_frames, self.scene):
            drawn = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [exe, "-hide_banner", "-loglevel", "error", "-ss", f"{seconds:.3f}", "-i", video, "-frames:v", "1",
                 "-vf", "scale='min(1600\\,iw)':-2", "-f", "image2pipe", "-vcodec", "png", "-"],
                capture_output=True,
            )
            if drawn.returncode == 0 and drawn.stdout.startswith(b"\x89PNG"):
                taken.append((seconds, drawn.stdout))
        return taken

    def _screen(self, video: str, name: str) -> List[Segment]:
        """What the screen says, at the second each picture settles: "On screen: LAUNCH AMERICA"."""
        if self.frames is None or not self.max_frames:
            return []
        from . import coerce

        said: List[Segment] = []
        last = ""
        for seconds, png in self._grab(video):
            try:
                read = coerce(self.frames(png, f"{Path(name).stem}-{int(seconds)}s.png"), name)
            except DependencyError as exc:
                # Every frame would fail the same way: said once, and the sound read on its own.
                logger.warning("the screen of %s is not read: %s", name, exc)
                break
            except Exception as exc:  # noqa: BLE001 - a frame that cannot be read is not a video that cannot
                logger.info("a frame of %s at %.0fs was not read: %s", name, seconds, exc)
                continue
            words = " ".join(str(read.text or "").split())
            if not words or words == last:
                continue  # the same slide still on screen
            last = words
            said.append((seconds, seconds + 1.0, f"On screen: {words}"))
        return said

    def __call__(self, data: bytes, name: str) -> LoadedDocument:
        from . import coerce

        video = _temp_file(data, Path(name).suffix or ".mp4")
        wav = video + ".wav"
        spoken: List[Segment] = []
        sound = True
        try:
            try:
                self._extract_audio(video, wav)
                heard = Path(wav).read_bytes()
            except _NoSound:
                heard, sound = b"", False
            screen = self._screen(video, name)
        finally:
            _remove(video)
        try:
            if heard:
                doc = coerce(self.audio(heard, Path(name).stem + ".wav"), name)
                spoken = list(doc.segments or [])
            else:
                doc = LoadedDocument(text="")
        finally:
            _remove(wav)
        if screen:
            # What was said and what was shown, in the order they happened;
            # a slide that appears as someone speaks is read first.
            metadata = dict(doc.metadata)
            timed = sorted(spoken + screen, key=lambda s: (s[0], not s[2].startswith("On screen:")))
            doc = segments_to_document(timed, float(metadata.get("seconds_per_page") or 60.0))
            doc.metadata = {**metadata, **doc.metadata, "on_screen": len(screen)}
        doc.metadata["video"] = True
        if not sound:
            doc.metadata["sound_track"] = False
        if not spoken:
            doc.metadata["speech"] = "none"
        return doc


# ============================================================================
# AZURE: layout, vision, and speech
# ============================================================================
#
# INPUT   a page, a picture, a recording, with an endpoint and a key or a
#         managed identity
# OUTPUT  layout analysis as Markdown with pages, nothing in it that is not
#         the document's; what a picture shows, in words; speech to text by
#         fast transcription
#
# The layout model's own markers are taken out, so the Markdown kept is the
# document and not the model's notes on it.

#: What the layout model writes about a page rather than on it: its running
#: header and footer, the number printed on it, where it ends.
_LAYOUT_COMMENT = re.compile(r'<!--\s*(PageHeader|PageFooter|PageNumber|PageBreak)(?:\s*=\s*"([^"]*)")?\s*-->', re.IGNORECASE)
_LAYOUT_FIGURE = re.compile(r"<figure\b[^>]*>(.*?)</figure\s*>", re.IGNORECASE | re.DOTALL)
_LAYOUT_CAPTION = re.compile(r"<figcaption\b[^>]*>(.*?)</figcaption\s*>", re.IGNORECASE | re.DOTALL)
_LAYOUT_TAG = re.compile(r"<[^>]+>")
#: A printed page number as a citation wants it: ``12`` of ``Page 12``, ``iv``, ``C1``, ``A-3``.
_PRINTED = re.compile(r"^(?:page\s+)?([A-Za-z]{0,2}[-.]?\d{1,5}|[ivxlcdm]{1,8})\b", re.IGNORECASE)


def _printed_number(value: str) -> Optional[str]:
    text = " ".join(str(value or "").split())
    found = _PRINTED.match(text)
    if found:
        return found.group(1)
    return text if 0 < len(text) <= 8 else None


def _page_of(pages: Sequence[Tuple[int, int]], offset: int) -> Optional[int]:
    found = None
    for start, number in pages:
        if start <= offset:
            found = number
        else:
            break
    return found


def _layout_document(
    content: str,
    pages: Sequence[Tuple[int, int]],
    figures: Sequence[Tuple[Optional[int], str, Optional[bytes]]] = (),
) -> LoadedDocument:
    """The layout model's Markdown as a document, with nothing in it that is not the document's.

    The page header and footer it marks are taken out, so a report's title
    at the top of every page is not in every chunk; the number printed on
    each page is kept as that page's label. A ``<figure>`` becomes a figure
    line with the words the model read in it, and the picture cut out of the
    page when there is one, for a describer to say more; a chart drawn in
    the PDF rather than pasted in as a picture is a figure here too. Tables
    become rows, the headings it found are headings.
    """
    from ..ingest import _apply_edits, _html_table_edits, _markdown_headings, _table_edits, figure_line

    pages = sorted((int(o), int(n)) for o, n in pages)
    labels: Dict[int, str] = {}
    edits: List[Tuple[int, int, str]] = []
    for found in _LAYOUT_COMMENT.finditer(content):
        if found.group(1).lower() == "pagenumber":
            page, printed = _page_of(pages, found.start()), _printed_number(found.group(2) or "")
            if page is not None and printed:
                labels.setdefault(page, printed)
        edits.append((found.start(), found.end(), ""))
    by_start = {start: (figure_id, crop) for start, figure_id, crop in figures if start is not None}
    in_order = [(figure_id, crop) for _start, figure_id, crop in figures]
    marks: List[Tuple[int, Dict[str, Any]]] = []
    images: Dict[str, bytes] = {}
    for index, found in enumerate(_LAYOUT_FIGURE.finditer(content)):
        inside = found.group(1)
        caption_of = _LAYOUT_CAPTION.search(inside)
        caption = " ".join(_LAYOUT_TAG.sub(" ", html.unescape(caption_of.group(1))).split()) if caption_of else ""
        if caption_of:
            inside = inside[: caption_of.start()] + inside[caption_of.end():]
        words = [w for w in (" ".join(_LAYOUT_TAG.sub(" ", html.unescape(line)).split()) for line in inside.splitlines()) if w]
        figure_id, crop = by_start.get(found.start()) or (in_order[index] if index < len(in_order) else (str(index + 1), None))
        src = f"figure-{figure_id}.png"
        if crop:
            images[src] = crop
        line = figure_line(caption, src)
        block = line + ("\nThe words in it read: " + "; ".join(words) + "." if words else "")
        edits.append((found.start(), found.end(), "\n\n" + block + "\n\n"))
        # Not described yet: a describer given the picture replaces the words with more.
        marks.append((found.start(), {"caption": line[9:-1], "src": src, "described": False}))
    text, (moved, marks) = _apply_edits(content, edits, list(pages), marks)
    marks = [(o + 2, info) for o, info in marks]
    text, (moved, marks) = _apply_edits(text, _table_edits(text) + _html_table_edits(text), moved, marks)
    # What the edits left: blank lines in runs, and space at either end.
    lead, end = len(text) - len(text.lstrip()), len(text.rstrip())
    edges = ([(0, lead, "")] if lead else []) + ([(end, len(text), "")] if lead < end < len(text) else [])
    text, (moved, marks) = _apply_edits(text, edges, moved, marks)
    runs = [(m.start(), m.end(), "\n\n") for m in re.finditer(r"\n[ \t]*\n(?:[ \t]*\n)+", text)]
    text, (moved, marks) = _apply_edits(text, runs, moved, marks)
    labels = {n: label for n, label in labels.items() if label != str(n)}
    return LoadedDocument(
        text=text,
        pages=[(int(o), int(n)) for o, n in moved],
        headings=_markdown_headings(text),
        figures=[(int(o), dict(info)) for o, info in marks],
        images=images,
        page_labels=labels,
    )


class AzureDocumentIntelligence:
    """Layout analysis by Azure AI Document Intelligence, as Markdown with pages.

    ``client`` is a ``DocumentIntelligenceClient`` the host built. The
    service returns Markdown, tables and headings included, and the span of
    every page inside it, so a scanned PDF comes back cited by page.
    Offsets are asked for in code points, which is what a Python string
    counts in; the default, UTF-16 units, drifts after the first emoji.

    With the layout model the page headers and footers it finds are taken
    out, the number printed on each page is kept, and every figure is a
    figure line with the words read in it. ``crops`` asks the service for
    each figure cut out of its page too, charts drawn in the PDF included,
    so a describer can say what it shows; an SDK or a service that cannot
    give them leaves the words.
    """

    label = "azure-document-intelligence"

    def __init__(self, client: Any, model: str = "prebuilt-layout", *, crops: bool = False) -> None:
        self.client = client
        self.model = model
        self.crops = bool(crops)

    def suffixes(self) -> List[str]:
        return sorted(IMAGE_SUFFIXES + (".pdf", ".docx", ".pptx", ".xlsx", ".html"))

    def _analyze(self, data: bytes) -> Tuple[Any, bool]:
        """The analysis under way, and whether it was asked for the figures' pictures."""
        asked: Dict[str, Any] = {
            "content_type": "application/octet-stream",
            "output_content_format": "markdown",
            "string_index_type": "unicodeCodePoint",
        }
        if self.crops:
            asked["output"] = ["figures"]
        try:
            return self.client.begin_analyze_document(self.model, bytes(data), **asked), self.crops
        except TypeError:
            if "output" not in asked:
                raise
            # An SDK too old to be asked for the figures: the words in them are still read.
            asked.pop("output")
            return self.client.begin_analyze_document(self.model, bytes(data), **asked), False

    def _crop(self, poller: Any, result: Any, figure_id: str) -> Optional[bytes]:
        """One figure cut out of its page, or None when the service or the SDK cannot give it."""
        try:
            operation = (getattr(poller, "details", None) or {}).get("operation_id")
            if not operation:
                return None
            model = _field(result, "model_id") or _field(result, "modelId") or self.model
            got = self.client.get_analyze_result_figure(model_id=model, result_id=operation, figure_id=figure_id)
            data = bytes(got) if isinstance(got, (bytes, bytearray)) else b"".join(got)
            return data or None
        except Exception:  # noqa: BLE001 - a figure without its picture keeps its words
            return None

    def __call__(self, data: bytes, name: str) -> LoadedDocument:
        poller, cropped = self._analyze(data)
        result = poller.result()
        content = _field(result, "content") or ""
        pages: List[Tuple[int, int]] = []
        for page in _field(result, "pages") or []:
            spans = _field(page, "spans") or []
            number = _field(page, "page_number") or _field(page, "pageNumber")
            if spans and number is not None:
                pages.append((int(_field(spans[0], "offset") or 0), int(number)))
        pages.sort()
        figures: List[Tuple[Optional[int], str, Optional[bytes]]] = []
        for index, figure in enumerate(_field(result, "figures") or [], start=1):
            figure_id = str(_field(figure, "id") or index)
            spans = _field(figure, "spans") or []
            start = int(_field(spans[0], "offset") or 0) if spans else None
            figures.append((start, figure_id, self._crop(poller, result, figure_id) if cropped else None))
        doc = _layout_document(content, pages, figures)
        doc.metadata = {"ocr": True, "pages": len(pages)}
        return doc


class AzureImageAnalysis:
    """What a picture shows, in words, from Azure AI Vision's Image Analysis.

    This is a describer, not an extractor: it is what
    ``Vectrix(describe_figures=...)`` wants, a callable of the image bytes
    and the context the library gathered around the figure. What it returns
    is written under the figure in the Markdown and kept there, so it is
    paid for once however often the document is read again.

    Two features of one call do the work and they are good at different
    things. The caption says what the picture *is*, in a sentence. The dense
    captions say what is in each part of it. The OCR reads the words printed
    inside it, which on a chart are its title, its axis labels and its
    numbers, and those are what somebody actually searches for: "the chart
    where revenue falls in the fourth quarter" matches none of a caption
    that reads "Figure 12".

    The words come back through
    :func:`~vectrixdb.extract.layout.reading_order`, so a chart with a
    legend down one side and a table of figures beneath it reads the way a
    person reads it rather than the order the service happened to emit, and
    through :func:`~vectrixdb.extract.layout.mend_sentence_breaks`, so a
    sentence that lost its space is searchable again. That is the same
    treatment :class:`RapidOcr` gives a scanned page, for the same reason.

    Image Analysis and not a language model, deliberately. It needs no GPT
    quota, which a new subscription does not have, and its free tier covers
    five thousand pictures a month. What it will not do is reason about a
    chart: it reports what is drawn and what is written, not what the trend
    means.

    Captioning is offered in some regions and not others. Where it is not,
    the call is made again without it and the OCR still comes back, so a
    figure is described by the words in it rather than not at all.

    ``describe_figures`` has already dropped the images not worth asking
    about, the small, the long and thin, the blank, and the one that turns
    up on every page, so what arrives here is a figure somebody might search
    for.
    """

    label = "azure-image-analysis"

    #: The one Image Analysis 4.0 version these features are stable on.
    API = "2024-02-01"
    #: Below this the service is guessing, and a guess written under a figure
    #: is worse than no description: it is searchable, and it is wrong.
    CONFIDENT = 0.3

    def __init__(
        self,
        endpoint: str,
        key: str,
        *,
        timeout: float = 30.0,
        captions: bool = True,
        most: int = 40,
        transport: Optional[Callable[..., Any]] = None,
    ) -> None:
        if not endpoint:
            raise ValueError("endpoint is the Vision resource, like https://<name>.cognitiveservices.azure.com")
        self.endpoint = endpoint.rstrip("/")
        self._key = key
        self.timeout = float(timeout)
        #: Turned off by the service in a region that cannot caption, and
        #: then stays off, so the next figure asks for what it can have.
        self.captions = bool(captions)
        self.most = int(most)
        self._transport = transport
        self.asked = 0

    def __repr__(self) -> str:  # the key is not in it, and never should be
        return f"{type(self).__name__}({self.endpoint!r})"

    @classmethod
    def from_environment(cls, env: Optional[Mapping[str, str]] = None, **kwargs: Any) -> Optional["AzureImageAnalysis"]:
        """One built from ``AZURE_VISION_ENDPOINT`` and ``AZURE_VISION_KEY``, or None.

        None is a working deployment without it: a figure then keeps its
        caption and its picture and gains no description, which costs recall
        on questions about what a chart shows and nothing else. So a host
        can pass this straight to ``describe_figures`` without deciding
        first whether the resource was made.
        """
        found = os.environ if env is None else env
        endpoint = str(found.get("AZURE_VISION_ENDPOINT") or "").strip()
        key = str(found.get("AZURE_VISION_KEY") or "").strip()
        if not (endpoint and key):
            return None
        return cls(endpoint, key, **kwargs)

    # -- the call

    def _url(self) -> str:
        features = ["caption", "denseCaptions", "read"] if self.captions else ["read"]
        return f"{self.endpoint}/computervision/imageanalysis:analyze?api-version={self.API}&features={','.join(features)}"

    def _ask(self, image: bytes) -> Optional[Dict[str, Any]]:
        import urllib.error
        import urllib.request

        url = self._url()
        try:
            # A transport is the network, so it is refused the same way: a
            # test that hands one in is testing what really happens.
            if self._transport is not None:
                return self._transport(url, image)
            request = urllib.request.Request(
                url,
                data=image,
                method="POST",
                headers={"Ocp-Apim-Subscription-Key": self._key, "Content-Type": "application/octet-stream"},
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as reply:  # noqa: S310 - the endpoint is the caller's
                return json.loads(reply.read())
        except urllib.error.HTTPError as refused:
            body = refused.read().decode("utf-8", "replace")
            if "not supported in this region" in body and self.captions:
                # Asked for more than this region offers. Ask for the rest.
                self.captions = False
                return self._ask(image)
            return None
        except (urllib.error.URLError, ValueError, TimeoutError, OSError):
            # A figure with no description is still indexed by its caption.
            # One refusal is not worth failing a two hundred page report for.
            return None

    # -- what came back

    @staticmethod
    def _confident(block: Any, floor: float) -> str:
        text = str((block or {}).get("text") or "").strip()
        if not text:
            return ""
        return text if float((block or {}).get("confidence") or 0.0) >= floor else ""

    def _words(self, answer: Mapping[str, Any]) -> List[str]:
        """The printed words, in the order a person reads them."""
        found = []
        for block in (answer.get("readResult") or {}).get("blocks") or []:
            for line in block.get("lines") or []:
                text = str(line.get("text") or "").strip()
                box = line.get("boundingPolygon") or line.get("boundingBox")
                if text and box:
                    found.append((box, text))
                elif text:  # no box to order by: keep it where it came
                    found.append(((0.0, len(found), 1.0, len(found) + 1.0), text))
        return [mend_sentence_breaks(line) for line in reading_order(found)][: self.most]

    def _parts(self, answer: Mapping[str, Any]) -> List[str]:
        """What is in the picture, from the whole and then from its parts."""
        whole = self._confident(answer.get("captionResult"), self.CONFIDENT)
        said: List[str] = []
        seen = {whole.lower()} if whole else set()
        for dense in (answer.get("denseCaptionsResult") or {}).get("values") or []:
            text = self._confident(dense, self.CONFIDENT)
            # The first dense caption is the whole image again, and the rest
            # overlap each other; the same sentence twice is noise in a chunk.
            if text and text.lower() not in seen:
                seen.add(text.lower())
                said.append(text)
        return said

    def __call__(self, image: bytes, context: Mapping[str, Any]) -> Optional[Any]:
        answer = self._ask(image)
        if answer is None:
            return None
        self.asked += 1
        whole = self._confident(answer.get("captionResult"), self.CONFIDENT)
        parts = self._parts(answer)
        words = self._words(answer)

        lines: List[str] = []
        if whole:
            lines.append(whole[0].upper() + whole[1:] + ".")
        if parts:
            lines.append("In it: " + "; ".join(parts) + ".")
        if words:
            lines.append("The words in it read: " + "; ".join(words) + ".")
        if not lines:
            # Nothing seen and nothing written: a rule, a spacer, a gradient.
            # describe_figures takes it out of the text and drops the image.
            return {"decorative": True}
        found: Dict[str, Any] = {"description": " ".join(lines)}
        # A caption the service is sure of is a better one than "Figure 12",
        # and the library keeps the figure's own when this is left out.
        if whole and not str(context.get("caption") or "").strip():
            found["caption"] = whole
        return found


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


class AzureSpeech:
    """Speech to text by Azure AI Speech, fast transcription.

    One request with the audio in it and the phrases with their offsets
    back, which is what files up to a couple of hours want. ``endpoint`` is
    the resource's address and ``key`` its key, both from the host.
    """

    label = "azure-speech"
    API_VERSION = "2024-11-15"

    def __init__(
        self,
        endpoint: str,
        key: str,
        locales: Sequence[str] = ("en-US",),
        seconds_per_page: float = 60.0,
        timeout: float = 600.0,
        transport: Any = None,
        retries: int = 3,
        sleep: Any = None,
        speakers: int = 0,
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(f"endpoint is an https address, got {endpoint!r}")
        self.endpoint = endpoint.rstrip("/")
        self._key = key
        self.locales = list(locales)
        self.seconds_per_page = float(seconds_per_page)
        self.timeout = float(timeout)
        self._transport = transport
        self.retries = max(0, int(retries))
        self._sleep = sleep
        #: Up to how many voices to tell apart; 0 or 1 asks for no speaker labels.
        self.speakers = max(0, int(speakers))

    def suffixes(self) -> List[str]:
        return sorted(AUDIO_SUFFIXES)

    def __repr__(self) -> str:  # the key never prints
        return f"AzureSpeech({self.endpoint!r})"

    def __call__(self, data: bytes, name: str) -> LoadedDocument:
        try:
            return self._transcribe(data, name, self.speakers)
        except ExtractionError as exc:
            said = str(exc).lower()
            if self.speakers > 1 and exc.status == 400 and "diariz" in said:
                # A locale or a tier that cannot tell voices apart: asked again without.
                return self._transcribe(data, name, 0)
            raise

    def _transcribe(self, data: bytes, name: str, speakers: int) -> LoadedDocument:
        from . import _urllib_transport

        boundary = "vx" + uuid.uuid4().hex
        asked: Dict[str, Any] = {"locales": self.locales}
        if speakers > 1:
            # Who said what: each phrase comes back with the voice that said it.
            asked["diarization"] = {"enabled": True, "maxSpeakers": speakers}
        definition = json.dumps(asked)
        safe = name.replace('"', "")
        body = (
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"definition\"\r\n"
                f"Content-Type: application/json\r\n\r\n{definition}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"{safe}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + bytes(data)
            + f"\r\n--{boundary}--\r\n".encode()
        )
        route = f"/speechtotext/transcriptions:transcribe?api-version={self.API_VERSION}"
        headers = {
            "Ocp-Apim-Subscription-Key": self._key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        }
        transport = self._transport or _urllib_transport
        import time

        pause = self._sleep or time.sleep
        for attempt in range(self.retries + 1):
            try:
                status, answered, reply = transport("POST", self.endpoint + route, headers, body, self.timeout)
            except Exception as exc:
                raise ExtractionError(f"Azure Speech could not be reached: {exc}", route=route) from exc
            # A free tier's quota and a busy service answer 429 and 503 and
            # say when to come back; that is asked again, not reported.
            if int(status) not in (429, 503) or attempt == self.retries:
                break
            said = {str(k).lower(): v for k, v in dict(answered or {}).items()}.get("retry-after")
            try:
                wait = float(said) if said is not None else 2.0 * (2 ** attempt)
            except ValueError:
                wait = 2.0 * (2 ** attempt)
            pause(min(wait, 30.0))
        if not 200 <= int(status) < 300:
            detail = bytes(reply or b"")[:300].decode("utf-8", errors="replace")
            raise ExtractionError(f"Azure Speech answered {status} for {name}: {detail}", route=route, status=int(status))
        try:
            parsed = json.loads(bytes(reply).decode("utf-8"))
        except ValueError as exc:
            raise ExtractionError("Azure Speech sent something that is not JSON", route=route) from exc
        phrases = list(parsed.get("phrases") or [])
        voices = {phrase.get("speaker") for phrase in phrases if phrase.get("speaker") is not None}
        segments: List[Segment] = []
        heard: Dict[str, float] = {}
        for phrase in phrases:
            start = float(phrase.get("offsetMilliseconds") or 0) / 1000.0
            end = start + float(phrase.get("durationMilliseconds") or 0) / 1000.0
            words = str(phrase.get("text") or "")
            if len(voices) > 1 and phrase.get("speaker") is not None:
                words = f"Speaker {phrase['speaker']}: {words}"
            segments.append((start, end, words))
            if phrase.get("locale"):
                heard[str(phrase["locale"])] = heard.get(str(phrase["locale"]), 0.0) + (end - start)
        doc = segments_to_document(segments, self.seconds_per_page)
        if len(voices) > 1:
            doc.metadata["speakers"] = len(voices)
        if heard:
            # The language most of it was spoken in, as Speech heard it.
            doc.metadata["language"] = max(heard, key=lambda locale: heard[locale])
        if not segments:
            doc.metadata["speech"] = "none"
        return doc


# ============================================================================
# AMAZON: Textract and Transcribe
# ============================================================================
#
# INPUT   a page or a recording, through boto3
# OUTPUT  text detected; speech to text
#
# The same shapes as the Azure engines, so a collection does not know which it
# has.


def _s3_parts(uri: Optional[str]) -> Optional[Tuple[str, str]]:
    if not uri or not uri.startswith("s3://"):
        return None
    parsed = urlparse(uri)
    return parsed.netloc, parsed.path.lstrip("/")


class Textract:
    """Text detection by Amazon Textract.

    An image, or a PDF of one page, goes up in the request. A longer PDF has
    to be read where it lies in S3, which Textract does as a job: the worker
    passes the object's address as ``source``, and the extractor starts the
    job and waits for it. ``client`` is a boto3 Textract client the host built.
    """

    label = "aws-textract"

    def __init__(self, client: Any, poll_seconds: float = 2.0, timeout: float = 900.0, sleep: Any = None) -> None:
        self.client = client
        self.poll_seconds = float(poll_seconds)
        self.timeout = float(timeout)
        self._sleep = sleep or time.sleep

    def suffixes(self) -> List[str]:
        return sorted(IMAGE_SUFFIXES + (".pdf",))

    @staticmethod
    def _document(blocks: Iterable[Mapping[str, Any]]) -> LoadedDocument:
        # Textract returns lines with where they sit and in no reading order:
        # a page in two columns comes back a line from each in turn. With the
        # boxes, the page is put in the order a person reads it; a block with
        # no box keeps the place the service gave it.
        by_page: Dict[int, List[Tuple[Any, str]]] = {}
        placed: Dict[int, bool] = {}
        for block in blocks:
            if block.get("BlockType") == "LINE" and block.get("Text"):
                number = int(block.get("Page") or 1)
                box = ((block.get("Geometry") or {}).get("BoundingBox") or {}) if isinstance(block.get("Geometry"), Mapping) else {}
                has = all(k in box for k in ("Left", "Top", "Width", "Height"))
                rect = (box["Left"], box["Top"], box["Left"] + box["Width"], box["Top"] + box["Height"]) if has else None
                placed[number] = placed.get(number, True) and has
                by_page.setdefault(number, []).append((rect, str(block["Text"])))
        last = max(by_page, default=0)
        page_texts = []
        for n in range(1, last + 1):
            found = by_page.get(n, [])
            page_texts.append("\n".join(reading_order(found) if found and placed.get(n) else [t for _, t in found]))
        page_texts, dropped = drop_running_lines(page_texts)
        text, pages = join_pages(page_texts)
        metadata: Dict[str, Any] = {"ocr": True, "pages": len(pages), "ocr_pages": list(range(1, last + 1))}
        if dropped:
            metadata["running_lines"] = dropped
        return LoadedDocument(text=text, pages=pages, metadata=metadata)

    def __call__(self, data: bytes, name: str, source: Optional[str] = None) -> LoadedDocument:
        where = _s3_parts(source)
        if where is None or Path(name).suffix.lower() != ".pdf":
            reply = self.client.detect_document_text(Document={"Bytes": bytes(data)})
            return self._document(reply.get("Blocks") or [])
        bucket, key = where
        job = self.client.start_document_text_detection(
            DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}}
        )["JobId"]
        waited = 0.0
        blocks: List[Mapping[str, Any]] = []
        token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {"JobId": job}
            if token:
                kwargs["NextToken"] = token
            reply = self.client.get_document_text_detection(**kwargs)
            status = reply.get("JobStatus")
            if status == "IN_PROGRESS":
                if waited >= self.timeout:
                    raise ExtractionError(f"Textract job {job} did not finish in {self.timeout:.0f} seconds")
                self._sleep(self.poll_seconds)
                waited += self.poll_seconds
                continue
            if status not in ("SUCCEEDED", "PARTIAL_SUCCESS"):
                raise ExtractionError(f"Textract job {job} ended {status}: {reply.get('StatusMessage') or 'no reason given'}")
            blocks.extend(reply.get("Blocks") or [])
            token = reply.get("NextToken")
            if not token:
                return self._document(blocks)


class Transcribe:
    """Speech to text by Amazon Transcribe.

    Transcribe reads media from S3 and writes its result to S3, so this
    extractor needs the object's address as ``source``, a Transcribe client,
    an S3 client to read the result with, and a bucket to have it written
    to. The bytes it is handed are not used.
    """

    label = "aws-transcribe"

    def __init__(
        self,
        client: Any,
        s3: Any,
        output_bucket: str,
        output_prefix: str = "vectrixdb-transcripts/",
        language_code: Optional[str] = "en-US",
        seconds_per_page: float = 60.0,
        poll_seconds: float = 5.0,
        timeout: float = 3600.0,
        sleep: Any = None,
    ) -> None:
        self.client = client
        self.s3 = s3
        self.output_bucket = output_bucket
        self.output_prefix = output_prefix
        self.language_code = language_code
        self.seconds_per_page = float(seconds_per_page)
        self.poll_seconds = float(poll_seconds)
        self.timeout = float(timeout)
        self._sleep = sleep or time.sleep

    def suffixes(self) -> List[str]:
        return sorted(AUDIO_SUFFIXES + VIDEO_SUFFIXES)

    def __call__(self, data: bytes, name: str, source: Optional[str] = None) -> LoadedDocument:
        if _s3_parts(source) is None:
            raise ExtractionError(
                f"Transcribe reads media from S3 and {name} did not come with an s3:// address; "
                "use it behind the worker with an S3 fetcher, or put the file in S3 first"
            )
        job = "vx-" + uuid.uuid4().hex
        key = f"{self.output_prefix}{job}.json"
        request: Dict[str, Any] = {
            "TranscriptionJobName": job,
            "Media": {"MediaFileUri": source},
            "OutputBucketName": self.output_bucket,
            "OutputKey": key,
        }
        if self.language_code:
            request["LanguageCode"] = self.language_code
        else:
            request["IdentifyLanguage"] = True
        self.client.start_transcription_job(**request)
        waited = 0.0
        while True:
            state = self.client.get_transcription_job(TranscriptionJobName=job)["TranscriptionJob"]
            status = state.get("TranscriptionJobStatus")
            if status == "COMPLETED":
                break
            if status == "FAILED":
                raise ExtractionError(f"Transcribe job {job} failed: {state.get('FailureReason') or 'no reason given'}")
            if waited >= self.timeout:
                raise ExtractionError(f"Transcribe job {job} did not finish in {self.timeout:.0f} seconds")
            self._sleep(self.poll_seconds)
            waited += self.poll_seconds
        body = self.s3.get_object(Bucket=self.output_bucket, Key=key)["Body"].read()
        items = (json.loads(body).get("results") or {}).get("items") or []
        segments: List[Segment] = []
        words: List[str] = []
        started: Optional[float] = None
        ended = 0.0
        for item in items:
            content = ((item.get("alternatives") or [{}])[0]).get("content") or ""
            if item.get("type") == "punctuation":
                if words:
                    words[-1] += content
                if content in ".?!" and words and started is not None:
                    segments.append((started, ended, " ".join(words)))
                    words, started = [], None
                continue
            if started is None:
                started = float(item.get("start_time") or ended)
            ended = float(item.get("end_time") or ended)
            words.append(content)
        if words and started is not None:
            segments.append((started, ended, " ".join(words)))
        return segments_to_document(segments, self.seconds_per_page)
