"""Azure AI Search backend: schema, key encoding, wiring and the end-to-end
path through Vectrix, all over the in-memory stand-in for the SDK clients.

The storage contract suite (test_storage_contract.py) covers the CRUD and
search behaviours for this backend too; this file covers what is specific
to Azure: how ids become keys, how an index is declared, how the factory
and ``with_azure_search`` build it, and that hybrid mode goes through the
service's own fusion.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("azure.search.documents")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_azure_search import FakeIndexClient  # noqa: E402

from vectrixdb.core import storage as storage_mod  # noqa: E402
from vectrixdb.core.storage import StorageBackend, StorageConfig, create_storage  # noqa: E402
from vectrixdb.core.storage_azure import (  # noqa: E402
    F_TEXT,
    F_VECTOR,
    SEMANTIC_CONFIG,
    AzureSearchStorage,
    _key,
)


def fake_storage(prefix="t", semantic=False) -> AzureSearchStorage:
    fake = FakeIndexClient()
    storage = AzureSearchStorage(
        StorageConfig(
            backend=StorageBackend.AZURE_SEARCH,
            azure_search_index_prefix=prefix,
            azure_search_semantic=semantic,
        ),
        index_client=fake,
        client_factory=fake.get_search_client,
    )
    storage.connect()
    return storage


class TestKeysAndNames:
    @pytest.mark.parametrize(
        "doc_id", ["plain", "id with spaces", "ünï-cödé", "a,b;c", "quote's", "=" * 10, "x" * 300]
    )
    def test_keys_are_azure_safe_and_unique(self, doc_id):
        key = _key(doc_id)
        assert key[0] == "k"
        assert all(ch.isalnum() or ch in "_-" for ch in key[1:]), key
        assert key != _key(doc_id + "!")

    def test_index_names_are_lowercase_and_prefixed(self):
        storage = fake_storage(prefix="Vx")
        assert storage._index_name("My Docs_v2") == "Vx-my-docs-v2"
        assert storage._index_name("***") == "Vx-default"


class TestSchema:
    def test_collection_index_declares_the_vector_and_text_fields(self):
        storage = fake_storage()
        index = storage._collection_index("docs", 384)
        by_name = {f.name: f for f in index.fields}
        assert by_name[F_VECTOR].vector_search_dimensions == 384
        assert by_name[F_VECTOR].vector_search_profile_name
        assert by_name[F_TEXT].searchable
        assert index.vector_search.profiles[0].algorithm_configuration_name
        assert index.semantic_search is None

    def test_semantic_option_adds_azure_ranker_config(self):
        storage = fake_storage(semantic=True)
        index = storage._collection_index("docs", 4)
        assert index.semantic_search.configurations[0].name == SEMANTIC_CONFIG

    def test_the_catalog_index_is_made_with_the_first_collection_and_not_on_connect(self):
        """connect() used to create the catalog index at once, so a handle opened with the wrong or an
        empty prefix left an empty vectrix-collections in the service that nothing used. Until a
        collection is created there is no catalog, and a missing one reads as no collections."""
        storage = fake_storage(prefix="wrong")
        fake = storage._index_client
        assert fake.indexes == {}, "connect() made nothing"
        assert storage.list_collections() == []
        assert storage.get_collection_config("docs") is None
        storage.delete_collection("docs")
        assert fake.indexes == {}
        storage.create_collection("docs", {"dimension": 8})
        assert set(fake.indexes) == {"wrong-collections", "wrong-docs"}
        assert storage.list_collections() == ["docs"]

    def test_create_records_the_config_and_dimension(self):
        storage = fake_storage()
        storage.create_collection("docs", {"mode": "hybrid", "dimension": 8})
        assert storage.get_collection_config("docs") == {"mode": "hybrid", "dimension": 8}
        assert "docs" in storage.list_collections()
        storage.delete_collection("docs")
        assert storage.get_collection_config("docs") is None


class TestWiring:
    def test_factory_builds_and_connects(self, monkeypatch):
        seen = {}

        def fake_connect(self):
            seen["endpoint"] = self.config.azure_search_endpoint

        monkeypatch.setattr(AzureSearchStorage, "connect", fake_connect)
        storage = create_storage(
            StorageConfig(
                backend=StorageBackend.AZURE_SEARCH,
                azure_search_endpoint="https://svc.search.windows.net",
                azure_search_key="k",
            )
        )
        assert isinstance(storage, AzureSearchStorage)
        assert seen["endpoint"] == "https://svc.search.windows.net"

    def test_with_azure_search_sets_the_config(self, monkeypatch):
        from vectrixdb.core.database import VectrixDB

        captured = {}

        def fake_create(config):
            captured["config"] = config
            return fake_storage()

        monkeypatch.setattr("vectrixdb.core.database.create_storage", fake_create)
        db = VectrixDB.with_azure_search(
            "https://svc.search.windows.net", key="secret", index_prefix="p", semantic=True
        )
        cfg = captured["config"]
        assert cfg.backend == StorageBackend.AZURE_SEARCH
        assert cfg.azure_search_endpoint == "https://svc.search.windows.net"
        assert cfg.azure_search_key == "secret"
        assert cfg.azure_search_index_prefix == "p"
        assert cfg.azure_search_semantic is True
        db.close()

    def test_missing_endpoint_is_a_connection_error(self):
        from vectrixdb.exceptions import StorageConnectionError

        with pytest.raises(StorageConnectionError):
            AzureSearchStorage(StorageConfig(backend=StorageBackend.AZURE_SEARCH)).connect()

    def test_exported_from_storage_module(self):
        assert storage_mod.AzureSearchStorage is AzureSearchStorage
        assert "AzureSearchStorage" in storage_mod.__all__


class TestThroughVectrix:
    @pytest.fixture
    def client(self, monkeypatch):
        from vectrixdb.core.database import VectrixDB

        storage = fake_storage()
        monkeypatch.setattr("vectrixdb.core.database.create_storage", lambda config: storage)
        db = VectrixDB.with_azure_search("https://svc.search.windows.net", key="k")
        yield db, storage
        db.close()

    def test_add_and_search_go_through_the_service(self, client, tmp_path):
        from vectrixdb import Vectrix

        client_db, storage = client
        db = Vectrix("docs", mode="hybrid", storage_backend=client_db, path=str(tmp_path))
        db.add(
            ["Basalt forms when lava cools quickly.", "Sourdough is leavened by wild yeast."],
            metadata=[{"topic": "rocks"}, {"topic": "bread"}],
        )
        assert storage.count("docs") == 2
        stored = storage.get("docs", db._generate_id("Sourdough is leavened by wild yeast."))
        assert stored["text_content"].startswith("Sourdough") and stored["topic"] == "bread"
        assert "sparse_embedding" in stored, "hybrid mode stores the BM25 vector too"

        hit = db.search("bread yeast", limit=1, mode="hybrid", rerank=False).top
        assert hit.text.startswith("Sourdough")
        assert hit.metadata["topic"] == "bread"

        dense = db.search("volcanic rock", limit=1, mode="dense").top
        assert dense.text.startswith("Basalt")
