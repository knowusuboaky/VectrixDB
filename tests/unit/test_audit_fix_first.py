"""Regression tests for the highest-severity findings of the September audit.

Each of these reproduces a failure that returned a normal-looking answer: an
empty result, an unfiltered result, a deletion that went further than asked,
or a credential sent somewhere it did not belong. Every one is written to
fail against the code as it stood before the fix.

Everything here runs offline against in-memory doubles. No cloud service, no
Docker, no network.
"""

from __future__ import annotations

import tempfile
import types
import warnings
from pathlib import Path

import numpy as np
import pytest

from vectrixdb import Vectrix, VectrixSync
from vectrixdb.core.database import VectrixDB, validate_collection_name
from vectrixdb.exceptions import (
    InvalidCollectionName,
    QuantizationError,
    SearchError,
    SparseModelUnavailableWarning,
    VectrixError,
)


# ---------------------------------------------------------------------------
# The optional-argument family: an explicit falsy value is not "not given"
# ---------------------------------------------------------------------------


class TestExplicitEmptyIsNotAbsent:
    def test_forgetting_an_empty_id_list_deletes_nothing(self, tmp_path):
        """An agent whose filter matched nothing sends ids=[]. Read for
        truthiness that fell through to the unscoped branch, which with no
        session deletes every turn in every session."""
        from vectrixdb.mcp_server import tool_forget

        db = Vectrix("mem", path=str(tmp_path))
        for who in ("alice", "bob"):
            for n in range(3):
                db.remember(f"{who} said thing {n}", session=who, role="user")
        before = len(db.recall("thing", limit=50))
        assert before > 0

        message = tool_forget(db, ids=[])

        assert "Forgot 0 memories" in message
        assert len(db.recall("thing", limit=50)) == before
        db.close()

    def test_a_user_in_no_groups_matches_no_documents(self, tmp_path):
        """The docstring says only None skips ACL. An empty principal list is
        a user who belongs to nothing, which must match nothing rather than
        everything."""
        db = VectrixDB(str(tmp_path))
        col = db.create_collection("acl", dimension=4)
        col.add(
            ids=["secret", "public"],
            vectors=[[1.0, 0, 0, 0], [0.9, 0.1, 0, 0]],
            metadata=[{"_acl": ["group:hr"]}, {"_acl": ["group:all"]}],
        )
        query = np.array([1.0, 0, 0, 0], dtype="float32")

        everything = col.enterprise_search(query, user_principals=None, limit=10)
        nothing = col.enterprise_search(query, user_principals=[], limit=10)

        assert len(everything.results) == 2, "None still means no ACL filtering"
        assert nothing.results == []
        db.close()

    def test_syncing_no_collections_copies_no_collections(self, tmp_path):
        """full(collections=[]) asks for nothing to be synced."""
        source = VectrixDB(str(tmp_path / "src"))
        target = VectrixDB(str(tmp_path / "dst"))
        for name in ("a", "b", "c"):
            source.create_collection(name, dimension=4).add(ids=["x"], vectors=[[1.0, 0, 0, 0]])

        VectrixSync(source=source, target=target).full(collections=[])
        assert [c.name for c in target.list_collections()] == []

        VectrixSync(source=source, target=target).full(collections=["a"])
        assert [c.name for c in target.list_collections()] == ["a"]
        source.close()
        target.close()

    def test_an_empty_api_key_is_not_the_environment_key(self, monkeypatch):
        """Pointing the embedder at a local server with api_key="" must not
        hand that endpoint the caller's real OPENAI_API_KEY."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-REAL-PRODUCTION-KEY")
        seen: dict = {}

        class FakeOpenAI:
            def __init__(self, api_key=None, base_url=None, timeout=None):
                seen["api_key"] = api_key

        fake = types.ModuleType("openai")
        fake.OpenAI = FakeOpenAI
        monkeypatch.setitem(__import__("sys").modules, "openai", fake)

        from vectrixdb.models.openai_compat import OpenAIEmbedder

        OpenAIEmbedder(api_key="", base_url="http://localhost:11434/v1", model="m")
        assert seen["api_key"] != "sk-REAL-PRODUCTION-KEY"

        seen.clear()
        OpenAIEmbedder(base_url="http://localhost:11434/v1", model="m")
        assert seen["api_key"] == "sk-REAL-PRODUCTION-KEY", "omitting it still uses the env"


# ---------------------------------------------------------------------------
# A failure must not look like an ordinary answer
# ---------------------------------------------------------------------------


class TestFailuresAreVisible:
    def _lakebase_that_fails(self):
        from vectrixdb.core.storage import LakebaseStorage, StorageBackend, StorageConfig

        storage = LakebaseStorage(
            StorageConfig(backend=StorageBackend.LAKEBASE, lakebase_schema="t")
        )

        class Boom:
            def cursor(self, *a, **k):
                raise RuntimeError("connection reset by peer")

        storage._conn = Boom()
        return storage

    def test_a_failed_vector_search_raises_instead_of_returning_nothing(self):
        """It printed the error and returned [], so a connection failure and
        a genuinely empty collection were the same answer."""
        storage = self._lakebase_that_fails()
        with pytest.raises(SearchError) as caught:
            storage.vector_search("docs", [0.1, 0.2], 10)
        assert isinstance(caught.value, VectrixError)
        assert "connection reset" in str(caught.value)

    def test_a_failed_hybrid_search_does_not_quietly_become_a_dense_one(self):
        storage = self._lakebase_that_fails()
        with pytest.raises(SearchError):
            storage.hybrid_search("docs", [0.1, 0.2], {1: 0.5}, 10)

    def test_a_collection_that_will_not_load_is_reported(self, tmp_path, monkeypatch):
        """It was dropped from the database after a printed line, so listing
        showed nothing and the caller could conclude the data was gone."""
        db = VectrixDB(str(tmp_path))
        db.create_collection("wanted", dimension=4).add(ids=["x"], vectors=[[1.0, 0, 0, 0]])
        db.close()

        import vectrixdb.core.database as database_module

        def explode(*args, **kwargs):
            raise RuntimeError("index file is corrupt")

        monkeypatch.setattr(database_module, "Collection", explode)

        with pytest.warns(database_module.CollectionLoadWarning):
            reopened = VectrixDB(str(tmp_path))

        assert "wanted" in reopened.failed_collections
        assert "corrupt" in reopened.failed_collections["wanted"]
        with pytest.raises(VectrixError, match="load failed"):
            reopened.get_collection("wanted")

    def test_thresholds_that_would_be_nan_are_refused(self):
        """A NaN threshold is silent and total: every comparison against it
        is false, so every vector encodes to the same code while the
        quantiser still reports itself fitted."""
        from vectrixdb import BinaryQuantizer

        with pytest.raises(QuantizationError):
            BinaryQuantizer(dimension=8, learn_thresholds=True).fit(
                np.zeros((0, 8), dtype="float32")
            )
        with pytest.raises(QuantizationError):
            BinaryQuantizer(dimension=8, learn_thresholds=True, threshold_samples=0).fit(
                np.random.rand(100, 8).astype("float32")
            )
        with pytest.raises(QuantizationError):
            BinaryQuantizer(dimension=8, learn_thresholds=True).fit(
                np.full((10, 8), np.nan, dtype="float32")
            )

        fitted = BinaryQuantizer(dimension=8, learn_thresholds=True)
        fitted.fit(np.random.rand(50, 8).astype("float32"))
        codes = fitted.encode(np.random.rand(5, 8).astype("float32"))
        assert len({bytes(c) for c in codes}) > 1, "a working fit still separates vectors"


# ---------------------------------------------------------------------------
# A name is not a path
# ---------------------------------------------------------------------------


class TestCollectionNamesAreValidated:
    @pytest.mark.parametrize(
        "name",
        ["../escape", "..\\escape", "a/b", "a\\b", "..", ".", "", "   ", "con", "c:x"],
    )
    def test_a_name_that_is_not_safe_as_a_directory_is_refused(self, name):
        with pytest.raises(InvalidCollectionName):
            validate_collection_name(name)

    @pytest.mark.parametrize("name", ["docs", "my_docs", "my-docs", "v2.1", "文書"])
    def test_ordinary_names_still_pass(self, name):
        assert validate_collection_name(name) == name

    def test_the_name_is_also_a_valueerror(self):
        """It used to raise a bare ValueError, so handlers keep working."""
        with pytest.raises(ValueError):
            validate_collection_name("../escape")

    def test_nothing_is_written_outside_the_database_directory(self, tmp_path):
        """The name became a path under the database directory, so two dots
        wrote the collection's files into the parent."""
        base = tmp_path / "db"
        base.mkdir()
        db = VectrixDB(str(base))
        with pytest.raises(InvalidCollectionName):
            db.create_collection("../escaped", dimension=4)
        assert list(tmp_path.iterdir()) == [base]
        db.close()

    def test_the_rest_route_answers_bad_request_not_conflict(self, tmp_path):
        fastapi = pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from vectrixdb.api.server import create_app

        base = tmp_path / "db"
        base.mkdir()
        with TestClient(create_app(db_path=str(base))) as client:
            refused = client.post("/api/v1/collections", json={"name": "../pwned", "dimension": 4})
            assert refused.status_code == 400

            assert (
                client.post(
                    "/api/v1/collections", json={"name": "fine", "dimension": 4}
                ).status_code
                == 200
            )
            # a real conflict is still a conflict
            assert (
                client.post(
                    "/api/v1/collections", json={"name": "fine", "dimension": 4}
                ).status_code
                == 409
            )
        assert sorted(p.name for p in tmp_path.iterdir()) == ["db"]


