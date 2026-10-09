"""Tests for the vectrixdb command line interface.

Every command runs in-process through typer's CliRunner against a database
under tmp_path. Commands that would start a server or an MCP transport have
the function they call replaced, and the recorded arguments are asserted.
The tests set VECTRIXDB_OFFLINE so nothing can reach the network; the
ingest, query and stats commands use the models bundled with the package.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vectrixdb import __version__
from vectrixdb.cli import _format_size, app


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "db")


@pytest.fixture
def text_file(tmp_path):
    p = tmp_path / "doc.txt"
    p.write_text(
        "Python is a programming language.\n\nVector databases enable semantic search.\n",
        encoding="utf-8",
    )
    return p


def _invoke(runner, args, **kw):
    result = runner.invoke(app, args, **kw)
    return result


# --- version and helpers -----------------------------------------------------


def test_version_prints_package_version(runner):
    result = _invoke(runner, ["version"])
    assert result.exit_code == 0
    assert f"v{__version__}" in result.output
    assert "Where vectors come alive" in result.output


def test_format_size_walks_units():
    assert _format_size(0) == "0.0 B"
    assert _format_size(1023) == "1023.0 B"
    assert _format_size(1024) == "1.0 KB"
    assert _format_size(5 * 1024**2) == "5.0 MB"
    assert _format_size(3 * 1024**3) == "3.0 GB"
    assert _format_size(2 * 1024**4) == "2.0 TB"
    assert _format_size(7 * 1024**5) == "7.0 PB"


def test_no_args_shows_help(runner):
    result = _invoke(runner, [])
    assert "Usage" in result.output


# --- serve ----------------------------------------------------------------------


@pytest.fixture
def fake_run_server(monkeypatch):
    """Stand in for vectrixdb.api.server.run_server without importing FastAPI."""
    calls = []

    def run_server(**kwargs):
        calls.append(kwargs)

    module = types.ModuleType("vectrixdb.api.server")
    module.run_server = run_server
    monkeypatch.setitem(sys.modules, "vectrixdb.api.server", module)
    return calls


def test_serve_passes_options_to_run_server(runner, fake_run_server, db_path):
    result = _invoke(
        runner,
        [
            "serve",
            "--port",
            "9001",
            "--host",
            "127.0.0.1",
            "--path",
            db_path,
            "--reload",
            "--api-key",
            "abcdefghijklmnop",
            "--read-only-key",
            "ro",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake_run_server == [
        {
            "host": "127.0.0.1",
            "port": 9001,
            "db_path": db_path,
            "reload": True,
            "api_key": "abcdefghijklmnop",
            "read_only_key": "ro",
            # --no-dashboard used to be collected and dropped, so the flag
            # did nothing and the banner advertised the dashboard anyway.
            "enable_dashboard": True,
        }
    ]
    assert "http://127.0.0.1:9001" in result.output
    # The key is said to be set and never shown, not even its first characters:
    # a terminal is scrolled back, shared and recorded.
    assert "API Key: set" in result.output
    assert "abcdefgh" not in result.output


def test_serve_defaults_and_no_key(runner, fake_run_server):
    result = _invoke(runner, ["serve"])
    assert result.exit_code == 0, result.output
    assert fake_run_server[0]["port"] == 7337
    # This machine only, by default: with no key, every interface was every
    # chunk of every collection, readable by whoever found the port.
    assert fake_run_server[0]["host"] == "127.0.0.1"
    assert fake_run_server[0]["api_key"] is None
    assert "API Key: not set" in result.output


def test_serve_never_shows_a_key_however_short(runner, fake_run_server):
    result = _invoke(runner, ["serve", "-k", "short"])
    assert result.exit_code == 0, result.output
    assert "API Key: set" in result.output and "short" not in result.output


def test_serve_rejects_non_integer_port(runner, fake_run_server):
    result = _invoke(runner, ["serve", "--port", "abc"])
    assert result.exit_code != 0
    assert fake_run_server == []


# --- info -----------------------------------------------------------------------


def test_info_on_fresh_database(runner, db_path):
    result = _invoke(runner, ["info", db_path])
    assert result.exit_code == 0, result.output
    assert "Database Info" in result.output
    assert "Collections" in result.output
    assert __version__ in result.output


def test_info_reports_error_and_exits_1(runner, monkeypatch):
    import vectrixdb.core.database as database

    def boom(path):
        raise RuntimeError("cannot open")

    monkeypatch.setattr(database, "VectrixDB", boom)
    result = _invoke(runner, ["info", "anything"])
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "cannot open" in result.output


# --- create, list, delete -------------------------------------------------------


def test_create_then_list_then_delete(runner, db_path):
    result = _invoke(runner, ["create", "vecs", "4", "--path", db_path, "--metric", "cosine"])
    assert result.exit_code == 0, result.output
    assert "Created collection" in result.output
    assert "vecs" in result.output

    result = _invoke(runner, ["list", db_path])
    assert result.exit_code == 0, result.output
    assert "vecs" in result.output
    assert "cosine" in result.output

    result = _invoke(runner, ["delete", "vecs", "--path", db_path, "--force"])
    assert result.exit_code == 0, result.output
    assert "Deleted collection" in result.output

    result = _invoke(runner, ["list", db_path])
    assert result.exit_code == 0
    assert "No collections found" in result.output


def test_create_rejects_unknown_metric(runner, db_path):
    result = _invoke(runner, ["create", "vecs", "4", "--path", db_path, "--metric", "bogus"])
    assert result.exit_code == 1
    assert "Error:" in result.output


def test_create_requires_dimension(runner, db_path):
    result = _invoke(runner, ["create", "vecs", "--path", db_path])
    assert result.exit_code != 0
    assert "Give the vector dimension" in result.output


def test_list_reports_error_and_exits_1(runner, monkeypatch):
    import vectrixdb.core.database as database

    def boom(path):
        raise RuntimeError("no such db")

    monkeypatch.setattr(database, "VectrixDB", boom)
    result = _invoke(runner, ["list", "anything"])
    assert result.exit_code == 1
    assert "no such db" in result.output


def test_delete_missing_collection_is_reported(runner, db_path):
    result = _invoke(runner, ["delete", "ghost", "--path", db_path, "--force"])
    assert result.exit_code == 0, result.output
    assert "Collection not found" in result.output


def test_delete_prompts_and_aborts_on_no(runner, db_path):
    _invoke(runner, ["create", "vecs", "4", "--path", db_path])
    result = _invoke(runner, ["delete", "vecs", "--path", db_path], input="n\n")
    assert result.exit_code != 0
    assert "Deleted collection" not in result.output
    listing = _invoke(runner, ["list", db_path])
    assert "vecs" in listing.output


def test_delete_prompts_and_proceeds_on_yes(runner, db_path):
    _invoke(runner, ["create", "vecs", "4", "--path", db_path])
    result = _invoke(runner, ["delete", "vecs", "--path", db_path], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Deleted collection" in result.output


def test_delete_reports_error_and_exits_1(runner, monkeypatch):
    import vectrixdb.core.database as database

    def boom(path):
        raise RuntimeError("locked")

    monkeypatch.setattr(database, "VectrixDB", boom)
    result = _invoke(runner, ["delete", "vecs", "--path", "x", "--force"])
    assert result.exit_code == 1
    assert "locked" in result.output


# --- ingest, query, stats -------------------------------------------------------


def test_ingest_query_stats_round_trip(runner, db_path, text_file):
    result = _invoke(runner, ["ingest", str(text_file), "--name", "docs", "--path", db_path])
    assert result.exit_code == 0, result.output
    assert "Indexed 1 chunks from 1 files into 'docs'." in result.output

    result = _invoke(runner, ["query", "programming", "--name", "docs", "--path", db_path])
    assert result.exit_code == 0, result.output
    assert "dense search: 'programming'" in result.output
    assert "Python" in result.output

    result = _invoke(
        runner,
        ["query", "programming", "--name", "docs", "--path", db_path, "--explain"],
    )
    assert result.exit_code == 0, result.output
    assert "dense=" in result.output

    result = _invoke(
        runner, ["query", "programming", "--name", "docs", "--path", db_path, "--json"]
    )
    assert result.exit_code == 0, result.output
    assert '"items"' in result.output
    assert '"mode": "dense"' in result.output

    result = _invoke(runner, ["stats", "--name", "docs", "--path", db_path])
    assert result.exit_code == 0, result.output
    assert "Collection 'docs'" in result.output
    assert "Documents" in result.output
    assert "384" in result.output
    assert "dense" in result.output


def test_ingest_directory_honours_glob(runner, db_path, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("alpha alpha alpha", encoding="utf-8")
    (src / "b.txt").write_text("beta beta beta", encoding="utf-8")
    (src / "skip.md").write_text("# gamma", encoding="utf-8")
    result = _invoke(
        runner, ["ingest", str(src), "--path", db_path, "--glob", "*.txt", "--dedupe", "0.99"]
    )
    assert result.exit_code == 0, result.output
    assert "from 2 files" in result.output
    assert "skip.md" not in result.output


def test_ingest_missing_source_exits_1(runner, db_path, tmp_path):
    result = _invoke(runner, ["ingest", str(tmp_path / "nope.txt"), "--path", db_path])
    assert result.exit_code == 1
    assert "Not found" in result.output


def test_ingest_empty_directory_exits_1(runner, db_path, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _invoke(runner, ["ingest", str(empty), "--path", db_path])
    assert result.exit_code == 1
    assert "Nothing to ingest" in result.output


def test_ingest_reports_per_file_failure_and_continues(runner, db_path, text_file, monkeypatch):
    from vectrixdb.easy import Vectrix

    def add_document(self, source, **kwargs):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(Vectrix, "add_document", add_document)
    result = _invoke(runner, ["ingest", str(text_file), "--path", db_path])
    assert result.exit_code == 0, result.output
    assert "unreadable" in result.output
    assert "Indexed 0 chunks from 1 files" in result.output


def test_ingest_counts_skipped_near_duplicates(runner, db_path, text_file, monkeypatch):
    from vectrixdb.easy import AddReport, Vectrix

    def add_document(self, source, **kwargs):
        self.last_add_report = AddReport(added=["a:0"], skipped=["a:1", "a:2"])
        return 1

    monkeypatch.setattr(Vectrix, "add_document", add_document)
    result = _invoke(runner, ["ingest", str(text_file), "--path", db_path])
    assert result.exit_code == 0, result.output
    assert "skipped 2 near-duplicates" in result.output


def test_query_reports_truncation(runner, db_path, monkeypatch):
    from vectrixdb.easy import Vectrix

    class Hit:
        id = "h1"
        score = 0.5
        text = "x" * 400
        explain = None

    class Results:
        mode = "dense"
        truncated = True
        cut_count = 3

        def __iter__(self):
            return iter([Hit()])

    monkeypatch.setattr(Vectrix, "search", lambda self, *a, **k: Results())
    result = _invoke(runner, ["query", "anything", "--path", db_path])
    assert result.exit_code == 0, result.output
    assert "3 results cut to fit the budget" in result.output
    # A long snippet is shortened with an ellipsis.
    assert "..." in result.output


def test_query_requires_text(runner, db_path):
    result = _invoke(runner, ["query", "--path", db_path])
    assert result.exit_code != 0
    assert "Missing argument" in result.output


# --- mcp ------------------------------------------------------------------------


@pytest.fixture
def fake_mcp_main(monkeypatch):
    calls = []
    module = types.ModuleType("vectrixdb.mcp_server")
    module.main = lambda argv: calls.append(argv)
    monkeypatch.setitem(sys.modules, "vectrixdb.mcp_server", module)
    return calls


def test_mcp_builds_argv(runner, fake_mcp_main, db_path):
    result = _invoke(
        runner,
        ["mcp", "--name", "docs", "--path", db_path, "--transport", "sse", "--mode", "hybrid"],
    )
    assert result.exit_code == 0, result.output
    assert fake_mcp_main == [
        ["--name", "docs", "--path", db_path, "--transport", "sse", "--mode", "hybrid"]
    ]


def test_mcp_omits_mode_when_not_given(runner, fake_mcp_main):
    result = _invoke(runner, ["mcp"])
    assert result.exit_code == 0, result.output
    assert fake_mcp_main == [
        ["--name", "default", "--path", "./vectrixdb_data", "--transport", "stdio"]
    ]


# --- download-models and models-info ----------------------------------------------


@pytest.fixture
def models_dir(tmp_path, monkeypatch):
    d = tmp_path / "models"
    d.mkdir()
    monkeypatch.setenv("VECTRIXDB_MODELS_DIR", str(d))
    return d


def _install_fake(models_dir: Path, *types_: str) -> None:
    from vectrixdb.models import MODEL_CONFIG

    from vectrixdb.models.embedded import model_dir_name

    for mt in types_:
        sub = models_dir / model_dir_name(mt)
        sub.mkdir(parents=True, exist_ok=True)
        cfg = MODEL_CONFIG.get(mt, {})
        if mt == "sparse":
            name = cfg.get("vocab_file", "vocab.json")
        elif mt == "rebel":
            name = cfg.get("onnx_encoder_file", "encoder.onnx")
        else:
            name = cfg.get("onnx_file", "model.onnx")
        (sub / name).write_bytes(b"stub")


@pytest.fixture
def fake_download(monkeypatch):
    import vectrixdb.models as models

    calls = []
    monkeypatch.setattr(models, "download_models", lambda **kw: calls.append(kw))
    return calls


def test_download_models_downloads_when_missing(runner, models_dir, fake_download):
    result = _invoke(runner, ["download-models"])
    assert result.exit_code == 0, result.output
    assert fake_download == [{"model_type": "all", "force": False, "progress": True}]
    assert "Setup complete" in result.output
    assert str(models_dir) in result.output.replace("\n", "")


def test_download_models_skips_when_all_installed(runner, models_dir, fake_download):
    _install_fake(models_dir, "dense", "sparse", "reranker", "colbert")
    result = _invoke(runner, ["download-models"])
    assert result.exit_code == 0, result.output
    assert "All models already installed" in result.output
    assert fake_download == []


def test_download_models_skips_single_type_when_installed(runner, models_dir, fake_download):
    _install_fake(models_dir, "dense")
    result = _invoke(runner, ["download-models", "--type", "dense"])
    assert result.exit_code == 0, result.output
    assert "Model 'dense' already installed" in result.output
    assert fake_download == []


def _stock_wheel(models_dir: Path) -> None:
    """What a fresh install holds: the English models, each in its own folder."""
    for folder in ("bge_small_en", "reranker_en", "colbert"):
        (models_dir / folder).mkdir(parents=True, exist_ok=True)
        (models_dir / folder / "model.onnx").write_bytes(b"stub")
    (models_dir / "sparse").mkdir(exist_ok=True)
    (models_dir / "sparse" / "vocab.json").write_bytes(b"{}")


def test_download_models_fetches_the_multilingual_model_beside_the_bundled_one(
    runner, models_dir, fake_download
):
    """The command every multilingual error message names. On a fresh install
    it said the model was already installed, because the English one counted,
    and fetched nothing."""
    _stock_wheel(models_dir)
    result = _invoke(runner, ["download-models", "--type", "dense"])
    assert result.exit_code == 0, result.output
    assert "already installed" not in result.output
    assert fake_download == [{"model_type": "dense", "force": False, "progress": True}]


def test_download_models_on_a_fresh_install_fetches_the_multilingual_pair(
    runner, models_dir, fake_download
):
    _stock_wheel(models_dir)
    result = _invoke(runner, ["download-models"])
    assert result.exit_code == 0, result.output
    assert fake_download == [{"model_type": "all", "force": False, "progress": True}]


def test_download_models_help_names_what_the_package_already_holds(runner):
    result = _invoke(runner, ["download-models", "--help"])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "all-MiniLM-L6-v2" not in text, "not a model the package uses"
    for name in ("bge-small-en-v1.5", "ms-marco-MiniLM-L-12-v2", "multilingual-e5-small"):
        assert name in text, name


def test_download_models_force_redownloads(runner, models_dir, fake_download):
    _install_fake(models_dir, "dense", "sparse", "reranker", "colbert")
    result = _invoke(runner, ["download-models", "--force", "-t", "sparse"])
    assert result.exit_code == 0, result.output
    assert fake_download == [{"model_type": "sparse", "force": True, "progress": True}]


def test_download_models_import_error_explains_extra(runner, models_dir, monkeypatch):
    import vectrixdb.models as models

    def boom(**kw):
        raise ImportError("No module named optimum")

    monkeypatch.setattr(models, "download_models", boom)
    result = _invoke(runner, ["download-models"])
    assert result.exit_code == 1
    assert "Missing dependencies" in result.output
    assert "pip install vectrixdb" in result.output


def test_download_models_import_error_names_the_extra(runner, models_dir, monkeypatch):
    import vectrixdb.models as models

    def boom(**kw):
        raise ImportError("No module named optimum")

    monkeypatch.setattr(models, "download_models", boom)
    result = _invoke(runner, ["download-models"])
    assert "vectrixdb[setup-models]" in result.output


def test_download_models_generic_error_exits_1(runner, models_dir, monkeypatch):
    import vectrixdb.models as models

    def boom(**kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(models, "download_models", boom)
    result = _invoke(runner, ["download-models"])
    assert result.exit_code == 1
    assert "disk full" in result.output


def test_models_info_lists_missing_models(runner, models_dir):
    result = _invoke(runner, ["models-info"])
    assert result.exit_code == 0, result.output
    assert "Installed Models" in result.output
    assert "Not installed" in result.output
    assert "download-models" in result.output


def test_models_info_does_not_count_the_english_model_as_the_multilingual_one(
    runner, models_dir, monkeypatch
):
    import vectrixdb.models as models

    _stock_wheel(models_dir)
    asked = []
    real = models.is_models_installed

    def spy(model_type="all", **kw):
        asked.append((model_type, kw.get("exact", False)))
        return real(model_type, **kw)

    monkeypatch.setattr(models, "is_models_installed", spy)
    result = _invoke(runner, ["models-info"])
    assert result.exit_code == 0, result.output
    rows = [kind for kind, exact in asked if kind != "all"]
    assert rows and all(exact for kind, exact in asked if kind != "all"), asked
    assert real("dense", exact=True) is False


def test_models_info_when_everything_installed(runner, models_dir):
    """Every row reads Installed and the hint is gone. The hint asks
    is_models_installed("all"), which checks a fixed list of four including
    "colbert", the directory the English late-interaction model installs into.
    """
    from vectrixdb.models import MODEL_CONFIG

    _install_fake(models_dir, *MODEL_CONFIG.keys())
    result = _invoke(runner, ["models-info"])
    assert result.exit_code == 0, result.output
    assert "Not installed" not in result.output
    assert "Run 'vectrixdb download-models'" not in result.output


def test_models_info_error_exits_1(runner, models_dir, monkeypatch):
    import vectrixdb.models as models

    def boom(*a, **k):
        raise RuntimeError("cannot stat")

    monkeypatch.setattr(models, "is_models_installed", boom)
    result = _invoke(runner, ["models-info"])
    assert result.exit_code == 1
    assert "cannot stat" in result.output
