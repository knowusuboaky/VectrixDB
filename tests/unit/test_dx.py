"""Developer experience: adapters, the OpenAI-compatible embedder, plugins,
timing hooks, the CLI and the release script."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from vectrixdb import Vectrix

ROOT = Path(__file__).resolve().parents[2]


# =============================================================================
# OpenAI-compatible embedder, with a fake client
# =============================================================================


class FakeEmbeddingsAPI:
    """Stands in for openai.OpenAI().embeddings; deterministic vectors."""

    def __init__(self, dim=8):
        self.dim = dim
        self.calls = []

    def create(self, model, input):
        self.calls.append((model, list(input)))
        data = []
        # Return out of order on purpose: the embedder must sort by index.
        for i, text in reversed(list(enumerate(input))):
            rng = np.random.default_rng(abs(hash(text)) % (2**32))
            data.append(SimpleNamespace(index=i, embedding=rng.standard_normal(self.dim).tolist()))
        return SimpleNamespace(data=data)


class FakeOpenAI:
    def __init__(self, dim=8):
        self.embeddings = FakeEmbeddingsAPI(dim)


class TestOpenAIEmbedder:
    def test_batches_and_keeps_order(self):
        from vectrixdb.models import OpenAIEmbedder

        client = FakeOpenAI(dim=6)
        e = OpenAIEmbedder("m", client=client, batch_size=2)
        out = e.embed(["a", "b", "c"])
        assert out.shape == (3, 6) and out.dtype == np.float32
        assert [len(c[1]) for c in client.embeddings.calls] == [2, 1]
        assert e.dimension == 6
        # Same text, same vector, regardless of batch position.
        again = e.embed(["c", "a"])
        assert np.allclose(again[0], out[2]) and np.allclose(again[1], out[0])
        assert e("a").shape == (1, 6)

    def test_needs_a_key_unless_local(self, monkeypatch):
        """A missing key is a setting to fix, and it is said before anything is imported."""
        from vectrixdb.exceptions import ConfigurationError
        from vectrixdb.models import OpenAIEmbedder

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ConfigurationError, match="API key"):
            OpenAIEmbedder("m")
        pytest.importorskip("openai")
        local = OpenAIEmbedder("m", base_url="http://localhost:11434/v1")
        assert "localhost" in repr(local)

    def test_without_the_package_it_says_what_to_install_in_plain_words(self, monkeypatch):
        """It passed a whole sentence as the package's name, so the message said pip install OpenAIEmbedder needs..."""
        from vectrixdb.exceptions import DependencyError
        from vectrixdb.models import OpenAIEmbedder

        monkeypatch.setitem(sys.modules, "openai", None)
        with pytest.raises(DependencyError) as caught:
            OpenAIEmbedder("m", api_key="k")
        assert str(caught.value) == "'openai' is required for this feature. Install it with: pip install openai"

    def test_vectrix_takes_an_openai_reference(self, tmp_path, monkeypatch):
        import vectrixdb.models.openai_compat as mod

        monkeypatch.setattr(mod, "OpenAIEmbedder", lambda model, **kw: _Embedder(model))
        db = Vectrix("oa", path=str(tmp_path), dense_model="openai:my-model")
        assert db.model_type == "custom" and db.dimension == 5
        db.add(["hello"])
        assert db.count() == 1
        db.close()


class _Embedder:
    """Also the shape of an embedder plugin: constructed with no arguments."""

    def __init__(self, model="fives"):
        self.model = model
        self.dimension = 5

    def embed(self, texts):
        return np.ones((len(texts), 5), dtype=np.float32)

    __call__ = embed


# =============================================================================
# Plugins
# =============================================================================


