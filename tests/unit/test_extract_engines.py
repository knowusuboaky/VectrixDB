"""The engines for what is not text yet, each with its moving part faked.

A real OCR or speech model is downloaded and slow, and a cloud service is a
live service, so the tests here stand in for every one of them: an engine
that returns lines, a client that returns the shape the service documents.
What is tested is everything around the model: page order, which pages of a
PDF get read, minutes as pages, the job polling, and the error a missing
package produces.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from vectrixdb.exceptions import DependencyError, ExtractionError
from vectrixdb.extract import load_url
from vectrixdb.extract.engines import (
    AzureDocumentIntelligence,
    AzureSpeech,
    RapidOcr,
    Textract,
    Transcribe,
    Video,
    Whisper,
    _moments,
    segments_to_document,
)
from vectrixdb.ingest import LoadedDocument, load


class TestMinutesAsPages:
    def test_each_minute_is_a_page_and_a_silent_one_still_counts(self):
        doc = segments_to_document(
            [
                (0.0, 4.0, "Good morning."),
                (30.0, 35.0, "Thanks for calling."),
                (185.0, 190.0, "About the October payment."),
            ]
        )
        assert doc.text == "Good morning. Thanks for calling.\n\nAbout the October payment."
        assert doc.pages == [(0, 1), (doc.text.index("About"), 4)]
        assert doc.page_at(doc.text.index("October")) == 4
        assert doc.metadata["duration_seconds"] == 190.0 and doc.metadata["transcript"] is True

    def test_nothing_said(self):
        doc = segments_to_document([])
        assert doc.text == "" and doc.pages == []


class TestRapidOcr:
    def test_an_image_is_read_whole(self):
        doc = RapidOcr(engine=lambda image: ["INVOICE 1042", "Total due: 310.00"])(
            b"png", "invoice.png"
        )
        assert doc.text == "INVOICE 1042\nTotal due: 310.00" and doc.metadata["ocr"] is True

    def test_only_the_scanned_pages_of_a_pdf_are_read(self):
        read = []

        def engine(image):
            read.append(image)
            return ["Scanned covenant schedule."]

        reader = RapidOcr(
            engine=engine,
            rasterize=lambda pdf: [b"page-1-png", b"page-2-png", b"page-3-png"],
            page_text=lambda pdf: [
                "The first page has a real text layer in it.",
                "  ",
                "The third page does too, plainly enough.",
            ],
        )
        doc = reader(b"%PDF", "mixed.pdf")
        assert read == [b"page-2-png"]
        assert doc.metadata == {"pages": 3, "ocr": True, "pages_ocr": 1, "ocr_pages": [2]}, (
            "and it says which page that was"
        )
        assert (
            doc.page_at(doc.text.index("Scanned")) == 2
            and doc.page_at(doc.text.index("third page")) == 3
        )

    def test_a_pdf_with_text_on_every_page_is_not_drawn_at_all(self):
        def never(pdf):
            raise AssertionError("no page needed reading")

        reader = RapidOcr(
            engine=never,
            rasterize=never,
            page_text=lambda pdf: ["A page of ordinary readable text."] * 2,
        )
        assert reader(b"%PDF", "born-digital.pdf").metadata["pages_ocr"] == 0

    def test_a_pdf_with_no_text_layer_is_read_throughout(self):
        reader = RapidOcr(
            engine=lambda image: [image.decode()],
            rasterize=lambda pdf: [b"First scanned page.", b"Second scanned page."],
            page_text=lambda pdf: [],
        )
        doc = reader(b"%PDF", "scan.pdf")
        assert (
            doc.text == "First scanned page.\n\nSecond scanned page."
            and doc.metadata["pages_ocr"] == 2
        )

    def test_a_missing_package_names_the_extra(self, tmp_path, monkeypatch):
        import builtins

        real = builtins.__import__

        def no_rapidocr(name, *args, **kwargs):
            if name.startswith("rapidocr"):
                raise ImportError(name)
            return real(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_rapidocr)
        monkeypatch.setattr("vectrixdb.ingest._LOCAL_ENGINES", {})
        picture = tmp_path / "page.png"
        picture.write_bytes(b"\x89PNG")
        with pytest.raises(DependencyError, match=r"pip install vectrixdb\[ocr\]"):
            load(picture)


class TestWhisper:
    def test_audio_becomes_minutes(self):
        heard = []

        def engine(path):
            heard.append(path)
            return [
                (2.0, 5.0, " The customer asked to defer. "),
                (61.0, 66.0, "Approved for ninety days."),
            ]

        doc = Whisper(engine=engine)(b"RIFF", "call.wav")
        assert heard[0].endswith(".wav")
        assert doc.page_at(doc.text.index("Approved")) == 2
        assert doc.text.startswith("The customer asked to defer.")

    def test_a_missing_package_names_the_extra(self, monkeypatch):
        import builtins

        real = builtins.__import__
        monkeypatch.setattr(
            builtins,
            "__import__",
            lambda name, *a, **k: (
                (_ for _ in ()).throw(ImportError(name))
                if name == "faster_whisper"
                else real(name, *a, **k)
            ),
        )
        with pytest.raises(DependencyError, match=r"pip install vectrixdb\[asr\]"):
            Whisper()(b"RIFF", "call.wav")


class TestVideo:
    def test_the_sound_goes_to_the_audio_extractor(self):
        def ffmpeg(video, wav):
            assert video.endswith(".mp4")
            with open(wav, "wb") as fh:
                fh.write(b"the-sound")

        seen = {}

        def audio(data, name):
            seen["data"], seen["name"] = data, name
            return segments_to_document([(0.0, 3.0, "Welcome to the town hall.")])

        doc = Video(audio=audio, ffmpeg=ffmpeg)(b"mp4-bytes", "townhall.mp4")
        assert seen == {"data": b"the-sound", "name": "townhall.wav"}
        assert doc.text == "Welcome to the town hall." and doc.metadata["video"] is True

    @staticmethod
    def sound(video, wav):
        with open(wav, "wb") as fh:
            fh.write(b"the-sound")

    @staticmethod
    def slides(shown):
        """A picture reader that reads each frame's bytes as the slide it shows, and says which it was asked."""
        asked = []

        def read(png, name):
            asked.append(name)
            return LoadedDocument(text=shown[png])

        read.asked = asked
        return read

    def test_what_the_screen_shows_joins_what_is_said_at_its_second(self):
        def audio(data, name):
            return segments_to_document(
                [
                    (0.0, 3.0, "Welcome to the town hall."),
                    (20.0, 24.0, "Revenue grew in every region."),
                ]
            )

        frames = self.slides({b"f0": "Q3 RESULTS", b"f1": "Q3  RESULTS", b"f2": "Revenue up 12%"})
        video = Video(
            audio=audio,
            ffmpeg=self.sound,
            frames=frames,
            grabber=lambda path: [(0.0, b"f0"), (9.5, b"f1"), (18.0, b"f2")],
        )
        doc = video(b"mp4-bytes", "townhall.mp4")
        assert doc.text == (
            "On screen: Q3 RESULTS\nWelcome to the town hall.\nOn screen: Revenue up 12%\nRevenue grew in every region."
        ), (
            "a slide is read before the words said over it, and a slide still showing is not said twice"
        )
        assert frames.asked == ["townhall-0s.png", "townhall-9s.png", "townhall-18s.png"]
        assert doc.metadata["on_screen"] == 2 and doc.metadata["video"] is True
        assert (18.0, 19.0, "On screen: Revenue up 12%") in doc.segments

    def test_a_video_with_no_sound_is_read_for_its_screen(self):
        from vectrixdb.extract.engines import _NoSound

        def silent(video, wav):
            raise _NoSound()

        def never(data, name):
            raise AssertionError("there is no sound to hear")

        frames = self.slides({b"a": "Agenda", b"b": "Thank you"})
        doc = Video(
            audio=never,
            ffmpeg=silent,
            frames=frames,
            grabber=lambda path: [(0.0, b"a"), (75.0, b"b")],
        )(b"mp4", "deck.mp4")
        assert (
            doc.text == "On screen: Agenda\n\nOn screen: Thank you"
            and doc.page_at(doc.text.index("Thank")) == 2
        )
        assert doc.metadata["sound_track"] is False and doc.metadata["speech"] == "none"

    def test_a_frame_that_cannot_be_read_is_passed_over(self):
        def read(png, name):
            if png == b"broken":
                raise RuntimeError("the picture reader refused it")
            return LoadedDocument(text="Closing remarks")

        video = Video(
            audio=lambda d, n: segments_to_document([]),
            ffmpeg=self.sound,
            frames=read,
            grabber=lambda path: [(0.0, b"broken"), (4.0, b"fine")],
        )
        doc = video(b"mp4", "end.mp4")
        assert doc.text == "On screen: Closing remarks" and doc.metadata["speech"] == "none"

    def test_a_reader_missing_its_package_stops_at_the_first_frame(self):
        asked = []

        def read(png, name):
            asked.append(name)
            raise DependencyError("rapidocr-onnxruntime", "ocr")

        video = Video(
            audio=lambda d, n: segments_to_document([(0.0, 2.0, "Hello.")]),
            ffmpeg=self.sound,
            frames=read,
            grabber=lambda path: [(0.0, b"a"), (5.0, b"b"), (9.0, b"c")],
        )
        assert video(b"mp4", "a.mp4").text == "Hello." and len(asked) == 1

    def test_no_frame_is_taken_without_a_reader_or_with_none_allowed(self):
        def never(path):
            raise AssertionError("no frame was wanted")

        said = lambda d, n: segments_to_document([(0.0, 2.0, "Hello.")])  # noqa: E731
        assert Video(audio=said, ffmpeg=self.sound, grabber=never)(b"mp4", "a.mp4").text == "Hello."
        frames = self.slides({})
        assert (
            Video(audio=said, ffmpeg=self.sound, frames=frames, max_frames=0, grabber=never)(
                b"mp4", "a.mp4"
            ).text
            == "Hello."
        )

    def test_at_most_max_frames_are_read(self):
        frames = self.slides({str(i).encode(): f"Slide {i}" for i in range(30)})
        grabber = lambda path: [(float(i * 10), str(i).encode()) for i in range(30)]  # noqa: E731
        Video(
            audio=lambda d, n: segments_to_document([]),
            ffmpeg=self.sound,
            frames=frames,
            max_frames=5,
            grabber=grabber,
        )(b"mp4", "long.mp4")
        assert len(frames.asked) == 5

    def test_the_real_ffmpeg_takes_a_frame_at_each_change(self, tmp_path):
        imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")
        video = tmp_path / "deck.mp4"
        subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=white:s=160x90:d=3",
                "-f",
                "lavfi",
                "-i",
                "color=c=navy:s=160x90:d=3",
                "-f",
                "lavfi",
                "-i",
                "color=c=white:s=160x90:d=3",
                "-filter_complex",
                "[0][1][2]concat=n=3:v=1:a=0",
                "-pix_fmt",
                "yuv420p",
                str(video),
            ],
            check=True,
            capture_output=True,
        )
        taken = Video(audio=lambda d, n: None, frames=lambda png, name: None)._grab(str(video))
        assert [int(seconds) for seconds, _png in taken] == [0, 3, 6], (
            "the start, and each slide once it has settled"
        )
        assert all(png.startswith(b"\x89PNG") for _seconds, png in taken)


