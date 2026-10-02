"""The inspection routes the dashboard reads: health, policy, builds, quality,
provenance, models and the audit trail.

The rule under test is the same one the rest of the API keeps: a policied
collection answers with configuration and never with a number that moves
when documents are written, and the audit trail is never served from an
open server.
"""

from __future__ import annotations

import json

import pytest

from vectrixdb.policy import AtMost, Overlap, Policy

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

LENDING = Policy([Overlap("client_id", "clients"), AtMost("classification", "clearance")])


@pytest.fixture
def client(tmp_path):
    from vectrixdb import Vectrix
    from vectrixdb.api import server

    walled = Vectrix("walled", path=str(tmp_path), policy=LENDING)
    walled.add(
        ["a covenant for the other borrower"],
        metadata=[{"client_id": "zeta", "classification": 3}],
    )
    walled.close()

    plain = Vectrix("plain", path=str(tmp_path))
    plain.add(
        ["clean prose about a covenant test", "c0venant t3st resu1ts sha11 be de1ivered"],
        ids=["good", "noisy"],
        metadata=[{"_vx_quality": 0.93}, {"_vx_quality": 0.31}],
    )
    plain.close()

    with TestClient(server.create_app(db_path=str(tmp_path), enable_dashboard=False)) as c:
        yield c


class TestHealth:
    def test_a_plain_collection_reports_its_state(self, client):
        data = client.get("/api/v1/collections/plain/health").json()["data"]
        assert data["state"] == "healthy"
        assert data["count"] == 2
        assert data["tombstone_ratio"] == 0.0
        assert data["policied"] is False
        assert data["index_build_id"]

    def test_a_policied_collection_answers_with_configuration_only(self, client):
        data = client.get("/api/v1/collections/walled/health").json()["data"]
        assert data["policied"] is True
        assert data["policy_fingerprint"] == LENDING.fingerprint
        assert "count" not in data and "tombstone_ratio" not in data

    def test_unknown_is_404(self, client):
        assert client.get("/api/v1/collections/nope/health").status_code == 404


