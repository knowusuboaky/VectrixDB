"""A YouTube video's words: its captions when they can be trusted, its sound read when not.

    doc = load_youtube("https://youtu.be/q3Results01")                      # the uploader's captions, else Whisper
    doc = load_youtube(url, audio=AzureSpeech(endpoint, key))              # Speech when there are none
    doc = load_youtube(url, audio=speech, captions="automatic")            # YouTube's automatic captions will do
    doc = load_youtube(url, audio=speech, captions="never")                # always the sound, as before 2.2
    doc = load_youtube(url, audio=speech, language="fr-FR")                # in French, this call only
    doc = load_youtube(url, audio=speech, save_to="output")                # and keep the transcript
    print(transcript_markdown(doc))                                        # the transcript to read

Most videos already have timed captions, and reading them costs nothing: no
sound is downloaded, no ffmpeg runs and no speech engine is paid. So the
captions are read first, and the sound only when there are none worth having
or the caller asks for it. Which are worth having is ``captions``:

* ``"uploaded"``, the default: the captions the uploader made, else the
  sound. YouTube's automatic captions have no punctuation and more mistakes
  than Azure Speech makes, so they are not taken unasked.
* ``"automatic"``: the uploader's, else YouTube's automatic ones, else the
  sound.
* ``"translated"``: as ``"automatic"``, and when neither is in the language
  asked for, YouTube's machine translation into it, else the sound.
* ``"never"``: always the sound, which is what every call did before 2.2.

A machine translation is never read unless asked for. YouTube lists one in
every language it knows, and a transcript in words nobody said is not a
transcript.

The sound, when it is read, is fetched alone, m4a when YouTube has it,
because Speech reads m4a as it comes; anything else goes through ffmpeg
first, the way a video file does. Either way the transcript is cited by the
minute like any other recording, the video's title, channel, length and date
travel with it as metadata, and ``transcript_source`` says where its words
came from.

Two things are refused before anything is fetched, and both are on purpose:

* **An address that is not YouTube's.** yt-dlp fetches from over a thousand
  sites and from plain addresses too, so without this a caller could point
  it at anything the host can reach, including addresses only the host can
  reach.
* **An address that is not one video.** A playlist would download, and bill
  the transcription of, every video in it. A watch address that happens to
  sit inside a playlist is taken as the one video it names.

Two things are worth knowing before depending on it. YouTube refuses more
and more requests from cloud addresses with "Sign in to confirm you're not
a bot", captions and sound alike, so something that works on a laptop can
fail in a function app; the error says so when that is the reason. And
YouTube's terms restrict downloading its content outside its own features,
which is the caller's to weigh for the videos they choose.
"""

from __future__ import annotations

import contextlib
import html
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

from ..exceptions import DependencyError, ExtractionError

__all__ = ["CAPTIONS", "YOUTUBE_HOSTS", "is_youtube", "load_youtube", "video_id"]

logger = logging.getLogger("vectrixdb.extract")


# ============================================================================
# SETTINGS: the hosts, the id's shape, the caption choices, the downloader type
# ============================================================================
#
# The hosts a YouTube address may use, what an id looks like, which captions
# may give the words, and what a downloader is, so the tests hand one in.

#: The hosts a video is fetched from, and no others.
YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

#: Which captions may give the words, each choice taking more than the one
#: before it, and ``never`` none: the sound is always read.
CAPTIONS = ("uploaded", "automatic", "translated", "never")
#: The kinds of track each choice reads, most trusted first.
_KINDS = {
    "uploaded": ("uploaded",),
    "automatic": ("uploaded", "automatic"),
    "translated": ("uploaded", "automatic", "translated"),
    "never": (),
}
#: What each choice looked for, to say so when it found none.
_LOOKED_FOR = {
    "uploaded": "captions by its uploader",
    "automatic": "captions by its uploader or automatic ones",
    "translated": "captions by its uploader, automatic ones or a translation",
}
#: The caption formats read, best first: json3 is YouTube's own timed text,
#: vtt and srt what everybody else writes.
_FORMATS = ("json3", "vtt", "srt")
#: Language codes YouTube still uses and everybody else retired.
_OLD_CODES = {"iw": "he", "in": "id", "ji": "yi"}
#: The part of a track's name that is a language: ``en``, ``fr-CA``,
#: ``zh-Hans``, ``es-419``, and not the id of a second English track after it.
_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{4})?(?:-(?:[A-Za-z]{2}|\d{3}))?(?=-|$)")