class TestTheMomentsAVideoIsLookedAt:
    """Which seconds get a frame, from how much each look at the video changed."""

    def test_every_slide_of_a_deck_and_the_start(self):
        assert _moments([(5.0, 0.027), (7.5, 0.004), (10.0, 0.026)], 15.2, 12) == [
            0.5,
            5.5,
            10.5,
        ], "a flicker is not a slide"

    def test_a_transition_is_one_moment_at_its_largest(self):
        assert _moments([(20.0, 0.1), (20.5, 0.6), (21.0, 0.2)], 60.0, 12) == [0.5, 21.0]

    def test_footage_that_never_stops_moving_is_covered_end_to_end(self):
        changes = [(t / 2, 0.05 + (t % 7) / 100) for t in range(1, 150)]
        taken = _moments(changes, 75.0, 12)
        assert len(taken) == 12 and taken[0] == 0.5 and taken[-1] > 68, (
            "a frame in every stretch of it, not twelve from its first seconds"
        )

    def test_the_last_frame_is_inside_the_video(self):
        assert _moments([(14.9, 0.5)], 15.0, 12) == [0.5, 14.9]

    def test_none_asked_for_takes_none(self):
        assert _moments([(5.0, 0.5)], 10.0, 0) == []


# ------------------------------------------------------------------- azure ---


