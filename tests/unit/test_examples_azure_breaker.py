"""The circuit breaker for the extraction app: closed, open, one probe, and the state in words.

What is being held to. Below the threshold every message is tried. At the
threshold the breaker opens, and allow() says no for the window. Once the
window has passed one probe goes through; a probe that reads closes it and
clears the count; a probe that fails opens it again for another window. A
success with nothing recorded is a no-op. Only a read that could not reach
the app counts: a file's own fault does not. The state is one file every
instance reads; no Azure, the blobs are a stand-in.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

MAIN_FUNCTION_APP = Path(__file__).resolve().parents[2] / "examples" / "azure" / "main_function_app"
if not MAIN_FUNCTION_APP.exists():
    pytest.skip("examples/ is kept on the machine that runs it, not in the repository", allow_module_level=True)



@pytest.fixture
def br(monkeypatch):
    monkeypatch.setenv("INGEST_COLLECTIONS", "financial")
    monkeypatch.syspath_prepend(str(MAIN_FUNCTION_APP))
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    sys.modules.pop("breaker", None)
    import breaker

    from test_documents import FakeBlobService

    clock = types.SimpleNamespace(now=1000.0)
    blobs = FakeBlobService()
    made = breaker.Breaker(breaker.state_files(blobs), "extraction app", threshold=3, open_for=60, clock=lambda: clock.now)
    return types.SimpleNamespace(module=breaker, breaker=made, clock=clock, blobs=blobs)


def test_it_opens_at_the_threshold_and_lets_one_probe_through_after_the_window(br):
    b = br.breaker
    assert b.allow() and b.status()["paused"] is False and "is answering" in b.status()["said"]
    b.record_failure("/extract/pdf could not be reached: refused")
    b.record_failure("/extract/pdf could not be reached: refused")
    assert b.allow(), "two in a row is not yet a pattern"
    b.record_failure("/extract/pdf answered 503 for a.pdf")
    assert not b.allow() and ("ingestion", "state/extraction-app.json") in br.blobs.blobs
    said = b.status()
    assert said["paused"] is True and said["failures"] == 3 and said["since"] == "1970-01-01T00:16:40Z" and said["until"] == "1970-01-01T00:17:40Z"
    assert "has not answered since 1970-01-01T00:16:40Z, 3 reads in a row; the next try is at 1970-01-01T00:17:40Z" in said["said"]
    br.clock.now += 30
    assert not b.allow(), "still inside the window"
    br.clock.now += 31
    assert b.allow(), "the window has passed: one probe"
    assert not b.allow(), "and only one"
    assert b.status()["probing"] is True


def test_a_probe_that_reads_closes_it_and_one_that_fails_opens_it_again(br):
    b = br.breaker
    for _ in range(3):
        b.record_failure("timed out")
    br.clock.now += 61
    assert b.allow()
    assert b.record_success() is True and b.allow() and b.status() == {"dependency": "extraction app", "paused": False, "probing": False, "since": None, "until": None, "failures": 0, "last_error": None, "said": "the extraction app is answering"}
    assert b.record_success() is False, "nothing to clear"
    for _ in range(3):
        b.record_failure("timed out")
    br.clock.now += 61
    assert b.allow()
    b.record_failure("timed out again")
    assert not b.allow() and b.status()["failures"] == 4 and b.status()["since"] == "1970-01-01T00:18:42Z", "open again, from the probe's failure"
    br.clock.now += 61
    assert b.allow(), "and the next window gives another probe"


def test_a_probe_an_instance_died_with_is_not_a_lock(br):
    b = br.breaker
    for _ in range(3):
        b.record_failure("timed out")
    br.clock.now += 61
    assert b.allow() and not b.allow()
    br.clock.now += 61
    assert b.allow(), "a probe that never reported back gives way to another after a window"


@pytest.mark.parametrize("error, transient", [
    ("/extract/pdf could not be reached: [Errno 111] Connection refused", True),
    ("/extract/pdf answered 503 for a.pdf: busy", True),
    ("/extract/pdf answered 429 for a.pdf", True),
    ("read timed out", True),
    ("/extract/pdf answered 500 for a.pdf: model not loaded", False),
    ("the reply for a.pdf has no text", False),
    ("", False),
])
def test_only_a_read_that_could_not_reach_the_app_counts(br, error, transient):
    assert br.module.is_transient(error) is transient
