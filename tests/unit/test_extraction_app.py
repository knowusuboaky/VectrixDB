"""The extraction service: files, addresses and videos in, text out, nothing indexed.

Every service it calls is a fake: Speech and Translator through their own
classes with a fake network, pictures, videos, YouTube and fetched addresses
by stand-ins. The one real network use is a server on 127.0.0.1 that exists
to redirect, because the redirect guard lives inside urllib and only a real
redirect proves it.
"""

from __future__ import annotations

import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb.api.extraction import (  # noqa: E402
    AzureJobs,
    ExtractionService,
    MemoryJobs,
    _guarded_transport,
    _Refused,
    allowed_host,
    create_extraction_app,
    extraction_routes,
    read_gateway_paths,
    route_prefix,
    run_extraction_job,
)
from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.extract.engines import AzureSpeech  # noqa: E402
from vectrixdb.ingest import LoadedDocument  # noqa: E402
from vectrixdb.translate import AzureTranslator  # noqa: E402

KEY = "a-test-key"
VIDEO = "dQw4w9WgXcQ"
SIGNED = {"api-key": KEY}


# ============================================================== the fakes ===


class Inline:
    """An executor that runs a job as it is submitted, so a test can read it straight after."""

    def submit(self, fn, *args):
        fn(*args)


def speech(sent=None):
    """The real AzureSpeech, answering through a fake network, recording what it was asked."""
    sent = [] if sent is None else sent

    def transport(method, url, headers, body, timeout):
        sent.append(body)
        phrases = [
            {
                "offsetMilliseconds": 0,
                "durationMilliseconds": 4200,
                "text": "Welcome to the quarterly results.",
            },
            {
                "offsetMilliseconds": 4200,
                "durationMilliseconds": 4800,
                "text": "Revenue grew in every region.",
            },
        ]
        return 200, {}, json.dumps({"phrases": phrases}).encode()

    made = AzureSpeech("https://s.cognitiveservices.azure.com", "k", transport=transport)
    made.sent = sent
    return made


def picture(data, name):
    return LoadedDocument(text="Total assets 1200", metadata={"ocr": True})


def translator():
    def transport(method, url, headers, body, timeout):
        if "/languages" in url:
            return (
                200,
                {},
                json.dumps(
                    {
                        "translation": {
                            "fr": {"name": "French", "nativeName": "Français", "dir": "ltr"}
                        }
                    }
                ).encode(),
            )
        texts = [item["Text"] for item in json.loads(body)]
        if "/detect" in url:
            return (
                200,
                {},
                json.dumps(
                    [
                        {"language": "fr", "score": 1.0, "isTranslationSupported": True}
                        for _ in texts
                    ]
                ).encode(),
            )
        return (
            200,
            {},
            json.dumps(
                [
                    {
                        "translations": [{"text": f"[en] {t}", "to": "en"}],
                        "detectedLanguage": {"language": "fr", "score": 0.9},
                    }
                    for t in texts
                ]
            ).encode(),
        )

    return AzureTranslator("k", transport=transport)


class Downloader:
    def __init__(self):
        self.asked = []

    def __call__(self, url, folder):
        self.asked.append(url)
        path = Path(folder) / f"{VIDEO}.m4a"
        path.write_bytes(b"sound")
        return str(path), {
            "id": VIDEO,
            "title": "Quarterly results, explained",
            "channel": "TD Bank",
            "duration": 212,
        }


PAGES = {
    "https://www.td.com/about.html": (
        200,
        {"Content-Type": "text/html"},
        b"<html><body><h1>About TD</h1><p>A bank.</p></body></html>",
    ),
    "https://www.td.com/logo.png": (200, {"Content-Type": "image/png"}, b"\x89PNG" + bytes(64)),
    "https://www.td.com/call.wav": (200, {"Content-Type": "audio/wav"}, b"RIFF" + bytes(64)),
    "https://www.td.com/ad.mp4": (
        200,
        {"Content-Type": "video/mp4"},
        b"\x00\x00\x00 ftyp" + bytes(64),
    ),
    "https://www.td.com/notes.txt": (
        200,
        {"Content-Type": "text/plain"},
        b"Plain notes about rates.",
    ),
}


