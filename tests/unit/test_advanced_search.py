"""Pure tests for vectrixdb.core.advanced_search.

Re-ranking, facets, ACL filtering and text analyzers on hand-built inputs.
The cross-encoder path is exercised through a fake sentence_transformers
module injected into sys.modules, so nothing is downloaded.
"""

import sys
import types

import numpy as np
import pytest

from vectrixdb.core.advanced_search import (
    ACLConfig,
    ACLFilter,
    ACLOperator,
    ACLPrincipal,
    AnalyzerChain,
    AnalyzerType,
    EnhancedSearchResults,
    FacetAggregator,
    FacetConfig,
    FacetResult,
    FacetValue,
    KeywordAnalyzer,
    RerankConfig,
    Reranker,
    RerankMethod,
    SimpleStemmer,
    TextAnalyzer,
)

# ---------------------------------------------------------------------------
# Re-ranking
# ---------------------------------------------------------------------------

QUERY = np.array([1.0, 0.0])
# The ANN stage got the order wrong on purpose: "far" has the highest score but
# points away from the query, "near" is aligned with it.
CANDIDATES = [
    {"id": "far", "score": 0.9, "vector": [0.0, 1.0]},
    {"id": "mid", "score": 0.5, "vector": [1.0, 1.0]},
    {"id": "near", "score": 0.1, "vector": [1.0, 0.0]},
]


def ids(results):
    return [r["id"] for r in results]


class TestRerankConfig:
    def test_defaults(self):
        cfg = RerankConfig()
        assert cfg.method is RerankMethod.EXACT
        assert cfg.candidate_multiplier == 10
        assert cfg.diversity_lambda == 0.5
        assert cfg.score_weights == {"vector": 0.7, "text": 0.2, "recency": 0.1}
        assert cfg.cross_encoder_model is None

    def test_score_weights_default_is_not_shared(self):
        RerankConfig().score_weights["vector"] = 0.0
        assert RerankConfig().score_weights["vector"] == 0.7

    def test_method_is_a_string_enum(self):
        assert RerankMethod("mmr") is RerankMethod.MMR
        assert RerankMethod.RECIPROCAL_RANK == "rrf"


class TestRerankerExact:
    def test_empty_candidates(self):
        assert Reranker().rerank(QUERY, []) == []

    def test_exact_cosine_reorders_by_true_similarity(self):
        out = Reranker().rerank(QUERY, CANDIDATES)
        assert ids(out) == ["near", "mid", "far"]
        assert out[0]["score"] == pytest.approx(1.0)
        assert out[1]["score"] == pytest.approx(np.sqrt(0.5))
        assert out[2]["score"] == pytest.approx(0.0)

    def test_original_score_is_preserved_and_input_not_mutated(self):
        out = Reranker().rerank(QUERY, CANDIDATES)
        assert {r["id"]: r["original_score"] for r in out} == {"near": 0.1, "mid": 0.5, "far": 0.9}
        assert CANDIDATES[0]["score"] == 0.9

    def test_limit(self):
        assert ids(Reranker().rerank(QUERY, CANDIDATES, limit=1)) == ["near"]

    def test_vectors_can_be_fetched_by_id(self):
        stripped = [{"id": c["id"], "score": c["score"]} for c in CANDIDATES]
        lookup = {c["id"]: np.array(c["vector"]) for c in CANDIDATES}
        out = Reranker().rerank(QUERY, stripped, get_vector_fn=lookup.get)
        assert ids(out) == ["near", "mid", "far"]

    def test_candidate_without_a_vector_keeps_its_ann_score(self):
        cands = [{"id": "blind", "score": 0.6}, {"id": "near", "score": 0.1, "vector": [1.0, 0.0]}]
        out = Reranker().rerank(QUERY, cands)
        assert ids(out) == ["near", "blind"]
        blind = out[1]
        assert blind["score"] == 0.6 and "original_score" not in blind

    def test_zero_query_vector_scores_everything_zero(self):
        out = Reranker().rerank(np.zeros(2), CANDIDATES)
        assert all(r["score"] == 0.0 for r in out)

    def test_zero_candidate_vector_scores_zero(self):
        out = Reranker().rerank(QUERY, [{"id": "z", "score": 1.0, "vector": [0.0, 0.0]}])
        assert out[0]["score"] == 0.0


