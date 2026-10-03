"""Timed speech: every phrase keeps when it was said, and a transcript can be written for a person.

The document a collection chunks is one paragraph a minute, cited by minute;
that does not change. What is new is that the phrases and their times are
kept beside it, survive the kept Markdown, and can be written out the way the
reference server writes a transcript. No engine here calls anything.
"""

from __future__ import annotations

import pytest

from vectrixdb.extract.engines import Whisper, _clock, segments_to_document, transcript_markdown
from vectrixdb.ingest import LoadedDocument, describe_figures

PHRASES = [
    (0.0, 4.2, "Welcome to the quarterly results."),
    (4.2, 9.0, "Revenue grew in every region."),
    (61.0, 65.5, "Costs were flat."),
]


class TestEveryPhraseKeepsItsTime:
    def test_segments_to_document_keeps_them(self):
        doc = segments_to_document(PHRASES, 60.0)
        assert doc.segments == PHRASES

    def test_the_text_a_collection_chunks_is_unchanged(self):
        """Still a paragraph a minute: that is what a chunk and a citation want."""
        doc = segments_to_document(PHRASES, 60.0)
        assert (
            doc.text
            == "Welcome to the quarterly results. Revenue grew in every region.\n\nCosts were flat."
        )
        assert doc.pages == [(0, 1), (65, 2)]

    def test_silence_and_stray_spaces_are_not_phrases(self):
        doc = segments_to_document([(0.0, 1.0, "  "), (1.0, 3.0, "two   spaces\nand a line")], 60.0)
        assert doc.segments == [(1.0, 3.0, "two spaces and a line")]

    def test_they_are_not_metadata(self):
        """Metadata is copied onto every chunk; an hour of phrases on each would be most of the index."""
        doc = segments_to_document(PHRASES, 60.0)
        assert "segments" not in doc.metadata


class TestTheyAreKept:
    def test_through_the_markdown_a_collection_keeps(self):
        doc = segments_to_document(PHRASES, 60.0)
        again = LoadedDocument.from_markdown(doc.to_markdown())
        assert again.segments == PHRASES and again.text == doc.text

    def test_a_document_that_is_not_a_recording_writes_nothing_new(self):
        """Every other file's front matter stays exactly as it was."""
        assert "segments" not in LoadedDocument(text="A report.").to_markdown()

    def test_describing_a_figure_does_not_lose_them(self):
        """Times are not offsets, so text moving does not move when anything was said."""
        doc = LoadedDocument(
            text="[Figure: p1-fig1.png]",
            figures=[(0, {"src": "p1-fig1.png"})],
            images={"p1-fig1.png": b"\x89PNG\r\n\x1a\n" + bytes(4000)},
            segments=PHRASES,
        )
        after = describe_figures(doc, lambda image, context: "A bar chart.", skip_decorative=False)
        assert after.text != doc.text and after.segments == PHRASES


class TestTheClock:
    @pytest.mark.parametrize(
        "seconds, shown",
        [
            (0, "0:00"),
            (4.2, "0:04"),
            (59.9, "0:59"),
            (61, "1:01"),
            (3599, "59:59"),
            (3600, "1:00:00"),
            (3725, "1:02:05"),
            (None, "0:00"),
        ],
    )
    def test_minutes_and_seconds_then_hours(self, seconds, shown):
        assert _clock(seconds) == shown


class TestTheTranscriptAPersonReads:
    def recording(self, **about):
        doc = segments_to_document(PHRASES, 60.0)
        doc.metadata.update(about)
        return doc

    def test_a_youtube_video_is_headed_by_its_title(self):
        said = transcript_markdown(
            self.recording(kind="youtube", title="Quarterly results, explained")
        )
        assert said.startswith("# YouTube: Quarterly results, explained\n")

    def test_any_other_recording_by_its_file(self):
        said = transcript_markdown(self.recording(filename="board-call.wav"))
        assert said.startswith("# Transcript: board-call.wav\n")

    def test_every_phrase_is_timed_from_start_to_end(self):
        said = transcript_markdown(self.recording(filename="x.wav"))
        assert "**[0:00 → 0:04]** Welcome to the quarterly results." in said
        assert "**[1:01 → 1:05]** Costs were flat." in said

    def test_the_full_text_comes_before_the_segments(self):
        said = transcript_markdown(self.recording(filename="x.wav"))
        assert said.index("## Full Text") < said.index("## Segments")

    def test_what_is_not_known_is_left_out_rather_than_written_unknown(self):
        said = transcript_markdown(self.recording(filename="x.wav"))
        assert "- Channel" not in said and "Unknown" not in said and "- URL" not in said

    def test_a_file_path_is_not_a_url(self):
        said = transcript_markdown(self.recording(filename="x.wav", source="C:/calls/x.wav"))
        assert "- URL" not in said

    def test_a_recording_duration_is_used_when_there_is_no_video_one(self):
        """segments_to_document records duration_seconds; a video's info gives duration."""
        assert "- Duration: 1:05" in transcript_markdown(self.recording(filename="x.wav"))

    def test_no_speech_says_so_and_has_no_segments(self):
        said = transcript_markdown(segments_to_document([], 60.0))
        assert "_(no speech detected)_" in said and "## Segments" not in said


class TestWhisperListensInItsOwnLanguage:
    """The model is kept on the instance; the language is read at every call.

    It used to be captured when the model loaded, so a copy made to listen in
    French shared the loaded model and went on transcribing in the original's
    language, with no error.
    """

    def test_a_copy_uses_its_own_language_and_the_same_model(self):
        import copy

        heard = []

        class Model:
            def transcribe(self, path, language=None):
                heard.append(language)
                return [], None

        english = Whisper(language="en")
        english._loaded = Model()
        french = copy.copy(english)
        french.language = "fr"
        english.transcribe_file("a.wav")
        french.transcribe_file("a.wav")
        assert heard == ["en", "fr"]
        assert french._loaded is english._loaded, "the model is loaded once, not once a language"

    def test_an_engine_of_your_own_is_still_used_as_given(self):
        whisper = Whisper(engine=lambda path: [(0.0, 1.0, "hello")])
        assert whisper.transcribe_file("a.wav") == [(0.0, 1.0, "hello")]
