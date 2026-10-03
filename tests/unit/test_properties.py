"""Property tests: invariants that hold for every input, not chosen examples.

Each test states a law and lets Hypothesis look for the input that breaks it.
"""

import math
from datetime import timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from vectrixdb._ranking import apply_score_gap, fit_to_budget
from vectrixdb._time import utcnow
from vectrixdb._tokens import estimate_tokens
from vectrixdb.core.collection import TextIndex
from vectrixdb.core.graphrag.graph.resolution import EntityResolver
from vectrixdb.core.types import Filter
from vectrixdb.memory import _memory_id

texts = st.lists(st.text(min_size=0, max_size=40), min_size=0, max_size=20)


class TestBudget:
    @given(items=texts, budget=st.integers(min_value=0, max_value=200))
    def test_never_over_budget_past_the_first_item(self, items, budget):
        kept, cut, spent = fit_to_budget(items, budget, str)
        assert kept == items[: len(kept)], "kept items are a prefix, order intact"
        assert cut == len(items) - len(kept)
        if len(kept) > 1:
            assert spent <= budget
        if items:
            assert kept, "the top item always ships"

    @given(items=texts)
    def test_no_budget_cuts_nothing(self, items):
        kept, cut, spent = fit_to_budget(items, None, str)
        assert kept == items and cut == 0
        assert spent == sum(estimate_tokens(t) for t in items)

    @given(text=st.text(max_size=500))
    def test_estimate_is_monotone_and_bounded(self, text):
        n = estimate_tokens(text)
        assert n == (0 if not text else max(1, math.ceil(len(text) / 4)))
        assert estimate_tokens(text + "x") >= n


class TestScoreGap:
    @given(
        scores=st.lists(
            st.floats(min_value=0, max_value=1, allow_nan=False), min_size=1, max_size=30
        ),
        ratio=st.floats(min_value=0.01, max_value=1.0),
    )
    def test_keeps_a_prefix_of_the_sorted_input(self, scores, ratio):
        ordered = sorted(scores, reverse=True)
        kept = apply_score_gap(ordered, ratio, lambda s: s)
        assert kept == ordered[: len(kept)]
        assert kept[0] == ordered[0]
        if ordered[0] > 0:
            assert all(s >= ordered[0] * ratio for s in kept[1:])


scalar = st.one_of(
    st.integers(-1000, 1000), st.floats(-1e6, 1e6, allow_nan=False), st.text(max_size=8)
)


class TestFilters:
    @given(value=scalar, target=scalar)
    def test_comparisons_agree_with_python(self, value, target):
        if type(value) is not type(target) or isinstance(value, bool):
            return
        meta = {"k": value}
        for op, fn in (
            ("$eq", lambda a, b: a == b),
            ("$ne", lambda a, b: a != b),
            ("$gt", lambda a, b: a > b),
            ("$gte", lambda a, b: a >= b),
            ("$lt", lambda a, b: a < b),
            ("$lte", lambda a, b: a <= b),
        ):
            assert Filter.from_dict({"k": {op: target}}).matches(meta) == fn(value, target), op

    @given(value=st.integers(-50, 50), options=st.lists(st.integers(-50, 50), max_size=6))
    def test_in_and_nin_are_complements(self, value, options):
        meta = {"k": value}
        inside = Filter.from_dict({"k": {"$in": options}}).matches(meta)
        outside = Filter.from_dict({"k": {"$nin": options}}).matches(meta)
        assert inside == (value in options)
        assert outside == (value not in options)

    @given(
        lat1=st.floats(-80, 80),
        lon1=st.floats(-179, 179),
        lat2=st.floats(-80, 80),
        lon2=st.floats(-179, 179),
        radius=st.floats(1, 20000),
    )
    def test_geo_radius_matches_haversine(self, lat1, lon1, lat2, lon2, radius):
        from math import atan2, cos, radians, sin, sqrt

        dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
        a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
        distance = 6371 * 2 * atan2(sqrt(a), sqrt(1 - a))
        if abs(distance - radius) < 1e-6:
            return  # boundary: floating-point noise decides, not a law
        flt = Filter.from_dict(
            {"loc": {"$geo_radius": {"center": {"lat": lat2, "lon": lon2}, "radius_km": radius}}}
        )
        assert flt.matches({"loc": {"lat": lat1, "lon": lon1}}) == (distance <= radius)


class TestEntityResolution:
    @given(
        name=st.text(
            alphabet=st.characters(whitelist_categories=("Lu", "Ll")), min_size=3, max_size=12
        )
    )
    def test_a_name_resolves_to_itself(self, name):
        assert EntityResolver().compare(name, name).matched

    @given(
        base=st.text(
            alphabet=st.characters(whitelist_categories=("Lu", "Ll")), min_size=3, max_size=10
        ),
        a=st.integers(1, 99),
        b=st.integers(1, 99),
    )
    def test_different_numbers_never_merge(self, base, a, b):
        if a == b:
            return
        resolver = EntityResolver()
        assert not resolver.compare(f"{base} {a}", f"{base} {b}").matched
        assert not resolver.compare(f"{base} {b}", f"{base} {a}").matched


class TestTextIndex:
    @given(text=st.text(max_size=200))
    def test_tokens_are_never_empty(self, text):
        index = TextIndex()
        assert all(t for t in index._tokenize(text))
        # casefold, not lower: Cherokee folds toward its capitals by Unicode's
        # own rule, so a folded token can differ from its lower() form.
        assert all(t == t.casefold() for t in index._tokenize(text))

    @settings(max_examples=60)
    @given(docs=st.lists(st.text(min_size=1, max_size=60), min_size=1, max_size=8))
    def test_remove_undoes_add(self, docs):
        index = TextIndex()
        for i, d in enumerate(docs):
            index.add(f"d{i}", d)
        for i in range(len(docs)):
            index.remove(f"d{i}")
        assert index._doc_count == 0 and index._total_length == 0
        assert index._inverted_index == {} and index._docs == {}


class TestMemoryIds:
    @given(
        session=st.one_of(st.none(), st.text(max_size=8)),
        turn_a=st.integers(0, 10**6),
        turn_b=st.integers(0, 10**6),
        text=st.text(max_size=30),
    )
    def test_ids_differ_when_turns_differ(self, session, turn_a, turn_b, text):
        now = utcnow()
        a = _memory_id(session, turn_a, now, text)
        b = _memory_id(session, turn_b, now + timedelta(seconds=1), text)
        assert a.startswith("mem_") and len(a) == 20
        if turn_a != turn_b:
            assert a != b
