"""The extraction service as a Function App: the library's utility API, hosted, plus the queue trigger that finishes a long job.

Read it top to bottom. First SETTINGS, every variable this app reads and what
it is for. Then the six parts, each under its own heading, and each with
what goes in, what comes out, and its code:

    THE DOOR        how_callers_get_in    the library's key, never a function key
    HOLDING         settings_hold         every setting checked at start, and one message naming all that are wrong
    READING         readers               the services 03 made, and what the app does without each
    MASKING         masking               identifiers taken out, on request, before the caller keeps a word
    WIRING          wiring                what this deployment was given, as /health/wiring says it, with no secret in it
    JOBS            extract_job           an hour for a long job: the queue a saved video waits on

Nothing here is a step. A file goes in one call and its text comes out, and
that is the whole of it: the ten steps are the main app's, which calls this
one at step two and asks it to mask as it reads. Last comes the MAIN SCRIPT,
which is what the Functions host runs: the settings held to, the app the
library builds from them with our one route in front, and the queue trigger.
Every route it serves but one is the library's, and each is described in
docs/how-to/extraction-service.md.

WHAT IS PUBLISHED
-----------------

    GET  https://<your app>.azurewebsites.net/health                 is it up, and which masking engine loaded
    GET  https://<your app>.azurewebsites.net/health/wiring          ours: what this deployment was given, with no secret in it
    GET  https://<your app>.azurewebsites.net/docs                   the routes, as a page
    POST https://<your app>.azurewebsites.net/extract/<kind>         pdf, docx, doc, pptx, xlsx, csv, txt, md, html: the file as the body
    POST https://<your app>.azurewebsites.net/transcribe/image       a picture: what it shows and the words in it
    POST https://<your app>.azurewebsites.net/transcribe/audio       a recording, ?language=en-US: a timed transcript
    POST https://<your app>.azurewebsites.net/transcribe/video       a video: the same, from its sound
    POST https://<your app>.azurewebsites.net/transcribe/webpage     {"url": ...}: the page's text
    POST https://<your app>.azurewebsites.net/transcribe/<kind>_url  image_url, audio_url, video_url: as for the file, fetched
    POST https://<your app>.azurewebsites.net/transcribe/youtube     {"url": ...}: the video's timed transcript
    POST https://<your app>.azurewebsites.net/transcribe/youtube_save  202 and a job; GET /jobs/{job} says how it is doing
    POST https://<your app>.azurewebsites.net/transcribe/auto        an address of any kind, read as whatever it turns out to be
    POST https://<your app>.azurewebsites.net/mask                   {"text": ...}: the text with its identifiers taken out
    POST https://<your app>.azurewebsites.net/translate/text         {"text": ..., "to": "fr"}
    POST https://<your app>.azurewebsites.net/translate/detect       {"text": ...}: the language of each
    GET  https://<your app>.azurewebsites.net/translate/languages    every language, by code
    GET  https://<your app>.azurewebsites.net/jobs/{job}             a long job: queued, running, done, or why not

Try ``/health`` first. It takes no key and no arguments, and the useful thing
about it is not the 200: a reply of any kind proves the Python worker indexed
this file. A 404 there means the app loaded no functions at all, which is
what one function failing to load looks like, and the live log then holds
one message from HOLDING below that lists every setting that is wrong.
Point an availability test at it. ``/health/wiring`` is the second thing to
try: it says what the app was given, by service, without opening anything.
Every route sits under ``VECTRIXDB_EXTRACT_PREFIX`` when one is set, and
answers at the gateway paths ``VECTRIXDB_EXTRACT_GATEWAY_PATHS`` names as
well, so the main app and a gateway work out the same path for every file.

HOW A CALLER GETS IN
--------------------

Not with a Functions key. ``auth_level`` is ANONYMOUS on purpose, because a
function key is one shared secret with no scope and no expiry, and the
library has scoped keys that expire, tokens, an identity provider and a rate
limit already. Every call presents the library's key in ``api-key`` or as a
Bearer token; with sign-in shared with a VectrixDB server, its named keys
and its identity provider's tokens are honoured here too. Health, the
wiring and the documentation need nothing. The app refuses to start without
a key, because every call spends money on a paid service.

WHERE THE DEPLOYED CODE IS
--------------------------

The same place the main app's is, and the same rule: Flex Consumption runs
the app from a package, so there is no editor in the portal and no console
behind ``<app>.scm.azurewebsites.net``. What you edit is this folder, and
``05_create_extraction_function_app.py --code-only`` publishes it again.
"""