#: A downloader takes the video's address and a folder, and answers the path
#: of the sound it wrote there and what it learned about the video.
Downloader = Callable[[str, str], Tuple[str, Dict[str, Any]]]
_Segment = Tuple[float, float, str]


# ============================================================================
# THE VIDEO
# ============================================================================
#
# INPUT   an address
# OUTPUT  the eleven characters that name one video, or None; whether this is
#         the address of one video, which is all load_youtube takes
#
# A playlist or a channel is not a video, and is refused as such.


def video_id(url: str) -> Optional[str]:
    """The eleven characters that name one video, or None when the address names none.

    ``youtu.be/<id>``, ``/watch?v=<id>``, ``/shorts/<id>``, ``/live/<id>``,
    ``/embed/<id>`` and ``/v/<id>``. A ``/playlist`` address names no one
    video and is None.
    """
    parsed = urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in YOUTUBE_HOSTS:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if host.endswith("youtu.be"):
        found = parts[0] if parts else ""
    elif parts[:1] == ["watch"]:
        found = (parse_qs(parsed.query).get("v") or [""])[0]
    elif len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
        found = parts[1]
    else:
        found = ""
    return found if _ID.match(found) else None


def is_youtube(url: str) -> bool:
    """Whether this is the address of one YouTube video, which is all load_youtube takes."""
    return video_id(url) is not None


# ============================================================================
# THE CAPTIONS: which track
# ============================================================================
#
# INPUT   what yt-dlp says about a video: its ``subtitles``, the uploader's,
#         and its ``automatic_captions``, YouTube's, each a language mapped
#         to formats with addresses; the captions choice; the language asked
# OUTPUT  every track sorted into uploaded, automatic and translated; the
#         one track to read, or None
#
# YouTube lists a machine translation of its automatic captions in every
# language it knows, each one an address with ``tlang`` in it, so a track is
# a translation by that mark first, whatever it is filed under.


class _Track(NamedTuple):
    """One caption track, as load_youtube reads it."""

    #: ``uploaded``, ``automatic`` or ``translated``.
    kind: str
    #: What it is written in: ``en``, ``fr-CA``.
    language: str
    url: str
    #: ``json3``, ``vtt`` or ``srt``.
    ext: str
    #: Made by YouTube's speech recognition, translated afterwards or not.
    automatic: bool
    #: For a translation, the language it was translated from.
    source: Optional[str]


def _primary(code: str) -> str:
    """The language of a language code, ``en`` of ``en-US``, with YouTube's retired codes brought up to date."""
    first = str(code).replace("_", "-").split("-")[0].lower()
    return _OLD_CODES.get(first, first)


def _language_of(key: str) -> str:
    """A track's language from the name yt-dlp files it under: ``en`` of ``en-orig`` or ``en-nP7-2PuUl7o``."""
    key = str(key)
    if key.endswith("-orig"):
        key = key[: -len("-orig")]
    found = _LANGUAGE_TAG.match(key)
    return found.group(0) if found else key


def _query(url: str) -> Dict[str, List[str]]:
    return parse_qs(urlparse(url).query)


def _best_format(formats: Any) -> Optional[Tuple[str, str]]:
    """The address and format of the best copy of a track this reads, json3, then vtt, then srt, or None."""
    by_format: Dict[str, str] = {}
    for offered in formats or []:
        if isinstance(offered, Mapping) and offered.get("url"):
            by_format.setdefault(str(offered.get("ext") or "").lower(), str(offered["url"]))
    return next(((by_format[ext], ext) for ext in _FORMATS if ext in by_format), None)


def _original(automatic: Mapping[str, Any]) -> Optional[str]:
    """The language YouTube's automatic captions were made in, before any translation of them.

    yt-dlp files them twice, once with ``-orig`` after the language to tell
    them from the translations; an older yt-dlp files them once, as the one
    automatic track that is not a translation.
    """
    for key in automatic:
        if str(key).endswith("-orig"):
            return _language_of(key)
    for key, formats in automatic.items():
        found = _best_format(formats)
        if found and "tlang" not in _query(found[0]):
            return _language_of(key)
    return None


