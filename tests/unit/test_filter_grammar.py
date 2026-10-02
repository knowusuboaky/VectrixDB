"""Every documented filter operator, exercised through the public search.

This file is the executable form of docs/reference/filters.md: if an operator
is documented, it is used here on a real collection.
"""

import pytest

from pathlib import Path

from vectrixdb import Vectrix

ROOT_DIR = Path(__file__).resolve().parents[2]

DOCS = [
    (
        "laptop stand, aluminium",
        {
            "cat": "office",
            "price": 51.25,
            "tags": ["desk", "metal"],
            "stock": 3,
            "added": "2026-03-01",
            "dims": {"w": 20, "h": 10},
            "notes": None,
            "loc": {"lat": 43.65, "lon": -79.38},
        },
    ),
    (
        "standing desk mat",
        {
            "cat": "office",
            "price": 39.0,
            "tags": ["desk", "foam"],
            "stock": 0,
            "added": "2026-06-15",
            "dims": {"w": 90, "h": 1},
            "notes": "",
            "loc": {"lat": 43.70, "lon": -79.40},
        },
    ),
    (
        "espresso machine",
        {
            "cat": "kitchen",
            "price": 420.0,
            "tags": ["coffee"],
            "stock": 1,
            "added": "2025-12-24",
            "dims": {"w": 30, "h": 40},
            "notes": "fragile, ships in a Crate",
        },
    ),
    (
        "pour-over kettle",
        {
            "cat": "kitchen",
            "price": 65.0,
            "tags": ["coffee", "metal"],
            "stock": 12,
            "added": "2026-09-01",
            "dims": {"w": 25, "h": 25},
            "discontinued": True,
        },
    ),
]


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    db = Vectrix("filters", path=str(tmp_path_factory.mktemp("f")))
    db.add([t for t, _ in DOCS], metadata=[m for _, m in DOCS])
    return db


def names(db, flt):
    return sorted(r.text for r in db.search("product", limit=10, filter=flt))


class TestSimpleForm:
    def test_equality(self, db):
        assert names(db, {"cat": "kitchen"}) == ["espresso machine", "pour-over kettle"]

    def test_comparisons(self, db):
        assert names(db, {"price": {"$lt": 60}}) == ["laptop stand, aluminium", "standing desk mat"]
        assert names(db, {"price": {"$gte": 65, "$lte": 420}}) == [
            "espresso machine",
            "pour-over kettle",
        ]

    def test_in_and_nin(self, db):
        assert names(db, {"cat": {"$in": ["kitchen"]}}) == ["espresso machine", "pour-over kettle"]
        assert names(db, {"cat": {"$nin": ["kitchen"]}}) == [
            "laptop stand, aluminium",
            "standing desk mat",
        ]

    def test_list_fields(self, db):
        assert names(db, {"tags": {"$any": ["metal"]}}) == [
            "laptop stand, aluminium",
            "pour-over kettle",
        ]
        assert names(db, {"tags": {"$all": ["desk", "metal"]}}) == ["laptop stand, aluminium"]

    def test_strings(self, db):
        assert names(db, {"cat": {"$starts_with": "off"}}) == [
            "laptop stand, aluminium",
            "standing desk mat",
        ]
        assert names(db, {"cat": {"$regex": "^kit"}}) == ["espresso machine", "pour-over kettle"]

    def test_dates_compare_as_iso_strings(self, db):
        assert names(db, {"added": {"$gte": "2026-06-01"}}) == [
            "pour-over kettle",
            "standing desk mat",
        ]
        assert names(db, {"added": {"$date_range": ["2026-01-01", "2026-06-30"]}}) == [
            "laptop stand, aluminium",
            "standing desk mat",
        ]

    def test_nested_keys(self, db):
        assert names(db, {"dims.w": {"$gt": 28}}) == ["espresso machine", "standing desk mat"]

    def test_exists_and_null(self, db):
        assert names(db, {"discontinued": {"$exists": True}}) == ["pour-over kettle"]
        assert len(names(db, {"discontinued": {"$exists": False}})) == 3

    def test_between(self, db):
        assert names(db, {"stock": {"$between": [1, 5]}}) == [
            "espresso machine",
            "laptop stand, aluminium",
        ]