import json
import logging
import os
import platform
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

import azure.functions as func
from fastapi import FastAPI

from vectrixdb.api import create_extraction_app, run_extraction_job
from vectrixdb.api.extraction import ExtractionService, read_gateway_paths, route_prefix
from vectrixdb.exceptions import ConfigurationError
from vectrixdb.masking import ENGINES, describe_engine

log = logging.getLogger("vectrixdb.extraction_app")


# ============================================================================
# SETTINGS: every variable this app reads, and what it is for
# ============================================================================
#
# 05_create_extraction_function_app.py sets them, from settings.env and from
# what 01 and 03 made. The keys among them are read from Azure by 05 and sent
# straight to the app; none is written into this code or into a file. Nothing
# of the main app's is here: this one stands alone. Every one of them is
# checked by settings_hold() before the app is built, and a wrong one is
# named there, in one message with every other wrong one, rather than
# raising on its own somewhere in the library.
#
# The Functions host
#   FUNCTIONS_WORKER_RUNTIME         python
#   AzureWebJobsStorage              the host's own storage, where a long job's queue and its results live
#
# The door
#   VECTRIXDB_API_KEY                the key every caller presents, made on 05's first run and kept in the app's settings;
#                                    the app will not start without one, because every call spends money on a paid service
#   VECTRIXDB_SIGNIN, _STORE         sign-in shared with a VectrixDB server, so its keys and its identity provider's
#                                    tokens are honoured here too; left out, the key alone
#   VECTRIXDB_ALLOW_OPEN             a laptop only: no key and no sign-in. Never set here
#
# Where it answers
#   VECTRIXDB_EXTRACT_PREFIX         the path every route lives under, as settings.env gave it; empty for the root
#   VECTRIXDB_EXTRACT_GATEWAY_PATHS  the paths a gateway publishes each route under, as its team handed them over
#   VECTRIXDB_ROOT_PATH              one path a proxy puts in front of everything
#   VECTRIXDB_TRUSTED_PROXIES        the proxies whose forwarded headers are believed
#   VECTRIXDB_PUBLIC_URL             the address callers use, which decides whether cookies are marked secure
#   VECTRIXDB_FRAME_ANCESTORS        who may put the documentation page in a frame
#
# Reading (the services 03 made; what the app does without each is in READERS below, in the log at start, and at /health/wiring)
#   AZURE_DOCINTEL_ENDPOINT, _KEY    Document Intelligence: scanned pages, and the layout model when asked
#   AZURE_SPEECH_ENDPOINT, _KEY      Speech: recordings, videos and YouTube
#   AZURE_VISION_ENDPOINT, _KEY      Vision: what a picture shows, and the words in it
#   AZURE_TRANSLATOR_ENDPOINT, _KEY, _REGION
#                                    Translator: the /translate routes. The key is refused without the region
#   VECTRIXDB_EXTRACT_PDF            text, a PDF's own text layer with OCR for scanned pages only; layout,
#                                    Document Intelligence's layout model, for tables as rows and the numbers printed on pages;
#                                    or vision, the unsure pages read by the chat model that can see
#   VECTRIXDB_EXTRACT_URL_HOSTS      the only hosts an address route may fetch from; *.example.com for subdomains
#   VECTRIXDB_MAX_UPLOAD_BYTES       the largest file a call may send; left out, 100 MB
#   VECTRIXDB_VIDEO_FRAMES           frames of a video read for what is on screen; left out, 12
#   VECTRIXDB_SPEECH_SPEAKERS        voices Speech tells apart; left out, 4
#
# Pictures in detail (VX_DETAILED_PICTURES=yes in settings.env)
#   AZURE_OPENAI_ENDPOINT            the Azure OpenAI resource whose chat model describes each picture in front of Vision
#   AZURE_OPENAI_VISION_DEPLOYMENT   that model's deployment
#   AZURE_OPENAI_KEY                 its key; left out, the app's managed identity
#   AZURE_OPENAI_API_VERSION         the API version it is called with
#   VECTRIXDB_DESCRIBER_URL, _MODEL, _KEY, _KEY_HEADER
#                                    any other chat model that can see, instead of Azure OpenAI
#
# Masking
#   VECTRIXDB_MASKING_ENGINE         how identifiers in a document are found: auto, regex, presidio, language or comprehend.
#                                    auto picks Azure AI Language when its endpoint is set, else Presidio when installed,
#                                    else the patterns. The patterns run after every engine
#   VECTRIXDB_MASKING_LANGUAGES      the languages the documents are in: en,fr
#   AZURE_LANGUAGE_ENDPOINT, _KEY    Azure AI Language, which reads names and addresses in English and French alike;
#                                    left empty, the key is the app's managed identity
#
# Jobs
#   VECTRIXDB_EXTRACT_JOBS           azure: a long job is a record in a container and a message on a queue;
#                                    memory, or left out: a job lives in this process, which a function app forgets
#   VECTRIXDB_EXTRACT_STORAGE        where those live; left out, AzureWebJobsStorage
#   VECTRIXDB_EXTRACT_CONTAINER      the container: extraction
#   VECTRIXDB_EXTRACT_QUEUE          the queue: extract-jobs, which the trigger at the bottom of this file reads
#
# and every other VECTRIXDB_ setting the library's server reads: docs/reference/settings.md

