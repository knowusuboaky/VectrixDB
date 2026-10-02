"""The rest of the dashboard's trends: Access, a collection's Overview and Builds, and Ingest.

Who reads most comes from the access log, names and counts as the log itself
holds. A collection's growth and its builds come from what every chunk already
carries: the time it was first written, the build that stored it and its
quality score, so neither needs a log or an audit trail. The Ingest page draws
what the documents route already replies with. And the Evaluate page's bands
gained one: first, top 3, top 5, top 10, missed.
"""

from __future__ import annotations

import json
import re
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

SECRET = "k" * 48
PUBLIC = "https://vectors.example.test"
KEY = "the-admin-api-key"
ADMIN = {"api-key": KEY}
DAY = 86400
NOON = (int(time.time()) // DAY) * DAY + DAY // 2
DASH = Path(__file__).resolve().parents[2] / "vectrixdb" / "dashboard"


def write(path: Path, lines) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines),
        encoding="utf-8",
    )


class TestWhoReadsMost:
    def test_the_busiest_first_with_searches_and_reads_apart(self, tmp_path):
        log = AccessLog(tmp_path / "access.jsonl")
        write(
            log.path,
            [
                *[{"at": NOON, "event": "search", "who": "ada@example.com", "method": "email"}] * 3,
                {"at": NOON, "event": "read", "who": "ada@example.com", "method": "email"},
                *[{"at": NOON - DAY, "event": "search", "who": "key:reports", "method": "key"}] * 5,
                {"at": NOON, "event": "search", "who": "guest", "method": "guest"},
            ],
        )
        assert log.readers(14, now=NOON) == [
            {"who": "key:reports", "key": True, "searches": 5, "reads": 0},
            {"who": "ada@example.com", "key": False, "searches": 3, "reads": 1},
            {"who": "guest", "key": False, "searches": 1, "reads": 0},
        ]

    def test_only_searches_and_reads_inside_the_window_count(self, tmp_path):
        log = AccessLog(tmp_path / "access.jsonl")
        write(
            log.path,
            [
                {"at": NOON - 20 * DAY, "event": "search", "who": "old@example.com"},
                {"at": NOON, "event": "signin", "who": "ada@example.com"},
                {"at": NOON, "event": "write", "who": "ada@example.com"},
                {"at": NOON, "event": "denied", "who": "lin@example.com"},
                {"at": NOON, "event": "search"},
                "not json",
            ],
        )
        assert log.readers(14, now=NOON) == []

    def test_the_top_few_and_a_tie_in_a_fixed_order(self, tmp_path):
        log = AccessLog(tmp_path / "access.jsonl")
        write(
            log.path, [{"at": NOON, "event": "search", "who": who} for who in ("c", "a", "b", "a")]
        )
        assert [r["who"] for r in log.readers(14, top=2, now=NOON)] == ["a", "b"]

    def test_a_log_on_the_servers_output_cannot_be_read_back(self):
        assert AccessLog("stdout").readers() is None


def _embed(texts):
    return np.array([[1, 0, 0, 0] for _ in texts], dtype=np.float32)


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "db"
    one = Vectrix("handbook", path=str(root), dimension=4, embed_fn=_embed, embedding_cache=False)
    one.add(
        ["alpha", "beta"], ids=["a", "b"], metadata=[{"_vx_quality": 0.9}, {"_vx_quality": 0.95}]
    )
    one.add(
        ["gamma", "delta", "epsilon"],
        ids=["c", "d", "e"],
        metadata=[{"_vx_quality": 0.4}, {"_vx_quality": 0.5}, {"_vx_quality": 0.9}],
    )
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
        store_path=data / "auth" / "signin.db",
        access_log=data / "auth" / "access.jsonl",
        sender=lambda to, subject, text: None,
    )
    with TestClient(
        create_app(db_path=str(data), enable_dashboard=False, signin=config), base_url=PUBLIC
    ) as client:
        yield client