class TestRerankerMMR:
    @staticmethod
    def make(lam):
        return Reranker(RerankConfig(method=RerankMethod.MMR, diversity_lambda=lam))

    DUPES = [
        {"id": "a", "score": 0.9, "vector": [1.0, 0.0]},
        {"id": "a2", "score": 0.8, "vector": [1.0, 0.0]},
        {"id": "c", "score": 0.1, "vector": [0.0, 1.0]},
    ]
    # Leans toward a but still gives c some relevance, so diversity has something to
    # trade against. Query similarities: a and a2 about 0.894, c about 0.447.
    Q = np.array([1.0, 0.5])

    def test_pure_relevance_orders_by_query_similarity(self):
        out = self.make(1.0).rerank(self.Q, self.DUPES)
        assert ids(out) == ["a", "a2", "c"]

    def test_diversity_pushes_the_duplicate_down(self):
        out = self.make(0.5).rerank(self.Q, self.DUPES)
        assert ids(out) == ["a", "c", "a2"]

    def test_mmr_rank_and_similarity_score_are_recorded(self):
        out = self.make(0.5).rerank(self.Q, self.DUPES)
        assert [r["mmr_rank"] for r in out] == [1, 2, 3]
        by_id = {r["id"]: r["score"] for r in out}
        assert by_id["a"] == pytest.approx(2 / np.sqrt(5)) and by_id["c"] == pytest.approx(
            1 / np.sqrt(5)
        )

    def test_limit(self):
        assert len(self.make(0.5).rerank(self.Q, self.DUPES, limit=2)) == 2

    def test_candidates_without_vectors_are_never_selected(self):
        cands = self.DUPES + [{"id": "ghost", "score": 1.0}]
        out = self.make(0.5).rerank(self.Q, cands, limit=10)
        assert "ghost" not in ids(out) and len(out) == 3

    def test_only_vectorless_candidates_falls_back_to_score_order(self):
        """Reranking is a reordering, so a non-empty candidate set must come
        back non-empty. Dropping the few it cannot place is reasonable;
        dropping all of them turns "reorder these" into "discard these",
        which is what made the Easy API's mmr path return nothing at all.
        _rerank_exact already keeps a vectorless candidate at its existing
        score, and this now agrees with it."""
        out = self.make(0.5).rerank(
            self.Q,
            [{"id": "ghost", "score": 0.2}, {"id": "phantom", "score": 0.7}],
        )
        assert ids(out) == ["phantom", "ghost"]

    def test_zero_query_vector_gives_zero_relevance(self):
        out = self.make(1.0).rerank(np.zeros(2), self.DUPES)
        assert all(r["score"] == 0 for r in out)

    def test_get_vector_fn_is_used(self):
        stripped = [{"id": c["id"], "score": c["score"]} for c in self.DUPES]
        lookup = {c["id"]: np.array(c["vector"]) for c in self.DUPES}
        out = self.make(0.5).rerank(self.Q, stripped, get_vector_fn=lookup.get)
        assert ids(out) == ["a", "c", "a2"]


class TestRerankerWeighted:
    def test_vector_key_falls_back_to_score_and_others_default_to_zero(self):
        rr = Reranker(RerankConfig(method=RerankMethod.WEIGHTED))
        out = rr.rerank(QUERY, [{"id": "x", "score": 1.0}])
        assert out[0]["combined_score"] == pytest.approx(0.7)
        assert out[0]["score"] == pytest.approx(0.7)

    def test_named_scores_are_combined_and_reorder(self):
        rr = Reranker(RerankConfig(method=RerankMethod.WEIGHTED))
        cands = [
            {"id": "vec", "score": 1.0, "text_score": 0.0, "recency_score": 0.0},
            {"id": "txt", "score": 0.0, "text_score": 1.0, "recency_score": 1.0},
        ]
        out = rr.rerank(QUERY, cands)
        assert ids(out) == ["vec", "txt"]
        assert {r["id"]: pytest.approx(r["score"]) for r in out} == {"vec": 0.7, "txt": 0.3}

    def test_custom_weights_flip_the_order(self):
        cfg = RerankConfig(method=RerankMethod.WEIGHTED, score_weights={"vector": 0.1, "text": 0.9})
        cands = [
            {"id": "vec", "score": 1.0, "text_score": 0.0},
            {"id": "txt", "score": 0.0, "text_score": 1.0},
        ]
        assert ids(Reranker(cfg).rerank(QUERY, cands)) == ["txt", "vec"]

    def test_limit_and_input_not_mutated(self):
        rr = Reranker(RerankConfig(method=RerankMethod.WEIGHTED))
        cands = [{"id": "a", "score": 1.0}, {"id": "b", "score": 0.5}]
        assert ids(rr.rerank(QUERY, cands, limit=1)) == ["a"]
        assert "combined_score" not in cands[0]


