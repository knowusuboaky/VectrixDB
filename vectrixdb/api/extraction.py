"""An extraction service: files, addresses and videos in, text out, nothing indexed.

The routes a document-reading server is usually called at, served by the
library, so a host is two lines::

    from vectrixdb.api import create_extraction_app

    app = create_extraction_app()                     # FastAPI: uvicorn main:app

    # or, as an Azure function app
    app = func.AsgiFunctionApp(app=create_extraction_app(), http_auth_level=func.AuthLevel.ANONYMOUS)

What it serves, at the paths such a server already uses, so a caller of one
needs no change to call this one::

    GET  /health
    POST /extract/pdf | docx | doc | pptx | xlsx | csv | txt | md | html   the file as the body
    POST /transcribe/image | audio | video                           the file as the body
    POST /transcribe/webpage | image_url | audio_url | video_url     {"url": ...}
    POST /transcribe/youtube                                         {"url": ..., "language": "en-US"}
    POST /transcribe/youtube_save                                    {"url": ...}   202 and a job
    POST /transcribe/auto                                            {"url": ...}
    POST /translate/text                                             {"text": ..., "to": "fr"}
    POST /translate/detect                                           {"text": ...}
    GET  /translate/languages
    GET  /jobs/{job}

A caller routes by a file's suffix, and :func:`extraction_routes` is that
table: every suffix this service reads and the route that reads it, so a
server reading through it sends each file to the one route that reads it.

Those are the paths with no prefix. ``VECTRIXDB_EXTRACT_PREFIX`` puts every
one of them under a path the deployment chooses, so ``/api`` serves
``/api/extract/pdf`` and ``/api/health``, with or without a gateway in front.
A gateway that publishes each endpoint under a path of its own is told once,
in ``VECTRIXDB_EXTRACT_GATEWAY_PATHS``, and each listed route then answers
with its path in front or without it.

Everything answers Markdown, or JSON when ``Accept`` asks for it first, which
is the shape :class:`~vectrixdb.extract.HttpExtractor` reads: text, pages,
headings, metadata and a recording's timed segments. So a library pointed at
this service with ``VECTRIXDB_EXTRACTOR_URL`` keeps its page citations.

Four things it does that a hand-written server usually does not, and each
was a real hole in one:

* **It will not be built open.** With no API key and no sign-in it refuses
  to start, because it spends money on every call and would spend it for
  anybody who found it. ``VECTRIXDB_ALLOW_OPEN=1`` says something in front
  of it does the asking.
* **It fetches only from hosts it is told**, ``VECTRIXDB_EXTRACT_URL_HOSTS``,
  and follows a redirect only to another of them. A route that fetches
  whatever a caller names can be pointed at addresses only the server can
  reach, and one that checks only the first address can be redirected
  there.
* **A long job is a job.** Azure ends every HTTP request at 230 seconds,
  whatever the function's own timeout says, so ``youtube_save`` answers 202
  and a job at once and the work finishes on a queue. The finished job
  carries the same fields a synchronous answer would.
* **A caller names a folder, never a path.** ``output_dir`` is one folder
  name under the server's own output, and anything with a separator in it
  is refused.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..exceptions import ConfigurationError, DependencyError, ExtractionError, TranslationError
from .replies import message_of, refusal
from .gateway import read_gateway_paths, route_prefix

__all__ = [
    "AzureJobs",
    "ExtractionService",
    "MemoryJobs",
    "allowed_host",
    "create_extraction_app",
    "extraction_routes",
    "run_extraction_job",
]

logger = logging.getLogger("vectrixdb.extraction")

#: The documents /extract reads, and the reader each is given by name, so a
#: reader registered for the suffix elsewhere in the process is not used.
DOCUMENT_KINDS = {
    "pdf": "pdf",
    "docx": "docx",
    "doc": "doc",
    "pptx": "pptx",
    "xlsx": "xlsx",
    "csv": "csv",
    "txt": "text",
    "md": "markdown",
    "html": "html",
}
#: The other suffixes a document kind goes by, read by the same route.
_ALSO_NAMED = {"md": (".markdown",), "html": (".htm",), "xlsx": (".xlsm",)}
_MEDIA_NAMES = {"image": "image.png", "audio": "audio.wav", "video": "video.mp4"}
_FOLDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")
_TRUE = ("1", "true", "yes", "on")


# ============================================================ what it reads ===


class UrlBody(BaseModel):
    url: str
    language: str = "en-US"


class WebpageBody(BaseModel):
    url: str
    include_images: bool = True
    include_audio: bool = True
    include_video: bool = True


class YouTubeSaveBody(BaseModel):
    url: str
    language: str = "en-US"
    output_dir: str = "output"
    auto_delete: bool = True


class TranslateBody(BaseModel):
    text: Union[str, List[str]]
    to: str
    from_lang: Optional[str] = None


class DetectBody(BaseModel):
    text: Union[str, List[str]]


class _Refused(Exception):
    """A request refused on purpose, with the status that says why."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# ====================================================== fetching addresses ===


def allowed_host(url: str, hosts: Sequence[str]) -> bool:
    """Whether ``url`` is on one of ``hosts``: exact names, or ``*.example.com`` for its subdomains."""
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in ("http", "https") or not host:
        return False
    for allowed in hosts:
        wanted = str(allowed).strip().lower().rstrip(".")
        if not wanted:
            continue
        if wanted.startswith("*."):
            if host.endswith(wanted[1:]) and host != wanted[2:]:
                return True
        elif host == wanted:
            return True
    return False


def _guarded_transport(
    hosts: Sequence[str], max_bytes: Optional[int] = None
) -> Callable[..., Tuple[int, Mapping[str, str], bytes]]:
    """urllib, following a redirect only to another allowed host.

    The library's own transport follows every redirect, which is right for an
    address the host chose and wrong for one a caller named: an allowed host
    that answers 302 to an internal address would be followed there.
    """
    import urllib.error
    import urllib.request

    class _AllowedRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - urllib's signature
            if not allowed_host(newurl, hosts):
                raise _Refused(
                    403, f"{req.full_url} redirected to {newurl}, which is not an allowed host"
                )
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(_AllowedRedirects)

    def send(method: str, url: str, headers: Dict[str, str], body: bytes, timeout: float):
        request = urllib.request.Request(url, data=body or None, headers=headers, method=method)
        try:
            with opener.open(request, timeout=timeout) as reply:
                # One byte past the limit is enough to say it is too large;
                # reading the whole body first let any allowed host fill memory.
                return (
                    reply.status,
                    dict(reply.headers.items()),
                    reply.read() if max_bytes is None else reply.read(max_bytes + 1),
                )
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read()

    return send


# ================================================================== jobs ===


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_record(kind: str, params: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "job": "j_" + uuid.uuid4().hex[:16],
        "kind": kind,
        "status": "queued",
        "params": dict(params),
        "created": _now(),
        "updated": _now(),
    }