# ---------------------------------------------------------------------------
# Arguments that were accepted and then ignored
# ---------------------------------------------------------------------------


class TestArgumentsThatWereIgnored:
    def test_reranking_by_vector_actually_reranks(self, tmp_path):
        """Candidates reach the reranker without vectors and no call site
        supplied a way to fetch them, so 'exact' silently returned the input
        order and 'mmr' returned nothing at all."""
        db = Vectrix("notes", path=str(tmp_path))
        db.add(
            [
                "cats nap on warm windowsills",
                "dogs fetch balls in the park",
                "felines groom themselves often",
                "puppies chew on shoes",
            ]
        )

        plain = list(db.search("cats", limit=3))
        assert plain, "the corpus must match something for this test to mean anything"

        exact = list(db.search("cats", limit=3, rerank="exact"))
        mmr = list(db.search("cats", limit=3, rerank="mmr"))

        assert exact, "exact reranking must not empty the result"
        assert mmr, "mmr reranking returned nothing at all"
        assert len(mmr) == len(plain)
        db.close()

    def test_mmr_falls_back_rather_than_returning_nothing(self):
        """Even with no vectors available at all, a diversity rerank of a
        non-empty candidate set must not be empty."""
        from vectrixdb.core.advanced_search import RerankConfig, Reranker, RerankMethod

        candidates = [
            {"id": "a", "score": 0.2, "vector": None},
            {"id": "b", "score": 0.9, "vector": None},
        ]
        out = Reranker(RerankConfig(method=RerankMethod.MMR)).rerank(
            query_vector=np.array([1.0, 0.0], dtype="float32"),
            candidates=candidates,
            limit=2,
        )
        assert [c["id"] for c in out] == ["b", "a"]

    def test_a_sparse_model_that_cannot_run_says_so(self, tmp_path):
        """Hybrid search on a local collection is BM25 whatever sparse_model
        says, and it used to return BM25 results with no warning."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            db = Vectrix("n", path=str(tmp_path / "a"), mode="hybrid", sparse_model="splade")
            db.close()
        assert any(issubclass(w.category, SparseModelUnavailableWarning) for w in caught), (
            "naming an unreachable sparse model must warn"
        )

        with warnings.catch_warnings(record=True) as quiet:
            warnings.simplefilter("always")
            db = Vectrix("n", path=str(tmp_path / "b"), mode="hybrid", sparse_model="bm25")
            db.close()
        assert not [w for w in quiet if issubclass(w.category, SparseModelUnavailableWarning)], (
            "the model that does run must not warn"
        )


class TestSelectiveFiltersFindTheirDocuments:
    def test_a_one_percent_filter_returns_results(self, tmp_path):
        """The filter is applied to whatever the index already chose, and the
        window was a flat limit * 10. At a one percent pass rate that window
        held no matching document, so the search answered with an empty list
        while ten documents matched. An empty result is the one answer that
        looks like an ordinary outcome."""
        db = Vectrix("t", path=str(tmp_path))
        db.add(
            [f"record number {i} about assorted office equipment" for i in range(1000)],
            metadata=[{"rare": i % 100 == 0} for i in range(1000)],
        )

        for limit in (5, 10):
            hits = list(db.search("office equipment", limit=limit, filter={"rare": True}))
            assert len(hits) == limit, f"limit={limit} came back with {len(hits)}"
            assert all(h.metadata["rare"] is True for h in hits)
        db.close()

    def test_a_filter_matching_fewer_than_the_limit_returns_them_all(self, tmp_path):
        """Widening must stop at the end of the index rather than spin."""
        db = Vectrix("t", path=str(tmp_path))
        db.add(
            [f"record number {i} about office equipment" for i in range(200)],
            metadata=[{"only": i < 3} for i in range(200)],
        )
        hits = list(db.search("office equipment", limit=50, filter={"only": True}))
        assert len(hits) == 3
        db.close()

    def test_an_unfiltered_search_is_unchanged(self, tmp_path):
        db = Vectrix("t", path=str(tmp_path))
        db.add([f"record number {i} about office equipment" for i in range(200)])
        assert len(list(db.search("office equipment", limit=10))) == 10
        db.close()


class TestPromisesTheCodeNowKeeps:
    """The second tier of the September audit: arguments and claims that were
    documented and then not honoured."""

    def test_the_models_installed_check_says_yes_on_a_correct_install(self):
        """The bundled cross-encoder ships as reranker_en and the downloader
        writes to reranker. Checking only the second answered False on a stock
        wheel, and the twelve tests gated on it skipped everywhere, CI
        included. Both directories count."""
        from vectrixdb import is_models_installed
        from vectrixdb.models.embedded import MODEL_DIR_ALTERNATIVES, model_dir_name

        assert "reranker_en" in MODEL_DIR_ALTERNATIVES["reranker"]
        assert model_dir_name("reranker") == "reranker", "the downloader target is unchanged"
        for kind in ("dense", "sparse", "reranker", "colbert"):
            assert is_models_installed(kind), f"{kind} reports missing"
        assert is_models_installed()

    def test_the_dashboard_flag_reaches_the_app(self, monkeypatch, tmp_path):
        """create_app honoured the flag; run_server starts the singleton by
        import string, so the CLI's --no-dashboard never arrived."""
        pytest.importorskip("fastapi")
        import importlib
        import sys

        from fastapi.testclient import TestClient

        import vectrixdb

        # The modules as they were, put back after. Left re-imported, every later
        # test held the old server while code importing it lazily got the new
        # one, whose database was never set up, and failed by the order it ran in.
        saved = {name: module for name, module in sys.modules.items() if name.startswith("vectrixdb.api")}
        package = getattr(vectrixdb, "api", None)
        try:
            for value, expected in (("1", 200), ("0", 404)):
                monkeypatch.setenv("VECTRIXDB_DASHBOARD", value)
                base = tmp_path / f"db{value}"
                base.mkdir()
                monkeypatch.setenv("VECTRIXDB_PATH", str(base))
                for name in [m for m in list(sys.modules) if m.startswith("vectrixdb.api")]:
                    del sys.modules[name]
                server = importlib.import_module("vectrixdb.api.server")
                with TestClient(server.app) as client:
                    got = client.get("/dashboard").status_code
                assert got == expected, f"VECTRIXDB_DASHBOARD={value} gave {got}"
        finally:
            for name in [m for m in list(sys.modules) if m.startswith("vectrixdb.api")]:
                del sys.modules[name]
            sys.modules.update(saved)
            if package is not None:
                vectrixdb.api = package

    def test_chunk_size_caps_how_long_a_node_may_be(self, tmp_path):
        """index_text documented both chunk arguments, index_file forwarded
        them, and the body read neither."""
        from vectrixdb.core.database import VectrixDB

        db = VectrixDB(str(tmp_path))
        text = "Section one." + chr(10) * 2 + ("word " * 400)

        def longest(doc_id, **kwargs):
            db.documents.index_text(doc_id, text, doc_type="text", **kwargs)
            nodes = db.documents._storage.get_document_nodes(doc_id)
            return max(len(n["text"]) for n in nodes)

        uncapped = longest("a", chunk_size=100_000)
        assert longest("b", chunk_size=1000, chunk_overlap=20) <= 1000 < uncapped
        assert longest("c", chunk_size=200, chunk_overlap=20) <= 200
        db.close()

    def test_incremental_sync_says_when_it_could_not_filter(self, tmp_path):
        """It returned full() and never read `since`, while the scheduler
        called it on every tick."""
        from vectrixdb.core.database import VectrixDB

        source = VectrixDB(str(tmp_path / "s"))
        target = VectrixDB(str(tmp_path / "t"))
        col = source.create_collection("d", dimension=4)
        col.add(ids=[f"x{i}" for i in range(20)], vectors=[[1.0, 0, 0, 0]] * 20)

        sync = VectrixSync(source=source, target=target)
        assert sync.incremental().filtered is True, "no cutoff yet"

        # A local row carries no timestamp, so the cutoff cannot be applied
        # and everything is copied. The caller is told rather than misled.
        assert sync.incremental().filtered is False
        source.close()
        target.close()

    def test_a_missing_model_raises_a_vectrix_error(self, tmp_path):
        """The README and the error guide both promise that one except covers
        the library; this path raised a bare FileNotFoundError."""
        from vectrixdb.exceptions import ModelNotFoundError

        with pytest.raises(ModelNotFoundError) as caught:
            Vectrix("x", path=str(tmp_path), dense_model="bge-base").add(["hello"])
        assert isinstance(caught.value, VectrixError)
        assert isinstance(caught.value, FileNotFoundError), "older handlers still catch it"
        assert "releases/latest" in str(caught.value), "the release tag was two versions stale"

    def test_a_fresh_store_reads_as_empty_rather_than_raising(self, tmp_path):
        """The document tables are created lazily, so reading from a store
        that never held a document raised sqlite3.OperationalError out of a
        call whose honest answer is that there are none."""
        from vectrixdb import StorageBackend, StorageConfig, create_storage

        storage = create_storage(
            StorageConfig(backend=StorageBackend.SQLITE, sqlite_path=str(tmp_path))
        )
        storage.connect()
        assert storage.list_documents() == []
        assert storage.get_document("nope") is None
        assert storage.delete_document("nope") is False

    def test_the_payload_index_is_deprecated(self):
        """Exported, called by nothing, and disagreeing with the scan on five
        operator families. Nothing on the roadmap wants a pre-filtered index."""
        import vectrixdb

        for name in ("PayloadIndexManager", "TagIndex", "GeoIndex"):
            with pytest.warns(DeprecationWarning, match="removed in 2.3"):
                getattr(vectrixdb, name)