class TestRerankerFallback:
    def test_unhandled_method_sorts_by_existing_score(self):
        rr = Reranker(RerankConfig(method=RerankMethod.RECIPROCAL_RANK))
        out = rr.rerank(QUERY, CANDIDATES, limit=2)
        assert ids(out) == ["far", "mid"]

    def test_missing_score_counts_as_zero(self):
        rr = Reranker(RerankConfig(method=RerankMethod.RECIPROCAL_RANK))
        out = rr.rerank(QUERY, [{"id": "none"}, {"id": "some", "score": 0.2}])
        assert ids(out) == ["some", "none"]


class _FakeCrossEncoder:
    """Scores a pair by how many query words appear in the text. No model, no download."""

    instances = []

    def __init__(self, model_name):
        self.model_name = model_name
        self.batch_sizes = []
        _FakeCrossEncoder.instances.append(self)

    def predict(self, pairs, batch_size=32):
        self.batch_sizes.append(batch_size)
        return [float(sum(w in text for w in query.split())) for query, text in pairs]


@pytest.fixture
def fake_sentence_transformers(monkeypatch):
    module = types.ModuleType("sentence_transformers")
    module.CrossEncoder = _FakeCrossEncoder
    _FakeCrossEncoder.instances.clear()
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return module


class TestRerankerCrossEncoder:
    CANDS = [
        {"id": "off", "score": 0.9, "text": "nothing relevant"},
        {"id": "meta", "score": 0.5, "metadata": {"text": "python search engine"}},
        {"id": "hit", "score": 0.1, "text": "python vector search"},
    ]

    def test_without_query_text_returns_the_head_of_the_list(self):
        rr = Reranker(RerankConfig(method=RerankMethod.CROSS_ENCODER))
        assert ids(rr.rerank(QUERY, self.CANDS, limit=2)) == ["off", "meta"]

    def test_fake_model_reorders_by_its_scores(self, fake_sentence_transformers):
        rr = Reranker(RerankConfig(method=RerankMethod.CROSS_ENCODER, cross_encoder_batch_size=4))
        out = rr.rerank(QUERY, self.CANDS, query_text="python vector search")
        assert ids(out) == ["hit", "meta", "off"]
        hit = out[0]
        assert hit["cross_encoder_score"] == 3.0 and hit["score"] == 3.0
        assert hit["original_score"] == 0.1
        assert _FakeCrossEncoder.instances[0].batch_sizes == [4]

    def test_default_model_name_and_instance_reuse(self, fake_sentence_transformers):
        rr = Reranker(RerankConfig(method=RerankMethod.CROSS_ENCODER))
        rr.rerank(QUERY, self.CANDS, query_text="python")
        rr.rerank(QUERY, self.CANDS, query_text="python")
        assert len(_FakeCrossEncoder.instances) == 1
        assert _FakeCrossEncoder.instances[0].model_name == "cross-encoder/ms-marco-MiniLM-L-6-v2"

    def test_custom_model_name_is_passed_through(self, fake_sentence_transformers):
        cfg = RerankConfig(method=RerankMethod.CROSS_ENCODER, cross_encoder_model="my/model")
        Reranker(cfg).rerank(QUERY, self.CANDS, query_text="python")
        assert _FakeCrossEncoder.instances[0].model_name == "my/model"

    def test_limit_applies_after_scoring(self, fake_sentence_transformers):
        rr = Reranker(RerankConfig(method=RerankMethod.CROSS_ENCODER))
        assert ids(rr.rerank(QUERY, self.CANDS, limit=1, query_text="python vector search")) == [
            "hit"
        ]

    def test_missing_package_falls_back_to_score_order(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "sentence_transformers", None)
        rr = Reranker(RerankConfig(method=RerankMethod.CROSS_ENCODER))
        out = rr.rerank(QUERY, self.CANDS, query_text="python", limit=2)
        assert ids(out) == ["off", "meta"]


# ---------------------------------------------------------------------------
# Facets
# ---------------------------------------------------------------------------