def _public(record: Mapping[str, Any]) -> Dict[str, Any]:
    """A job as a caller sees it: what it was asked, how it went, and what it made."""
    shown = {
        key: record[key] for key in ("job", "kind", "status", "created", "updated") if key in record
    }
    if record.get("result") is not None:
        shown["result"] = record["result"]
    if record.get("error"):
        shown["error"] = record["error"]
    return shown


class MemoryJobs:
    """Jobs run in this process, their results written to a folder.

    For one server on one machine: a restart forgets every job, and a second
    instance cannot see the first one's. A function app scales out and
    restarts at will, so it wants :class:`AzureJobs`.
    """

    def __init__(self, output: Union[str, Path] = "./output", executor: Any = None) -> None:
        self.output = Path(output)
        self._records: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._executor = executor or concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="vectrixdb-job"
        )

    def submit(
        self, kind: str, params: Mapping[str, Any], runner: Callable[[Mapping[str, Any]], Any]
    ) -> Dict[str, Any]:
        record = _new_record(kind, params)
        with self._lock:
            self._records[record["job"]] = record
            # The job as it was handed over. Taken first, because a fast
            # worker can finish it before this returns, and the answer to a
            # submission describes the submission.
            submitted = dict(record)
        self._executor.submit(self._run, record["job"], runner)
        return submitted

    def _run(self, job: str, runner: Callable[[Mapping[str, Any]], Any]) -> None:
        self._update(job, status="running")
        try:
            result = runner(self.get(job) or {})
        except Exception as exc:  # noqa: BLE001 - a failed job says why rather than vanishing
            logger.warning("job %s failed: %s", job, exc)
            self._update(job, status="failed", error=str(exc))
            return
        self._update(job, status="done", result=result)

    def _update(self, job: str, **changes: Any) -> None:
        with self._lock:
            if job in self._records:
                self._records[job].update(changes, updated=_now())

    def get(self, job: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            found = self._records.get(job)
            return dict(found) if found else None

    def keep(self, source: Path, folder: str) -> str:
        target = self.output / folder
        target.mkdir(parents=True, exist_ok=True)
        placed = target / source.name
        shutil.copy2(source, placed)
        return str(placed)


class AzureJobs:
    """Jobs in Azure Storage: a record a job in a container, the work on a queue.

    The route writes the record and a message and answers at once; the
    queue trigger, which has the function's whole timeout rather than the
    230 seconds an HTTP request gets, does the work and writes the result
    beside the record. Any instance can answer ``GET /jobs/{job}``, because
    the record is in the container and not in anybody's memory.

    A job that fails is recorded as failed and not raised, so the queue does
    not try it five more times: a video YouTube refused once it refuses
    again, and each try is paid for.
    """

    def __init__(self, container: Any, queue: Any, *, folder: str = "transcripts") -> None:
        self._container = container
        self._queue = queue
        self.folder = folder

    @classmethod
    def from_environment(cls, env: Optional[Mapping[str, str]] = None) -> "AzureJobs":
        """From ``VECTRIXDB_EXTRACT_STORAGE``: a connection string, or an account URL and the managed identity.

        Left unset, ``AzureWebJobsStorage``, which a function app always has.
        ``VECTRIXDB_EXTRACT_CONTAINER`` and ``VECTRIXDB_EXTRACT_QUEUE`` name
        the container and queue, ``extraction`` and ``extract-jobs`` by
        default, and both are made when they are not there.
        """
        found = os.environ if env is None else env
        where = str(
            found.get("VECTRIXDB_EXTRACT_STORAGE") or found.get("AzureWebJobsStorage") or ""
        ).strip()
        if not where:
            raise ConfigurationError(
                "AzureJobs needs VECTRIXDB_EXTRACT_STORAGE: a storage connection string, or an account URL "
                "like https://<account>.blob.core.windows.net for the managed identity"
            )
        try:
            from azure.storage.blob import BlobServiceClient
            from azure.storage.queue import QueueServiceClient
        except ImportError as exc:
            raise DependencyError(
                getattr(exc, "name", None) or "azure-storage-queue", "jobs-azure"
            ) from exc
        container_name = str(found.get("VECTRIXDB_EXTRACT_CONTAINER") or "extraction")
        queue_name = str(found.get("VECTRIXDB_EXTRACT_QUEUE") or "extract-jobs")
        if where.startswith("https://"):
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential()
            blobs = BlobServiceClient(where, credential=credential)
            queues = QueueServiceClient(where.replace(".blob.", ".queue."), credential=credential)
        else:
            blobs = BlobServiceClient.from_connection_string(where)
            queues = QueueServiceClient.from_connection_string(where)
        container = blobs.get_container_client(container_name)
        queue = queues.get_queue_client(queue_name)
        for made in (container.create_container, queue.create_queue):
            try:
                made()
            except Exception as exc:  # noqa: BLE001 - there already, which is what was wanted
                if "exist" not in str(exc).lower():
                    raise
        return cls(container, queue)

    def _write(self, record: Mapping[str, Any]) -> None:
        self._container.upload_blob(
            f"jobs/{record['job']}.json", json.dumps(record, default=str), overwrite=True
        )

    def submit(self, kind: str, params: Mapping[str, Any], runner: Any = None) -> Dict[str, Any]:
        record = _new_record(kind, params)
        self._write(record)
        self._queue.send_message(json.dumps({"job": record["job"]}))
        return record

    def get(self, job: str) -> Optional[Dict[str, Any]]:
        if not re.match(r"^j_[0-9a-f]{16}$", job or ""):
            return None
        try:
            return json.loads(self._container.download_blob(f"jobs/{job}.json").readall())
        except Exception as exc:  # noqa: BLE001 - not there is None; anything else is raised
            if (
                "not" in str(exc).lower()
                and "found" in str(exc).lower()
                or getattr(exc, "status_code", None) == 404
            ):
                return None
            raise

    def run(self, job: str, runner: Callable[[Mapping[str, Any]], Any]) -> None:
        record = self.get(job)
        if record is None:
            logger.warning("job %s has no record, so there is nothing to run", job)
            return
        if record.get("status") in ("done", "failed"):
            return  # a message delivered twice is not a job run twice
        record.update(status="running", updated=_now())
        self._write(record)
        try:
            record["result"] = runner(record)
            record["status"] = "done"
        except Exception as exc:  # noqa: BLE001 - recorded, not raised: see the class docstring
            logger.warning("job %s failed: %s", job, exc)
            record.update(status="failed", error=str(exc))
        record["updated"] = _now()
        self._write(record)

    def keep(self, source: Path, folder: str) -> str:
        name = f"{self.folder}/{folder}/{source.name}"
        with open(source, "rb") as handle:
            self._container.upload_blob(name, handle, overwrite=True)
        return f"{self._container.url}/{name}"


# ================================================================ services ===


@dataclass
class ExtractionService:
    """What reads what, and where jobs go. Built from the environment unless given.

    ``image`` reads a picture's words, ``audio`` a recording's; a video is its
    sound taken out and given to ``audio``. ``describer`` says what a picture
    shows and describes the figures inside a document; from the settings it
    is a chat model that can see when one is set, then Vision, then the
    words alone, asked in that order for each picture. ``page_ocr`` reads a PDF's
    pages that have no text layer. ``pdf_layout``, when set, reads a PDF
    whole with Document Intelligence's layout model instead: tables as
    rows, headings, the number printed on each page, running headers and
    footers taken out, and each chart cut out of its page for ``describer``.
    ``page_reader``, when set, reads by sight the pages of a PDF the rules
    are unsure of, or every page with ``every_page``, each held to the page's
    own words. Any of them can be None, and the routes that need one say
    which setting is missing.
    """

    image: Any = None
    audio: Any = None
    describer: Any = None
    page_ocr: Any = None
    pdf_layout: Any = None
    translator: Any = None
    url_hosts: Tuple[str, ...] = ()
    jobs: Any = None
    download: Any = None
    transport: Any = None
    timeout: float = 60.0
    max_bytes: int = 100 * 1024 * 1024
    #: Frames of a video read for what is on screen, one at each change of scene; 0 reads none.
    video_frames: int = 12
    #: Reads a PDF's pages by sight, the pages the rules are unsure of; None reads them by the rules alone.
    page_reader: Any = None
    #: With a page reader, every page with words on it goes to it, not the unsure ones alone.
    every_page: bool = False
    _fetch: Any = field(default=None, repr=False, init=False)

    def __post_init__(self) -> None:
        self.url_hosts = tuple(h for h in (self.url_hosts or ()) if str(h).strip())
        self._fetch = self.transport or _guarded_transport(self.url_hosts, self.max_bytes)

    @classmethod
    def from_environment(
        cls, env: Optional[Mapping[str, str]] = None, **given: Any
    ) -> "ExtractionService":
        """Every service whose settings are there; ``given`` wins over the environment.

        Azure first: Document Intelligence for pictures and scanned pages,
        Speech for sound, Vision for what a picture shows, Translator for
        languages. A chat model that can see, named by
        ``AZURE_OPENAI_VISION_DEPLOYMENT`` or ``VECTRIXDB_DESCRIBER_URL``,
        describes pictures in detail ahead of Vision. Without Azure, pictures are read by RapidOCR and sound by
        Whisper on this machine, which need the ``ocr`` and ``asr`` extras.
        """
        found = dict(os.environ if env is None else env)
        from ..extract.engines import AzureSpeech, RapidOcr, Whisper
        from ..translate import AzureTranslator

        pieces: Dict[str, Any] = {}
        docintel = _docintel(found)
        pieces["image"] = docintel if docintel is not None else RapidOcr()
        if docintel is not None:
            pieces["page_ocr"] = _page_reader(docintel)
        speech, speech_key = (
            found.get("AZURE_SPEECH_ENDPOINT", "").strip(),
            found.get("AZURE_SPEECH_KEY", "").strip(),
        )
        # The languages a recording may be in, when a caller names none: Speech
        # hears which one it is. Up to four voices told apart, "Speaker 1:".
        locales = [
            part.strip()
            for part in found.get("VECTRIXDB_SPEECH_LOCALES", "en-US,fr-CA").split(",")
            if part.strip()
        ]
        speakers = int(found.get("VECTRIXDB_SPEECH_SPEAKERS", "4") or 0)
        pieces["audio"] = (
            AzureSpeech(speech, speech_key, locales=locales, speakers=speakers)
            if speech and speech_key
            else Whisper()
        )
        pieces["video_frames"] = int(found.get("VECTRIXDB_VIDEO_FRAMES", "12") or 0)
        pieces["translator"] = AzureTranslator.from_environment(found)
        pieces["url_hosts"] = tuple(
            h.strip() for h in found.get("VECTRIXDB_EXTRACT_URL_HOSTS", "").split(",") if h.strip()
        )
        jobs = found.get("VECTRIXDB_EXTRACT_JOBS", "memory").strip().lower()
        if "jobs" not in given:
            if jobs == "azure":
                pieces["jobs"] = AzureJobs.from_environment(found)
            else:
                pieces["jobs"] = MemoryJobs(found.get("VECTRIXDB_EXTRACT_OUTPUT") or "./output")
        if found.get("VECTRIXDB_MAX_UPLOAD_BYTES"):
            pieces["max_bytes"] = int(found["VECTRIXDB_MAX_UPLOAD_BYTES"])
        pieces.update(given)
        if "describer" not in given:
            # For each picture: a chat model that can see, then Vision, then
            # the words alone. None when nothing can see, and then a
            # document's pictures are not opened at all.
            from ..extract.describers import describer_from_environment

            pieces["describer"] = describer_from_environment(found, reader=pieces.get("image"))
        if (
            found.get("VECTRIXDB_EXTRACT_PDF", "").strip().lower() == "vision"
            and "page_reader" not in given
        ):
            # The pages a person would have to look at to read, read by a
            # model that can see them, held to each page's own words.
            from ..extract.page_reader import PageReader

            pieces["page_reader"] = PageReader.from_environment(found)
            pieces["every_page"] = (
                found.get("VECTRIXDB_VISION_PAGES", "hard").strip().lower() == "all"
            )
            if pieces["page_reader"] is None:
                logger.warning(
                    "VECTRIXDB_EXTRACT_PDF=vision needs a chat model that can see: AZURE_OPENAI_VISION_DEPLOYMENT "
                    "with AZURE_OPENAI_ENDPOINT, or VECTRIXDB_DESCRIBER_URL; PDFs are read by the rules alone"
                )
        if (
            found.get("VECTRIXDB_EXTRACT_PDF", "").strip().lower() == "layout"
            and "pdf_layout" not in given
        ):
            client = getattr(pieces.get("image"), "client", None)
            if client is None:
                logger.warning(
                    "VECTRIXDB_EXTRACT_PDF=layout needs AZURE_DOCINTEL_ENDPOINT and AZURE_DOCINTEL_KEY; "
                    "PDFs are read from their own text instead"
                )
            else:
                from ..extract.engines import AzureDocumentIntelligence

                # Charts are cut out of their pages only when something is there to describe them.
                pieces["pdf_layout"] = AzureDocumentIntelligence(
                    client, model="prebuilt-layout", crops=pieces.get("describer") is not None
                )
        return cls(**pieces)

    # -- reading

    def listening_in(self, language: Optional[str]) -> Any:
        """The sound engine for this call's language, the shared one untouched."""
        if self.audio is None:
            raise _Refused(
                503, "nothing reads sound: set AZURE_SPEECH_ENDPOINT and AZURE_SPEECH_KEY"
            )
        if not language:
            return self.audio
        from ..extract.youtube import _listening_in

        return _listening_in(self.audio, language)

    def read_document(self, data: bytes, name: str, kind: str) -> Any:
        from ..ingest import _pdf_extras, describe_figures, load_bytes

        describe = self.describer is not None
        if kind == "pdf" and self.pdf_layout is not None:
            doc = self.pdf_layout(data, name)
            doc.metadata = {"source": name, "filename": name, "kind": "pdf", **doc.metadata}
            # Its title, and what the service found none of: bookmarks, the page-label table.
            _pdf_extras(data, doc)
        else:
            doc = load_bytes(
                data,
                name,
                kind=DOCUMENT_KINDS[kind],
                images=describe,
                ocr=self.page_ocr,
                page_reader=self.page_reader,
                every_page=self.every_page,
            )
        if describe and doc.figures:
            doc = describe_figures(doc, self.describer, name=name)
        return doc

    def read_image(self, data: bytes, name: str) -> Any:
        from ..ingest import LoadedDocument

        if self.image is None:
            raise _Refused(
                503, "nothing reads pictures: set AZURE_DOCINTEL_ENDPOINT and AZURE_DOCINTEL_KEY"
            )
        doc = self.image(data, name)
        # Its words were read just now, so the words-only last resort is not asked again.
        describer = getattr(self.describer, "seeing", self.describer)
        if describer is not None:
            said = describer(data, {"caption": "", "name": name, "src": name})
            found = said if isinstance(said, Mapping) else {"description": said} if said else {}
            description = str(found.get("description") or "")
            if doc.text.strip():
                # The reader's words are exact; the describer's list of them is a second, rougher copy.
                description = "\n".join(
                    line
                    for line in description.splitlines()
                    if not line.startswith("Words in the picture:")
                )
            from ..ingest import _header_rows_lines

            parts = [
                f"[Figure: {found['caption']}]" if found.get("caption") else "",
                description.strip(),
            ]
            rows = found.get("table")
            if rows:
                # A chart's values or a table's rows, the way a sheet's rows are written.
                parts.append("\n".join(_header_rows_lines(rows)))
            parts.append(doc.text.strip())
            text = "\n\n".join(part for part in parts if part)
            if text.strip() and (description or rows):
                meta = {**doc.metadata, "described": True}
                if found.get("by"):
                    meta["described_by"] = found["by"]
                doc = LoadedDocument(text=text, metadata=meta)
        return doc

    def read_audio(self, data: bytes, name: str, language: Optional[str]) -> Any:
        return self.listening_in(language)(data, name)

    def read_video(self, data: bytes, name: str, language: Optional[str]) -> Any:
        from ..extract.engines import Video

        video = Video(audio=self.listening_in(language))
        if self.video_frames and self.image is not None and hasattr(video, "frames"):
            # The screen is read with the picture reader, a frame at each change of scene.
            video.frames, video.max_frames = self.image, self.video_frames
        return video(data, name)

    def fetch(self, url: str) -> Tuple[bytes, str, str]:
        """An allowed address's bytes, the name to read them by, and their content type."""
        if not self.url_hosts:
            raise _Refused(
                403,
                "no address may be fetched: set VECTRIXDB_EXTRACT_URL_HOSTS to the hosts that may",
            )
        if not allowed_host(url, self.url_hosts):
            raise _Refused(
                403,
                f"{urlparse(url).hostname or url} is not one of the hosts this service fetches from",
            )
        status, headers, body = self._fetch("GET", url, {"Accept": "*/*"}, b"", self.timeout)
        if not 200 <= int(status) < 300:
            raise _Refused(502, f"{url} answered {status}")
        if len(body) > self.max_bytes:
            raise _Refused(413, f"{url} is larger than {self.max_bytes:,} bytes")
        content_type = next(
            (
                str(v).lower()
                for k, v in (headers or {}).items()
                if str(k).lower() == "content-type"
            ),
            "",
        )
        return bytes(body), PurePosixPath(urlparse(url).path).name, content_type

    # -- jobs

    def run_job(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        if record.get("kind") != "youtube_save":
            raise ValueError(f"no job is called {record.get('kind')!r}")
        return self._youtube_save(record.get("params") or {})

    def _youtube_save(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        from ..extract.youtube import load_youtube

        keep_audio = not bool(params.get("auto_delete", True))
        folder = str(params.get("output_dir") or "output")
        with tempfile.TemporaryDirectory(prefix="vectrixdb-save-") as scratch:
            doc = load_youtube(
                str(params["url"]),
                audio=self.audio,
                language=params.get("language") or None,
                save_to=scratch,
                keep_audio=keep_audio,
                download=self.download,
            )
            kept = self.jobs.keep(Path(doc.metadata["saved_to"]), folder)
            if keep_audio and doc.metadata.get("audio_saved_to"):
                self.jobs.keep(Path(doc.metadata["audio_saved_to"]), folder)
        about = doc.metadata
        # The fields a synchronous youtube_save answered with, so a caller
        # that waited for them finds them in the finished job.
        return {
            "success": True,
            "video_title": about.get("title") or about.get("youtube_id"),
            "transcript_file": kept,
            "transcript_filename": PurePosixPath(kept.replace("\\", "/")).name,
            "media_deleted": None if keep_audio else about.get("audio_file"),
            "auto_delete_enabled": not keep_audio,
            "duration": about.get("duration", 0),
            "channel": about.get("channel", "Unknown"),
        }


def _docintel(env: Mapping[str, str]) -> Any:
    endpoint, key = (
        env.get("AZURE_DOCINTEL_ENDPOINT", "").strip(),
        env.get("AZURE_DOCINTEL_KEY", "").strip(),
    )
    if not (endpoint and key):
        return None
    try:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.core.credentials import AzureKeyCredential
    except ImportError as exc:
        raise DependencyError("azure-ai-documentintelligence", "ocr-azure") from exc
    from ..extract.engines import AzureDocumentIntelligence

    return AzureDocumentIntelligence(
        DocumentIntelligenceClient(endpoint, AzureKeyCredential(key)), model="prebuilt-read"
    )


def _page_reader(docintel: Any) -> Callable[[bytes], List[str]]:
    def read(page: bytes) -> List[str]:
        return [line for line in docintel(page, "page.png").text.splitlines() if line.strip()]

    return read


# ============================================================ the answers ===


def _wants_json(request: Request) -> bool:
    """JSON when ``Accept`` puts it ahead of every text type, as HttpExtractor does."""
    kinds = []
    for part in request.headers.get("accept", "").split(","):
        media, _, params = part.strip().partition(";")
        weight = 1.0
        for param in params.split(";"):
            key, _, value = param.strip().partition("=")
            if key == "q":
                try:
                    weight = float(value)
                except ValueError:
                    weight = 0.0
        kinds.append((media.strip().lower(), weight))
    json_weight = max((w for m, w in kinds if m == "application/json"), default=0.0)
    text_weight = max((w for m, w in kinds if m.startswith("text/")), default=0.0)
    return json_weight > 0 and json_weight >= text_weight


def _as_json(doc: Any) -> Dict[str, Any]:
    from ..ingest import _jsonable

    reply = {
        "text": doc.text,
        "pages": [list(p) for p in doc.pages],
        "headings": [list(h) for h in doc.headings],
        "metadata": _jsonable(doc.metadata),
        "segments": [list(s) for s in doc.segments],
    }
    figures = getattr(doc, "figures", None)
    if figures:
        # Where each figure line is and what it is: its caption, its name, who
        # described it. Never the picture: pictures do not travel.
        reply["figures"] = [[int(o), _jsonable(dict(info))] for o, info in figures]
    labels = getattr(doc, "page_labels", None)
    if labels:
        # Only for a PDF that numbers its own pages: {"41": "39"}.
        reply["page_labels"] = {str(n): str(label) for n, label in sorted(labels.items())}
    return reply


#: Keys whose values name the file or the machinery, not what the file says.
_NOT_WORDS = frozenset(
    {
        "kind",
        "source",
        "filename",
        "doc_id",
        "language",
        "engine",
        "format",
        "content_type",
        "src",
        "name",
        "described_by",
        "mime",
        "running_lines",
    }
)

#: Between two texts sent to the masking engine at once: nothing any engine takes for a name, a number or an address.
_BETWEEN = "\n␞\n"


def _mask_everything_else(
    reply: Dict[str, Any], types: Optional[str], engine: Any, language: Optional[str]
) -> None:
    """Mask every text in a JSON reply but its ``text``, which is masked already, in place.

    The texts go to the engine in one call, joined by a mark no engine
    changes, so a transcript of four hundred segments costs one call and
    not four hundred. When the mark does not come back as often as it went,
    each text is masked on its own.
    """
    from ..masking import mask

    places: List[Tuple[Any, Any]] = []

    def walk(value: Any, key: Any = None) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if k in _NOT_WORDS or (value is reply and k == "text"):
                    continue
                if isinstance(v, str):
                    if v.strip():
                        places.append((value, k))
                else:
                    walk(v, k)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                if isinstance(v, str):
                    if v.strip() and not _NOT_WORDS.intersection({key}):
                        places.append((value, i))
                else:
                    walk(v, key)

    walk(reply)
    if not places:
        return
    texts = [holder[at] for holder, at in places]
    joined = mask(_BETWEEN.join(texts), types=types, engine=engine, language=language).text
    parts = joined.split(_BETWEEN)
    if len(parts) != len(texts):
        parts = [mask(t, types=types, engine=engine, language=language).text for t in texts]
    for (holder, at), masked in zip(places, parts):
        holder[at] = masked


def _mask_wanted(request: Request) -> bool:
    return str(request.query_params.get("mask", "")).strip().lower() not in (
        "",
        "0",
        "no",
        "false",
        "off",
    )


async def _answer(request: Request, doc: Any, markdown: Optional[str] = None) -> Response:
    """The document as JSON or Markdown, masked first when the caller asked with ``?mask=1``.

    ``types`` names what to mask, ``all`` or a comma-separated list, the
    identifiers when left out; ``language`` is the text's, or the language the
    document carries, which a transcript does. The engine is the deployment's,
    ``VECTRIXDB_MASKING_ENGINE``, and the patterns run after it whatever it is.
    """
    if not _mask_wanted(request):
        if _wants_json(request):
            return JSONResponse(_as_json(doc))
        return PlainTextResponse(
            doc.text if markdown is None else markdown, media_type="text/markdown"
        )
    from ..masking import mask

    engine = getattr(request.app.state, "masking", None)
    language = request.query_params.get("language") or (
        doc.metadata.get("language") if isinstance(doc.metadata, dict) else None
    )
    types = request.query_params.get("types") or None
    try:
        done = await run_in_threadpool(
            mask, doc.text, types=types, engine=engine, language=language
        )
        shown = (
            done
            if markdown is None
            else await run_in_threadpool(
                mask, markdown, types=types, engine=engine, language=language
            )
        )
    except ConfigurationError as exc:
        raise _Refused(422, str(exc)) from exc
    # What the reply says was masked is what was masked in the text it carries.
    about = {
        key: value
        for key, value in (done if _wants_json(request) else shown).to_dict().items()
        if key != "text"
    }
    if _wants_json(request):
        reply = _as_json(doc)
        reply["text"] = done.text
        # The text is not the only place a name is written: a transcript's
        # segments, a heading, a figure's caption and a title carry the same
        # words. Every one of them is masked, or the reply masks nothing.
        await run_in_threadpool(_mask_everything_else, reply, types, engine, language)
        reply["masking"] = about
        return JSONResponse(reply)
    return PlainTextResponse(
        shown.text,
        media_type="text/markdown",
        headers={
            "X-Masking": json.dumps(
                {k: about[k] for k in ("engine", "score", "counts", "regex_only")}
            )
        },
    )


async def _body(request: Request, service: ExtractionService) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > service.max_bytes:
        raise _Refused(413, f"the file is larger than {service.max_bytes:,} bytes")
    data = await request.body()
    if not data:
        raise _Refused(400, "the request has no body: send the file as it is")
    if len(data) > service.max_bytes:
        raise _Refused(413, f"the file is larger than {service.max_bytes:,} bytes")
    return data


def _named(request: Request, suffix: Union[str, Tuple[str, ...]], default: str) -> str:
    """The file's name from X-Filename, or a default of the kind the route reads."""
    given = (
        unquote(request.headers.get("x-filename", "")).replace("\\", "/").rsplit("/", 1)[-1].strip()
    )
    if given and given.lower().endswith(suffix):
        return given
    return default


def _refuse_open(allow_open: bool) -> None:
    """Refuse to be built with no key and no sign-in, as run_server refuses to listen that way."""
    from .signin import full_key_configured

    if full_key_configured() or os.environ.get("VECTRIXDB_SIGNIN", "").strip():
        return
    if allow_open or os.environ.get("VECTRIXDB_ALLOW_OPEN", "").strip().lower() in _TRUE:
        logger.warning(
            "the extraction service is open: no API key and no sign-in, because it was told it may be"
        )
        return
    raise ConfigurationError(
        "refusing to build the extraction service with no API key and no sign-in: every call spends money, and "
        "anybody who found it would spend yours. Set VECTRIXDB_API_KEY, or turn sign-in on with VECTRIXDB_SIGNIN. "
        "If something in front of it does the asking, set VECTRIXDB_ALLOW_OPEN=1."
    )


def _door(base: Any, public: set) -> Any:
    """The library's access middleware, with a few more paths that need no key."""
    from .rootpath import route_path

    class Door(base):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            if route_path(request) in public:
                return await call_next(request)
            return await super().dispatch(request, call_next)

    return Door


# ================================================================ the app ===

#: The service the most recent app was built with, which the queue trigger
#: in the same process uses to run a job.
_current: Optional[ExtractionService] = None


def _route_key(path: str) -> str:
    """``extract/pdf`` for ``/extract/pdf``, ``jobs`` for ``/jobs/{job}``: a route as the gateway list names it."""
    return "/".join(part for part in path.strip("/").split("/") if not part.startswith("{"))


def _publish_gateway_paths(
    app: FastAPI, router: Any, prefix: str, gateways: Mapping[str, str]
) -> None:
    """Serve each listed route under its own gateway path too, so the gateway may pass it on or take it off.

    The copies are left out of the OpenAPI document, which lists each route
    once, under the prefix, because it is what a gateway imports. They are
    served before the routes themselves, so a gateway path is never read as
    part of another route: ``/jobs`` in front of ``/health`` is still health.
    A name the list gives that is not a route here is refused now, so a
    typing mistake is found when the app starts and not by its first caller.
    """
    from fastapi import APIRouter

    served = {_route_key(route.path) for route in router.routes}
    unknown = sorted(set(gateways) - served)
    if unknown:
        raise ConfigurationError(
            f"there is no route called {', '.join(unknown)} to give a gateway path to; "
            f"the routes are {', '.join(sorted(served))}"
        )
    for where in sorted(set(gateways.values())):
        published = APIRouter()
        published.routes.extend(
            route for route in router.routes if gateways.get(_route_key(route.path)) == where
        )
        app.include_router(published, prefix=f"{where}{prefix}", include_in_schema=False)


def extraction_routes(
    prefix: Optional[str] = "", gateway_paths: Union[None, str, Mapping[str, str]] = None
) -> Dict[str, str]:
    """Every suffix this service reads, and the path of the route that reads it.

    What a caller routes by: :class:`~vectrixdb.extract.HttpExtractor` and
    ``VECTRIXDB_EXTRACTOR_ROUTES`` take exactly this, so a server reading
    through the service sends each file to the one route that reads it, and
    a suffix missing from here is one the service does not read::

        {".pdf": "/extract/pdf", ".xlsm": "/extract/xlsx", ".png": "/transcribe/image", ...}

    ``prefix`` and ``gateway_paths`` are the service's own settings, as
    :func:`create_extraction_app` takes them. Each path is then the route's
    own gateway path, when it has one, the prefix, and the route: the path a
    caller asks for through the gateway, and one the service answers directly
    as well, so the same table serves with a gateway in front or without.
    """
    from ..extract.engines import AUDIO_SUFFIXES, IMAGE_SUFFIXES, VIDEO_SUFFIXES

    prefix = route_prefix(prefix)
    gateways = read_gateway_paths(gateway_paths)
    table: Dict[str, str] = {}

    def serve(suffixes: Sequence[str], route: str) -> None:
        path = f"{gateways.get(route.strip('/'), '')}{prefix}{route}"
        table.update({suffix: path for suffix in suffixes})

    for kind in DOCUMENT_KINDS:
        serve((f".{kind}", *_ALSO_NAMED.get(kind, ())), f"/extract/{kind}")
    serve(IMAGE_SUFFIXES, "/transcribe/image")
    serve(AUDIO_SUFFIXES, "/transcribe/audio")
    serve(VIDEO_SUFFIXES, "/transcribe/video")
    return table


class MaskRequest(BaseModel):
    """A text to mask: what to hide, ``all`` or a list or nothing for the identifiers, and the text's language."""

    text: str
    types: Optional[Union[str, List[str]]] = None
    language: Optional[str] = None


def create_extraction_app(
    service: Optional[ExtractionService] = None,
    *,
    prefix: Optional[str] = None,
    gateway_paths: Union[None, str, Mapping[str, str]] = None,
    allow_open: bool = False,
    **given: Any,
) -> FastAPI:
    """The extraction service as an ASGI app, for uvicorn or for an Azure function app.

    ``service`` is what reads what; left out, it is built from the
    environment, and ``given`` overrides any piece of it:
    ``create_extraction_app(url_hosts=["www.northwind.example"], translator=my_translator)``.

    ``prefix`` is the path every route lives under, the deployment's choice:
    ``/api`` puts the PDF route at ``/api/extract/pdf``, ``/health`` at
    ``/api/health`` and the documentation at ``/api/docs``. Left out, it is
    ``VECTRIXDB_EXTRACT_PREFIX``, and with neither the routes are at the
    root. It is part of the app, so the app answers at it with or without a
    gateway in front.

    ``gateway_paths`` is for a gateway that publishes each endpoint under a
    path of its own, chosen by the team that runs it: the paths as they were
    handed over, ``"extract/pdf=/files/pdf, jobs=/media/jobs"``, or the same
    as a mapping. Each listed route answers with its path in front or
    without it, so the gateway may pass the path on or take it off, and
    ``youtube_save`` hands back a job address that starts with the path of
    ``jobs``. Left out, it is ``VECTRIXDB_EXTRACT_GATEWAY_PATHS``. The
    gateway's host is never needed, nor any path the host carries: a caller
    puts the host it was given in front of every address.

    One path a proxy puts in front of every route is a third thing,
    ``VECTRIXDB_ROOT_PATH``: see :func:`vectrixdb.api.server.root_path_from_env`.

    ``allow_open`` builds it with no key and no sign-in, for a test or a
    laptop. Anywhere else, leave it off.
    """
    global _current
    _refuse_open(allow_open)
    prefix = route_prefix(
        os.environ.get("VECTRIXDB_EXTRACT_PREFIX", "") if prefix is None else prefix
    )
    gateways = read_gateway_paths(
        os.environ.get("VECTRIXDB_EXTRACT_GATEWAY_PATHS", "")
        if gateway_paths is None
        else gateway_paths
    )
    service = service or ExtractionService.from_environment(**given)
    _current = service

    from .. import __version__
    from ..brand import Brand
    from ..signin import SignInConfig
    from .signin import AccessMiddleware, SignInRuntime

    from .forwarded import trusted_from_env
    from .server import root_path_from_env

    # root_path: behind a proxy that serves the whole app under one path,
    # FastAPI routes either way and the OpenAPI document carries the path.
    from fastapi import APIRouter

    app = FastAPI(
        title="VectrixDB extraction",
        version=__version__,
        docs_url=f"{prefix}/docs",
        openapi_url=f"{prefix}/openapi.json",
        redoc_url=None,
        root_path=root_path_from_env(),
        # A redirect to add or drop a slash names the host the app sees, so
        # behind a gateway it sends the caller around it. A path with a
        # slash too many is not found, like any other wrong path.
        redirect_slashes=False,
    )
    router = APIRouter()
    app.state.extraction = service
    app.state.brand = Brand.from_env()
    # The engine that finds identifiers in a document, for ``?mask=1`` and ``/mask``:
    # chosen once, from the settings, so a bad setting is a start-up error.
    from ..masking import describe_engine, engine_from_env, languages_from_env

    app.state.masking = engine_from_env()
    app.state.masking_languages = languages_from_env()
    try:
        app.state.trusted_proxies = trusted_from_env()
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
    # Keys issued by a VectrixDB server, and tokens from its identity
    # provider, are honoured here too when the two share a sign-in store.
    signin = SignInConfig.from_env(os.environ.get("VECTRIXDB_PATH") or tempfile.gettempdir())
    app.state.signin = (
        SignInRuntime(signin, product=app.state.brand.name) if signin.enabled else None
    )
    # The library's door, told that health and the documentation are public
    # under the prefix too, or /api/health would ask a load balancer for a
    # key; and health under its own gateway path, when it has one.
    public = {f"{prefix}/health", f"{prefix}/docs", f"{prefix}/openapi.json"}
    if "health" in gateways:
        public.add(f"{gateways['health']}{prefix}/health")
    app.add_middleware(_door(AccessMiddleware, public))
    from .security_headers import SecurityHeadersMiddleware, frame_ancestors_from_env

    https = bool(
        app.state.signin is not None and app.state.signin.config.secure_cookies
    ) or os.environ.get("VECTRIXDB_PUBLIC_URL", "").strip().startswith("https://")
    app.add_middleware(SecurityHeadersMiddleware, ancestors=frame_ancestors_from_env(), https=https)
    if app.root_path:
        from .rootpath import RootPathMiddleware

        app.add_middleware(RootPathMiddleware, root_path=app.root_path)

    @app.exception_handler(_Refused)
    async def _refused(request: Request, exc: _Refused) -> Response:
        return refusal(exc.status, str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Response:
        return refusal(exc.status_code, message_of(exc.detail), detail=exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _shape(request: Request, exc: RequestValidationError) -> Response:
        detail = [
            {k: v for k, v in error.items() if k in ("type", "loc", "msg")}
            for error in exc.errors()
        ]
        return refusal(422, message_of(detail), detail=detail)

    @app.exception_handler(DependencyError)
    async def _missing(request: Request, exc: DependencyError) -> Response:
        return refusal(503, str(exc))

    @app.exception_handler(ExtractionError)
    async def _unreadable(request: Request, exc: ExtractionError) -> Response:
        # A service that answered badly is the server's problem, 502; one that
        # is busy or out of quota, 503, so a caller knows to come back; a file
        # that cannot be read is the caller's, 422.
        if exc.status in (429, 503):
            return refusal(503, str(exc))
        return refusal(502 if exc.status else 422, str(exc))

    @app.exception_handler(TranslationError)
    async def _translation(request: Request, exc: TranslationError) -> Response:
        return refusal(502, str(exc))

    @app.exception_handler(ValueError)
    async def _bad_value(request: Request, exc: ValueError) -> Response:
        return refusal(422, str(exc))

    # -- about

    @router.get("/health", tags=["about"])
    async def health() -> Dict[str, Any]:
        return {
            "status": "healthy",
            "timestamp": _now(),
            "masking": describe_engine(app.state.masking, app.state.masking_languages),
        }

    # -- masking

    @router.post("/mask", tags=["mask"], summary="Mask identifiers in a text")
    async def mask_route(body: MaskRequest, request: Request) -> Dict[str, Any]:
        """The text with its identifiers masked, what was found with offsets, a risk score, and which engine ran.

        ``types`` is what to hide: nothing said means the identifiers, not
        names; ``all`` means everything the engine knows; or a list. The
        engine is the deployment's, and the patterns run after it whatever
        it is, so a shape the model missed is still caught by its form.
        """
        from ..masking import mask

        try:
            done = await run_in_threadpool(
                mask, body.text, types=body.types, engine=app.state.masking, language=body.language
            )
        except ConfigurationError as exc:
            raise _Refused(422, str(exc)) from exc
        return done.to_dict()

    # -- documents

    def document_route(kind: str) -> None:
        async def extract(request: Request) -> Response:
            data = await _body(request, service)
            name = _named(request, (f".{kind}", *_ALSO_NAMED.get(kind, ())), f"document.{kind}")
            doc = await run_in_threadpool(service.read_document, data, name, kind)
            return await _answer(request, doc)

        extract.__name__ = f"extract_{kind}"
        router.post(f"/extract/{kind}", tags=["extract"], summary=f"Read a .{kind} file")(extract)

    for kind in DOCUMENT_KINDS:
        document_route(kind)

    # -- files that are not documents

    from ..extract.engines import transcript_markdown

    @router.post("/transcribe/image", tags=["transcribe"])
    async def transcribe_image(request: Request) -> Response:
        data = await _body(request, service)
        name = _named(request, "", _MEDIA_NAMES["image"])
        return await _answer(request, await run_in_threadpool(service.read_image, data, name))

    @router.post("/transcribe/audio", tags=["transcribe"])
    async def transcribe_audio(request: Request, language: Optional[str] = None) -> Response:
        data = await _body(request, service)
        name = _named(request, "", _MEDIA_NAMES["audio"])
        doc = await run_in_threadpool(service.read_audio, data, name, language)
        doc.metadata.setdefault("filename", name)
        if language:
            doc.metadata.setdefault("language", language)
        return await _answer(request, doc, transcript_markdown(doc))

    @router.post("/transcribe/video", tags=["transcribe"])
    async def transcribe_video(request: Request, language: Optional[str] = None) -> Response:
        data = await _body(request, service)
        name = _named(request, "", _MEDIA_NAMES["video"])
        doc = await run_in_threadpool(service.read_video, data, name, language)
        doc.metadata.setdefault("filename", name)
        if language:
            doc.metadata.setdefault("language", language)
        return await _answer(request, doc, transcript_markdown(doc))

    # -- addresses

    @router.post("/transcribe/webpage", tags=["transcribe"])
    async def transcribe_webpage(body: WebpageBody, request: Request) -> Response:
        """The page's text. The pictures, sound and video it embeds are not fetched: each would be
        another address, usually on a CDN nobody allowed, so include_* are taken and not acted on."""
        from ..ingest import load_bytes

        data, _name, _type = await run_in_threadpool(service.fetch, body.url)
        doc = await run_in_threadpool(
            lambda: load_bytes(data, "page.html", kind="html", source=body.url)
        )
        doc.metadata["embedded_media"] = "not read"
        return await _answer(request, doc)

    @router.post("/transcribe/image_url", tags=["transcribe"])
    async def transcribe_image_url(body: UrlBody, request: Request) -> Response:
        data, name, _type = await run_in_threadpool(service.fetch, body.url)
        return await _answer(
            request,
            await run_in_threadpool(service.read_image, data, name or _MEDIA_NAMES["image"]),
        )

    @router.post("/transcribe/audio_url", tags=["transcribe"])
    async def transcribe_audio_url(body: UrlBody, request: Request) -> Response:
        data, name, _type = await run_in_threadpool(service.fetch, body.url)
        doc = await run_in_threadpool(
            service.read_audio, data, name or _MEDIA_NAMES["audio"], body.language
        )
        doc.metadata.update(source=body.url, language=body.language)
        doc.metadata.setdefault("filename", name)
        return await _answer(request, doc, transcript_markdown(doc))

    @router.post("/transcribe/video_url", tags=["transcribe"])
    async def transcribe_video_url(body: UrlBody, request: Request) -> Response:
        data, name, _type = await run_in_threadpool(service.fetch, body.url)
        doc = await run_in_threadpool(
            service.read_video, data, name or _MEDIA_NAMES["video"], body.language
        )
        doc.metadata.update(source=body.url, language=body.language)
        doc.metadata.setdefault("filename", name)
        return await _answer(request, doc, transcript_markdown(doc))

    # -- YouTube

    @router.post("/transcribe/youtube", tags=["transcribe"])
    async def transcribe_youtube(body: UrlBody, request: Request) -> Response:
        from ..extract.youtube import load_youtube

        doc = await run_in_threadpool(
            lambda: load_youtube(
                body.url, audio=service.audio, language=body.language, download=service.download
            )
        )
        return await _answer(request, doc, transcript_markdown(doc))

    @router.post("/transcribe/youtube_save", tags=["transcribe"], status_code=202)
    async def transcribe_youtube_save(body: YouTubeSaveBody, request: Request) -> Response:
        from ..extract.youtube import video_id

        if video_id(body.url) is None:
            raise _Refused(422, f"{body.url!r} is not the address of one YouTube video")
        if not _FOLDER.match(body.output_dir) or ".." in body.output_dir:
            raise _Refused(
                422,
                "output_dir is one folder name, letters, digits, spaces, dots, dashes and underscores",
            )
        record = await run_in_threadpool(
            service.jobs.submit, "youtube_save", body.model_dump(), service.run_job
        )
        # The address the caller asks next, from the host on: a proxy's
        # path, the job route's own gateway path, then the prefix. The
        # caller puts the host it was given in front.
        check = f"{request.scope.get('root_path', '')}{gateways.get('jobs', '')}{prefix}/jobs/{record['job']}"
        return JSONResponse({"job": record["job"], "status": record["status"], "check": check}, 202)

    @router.get("/jobs/{job}", tags=["transcribe"])
    async def job_status(job: str) -> Response:
        record = await run_in_threadpool(service.jobs.get, job)
        if record is None:
            raise _Refused(404, f"there is no job {job}")
        return JSONResponse(_public(record))

    # -- anything

    @router.post("/transcribe/auto", tags=["transcribe"])
    async def transcribe_auto(body: UrlBody, request: Request) -> Response:
        """A YouTube video, or an allowed address read by what it turns out to be."""
        from ..extract.youtube import is_youtube

        if is_youtube(body.url):
            return await transcribe_youtube(body, request)
        data, name, content_type = await run_in_threadpool(service.fetch, body.url)
        suffix = PurePosixPath(name).suffix.lower().lstrip(".")
        if suffix in DOCUMENT_KINDS:
            return await _answer(
                request, await run_in_threadpool(service.read_document, data, name, suffix)
            )
        from ..ingest import media_kind_of

        # A server that labels everything application/octet-stream says
        # nothing; the file's own first bytes say what it is.
        if not content_type.startswith(("image/", "audio/", "video/", "text/html")):
            proven = media_kind_of(data[:8192])
            if proven in ("image", "audio", "video"):
                content_type = f"{proven}/{suffix or 'octet-stream'}"
            elif proven in ("pdf", "office", "rtf"):
                kind = {"pdf": "pdf", "office": "docx", "rtf": "doc"}[proven]
                return await _answer(
                    request,
                    await run_in_threadpool(
                        service.read_document, data, name or f"file.{kind}", kind
                    ),
                )
        if content_type.startswith("image/"):
            return await _answer(
                request,
                await run_in_threadpool(service.read_image, data, name or _MEDIA_NAMES["image"]),
            )
        if content_type.startswith(("audio/", "video/")):
            reader = service.read_audio if content_type.startswith("audio/") else service.read_video
            doc = await run_in_threadpool(
                reader, data, name or _MEDIA_NAMES["audio"], body.language
            )
            doc.metadata.update(source=body.url, language=body.language)
            return await _answer(request, doc, transcript_markdown(doc))
        from ..ingest import load_bytes

        doc = await run_in_threadpool(
            lambda: load_bytes(data, "page.html", kind="html", source=body.url)
        )
        return await _answer(request, doc)

    # -- languages

    def translator() -> Any:
        if service.translator is None:
            raise _Refused(
                503,
                "translation is not set up: set AZURE_TRANSLATOR_KEY, and AZURE_TRANSLATOR_REGION for a regional resource",
            )
        return service.translator

    @router.post("/translate/text", tags=["translate"])
    async def translate_text(body: TranslateBody) -> Dict[str, Any]:
        texts = [body.text] if isinstance(body.text, str) else list(body.text)
        said = await run_in_threadpool(
            translator().translate, texts, body.to, source=body.from_lang
        )
        translations = []
        for one in said:
            detected = one.get("detected") or {}
            translations.append(
                {
                    "text": one["translations"].get(body.to, ""),
                    "to": body.to,
                    "detected_language": detected.get("language"),
                    "detected_score": detected.get("score"),
                }
            )
        return {
            "translations": translations,
            "from_language": body.from_lang or "auto-detected",
            "to_language": body.to,
        }

    @router.post("/translate/detect", tags=["translate"])
    async def translate_detect(body: DetectBody) -> Dict[str, Any]:
        texts = [body.text] if isinstance(body.text, str) else list(body.text)
        said = await run_in_threadpool(translator().detect, texts)
        return {
            "detections": [
                {
                    "language": one.get("language"),
                    "score": one.get("score"),
                    "is_translation_supported": one.get("translatable"),
                    "is_transliteration_supported": None,
                }
                for one in said
            ]
        }

    @router.get("/translate/languages", tags=["translate"])
    async def translate_languages() -> Dict[str, Any]:
        # The list needs no key, so it answers even when translation is not set up.
        from ..translate import AzureTranslator

        found = await run_in_threadpool(
            (service.translator or AzureTranslator(transport=None)).languages
        )
        languages = {
            code: {
                "name": info.get("name"),
                "native_name": info.get("native"),
                "dir": info.get("direction", "ltr"),
            }
            for code, info in found.items()
        }
        return {"languages": languages, "count": len(languages)}

    _publish_gateway_paths(app, router, prefix, gateways)
    app.include_router(router, prefix=prefix)
    return app


def run_extraction_job(
    body: Union[bytes, str], service: Optional[ExtractionService] = None
) -> None:
    """Run the job a queue message names, for the function app's queue trigger.

    The message is ``{"job": "j_..."}``, as ``AzureJobs.submit`` writes it.
    ``service`` defaults to the one the app in this process was built with,
    so the trigger and the routes read with the same services.
    """
    service = service or _current or ExtractionService.from_environment()
    if not isinstance(service.jobs, AzureJobs):
        raise ConfigurationError(
            "run_extraction_job is for AzureJobs: set VECTRIXDB_EXTRACT_JOBS=azure"
        )
    text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
    job = str(json.loads(text).get("job") or "")
    service.jobs.run(job, service.run_job)
