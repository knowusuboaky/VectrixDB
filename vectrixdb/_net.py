"""One switch for everything that would touch the network.

The package's promise is that it works offline, so nothing may fetch anything
unless a person asked. Two environment variables draw the line:

``VECTRIXDB_OFFLINE=1``
    Refuses every download, explicit ones included. For air-gapped hosts and
    for CI, where a surprise fetch is a flaky test waiting to happen.

``VECTRIXDB_AUTO_DOWNLOAD=1``
    Permits the *implicit* downloads: a model fetched because first use found
    it missing, a spaCy model pulled while an extractor was being built. Off by
    default. The explicit paths (``vectrixdb download-models``,
    ``download_models()``) do not need it.

Every download site in the package goes through here, so the answer to "did
this touch the network?" has one place to be decided.
"""

from __future__ import annotations

import os

from .exceptions import ModelDownloadError


# ============================================================================
# SETTINGS: what counts as true
# ============================================================================
#
# The spellings of yes the two environment variables accept.

_TRUE = ("1", "true", "yes", "on")


# ============================================================================
# THE SWITCH: offline, allowed, asserted, refused
# ============================================================================
#
# INPUT   VECTRIXDB_OFFLINE and VECTRIXDB_AUTO_DOWNLOAD
# OUTPUT  whether every download is refused; whether a first-use download is
#         permitted; an error naming the command to run when one was not asked
#         for
#
# The package's promise is that it works offline, so nothing may fetch
# anything unless a person asked.


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def offline() -> bool:
    """Every download is refused."""
    return _flag("VECTRIXDB_OFFLINE")


def auto_download_allowed() -> bool:
    """Implicit, first-use downloads are permitted."""
    return not offline() and _flag("VECTRIXDB_AUTO_DOWNLOAD")


def assert_network(what: str) -> None:
    """Raise unless an explicit download may proceed.

    ``what`` completes the sentence "Refusing to ...", so pass a verb phrase:
    ``assert_network("download the dense model")``.
    """
    if offline():
        raise ModelDownloadError(
            f"Refusing to {what}: VECTRIXDB_OFFLINE is set. The bundled models "
            "work without a network. Unset it, or run the download on a machine "
            "that has one and copy the models directory across."
        )


def refuse_implicit(what: str, command: str) -> ModelDownloadError:
    """The error for a first-use download that was not asked for.

    Returned rather than raised so a caller can attach it as a cause.
    """
    return ModelDownloadError(
        f"{what} is not on this machine and VectrixDB does not download on first "
        f"use unless asked. Fetch it explicitly with `{command}`, or set "
        "VECTRIXDB_AUTO_DOWNLOAD=1 to allow first-use downloads."
    )


# ============================================================================
# POSTING JSON
# ============================================================================
#
# INPUT   a url, a payload and a timeout
# OUTPUT  the JSON reply, with the standard library alone
#
# No requests dependency for one call.


def post_json(url: str, payload: dict, timeout: float = 120.0) -> dict:
    """POST a JSON body and read the JSON reply, with the standard library alone.

    For a service somebody pointed the package at, a local Ollama say. That
    is a call they asked for, not a download, so VECTRIXDB_OFFLINE does not
    stop it. A reply of 400 or more raises urllib's HTTPError, which names
    the status. The package declares no HTTP client, so none may be imported.
    """
    import json
    import urllib.request

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        return json.loads(reply.read().decode("utf-8"))
