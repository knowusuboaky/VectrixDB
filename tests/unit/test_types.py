"""
Tests for VectrixDB type definitions.
"""

import pytest

from vectrixdb import (
    DistanceMetric,
    SearchResult,
    SearchResults,
    Point,
    CollectionInfo,
    DatabaseInfo,
    BatchResult,
    SparseVector,
)


class TestDistanceMetric:
    """Test DistanceMetric enum."""

    def test_cosine_exists(self):
        """Test COSINE metric exists."""
        assert hasattr(DistanceMetric, "COSINE")

    def test_euclidean_exists(self):
        """Test EUCLIDEAN metric exists."""
        assert hasattr(DistanceMetric, "EUCLIDEAN")

    def test_dot_metric_exists(self):
        """Test DOT metric exists."""
        assert hasattr(DistanceMetric, "DOT")


class TestPoint:
    """Test Point dataclass."""

    def test_point_class_exists(self):
        """Test Point class exists."""
        assert Point is not None


class TestSearchResult:
    """Test SearchResult dataclass."""

    def test_search_result_class_exists(self):
        """Test SearchResult class exists."""
        assert SearchResult is not None


class TestSearchResults:
    """Test SearchResults container."""

    def test_search_results_class_exists(self):
        """Test SearchResults class exists."""
        assert SearchResults is not None


class TestCollectionInfo:
    """Test CollectionInfo dataclass."""

    def test_collection_info_class_exists(self):
        """Test CollectionInfo class exists."""
        assert CollectionInfo is not None


class TestDatabaseInfo:
    """Test DatabaseInfo dataclass."""

    def test_database_info_class_exists(self):
        """Test DatabaseInfo class exists."""
        assert DatabaseInfo is not None


class TestSparseVector:
    """Test SparseVector dataclass."""

    def test_sparse_vector_class_exists(self):
        """Test SparseVector class exists."""
        assert SparseVector is not None


class TestBatchResult:
    """Test BatchResult dataclass."""

    def test_batch_result_class_exists(self):
        """Test BatchResult class exists."""
        assert BatchResult is not None


# =============================================================================
# Behaviour: enum parsing, validation, serialisation round trips and filters.
# =============================================================================

from datetime import datetime, timezone

import numpy as np

from vectrixdb.core.types import (
    Filter,
    FilterCondition,
    FilterOperator,
    IndexConfig,
    IndexType,
    QuantizationType,
    SearchMode,
    SearchQuery,
)


class TestEnumParsing:
    def test_distance_metric_values_and_usearch_names(self):
        assert DistanceMetric("cosine") is DistanceMetric.COSINE
        assert DistanceMetric.COSINE.usearch_metric == "cos"
        assert DistanceMetric.EUCLIDEAN.usearch_metric == "l2sq"
        assert DistanceMetric.DOT.usearch_metric == "ip"
        assert DistanceMetric.MANHATTAN.usearch_metric == "l1"
        assert DistanceMetric.COSINE == "cosine"  # str-backed enum

    def test_unknown_metric_is_a_value_error(self):
        with pytest.raises(ValueError):
            DistanceMetric("hamming")

    def test_other_enums_parse_from_strings(self):
        assert IndexType("hnsw_pq") is IndexType.HNSW_PQ
        assert SearchMode("hybrid") is SearchMode.HYBRID
        assert QuantizationType("binary") is QuantizationType.BINARY
        assert FilterOperator("starts_with") is FilterOperator.STARTS_WITH
        assert len(FilterOperator) == 22