#: Each reader, by the service that does it: its name as /health/wiring says
#: it, the settings that have to be there for it, as groups of which any one
#: is enough, and what the app does without it. The describer is a chat
#: model that can see, named either as an Azure OpenAI deployment or as any
#: other model's address.
READERS: Tuple[Tuple[str, Tuple[Tuple[str, ...], ...], str], ...] = (
    (
        "document_intelligence",
        (("AZURE_DOCINTEL_ENDPOINT", "AZURE_DOCINTEL_KEY"),),
        "pictures answer 503, and a scanned PDF page comes back without its text",
    ),
    (
        "speech",
        (("AZURE_SPEECH_ENDPOINT", "AZURE_SPEECH_KEY"),),
        "recordings, videos and YouTube answer 503",
    ),
    (
        "vision",
        (("AZURE_VISION_ENDPOINT", "AZURE_VISION_KEY"),),
        "a picture is read for its words and not described",
    ),
    (
        "translator",
        (("AZURE_TRANSLATOR_KEY", "AZURE_TRANSLATOR_REGION"),),
        "the /translate routes answer 503",
    ),
    (
        "describer",
        (("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_VISION_DEPLOYMENT"), ("VECTRIXDB_DESCRIBER_URL",)),
        "a picture is described by Vision alone, and a PDF's pictures are not opened at all",
    ),
)

#: The pairs where one half without the other looks configured and is not.
PAIRED = (
    ("AZURE_DOCINTEL_ENDPOINT", "AZURE_DOCINTEL_KEY", "Document Intelligence"),
    ("AZURE_SPEECH_ENDPOINT", "AZURE_SPEECH_KEY", "Speech"),
    ("AZURE_VISION_ENDPOINT", "AZURE_VISION_KEY", "Vision"),
)

#: What VECTRIXDB_EXTRACT_PDF may say.
PDF_WAYS = ("text", "layout", "vision")

#: A host as VECTRIXDB_EXTRACT_URL_HOSTS may name one: a name, or *. in front of one for its subdomains.
HOST = re.compile(
    r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$|^localhost$"
)


# ============================================================================
# THE DOOR
# ============================================================================
#
# INPUT   a call, with the key in api-key or as a Bearer token; or a session or
#         an identity provider's token, when sign-in is shared with a server
# OUTPUT  the call goes through, or 401 with one refusal shape; health, the
#         wiring and the documentation need nothing
#
# The Functions auth level is anonymous on purpose. A function key is one
# shared secret with no scope and no expiry, and the library has scoped keys
# that expire, tokens, an identity provider and a rate limit already: Azure is
# the building, and the library's key layer is the door.


def how_callers_get_in(env: Optional[Mapping[str, str]] = None) -> str:
    """One line for the log at start, so the first cold start says who this app answers.

    INPUT
    -----
    The door's settings: ``VECTRIXDB_SIGNIN`` and ``VECTRIXDB_ALLOW_OPEN``,
    read as set or not set, never for their values.

    OUTPUT
    ------
    One of three lines, the first two of which are what a deployment should
    say and the last of which is a laptop's::

        the library's key, and the sign-in it shares with the server
        the library's key, in api-key or as a Bearer token
        nobody asked: open, which is for a laptop and nowhere else

    It decides nothing: the library's key layer does that on every call. It
    is there so the log's first line, before any file, says which door this
    app has, and a deployment left open by mistake says so where somebody
    reads.
    """
    found = os.environ if env is None else env
    if str(found.get("VECTRIXDB_SIGNIN", "")).strip():
        return "the library's key, and the sign-in it shares with the server"
    if str(found.get("VECTRIXDB_ALLOW_OPEN", "")).strip():
        return "nobody asked: open, which is for a laptop and nowhere else"
    return "the library's key, in api-key or as a Bearer token"


