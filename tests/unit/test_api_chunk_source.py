"""The collection pages on an instance that wrote nothing: every number comes from the chunk store the writer shares.

Two instances, as a deployment that scales out has them. The writer ingests
into its own path; the reader serves the pages from another path, where the
same collection holds nothing. With a store shared between them the reader's
pages show what the writer wrote, and without one they show nothing, which is
what an instance that did not do the writing used to show.

Also here: a policy applied to chunks read from a store the same way it is
to chunks read beside the process, and the kept Markdown served from a folder,
S3 or Blob that another process writes, one folder a collection.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_chunk_store import MemoryChunks  # noqa: E402

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb.api import chunk_source  # noqa: E402
from vectrixdb.exceptions import PrincipalRequired  # noqa: E402
from vectrixdb.policy import Overlap, Policy  # noqa: E402
from vectrixdb.quality import DEFAULT_THRESHOLD  # noqa: E402

NOTE = "# Notes\n\nThe covenant test is due at the end of each quarter, and the ratio is reported to the lender.\n"


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    """A writer that ingested, a reader that did not, and what the reader serves: ``open(store)``."""
    from vectrixdb import Vectrix
    from vectrixdb.api import server

    for name in ("VECTRIXDB_KEEP_SOURCE", "VECTRIXDB_CHUNK_STORE", "VECTRIXDB_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    store = MemoryChunks()
    writer = Vectrix("plain", path=str(tmp_path / "writer"), chunk_store=store)
    writer.add(
        ["clean prose about a covenant test", "c0venant t3st resu1ts sha11 be de1ivered", "the report's first page"],
        ids=["good", "noisy", "report:0"],
        metadata=[{"_vx_quality": 0.93}, {"_vx_quality": 0.31}, {"_vx_doc": "report.md"}],
    )
    writer.close()
    # The same collection on the reader's path, with nothing written beside it.
    Vectrix("plain", path=str(tmp_path / "reader")).close()
    opened = []

    def open_reader(shared=store, **env):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        client = TestClient(server.create_app(db_path=str(tmp_path / "reader"), enable_dashboard=False, chunk_store=shared))
        client.__enter__()
        opened.append(client)
        return client

    open_reader.store = store
    yield open_reader
    for client in opened:
        client.__exit__(None, None, None)


class TestThePagesReadTheStore:
    def test_health_and_the_list_count_what_the_writer_wrote(self, deployment):
        reader = deployment()
        health = reader.get("/api/v1/collections/plain/health").json()["data"]
        assert health["count"] == 3 and health["updated_at"]
        (listed,) = [c for c in reader.get("/api/v1/collections").json()["collections"] if c["name"] == "plain"]
        assert listed["count"] == 3

    def test_one_collection_and_the_whole_server_count_the_same_chunks_the_list_does(self, deployment):
        """The Overview's tile and a collection read by name said 0 on a deployment, beside a list that said 3."""
        reader = deployment()
        one = reader.get("/api/v1/collections/plain").json()["data"]
        assert one["count"] == 3 and one["updated_at"]
        assert reader.get("/api/v1/info").json()["total_vectors"] == 3

    def test_the_size_of_this_disk_and_when_this_process_started_are_not_the_collections(self, deployment):
        """On a deployment the pages said 40 KB on disk and made 37 minutes ago, of chunks in a shared store made a day before."""
        reader = deployment()
        one = reader.get("/api/v1/collections/plain").json()["data"]
        assert one["shared_store"] is True and one["size_bytes"] is None
        assert one["created_at"] is None, "nothing here knows when it was made, so nothing is said"
        (listed,) = [c for c in reader.get("/api/v1/collections").json()["collections"] if c["name"] == "plain"]
        assert listed["shared_store"] is True and listed["size_bytes"] is None and listed["created_at"] is None
        info = reader.get("/api/v1/info").json()
        assert info["shared_store"] is True and info["total_size_bytes"] is None

    def test_when_it_was_made_is_the_collection_records(self, deployment):
        import types

        reader = deployment()
        asked = []

        def get(name):
            asked.append(name)
            return types.SimpleNamespace(created_at="2026-09-27T21:14:00+00:00")

        reader.app.state.collection_store = types.SimpleNamespace(get=get)
        assert reader.get("/api/v1/collections/plain").json()["data"]["created_at"] == "2026-09-27T21:14:00+00:00"
        assert asked == ["plain"]

        def broken(name):
            raise OSError("the store is away")

        reader.app.state.collection_store = types.SimpleNamespace(get=broken)
        assert reader.get("/api/v1/collections/plain").json()["data"]["created_at"] is None, "a record that cannot be read leaves the time out"

    def test_beside_the_process_the_size_and_the_time_are_as_they_were(self, deployment):
        reader = deployment(shared=None)
        one = reader.get("/api/v1/collections/plain").json()["data"]
        assert "shared_store" not in one and one["created_at"] and one["size_bytes"] is not None
        assert reader.get("/api/v1/info").json()["shared_store"] is False

    def test_without_a_store_the_reader_sees_nothing(self, deployment):
        reader = deployment(shared=None)
        assert reader.get("/api/v1/collections/plain/health").json()["data"]["count"] == 0
        assert reader.get("/api/v1/collections/plain/builds").json()["data"]["builds"] == []

    def test_builds_and_growth_are_the_writers(self, deployment):
        reader = deployment()
        (build,) = reader.get("/api/v1/collections/plain/builds").json()["data"]["builds"]
        assert build["chunks"] == 3 and build["written_at"]
        assert build["quality"] == round((0.93 + 0.31) / 2, 4) and build["low"] == 1
        growth = reader.get("/api/v1/collections/plain/growth", params={"days": 1}).json()["data"]
        assert growth["written"] == [3] and growth["before"] == 0

    def test_quality_names_the_worst_with_its_text_read_from_the_store(self, deployment):
        data = deployment().get("/api/v1/collections/plain/quality").json()["data"]
        assert (data["scored"], data["unscored"], data["below"], sum(data["bins"])) == (2, 1, 1, 2)
        assert data["threshold"] == DEFAULT_THRESHOLD
        worst = data["worst"][0]
        assert worst["id"] == "noisy" and "c0venant" in worst["text"] and worst["below_line"] is True

    def test_a_chunk_the_reader_never_held_has_its_provenance(self, deployment):
        reader = deployment()
        data = reader.get("/api/v1/collections/plain/provenance/noisy").json()["data"]
        assert data["present"] is True and data["quality"] == 0.31 and data["build_id"]
        assert reader.get("/api/v1/collections/plain/provenance/missing").json()["data"]["present"] is False

    def test_the_points_are_paged_from_the_store(self, deployment):
        reader = deployment()
        data = reader.get("/api/v1/collections/plain/points", params={"limit": 2, "index": True}).json()["data"]
        assert data["total"] == 3 and len(data["ids"]) == 2
        rest = reader.get("/api/v1/collections/plain/points", params={"limit": 2, "offset": 2}).json()["data"]["ids"]
        assert sorted(data["ids"] + rest) == ["good", "noisy", "report:0"]
        assert {row["id"]: row["quality"] for row in data["rows"]} == {i: {"good": 0.93, "noisy": 0.31, "report:0": None}[i] for i in data["ids"]}


