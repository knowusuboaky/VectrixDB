"""The same relevance from OpenSearch, from Azure AI Search, over REST, and on the page.

The two cloud stores are driven through the fakes, which score the way each
vendor documents: Azure ``1 / (1 + cosine_distance)``, OpenSearch either
``1 / (1 + d)`` or ``(2 - d) / 2`` depending on version and engine. The point
of every test here is that what comes out is the true cosine, whatever went
in, and that where no honest number exists there is none.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(__file__))

from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.core.storage import OpenSearchStorage, StorageBackend, StorageConfig  # noqa: E402

DASH = Path(__file__).resolve().parents[2] / "vectrixdb" / "dashboard"
JS = (DASH / "app.js").read_text(encoding="utf-8")
HTML = (DASH / "index.html").read_text(encoding="utf-8")
CSS = (DASH / "app.css").read_text(encoding="utf-8")

NEAR = [0.6, 0.8, 0.0, 0.0]  # cosine 0.6 with the query below
QUERY = [1.0, 0.0, 0.0, 0.0]


# ------------------------------------------------------------- opensearch ---


def opensearch(formula, **config):
    from fake_opensearch import FakeOpenSearch

    client = FakeOpenSearch()
    client.cosine_score = formula
    store = OpenSearchStorage(
        StorageConfig(backend=StorageBackend.OPENSEARCH, opensearch_index_prefix="t", **config),
        client=client,
    )
    store.create_collection("docs", {"dimension": 4})
    store.insert("docs", "same", {"text_content": "alpha", "_embedding": QUERY})
    store.insert("docs", "near", {"text_content": "beta", "_embedding": NEAR})
    return store


class TestOpenSearch:
    @pytest.mark.parametrize("formula", ["reciprocal", "half"])
    def test_the_same_similarity_whichever_way_the_cluster_scores(self, formula):
        store = opensearch(formula)
        hits = {
            doc_id: (data, score)
            for doc_id, data, score in store.vector_search("docs", QUERY, limit=2)
        }
        assert store._cosine_formula == formula, (
            "the perfect match fits both formulas; the second hit is what settled it"
        )
        assert hits["same"][0]["_vx_relevance"] == pytest.approx(1.0, abs=1e-6)
        assert hits["near"][0]["_vx_relevance"] == pytest.approx(0.6, abs=1e-6)
        raw = {"reciprocal": 1 / 1.4, "half": 0.8}[formula]
        assert 1.0 - hits["near"][1] == pytest.approx(raw, abs=1e-6), (
            "one minus the engine's score, as every store hands it on"
        )

    def test_a_formula_somebody_configured_is_taken_at_their_word(self):
        store = opensearch("reciprocal", opensearch_score_formula="half")
        near = {d: data for d, data, _ in store.vector_search("docs", QUERY, limit=2)}["near"]
        assert near["_vx_relevance"] == pytest.approx(2 * (1 / 1.4) - 1, abs=1e-6), (
            "wrong, because they said so: configuration is the operator's"
        )

    def test_nothing_to_go_on_means_no_number(self):
        store = opensearch("reciprocal")
        assert store._score_formula([], "dense_embedding", QUERY) is None
        only_perfect = [{"_score": 1.0, "_source": {"dense_embedding": QUERY}}]
        assert store._score_formula(only_perfect, "dense_embedding", QUERY) is None
        no_vector = [{"_score": 0.7, "_source": {}}]
        assert store._score_formula(no_vector, "dense_embedding", QUERY) is None

    def test_through_the_library(self, monkeypatch, tmp_path):
        from fake_opensearch import FakeOpenSearch

        from vectrixdb.core.database import VectrixDB

        client = FakeOpenSearch()
        client.cosine_score = "half"
        store = OpenSearchStorage(
            StorageConfig(backend=StorageBackend.OPENSEARCH, opensearch_index_prefix="t"),
            client=client,
        )
        monkeypatch.setattr("vectrixdb.core.database.create_storage", lambda config: store)
        backend = VectrixDB.with_opensearch("https://x.us-east-1.aoss.amazonaws.com")
        vectors = {"alpha": QUERY, "beta": NEAR, "question": QUERY}
        docs = Vectrix(
            "memos",
            storage_backend=backend,
            path=str(tmp_path),
            dimension=4,
            mode="dense",
            embedding_cache=False,
            embed_fn=lambda texts: np.array([vectors[t] for t in texts], dtype=np.float32),
        )
        docs.add(["alpha", "beta"], ids=["same", "near"])
        hits = {h.id: h for h in docs.search("question", mode="dense", limit=2)}
        assert (
            hits["near"].relevance == pytest.approx(0.6, abs=1e-4)
            and hits["near"].relevance_kind == "similarity"
        )
        assert hits["same"].relevance == pytest.approx(1.0, abs=1e-4)
        docs.close()

    @pytest.mark.parametrize("formula", ["reciprocal", "half"])
    def test_a_served_score_is_the_engines_own_and_a_threshold_keeps_the_best(
        self, formula, monkeypatch, tmp_path
    ):
        """It came back as one minus the engine's score, so a threshold kept the worst matches and dropped the best."""
        from fake_opensearch import FakeOpenSearch

        from vectrixdb.core.database import VectrixDB

        client = FakeOpenSearch()
        client.cosine_score = formula
        store = OpenSearchStorage(
            StorageConfig(backend=StorageBackend.OPENSEARCH, opensearch_index_prefix="t"),
            client=client,
        )
        monkeypatch.setattr("vectrixdb.core.database.create_storage", lambda config: store)
        backend = VectrixDB.with_opensearch("https://x.us-east-1.aoss.amazonaws.com")
        vectors = {"alpha": QUERY, "beta": NEAR}
        docs = Vectrix(
            "memos",
            storage_backend=backend,
            path=str(tmp_path),
            dimension=4,
            mode="dense",
            embedding_cache=False,
            embed_fn=lambda texts: np.array([vectors[t] for t in texts], dtype=np.float32),
        )
        docs.add(["alpha", "beta"], ids=["same", "near"])
        served = docs._collection.search(QUERY, limit=2, use_backend=True, use_cache=False)
        near = {"reciprocal": 1 / 1.4, "half": 0.8}[formula]
        assert [(r.id, round(r.score, 6)) for r in served.results] == [
            ("same", 1.0),
            ("near", round(near, 6)),
        ], "the engine's own score, best first"
        kept = docs._collection.search(
            QUERY, limit=2, use_backend=True, use_cache=False, score_threshold=0.9
        )
        assert [r.id for r in kept.results] == ["same"], (
            "the threshold keeps the best match, not the worst"
        )
        docs.close()