# ============================================================================
# HOLDING: every setting checked at start
# ============================================================================
#
# INPUT   the settings above, as the Functions host gave them
# OUTPUT  nothing when they hold; else one ConfigurationError that names
#         every setting that is wrong or missing, and the app does not start
#
# Each of these would be caught by the library too, one at a time, as the
# first wrong one raised from wherever it is read: the jobs backend from
# inside from_environment, the masking engine from inside the app builder, a
# bad host only on the first address fetched. A function app that fails to
# load answers 404 to everything and shows one traceback, so one setting is
# fixed, the app is published again, and the next one shows. Here they are
# all read first, by name, and said together.


def given(found: Mapping[str, str], *names: str) -> bool:
    """Set to something, every one of them, which is all this file ever says about a secret."""
    return all(str(found.get(name, "")).strip() for name in names)


def settings_hold(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """Every setting that is wrong or missing, each as one line that says what to change; empty when they all hold.

    INPUT
    -----
    The settings, ``os.environ`` unless a mapping is given. Read by name and
    by shape; no service is called and nothing is opened.

    OUTPUT
    ------
    Lines like these, in the order the settings are listed at the top of
    this file, so a reader finds each where it is explained::

        VECTRIXDB_API_KEY is not set, and neither is VECTRIXDB_SIGNIN: ...
        VECTRIXDB_EXTRACT_JOBS=azure needs VECTRIXDB_EXTRACT_STORAGE or AzureWebJobsStorage, and neither is set
        VECTRIXDB_EXTRACT_URL_HOSTS has 'https://www.td.com', which is not a host: write www.td.com, ...
        VECTRIXDB_MASKING_ENGINE is 'nlp', which is none of auto, regex, presidio, language, comprehend

    A key is never quoted here, only named.
    """
    found: Mapping[str, str] = os.environ if env is None else env
    wrong: List[str] = []

    # The door.
    if not (
        given(found, "VECTRIXDB_API_KEY")
        or given(found, "VECTRIXDB_API_KEY_SHA256")
        or given(found, "VECTRIXDB_SIGNIN")
    ):
        if str(found.get("VECTRIXDB_ALLOW_OPEN", "")).strip().lower() in ("1", "true", "yes", "on"):
            log.warning(
                "VECTRIXDB_ALLOW_OPEN is set: the extraction app is open, which is for a laptop and nowhere else"
            )
        else:
            wrong.append(
                "VECTRIXDB_API_KEY is not set, and neither is VECTRIXDB_SIGNIN: every call spends money, so the app will "
                "not start open. Run 05_create_extraction_function_app.py --settings-only, which makes and keeps a key"
            )

    # Reading: a half-configured service looks configured and is not.
    for endpoint, key, service in PAIRED:
        if given(found, endpoint) != given(found, key):
            there, missing = (endpoint, key) if given(found, endpoint) else (key, endpoint)
            wrong.append(
                f"{there} is set and {missing} is not: {service} needs both, and with one it is not there at all"
            )
    if given(found, "AZURE_TRANSLATOR_KEY") and not given(found, "AZURE_TRANSLATOR_REGION"):
        wrong.append(
            "AZURE_TRANSLATOR_KEY is set and AZURE_TRANSLATOR_REGION is not: Translator refuses a key without the region it was made in"
        )
    pdf = str(found.get("VECTRIXDB_EXTRACT_PDF", "")).strip().lower()
    if pdf and pdf not in PDF_WAYS:
        wrong.append(f"VECTRIXDB_EXTRACT_PDF is {pdf!r}, which is none of {', '.join(PDF_WAYS)}")
    elif pdf == "layout" and not given(found, "AZURE_DOCINTEL_ENDPOINT", "AZURE_DOCINTEL_KEY"):
        wrong.append(
            "VECTRIXDB_EXTRACT_PDF=layout needs AZURE_DOCINTEL_ENDPOINT and AZURE_DOCINTEL_KEY, the layout model's"
        )
    elif pdf == "vision" and not (
        given(found, "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_VISION_DEPLOYMENT")
        or given(found, "VECTRIXDB_DESCRIBER_URL")
    ):
        wrong.append(
            "VECTRIXDB_EXTRACT_PDF=vision needs a chat model that can see: AZURE_OPENAI_VISION_DEPLOYMENT, or VECTRIXDB_DESCRIBER_URL"
        )
    for host in (
        part.strip() for part in str(found.get("VECTRIXDB_EXTRACT_URL_HOSTS", "")).split(",")
    ):
        if host and not HOST.match(host.lower().rstrip(".")):
            wrong.append(
                f"VECTRIXDB_EXTRACT_URL_HOSTS has {host!r}, which is not a host: write the name alone, www.td.com, "
                "or *.td.com for its subdomains, with no scheme and no path"
            )
    for name, least in (
        ("VECTRIXDB_MAX_UPLOAD_BYTES", 1),
        ("VECTRIXDB_VIDEO_FRAMES", 0),
        ("VECTRIXDB_SPEECH_SPEAKERS", 0),
    ):
        value = str(found.get(name, "")).strip()
        if value and not (value.isdigit() and int(value) >= least):
            wrong.append(
                f"{name} is {value!r}, and it is a whole number{' above zero' if least else ''}, in bytes"
                if "BYTES" in name
                else f"{name} is {value!r}, and it is a whole number"
            )

    # Masking.
    engine = str(found.get("VECTRIXDB_MASKING_ENGINE", "") or "auto").strip().lower()
    if engine not in ENGINES:
        wrong.append(
            f"VECTRIXDB_MASKING_ENGINE is {engine!r}, which is none of {', '.join(ENGINES)}"
        )
    elif engine == "language" and not given(found, "AZURE_LANGUAGE_ENDPOINT"):
        wrong.append(
            "VECTRIXDB_MASKING_ENGINE=language needs AZURE_LANGUAGE_ENDPOINT, the Language resource's address"
        )

    # Jobs.
    jobs = str(found.get("VECTRIXDB_EXTRACT_JOBS", "") or "memory").strip().lower()
    if jobs not in ("memory", "azure"):
        wrong.append(f"VECTRIXDB_EXTRACT_JOBS is {jobs!r}, and it is azure or memory")
    elif jobs == "azure" and not (
        given(found, "VECTRIXDB_EXTRACT_STORAGE") or given(found, "AzureWebJobsStorage")
    ):
        wrong.append(
            "VECTRIXDB_EXTRACT_JOBS=azure needs VECTRIXDB_EXTRACT_STORAGE or AzureWebJobsStorage, and neither is set"
        )
    return wrong


def hold(env: Optional[Mapping[str, str]] = None) -> None:
    """Stop the app before it is built when a setting is wrong, with one message that names every one.

    Raised rather than logged and carried on, because a function app with a
    wrong setting is one that answers 503 to the routes that setting serves
    and looks healthy on the rest; and said once, in full, because each
    publish takes minutes and a list fixed in one go is fixed in one publish.
    The message is in the live log, under the function app in the portal.
    """
    wrong = settings_hold(env)
    if not wrong:
        return
    message = (
        f"The extraction app's settings will not do, so it has not started. {len(wrong)} "
        f"{'thing' if len(wrong) == 1 else 'things'} to change, then publish again with 05_create_extraction_function_app.py --settings-only:\n"
        + "\n".join(f"  - {line}" for line in wrong)
    )
    log.error(message)
    raise ConfigurationError(message)


# ============================================================================
# READING
# ============================================================================
#
# INPUT   a file as the body of the one route that reads its kind, with its
#         name in X-Filename; or an address, or a YouTube video
# OUTPUT  its text as Markdown, or as JSON with page breaks, headings, what
#         each picture shows, and every phrase of a recording with its time
#
# Every reader is the library's, built from the settings above. A service that
# is not there takes its routes with it: without Speech a recording answers
# 503 and says so, rather than a route that pretends.


def readers() -> ExtractionService:
    """Every service whose settings are there, as the library assembles them.

    INPUT
    -----
    The reading settings above: an endpoint and a key for each of Document
    Intelligence, Speech, Vision and Translator, and the describer's when
    pictures are read in detail. Each is optional.

    OUTPUT
    ------
    The library's :class:`ExtractionService`, with a reader on each service
    it was given and ``None`` where it was not::

        service.image        Vision, or None:      /transcribe/image answers 503 and says so
        service.audio        Speech, or None:      /transcribe/audio, /video and /youtube answer 503
        service.describer    a chat model, or None: pictures are described by Vision alone
        service.translator   Translator, or None:  the /translate routes answer 503

    Documents need none of them: a PDF's own text layer is read here, and
    only a scanned page goes to Document Intelligence. Built once, at start,
    because a setting that is wrong should stop the app at start rather than
    at the first file; :func:`what_reads_what` puts the result in the log.
    """
    return ExtractionService.from_environment()


def what_each_reads(env: Optional[Mapping[str, str]] = None) -> List[Tuple[str, bool, str]]:
    """Each reader by service: its name, whether its settings are there, and what the app does without it.

    INPUT
    -----
    The reading settings, read as set or not set, as :data:`READERS` lists
    them. A key is only ever asked whether it is there.

    OUTPUT
    ------
    One triple a service, in the order of :data:`READERS`::

        ("speech", True, "recordings, videos and YouTube answer 503")
        ("translator", False, "the /translate routes answer 503")

    The same table is one line each in the log at start, and the ``readers``
    part of ``/health/wiring``. Compare it with what 05 said it sent: a
    service absent here is a setting that did not arrive.
    """
    found = os.environ if env is None else env
    return [
        (name, any(given(found, *group) for group in needs), without)
        for name, needs, without in READERS
    ]


def what_reads_what(service: ExtractionService) -> str:
    """One line for the log: which services this app was given, and so what it can read.

    INPUT
    -----
    The service :func:`readers` built.

    OUTPUT
    ------
    The kinds it reads, in the order a person would list them, with the
    ones it was not given left out rather than marked::

        documents, pictures, recordings and videos, pictures in detail, translation
        documents

    The second line is an app given nothing but the library, which still
    reads every document kind. Compare it with what 05 said it sent: a kind
    missing here is a setting that did not arrive.
    """
    given_ = [
        "documents",
        "pictures" if getattr(service, "image", None) is not None else "",
        "recordings and videos" if getattr(service, "audio", None) is not None else "",
        "pictures in detail" if getattr(service, "describer", None) is not None else "",
        "translation" if getattr(service, "translator", None) is not None else "",
    ]
    return ", ".join(part for part in given_ if part)


# ============================================================================
# MASKING
# ============================================================================
#
# INPUT   ?mask=1 on any reading route, with types and language if you like;
#         or POST /mask with a text of its own
# OUTPUT  the same text with its identifiers taken out, what was found with
#         its offsets, a risk score and which engine ran; on a reading route the
#         JSON carries a masking summary beside the text, the Markdown an
#         X-Masking header
#
# The main app asks for it on every file it reads, so the Markdown it keeps and
# the index it builds never held an email, a card or a social insurance number.
# The engine is chosen once, by the library as it builds the app, from the
# settings: a bad setting is named by HOLDING above before that. Whatever
# engine ran, the patterns run after it, so a shape the model missed is still
# caught by its form; and an engine asked about a language it does not cover
# says so rather than answering as if it had.


def masking(library: Any) -> str:
    """One line for the log: the engine that loaded, and which of the documents' languages it covers.

    INPUT
    -----
    The library's app, which chose the engine once as it was built, from
    ``VECTRIXDB_MASKING_ENGINE`` and ``VECTRIXDB_MASKING_LANGUAGES``, and
    keeps it on its state. Read from there, so the line says what runs
    rather than building a second engine to describe.

    OUTPUT
    ------
    The engine and a yes or no for each language the documents are in::

        language, en yes, fr yes; the patterns run after it
        presidio, en yes, fr patterns only; the patterns run after it
        regex, en patterns only, fr patterns only; the patterns run after it

    "patterns only" is honest, not a failure: the engine does not read that
    language, so its names and addresses go unfound, and what has a shape,
    an email, a card, a social insurance number, is still caught by its form.
    """
    said = masking_of(library)
    covered = ", ".join(
        f"{lang} {'yes' if ok else 'patterns only'}" for lang, ok in said["languages"].items()
    )
    return f"{said['engine']}, {covered}; the patterns run after it"


def masking_of(library: Any) -> Dict[str, Any]:
    """The engine as the library's health reply says it: its name and which language it covers, and that the patterns run last."""
    said = describe_engine(library.state.masking, library.state.masking_languages)
    return {
        "engine": said["engine"],
        "languages": said["languages"],
        "patterns_last": said["patterns_last"],
    }


# ============================================================================
# WIRING: what this deployment was given
# ============================================================================
#
# INPUT   the settings above, and the service and the app built from them
# OUTPUT  one JSON reply, GET /health/wiring, with no secret in it
#
# The main app has the same route, and for the same reason: /health says the
# app is up, this says what it was given, and neither opens anything, so a
# service that is down is never reported here as an app that is down. It is
# ours, not the library's, and sits in front of the library's app so the
# library's door never sees it: a wiring check that needs the key is no use
# to the person who is trying to find out why the key is refused.


def wiring(
    service: ExtractionService, library: Any, env: Optional[Mapping[str, str]] = None
) -> Dict[str, Any]:
    """What this deployment was given, as a dict a health check can read, with no secret in it.

    INPUT
    -----
    The service :func:`readers` built, the library's app, and the settings.
    Every key is asked only whether it is there, through :func:`given`, and
    no endpoint is repeated back: the reply says which services the app
    has, never where or with what.

    OUTPUT
    ------
    ::

        {"status": "ok", "role": "extraction", "vectrixdb": "2.2.0", "python": "3.11.9",
         "door": "the library's key, in api-key or as a Bearer token",
         "readers": {"document_intelligence": {"given": true, "without": "pictures answer 503, ..."}, ...},
         "reads": "documents, pictures, recordings and videos",
         "masking": {"engine": "regex", "languages": {"en": false, "fr": false}, "patterns_last": true},
         "jobs": {"backend": "azure", "storage": true, "container": "extraction", "queue": "extract-jobs"},
         "url_hosts": ["www.td.com"], "max_upload_bytes": 104857600, "pdf": "text",
         "paths": {"prefix": "", "gateway_paths": {}}}

    ``jobs.storage`` is whether a connection is set, never the connection.
    """
    found = os.environ if env is None else env
    try:
        from vectrixdb import __version__ as version
    except ImportError:  # pragma: no cover - a wheel that installed has one
        version = "unknown"
    jobs = str(found.get("VECTRIXDB_EXTRACT_JOBS", "") or "memory").strip().lower()
    return {
        "status": "ok",
        "role": "extraction",
        "vectrixdb": version,
        "python": platform.python_version(),
        "door": how_callers_get_in(found),
        "readers": {
            name: {"given": there, "without": without}
            for name, there, without in what_each_reads(found)
        },
        "reads": what_reads_what(service),
        "masking": masking_of(library),
        "jobs": {
            "backend": jobs,
            "storage": given(found, "VECTRIXDB_EXTRACT_STORAGE")
            or given(found, "AzureWebJobsStorage"),
            "container": str(found.get("VECTRIXDB_EXTRACT_CONTAINER", "") or "extraction"),
            "queue": str(found.get("VECTRIXDB_EXTRACT_QUEUE", "") or "extract-jobs"),
        },
        "url_hosts": list(getattr(service, "url_hosts", ()) or ()),
        "max_upload_bytes": int(getattr(service, "max_bytes", 0) or 0),
        "pdf": str(found.get("VECTRIXDB_EXTRACT_PDF", "")).strip().lower() or "text",
        "paths": {
            "prefix": route_prefix(str(found.get("VECTRIXDB_EXTRACT_PREFIX", ""))),
            "gateway_paths": dict(
                read_gateway_paths(str(found.get("VECTRIXDB_EXTRACT_GATEWAY_PATHS", "")))
            ),
        },
    }


def face_of(service: ExtractionService, library: Any) -> FastAPI:
    """The app the host runs: our one route in front, and the library's app behind it for everything else.

    Mounted, not added to the library's app, because the library's door is
    told which paths are public when the app is built and ``/health/wiring``
    is not among them; a route added behind that door would ask for a key.
    Everything the route in front does not match falls through to the
    library's app, whose door, routes and refusals are untouched.
    """
    prefix = route_prefix(os.environ.get("VECTRIXDB_EXTRACT_PREFIX", ""))
    face = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @face.get(f"{prefix}/health/wiring", tags=["about"], summary="What this deployment is wired to")
    async def health_wiring() -> Dict[str, Any]:
        return wiring(service, library)

    face.mount("/", library)
    return face


# ============================================================================
# JOBS
# ============================================================================
#
# INPUT   a message on extract-jobs, which the library wrote when a caller
#         asked for a saved YouTube transcript and was answered 202 at once
# OUTPUT  the transcript in the extraction container, and the job record
#         saying done, which GET /jobs/{job} reads
#
# Azure ends every HTTP request at 230 seconds, and a long video takes more,
# so the work waits on a queue and runs where there is an hour for it: the
# queue trigger at the bottom of this file, the one function this file
# declares besides the app itself.


def job_named(body: Any) -> str:
    """The job a queue message names, ``{"job": "j_..."}`` as the library writes it; "" for a message that is anything else."""
    text = (
        body.decode("utf-8", errors="replace")
        if isinstance(body, (bytes, bytearray))
        else str(body or "")
    )
    try:
        found = json.loads(text)
    except ValueError:
        return ""
    job = found.get("job") if isinstance(found, dict) else None
    return str(job).strip() if isinstance(job, str) else ""


def run_job(body: Any, message_id: str = "") -> bool:
    """One message, run or dropped: True when a job was run, False when the message named none.

    A message that is not ``{"job": ...}`` is logged by its length and its
    id, never its content, and dropped. Not raised: raising sends it round
    five times, five cold starts paid for, and then into
    ``extract-jobs-poison``, which nobody reads and which the library did
    not write. It can never become a job by going round again.
    """
    job = job_named(body)
    if not job:
        size = len(body) if isinstance(body, (bytes, bytearray)) else len(str(body or ""))
        log.warning(
            'extract-jobs: message %s is not {"job": ...}, %d bytes, dropped: nothing on the queue but a job id is this app\'s',
            message_id or "?",
            size,
        )
        return False
    run_extraction_job(body)
    return True


# ============================================================================
# MAIN SCRIPT
# ============================================================================

hold()
service = readers()
for name, there, without in what_each_reads():
    log.info("reader %s: %s", name, "present" if there else f"absent, so {without}")
log.info(
    "the extraction app reads %s; callers get in with %s",
    what_reads_what(service),
    how_callers_get_in(),
)

#: The library's app, built from the settings held to above: every route but
#: ours, and the door in front of them.
library = create_extraction_app(service)
log.info("masking by %s", masking(library))

#: The app the Functions host runs: our wiring route in front, the library's
#: app behind it. Azure is the building and the library's key layer is the
#: door, so the Functions auth level is deliberately open: see THE DOOR above.
app = func.AsgiFunctionApp(app=face_of(service, library), http_auth_level=func.AuthLevel.ANONYMOUS)


@app.queue_trigger(arg_name="message", queue_name="extract-jobs", connection="AzureWebJobsStorage")
def extract_job(message: func.QueueMessage) -> None:
    """A long job, such as a saved YouTube transcript, run where there is an hour for it.

    INPUT
    -----
    One message on ``extract-jobs``, which the library wrote when a caller
    asked ``POST /transcribe/youtube_save`` and was answered 202 with the
    job's id at once, as JSON::

        {"job": "j_3f9a0c2e7b1d4e65"}

    The job's record, in the extraction container, is what the library keeps
    of the request: the job's id, its kind, its ``params`` as the caller sent
    them, ``url``, ``output_dir`` and ``auto_delete``, its status, and when
    it was created and last updated.

    OUTPUT
    ------
    The transcript written in the folder ``output_dir`` names, and the
    record updated: ``running`` while it works, then ``done`` with its
    ``result``, or ``failed`` with the ``error`` in words. ``GET /jobs/{job}``
    reads the record, so a caller polls that and never this.

    **It has no URL and cannot be called over HTTP**: looking for one is the
    usual first confusion. To make it run, ask for a saved transcript. Azure
    ends every HTTP request at 230 seconds, and a long video takes more, so
    the request only writes the message and the work happens here, with the
    function's whole hour for one job. Work that raises is recorded as
    ``failed`` with why, and the message is done with: it does not go round
    again, because the record is the answer.

    A message that is not ``{"job": ...}`` at all is done with too, by
    :func:`run_job`: one warning with its id and its length, never its
    content, and the message is gone. The other choice was to let it raise
    and go round five times into ``extract-jobs-poison``, and that was
    rejected on purpose: nothing but the library writes this queue, a
    message it did not write can never become a job by being tried again,
    each try is a cold start paid for, and a poison queue is a place nobody
    looks. There is nothing to record against a job either, because the
    message names none. The warning in the live log is the whole of it.

    The library's :func:`run_extraction_job` does the work, with the same
    :func:`readers` the routes use, so a transcript saved by a job is the one
    the route would have returned.
    """
    run_job(message.get_body(), str(getattr(message, "id", "") or ""))
