"""The evals container's files, the picks in force, and the queue messages that ask for the jobs. No Azure import.

Kept apart from ``function_app.py`` so it can be tested, as ``ingest_queue.py``
is: that file cannot be imported without the Functions runtime. Step four's
files sit together in ``golden_dataset/``, the folder they have in data/ on
your machine, the runs of steps five and nine beside it, and the picks they
make in ``picks.json``:

    golden_dataset/golden.jsonl            the dataset, every row a draft until a person checks it
    golden_dataset/golden.answers.jsonl    every answer the model gave, so a stopped run starts where it stopped
    golden_dataset/golden.status.json      how the writing is going: asked, writing, done, refused or failed
    golden_dataset/examples.txt            a few questions people really ask, one a line, which you write
    golden_dataset/<sha256>.jsonl          the golden file each run used, kept once a version for an admin to download
    picks.json                             the cut step six uses and the way step ten searches, and which run chose each
    apply.status.json                      how bringing every document to a new cut is going
    evaluation.status.json                 how the evaluation is going: asked, running, done, refused or failed
    retrieval/runs/<id>/report.json        each run of every way of searching, which the Evaluate page's Retrieval tab draws
    chunking.status.json                   how the chunking comparison is going: asked, running, done, refused or failed
    chunking/work/<job>/<build>.json       each build of a comparison under way, so the next message goes on from it
    chunking/runs/<id>/report.json         each run of every way of cutting, which the Evaluate page's Chunking tab draws
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlparse

from vectrixdb.evaluation import chunking_build, search_of


# ============================================================================
# SETTINGS: what is exported, and the files in the evals container
# ============================================================================
#
# What the module exports, and the files in the evals container: the golden
# dataset with its answers and status, the runs of steps five and nine beside
# it, and the picks they make in picks.json.

__all__ = [
    "ANSWERS",
    "APPLY_STATUS",
    "AUTO",
    "AUTO_ROLE",
    "CHUNKING_STATUS",
    "CHUNKING_WORK",
    "DATASET",
    "EVALUATION_STATUS",
    "EXAMPLES",
    "GOLDEN",
    "MOST",
    "PICKS_FILE",
    "ROLES",
    "STATUS",
    "GoldenFolder",
    "apply_message",
    "apply_request",
    "chunking_in_force",
    "chunking_job",
    "chunking_message",
    "chunking_request",
    "chunking_turn",
    "evaluation_message",
    "evaluation_request",
    "golden_message",
    "golden_request",
    "queue_account",
    "recorded_cut",
    "retrieval_in_force",
    "with_chunking_pick",
    "with_retrieval_picks",
]

#: Step four's folder in the evals container, named as it is in data/.
DATASET = "golden_dataset"
GOLDEN, ANSWERS, STATUS, EXAMPLES = (
    f"{DATASET}/golden.jsonl",
    f"{DATASET}/golden.answers.jsonl",
    f"{DATASET}/golden.status.json",
    f"{DATASET}/examples.txt",
)
EVALUATION_STATUS = "evaluation.status.json"
CHUNKING_STATUS = "chunking.status.json"
#: Where a comparison under way keeps each build it made, until the run is saved.
CHUNKING_WORK = "chunking/work"
#: The picks in force, and which run chose each: step six's cut and step ten's way of searching.
PICKS_FILE = "picks.json"
#: How bringing every document to a new cut is going.
APPLY_STATUS = "apply.status.json"

#: What INGEST_CHUNKING and RETRIEVAL_SETUP say to follow the runs. Any other value pins a pick.
AUTO = "auto"
#: The three a retrieval run names, and the one ``auto`` follows.
ROLES = ("finds_the_most", "best_for_balance", "best_for_time")
AUTO_ROLE = "best_for_balance"

#: The most questions one request may ask for: what fits in the function's hour.
MOST = 300


# ============================================================================
# GOLDEN: the message and the request
# ============================================================================
#
# INPUT   how many questions; a queue message
# OUTPUT  the message that asks for the golden dataset; what a message asks
#         for when it asks for that, None when it is anything else
#
# A queue message is one job; each kind reads its own and refuses the rest, so
# a message never runs as the wrong job.


def golden_message(n: int) -> str:
    """The queue message that asks for the golden dataset, ``n`` questions of it."""
    return json.dumps({"vectrixdb": "golden", "n": int(n)})


def _asked(body: Any, job: str) -> Optional[Dict[str, Any]]:
    """The message as the job it names, or None when it names another or none.

    Read decoded or as base64, as ``events_in`` reads an event, because
    which one the host hands over is a setting people get wrong. A blob
    event from Event Grid names no job, and neither does a message that is
    not JSON.
    """
    if isinstance(body, (bytes, bytearray)):
        body = bytes(body).decode("utf-8", errors="replace")
    if isinstance(body, str):
        text = body.strip()
        if not text.startswith("{"):
            try:
                text = base64.b64decode(text, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                return None
        try:
            body = json.loads(text)
        except ValueError:
            return None
    if not isinstance(body, dict) or body.get("vectrixdb") != job:
        return None
    return body


def golden_request(body: Any) -> Optional[Dict[str, Any]]:
    """What a queue message asks for when it asks for the golden dataset; None when it is anything else.

    The count is left out when it is not a number from 1 to :data:`MOST`,
    and the step's own count is used.
    """
    asked = _asked(body, "golden")
    if asked is None:
        return None
    try:
        n = int(asked.get("n") or 0)
    except (TypeError, ValueError):
        n = 0
    return {"n": n} if 0 < n <= MOST else {}


# ============================================================================
# EVALUATION: the message and the request
# ============================================================================
#
# INPUT   the golden file's address and the collections; a queue message
# OUTPUT  the message that asks for an evaluation; the request it carries, or
#         None
#
# The same shape as the golden job's.


def evaluation_message(golden: str, collections: Sequence[str]) -> str:
    """The queue message that asks for an evaluation: the golden file's address, and the collections to ask."""
    return json.dumps(
        {
            "vectrixdb": "evaluate",
            "golden": str(golden),
            "collections": [str(c) for c in collections],
        }
    )