class _Poller:
    def __init__(self, result):
        self._result = result

    def result(self):
        return self._result


class _FakeDI:
    def __init__(self, result):
        self.result, self.calls = result, []

    def begin_analyze_document(self, model, body, **kwargs):
        self.calls.append((model, body, kwargs))
        return _Poller(self.result)


class TestAzureDocumentIntelligence:
    def test_markdown_with_page_spans(self):
        content = "# Terms\n\nPayment in thirty days.\n\n| Region | Fee |\n|---|---|\n| EMEA | 2% |\n\n## Late fees\n\nInterest accrues."
        result = {
            "content": content,
            "pages": [
                {"pageNumber": 1, "spans": [{"offset": 0, "length": content.index("## Late")}]},
                {"pageNumber": 2, "spans": [{"offset": content.index("## Late"), "length": 30}]},
            ],
        }
        client = _FakeDI(result)
        doc = AzureDocumentIntelligence(client)(b"%PDF", "scan.pdf")
        model, body, kwargs = client.calls[0]
        assert model == "prebuilt-layout" and body == b"%PDF"
        assert (
            kwargs["output_content_format"] == "markdown"
            and kwargs["string_index_type"] == "unicodeCodePoint"
        )
        assert "Region: EMEA; Fee: 2%" in doc.text
        assert (
            doc.page_at(doc.text.index("Interest accrues")) == 2
            and doc.page_at(doc.text.index("Region")) == 1
        )
        assert [h[1] for h in doc.headings] == ["Terms", "Late fees"]


