"""The Evaluate pages read runs the library saved, and nothing else.

The routes list runs and hand back one; they never search. Where runs live
is the data path's evaluations folder unless VECTRIXDB_EVALUATIONS says
otherwise, and who may read them is the sign-in table's evaluation.read:
anybody signed in who may see a collection's health, and no guest.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb._eval_report import ReportStore, build_report  # noqa: E402
from vectrixdb.api.server import create_app  # noqa: E402
from vectrixdb.signin import roles  # noqa: E402

GOLDEN = {"source": "golden.jsonl", "sha256": "cd" * 32, "questions": 2, "labelled": 2, "unfilled": 0, "drafts": 0}


def one(key, ranks, ms):
    return {
        "key": key, "target": "VectrixDB", "engine": "VectrixDB", "engine_short": "VectrixDB", "engine_kind": "vectrixdb",
        "method": "dense", "method_label": "Dense", "ranker": "the vectors only", "models": [], "search": {"mode": "dense"},
        "ranks": ranks, "times_ms": [ms] * len(ranks), "error": None,
    }


@pytest.fixture(autouse=True)
def plain_env(monkeypatch):
    for name in ("VECTRIXDB_API_KEY", "VECTRIXDB_READ_ONLY_API_KEY", "VECTRIXDB_PATH", "VECTRIXDB_EVALUATIONS", "VECTRIXDB_SIGNIN", "VECTRIXDB_STORAGE_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")


def serve(path):
    return TestClient(create_app(db_path=str(path), enable_dashboard=False))


class TestTheRoutes:
    def test_no_runs_yet_is_an_empty_list_and_a_404(self, tmp_path):
        with serve(tmp_path / "db") as client:
            listed = client.get("/api/v1/evaluations")
            assert listed.status_code == 200 and listed.json()["data"]["runs"] == []
            assert listed.json()["data"]["where"].replace("\\", "/").endswith("db/evaluations")
            latest = client.get("/api/v1/evaluations/latest")
            assert latest.status_code == 404 and latest.json()["detail"] == "No evaluation runs yet"

    def test_runs_in_the_data_path_are_listed_newest_first_and_read_whole(self, tmp_path):
        store = ReportStore(tmp_path / "db" / "evaluations")
        old = store.save(build_report([one("a", [1, None], 5.0)], GOLDEN, created=0))
        new = store.save(build_report([one("a", [1, 2], 4.0)], GOLDEN, created=60))
        with serve(tmp_path / "db") as client:
            runs = client.get("/api/v1/evaluations").json()["data"]["runs"]
            assert all(isinstance(r.get("collections"), list) for r in runs), "every listed run says which collections it searched"
            assert [r["id"] for r in runs] == [new, old] and runs[0]["top10"] == {"a": 2}
            report = client.get("/api/v1/evaluations/latest").json()["data"]
            assert report["id"] == new and report["picks"]["finds_the_most"] == "a" and report["setups"][0]["ranks"] == [1, 2]
            assert client.get(f"/api/v1/evaluations/{old}").json()["data"]["id"] == old
            assert client.get("/api/v1/evaluations/19990101-000000").status_code == 404
            assert client.get("/api/v1/evaluations?limit=1").json()["data"]["runs"][0]["id"] == new

    def test_a_run_is_kept_under_retrieval_and_one_saved_before_is_still_read(self, tmp_path):
        import json

        where = tmp_path / "db" / "evaluations"
        new = ReportStore(where).save(build_report([one("a", [1, 2], 4.0)], GOLDEN, created=60))
        assert (where / "retrieval" / "runs" / new / "report.json").is_file()
        before = build_report([one("a", [1, None], 5.0)], GOLDEN, created=0)
        (where / "runs" / before["id"]).mkdir(parents=True)
        (where / "runs" / before["id"] / "report.json").write_text(json.dumps(before), encoding="utf-8")
        with serve(tmp_path / "db") as client:
            assert [r["id"] for r in client.get("/api/v1/evaluations").json()["data"]["runs"]] == [new, before["id"]]
            assert client.get(f"/api/v1/evaluations/{before['id']}").json()["data"]["id"] == before["id"], "a run from before 2.2 is not lost"

    def test_the_setting_points_the_server_at_another_store(self, tmp_path, monkeypatch):
        elsewhere = tmp_path / "shared" / "evals"
        run = ReportStore(elsewhere).save(build_report([one("a", [1], 3.0)], GOLDEN, created=0))
        monkeypatch.setenv("VECTRIXDB_EVALUATIONS", str(elsewhere))
        with serve(tmp_path / "db") as client:
            assert client.get("/api/v1/evaluations/latest").json()["data"]["id"] == run

    def test_a_store_whose_library_is_missing_says_what_to_install(self, tmp_path, monkeypatch):
        import vectrixdb.evaluation as evaluation

        def missing():
            raise ImportError("An s3:// address needs boto3: pip install 'vectrixdb[aws]'")

        monkeypatch.setattr(evaluation, "_s3_client", missing)
        monkeypatch.setenv("VECTRIXDB_EVALUATIONS", "s3://evals/handbook")
        with serve(tmp_path / "db") as client:
            answer = client.get("/api/v1/evaluations")
            assert answer.status_code == 503 and "boto3" in answer.json()["detail"]

    def test_replies_are_never_cached_and_carry_no_page_policy(self, tmp_path):
        with serve(tmp_path / "db") as client:
            answer = client.get("/api/v1/evaluations")
            assert answer.headers["cache-control"] == "no-store"
            assert answer.headers["content-security-policy"].startswith("default-src 'none'")


def chunking_run(created, right):
    from vectrixdb._eval_chunking import chunking_report

    outcomes = {f"q{i}": [True, i < right] for i in range(2)}
    build = {
        "key": "markdown-1000-h", "technique": "markdown", "chunk": "markdown", "size": 1000, "overlap": 200, "headings": True, "parent_size": None,
        "questions": 2, "left_out": 0, "collections": [], "chunks": 4, "build_s": 0.1, "model": None, "found": 2, "answered": right, "outcomes": outcomes, "error": None,
    }
    return chunking_report([build], GOLDEN, created=created)


class TestTheChunkingRoutes:
    def test_no_runs_yet_is_an_empty_list_and_a_404_that_says_chunking(self, tmp_path):
        with serve(tmp_path / "db") as client:
            assert client.get("/api/v1/chunking").json()["data"]["runs"] == []
            latest = client.get("/api/v1/chunking/latest")
            assert latest.status_code == 404 and latest.json()["detail"] == "No chunking runs yet"

    def test_chunking_runs_are_read_beside_the_evaluation_runs_and_apart_from_them(self, tmp_path):
        from vectrixdb.evaluation import ChunkingStore

        where = tmp_path / "db" / "evaluations"
        ReportStore(where).save(build_report([one("a", [1, 2], 4.0)], GOLDEN, created=0))
        old = ChunkingStore(where).save(chunking_run(0, 1))
        new = ChunkingStore(where).save(chunking_run(60, 2))
        assert (where / "chunking" / "runs" / new / "report.json").is_file()
        with serve(tmp_path / "db") as client:
            runs = client.get("/api/v1/chunking").json()["data"]["runs"]
            assert [r["id"] for r in runs] == [new, old] and runs[0]["techniques"]["markdown"]["right"] == 2
            report = client.get("/api/v1/chunking/latest").json()["data"]
            assert report["kind"] == "chunking" and report["best"] == "markdown" and report["golden_download"] is False
            assert client.get(f"/api/v1/chunking/{old}").json()["data"]["techniques"][0]["right"] == 1
            assert "setups" in client.get("/api/v1/evaluations/latest").json()["data"], "the evaluation runs are where they were"
            assert client.get("/api/v1/chunking/19990101-000000").status_code == 404

    def test_the_golden_file_is_for_an_admin_as_a_person(self, tmp_path):
        from vectrixdb.evaluation import ChunkingStore

        run = ChunkingStore(tmp_path / "db" / "evaluations").save(chunking_run(0, 2))
        with serve(tmp_path / "db") as client:
            assert client.get(f"/api/v1/chunking/{run}/golden").status_code == 403


class TestWhoMayRead:
    def test_the_routes_are_evaluation_read(self):
        assert roles.action_for("GET", "/api/v1/evaluations") == "evaluation.read"
        assert roles.action_for("GET", "/api/v1/evaluations/latest") == "evaluation.read"
        assert roles.action_for("POST", "/api/v1/evaluations") is None, "nothing writes a run through the server"
        assert roles.action_for("GET", "/api/v1/chunking") == "evaluation.read"
        assert roles.action_for("GET", "/api/v1/chunking/latest") == "evaluation.read"
        assert roles.action_for("GET", "/api/v1/chunking/latest/golden") == "evaluation.golden"
        assert roles.action_for("POST", "/api/v1/chunking") is None, "nothing writes a chunking run through the server either"

    def test_every_person_and_key_and_a_guest_may_read(self):
        for role in ("viewer", "operator", "admin", "reader", "searcher"):
            assert roles.can(role, "evaluation.read"), role
        assert roles.can("guest", "evaluation.read"), "a guest reads how the setups scored: it says nothing about what is stored"
        assert "evaluation.read" in roles.ACTIONS