def evaluation_request(body: Any) -> Optional[Dict[str, Any]]:
    """What a queue message asks for when it asks for an evaluation; None when it is anything else.

    No collections means every one, and no golden address means the step's own.
    """
    asked = _asked(body, "evaluate")
    if asked is None:
        return None
    named = asked.get("collections")
    return {
        "golden": str(asked.get("golden") or "").strip(),
        "collections": [str(c).strip() for c in named if str(c).strip()]
        if isinstance(named, list)
        else [],
    }


# ============================================================================
# CHUNKING: the job, its messages, and one turn
# ============================================================================
#
# INPUT   a plan; a job's name and which build is next; a queue message
# OUTPUT  a new comparison's name, when it was asked for; the message for one
#         build; the request, or None; one turn: one build made and kept, then
#         the next asked for, or the run saved
#
# One build a message, so a comparison of many builds is many short runs
# inside the host's limit, and a stopped run starts where it stopped.


def chunking_job() -> str:
    """A new comparison's name: when it was asked for, which is also the order the runs are listed in."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def chunking_message(golden: str, collections: Sequence[str], job: str, at: int = 0) -> str:
    """The queue message that asks for one build of a chunking comparison: the ``at``-th of the plan, in ``job``."""
    return json.dumps(
        {
            "vectrixdb": "chunking",
            "golden": str(golden),
            "collections": [str(c) for c in collections],
            "job": str(job),
            "at": int(at),
        }
    )


def chunking_request(body: Any) -> Optional[Dict[str, Any]]:
    """What a queue message asks for when it asks for a chunking comparison; None when it is anything else.

    No collections means every one, no golden address means the step's own,
    and no job means a new comparison, from its first build.
    """
    asked = _asked(body, "chunking")
    if asked is None:
        return None
    named = asked.get("collections")
    try:
        at = max(0, int(asked.get("at") or 0))
    except (TypeError, ValueError):
        at = 0
    return {
        "golden": str(asked.get("golden") or "").strip(),
        "collections": [str(c).strip() for c in named if str(c).strip()]
        if isinstance(named, list)
        else [],
        "job": "".join(ch for ch in str(asked.get("job") or "") if ch.isalnum() or ch in "-_"),
        "at": at,
    }


def chunking_turn(
    folder: "GoldenFolder",
    asked: Dict[str, Any],
    plan: Sequence[Dict[str, Any]],
    *,
    build: Callable[[Dict[str, Any]], Dict[str, Any]],
    finish: Callable[[List[Dict[str, Any]]], Dict[str, Any]],
    send: Callable[[str], Any],
) -> Dict[str, Any]:
    """One queue message's share of step five: one build made and kept, then the next asked for, or the run saved.

    A comparison is some forty builds, each the documents cut one way,
    embedded, searched with every golden question and, with a model, every
    answer judged. That is more than the function's hour, so each message
    makes one build, keeps it in ``chunking/work/<job>/``, and puts the next
    on the queue. A message the host hands over twice finds its build kept
    and goes straight on. After the last, ``finish`` is given every build
    and saves the run, the work folder is cleared, and the status says what
    was picked, and what the run did to the cut in force when ``finish``
    says so as ``picked``. What it gives back is what the status says.
    """
    job = asked.get("job") or chunking_job()
    at = int(asked.get("at") or 0)
    said = {
        "golden": asked.get("golden") or "",
        "collections": list(asked.get("collections") or []),
        "job": job,
    }
    if at < len(plan):
        one = plan[at]
        kept = f"{CHUNKING_WORK}/{job}/{one['key']}.json"
        if not folder.exists(kept):
            folder.status(
                "running", into=CHUNKING_STATUS, build=at + 1, of=len(plan), key=one["key"], **said
            )
            folder.write(kept, json.dumps(build(dict(one)), ensure_ascii=False).encode("utf-8"))
        if at + 1 < len(plan):
            send(chunking_message(said["golden"], said["collections"], job, at + 1))
            return folder.status(
                "running", into=CHUNKING_STATUS, build=at + 1, of=len(plan), key=one["key"], **said
            )
    results = []
    for one in plan:
        data = folder.read(f"{CHUNKING_WORK}/{job}/{one['key']}.json")
        results.append(
            json.loads(data.decode("utf-8"))
            if data
            else {**one, "questions": 0, "error": "this build was not kept"}
        )
    report = finish(results)
    for one in plan:
        folder.remove(f"{CHUNKING_WORK}/{job}/{one['key']}.json")
    techniques = report.get("techniques") or []
    return folder.status(
        "done",
        into=CHUNKING_STATUS,
        run=report.get("id"),
        scored=report.get("scored"),
        best=techniques[0]["name"] if techniques else None,
        techniques=[
            f"{t['rank']}. {t['name']}, {t.get('right', 0)} of {t.get('questions', 0)}{' (a tie)' if t.get('tie') else ''}"
            for t in techniques
        ],
        failed=[f"{b['key']}: {b['error']}" for b in report.get("builds") or [] if b.get("error")],
        **({"picked": report["picked"]} if report.get("picked") else {}),
        **said,
    )


# ============================================================================
# WHAT IS IN FORCE: the cut and the picks
# ============================================================================
#
# INPUT   picks.json and settings.env
# OUTPUT  the cut step six uses and why, pinned or auto; how step ten searches
#         one collection and why; what a document cut with a build has
#         recorded; picks with a cut, or with a collection's three named by a
#         run
#
# Pinned in settings.env wins; otherwise the newest run's choice; the key None
# while there is none.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def chunking_in_force(setting: Optional[str], picks: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The cut step six uses, and why: ``{"key": ..., "by": "pinned" | "auto", ...}``, the key None while there is none.

    ``setting`` is ``INGEST_CHUNKING``. ``auto``, or nothing, follows
    ``picks.json``, which step five writes when a run over checked questions
    picks a cut. Anything else is a build key, ``markdown-1000-h``, and pins
    that cut whatever the runs say. A key that names no build raises
    ValueError, which is a setting to put right, not a cut to guess.
    """
    value = str(setting or "").strip() or AUTO
    if value != AUTO:
        chunking_build(value)
        return {"key": value, "by": "pinned", "why": "INGEST_CHUNKING names it"}
    chosen = dict((picks or {}).get("chunking") or {})
    if chosen.get("key"):
        return {
            "key": str(chosen["key"]),
            "by": "auto",
            "run": chosen.get("run"),
            "why": chosen.get("why"),
            "at": chosen.get("at"),
        }
    return {
        "key": None,
        "by": "auto",
        "why": "no chunking run over checked questions has picked a cut yet, so files wait at step three with their Markdown kept",
    }


