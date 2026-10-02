"""Making a collection from the dashboard, adding files to it, and deleting it: the same way the steps make one.

Kept apart from ``function_app.py`` so it can be read and tested without the
Functions runtime. Nothing here reads a request; the routes in
``function_app.py`` hand these the storage account, the collection records
and a name, and what comes back is what the spinner says.

A collection made here is made the way every collection is made:

1. Its record first, with who may retrieve from it, so no file is ever
   searchable before its rule is in place.
2. Its files, dropped in ``ingestion/raw/<name>/``, which is the only place a
   file goes. Event Grid notices each one, the queue gets a message, and the
   function reads it through steps two to eight like any other file.

Deleting takes everything: the record, so nothing lands in the folder any
more; every file under ``raw/``, ``markdown/`` and ``chunks/``; the search
index, its chunk records and its parent sections. No copy is kept.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from collections_of import CHUNKS, CONTAINER, MARKDOWN, RAW


# ============================================================================
# SETTINGS: what is exported, the shape of a name, and the file limit
# ============================================================================
#
# What the module exports, the shape a name and a file name must have, and the
# most a file may weigh.

__all__ = [
    "check_name",
    "files_of",
    "file_name",
    "masking_of",
    "progress",
    "put_file",
    "remove_everything",
    "steps_of_delete",
]

#: A name is a folder, an index and a file: lowercase letters, digits and dashes.
_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_FILE = re.compile(r"[^A-Za-z0-9._ -]+")
FILE_LIMIT = 100 * 1024 * 1024


# ============================================================================
# NAMES AND FILES: checked, named, dropped in raw/
# ============================================================================
#
# INPUT   a name; a file's name or a title; a file's bytes
# OUTPUT  the name as a folder, an index and a file, or a ConfigurationError
#         saying what a name is; the name a file is kept under; the blob
#         dropped into raw/<name>/, and Event Grid does the rest
#
# raw/<name>/ is the only place a file goes; from there it is read through
# steps two to eight like any other file.


def check_name(name: str) -> str:
    """The name as a folder, an index and a file, or a ConfigurationError saying what a name is."""
    from vectrixdb.exceptions import ConfigurationError

    cleaned = str(name or "").strip()
    if not _NAME.match(cleaned):
        raise ConfigurationError(
            "A collection's name is lowercase letters, digits and dashes, up to 63 long, starting and ending with a letter or digit: lending-memos"
        )
    return cleaned


def file_name(given: Optional[str], *, title: Optional[str] = None, typed: bool = False) -> str:
    """The name a file is kept under in ``raw/<collection>/``: the one uploaded, cleaned, or the title as a Markdown file."""
    if typed:
        stem = (
            re.sub(r"-+", "-", _FILE.sub("-", (title or "typed-text").strip()).replace(" ", "-"))
            .strip("-.")
            .lower()
            or "typed-text"
        )
        return f"{stem[:80]}.md"
    stem = (given or "").replace("\\", "/").split("/")[-1].strip()
    stem = _FILE.sub("-", stem).strip("-. ")
    if not stem or stem in (".", ".."):
        from vectrixdb.exceptions import ConfigurationError

        raise ConfigurationError("The file needs a name, sent in X-Filename")
    return stem[:200]


def _raw(blobs: Any, name: str) -> Any:
    from vectrixdb.documents import BlobFiles

    return BlobFiles(blobs, CONTAINER, prefix=f"{RAW}/{name}")


def put_file(blobs: Any, name: str, filename: str, data: bytes) -> str:
    """Drop one file into ``raw/<name>/``: the blob's name. Event Grid does the rest."""
    from vectrixdb.exceptions import ConfigurationError

    if len(data) > FILE_LIMIT:
        raise ConfigurationError(
            f"{filename} is {len(data) // (1024 * 1024)} MB, and a file dropped here is at most {FILE_LIMIT // (1024 * 1024)} MB"
        )
    if not data:
        raise ConfigurationError(f"{filename} is empty")
    _raw(blobs, name).write(filename, data)
    return f"{RAW}/{name}/{filename}"


def files_of(blobs: Any, name: str) -> List[str]:
    """Every file dropped in ``raw/<name>/``, by its name inside the folder."""
    return sorted(_raw(blobs, name).list())


# ============================================================================
# DELETING: everything of a collection
# ============================================================================
#
# INPUT   a collection's name
# OUTPUT  every blob of it deleted, what was uploaded, its Markdown and its
#         chunks, with a count of each; and the steps in the order the spinner
#         shows them
#
# Deleting takes everything: the record first, so nothing lands in the folder
# any more, then the files, the index and the records.


