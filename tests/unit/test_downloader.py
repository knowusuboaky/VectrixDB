"""Tests for vectrixdb.models.downloader with no network.

urlopen is replaced by a fake that serves an in-memory zip with a
Content-Length header. The models directory is redirected to tmp_path
through VECTRIXDB_MODELS_DIR. The HuggingFace and torch fallbacks are
exercised by planting fake modules in sys.modules, so nothing is
imported from the real packages even when they happen to be installed.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import types
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

import vectrixdb.models.checksums as checksums
import vectrixdb.models.downloader as downloader_mod
from vectrixdb.exceptions import ModelDownloadError
from vectrixdb.models.downloader import ModelDownloader, download_models_cli
from vectrixdb.models.embedded import GITHUB_RELEASE_BASE, MODEL_CONFIG


# --- fixtures ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def models_dir(tmp_path, monkeypatch):
    """Point the package at an empty models directory and allow explicit downloads."""
    d = tmp_path / "models"
    d.mkdir()
    monkeypatch.setenv("VECTRIXDB_MODELS_DIR", str(d))
    monkeypatch.delenv("VECTRIXDB_OFFLINE", raising=False)
    return d


@pytest.fixture(autouse=True)
def no_manifest(monkeypatch):
    """Default to an empty checksum manifest; tests that verify supply their own."""
    monkeypatch.setattr(checksums, "load_manifest", lambda path=None: {})


@pytest.fixture(autouse=True)
def temp_files(monkeypatch, tmp_path):
    """Keep the downloader's temp files under tmp_path and record their names."""
    created = []
    real = tempfile.NamedTemporaryFile
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    def fake(*args, **kwargs):
        kwargs.setdefault("dir", str(scratch))
        f = real(*args, **kwargs)
        created.append(Path(f.name))
        return f

    monkeypatch.setattr(downloader_mod.tempfile, "NamedTemporaryFile", fake)
    return created