class TestPolicy:
    def test_the_rules_come_back_as_data(self, client):
        data = client.get("/api/v1/collections/walled/policy").json()["data"]
        assert data["fingerprint"] == LENDING.fingerprint
        kinds = [r["kind"] for r in data["policy"]["rules"]]
        assert kinds == ["Overlap", "AtMost"]

    def test_a_plain_collection_has_none(self, client):
        assert client.get("/api/v1/collections/plain/policy").json()["data"]["policy"] is None

    def test_it_needs_the_key_when_one_is_set(self, client, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_API_KEY", "k")
        assert client.get("/api/v1/collections/walled/policy").status_code == 403
        ok = client.get("/api/v1/collections/walled/policy", headers={"api-key": "k"})
        assert ok.status_code == 200


class TestBuildsQualityProvenance:
    def test_builds_are_read_off_the_chunks(self, client):
        data = client.get("/api/v1/collections/plain/builds").json()["data"]
        assert data["current"]
        (build,) = data["builds"]
        assert {k: build[k] for k in ("build_id", "chunks", "current")} == {"build_id": data["current"], "chunks": 2, "current": True}
        # Since 2.2 a build also says when it first wrote and how its chunks read.
        assert build["written_at"] and isinstance(build["low"], int) and "quality" in build

    def test_quality_counts_below_the_line_and_names_the_worst(self, client):
        data = client.get("/api/v1/collections/plain/quality").json()["data"]
        assert data["scored"] == 2 and data["below"] == 1
        assert sum(data["bins"]) == 2
        assert data["worst"][0]["id"] == "noisy"
        assert "c0venant" in data["worst"][0]["text"]

    def test_provenance_of_one_chunk(self, client):
        data = client.get("/api/v1/collections/plain/provenance/good").json()["data"]
        assert data["present"] is True and data["build_id"]
        assert data["quality"] == 0.93
        gone = client.get("/api/v1/collections/plain/provenance/missing").json()["data"]
        assert gone["present"] is False

    def test_the_lowest_chunks_page_with_an_offset(self, client):
        first = client.get("/api/v1/collections/plain/quality?worst=1&text=false").json()["data"]
        second = client.get("/api/v1/collections/plain/quality?worst=1&offset=1&text=false").json()["data"]
        assert first["offset"] == 0 and second["offset"] == 1
        assert first["worst"][0]["quality"] == 0.31 and second["worst"][0]["quality"] == 0.93, "the page after the lowest is the next lowest"
        assert first["below"] == second["below"], "the count under the line is the whole collection's, whatever the page"

    @pytest.mark.parametrize("route", ["builds", "quality", "provenance/x"])
    def test_a_policied_collection_is_refused(self, client, route):
        assert client.get(f"/api/v1/collections/walled/{route}").status_code == 403


class TestModelNames:
    def test_a_recorded_key_is_shown_by_the_model_name(self, tmp_path):
        """Collections an older server wrote record the embedder's key; the
        dashboard showed it as written, bge_small_en beside collections that
        read vectrixdb/bge-small-en-v1.5, for the same model."""
        from vectrixdb import Vectrix
        from vectrixdb.api.inspection import _services

        db = Vectrix("k", path=str(tmp_path))
        db._collection.set_meta("embedding_model", "bge_small_en")
        try:
            shown = _services(db._db, db._collection, "k", with_extractors=False)["embedded_by"]
        finally:
            db.close()
        assert shown == [Vectrix._default_model]


class TestModels:
    def test_every_registered_model_is_listed_with_presence(self, client):
        data = client.get("/api/v1/models").json()["data"]
        types = {m["type"] for m in data["models"]}
        assert {"dense", "dense_en", "bge_small_en", "sparse", "reranker"} <= types
        sparse = next(m for m in data["models"] if m["type"] == "sparse")
        assert sparse["present"] is True
        assert data["auto_download"] is False

    def test_the_english_model_is_not_listed_as_the_multilingual_one(
        self, client, tmp_path, monkeypatch
    ):
        """A fresh install holds the English dense model and reranker, and the
        multilingual ones are downloads. The list said they were here, found
        in the wheel."""
        models = tmp_path / "fresh-install"
        for folder in ("bge_small_en", "reranker_en"):
            (models / folder).mkdir(parents=True)
            (models / folder / "model.onnx").write_bytes(b"stub")
        monkeypatch.setenv("VECTRIXDB_MODELS_DIR", str(models))
        listed = {m["type"]: m for m in client.get("/api/v1/models").json()["data"]["models"]}
        assert listed["bge_small_en"]["present"] is True
        assert listed["reranker_en"]["present"] is True
        assert listed["dense"]["present"] is False and listed["dense"]["location"] is None
        assert listed["reranker"]["present"] is False

    def test_a_download_into_the_package_folder_is_not_called_the_wheel(
        self, client, tmp_path, monkeypatch
    ):
        from vectrixdb.models import embedded

        models = tmp_path / "data"  # the package folder, beside the module below
        for folder in ("bge_small_en", "dense"):
            (models / folder).mkdir(parents=True)
            (models / folder / "model.onnx").write_bytes(b"stub")
        monkeypatch.setenv("VECTRIXDB_MODELS_DIR", str(models))
        monkeypatch.setattr(embedded, "__file__", str(tmp_path / "embedded.py"))
        listed = {m["type"]: m for m in client.get("/api/v1/models").json()["data"]["models"]}
        assert listed["bge_small_en"]["location"] == "wheel"
        assert listed["dense"]["location"] == "downloaded"


class TestAudit:
    def _records(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        rows = [
            {
                "decision_id": "dec_1",
                "collection": "walled",
                "outcome": "allowed",
                "principal_id": "analyst_a",
                "principal_snapshot": {"clients": ["acme"]},
                "result_ids": ["good"],
                "withheld_disclosable": 1,
                "withheld_undisclosable": 4,
                "query_fingerprint": "3b0e",
            },
            {"decision_id": "dec_2", "collection": "walled", "outcome": "undecidable"},
            {"ingestion_id": "ing_1", "collection": "walled", "documents_written": 3},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return path

    def test_never_from_an_open_server(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_AUDIT_JSONL", str(self._records(tmp_path)))
        response = client.get("/api/v1/audit")
        assert response.status_code == 403
        assert "without an API key" in response.json()["detail"]

    def test_needs_the_key_and_the_path(self, client, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_API_KEY", "k")
        monkeypatch.delenv("VECTRIXDB_AUDIT_JSONL", raising=False)
        assert client.get("/api/v1/audit").status_code == 403
        data = client.get("/api/v1/audit", headers={"api-key": "k"}).json()["data"]
        assert data["available"] is False and "VECTRIXDB_AUDIT_JSONL" in data["reason"]

    def test_records_come_back_newest_first_without_the_sensitive_fields(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_API_KEY", "k")
        monkeypatch.setenv("VECTRIXDB_AUDIT_JSONL", str(self._records(tmp_path)))
        data = client.get("/api/v1/audit", headers={"api-key": "k"}).json()["data"]
        assert data["available"] is True
        assert [r.get("decision_id") or r.get("ingestion_id") for r in data["records"]] == ["ing_1", "dec_2", "dec_1"]
        first = data["records"][-1]
        assert first["withheld_disclosable"] == 1 and first["query_fingerprint"] == "3b0e"
        for banned in ("principal_snapshot", "result_ids", "withheld_undisclosable"):
            assert banned not in first
        assert data["counts"] == {"decisions": 2, "ingestions": 1, "denied": 0, "undecidable": 1, "refused": 0}