DOCS = [
    {"category": "tech", "tags": ["a", "b"], "author": {"name": "ann"}},
    {"category": "tech", "tags": ["a"], "author": {"name": "bob"}},
    {"category": "science", "tags": [], "author": {"name": "ann"}},
    {"category": None, "tags": ["c"]},
    {"tags": "solo"},
]


class TestFacetAggregator:
    def test_string_field_names_become_default_configs(self):
        facets = FacetAggregator().aggregate(DOCS, ["category"])
        result = facets["category"]
        assert isinstance(result, FacetResult)
        assert [(v.value, v.count) for v in result.values] == [("tech", 2), ("science", 1)]
        assert result.total_count == 3 and result.other_count == 0

    def test_list_values_count_each_element_and_scalars_count_once(self):
        result = FacetAggregator().aggregate(DOCS, ["tags"])["tags"]
        counts = {v.value: v.count for v in result.values}
        assert counts == {"a": 2, "b": 1, "c": 1, "solo": 1}

    def test_nested_dot_path(self):
        result = FacetAggregator().aggregate(DOCS, ["author.name"])["author.name"]
        assert [(v.value, v.count) for v in result.values] == [("ann", 2), ("bob", 1)]

    def test_dot_path_through_a_non_dict_gives_nothing(self):
        result = FacetAggregator().aggregate([{"author": "ann"}], ["author.name"])["author.name"]
        assert result.values == [] and result.total_count == 0

    def test_missing_field_gives_empty_result(self):
        result = FacetAggregator().aggregate(DOCS, ["nope"])["nope"]
        assert result.values == [] and result.total_count == 0 and result.other_count == 0

    def test_empty_documents(self):
        assert FacetAggregator().aggregate([], ["category"])["category"].values == []

    def test_limit_moves_the_tail_into_other_count(self):
        result = FacetAggregator().aggregate(DOCS, [FacetConfig("tags", limit=1)])["tags"]
        assert [(v.value, v.count) for v in result.values] == [("a", 2)]
        assert result.other_count == 3
        assert result.total_count == 5

    def test_min_count_drops_rare_values(self):
        result = FacetAggregator().aggregate(DOCS, [FacetConfig("tags", min_count=2)])["tags"]
        assert [(v.value, v.count) for v in result.values] == [("a", 2)]
        assert result.total_count == 2, "total counts only the values that survived"

    def test_include_zero_keeps_values_under_min_count(self):
        cfg = FacetConfig("tags", min_count=2, include_zero=True)
        result = FacetAggregator().aggregate(DOCS, [cfg])["tags"]
        assert {v.value for v in result.values} == {"a", "b", "c", "solo"}

    def test_sort_by_value_orders_alphabetically(self):
        result = FacetAggregator().aggregate(DOCS, [FacetConfig("tags", sort_by="value")])["tags"]
        assert [v.value for v in result.values] == ["a", "b", "c", "solo"]

    def test_sort_by_value_handles_mixed_types(self):
        docs = [{"n": 10}, {"n": "x"}, {"n": 2}]
        result = FacetAggregator().aggregate(docs, [FacetConfig("n", sort_by="value")])["n"]
        assert [v.value for v in result.values] == [10, 2, "x"]

    def test_falsy_values_are_still_counted(self):
        docs = [{"flag": False}, {"flag": 0}, {"flag": ""}]
        result = FacetAggregator().aggregate(docs, ["flag"])["flag"]
        assert result.total_count == 3

    def test_multiple_fields_and_to_dict(self):
        agg = FacetAggregator()
        facets = agg.aggregate(DOCS, ["category", FacetConfig("tags", limit=1)])
        assert set(facets) == {"category", "tags"}
        as_dict = agg.to_dict(facets)
        assert as_dict["category"] == {
            "values": {"tech": 2, "science": 1},
            "total_count": 3,
            "other_count": 0,
        }
        assert as_dict["tags"] == {"values": {"a": 2}, "total_count": 5, "other_count": 3}

    def test_facet_value_dataclass(self):
        assert FacetValue("x", 3) == FacetValue(value="x", count=3)


# ---------------------------------------------------------------------------
# ACL
# ---------------------------------------------------------------------------


