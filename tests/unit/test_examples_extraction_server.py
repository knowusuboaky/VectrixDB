"""The reference extraction server: a key on the way in, pages from a PDF, minutes from speech.

Its engines here are made up, since the real ones talk to Azure. What is held
to is what a copy of it has to get right: nobody reads without the key, and
what it replies is cited by the page and by the minute once the library has
read it through :class:`HttpExtractor`, over a real connection.
"""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the api extra is not installed")
pytest.importorskip("httpx", reason="the test client needs httpx")

from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb.extract import HttpExtractor  # noqa: E402
from vectrixdb.extract.engines import segments_to_document  # noqa: E402

HERE = Path(__file__).resolve().parents[2] / "examples" / "extraction_server" / "server.py"
if not HERE.exists():
    pytest.skip(
        "examples/ is kept on the machine that runs it, not in the repository",
        allow_module_level=True,
    )

KEY = "k" * 40


def load():
    spec = importlib.util.spec_from_file_location("extraction_server_example", HERE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = load()
PAGES = ["Terms of the facility.", "", "  Late fees accrue monthly.  ", "Signed in Accra."]
SPEECH = [
    (2.0, 6.0, "Good morning."),
    (61.5, 70.0, "The  covenant\nwas breached."),
    (662.0, 668.5, "Thank you all."),
]


def app(**kwargs):
    kwargs.setdefault("key", KEY)
    return server.create_app(
        lambda data, name: PAGES, lambda data, name: PAGES[:1], lambda data, name: SPEECH, **kwargs
    )


class TestPageSpans:
    def test_every_offset_is_where_that_page_starts(self):
        reply = server.pages_reply(PAGES)
        for offset, number in reply["pages"]:
            assert reply["text"][offset:].startswith(PAGES[number - 1].strip())

    def test_a_blank_page_has_no_entry_and_still_counts(self):
        reply = server.pages_reply(PAGES)
        assert [number for _, number in reply["pages"]] == [1, 3, 4], "the fourth page is page 4"
        assert reply["metadata"] == {"pages": 4}

    def test_nothing_read_is_an_empty_document_and_not_an_error(self):
        assert server.pages_reply(["", "  "]) == {"text": "", "pages": [], "metadata": {"pages": 2}}


class TestMinuteOffsets:
    def test_a_minute_is_a_page_and_a_silent_minute_still_counts(self):
        reply = server.minutes_reply(SPEECH)
        assert [number for _, number in reply["pages"]] == [1, 2, 12], (
            "said at 11:02, so the twelfth minute"
        )
        for offset, _ in reply["pages"]:
            assert reply["text"][offset - 2 : offset] in ("", "\n\n")
        assert "The covenant was breached." in reply["text"], (
            "white space inside a segment is one space"
        )
        assert reply["metadata"]["duration_seconds"] == 668.5

    def test_it_is_the_librarys_own_arithmetic(self):
        """A recording reads the same whether the library transcribed it or the server did."""
        ours = segments_to_document(SPEECH)
        reply = server.minutes_reply(SPEECH)
        assert reply["text"] == ours.text and [tuple(p) for p in reply["pages"]] == list(ours.pages)
        assert reply["metadata"] == ours.metadata


class TestTheKey:
    def test_it_will_not_be_made_without_one(self, monkeypatch):
        monkeypatch.delenv("EXTRACTION_API_KEY", raising=False)
        monkeypatch.delenv("EXTRACTION_ALLOW_OPEN", raising=False)
        with pytest.raises(RuntimeError, match="EXTRACTION_API_KEY is not set"):
            server.create_app(lambda d, n: PAGES, key=None)

    def test_unless_told_that_something_in_front_does_the_asking(self, monkeypatch):
        monkeypatch.delenv("EXTRACTION_API_KEY", raising=False)
        client = TestClient(server.create_app(lambda d, n: PAGES, allow_open=True))
        assert client.post("/ocr/pdf", content=b"%PDF").status_code == 200

    def test_the_key_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("EXTRACTION_API_KEY", KEY)
        client = TestClient(server.create_app(lambda d, n: PAGES))
        assert client.post("/ocr/pdf", content=b"%PDF").status_code == 401
        assert client.post("/ocr/pdf", content=b"%PDF", headers={"api-key": KEY}).status_code == 200

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"api-key": "wrong"},
            {"Authorization": "Bearer wrong"},
            {"Authorization": f"Basic {KEY}"},
        ],
    )
    def test_no_key_and_a_wrong_key_read_nothing(self, headers):
        calls = []
        made = server.create_app(lambda d, n: calls.append(n) or PAGES, key=KEY)
        for route in ("/ocr/pdf",):
            assert TestClient(made).post(route, content=b"%PDF", headers=headers).status_code == 401
        assert calls == [], "the engine, which is what costs money, was never called"

    @pytest.mark.parametrize(
        "headers", [{"api-key": KEY}, {"x-api-key": KEY}, {"Authorization": f"Bearer {KEY}"}]
    )
    def test_the_key_is_taken_three_ways(self, headers):
        assert (
            TestClient(app()).post("/asr/audio", content=b"RIFF", headers=headers).status_code
            == 200
        )

    def test_health_asks_for_nothing_and_says_nothing_about_files(self):
        reply = TestClient(app()).get("/health").json()
        assert reply == {"status": "ok", "routes": ["/asr/audio", "/ocr/image", "/ocr/pdf"]}

    def test_a_route_exists_only_for_an_engine_that_was_given(self):
        client = TestClient(server.create_app(lambda d, n: PAGES, key=KEY))
        assert (
            client.post("/asr/audio", content=b"RIFF", headers={"api-key": KEY}).status_code == 404
        )