class TestAzureSpeech:
    def test_phrases_with_offsets_become_minutes(self):
        sent = {}

        def transport(method, url, headers, body, timeout):
            sent.update(method=method, url=url, headers=headers, body=body)
            reply = {
                "phrases": [
                    {
                        "offsetMilliseconds": 1000,
                        "durationMilliseconds": 2000,
                        "text": "Good morning.",
                    },
                    {
                        "offsetMilliseconds": 125000,
                        "durationMilliseconds": 1500,
                        "text": "About the deferral.",
                    },
                ]
            }
            return 200, {"Content-Type": "application/json"}, json.dumps(reply).encode()

        speech = AzureSpeech(
            "https://eastus.api.cognitive.microsoft.com/",
            key="secret",
            locales=["en-CA"],
            transport=transport,
        )
        doc = speech(b"RIFFDATA", "call.wav")
        assert (
            sent["url"]
            == "https://eastus.api.cognitive.microsoft.com/speechtotext/transcriptions:transcribe?api-version=2024-11-15"
        )
        assert sent["headers"]["Ocp-Apim-Subscription-Key"] == "secret"
        assert b'{"locales": ["en-CA"]}' in sent["body"] and b"RIFFDATA" in sent["body"]
        assert doc.page_at(doc.text.index("deferral")) == 3
        assert "secret" not in repr(speech)

    def test_a_refusal_names_the_status(self):
        speech = AzureSpeech(
            "https://x.example.test", key="k", transport=lambda *a: (401, {}, b"bad key")
        )
        with pytest.raises(ExtractionError, match="answered 401") as caught:
            speech(b"x", "call.wav")
        assert caught.value.status == 401

    def test_https_only(self):
        with pytest.raises(ValueError, match="https address"):
            AzureSpeech("http://plain.example.test", key="k")

    @staticmethod
    def answering(*phrases, sent=None):
        def transport(method, url, headers, body, timeout):
            if sent is not None:
                sent.append(body)
            return 200, {}, json.dumps({"phrases": list(phrases)}).encode()

        return transport

    def test_voices_are_told_apart_each_turn_on_a_line(self):
        sent = []
        transport = self.answering(
            {
                "offsetMilliseconds": 0,
                "durationMilliseconds": 2000,
                "text": "Thanks for calling.",
                "speaker": 1,
                "locale": "en-US",
            },
            {
                "offsetMilliseconds": 2500,
                "durationMilliseconds": 3000,
                "text": "My card was charged twice.",
                "speaker": 2,
                "locale": "en-US",
            },
            {
                "offsetMilliseconds": 6000,
                "durationMilliseconds": 1500,
                "text": "Merci.",
                "speaker": 2,
                "locale": "fr-CA",
            },
            sent=sent,
        )
        doc = AzureSpeech("https://x.example.test", key="k", speakers=4, transport=transport)(
            b"RIFF", "call.wav"
        )
        assert b'"diarization": {"enabled": true, "maxSpeakers": 4}' in sent[0]
        assert (
            doc.text
            == "Speaker 1: Thanks for calling.\nSpeaker 2: My card was charged twice.\nSpeaker 2: Merci."
        )
        assert doc.metadata["speakers"] == 2
        assert doc.metadata["language"] == "en-US", "the language most of it was spoken in"
        assert doc.segments[1] == (2.5, 5.5, "Speaker 2: My card was charged twice."), (
            "each phrase says who said it"
        )

    def test_one_voice_carries_no_label(self):
        transport = self.answering(
            {
                "offsetMilliseconds": 0,
                "durationMilliseconds": 2000,
                "text": "Good morning.",
                "speaker": 1,
            },
            {
                "offsetMilliseconds": 2000,
                "durationMilliseconds": 2000,
                "text": "Let us begin.",
                "speaker": 1,
            },
        )
        doc = AzureSpeech("https://x.example.test", key="k", speakers=4, transport=transport)(
            b"RIFF", "talk.wav"
        )
        assert doc.text == "Good morning. Let us begin." and "speakers" not in doc.metadata

    def test_voices_are_not_asked_for_unless_wanted(self):
        sent = []
        AzureSpeech("https://x.example.test", key="k", transport=self.answering(sent=sent))(
            b"RIFF", "call.wav"
        )
        assert b"diarization" not in sent[0]

    def test_a_locale_that_cannot_tell_voices_apart_is_asked_again_without(self):
        sent = []

        def transport(method, url, headers, body, timeout):
            sent.append(body)
            if b"diarization" in body:
                return (
                    400,
                    {},
                    b'{"error": {"message": "Diarization is not supported for this locale."}}',
                )
            return (
                200,
                {},
                json.dumps(
                    {
                        "phrases": [
                            {
                                "offsetMilliseconds": 0,
                                "durationMilliseconds": 900,
                                "text": "Bonjour.",
                            }
                        ]
                    }
                ).encode(),
            )

        doc = AzureSpeech(
            "https://x.example.test", key="k", locales=["fr-CA"], speakers=3, transport=transport
        )(b"RIFF", "appel.wav")
        assert len(sent) == 2 and b"diarization" not in sent[1] and doc.text == "Bonjour."

    def test_any_other_refusal_is_not_asked_again(self):
        sent = []

        def transport(method, url, headers, body, timeout):
            sent.append(body)
            return 400, {}, b"the audio is not a format Speech reads"

        with pytest.raises(ExtractionError, match="answered 400"):
            AzureSpeech("https://x.example.test", key="k", speakers=3, transport=transport)(
                b"RIFF", "call.wav"
            )
        assert len(sent) == 1

    def test_a_busy_service_is_asked_again_when_it_says(self):
        answers = [
            (429, {"Retry-After": "7"}, b"slow down"),
            (503, {}, b"busy"),
            (
                200,
                {},
                json.dumps(
                    {
                        "phrases": [
                            {"offsetMilliseconds": 0, "durationMilliseconds": 900, "text": "Hello."}
                        ]
                    }
                ).encode(),
            ),
        ]
        waited = []
        speech = AzureSpeech(
            "https://x.example.test",
            key="k",
            transport=lambda *a: answers.pop(0),
            sleep=waited.append,
        )
        assert speech(b"RIFF", "call.wav").text == "Hello."
        assert waited == [7.0, 4.0], "the service's own wait first, then twice as long each time"

    def test_it_stops_after_its_retries_and_never_waits_long(self):
        waited = []
        speech = AzureSpeech(
            "https://x.example.test",
            key="k",
            retries=2,
            transport=lambda *a: (429, {"Retry-After": "600"}, b"quota"),
            sleep=waited.append,
        )
        with pytest.raises(ExtractionError, match="answered 429") as caught:
            speech(b"RIFF", "call.wav")
        assert waited == [30.0, 30.0] and caught.value.status == 429

    def test_nothing_said_says_so(self):
        doc = AzureSpeech("https://x.example.test", key="k", transport=self.answering())(
            b"RIFF", "silence.wav"
        )
        assert doc.text == "" and doc.metadata["speech"] == "none"