def make_zip(files: dict, root: str | None = None, dir_entries=()) -> bytes:
    """Build a zip in memory. ``root`` nests everything under one folder."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if root:
            zf.writestr(f"{root}/", "")
        for name in dir_entries:
            zf.writestr(f"{root}/{name}/" if root else f"{name}/", "")
        for name, data in files.items():
            zf.writestr(f"{root}/{name}" if root else name, data)
    return buf.getvalue()


class FakeResponse:
    """The parts of an HTTP response the downloader reads."""

    def __init__(self, body: bytes, content_length: bool = True, fail_after: int | None = None):
        self._buf = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body))} if content_length else {}
        self._fail_after = fail_after
        self._reads = 0

    def read(self, n=-1):
        self._reads += 1
        if self._fail_after is not None and self._reads > self._fail_after:
            raise ConnectionResetError("connection dropped")
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_urlopen(monkeypatch):
    """Replace urlopen. ``serve(body)`` sets the reply; ``requests`` records the calls."""
    state = {"response": None, "error": None}
    requests = []

    def urlopen(req, timeout=None):
        requests.append((req, timeout))
        if state["error"] is not None:
            raise state["error"]
        if state["response"] is None:
            raise AssertionError("no response configured")
        return state["response"]

    monkeypatch.setattr(downloader_mod, "urlopen", urlopen)

    class Control:
        def serve(self, body: bytes, **kw):
            state["response"] = FakeResponse(body, **kw)

        def fail(self, exc: Exception):
            state["error"] = exc

        @property
        def requests(self):
            return requests

    return Control()


ONNX_BYTES = b"\x08\x01ONNX-fake-model"
TOKENIZER = json.dumps({"model": {"vocab": {"hello": 0}}}).encode()


# --- resolution and dispatch ----------------------------------------------------------


def test_unknown_model_type_raises():
    with pytest.raises(ValueError, match="Unknown model type: nope"):
        ModelDownloader(progress=False).download("nope")


def test_offline_flag_refuses_download(monkeypatch, fake_urlopen):
    monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
    with pytest.raises(ModelDownloadError, match="VECTRIXDB_OFFLINE"):
        ModelDownloader(progress=False).download("dense")
    assert fake_urlopen.requests == []


def test_models_dir_comes_from_environment(models_dir):
    assert ModelDownloader().models_dir == models_dir


def test_no_release_tag_means_no_github_attempt(fake_urlopen, models_dir):
    dl = ModelDownloader(progress=False)
    assert dl._download_from_github("thing", models_dir / "thing", {}) is False
    assert fake_urlopen.requests == []


def test_release_url_is_built_from_tag_and_type(fake_urlopen, models_dir):
    fake_urlopen.serve(make_zip({"model.onnx": ONNX_BYTES}))
    dl = ModelDownloader(progress=False)
    ok = dl._download_from_github("widget", models_dir / "widget", {"github_release": "v9"})
    assert ok is True
    ((req, timeout),) = fake_urlopen.requests
    assert req.full_url == f"{GITHUB_RELEASE_BASE}/v9/widget.zip"
    assert req.get_header("User-agent") == "VectrixDB-Downloader/1.0"
    assert timeout == 60


# --- download, progress and extraction --------------------------------------------------


def test_flat_zip_is_extracted_and_temp_file_removed(fake_urlopen, models_dir, temp_files, capsys):
    fake_urlopen.serve(make_zip({"model.onnx": ONNX_BYTES, "tokenizer.json": TOKENIZER}))
    target = models_dir / "widget"
    ok = ModelDownloader(progress=True)._download_from_github(
        "widget", target, {"github_release": "v1"}
    )
    assert ok is True
    assert (target / "model.onnx").read_bytes() == ONNX_BYTES
    assert (target / "tokenizer.json").read_bytes() == TOKENIZER
    assert len(temp_files) == 1
    assert not temp_files[0].exists()
    out = capsys.readouterr().out
    assert "Downloading: 100.0%" in out
    assert "Successfully downloaded from GitHub" in out


def test_progress_is_silent_when_disabled(fake_urlopen, models_dir, capsys):
    fake_urlopen.serve(make_zip({"model.onnx": ONNX_BYTES}))
    ModelDownloader(progress=False)._download_from_github(
        "widget", models_dir / "widget", {"github_release": "v1"}
    )
    assert "Downloading:" not in capsys.readouterr().out


def test_progress_skipped_without_content_length(fake_urlopen, models_dir, capsys):
    fake_urlopen.serve(make_zip({"model.onnx": ONNX_BYTES}), content_length=False)
    ok = ModelDownloader(progress=True)._download_from_github(
        "widget", models_dir / "widget", {"github_release": "v1"}
    )
    assert ok is True
    assert "Downloading:" not in capsys.readouterr().out


def test_progress_reads_in_chunks(fake_urlopen, models_dir, capsys):
    big = make_zip({"model.onnx": b"x" * 20000})
    fake_urlopen.serve(big)
    ModelDownloader(progress=True)._download_from_github(
        "widget", models_dir / "widget", {"github_release": "v1"}
    )
    out = capsys.readouterr().out
    # More than one progress line means the 8 KiB chunk loop ran more than once.
    assert out.count("Downloading:") > 1


def test_nested_zip_is_flattened(fake_urlopen, models_dir, capsys):
    fake_urlopen.serve(
        make_zip(
            {"model.onnx": ONNX_BYTES, "sub/extra.txt": b"nested"},
            root="dense",
            dir_entries=["empty_dir"],
        )
    )
    target = models_dir / "dense"
    ok = ModelDownloader(progress=False)._download_from_github(
        "widget", target, {"github_release": "v1"}
    )
    assert ok is True
    assert (target / "model.onnx").read_bytes() == ONNX_BYTES
    assert (target / "sub" / "extra.txt").read_bytes() == b"nested"
    assert (target / "empty_dir").is_dir()
    assert not (target / "dense").exists()
    assert "Flattening nested folder: dense/" in capsys.readouterr().out


@pytest.mark.parametrize("escape", ["../../escaped.txt", "sub/../../escaped.txt"])
def test_a_zip_entry_cannot_escape_the_model_directory(fake_urlopen, models_dir, escape):
    """Flattening joined each entry to the model directory as it came, so a
    nested "../" wrote outside it; now nothing is written at all."""
    fake_urlopen.serve(make_zip({"model.onnx": ONNX_BYTES, escape: b"pwned"}, root="dense"))
    target = models_dir / "a" / "dense"
    with pytest.raises(ModelDownloadError, match="outside"):
        ModelDownloader(progress=False)._download_from_github(
            "dense", target, {"github_release": "v1"}
        )
    assert not (models_dir / "escaped.txt").exists()
    assert not (target / "model.onnx").exists()


def test_the_inside_check_refuses_absolute_and_parent_names(tmp_path):
    assert downloader_mod._inside(tmp_path, "ok/model.onnx") == tmp_path / "ok" / "model.onnx"
    for name in ("../x", "/etc/passwd", "a/../../x"):
        with pytest.raises(ModelDownloadError):
            downloader_mod._inside(tmp_path, name)


def test_mixed_root_zip_is_not_flattened(fake_urlopen, models_dir):
    # One entry sits outside the folder, so the folder is kept as-is.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("dense/model.onnx", ONNX_BYTES)
        zf.writestr("README.md", b"top level")
    fake_urlopen.serve(buf.getvalue())
    target = models_dir / "widget"
    ok = ModelDownloader(progress=False)._download_from_github(
        "widget", target, {"github_release": "v1"}
    )
    assert ok is True
    assert (target / "dense" / "model.onnx").exists()
    assert (target / "README.md").exists()


# --- failure paths --------------------------------------------------------------------


def _http_error(code: int) -> HTTPError:
    return HTTPError("http://x", code, "err", {}, None)


def test_http_404_returns_false(fake_urlopen, models_dir, capsys):
    fake_urlopen.fail(_http_error(404))
    ok = ModelDownloader(progress=False)._download_from_github(
        "widget", models_dir / "widget", {"github_release": "v1"}
    )
    assert ok is False
    assert "404" in capsys.readouterr().out


def test_http_500_returns_false(fake_urlopen, models_dir, capsys):
    fake_urlopen.fail(_http_error(500))
    ok = ModelDownloader(progress=False)._download_from_github(
        "widget", models_dir / "widget", {"github_release": "v1"}
    )
    assert ok is False
    assert "HTTP 500" in capsys.readouterr().out


def test_url_error_returns_false(fake_urlopen, models_dir, capsys):
    fake_urlopen.fail(URLError("name resolution failed"))
    ok = ModelDownloader(progress=False)._download_from_github(
        "widget", models_dir / "widget", {"github_release": "v1"}
    )
    assert ok is False
    assert "name resolution failed" in capsys.readouterr().out


def test_unexpected_error_returns_false(fake_urlopen, models_dir, capsys):
    fake_urlopen.fail(RuntimeError("socket exploded"))
    ok = ModelDownloader(progress=False)._download_from_github(
        "widget", models_dir / "widget", {"github_release": "v1"}
    )
    assert ok is False
    assert "socket exploded" in capsys.readouterr().out


def test_corrupt_archive_returns_false(fake_urlopen, models_dir, capsys):
    fake_urlopen.serve(b"this is not a zip file at all")
    target = models_dir / "widget"
    ok = ModelDownloader(progress=False)._download_from_github(
        "widget", target, {"github_release": "v1"}
    )
    assert ok is False
    assert "GitHub download failed" in capsys.readouterr().out
    assert not any(target.iterdir())


def test_interrupted_download_returns_false(fake_urlopen, models_dir, capsys):
    fake_urlopen.serve(make_zip({"model.onnx": b"x" * 20000}), fail_after=1)
    target = models_dir / "widget"
    ok = ModelDownloader(progress=False)._download_from_github(
        "widget", target, {"github_release": "v1"}
    )
    assert ok is False
    assert "connection dropped" in capsys.readouterr().out
    assert not (target / "model.onnx").exists()


def test_interrupted_download_removes_temp_file(fake_urlopen, models_dir, temp_files):
    """The zip is a delete=False temporary file, so until 2.2 a download that
    failed part way left it in the system temp directory for good."""
    fake_urlopen.serve(make_zip({"model.onnx": b"x" * 20000}), fail_after=1)
    ModelDownloader(progress=False)._download_from_github(
        "widget", models_dir / "widget", {"github_release": "v1"}
    )
    assert len(temp_files) == 1
    assert not temp_files[0].exists()


# --- checksum verification -------------------------------------------------------------


def _manifest_for(files: dict) -> dict:
    return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def test_matching_checksums_pass(fake_urlopen, models_dir, monkeypatch):
    files = {"model.onnx": ONNX_BYTES, "tokenizer.json": TOKENIZER}
    monkeypatch.setattr(
        checksums, "load_manifest", lambda path=None: {"widget": _manifest_for(files)}
    )
    fake_urlopen.serve(make_zip(files))
    ok = ModelDownloader(progress=False)._download_from_github(
        "widget", models_dir / "widget", {"github_release": "v1"}
    )
    assert ok is True


def test_checksum_mismatch_raises_and_is_not_swallowed(fake_urlopen, models_dir, monkeypatch):
    expected = {"model.onnx": ONNX_BYTES}
    monkeypatch.setattr(
        checksums, "load_manifest", lambda path=None: {"widget": _manifest_for(expected)}
    )
    fake_urlopen.serve(make_zip({"model.onnx": b"tampered"}))
    with pytest.raises(ModelDownloadError, match="Checksum mismatch"):
        ModelDownloader(progress=False)._download_from_github(
            "widget", models_dir / "widget", {"github_release": "v1"}
        )


def test_missing_manifest_file_raises(fake_urlopen, models_dir, monkeypatch):
    expected = {"model.onnx": ONNX_BYTES, "tokenizer.json": TOKENIZER}
    monkeypatch.setattr(
        checksums, "load_manifest", lambda path=None: {"widget": _manifest_for(expected)}
    )
    fake_urlopen.serve(make_zip({"model.onnx": ONNX_BYTES}))
    with pytest.raises(ModelDownloadError, match="missing 'tokenizer.json'"):
        ModelDownloader(progress=False)._download_from_github(
            "widget", models_dir / "widget", {"github_release": "v1"}
        )


def test_checksum_failure_stops_dense_fallback(fake_urlopen, models_dir, monkeypatch):
    monkeypatch.setattr(
        checksums,
        "load_manifest",
        lambda path=None: {"dense": _manifest_for({"model.onnx": ONNX_BYTES})},
    )
    fake_urlopen.serve(make_zip({"model.onnx": b"tampered"}))
    with pytest.raises(ModelDownloadError):
        ModelDownloader(progress=False).download("dense")
    # Only the GitHub request was made; no HuggingFace attempt followed.
    assert len(fake_urlopen.requests) == 1


# --- sparse -----------------------------------------------------------------------------


def test_sparse_is_created_locally(fake_urlopen, models_dir, capsys):
    out_dir = ModelDownloader(progress=False).download("sparse")
    assert out_dir == models_dir / "sparse"
    assert fake_urlopen.requests == []
    cfg = MODEL_CONFIG["sparse"]
    vocab = json.loads((out_dir / cfg["vocab_file"]).read_text(encoding="utf-8"))
    idf = json.loads((out_dir / cfg["idf_file"]).read_text(encoding="utf-8"))
    bm25 = json.loads((out_dir / cfg["config_file"]).read_text(encoding="utf-8"))
    assert vocab["the"] == 0
    assert set(idf) == set(vocab)
    assert idf["the"] == 0.1
    assert idf["python"] == 1.0
    assert bm25["k1"] == 1.5 and bm25["b"] == 0.75
    assert f"Vocabulary size: {len(vocab)}" in capsys.readouterr().out


def test_cached_model_is_not_downloaded_again(fake_urlopen, models_dir):
    """embedded.download_models is the caller that honours an existing install."""
    from vectrixdb.models.embedded import download_models

    fake_urlopen.serve(make_zip({"model.onnx": ONNX_BYTES}))
    download_models(model_type="reranker", progress=False)
    assert len(fake_urlopen.requests) == 1
    download_models(model_type="reranker", progress=False)
    assert len(fake_urlopen.requests) == 1
    fake_urlopen.serve(make_zip({"model.onnx": ONNX_BYTES}))
    download_models(model_type="reranker", progress=False, force=True)
    assert len(fake_urlopen.requests) == 2


def test_a_bundled_english_model_does_not_stand_in_for_a_download(models_dir, monkeypatch):
    """The wheel's English models answer whether a dense model or a reranker
    is here at all, and must not answer whether the multilingual one is. They
    did, so neither the first-use download a multilingual collection asks for
    nor the command its error message names fetched anything."""
    from vectrixdb.models.embedded import download_models, is_models_installed

    for folder in ("bge_small_en", "reranker_en"):
        (models_dir / folder).mkdir()
        (models_dir / folder / "model.onnx").write_bytes(ONNX_BYTES)
    fetched = []
    monkeypatch.setattr(ModelDownloader, "download", lambda self, kind: fetched.append(kind))

    assert is_models_installed("dense") and is_models_installed("reranker")
    assert not is_models_installed("dense", exact=True)
    assert not is_models_installed("reranker", exact=True)
    download_models(model_type="dense", progress=False)
    download_models(model_type="reranker", progress=False)
    assert fetched == ["dense", "reranker"]


# --- GitHub-only models -----------------------------------------------------------------


@pytest.mark.parametrize(
    "model_type, folder",
    [
        ("reranker_en", "reranker_en"),
        ("late_interaction_en", "colbert"),
    ],
)
def test_github_only_models_succeed(fake_urlopen, models_dir, model_type, folder):
    fake_urlopen.serve(make_zip({"model.onnx": ONNX_BYTES}))
    out_dir = ModelDownloader(progress=False).download(model_type)
    assert out_dir == models_dir / folder
    assert (out_dir / "model.onnx").exists()
    tag = MODEL_CONFIG[model_type]["github_release"]
    assert fake_urlopen.requests[0][0].full_url == f"{GITHUB_RELEASE_BASE}/{tag}/{folder}.zip"


@pytest.mark.parametrize("model_type", ["reranker_en", "late_interaction_en"])
def test_github_only_models_fail_loudly(fake_urlopen, models_dir, model_type):
    fake_urlopen.fail(_http_error(404))
    with pytest.raises(ModelDownloadError, match="(?s)Could not fetch.*no HuggingFace fallback"):
        ModelDownloader(progress=False).download(model_type)


# --- when every source fails, the error says which, and what to do -------------------


@pytest.mark.parametrize(
    "model_type, asset, export_name",
    [
        ("dense_en", "dense_en", "_manual_dense_export"),
        ("bge_base_en", "bge_base_en", "_manual_dense_export"),
        ("reranker_en", "reranker_en", None),
        ("late_interaction_en", "colbert", None),
    ],
)
def test_a_missing_release_is_named_with_its_publishing_command(
    fake_urlopen, models_dir, monkeypatch, model_type, asset, export_name
):
    """No release tag has been published, so the GitHub fallback answers 404 to
    everyone, and dense_en is not in the wheel either. The error used to say
    "check your internet connection"; it now lists the sources tried, says the
    release does not exist, and names the command that creates it."""
    fake_urlopen.fail(_http_error(404))
    dl = ModelDownloader(progress=False)
    if export_name:
        monkeypatch.setattr(
            dl, export_name, lambda *a, **k: (_ for _ in ()).throw(OSError("no torch"))
        )
    with pytest.raises(ModelDownloadError) as exc:
        dl.download(model_type)
    text = str(exc.value)
    tag = MODEL_CONFIG[model_type]["github_release"]
    assert f"1. GitHub release {tag}, asset {asset}.zip" in text
    assert f"{GITHUB_RELEASE_BASE}/{tag}/{asset}.zip" in text
    assert "HTTP 404: this release, or its asset, does not exist" in text
    assert "has not been published yet" in text
    assert f"python scripts/publish_models.py {model_type}" in text
    assert f"gh release create {tag} dist/models/{asset}.zip" in text
    if export_name:
        assert f"2. HuggingFace {MODEL_CONFIG[model_type]['huggingface_id']}" in text
        assert "no torch" in text
        assert f"--type {model_type}" in text
    else:
        assert "no HuggingFace fallback" in text
        assert "pip install --force-reinstall vectrixdb" in text
    if model_type == "dense_en":
        assert 'dense_model="bge-small").reembed()' in text


def test_a_network_fault_is_not_called_a_missing_release(fake_urlopen, models_dir, monkeypatch):
    fake_urlopen.fail(URLError("name resolution failed"))
    dl = ModelDownloader(progress=False)
    monkeypatch.setattr(
        dl, "_manual_dense_export", lambda *a, **k: (_ for _ in ()).throw(OSError("no torch"))
    )
    with pytest.raises(ModelDownloadError) as exc:
        dl.download("dense_en")
    text = str(exc.value)
    assert "unreachable: name resolution failed" in text
    assert "Check the network or the proxy" in text
    assert "has not been published" not in text and "gh release" not in text


def test_the_easy_api_names_a_missing_release_too(tmp_path, monkeypatch):
    import urllib.request

    from vectrixdb.easy import Vectrix

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def gone(url, dest):
        raise _http_error(404)

    monkeypatch.setattr(urllib.request, "urlretrieve", gone)
    with pytest.raises(ModelDownloadError) as exc:
        Vectrix._download_github_model(object.__new__(Vectrix), "github:my-tag", "reranker")
    text = str(exc.value)
    assert f"{GITHUB_RELEASE_BASE}/my-tag/reranker.zip" in text
    assert "does not exist" in text
    assert "gh release create my-tag dist/models/reranker.zip" in text


@pytest.mark.parametrize("model_type", sorted(downloader_mod.RELEASE_ASSETS))
def test_the_release_url_is_the_one_the_downloader_asks_for(fake_urlopen, models_dir, model_type):
    """release_asset_url, which the publish and check scripts read, must name
    the URL download() requests, or a maintainer would publish the wrong zip."""
    fake_urlopen.fail(_http_error(404))
    dl = ModelDownloader(progress=False)
    for name in (
        "_manual_dense_export",
        "_manual_reranker_export",
        "_manual_colbert_export",
        "_manual_late_interaction_export",
        "_manual_rebel_export",
    ):
        setattr(dl, name, lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    for mod in ("optimum", "optimum.onnxruntime"):
        sys.modules.pop(mod, None)
    sys.modules["optimum"] = types.ModuleType(
        "optimum"
    )  # ImportError on the submodule: manual export
    try:
        with pytest.raises(ModelDownloadError):
            dl.download(model_type)
    finally:
        sys.modules.pop("optimum", None)
    requested = fake_urlopen.requests[0][0].full_url
    assert requested == downloader_mod.release_asset_url(model_type)
    asset, folder = downloader_mod.RELEASE_ASSETS[model_type]
    assert requested.endswith(f"/{asset}.zip")
    assert (models_dir / folder).is_dir()


# --- scripts/publish_models.py and scripts/check_model_releases.py ----------------------


def _script(name: str):
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / name
    spec = importlib.util.spec_from_file_location(name[:-3], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_publish_models_zips_what_the_downloader_accepts(
    fake_urlopen, models_dir, tmp_path, monkeypatch, capsys
):
    """The zip the script makes is flat, leaves backups out, and comes back
    through _download_from_github with its checksums verified."""
    publish = _script("publish_models.py")
    source = models_dir / "dense_en"
    source.mkdir()
    (source / "model.onnx").write_bytes(ONNX_BYTES)
    (source / "tokenizer.json").write_bytes(TOKENIZER)
    (source / "model.onnx.backup").write_bytes(b"old")
    expected = {
        "model.onnx": hashlib.sha256(ONNX_BYTES).hexdigest(),
        "tokenizer.json": hashlib.sha256(TOKENIZER).hexdigest(),
    }
    monkeypatch.setattr(checksums, "load_manifest", lambda path=None: {"dense_en": expected})

    out = tmp_path / "dist"
    assert publish.main(["dense_en", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert f"gh release create dense-en {out / 'dense_en.zip'}" in printed
    assert "gh release upload dense-en" in printed
    with zipfile.ZipFile(out / "dense_en.zip") as zf:
        assert sorted(zf.namelist()) == ["model.onnx", "tokenizer.json"]

    fake_urlopen.serve((out / "dense_en.zip").read_bytes())
    target = models_dir / "fetched"
    assert ModelDownloader(progress=False)._download_from_github(
        "dense_en", target, MODEL_CONFIG["dense_en"]
    )
    assert (target / "model.onnx").read_bytes() == ONNX_BYTES


def test_publish_models_refuses_what_it_cannot_publish(models_dir, tmp_path, capsys):
    publish = _script("publish_models.py")
    with pytest.raises(SystemExit, match="not a type the downloader fetches"):
        publish.main(["bge_small_en", "--out", str(tmp_path)])
    with pytest.raises(SystemExit, match="no model at"):
        publish.main(["rebel", "--out", str(tmp_path)])
    assert publish.main([]) == 0
    listing = capsys.readouterr().out
    assert "dense_en" in listing and "dense-en" in listing and "dense_en.zip" in listing


def test_publish_models_warns_when_checksums_are_not_on_record(models_dir, tmp_path, capsys):
    publish = _script("publish_models.py")
    (models_dir / "rebel").mkdir()
    (models_dir / "rebel" / "model.onnx").write_bytes(ONNX_BYTES)
    assert publish.main(["rebel", "--out", str(tmp_path)]) == 1
    assert "model_checksums.py --write rebel" in capsys.readouterr().out


def test_check_model_releases_reports_the_missing_ones(monkeypatch, capsys):
    check = _script("check_model_releases.py")
    seen = []

    def head(url, timeout=30.0):
        seen.append(url)
        return "ok" if "/dense-en/" in url else "missing (HTTP 404)"

    monkeypatch.setattr(check, "head", head)
    assert check.main([]) == 1
    out = capsys.readouterr().out
    assert "dense_en             ok" in out
    assert (
        "cannot be fetched" in out
        and "rebel" in out
        and "dense_en," not in out.split("cannot be fetched")[1]
    )
    assert "colbert" not in [u.rsplit("/", 1)[1] for u in seen if "/colbert-en/" not in u]
    assert set(seen) == {
        downloader_mod.release_asset_url(t) for t in downloader_mod.RELEASE_ASSETS if t != "colbert"
    }

    assert check.main(["dense_en"]) == 0
    assert "all 1 release assets are there" in capsys.readouterr().out


def test_check_model_releases_reads_the_status(monkeypatch):
    check = _script("check_model_releases.py")
    monkeypatch.setattr(
        check, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(_http_error(404))
    )
    assert check.head("http://x") == "missing (HTTP 404)"
    monkeypatch.setattr(
        check, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(URLError("down"))
    )
    assert check.head("http://x") == "unreachable: down"

    class Ok:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(check, "urlopen", lambda req, timeout=None: Ok())
    assert check.head("http://x") == "ok"


# --- BGE / ColBERT v2: GitHub then manual export ---------------------------------------


@pytest.mark.parametrize(
    "model_type, export_name",
    [
        ("bge_base_en", "_manual_dense_export"),
        ("bge_reranker_base", "_manual_reranker_export"),
        ("colbert_v2", "_manual_colbert_export"),
    ],
)
def test_bge_family_uses_github_first(
    fake_urlopen, models_dir, monkeypatch, model_type, export_name
):
    fake_urlopen.serve(make_zip({"model.onnx": ONNX_BYTES}))
    dl = ModelDownloader(progress=False)
    monkeypatch.setattr(dl, export_name, lambda *a, **k: pytest.fail("export must not run"))
    assert dl.download(model_type) == models_dir / model_type


@pytest.mark.parametrize(
    "model_type, export_name",
    [
        ("bge_base_en", "_manual_dense_export"),
        ("bge_reranker_base", "_manual_reranker_export"),
        ("colbert_v2", "_manual_colbert_export"),
    ],
)
def test_bge_family_falls_back_to_export(
    fake_urlopen, models_dir, monkeypatch, model_type, export_name
):
    fake_urlopen.fail(_http_error(404))
    dl = ModelDownloader(progress=False)
    calls = []
    monkeypatch.setattr(dl, export_name, lambda model_dir, config: calls.append(model_dir))
    assert dl.download(model_type) == models_dir / model_type
    assert calls == [models_dir / model_type]


@pytest.mark.parametrize(
    "model_type, export_name",
    [
        ("bge_base_en", "_manual_dense_export"),
        ("bge_reranker_base", "_manual_reranker_export"),
        ("colbert_v2", "_manual_colbert_export"),
    ],
)
def test_bge_family_wraps_export_failure(
    fake_urlopen, models_dir, monkeypatch, model_type, export_name
):
    fake_urlopen.fail(_http_error(404))
    dl = ModelDownloader(progress=False)

    def boom(model_dir, config):
        raise OSError("no torch")

    monkeypatch.setattr(dl, export_name, boom)
    with pytest.raises(ModelDownloadError, match=f"(?s)no torch.*--type {model_type}"):
        dl.download(model_type)


# --- HuggingFace fallbacks through fake optimum / transformers / torch -------------------


class _FakeTokenizer:
    """Minimal stand-in for a transformers tokenizer."""

    lang_code_to_id = {"en_XX": 250004}

    def __init__(self, token_type_ids: bool):
        self._tt = token_type_ids

    def __call__(self, *texts, **kwargs):
        d = {"input_ids": "ids", "attention_mask": "mask"}
        if self._tt:
            d["token_type_ids"] = "tt"
        return d

    def save_pretrained(self, path):
        Path(path).joinpath("tokenizer.json").write_bytes(TOKENIZER)


def _plant(monkeypatch, name: str, module):
    monkeypatch.setitem(sys.modules, name, module)


def _fake_transformers(monkeypatch, token_type_ids: bool = False):
    tok = _FakeTokenizer(token_type_ids)
    transformers = types.ModuleType("transformers")
    transformers.AutoTokenizer = types.SimpleNamespace(from_pretrained=lambda hf_id: tok)

    class _Model:
        def __init__(self):
            self.config = types.SimpleNamespace(
                decoder_start_token_id=2, eos_token_id=1, pad_token_id=0
            )
            self.model = types.SimpleNamespace(decoder=object())

        def eval(self):
            return self

        def get_encoder(self):
            return lambda *a, **k: types.SimpleNamespace(last_hidden_state="hidden")

    for cls in ("AutoModel", "AutoModelForSequenceClassification", "AutoModelForSeq2SeqLM"):
        setattr(transformers, cls, types.SimpleNamespace(from_pretrained=lambda hf_id: _Model()))
    _plant(monkeypatch, "transformers", transformers)
    return transformers


def _fake_torch(monkeypatch, exports: list):
    torch = types.ModuleType("torch")

    def export(model, args, path, **kwargs):
        exports.append((Path(path), kwargs["input_names"], kwargs["output_names"]))
        Path(path).write_bytes(ONNX_BYTES)

    torch.onnx = types.SimpleNamespace(export=export)
    torch.nn = types.SimpleNamespace(Module=object)
    torch.long = "long"
    torch.tensor = lambda data, dtype=None: data
    torch.ones_like = lambda x: x
    _plant(monkeypatch, "torch", torch)
    return torch


def _no_optimum(monkeypatch):
    _plant(monkeypatch, "optimum", None)
    _plant(monkeypatch, "optimum.onnxruntime", None)


def _fake_optimum(monkeypatch, onnx_names, raise_exc: Exception | None = None):
    """optimum whose ORT classes save the given .onnx names into model_dir."""

    class _ORT:
        @staticmethod
        def from_pretrained(hf_id, export=False):
            if raise_exc is not None:
                raise raise_exc

            class Saved:
                def save_pretrained(self, path):
                    for n in onnx_names:
                        Path(path).joinpath(n).write_bytes(ONNX_BYTES)

            return Saved()

    module = types.ModuleType("optimum.onnxruntime")
    for cls in (
        "ORTModelForFeatureExtraction",
        "ORTModelForSequenceClassification",
        "ORTModelForSeq2SeqLM",
    ):
        setattr(module, cls, _ORT)
    _plant(monkeypatch, "optimum", types.ModuleType("optimum"))
    _plant(monkeypatch, "optimum.onnxruntime", module)


HF_MODELS = [
    ("dense", "dense"),
    ("reranker", "reranker"),
    ("late_interaction", "bge-m3"),
]


@pytest.mark.parametrize("model_type, folder", HF_MODELS)
def test_hf_optimum_path_renames_onnx(fake_urlopen, models_dir, monkeypatch, model_type, folder):
    fake_urlopen.fail(_http_error(404))
    _fake_optimum(monkeypatch, ["exported.onnx"])
    _fake_transformers(monkeypatch)
    out_dir = ModelDownloader(progress=False).download(model_type)
    assert out_dir == models_dir / folder
    assert (out_dir / MODEL_CONFIG[model_type]["onnx_file"]).read_bytes() == ONNX_BYTES
    assert (out_dir / "tokenizer.json").exists()


@pytest.mark.parametrize("model_type, folder", HF_MODELS)
def test_hf_optimum_failure_raises(
    fake_urlopen, models_dir, monkeypatch, model_type, folder, capsys
):
    fake_urlopen.fail(_http_error(404))
    _fake_optimum(monkeypatch, [], raise_exc=OSError("hub unreachable"))
    _fake_transformers(monkeypatch)
    with pytest.raises(ModelDownloadError, match=r"(?s)1\. GitHub release.*2\. HuggingFace"):
        ModelDownloader(progress=False).download(model_type)
    assert "hub unreachable" in capsys.readouterr().out


@pytest.mark.parametrize("model_type, folder", HF_MODELS)
def test_manual_export_without_optimum(fake_urlopen, models_dir, monkeypatch, model_type, folder):
    fake_urlopen.fail(_http_error(404))
    _no_optimum(monkeypatch)
    _fake_transformers(monkeypatch, token_type_ids=True)
    exports = []
    _fake_torch(monkeypatch, exports)
    out_dir = ModelDownloader(progress=False).download(model_type)
    assert out_dir == models_dir / folder
    ((path, inputs, _outputs),) = exports
    assert path == out_dir / MODEL_CONFIG[model_type]["onnx_file"]
    assert "token_type_ids" in inputs
    cfg = json.loads((out_dir / "vectrix_config.json").read_text())
    assert cfg["has_token_type_ids"] is True
    assert cfg["max_length"] == MODEL_CONFIG[model_type]["max_length"]


@pytest.mark.parametrize("model_type, folder", HF_MODELS)
def test_manual_export_without_token_type_ids(
    fake_urlopen, models_dir, monkeypatch, model_type, folder
):
    fake_urlopen.fail(_http_error(404))
    _no_optimum(monkeypatch)
    _fake_transformers(monkeypatch, token_type_ids=False)
    exports = []
    _fake_torch(monkeypatch, exports)
    out_dir = ModelDownloader(progress=False).download(model_type)
    ((_path, inputs, _outputs),) = exports
    assert inputs == ["input_ids", "attention_mask"]
    cfg = json.loads((out_dir / "vectrix_config.json").read_text())
    assert cfg["has_token_type_ids"] is False


@pytest.mark.parametrize("model_type, folder", HF_MODELS)
def test_manual_export_failure_raises(
    fake_urlopen, models_dir, monkeypatch, model_type, folder, capsys
):
    fake_urlopen.fail(_http_error(404))
    _no_optimum(monkeypatch)
    _plant(monkeypatch, "torch", None)
    _plant(monkeypatch, "transformers", None)
    with pytest.raises(ModelDownloadError, match=r"(?s)1\. GitHub release.*2\. HuggingFace"):
        ModelDownloader(progress=False).download(model_type)
    assert "Manual export failed" in capsys.readouterr().out


# --- mREBEL (encoder-decoder) ----------------------------------------------------------


def test_rebel_github_success(fake_urlopen, models_dir):
    fake_urlopen.serve(make_zip({"encoder.onnx": ONNX_BYTES, "decoder.onnx": ONNX_BYTES}))
    out_dir = ModelDownloader(progress=False).download("rebel")
    assert out_dir == models_dir / "rebel"
    assert fake_urlopen.requests[0][0].full_url.endswith("/mrebel/rebel.zip")


def test_rebel_optimum_path_renames_encoder_and_decoder(fake_urlopen, models_dir, monkeypatch):
    fake_urlopen.fail(_http_error(404))
    _fake_optimum(
        monkeypatch,
        ["encoder_model.onnx", "decoder_with_past_model.onnx", "decoder_model.onnx"],
    )
    _fake_transformers(monkeypatch)
    out_dir = ModelDownloader(progress=False).download("rebel")
    cfg = MODEL_CONFIG["rebel"]
    assert (out_dir / cfg["onnx_encoder_file"]).exists()
    assert (out_dir / cfg["onnx_decoder_file"]).exists()
    # The with_past decoder is left alone, the plain one was renamed.
    assert (out_dir / "decoder_with_past_model.onnx").exists()
    assert not (out_dir / "decoder_model.onnx").exists()


def test_rebel_manual_export(fake_urlopen, models_dir, monkeypatch):
    fake_urlopen.fail(_http_error(404))
    _no_optimum(monkeypatch)
    _fake_transformers(monkeypatch)
    exports = []
    _fake_torch(monkeypatch, exports)
    out_dir = ModelDownloader(progress=False).download("rebel")
    cfg = MODEL_CONFIG["rebel"]
    assert [e[0].name for e in exports] == [cfg["onnx_encoder_file"], cfg["onnx_decoder_file"]]
    assert exports[1][2] == ["logits"]
    saved = json.loads((out_dir / "vectrix_config.json").read_text())
    assert saved == {
        "max_length": cfg["max_length"],
        "decoder_start_token_id": 2,
        "eos_token_id": 1,
        "pad_token_id": 0,
    }


def test_rebel_all_sources_fail(fake_urlopen, models_dir, monkeypatch):
    fake_urlopen.fail(_http_error(404))
    _fake_optimum(monkeypatch, [], raise_exc=OSError("no hub"))
    _fake_transformers(monkeypatch)
    with pytest.raises(
        ModelDownloadError,
        match=r"(?s)mrebel-base-int8 model \(rebel\).*no hub.*publish_models\.py rebel",
    ):
        ModelDownloader(progress=False).download("rebel")


# --- colbert, the older name for the English late-interaction model ---------------------


def test_colbert_download_reads_its_config(fake_urlopen, models_dir):
    """``--type colbert`` is an advertised choice. Until 2.2 its handler began
    with MODEL_CONFIG["colbert"], a key that has never existed, so the option
    raised KeyError before it reached the network. It resolves to the English
    late-interaction config now, which installs into the same directory."""
    fake_urlopen.serve(make_zip({"model.onnx": ONNX_BYTES}))
    out_dir = ModelDownloader(progress=False).download("colbert")
    assert out_dir == models_dir / "colbert"
    assert (out_dir / "model.onnx").exists()


# --- download_all -----------------------------------------------------------------------


@pytest.fixture
def stubbed_steps(monkeypatch):
    """Record which per-model steps download_all runs, without any I/O."""
    calls = []
    for name in (
        "_download_dense",
        "_create_sparse",
        "_download_reranker",
        "_download_colbert",
        "_download_late_interaction",
        "_download_rebel",
    ):
        monkeypatch.setattr(ModelDownloader, name, (lambda n: lambda self: calls.append(n))(name))
    return calls


def test_download_all_full_bundle(stubbed_steps, capsys):
    ModelDownloader(progress=False).download_all()
    assert stubbed_steps == [
        "_download_dense",
        "_create_sparse",
        "_download_reranker",
        "_download_colbert",
        "_download_late_interaction",
        "_download_rebel",
    ]
    out = capsys.readouterr().out
    assert "~1.7GB" in out
    assert "BGE-M3" in out and "mREBEL" in out


def test_download_all_without_graphrag(stubbed_steps, capsys):
    ModelDownloader(progress=False).download_all(include_graphrag=False)
    assert "_download_rebel" not in stubbed_steps
    assert "_download_late_interaction" in stubbed_steps
    out = capsys.readouterr().out
    assert "~1GB" in out
    assert "mREBEL" not in out


def test_download_all_without_multilingual(stubbed_steps, capsys):
    ModelDownloader(progress=False).download_all(include_multilingual=False)
    assert "_download_late_interaction" not in stubbed_steps
    assert "_download_rebel" in stubbed_steps
    assert "~950MB" in capsys.readouterr().out


def test_download_all_minimal(stubbed_steps, capsys):
    ModelDownloader(progress=False).download_all(include_graphrag=False, include_multilingual=False)
    assert stubbed_steps == [
        "_download_dense",
        "_create_sparse",
        "_download_reranker",
        "_download_colbert",
    ]
    assert "~250MB" in capsys.readouterr().out


# --- download_models_cli ------------------------------------------------------------------


@pytest.fixture
def cli_recorder(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ModelDownloader, "download_all", lambda self, **kw: calls.append(("all", kw))
    )
    monkeypatch.setattr(ModelDownloader, "download", lambda self, mt: calls.append(("one", mt)))
    return calls


def test_cli_default_downloads_everything(monkeypatch, cli_recorder):
    monkeypatch.setattr(sys, "argv", ["download-models"])
    download_models_cli()
    assert cli_recorder == [("all", {"include_graphrag": True})]


def test_cli_no_graphrag_flag(monkeypatch, cli_recorder):
    monkeypatch.setattr(sys, "argv", ["download-models", "--no-graphrag"])
    download_models_cli()
    assert cli_recorder == [("all", {"include_graphrag": False})]


def test_cli_graphrag_maps_to_rebel(monkeypatch, cli_recorder):
    monkeypatch.setattr(sys, "argv", ["download-models", "--type", "graphrag"])
    download_models_cli()
    assert cli_recorder == [("one", "rebel")]


def test_cli_single_type(monkeypatch, cli_recorder):
    monkeypatch.setattr(sys, "argv", ["download-models", "--type", "dense", "--force"])
    download_models_cli()
    assert cli_recorder == [("one", "dense")]


def test_cli_rejects_unknown_type(monkeypatch, cli_recorder, capsys):
    monkeypatch.setattr(sys, "argv", ["download-models", "--type", "bogus"])
    with pytest.raises(SystemExit) as exc:
        download_models_cli()
    assert exc.value.code == 2
    assert cli_recorder == []
    assert "invalid choice" in capsys.readouterr().err


def test_the_easy_api_github_download_is_contained_too(tmp_path, monkeypatch):
    """Vectrix._download_github_model extracted a release zip with no check."""
    import urllib.request

    from vectrixdb.easy import Vectrix

    body = make_zip({"model.onnx": ONNX_BYTES, "../../escaped.txt": b"pwned"})
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        urllib.request, "urlretrieve", lambda url, dest: Path(dest).write_bytes(body)
    )
    with pytest.raises(ModelDownloadError, match="outside"):
        Vectrix._download_github_model(object.__new__(Vectrix), "github:v1", "reranker")
    assert not list(tmp_path.rglob("escaped.txt"))
    assert not list(tmp_path.rglob("model.onnx"))