class TestADocumentsChunks:
    def test_deleting_a_document_takes_the_chunks_the_writer_wrote(self, deployment):
        reader = deployment()
        gone = reader.delete("/api/v1/collections/plain/documents/report.md").json()
        assert gone["chunks_removed"] == 1
        assert set(deployment.store.of("plain")) == {"good", "noisy"}

    def test_sending_it_again_replaces_them(self, deployment):
        reader = deployment()
        sent = reader.post(
            "/api/v1/collections/plain/documents",
            content=NOTE.encode(),
            headers={"X-Filename": quote("report.md")},
            params={"doc_id": "report.md"},
        ).json()
        assert sent["replaced"] == 1
        kept = deployment.store.of("plain")
        assert "report:0" not in kept and len([r for r in kept.values() if r["metadata"].get("_vx_doc") == "report.md"]) == sent["chunks"]


class TestAPolicyDecidesChunkByChunk:
    @pytest.fixture
    def walled(self):
        store = MemoryChunks()
        rows = store.collection("walled")
        rows.put(["mine", "theirs"], ["ours", "not ours"], [{"client_id": "td"}, {"client_id": "rbc"}], "2026-09-21T10:00:00+00:00")
        return types.SimpleNamespace(name="walled", policy=Policy([Overlap("client_id", "clients")]), _chunk_store=rows)

    def test_a_principal_sees_what_it_is_entitled_to_and_a_denial_is_a_miss(self, walled):
        td = {"clients": ["td"]}
        assert chunk_source.point(walled, "mine", td).text == "ours"
        assert chunk_source.point(walled, "theirs", td) is None
        assert chunk_source.page(walled, 10, 0, td) == (["mine"], 1)
        assert chunk_source.page(walled, 10, 1, td) == ([], 1)

    def test_nobody_in_particular_is_refused(self, walled):
        with pytest.raises(PrincipalRequired):
            chunk_source.point(walled, "mine")
        with pytest.raises(PrincipalRequired):
            chunk_source.page(walled, 10, 0)