class TestWhatItIsSent:
    def test_an_empty_body_and_one_too_big_are_refused_before_the_engine(self):
        calls = []
        client = TestClient(
            server.create_app(lambda d, n: calls.append(n) or PAGES, key=KEY, max_bytes=10)
        )
        assert client.post("/ocr/pdf", content=b"", headers={"api-key": KEY}).status_code == 400
        assert (
            client.post("/ocr/pdf", content=b"x" * 11, headers={"api-key": KEY}).status_code == 413
        )
        assert calls == []

    def test_a_path_in_the_name_is_cut_to_its_last_part(self):
        names = []
        client = TestClient(server.create_app(lambda d, n: names.append(n) or PAGES, key=KEY))
        client.post(
            "/ocr/pdf",
            content=b"%PDF",
            headers={"api-key": KEY, "X-Filename": "..\\..\\secrets/q3.pdf"},
        )
        assert names == ["q3.pdf"]


class TestTheLibraryReadsIt:
    """Over a real connection on the loopback address, so the bytes on the wire are the ones a service sees."""

    @pytest.fixture
    def address(self):
        uvicorn = pytest.importorskip("uvicorn")
        import socket
        import time

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        running = uvicorn.Server(
            uvicorn.Config(app(), host="127.0.0.1", port=port, log_level="error")
        )
        thread = threading.Thread(target=running.run, daemon=True)
        thread.start()
        for _ in range(200):
            if running.started:
                break
            time.sleep(0.05)
        assert running.started
        yield f"http://127.0.0.1:{port}"
        running.should_exit = True
        thread.join(timeout=10)

    def test_a_pdf_is_cited_by_page_and_a_recording_by_the_minute(self, address):
        reader = HttpExtractor(
            address,
            {".pdf": "/ocr/pdf", ".wav": "/asr/audio"},
            body="raw",
            headers={"api-key": KEY},
        )
        scan = reader(b"%PDF-1.7", "scan.pdf")
        assert [number for _, number in scan.pages] == [1, 3, 4] and scan.metadata["ocr"] is True
        assert scan.text[scan.pages[1][0] :].startswith("Late fees accrue monthly.")
        call = reader(b"RIFF", "call.wav")
        assert [number for _, number in call.pages] == [1, 2, 12] and call.metadata[
            "transcript"
        ] is True

    def test_without_the_key_the_library_says_which_route_refused(self, address):
        from vectrixdb.exceptions import ExtractionError

        reader = HttpExtractor(address, {".pdf": "/ocr/pdf"}, body="raw")
        with pytest.raises(ExtractionError, match="401"):
            reader(b"%PDF-1.7", "scan.pdf")


def test_importing_the_file_for_its_helpers_never_builds_an_app_or_needs_a_key(monkeypatch):
    monkeypatch.delenv("EXTRACTION_API_KEY", raising=False)
    assert callable(load().pages_reply)
