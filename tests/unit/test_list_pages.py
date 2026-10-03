"""Every list the dashboard pages comes from the server a page at a time, with a find and a total.

The access log, who reads most and the people are the sign-in server's; the
audit records, the document index and a collection's points are the API's.
Each takes ``q`` (a find), a page size and an ``offset``, and answers with
``total``: how many match, so the page can say "1 to 10 of 9,412". What is
held to: the find is case-blind and looks in what a row says, not only its
name; a page past the end is empty and the total unchanged; a route asked
the old way, with no page, answers as it always did.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")

from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.signin.access import AccessLog  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))

NOON = (int(time.time()) // 86400) * 86400 + 43200


def write(path: Path, lines) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


class TestTheAccessLogListing:
    def test_a_page_the_newest_first_and_how_many_match(self, tmp_path):
        log = AccessLog(tmp_path / "access.jsonl")
        write(
            log.path,
            [
                {
                    "at": NOON - n,
                    "event": "search",
                    "who": f"person{n}@example.com",
                    "collection": "financial" if n % 2 else "media",
                }
                for n in range(25)
            ],
        )
        page, total = log.listing(10, 0)
        assert total == 25 and [r["who"] for r in page] == [
            f"person{n}@example.com" for n in range(24, 14, -1)
        ], "the last line written is first"
        page, total = log.listing(10, 20)
        assert total == 25 and len(page) == 5, "the last page is what is left"
        assert log.listing(10, 90) == ([], 25), "past the end is empty, the total stands"

    def test_the_find_looks_in_what_a_line_says(self, tmp_path):
        log = AccessLog(tmp_path / "access.jsonl")
        write(
            log.path,
            [
                {
                    "at": NOON,
                    "event": "search",
                    "who": "ada@example.com",
                    "collection": "financial",
                },
                {"at": NOON, "event": "denied", "who": "olu@example.com", "collection": "media"},
            ],
        )
        assert log.listing(10, 0, q="FINANCIAL")[1] == 1, "case aside"
        assert log.listing(10, 0, q="denied")[0][0]["who"] == "olu@example.com", (
            "the event is found too"
        )
        assert log.listing(10, 0, q="nobody") == ([], 0)
        assert log.listing(10, 0, q="example", event="denied")[1] == 1, (
            "an exact event narrows the find"
        )

    def test_a_log_on_the_servers_output_lists_nothing(self):
        assert AccessLog(None).listing(10, 0) == ([], 0)


@pytest.fixture
def api(tmp_path):
    from vectrixdb.api.server import create_app

    app = create_app(db_path=str(tmp_path / "db"))
    with TestClient(app) as client:
        yield client


class TestTheDocumentIndex:
    def test_a_page_with_a_find_and_the_old_shape_without(self, api):
        for n in range(12):
            r = api.post(
                "/api/v1/documents",
                json={
                    "text": f"# Doc {n:02d}"
                    + chr(10) * 2
                    + f"Document number {n} about {'grants' if n % 3 else 'payroll'}. " * 4,
                    "title": f"Doc {n:02d}",
                    "doc_type": "markdown",
                },
            )
            assert r.status_code == 200, r.text
        everything = api.get("/api/v1/documents").json()
        assert everything["total"] == 12 and len(everything["documents"]) == 12, (
            "asked the old way, every document"
        )
        page = api.get("/api/v1/documents", params={"limit": 10, "offset": 10}).json()
        assert page["total"] == 12 and len(page["documents"]) == 2
        found = api.get("/api/v1/documents", params={"q": "doc 1", "limit": 10}).json()
        assert found["total"] == 2 and {d["title"] for d in found["documents"]} == {
            "Doc 10",
            "Doc 11",
        }, "the find is on the title, case aside"


class TestACollectionsPoints:
    def test_a_find_by_id_and_the_total_that_match(self, api):
        api.post("/api/v2/collections", json={"name": "plain", "dimension": 4, "metric": "cosine"})
        points = [
            {
                "id": f"{'note' if n % 2 else 'memo'}-{n:02d}",
                "vector": [1.0, 0.0, 0.0, float(n) / 100],
            }
            for n in range(14)
        ]
        r = api.post("/api/v1/collections/plain/points", json={"points": points})
        assert r.status_code == 200, r.text
        listed = api.get(
            "/api/v1/collections/plain/points", params={"limit": 10, "offset": 0}
        ).json()["data"]
        assert listed["total"] == 14 and len(listed["ids"]) == 10, (
            "a page without a find, as before"
        )
        found = api.get(
            "/api/v1/collections/plain/points",
            params={"q": "NOTE", "limit": 10, "offset": 0, "index": "true"},
        ).json()["data"]
        assert (
            found["total"] == 7
            and len(found["ids"]) == 7
            and all(i.startswith("note-") for i in found["ids"])
        )
        assert len(found["rows"]) == 7, "the index rows come with a find too"
        assert (
            api.get(
                "/api/v1/collections/plain/points", params={"q": "note", "limit": 5, "offset": 5}
            ).json()["data"]["ids"]
            == found["ids"][5:]
        )


def at(days_ago: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (
        datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        - timedelta(days=days_ago)
    ).isoformat()


class TestTheAuditRecords:
    def test_a_page_with_a_find_while_the_counts_stay_whole(self, tmp_path, monkeypatch):
        from vectrixdb.api.server import create_app

        trail = tmp_path / "audit.jsonl"
        write(
            trail,
            [
                {
                    "decision_id": f"d{n:02d}",
                    "collection": "financial" if n % 3 else "media",
                    "principal_id": f"p{n}",
                    "outcome": "allowed" if n % 5 else "denied_in_scope",
                    "decided_at": at(0),
                }
                for n in range(15)
            ],
        )
        monkeypatch.setenv("VECTRIXDB_API_KEY", "the-admin-api-key")
        monkeypatch.setenv("VECTRIXDB_AUDIT_JSONL", str(trail))
        monkeypatch.setenv("VECTRIXDB_ALLOW_OPEN", "1")
        with TestClient(create_app(db_path=str(tmp_path / "db"), enable_dashboard=False)) as client:
            key = {"api-key": "the-admin-api-key"}
            whole = client.get("/api/v1/audit", params={"limit": 10}, headers=key).json()["data"]
            assert whole["available"], whole
            assert (
                whole["total"] == 15
                and len(whole["records"]) == 10
                and whole["counts"]["decisions"] == 15
            )
            second = client.get(
                "/api/v1/audit", params={"limit": 10, "offset": 10}, headers=key
            ).json()["data"]
            assert len(second["records"]) == 5 and second["total"] == 15
            found = client.get(
                "/api/v1/audit", params={"q": "media", "limit": 10}, headers=key
            ).json()["data"]
            assert found["total"] == 5 and all(r["collection"] == "media" for r in found["records"])
            assert found["counts"]["decisions"] == 15, (
                "the counts are of every record, whatever the find"
            )
            denied = client.get(
                "/api/v1/audit", params={"q": "DENIED", "limit": 10}, headers=key
            ).json()["data"]
            assert denied["total"] == 3, "the outcome is found too, case aside"


@pytest.fixture
def signed(tmp_path, monkeypatch):
    """A sign-in server with an admin key at the door, an access log on disk, and thirteen people on its list."""
    from vectrixdb.api.server import create_app
    from vectrixdb.signin import SignInConfig

    log = tmp_path / "auth" / "access.jsonl"
    write(
        log,
        [
            {
                "at": NOON - n,
                "event": "search" if n % 4 else "read",
                "who": f"reader{n % 3}@example.com",
                "role": "viewer",
                "collection": "financial" if n % 2 else "media",
            }
            for n in range(30)
        ],
    )
    monkeypatch.setenv("VECTRIXDB_API_KEY", "the-admin-api-key")
    people = (("ada@example.com", "admin"),) + tuple(
        (f"person{n:02d}@example.com", "operator" if n % 4 else "viewer") for n in range(12)
    )
    config = SignInConfig(
        methods=("email",),
        secrets=("k" * 48,),
        public_url="https://vectors.example.test",
        users=people,
        store_path=tmp_path / "auth" / "signin.db",
        access_log=log,
        sender=lambda to, subject, text: None,
    )
    with TestClient(
        create_app(db_path=str(tmp_path / "db"), enable_dashboard=False, signin=config),
        base_url="https://vectors.example.test",
    ) as client:
        yield client


class TestThePeopleAndTheLogAtTheDoor:
    KEY = {"api-key": "the-admin-api-key"}

    def test_the_access_log_pages_and_finds(self, signed):
        page = signed.get(
            "/api/v1/access", params={"limit": 10, "offset": 0}, headers=self.KEY
        ).json()["data"]
        assert page["total"] >= 30 and len(page["records"]) == 10 and page["offset"] == 0, (
            "the log holds what was written, and what the server itself recorded since"
        )
        found = signed.get(
            "/api/v1/access", params={"limit": 10, "q": "reader1"}, headers=self.KEY
        ).json()["data"]
        assert found["total"] == 10 and all(
            r["who"] == "reader1@example.com" for r in found["records"]
        )
        old = signed.get("/api/v1/access", headers=self.KEY).json()["data"]
        assert len(old["records"]) == old["total"] >= 30, (
            "asked the old way, the newest two hundred"
        )

    def test_who_reads_most_pages_and_finds(self, signed):
        d = signed.get(
            "/api/v1/access/readers", params={"days": 14, "top": 2, "q": "reader"}, headers=self.KEY
        ).json()["data"]
        assert d["available"] and d["total"] == 3 and len(d["readers"]) == 2, (
            "three readers were written; a page of two"
        )
        rest = signed.get(
            "/api/v1/access/readers",
            params={"days": 14, "top": 2, "offset": 2, "q": "reader"},
            headers=self.KEY,
        ).json()["data"]
        assert len(rest["readers"]) == 1 and rest["total"] == 3
        one = signed.get(
            "/api/v1/access/readers", params={"days": 14, "q": "READER2"}, headers=self.KEY
        ).json()["data"]
        assert one["total"] == 1 and one["readers"][0]["who"] == "reader2@example.com"

    def test_people_page_and_find_and_answer_whole_when_not_asked_for_a_page(self, signed):
        everybody = signed.get("/auth/people", headers=self.KEY).json()["data"]
        assert everybody["total"] == len(everybody["people"]) == 13, "asked the old way, everybody"
        page = signed.get(
            "/auth/people", params={"limit": 10, "offset": 0}, headers=self.KEY
        ).json()["data"]
        assert len(page["people"]) == 10 and page["total"] == 13
        assert (
            len(
                signed.get(
                    "/auth/people", params={"limit": 10, "offset": 10}, headers=self.KEY
                ).json()["data"]["people"]
            )
            == 3
        )
        viewers = signed.get(
            "/auth/people", params={"q": "viewer", "limit": 10}, headers=self.KEY
        ).json()["data"]
        assert viewers["total"] == 3 and all(p["role"] == "viewer" for p in viewers["people"]), (
            "the role is found, not only the address"
        )
        assert (
            signed.get("/auth/people", params={"q": "PERSON01"}, headers=self.KEY).json()["data"][
                "total"
            ]
            == 1
        )