class TestKeptMarkdownFromAnotherProcess:
    def test_a_folder_holds_one_folder_a_collection(self, deployment, tmp_path):
        kept = tmp_path / "kept"
        reader = deployment(VECTRIXDB_KEEP_SOURCE=str(kept))
        assert reader.get("/api/v1/extractors").json()["keeps_source"] is True
        sent = reader.post("/api/v1/collections/plain/documents", content=NOTE.encode(), headers={"X-Filename": "notes.md"}).json()
        assert sent["kept"] is True and (kept / "plain" / "notes.md.md").is_file()
        assert [d["doc_id"] for d in reader.get("/api/v1/collections/plain/documents").json()["documents"]] == ["notes.md"]

    def test_s3_and_blob_addresses_name_a_prefix_a_collection(self, monkeypatch):
        from vectrixdb import evaluation
        from vectrixdb.api import documents
        from vectrixdb.documents import BlobFiles, S3Files

        monkeypatch.setattr(documents, "_files", {})
        monkeypatch.setattr(evaluation, "_s3_client", lambda: "s3 client")
        monkeypatch.setattr(evaluation, "_blob_client", lambda account: f"blob client for {account}")
        s3 = documents._files_at("s3://bucket/ingestion/markdown", "financial")
        assert isinstance(s3, S3Files) and (s3.client, s3.bucket, s3.prefix) == ("s3 client", "bucket", "ingestion/markdown/financial/")
        blob = documents._files_at("https://acct.blob.core.windows.net/ingestion/markdown", "financial")
        assert isinstance(blob, BlobFiles)
        assert (blob.client, blob.container, blob.prefix) == ("blob client for https://acct.blob.core.windows.net", "ingestion", "markdown/financial/")
        # A client is made once a process, not once a request.
        assert documents._files_at("https://acct.blob.core.windows.net/ingestion/markdown", "financial") is blob
        whole = documents._files_at("https://acct.blob.core.windows.net/markdown", "misc")
        assert (whole.container, whole.prefix) == ("markdown", "misc/")

    def test_a_blob_address_with_no_container_is_refused(self, monkeypatch):
        from fastapi import HTTPException

        from vectrixdb import evaluation
        from vectrixdb.api import documents

        monkeypatch.setattr(documents, "_files", {})
        monkeypatch.setattr(evaluation, "_blob_client", lambda account: "client")
        with pytest.raises(HTTPException):
            documents._files_at("https://acct.blob.core.windows.net/", "financial")

    @pytest.mark.parametrize("off", ["", "0", "false", "no", "off"])
    def test_off_is_off(self, off, monkeypatch):
        from vectrixdb.api import documents

        monkeypatch.setenv("VECTRIXDB_KEEP_SOURCE", off)
        assert documents.keeps_source() is False and documents.store_for("plain") is None
