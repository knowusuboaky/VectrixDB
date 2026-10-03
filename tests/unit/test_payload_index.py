"""Payload indexes decide which documents a filter admits.

A filter that quietly returns the wrong set is worse than one that errors: the
search still succeeds and the caller never learns that a document was excluded.
These cover the query operators against a brute-force answer computed from the
same data, so a wrong bisect boundary or a missed tie shows up immediately.
"""

import math

import pytest

from vectrixdb.core.payload_index import (
    GeoIndex,
    NumericRangeIndex,
    PayloadIndexManager,
    StringIndex,
)

PRICES = {
    "a": 10.0,
    "b": 25.5,
    "c": 25.5,  # a deliberate tie, which is where bisect boundaries go wrong
    "d": 99.99,
    "e": 0.0,
    "f": -5.0,
}


@pytest.fixture
def prices() -> NumericRangeIndex:
    index = NumericRangeIndex("price")
    for doc_id, value in PRICES.items():
        index.add(doc_id, value)
    return index


def brute(op, threshold) -> set:
    """The answer computed directly, for comparison."""
    tests = {
        "eq": lambda v: v == threshold,
        "ne": lambda v: v != threshold,
        "gt": lambda v: v > threshold,
        "gte": lambda v: v >= threshold,
        "lt": lambda v: v < threshold,
        "lte": lambda v: v <= threshold,
    }
    return {k for k, v in PRICES.items() if tests[op](v)}


class TestNumericRangeIndex:
    @pytest.mark.parametrize("op", ["eq", "ne", "gt", "gte", "lt", "lte"])
    @pytest.mark.parametrize("threshold", [-5.0, 0.0, 10.0, 25.5, 99.99, 50.0])
    def test_operators_match_a_direct_scan(self, prices, op, threshold):
        assert prices.query(op, threshold) == brute(op, threshold)

    def test_between_is_inclusive_at_both_ends(self, prices):
        assert prices.query("between", (10.0, 25.5)) == {"a", "b", "c"}

    def test_between_requires_a_pair(self, prices):
        with pytest.raises(ValueError):
            prices.query("between", 10.0)

    def test_unknown_operator_is_rejected(self, prices):
        with pytest.raises(ValueError):
            prices.query("approximately", 10.0)

    def test_ties_are_all_returned(self, prices):
        """b and c share a value; neither may be lost to a boundary error."""
        assert prices.query("eq", 25.5) == {"b", "c"}

    def test_remove_then_query(self, prices):
        assert prices.remove("b") is True
        assert prices.query("eq", 25.5) == {"c"}
        assert prices.remove("b") is False

    def test_update_moves_a_document(self, prices):
        prices.update("a", 1000.0)
        assert "a" not in prices.query("lt", 50.0)
        assert "a" in prices.query("gt", 500.0)

    def test_non_numeric_values_are_ignored(self):
        index = NumericRangeIndex("price")
        index.add("x", "not a number")
        index.add("y", None)
        assert index.query("gte", 0) == set()

    def test_min_and_max(self, prices):
        assert prices.get_min() == -5.0
        assert prices.get_max() == 99.99

    def test_readding_the_same_document_does_not_duplicate_it(self, prices):
        prices.add("a", 10.0)
        assert prices.query("eq", 10.0) == {"a"}


class TestStringIndex:
    @pytest.fixture
    def colours(self) -> StringIndex:
        index = StringIndex("colour")
        for doc_id, value in {"a": "red", "b": "blue", "c": "red", "d": "green"}.items():
            index.add(doc_id, value)
        return index

    def test_exact_match(self, colours):
        assert colours.query("eq", "red") == {"a", "c"}

    def test_not_equal_excludes_only_the_match(self, colours):
        assert colours.query("ne", "red") == {"b", "d"}

    def test_contains_is_a_substring_match(self, colours):
        assert colours.query("contains", "red") == {"a", "c"}
        assert colours.query("contains", "ree") == {"d"}  # green

    def test_contains_finds_substrings_anywhere_in_the_value(self, colours):
        """Not just whole values.

        The query used to be padded with boundary markers exactly like the
        stored value, which anchored every search at both ends and made
        `contains` behave as `equals`. It only looked correct when the query
        happened to be the entire value.
        """
        assert colours.query("contains", "lue") == {"b"}  # blue
        assert colours.query("contains", "een") == {"d"}  # green

    def test_contains_handles_queries_shorter_than_a_trigram(self, colours):
        """Too short to look up in the trigram index, so it falls back to a scan
        rather than silently returning nothing."""
        assert colours.query("contains", "re") == {"a", "c", "d"}  # red, red, green
        assert colours.query("contains", "zz") == set()

    def test_starts_with(self, colours):
        assert colours.query("starts_with", "gr") == {"d"}

    def test_ends_with(self, colours):
        assert colours.query("ends_with", "ue") == {"b"}

    def test_missing_value_returns_empty(self, colours):
        assert colours.query("eq", "purple") == set()

    def test_remove(self, colours):
        assert colours.remove("a") is True
        assert colours.query("eq", "red") == {"c"}

    def test_update_moves_between_buckets(self, colours):
        colours.update("a", "blue")
        assert colours.query("eq", "red") == {"c"}
        assert colours.query("eq", "blue") == {"a", "b"}

    def test_unique_values(self, colours):
        assert set(colours.get_unique_values()) == {"red", "blue", "green"}


