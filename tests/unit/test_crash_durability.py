"""Kill the process mid-write, reopen, nothing is corrupt.

Reopen tests show that a clean close survives. This is the other case: the
process dies between an add and the next one, with no chance to close. SQLite
in WAL mode promises the database; the index file has to be written atomically
for the promise to hold there too.
"""

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vectrixdb import Vectrix

CHILD = r"""
import sys
from vectrixdb import Vectrix
db = Vectrix("crash", path=sys.argv[1])
i = 0
while True:
    db.add(f"document number {i} about topic {i % 5}")
    i += 1
    print(i, flush=True)
"""


def _run_and_kill(path: Path, seconds: float) -> int:
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    reported = 0
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            reported = int(line.strip())
    finally:
        proc.kill()
        # What it wrote between the last line read and the kill is still in the pipe: a
        # line for every add that was finished, so the count is held to what was done.
        for line in proc.stdout:
            if line.strip().isdigit():
                reported = int(line.strip())
        proc.wait()
    return reported


@pytest.mark.slow
def test_kill_mid_write_leaves_a_consistent_store(tmp_path):
    reported = _run_and_kill(tmp_path, seconds=6.0)
    assert reported >= 3, "the child did not get far enough to mean anything"

    for db_file in tmp_path.rglob("*.db"):
        con = sqlite3.connect(str(db_file))
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok", db_file.name
        con.close()

    db = Vectrix("crash", path=str(tmp_path))
    count = db.count()
    assert reported - 1 <= count <= reported + 1, f"reported {reported}, found {count}"
    assert db.search("topic 2", limit=3), "the index was lost or unreadable"

    # Still writable, and the rebuild reconciles anything the kill left behind.
    db.add("one more after the crash")
    assert db.rebuild_index() == db.count()
    assert db.search("after the crash", limit=1).top.text == "one more after the crash"