def fetch(method, url, headers, body, timeout):
    return PAGES.get(url, (404, {}, b"no"))


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
    # Whatever the machine's own environment says about who may call it or
    # where it answers; a test that wants one of these sets it itself.
    for name in (
        "VECTRIXDB_SIGNIN",
        "VECTRIXDB_ALLOW_OPEN",
        "VECTRIXDB_READ_ONLY_API_KEY",
        "VECTRIXDB_EXTRACT_PREFIX",
        "VECTRIXDB_EXTRACT_GATEWAY_PATHS",
        "VECTRIXDB_ROOT_PATH",
        "VECTRIXDB_PUBLIC_URL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def service(tmp_path):
    return ExtractionService(
        image=picture,
        audio=speech(),
        translator=translator(),
        url_hosts=("www.td.com",),
        jobs=MemoryJobs(tmp_path / "output", executor=Inline()),
        download=Downloader(),
        transport=fetch,
    )


@pytest.fixture
def client(keyed, service):
    return TestClient(create_extraction_app(service))


# =============================================================== the door ===


class TestItWillNotBeBuiltOpen:
    def test_no_key_and_no_sign_in_is_refused(self, monkeypatch, service):
        for name in (
            "VECTRIXDB_API_KEY",
            "VECTRIXDB_API_KEY_SHA256",
            "VECTRIXDB_SIGNIN",
            "VECTRIXDB_ALLOW_OPEN",
        ):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(ConfigurationError, match="refusing to build the extraction service"):
            create_extraction_app(service)

    def test_it_can_be_told_something_in_front_does_the_asking(self, monkeypatch, service):
        for name in ("VECTRIXDB_API_KEY", "VECTRIXDB_SIGNIN"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("VECTRIXDB_ALLOW_OPEN", "1")
        assert create_extraction_app(service) is not None

    def test_or_for_a_test_on_a_laptop(self, monkeypatch, service):
        for name in ("VECTRIXDB_API_KEY", "VECTRIXDB_SIGNIN", "VECTRIXDB_ALLOW_OPEN"):
            monkeypatch.delenv(name, raising=False)
        assert create_extraction_app(service, allow_open=True) is not None


class TestTheDoor:
    def test_health_needs_no_key(self, client):
        assert client.get("/health").json()["status"] == "healthy"

    def test_everything_else_does(self, client):
        assert client.post("/extract/txt", content=b"hello").status_code == 401

    def test_a_wrong_key_is_refused(self, client):
        assert (
            client.post("/extract/txt", content=b"hello", headers={"api-key": "wrong"}).status_code
            == 401
        )

    def test_a_bearer_token_is_the_same_key(self, client):
        response = client.post(
            "/extract/txt", content=b"hello", headers={"Authorization": f"Bearer {KEY}"}
        )
        assert response.status_code == 200

    def test_a_read_only_key_cannot_spend_money(self, monkeypatch, service):
        """Every extraction calls a paid service; a key that may only read may not."""
        monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
        monkeypatch.setenv("VECTRIXDB_READ_ONLY_API_KEY", "read-only")
        app = TestClient(create_extraction_app(service))
        assert (
            app.post("/extract/txt", content=b"hello", headers={"api-key": "read-only"}).status_code
            == 403
        )


# ========================================================== the documents ===


def word_doc(text: str) -> tuple:
    """A WordDocument and table stream holding ``text``, to the format's offsets."""
    word = bytearray(0x400)
    struct.pack_into("<H", word, 0x00, 0xA5EC)
    struct.pack_into("<H", word, 0x0A, 0x0200)
    struct.pack_into("<H", word, 0x20, 14)
    struct.pack_into("<H", word, 0x3E, 22)
    struct.pack_into("<i", word, 0x4C, len(text))
    struct.pack_into("<H", word, 0x98, 93)
    plc = struct.pack("<2i", 0, len(text)) + struct.pack("<HIH", 0, 0x400, 0)
    clx = b"\x02" + struct.pack("<I", len(plc)) + plc
    struct.pack_into("<II", word, 0x1A2, 0, len(clx))
    return bytes(word) + text.encode("utf-16-le"), clx


class TestExtract:
    def test_a_text_file_answers_markdown(self, client):
        response = client.post(
            "/extract/txt", content=b"Late fees are charged monthly.", headers=SIGNED
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        assert response.text == "Late fees are charged monthly."

    def test_json_when_accept_asks_for_it_first(self, client):
        """The shape HttpExtractor reads, so a library reading through this keeps its structure."""
        response = client.post(
            "/extract/txt",
            content=b"hello",
            headers={**SIGNED, "Accept": "application/json, text/plain;q=0.9"},
        )
        body = response.json()
        assert set(body) == {"text", "pages", "headings", "metadata", "segments"}
        assert body["text"] == "hello"

    def test_markdown_for_a_caller_that_asks_for_anything(self, client):
        assert (
            client.post("/extract/txt", content=b"hi", headers={**SIGNED, "Accept": "*/*"}).text
            == "hi"
        )

    def test_a_spreadsheet_row_reads_as_its_headers(self, client):
        response = client.post(
            "/extract/csv", content=b"Region,Revenue\nEMEA,1200\n", headers=SIGNED
        )
        assert "EMEA" in response.text and "1200" in response.text

    def test_an_old_word_file(self, client, monkeypatch):
        monkeypatch.setattr(
            "vectrixdb.ingest._doc_streams", lambda path: word_doc("Quarterly summary\r")
        )
        response = client.post("/extract/doc", content=b"the bytes of a .doc", headers=SIGNED)
        assert response.status_code == 200 and response.text == "Quarterly summary"

    def test_the_route_decides_the_reader_not_the_file_name(self, client):
        """A .txt route reads text whatever the caller named the file."""
        response = client.post(
            "/extract/txt", content=b"plain", headers={**SIGNED, "X-Filename": "report.pdf"}
        )
        assert response.text == "plain"

    def test_an_empty_body_is_refused(self, client):
        assert client.post("/extract/txt", content=b"", headers=SIGNED).status_code == 400

    def test_a_file_too_large_is_refused_before_it_is_read(self, keyed, service):
        service.max_bytes = 10
        app = TestClient(create_extraction_app(service))
        response = app.post("/extract/txt", content=b"x" * 11, headers=SIGNED)
        assert response.status_code == 413 and "larger than" in response.json()["message"]


# ============================================================== the media ===


class TestTranscribeFiles:
    def test_a_picture_is_its_words(self, client):
        assert (
            client.post("/transcribe/image", content=b"\x89PNG", headers=SIGNED).text
            == "Total assets 1200"
        )

    def test_and_what_it_shows_when_vision_is_there(self, keyed, service):
        service.describer = lambda data, context: {"description": "A bar chart of revenue."}
        app = TestClient(create_extraction_app(service))
        said = app.post("/transcribe/image", content=b"\x89PNG", headers=SIGNED).text
        assert said.startswith("A bar chart of revenue.") and "Total assets 1200" in said

    def test_a_recording_is_a_timed_transcript(self, client):
        said = client.post(
            "/transcribe/audio", content=b"RIFF", headers={**SIGNED, "X-Filename": "call.wav"}
        ).text
        assert said.startswith("# Transcript: call.wav")
        assert "**[0:00 → 0:04]** Welcome to the quarterly results." in said

    def test_the_language_is_asked_for_on_this_call_only(self, keyed, service):
        app = TestClient(create_extraction_app(service))
        app.post("/transcribe/audio?language=fr-FR", content=b"RIFF", headers=SIGNED)
        app.post("/transcribe/audio", content=b"RIFF", headers=SIGNED)
        assert b'{"locales": ["fr-FR"]}' in service.audio.sent[0]
        assert b'{"locales": ["en-US"]}' in service.audio.sent[1], (
            "the shared engine is not changed"
        )

    def test_a_video_has_its_sound_taken_out(self, client, monkeypatch):
        seen = []

        class FakeVideo:
            def __init__(self, audio=None, ffmpeg=None):
                self.audio = audio

            def __call__(self, data, name):
                seen.append(name)
                return self.audio(data, name)

        monkeypatch.setattr("vectrixdb.extract.engines.Video", FakeVideo)
        response = client.post(
            "/transcribe/video", content=b"mp4", headers={**SIGNED, "X-Filename": "ad.mp4"}
        )
        assert seen == ["ad.mp4"] and "## Segments" in response.text

    def test_a_chart_is_its_description_its_values_as_rows_and_its_words(self, keyed, service):
        service.describer = lambda data, context: {
            "caption": "Revenue by region",
            "description": "A bar chart of revenue by region, EMEA the largest.\nWords in the picture: Revenue EMEA APAC",
            "table": [["Region", "Revenue"], ["EMEA", "1200"], ["APAC", "900"]],
            "by": "gpt-5.4-mini",
        }
        app = TestClient(create_extraction_app(service))
        body = app.post(
            "/transcribe/image",
            content=b"\x89PNG",
            headers={**SIGNED, "Accept": "application/json"},
        ).json()
        assert body["text"] == (
            "[Figure: Revenue by region]\n\nA bar chart of revenue by region, EMEA the largest.\n\n"
            "Region: EMEA; Revenue: 1200\nRegion: APAC; Revenue: 900\n\nTotal assets 1200"
        ), "the reader's own words are exact, so the describer's copy of them is left out"
        assert (
            body["metadata"]["described"] is True
            and body["metadata"]["described_by"] == "gpt-5.4-mini"
        )

    def test_a_picture_with_no_words_keeps_the_ones_the_describer_saw(self, keyed, service):
        service.image = lambda data, name: LoadedDocument(text="")
        service.describer = lambda data, context: {
            "description": "A photo of a branch front.\nWords in the picture: TD Bank"
        }
        app = TestClient(create_extraction_app(service))
        said = app.post("/transcribe/image", content=b"\x89PNG", headers=SIGNED).text
        assert said == "A photo of a branch front.\nWords in the picture: TD Bank"

    def test_the_screen_of_a_video_is_read_by_the_picture_reader(self, keyed, service, monkeypatch):
        made = []

        class FakeVideo:
            def __init__(self, audio=None, ffmpeg=None):
                self.audio, self.frames, self.max_frames = audio, None, 12
                made.append(self)

            def __call__(self, data, name):
                return self.audio(data, name)

        monkeypatch.setattr("vectrixdb.extract.engines.Video", FakeVideo)
        service.video_frames = 6
        app = TestClient(create_extraction_app(service))
        app.post("/transcribe/video", content=b"mp4", headers={**SIGNED, "X-Filename": "ad.mp4"})
        assert made[0].frames is service.image and made[0].max_frames == 6
        service.video_frames = 0
        app.post("/transcribe/video", content=b"mp4", headers={**SIGNED, "X-Filename": "ad.mp4"})
        assert made[1].frames is None, "no frames asked for reads the sound alone"

    def test_no_language_asked_for_is_none_claimed(self, client):
        """Speech hears which of its languages it was; the call does not claim one for it."""
        body = client.post(
            "/transcribe/audio",
            content=b"RIFF",
            headers={**SIGNED, "X-Filename": "call.wav", "Accept": "application/json"},
        ).json()
        assert "language" not in body["metadata"]


class TestPagesReadBySight:
    AZURE = {
        "VECTRIXDB_EXTRACT_PDF": "vision",
        "AZURE_OPENAI_ENDPOINT": "https://o.openai.azure.com",
        "AZURE_OPENAI_KEY": "k",
        "AZURE_OPENAI_VISION_DEPLOYMENT": "gpt-5.4-mini",
    }

    def test_vision_reads_the_unsure_pages_with_the_describers_model(self):
        made = ExtractionService.from_environment(self.AZURE, jobs=None, describer=None)
        assert made.page_reader.label == "gpt-5.4-mini" and made.every_page is False
        every = ExtractionService.from_environment(
            {**self.AZURE, "VECTRIXDB_VISION_PAGES": "all"}, jobs=None, describer=None
        )
        assert every.every_page is True

    def test_with_nothing_that_can_see_pdfs_are_read_by_the_rules_and_it_says_so(self, caplog):
        made = ExtractionService.from_environment(
            {"VECTRIXDB_EXTRACT_PDF": "vision"}, jobs=None, describer=None
        )
        assert made.page_reader is None and "needs a chat model that can see" in caplog.text

    def test_the_route_reads_the_unsure_pages_by_sight(self, keyed, service, tmp_path):
        fitz = pytest.importorskip("fitz")
        pytest.importorskip("pypdfium2")
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 90), "The year in brief, as the board reported it.", fontsize=10)
        for top, figure, label in (
            (160, "4.6%", "2025 Dividend Yield"),
            (240, "25.9%", "Total Shareholder Return"),
        ):
            page.insert_text((72, top), figure, fontsize=28)
            page.insert_text((72, top + 25), label, fontsize=10)
        data = doc.tobytes()
        doc.close()
        service.page_reader = lambda png, layer, context: (
            "The year in brief, as the board reported it.\n\n- 4.6% 2025 dividend yield\n- 25.9% total shareholder return"
        )
        app = TestClient(create_extraction_app(service))
        body = app.post(
            "/extract/pdf", content=data, headers={**SIGNED, "Accept": "application/json"}
        ).json()
        assert "- 4.6% 2025 dividend yield" in body["text"] and body["metadata"][
            "pages_read_by_sight"
        ] == [1]


class TestSpeechAndVideoSettings:
    @staticmethod
    def env(**more):
        return {
            "AZURE_SPEECH_ENDPOINT": "https://s.cognitiveservices.azure.com",
            "AZURE_SPEECH_KEY": "k",
            **more,
        }

    def test_english_and_french_four_voices_and_twelve_frames_unless_told(self):
        made = ExtractionService.from_environment(self.env(), jobs=None, describer=None)
        assert (
            made.audio.locales == ["en-US", "fr-CA"]
            and made.audio.speakers == 4
            and made.video_frames == 12
        )

    def test_each_is_a_setting(self):
        made = ExtractionService.from_environment(
            self.env(
                VECTRIXDB_SPEECH_LOCALES="en-GB, de-DE",
                VECTRIXDB_SPEECH_SPEAKERS="0",
                VECTRIXDB_VIDEO_FRAMES="0",
            ),
            jobs=None,
            describer=None,
        )
        assert (
            made.audio.locales == ["en-GB", "de-DE"]
            and made.audio.speakers == 0
            and made.video_frames == 0
        )


# ========================================================== the addresses ===


class TestAddresses:
    def test_a_page_on_an_allowed_host(self, client):
        response = client.post(
            "/transcribe/webpage",
            json={"url": "https://www.td.com/about.html"},
            headers={**SIGNED, "Accept": "application/json"},
        )
        body = response.json()
        assert "About TD" in body["text"]
        assert body["metadata"]["embedded_media"] == "not read", (
            "said, rather than silently skipped"
        )

    def test_a_host_not_allowed_is_refused(self, client):
        response = client.post(
            "/transcribe/webpage",
            json={"url": "http://169.254.169.254/latest/meta-data/"},
            headers=SIGNED,
        )
        assert response.status_code == 403

    def test_with_no_hosts_allowed_nothing_is_fetched(self, keyed, service):
        service.url_hosts = ()
        app = TestClient(create_extraction_app(service))
        response = app.post(
            "/transcribe/webpage", json={"url": "https://www.td.com/about.html"}, headers=SIGNED
        )
        assert (
            response.status_code == 403
            and "VECTRIXDB_EXTRACT_URL_HOSTS" in response.json()["message"]
        )

    def test_image_audio_and_video_by_address(self, client, monkeypatch):
        monkeypatch.setattr(
            "vectrixdb.extract.engines.Video",
            lambda audio=None, ffmpeg=None: lambda data, name: audio(data, name),
        )
        assert (
            client.post(
                "/transcribe/image_url", json={"url": "https://www.td.com/logo.png"}, headers=SIGNED
            ).text
            == "Total assets 1200"
        )
        for route, url in (("audio_url", "call.wav"), ("video_url", "ad.mp4")):
            said = client.post(
                f"/transcribe/{route}", json={"url": f"https://www.td.com/{url}"}, headers=SIGNED
            ).text
            assert "**[0:00 → 0:04]**" in said and "- URL: https://www.td.com/" in said

    @pytest.mark.parametrize(
        "host, url, allowed",
        [
            ("www.td.com", "https://www.td.com/x", True),
            ("www.td.com", "https://td.com/x", False),
            ("www.td.com", "https://www.td.com.evil.test/x", False),
            ("*.td.com", "https://cdn.td.com/x", True),
            ("*.td.com", "https://td.com/x", False),
            ("www.td.com", "ftp://www.td.com/x", False),
            ("www.td.com", "https://WWW.TD.COM/x", True),
        ],
    )
    def test_what_counts_as_an_allowed_host(self, host, url, allowed):
        assert allowed_host(url, [host]) is allowed


class _Redirecting(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server's name
        self.send_response(302)
        self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
        self.end_headers()

    def log_message(self, *args):
        pass


class TestARedirectCannotLeaveTheAllowedHosts:
    """Checked on a real redirect, because the guard lives inside urllib."""

    def test_an_allowed_host_redirecting_elsewhere_is_not_followed(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Redirecting)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            send = _guarded_transport(["127.0.0.1"])
            with pytest.raises(_Refused, match="not an allowed host"):
                send("GET", f"http://127.0.0.1:{server.server_port}/start", {}, b"", 5.0)
        finally:
            server.shutdown()


class _Large(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server's name
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"x" * 5000)

    def log_message(self, *args):
        pass


class TestALargeBodyIsNotReadWhole:
    """A body past the limit is read one byte past it and no further, on a real socket."""

    def test_the_read_stops_one_byte_past_the_limit(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Large)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/big.txt"
            _status, _headers, body = _guarded_transport(["127.0.0.1"], max_bytes=100)(
                "GET", url, {}, b"", 5.0
            )
            assert len(body) == 101
            service = ExtractionService(url_hosts=("127.0.0.1",), max_bytes=100)
            with pytest.raises(_Refused, match="larger than 100 bytes"):
                service.fetch(url)
        finally:
            server.shutdown()


# ================================================================ YouTube ===


class TestYouTube:
    def test_a_video_is_the_references_transcript(self, client):
        said = client.post(
            "/transcribe/youtube",
            json={"url": f"https://youtu.be/{VIDEO}", "language": "en-US"},
            headers=SIGNED,
        ).text
        assert said.startswith("# YouTube: Quarterly results, explained")
        assert (
            "- Channel: TD Bank" in said
            and "**[0:04 → 0:09]** Revenue grew in every region." in said
        )

    def test_an_address_that_is_not_one_video_is_refused(self, client):
        response = client.post(
            "/transcribe/youtube",
            json={"url": "https://www.youtube.com/playlist?list=PL1"},
            headers=SIGNED,
        )
        assert response.status_code == 422

    def test_saving_is_a_job(self, client):
        response = client.post(
            "/transcribe/youtube_save", json={"url": f"https://youtu.be/{VIDEO}"}, headers=SIGNED
        )
        assert response.status_code == 202
        started = response.json()
        assert started["status"] == "queued" and started["check"] == f"/jobs/{started['job']}"

    def test_the_finished_job_carries_what_a_synchronous_save_answered(self, client, tmp_path):
        job = client.post(
            "/transcribe/youtube_save", json={"url": f"https://youtu.be/{VIDEO}"}, headers=SIGNED
        ).json()["job"]
        done = client.get(f"/jobs/{job}", headers=SIGNED).json()
        assert done["status"] == "done"
        result = done["result"]
        assert result["success"] is True and result["video_title"] == "Quarterly results, explained"
        assert result["transcript_filename"] == f"{VIDEO}_transcript.md"
        assert result["media_deleted"] == f"{VIDEO}.m4a" and result["auto_delete_enabled"] is True
        assert result["duration"] == 212 and result["channel"] == "TD Bank"
        assert Path(result["transcript_file"]).read_text(encoding="utf-8").startswith("# YouTube:")
        assert Path(result["transcript_file"]).parent == tmp_path / "output" / "output"

    def test_the_sound_is_kept_when_asked(self, client, tmp_path):
        body = {"url": f"https://youtu.be/{VIDEO}", "output_dir": "calls", "auto_delete": False}
        job = client.post("/transcribe/youtube_save", json=body, headers=SIGNED).json()["job"]
        result = client.get(f"/jobs/{job}", headers=SIGNED).json()["result"]
        assert result["media_deleted"] is None
        assert (tmp_path / "output" / "calls" / f"{VIDEO}.m4a").exists()

    @pytest.mark.parametrize("folder", ["../etc", "a/b", "a\\b", "..", "", ".hidden"])
    def test_a_caller_names_a_folder_never_a_path(self, client, folder):
        body = {"url": f"https://youtu.be/{VIDEO}", "output_dir": folder}
        assert client.post("/transcribe/youtube_save", json=body, headers=SIGNED).status_code == 422

    def test_a_job_that_is_not_there(self, client):
        assert client.get("/jobs/j_0000000000000000", headers=SIGNED).status_code == 404

    def test_a_failed_job_says_why(self, keyed, service):
        def refuse(url, folder):
            raise RuntimeError("Video unavailable")

        service.download = refuse
        app = TestClient(create_extraction_app(service))
        job = app.post(
            "/transcribe/youtube_save", json={"url": f"https://youtu.be/{VIDEO}"}, headers=SIGNED
        ).json()["job"]
        failed = app.get(f"/jobs/{job}", headers=SIGNED).json()
        assert failed["status"] == "failed" and "Video unavailable" in failed["error"]


class TestAuto:
    def test_a_youtube_address_is_a_video(self, client):
        said = client.post(
            "/transcribe/auto", json={"url": f"https://youtu.be/{VIDEO}"}, headers=SIGNED
        ).text
        assert said.startswith("# YouTube:")

    def test_anything_else_is_read_by_what_it_turns_out_to_be(self, client):
        assert (
            client.post(
                "/transcribe/auto", json={"url": "https://www.td.com/logo.png"}, headers=SIGNED
            ).text
            == "Total assets 1200"
        )
        assert (
            "About TD"
            in client.post(
                "/transcribe/auto", json={"url": "https://www.td.com/about.html"}, headers=SIGNED
            ).text
        )
        assert (
            client.post(
                "/transcribe/auto", json={"url": "https://www.td.com/notes.txt"}, headers=SIGNED
            ).text
            == "Plain notes about rates."
        )

    def test_it_is_held_to_the_same_hosts(self, client):
        assert (
            client.post(
                "/transcribe/auto", json={"url": "https://evil.test/x.pdf"}, headers=SIGNED
            ).status_code
            == 403
        )


# ============================================================ translation ===


class TestTranslate:
    def test_text_answers_the_references_shape(self, client):
        body = client.post(
            "/translate/text", json={"text": ["Bonjour"], "to": "en"}, headers=SIGNED
        ).json()
        assert body == {
            "translations": [
                {
                    "text": "[en] Bonjour",
                    "to": "en",
                    "detected_language": "fr",
                    "detected_score": 0.9,
                }
            ],
            "from_language": "auto-detected",
            "to_language": "en",
        }

    def test_one_text_is_a_list_of_one(self, client):
        body = client.post(
            "/translate/text",
            json={"text": "Bonjour", "to": "en", "from_lang": "fr"},
            headers=SIGNED,
        ).json()
        assert len(body["translations"]) == 1 and body["from_language"] == "fr"

    def test_detect(self, client):
        body = client.post("/translate/detect", json={"text": "Bonjour"}, headers=SIGNED).json()
        assert (
            body["detections"][0]["language"] == "fr"
            and body["detections"][0]["is_translation_supported"] is True
        )

    def test_languages(self, client):
        body = client.get("/translate/languages", headers=SIGNED).json()
        assert body == {
            "languages": {"fr": {"name": "French", "native_name": "Français", "dir": "ltr"}},
            "count": 1,
        }

    def test_not_set_up_says_which_setting(self, keyed, service):
        service.translator = None
        app = TestClient(create_extraction_app(service))
        response = app.post("/translate/text", json={"text": "x", "to": "en"}, headers=SIGNED)
        assert response.status_code == 503 and "AZURE_TRANSLATOR_KEY" in response.json()["message"]


# ============================================================ azure jobs ===


class FakeBlob:
    def __init__(self, data):
        self.data = data

    def readall(self):
        return self.data


class NotFound(Exception):
    status_code = 404


class Container:
    url = "https://acct.blob.core.windows.net/extraction"

    def __init__(self):
        self.blobs = {}

    def upload_blob(self, name, data, overwrite=False):
        self.blobs[name] = (
            data.read()
            if hasattr(data, "read")
            else (data.encode() if isinstance(data, str) else data)
        )

    def download_blob(self, name):
        if name not in self.blobs:
            raise NotFound("The specified blob does not exist. BlobNotFound")
        return FakeBlob(self.blobs[name])


class Queue:
    def __init__(self):
        self.sent = []

    def send_message(self, text):
        self.sent.append(text)


class TestAzureJobs:
    """What a function app uses: records in a container, work on a queue, any instance can answer."""

    def service(self, tmp_path):
        return ExtractionService(
            audio=speech(),
            jobs=AzureJobs(Container(), Queue()),
            download=Downloader(),
            url_hosts=(),
        )

    def test_the_route_writes_a_record_and_a_message_and_does_no_work(self, keyed, tmp_path):
        service = self.service(tmp_path)
        app = TestClient(create_extraction_app(service))
        job = app.post(
            "/transcribe/youtube_save", json={"url": f"https://youtu.be/{VIDEO}"}, headers=SIGNED
        ).json()["job"]
        assert json.loads(service.jobs._queue.sent[0]) == {"job": job}
        assert app.get(f"/jobs/{job}", headers=SIGNED).json()["status"] == "queued"
        assert service.download.asked == [], "the work waits for the queue"

    def test_the_queue_trigger_finishes_it_and_the_result_is_in_the_container(
        self, keyed, tmp_path
    ):
        service = self.service(tmp_path)
        app = TestClient(create_extraction_app(service))
        job = app.post(
            "/transcribe/youtube_save", json={"url": f"https://youtu.be/{VIDEO}"}, headers=SIGNED
        ).json()["job"]
        run_extraction_job(service.jobs._queue.sent[0].encode(), service)
        done = app.get(f"/jobs/{job}", headers=SIGNED).json()
        assert done["status"] == "done"
        assert (
            done["result"]["transcript_file"]
            == f"{Container.url}/transcripts/output/{VIDEO}_transcript.md"
        )
        assert f"transcripts/output/{VIDEO}_transcript.md" in service.jobs._container.blobs

    def test_a_message_delivered_twice_is_not_a_job_run_twice(self, keyed, tmp_path):
        service = self.service(tmp_path)
        app = TestClient(create_extraction_app(service))
        app.post(
            "/transcribe/youtube_save", json={"url": f"https://youtu.be/{VIDEO}"}, headers=SIGNED
        )
        message = service.jobs._queue.sent[0]
        run_extraction_job(message, service)
        run_extraction_job(message, service)
        assert len(service.download.asked) == 1

    def test_a_failure_is_recorded_and_not_raised(self, keyed, tmp_path):
        """Raised, the queue would try it five more times, and each try is paid for."""
        service = self.service(tmp_path)

        def refuse(url, folder):
            raise RuntimeError("Sign in to confirm you're not a bot")

        service.download = refuse
        app = TestClient(create_extraction_app(service))
        job = app.post(
            "/transcribe/youtube_save", json={"url": f"https://youtu.be/{VIDEO}"}, headers=SIGNED
        ).json()["job"]
        run_extraction_job(service.jobs._queue.sent[0], service)
        assert app.get(f"/jobs/{job}", headers=SIGNED).json()["status"] == "failed"

    def test_a_malformed_job_name_is_not_looked_up(self, tmp_path):
        assert self.service(tmp_path).jobs.get("../../etc/passwd") is None

    def test_memory_jobs_are_refused_by_the_queue_trigger(self, service):
        with pytest.raises(ConfigurationError, match="VECTRIXDB_EXTRACT_JOBS=azure"):
            run_extraction_job(b'{"job": "j_0000000000000000"}', service)


# ================================================================ the bridge ===


class TestALibraryReadsThroughIt:
    """App 2 pointed at App 1 with VECTRIXDB_EXTRACTOR_URL: pages and timings survive the trip."""

    def extractor(self, client, routes):
        from vectrixdb.extract import HttpExtractor

        def transport(method, url, headers, body, timeout):
            path = url.replace("https://app1.test", "")
            response = client.request(method, path, headers=headers, content=body)
            return response.status_code, dict(response.headers), response.content

        return HttpExtractor(
            "https://app1.test",
            routes=routes,
            body="raw",
            headers={"api-key": KEY},
            transport=transport,
        )

    def test_a_document_comes_back_as_a_document(self, client):
        doc = self.extractor(client, {".txt": "/extract/txt"})(
            b"Late fees are charged monthly.", "fees.txt"
        )
        assert doc.text == "Late fees are charged monthly."

    def test_a_recording_keeps_every_phrase_and_its_time(self, client):
        doc = self.extractor(client, {".wav": "/transcribe/audio"})(b"RIFF", "call.wav")
        assert doc.segments == [
            (0.0, 4.2, "Welcome to the quarterly results."),
            (4.2, 9.0, "Revenue grew in every region."),
        ]
        assert doc.pages == [(0, 1)], "cited by minute, as it was on the other side"


# ======================================================== where it answers ===


class TestTheDeploymentChoosesThePath:
    """Every route under a prefix the deployment picks, with something in front or not.

    One path a proxy puts in front of every route is a separate setting,
    VECTRIXDB_ROOT_PATH, because the two are different things: the prefix is
    part of the app and is always what arrives, while a proxy's path may be
    forwarded or taken off. A path for each endpoint is the next class.
    """

    def app(self, monkeypatch, service, *, prefix="", proxy=""):
        if proxy:
            monkeypatch.setenv("VECTRIXDB_ROOT_PATH", proxy)
        return TestClient(create_extraction_app(service, prefix=prefix))

    @pytest.mark.parametrize(
        "prefix, proxy, arrives",
        [
            ("/company", "", "/company/extract/txt"),  # nothing in front
            ("/company", "/proxy", "/proxy/company/extract/txt"),  # a proxy that forwards its path
            ("/company", "/proxy", "/company/extract/txt"),  # a proxy that takes its own path off
            ("", "/company", "/company/extract/txt"),  # the proxy's path is the company's
            ("", "/company", "/extract/txt"),  # ... and it takes it off
            ("", "", "/extract/txt"),  # neither
            ("/one/two", "", "/one/two/extract/txt"),  # a prefix of more than one part
        ],
    )
    def test_it_answers_however_the_request_arrives(
        self, keyed, monkeypatch, service, prefix, proxy, arrives
    ):
        response = self.app(monkeypatch, service, prefix=prefix, proxy=proxy).post(
            arrives, content=b"hello", headers=SIGNED
        )
        assert response.status_code == 200 and response.text == "hello"

    def test_the_prefix_can_come_from_the_environment(self, keyed, monkeypatch, service):
        monkeypatch.setenv("VECTRIXDB_EXTRACT_PREFIX", "/company")
        app = TestClient(create_extraction_app(service))
        assert app.post("/company/extract/txt", content=b"hello", headers=SIGNED).status_code == 200

    def test_once_a_prefix_is_chosen_the_root_is_not_answered(self, keyed, monkeypatch, service):
        app = self.app(monkeypatch, service, prefix="/company")
        assert app.post("/extract/txt", content=b"hello", headers=SIGNED).status_code == 404

    def test_health_and_the_documentation_need_no_key_under_it(self, keyed, monkeypatch, service):
        """Or a load balancer's probe would be asked for a key."""
        app = self.app(monkeypatch, service, prefix="/company")
        assert app.get("/company/health").json()["status"] == "healthy"
        assert app.get("/company/docs").status_code == 200
        assert "/company/extract/pdf" in app.get("/company/openapi.json").json()["paths"]

    def test_everything_else_under_it_still_does(self, keyed, monkeypatch, service):
        app = self.app(monkeypatch, service, prefix="/company")
        assert app.post("/company/extract/txt", content=b"hello").status_code == 401

    @pytest.mark.parametrize("prefix", ["", "/company"])
    def test_a_slash_too_many_is_not_found_and_not_redirected(
        self, keyed, monkeypatch, service, prefix
    ):
        """A redirect names the host the app sees, which behind a gateway sends the caller around it."""
        app = self.app(monkeypatch, service, prefix=prefix)
        response = app.post(
            f"{prefix}/extract/txt/", content=b"hello", headers=SIGNED, follow_redirects=False
        )
        assert response.status_code == 404 and "location" not in response.headers

    def test_the_address_of_a_job_is_the_one_the_caller_uses(self, keyed, monkeypatch, service):
        app = self.app(monkeypatch, service, prefix="/company", proxy="/proxy")
        started = app.post(
            "/proxy/company/transcribe/youtube_save",
            json={"url": f"https://youtu.be/{VIDEO}"},
            headers=SIGNED,
        ).json()
        assert started["check"] == f"/proxy/company/jobs/{started['job']}"
        assert app.get(started["check"], headers=SIGNED).json()["status"] == "done"

    @pytest.mark.parametrize(
        "written, meant",
        [
            ("api", "/api"),
            ("/api/", "/api"),
            ("//one//two/", "/one/two"),
            ("", ""),
            ("/", ""),
            (None, ""),
        ],
    )
    def test_a_prefix_however_it_is_written(self, written, meant):
        assert route_prefix(written) == meant

    @pytest.mark.parametrize("bad", ["/../admin", "/api/./x"])
    def test_a_prefix_that_climbs_is_refused(self, bad):
        with pytest.raises(ConfigurationError, match="a path of names"):
            route_prefix(bad)


class TestEachEndpointHasAGatewayPathOfItsOwn:
    """A gateway that publishes each endpoint under a path its team chooses, told once in a list.

    The gateway may pass that path on or take it off, and the host it gives
    callers may carry a path of its own; the app needs the list and nothing
    else. Every path here is made up.
    """

    LISTED = "extract/txt=/files/text, transcribe/youtube_save=/media/save, jobs=/media/jobs/v1, health=/probe"

    def app(self, monkeypatch, service, listed=LISTED, *, prefix="/acme", proxy=""):
        if proxy:
            monkeypatch.setenv("VECTRIXDB_ROOT_PATH", proxy)
        return TestClient(create_extraction_app(service, prefix=prefix, gateway_paths=listed))

    def save(self, app, path="/media/save/acme/transcribe/youtube_save"):
        return app.post(path, json={"url": f"https://youtu.be/{VIDEO}"}, headers=SIGNED).json()

    @pytest.mark.parametrize(
        "arrives",
        [
            "/files/text/acme/extract/txt",  # the gateway passes its path on
            "/acme/extract/txt",  # it takes the path off, or nothing stands in front
        ],
    )
    def test_a_listed_route_answers_with_its_path_or_without_it(
        self, keyed, monkeypatch, service, arrives
    ):
        response = self.app(monkeypatch, service).post(arrives, content=b"hello", headers=SIGNED)
        assert response.status_code == 200 and response.text == "hello"

    def test_each_endpoint_answers_under_a_path_of_its_own(self, keyed, monkeypatch, service):
        app = self.app(monkeypatch, service)
        assert (
            app.post("/files/text/acme/extract/txt", content=b"hello", headers=SIGNED).status_code
            == 200
        )
        assert self.save(app)["status"] in ("queued", "done")

    @pytest.mark.parametrize(
        "wrong",
        [
            "/files/text/acme/extract/pdf",  # another route under this one's path
            "/media/save/acme/extract/txt",  # this route under another one's path
            "/files/text/acme/translate/detect",  # a route with no gateway path at all
        ],
    )
    def test_a_path_opens_its_own_route_and_no_other(self, keyed, monkeypatch, service, wrong):
        assert (
            self.app(monkeypatch, service).post(wrong, content=b"hello", headers=SIGNED).status_code
            == 404
        )

    def test_a_gateway_path_is_not_read_as_part_of_another_route(self, keyed, monkeypatch, service):
        """With no prefix, /jobs in front of /health looks like the job route asked for a job called health."""
        app = self.app(monkeypatch, service, "health=/jobs", prefix="")
        assert app.get("/jobs/health").json()["status"] == "healthy"

    def test_a_route_that_is_not_listed_answers_under_the_prefix_alone(
        self, keyed, monkeypatch, service
    ):
        assert (
            self.app(monkeypatch, service)
            .get("/acme/translate/languages", headers=SIGNED)
            .json()["count"]
            == 1
        )

    def test_the_address_of_a_job_starts_with_the_job_routes_own_path(
        self, keyed, monkeypatch, service
    ):
        """The caller puts the host it was given in front, and the app answers the same address itself."""
        app = self.app(monkeypatch, service)
        started = self.save(app)
        assert started["check"] == f"/media/jobs/v1/acme/jobs/{started['job']}"
        assert app.get(started["check"], headers=SIGNED).json()["status"] == "done"
        assert app.get(f"/acme/jobs/{started['job']}", headers=SIGNED).json()["status"] == "done"

    def test_with_no_path_for_jobs_the_address_is_under_the_prefix(
        self, keyed, monkeypatch, service
    ):
        started = self.save(self.app(monkeypatch, service, "transcribe/youtube_save=/media/save"))
        assert started["check"] == f"/acme/jobs/{started['job']}"

    def test_a_proxy_path_in_front_of_every_route_still_comes_first(
        self, keyed, monkeypatch, service
    ):
        app = self.app(monkeypatch, service, proxy="/proxy")
        assert (
            app.post(
                "/proxy/files/text/acme/extract/txt", content=b"hello", headers=SIGNED
            ).status_code
            == 200
        )
        started = self.save(app, "/proxy/media/save/acme/transcribe/youtube_save")
        assert started["check"] == f"/proxy/media/jobs/v1/acme/jobs/{started['job']}"
        assert app.get(started["check"], headers=SIGNED).json()["status"] == "done"

    def test_health_under_its_own_path_needs_no_key(self, keyed, monkeypatch, service):
        """Or a gateway's probe would be asked for one; nothing else under a gateway path is let through."""
        app = self.app(monkeypatch, service)
        assert app.get("/probe/acme/health").json()["status"] == "healthy"
        assert app.post("/files/text/acme/extract/txt", content=b"hello").status_code == 401

    def test_the_description_lists_each_route_once_under_the_prefix(
        self, keyed, monkeypatch, service
    ):
        """It is what a gateway imports, and a gateway path in it would be imported as a route of its own."""
        paths = self.app(monkeypatch, service).get("/acme/openapi.json").json()["paths"]
        assert "/acme/extract/txt" in paths and "/acme/jobs/{job}" in paths
        assert not [path for path in paths if not path.startswith("/acme/")]

    def test_the_list_can_come_from_the_environment(self, keyed, monkeypatch, service):
        monkeypatch.setenv("VECTRIXDB_EXTRACT_PREFIX", "/acme")
        monkeypatch.setenv("VECTRIXDB_EXTRACT_GATEWAY_PATHS", "extract/txt=/files/text")
        app = TestClient(create_extraction_app(service))
        assert (
            app.post("/files/text/acme/extract/txt", content=b"hello", headers=SIGNED).status_code
            == 200
        )

    def test_a_mapping_does_as_well_as_the_written_form(self, keyed, monkeypatch, service):
        app = self.app(monkeypatch, service, {"extract/txt": "/files/text"})
        assert (
            app.post("/files/text/acme/extract/txt", content=b"hello", headers=SIGNED).status_code
            == 200
        )

    def test_a_route_that_is_not_served_is_refused_when_the_app_is_built(
        self, keyed, monkeypatch, service
    ):
        """A typing mistake is found at start-up, and not by the first caller."""
        with pytest.raises(ConfigurationError, match="no route called extract/pfd"):
            self.app(monkeypatch, service, "extract/pfd=/files/pdf")

    def test_the_list_however_it_is_written(self):
        written = " /extract/pdf/ = files//pdf/ , jobs=/media/jobs "
        assert read_gateway_paths(written) == {"extract/pdf": "/files/pdf", "jobs": "/media/jobs"}
        assert read_gateway_paths("") == {} and read_gateway_paths(None) == {}

    @pytest.mark.parametrize(
        "bad, says",
        [
            ("extract/pdf", "route=path"),
            ("extract/pdf=", "both given"),
            ("=/files/pdf", "both given"),
            ("extract/pdf=/../admin", "a path of names"),
            ("extract/pdf=/one, extract/pdf=/two", "two gateway paths"),
        ],
    )
    def test_a_list_that_cannot_be_read_is_refused(self, bad, says):
        with pytest.raises(ConfigurationError, match=says):
            read_gateway_paths(bad)


class TestEveryFileHasOneRoute:
    """The suffix decides the route, from one table, so a caller never keeps a list of its own."""

    def test_markdown_and_html_have_routes_of_their_own(self, client):
        markdown = client.post(
            "/extract/md", content=b"# Late fees\n\nCharged monthly.", headers=SIGNED
        )
        page = client.post(
            "/extract/html", content=b"<h1>Late fees</h1><p>Charged monthly.</p>", headers=SIGNED
        )
        assert markdown.status_code == page.status_code == 200
        assert "Charged monthly." in markdown.text and "Charged monthly." in page.text

    def test_a_workbook_with_macros_is_read_by_the_workbook_route_under_its_own_name(
        self, client, tmp_path
    ):
        openpyxl = pytest.importorskip("openpyxl")
        book = openpyxl.Workbook()
        book.active.append(["Region", "Revenue"])
        book.active.append(["EMEA", 1200])
        book.save(tmp_path / "book.xlsx")
        response = client.post(
            "/extract/xlsx",
            content=(tmp_path / "book.xlsx").read_bytes(),
            headers={**SIGNED, "X-Filename": "book.xlsm", "Accept": "application/json"},
        )
        assert (
            "EMEA" in response.json()["text"]
            and response.json()["metadata"]["filename"] == "book.xlsm"
        )

    def test_the_table_is_the_routes_the_app_serves(self, keyed, service):
        """Held equal, so a route added to the app is a suffix a caller can send, and never one it cannot."""
        described = create_extraction_app(service).openapi()["paths"]
        reading = {
            path
            for path, operations in described.items()
            if "post" in operations
            and (
                path.startswith("/extract/")
                or path in ("/transcribe/image", "/transcribe/audio", "/transcribe/video")
            )
        }
        assert reading and set(extraction_routes().values()) == reading

    def test_every_file_the_library_reads_has_a_route(self):
        from vectrixdb.extract.engines import AUDIO_SUFFIXES, IMAGE_SUFFIXES, VIDEO_SUFFIXES

        documents = (
            ".md",
            ".markdown",
            ".txt",
            ".html",
            ".htm",
            ".csv",
            ".pptx",
            ".pdf",
            ".docx",
            ".doc",
            ".xlsx",
            ".xlsm",
        )
        assert set(documents + IMAGE_SUFFIXES + AUDIO_SUFFIXES + VIDEO_SUFFIXES) == set(
            extraction_routes()
        )

    def test_each_kind_goes_to_its_reader(self):
        table = extraction_routes()
        assert table[".pdf"] == "/extract/pdf" and table[".xlsm"] == "/extract/xlsx"
        assert (
            table[".markdown"] == table[".md"] == "/extract/md" and table[".htm"] == "/extract/html"
        )
        assert (
            table[".jpeg"] == "/transcribe/image"
            and table[".flac"] == "/transcribe/audio"
            and table[".mkv"] == "/transcribe/video"
        )

    def test_with_a_prefix_and_gateway_paths_the_paths_are_the_callers(self):
        table = extraction_routes("/acme", "extract/pdf=/files/pdf, transcribe/audio=/media/sound")
        assert table[".pdf"] == "/files/pdf/acme/extract/pdf"
        assert table[".docx"] == "/acme/extract/docx", (
            "a route with no gateway path is under the prefix alone"
        )
        assert table[".wav"] == table[".flac"] == "/media/sound/acme/transcribe/audio"

    def test_the_app_answers_every_path_the_table_names(self, keyed, service):
        """Called directly as well as through the gateway, so one table serves both."""
        listed = "extract/txt=/files/text"
        app = TestClient(create_extraction_app(service, prefix="/acme", gateway_paths=listed))
        path = extraction_routes("/acme", listed)[".txt"]
        assert path == "/files/text/acme/extract/txt"
        assert app.post(path, content=b"hello", headers=SIGNED).text == "hello"


class TestAPictureInADocumentIsDescribedWhereItSits:
    """What a picture shows ends up in the Markdown, and the picture itself is not sent back."""

    def document(self, tmp_path):
        docx = pytest.importorskip("docx")
        pytest.importorskip("PIL")
        import io
        import random

        from PIL import Image

        rng = random.Random(3)
        image = Image.frombytes(
            "RGB", (120, 90), bytes(rng.randrange(256) for _ in range(120 * 90 * 3))
        )
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        document = docx.Document()
        document.add_paragraph("Revenue grew in every region.")
        document.add_picture(io.BytesIO(buffer.getvalue()))
        document.add_paragraph("Unaudited.")
        document.save(tmp_path / "report.docx")
        return (tmp_path / "report.docx").read_bytes()

    def test_with_vision_the_description_is_in_the_reply(self, keyed, service, tmp_path):
        service.describer = lambda data, context: "A bar chart of revenue by region."
        app = TestClient(create_extraction_app(service))
        body = app.post(
            "/extract/docx",
            content=self.document(tmp_path),
            headers={**SIGNED, "X-Filename": "report.docx", "Accept": "application/json"},
        ).json()
        assert "A bar chart of revenue by region." in body["text"]
        assert (
            body["text"].index("Revenue grew")
            < body["text"].index("A bar chart")
            < body["text"].index("Unaudited.")
        )
        assert set(body) == {"text", "pages", "headings", "metadata", "segments", "figures"}
        # Descriptions travel, and where each figure is; pictures do not.
        assert all(
            set(info) <= {"caption", "src", "described", "described_by"}
            for _at, info in body["figures"]
        )

    def test_without_vision_the_pictures_are_not_opened(self, keyed, service, tmp_path):
        """A file with no describer to hand is read the regular way, and costs nothing more."""
        app = TestClient(create_extraction_app(service))
        text = app.post(
            "/extract/docx",
            content=self.document(tmp_path),
            headers={**SIGNED, "X-Filename": "report.docx"},
        ).text
        assert "Figure" not in text and "Revenue grew" in text and "Unaudited." in text


class TestMaskingOnRequest:
    """``?mask=1`` on a reading route, and ``/mask`` for any text, with the deployment's engine and the patterns after it."""

    @pytest.fixture
    def masking(self, keyed, service, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_MASKING_ENGINE", "regex")
        monkeypatch.setenv("VECTRIXDB_MASKING_LANGUAGES", "en,fr")
        return TestClient(create_extraction_app(service))

    def test_a_document_comes_back_masked_when_asked_and_as_read_when_not(self, masking):
        body = b"Call Ada on 416-555-0199, SSN 078-05-1120, key AKIAIOSFODNN7EXAMPLE."
        plain = masking.post("/extract/txt", content=body, headers=SIGNED)
        assert plain.status_code == 200 and "078-05-1120" in plain.text, "not asked, not masked"
        shown = masking.post("/extract/txt?mask=1", content=body, headers=SIGNED)
        assert (
            shown.status_code == 200
            and shown.text.strip() == "Call Ada on •••-•••-0199, SSN [SSN], key [API_KEY]."
        )
        about = json.loads(shown.headers["x-masking"])
        assert (
            about["engine"] == "regex"
            and about["counts"] == {"phone": 1, "ssn": 1, "api_key": 1}
            and about["score"] == 1.0
        )
        as_json = masking.post(
            "/extract/txt?mask=1&types=ssn",
            content=body,
            headers={**SIGNED, "Accept": "application/json"},
        ).json()
        assert as_json[
            "text"
        ].strip() == "Call Ada on 416-555-0199, SSN [SSN], key AKIAIOSFODNN7EXAMPLE." and as_json[
            "masking"
        ]["counts"] == {"ssn": 1}
        assert "text" not in as_json["masking"], "the text is said once"

    def test_any_text_through_the_mask_route(self, masking):
        said = masking.post(
            "/mask",
            json={
                "text": "ada@example.com, IBAN GB82 WEST 1234 5698 7654 32",
                "types": "all",
                "language": "fr",
            },
            headers=SIGNED,
        )
        assert said.status_code == 200, said.text
        assert (
            said.json()["text"] == "a•••@example.com, IBAN [IBAN]"
            and said.json()["language"] == "fr"
            and said.json()["regex_only"] is False
        )
        bad = masking.post("/mask", json={"text": "x", "types": "hairstyle"}, headers=SIGNED)
        assert bad.status_code == 422 and "not a type" in bad.json()["message"]
        assert masking.post("/mask", json={"text": "x"}).status_code == 401, (
            "the door is the same one"
        )

    def test_the_health_reply_says_which_engine_loaded(self, masking):
        said = masking.get("/health").json()["masking"]
        assert (
            said["engine"] == "regex"
            and said["languages"] == {"en": True, "fr": True}
            and said["patterns_last"] is True
        )

    def test_a_bad_engine_setting_stops_the_app_at_start(self, keyed, service, monkeypatch):
        from vectrixdb.exceptions import ConfigurationError

        monkeypatch.setenv("VECTRIXDB_MASKING_ENGINE", "language")
        monkeypatch.delenv("AZURE_LANGUAGE_ENDPOINT", raising=False)
        with pytest.raises(ConfigurationError, match="AZURE_LANGUAGE_ENDPOINT"):
            create_extraction_app(service)