class TestSparseVectorBehaviour:
    def test_length_mismatch_is_rejected(self):
        with pytest.raises(ValueError, match="same length"):
            SparseVector(indices=[1, 2], values=[0.5])

    def test_unsorted_indices_are_sorted_with_values(self):
        sv = SparseVector(indices=[5, 1, 3], values=[0.5, 0.1, 0.3])
        assert sv.indices == [1, 3, 5]
        assert sv.values == [0.1, 0.3, 0.5]

    def test_from_dict_and_to_dict_round_trip(self):
        assert SparseVector.from_dict({}).indices == []
        sv = SparseVector.from_dict({4: 0.4, 2: 0.2})
        assert sv.to_dict() == {2: 0.2, 4: 0.4}
        assert len(sv) == 2

    def test_from_dense_list_and_array_with_threshold(self):
        sv = SparseVector.from_dense([0.0, 0.5, 1e-9, 2.0])
        assert sv.indices == [1, 3] and sv.values == [0.5, 2.0]
        sv2 = SparseVector.from_dense(np.array([0.0, 0.01, 0.2]), threshold=0.1)
        assert sv2.indices == [2]

    def test_to_dense_ignores_out_of_range_indices(self):
        sv = SparseVector(indices=[0, 2, 9], values=[1.0, 2.0, 3.0])
        dense = sv.to_dense(3)
        assert dense.tolist() == [1.0, 0.0, 2.0]

    def test_from_text_with_vocab_and_idf(self):
        vocab = {"cat": 0, "dog": 1, "fish": 2}
        sv = SparseVector.from_text("Cat cat dog bird", vocab, normalize=False)
        assert sv.indices == [0, 1]
        assert sv.values[0] == pytest.approx(1 + np.log(2))
        assert sv.values[1] == pytest.approx(1.0)
        weighted = SparseVector.from_text("cat dog", vocab, idf_weights={"cat": 3.0})
        assert weighted.norm() == pytest.approx(1.0)
        assert weighted.values[0] > weighted.values[1]

    def test_from_text_with_no_known_tokens_is_empty(self):
        sv = SparseVector.from_text("nothing here", {"cat": 0})
        assert len(sv) == 0

    def test_dot_norm_cosine(self):
        a = SparseVector(indices=[0, 1, 5], values=[1.0, 2.0, 3.0])
        b = SparseVector(indices=[1, 5, 7], values=[4.0, 5.0, 6.0])
        assert a.dot(b) == pytest.approx(2 * 4 + 3 * 5)
        assert a.dot(SparseVector(indices=[], values=[])) == 0.0
        assert a.dot(SparseVector(indices=[9], values=[1.0])) == 0.0
        assert a.norm() == pytest.approx(np.sqrt(14))
        assert a.cosine_similarity(a) == pytest.approx(1.0)
        assert a.cosine_similarity(SparseVector(indices=[], values=[])) == 0.0

    def test_normalize_handles_zero_norm(self):
        zero = SparseVector(indices=[1], values=[0.0])
        assert zero.normalize().values == [0.0]
        n = SparseVector(indices=[0, 1], values=[3.0, 4.0]).normalize()
        assert n.values == pytest.approx([0.6, 0.8])

    def test_top_k(self):
        sv = SparseVector(indices=[0, 1, 2, 3], values=[0.1, -0.9, 0.5, 0.2])
        top = sv.top_k(2)
        assert top.indices == [1, 2]
        assert top.values == [-0.9, 0.5]
        same = sv.top_k(10)
        assert same.indices == sv.indices and same is not sv

    def test_repr_short_and_long(self):
        short = repr(SparseVector(indices=[1, 2], values=[0.5, 1.0]))
        assert short == "SparseVector({1:0.500, 2:1.000})"
        long = repr(SparseVector(indices=list(range(6)), values=[1.0] * 6))
        assert "... (6 total)" in long


class TestIndexConfig:
    def test_defaults_serialise(self):
        d = IndexConfig().to_dict()
        assert d["index_type"] == "hnsw" and d["quantization"] == "none"
        assert d["hnsw_m"] == 16 and d["pq_bits"] == 8
        custom = IndexConfig(index_type=IndexType.IVF, quantization=QuantizationType.SCALAR)
        assert custom.to_dict()["index_type"] == "ivf"
        assert custom.to_dict()["quantization"] == "scalar"


NOW = datetime(2024, 1, 2, tzinfo=timezone.utc)


def point(**kwargs):
    """A Point with an explicit aware created_at, see the xfail below for why."""
    kwargs.setdefault("created_at", NOW)
    return Point(**kwargs)


