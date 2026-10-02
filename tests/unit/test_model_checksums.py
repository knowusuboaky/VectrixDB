"""The checksum manifest against the models the repository carries.

Part of the manifest was recorded on a Windows checkout, where git writes text
files with CRLF; every other checkout, and the release archives, carry LF. A
text file is the same file either way, so either ending verifies, and a binary
file is held to its bytes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import vectrixdb.models.checksums as checksums
from vectrixdb.exceptions import ModelDownloadError

DATA = Path(checksums.__file__).with_name("data")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
def test_a_text_file_verifies_with_either_line_ending(tmp_path, ending):
    (tmp_path / "config.json").write_bytes(b'{\n  "a": 1\n}\n'.replace(b"\n", ending))
    recorded = _sha(b'{\r\n  "a": 1\r\n}\r\n')
    assert checksums.verify("m", tmp_path, {"m": {"config.json": recorded}}) == ["config.json"]


def test_a_binary_file_is_held_to_its_bytes(tmp_path):
    (tmp_path / "model.onnx").write_bytes(b"one\ntwo\n")
    recorded = _sha(b"one\r\ntwo\r\n")
    with pytest.raises(ModelDownloadError, match="Checksum mismatch"):
        checksums.verify("m", tmp_path, {"m": {"model.onnx": recorded}})


def test_a_changed_text_file_still_fails(tmp_path):
    (tmp_path / "vocab.txt").write_bytes(b"a\nb\n")
    with pytest.raises(ModelDownloadError, match="Checksum mismatch"):
        checksums.verify("m", tmp_path, {"m": {"vocab.txt": _sha(b"a\nc\n")}})


@pytest.mark.parametrize("model", sorted(checksums.load_manifest()))
def test_the_bundled_models_match_the_manifest(model):
    folder = DATA / model
    if not folder.is_dir():
        pytest.skip(f"{model} is downloaded, not bundled")
    onnx = folder / "model.onnx"
    if onnx.exists() and onnx.stat().st_size < 1024:
        pytest.skip("model.onnx is a Git LFS pointer in this checkout")
    assert checksums.verify(model, folder)