def retrieval_in_force(
    setting: Optional[str], picks: Optional[Mapping[str, Any]], collection: str
) -> Dict[str, Any]:
    """How step ten searches one collection, and why: ``{"search": kwargs or None, "setup": ..., "by": ..., ...}``.

    ``setting`` is ``RETRIEVAL_SETUP``. ``auto``, or nothing, is the
    collection's ``best_for_balance`` in ``picks.json``, and a pick's name,
    ``finds_the_most`` or ``best_for_time``, follows that one instead: both
    move when step nine names another. Anything else is a way of searching,
    ``hybrid_semantic.both``, pinned in every collection. With no run yet,
    ``search`` is None, and step ten searches the way step five searched every cut.
    """
    value = str(setting or "").strip() or AUTO
    role = AUTO_ROLE if value == AUTO else value
    if role in ROLES:
        chosen = dict(((picks or {}).get("retrieval") or {}).get(collection) or {})
        one = dict((chosen.get("picks") or {}).get(role) or {})
        if one.get("search") is not None:
            return {
                "search": dict(one["search"]),
                "setup": one.get("key"),
                "by": "auto",
                "role": role,
                "run": chosen.get("run"),
                "at": chosen.get("at"),
            }
        return {
            "search": None,
            "setup": None,
            "by": "auto",
            "role": role,
            "why": f"no retrieval run over checked questions has named {role} for {collection} yet, so step ten searches the way step five searched every cut",
        }
    return {
        "search": search_of(value),
        "setup": value,
        "by": "pinned",
        "why": "RETRIEVAL_SETUP names it",
    }


