# ============================================================================
# THE FILES THAT WOULD NOT READ
# ============================================================================
#
# INPUT   the poison queue's messages; the worker's outcome for a file and
#         how many times it has been tried; a person's Retry or Drop
# OUTPUT  one record a failed file beside the queue, ``ingestion/failed/
#         <collection>/<file>.json``, the error in words a person acts on;
#         one row a poisoned message for the Ingest page; the message back on
#         the ingest queue, or the file and everything of it gone
#
# The poison queue is where a file lands after five tries, and a queue is a
# list nobody opens. This is what lets the dashboard show it: which file, why
# it stopped, and the two things a person can do about it. No Azure import,
# so it is tested against stand-ins like ingest_queue.py beside it.
#
# Author: Kwadwo Daddy Nyame Owusu - Boakye

"""The files that would not read: the poison queue in words, with Retry and Drop.

A file the worker cannot read goes round the ingest queue five times and
then sits in ``<queue>-poison``. The message there is the blob event, which
says which file but not why it failed, so the worker keeps one small record
a failed file, ``ingestion/failed/<collection>/<file>.json``, with the last
error, how many tries and when, and clears it the moment the file reads.
The Ingest page joins the queue with those records: one row a file, the
error as a line a person acts on with the reason under it, and Retry, which
puts the message back on the ingest queue, or Drop, which deletes the file
and everything of it.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from collections_of import CHUNKS, CONTAINER, MARKDOWN, RAW, collection_of
from ingest_queue import events_in

__all__ = ["FAILED", "clear_failure", "drop", "file_of", "poison_queue_name", "poison_rows", "record_failure", "retry", "take_message", "why_in_words"]

#: The folder beside raw/, markdown/ and chunks/: one JSON record a file that would not read.
FAILED = "failed"


def poison_queue_name(queue: str) -> str:
    """Where the Functions host moves a message after its five tries."""
    return f"{queue}-poison"


def file_of(uri: str, collection: str) -> str:
    """The file's name inside ``raw/<collection>/``, from the blob's address."""
    marker = f"/{RAW}/{collection}/"
    at = uri.find(marker)
    return uri[at + len(marker):] if at >= 0 else uri.rsplit("/", 1)[-1]


def _failed(blobs: Any) -> Any:
    from vectrixdb.documents import BlobFiles

    return BlobFiles(blobs, CONTAINER, prefix=FAILED)


# ============================================================================
# THE WORDS
# ============================================================================
#
# INPUT   the worker's error for a file, and the file's name
# OUTPUT  one line a person acts on, and the reason under it


def why_in_words(error: str, file: str) -> Tuple[str, str]:
    """What stopped a file, as a line a person acts on and the reason under it; the raw error stays on the record."""
    low = (error or "").lower()
    suffix = file.rsplit(".", 1)[-1].lower() if "." in file.rsplit("/", 1)[-1] else ""
    if "timed out" in low or "timeout" in low or "out of time" in low or "time limit" in low:
        return "Ran out of time", "the file is too long for one try: split it, or raise the queue's time limit"
    if "no reader" in low or "unsupported" in low or "does not read" in low or "no extractor" in low or "not read" in low:
        return (f"No reader for .{suffix}" if suffix else "No reader for this file"), "export it as a PDF and drop it again"
    if "0 of" in low or "no text" in low or "nothing to read" in low or "empty" in low or "0 pages" in low:
        return "Nothing could be read from it", "the file may be an image with no text layer, or empty; OCR found nothing on it"
    if "429" in low or "too many" in low or "busy" in low or "rate" in low:
        return "The reader was busy every time", "the extraction service refused all five tries; retry when it is quiet"
    if "503" in low or "unavailable" in low or "connection" in low or "unreachable" in low or "refused" in low:
        return "The reader could not be reached", "the extraction app was down or unreachable; retry once its /health answers"
    said = (error or "").strip().splitlines()[0][:160] if (error or "").strip() else "Reading failed"
    return said, "retry, and if it fails again the file needs a look"


# ============================================================================
# THE RECORD A FAILED FILE KEEPS
# ============================================================================
#
# INPUT   the worker's outcome for one file, and which try this was
# OUTPUT  ingestion/failed/<collection>/<file>.json written, or removed once
#         the file reads


def record_failure(blobs: Any, outcome: Any, tries: int, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Keep what stopped a file, for the page to say: the error, how many tries, when. None for a blob in no collection."""
    uri = str(getattr(outcome, "uri", "") or "")
    collection = collection_of(uri) if uri else None
    if not collection:
        return None
    file = file_of(uri, collection)
    error = str(getattr(outcome, "error", "") or "read failed")
    said, why = why_in_words(error, file)
    record = {
        "uri": uri, "collection": collection, "file": file, "error": error, "said": said, "why": why,
        "tries": int(tries or 0), "last_tried": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }
    _failed(blobs).write(f"{collection}/{file}.json", json.dumps(record, ensure_ascii=False).encode("utf-8"))
    return record


