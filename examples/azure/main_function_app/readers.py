"""Who reads which file type, and who describes a picture. No Azure import at the top.

Kept apart from ``function_app.py`` so it can be read, and tested, without the
Functions runtime. Every object here is built from environment variables and
handed to the collection; the library does the rest.

Deployed, every file is read by the extraction app of step 05. Its suffix names the
route, from the library's table, ``extraction_routes``, worked out from the
prefix and gateway paths the extraction app answers at:

    .pdf .docx .doc .pptx .xlsx .xlsm .csv .txt .md .html    /extract/<its kind>
    .png .jpg .jpeg .tif .tiff .bmp .webp                     /transcribe/image
    .wav .mp3 .m4a .flac .ogg .opus .aac                      /transcribe/audio
    .mp4 .mov .webm .mkv .avi                                 /transcribe/video

A long file goes in pieces, each read in one call inside the 230 seconds
Azure gives an HTTP request: sound in pieces of about ten minutes, cut at a
pause, a video's sound the same way, a PDF twenty pages at a time. The
answers are put back together with their times and page numbers moved on,
so a citation points where it did. ``EXTRACTION_BATCH_MINUTES`` and
``EXTRACTION_BATCH_PAGES`` change the sizes.

A file with any other suffix is left alone. Without an extraction app, the
services this app is given read the files here instead, as it once did:

    .png .jpg .tiff .bmp    Azure Document Intelligence
    .wav .mp3 .m4a          Azure Speech, cited by the minute
    .mp4 .mov .webm         ffmpeg for the sound, then Azure Speech
    .pdf .docx .pptx .md    the library's own readers

A PDF is not listed above because nothing here has to do anything about it.
The worker asks the collection whether it wants a document's pictures, and a
collection opened with ``describe_figures`` says yes, so the figures inside a
fetched PDF are there to be described. That used to need a reader of its own
in this file; it does not any more.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional


# ============================================================================
# SETTINGS: what is exported, and the settings a reader is built from
# ============================================================================
#
# What the module exports, and the environment variables a reader is built
# from.

__all__ = [
    "describer_from_environment",
    "extraction_app_from_environment",
    "extractors_from_environment",
    "page_reader_from_environment",
    "unread",
    "what_reads_what",
]


# ============================================================================
# READERS FROM THE ENVIRONMENT
# ============================================================================
#
# INPUT   the environment variables
# OUTPUT  the extraction app every file is read through, or None when there is
#         none; the suffix of a file it does not read; what describes a
#         figure, best first; what reads a page of a PDF with no text layer; a
#         suffix to reader mapping
#
# Every object here is built from environment variables and handed to the
# collection; the library does the rest. Deployed, every file goes to the
# extraction app of step 05, its suffix naming the route.


def extraction_app_from_environment(env: Optional[Dict[str, str]] = None) -> Optional[Any]:
    """The extraction app every file is read through, or None when there is none.

    ``VECTRIXDB_EXTRACTOR_URL`` is its address, and its key and the header the
    key goes in come with it. Which route reads which file is the library's
    table, worked out from ``EXTRACTION_PREFIX`` and ``EXTRACTION_GATEWAY_PATHS``:
    the prefix and the gateway paths the extraction app is given, so the two
    apps can never disagree about a path.
    """
    env = dict(os.environ if env is None else env)
    if not env.get("VECTRIXDB_EXTRACTOR_URL", "").strip():
        return None
    from vectrixdb.api.extraction import extraction_routes
    from vectrixdb.extract import HttpExtractor

    table = extraction_routes(
        env.get("EXTRACTION_PREFIX", ""), env.get("EXTRACTION_GATEWAY_PATHS", "")
    )
    return HttpExtractor.from_environment(env, routes=table)


def unread(uri: str, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The suffix of a file the extraction app does not read, or None when it reads it.

    Only the extraction app's table can say a file is unread. Without one, the
    library's own readers take whatever they know and say so themselves.
    """
    service = extraction_app_from_environment(env)
    if service is None:
        return None
    from pathlib import PurePosixPath
    from urllib.parse import unquote, urlparse

    name = PurePosixPath(unquote(urlparse(uri).path)).name
    if service.route_for(name) is not None:
        return None
    return PurePosixPath(name).suffix.lower() or "(no suffix)"


def describer_from_environment(env: Optional[Dict[str, str]] = None) -> Optional[Any]:
    """What describes a figure: the library's describers, best first.

    A chat model that can see when ``AZURE_OPENAI_VISION_DEPLOYMENT`` names
    one, then Azure AI Vision when the resource was made, asked in that
    order for each picture. The same chain the extraction app builds, so a
    file read here without it is described the same way.

    This used to be a class in this file, and it was the worse half of one.
    It flattened the service's answer into a string here, which threw away
    every bounding box, so a chart's title, legend and axis labels were
    written down in whatever order the service emitted them; it asked for
    dense captions and dropped them; and it never returned a caption or a
    table, which are two of the four things the library knows how to put in
    the Markdown. The library's own
    :class:`~vectrixdb.extract.engines.AzureImageAnalysis` reads the words
    through ``reading_order`` and hands back the parts rather than the
    sentence, which is the same treatment a scanned page gets.

    None is a working deployment without it: a figure then keeps its caption
    and its picture and gains no description.
    """
    from vectrixdb.extract.describers import describer_from_environment as best_first

    return best_first(env)


