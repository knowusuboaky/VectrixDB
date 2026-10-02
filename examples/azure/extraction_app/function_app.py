"""The extraction service as a Function App: the library's utility API, hosted, plus the queue trigger that finishes a long job.

Read it top to bottom. First SETTINGS, every variable this app reads and what
it is for. Then the four parts, each under its own heading, and each with
what goes in, what comes out, and its code:

    THE DOOR        how_callers_get_in    the library's key, never a function key
    READING         readers               the services 03 made, and what the app does without each
    MASKING         masking               identifiers taken out, on request, before the caller keeps a word
    JOBS            extract_job           an hour for a long job: the queue a saved video waits on

Nothing here is a step. A file goes in one call and its text comes out, and
that is the whole of it: the ten steps are the main app's, which calls this
one at step two and asks it to mask as it reads. Last comes the MAIN SCRIPT,
which is what the Functions host runs: the app the library builds from these
settings, and the queue trigger. Every route it serves is the library's, and
each is described in docs/how-to/extraction-service.md.

WHAT IS PUBLISHED
-----------------

    GET  https://<your app>.azurewebsites.net/health                 is it up, and which masking engine loaded
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
what one function failing to load looks like. Point an availability test at
it. Every route sits under ``VECTRIXDB_EXTRACT_PREFIX`` when one is set, and
answers at the gateway paths ``VECTRIXDB_EXTRACT_GATEWAY_PATHS`` names as
well, so the main app and a gateway work out the same path for every file.

HOW A CALLER GETS IN
--------------------

Not with a Functions key. ``auth_level`` is ANONYMOUS on purpose, because a
function key is one shared secret with no scope and no expiry, and the
library has scoped keys that expire, tokens, an identity provider and a rate
limit already. Every call presents the library's key in ``api-key`` or as a
Bearer token; with sign-in shared with a VectrixDB server, its named keys
and its identity provider's tokens are honoured here too. Health and the
documentation need nothing. The app refuses to start without a key, because
every call spends money on a paid service.

WHERE THE DEPLOYED CODE IS
--------------------------

The same place the main app's is, and the same rule: Flex Consumption runs
the app from a package, so there is no editor in the portal and no console
behind ``<app>.scm.azurewebsites.net``. What you edit is this folder, and
``05_create_extraction_function_app.py --code-only`` publishes it again.
"""

import logging
import os

import azure.functions as func

from vectrixdb.api import create_extraction_app, run_extraction_job
from vectrixdb.api.extraction import ExtractionService
from vectrixdb.masking import describe_engine, engine_from_env, languages_from_env

log = logging.getLogger("vectrixdb.extraction_app")


# ============================================================================
# SETTINGS: every variable this app reads, and what it is for
# ============================================================================
#
# 05_create_extraction_function_app.py sets them, from settings.env and from
# what 01 and 03 made. The keys among them are read from Azure by 05 and sent
# straight to the app; none is written into this code or into a file. Nothing
# of the main app's is here: this one stands alone.
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
# Reading (the services 03 made; what the app does without each is said when 05 sends the settings)
#   AZURE_DOCINTEL_ENDPOINT, _KEY    Document Intelligence: scanned pages, and the layout model when asked
#   AZURE_SPEECH_ENDPOINT, _KEY      Speech: recordings, videos and YouTube
#   AZURE_VISION_ENDPOINT, _KEY      Vision: what a picture shows, and the words in it
#   AZURE_TRANSLATOR_ENDPOINT, _KEY, _REGION
#                                    Translator: the /translate routes
#   VECTRIXDB_EXTRACT_PDF            text, a PDF's own text layer with OCR for scanned pages only; or layout,
#                                    Document Intelligence's layout model, for tables as rows and the numbers printed on pages
#   VECTRIXDB_EXTRACT_URL_HOSTS      the only hosts an address route may fetch from; *.example.com for subdomains
#   VECTRIXDB_MAX_UPLOAD_BYTES       the largest file a call may send; left out, 100 MB
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
#   VECTRIXDB_EXTRACT_JOBS           azure: a long job is a record in a container and a message on a queue
#   VECTRIXDB_EXTRACT_STORAGE        where those live; left out, AzureWebJobsStorage
#   VECTRIXDB_EXTRACT_CONTAINER      the container: extraction
#   VECTRIXDB_EXTRACT_QUEUE          the queue: extract-jobs, which the trigger at the bottom of this file reads
#
# and every other VECTRIXDB_ setting the library's server reads: docs/reference/settings.md


# ============================================================================
# THE DOOR
# ============================================================================
#
# INPUT   a call, with the key in api-key or as a Bearer token; or a session or
#         an identity provider's token, when sign-in is shared with a server
# OUTPUT  the call goes through, or 401 with one refusal shape; health and the
#         documentation need nothing
#
# The Functions auth level is anonymous on purpose. A function key is one
# shared secret with no scope and no expiry, and the library has scoped keys
# that expire, tokens, an identity provider and a rate limit already: Azure is
# the building, and the library's key layer is the door.


def how_callers_get_in() -> str:
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
    if os.environ.get("VECTRIXDB_SIGNIN", "").strip():
        return "the library's key, and the sign-in it shares with the server"
    if os.environ.get("VECTRIXDB_ALLOW_OPEN", "").strip():
        return "nobody asked: open, which is for a laptop and nowhere else"
    return "the library's key, in api-key or as a Bearer token"


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
    given = [
        "documents",
        "pictures" if getattr(service, "image", None) is not None else "",
        "recordings and videos" if getattr(service, "audio", None) is not None else "",
        "pictures in detail" if getattr(service, "describer", None) is not None else "",
        "translation" if getattr(service, "translator", None) is not None else "",
    ]
    return ", ".join(part for part in given if part)


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
# The engine is chosen once, here, from the settings: a bad setting stops the
# app at start rather than at the first file. Whatever engine ran, the
# patterns run after it, so a shape the model missed is still caught by its
# form; and an engine asked about a language it does not cover says so rather
# than answering as if it had.


def masking() -> str:
    """One line for the log: the engine that loaded, and which of the documents' languages it covers.

    INPUT
    -----
    ``VECTRIXDB_MASKING_ENGINE`` and ``VECTRIXDB_MASKING_LANGUAGES``, read
    the way the library reads them: the engine is loaded here, once, and a
    name that is none of auto, regex, presidio, language or comprehend
    raises now, at start, rather than on the first file.

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
    said = describe_engine(engine_from_env(), languages_from_env())
    covered = ", ".join(f"{lang} {'yes' if ok else 'patterns only'}" for lang, ok in said["languages"].items())
    return f"{said['engine']}, {covered}; the patterns run after it"


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


# ============================================================================
# MAIN SCRIPT
# ============================================================================

service = readers()
log.info("the extraction app reads %s; callers get in with %s; masking by %s", what_reads_what(service), how_callers_get_in(), masking())

#: The app the Functions host runs. Azure is the building and the library's
#: key layer is the door, so the Functions auth level is deliberately open:
#: see THE DOOR above.
app = func.AsgiFunctionApp(app=create_extraction_app(service), http_auth_level=func.AuthLevel.ANONYMOUS)


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
    again, because the record is the answer. Only a message the runner
    cannot read at all, one that is not ``{"job": ...}`` say, goes round
    five times and then sits in ``extract-jobs-poison``.

    The library's :func:`run_extraction_job` does the work, with the same
    :func:`readers` the routes use, so a transcript saved by a job is the one
    the route would have returned.
    """
    run_extraction_job(message.get_body())