class TestPointBehaviour:
    def test_default_created_at_is_aware(self):
        """A new Point used to stamp the deprecated naive datetime.utcnow while
        from_dict produced an aware one, so two Points of the same collection
        could not be compared."""
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            p = Point(id="a", vector=[0.1])
        assert p.created_at.tzinfo is not None

    def test_payload_is_merged_into_metadata(self):
        p = point(id="a", vector=[0.1], payload={"k": 1})
        assert p.metadata == {"k": 1}
        q = point(id="b", vector=[0.1], metadata={"x": 1}, payload={"k": 1})
        assert q.metadata == {"x": 1, "k": 1}

    def test_dict_sparse_vector_becomes_sparse_vector(self):
        p = point(id="a", vector=[0.1], sparse_vector={3: 0.3})
        assert isinstance(p.sparse_vector, SparseVector)
        assert p.has_sparse() is True
        assert point(id="b", vector=[0.1]).has_sparse() is False
        assert point(id="c", vector=[0.1], sparse_vector={}).has_sparse() is False

    def test_to_dict_with_ndarray_sparse_and_text(self):
        now = datetime(2024, 1, 2, tzinfo=timezone.utc)
        p = Point(
            id="a",
            vector=np.array([0.1, 0.2], dtype=np.float32),
            sparse_vector=SparseVector(indices=[1], values=[0.5]),
            text="hello",
            created_at=now,
            updated_at=now,
        )
        d = p.to_dict()
        assert d["vector"] == pytest.approx([0.1, 0.2])
        assert d["sparse_vector"] == {"indices": [1], "values": [0.5]}
        assert d["text"] == "hello"
        assert d["created_at"] == now.isoformat()
        assert d["updated_at"] == now.isoformat()

    def test_to_dict_minimal(self):
        d = point(id="a", vector=[0.1]).to_dict()
        assert "sparse_vector" not in d and "text" not in d
        assert d["updated_at"] is None

    def test_from_dict_round_trip(self):
        now = datetime(2024, 1, 2, tzinfo=timezone.utc)
        original = Point(
            id="a",
            vector=[0.1, 0.2],
            metadata={"k": 1},
            sparse_vector={2: 0.5},
            text="t",
            created_at=now,
            updated_at=now,
        )
        restored = Point.from_dict(original.to_dict())
        assert restored.id == "a" and restored.vector == [0.1, 0.2]
        assert restored.metadata == {"k": 1} and restored.text == "t"
        assert restored.sparse_vector.to_dict() == {2: 0.5}
        assert restored.created_at == now and restored.updated_at == now

    def test_from_dict_accepts_payload_and_index_value_sparse(self):
        p = Point.from_dict(
            {"id": "a", "vector": [1.0], "payload": {"k": 2}, "sparse_vector": {7: 0.7}}
        )
        assert p.metadata == {"k": 2}
        assert p.sparse_vector.indices == [7]
        assert p.created_at.tzinfo is not None  # defaults to an aware utcnow
        assert p.updated_at is None

    def test_from_dict_requires_id_and_vector(self):
        with pytest.raises(KeyError):
            Point.from_dict({"vector": [1.0]})


class TestSearchResultSerialisation:
    def test_optional_fields_are_only_present_when_set(self):
        d = SearchResult(id="a", score=0.5).to_dict()
        assert d == {"id": "a", "score": 0.5, "metadata": {}}

    def test_every_optional_field(self):
        r = SearchResult(
            id="a",
            score=0.9,
            vector=[1.0],
            metadata={"k": 1},
            text="body",
            dense_score=0.8,
            sparse_score=0.7,
            text_score=0.6,
            highlights=["bo"],
        )
        d = r.to_dict()
        assert d["text"] == "body" and d["vector"] == [1.0]
        assert d["dense_score"] == 0.8 and d["sparse_score"] == 0.7 and d["text_score"] == 0.6
        assert d["highlights"] == ["bo"]

    def test_empty_text_and_empty_highlights(self):
        d = SearchResult(id="a", score=0.1, text="", highlights=[]).to_dict()
        assert d["text"] == ""  # empty string is still a value
        assert "highlights" not in d

    def test_search_results_container(self):
        results = SearchResults(
            results=[SearchResult(id="a", score=1.0)],
            query_time_ms=1.5,
            total_searched=3,
            search_mode=SearchMode.HYBRID,
        )
        d = results.to_dict()
        assert d["search_mode"] == "hybrid" and d["total_searched"] == 3
        assert d["results"][0]["id"] == "a"


