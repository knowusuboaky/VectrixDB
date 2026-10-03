"""The evaluation runs the library saved, read for the Evaluate pages.

Nothing here runs a search. A run is made where the golden questions live:
from Python, from ``vectrixdb evaluate``, or from a function that watches
the golden file. It is saved as ``retrieval/runs/<id>/report.json`` in a
store, a chunking run as ``chunking/runs/<id>/report.json`` beside it, and
the server only lists and reads that store. A run saved before 2.2, at
``runs/<id>/report.json``, is still read.

The store is ``VECTRIXDB_EVALUATIONS``: a folder, ``s3://bucket/prefix``,
or a Blob container's address, ``https://<account>.blob.core.windows.net/
<container>/<prefix>``. Left unset it is the ``evaluations`` folder under
the server's data path, which is where ``vectrixdb evaluate`` saves when it
is pointed at the same path.

A run holds how every setup scored, never the questions' text or the
documents', so a person who may see a collection's health may see this. The
golden file a run used is kept beside the runs once a version, and that has
the questions' text: it is downloaded by a signed-in admin, as a person, and
by nobody else, a key included.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

__all__ = ["EVALUATIONS_ENV", "router"]


# ============================================================================
# SETTINGS: the router, where runs are, and who may download
# ============================================================================
#
# The setting that names where runs are read from, and the roles that may
# download a golden file.

router = APIRouter()

EVALUATIONS_ENV = "VECTRIXDB_EVALUATIONS"

#: The ways a person signs in. A key and a guest are not people.
_PEOPLE = ("oidc", "email", "passkey")


# ============================================================================
# WHERE THE RUNS ARE
# ============================================================================
#
# INPUT   the setting, or the data path
# OUTPUT  the server module, imported late because it imports this one at app
#         build; where runs are read from; the runs' store, evaluation or
#         chunking
#
# One store answers for both kinds of run, the chunking runs kept beside the
# retrieval ones.


def _server():
    """The server module, imported late: it imports this one at app build."""
    from . import server

    return server


def _where() -> Optional[str]:
    """Where runs are read from: the setting, or the data path's own folder."""
    configured = os.environ.get(EVALUATIONS_ENV, "").strip()
    if configured:
        return configured
    try:
        base = getattr(_server().get_db(), "path", None)
    except RuntimeError:  # no database yet
        base = None
    return str(Path(base) / "evaluations") if base else None


def _store(chunking: bool = False) -> Any:
    """The runs' store: evaluation runs, or with ``chunking`` the chunking runs kept beside them."""
    from ..evaluation import chunking_store, report_store

    where = _where()
    if where is None:
        return None
    try:
        return chunking_store(where) if chunking else report_store(where)
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ============================================================================
# THE ROUTES: runs listed, one run, its golden file
# ============================================================================
#
# INPUT   a limit and an offset; a run's id; the request
# OUTPUT  every saved run newest first; one run's report with the picks and
#         the rules they were chosen by; the golden file exactly as it was
#         read, for a signed-in admin as a person; the same three for chunking
#         runs
#
# Nothing here runs a search. A run is made where the golden questions live:
# from Python, from vectrixdb evaluate, or from a function that watches the
# golden file. The server's own key holds the admin role too, but a key is a
# script, so a download needs a person.


@router.get("/api/v1/evaluations", tags=["evaluations"])
async def list_evaluations(
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)
) -> Dict[str, Any]:
    """Every saved run, newest first: when, which golden file, which collections it searched, and each setup's top 10 count.

    ``offset`` skips that many of the newest, for the next page. The full
    report of one run is ``/api/v1/evaluations/{run}``, with ``latest`` for
    the newest.
    """
    store = _store()
    if store is None:
        return {"ok": True, "data": {"runs": [], "where": None}}
    runs = await run_in_threadpool(store.history, limit, offset)
    return {"ok": True, "data": {"runs": runs, "where": _describe(store)}}


def _may_download(request: Request) -> bool:
    """A signed-in admin, as a person: the server's own key holds the role too, and a key is a script."""
    from .signin import caller_of

    caller = caller_of(request)
    return caller is not None and caller.method in _PEOPLE and caller.can("evaluation.golden")