class TestACLPrincipal:
    def test_parse_typed_string(self):
        p = ACLPrincipal.parse("group:engineering")
        assert p.type is ACLOperator.GROUP and p.value == "engineering"

    def test_parse_is_case_insensitive_on_the_type(self):
        assert ACLPrincipal.parse("ROLE:admin").type is ACLOperator.ROLE

    def test_parse_bare_and_unknown_types_default_to_user(self):
        assert ACLPrincipal.parse("alice") == ACLPrincipal(ACLOperator.USER, "alice")
        assert ACLPrincipal.parse("team:core") == ACLPrincipal(ACLOperator.USER, "core")

    def test_parse_keeps_colons_in_the_value(self):
        assert ACLPrincipal.parse("user:a:b").value == "a:b"

    def test_matches_requires_same_type(self):
        user = ACLPrincipal(ACLOperator.USER, "alice")
        group = ACLPrincipal(ACLOperator.GROUP, "alice")
        assert user.matches(ACLPrincipal(ACLOperator.USER, "alice"))
        assert not user.matches(group)
        assert not user.matches(ACLPrincipal(ACLOperator.USER, "bob"))

    def test_wildcard_matches_either_way(self):
        star = ACLPrincipal(ACLOperator.GROUP, "*")
        eng = ACLPrincipal(ACLOperator.GROUP, "engineering")
        assert star.matches(eng) and eng.matches(star)
        assert not star.matches(ACLPrincipal(ACLOperator.USER, "*"))

    def test_str_round_trips(self):
        assert str(ACLPrincipal.parse("group:eng")) == "group:eng"


class TestACLConfig:
    def test_from_list_splits_read_deny_and_public(self):
        cfg = ACLConfig.from_list(["user:alice", "deny:user:bob", "group:eng"])
        assert [str(p) for p in cfg.read_principals] == ["user:alice", "group:eng"]
        assert [str(p) for p in cfg.deny_principals] == ["user:bob"]
        assert cfg.is_public is False

    @pytest.mark.parametrize("marker", ["everyone", "public", "*", "EVERYONE"])
    def test_public_markers(self, marker):
        assert ACLConfig.from_list([marker]).is_public is True

    def test_defaults_are_empty_and_private(self):
        cfg = ACLConfig()
        assert cfg.read_principals == [] and cfg.deny_principals == [] and cfg.is_public is False


class TestACLFilter:
    ALICE = ["user:alice", "group:eng"]

    def test_document_without_acl_uses_default_allow(self):
        docs = [{"id": 1, "metadata": {}}]
        assert ACLFilter().filter(docs, self.ALICE) == []
        assert ACLFilter().filter(docs, self.ALICE, default_allow=True) == docs

    def test_list_acl_allows_matching_user_and_group(self):
        docs = [
            {"id": "mine", "metadata": {"_acl": ["user:alice"]}},
            {"id": "team", "metadata": {"_acl": ["group:eng"]}},
            {"id": "other", "metadata": {"_acl": ["user:bob"]}},
            {"id": "locked", "metadata": {"_acl": []}},
        ]
        assert ids(ACLFilter().filter(docs, self.ALICE)) == ["mine", "team"]

    def test_public_documents_are_visible_to_anyone(self):
        docs = [{"id": "pub", "metadata": {"_acl": ["everyone"]}}]
        assert ids(ACLFilter().filter(docs, [])) == ["pub"]

    def test_deny_wins_over_allow_and_over_public_is_not_checked(self):
        docs = [{"id": "d", "metadata": {"_acl": ["group:eng", "deny:user:alice"]}}]
        assert ACLFilter().filter(docs, self.ALICE) == []
        assert ids(ACLFilter().filter(docs, ["user:carol", "group:eng"])) == ["d"]

    def test_dict_acl_form(self):
        docs = [
            {"id": "d", "metadata": {"_acl": {"read": ["user:alice"], "deny": ["user:bob"]}}},
            {"id": "p", "metadata": {"_acl": {"public": True}}},
        ]
        assert ids(ACLFilter().filter(docs, ["user:alice"])) == ["d", "p"]
        assert ids(ACLFilter().filter(docs, ["user:bob"])) == ["p"]
        assert ids(ACLFilter().filter(docs, ["user:carol"])) == ["p"]

    def test_unrecognised_acl_shape_uses_default_allow(self):
        docs = [{"id": "d", "metadata": {"_acl": "user:alice"}}]
        assert ACLFilter().filter(docs, ["user:alice"]) == []
        assert ACLFilter().filter(docs, ["user:alice"], default_allow=True) == docs

    def test_acl_can_live_at_the_top_level_of_the_document(self):
        docs = [{"id": "flat", "_acl": ["user:alice"]}]
        assert ids(ACLFilter().filter(docs, ["user:alice"])) == ["flat"]

    def test_custom_acl_field(self):
        docs = [{"id": "d", "metadata": {"perms": ["user:alice"]}}]
        assert ACLFilter(acl_field="perms").filter(docs, ["user:alice"]) == docs
        assert ACLFilter().filter(docs, ["user:alice"]) == []

    def test_principals_may_be_objects(self):
        docs = [{"id": "d", "metadata": {"_acl": ["group:eng"]}}]
        who = [ACLPrincipal(ACLOperator.GROUP, "eng")]
        assert ACLFilter().filter(docs, who) == docs

    def test_wildcard_acl_entry_matches_any_user(self):
        docs = [{"id": "d", "metadata": {"_acl": ["user:*"]}}]
        assert ACLFilter().filter(docs, ["user:anyone"]) == docs
        assert ACLFilter().filter(docs, ["group:anyone"]) == []

    def test_add_acl_to_metadata_copies(self):
        original = {"title": "t"}
        out = ACLFilter().add_acl_to_metadata(original, ["user:alice"])
        assert out == {"title": "t", "_acl": ["user:alice"]}
        assert "_acl" not in original

    def test_create_acl_filter_condition(self):
        cond = ACLFilter().create_acl_filter_condition(["user:alice", "group:eng"])
        assert cond == {
            "$or": [
                {"_acl": {"$contains": "everyone"}},
                {"_acl": {"$contains": "public"}},
                {"_acl": {"$contains": "user:alice"}},
                {"_acl": {"$contains": "group:eng"}},
            ]
        }


