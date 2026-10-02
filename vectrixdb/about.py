"""Which VectrixDB a server runs, under what licence, and the notice that travels with it.

Apache 2.0 asks that whoever passes the work on passes the licence and the
NOTICE file with it. Both ship inside the package, in its metadata, and a
server shows them to its admins under About in the account menu, beside the
version. A branded dashboard names VectrixDB nowhere else a stranger can see,
so this is where the software's own name, version and terms are kept in view.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
from typing import Optional

__all__ = [
    "LICENCE",
    "LICENCE_LINE",
    "about",
    "licence_text",
]


# ============================================================================
# SETTINGS: the licence
# ============================================================================
#
# The licence by its SPDX name, and the line the About page says it in.

LICENCE = "Apache-2.0"
LICENCE_LINE = "Licensed under the Apache License, Version 2.0."


# ============================================================================
# THE FILES, AND WHAT ABOUT SAYS
# ============================================================================
#
# INPUT   a file's name, LICENSE or NOTICE
# OUTPUT  its text, from the source tree when this is a checkout, or from the
#         installed package's metadata; the version, the licence and the notice
#
# A checkout reads the files beside the package, so the page shows what is
# being edited; an install reads the ones the wheel carried.


def _text(name: str) -> Optional[str]:
    here = Path(__file__).resolve().parents[1]
    if (here / "pyproject.toml").is_file() and (here / name).is_file():
        return (here / name).read_text(encoding="utf-8")
    try:
        dist = metadata.distribution("vectrixdb")
    except metadata.PackageNotFoundError:
        return None
    # Newer wheels keep licence files under licenses/, older ones beside METADATA.
    for inside in (f"licenses/{name}", name):
        text = dist.read_text(inside)
        if text:
            return text
    return None


def licence_text() -> Optional[str]:
    """The Apache License, Version 2.0, as the package carries it."""
    return _text("LICENSE")


def about() -> dict:
    """The version, the licence and the notice, for the About page."""
    from . import __version__

    return {
        "name": "VectrixDB",
        "version": __version__,
        "licence": LICENCE,
        "licence_line": LICENCE_LINE,
        "notice": _text("NOTICE") or "",
    }