# --------------------------------------------------------------------- aws ---


class _FakeTextract:
    def __init__(self):
        self.calls = []
        self.polls = 0

    def detect_document_text(self, Document):
        self.calls.append(("sync", Document))
        return {
            "Blocks": [
                {"BlockType": "PAGE"},
                {"BlockType": "LINE", "Text": "Total due: 310.00", "Page": 1},
            ]
        }

    def start_document_text_detection(self, DocumentLocation):
        self.calls.append(("start", DocumentLocation))
        return {"JobId": "job-1"}

    def get_document_text_detection(self, JobId, NextToken=None):
        self.polls += 1
        if self.polls == 1:
            return {"JobStatus": "IN_PROGRESS"}
        if NextToken is None:
            return {
                "JobStatus": "SUCCEEDED",
                "NextToken": "more",
                "Blocks": [{"BlockType": "LINE", "Text": "Page one line.", "Page": 1}],
            }
        return {
            "JobStatus": "SUCCEEDED",
            "Blocks": [{"BlockType": "LINE", "Text": "Page two line.", "Page": 2}],
        }


class TestTextract:
    def test_an_image_goes_up_in_the_request(self):
        client = _FakeTextract()
        doc = Textract(client)(b"png-bytes", "receipt.png", source="s3://inbox/receipt.png")
        assert client.calls == [("sync", {"Bytes": b"png-bytes"})]
        assert doc.text == "Total due: 310.00" and doc.pages == [(0, 1)]

    def test_a_pdf_in_s3_is_a_job_that_is_waited_for_and_paged_through(self):
        client, naps = _FakeTextract(), []
        doc = Textract(client, sleep=naps.append)(
            b"ignored", "long.pdf", source="s3://inbox/acme/long.pdf"
        )
        assert client.calls == [
            ("start", {"S3Object": {"Bucket": "inbox", "Name": "acme/long.pdf"}})
        ]
        assert naps == [2.0] and client.polls == 3
        assert doc.page_at(doc.text.index("Page two")) == 2

    def test_a_failed_job_says_why(self):
        class Failing(_FakeTextract):
            def get_document_text_detection(self, JobId, NextToken=None):
                return {"JobStatus": "FAILED", "StatusMessage": "unsupported document"}

        with pytest.raises(ExtractionError, match="ended FAILED: unsupported document"):
            Textract(Failing())(b"x", "bad.pdf", source="s3://inbox/bad.pdf")


