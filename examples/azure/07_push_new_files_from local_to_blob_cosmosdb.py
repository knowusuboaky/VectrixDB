"""Drop what was held back, and send whatever else Blob and Cosmos DB have not got.

    python "07_push_new_files_from local_to_blob_cosmosdb.py"
    python "07_push_new_files_from local_to_blob_cosmosdb.py" --again   # send what is already there again

Read it top to bottom. First SETTINGS, what this script reads and where it
sends it. Then the three steps, each under its own heading, and each with what
goes in, what comes out, and its code:

    STEP ONE: THE SHELF   off_the_shelf   what step 04 held back, moved into the mirror
    STEP TWO: RECORDS     records         each collection's record, to Cosmos DB data_db/collection_records
    STEP THREE: FILES     files           whatever the ingestion container has not got

Last the MAIN SCRIPT, which runs them in that order. The records go before the
files, because the app is running now: a file is read the moment it lands, and
its collection's rules have to be there first.

This is the second batch, and it is the one that shows the trigger working.
The first batch went up before the apps were there; these files land on a
deployment that is already running.

``.local/later`` is the shelf: files fetched by step 04 and deliberately not
sent, so the container does not hold them and the mirror does not show them
either. This script moves them into ``.local/blob/data_db/ingestion/raw`` and sends
them, and after that the mirror is the container again.

Anything else you have put in the mirror by hand goes up in the same pass,
because what is sent is whatever the cloud has not got. That is all "new"
means here: the mirror and the cloud are compared, and the difference
travels. A policy edited in ``.local/cosmosdb/data_db/collection_records/``
reaches every server from here, the function app and the hosted API alike,
without either being published again.

Where a file sits is where it goes: ``.local/blob/data_db/ingestion/raw/financial/td/
x.pdf`` is uploaded as ``raw/financial/td/x.pdf``, and the function reads the
collection from that first folder and the client from the second. Nothing is
looked up and nothing is configured; to send a file somewhere else, move it.

Nothing calls the function. A blob is written, Event Grid notices, a message
lands on the queue, and the function wakes on its own. That is the whole point
of dropping these separately: by then you have walked away, and the file is
read anyway.

Two batches also mean two builds, which is what puts a second bar on the
chunks by build chart. One build draws no chart.

A file already there at the same size is skipped, and a record the same as its
file is left alone, so this is safe to run again. To send them anyway, pass
--again.
"""

from __future__ import annotations

import shutil
from typing import Dict