# ------------------------------------------------------------------ azure ---

# Only the Azure tests need the SDK. A module-level importorskip here used to
# skip this whole file without it, the OpenSearch, REST and page tests with it,
# which is every CI job: none of them installs the azure extra.
HAS_AZURE = (
    importlib.util.find_spec("azure") is not None
    and importlib.util.find_spec("azure.search.documents") is not None
)


def azure(monkeypatch, tmp_path, embeddings="vectrixdb", **overrides):
    from fake_azure_search import FakeIndexClient

    from vectrixdb.core.database import VectrixDB
    from vectrixdb.core.storage_azure import AzureSearchStorage

    theirs = {
        "alpha": [0, 1, 0, 0, 0, 0],
        "beta": [0, 0.6, 0.8, 0, 0, 0],
        "question": [0, 1, 0, 0, 0, 0],
    }
    fake = FakeIndexClient(
        vectorize=(lambda text: theirs.get(text, [0, 0, 0, 0, 0, 1]))
        if embeddings != "vectrixdb"
        else None
    )
    config = StorageConfig(
        backend=StorageBackend.AZURE_SEARCH,
        azure_search_index_prefix="t",
        azure_search_embeddings=embeddings,
        azure_search_vectorizer={
            "endpoint": "https://aoai.example.test",
            "deployment": "d",
            "dimensions": 6,
        }
        if embeddings != "vectrixdb"
        else None,
        azure_search_embed_fn=(lambda texts: [theirs.get(t, [0, 0, 0, 0, 0, 1]) for t in texts])
        if embeddings != "vectrixdb"
        else None,
        **overrides,
    )
    storage = AzureSearchStorage(config, index_client=fake, client_factory=fake.get_search_client)
    storage.connect()
    monkeypatch.setattr("vectrixdb.core.database.create_storage", lambda _config: storage)
    backend = VectrixDB.with_azure_search("https://svc.search.windows.net", key="k")
    ours = {"alpha": QUERY, "beta": NEAR, "question": QUERY}
    docs = Vectrix(
        "memos",
        storage_backend=backend,
        path=str(tmp_path),
        dimension=4,
        mode="hybrid",
        embedding_cache=False,
        embed_fn=lambda texts: np.array([ours[t] for t in texts], dtype=np.float32),
    )
    docs.add(["alpha", "beta"], ids=["same", "near"])
    return docs, storage, fake