class TestPlugins:
    def test_register_load_and_available(self):
        from vectrixdb import plugins

        undo = plugins.register("embedders", "unit-test", _Embedder)
        try:
            assert "unit-test" in plugins.available("embedders")
            assert plugins.load("embedders", "unit-test") is _Embedder
        finally:
            undo()
        assert "unit-test" not in plugins.available("embedders")
        with pytest.raises(plugins.PluginNotFound, match="unit-test"):
            plugins.load("embedders", "unit-test")
        with pytest.raises(ValueError):
            plugins.available("nope")

    def test_vectrix_uses_an_embedder_plugin(self, tmp_path):
        from vectrixdb import plugins

        undo = plugins.register("embedders", "fives", _Embedder)
        try:
            db = Vectrix("pl", path=str(tmp_path), dense_model="plugin:fives")
            assert db.dimension == 5
            db.add(["x", "y"])
            assert db.count() == 2
            db.close()
        finally:
            undo()

    def test_storage_plugin_through_the_factory(self):
        from vectrixdb import plugins
        from vectrixdb.core.storage import InMemoryStorage, StorageConfig, create_storage

        made = []

        def factory(config):
            made.append(config)
            return InMemoryStorage(config)

        undo = plugins.register("storage", "mem2", factory)
        try:
            storage = create_storage(StorageConfig(backend="plugin:mem2"))
            assert isinstance(storage, InMemoryStorage) and made
        finally:
            undo()
        with pytest.raises(plugins.PluginNotFound):
            create_storage(StorageConfig(backend="plugin:missing"))


# =============================================================================
# Timing hooks
# =============================================================================


class TestHooks:
    def test_search_and_add_fire_events(self, tmp_path):
        events = []
        db = Vectrix("hooks", path=str(tmp_path), on_search=events.append, on_add=events.append)
        db.add(["alpha beta", "gamma delta"])
        db.search("alpha", limit=1)
        ops = [e["op"] for e in events]
        assert ops == ["add", "search"]
        assert (
            events[0]["count"] == 2 and events[0]["ms"] >= 0 and events[0]["collection"] == "hooks"
        )
        assert events[1]["count"] == 1 and events[1]["mode"] == "dense"
        db.close()

    def test_a_failing_hook_does_not_break_the_query(self, tmp_path, caplog):
        def bad(event):
            raise RuntimeError("meter is down")

        db = Vectrix("hooks2", path=str(tmp_path), on_search=bad)
        db.add(["alpha beta"])
        assert db.search("alpha", limit=1).top.text == "alpha beta"
        assert "meter is down" in caplog.text
        db.close()


# =============================================================================
# Adapters
# =============================================================================


class TestLangChain:
    def test_round_trip_with_the_bundled_model(self, tmp_path):
        pytest.importorskip("langchain_core")
        from vectrixdb.integrations.langchain import VectrixVectorStore

        store = VectrixVectorStore("lc", path=str(tmp_path))
        ids = store.add_texts(
            ["Basalt forms when lava cools quickly.", "Sourdough is leavened by wild yeast."],
            metadatas=[{"topic": "rocks"}, {"topic": "bread"}],
        )
        assert len(ids) == 2
        docs = store.similarity_search("volcanic rock", k=1)
        assert docs[0].page_content.startswith("Basalt") and docs[0].metadata["topic"] == "rocks"
        scored = store.similarity_search_with_score("bread", k=1, filter={"topic": "bread"})
        assert scored[0][0].metadata["topic"] == "bread" and 0 <= scored[0][1] <= 1
        retriever = store.as_retriever(search_kwargs={"k": 1})
        assert retriever.invoke("yeast")[0].page_content.startswith("Sourdough")
        assert store.delete([ids[0]]) is True and store.db.count() == 1
        store.close()

    def test_with_a_langchain_embedding(self, tmp_path):
        pytest.importorskip("langchain_core")
        from langchain_core.embeddings import FakeEmbeddings

        from vectrixdb.integrations.langchain import VectrixVectorStore

        store = VectrixVectorStore.from_texts(
            ["one", "two"], embedding=FakeEmbeddings(size=12), name="lce", path=str(tmp_path)
        )
        assert store.db.dimension == 12 and store.db.model_type == "custom"
        assert len(store.similarity_search("one", k=2)) == 2
        store.close()