async def _report(store: Any, run: str, none: str = "No evaluation runs yet") -> Dict[str, Any]:
    if store is None:
        raise HTTPException(status_code=404, detail=none)
    try:
        report: Dict[str, Any] = await run_in_threadpool(store.get, run)
    except KeyError:
        raise HTTPException(
            status_code=404, detail=none if run == "latest" else f"No run '{run}'"
        ) from None
    return report


async def _golden_file(store: Any, report: Mapping[str, Any]) -> Response:
    sha = str((report.get("golden") or {}).get("sha256") or "")
    try:
        data = await run_in_threadpool(store.golden, sha)
    except KeyError:
        raise HTTPException(
            status_code=404, detail="This run's golden questions were not kept"
        ) from None
    return Response(
        content=data,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="golden-{sha[:8]}.jsonl"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/api/v1/evaluations/{run}", tags=["evaluations"])
async def get_evaluation(run: str, request: Request) -> Dict[str, Any]:
    """One run's report: every setup's numbers, the three picks and the rules they were chosen by.

    ``golden_download`` says whether the caller can download the golden
    questions this run used: a signed-in admin, when the file was kept.
    ``missing_notes`` says in words what ``missing`` holds, each expected
    document an index does not hold, the way the command line says it.
    """
    from ..evaluation import _missing_message

    store = _store()
    report = dict(await _report(store, run))
    sha = str((report.get("golden") or {}).get("sha256") or "")
    report["golden_download"] = bool(
        _may_download(request) and await run_in_threadpool(store.has_golden, sha)
    )
    report["missing_notes"] = [
        _missing_message(str(name), gone) for name, gone in (report.get("missing") or {}).items()
    ]
    return {"ok": True, "data": report}


@router.get("/api/v1/evaluations/{run}/golden", tags=["evaluations"])
async def download_golden(run: str, request: Request) -> Response:
    """The golden file a run used, exactly as it was read: every question, reference answer and expected id.

    A signed-in admin only, as a person. The file is the one whose
    fingerprint the run names, so an old run gives its own questions after
    the file has changed. A run saved before files were kept, or made from
    questions in memory, has none.
    """
    if not _may_download(request):
        raise HTTPException(
            status_code=403,
            detail="Only an admin signed in as a person can download the golden questions",
        )
    store = _store()
    return await _golden_file(store, await _report(store, run))


_NO_CHUNKING = "No chunking runs yet"


@router.get("/api/v1/chunking", tags=["evaluations"])
async def list_chunking(
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)
) -> Dict[str, Any]:
    """Every saved chunking run, newest first: when, which golden file, which collections it cut, and each technique's count.

    Kept beside the retrieval runs, as ``chunking/runs/<id>/report.json`` in
    the same store, and made where the golden questions live, by
    ``compare_chunking`` or a function that runs it. The full report of one
    is ``/api/v1/chunking/{run}``, with ``latest`` for the newest.
    """
    store = _store(chunking=True)
    if store is None:
        return {"ok": True, "data": {"runs": [], "where": None}}
    runs = await run_in_threadpool(store.history, limit, offset)
    return {"ok": True, "data": {"runs": runs, "where": _describe(store)}}


@router.get("/api/v1/chunking/{run}", tags=["evaluations"])
async def get_chunking(run: str, request: Request) -> Dict[str, Any]:
    """One chunking run: every build, each technique at its best, the head to head between them, and the pick.

    ``golden_download`` says whether the caller can download the golden
    questions the run used, as for an evaluation run.
    """
    store = _store(chunking=True)
    report = dict(await _report(store, run, _NO_CHUNKING))
    sha = str((report.get("golden") or {}).get("sha256") or "")
    report["golden_download"] = bool(
        _may_download(request) and await run_in_threadpool(store.has_golden, sha)
    )
    return {"ok": True, "data": report}


@router.get("/api/v1/chunking/{run}/golden", tags=["evaluations"])
async def download_chunking_golden(run: str, request: Request) -> Response:
    """The golden file a chunking run used, exactly as it was read. A signed-in admin only, as a person."""
    if not _may_download(request):
        raise HTTPException(
            status_code=403,
            detail="Only an admin signed in as a person can download the golden questions",
        )
    store = _store(chunking=True)
    return await _golden_file(store, await _report(store, run, _NO_CHUNKING))


def _describe(store: Any) -> str:
    files = getattr(store, "files", None)
    describe = getattr(files, "describe", None)
    return str(describe()) if callable(describe) else str(files)
