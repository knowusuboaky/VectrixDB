"""Long files read in pieces, and put back together so every citation points where it did.

Every reader here is a fake, and so is ffmpeg: what ffmpeg prints is held as
text it really prints, and the sound it would make is a WAV written here. The
cutting of that WAV and of a PDF is real.
"""

from __future__ import annotations

import io
import random
import time
import wave

import pytest

from vectrixdb.exceptions import ExtractionError
from vectrixdb.extract.batches import Batched, batched, cut_points, parse_duration, parse_silences
from vectrixdb.extract.engines import segments_to_document
from vectrixdb.ingest import LoadedDocument


def sound(seconds: float, rate: int = 8000) -> bytes:
    """A mono WAV this many seconds long. What it sounds like does not matter here."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(int(seconds * rate) * 2))
    return buffer.getvalue()


def seconds_of(data: bytes) -> float:
    with wave.open(io.BytesIO(data), "rb") as heard:
        return heard.getnframes() / heard.getframerate()


class Ffmpeg:
    """What FfmpegSound would say, without running it."""

    def __init__(self, seconds, pauses=(), made=None):
        self.seconds = seconds
        self.pauses = list(pauses)
        self.made = seconds if made is None else made
        self.converted = []

    def duration(self, path):
        return self.seconds

    def to_wav(self, path, wav):
        self.converted.append(path)
        with open(wav, "wb") as out:
            out.write(sound(self.made))

    def silences(self, wav):
        return self.pauses


class Reader:
    """A reader like the extraction service: a recording comes back as timed phrases, a PDF as pages."""

    label = "https://extract.example.com"

    def __init__(self, failures=None, slow=None):
        self.calls = []
        self.failures = dict(failures or {})
        self.slow = dict(slow or {})

    def suffixes(self):
        return [".pdf", ".wav", ".mp3", ".mp4", ".docx"]

    def __call__(self, data, name, source=None):
        self.calls.append((name, data, source))
        if self.failures.get(name):
            raise self.failures[name].pop(0)
        time.sleep(self.slow.get(name, 0))
        if name.endswith(".pdf"):
            return self.pages(data, name)
        if name.endswith(".wav"):
            length = seconds_of(data)
            return segments_to_document(
                [(0.0, length / 2, f"{name} begins"), (length / 2, length, f"{name} ends")]
            )
        return LoadedDocument(text=f"all of {name}", metadata={"filename": name})

    def pages(self, data, name):
        import pypdf

        count = len(pypdf.PdfReader(io.BytesIO(data)).pages)
        texts = [f"Page {n} of {name}" for n in range(1, count + 1)]
        offsets, offset = [], 0
        for n, text in enumerate(texts, start=1):
            offsets.append((offset, n))
            offset += len(text) + 2
        return LoadedDocument(
            text="\n\n".join(texts),
            pages=offsets,
            metadata={"ocr_pages": [1], "pages_ocr": 1, "filename": name},
        )


def a_pdf(pages: int) -> bytes:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# ================================================================ ffmpeg says ===


class TestWhatFfmpegSays:
    def test_the_length_of_the_input(self):
        said = "Input #0, wav, from 'call.wav':\n  Duration: 01:02:03.50, bitrate: 256 kb/s\n"
        assert parse_duration(said) == 3723.5

    def test_no_length_is_none(self):
        assert parse_duration("  Duration: N/A, start: 0.000000") is None
        assert parse_duration("") is None

    def test_the_pauses(self):
        said = (
            "[silencedetect @ 0x1] silence_start: -0.01\n"
            "[silencedetect @ 0x1] silence_end: 0.52 | silence_duration: 0.53\n"
            "size=N/A time=00:00:30.00 bitrate=N/A\n"
            "[silencedetect @ 0x1] silence_start: 594.25\n"
            "[silencedetect @ 0x1] silence_end: 596.75 | silence_duration: 2.5\n"
            "[silencedetect @ 0x1] silence_start: 1200.5\n"
        )
        assert parse_silences(said) == [(0.0, 0.52), (594.25, 596.75)], (
            "a pause still open at the end is not one"
        )


# ================================================================ where to cut ===


class TestWhereToCut:
    def test_at_the_pause_nearest_each_mark(self):
        assert cut_points(1250, 600, [(590, 596), (1210, 1212)]) == [593.0, 1211.0]

    def test_the_longest_pause_near_a_mark_wins(self):
        """A long pause is more likely the end of a sentence than a short one."""
        assert cut_points(900, 600, [(540, 541), (620, 624)]) == [622.0]

    def test_with_no_pause_near_a_mark_the_cut_is_the_mark(self):
        assert cut_points(1300, 600, [(100, 102), (900, 901)]) == [600.0, 1200.0]

    def test_sound_that_fits_is_not_cut(self):
        assert cut_points(600, 600, [(300, 301)]) == []

    def test_no_piece_is_ever_much_longer_than_asked(self):
        rng = random.Random(4)
        pauses = sorted(
            (t, t + rng.uniform(0.3, 2.0)) for t in (rng.uniform(0, 3600) for _ in range(400))
        )
        bounds = [0.0, *cut_points(3600, 600, pauses), 3600.0]
        assert all(0 < b - a <= 600 * 1.2 + 2 for a, b in zip(bounds, bounds[1:]))


# ======================================================================= sound ===


class TestSound:
    def test_a_short_recording_is_one_call_as_it_came(self):
        reader, ffmpeg = Reader(), Ffmpeg(seconds=2.0)
        doc = batched(reader, minutes=0.05, sound=ffmpeg)(b"the mp3 as it came", "call.mp3")
        assert [(name, data) for name, data, _ in reader.calls] == [
            ("call.mp3", b"the mp3 as it came")
        ]
        assert ffmpeg.converted == [] and doc.text == "all of call.mp3"

    def test_a_long_one_is_cut_at_its_pauses_and_put_back_in_time(self):
        """Three-second pieces, cut at the pauses half a second before each mark rather than at the marks."""
        reader = Reader()
        ffmpeg = Ffmpeg(seconds=8.0, pauses=[(2.4, 2.6), (5.4, 5.6), (7.0, 7.1)])
        doc = batched(reader, minutes=0.05, sound=ffmpeg)(b"eight seconds", "call.mp3")
        assert [name for name, _data, _ in reader.calls] == [
            "call.part1.wav",
            "call.part2.wav",
            "call.part3.wav",
        ]
        assert [round(seconds_of(data), 2) for _name, data, _ in reader.calls] == [2.5, 3.0, 2.5]
        assert [(round(a, 2), text) for a, _b, text in doc.segments] == [
            (0.0, "call.part1.wav begins"),
            (1.25, "call.part1.wav ends"),
            (2.5, "call.part2.wav begins"),
            (4.0, "call.part2.wav ends"),
            (5.5, "call.part3.wav begins"),
            (6.75, "call.part3.wav ends"),
        ]
        assert doc.metadata["filename"] == "call.mp3" and doc.metadata["pieces"] == 3
        assert doc.metadata["duration_seconds"] == 8.0

    def test_the_minutes_are_still_the_pages(self):
        """Put back through the same builder, so minute 3 is page 3 whichever piece said it."""
        reader, ffmpeg = Reader(), Ffmpeg(seconds=150.0, made=150.0)
        doc = batched(reader, minutes=1, sound=ffmpeg)(b"x", "talk.wav")
        assert [n for _o, n in doc.pages] == [1, 2, 3]

    def test_a_big_short_file_goes_as_one_small_wav(self):
        """Too big to send as it is, short enough for one call: its sound alone is small."""
        reader, ffmpeg = Reader(), Ffmpeg(seconds=2.0)
        batched(reader, minutes=0.05, max_bytes=10, sound=ffmpeg)(
            b"more than ten bytes", "call.wav"
        )
        assert [name for name, _data, _ in reader.calls] == ["call.part1.wav"] and ffmpeg.converted

    def test_a_video_is_its_sound(self):
        reader, ffmpeg = Reader(), Ffmpeg(seconds=8.0, pauses=[(2.9, 3.1), (5.9, 6.1)])
        doc = batched(reader, minutes=0.05, sound=ffmpeg)(b"a film", "town-hall.mp4")
        assert ffmpeg.converted and all(name.endswith(".wav") for name, _data, _ in reader.calls)
        assert doc.metadata["video"] is True and doc.metadata["filename"] == "town-hall.mp4"

    def test_the_pieces_come_back_in_order_whichever_answers_first(self):
        reader = Reader(slow={"call.part1.wav": 0.2})
        ffmpeg = Ffmpeg(seconds=8.0, pauses=[(2.9, 3.1), (5.9, 6.1)])
        doc = batched(reader, minutes=0.05, at_once=3, sound=ffmpeg)(b"x", "call.mp3")
        assert [text for _a, _b, text in doc.segments][0] == "call.part1.wav begins"


# ======================================================================= PDFs ===


class TestPdf:
    def test_a_long_one_is_read_twenty_pages_at_a_time_and_numbered_as_one(self):
        reader = Reader()
        doc = batched(reader)(a_pdf(45), "report.pdf")
        # The batches are read side by side, so the calls arrive in whichever
        # order they finish; the pages must still come back as one numbering.
        called = sorted(
            (name for name, _data, _ in reader.calls),
            key=lambda name: int(name.split("-")[1].split(".")[0]),
        )
        assert called == ["report.pages-1.pdf", "report.pages-21.pdf", "report.pages-41.pdf"]
        assert [n for _o, n in doc.pages] == list(range(1, 46))
        for offset, number in doc.pages:
            piece_page = (number - 1) % 20 + 1
            assert doc.text[offset:].startswith(f"Page {piece_page} of report.pages-")

    def test_what_each_piece_says_about_its_pages_is_moved_on_too(self):
        doc = batched(Reader())(a_pdf(45), "report.pdf")
        assert doc.metadata["ocr_pages"] == [1, 21, 41] and doc.metadata["pages_ocr"] == 3
        assert (
            doc.metadata["pages"] == 45
            and doc.metadata["pieces"] == 3
            and doc.metadata["filename"] == "report.pdf"
        )

    def test_a_short_one_is_one_call_as_it_came(self):
        reader, data = Reader(), a_pdf(12)
        batched(reader)(data, "brief.pdf")
        assert [(name, sent) for name, sent, _ in reader.calls] == [("brief.pdf", data)]

    def test_one_that_cannot_be_opened_here_is_still_the_readers_to_try(self):
        pytest.importorskip("pypdf")
        reader = Reader()
        reader.pages = lambda data, name: LoadedDocument(text="read anyway")
        assert batched(reader)(b"%PDF-1.7 not really", "odd.pdf").text == "read anyway"

    def test_a_piece_too_big_to_send_is_halved(self):
        reader = Reader()
        batched(reader, pages=20, max_bytes=len(a_pdf(6)))(a_pdf(12), "report.pdf")
        sizes = [name for name, _data, _ in reader.calls]
        assert len(sizes) > 1 and sizes[0] == "report.pages-1.pdf"


# ==================================================================== retries ===


class TestAPieceThatFails:
    def test_a_failure_on_the_readers_side_is_tried_again_alone(self):
        busy = ExtractionError("/extract/pdf answered 503", route="/extract/pdf", status=503)
        reader = Reader(failures={"report.pages-21.pdf": [busy]})
        doc = Batched(reader, pause=0)(a_pdf(45), "report.pdf")
        asked = [name for name, _data, _ in reader.calls]
        assert asked.count("report.pages-21.pdf") == 2 and asked.count("report.pages-1.pdf") == 1
        assert [n for _o, n in doc.pages] == list(range(1, 46))

    def test_no_answer_at_all_is_tried_again(self):
        gone = ExtractionError("/extract/pdf could not be reached", route="/extract/pdf")
        reader = Reader(failures={"report.pages-1.pdf": [gone, gone]})
        Batched(reader, pause=0)(a_pdf(45), "report.pdf")
        assert [name for name, _data, _ in reader.calls].count("report.pages-1.pdf") == 3

    def test_a_refusal_is_not_and_says_which_piece(self):
        refused = ExtractionError("/extract/pdf answered 413", route="/extract/pdf", status=413)
        reader = Reader(failures={"report.pages-21.pdf": [refused]})
        with pytest.raises(ExtractionError, match="piece 2 of 3 of report.pdf") as caught:
            Batched(reader, pause=0, at_once=1)(a_pdf(45), "report.pdf")
        assert caught.value.status == 413
        assert [name for name, _data, _ in reader.calls].count("report.pages-21.pdf") == 1


# =================================================================== the rest ===


class TestEverythingElse:
    def test_a_word_document_is_one_call_as_it_came(self):
        reader = Reader()
        doc = batched(reader)(
            b"a docx", "notes.docx", source="https://x.blob.core.windows.net/notes.docx"
        )
        assert reader.calls == [
            ("notes.docx", b"a docx", "https://x.blob.core.windows.net/notes.docx")
        ]
        assert doc.text == "all of notes.docx"

    def test_it_reads_what_its_reader_reads_and_says_it_cuts(self):
        wrapped = batched(Reader())
        assert wrapped.suffixes() == [".docx", ".mp3", ".mp4", ".pdf", ".wav"]
        assert wrapped.label == "https://extract.example.com, in pieces"

    @pytest.mark.parametrize("bad", [{"minutes": 0}, {"pages": 0}, {"at_once": 0}])
    def test_a_piece_of_nothing_is_refused(self, bad):
        with pytest.raises(ValueError):
            batched(Reader(), **bad)

    def test_no_reader_is_no_reader(self):
        """So wrapping a service that is not set up gives what the service gave: nothing."""
        assert batched(None) is None


# ================================================================ the package ===


def test_ffmpeg_is_an_extra_of_its_own():
    """So a host that has a service transcribe gets ffmpeg without Whisper, which is far bigger."""
    from pathlib import Path

    tomllib = pytest.importorskip("tomllib")
    extras = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["optional-dependencies"]
    assert extras["ffmpeg"] == ["imageio-ffmpeg>=0.4.9"]
    assert "vectrixdb[ffmpeg]" in extras["video"], "video is ffmpeg and a local transcriber"


def test_a_missing_ffmpeg_names_that_extra(monkeypatch):
    import sys

    from vectrixdb.exceptions import DependencyError
    from vectrixdb.extract import batches

    # Not installed, and none on the path either.
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
    monkeypatch.setattr(batches.shutil, "which", lambda name: None)
    with pytest.raises(DependencyError, match=r"vectrixdb\[ffmpeg\]"):
        batches.FfmpegSound()._ffmpeg()


# ============================================================ the real ffmpeg ===


def _ffmpeg_here() -> bool:
    try:
        import imageio_ffmpeg  # noqa: F401

        return True
    except ImportError:
        import shutil

        return shutil.which("ffmpeg") is not None


@pytest.mark.skipif(not _ffmpeg_here(), reason="no ffmpeg here: pip install vectrixdb[ffmpeg]")
class TestTheRealFfmpeg:
    """The half the fakes stand in for, against the program itself, when it is installed."""

    def talk(self, tmp_path):
        """Eight seconds at 16 kHz: a tone where somebody speaks, nothing where they pause."""
        import math

        rate, frames, sample = 16000, bytearray(), 0
        for seconds, speaking in (
            (2.4, True),
            (0.4, False),
            (2.5, True),
            (0.4, False),
            (2.3, True),
        ):
            for _ in range(int(seconds * rate)):
                value = int(12000 * math.sin(2 * math.pi * 440 * sample / rate)) if speaking else 0
                frames += value.to_bytes(2, "little", signed=True)
                sample += 1
        path = tmp_path / "talk.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(bytes(frames))
        return path

    def test_it_reads_the_length_and_finds_the_pauses(self, tmp_path):
        from vectrixdb.extract.batches import FfmpegSound

        path, ffmpeg = self.talk(tmp_path), FfmpegSound(pause=0.3)
        assert ffmpeg.duration(str(path)) == pytest.approx(8.0, abs=0.05)
        pauses = ffmpeg.silences(str(path))
        assert [(round(a, 1), round(b, 1)) for a, b in pauses] == [(2.4, 2.8), (5.3, 5.7)]

    def test_its_sound_is_sixteen_kilohertz_and_one_channel(self, tmp_path):
        from vectrixdb.extract.batches import FfmpegSound

        wav = tmp_path / "sound.wav"
        FfmpegSound().to_wav(str(self.talk(tmp_path)), str(wav))
        with wave.open(str(wav), "rb") as heard:
            assert heard.getframerate() == 16000 and heard.getnchannels() == 1

    def test_a_recording_is_cut_at_its_real_pauses(self, tmp_path):
        from vectrixdb.extract.batches import FfmpegSound

        path = self.talk(tmp_path)
        reader = Reader()
        doc = batched(reader, minutes=0.05, sound=FfmpegSound(pause=0.3))(
            path.read_bytes(), "talk.wav"
        )
        assert [round(seconds_of(data), 1) for _name, data, _ in reader.calls] == [2.6, 2.9, 2.5]
        assert [round(a, 1) for a, _b, _t in doc.segments][::2] == [0.0, 2.6, 5.5]