class TestQuantisersMeasureWhatTheyClaim:
    def test_scalar_rounds_instead_of_truncating(self):
        """astype(uint8) floors, so every reconstruction was biased low by
        half a step and code 255 was unreachable."""
        from vectrixdb import ScalarQuantizer

        rng = np.random.default_rng(0)
        vectors = rng.uniform(-1, 1, (2000, 32)).astype("float32")
        q = ScalarQuantizer(dimension=32)
        q.fit(vectors)
        error = q.decode(q.encode(vectors)) - vectors
        step = (vectors.max() - vectors.min()) / 255

        assert abs(error.mean() / step) < 0.05, "reconstruction is biased"
        assert np.abs(error).max() / step <= 0.51, "error exceeds half a step"
        assert 0.4 < (error > 0).mean() < 0.6, "errors should fall both ways"
        top = q.encode(np.full((1, 32), vectors.max(), dtype="float32"))
        assert top.max() == 255, "the training maximum must reach the top code"

    def test_product_cosine_is_an_angle_not_a_projection(self):
        """It divided by the query norm alone and called that an
        approximation, so distances fell outside the valid range."""
        from vectrixdb import ProductQuantizer

        rng = np.random.default_rng(0)
        db = rng.standard_normal((400, 32)).astype("float32")
        db *= rng.uniform(0.2, 5.0, (400, 1)).astype("float32")
        q = ProductQuantizer(dimension=32, n_subvectors=4, n_clusters=64, train_size=400)
        q.fit(db)
        distances = q.compute_distances(
            rng.standard_normal(32).astype("float32"), q.encode(db), metric="cosine"
        )
        assert distances.min() >= 0.0, "a cosine distance is never negative"
        assert distances.max() <= 2.0, "a cosine distance never exceeds 2"

    def test_product_training_survives_a_duplicate_heavy_corpus(self):
        """Once k-means++ has picked every distinct point the weights sum to
        zero, and numpy refused with a message pointing nowhere near it."""
        from vectrixdb import ProductQuantizer

        rng = np.random.default_rng(0)
        palette = rng.standard_normal((40, 32)).astype("float32")

        for name, data in (
            ("a palette of 40", palette[rng.integers(0, 40, 5000)]),
            ("one vector", rng.standard_normal((1, 32)).astype("float32")),
            ("1000 identical", np.tile(palette[:1], (1000, 1))),
        ):
            q = ProductQuantizer(dimension=32, n_subvectors=4)
            q.fit(data)
            assert q.encode(data[:3]).shape[1] == 4, name
