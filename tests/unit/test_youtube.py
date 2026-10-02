"""A YouTube video's words. Nothing is fetched: the downloader and yt-dlp are fakes."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from vectrixdb.exceptions import DependencyError, ExtractionError
from vectrixdb.extract.youtube import is_youtube, load_youtube, video_id
from vectrixdb.ingest import LoadedDocument

VIDEO = "dQw4w9WgXcQ"
INFO = {
    "id": VIDEO,
    "title": "Quarterly results, explained",
    "channel": "TD Bank",
    "duration": 212,
    "upload_date": "20250901",
    "webpage_url": f"https://www.youtube.com/watch?v={VIDEO}",
}


class Downloader:
    """Writes a small file where yt-dlp would, and remembers what it was asked and where."""

    def __init__(self, suffix=".m4a", info=None):
        self.suffix, self.info = suffix, dict(INFO if info is None else info)
        self.asked, self.folder = None, None

    def __call__(self, url, folder):
        self.asked, self.folder = url, folder
        path = Path(folder) / f"{VIDEO}{self.suffix}"
        path.write_bytes(b"sound")
        return str(path), self.info


class Listener:
    """An audio engine that says what it heard."""

    def __init__(self):
        self.heard = []

    def __call__(self, data, name):
        self.heard.append((data, name))
        return LoadedDocument(text="Revenue grew in every region.", pages=[(0, 1)], metadata={"asr": "fake"})


class Timed:
    """An audio engine that answers with timed phrases, through the library's own segments_to_document."""

    # A locale to set, like AzureSpeech's, so it can be asked for a language.
    locales = ("en-US",)

    def __call__(self, data, name):
        from vectrixdb.extract.engines import segments_to_document

        return segments_to_document(
            [(0.0, 4.2, "Welcome to the quarterly results."), (4.2, 9.0, "Revenue grew in every region.")], 60.0
        )


class TestOnlyOneYouTubeVideo:
    @pytest.mark.parametrize(
        "url",
        [
            f"https://youtu.be/{VIDEO}",
            f"https://www.youtube.com/watch?v={VIDEO}",
            f"https://youtube.com/watch?v={VIDEO}&t=42s",
            f"https://m.youtube.com/watch?v={VIDEO}",
            f"https://www.youtube.com/shorts/{VIDEO}",
            f"https://www.youtube.com/live/{VIDEO}",
            f"https://www.youtube.com/embed/{VIDEO}",
            f"http://youtu.be/{VIDEO}",
        ],
    )
    def test_the_ways_a_video_is_addressed(self, url):
        assert video_id(url) == VIDEO and is_youtube(url)

    @pytest.mark.parametrize(
        "url",
        [
            f"https://youtube.com.evil.test/watch?v={VIDEO}",
            f"https://notyoutube.com/watch?v={VIDEO}",
            f"https://evil.test/?u=https://youtu.be/{VIDEO}",
            f"ftp://youtu.be/{VIDEO}",
            "http://169.254.169.254/latest/meta-data/",
        ],
    )
    def test_anything_else_is_refused_before_it_is_fetched(self, url):
        """yt-dlp fetches from a thousand sites and plain addresses; this one only fetches YouTube."""
        assert video_id(url) is None
        with pytest.raises(ValueError, match="not the address of one YouTube video"):
            load_youtube(url, audio=Listener(), download=Downloader())

    def test_a_playlist_is_refused(self):
        """It would fetch, and bill the transcription of, every video in it."""
        url = "https://www.youtube.com/playlist?list=PL12345"
        assert video_id(url) is None
        with pytest.raises(ValueError, match="a playlist"):
            load_youtube(url, audio=Listener(), download=Downloader())

    def test_a_video_inside_a_playlist_is_the_one_video(self):
        fetch = Downloader()
        load_youtube(f"https://www.youtube.com/watch?v={VIDEO}&list=PL12345&index=3", audio=Listener(), download=fetch)
        assert fetch.asked == f"https://www.youtube.com/watch?v={VIDEO}", "no playlist, no tracking, just the video"

    def test_a_malformed_id_is_no_video(self):
        assert video_id("https://youtu.be/short") is None