class TestLlamaIndex:
    def test_round_trip_with_supplied_vectors(self, tmp_path):
        pytest.importorskip("llama_index.core")
        from llama_index.core.schema import TextNode
        from llama_index.core.vector_stores.types import VectorStoreQuery

        from vectrixdb.integrations.llamaindex import VectrixLlamaStore

        store = VectrixLlamaStore("li", path=str(tmp_path))
        nodes = [
            TextNode(id_="n1", text="rocks", embedding=[1.0, 0.0, 0.0], metadata={"k": 1}),
            TextNode(id_="n2", text="bread", embedding=[0.0, 1.0, 0.0], metadata={"k": 2}),
        ]
        assert store.add(nodes) == ["n1", "n2"]
        result = store.query(VectorStoreQuery(query_embedding=[0.9, 0.1, 0.0], similarity_top_k=1))
        assert result.ids == ["n1"] and result.nodes[0].text == "rocks"
        assert result.nodes[0].metadata["k"] == 1
        store.delete(nodes[0].ref_doc_id or "n1")
        store.close()

    def test_query_needs_an_embedding(self, tmp_path):
        pytest.importorskip("llama_index.core")
        from llama_index.core.vector_stores.types import VectorStoreQuery

        from vectrixdb.integrations.llamaindex import VectrixLlamaStore

        store = VectrixLlamaStore("li2", path=str(tmp_path), dimension=3)
        with pytest.raises(ValueError, match="embed_model"):
            store.query(VectorStoreQuery(query_str="x", similarity_top_k=1))
        store.close()


# =============================================================================
# CLI
# =============================================================================


class TestCLI:
    @pytest.fixture
    def runner(self):
        from typer.testing import CliRunner

        return CliRunner()

    def test_ingest_query_stats(self, runner, tmp_path):
        from vectrixdb.cli import app

        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.md").write_text(
            "# Rocks\n\nBasalt forms when lava cools quickly.\n", encoding="utf-8"
        )
        (docs / "b.txt").write_text("Sourdough is leavened by wild yeast.", encoding="utf-8")
        db_path = str(tmp_path / "db")

        result = runner.invoke(
            app, ["ingest", str(docs), "--name", "cli", "--path", db_path, "--chunk", "markdown"]
        )
        assert result.exit_code == 0, result.output
        assert "Indexed 2" in result.output

        result = runner.invoke(
            app,
            [
                "query",
                "volcanic rock",
                "--name",
                "cli",
                "--path",
                db_path,
                "--limit",
                "1",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output[result.output.index("{") :])
        assert payload["items"][0]["text"].startswith("Basalt")

        result = runner.invoke(app, ["stats", "--name", "cli", "--path", db_path])
        assert result.exit_code == 0, result.output
        assert "Documents" in result.output and "2" in result.output

    def test_ingest_missing_path_fails_clearly(self, runner, tmp_path):
        from vectrixdb.cli import app

        result = runner.invoke(app, ["ingest", str(tmp_path / "nope"), "--path", str(tmp_path)])
        assert result.exit_code == 1 and "Not found" in result.output


# =============================================================================
# Release script
# =============================================================================


class TestRelease:
    def test_check_and_stamp(self, tmp_path, monkeypatch):
        sys.path.insert(0, str(ROOT / "scripts"))
        import release

        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text(
            "# Changelog\n\n## [2.2.0] - Unreleased\n\n### Added\n\n- a thing\n\n## [2.1.7]\n\n- old\n",
            encoding="utf-8",
        )
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "x"\nversion = "2.1.7"\n', encoding="utf-8")
        monkeypatch.setattr(release, "CHANGELOG", changelog)
        monkeypatch.setattr(release, "PYPROJECT", pyproject)

        assert release.main(["--check"]) == 0
        assert release.main(["2.2.0", "--dry-run"]) == 0
        assert 'version = "2.1.7"' in pyproject.read_text(encoding="utf-8")
        assert release.main(["2.2.0"]) == 0
        text = changelog.read_text(encoding="utf-8")
        assert "## [Unreleased]" in text and "## [2.2.0] - 20" in text
        assert 'version = "2.2.0"' in pyproject.read_text(encoding="utf-8")

        changelog.write_text(
            "# Changelog\n\n## [Unreleased]\n\n## [2.2.0] - 2026-09-11\n\n- a thing\n",
            encoding="utf-8",
        )
        assert release.main(["--check"]) == 1
        with pytest.raises(SystemExit):
            release.main(["not-a-version"])