class TestTheReadersRoute:
    def test_a_search_made_now_is_under_the_callers_name(self, server):
        server.post(
            "/api/v1/collections/handbook/search", json={"query": [1, 0, 0, 0]}, headers=ADMIN
        )
        reply = server.get("/api/v1/access/readers", headers=ADMIN).json()["data"]
        assert reply["available"] is True and reply["days"] == 14
        (row,) = reply["readers"]
        # A search that returns whole chunks is also a read of their text, and the log says both.
        assert (
            row["who"] == "api-key"
            and row["key"] is True
            and row["searches"] == 1
            and row["reads"] in (0, 1)
        )

    def test_it_is_for_whoever_may_read_the_access_log(self, server):
        from vectrixdb.signin import roles

        made = server.post(
            "/api/v1/keys", json={"name": "app", "role": "operator"}, headers=ADMIN
        ).json()["data"]["key"]
        assert server.get("/api/v1/access/readers", headers={"api-key": made}).status_code == 403
        assert server.get("/api/v1/access/readers").status_code == 401
        assert roles.action_for("GET", "/api/v1/access/readers") == "access.read"


class TestACollectionsGrowth:
    def test_chunks_are_counted_on_the_day_they_were_written_with_today_last(self, server):
        reply = server.get("/api/v1/collections/handbook/growth?days=7", headers=ADMIN).json()[
            "data"
        ]
        today = datetime.now(timezone.utc).date()
        assert reply["days"] == [(today - timedelta(days=6 - n)).isoformat() for n in range(7)]
        assert reply["written"] == [0, 0, 0, 0, 0, 0, 5] and reply["before"] == 0

    def test_what_was_written_before_the_window_is_counted_apart(self, data, server):
        import sqlite3

        path = next(
            p
            for p in data.rglob("*.db")
            if "auth" not in p.parts and "parents" not in p.name and _has_points(p)
        )
        with sqlite3.connect(path) as db:
            db.execute(
                "UPDATE points SET created_at = ? WHERE id IN ('a', 'b')",
                ((datetime.now(timezone.utc) - timedelta(days=40)).isoformat(),),
            )
        reply = server.get("/api/v1/collections/handbook/growth?days=30", headers=ADMIN).json()[
            "data"
        ]
        assert sum(reply["written"]) == 3 and reply["before"] == 2 and len(reply["days"]) == 30

    def test_the_role_table_places_it_beside_health_and_builds(self):
        from vectrixdb.signin import roles

        assert roles.action_for("GET", "/api/v1/collections/handbook/growth") == "meta.read"

    def test_the_growth_across_every_collection_sums_them_and_counts_what_reads_badly(
        self, server, monkeypatch
    ):
        from vectrixdb.api import inspection

        now = datetime.now(timezone.utc)
        rows = [
            (now.isoformat(), {"_vx_quality": 0.31}),
            (now.isoformat(), {"_vx_quality": 0.93}),
            (now.isoformat(), {}),
            ((now - timedelta(days=40)).isoformat(), {"_vx_quality": 0.2}),
        ]
        monkeypatch.setattr(inspection, "_each_written", lambda collection: iter(rows))
        reply = server.get("/api/v1/growth?days=7", headers=ADMIN).json()["data"]
        assert len(reply["days"]) == 7 and reply["days"][-1] == now.date().isoformat()
        assert reply["written"] == [0, 0, 0, 0, 0, 0, 3] and reply["low"] == [
            0,
            0,
            0,
            0,
            0,
            0,
            1,
        ], "one of the three written today is under the line"
        assert reply["before"] == 1 and reply["collections"] == 1 and reply["skipped"] == 0
        assert set(reply) == {
            "days",
            "written",
            "low",
            "before",
            "collections",
            "skipped",
            "quality_threshold",
        }, "counts alone, no names"

    def test_the_overview_draws_the_growth_beside_the_searches(self):
        import vectrixdb

        app = (Path(vectrixdb.__file__).parent / "dashboard" / "app.js").read_text(encoding="utf-8")
        assert "/api/v1/growth?days=14" in app and "Best setup" not in app, (
            "a guest's tiles say nothing a run over one collection cannot stand for"
        )

    def test_the_rank_card_leads_with_where_the_answer_is_on_average(self):
        import vectrixdb

        ev = (Path(vectrixdb.__file__).parent / "dashboard" / "evaluate.js").read_text(
            encoding="utf-8"
        )
        assert "function evPlaceLine(" in ev and "${evPlaceLine(s)}" in ev
        assert "Math.round(1 / mrr)" in ev and "MRR ${mrr.toFixed(2)}" in ev, (
            "the place is the rounded reciprocal, the MRR itself in the tooltip"
        )

    def test_a_collection_with_a_policy_says_nothing_that_moves_with_its_contents(
        self, tmp_path, monkeypatch
    ):
        from vectrixdb.api.server import create_app
        from vectrixdb.policy import Overlap, Policy

        root = tmp_path / "walled"
        db = Vectrix(
            "memos",
            path=str(root),
            dimension=4,
            embed_fn=_embed,
            embedding_cache=False,
            policy=Policy([Overlap("client_id", "clients", scope=True)]),
        )
        db.add(["alpha"], ids=["a"], metadata=[{"client_id": "acme"}])
        db.close()
        monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
        with TestClient(create_app(db_path=str(root), enable_dashboard=False)) as client:
            assert (
                client.get("/api/v1/collections/memos/growth", headers=ADMIN).status_code
                == client.get("/api/v1/collections/memos/builds", headers=ADMIN).status_code
                != 200
            )