def _spoken_in(info: Mapping[str, Any]) -> Optional[str]:
    """The video's own language, as far as YouTube says: its own field, else what its automatic captions heard."""
    return str(info.get("language") or "") or _original(info.get("automatic_captions") or {})


def _tracks(info: Mapping[str, Any]) -> List[_Track]:
    """Every caption track the video lists that this can read, in YouTube's order, each once."""
    automatic = dict(info.get("automatic_captions") or {})
    made_in = _original(automatic)
    found: List[_Track] = []
    seen: Set[Tuple[str, str]] = set()
    for listed, entries in (("uploaded", info.get("subtitles") or {}), ("automatic", automatic)):
        for key, formats in dict(entries).items():
            best = _best_format(formats)
            if best is None:
                continue  # a live chat, or a format nobody here reads
            url, ext = best
            query = _query(url)
            language = _language_of(key)
            into = (query.get("tlang") or [""])[0]
            if into or (
                listed == "automatic" and made_in and _primary(language) != _primary(made_in)
            ):
                # A machine translation. Translated from YouTube's own
                # captions when the address says kind=asr, the uploader's when not.
                track = _Track(
                    "translated",
                    _language_of(into) if into else language,
                    url,
                    ext,
                    "asr" in query.get("kind", []),
                    (query.get("lang") or [made_in or ""])[0] or None,
                )
            else:
                track = _Track(listed, language, url, ext, listed == "automatic", None)
            if (track.kind, track.language.lower()) not in seen:
                seen.add((track.kind, track.language.lower()))
                found.append(track)
    return found


def _matching(pool: List[_Track], wanted: str) -> Optional[_Track]:
    """The track in ``wanted``: the same code first, ``en-GB`` for ``en-GB``, then ``en``, then any English."""
    asked = wanted.strip().replace("_", "-").lower()
    first = _primary(asked)
    for same in (
        lambda track: track.language.lower() == asked,
        lambda track: track.language.lower() == first,
        lambda track: _primary(track.language) == first,
    ):
        for track in pool:
            if same(track):
                return track
    return None


def _pick_track(
    info: Mapping[str, Any], captions: str, language: Optional[str]
) -> Optional[_Track]:
    """The one track to read, or None when no caption the choice allows will do.

    Kinds in the order they are trusted, the uploader's first. Within each,
    the language asked for, several in turn, ``en-US`` taking an ``en``
    track; a language asked for and not there is not traded for another.
    Left out, the video's own language, then English, then the uploader's
    first track whatever its language.
    """
    if language:
        wanted = [part.strip() for part in str(language).split(",") if part.strip()]
    else:
        wanted = list(dict.fromkeys(code for code in (_spoken_in(info), "en") if code))
    tracks = _tracks(info)
    for kind in _KINDS[captions]:
        pool = [track for track in tracks if track.kind == kind]
        for code in wanted:
            found = _matching(pool, code)
            if found is not None:
                return found
        if kind == "uploaded" and not language and pool:
            return pool[0]
    return None


def _named(track: _Track) -> str:
    """A track as a person would say it: "the uploader's en captions"."""
    if track.kind == "uploaded":
        return f"the uploader's {track.language} captions"
    if track.kind == "automatic":
        return f"YouTube's automatic {track.language} captions"
    return f"YouTube's {track.language} translation of its {track.source or 'other'} captions"


# ============================================================================
# THE CAPTIONS: read
# ============================================================================
#
# INPUT   the bytes of a caption track, json3, vtt or srt
# OUTPUT  timed segments of what was said, each line once, styling taken out,
#         each ending where the next begins at the latest
#
# Automatic captions roll: each new line is shown under the one before, so a
# line comes twice, and words come in one at a time with a timestamp and a
# style tag each. A line the cue before it showed is not said again.

