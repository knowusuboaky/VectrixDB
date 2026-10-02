"""The first drop: the files into Blob, and each collection's record into Cosmos DB.

    python 04_push_local_to_blob_cosmosdb.py
    python 04_push_local_to_blob_cosmosdb.py --check     # say what is missing, fetch nothing, send nothing
    python 04_push_local_to_blob_cosmosdb.py --again     # send even what is already there

Read it top to bottom. First SETTINGS, what this script reads and where it
sends it. Then the three steps, each under its own heading, and each with what
goes in, what comes out, and its code:

    STEP ONE: FETCH       gather     what the mirror is missing, from .local/sources.json
    STEP TWO: RECORDS     records    each collection's record, to Cosmos DB data_db/collection_records
    STEP THREE: FILES     files      the mirror's raw/ folder, to the ingestion container

Last the MAIN SCRIPT, which runs them in that order. The records go before the
files so a collection's rules are in place before its first file is read.

The mirror is laid out as the cloud is::

    .local/blob/data_db/ingestion/raw/financial/td/td-annual-report-2025.pdf    ->  ingestion container, raw/financial/td/...
    .local/blob/data_db/ingestion/raw/media/male.wav                            ->  ingestion container, raw/media/male.wav
    .local/cosmosdb/data_db/collection_records/financial.json            ->  data_db database, collection_records, collection.financial

so the folder a file sits in is the collection it lands in, and any folder
under that is only part of the document's name. Nothing is looked up and
nothing is configured. To send a file somewhere else, move it.

A collection's record is where its policy is written: who may retrieve from
it, by security group, by address or as everyone, checked on the server
before anything is searched. Beside the policy it holds who may see the
collection and whether identifiers in it are masked, which are changed on
the dashboard afterwards. Every server reads the same record, the
function app and the hosted API alike, so a policy set here is enforced by
whichever one answers.

``.local/later`` is the shelf, and nothing here sends it. Those files have not
been dropped yet, so the container does not hold them and the mirror does not
show them either. Step 07 moves them in and sends them, which is what fires
the trigger on a deployment that is already running.

This batch goes up before either app exists, so nothing reads it yet. The
events it makes wait on the queue until step 06 deploys the app that drains
them, and then the whole first drop is read at once.

A file the container already has at the same size is left alone, and a record
Cosmos already has is written only where it differs, so this is safe to run
again. Nothing under ``.local`` is committed but the records: the TD reports
are TD's to publish and the media came from a course.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

from _common import (
    INGESTION,
    LATER,
    LOCAL,
    RAW_FILES,
    RECORD_FILES,
    SOURCES,
    Az,
    arguments,
    begin,
    done,
    finish,
    folder_link,
    links,
    local_files,
    note,
    portal,
    push_raw,
    push_records,
    settings,
    size,
    skipped,
    step,
    stop,
    wants,
)

# ============================================================================
# SETTINGS: what this script reads, and where it sends it
# ============================================================================
#
# settings.env, read by _common.settings():
#   VX_STORAGE           the storage account whose ingestion container the files go to
#   VX_COLLECTIONS       the collections, each a folder under raw/: financial,media,misc
#   VX_COSMOS            yes: the records go to Cosmos DB. no: they stay in the mirror
#   VX_COSMOS_NAME       the Cosmos account whose data_db database holds the records
#
# The mirror, .local/:
#   sources.json                                        where each file comes from, and its collection
#   blob/data_db/ingestion/raw/<collection>/<client>/   the files that go up, laid out as the container is
#   cosmosdb/data_db/collection_records/                each collection's record, <collection>.json
#   later/                                              the shelf, which step 07 drops

#: Where each batch is fetched to. The first drop goes straight into the
#: mirror, because it is about to be in the container. The second waits on the
#: shelf, because a mirror showing a file the container has not got is a lie.
INTO = {"start": RAW_FILES, "later": LATER}
#: Who a record says wrote it last.
BY = "04_push_local_to_blob_cosmosdb.py"


# ============================================================================
# STEP ONE: FETCH
# ============================================================================


def fetch(url: str, into: Path) -> int:
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "vectrixdb-azure-example"})
    with urllib.request.urlopen(request, timeout=120) as reply:  # noqa: S310 - https, from sources.json
        data = reply.read()
    into.write_bytes(data)
    return len(data)


def gather(listed: Dict[str, Any], check: bool) -> List[str]:
    """Fill both batches from their addresses, and say what could not be had.

    INPUT
    -----
    ``.local/sources.json``: two lists, ``start`` and ``later``, each file with
    a ``url`` to download or a ``path`` on this machine, and the collection
    folder it belongs in::

        {"name": "male.wav", "path": "~/Downloads/.../male.wav", "collection": "media", "what": "speech"}

    OUTPUT
    ------
    The files, in the mirror (``start``) or on the shelf (``later``), and the
    names of those that could not be had. A file already there is left alone.
    """
    missing: List[str] = []
    for batch, root in INTO.items():
        step(".local/blob/data_db/ingestion/raw" if batch == "start" else ".local/later")
        for entry in listed.get(batch, []):
            # The folder is the route: collection, then client where there is one.
            where = str(entry.get("collection") or "").strip("/")
            folder = root / where if where else root
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / entry["name"]
            shown = target.relative_to(LOCAL).as_posix()
            if target.exists() and target.stat().st_size > 0:
                skipped(f"{shown}, {size(target.stat().st_size)}")
                continue
            if check:
                print(f"  need {entry['name']}: {entry.get('url') or entry.get('path')}")
                missing.append(entry["name"])
                continue
            if entry.get("url"):
                print(f"  ..   {entry['name']} from {entry['url'][:70]}")
                try:
                    count = fetch(entry["url"], target)
                except Exception as exc:  # noqa: BLE001 - somebody else's web server
                    print(f"  no   {entry['name']}: {type(exc).__name__}: {exc}")
                    missing.append(entry["name"])
                    continue
                done(f"{shown}, {size(count)}")
            else:
                source = Path(entry["path"]).expanduser()
                if not source.is_file():
                    print(f"  no   {entry['name']}: there is no {source}")
                    print("       put the file there, or change its path in .local/sources.json")
                    missing.append(entry["name"])
                    continue
                shutil.copy2(source, target)
                done(f"{shown}, {size(target.stat().st_size)}, copied")
            print(f"       {entry['what']}")
    return missing


# ============================================================================
# STEP TWO: RECORDS
# ============================================================================


def records(az: Az, config: Dict[str, str], again: bool) -> int:
    """Each collection's record, from the mirror to Cosmos DB. Returns how many were written.

    INPUT
    -----
    ``.local/cosmosdb/data_db/collection_records/<collection>.json``, one a
    collection, as a person writes it. A collection with no file gets one
    written from settings.env first, so the mirror shows what goes up::

        {"name": "financial",
         "path": "raw/financial/",
         "policy": {"method": "store", "allow": [{"email": "ama@company.com"}]}}

    OUTPUT
    ------
    One item a collection in the ``data_db`` database's ``collection_records``
    container, partitioned by kind, as the library's records keep it::

        {"id": "collection.financial", "kind": "collection", "key": "financial",
         "data": {"generation": "2026-09-23T10:15:00Z", "policy": {...},
                  "changed_by": "04_push_local_to_blob_cosmosdb.py", ...},
         "updated_at": "2026-09-23T10:15:00.204000Z"}

    A policy that differs from the file's is replaced by it: the file is
    the rule, and the dashboard edits the same record.
    """
    step(f"{len(local_files(RECORD_FILES)) or 'the'} records to Cosmos DB, data_db/collection_records")
    return push_records(az, config, by=BY, again=again)


# ============================================================================
# STEP THREE: FILES
# ============================================================================


def files(az: Az, config: Dict[str, str], again: bool) -> int:
    """The mirror's raw/ folder, to the ingestion container. Returns how many went up.

    INPUT
    -----
    Every file under ``.local/blob/data_db/ingestion/raw/<collection>/``, the path
    under ``raw/`` being the path it gets in the container.

    OUTPUT
    ------
    The blobs, ``raw/<collection>/<client>/<file>`` in the ingestion
    container, and one event each on the ingest queue, which nothing drains
    until step 06 deploys the app. A blob already there at the same size is
    left alone, unless ``--again``.
    """
    sent_files = local_files(RAW_FILES)
    if not sent_files:
        stop(f"There is nothing in {RAW_FILES}. Put a file in a collection folder there, or list it in .local/sources.json.")
    step(f"{len(sent_files)} files to {INGESTION}/raw")
    sent = push_raw(az, config, again=again)
    if az.pretend:
        note(f"{sent} would go up, each an event on the {INGESTION} queue" if sent else "nothing new: the container already holds this drop")
    elif not sent:
        note("nothing new, so the container already holds this drop")
    else:
        note(f"{sent} up, and each one has put an event on the {INGESTION} queue")
        note("nothing reads them yet: step 06 deploys the app that drains the queue")
    return sent


# ============================================================================
# MAIN SCRIPT
# ============================================================================


def options(parser) -> None:
    parser.add_argument("--check", action="store_true", help="say what is missing, fetch nothing, send nothing")
    parser.add_argument("--again", action="store_true", help="send even what is already there, and each record as its file says")


def main() -> int:
    args = arguments(__doc__, options)
    config = settings()
    az = Az(args.dry_run)
    account = config["VX_STORAGE"]
    begin("04", "The first drop", f"What .local is missing is fetched, the records go to Cosmos DB, then the files to the {INGESTION} container.")

    if not SOURCES.exists():
        stop(f"There is no {SOURCES}. It lists what goes in each batch.")
    listed: Dict[str, Any] = json.loads(SOURCES.read_text(encoding="utf-8"))

    missing = gather(listed, args.check)
    if missing:
        note(f"{len(missing)} still missing: {', '.join(missing)}")
        note("whatever is there goes up anyway, so a missing one is not the end")

    if args.check:
        finish(f"{len(local_files(RAW_FILES))} in the mirror, {len(missing)} still to fetch", "")
        return 0

    written = records(az, config, args.again)
    sent = files(az, config, args.again)

    links(
        ("the mirror on this machine", folder_link(LOCAL)),
        (f"the {INGESTION} container", portal("storage", account, config, page="containersList")),
        ("the queue, filling", portal("storage", account, config, page="queuesList")),
        ("the records, in Data Explorer", portal("cosmos", config["VX_COSMOS_NAME"], config, page="dataExplorer") if wants(config, "VX_COSMOS") else ""),
    )
    finish(
        (f"a dry run, so nothing was sent: {written} records would be written, {sent} files would go up, "
         if args.dry_run else f"{written} records written, {sent} files uploaded, ")
        + f"{len(local_files(RAW_FILES))} in the mirror, "
        f"{len(local_files(LATER))} waiting on the shelf",
        "05_create_extraction_function_app.py",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
