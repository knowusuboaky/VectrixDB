"""Fuzzing the two surfaces that take untrusted input: filters and the REST API.

A bad filter may be refused with ValueError, TypeError or KeyError; it must
never escape as anything else. A bad request may get any 4xx; it must never be
a 500, because a 500 is the server failing rather than the caller.
"""

import json
import os

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vectrixdb.core.types import Filter

leaf = st.one_of(
    st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False), st.text(max_size=10)
)
anything = st.recursive(
    leaf,
    lambda inner: st.one_of(
        st.lists(inner, max_size=4), st.dictionaries(st.text(max_size=8), inner, max_size=4)
    ),
    max_leaves=12,
)
op_names = st.sampled_from(
    [
        "$eq",
        "$ne",
        "$gt",
        "$gte",
        "$lt",
        "$lte",
        "$in",
        "$nin",
        "$all",
        "$any",
        "$contains",
        "$starts_with",
        "$regex",
        "$between",
        "$date_range",
        "$exists",
        "$geo_radius",
        "$bogus",
        "$and",
        "$or",
        "$not",
    ]
)
filter_like = st.recursive(
    st.dictionaries(
        st.text(max_size=6),
        st.one_of(leaf, st.dictionaries(op_names, anything, max_size=2)),
        max_size=3,
    ),
    lambda inner: st.dictionaries(
        st.sampled_from(["$and", "$or", "$not", "must", "should", "must_not"]),
        st.one_of(inner, st.lists(inner, max_size=3)),
        max_size=2,
    ),
    max_leaves=10,
)


class TestFilterParser:
    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    @given(spec=filter_like, meta=st.dictionaries(st.text(max_size=6), anything, max_size=4))
    def test_only_the_documented_errors_escape(self, spec, meta):
        try:
            flt = Filter.from_dict(spec)
        except (ValueError, TypeError, KeyError):
            return
        try:
            result = flt.matches(meta)
        except (ValueError, TypeError, KeyError):
            return
        assert isinstance(result, bool)


fastapi = pytest.importorskip("fastapi")


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from fastapi.testclient import TestClient

    os.environ["VECTRIXDB_PATH"] = str(tmp_path_factory.mktemp("api"))
    from vectrixdb.api import server

    with TestClient(server.create_app(enable_dashboard=False)) as c:
        yield c


BODIES = [
    None,
    {},
    [],
    "text",
    42,
    {"vectors": "not-a-list"},
    {"ids": [1, 2]},
    {"texts": None},
    {"query": {"nested": True}},
    {"limit": -1},
    {"limit": "many"},
    {"filter": {"$bogus": 1}},
]


def _routes():
    from vectrixdb.api import server

    for route in server.router.routes:
        methods = getattr(route, "methods", None) or set()
        for method in methods:
            if method in ("GET", "POST", "DELETE", "PUT"):
                yield (
                    method,
                    route.path.replace("{name}", "zz")
                    .replace("{doc_id}", "zz")
                    .replace("{id}", "zz"),
                )


class TestRestSurface:
    def test_no_request_produces_a_500(self, client):
        failures = []
        for method, path in _routes():
            if "{" in path:
                path = path.replace("{", "").replace("}", "")
            for body in BODIES:
                kwargs = {}
                if method in ("POST", "PUT"):
                    kwargs["content"] = json.dumps(body)
                    kwargs["headers"] = {"content-type": "application/json"}
                response = client.request(method, path, **kwargs)
                if response.status_code >= 500:
                    failures.append((method, path, body, response.status_code, response.text[:120]))
                if method in ("GET", "DELETE"):
                    break
        assert not failures, "\n".join(map(str, failures))
