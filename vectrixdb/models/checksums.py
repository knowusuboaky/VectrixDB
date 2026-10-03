"""SHA-256 checksums for downloaded model files.

A model that arrives over the network is verified against ``checksums.json``
beside this module before it is trusted. The manifest is generated from a
known-good copy with ``python scripts/model_checksums.py --write <type>``.
Where no entry exists yet the download is accepted and a warning says it was
not verified, so a missing manifest entry is visible rather than silent.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from ..exceptions import ModelDownloadError


__all__ = [
    "MANIFEST",
    "sha256_of",
    "compute",
    "load_manifest",
    "save_manifest",
    "verify",
]


# ============================================================================
# SETTINGS: the logger, the manifest, and what is ignored
# ============================================================================
#
# Where the manifest lives beside this module, and the files under a model
# directory that are not part of the model.

logger = logging.getLogger(__name__)

MANIFEST = Path(__file__).with_name("checksums.json")

#: Files that carry no weights and vary between exports; never checksummed.
_IGNORED = {".gitattributes", ".gitignore", "README.md"}


# ============================================================================
# COMPUTE, LOAD, SAVE, AND VERIFY
# ============================================================================
#
# INPUT   a model directory; the manifest
# OUTPUT  relative path to sha256 for every file under the directory; the
#         manifest read and written; the directory checked against it
#
# A model that arrives over the network is verified against checksums.json
# before it is trusted.


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: Text files a checkout may carry with either line ending.
_TEXT_SUFFIXES = {".json", ".txt"}


def _line_ending_variants(path: Path) -> List[str]:
    """The file's hash with LF and with CRLF endings, for a text file.

    Part of the manifest was recorded from a Windows checkout, where git wrote
    CRLF, while every other checkout and the release archives carry LF; the
    bytes differ and the text does not. A binary file has no variants.
    """
    if path.suffix not in _TEXT_SUFFIXES:
        return []
    data = path.read_bytes()
    lf = data.replace(b"\r\n", b"\n")
    crlf = lf.replace(b"\n", b"\r\n")
    return [hashlib.sha256(lf).hexdigest(), hashlib.sha256(crlf).hexdigest()]


def compute(model_dir: Path) -> Dict[str, str]:
    """Relative path -> sha256 for every file under ``model_dir``."""
    out: Dict[str, str] = {}
    for path in sorted(model_dir.rglob("*")):
        if path.is_file() and path.name not in _IGNORED:
            out[path.relative_to(model_dir).as_posix()] = sha256_of(path)
    return out


def load_manifest(path: Path = MANIFEST) -> Dict[str, Dict[str, str]]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_manifest(manifest: Dict[str, Dict[str, str]], path: Path = MANIFEST) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")


def verify(
    model_type: str,
    model_dir: Path,
    manifest: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[str]:
    """Check ``model_dir`` against the manifest.

    Returns the list of verified files. Raises ``ModelDownloadError`` on a
    mismatch or a missing file. When the manifest has no entry for
    ``model_type`` nothing is checked and a warning says so.
    """
    manifest = load_manifest() if manifest is None else manifest
    expected = manifest.get(model_type)
    if not expected:
        logger.warning(
            "No checksums on record for the %r model; the download was not verified. "
            "Generate them from a known-good copy with "
            "`python scripts/model_checksums.py --write %s`.",
            model_type,
            model_type,
        )
        return []

    verified: List[str] = []
    for relative, digest in expected.items():
        path = model_dir / relative
        if not path.exists():
            raise ModelDownloadError(
                f"The {model_type!r} model is missing {relative!r} after download."
            )
        actual = sha256_of(path)
        if actual != digest and digest not in _line_ending_variants(path):
            raise ModelDownloadError(
                f"Checksum mismatch for {model_type!r} file {relative!r}: "
                f"expected {digest[:12]}…, got {actual[:12]}…. The download is "
                "corrupt or the release asset changed; delete the directory and retry."
            )
        verified.append(relative)
    return verified