def remove_everything(blobs: Any, name: str) -> Dict[str, int]:
    """Delete every blob of a collection: what was uploaded, its Markdown and its chunks. How many of each went."""
    from vectrixdb.documents import BlobFiles

    gone: Dict[str, int] = {}
    for folder in (RAW, MARKDOWN, CHUNKS):
        files = BlobFiles(blobs, CONTAINER, prefix=f"{folder}/{name}")
        count = 0
        for rel in list(files.list()):
            files.delete(rel)
            count += 1
        gone[folder] = count
    return gone


def steps_of_delete(gone: Dict[str, int], index: bool, record: bool) -> List[Dict[str, str]]:
    """What deleting did, in the order the spinner shows it."""
    return [
        {
            "step": "New files refused",
            "line": "Its record is gone, so nothing that lands in its folder is read as its."
            if record
            else "It had no record, so nothing was being read as its.",
        },
        {
            "step": "Documents removed",
            "line": f"{gone.get(RAW, 0)} uploaded, {gone.get(MARKDOWN, 0)} Markdown and {gone.get(CHUNKS, 0)} chunk files deleted.",
        },
        {
            "step": "Index removed",
            "line": "The search index is gone."
            if index
            else "There was no search index to remove.",
        },
        {
            "step": "Records removed",
            "line": "Its chunk records and parent sections are deleted."
            if index
            else "There were none.",
        },
        {"step": "Taken off the list", "line": "The app no longer reads it as a collection."},
        {"step": "Recorded in audit", "line": "Who deleted it, when, and what was removed."},
    ]


# ============================================================================
# PROGRESS: what the spinner says
# ============================================================================
#
# INPUT   the record, the files, how many are read, the chunks, the cut in
#         force, the job statuses, and what was masked
# OUTPUT  one step, one line, and whether the collection is ready or something
#         failed
#
# The stages are read from what exists, not from a log: the record, the kept
# documents, the chunks and the runs. The Masking stage counts what each kept
# document says was taken out of it, so the index never held an identifier.


def _state(status: Optional[Dict[str, Any]]) -> str:
    return str((status or {}).get("state") or "")


#: Each masked type in the words the spinner uses, plural.
_TYPE_WORDS = {
    "email": "emails",
    "phone": "phone numbers",
    "credit_card": "cards",
    "ssn": "SSNs",
    "sin": "SINs",
    "iban": "IBANs",
    "bank_account": "bank accounts",
    "passport": "passports",
    "drivers_license": "licences",
    "ip_address": "IP addresses",
    "address": "addresses",
    "date_of_birth": "dates",
    "api_key": "keys",
    "connection_string": "passwords",
    "internal_host": "internal hosts",
    "name": "names",
    "national_id": "national ids",
}


