"""Extraction is a slot: a callable or an endpoint reads a file type, and the
built-in readers read the rest.

The endpoint in these tests is a real HTTP server on the loopback address,
started for the test and stopped after it, so the bytes on the wire are the
bytes a service would see. Nothing leaves the machine.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from vectrixdb import extract
from vectrixdb.exceptions import ExtractionError
from vectrixdb.extract import ExtractorRegistry, HttpExtractor, coerce, resolve
from vectrixdb.ingest import LoadedDocument, load, load_bytes

PAGE_ONE = "# Terms\n\nPayment is due within thirty days of the invoice date."
PAGE_TWO = "## Late fees\n\nInterest accrues monthly on any overdue balance."
TWO_PAGES = PAGE_ONE + "\n\n" + PAGE_TWO


def embed(texts):
    """A deterministic stand-in for a model: eight numbers from the words."""
    out = np.zeros((len(texts), 8), dtype=np.float32)
    for i, text in enumerate(texts):
        for word in text.lower().split():
            out[i, hash(word) % 8] += 1.0
        out[i] /= np.linalg.norm(out[i]) or 1.0
    return out


@pytest.fixture(autouse=True)
def clean_registry():
    before = dict(extract._DEFAULT._own)
    yield
    extract._DEFAULT._own.clear()
    extract._DEFAULT._own.update(before)


# ------------------------------------------------------------ fake server ---


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        seen = {
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        }
        self.server.seen.append(seen)
        route = self.path.split("?")[0]
        if route == "/extract/pdf":
            self._send(200, "text/plain; charset=utf-8", TWO_PAGES.encode())
        elif route == "/extract/pages":
            reply = {
                "text": TWO_PAGES,
                "pages": [[0, 1], [TWO_PAGES.index("## Late fees"), 2]],
                "metadata": {"ocr": True},
            }
            self._send(200, "application/json", json.dumps(reply).encode())
        elif route == "/extract/badpages":
            self._send(
                200,
                "application/json",
                json.dumps({"text": "short", "pages": [[0, 1], [900, 2]]}).encode(),
            )
        elif route == "/extract/notjson":
            self._send(200, "application/json", b"<html>gateway</html>")
        else:
            self._send(500, "text/plain", b"model not loaded")

    def _send(self, status, content_type, payload):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.seen = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


# ----------------------------------------------------------------- registry ---


class TestRegistry:
    def test_a_registered_extractor_replaces_the_built_in_reader(self, tmp_path):
        path = tmp_path / "scan.pdf"
        path.write_bytes(b"%PDF not really")
        extract.register(".pdf", lambda data, name: f"# {name}\n\n{len(data)} bytes read by OCR.")
        doc = load(path)
        assert doc.text.endswith("15 bytes read by OCR.")
        assert doc.metadata["filename"] == "scan.pdf" and doc.metadata["kind"] == "pdf"
        assert [h[1] for h in doc.headings] == ["scan.pdf"]
        assert extract.registered() == [".pdf"]
        assert extract.unregister(".pdf") is True and extract.registered() == []

    def test_naming_a_kind_asks_for_the_built_in_reader(self, tmp_path):
        path = tmp_path / "notes.md"
        path.write_text("# Notes\n\nAs written.", encoding="utf-8")
        extract.register(".md", lambda data, name: "replaced")
        assert load(path).text == "replaced"
        assert load(path, kind="markdown").text.startswith("# Notes")

    def test_a_collection_registry_sits_on_top_of_the_process_one(self):
        extract.register(".wav", lambda data, name: "process-wide audio")
        registry = resolve({".pdf": lambda data, name: "this collection's pdf"})
        assert registry.extract(b"", "a.pdf").text == "this collection's pdf"
        assert registry.extract(b"", "b.WAV").text == "process-wide audio"
        assert registry.extract(b"", "c.docx") is None
        assert registry.suffixes() == [".pdf", ".wav"]

    def test_a_wildcard_takes_what_nothing_else_claimed(self):
        registry = ExtractorRegistry({".pdf": lambda d, n: "pdf", "*": lambda d, n: "anything"})
        assert registry.extract(b"", "x.pdf").text == "pdf"
        assert registry.extract(b"", "x.xyz").text == "anything"

    def test_an_extractor_that_asks_for_the_source_is_given_it(self):
        seen = {}

        def reader(data, name, source=None):
            seen["source"] = source
            return "text"

        load_bytes(
            b"x", "call.wav", extractors={".wav": reader}, source="s3://bucket/acme/call.wav"
        )
        assert seen["source"] == "s3://bucket/acme/call.wav"

    def test_whatever_an_extractor_raises_is_one_error_type(self):
        def broken(data, name):
            raise RuntimeError("model not loaded")

        with pytest.raises(ExtractionError, match="extracting x.pdf failed: model not loaded"):
            resolve({".pdf": broken}).extract(b"", "x.pdf")

    def test_what_resolve_accepts(self):
        assert resolve(None) is extract._DEFAULT
        registry = ExtractorRegistry()
        assert resolve(registry) is registry
        with pytest.raises(TypeError, match="mapping of suffix to callable"):
            resolve(lambda data, name: "bare callable says nothing about suffixes")
        with pytest.raises(TypeError, match="callable"):
            resolve({".pdf": "not callable"})
        with pytest.raises(ValueError, match="not a suffix"):
            resolve({"pdf-files": lambda d, n: ""})


class TestCoerce:
    def test_a_string_is_read_as_markdown(self):
        doc = coerce(
            "# Sales\n\n| Region | Revenue |\n|---|---|\n| EMEA | 1200 |\n\n![Revenue by region](q3.png)\n"
        )
        assert "Region: EMEA; Revenue: 1200" in doc.text and "|" not in doc.text
        assert "[Figure: Revenue by region]" in doc.text
        assert [h[1] for h in doc.headings] == ["Sales"]

    def test_a_mapping_keeps_its_pages_and_they_move_with_the_text(self):
        text = "| A | B |\n|---|---|\n| 1 | 2 |\n\nSecond page starts here."
        doc = coerce(
            {"text": text, "pages": [[0, 1], [text.index("Second"), 2]], "metadata": {"ocr": True}}
        )
        assert doc.text.startswith("A: 1; B: 2")
        assert doc.text[doc.pages[1][0] :].startswith("Second page")
        assert doc.page_at(doc.text.index("Second")) == 2 and doc.metadata == {"ocr": True}

    @pytest.mark.parametrize(
        "reply, message",
        [
            ({"pages": [[0, 1]]}, "has no text"),
            ({"text": "short", "pages": [[0, 1], [900, 2]]}, "inside the text"),
            ({"text": "some text here", "pages": [[5, 1], [2, 2]]}, "offsets ascend"),
            ({"text": "some text", "headings": [[0, "Title"]]}, "lists of 3"),
            ({"text": "some text", "metadata": ["a"]}, "metadata is an object"),
            (42, "not int"),
        ],
    )
    def test_a_reply_that_cannot_be_trusted_is_refused(self, reply, message):
        with pytest.raises(ExtractionError, match=message):
            coerce(reply, "x.pdf")

    def test_a_loaded_document_passes_through(self):
        doc = LoadedDocument(text="as is")
        assert coerce(doc) is doc


# --------------------------------------------------------------------- http ---


class TestHttpExtractor:
    def test_raw_body_with_the_name_in_a_header(self, server):
        httpd, url = server
        reader = HttpExtractor(url, routes={".pdf": "/extract/pdf?language=en-US"}, body="raw")
        doc = reader(b"%PDF-bytes", "scan.pdf")
        seen = httpd.seen[-1]
        assert seen["body"] == b"%PDF-bytes"
        assert seen["headers"]["x-filename"] == "scan.pdf"
        assert seen["path"] == "/extract/pdf?language=en-US"
        assert [h[1] for h in doc.headings] == ["Terms", "Late fees"]
        assert doc.metadata["extractor"] == url + "/extract/pdf"

    def test_multipart_is_the_default(self, server):
        httpd, url = server
        HttpExtractor(url, routes={"pdf": "extract/pdf"}, headers={"x-api-key": "k"})(
            b"PDFDATA", 'we"ird.pdf'
        )
        seen = httpd.seen[-1]
        assert seen["headers"]["content-type"].startswith("multipart/form-data; boundary=")
        assert b'name="file"; filename="weird.pdf"' in seen["body"] and b"PDFDATA" in seen["body"]
        assert seen["headers"]["x-api-key"] == "k"

    def test_a_name_a_header_cannot_carry_is_percent_encoded(self, server):
        httpd, url = server
        HttpExtractor(url, routes={".pdf": "/extract/pdf"}, body="raw")(b"x", "résumé 東京.pdf")
        assert (
            httpd.seen[-1]["headers"]["x-filename"] == "r%C3%A9sum%C3%A9%20%E6%9D%B1%E4%BA%AC.pdf"
        )

    def test_a_json_reply_carries_pages(self, server):
        _, url = server
        doc = HttpExtractor(url, routes={".pdf": "/extract/pages"})(b"x", "scan.pdf")
        assert doc.page_at(doc.text.index("Interest accrues")) == 2
        assert doc.metadata["ocr"] is True

    def test_anything_but_success_names_the_route_and_the_status(self, server):
        _, url = server
        with pytest.raises(
            ExtractionError, match=r"/extract/wav answered 500 for call.wav: model not loaded"
        ) as caught:
            HttpExtractor(url, routes={".wav": "/extract/wav"})(b"x", "call.wav")
        assert caught.value.route == "/extract/wav" and caught.value.status == 500

    def test_a_reply_with_pages_past_its_own_text_is_refused(self, server):
        _, url = server
        with pytest.raises(ExtractionError, match="inside the text"):
            HttpExtractor(url, routes={".pdf": "/extract/badpages"})(b"x", "scan.pdf")

    def test_json_that_is_not_json(self, server):
        _, url = server
        with pytest.raises(ExtractionError, match="said JSON and sent something else"):
            HttpExtractor(url, routes={".pdf": "/extract/notjson"})(b"x", "scan.pdf")

    def test_an_endpoint_that_is_not_there(self):
        reader = HttpExtractor(
            "http://127.0.0.1:9", routes={".pdf": "/extract/pdf"}, timeout=2, retries=0
        )
        with pytest.raises(ExtractionError, match="could not be reached") as caught:
            reader(b"x", "scan.pdf")
        assert caught.value.route == "/extract/pdf" and caught.value.status is None

    def test_a_service_that_times_out_or_is_busy_is_asked_again_with_backoff_and_jitter(self):
        calls, waited = [], []

        def transport(method, url, headers, body, timeout):
            calls.append(url)
            if len(calls) == 1:
                raise TimeoutError("read timed out")
            if len(calls) == 2:
                return 503, {"Retry-After": "2"}, b"busy"
            if len(calls) == 3:
                return 429, {}, b"slow down"
            return 200, {"Content-Type": "text/plain"}, b"read at last"

        reader = HttpExtractor(
            "https://ai.example.test",
            routes={".pdf": "/extract/pdf"},
            transport=transport,
            retries=3,
            backoff=1.0,
            sleep=waited.append,
        )
        assert reader(b"x", "a.pdf").text == "read at last" and len(calls) == 4
        assert 0.5 <= waited[0] <= 1.5, "the first wait is about a second, with jitter"
        assert waited[1] == 2.0, "Retry-After is honoured as it is"
        assert 2.0 <= waited[2] <= 6.0, "the third wait is about four seconds, with jitter"

    def test_after_the_last_try_the_error_says_how_many_and_a_plain_failure_is_not_retried(self):
        calls, waited = [], []

        def down(method, url, headers, body, timeout):
            calls.append(1)
            raise ConnectionError("refused")

        reader = HttpExtractor(
            "https://ai.example.test",
            routes={".pdf": "/extract/pdf"},
            transport=down,
            retries=2,
            sleep=waited.append,
        )
        with pytest.raises(
            ExtractionError, match=r"/extract/pdf could not be reached after 3 tries: refused"
        ):
            reader(b"x", "a.pdf")
        assert len(calls) == 3 and len(waited) == 2

        def broken(method, url, headers, body, timeout):
            calls.append(2)
            return 500, {}, b"model not loaded"

        calls.clear()
        with pytest.raises(ExtractionError, match=r"answered 500 for a.pdf: model not loaded$"):
            HttpExtractor(
                "https://ai.example.test",
                routes={".pdf": "/extract/pdf"},
                transport=broken,
                retries=3,
                sleep=waited.append,
            )(b"x", "a.pdf")
        assert calls == [2], (
            "a 500 is the file's or the service's own fault, not the network's: asked once"
        )

        def busy(method, url, headers, body, timeout):
            return 503, {}, b"busy"

        with pytest.raises(ExtractionError, match=r"answered 503 for a.pdf: busy \(2 tries\)"):
            HttpExtractor(
                "https://ai.example.test",
                routes={".pdf": "/extract/pdf"},
                transport=busy,
                retries=1,
                sleep=waited.append,
            )(b"x", "a.pdf")

    def test_the_retries_come_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_EXTRACTOR_URL", "https://ai.example.test")
        monkeypatch.setenv("VECTRIXDB_EXTRACTOR_ROUTES", json.dumps({".pdf": "/extract/pdf"}))
        monkeypatch.setenv("VECTRIXDB_EXTRACTOR_RETRIES", "5")
        assert HttpExtractor.from_environment().retries == 5
        monkeypatch.delenv("VECTRIXDB_EXTRACTOR_RETRIES")
        assert HttpExtractor.from_environment().retries == 3

    def test_a_transport_can_be_handed_in(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append((method, url, timeout))
            return 200, {"Content-Type": "text/plain"}, b"from the transport"

        reader = HttpExtractor(
            "https://ai.example.test/", routes={".png": "/ocr"}, timeout=7, transport=transport
        )
        assert reader(b"x", "page.png").text == "from the transport"
        assert calls == [("POST", "https://ai.example.test/ocr", 7.0)]

    def test_it_asks_the_service_to_mask_and_keeps_what_was_masked(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(url)
            if "json" in headers.get("Accept", ""):
                return (
                    200,
                    {"Content-Type": "application/json"},
                    json.dumps(
                        {
                            "text": "Call [PHONE]",
                            "masking": {
                                "text": "never kept",
                                "found": [{"type": "phone"}],
                                "counts": {"phone": 1},
                                "score": 0.5,
                                "engine": "regex",
                                "language": "en",
                                "regex_only": False,
                            },
                        }
                    ).encode(),
                )
            return 200, {"Content-Type": "text/plain"}, b"x"

        reader = HttpExtractor(
            "https://ai.example.test",
            routes={".txt": "/extract/txt", ".md": "/extract/md?verbose=1"},
            transport=transport,
            mask=True,
        )
        doc = reader(b"Call 416-555-0199", "call.txt")
        assert calls[-1] == "https://ai.example.test/extract/txt?mask=1"
        assert doc.text == "Call [PHONE]" and doc.metadata["masking"] == {
            "counts": {"phone": 1},
            "score": 0.5,
            "engine": "regex",
            "language": "en",
            "regex_only": False,
        }, "counts and the score, never the text or the offsets"
        assert doc.metadata["extractor"] == "https://ai.example.test/extract/txt", (
            "the route it went to, without the question"
        )
        reader(b"x", "notes.md")
        assert calls[-1] == "https://ai.example.test/extract/md?verbose=1&mask=1", (
            "a route with a query of its own keeps it"
        )
        typed = HttpExtractor(
            "https://ai.example.test",
            routes={".txt": "/extract/txt"},
            transport=transport,
            mask=["email", "SSN"],
        )
        typed(b"x", "a.txt")
        assert calls[-1] == "https://ai.example.test/extract/txt?mask=1&types=email,ssn"
        assert (
            HttpExtractor("https://ai.example.test", routes={".txt": "/x"}, mask="all").mask
            == "mask=1&types=all"
        )
        assert (
            HttpExtractor("https://ai.example.test", routes={".txt": "/x"}, mask="off").mask == ""
            and HttpExtractor("https://ai.example.test", routes={".txt": "/x"}).mask == ""
        )

    def test_a_markdown_reply_carries_its_masking_in_a_header(self):
        def transport(method, url, headers, body, timeout):
            return (
                200,
                {
                    "Content-Type": "text/markdown",
                    "X-Masking": json.dumps(
                        {
                            "counts": {"email": 2},
                            "score": 0.7,
                            "engine": "language",
                            "regex_only": False,
                        }
                    ),
                },
                b"# Notes\n\na\u2022\u2022\u2022@example.com",
            )

        reader = HttpExtractor(
            "https://ai.example.test", routes={".md": "/extract/md"}, transport=transport, mask="1"
        )
        doc = reader(b"x", "notes.md")
        assert (
            doc.metadata["masking"]["counts"] == {"email": 2}
            and doc.metadata["masking"]["engine"] == "language"
        )

    def test_the_setting_asks_for_masking(self):
        url = {"VECTRIXDB_EXTRACTOR_URL": "https://extract.example.com"}
        reader = HttpExtractor.from_environment(
            {**url, "VECTRIXDB_EXTRACTOR_MASK": "1"}, routes={".pdf": "/extract/pdf"}
        )
        assert reader.mask == "mask=1"
        assert (
            HttpExtractor.from_environment(
                {**url, "VECTRIXDB_EXTRACTOR_MASK": "email, phone"}, routes={".pdf": "/extract/pdf"}
            ).mask
            == "mask=1&types=email,phone"
        )
        assert HttpExtractor.from_environment(url, routes={".pdf": "/extract/pdf"}).mask == "", (
            "not asked unless said"
        )

    def test_what_construction_refuses(self):
        with pytest.raises(ValueError, match="http or https"):
            HttpExtractor("file:///etc/passwd", routes={".pdf": "/x"})
        with pytest.raises(ValueError, match="'multipart' or 'raw'"):
            HttpExtractor("http://h", routes={".pdf": "/x"}, body="form")
        with pytest.raises(ValueError, match="at least one suffix"):
            HttpExtractor("http://h", routes={})

    def test_it_resolves_to_a_registry_of_its_own_suffixes(self):
        reader = HttpExtractor("http://h", routes={".pdf": "/a", ".WAV": "/b"})
        registry = resolve(reader)
        assert registry.suffixes() == [".pdf", ".wav"] and registry.get("x.wav") is reader
        assert registry.get("x.docx") is None


# ---------------------------------------------------------- through Vectrix ---


class TestThroughVectrix:
    def test_an_endpoint_reads_the_pdf_and_the_citation_has_the_page(self, server, tmp_path):
        from vectrixdb import Vectrix

        _, url = server
        scan = tmp_path / "scan.pdf"
        scan.write_bytes(b"%PDF scanned")
        db = Vectrix(
            "docs",
            path=str(tmp_path / "db"),
            embed_fn=embed,
            dimension=8,
            extractors=HttpExtractor(url, routes={".pdf": "/extract/pages"}, body="raw"),
        )
        try:
            assert db.add_document(scan, chunk="markdown") == 2
            rows = {m["heading"]: m for _, _, m in db._collection._iter_documents_raw()}
            assert rows["Late fees"]["_vx_citation"] == "scan.pdf#page=2"
            assert rows["Terms"]["_vx_citation"] == "scan.pdf#page=1"
            assert rows["Terms"]["extractor"] == url + "/extract/pages"
        finally:
            db.close()

    def test_a_suffix_the_library_never_knew(self, tmp_path):
        from vectrixdb import Vectrix

        call = tmp_path / "call.wav"
        call.write_bytes(b"RIFF....")
        db = Vectrix(
            "calls",
            path=str(tmp_path / "db"),
            embed_fn=embed,
            dimension=8,
            extractors={
                ".wav": lambda data, name: "The customer asked to defer the October payment."
            },
        )
        try:
            assert db.add_document(call) == 1
            hit = db.search("defer payment", limit=1).top
            assert hit.citation == "call.wav" and "October" in hit.text
        finally:
            db.close()

    def test_a_mistake_in_extractors_is_an_error_at_construction(self, tmp_path):
        from vectrixdb import Vectrix

        with pytest.raises(TypeError, match="callable"):
            Vectrix(
                "bad",
                path=str(tmp_path / "db"),
                embed_fn=embed,
                dimension=8,
                extractors={".pdf": 3},
            )


class TestEmbedHeading:
    def test_the_model_sees_the_heading_path_and_the_store_does_not(self, tmp_path):
        from vectrixdb import Vectrix

        seen = []

        def spy(texts):
            seen.extend(texts)
            return embed(texts)

        db = Vectrix(
            "policy", path=str(tmp_path / "db"), embed_fn=spy, dimension=8, embedding_cache=False
        )
        try:
            db.add_document(TWO_PAGES, chunk="markdown", embed_heading=True, doc_id="terms")
            assert "Terms > Late fees: Interest accrues monthly on any overdue balance." in seen
            assert "Terms: Payment is due within thirty days of the invoice date." in seen
            rows = {m["heading"]: (text, m) for _, text, m in db._collection._iter_documents_raw()}
            text, meta = rows["Late fees"]
            assert text == "Interest accrues monthly on any overdue balance."
            assert meta["_vx_embed_prefix"] == "Terms > Late fees"

            seen.clear()
            db.reembed()
            assert "Terms > Late fees: Interest accrues monthly on any overdue balance." in seen
        finally:
            db.close()

    def test_off_by_default(self, tmp_path):
        from vectrixdb import Vectrix

        seen = []
        db = Vectrix(
            "plain",
            path=str(tmp_path / "db"),
            dimension=8,
            embedding_cache=False,
            embed_fn=lambda texts: (seen.extend(texts), embed(texts))[1],
        )
        try:
            db.add_document(TWO_PAGES, chunk="markdown", doc_id="terms")
            assert "Interest accrues monthly on any overdue balance." in seen
            assert all(
                "_vx_embed_prefix" not in m for _, _, m in db._collection._iter_documents_raw()
            )
        finally:
            db.close()


# ------------------------------------------------------------------- worker ---


class TestWorker:
    def _worker(self, tmp_path, reader, **options):
        from vectrixdb import Vectrix
        from vectrixdb.worker import IngestWorker, LocalFetcher

        db = Vectrix(
            "inbox",
            path=str(tmp_path / "db"),
            embed_fn=embed,
            dimension=8,
            extractors={".pdf": reader},
        )
        return db, IngestWorker(
            db, LocalFetcher(), doc_id_of=lambda uri: uri.rsplit("/", 1)[-1], **options
        )

    def test_it_uses_the_extractors_the_collection_was_opened_with(self, tmp_path):
        from vectrixdb.worker import IngestEvent

        scan = tmp_path / "scan.pdf"
        scan.write_bytes(b"%PDF")
        db, worker = self._worker(
            tmp_path, lambda data, name: "OCR says the covenant is tested quarterly."
        )
        try:
            outcome = worker.handle(IngestEvent("created", scan.as_uri()))
            assert outcome.action == "created" and outcome.chunks == 1 and outcome.error is None
            assert "covenant" in db.search("covenant", limit=1).top.text
        finally:
            db.close()

    def test_a_failed_extraction_raises_and_leaves_the_old_chunks_alone(self, tmp_path):
        from vectrixdb.worker import IngestEvent

        scan = tmp_path / "scan.pdf"
        scan.write_bytes(b"%PDF")
        state = {"fail": False}

        def reader(data, name):
            if state["fail"]:
                raise ExtractionError("/extract/pdf answered 503", route="/extract/pdf", status=503)
            return "The first reading of the document."

        db, worker = self._worker(tmp_path, reader)
        try:
            worker.handle(IngestEvent("created", scan.as_uri()))
            state["fail"] = True
            with pytest.raises(ExtractionError, match="503"):
                worker.handle(IngestEvent("created", scan.as_uri()))
            assert "first reading" in db.search("reading", limit=1).top.text
        finally:
            db.close()

    def test_record_returns_a_failed_outcome_and_the_batch_goes_on(self, tmp_path):
        from vectrixdb.worker import IngestEvent

        bad, good = tmp_path / "bad.pdf", tmp_path / "good.pdf"
        bad.write_bytes(b"bad")
        good.write_bytes(b"good")

        def reader(data, name):
            if data == b"bad":
                raise RuntimeError("unreadable")
            return "A readable page."

        db, worker = self._worker(tmp_path, reader, on_error="record")
        try:
            first, second = worker.handle_all(
                [IngestEvent("created", bad.as_uri()), IngestEvent("created", good.as_uri())]
            )
            assert first.action == "failed" and "unreadable" in first.error and first.chunks == 0
            assert second.action == "created"
        finally:
            db.close()

    def test_on_error_is_checked(self, tmp_path):
        with pytest.raises(ValueError, match="'raise' or 'record'"):
            self._worker(tmp_path, lambda d, n: "", on_error="ignore")


class TestFromTheEnvironment:
    """One reader of the VECTRIXDB_EXTRACTOR_* settings, for the REST route and anybody else."""

    URL = {"VECTRIXDB_EXTRACTOR_URL": "https://extract.example.com"}

    def test_no_address_is_no_service(self):
        assert HttpExtractor.from_environment({}, routes={".pdf": "/extract/pdf"}) is None

    def test_the_routes_given_are_what_it_reads_by(self):
        reader = HttpExtractor.from_environment(
            self.URL, routes={".pdf": "/extract/pdf", ".png": "/transcribe/image"}
        )
        assert reader.route_for("q3.pdf") == "/extract/pdf" and reader.route_for("x.zip") is None
        assert reader.body == "raw" and reader.timeout == 300 and reader.headers == {}

    def test_the_key_goes_in_the_header_it_is_told(self):
        env = {
            **self.URL,
            "VECTRIXDB_EXTRACTOR_KEY": "k",
            "VECTRIXDB_EXTRACTOR_KEY_HEADER": "api-key",
            "VECTRIXDB_EXTRACTOR_TIMEOUT": "230",
        }
        reader = HttpExtractor.from_environment(env, routes={".pdf": "/extract/pdf"})
        assert reader.headers == {"api-key": "k"} and reader.timeout == 230

    def test_the_key_header_is_x_api_key_unless_told(self):
        reader = HttpExtractor.from_environment(
            {**self.URL, "VECTRIXDB_EXTRACTOR_KEY": "k"}, routes={".pdf": "/extract/pdf"}
        )
        assert reader.headers == {"x-api-key": "k"}

    def test_the_routes_setting_wins_over_the_routes_given(self):
        env = {**self.URL, "VECTRIXDB_EXTRACTOR_ROUTES": json.dumps({".wav": "/asr"})}
        reader = HttpExtractor.from_environment(env, routes={".pdf": "/extract/pdf"})
        assert reader.suffixes() == [".wav"]

    @pytest.mark.parametrize(
        "env, says",
        [
            ({"VECTRIXDB_EXTRACTOR_ROUTES": "{not json"}, "not JSON"),
            ({"VECTRIXDB_EXTRACTOR_ROUTES": "[1, 2]"}, "mapping of suffix"),
            ({}, "nothing says which route"),
            (
                {
                    "VECTRIXDB_EXTRACTOR_URL": "ftp://extract.example.com",
                    "VECTRIXDB_EXTRACTOR_ROUTES": '{".pdf": "/p"}',
                },
                "http or https",
            ),
        ],
    )
    def test_settings_it_cannot_use_are_refused_by_name(self, env, says):
        from vectrixdb.exceptions import ConfigurationError

        with pytest.raises(ConfigurationError, match=says):
            HttpExtractor.from_environment({**self.URL, **env})
