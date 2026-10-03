"""DeltaLakeStorage binds values instead of interpolating them.

Verified against a recording cursor, not a live warehouse: the assertions are
about the statements this backend hands the connector. No caller-supplied value
may appear in statement text, every value travels as a named parameter, and
the columns match the schema the class itself creates.
"""

import pytest

from vectrixdb.core.storage import DeltaLakeStorage, StorageBackend, StorageConfig

HOSTILE = "O'Brien`\\x; DROP TABLE t; --"


class RecordingCursor:
    def __init__(self):
        self.calls = []
        self.description = [("data",), ("dense_embedding",)]

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass


@pytest.fixture
def storage():
    config = StorageConfig(
        backend=StorageBackend.DELTA_LAKE, delta_catalog="main", delta_schema="vx"
    )
    s = DeltaLakeStorage(config)
    s._cursor = RecordingCursor()
    return s


def _statements(storage):
    return storage._cursor.calls


def _value_never_inlined(storage, value):
    for sql, _ in _statements(storage):
        assert value not in sql, f"value leaked into statement text: {sql[:120]}"


def _bound(storage, value):
    return any(params and value in params.values() for _, params in _statements(storage))


class TestValuesAreBound:
    def test_collection_name_as_a_value(self, storage):
        """In the registry table the name is a value, so it is bound."""
        storage.get_collection_config(HOSTILE)
        storage.delete_collection(HOSTILE)
        _value_never_inlined(storage, HOSTILE)
        assert _bound(storage, HOSTILE)

    def test_collection_name_as_an_identifier(self, storage):
        """As a table name it cannot be bound, so it is quoted and escaped."""
        storage.create_collection(HOSTILE, {"mode": "dense"})
        _value_never_inlined(storage, HOSTILE)
        describe = next(sql for sql, _ in _statements(storage) if sql.startswith("DESCRIBE"))
        assert "`" + HOSTILE.replace("`", "``") + "`" in describe

    def test_control_characters_in_an_identifier_are_refused(self, storage):
        from vectrixdb.exceptions import ConfigurationError

        with pytest.raises(ConfigurationError):
            storage._full_table_name("bad\nname")

    def test_point_id_text_and_metadata(self, storage):
        storage.insert(
            "c", HOSTILE, {"text_content": HOSTILE, "note": HOSTILE, "_embedding": [0.1, 0.2]}
        )
        storage.get("c", HOSTILE)
        storage.update("c", HOSTILE, {"note": HOSTILE})
        storage.delete("c", HOSTILE)
        _value_never_inlined(storage, HOSTILE)
        assert _bound(storage, HOSTILE)

    def test_documents_and_nodes(self, storage):
        storage.save_document({"doc_id": HOSTILE, "title": HOSTILE, "doc_type": "pdf"})
        storage.get_document(HOSTILE)
        storage.save_node({"node_id": HOSTILE, "doc_id": "d", "title": HOSTILE, "text": HOSTILE})
        storage.get_child_nodes(HOSTILE)
        storage.delete_document_nodes(HOSTILE)
        _value_never_inlined(storage, HOSTILE)
        assert _bound(storage, HOSTILE)

    def test_no_statement_carries_a_quoted_literal(self, storage):
        storage.insert("c", "id1", {"text_content": "hello", "_embedding": [1.0]})
        storage.save_node({"node_id": "n", "doc_id": "d", "title": "t", "text": "x"})
        for sql, params in _statements(storage):
            if params:
                assert "'" not in sql.replace("''", ""), sql


class TestEmbeddings:
    def test_arrays_render_from_floats(self, storage):
        storage.insert("c", "id1", {"_embedding": [0.5, 1, "2.5"]})
        sql = _statements(storage)[-1][0]
        assert "ARRAY(0.5,1.0,2.5)" in sql

    def test_a_non_numeric_embedding_is_refused_not_interpolated(self, storage):
        with pytest.raises((ValueError, TypeError)):
            storage.insert("c", "id1", {"_embedding": ["1); DROP TABLE t; --"]})

    def test_update_without_an_embedding_keeps_the_column(self, storage):
        storage.update("c", "id1", {"note": "x"})
        assert "dense_embedding = dense_embedding" in _statements(storage)[-1][0]


class TestSchemaAgreement:
    """get(), iterate() and update() named a column the schema does not have."""

    def test_get_reads_the_columns_the_schema_creates(self, storage):
        storage.get("c", "id1")
        sql = _statements(storage)[-1][0]
        for column in (
            "dense_embedding",
            "sparse_embedding",
            "late_interaction_embedding",
            "text_content",
        ):
            assert column in sql
        assert " embedding " not in sql and ", embedding" not in sql

    def test_rows_round_trip_every_embedding(self, storage):
        row = ('{"k": 1}', [0.1, 0.2], '{"3": 0.5}', "[[0.1]]", "hello")
        data = storage._row_to_data(row)
        assert data["_embedding"] == [0.1, 0.2]
        assert data["sparse_embedding"] == {"3": 0.5}
        assert data["late_interaction_embedding"] == [[0.1]]
        assert data["text_content"] == "hello"
        assert data["k"] == 1