@pytest.mark.skipif(not HAS_AZURE, reason="the azure extra is not installed")
class TestAzureAiSearch:
    def test_one_vector_is_turned_back_into_the_cosine(self, monkeypatch, tmp_path):
        docs, storage, _ = azure(monkeypatch, tmp_path)
        hits = {
            doc_id: (data, distance)
            for doc_id, data, distance in storage.vector_search("memos", QUERY, limit=2)
        }
        assert hits["near"][0]["_vx_relevance"] == pytest.approx(0.6, abs=1e-6)
        assert 1.0 - hits["near"][1] == pytest.approx(1 / 1.4, abs=1e-6), (
            "what the service sent was 1 / (1 + distance), as Microsoft documents"
        )
        found = {h.id: h for h in docs.search("question", mode="dense", limit=2)}
        assert found["near"].relevance == pytest.approx(0.6, abs=1e-4) and found[
            "same"
        ].relevance == pytest.approx(1.0, abs=1e-4)
        docs.close()

    def test_two_vectors_come_back_as_ranks_so_the_similarity_is_asked_for(
        self, monkeypatch, tmp_path
    ):
        docs, storage, fake = azure(monkeypatch, tmp_path, embeddings="both")
        with storage.using_vectors("both", "question"):
            hits = {
                doc_id: (data, distance)
                for doc_id, data, distance in storage.vector_search("memos", QUERY, limit=2)
            }
        assert 1.0 - hits["near"][1] < 0.05, "a fused rank, which is no similarity at all"
        assert hits["near"][0]["_vx_relevance"] == pytest.approx(0.6, abs=1e-6), (
            "from the collection's own vector, asked alone"
        )
        docs.close()

    def test_a_hybrid_search_reports_the_similarity_and_not_the_fused_rank(
        self, monkeypatch, tmp_path
    ):
        docs, storage, _ = azure(monkeypatch, tmp_path)
        hits = {h.id: h for h in docs.search("question", mode="hybrid", limit=2, rerank=False)}
        assert (
            hits["near"].relevance == pytest.approx(0.6, abs=1e-4)
            and hits["near"].relevance_kind == "similarity"
        )
        assert hits["near"].score != pytest.approx(0.6, abs=1e-3), (
            "whatever the service ranked by, it was not this"
        )
        docs.close()

    def test_it_can_be_turned_off_to_save_the_second_question(self, monkeypatch, tmp_path):
        docs, storage, _ = azure(monkeypatch, tmp_path, azure_search_relevance=False)
        hits = {h.id: h for h in docs.search("question", mode="hybrid", limit=2, rerank=False)}
        assert hits["near"].relevance is None, "no number is better than a wrong one"
        docs.close()

    def test_the_semantic_rankers_verdict_leads_when_it_is_on(self):
        from vectrixdb.core.storage_azure import AzureSearchStorage

        judged = AzureSearchStorage._judged({"_semantic_score": 3.0}, {"x": 0.4}, "x", fused=True)
        assert judged["_vx_relevance"] == 0.75 and judged["_vx_relevance_kind"] == "reranker", (
            "0 to 4, as Microsoft documents it"
        )
        found = AzureSearchStorage._judged({}, {"x": 0.4}, "y", fused=True)
        assert "_vx_relevance" not in found and found["_vx_matched_by"] == ["keywords"], (
            "not among the nearest, so the words found it"
        )