# ---------------------------------------------------------------------------
# Text analyzers
# ---------------------------------------------------------------------------


class TestTextAnalyzer:
    def test_empty_text(self):
        assert TextAnalyzer().analyze("") == []
        assert TextAnalyzer().analyze(None) == []

    def test_standard_lowercases_and_splits_on_non_alphanumerics(self):
        assert TextAnalyzer.standard().analyze("Hello, World-2!") == ["hello", "world", "2"]

    def test_lowercase_can_be_disabled(self):
        assert TextAnalyzer(lowercase=False).analyze("Hello World") == ["Hello", "World"]

    def test_simple_analyzer_keeps_letters_only(self):
        assert TextAnalyzer.simple().analyze("abc123def 42") == ["abc", "def"]

    def test_token_length_bounds(self):
        analyzer = TextAnalyzer(min_token_length=2, max_token_length=4)
        assert analyzer.analyze("a ab abcd abcde") == ["ab", "abcd"]

    def test_stopwords_removed_when_asked(self):
        text = "the cat and the dog"
        assert TextAnalyzer().analyze(text) == ["the", "cat", "and", "the", "dog"]
        assert TextAnalyzer(remove_stopwords=True).analyze(text) == ["cat", "dog"]

    def test_custom_stopwords(self):
        analyzer = TextAnalyzer(remove_stopwords=True, stopwords={"cat"})
        assert analyzer.analyze("the cat sat") == ["the", "sat"]

    def test_empty_stopword_set_means_no_stopwords(self):
        """An explicit empty set means strip nothing. The constructor used
        ``stopwords or ENGLISH_STOPWORDS``, which read it as unset."""
        analyzer = TextAnalyzer(remove_stopwords=True, stopwords=set())
        assert analyzer.analyze("the cat") == ["the", "cat"]

    def test_synonyms_are_appended_after_the_original(self):
        analyzer = TextAnalyzer(synonyms={"car": ["auto", "vehicle"]})
        assert analyzer.analyze("red car") == ["red", "car", "auto", "vehicle"]

    def test_with_synonyms_factory_also_drops_stopwords(self):
        analyzer = TextAnalyzer.with_synonyms({"big": ["large"]})
        assert analyzer.analyze("the big one") == ["big", "large", "one"]

    def test_english_analyzer_stems_and_drops_stopwords(self):
        tokens = TextAnalyzer.english().analyze("The quick brown foxes are jumping")
        assert tokens[:2] == ["quick", "brown"]
        assert "the" not in tokens and "are" not in tokens
        # NLTK's Porter stemmer and the built-in fallback agree on these two.
        assert tokens[2].startswith("fox") and tokens[3].startswith("jump")
        assert tokens[2] != "foxes" and tokens[3] != "jumping"

    def test_unknown_stemmer_name_means_no_stemming(self):
        analyzer = TextAnalyzer(stemmer="nope")
        assert analyzer._stemmer is None
        assert analyzer.analyze("running") == ["running"]

    def test_falls_back_to_simple_stemmer_without_nltk(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "nltk", None)
        monkeypatch.setitem(sys.modules, "nltk.stem", None)
        for name in ("porter", "snowball", "lancaster"):
            analyzer = TextAnalyzer(stemmer=name)
            assert isinstance(analyzer._stemmer, SimpleStemmer)
        assert TextAnalyzer(stemmer="porter").analyze("cats jumping") == ["cat", "jump"]

    @pytest.mark.parametrize("name", ["porter", "snowball", "lancaster"])
    def test_nltk_stemmers_are_used_when_installed(self, name):
        pytest.importorskip("nltk")
        analyzer = TextAnalyzer(stemmer=name)
        assert not isinstance(analyzer._stemmer, SimpleStemmer)
        assert analyzer.analyze("running") == ["run"]

    def test_analyze_query_matches_analyze(self):
        analyzer = TextAnalyzer.standard()
        assert analyzer.analyze_query("A b") == analyzer.analyze("A b")

    def test_custom_token_pattern(self):
        analyzer = TextAnalyzer(token_pattern=r"#\w+")
        assert analyzer.analyze("say #hi and #bye") == ["#hi", "#bye"]

    def test_analyzer_type_enum_values(self):
        assert AnalyzerType("english") is AnalyzerType.ENGLISH
        assert {t.value for t in AnalyzerType} == {
            "standard",
            "simple",
            "whitespace",
            "keyword",
            "english",
            "custom",
        }