class TestInfoSerialisation:
    def test_collection_info_with_and_without_optionals(self):
        now = datetime(2024, 5, 6, tzinfo=timezone.utc)
        bare = CollectionInfo(
            name="c", dimension=3, metric=DistanceMetric.DOT, count=0, size_bytes=0, created_at=now
        )
        d = bare.to_dict()
        assert d["metric"] == "dot" and d["updated_at"] is None and d["index_config"] is None
        assert d["tags"] == [] and d["has_text_index"] is False
        full = CollectionInfo(
            name="c",
            dimension=3,
            metric=DistanceMetric.COSINE,
            count=2,
            size_bytes=10,
            created_at=now,
            updated_at=now,
            description="d",
            index_config=IndexConfig(),
            has_text_index=True,
            indexed_fields=["f"],
            tags=["dense"],
        )
        d = full.to_dict()
        assert d["index_config"]["index_type"] == "hnsw"
        assert d["updated_at"] == now.isoformat() and d["indexed_fields"] == ["f"]

    def test_database_info(self):
        now = datetime(2024, 5, 6, tzinfo=timezone.utc)
        d = DatabaseInfo(
            path="/p",
            version="2",
            collections_count=1,
            total_vectors=2,
            total_size_bytes=3,
            created_at=now,
        ).to_dict()
        assert d == {
            "path": "/p",
            "version": "2",
            "collections_count": 1,
            "total_vectors": 2,
            "total_size_bytes": 3,
            "created_at": now.isoformat(),
        }

    def test_batch_result(self):
        d = BatchResult(success_count=2, error_count=1, errors=[{"id": "x"}]).to_dict()
        assert d["success_count"] == 2 and d["errors"] == [{"id": "x"}]
        assert d["operation_time_ms"] == 0.0

    def test_search_query_to_dict_with_array_and_list(self):
        q = SearchQuery(vector=np.array([0.1, 0.2]), mode=SearchMode.KEYWORD, query_text="x")
        d = q.to_dict()
        assert d["vector"] == pytest.approx([0.1, 0.2])
        assert d["mode"] == "keyword" and d["query_text"] == "x"
        assert SearchQuery(vector=[1.0]).to_dict()["vector"] == [1.0]
        assert SearchQuery().to_dict()["vector"] is None


def cond(op, value, field="f"):
    return FilterCondition(field=field, operator=op, value=value)