def _has_points(path: Path) -> bool:
    import sqlite3

    try:
        with sqlite3.connect(path) as db:
            return bool(
                db.execute("SELECT name FROM sqlite_master WHERE name = 'points'").fetchone()
            )
    except sqlite3.DatabaseError:
        return False


class TestABuildSaysWhenItWroteAndHowItReads:
    def test_each_build_has_its_time_its_mean_quality_and_how_many_read_badly(self, server):
        reply = server.get("/api/v1/collections/handbook/builds", headers=ADMIN).json()["data"]
        assert reply["quality_threshold"] == 0.78
        by_size = {b["chunks"]: b for b in reply["builds"]}
        assert set(by_size) == {2, 3}
        assert by_size[2]["quality"] == 0.925 and by_size[2]["low"] == 0
        assert (
            by_size[3]["quality"] == 0.6
            and by_size[3]["low"] == 2
            and by_size[3]["current"] is True
        )
        assert by_size[2]["written_at"] <= by_size[3]["written_at"], "the first build wrote first"
        assert {"build_id", "chunks", "current"} <= set(by_size[2]), (
            "what the reply always had is still there"
        )

    def test_chunks_with_no_score_leave_the_quality_unsaid(self, tmp_path, monkeypatch):
        from vectrixdb.api.server import create_app

        root = tmp_path / "plain"
        db = Vectrix("notes", path=str(root), dimension=4, embed_fn=_embed, embedding_cache=False)
        db.add(["alpha"], ids=["a"])
        db.close()
        monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
        with TestClient(create_app(db_path=str(root), enable_dashboard=False)) as client:
            (build,) = client.get("/api/v1/collections/notes/builds", headers=ADMIN).json()["data"][
                "builds"
            ]
        assert build["quality"] is None and build["low"] == 0 and build["written_at"]


class TestTheDocumentsRouteSaysWhereTheLineIs:
    def test_the_reply_carries_the_threshold_the_page_draws(self, tmp_path, monkeypatch):
        from vectrixdb.api.server import create_app

        root = tmp_path / "notes"
        notes = Vectrix(
            "notes", path=str(root)
        )  # the bundled model, which is what the documents route embeds with
        notes.add(["a first note"], ids=["n0"])
        notes.close()
        monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
        monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
        with TestClient(create_app(db_path=str(root), enable_dashboard=False)) as client:
            reply = client.post(
                "/api/v1/collections/notes/documents",
                content=b"# Note\n\nPayment is due within thirty days of the invoice date.",
                headers={**ADMIN, "X-Filename": "note.md"},
            )
        assert reply.status_code == 200, reply.text
        body = reply.json()
        body = body.get("data", body)
        assert body["quality_threshold"] == 0.78 and isinstance(body["quality"], float)


