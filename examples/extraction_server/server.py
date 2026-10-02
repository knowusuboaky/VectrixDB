"""An extraction server VectrixDB can call: a key on the way in, pages from a PDF, minutes from speech.

Reference code, to read and to copy from. The library reads a file through a
server like this with :class:`vectrixdb.extract.HttpExtractor`::

    from vectrixdb import Vectrix
    from vectrixdb.extract import HttpExtractor

    reader = HttpExtractor(
        "https://extract.company.com",
        {".pdf": "/ocr/pdf", ".png": "/ocr/image", ".wav": "/asr/audio"},
        body="raw",
        headers={"api-key": os.environ["EXTRACTION_API_KEY"]},
    )
    db = Vectrix("reports", extractors=reader, keep_source=True)

Three things a server written in an afternoon tends to leave out, and what
each costs:

1. **A key on the way in.** The keys in such a server's settings are usually
   the ones it sends to Azure or AWS, and nothing checks who is calling it, so
   anybody who finds the port reads documents through your OCR account.
   :func:`create_app` refuses to start without ``EXTRACTION_API_KEY`` unless
   told the network does the asking, and compares the key in constant time.
2. **Page spans from the PDF route.** A reply that is only text gives a
   document cited by name. :func:`pages_reply` says where each page starts,
   and that is what makes ``scan.pdf#page=4`` possible.
3. **Minute offsets from the audio route.** :func:`minutes_reply` makes a
   page of every minute, so a hit in a transcript is cited
   ``call.wav#page=12`` and a listener goes to the twelfth minute.

The fourth, a queue between the storage event and the worker, is not in this
server at all: see ``examples/deploy/azure_ingest_queue``.

The two reply helpers import nothing, so they paste into a server that does
not have VectrixDB installed. The engines are yours: :func:`create_app` takes
three functions, and :func:`azure_engines` builds them from Azure Document
Intelligence and Azure Speech. The suite runs this file with made-up engines;
the Azure ones are not run there, because every line of them talks to Azure.

Run it::

    pip install fastapi uvicorn
    set EXTRACTION_API_KEY=...            # a long random string
    uvicorn server:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:  # the two reply helpers and the key check need none of it
    from fastapi import FastAPI, HTTPException, Request
except ImportError:  # pragma: no cover - only create_app needs it, and says so
    FastAPI = None  # type: ignore[assignment,misc]

__all__ = ["azure_engines", "create_app", "key_is_right", "minutes_reply", "pages_reply", "presented_key"]

#: bytes and the file's name in; the text of each page out, in order, one string a page.
ReadPages = Callable[[bytes, str], Sequence[str]]
#: bytes and the file's name in; (start seconds, end seconds, what was said) out.
Transcribe = Callable[[bytes, str], Iterable[Tuple[float, float, str]]]

MAX_BYTES = 200 * 1024 * 1024


# ------------------------------------------------------------- the replies ---


def pages_reply(page_texts: Sequence[str], metadata: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The JSON reply for a paged document: the text, and where each page starts in it.

    ``pages`` is a list of ``[offset, page number]``: the character in
    ``text`` where that page begins. A page with nothing on it has no entry
    and still counts, so the fourth page is page 4 whether or not the third
    was blank. Offsets are counted in the text exactly as sent; change the
    text afterwards, a strip, a normalised line ending, and every later page
    is cited wrongly, which the library refuses when it can see it and cannot
    always see.
    """
    parts: List[str] = []
    pages: List[List[int]] = []
    offset = 0
    for number, text in enumerate(page_texts, start=1):
        cleaned = str(text or "").strip()
        if not cleaned:
            continue
        if parts:
            offset += 2  # the blank line between pages
        pages.append([offset, number])
        parts.append(cleaned)
        offset += len(cleaned)
    reply: Dict[str, Any] = {"text": "\n\n".join(parts), "pages": pages}
    reply["metadata"] = {"pages": len(page_texts), **dict(metadata or {})}
    return reply