# ------------------------------------------------------------------- rest ---


class TestOverRest:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from vectrixdb.api.server import create_app

        for name in ("VECTRIXDB_API_KEY", "VECTRIXDB_SIGNIN", "VECTRIXDB_KEEP_SOURCE"):
            monkeypatch.delenv(name, raising=False)
        root = tmp_path / "db"
        db = Vectrix(
            "docs",
            path=str(root),
            dimension=4,
            tier="hybrid",
            embedding_cache=False,
            embed_fn=lambda texts: np.array(
                [QUERY if "alpha" in t else NEAR for t in texts], dtype=np.float32
            ),
        )
        db.add(["alpha memo", "beta memo"], ids=["same", "near"])
        db.close()
        with TestClient(create_app(db_path=str(root), enable_dashboard=False)) as c:
            yield c

    def test_a_search_reply_carries_it(self, client):
        results = client.post(
            "/api/v1/collections/docs/search", json={"query": QUERY, "limit": 2}
        ).json()["data"]["results"]
        assert [(r["id"], r["relevance_kind"], r["matched_by"]) for r in results] == [
            ("same", "similarity", ["meaning"]),
            ("near", "similarity", ["meaning"]),
        ]
        assert results[1]["relevance"] == pytest.approx(0.6, abs=1e-4) and results[1][
            "score"
        ] == pytest.approx(0.6, abs=1e-4)

    def test_keyword_says_it_is_relative(self, client):
        result = client.post(
            "/api/v1/collections/docs/keyword-search", json={"query_text": "alpha", "limit": 1}
        ).json()["data"]["results"][0]
        assert (
            result["relevance"] == 1.0
            and result["relevance_kind"] == "relative"
            and result["matched_by"] == ["keywords"]
        )

    def test_health_says_what_a_collection_can_do_and_what_it_is_built_with(self, client):
        health = client.get("/api/v1/collections/docs/health").json()["data"]
        assert health["capabilities"] == {
            "dense": True,
            "keyword": True,
            "hybrid": True,
            "graph": False,
        }
        assert (
            health["services"]["stored_in"] == "SQLite" and health["services"]["extracted_by"] == []
        )
        assert isinstance(health["services"]["embedded_by"], list)

    def test_a_collection_with_no_text_index_cannot_do_keywords(self, client):
        client.post(
            "/api/v1/collections",
            json={"name": "plain", "dimension": 4, "enable_text_index": False},
        )
        caps = client.get("/api/v1/collections/plain/health").json()["data"]["capabilities"]
        assert caps["dense"] is True and caps["keyword"] == caps["hybrid"]

    def test_the_services_behind_a_collection_are_named_as_people_know_them(self):
        from vectrixdb.api import inspection

        assert (
            inspection._STORE_NAMES["AzureSearchStorage"] == "Azure AI Search"
            and inspection._STORE_NAMES["OpenSearchStorage"] == "OpenSearch"
        )
        assert inspection._VECTOR_NAMES == {"azure": "Azure OpenAI", "bedrock": "Amazon Bedrock"}
        assert (
            inspection._EXTRACTOR_NAMES["Textract"] == "Amazon Textract"
            and inspection._EXTRACTOR_NAMES["AzureDocumentIntelligence"]
            == "Azure Document Intelligence"
        )

    def test_what_read_the_files_comes_from_the_document_store(self, tmp_path, monkeypatch):
        from vectrixdb.api import inspection
        from vectrixdb.core.database import VectrixDB

        class Store:
            def entries(self):
                return {
                    "a": {"extractor": "Textract"},
                    "b": {"extractor": "https://ocr.example.com/extract?lang=en"},
                    "c": {"extractor": "Textract"},
                    "d": {},
                }

        monkeypatch.setattr("vectrixdb.api.documents.store_for", lambda name: Store())
        database = VectrixDB(str(tmp_path))
        coll = database.create_collection(name="c", dimension=4)
        found = inspection._services(database, coll, "c", with_extractors=True)
        assert found["extracted_by"] == ["Amazon Textract", "ocr.example.com"], (
            "once each, a service by its name and an endpoint by its host"
        )
        assert (
            inspection._services(database, coll, "c", with_extractors=False)["extracted_by"] == []
        )
        database.close()