def recorded_cut(key: str) -> Dict[str, Any]:
    """What a document cut with this build has recorded as its chunking: how a document at the pick is told from one that is not."""
    build = chunking_build(key)
    cut: Dict[str, Any] = {
        "chunk": build["chunk"],
        "chunk_size": build["size"],
        "overlap": build["overlap"],
        "parent_size": build["parent_size"],
        "embed_heading": build["headings"],
    }
    if build["adds"] == "context":
        cut["context"] = True
    if build["adds"] == "late":
        cut["late"] = True
    return cut


def with_chunking_pick(
    picks: Optional[Mapping[str, Any]], *, key: str, run: Optional[str], why: str, questions: int
) -> Dict[str, Any]:
    """``picks`` with a cut in force: its key, the run that chose it, why, over how many checked questions, and when."""
    return {
        **dict(picks or {}),
        "chunking": {"key": key, "run": run, "why": why, "questions": int(questions), "at": _now()},
    }


def with_retrieval_picks(
    picks: Optional[Mapping[str, Any]], collection: str, report: Mapping[str, Any], questions: int
) -> Dict[str, Any]:
    """``picks`` with a collection's three named by a run: each pick's setup and the ``search()`` arguments that run it."""
    setups = {str(s.get("key")): s for s in report.get("setups") or []}
    named = {
        role: {
            "key": key,
            "search": dict(setups[key].get("search") or {}),
            "label": setups[key].get("method_label"),
        }
        for role, key in (report.get("picks") or {}).items()
        if key in setups
    }
    retrieval = {
        **dict((picks or {}).get("retrieval") or {}),
        collection: {
            "run": report.get("id"),
            "picks": named,
            "questions": int(questions),
            "at": _now(),
        },
    }
    return {**dict(picks or {}), "retrieval": retrieval}


# ============================================================================
# APPLY: the message and the request
# ============================================================================
#
# INPUT   the collections and the cut's key; a queue message
# OUTPUT  the message that asks for every kept document to be cut that way;
#         the request, or None when it names no build
#
# The same shape as the other jobs'.


def apply_message(key: str, collections: Sequence[str]) -> str:
    """The queue message that asks for every kept document of these collections to be cut the way ``key`` names."""
    return json.dumps(
        {"vectrixdb": "apply", "key": str(key), "collections": [str(c) for c in collections]}
    )


def apply_request(body: Any) -> Optional[Dict[str, Any]]:
    """What a queue message asks for when it asks for a cut to be applied; None when it is anything else, or names no build."""
    asked = _asked(body, "apply")
    if asked is None:
        return None
    key = str(asked.get("key") or "").strip()
    try:
        chunking_build(key)
    except ValueError:
        return None
    named = asked.get("collections")
    return {
        "key": key,
        "collections": [str(c).strip() for c in named if str(c).strip()]
        if isinstance(named, list)
        else [],
    }


# ============================================================================
# THE FOLDER: the evals container
# ============================================================================
#
# INPUT   the storage account's Blob address
# OUTPUT  the queue service beside the blobs; the folder where the golden
#         dataset and the files beside it live, read and written
#
# The ingest queue lives beside the blobs, so one address finds both.


