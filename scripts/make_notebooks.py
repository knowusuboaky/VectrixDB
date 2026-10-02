"""Generate examples/notebooks/*.ipynb from examples/*.py.

    python scripts/make_notebooks.py

The scripts are the source of truth (they run in the test suite); each
notebook is the same code split at the numbered comments and blank-line
paragraphs into cells, with the module docstring as the first Markdown cell.
Regenerate after editing a script; the test suite checks they agree.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path


# ============================================================================
# SETTINGS: where the examples are, and where the notebooks go
# ============================================================================
#
# examples/*.py in, examples/notebooks/*.ipynb out.

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
OUT = EXAMPLES / "notebooks"


# ============================================================================
# CELLS AND NOTEBOOKS: an example split, then written
# ============================================================================
#
# INPUT   an example's source; then its title, docstring and cells
# OUTPUT  the code split at the numbered comments and blank-line paragraphs; a
#         notebook with the docstring as its first Markdown cell
#
# The scripts are the source of truth, they run in the test suite; each
# notebook is the same code cut into cells, and the suite checks they agree.


def split_cells(source: str) -> tuple[str, list[str]]:
    tree = ast.parse(source)
    doc = ast.get_docstring(tree) or ""
    body_start = 0
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
    ):
        body_start = tree.body[0].end_lineno or 0
    lines = source.splitlines()[body_start:]
    code = "\n".join(lines).strip("\n")
    # Split on blank lines that precede a top-level statement or a "# N." comment.
    chunks: list[str] = []
    current: list[str] = []
    for line in code.splitlines():
        if current and (
            re.match(r"^# \d+\.", line) or (line and not line[0].isspace() and current[-1] == "")
        ):
            chunks.append("\n".join(current).strip("\n"))
            current = []
        current.append(line)
    if current:
        chunks.append("\n".join(current).strip("\n"))
    # Merge tiny fragments (a lone import or comment) into the next chunk.
    merged: list[str] = []
    for chunk in chunks:
        if merged and len(chunk.splitlines()) <= 1 and not chunk.startswith("# "):
            merged[-1] = merged[-1] + "\n\n" + chunk
        else:
            merged.append(chunk)
    return doc, [c for c in merged if c.strip()]


def notebook(title: str, doc: str, cells: list[str]) -> dict:
    md = f"# {title}\n\n" + doc.replace("Run it:", "Run it as a script with:")
    nb_cells = [{"cell_type": "markdown", "metadata": {}, "source": md}]
    for code in cells:
        nb_cells.append(
            {
                "cell_type": "code",
                "metadata": {},
                "execution_count": None,
                "outputs": [],
                "source": code,
            }
        )
    return {
        "cells": nb_cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   nothing
# OUTPUT  every notebook rewritten
#
# Regenerate after editing a script.


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args(argv)
    scripts = sorted(EXAMPLES.glob("*.py"))
    if not scripts:
        # examples/ is kept on the machine that runs it, not in the repository,
        # so a fresh checkout has none: nothing to make, and nothing is made.
        print(
            f"no example scripts in {EXAMPLES.relative_to(ROOT).as_posix()}/, so there are no notebooks to make"
        )
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for script in scripts:
        doc, cells = split_cells(script.read_text(encoding="utf-8"))
        title = script.stem.replace("_", " ").capitalize()
        target = OUT / f"{script.stem}.ipynb"
        target.write_text(
            json.dumps(notebook(title, doc, cells), indent=1) + "\n", encoding="utf-8"
        )
        print(f"{target.relative_to(ROOT)}: {len(cells)} code cells")
    return 0


if __name__ == "__main__":
    sys.exit(main())