# -------------------------------------------------------------- the page ---


class TestThePage:
    def test_relevance_leads_and_the_score_sits_under_it(self):
        verdict = JS[JS.index("function verdict(r)") : JS.index("function textQuality(")]
        assert verdict.index('class="pct') < verdict.rindex("${raw}"), (
            "the number people read comes first"
        )
        assert (
            "Math.round(r.relevance * 100)}%" in verdict
            and "score ${Number(r.score || 0).toFixed(4)}" in verdict
        )
        assert "no relevance from this engine" in verdict, (
            "and an engine that gives none says so instead of showing a blank"
        )
        assert "${verdict(r)}" in JS and '<div class="score">${Number(r.score' not in JS

    def test_each_kind_explains_itself_and_only_two_kinds_get_a_word(self):
        words = JS[JS.index("const RELEVANCE_WORDS") : JS.index("function verdict(r)")]
        for kind in ("similarity", "reranker", "relative", "distance"):
            assert f"  {kind}: {{" in words
        assert words.count("cuts: null") == 2, (
            "a share of the best hit is not strong or weak, and neither is a distance"
        )

    def test_the_quality_pill_says_what_it_is_about(self):
        assert "text quality ${Number(q).toFixed(2)}" in JS and "pill(`quality ${" not in JS
        assert "It is about the text, not about how well it matches your search." in JS

    def test_who_found_a_chunk_is_said(self):
        assert "found by ${esc(r.matched_by.join(' + '))}" in JS

    def test_a_mode_the_collection_cannot_do_is_greyed_out_and_says_why(self):
        modes = JS[JS.index("async function availableModes()") : JS.index("function setMode(")]
        assert (
            "b.disabled = off" in modes
            and "aria-disabled" in modes
            and "b.title = off ? why[b.dataset.mode]" in modes
        )
        assert "if (why[state.searchMode]) state.searchMode = 'dense';" in modes, (
            "the page never sits on a mode that cannot run"
        )
        assert "$('s-collection').onchange = availableModes;" in JS
        assert "if (chip && chip.disabled) return;" in JS
        assert re.search(r"^\.chip:disabled \{[^}]*cursor: not-allowed", CSS, re.M)
        assert JS.count("availableModes(); }, 50)") == 1, (
            "one helper picks the collection once the search page has loaded"
        )
        assert (
            "function searchIn(name) { go('#/search'); setTimeout(() => { $('s-collection').value = name; availableModes(); }, 50); }"
            in JS
        )
        assert JS.count("on('click', ['searchIn', ") == 1, (
            "the collection header opens Search through it; a card opens the collection, and a guest never searches"
        )

    def test_a_card_says_what_a_collection_can_do_and_is_built_with_not_how_it_was_once_opened(
        self,
    ):
        assert JS.count("searchWays(c, h)") >= 2 and JS.count("builtWith(c, h)") >= 2
        assert "const TIER_TAGS = ['dense', 'sparse', 'hybrid', 'ultimate', 'graph'];" in JS
        assert "pill(mode, 'acc')" not in JS, "a card said hybrid beside a tag that said dense"
        built = JS[JS.index("function builtWith(") : JS.index("function searchWays(")]
        for title in (
            "Where the vectors are stored",
            "The model that embedded the text",
            "What read the files",
        ):
            assert title in built

    def test_the_new_icons_are_drawn(self):
        table = JS[JS.index("const paths = {") : JS.index("return `<svg")]
        assert "    cpu: '" in table and "    scan: '" in table

    def test_the_copy_has_no_dashes(self):
        assert "—" not in JS and "—" not in CSS
