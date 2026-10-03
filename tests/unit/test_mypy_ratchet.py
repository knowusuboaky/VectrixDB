"""The ratchet script: parse mypy's summary, compare, decide."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mypy_ratchet.py"
spec = importlib.util.spec_from_file_location("mypy_ratchet", SCRIPT)
ratchet = importlib.util.module_from_spec(spec)
sys.modules["mypy_ratchet"] = ratchet
spec.loader.exec_module(ratchet)


class TestParse:
    def test_error_count(self):
        assert (
            ratchet.parse_count("...\nFound 517 errors in 45 files (checked 78 source files)\n")
            == 517
        )

    def test_single_error(self):
        assert ratchet.parse_count("Found 1 error in 1 file (checked 2 source files)") == 1

    def test_clean_run(self):
        assert ratchet.parse_count("Success: no issues found in 78 source files") == 0

    def test_garbage_is_an_error_not_a_zero(self):
        with pytest.raises(RuntimeError):
            ratchet.parse_count("mypy: command not found")


class TestDecide:
    def test_up_fails(self):
        status, message = ratchet.decide(10, 8)
        assert status == 1 and "2 new" in message

    def test_down_passes_and_asks_to_lock_in(self):
        status, message = ratchet.decide(5, 8)
        assert status == 0 and "--update" in message

    def test_equal_passes(self):
        assert ratchet.decide(8, 8)[0] == 0


class TestBaselineFile:
    def test_it_exists_and_is_a_number(self):
        baseline = SCRIPT.parent.parent / ".mypy-baseline"
        assert baseline.exists(), "run python scripts/mypy_ratchet.py --update"
        assert int(baseline.read_text().strip()) >= 0
