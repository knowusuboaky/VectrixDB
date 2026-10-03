"""A YouTube video's words: its sound fetched with yt-dlp, then read like any audio.

    doc = load_youtube("https://youtu.be/q3Results01", audio=AzureSpeech(endpoint, key))
    doc = load_youtube(url, audio=speech, language="fr-FR")                   # in French, this call only
    doc = load_youtube(url, audio=speech, save_to="output")                   # and keep the transcript
    doc = load_youtube(url, audio=speech, save_to="output", keep_audio=True)  # and the sound too
    print(transcript_markdown(doc))                                           # the transcript to read

Only the sound is fetched, and m4a when YouTube has it, because Speech reads
m4a as it comes; anything else goes through ffmpeg first, the way a video
file does. The transcript is cited by the minute like any other recording,
and the video's title, channel, length and date travel with it as metadata.

Two things are refused before anything is fetched, and both are on purpose:

* **An address that is not YouTube's.** yt-dlp fetches from over a thousand
  sites and from plain addresses too, so without this a caller could point
  it at anything the host can reach, including addresses only the host can
  reach.
* **An address that is not one video.** A playlist would download, and bill
  the transcription of, every video in it. A watch address that happens to
  sit inside a playlist is taken as the one video it names.

Two things are worth knowing before depending on it. YouTube refuses more
and more downloads from cloud addresses with "Sign in to confirm you're not
a bot", so something that works on a laptop can fail in a function app; the
error says so when that is the reason. And YouTube's terms restrict
downloading its content outside its own features, which is the caller's to
weigh for the videos they choose.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..exceptions import DependencyError, ExtractionError

__all__ = ["YOUTUBE_HOSTS", "is_youtube", "load_youtube", "video_id"]


# ============================================================================
# SETTINGS: the hosts, the id's shape, and the downloader type
# ============================================================================
#
# The hosts a YouTube address may use, what an id looks like, and what a
# downloader is, so the tests hand one in.

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

#: A downloader takes the video's address and a folder, and answers the path
#: of the sound it wrote there and what it learned about the video.
Downloader = Callable[[str, str], Tuple[str, Dict[str, Any]]]


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
# THE SOUND, FETCHED AND READ
# ============================================================================
#
# INPUT   the address, a folder, and an audio engine with a language
# OUTPUT  the video's sound written by yt-dlp; a copy of the engine listening
#         for the language, the one passed in untouched; the words of the
#         video as a document cited by the minute
#
# Then read like any audio.


def _download_with_ytdlp(url: str, folder: str) -> Tuple[str, Dict[str, Any]]:
    """The video's sound, written into ``folder`` by yt-dlp."""
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
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
    except Exception as exc:
        message = str(exc)
        if "confirm you" in message and "bot" in message:
            raise ExtractionError(
                f"YouTube refused {url} as coming from a bot. It does this to cloud addresses, so it "
                "can work on a laptop and fail in a function app; it is not a fault in the address",
                route="youtube",
            ) from exc
        raise ExtractionError(
            f"YouTube would not give the sound of {url}: {message}", route="youtube"
        ) from exc
    return path, dict(info or {})


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


def load_youtube(
    url: str,
    audio: Any = None,
    *,
    language: Optional[str] = None,
    save_to: Optional[str] = None,
    keep_audio: bool = False,
    download: Optional[Downloader] = None,
) -> Any:
    """The words of one YouTube video, as a document cited by the minute.

    ``audio`` reads the sound: :class:`~vectrixdb.extract.engines.AzureSpeech`
    for this library's Azure deployment, :class:`Whisper` on the machine by
    default.

    ``language`` is the language to listen for, on this call only: ``en-US``,
    ``fr-FR``. Left out, the engine's own. The engine passed in is never
    changed, because it is usually shared by every request at once.

    Every phrase keeps when it was said, in ``doc.segments``, and
    :func:`~vectrixdb.extract.engines.transcript_markdown` writes the
    transcript the way a person reads one: a heading, the URL, channel,
    duration and language, the full text, and every phrase timed
    ``**[0:04 → 0:09]**``.

    ``save_to`` writes that transcript there as ``<video id>_transcript.md``.
    Named by the video's id and not its title, because two titles can share
    a first word and a folder shared by two requests must never hand one of
    them the other's file. ``keep_audio`` keeps the downloaded sound beside
    it; left off, it is deleted with the temporary folder it was fetched
    into, whatever happens.

    ``download`` replaces yt-dlp, for a test or for a fetcher of your own:
    it takes the address and a folder and answers ``(path, info)``.
    """
    from ..ingest import LoadedDocument
    from .engines import AUDIO_SUFFIXES, Video, Whisper

    found = video_id(url)
    if found is None:
        raise ValueError(
            f"{url!r} is not the address of one YouTube video. It has to be youtube.com or youtu.be, "
            "and one video: a playlist would fetch and transcribe every video in it"
        )
    if keep_audio and not save_to:
        raise ValueError("keep_audio keeps the sound beside the transcript, so it needs save_to")
    listener = audio if audio is not None else Whisper()
    if language:
        listener = _listening_in(listener, language.strip())
    # The address as YouTube itself would write it: one video, no playlist,
    # no tracking, whatever the caller pasted.
    canonical = f"https://www.youtube.com/watch?v={found}"

    with tempfile.TemporaryDirectory(prefix="vectrixdb-youtube-") as folder:
        path, info = (download or _download_with_ytdlp)(canonical, folder)
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

        title = str(info.get("title") or "").strip()
        about: Dict[str, Any] = {
            "source": str(info.get("webpage_url") or canonical),
            "filename": title or found,
            "kind": "youtube",
            "youtube_id": found,
            # The sound's own name, so whoever deletes it can say what went.
            "audio_file": name,
        }
        heard = (
            language
            or next(iter(getattr(listener, "locales", None) or []), None)
            or getattr(listener, "language", None)
        )
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

        if save_to:
            from .engines import transcript_markdown

            target = Path(save_to)
            target.mkdir(parents=True, exist_ok=True)
            written = target / f"{found}_transcript.md"
            written.write_text(transcript_markdown(doc), encoding="utf-8")
            doc.metadata["saved_to"] = str(written)
            if keep_audio:
                kept = target / name
                shutil.copy2(path, kept)
                doc.metadata["audio_saved_to"] = str(kept)
    return doc