def queue_account(blob_account: str) -> str:
    """The queue service of the storage account whose Blob address is given: the ingest queue lives beside the blobs."""
    return blob_account.replace(".blob.", ".queue.", 1)


class GoldenFolder:
    """The evals container, where the golden dataset and the files beside it live.

    ``container`` is an azure-storage-blob ``ContainerClient``, or anything
    with ``get_blob_client`` whose clients answer ``exists``,
    ``download_blob`` and ``upload_blob``, which is what the tests give it.
    """

    def __init__(self, container: Any, prefix: str = "") -> None:
        self.container = container
        self.prefix = prefix.strip("/")

    @classmethod
    def at(cls, url: str, service: Any) -> "GoldenFolder":
        """The folder at a Blob address, ``https://<account>.blob.core.windows.net/evals``, through a ``BlobServiceClient``."""
        parts = urlparse(url).path.strip("/").split("/", 1)
        if not parts[0]:
            raise ValueError(f"{url}: a Blob address names a container")
        return cls(service.get_container_client(parts[0]), parts[1] if len(parts) > 1 else "")

    def _blob(self, name: str) -> Any:
        return self.container.get_blob_client(f"{self.prefix}/{name}" if self.prefix else name)

    def exists(self, name: str) -> bool:
        return bool(self._blob(name).exists())

    def read(self, name: str) -> Optional[bytes]:
        blob = self._blob(name)
        return blob.download_blob().readall() if blob.exists() else None

    def write(self, name: str, data: bytes, *, overwrite: bool = True) -> None:
        self._blob(name).upload_blob(data, overwrite=overwrite)

    def remove(self, name: str) -> None:
        """Remove one file, and say nothing when it is not there."""
        blob = self._blob(name)
        if blob.exists():
            blob.delete_blob()

    def url(self, name: str) -> str:
        return str(self._blob(name).url)

    def examples(self) -> List[str]:
        """The questions in ``examples.txt``, one a line, blank lines left out; none when there is no such file."""
        data = self.read(EXAMPLES)
        if not data:
            return []
        return [
            line.strip()
            for line in data.decode("utf-8-sig", errors="replace").splitlines()
            if line.strip()
        ]

    def fetch_answers(self, local: str) -> None:
        """The answers kept in the container, copied to ``local``; one left there from an earlier run is removed when the container has none.

        Deleting ``golden_dataset/golden.answers.jsonl`` from the container is
        how a person asks for everything afresh, so an old copy on the
        instance must not answer in its place.
        """
        data = self.read(ANSWERS)
        if data is None:
            if os.path.exists(local):
                os.remove(local)
            return
        with open(local, "wb") as out:
            out.write(data)

    def keep_answers(self, local: str) -> None:
        """The answers so far, copied to the container."""
        if os.path.exists(local):
            with open(local, "rb") as kept:
                self.write(ANSWERS, kept.read())

    def status(self, state: str, into: str = STATUS, **said: Any) -> Dict[str, Any]:
        """Say how a job is going, in ``golden.status.json`` or the file ``into`` names, and give it back."""
        record = {
            "state": state,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **said,
        }
        self.write(into, json.dumps(record, indent=2, ensure_ascii=False).encode("utf-8"))
        return record

    def read_status(self, into: str = STATUS) -> Optional[Dict[str, Any]]:
        data = self.read(into)
        if not data:
            return None
        try:
            found = json.loads(data.decode("utf-8"))
        except ValueError:
            return None
        return found if isinstance(found, dict) else None

    def read_picks(self) -> Dict[str, Any]:
        """What ``picks.json`` holds, or nothing when no pick was ever made."""
        data = self.read(PICKS_FILE)
        if not data:
            return {}
        try:
            found = json.loads(data.decode("utf-8"))
        except ValueError:
            return {}
        return found if isinstance(found, dict) else {}

    def write_picks(self, picks: Mapping[str, Any]) -> None:
        self.write(
            PICKS_FILE, json.dumps(dict(picks), indent=2, ensure_ascii=False).encode("utf-8")
        )

    def busy(self, within_seconds: float = 3600.0, into: str = STATUS) -> bool:
        """Whether a job was asked for or began within the function's hour: one at a time, and one that died long ago does not block."""
        found = self.read_status(into) or {}
        if found.get("state") not in ("asked", "writing", "running"):
            return False
        try:
            at = datetime.fromisoformat(str(found.get("at")))
        except ValueError:
            return False
        return (datetime.now(timezone.utc) - at).total_seconds() < within_seconds
