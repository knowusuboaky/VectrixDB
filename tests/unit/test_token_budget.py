"""search(token_budget=..., score_gap=...) and the helpers behind them.

A result list goes into a model's context, so its cost has to be bounded and
any cut has to be announced. The two rules that matter: the top result always
ships, and a shortened list never looks complete.
"""

import pytest

from vectrixdb import Vectrix
from vectrixdb._ranking import apply_score_gap, fit_to_budget
from vectrixdb._tokens import estimate_tokens

CORPUS = [
    "Python is a programming language with significant whitespace and a large standard library.",
    "Rust guarantees memory safety without a garbage collector through ownership rules.",
    "Go compiles quickly and ships goroutines for cheap concurrency.",
    "Haskell is a purely functional language with lazy evaluation and a strong type system.",
    "Tomatoes are botanically fruit but get cooked as a vegetable in most kitchens.",
    "The Nobel Prize was awarded for research on radioactivity.",
    "A sourdough starter is flour and water kept alive by wild yeast and bacteria.",
    "Basalt forms when lava cools quickly at the surface.",
]


@pytest.fixture(scope="module")
def db(tmp_path_factory) -> Vectrix:
    db = Vectrix("budget", path=str(tmp_path_factory.mktemp("budget")))
    db.add(CORPUS)
    return db


class TestEstimate:
    def test_empty_is_free(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens(None) == 0

    def test_never_zero_for_text(self):
        assert estimate_tokens("a") == 1

    def test_four_chars_per_token(self):
        assert estimate_tokens("x" * 40) == 10
        assert estimate_tokens("x" * 41) == 11

    def test_counter_is_authoritative(self):
        assert estimate_tokens("anything", counter=lambda s: 7) == 7


class TestFitToBudget:
    @staticmethod
    def _items(*lengths):
        return ["x" * n for n in lengths]

    def test_keeps_in_order_until_spent(self):
        kept, cut, spent = fit_to_budget(self._items(10, 10, 10), 25, str, counter=len)
        assert kept == ["x" * 10, "x" * 10]
        assert cut == 1
        assert spent == 20

    def test_first_item_survives_any_budget(self):
        kept, cut, spent = fit_to_budget(self._items(100, 1), 5, str, counter=len)
        assert kept == ["x" * 100]
        assert cut == 1
        assert spent == 100, "the estimate reports the real cost, not the budget"

    def test_no_budget_cuts_nothing_but_still_counts(self):
        kept, cut, spent = fit_to_budget(self._items(3, 4), None, str, counter=len)
        assert len(kept) == 2 and cut == 0 and spent == 7

    def test_zero_budget_still_ships_the_top(self):
        kept, cut, _ = fit_to_budget(self._items(3, 4), 0, str, counter=len)
        assert len(kept) == 1 and cut == 1

    def test_negative_budget_is_refused(self):
        with pytest.raises(ValueError):
            fit_to_budget(self._items(1), -1, str)


class TestScoreGap:
    def test_drops_the_tail(self):
        scored = [("a", 1.0), ("b", 0.9), ("c", 0.15), ("d", 0.1)]
        kept = apply_score_gap(scored, 0.2, lambda s: s[1])
        assert [k for k, _ in kept] == ["a", "b"]

    def test_first_always_survives(self):
        assert apply_score_gap([("a", 0.01)], 0.5, lambda s: s[1]) == [("a", 0.01)]

    def test_none_disables(self):
        scored = [("a", 1.0), ("b", 0.0)]
        assert apply_score_gap(scored, None, lambda s: s[1]) == scored

    def test_non_positive_top_disables(self):
        """A fraction of nothing is not a threshold."""
        scored = [("a", 0.0), ("b", -0.5)]
        assert apply_score_gap(scored, 0.2, lambda s: s[1]) == scored

    def test_out_of_range_is_refused(self):
        with pytest.raises(ValueError):
            apply_score_gap([("a", 1.0)], 1.5, lambda s: s[1])


class TestSearchBudget:
    def test_budget_cuts_and_reports(self, db):
        full = db.search("programming languages", limit=8)
        assert len(full) == 8 and not full.truncated and full.cut_count == 0
        assert full.token_estimate > 0, "cost is reported even without a budget"

        small = db.search("programming languages", limit=8, token_budget=40)
        assert 0 < len(small) < 8
        assert small.truncated
        assert small.cut_count == 8 - len(small)
        assert small.token_estimate <= 40
        assert small.token_budget == 40

    def test_budget_never_reorders(self, db):
        full = db.search("programming languages", limit=8)
        small = db.search("programming languages", limit=8, token_budget=60)
        assert full.ids[: len(small)] == small.ids

    def test_top_result_survives_a_budget_smaller_than_itself(self, db):
        results = db.search("memory safety", limit=5, token_budget=1)
        assert len(results) == 1
        assert results.truncated and results.cut_count == 4
        assert "Rust" in results.top.text

    def test_custom_counter_is_used(self, tmp_path):
        db = Vectrix("counted", path=str(tmp_path), token_counter=lambda s: 1000)
        db.add(CORPUS[:3])
        results = db.search("language", limit=3, token_budget=1500)
        assert len(results) == 1, "with every text costing 1000, a 1500 budget fits one"
        assert results.token_estimate == 1000

    def test_repr_mentions_the_cut(self, db):
        assert "cut" in repr(db.search("language", limit=8, token_budget=1))


class TestSearchGap:
    def test_gap_returns_fewer_for_a_specific_query(self, db):
        loose = db.search("sourdough starter yeast", limit=8)
        tight = db.search("sourdough starter yeast", limit=8, score_gap=0.9)
        assert tight.top.id == loose.top.id
        assert len(tight) < len(loose)
        floor = tight.top.score * 0.9
        assert all(s >= floor for s in tight.scores)

    def test_gap_and_budget_compose(self, db):
        results = db.search("sourdough starter yeast", limit=8, score_gap=0.5, token_budget=1)
        assert len(results) == 1 and results.truncated

    def test_invalid_gap_is_refused(self, db):
        with pytest.raises(ValueError):
            db.search("x", score_gap=2.0)


class TestGetById:
    """Regression: get() passed ids= to a method that takes one id, and raised."""

    def test_get_returns_the_document(self, db):
        target = db.search("basalt lava", limit=1).top
        got = db.get(target.id)
        assert len(got) == 1
        assert got[0].text == target.text

    def test_missing_ids_are_skipped_not_raised(self, db):
        assert db.get(["does-not-exist"]) == []

    def test_similar_works(self, db):
        target = db.search("basalt lava", limit=1).top
        similar = db.similar(target.id, limit=3)
        assert target.id not in similar.ids
        assert len(similar) == 3