class _Body:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class TestTranscribe:
    ITEMS = [
        {
            "type": "pronunciation",
            "start_time": "1.0",
            "end_time": "1.4",
            "alternatives": [{"content": "Good"}],
        },
        {
            "type": "pronunciation",
            "start_time": "1.4",
            "end_time": "1.9",
            "alternatives": [{"content": "morning"}],
        },
        {"type": "punctuation", "alternatives": [{"content": "."}]},
        {
            "type": "pronunciation",
            "start_time": "70.0",
            "end_time": "70.5",
            "alternatives": [{"content": "Deferral"}],
        },
        {
            "type": "pronunciation",
            "start_time": "70.5",
            "end_time": "71.0",
            "alternatives": [{"content": "approved"}],
        },
    ]

    def test_the_job_runs_where_the_media_lies(self):
        started, states = {}, iter(["IN_PROGRESS", "COMPLETED"])

        class Client:
            def start_transcription_job(self, **kwargs):
                started.update(kwargs)

            def get_transcription_job(self, TranscriptionJobName):
                return {"TranscriptionJob": {"TranscriptionJobStatus": next(states)}}

        class S3:
            def get_object(self, Bucket, Key):
                assert Bucket == "out" and Key == started["OutputKey"]
                return {
                    "Body": _Body(json.dumps({"results": {"items": TestTranscribe.ITEMS}}).encode())
                }

        doc = Transcribe(Client(), S3(), "out", sleep=lambda s: None)(
            b"", "call.mp3", source="s3://inbox/call.mp3"
        )
        assert (
            started["Media"] == {"MediaFileUri": "s3://inbox/call.mp3"}
            and started["LanguageCode"] == "en-US"
        )
        assert doc.text == "Good morning.\n\nDeferral approved"
        assert doc.page_at(doc.text.index("Deferral")) == 2

    def test_media_that_is_not_in_s3_is_refused_with_the_way_out(self):
        with pytest.raises(ExtractionError, match="did not come with an s3:// address"):
            Transcribe(object(), object(), "out")(b"bytes", "call.mp3", source="/tmp/call.mp3")


class TestLoadUrl:
    def test_a_page_is_read_as_a_page(self):
        doc = load_url(
            "https://example.test/guide",
            transport=lambda m, u, h, b, t: (
                200,
                {"Content-Type": "text/html"},
                b"<h1>Guide</h1><p>Read me.</p>",
            ),
        )
        assert (
            doc.text == "# Guide\n\nRead me."
            and doc.metadata["source"] == "https://example.test/guide"
        )

    def test_a_named_file_goes_to_whoever_reads_that_suffix(self):
        doc = load_url(
            "https://example.test/files/scan.pdf?sig=abc",
            extractors={".pdf": lambda data, name: f"read {name}, {len(data)} bytes"},
            transport=lambda m, u, h, b, t: (200, {"Content-Type": "application/pdf"}, b"%PDF"),
        )
        assert doc.text == "read scan.pdf, 4 bytes" and doc.metadata["filename"] == "scan.pdf"

    def test_what_it_refuses(self):
        with pytest.raises(ValueError, match="http or https"):
            load_url("file:///etc/passwd")
        with pytest.raises(ExtractionError, match="answered 404"):
            load_url("https://example.test/gone", transport=lambda *a: (404, {}, b""))