def page_reader_from_environment(env: Optional[Dict[str, str]] = None) -> Optional[Any]:
    """What reads a page of a PDF that has no text layer, or None.

    Document Intelligence again, the same resource and the same client that
    reads a standalone picture; there is nothing else to make and nothing
    else to pay for. It is handed one page, drawn as a PNG, and gives back
    its lines.

    The library only calls this for pages whose text layer is under a couple
    of dozen characters, and never for a page with no ink on it, so a report
    that is text throughout costs nothing at all and a report with two
    scanned inserts costs two pages. That selectivity is the whole reason
    this is a hook on the PDF reader rather than an extractor registered for
    ``.pdf``: an extractor would replace the reader, and take every figure in
    the document with it.
    """
    env = dict(os.environ if env is None else env)
    endpoint, key = (
        env.get("AZURE_DOCINTEL_ENDPOINT", "").strip(),
        env.get("AZURE_DOCINTEL_KEY", "").strip(),
    )
    if not (endpoint and key):
        return None
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.core.credentials import AzureKeyCredential

    from vectrixdb.extract.engines import AzureDocumentIntelligence

    reader = AzureDocumentIntelligence(
        DocumentIntelligenceClient(endpoint, AzureKeyCredential(key)), model="prebuilt-read"
    )

    def read(page: bytes) -> List[str]:
        # The service returns the page in reading order already, so the lines
        # go on as they came rather than being ordered a second time.
        return [line for line in reader(page, "page.png").text.splitlines() if line.strip()]

    return read


def extractors_from_environment(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """A suffix to reader mapping.

    With an extraction app, every suffix it reads goes to it, at the route the
    suffix names, and nothing is read here. Without one, from whichever
    services were made, and a suffix nobody claims falls through to the
    library's own readers, so a .docx and a .md are read the same whether
    these services exist or not.
    """
    service = extraction_app_from_environment(env)
    if service is not None:
        from vectrixdb.extract.batches import batched

        given = dict(os.environ if env is None else env)
        # A long file in pieces, each one call inside Azure's 230 seconds.
        reader = batched(
            service,
            minutes=float(given.get("EXTRACTION_BATCH_MINUTES") or 10),
            pages=int(given.get("EXTRACTION_BATCH_PAGES") or 20),
        )
        return {suffix: reader for suffix in service.suffixes()}
    env = dict(os.environ if env is None else env)
    from vectrixdb.extract.engines import (
        AUDIO_SUFFIXES,
        IMAGE_SUFFIXES,
        VIDEO_SUFFIXES,
        AzureSpeech,
        Video,
    )

    readers: Dict[str, Any] = {}

    docintel, docintel_key = (
        env.get("AZURE_DOCINTEL_ENDPOINT", "").strip(),
        env.get("AZURE_DOCINTEL_KEY", "").strip(),
    )
    if docintel and docintel_key:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.core.credentials import AzureKeyCredential

        from vectrixdb.extract.engines import AzureDocumentIntelligence

        # prebuilt-read, not prebuilt-layout: a photograph has no tables to
        # find, and read is a sixth of the price when the free pages run out.
        picture_reader = AzureDocumentIntelligence(
            DocumentIntelligenceClient(docintel, AzureKeyCredential(docintel_key)),
            model="prebuilt-read",
        )
        readers.update({suffix: picture_reader for suffix in IMAGE_SUFFIXES})

    speech, speech_key = (
        env.get("AZURE_SPEECH_ENDPOINT", "").strip(),
        env.get("AZURE_SPEECH_KEY", "").strip(),
    )
    if speech and speech_key:
        listener = AzureSpeech(speech.rstrip("/"), speech_key)
        readers.update({suffix: listener for suffix in AUDIO_SUFFIXES})
        # The sound of a video, through ffmpeg. imageio-ffmpeg ships the
        # binary in the deployment, so no container and no apt-get.
        watcher = Video(audio=listener)
        readers.update({suffix: watcher for suffix in VIDEO_SUFFIXES})

    return readers


# ============================================================================
# WHAT READS WHAT
# ============================================================================
#
# INPUT   the readers
# OUTPUT  one line a reader, for the log, so the first cold start says what it
#         can read
#
# Said once, at start.


def what_reads_what(readers: Dict[str, Any]) -> str:
    """One line a reader, for the log, so the first cold start says what it can read."""
    by_reader: Dict[str, List[str]] = {}
    for suffix, reader in sorted(readers.items()):
        by_reader.setdefault(getattr(reader, "label", type(reader).__name__), []).append(suffix)
    return "; ".join(
        f"{label}: {' '.join(suffixes)}" for label, suffixes in sorted(by_reader.items())
    )