#: A cue's times, ``00:01:02.500 --> 00:01:04.000``, the hours optional, a comma in srt.
_CUE_TIMES = re.compile(
    r"((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})"
)
#: Anything in angle brackets: a word's own time, a style, a voice.
_CUE_TAG = re.compile(r"<[^>]*>")

_Cue = Tuple[float, float, List[str]]


def _seconds(stamp: str) -> float:
    total = 0.0
    for part in stamp.replace(",", ".").split(":"):
        total = total * 60 + float(part)
    return total


def _json3_cues(text: str) -> List[_Cue]:
    """YouTube's json3: each event with words in it, its start, its end, and its lines."""
    parsed = json.loads(text)
    if not isinstance(parsed, Mapping):
        raise ValueError("a json3 caption track is an object with events in it")
    cues: List[_Cue] = []
    for event in parsed.get("events") or []:
        pieces = event.get("segs") if isinstance(event, Mapping) else None
        if not pieces:
            continue  # a window being placed, not words
        words = "".join(str(piece.get("utf8") or "") for piece in pieces)
        start = float(event.get("tStartMs") or 0)
        end = start + float(event.get("dDurationMs") or 0)
        cues.append((start / 1000.0, end / 1000.0, words.split("\n")))
    return cues


def _vtt_cues(text: str) -> List[_Cue]:
    """WebVTT or SubRip: each cue's start, end and lines, with the tags and entities taken out.

    A cue's text runs to the first empty line. A line holding a space is
    not empty: automatic captions open every cue with one.
    """
    cues: List[_Cue] = []
    lines: Optional[List[str]] = None
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        times = _CUE_TIMES.search(line)
        if times:
            lines = []
            cues.append((_seconds(times.group(1)), _seconds(times.group(2)), lines))
        elif lines is not None:
            if line == "":
                lines = None
            else:
                lines.append(html.unescape(_CUE_TAG.sub("", line)))
    return cues


def _said_once(cues: List[_Cue]) -> List[_Segment]:
    """Each cue's new lines as one segment, a line already on screen in the cue before left out."""
    said: List[List[Any]] = []
    before: Set[str] = set()
    for start, end, lines in cues:
        shown = [joined for joined in (" ".join(line.split()) for line in lines) if joined]
        if not shown:
            continue
        new = [line for line in shown if line not in before]
        before = set(shown)
        if new:
            said.append([start, end, " ".join(new)])
    for here, after in zip(said, said[1:]):
        if after[0] > here[0]:
            # Shown until the next line came, which on a rolling caption is
            # well after it was said.
            here[1] = min(here[1], after[0])
    return [(round(start, 3), round(max(start, end), 3), text) for start, end, text in said]


def _caption_segments(data: Any, ext: str) -> List[_Segment]:
    """A caption track's timed segments, from its bytes, or its text, and its format."""
    text = data if isinstance(data, str) else bytes(data).decode("utf-8-sig", errors="replace")
    return _said_once(_json3_cues(text) if ext == "json3" else _vtt_cues(text))


def _read_captions(
    fetcher: Any,
    info: Mapping[str, Any],
    captions: str,
    language: Optional[str],
    seconds_per_page: float,
) -> Tuple[Any, str]:
    """The words from the video's captions, or None and why there were none worth having."""
    from .engines import segments_to_document

    track = _pick_track(info, captions, language)
    if track is None:
        where = f" in {language}" if language else ""
        return None, f"the video has no {_LOOKED_FOR[captions]}{where}"
    try:
        segments = _caption_segments(fetcher.read(track.url), track.ext)
    except Exception as exc:  # noqa: BLE001 - captions that cannot be read are captions not there
        said = " ".join(str(exc).split())[:160] or type(exc).__name__
        return None, f"{_named(track)} could not be read: {said}"
    if not segments:
        return None, f"{_named(track)} have no words in them"
    doc = segments_to_document(segments, seconds_per_page)
    doc.metadata.update(
        language=track.language,
        caption_language=track.language,
        caption_automatic=track.automatic,
    )
    if track.source:
        doc.metadata["caption_translated_from"] = track.source
    return doc, ""