class TestFilterConditionOperators:
    def test_existence_operators(self):
        assert cond("exists", True).matches({"f": 1})
        assert cond("exists", False).matches({})
        assert cond("is_null", True).matches({"f": None})
        assert cond("is_null", False).matches({"f": 0})
        assert cond("is_empty", True).matches({"f": []})
        assert cond("is_empty", False).matches({"f": "x"})
        assert cond("is_empty", True).matches({"f": 5}) is False

    def test_absent_null_and_empty_are_three_different_things(self):
        """The docs give each of these its own meaning, so each must pick out
        exactly one of the three. Reading a missing path as None made every
        one of them wrong about one case: is_empty said an absent field was
        empty, is_null said it was null, and exists said a field explicitly
        set to null was not there."""
        absent: dict = {}
        null = {"f": None}
        empty = {"f": ""}
        value = {"f": "x"}

        assert cond("exists", True).matches(null) is True
        assert cond("exists", True).matches(absent) is False
        assert cond("exists", False).matches(absent) is True

        assert cond("is_null", True).matches(null) is True
        assert cond("is_null", True).matches(absent) is False
        assert cond("is_null", True).matches(empty) is False

        assert cond("is_empty", True).matches(empty) is True
        assert cond("is_empty", True).matches(absent) is False
        assert cond("is_empty", True).matches(null) is False

        # eq and ne must not both be false for the same document
        assert cond("eq", None).matches(null) is True
        assert cond("ne", "x").matches(null) is True
        assert cond("eq", "x").matches(value) is True
        assert cond("ne", "x").matches(value) is False

    def test_membership_against_a_list_valued_field(self):
        """$in on a list field is an overlap test, and $nin is its exact
        negation. Testing `field in operand` put the whole list inside the
        operand, so $in matched nothing and $nin excluded nothing, which is
        the direction that leaks records a caller meant to hide."""
        has = {"tags": ["desk", "metal"]}
        other = {"tags": ["chair"]}

        assert cond("in", ["desk"], field="tags").matches(has) is True
        assert cond("in", ["desk"], field="tags").matches(other) is False
        assert cond("nin", ["desk"], field="tags").matches(has) is False
        assert cond("nin", ["desk"], field="tags").matches(other) is True

        # a scalar field keeps the behaviour the docs describe
        assert cond("in", ["kitchen", "bath"], field="cat").matches({"cat": "bath"}) is True
        assert cond("nin", ["kitchen", "bath"], field="cat").matches({"cat": "hall"}) is True

    def test_missing_field_fails_every_other_operator(self):
        assert cond("eq", 1).matches({}) is False
        assert cond("gt", 1).matches({"f": None}) is False

    @pytest.mark.parametrize(
        "op,value,good,bad",
        [
            ("eq", 1, 1, 2),
            ("ne", 1, 2, 1),
            ("gt", 1, 2, 1),
            ("gte", 1, 1, 0),
            ("lt", 1, 0, 1),
            ("lte", 1, 1, 2),
            ("in", [1, 2], 1, 3),
            ("nin", [1, 2], 3, 1),
            ("contains", "ell", "hello", "world"),
            ("icontains", "ELL", "hello", "world"),
            ("starts_with", "he", "hello", "ohello"),
            ("ends_with", "lo", "hello", "hellox"),
            ("regex", r"^h.l+o$", "hello", "hero"),
            ("between", [1, 3], 2, 4),
        ],
    )
    def test_comparison_string_and_range_operators(self, op, value, good, bad):
        assert cond(op, value).matches({"f": good}) is True
        assert cond(op, value).matches({"f": bad}) is False

    def test_array_all_and_any(self):
        assert cond("all", [1, 2]).matches({"f": [1, 2, 3]})
        assert cond("all", [1, 4]).matches({"f": [1, 2, 3]}) is False
        assert cond("all", [1]).matches({"f": 1}) is False
        assert cond("any", [1, 9]).matches({"f": [3, 9]})
        assert cond("any", [1, 9]).matches({"f": [3]}) is False
        assert cond("any", [1, 9]).matches({"f": 9})  # scalar field

    def test_bad_regex_and_bad_between_are_false(self):
        assert cond("regex", "(").matches({"f": "x"}) is False
        assert cond("between", [1]).matches({"f": 1}) is False
        assert cond("between", 5).matches({"f": 1}) is False

    def test_date_range(self):
        window = ["2024-01-01T00:00:00Z", "2024-12-31T00:00:00+00:00"]
        assert cond("date_range", window).matches({"f": "2024-06-01T00:00:00Z"})
        assert cond("date_range", window).matches({"f": "2023-06-01T00:00:00Z"}) is False
        assert cond("date_range", window).matches({"f": datetime(2024, 6, 1, tzinfo=timezone.utc)})
        assert cond("date_range", window).matches({"f": 12345}) is False
        assert cond("date_range", window).matches({"f": "not a date"}) is False
        assert cond("date_range", ["", ""]).matches({"f": "2024-06-01T00:00:00Z"}) is False

    def test_geo_radius(self):
        toronto = {"lat": 43.65, "lon": -79.38}
        params = {"center": {"lat": 43.70, "lon": -79.40}, "radius_km": 10}
        assert cond("geo_radius", params).matches({"f": toronto})
        far = {"center": {"lat": 45.50, "lon": -73.57}, "radius_km": 10}
        assert cond("geo_radius", far).matches({"f": toronto}) is False
        assert cond("geo_radius", params).matches({"f": "not a point"}) is False
        assert cond("geo_radius", {"center": {}}).matches({"f": toronto}) is False

    def test_geo_box(self):
        box = {"min_lat": 40, "max_lat": 45, "min_lon": -80, "max_lon": -70}
        assert cond("geo_box", box).matches({"f": {"lat": 43, "lon": -75}})
        assert cond("geo_box", box).matches({"f": {"lat": 50, "lon": -75}}) is False
        assert cond("geo_box", box).matches({"f": {"lat": 43}}) is False
        assert cond("geo_box", {}).matches({"f": {"lat": 43, "lon": -75}}) is False

    def test_unknown_operator_raises(self):
        with pytest.raises(ValueError, match="Unknown operator"):
            cond("like", 1).matches({"f": 1})

    def test_nested_paths_and_list_indexes(self):
        data = {"a": {"b": [10, {"c": "deep"}]}}
        assert cond("eq", 10, field="a.b.0").matches(data)
        assert cond("eq", "deep", field="a.b.1.c").matches(data)
        assert cond("exists", False, field="a.b.5").matches(data)
        assert cond("exists", False, field="a.zzz").matches(data)
        assert cond("exists", False, field="a.b.x").matches(data)