class TestItIsReadLikeAnyRecording:
    def test_m4a_goes_straight_to_the_audio_engine(self):
        listener = Listener()
        doc = load_youtube(f"https://youtu.be/{VIDEO}", audio=listener, download=Downloader(".m4a"))
        assert listener.heard == [(b"sound", f"{VIDEO}.m4a")]
        assert doc.text == "Revenue grew in every region."

    def test_anything_else_has_its_sound_taken_out_first(self, monkeypatch):
        """Opus in webm is not a file Speech takes, so it goes the way a video file does."""
        used = []

        class FakeVideo:
            def __init__(self, audio=None, ffmpeg=None):
                self.audio = audio

            def __call__(self, data, name):
                used.append(name)
                return self.audio(data, name)

        monkeypatch.setattr("vectrixdb.extract.engines.Video", FakeVideo)
        load_youtube(f"https://youtu.be/{VIDEO}", audio=Listener(), download=Downloader(".webm"))
        assert used == [f"{VIDEO}.webm"]

    def test_the_video_travels_with_its_words(self):
        doc = load_youtube(f"https://youtu.be/{VIDEO}", audio=Listener(), download=Downloader())
        about = doc.metadata
        assert about["kind"] == "youtube" and about["youtube_id"] == VIDEO
        assert about["title"] == "Quarterly results, explained" and about["channel"] == "TD Bank"
        assert about["duration"] == 212 and about["published"] == "20250901"
        assert about["source"] == f"https://www.youtube.com/watch?v={VIDEO}"
        assert about["asr"] == "fake", "what the engine said about itself is kept"

    def test_a_video_with_no_title_is_named_by_its_id(self):
        doc = load_youtube(f"https://youtu.be/{VIDEO}", audio=Listener(), download=Downloader(info={"id": VIDEO}))
        assert doc.metadata["filename"] == VIDEO and "title" not in doc.metadata

    def test_the_download_is_gone_afterwards(self):
        fetch = Downloader()
        load_youtube(f"https://youtu.be/{VIDEO}", audio=Listener(), download=fetch)
        assert fetch.folder and not Path(fetch.folder).exists()


class TestKeepingIt:
    def test_the_transcript_is_saved_the_way_a_person_reads_it(self, tmp_path):
        """What the reference's youtube_save writes: heading, details, full text, timed segments."""
        doc = load_youtube(f"https://youtu.be/{VIDEO}", audio=Timed(), save_to=str(tmp_path), download=Downloader())
        written = tmp_path / f"{VIDEO}_transcript.md"
        assert doc.metadata["saved_to"] == str(written)
        saved = written.read_text(encoding="utf-8")
        assert saved.startswith("# YouTube: Quarterly results, explained\n")
        assert "- Channel: TD Bank" in saved and "## Full Text" in saved and "## Segments" in saved
        assert "**[0:00 → 0:04]** Welcome to the quarterly results." in saved

    def test_it_is_named_by_the_video_and_not_its_title(self, tmp_path):
        """Two titles can share a first word; two requests must never swap files."""
        load_youtube(f"https://youtu.be/{VIDEO}", audio=Timed(), save_to=str(tmp_path), download=Downloader())
        assert [f.name for f in tmp_path.iterdir()] == [f"{VIDEO}_transcript.md"]

    def test_the_sound_is_deleted_unless_asked_for(self, tmp_path):
        load_youtube(f"https://youtu.be/{VIDEO}", audio=Listener(), save_to=str(tmp_path), download=Downloader())
        assert not (tmp_path / f"{VIDEO}.m4a").exists()

    def test_keep_audio_keeps_it_beside_the_transcript(self, tmp_path):
        doc = load_youtube(
            f"https://youtu.be/{VIDEO}", audio=Listener(), save_to=str(tmp_path), keep_audio=True, download=Downloader()
        )
        assert (tmp_path / f"{VIDEO}.m4a").read_bytes() == b"sound"
        assert doc.metadata["audio_saved_to"] == str(tmp_path / f"{VIDEO}.m4a")

    def test_keep_audio_needs_somewhere_to_keep_it(self):
        with pytest.raises(ValueError, match="needs save_to"):
            load_youtube(f"https://youtu.be/{VIDEO}", audio=Listener(), keep_audio=True, download=Downloader())


class TestTheRealDownloader:
    """The default path, through a stand-in for yt-dlp, so its options are held."""

    @pytest.fixture
    def ytdlp(self, monkeypatch):
        seen = {}

        class YoutubeDL:
            def __init__(self, options):
                seen["options"] = options

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download):
                seen["url"], seen["download"] = url, download
                if seen.get("refuse"):
                    raise Exception(seen["refuse"])
                Path(seen["options"]["outtmpl"].replace("%(id)s", VIDEO).replace("%(ext)s", "m4a")).write_bytes(b"sound")
                return dict(INFO, ext="m4a")

            def prepare_filename(self, info):
                return seen["options"]["outtmpl"].replace("%(id)s", info["id"]).replace("%(ext)s", info["ext"])

        monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=YoutubeDL))
        return seen

    def test_it_asks_for_the_sound_alone_and_one_video(self, ytdlp):
        load_youtube(f"https://youtu.be/{VIDEO}", audio=Listener())
        options = ytdlp["options"]
        assert options["format"].startswith("bestaudio[ext=m4a]"), "m4a first, which Speech reads as it comes"
        assert options["noplaylist"] is True and ytdlp["download"] is True

    def test_a_refusal_as_a_bot_says_why(self, ytdlp):
        ytdlp["refuse"] = "ERROR: [youtube] Sign in to confirm you're not a bot"
        with pytest.raises(ExtractionError, match="cloud addresses"):
            load_youtube(f"https://youtu.be/{VIDEO}", audio=Listener())

    def test_any_other_refusal_is_passed_on(self, ytdlp):
        ytdlp["refuse"] = "ERROR: Video unavailable"
        with pytest.raises(ExtractionError, match="Video unavailable"):
            load_youtube(f"https://youtu.be/{VIDEO}", audio=Listener())

    def test_it_says_which_package_it_needs(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "yt_dlp", None)
        with pytest.raises(DependencyError, match="yt-dlp"):
            load_youtube(f"https://youtu.be/{VIDEO}", audio=Listener())


