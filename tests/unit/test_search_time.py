"""How long a search took, written on its line in the access log.

The server never recorded it. The Overview's "Search p50" is the browser's own
session, so nobody could see search getting slower over a week. A search's
line is now written once the search has run, with the time on it. What is held
to: the time is there and is about the search alone, nothing else about the
line changed, the rule the line was written first to keep still holds, a search
that cannot be recorded is not served, and the daily figures are the median
and the slowest one in twenty by nearest rank.
"""

from __future__ import annotations

import json
import time

import numpy as np
import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("jwt", reason="the signin extra is not installed")
pytest.importorskip("cryptography", reason="the signin extra is not installed")

from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.signin import SignInConfig  # noqa: E402
from vectrixdb.signin.access import AccessLog, AccessLogUnavailable, _at  # noqa: E402

SECRET = "k" * 48
PUBLIC = "https://vectors.example.test"
KEY = "the-admin-api-key"
ADMIN = {"api-key": KEY}
DAY = 86400
NOON = (int(time.time()) // DAY) * DAY + DAY // 2


def _embed(texts):
    return np.array([[1, 0, 0, 0] for _ in texts], dtype=np.float32)


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "db"
    one = Vectrix("handbook", path=str(root), dimension=4, embed_fn=_embed, embedding_cache=False)
    one.add(["alpha"], ids=["a"])
    one.close()
    return root


@pytest.fixture
def server(data, monkeypatch):
    from vectrixdb.api.server import create_app

    monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
    monkeypatch.delenv("VECTRIXDB_AUDIT_JSONL", raising=False)
    config = SignInConfig(
        methods=("email",),
        secrets=(SECRET,),
        public_url=PUBLIC,
        users=(("ada@example.com", "admin"),),
        guests=True,
        store_path=data / "auth" / "signin.db",
        access_log=data / "auth" / "access.jsonl",
        sender=lambda to, subject, text: None,
    )
    with TestClient(
        create_app(db_path=str(data), enable_dashboard=False, signin=config), base_url=PUBLIC
    ) as client:
        yield client, data / "auth" / "access.jsonl"


def lines(path, event="search"):
    return [
        json.loads(raw)
        for raw in path.read_text(encoding="utf-8").splitlines()
        if json.loads(raw).get("event") == event
    ]


class TestTheLineSaysHowLong:
    def test_a_search_is_one_line_with_its_time_and_its_status(self, server):
        client, log = server
        assert (
            client.post(
                "/api/v1/collections/handbook/search", json={"query": [1, 0, 0, 0]}, headers=ADMIN
            ).status_code
            == 200
        )
        (line,) = lines(log)
        assert 0 <= line["took_ms"] < 60_000 and line["status"] == 200
        assert (
            line["collection"] == "handbook"
            and line["who"] == "api-key"
            and line["action"] == "search"
        )

    def test_a_search_that_fails_is_still_written_with_what_it_answered(self, server):
        client, log = server
        assert (
            client.post(
                "/api/v1/collections/nowhere/search", json={"query": [1, 0, 0, 0]}, headers=ADMIN
            ).status_code
            == 404
        )
        (line,) = lines(log)
        assert line["status"] == 404 and "took_ms" in line

    def test_nothing_but_a_search_is_timed(self, server):
        client, log = server
        client.get("/api/v1/collections/handbook/points/a", headers=ADMIN)
        assert lines(log, "read") and all("took_ms" not in line for line in lines(log, "read"))

    def test_a_guest_is_sent_to_sign_in_and_no_search_is_timed(self, server):
        client, log = server
        guest = TestClient(client.app, base_url=PUBLIC)
        assert (
            guest.post(
                "/api/v1/collections/handbook/search", json={"query": [1, 0, 0, 0]}
            ).status_code
            == 401
        )
        assert not log.exists() or not [entry for entry in lines(log) if entry["who"] == "guest"], (
            "nothing was searched, so nothing was timed"
        )

    def test_a_search_that_cannot_be_recorded_is_not_served(self, server, monkeypatch):
        """The line is written after the search now, and the reply still does not leave without it."""
        client, _ = server

        def broken(self, event, **fields):
            raise AccessLogUnavailable("the disk is full")

        monkeypatch.setattr(AccessLog, "record", broken)
        reply = client.post(
            "/api/v1/collections/handbook/search", json={"query": [1, 0, 0, 0]}, headers=ADMIN
        )
        assert reply.status_code == 503 and "results" not in reply.text
        assert "access log cannot be written" in reply.json()["message"]


class TestTheDailyFigures:
    def test_the_median_and_the_slowest_one_in_twenty(self, tmp_path):
        log = AccessLog(tmp_path / "access.jsonl")
        took = list(range(1, 101))  # 1..100 ms
        log.path.write_text(
            "".join(
                json.dumps({"at": NOON, "event": "search", "took_ms": ms}) + "\n" for ms in took
            ),
            encoding="utf-8",
        )
        counted = log.daily(1, now=NOON)
        assert counted["search_ms_median"] == [50.0] and counted["search_ms_p95"] == [95.0]

    def test_a_day_with_nothing_timed_is_none_not_nought(self, tmp_path):
        log = AccessLog(tmp_path / "access.jsonl")
        rows = [
            {"at": NOON - DAY, "event": "search"},
            {"at": NOON, "event": "search", "took_ms": 40},
        ]
        log.path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        counted = log.daily(3, now=NOON)
        assert counted["searches"] == [0, 1, 1], (
            "a search written before searches were timed is still a search"
        )
        assert counted["search_ms_median"] == [None, None, 40.0] and counted["search_ms_p95"] == [
            None,
            None,
            40.0,
        ]

    def test_a_time_that_is_not_a_number_is_left_out(self, tmp_path):
        log = AccessLog(tmp_path / "access.jsonl")
        rows = [
            {"at": NOON, "event": "search", "took_ms": "fast"},
            {"at": NOON, "event": "search", "took_ms": 12.5},
        ]
        log.path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        assert log.daily(1, now=NOON)["search_ms_median"] == [12.5]

    @pytest.mark.parametrize(
        "values, share, wanted",
        [
            ([], 0.5, None),
            ([7], 0.95, 7.0),
            ([1, 2, 3, 4], 0.5, 2.0),
            ([1, 2, 3, 4], 0.95, 4.0),
            ([10, 20], 0.5, 10.0),
        ],
    )
    def test_nearest_rank(self, values, share, wanted):
        assert _at(values, share) == wanted

    def test_the_route_carries_them(self, server):
        client, _ = server
        client.post(
            "/api/v1/collections/handbook/search", json={"query": [1, 0, 0, 0]}, headers=ADMIN
        )
        data = client.get("/api/v1/access/daily?days=2", headers=ADMIN).json()["data"]
        assert data["search_ms_median"][0] is None and data["search_ms_median"][1] >= 0
        assert data["search_ms_p95"][1] >= data["search_ms_median"][1]