def clear_failure(blobs: Any, uri: str) -> bool:
    """A file that read is no longer a failure: its record goes. False when there was none."""
    collection = collection_of(uri) if uri else None
    if not collection:
        return False
    files = _failed(blobs)
    rel = f"{collection}/{file_of(uri, collection)}.json"
    try:
        if not files.exists(rel):
            return False
        files.delete(rel)
        return True
    except FileNotFoundError:
        return False


def _record_of(blobs: Any, collection: Optional[str], file: str) -> Optional[Dict[str, Any]]:
    if not collection:
        return None
    files = _failed(blobs)
    rel = f"{collection}/{file}.json"
    try:
        if not files.exists(rel):
            return None
        return json.loads(files.read(rel).decode("utf-8"))
    except (FileNotFoundError, ValueError):
        return None


# ============================================================================
# THE ROWS, RETRY AND DROP
# ============================================================================
#
# INPUT   the poison queue's messages, peeked or received; the blobs
# OUTPUT  one row a message for the page; the message back on the ingest
#         queue; or the file, its Markdown, its chunks and its record gone


def poison_rows(messages: Any, blobs: Any) -> List[Dict[str, Any]]:
    """Each message on the poison queue as one row: which file, what stopped it, how many tries, when."""
    rows: List[Dict[str, Any]] = []
    for message in messages:
        events = events_in(getattr(message, "content", None))
        uri = str(events[0].uri) if events else ""
        collection = collection_of(uri) if uri else None
        file = file_of(uri, collection) if collection else (uri.rsplit("/", 1)[-1] if uri else "?")
        record = _record_of(blobs, collection, file)
        error = str((record or {}).get("error") or "read failed")
        said, why = why_in_words(error, file)
        when = (record or {}).get("last_tried")
        if not when:
            inserted = getattr(message, "inserted_on", None)
            when = inserted.isoformat() if hasattr(inserted, "isoformat") else (str(inserted) if inserted else None)
        rows.append({
            "id": str(getattr(message, "id", "")), "file": file, "collection": collection, "uri": uri,
            "error": error, "said": said, "why": why,
            "tries": int((record or {}).get("tries") or getattr(message, "dequeue_count", 0) or 0),
            "last_tried": when,
        })
    return rows


def take_message(poison: Any, message_id: str) -> Optional[Any]:
    """The one message asked for, received so it can be deleted; the others become visible again in half a minute."""
    for message in poison.receive_messages(messages_per_page=32, visibility_timeout=30):
        if str(getattr(message, "id", "")) == message_id:
            return message
    return None


def retry(poison: Any, ingest: Any, message: Any) -> None:
    """Back on the ingest queue, as it was, and off the poison queue. The worker starts from the Markdown when there is one."""
    ingest.send_message(message.content)
    poison.delete_message(message)


def drop(blobs: Any, poison: Any, message: Any, collection: Optional[str], file: str) -> Dict[str, int]:
    """The file and everything of it gone: what was uploaded, its Markdown, its chunks, its record, and the message. How many of each."""
    from vectrixdb.documents import BlobFiles

    gone: Dict[str, int] = {}
    if collection:
        for folder, rel in ((RAW, file), (MARKDOWN, f"{file}.md"), (CHUNKS, f"{file}.jsonl"), (FAILED, f"{file}.json")):
            files = BlobFiles(blobs, CONTAINER, prefix=f"{folder}/{collection}")
            count = 0
            try:
                if files.exists(rel):
                    files.delete(rel)
                    count = 1
            except FileNotFoundError:
                count = 0
            gone[folder] = count
    poison.delete_message(message)
    return gone