class TestWhereItLives:
    def test_beside_load_url_and_from_the_package(self):
        import vectrixdb
        from vectrixdb import extract

        assert vectrixdb.load_youtube is extract.load_youtube is load_youtube
        assert extract.is_youtube(f"https://youtu.be/{VIDEO}")


class TestTheLanguageIsPerCall:
    """The reference takes language on every request. The engine is shared, so it is never changed."""

    def speech(self, sent):
        from vectrixdb.extract.engines import AzureSpeech

        def transport(method, url, headers, body, timeout):
            sent.append(body)
            return 200, {}, b'{"phrases": [{"offsetMilliseconds": 0, "durationMilliseconds": 4200, "text": "Bonjour."}]}'

        return AzureSpeech("https://s.cognitiveservices.azure.com", "k", locales=("en-US",), transport=transport)

    def test_azure_speech_is_asked_for_the_locale_given(self):
        """Checked in the request Speech receives, not on an attribute."""
        sent = []
        speech = self.speech(sent)
        doc = load_youtube(f"https://youtu.be/{VIDEO}", audio=speech, language="fr-FR", download=Downloader())
        assert b'{"locales": ["fr-FR"]}' in sent[0]
        assert doc.metadata["language"] == "fr-FR"

    def test_the_shared_engine_is_left_as_it_was(self):
        sent = []
        speech = self.speech(sent)
        load_youtube(f"https://youtu.be/{VIDEO}", audio=speech, language="fr-FR", download=Downloader())
        load_youtube(f"https://youtu.be/{VIDEO}", audio=speech, download=Downloader())
        assert speech.locales == ["en-US"], "the request running beside this one keeps its language"
        assert b'{"locales": ["en-US"]}' in sent[1]

    def test_left_out_it_is_the_engines_own(self):
        doc = load_youtube(f"https://youtu.be/{VIDEO}", audio=self.speech([]), download=Downloader())
        assert doc.metadata["language"] == "en-US"

    def test_whisper_takes_the_language_part_of_a_locale(self):
        from vectrixdb.extract.engines import Whisper

        heard = []

        class Model:
            def transcribe(self, path, language=None):
                heard.append(language)
                return [], None

        whisper = Whisper(language="en")
        whisper._loaded = Model()
        load_youtube(f"https://youtu.be/{VIDEO}", audio=whisper, language="fr-FR", download=Downloader())
        assert heard == ["fr"], "Whisper wants fr, and fr-FR is given the way Azure wants it"
        assert whisper.language == "en"

    def test_several_languages_are_all_given_to_speech(self):
        """Speech is told every language the recording may be in, and says which it heard."""
        sent = []
        load_youtube(f"https://youtu.be/{VIDEO}", audio=self.speech(sent), language="en-US, fr-CA", download=Downloader())
        assert b'{"locales": ["en-US", "fr-CA"]}' in sent[0]

    def test_whisper_listens_for_the_first_of_several(self):
        from vectrixdb.extract.engines import Whisper

        heard = []

        class Model:
            def transcribe(self, path, language=None):
                heard.append(language)
                return [], None

        whisper = Whisper(language="en")
        whisper._loaded = Model()
        load_youtube(f"https://youtu.be/{VIDEO}", audio=whisper, language="fr-CA,en-US", download=Downloader())
        assert heard == ["fr"]

    def test_an_engine_with_no_language_says_so_rather_than_ignoring_it(self):
        """Ignored, it would give a transcript in the wrong language and no error."""
        with pytest.raises(ValueError, match="has no language to set"):
            load_youtube(f"https://youtu.be/{VIDEO}", audio=Listener(), language="fr-FR", download=Downloader())


class TestEveryPhraseKeepsItsTime:
    def test_the_document_carries_its_segments(self):
        doc = load_youtube(f"https://youtu.be/{VIDEO}", audio=Timed(), download=Downloader())
        assert doc.segments == [(0.0, 4.2, "Welcome to the quarterly results."), (4.2, 9.0, "Revenue grew in every region.")]

    def test_the_transcript_is_the_references_layout(self):
        from vectrixdb.extract.engines import transcript_markdown

        doc = load_youtube(f"https://youtu.be/{VIDEO}", audio=Timed(), language="en-US", download=Downloader())
        assert transcript_markdown(doc) == (
            "# YouTube: Quarterly results, explained\n"
            "\n"
            f"- URL: https://www.youtube.com/watch?v={VIDEO}\n"
            "- Channel: TD Bank\n"
            "- Duration: 3:32\n"
            "- Language: en-US\n"
            "\n"
            "---\n"
            "\n"
            "## Full Text\n"
            "\n"
            "Welcome to the quarterly results. Revenue grew in every region.\n"
            "\n"
            "## Segments\n"
            "\n"
            "**[0:00 → 0:04]** Welcome to the quarterly results.\n"
            "\n"
            "**[0:04 → 0:09]** Revenue grew in every region.\n"
        )