def masking_of(entries: Iterable[Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    """What the reader masked across the kept documents, from the ``masking`` each entry carries.

    ``files`` is how many documents carry a summary at all, ``masked`` how
    many had something taken out, ``total`` how many identifiers went,
    ``counts`` how many of each type, ``patterns_only`` how many documents the
    engine could not read, their language not covered, and ``words`` the line
    the spinner shows.
    """
    counts: Dict[str, int] = {}
    files = masked = patterns_only = 0
    for entry in entries:
        said = (entry or {}).get("masking")
        if not isinstance(said, dict):
            continue
        files += 1
        found = {str(k): int(v) for k, v in (said.get("counts") or {}).items() if int(v)}
        if found:
            masked += 1
        for kind, n in found.items():
            counts[kind] = counts.get(kind, 0) + n
        if said.get("regex_only"):
            patterns_only += 1
    total = sum(counts.values())
    if not files:
        words = ""
    elif not total:
        words = (
            f"Nothing to mask in {files} file{'s' if files != 1 else ''}: no identifier was found."
        )
    else:
        by_type = ", ".join(
            f"{n} {_TYPE_WORDS.get(kind, kind)}"
            for kind, n in sorted(counts.items(), key=lambda kv: -kv[1])
        )
        words = f"{total} identifier{'s' if total != 1 else ''} masked in {masked} of {files} file{'s' if files != 1 else ''} before anything was kept: {by_type}. The index never holds them."
    if patterns_only:
        words += f" {patterns_only} file{'s' if patterns_only != 1 else ''} waited for the patterns alone, {'its' if patterns_only == 1 else 'their'} language not covered."
    return {
        "files": files,
        "masked": masked,
        "total": total,
        "counts": counts,
        "patterns_only": patterns_only,
        "words": words.strip(),
    }


def progress(
    name: str,
    *,
    record: Any,
    files: int,
    read: int,
    chunks: int,
    cut: Optional[str],
    jobs: Dict[str, Optional[Dict[str, Any]]],
    masking: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Where a new collection is, as the spinner says it: one step, one line, and whether it is ready.

    ``record`` is the collection's record or None; ``files`` how many were
    dropped; ``read`` how many have their Markdown kept; ``chunks`` how many
    chunks the index holds; ``cut`` the cut in force or None; ``jobs`` the
    four job statuses, ``golden``, ``chunking``, ``apply`` and
    ``evaluation``, each as its status file holds it or None; ``masking``
    what :func:`masking_of` made of the kept documents, which is the
    Masking stage between Reading and Markdown: the reader masks as it reads,
    so once every file is read the stage says what went.
    """
    policy = record.policy_object() if record is not None else None
    masked = masking or {
        "files": 0,
        "masked": 0,
        "total": 0,
        "counts": {},
        "patterns_only": 0,
        "words": "",
    }
    facts = {
        "policy": policy.describe() if policy is not None else None,
        "files": files,
        "read": read,
        "chunks": chunks,
        "cut": cut,
        "masking": masked,
    }
    if record is None:
        return {
            "step": "Policy",
            "line": f"{name} has no record yet, so nothing can retrieve from it.",
            "ready": False,
            "failed": False,
            **facts,
        }
    if policy is None:
        return {
            "step": "Policy",
            "line": f"{name} has a record and no policy, so nobody can retrieve from it until it is given one.",
            "ready": False,
            "failed": False,
            **facts,
        }
    if files == 0:
        return {
            "step": "Policy",
            "line": f"Only {policy.describe()} can retrieve from {name}. Waiting for its first file.",
            "ready": False,
            "failed": False,
            **facts,
        }
    if read < files:
        so_far = (
            f" So far {masked['total']} identifier{'s' if masked['total'] != 1 else ''} masked."
            if masked["total"]
            else ""
        )
        return {
            "step": "Reading",
            "line": f"{read} of {files} files read. Each is read by what reads its type: PDFs, documents, pictures and sound, and masked as it is read.{so_far}",
            "ready": False,
            "failed": False,
            **facts,
        }
    if cut is None:
        if masked["files"]:
            return {
                "step": "Masking",
                "line": f"{masked['words']} Kept as Markdown, waiting for a cut to be picked, then every step follows.",
                "ready": False,
                "failed": False,
                **facts,
            }
        return {
            "step": "Markdown",
            "line": f"All {files} read and kept as Markdown. They wait for a cut to be picked, then every step follows.",
            "ready": False,
            "failed": False,
            **facts,
        }
    if chunks == 0:
        return {
            "step": "Chunks",
            "line": f"Cut the way every collection is cut right now, {cut}, then embedded and indexed.",
            "ready": False,
            "failed": False,
            **facts,
        }
    for job, step, line in (
        ("golden", "Golden data", "Questions whose answers are known are added for its documents."),
        (
            "chunking",
            "Chunking compared",
            "Every way of cutting is tried on every collection, each handed the same characters.",
        ),
        ("apply", "Chunks", "Every kept document is cut the way now in force."),
        (
            "evaluation",
            "Retrieval compared",
            "Every way of searching is timed and ranked, and three are named.",
        ),
    ):
        state = _state(jobs.get(job))
        if state in ("asked", "running", "writing", "applying"):
            said = jobs.get(job) or {}
            detail = ""
            if said.get("build") and said.get("of"):
                detail = f" Build {said['build']} of {said['of']}."
            elif said.get("way") and said.get("of"):
                detail = f" Way {said['way']} of {said['of']}."
            elif said.get("written") is not None and said.get("wanted"):
                detail = f" {said['written']} of {said['wanted']} written."
            return {"step": step, "line": line + detail, "ready": False, "failed": False, **facts}
        if state == "failed":
            return {
                "step": step,
                "line": f"{step} failed: {(jobs.get(job) or {}).get('message') or 'see the Evaluate page'}.",
                "ready": False,
                "failed": True,
                **facts,
            }
    held = " Nothing in the index holds an identifier." if masked["files"] else ""
    return {
        "step": "Ready",
        "line": f"Searchable by {policy.describe()}. It went through every step, and follows what is in force from now on.{held}",
        "ready": True,
        "failed": False,
        **facts,
    }


# ============================================================================
# TIME AND COUNTS
# ============================================================================
#
# INPUT   a clock; something that may fail
# OUTPUT  now, as ISO; a count, or zero when the store cannot be asked right
#         now
#
# A store that cannot be asked makes a stage wait, not fail.


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def count_or_zero(counter: Callable[[], int]) -> int:
    try:
        return int(counter() or 0)
    except Exception:  # a store that cannot be asked right now: the spinner says what it can
        return 0