class TestKeywordAnalyzer:
    def test_whole_input_is_one_token(self):
        assert KeywordAnalyzer().analyze("  New York  ") == ["new york"]

    def test_empty(self):
        assert KeywordAnalyzer().analyze("") == []

    def test_factory_returns_keyword_analyzer(self):
        assert isinstance(TextAnalyzer.keyword(), KeywordAnalyzer)


class TestSimpleStemmer:
    @pytest.mark.parametrize(
        "word, stem",
        [
            ("cats", "cat"),
            ("jumping", "jump"),
            ("walked", "walk"),
            ("organization", "organ"),
            ("happiness", "happi"),
            ("Running", "runn"),
            ("dog", "dog"),
            ("is", "is"),
            ("sing", "sing"),
        ],
    )
    def test_strips_one_suffix_when_a_stem_of_three_remains(self, word, stem):
        assert SimpleStemmer().stem(word) == stem

    def test_longest_suffix_is_tried_first(self):
        assert SimpleStemmer().stem("nationalization") == "national"


class TestAnalyzerChain:
    def test_chains_tokens_through_each_analyzer(self):
        chain = AnalyzerChain([KeywordAnalyzer(), TextAnalyzer.standard()])
        assert chain.analyze("Hello World") == ["hello", "world"]

    def test_empty_chain_returns_the_input_untouched(self):
        assert AnalyzerChain([]).analyze("Hello") == ["Hello"]

    def test_later_analyzers_see_expanded_tokens(self):
        chain = AnalyzerChain([TextAnalyzer(synonyms={"car": ["auto"]}), TextAnalyzer.simple()])
        assert chain.analyze("car 1") == ["car", "auto"]


# ---------------------------------------------------------------------------
# EnhancedSearchResults
# ---------------------------------------------------------------------------


class TestEnhancedSearchResults:
    def test_to_dict_flattens_facets(self):
        facets = {"category": FacetResult("category", [FacetValue("tech", 2)], 3, other_count=1)}
        results = EnhancedSearchResults(
            results=[{"id": "a"}],
            facets=facets,
            total_count=10,
            filtered_count=1,
            query_time_ms=1.5,
        )
        assert results.to_dict() == {
            "results": [{"id": "a"}],
            "facets": {"category": {"values": {"tech": 2}, "total": 3, "other": 1}},
            "total_count": 10,
            "filtered_count": 1,
            "query_time_ms": 1.5,
            "rerank_time_ms": 0.0,
            "facet_time_ms": 0.0,
        }