class TestFilterComposition:
    def test_empty_filter_matches_everything(self):
        assert Filter().matches({"anything": 1})
        assert Filter(negate=True).matches({}) is False

    def test_and_or_and_negate(self):
        a = cond("eq", 1, "a")
        b = cond("eq", 2, "b")
        assert Filter(conditions=[a, b]).matches({"a": 1, "b": 2})
        assert Filter(conditions=[a, b]).matches({"a": 1, "b": 3}) is False
        assert Filter(conditions=[a, b], logic="or").matches({"a": 1, "b": 3})
        assert Filter(conditions=[a], negate=True).matches({"a": 1}) is False
        nested = Filter(nested=[Filter(conditions=[a]), Filter(conditions=[b])], logic="or")
        assert nested.matches({"b": 2})

    def test_from_dict_simple_and_dollar_ops(self):
        f = Filter.from_dict({"category": "tech", "price": {"$lt": 100, "$gte": 10}})
        assert f.matches({"category": "tech", "price": 50})
        assert f.matches({"category": "tech", "price": 5}) is False
        assert f.matches({"category": "news", "price": 50}) is False
        assert [c.operator for c in f.conditions] == ["eq", "lt", "gte"]

    def test_from_dict_extended_forms(self):
        f = Filter.from_dict(
            {
                "$and": [
                    {"field": "category", "op": "eq", "value": "tech"},
                    {
                        "$or": [
                            {"field": "price", "op": "lt", "value": 100},
                            {"field": "premium", "op": "eq", "value": True},
                        ]
                    },
                ]
            }
        )
        assert f.matches({"category": "tech", "price": 500, "premium": True})
        assert f.matches({"category": "tech", "price": 500, "premium": False}) is False
        single = Filter.from_dict({"$or": {"field": "x", "op": "exists", "value": True}})
        assert Filter.from_dict({"field": "x", "op": "exists"}).conditions[0].value is None
        assert single.logic == "or" and single.matches({"x": 1})
        negated = Filter.from_dict({"$not": {"x": 1}})
        assert negated.negate is True and negated.matches({"x": 2})

    def test_from_dict_rejects_bad_shapes(self):
        with pytest.raises(TypeError, match="filter must be a dict"):
            Filter.from_dict(["x"])
        with pytest.raises(TypeError, match=r"\$and takes a list"):
            Filter.from_dict({"$and": "x"})
        with pytest.raises(TypeError):
            Filter.from_dict({"$or": [3]})
        assert Filter._clauses((1, 2), "$and") == [1, 2]

    def test_from_dict_qdrant_format(self):
        f = Filter.from_dict(
            {
                "must": [{"key": "category", "match": {"value": "tech"}}],
                "should": [
                    {"key": "price", "range": {"lt": 100}},
                    {"key": "title", "match": {"text": "vector"}},
                ],
                "must_not": [{"key": "deleted", "match": {"value": True}}],
            }
        )
        assert f.matches({"category": "tech", "price": 50, "deleted": False})
        assert f.matches({"category": "tech", "price": 500, "title": "vector db"})
        assert f.matches({"category": "tech", "price": 50, "deleted": True}) is False
        assert f.matches({"category": "news", "price": 50}) is False
        assert len(f.nested) == 3 and f.nested[2].negate is True

    def test_qdrant_single_clause_dict_and_empty_lists(self):
        f = Filter.from_dict({"must": {"key": "a", "match": {"value": 1}}, "should": []})
        assert len(f.nested) == 1 and f.matches({"a": 1})

    def test_qdrant_condition_variants(self):
        parse = Filter._parse_qdrant_condition
        assert parse({"key": "k", "match": {"any": [1, 2]}}).operator == "any"
        assert parse({"key": "k", "range": {"gte": 3}}).value == 3
        geo = parse({"key": "k", "geo_radius": {"center": {"lat": 0, "lon": 0}, "radius_km": 1}})
        assert geo.operator == "geo_radius"
        box = parse(
            {
                "key": "k",
                "geo_bounding_box": {
                    "top_left": {"lat": 45, "lon": -80},
                    "bottom_right": {"lat": 40, "lon": -70},
                },
            }
        )
        assert box.operator == "geo_box"
        assert box.value == {"min_lat": 40, "max_lat": 45, "min_lon": -80, "max_lon": -70}
        assert parse({"key": "k", "is_null": {"value": True}}).operator == "is_null"
        assert parse({"key": "k", "is_empty": {"value": False}}).value is False

    def test_qdrant_condition_errors(self):
        with pytest.raises(ValueError, match="Unknown Qdrant condition"):
            Filter._parse_qdrant_condition({"key": "k", "match": {"nope": 1}})
        with pytest.raises(ValueError):
            Filter._parse_qdrant_condition({"key": "k"})
        with pytest.raises(TypeError, match="Qdrant condition is a dict"):
            Filter._parse_qdrant_condition("k")
        with pytest.raises(TypeError):
            Filter.from_dict({"must": ["not a dict"]})
