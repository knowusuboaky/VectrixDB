"""Counts a day, for the Overview's and the Audit page's charts.

The Overview draws searches, sign-ins and refusals a day from the access log;
Audit draws decisions a day by outcome, and by collection, from the audit
trail. What is held to: a day is a UTC day, the window ends today, a line that
cannot be read is skipped and not fatal, every record in the window is counted
and not only the newest few that are listed, the access counts carry no names,
and they are for whoever may read the access log and nobody else.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("jwt", reason="the signin extra is not installed")
pytest.importorskip("cryptography", reason="the signin extra is not installed")

from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.signin import SignInConfig  # noqa: E402
from vectrixdb.signin.access import AccessLog  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))

SECRET = "k" * 48
PUBLIC = "https://vectors.example.test"
KEY = "the-admin-api-key"
ADMIN = {"api-key": KEY}
DAY = 86400
NOON = (int(time.time()) // DAY) * DAY + DAY // 2  # today, UTC, well away from midnight


def write(path: Path, lines) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines), encoding="utf-8")


class TestTheAccessLogCountedByDay:
    def test_each_kind_lands_on_its_own_day_with_today_last(self, tmp_path):
        log = AccessLog(tmp_path / "access.jsonl")
        write(log.path, [
            {"at": NOON, "event": "search"}, {"at": NOON, "event": "search"}, {"at": NOON, "event": "signin"},
            {"at": NOON - DAY, "event": "search"}, {"at": NOON - DAY, "event": "signin_failed"}, {"at": NOON - DAY, "event": "denied"},
            {"at": NOON - DAY, "event": "denied", "reason": "not_on_policy"}, {"at": NOON, "event": "denied", "reason": "no_policy", "collection": "walled"},
            {"at": NOON - 3 * DAY, "event": "signin"},
        ])
        counted = log.daily(4, now=NOON)
        assert counted["searches"] == [0, 0, 1, 2] and counted["signins"] == [1, 0, 0, 1] and counted["refused"] == [0, 0, 2, 0]
        assert counted["denied"] == [0, 0, 1, 1], "a policy's no carries its reason; a role's no is a refusal"
        assert counted["days"][-1] == time.strftime("%Y-%m-%d", time.gmtime(NOON)) and len(counted["days"]) == 4

    def test_a_day_is_a_utc_day(self, tmp_path):
        log = AccessLog(tmp_path / "access.jsonl")
        midnight = (NOON // DAY) * DAY
        write(log.path, [{"at": midnight - 1, "event": "search"}, {"at": midnight, "event": "search"}])
        assert log.daily(2, now=NOON)["searches"] == [1, 1]

    def test_what_is_older_than_the_window_or_not_counted_is_left_out(self, tmp_path):
        log = AccessLog(tmp_path / "access.jsonl")
        write(log.path, [{"at": NOON - 14 * DAY, "event": "search"}, {"at": NOON + 2 * DAY, "event": "search"}, {"at": NOON, "event": "signout"}, {"at": NOON, "event": "key_created"}])
        counted = log.daily(14, now=NOON)
        assert sum(counted["searches"]) == 0 and sum(counted["signins"]) == 0 and sum(counted["refused"]) == 0 and sum(counted["denied"]) == 0

    def test_a_line_that_cannot_be_read_is_skipped_not_fatal(self, tmp_path):
        log = AccessLog(tmp_path / "access.jsonl")
        write(log.path, ["not json", {"event": "search"}, {"at": "yesterday", "event": "search"}, {"at": None, "event": "search"}, {"at": NOON, "event": "search"}])
        assert log.daily(1, now=NOON)["searches"] == [1]

    def test_no_log_yet_is_a_run_of_noughts(self, tmp_path):
        counted = AccessLog(tmp_path / "none.jsonl").daily(3, now=NOON)
        assert counted["searches"] == [0, 0, 0] and len(counted["days"]) == 3

    def test_a_log_sent_to_the_servers_output_cannot_be_counted(self):
        assert AccessLog("stdout").daily(14) is None

    @pytest.mark.parametrize("asked, got", [(0, 1), (-3, 1), (14, 14), (500, 90)])
    def test_the_window_is_between_one_day_and_ninety(self, tmp_path, asked, got):
        assert len(AccessLog(tmp_path / "a.jsonl").daily(asked, now=NOON)["days"]) == got


# ------------------------------------------------------------- over the wire


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
        methods=("email",), secrets=(SECRET,), public_url=PUBLIC, users=(("ada@example.com", "admin"),),
        store_path=data / "auth" / "signin.db", access_log=data / "auth" / "access.jsonl", sender=lambda to, subject, text: None,
    )
    with TestClient(create_app(db_path=str(data), enable_dashboard=False, signin=config), base_url=PUBLIC) as client:
        yield client


class TestTheAccessCountsRoute:
    def test_a_search_made_now_is_in_todays_count(self, server):
        before = server.get("/api/v1/access/daily", headers=ADMIN).json()["data"]
        assert before["available"] is True and len(before["days"]) == 14
        server.post("/api/v1/collections/handbook/search", json={"query": [1, 0, 0, 0]}, headers=ADMIN)
        after = server.get("/api/v1/access/daily?days=7", headers=ADMIN).json()["data"]
        assert len(after["days"]) == 7 and after["searches"][-1] == before["searches"][-1] + 1

    def test_the_reply_is_counts_and_nothing_about_anybody(self, server):
        server.post("/api/v1/collections/handbook/search", json={"query": [1, 0, 0, 0]}, headers=ADMIN)
        reply = server.get("/api/v1/access/daily", headers=ADMIN)
        assert set(reply.json()["data"]) == {"available", "days", "searches", "signins", "refused", "denied", "search_ms_median", "search_ms_p95"}
        assert "handbook" not in reply.text and "api-key" not in reply.text

    def test_searches_are_everyones_and_the_sign_in_counts_are_for_whoever_may_read_the_log(self, server):
        made = server.post("/api/v1/keys", json={"name": "app", "role": "operator"}, headers=ADMIN).json()["data"]["key"]
        reply = server.get("/api/v1/access/daily", headers={"api-key": made})
        assert reply.status_code == 200, reply.text
        assert set(reply.json()["data"]) == {"available", "days", "searches", "search_ms_median", "search_ms_p95"}, "nothing about people"
        assert server.get("/api/v1/access/daily").status_code == 401, "and nobody at all is sent to sign in"

    def test_the_role_table_places_it(self):
        from vectrixdb.signin import roles

        assert roles.action_for("GET", "/api/v1/access/daily") == "meta.read"
        assert roles.action_for("GET", "/api/v1/growth") == "meta.read"

    def test_a_policys_refusal_is_logged_with_its_reason_which_is_what_makes_it_a_denial(self):
        import inspect

        from vectrixdb.api.signin import AccessMiddleware

        gate = inspect.getsource(AccessMiddleware._gate)
        assert '"denied"' in gate and "reason=decision.code" in gate, "the day counts a denied line with a reason as the policy's no"

    def test_a_log_on_the_servers_output_says_so(self, data, monkeypatch):
        from vectrixdb.api.server import create_app

        monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
        config = SignInConfig(
            methods=("email",), secrets=(SECRET,), public_url=PUBLIC, users=(("ada@example.com", "admin"),),
            store_path=data / "auth" / "signin.db", access_log="stdout", sender=lambda to, subject, text: None,
        )
        with TestClient(create_app(db_path=str(data), enable_dashboard=False, signin=config), base_url=PUBLIC) as client:
            said = client.get("/api/v1/access/daily", headers=ADMIN).json()["data"]
        assert said["available"] is False and "output" in said["reason"]


# ------------------------------------------------------------ the audit trail


def at(days_ago: int) -> str:
    return (datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def audited(data, monkeypatch, tmp_path):
    from vectrixdb.api.server import create_app

    trail = tmp_path / "audit.jsonl"
    rows = [
        {"decision_id": "d1", "collection": "memos", "outcome": "allowed", "decided_at": at(0)},
        {"decision_id": "d2", "collection": "memos", "outcome": "allowed", "decided_at": at(0)},
        {"decision_id": "d3", "collection": "memos", "outcome": "denied_in_scope", "decided_at": at(0)},
        {"decision_id": "d4", "collection": "notes", "outcome": "denied_out_of_scope", "decided_at": at(1)},
        {"decision_id": "d5", "collection": "notes", "outcome": "refused_no_principal", "decided_at": at(1)},
        {"decision_id": "d6", "collection": "notes", "outcome": "undecidable_document", "decided_at": at(2)},
        {"decision_id": "d7", "collection": "memos", "outcome": "allowed", "decided_at": at(40)},
        {"ingestion_id": "i1", "collection": "memos", "documents_written": 9, "recorded_at": at(0)},
        {"decision_id": "d8", "collection": "memos", "outcome": "allowed", "decided_at": "not a time"},
        {"decision_id": "d9", "collection": "memos", "outcome": "allowed"},
    ]
    write(trail, rows)
    monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
    monkeypatch.setenv("VECTRIXDB_AUDIT_JSONL", str(trail))
    monkeypatch.setenv("VECTRIXDB_ALLOW_OPEN", "1")
    with TestClient(create_app(db_path=str(data), enable_dashboard=False)) as client:
        yield client


class TestTheAuditTrailCountedByDay:
    def test_decisions_by_outcome_with_today_last(self, audited):
        got = audited.get("/api/v1/audit?days=3", headers=ADMIN).json()["data"]["daily"]
        assert len(got["days"]) == 3
        assert got["allowed"] == [0, 0, 2] and got["denied"] == [0, 1, 1]
        assert got["refused"] == [1, 1, 0], "undecidable counts with refused: nobody was answered either way"

    def test_an_ingestion_is_not_a_decision_and_a_record_with_no_time_is_left_out(self, audited):
        got = audited.get("/api/v1/audit", headers=ADMIN).json()["data"]
        total = sum(got["daily"]["allowed"]) + sum(got["daily"]["denied"]) + sum(got["daily"]["refused"])
        assert total == 6, "d7 is older than fourteen days, i1 is an ingestion, d8 and d9 have no time"
        assert got["counts"]["decisions"] == 9, "the tiles still count the whole trail"

    def test_by_collection_the_busiest_first(self, audited):
        rows = audited.get("/api/v1/audit", headers=ADMIN).json()["data"]["by_collection"]
        assert rows == [
            {"collection": "memos", "allowed": 2, "denied": 1, "refused": 0},
            {"collection": "notes", "allowed": 0, "denied": 1, "refused": 2},
        ]

    def test_every_record_in_the_window_is_counted_not_only_the_ones_listed(self, audited):
        got = audited.get("/api/v1/audit?limit=1", headers=ADMIN).json()["data"]
        assert len(got["records"]) == 1 and sum(got["daily"]["allowed"]) == 2

    def test_a_longer_window_reaches_further_back(self, audited):
        got = audited.get("/api/v1/audit?days=60", headers=ADMIN).json()["data"]["daily"]
        assert len(got["days"]) == 60 and sum(got["allowed"]) == 3

    def test_one_collection_asked_for_is_one_collection_counted(self, audited):
        got = audited.get("/api/v1/audit?collection=notes", headers=ADMIN).json()["data"]
        assert [r["collection"] for r in got["by_collection"]] == ["notes"] and sum(got["daily"]["allowed"]) == 0

    @pytest.mark.parametrize("days", [0, 91])
    def test_a_window_that_is_no_window_is_refused(self, audited, days):
        assert audited.get(f"/api/v1/audit?days={days}", headers=ADMIN).status_code == 422


# ---------------------------------------------------------------- the page

DASH = Path(__file__).resolve().parents[2] / "vectrixdb" / "dashboard"


class TestThePageDrawsThem:
    def test_the_script_is_loaded_after_the_app_it_borrows_from(self):
        page = (DASH / "index.html").read_text(encoding="utf-8")
        assert page.index('<script src="app.js">') < page.index('<script src="trends.js">')

    def test_the_overview_row_sits_between_the_tiles_and_the_rest(self):
        page = (DASH / "index.html").read_text(encoding="utf-8")
        assert page.index('id="ov-metrics"') < page.index('id="ov-trends"') < page.index('id="ov-body"')
        assert re.search(r'id="ov-trends" hidden', page), "nothing is drawn until there is something to draw from"

    def test_colour_is_a_class_so_both_themes_get_it(self):
        script = (DASH / "trends.js").read_text(encoding="utf-8")
        assert "fill=" not in script and "#" not in re.sub(r"/\*.*?\*/", "", script, flags=re.S).replace("'#", "")
        css = (DASH / "app.css").read_text(encoding="utf-8")
        for cls in ("tr-acc", "tr-mute", "tr-ok", "tr-warn", "tr-bad", "tr-tick"):
            assert f".{cls} " in css, cls
        assert css.count("--tr-mute:") == 2 and css.count("--tr-warn:") == 2, "one for each theme"

    def test_a_tablet_gets_one_chart_a_row(self):
        css = (DASH / "app.css").read_text(encoding="utf-8")
        assert ".lay-trends { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }" in css
        assert "  .lay-trends, .lay-trends:has(> :nth-child(3):last-child) { grid-template-columns: minmax(0, 1fr); }" in css

    def test_three_cards_are_three_across_so_none_sits_alone(self):
        css = (DASH / "app.css").read_text(encoding="utf-8")
        assert ".lay-trends:has(> :nth-child(3):last-child) { grid-template-columns: repeat(3, minmax(0, 1fr)); }" in css

    def test_a_day_with_nothing_timed_breaks_the_line_and_is_not_drawn_as_nought(self):
        script = (DASH / "trends.js").read_text(encoding="utf-8")
        body = script[script.index("function trLines("):script.index("const trKeys")]
        assert "v === null" in body and "flush()" in body

    def test_the_search_time_card_is_left_out_until_a_search_has_been_timed(self):
        script = (DASH / "trends.js").read_text(encoding="utf-8")
        assert "medians.length ? [{ title: 'Search time'" in script and "cards.push(...time);" in script

    def test_the_overview_asks_for_everyone_and_the_server_keeps_the_sign_in_counts_back(self):
        app = (DASH / "app.js").read_text(encoding="utf-8")
        body = app[app.index("async function loadTrends()"):app.index("async function loadOverview()")]
        assert "can('access.read')" not in body, "a guest's overview draws searches too; the route leaves the sign-in counts out for them"
        assert "/api/v1/access/daily" in body and "/api/v1/growth" in body

    def test_the_sign_ins_chart_is_on_the_access_page_and_the_overview_draws_what_was_written(self):
        tr = (DASH / "trends.js").read_text(encoding="utf-8")
        overview = tr[tr.index("function trOverview("):tr.index("/* --------------------------------------------------------------- audit */")]
        assert "Sign-ins and refusals" not in overview and "Written a day" in overview and "Refused by a policy" in overview
        access = tr[tr.index("function trSigninsCard("):tr.index("function trGrowth(")]
        assert "Sign-ins and refusals" in access and "trSigninsCard(daily)" in access
