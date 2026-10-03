"""Retrieval quality must not drop without someone deciding it may.

tests/fixtures/recall_baseline.json records MRR@10 and recall@1 on the shared
question set. A change that lowers them beyond the tolerance fails here; to
accept a deliberate trade-off, rerun scripts/recall_baseline.py --update and
say why in the change.
"""

import importlib.util
import json
import platform
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "tests" / "fixtures" / "recall_baseline.json"
SCRIPT = ROOT / "scripts" / "recall_baseline.py"
# The baseline is measured on x86. ARM's ONNX kernels round differently and land
# about a hundredth lower on the same questions, so it is held a hundredth wider.
TOLERANCE = 0.03 if platform.machine().lower() in {"arm64", "aarch64"} else 0.02


def _script():
    spec = importlib.util.spec_from_file_location("recall_baseline", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["recall_baseline"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def recorded():
    assert BASELINE.exists(), "run python scripts/recall_baseline.py --update"
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_dense_holds_its_baseline(recorded):
    script = _script()
    now = script.measure("dense", script.load_pairs())
    assert now["mrr"] >= recorded["dense"]["mrr"] - TOLERANCE, (now, recorded["dense"])
    assert now["recall1"] >= recorded["dense"]["recall1"] - TOLERANCE


@pytest.mark.slow
def test_hybrid_holds_its_baseline(recorded):
    if "hybrid" not in recorded:
        pytest.skip("no hybrid baseline recorded")
    script = _script()
    now = script.measure("hybrid", script.load_pairs())
    assert now["mrr"] >= recorded["hybrid"]["mrr"] - TOLERANCE, (now, recorded["hybrid"])