def minutes_reply(
    segments: Iterable[Tuple[float, float, str]],
    seconds_per_page: float = 60.0,
    metadata: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The JSON reply for timed speech: everything said in one minute is one page.

    Minute ``n`` is page ``n + 1``. A silent minute has no text and still
    counts, so page twelve is always the twelfth minute. It is the same
    arithmetic as :func:`vectrixdb.extract.engines.segments_to_document`, so
    a recording reads the same whether the library transcribed it or this
    server did.
    """
    segments = list(segments)
    said: Dict[int, List[str]] = {}
    for start, _end, text in segments:
        cleaned = " ".join(str(text).split())
        if cleaned:
            said.setdefault(int(max(float(start), 0.0) // seconds_per_page), []).append(cleaned)
    parts: List[str] = []
    pages: List[List[int]] = []
    offset = 0
    for minute in sorted(said):
        if parts:
            offset += 2
        pages.append([offset, minute + 1])
        paragraph = " ".join(said[minute])
        parts.append(paragraph)
        offset += len(paragraph)
    about: Dict[str, Any] = {"transcript": True, "seconds_per_page": seconds_per_page}
    duration = max((float(end) for _, end, _ in segments), default=0.0)
    if duration:
        about["duration_seconds"] = round(duration, 2)
    about.update(dict(metadata or {}))
    return {"text": "\n\n".join(parts), "pages": pages, "metadata": about}


# ------------------------------------------------------------------ the key ---


def presented_key(headers: Mapping[str, str]) -> str:
    """The key a request carries: ``api-key``, ``x-api-key``, or ``Authorization: Bearer``."""
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    given = lowered.get("api-key") or lowered.get("x-api-key") or ""
    if not given:
        scheme, _, value = lowered.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer":
            given = value.strip()
    return given


def key_is_right(given: str, expected: str) -> bool:
    """Compared in constant time, so how long a refusal took says nothing about the key."""
    return bool(expected) and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


# ------------------------------------------------------------------ the app ---


def create_app(
    read_pdf: Optional[ReadPages] = None,
    read_image: Optional[ReadPages] = None,
    transcribe: Optional[Transcribe] = None,
    *,
    key: Optional[str] = None,
    allow_open: bool = False,
    max_bytes: int = MAX_BYTES,
) -> Any:
    """The server. A route exists only for an engine that was given.

    ``key`` defaults to ``EXTRACTION_API_KEY``. With no key it refuses to be
    made, unless ``allow_open`` (or ``EXTRACTION_ALLOW_OPEN=1``) says that
    something in front of it does the asking: a private network, or a gateway
    with a subscription key of its own.
    """
    if FastAPI is None:
        raise RuntimeError("the server needs FastAPI: pip install fastapi uvicorn")
    expected = key if key is not None else os.environ.get("EXTRACTION_API_KEY", "")
    open_to_all = allow_open or os.environ.get("EXTRACTION_ALLOW_OPEN", "") == "1"
    if not expected and not open_to_all:
        raise RuntimeError(
            "EXTRACTION_API_KEY is not set, so anybody who finds this port would read documents through your "
            "OCR account. Set it, or set EXTRACTION_ALLOW_OPEN=1 when a private network or a gateway does the asking."
        )

    app = FastAPI(title="Extraction server", docs_url=None, redoc_url=None)

    async def body_of(request: Request) -> Tuple[bytes, str]:
        if expected and not key_is_right(presented_key(request.headers), expected):
            raise HTTPException(status_code=401, detail="A key is needed: send it as api-key or as Authorization: Bearer.")
        said = request.headers.get("content-length")
        if said and said.isdigit() and int(said) > max_bytes:
            raise HTTPException(status_code=413, detail=f"The file is over {max_bytes} bytes.")
        data = await request.body()
        if len(data) > max_bytes:
            raise HTTPException(status_code=413, detail=f"The file is over {max_bytes} bytes.")
        if not data:
            raise HTTPException(status_code=400, detail="The request has no body. Send the file's bytes as the body.")
        # The name says what the file is, and nothing is ever opened by it.
        name = os.path.basename(request.headers.get("x-filename", "").replace("\\", "/")) or "file"
        return data, name

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {"status": "ok", "routes": sorted(r.path for r in app.routes if r.path.startswith(("/ocr", "/asr")))}

    if read_pdf is not None:

        @app.post("/ocr/pdf")
        async def ocr_pdf(request: Request) -> Dict[str, Any]:
            data, name = await body_of(request)
            return pages_reply(read_pdf(data, name), {"ocr": True})

    if read_image is not None:

        @app.post("/ocr/image")
        async def ocr_image(request: Request) -> Dict[str, Any]:
            data, name = await body_of(request)
            return pages_reply(read_image(data, name), {"ocr": True})

    if transcribe is not None:

        @app.post("/asr/audio")
        async def asr_audio(request: Request) -> Dict[str, Any]:
            data, name = await body_of(request)
            return minutes_reply(transcribe(data, name))

    return app


# -------------------------------------------------------------- the engines ---


def azure_engines() -> Tuple[ReadPages, ReadPages, Transcribe]:
    """Azure Document Intelligence for pages and Azure Speech for minutes, from the environment.

    ``AZURE_DOCINTEL_ENDPOINT`` and ``AZURE_DOCINTEL_KEY``, ``AZURE_SPEECH_KEY``
    and ``AZURE_SPEECH_REGION``. These are the keys this server sends out;
    ``EXTRACTION_API_KEY`` is the one it asks for, and they are never the same
    string. Not run by the suite.
    """

    def read_pages(data: bytes, name: str) -> List[str]:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.core.credentials import AzureKeyCredential

        client = DocumentIntelligenceClient(
            os.environ["AZURE_DOCINTEL_ENDPOINT"], AzureKeyCredential(os.environ["AZURE_DOCINTEL_KEY"])
        )
        result = client.begin_analyze_document("prebuilt-read", body=data, content_type="application/octet-stream").result()
        # One string a page, in the page's own order, and an empty one for a
        # page with nothing on it: the numbering is what a citation rests on.
        texts = {page.page_number: "\n".join(line.content for line in page.lines or []) for page in result.pages or []}
        return [texts.get(n, "") for n in range(1, max(texts, default=0) + 1)]

    def transcribe(data: bytes, name: str) -> List[Tuple[float, float, str]]:
        import tempfile
        import threading

        import azure.cognitiveservices.speech as speech

        config = speech.SpeechConfig(subscription=os.environ["AZURE_SPEECH_KEY"], region=os.environ["AZURE_SPEECH_REGION"])
        with tempfile.NamedTemporaryFile(suffix=os.path.splitext(name)[1] or ".wav", delete=False) as handle:
            handle.write(data)
            path = handle.name
        try:
            recognizer = speech.SpeechRecognizer(speech_config=config, audio_config=speech.audio.AudioConfig(filename=path))
            heard: List[Tuple[float, float, str]] = []
            finished = threading.Event()

            def recognised(event: Any) -> None:
                # Offsets and durations are in ticks of 100 nanoseconds.
                start = event.result.offset / 10_000_000
                heard.append((start, start + event.result.duration / 10_000_000, event.result.text))

            recognizer.recognized.connect(recognised)
            recognizer.session_stopped.connect(lambda _event: finished.set())
            recognizer.canceled.connect(lambda _event: finished.set())
            recognizer.start_continuous_recognition()
            finished.wait()
            recognizer.stop_continuous_recognition()
            return heard
        finally:
            os.unlink(path)

    return read_pages, read_pages, transcribe


def _app_from_environment() -> Any:
    read_pdf, read_image, transcribe = azure_engines()
    return create_app(read_pdf, read_image, transcribe)


def __getattr__(name: str) -> Any:
    # ``uvicorn server:app`` asks for ``app``; importing the file for its
    # helpers, as the tests do, never builds one and never needs a key.
    if name == "app":
        return _app_from_environment()
    raise AttributeError(name)
