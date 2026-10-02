"""Run the README's first example against whatever VectrixDB is installed.

The wheel job already installs the wheel alone into an empty environment and
searches, which is the check that catches an undeclared dependency: psutil was
imported at module scope and declared nowhere, and creating a collection in an
environment that had only the five real dependencies is what finds that.

What it ran was a snippet written into the workflow, which is a copy of the
README that nothing keeps in step. This runs the README's own first example
instead, so the thing a new reader types is the thing CI proved works.

Usage, from a checkout, with the wheel installed into the interpreter running
this rather than the source tree on the path::

    python scripts/readme_smoke.py

It prints what it executed, then executes it. A README whose opening example
does not run on a clean install is a broken front door.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path


# ============================================================================
# SETTINGS: where the README is
# ============================================================================
#
# The README at the root of the checkout, whose first example is the thing a
# new reader types.

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"


# ============================================================================
# THE FIRST PYTHON FENCE
# ============================================================================
#
# INPUT   the README's text
# OUTPUT  its first ```python block, which is the quick start
#
# The example is taken from the README itself, not from a copy in a workflow
# that nothing keeps in step.


def first_python_fence(text: str) -> str:
    """The first ```python block, which is the quick start."""
    fences = re.findall(r"```python\n(.*?)```", text, re.S)
    if not fences:
        raise SystemExit("README.md has no python fence; the quick start has gone missing")
    return fences[0]


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   nothing; run with the wheel installed into this interpreter rather
#         than the source tree on the path
# OUTPUT  the snippet printed, then executed; exit 1 if it fails
#
# The wheel job installs the wheel alone into an empty environment, which is
# what catches an undeclared dependency. A README whose opening example does
# not run on a clean install is a broken front door.


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args(argv)
    if not README.exists():
        raise SystemExit(f"no README at {README}")

    source = first_python_fence(README.read_text(encoding="utf-8"))
    print("--- README.md, first example ---")
    print(source)
    print("--- running it ---")

    # In a temporary directory, because the example writes a collection and a
    # CI checkout is not the place for it. ignore_cleanup_errors because the
    # collection holds a SQLite handle and Windows will not unlink an open
    # file: the example already ran by then, so a cleanup failure is not a
    # result worth failing the build over.
    import os

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workdir:
        namespace: dict = {"__name__": "__readme__"}
        here = os.getcwd()
        os.chdir(workdir)
        try:
            exec(compile(source, "README.md#quickstart", "exec"), namespace)  # noqa: S102
        finally:
            for value in namespace.values():
                close = getattr(value, "close", None)
                if callable(close) and type(value).__name__ == "Vectrix":
                    try:
                        close()
                    except Exception:  # pragma: no cover - best effort
                        pass
            os.chdir(here)

    import vectrixdb

    print(f"--- ok, against vectrixdb {vectrixdb.__version__} from {vectrixdb.__file__} ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