class TestThePagesDrawThem:
    JS = (DASH / "app.js").read_text(encoding="utf-8")
    TR = (DASH / "trends.js").read_text(encoding="utf-8")
    CSS = (DASH / "app.css").read_text(encoding="utf-8")
    HTML = (DASH / "index.html").read_text(encoding="utf-8")

    def test_every_card_is_a_function_the_page_calls(self):
        for name in ("trAccess", "trGrowth", "trBuilds", "trBatch", "trByCollection"):
            assert f"function {name}(" in self.TR, name
        for call in (
            "trAccess(holder",
            "trGrowth(holder",
            "trBuilds($('c-builds-chart')",
            "trBatch(ingestRun",
        ):
            assert call in self.JS, call

    def test_a_row_is_left_out_and_not_drawn_empty(self):
        assert 'id="ac-trends" hidden' in self.JS and 'id="c-growth" hidden' in self.JS
        assert "if (!d.available) return;" in self.JS, "a log on the server's output draws nothing"
        assert "if (known.length < 2)" in self.TR, "one build is not a chart"
        assert "if (docs.length < 2) return '';" in self.TR, "nor is one document"

    def test_keys_are_asked_for_only_by_somebody_who_may_manage_them(self):
        body = self.JS[self.JS.index("async function loadAccessTrends") :]
        assert body.index("can('keys.manage')") < body.index("api('/api/v1/keys')")

    def test_an_idle_key_is_marked_and_the_rest_are_not(self):
        assert "TR_IDLE_DAYS = 30" in self.TR and 'class="pill warn" title="Not used for' in self.TR

    def test_the_growth_does_not_hold_up_the_tiles(self):
        assert re.search(
            r"api\(`/api/v1/collections/\$\{enc\}/growth\?days=30`, \{ quiet: true \}\)\.then\(",
            self.JS,
        ), "asked for after the page is up, not awaited before it"

    def test_the_layouts_and_classes_the_cards_use_exist(self):
        for rule in (
            ".lay-one {",
            ".tr-hb.wide {",
            ".tr-kr {",
            ".tr-qb {",
            ".tr-q b {",
            ".tr-batch {",
        ):
            assert rule in self.CSS, rule
        assert 'id="ing-quality"' in self.HTML and self.HTML.index(
            'id="ing-quality"'
        ) < self.HTML.index('id="ing-log"')
        assert "fill=" not in self.TR and not re.search(r"#[0-9a-fA-F]{3,6}\b", self.TR), (
            "colour is still a class"
        )

    def test_the_builds_table_has_a_column_for_how_each_reads(self):
        assert "<span>Build</span><span>Chunks</span><span>Quality</span><span></span>" in self.JS
        assert re.search(
            r"^\.t-builds \{ grid-template-columns: (minmax\(0, \d+fr\) ?){4}; \}", self.CSS, re.M
        )


