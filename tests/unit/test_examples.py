"""The examples run, offline, from a clean directory, and the notebooks
match the scripts they were generated from."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = sorted((ROOT / "examples").glob("*.py"))


@pytest.mark.parametrize("script", EXAMPLES, ids=[s.stem for s in EXAMPLES])
def test_example_runs_offline(script, tmp_path):
    env = {**os.environ, "VECTRIXDB_OFFLINE": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip(), "an example should print what it did"
    assert (tmp_path / "example_data").exists()


def test_notebooks_are_generated_from_the_scripts(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import make_notebooks

    for script in EXAMPLES:
        nb_path = ROOT / "examples" / "notebooks" / f"{script.stem}.ipynb"
        assert nb_path.exists(), f"run scripts/make_notebooks.py: {nb_path.name} is missing"
        nb = json.loads(nb_path.read_text(encoding="utf-8"))
        code = "\n".join(c["source"] for c in nb["cells"] if c["cell_type"] == "code")
        _, cells = make_notebooks.split_cells(script.read_text(encoding="utf-8"))
        assert code == "\n".join(cells), f"{nb_path.name} is stale; run scripts/make_notebooks.py"
        assert nb["cells"][0]["cell_type"] == "markdown"


def test_with_no_examples_the_notebook_script_makes_nothing(tmp_path, monkeypatch, capsys):
    """examples/ is not in the repository, and the script made an empty examples/notebooks in a fresh checkout."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import make_notebooks

    monkeypatch.setattr(make_notebooks, "ROOT", tmp_path)
    monkeypatch.setattr(make_notebooks, "EXAMPLES", tmp_path / "examples")
    monkeypatch.setattr(make_notebooks, "OUT", tmp_path / "examples" / "notebooks")
    assert make_notebooks.main([]) == 0
    assert not (tmp_path / "examples").exists(), "nothing is made"
    assert "no example scripts in examples/" in capsys.readouterr().out
