"""Retrieve through ``best_for_time``: the fastest setup within eight points of the one that finds the most.

    python 12_retrieve_best_for_time.py
    python 12_retrieve_best_for_time.py --question "How many people does the bank employ?"
    python 12_retrieve_best_for_time.py --dry-run        # says what it would ask, asks nothing

One of the three retrievals. The newest evaluation run names three setups
and picks none; this asks the questions through the one it called
``best_for_time``, signed in with a key of that name, so the Access page shows it
as a caller of its own. The work is in ``_retrieve.py``, the same for all
three, so comparing the scripts is comparing the setup and nothing else.

Run it against a dashboard on this machine, ``--port`` saying where it is
serving, after the evaluation of step nine has been run at least once.
"""

from __future__ import annotations

from _common import arguments
from _retrieve import options, retrieve

#: This step, and the pick it asks through, as the run names it.
STEP, PICK = "12", "best_for_time"


def main() -> int:
    args = arguments(__doc__, options)
    return retrieve(
        PICK,
        port=args.port,
        limit=args.limit,
        questions=args.question,
        dry_run=args.dry_run,
        which=args.collection,
        number=STEP,
    )


if __name__ == "__main__":
    raise SystemExit(main())