from _common import (
    INGESTION,
    LATER,
    LOCAL,
    RAW_FILES,
    RECORD_FILES,
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
#   later/                                              the shelf, which this drops
#   blob/data_db/ingestion/raw/<collection>/<client>/   the files, laid out as the container is
#   cosmosdb/data_db/collection_records/                each collection's record, <collection>.json

#: Who a record says wrote it last.
BY = "07_push_new_files_from local_to_blob_cosmosdb.py"


# ============================================================================
# STEP ONE: THE SHELF
# ============================================================================


def off_the_shelf(pretend: bool) -> int:
    """Move what was held back into the mirror, so the mirror tells the truth after the push.

    INPUT
    -----
    Every file under ``.local/later/<collection>/``, fetched by step 04 and
    held back on purpose.

    OUTPUT
    ------
    The same files under ``.local/blob/data_db/ingestion/raw/<collection>/``, and how
    many moved, or would in a dry run. One the mirror already has is left on the shelf, and said so.
    """
    waiting = local_files(LATER)
    if not waiting:
        return 0
    step(f"{len(waiting)} off the shelf")
    moved = 0
    for path in waiting:
        inside = path.relative_to(LATER)
        target = RAW_FILES / inside
        if target.exists():
            note(
                f"{inside.as_posix()} is in the mirror already, so the one on the shelf is left where it is"
            )
            continue
        if pretend:
            print(f"  would move .local/later/{inside.as_posix()} into the mirror")
            moved += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
        done(f"{inside.as_posix()}, {size(target.stat().st_size)}")
        moved += 1
    return moved


# ============================================================================
# STEP TWO: RECORDS
# ============================================================================


def records(az: Az, config: Dict[str, str], again: bool) -> int:
    """Each collection's record, from the mirror to Cosmos DB, where it differs. Returns how many were written.

    INPUT
    -----
    ``.local/cosmosdb/data_db/collection_records/<collection>.json``, one a
    collection, the same files step 04 sent: the policy, who may see the
    collection, and whether it is masked.

    OUTPUT
    ------
    The ``collection.<name>`` items in ``data_db/collection_records``, a policy
    changed in its file now the one every server enforces. The servers hold a
    record for up to thirty seconds, so a change is everywhere within that.
    Who may see a collection and its masking stay as the dashboard left them,
    unless ``--again``.
    """
    step(
        f"{len(local_files(RECORD_FILES)) or 'the'} records to Cosmos DB, data_db/collection_records"
    )
    return push_records(az, config, by=BY, again=again)


# ============================================================================
# STEP THREE: FILES
# ============================================================================


def files(az: Az, config: Dict[str, str], again: bool, pretend: bool) -> int:
    """Whatever the ingestion container has not got, from the mirror. Returns how many went up.

    INPUT
    -----
    Every file under ``.local/blob/data_db/ingestion/raw/<collection>/``: what came
    off the shelf, and anything put there by hand.

    OUTPUT
    ------
    The blobs the container did not have, ``raw/<collection>/<client>/<file>``,
    and an event each on the ingest queue, which wakes the running app. A blob
    already there at the same size is left alone, unless ``--again``. A dry
    run plans what is still on the shelf as well, since the real run moves it
    in before it sends.
    """
    in_mirror = local_files(RAW_FILES)
    shelf = (
        [
            (path, path.relative_to(LATER).as_posix())
            for path in local_files(LATER)
            if not (RAW_FILES / path.relative_to(LATER)).exists()
        ]
        if pretend
        else []
    )
    if not in_mirror and not shelf:
        if pretend:
            note(
                f"there is nothing in {RAW_FILES} yet, so there is no plan to show. Run 04_push_local_to_blob_cosmosdb.py first."
            )
            return 0
        stop(f"There is nothing in {RAW_FILES}. Run 04_push_local_to_blob_cosmosdb.py first.")
    step(
        f"{len(in_mirror) + len(shelf)} in the mirror"
        + (f", {len(shelf)} of them once off the shelf" if shelf else "")
    )
    sent = push_raw(az, config, again=again, also=shelf)
    if pretend:
        note(
            f"{sent} would go up, each an event on the queue that wakes the running app"
            if sent
            else "nothing new, so no event would fire"
        )
    elif not sent:
        note("nothing new, so no event fired and the function will not wake")
    else:
        note(f"{sent} events are on their way to the queue now")
        note(
            "the long report takes minutes: it is read, cut, every picture described, then embedded"
        )
    return sent


# ============================================================================
# MAIN SCRIPT
# ============================================================================


def options(parser) -> None:
    parser.add_argument(
        "--again",
        action="store_true",
        help="upload even what is already there, and each record as its file says",
    )


def main() -> int:
    args = arguments(__doc__, options)
    config = settings()
    az = Az(args.dry_run)
    account = config["VX_STORAGE"]
    begin(
        "07",
        "The files that come later",
        f"The shelf moves into the mirror, the records go to Cosmos DB, and whatever {INGESTION} has not got goes up.",
    )

    moved = off_the_shelf(args.dry_run)
    written = records(az, config, args.again)
    sent = files(az, config, args.again, args.dry_run)

    links(
        (f"the {INGESTION} container", portal("storage", account, config, page="containersList")),
        ("the queue, filling and draining", portal("storage", account, config, page="queuesList")),
        (
            "the records, in Data Explorer",
            portal("cosmos", config["VX_COSMOS_NAME"], config, page="dataExplorer")
            if wants(config, "VX_COSMOS")
            else "",
        ),
        ("the mirror on this machine", folder_link(LOCAL)),
    )
    if args.dry_run:
        finish(
            f"a dry run, so nothing moved: {moved} would come off the shelf, {written} records would be written, {sent} files would go up to {INGESTION}"
        )
    else:
        finish(
            f"{written} records written, {sent} files uploaded to {INGESTION}, {len(local_files(LATER))} left on the shelf"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