# ============================================================================
# YT-DLP: one look at the video, its captions, its sound
# ============================================================================
#
# INPUT   the address of one video, and a folder
# OUTPUT  what YouTube says about it, nothing downloaded; one caption track's
#         bytes; the sound, written into the folder; a refusal that says
#         when YouTube took the caller for a bot
#
# One YoutubeDL for all three, so a caption track is fetched with the same
# headers, cookies and proxy as the look, and a video whose captions will
# not do is downloaded from that look and not asked about a second time.


def _refused(url: str, exc: Exception, said: str) -> ExtractionError:
    message = str(exc)
    if "confirm you" in message and "bot" in message:
        return ExtractionError(
            f"YouTube refused {url} as coming from a bot. It does this to cloud addresses, so it "
            "can work on a laptop and fail in a function app; it is not a fault in the address",
            route="youtube",
        )
    return ExtractionError(f"{said}: {message}", route="youtube")


class _YtDlp:
    """yt-dlp, opened once for one video: what it is, one caption track, and its sound.

    The sound goes into the folder this was opened with, the folder
    load_youtube passes it, so it is deleted with that folder.
    """

    def __init__(self, folder: str) -> None:
        try:
            import yt_dlp
        except ImportError as exc:
            raise DependencyError("yt-dlp", "youtube") from exc
        options = {
            # m4a first: Speech reads it as it comes, with no ffmpeg in between.
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        self._open = contextlib.ExitStack()
        self._ydl = self._open.enter_context(yt_dlp.YoutubeDL(options))
        self._looked: Dict[str, Any] = {}

    def look(self, url: str) -> Dict[str, Any]:
        """What YouTube says about the video, its caption tracks among it; nothing is downloaded."""
        try:
            # Not processed: no format is chosen and nothing else is fetched.
            info = self._ydl.extract_info(url, download=False, process=False)
        except Exception as exc:
            raise _refused(url, exc, f"YouTube would not say what {url} is") from exc
        self._looked[url] = info
        return dict(info or {})

    def read(self, url: str) -> bytes:
        """One caption track's bytes, by the address the look gave it."""
        with self._ydl.urlopen(url) as reply:
            return bytes(reply.read())

    def __call__(self, url: str, folder: str) -> Tuple[str, Dict[str, Any]]:
        """The video's sound, written into the folder this was opened with."""
        looked = self._looked.pop(url, None)
        try:
            if looked is not None:
                # The look already made, carried on to a download: one more
                # question to YouTube is one more chance to be refused.
                info = self._ydl.process_ie_result(looked, download=True)
            else:
                info = self._ydl.extract_info(url, download=True)
            path = self._ydl.prepare_filename(info)
        except Exception as exc:
            raise _refused(url, exc, f"YouTube would not give the sound of {url}") from exc
        return path, dict(info or {})

    def close(self) -> None:
        self._open.close()


def _reads_captions(fetcher: Any) -> bool:
    """Whether a fetcher can look at a video and read its captions, as yt-dlp can."""
    return callable(getattr(fetcher, "look", None)) and callable(getattr(fetcher, "read", None))


# ============================================================================
# THE SOUND, READ
# ============================================================================
#
# INPUT   an audio engine with a language; the sound yt-dlp wrote
# OUTPUT  a copy of the engine listening for the language, the one passed in
#         untouched; the words of the video as a document cited by the minute
#
# Read like any audio, when the captions did not give the words.


def _listening_in(engine: Any, language: str) -> Any:
    """A copy of ``engine`` that listens for ``language``, the one passed in untouched.

    A copy, because an engine is shared: a function app makes one and every
    request uses it, so setting the language on it would change the language
    of the request running beside this one. Azure Speech takes a locale,
    ``fr-FR``; Whisper takes a language, ``fr``, which is the part of a
    locale before its dash, so either spelling works for either engine.
    """
    import copy

    # Several, "en-US,fr-CA", are the languages a recording may be in: Azure
    # Speech is given them all and says which it heard; Whisper, which
    # listens for one, takes the first.
    wanted = [part.strip() for part in str(language).split(",") if part.strip()]
    if not wanted:
        return engine
    if hasattr(engine, "locales"):
        made = copy.copy(engine)
        made.locales = wanted
        return made
    if hasattr(engine, "language"):
        made = copy.copy(engine)
        made.language = wanted[0].split("-")[0].lower()
        return made
    raise ValueError(
        f"{type(engine).__name__} has no language to set, so language={language!r} cannot be honoured. "
        "Give it an engine that has one, AzureSpeech or Whisper, or leave language out"
    )


def _heard(listener: Any, path: str) -> Any:
    """The words of the sound at ``path``, read by ``listener``."""
    from ..ingest import LoadedDocument
    from .engines import AUDIO_SUFFIXES, Video

    name = os.path.basename(path)
    data = Path(path).read_bytes()
    if Path(name).suffix.lower() in AUDIO_SUFFIXES:
        doc = listener(data, name)
    else:
        # Opus in webm, usually: not a file Speech takes, so its sound is
        # taken out the way a video's is.
        doc = Video(audio=listener)(data, name)
    if not isinstance(doc, LoadedDocument):
        from . import coerce

        doc = coerce(doc, name)
    return doc


# ============================================================================
# THE VIDEO'S WORDS
# ============================================================================
#
# INPUT   an address, an audio engine, the captions choice, a language
# OUTPUT  the words of the video as a document cited by the minute, from its
#         captions or its sound, with where they came from; the transcript
#         saved, and the sound when it was downloaded and asked for
#
# Captions first, because they cost nothing; the sound when they will not do.


def load_youtube(
    url: str,
    audio: Any = None,
    *,
    captions: str = "uploaded",
    language: Optional[str] = None,
    save_to: Optional[str] = None,
    keep_audio: bool = False,
    download: Optional[Downloader] = None,
) -> Any:
    """The words of one YouTube video, as a document cited by the minute.

    ``captions`` says which of the video's captions may give the words
    before its sound is downloaded and read: ``"uploaded"``, the default,
    the uploader's own; ``"automatic"``, YouTube's automatic ones too;
    ``"translated"``, YouTube's machine translation into the language asked
    for too; ``"never"``, none, so the sound is always read. Captions cost
    nothing and need no speech engine, no ffmpeg and no download; the sound
    costs a download and a transcription. Automatic captions have no
    punctuation and more mistakes than Azure Speech, which is why they wait
    to be asked for.

    ``audio`` reads the sound when the captions do not give the words:
    :class:`~vectrixdb.extract.engines.AzureSpeech` for this library's Azure
    deployment, :class:`Whisper` on the machine by default.

    ``language`` is the language of the words, on this call only: ``en-US``,
    ``fr-FR``. It picks the caption track, ``en-US`` taking an ``en``
    track, and it is what speech listens for. Several, ``en-US,fr-CA``,
    are tracks tried in turn and every language speech listens for. A
    language with no track is heard from the sound rather than read in
    another. Left out, the track is the video's own language, then English,
    then the uploader's first, and speech listens for the engine's own
    languages. The engine passed in is never changed, because it is usually
    shared by every request at once.

    Every phrase keeps when it was said, in ``doc.segments``, and
    :func:`~vectrixdb.extract.engines.transcript_markdown` writes the
    transcript the way a person reads one: a heading, the URL, channel,
    duration and language, the full text, and every phrase timed
    ``**[0:04 → 0:09]**``. ``transcript_source`` in its metadata says
    ``captions`` or ``speech``. From captions, ``caption_language`` names
    the track, ``caption_automatic`` says whether YouTube made it, and
    ``caption_translated_from`` is there for a translation. From speech
    when captions were allowed, ``captions_unused`` says why they were not
    read.

    ``save_to`` writes that transcript there as ``<video id>_transcript.md``.
    Named by the video's id and not its title, because two titles can share
    a first word and a folder shared by two requests must never hand one of
    them the other's file. ``keep_audio`` keeps the downloaded sound beside
    it. There is a sound only when it was read: when the captions gave the
    words nothing was downloaded, so the transcript is saved alone and the
    log says so; ``captions="never"`` always downloads it. Left off, the
    sound is deleted with the temporary folder it was fetched into,
    whatever happens.

    ``download`` replaces yt-dlp, for a test or for a fetcher of your own.
    Called as ``download(url, folder)`` it answers ``(path, info)``: the
    sound it wrote there and what it learned about the video. One that also
    has ``look(url)``, answering what the video is with its ``subtitles``
    and ``automatic_captions`` as yt-dlp lists them, and ``read(url)``,
    answering the bytes of one track, has the captions read first. A plain
    function is asked for the sound alone, as it always was: a fetcher of
    your own is never gone around to reach YouTube another way.
    """
    from .engines import Whisper

    found = video_id(url)
    if found is None:
        raise ValueError(
            f"{url!r} is not the address of one YouTube video. It has to be youtube.com or youtu.be, "
            "and one video: a playlist would fetch and transcribe every video in it"
        )
    if captions not in CAPTIONS:
        raise ValueError(f"captions is one of {', '.join(CAPTIONS)}, not {captions!r}")
    if keep_audio and not save_to:
        raise ValueError("keep_audio keeps the sound beside the transcript, so it needs save_to")
    listener = audio if audio is not None else Whisper()
    if language:
        listener = _listening_in(listener, language.strip())
    # The address as YouTube itself would write it: one video, no playlist,
    # no tracking, whatever the caller pasted.
    canonical = f"https://www.youtube.com/watch?v={found}"

    with tempfile.TemporaryDirectory(prefix="vectrixdb-youtube-") as folder:
        fetcher: Any = download if download is not None else _YtDlp(folder)
        try:
            doc: Any = None
            info: Dict[str, Any] = {}
            unused, path = "", ""
            if captions != "never" and _reads_captions(fetcher):
                info = dict(fetcher.look(canonical) or {})
                pages = float(getattr(listener, "seconds_per_page", 60.0) or 60.0)
                doc, unused = _read_captions(fetcher, info, captions, language, pages)
            elif captions != "never":
                unused = "the download handed in fetches the sound alone"
            if doc is None:
                if unused:
                    logger.info("the sound of %s is read: %s", canonical, unused)
                path, fetched = fetcher(canonical, folder)
                info = {**info, **dict(fetched or {})}
                doc = _heard(listener, path)
            _describe(doc, info, found, canonical, path, unused, language, listener)
            if save_to:
                _save(doc, found, save_to, path if keep_audio else "", keep_audio)
        finally:
            if download is None:
                fetcher.close()
    return doc


def _describe(
    doc: Any,
    info: Mapping[str, Any],
    found: str,
    canonical: str,
    path: str,
    unused: str,
    language: Optional[str],
    listener: Any,
) -> None:
    """The video's title, channel, length and date, and where its words came from, in the document's metadata."""
    title = str(info.get("title") or "").strip()
    about: Dict[str, Any] = {
        "source": str(info.get("webpage_url") or canonical),
        "filename": title or found,
        "kind": "youtube",
        "youtube_id": found,
        "transcript_source": "speech" if path else "captions",
    }
    if path:
        # The sound's own name, so whoever deletes it can say what went.
        about["audio_file"] = os.path.basename(path)
        heard = (
            language
            or next(iter(getattr(listener, "locales", None) or []), None)
            or getattr(listener, "language", None)
        )
    else:
        heard = doc.metadata.get("language")
    if unused and path:
        about["captions_unused"] = unused
    for key, value in (
        ("language", heard),
        ("title", title),
        ("channel", info.get("channel") or info.get("uploader")),
        ("duration", info.get("duration")),
        ("published", info.get("upload_date")),
    ):
        if value:
            about[key] = value
    doc.metadata = {**doc.metadata, **about}


def _save(doc: Any, found: str, save_to: str, sound: str, keep_audio: bool) -> None:
    """The transcript written as ``<video id>_transcript.md``, and the sound beside it when there is one to keep."""
    from .engines import transcript_markdown

    target = Path(save_to)
    target.mkdir(parents=True, exist_ok=True)
    written = target / f"{found}_transcript.md"
    written.write_text(transcript_markdown(doc), encoding="utf-8")
    doc.metadata["saved_to"] = str(written)
    if sound:
        kept = target / os.path.basename(sound)
        shutil.copy2(sound, kept)
        doc.metadata["audio_saved_to"] = str(kept)
    elif keep_audio:
        logger.info(
            "keep_audio kept no sound for %s: its captions gave the words, so none was downloaded; "
            "captions='never' downloads and reads it",
            found,
        )