class TestGeoIndex:
    @pytest.fixture
    def cities(self) -> GeoIndex:
        index = GeoIndex("location")
        # Toronto, Montreal, Vancouver, Sydney
        for doc_id, coords in {
            "toronto": (43.6532, -79.3832),
            "montreal": (45.5019, -73.5674),
            "vancouver": (49.2827, -123.1207),
            "sydney": (-33.8688, 151.2093),
        }.items():
            index.add(doc_id, coords)
        return index

    def test_radius_includes_the_near_city_and_excludes_the_far_one(self, cities):
        """Montreal is ~500 km from Toronto; Sydney is not."""
        near = cities.query("geo_radius", {"lat": 43.6532, "lon": -79.3832, "radius_km": 600})
        assert "toronto" in near
        assert "montreal" in near
        assert "sydney" not in near

    def test_a_tiny_radius_returns_only_the_centre(self, cities):
        near = cities.query("geo_radius", {"lat": 43.6532, "lon": -79.3832, "radius_km": 1})
        assert near == {"toronto"}

    def test_antipodal_distance_is_within_earth_half_circumference(self, cities):
        everything = cities.query(
            "geo_radius", {"lat": 43.6532, "lon": -79.3832, "radius_km": 20100}
        )
        assert everything == {"toronto", "montreal", "vancouver", "sydney"}

    def test_location_round_trips(self, cities):
        lat, lon = cities.get_location("toronto")
        assert math.isclose(lat, 43.6532, abs_tol=1e-4)
        assert math.isclose(lon, -79.3832, abs_tol=1e-4)

    def test_remove(self, cities):
        assert cities.remove("sydney") is True
        assert cities.get_location("sydney") is None


class TestPayloadIndexManager:
    @pytest.fixture
    def manager(self, tmp_path) -> PayloadIndexManager:
        return PayloadIndexManager(path=tmp_path)

    def test_auto_picks_a_type_per_field(self, manager):
        manager.create_index("price", "auto")
        manager.create_index("colour", "auto")
        manager.index_document("a", {"price": 10.0, "colour": "red"})
        assert manager.has_index("price")
        assert manager.has_index("colour")

    def test_query_intersects_across_fields(self, manager):
        manager.create_index("price", "numeric")
        manager.create_index("colour", "string")
        manager.index_document("a", {"price": 10.0, "colour": "red"})
        manager.index_document("b", {"price": 10.0, "colour": "blue"})
        manager.index_document("c", {"price": 99.0, "colour": "red"})

        assert manager.query({"colour": "red"}) == {"a", "c"}
        assert manager.query({"price": 10.0, "colour": "red"}) == {"a"}

    def test_query_without_any_index_returns_none(self, manager):
        """None means "cannot help", which is different from "matched nothing"."""
        assert manager.query({"unindexed": 1}) is None

    def test_remove_document_clears_every_field(self, manager):
        manager.create_index("colour", "string")
        manager.index_document("a", {"colour": "red"})
        manager.remove_document("a")
        assert manager.query({"colour": "red"}) == set()

    def test_drop_index(self, manager):
        manager.create_index("colour", "string")
        assert manager.drop_index("colour") is True
        assert not manager.has_index("colour")
        assert manager.drop_index("colour") is False

    def test_indexes_survive_save_and_load(self, manager, tmp_path):
        manager.create_index("colour", "string")
        manager.index_document("a", {"colour": "red"})
        manager.save()

        reloaded = PayloadIndexManager(path=tmp_path).load()
        assert reloaded.has_index("colour")
        assert reloaded.query({"colour": "red"}) == {"a"}