class TestComposition:
    def test_and_or_not(self, db):
        flt = {"$and": [{"cat": "office"}, {"$or": [{"stock": 0}, {"price": {"$gt": 50}}]}]}
        assert names(db, flt) == ["laptop stand, aluminium", "standing desk mat"]
        assert names(db, {"$not": {"cat": "office"}}) == ["espresso machine", "pour-over kettle"]

    def test_qdrant_style(self, db):
        flt = {
            "must": [{"key": "cat", "match": {"value": "kitchen"}}],
            "must_not": [{"key": "discontinued", "match": {"value": True}}],
        }
        assert names(db, flt) == ["espresso machine"]

    def test_extended_form(self, db):
        flt = {"field": "price", "op": "lte", "value": 51.25}
        assert names(db, flt) == ["laptop stand, aluminium", "standing desk mat"]


class TestTheOperatorsThisFileUsedToMiss:
    """Nine documented operators had no test here, and every filter bug found
    in the September audit was among them. The page promises that each one is
    exercised against a real collection, so each one is."""

    def test_explicit_equality_and_inequality(self, db):
        assert names(db, {"cat": {"$eq": "kitchen"}}) == [
            "espresso machine",
            "pour-over kettle",
        ]
        assert names(db, {"cat": {"$ne": "kitchen"}}) == [
            "laptop stand, aluminium",
            "standing desk mat",
        ]

    def test_substring_operators(self, db):
        assert names(db, {"cat": {"$contains": "ffic"}}) == [
            "laptop stand, aluminium",
            "standing desk mat",
        ]
        # $contains is case-sensitive and $icontains is not
        assert names(db, {"notes": {"$contains": "crate"}}) == []
        assert names(db, {"notes": {"$icontains": "crate"}}) == ["espresso machine"]
        assert names(db, {"cat": {"$ends_with": "hen"}}) == [
            "espresso machine",
            "pour-over kettle",
        ]

    def test_null_empty_and_absent_are_three_different_things(self, db):
        # laptop stand has notes=None, the mat has "", the machine has text,
        # and the kettle has no notes key at all.
        assert names(db, {"notes": {"$is_null": True}}) == ["laptop stand, aluminium"]
        assert names(db, {"notes": {"$is_empty": True}}) == ["standing desk mat"]
        assert names(db, {"notes": {"$exists": True}}) == [
            "espresso machine",
            "laptop stand, aluminium",
            "standing desk mat",
        ]
        assert names(db, {"notes": {"$exists": False}}) == ["pour-over kettle"]

    def test_geo_radius(self, db):
        near_toronto = {
            "loc": {
                "$geo_radius": {
                    "center": {"lat": 43.65, "lon": -79.38},
                    "radius_km": 10,
                }
            }
        }
        assert names(db, near_toronto) == ["laptop stand, aluminium", "standing desk mat"]

        tight = {
            "loc": {
                "$geo_radius": {
                    "center": {"lat": 43.65, "lon": -79.38},
                    "radius_km": 1,
                }
            }
        }
        assert names(db, tight) == ["laptop stand, aluminium"]

    def test_geo_box(self, db):
        box = {
            "loc": {
                "$geo_box": {
                    "min_lat": 43.6,
                    "max_lat": 43.8,
                    "min_lon": -79.5,
                    "max_lon": -79.3,
                }
            }
        }
        assert names(db, box) == ["laptop stand, aluminium", "standing desk mat"]

        elsewhere = {
            "loc": {
                "$geo_box": {
                    "min_lat": 0.0,
                    "max_lat": 1.0,
                    "min_lon": 0.0,
                    "max_lon": 1.0,
                }
            }
        }
        assert names(db, elsewhere) == []


def test_every_documented_operator_is_used_here():
    """The claim at the top of docs/reference/filters.md, enforced. Ten
    operators were named on that page and used nowhere in this file."""
    import re

    doc = (ROOT_DIR / "docs" / "reference" / "filters.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"\$[a-z_]+", doc)) - {"$op"}  # $op is prose
    here = Path(__file__).read_text(encoding="utf-8")
    missing = sorted(op for op in documented if op not in here)
    assert not missing, f"documented but never exercised: {missing}"