class TestTheTopFiveBand:
    EV = (DASH / "evaluate.js").read_text(encoding="utf-8")
    CSS = (DASH / "app.css").read_text(encoding="utf-8")

    def test_a_summary_counts_the_top_five_apart(self):
        from vectrixdb._eval_report import BUCKETS, summarise

        assert BUCKETS == ((1, 1), (2, 3), (4, 5), (6, 10), (11, 20))
        s = summarise({"ranks": [1, 2, 4, 5, 6, 10, 11, None], "times_ms": [10] * 8})
        assert s["landed"] == [1, 1, 2, 2, 1, 1] and sum(s["landed"]) == 8
        assert s["counts"]["5"] == 4 and s["counts"]["10"] == 6

    def test_the_bar_and_the_legend_have_five_bands(self):
        assert (
            "First</span>" in self.EV
            and "Top 3</span>" in self.EV
            and "Top 5</span>" in self.EV
            and "Top 10</span>" in self.EV
            and "Missed</span>" in self.EV
        )
        stack = self.EV[self.EV.index("function evStack") : self.EV.index("function evKeys")]
        assert re.findall(r'class="(b-[a-z0-9]+)"', stack) == [
            "b-first",
            "b-top3",
            "b-top5",
            "b-top10",
        ]
        assert "(c['5'] - c['3'])" in stack and "(c['10'] - c['5'])" in stack, (
            "the top 10 band is what the top 5 leaves"
        )

    def test_a_run_saved_before_is_still_drawn(self):
        assert (
            "const old = landed.length === 5;" in self.EV
            and "'4th to 10th'" in self.EV
            and "'4th to 5th', '6th to 10th'" in self.EV
        )

    def test_the_colour_is_in_both_themes(self):
        assert (
            self.CSS.count("--ev-top5:") == 2
            and ".b-top5 { background: var(--ev-top5); }" in self.CSS
        )


class TestThreeThingsThePagesGotWrong:
    """A value label cut off, a dense collection called hybrid, and the server's own path on a page."""

    def test_the_tallest_bars_value_has_room_above_the_plot(self):
        script = (DASH / "trends.js").read_text(encoding="utf-8")
        body = script[script.index("function trBars(") : script.index("function trLines(")]
        top = int(re.search(r"\bT = (\d+)\b", body).group(1))
        above = int(re.search(r"y\(series\[0\]\.values\[last\]\) - (\d+)", body).group(1))
        size = float(
            re.search(
                r"\.tr-val \{ font-size: ([\d.]+)px", (DASH / "app.css").read_text(encoding="utf-8")
            ).group(1)
        )
        # The bar that reaches the top of the scale starts at T; its label's baseline is `above` higher,
        # and the digits rise about a font size from the baseline. All of that must be inside the picture.
        assert top - above - size >= 0, "the label above a bar at the top of the scale was clipped"

    def test_a_collection_tagged_dense_is_dense_whatever_index_it_carries(self):
        for app in (
            DASH / "app.js",
            DASH.parents[1]
            / "examples"
            / "azure"
            / "dashboard"
            / "Frontend"
            / "public"
            / "pages"
            / "app.js",
        ):
            script = app.read_text(encoding="utf-8")
            body = script[script.index("function modeOf(") : script.index("function hasPolicy(")]
            assert body.index("tags.includes('dense')") < body.index("c.has_text_index"), app
            assert "tags.includes('hybrid') || c.has_text_index" not in body, (
                "the v1 API tags a collection dense and gives it a text index"
            )

    def test_the_evaluate_page_names_the_runs_folder_and_not_the_machines_path(self):
        script = (DASH / "evaluate.js").read_text(encoding="utf-8")
        assert "esc(EV.where)" not in script and "esc(evWhereLabel(EV.where))" in script
        chunking = (DASH / "chunking.js").read_text(encoding="utf-8")
        assert "esc(CK.where)" not in chunking and "esc(evWhereLabel(CK.where))" in chunking
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            pytest.skip("node is not installed")
        fn = script[
            script.index("function evWhereIsLocal(") : script.index(
                "/* ------------------------------------------------------------ pieces */"
            )
        ]
        cases = {
            "/srv/vectrix/db/evaluations": "evaluations",
            "C:\\\\data\\\\db\\\\evaluations\\\\chunking": "evaluations/chunking",
            "/home/me/runs": "runs",
            "s3://bucket/prefix/evaluations": "s3://bucket/prefix/evaluations",
        }
        out = subprocess.run(
            [
                node,
                "-e",
                fn
                + "\nconsole.log(JSON.stringify(Object.fromEntries(JSON.parse(process.argv[1]).map((w) => [w, evWhereLabel(w)]))))",
                json.dumps([k.replace("\\\\", "\\") for k in cases]),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert json.loads(out) == {k.replace("\\\\", "\\"): v for k, v in cases.items()}
