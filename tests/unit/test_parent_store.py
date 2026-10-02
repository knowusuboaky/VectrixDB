"""Where the parent sections live, and why it is not always beside the collection.

A chunk is small so it is found precisely; the passage around it is what is
returned. On one process a file beside the collection is right. On anything
that scales out it is wrong in a way nothing reports: the instance that
ingested a document is not the instance answering the question, so it reads
an empty store and returns nothing, with no error anywhere.

Cosmos here is a fake. Nothing connects to anything.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from vectrixdb.exceptions import ConfigurationError, DependencyError
from vectrixdb.ingest import ParentStore, _CosmosParents, _doc_of


class FakeContainer:
    """Enough of a Cosmos container to hold items and answer one query."""

    def __init__(self):
        self.items = {}
        self.reads = 0

    def read(self):
        self.reads += 1

    def upsert_item(self, item):
        self.items[item["id"]] = item

    def read_item(self, item, partition_key):
        if item not in self.items:
            raise _Gone()
        return self.items[item]

    def query_items(self, query, parameters, partition_key):
        return [{"id": k} for k, v in self.items.items() if v["doc"] == partition_key]

    def delete_item(self, item, partition_key):
        self.items.pop(item, None)


class _Gone(Exception):
    status_code = 404


def cosmos_store():
    container = FakeContainer()
    store = ParentStore()
    store._cosmos = _CosmosParents(container)
    return store, container


class TestAFileBesideTheCollection:
    def test_it_makes_its_own_folder(self, tmp_path):
        """It did not, and a collection on a remote backend makes no folder either.

        The first document into a fresh deployment then died on "unable to
        open database file", raised from three frames inside a call that
        mentions neither SQLite nor a folder.
        """
        deep = tmp_path / "vectrixdb" / "financial" / "financial.parents.db"
        assert not deep.parent.exists()
        store = ParentStore(deep)
        try:
            assert deep.parent.is_dir()
            store.put("a.pdf:parent:0", "the section", {"page": 3})
            assert store.get("a.pdf:parent:0") == ("the section", {"page": 3})
        finally:
            store.close()

    def test_it_survives_being_reopened(self, tmp_path):
        where = tmp_path / "p.db"
        first = ParentStore(where)
        first.put("a.pdf:parent:0", "kept", {})
        first.close()
        second = ParentStore(where)
        try:
            assert second.get("a.pdf:parent:0") == ("kept", {})
        finally:
            second.close()

    def test_where_it_is_says_so(self, tmp_path):
        store = ParentStore(tmp_path / "p.db")
        try:
            assert store.where.endswith("p.db")
        finally:
            store.close()


class TestInMemory:
    def test_nowhere_given_is_memory(self):
        store = ParentStore()
        assert store.where == "memory"
        store.put("a.pdf:parent:0", "the section", {"page": 3})
        assert store.get("a.pdf:parent:0") == ("the section", {"page": 3})
        assert store.delete_document("a.pdf") == 1
        assert store.get("a.pdf:parent:0") is None


class TestCosmos:
    def test_a_parent_knows_the_document_it_came_from(self):
        """The partition key, so forgetting a document is one partition and not a scan."""
        assert _doc_of("financial/td/ar.pdf:parent:7") == "financial/td/ar.pdf"
        assert _doc_of("no-parent-marker") == "no-parent-marker"

    def test_an_id_with_a_slash_in_it_is_made_safe(self):
        """A document id holds slashes; a Cosmos item id may not."""
        assert "/" not in _CosmosParents._safe("financial/td/ar.pdf:parent:0")

    def test_it_keeps_and_returns_a_parent(self):
        store, container = cosmos_store()
        store.put("financial/td/ar.pdf:parent:0", "the enclosing section", {"page": 12})
        assert store.get("financial/td/ar.pdf:parent:0") == ("the enclosing section", {"page": 12})
        assert container.items, "it went to the container, not to memory"

    def test_a_parent_nothing_wrote_is_none_and_not_an_error(self):
        store, _ = cosmos_store()
        assert store.get("never/written.pdf:parent:0") is None

    def test_forgetting_a_document_forgets_its_parents_and_no_others(self):
        store, _ = cosmos_store()
        store.put("a/one.pdf:parent:0", "first", {})
        store.put("a/one.pdf:parent:1", "second", {})
        store.put("a/two.pdf:parent:0", "other", {})
        assert store.delete_document("a/one.pdf") == 2
        assert store.get("a/one.pdf:parent:0") is None
        assert store.get("a/two.pdf:parent:0") == ("other", {})

    def test_where_it_is_says_so(self):
        store, _ = cosmos_store()
        assert store.where == "Cosmos DB"

    def test_closing_it_is_not_an_error(self):
        store, _ = cosmos_store()
        store.close()


class TestWhatItWillAccept:
    def test_an_address_it_does_not_know_is_refused_by_name(self):
        with pytest.raises(ConfigurationError, match="cosmos://"):
            ParentStore("postgres://host/db/table")

    def test_a_cosmos_address_needs_a_database_and_a_container(self):
        with pytest.raises((ConfigurationError, DependencyError)):
            ParentStore("cosmos://account.documents.azure.com/only-one-name")

    def test_it_says_which_package_it_needs(self):
        """Not an ImportError from three frames down."""
        pytest.importorskip  # noqa: B018 - the point is the branch below, not this
        try:
            import azure.cosmos  # noqa: F401
        except ImportError:
            with pytest.raises(DependencyError, match="azure-cosmos"):
                ParentStore("cosmos://a.documents.azure.com/db/parents")


class TestACollectionCanBeToldWhere:
    def test_it_defaults_to_a_file_beside_the_collection(self):
        from vectrixdb import Vectrix

        folder = tempfile.mkdtemp()
        db = Vectrix("t", path=folder)
        try:
            assert Path(db.parents_are_at).name == "t.parents.db"
        finally:
            db.close()

    def test_what_it_is_told_wins(self):
        from vectrixdb import Vectrix

        folder = Path(tempfile.mkdtemp())
        elsewhere = folder / "shared" / "parents.db"
        db = Vectrix("t", path=str(folder / "db"), parent_store=elsewhere)
        try:
            assert Path(db.parents_are_at) == elsewhere
            assert elsewhere.parent.is_dir()
        finally:
            db.close()

    def test_a_collection_with_no_path_keeps_them_in_memory(self):
        from vectrixdb import Vectrix

        db = Vectrix("t", path=None)
        try:
            assert db.parents_are_at == "memory"
        finally:
            db.close()
