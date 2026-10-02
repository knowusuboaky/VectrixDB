"""Four things that were quietly wrong at the edges of the public API.

Each of these was found by using the library rather than by reading it, and
each one failed in the direction that does not look like a failure: a wrong
number instead of an exception, a slow search instead of an error, a
deprecated name that raised instead of working.
"""

from __future__ import annotations

import warnings

import pytest


@pytest.fixture
def db(tmp_path):
    from vectrixdb import Vectrix

    database = Vectrix("edges", path=str(tmp_path / "db"))
    database.add(["alpha one", "beta two", "gamma three"])
    try:
        yield database
    finally:
        database.close()


class TestLimitIsValidated:
    """A limit of zero asked for nothing and got one result: the number went
    to the index unchecked and negative slicing did the rest. The REST layer
    has always rejected it with ``gt=0``."""

    @pytest.mark.parametrize("limit", [0, -1, -5])
    def test_a_limit_below_one_is_refused(self, db, limit):
        with pytest.raises(ValueError, match="limit must be at least 1"):
            db.search("one", limit=limit)

    def test_a_real_limit_still_works(self, db):
        assert len(db.search("one", limit=2)) == 2

    def test_the_error_names_the_value(self, db):
        with pytest.raises(ValueError) as caught:
            db.search("one", limit=0)
        assert "0" in str(caught.value)


class TestDeletionDragIsReported:
    """Deleted vectors stay in the HNSW graph and are filtered after the
    search, so a heavily deleted collection gets slower with no sign of why.
    Measured at 20,000 vectors: a fifth deleted took a query from 1.0 ms to
    18.0 ms. The result says so now, through the same field that reports a
    degraded graph search."""

    def _stocked(self, tmp_path):
        from vectrixdb import Vectrix

        database = Vectrix("drag", path=str(tmp_path / "drag"))
        database.add(
            [f"document number {i} about rocks and minerals" for i in range(40)],
            ids=[f"d{i}" for i in range(40)],
        )
        return database

    def test_a_clean_index_says_nothing(self, tmp_path):
        database = self._stocked(tmp_path)
        try:
            assert database.search("rocks", limit=3).degraded is None
            assert database._collection.tombstone_ratio == 0.0
        finally:
            database.close()

    def test_heavy_deletion_is_reported_on_the_result(self, tmp_path):
        database = self._stocked(tmp_path)
        try:
            database.delete([f"d{i}" for i in range(12)])
            note = database.search("rocks", limit=3).degraded

            assert note is not None, "a third of the index is tombstones and nothing said so"
            assert "rebuild_index()" in note, note
            assert round(database._collection.tombstone_ratio, 2) == 0.30
        finally:
            database.close()

    def test_compaction_clears_the_report(self, tmp_path):
        database = self._stocked(tmp_path)
        try:
            database.delete([f"d{i}" for i in range(12)])
            live = database._collection.rebuild_index()

            assert live == 28
            assert database._collection.tombstone_ratio == 0.0
            assert database.search("rocks", limit=3).degraded is None
        finally:
            database.close()

    def test_a_few_deletions_stay_quiet(self, tmp_path):
        """The note is for the case worth acting on, not every delete."""
        database = self._stocked(tmp_path)
        try:
            database.delete(["d0", "d1"])
            assert database.search("rocks", limit=3).degraded is None
        finally:
            database.close()


class TestDeprecatedPostgresBackend:
    """``StorageBackend.POSTGRESQL`` is a public name with no backend behind
    it. Choosing it used to raise "Unknown storage backend". It warns and
    resolves to the pgvector backend it always meant, and goes in 2.3."""

    def test_it_warns_and_names_its_replacement(self):
        from vectrixdb.core.storage import StorageBackend, StorageConfig, create_storage

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                create_storage(StorageConfig(backend=StorageBackend.POSTGRESQL))
            except Exception:
                # No server here, and none needed: the warning fires before
                # anything tries to connect.
                pass

        messages = [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]
        assert messages, "choosing the deprecated backend warned about nothing"
        assert "AURORA_POSTGRESQL" in messages[0]
        assert "2.3" in messages[0]

    def test_it_no_longer_reports_itself_as_unknown(self):
        from vectrixdb.core.storage import StorageBackend, StorageConfig, create_storage

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            try:
                create_storage(StorageConfig(backend=StorageBackend.POSTGRESQL))
            except ValueError as exc:
                assert "Unknown storage backend" not in str(exc)
            except Exception:
                pass  # an import or connection failure is the expected outcome here

    def test_an_actually_unknown_backend_lists_the_real_ones(self):
        from vectrixdb.core.storage import StorageConfig, create_storage

        class NotABackend:
            value = "invented"

        config = StorageConfig()
        config.backend = NotABackend()
        with pytest.raises(ValueError) as caught:
            create_storage(config)
        message = str(caught.value)
        assert "Unknown storage backend" in message
        listed = message.split("Supported:")[1].strip().split(", ")
        assert "sqlite" in listed and "aurora_postgresql" in listed
        # The deprecated bare name is not offered as a choice.
        assert "postgresql" not in listed


class TestTheDashboardUploadPath:
    """The page posted uploads to a route the server never served."""

    def test_the_page_does_not_reference_the_missing_route(self):
        from pathlib import Path

        import vectrixdb

        # The handler lives in the page's script now, not inline.
        page = Path(vectrixdb.__file__).parent / "dashboard" / "app.js"
        text = page.read_text(encoding="utf-8", errors="ignore")
        assert "/add`" not in text, "the upload handler is back on a route that does not exist"
        assert "text-upsert" in text


class TestGetOne:
    """``get()`` answers with a list whether given one id or many, so a miss
    is ``[]`` and a hit has to be unwrapped. That is right for a batch and
    wrong for a single lookup, which is what ``get_one`` is for."""

    def test_a_hit_is_the_document_itself(self, db):
        db.add(["a document about basalt"], ids=["d1"])
        found = db.get_one("d1")
        assert found is not None
        assert "basalt" in found.text

    def test_a_miss_is_none(self, db):
        assert db.get_one("no-such-id") is None

    def test_get_still_answers_with_a_list(self, db):
        db.add(["a document about basalt"], ids=["d1"])
        assert isinstance(db.get("d1"), list)
        assert db.get("no-such-id") == []
