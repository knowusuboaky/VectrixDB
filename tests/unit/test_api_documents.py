"""Documents over REST: a file goes in as the request body, comes out as cited
chunks, and the Markdown it was indexed from is served back when the server
keeps it. The extraction server in these tests is a fake on the loopback
address.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import pytest

from vectrixdb.policy import Overlap, Policy

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

CONTRACT = (
    "# Terms\n\nPayment is due within thirty days of the invoice date. "
    "Invoices are issued monthly and sent to the billing contact on file.\n\n"
    "## Late fees\n\nInterest accrues monthly on any overdue balance. "
    "A reminder is sent after ten days and a second one after twenty.\n"
)


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    from vectrixdb import Vectrix
    from vectrixdb.api import server

    for key in (
        "VECTRIXDB_KEEP_SOURCE",
        "VECTRIXDB_EXTRACTOR_URL",
        "VECTRIXDB_EXTRACTOR_ROUTES",
        "VECTRIXDB_EXTRACTOR_BODY",
    ):
        monkeypatch.delenv(key, raising=False)
    Vectrix("docs", path=str(tmp_path)).close()
    walled = Vectrix("walled", path=str(tmp_path), policy=Policy([Overlap("client_id", "clients")]))
    walled.close()
    opened = []

    def build(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        client = TestClient(server.create_app(db_path=str(tmp_path), enable_dashboard=False))
        client.__enter__()
        opened.append(client)
        return client

    yield build
    for client in opened:
        client.__exit__(None, None, None)


def send(client, name, data, collection="docs", **params):
    return client.post(
        f"/api/v1/collections/{collection}/documents",
        content=data,
        headers={"X-Filename": quote(name)},
        params=params,
    )


class TestAddDocument:
    def test_a_file_in_the_body_becomes_cited_chunks(self, make_client):
        client = make_client()
        reply = send(
            client, "msa.md", CONTRACT.encode(), metadata=json.dumps({"client_id": "acme"})
        )
        assert reply.status_code == 200, reply.text
        body = reply.json()
        assert body["doc_id"] == "msa.md" and body["chunks"] == 2 and body["replaced"] == 0
        assert body["citations"] == ["msa.md#Terms", "msa.md#Late fees"] and body[
            "build"
        ].startswith("build_")
        assert body["extractor"] == "built-in" and body["kept"] is False

        hits = client.post(
            "/api/v1/collections/docs/text-search",
            json={"query_text": "overdue balance interest", "limit": 1},
        ).json()
        top = (hits.get("data") or hits)["results"][0]
        assert (
            top["metadata"]["_vx_citation"] == "msa.md#Late fees"
            and top["metadata"]["client_id"] == "acme"
        )
        assert top["metadata"]["_vx_build"] == body["build"]

    def test_the_same_id_again_replaces_the_document(self, make_client):
        client = make_client()
        send(client, "msa.md", CONTRACT.encode())
        again = send(
            client,
            "msa.md",
            b"# Terms\n\nPayment is now due within sixty days of the invoice date.\n",
        ).json()
        assert again["replaced"] == 2 and again["chunks"] == 1
        assert client.get("/api/v1/collections/docs").json()["data"]["count"] == 1

    def test_a_spreadsheet_and_a_front_matter_id(self, make_client):
        client = make_client()
        csv = send(
            client, "sales.csv", b"Region,Revenue\nEMEA,1200\nAPAC,950\n", chunk="sentence"
        ).json()
        assert csv["chunks"] >= 1 and csv["citations"][0] == "sales.csv"
        fronted = send(
            client,
            "page.md",
            b"---\ndoc_id: deferment\naudience: [wealth]\n---\n\n" + CONTRACT.encode(),
        ).json()
        assert fronted["doc_id"] == "deferment"

    def test_what_is_refused(self, make_client):
        client = make_client()
        assert send(client, "msa.md", b"").status_code == 400
        assert client.post("/api/v1/collections/docs/documents", content=b"text").status_code == 400
        assert send(client, "msa.md", b"text", metadata="[1]").status_code == 400
        assert send(client, "msa.md", b"text", metadata="{not json").status_code == 400
        assert send(client, "msa.md", b"text", chunk="semantic").status_code == 400
        assert send(client, "msa.md", b"text", collection="nope").status_code == 404
        assert send(client, "  .md", b"   \n").status_code == 422
        noisy = send(client, "scan.txt", ("zq xv kj " * 60).encode(), on_low_quality="reject")
        assert noisy.status_code == 422 and "extraction" in noisy.json()["detail"]

    def test_a_policied_collection_is_refused_like_every_data_route(self, make_client):
        client = make_client(VECTRIXDB_KEEP_SOURCE="1")
        assert send(client, "msa.md", CONTRACT.encode(), collection="walled").status_code == 403
        assert client.get("/api/v1/collections/walled/documents").status_code == 403
        assert client.get("/api/v1/collections/walled/documents/msa.md").status_code == 403

    def test_a_path_in_the_name_is_only_its_last_part(self, make_client):
        client = make_client()
        assert (
            send(client, "../../etc/passwd.md", CONTRACT.encode()).json()["doc_id"] == "passwd.md"
        )

    def test_a_file_that_says_it_is_too_big_is_refused_unread(self, make_client, monkeypatch):
        from vectrixdb.api import documents

        monkeypatch.setattr(documents, "MAX_UPLOAD_BYTES", 64)
        client = make_client()
        assert send(client, "big.md", b"x" * 200).status_code == 413


class TestKeptDocuments:
    def test_the_markdown_is_kept_listed_served_and_moved_aside(self, make_client, tmp_path):
        client = make_client(VECTRIXDB_KEEP_SOURCE="1")
        assert (
            send(client, "acme/msa.md", CONTRACT.encode(), doc_id="acme/msa.md").json()["kept"]
            is True
        )
        assert (tmp_path / "docs.documents" / "acme" / "msa.md.md").is_file()

        listing = client.get("/api/v1/collections/docs/documents").json()["documents"]
        assert [d["doc_id"] for d in listing] == ["acme/msa.md"] and listing[0]["chunking"][
            "chunk"
        ] == "markdown"

        served = client.get("/api/v1/collections/docs/documents/acme/msa.md")
        assert served.status_code == 200 and served.headers["content-type"].startswith(
            "text/markdown"
        )
        assert served.text.startswith("# Terms") and "vectrixdb" not in served.text
        assert client.get(
            "/api/v1/collections/docs/documents/acme/msa.md", params={"raw": True}
        ).text.startswith("---\nvectrixdb")
        assert client.get("/api/v1/collections/docs/documents/missing.md").status_code == 404

        gone = client.delete("/api/v1/collections/docs/documents/acme/msa.md").json()
        assert gone == {
            "ok": True,
            "doc_id": "acme/msa.md",
            "chunks_removed": 2,
            "kept_copy_moved_aside": True,
        }
        assert list((tmp_path / "docs.documents" / "_deleted").rglob("msa.md.md"))
        assert client.delete("/api/v1/collections/docs/documents/acme/msa.md").status_code == 404

    def test_without_the_setting_the_routes_say_how_to_turn_it_on(self, make_client):
        client = make_client()
        reply = client.get("/api/v1/collections/docs/documents")
        assert reply.status_code == 404 and "VECTRIXDB_KEEP_SOURCE=1" in reply.json()["detail"]


class _Extraction(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.seen.append((self.path, self.headers.get("X-Filename"), body))
        if self.path == "/extract/pdf":
            reply = {"text": CONTRACT, "pages": [[0, 1], [CONTRACT.index("## Late"), 2]]}
            payload, status = json.dumps(reply).encode(), 200
        else:
            payload, status = b"speech model not loaded", 503
        self.send_response(status)
        self.send_header("Content-Type", "application/json" if status == 200 else "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def extraction_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Extraction)
    httpd.seen = []
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


class TestDelegation:
    def test_a_pdf_goes_to_the_operators_service_and_comes_back_cited_by_page(
        self, make_client, extraction_server
    ):
        httpd, url = extraction_server
        client = make_client(VECTRIXDB_EXTRACTOR_URL=url)
        body = send(client, "scan.pdf", b"%PDF scanned").json()
        assert httpd.seen == [("/extract/pdf", "scan.pdf", b"%PDF scanned")]
        assert body["citations"] == ["scan.pdf#page=1", "scan.pdf#page=2"] and body["pages"] == 2
        assert body["extractor"] == url + "/extract/pdf"

    def test_a_service_that_fails_is_a_bad_gateway_and_nothing_is_written(
        self, make_client, extraction_server
    ):
        _, url = extraction_server
        client = make_client(VECTRIXDB_EXTRACTOR_URL=url)
        reply = send(client, "call.wav", b"RIFF")
        assert reply.status_code == 502 and "answered 503" in reply.json()["detail"]
        assert client.get("/api/v1/collections/docs").json()["data"]["count"] == 0

    def test_a_markdown_file_stays_local(self, make_client, extraction_server):
        httpd, url = extraction_server
        client = make_client(VECTRIXDB_EXTRACTOR_URL=url)
        assert send(client, "msa.md", CONTRACT.encode()).json()["extractor"] == "built-in"
        assert httpd.seen == []

    def test_the_server_says_what_it_reads(self, make_client, extraction_server):
        _, url = extraction_server
        routes = json.dumps({".pdf": "/extract/pdf", ".xyz": "/extract/xyz"})
        info = (
            make_client(
                VECTRIXDB_EXTRACTOR_URL=url,
                VECTRIXDB_EXTRACTOR_ROUTES=routes,
                VECTRIXDB_KEEP_SOURCE="1",
            )
            .get("/api/v1/extractors")
            .json()
        )
        assert (
            info["extractors"] == [".pdf", ".xyz"]
            and ".xyz" in info["accepted"]
            and ".md" in info["accepted"]
        )
        assert info["extractor_url"] is True and info["keeps_source"] is True
