"""A YouTube video's captions before its sound. Nothing is fetched: YouTube is a stand-in.

The stand-in answers what yt-dlp would: a look at the video with its
``subtitles``, the uploader's, and its ``automatic_captions``, YouTube's,
each a language mapped to formats at addresses, shaped as yt-dlp files them;
the bytes of a track; and the sound. Speech, when it is used, is the real
AzureSpeech answering through a fake network, so a test can tell whether
anybody was paid to listen.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

from vectrixdb.exceptions import DependencyError
from vectrixdb.extract.engines import AzureSpeech, transcript_markdown
from vectrixdb.extract.youtube import (
    CAPTIONS,
    _caption_segments,
    _pick_track,
    _tracks,
    load_youtube,
)

VIDEO = "dQw4w9WgXcQ"
URL = f"https://youtu.be/{VIDEO}"
WATCH = f"https://www.youtube.com/watch?v={VIDEO}"
TIMEDTEXT = f"https://www.youtube.com/api/timedtext?v={VIDEO}"
INFO = {
    "id": VIDEO,
    "title": "Quarterly results, explained",
    "channel": "Northwind Bank",
    "duration": 212,
    "upload_date": "20250901",
    "webpage_url": WATCH,
}


# ============================================================== the fakes ===


def listed(lang, *, asr=False, tlang=None, formats=("json3", "srv1", "srv3", "ttml", "vtt")):
    """One language's formats as yt-dlp lists them, each at its own address."""
    query = f"&lang={lang}" + ("&kind=asr" if asr else "") + (f"&tlang={tlang}" if tlang else "")
    return [{"ext": fmt, "url": f"{TIMEDTEXT}{query}&fmt={fmt}", "name": lang} for fmt in formats]


def address(lang, *, asr=False, tlang=None, fmt="json3"):
    """The address of one format of one track, as listed() writes it."""
    query = f"&lang={lang}" + ("&kind=asr" if asr else "") + (f"&tlang={tlang}" if tlang else "")
    return f"{TIMEDTEXT}{query}&fmt={fmt}"


def automatic_in(spoken, *translations):
    """YouTube's automatic captions as yt-dlp files them: a translation in every language it knows,
    the original twice, once marked ``-orig``, in the order YouTube lists its languages."""
    found = {}
    for lang in sorted({*translations, spoken}):
        if lang == spoken:
            found[f"{lang}-orig"] = listed(lang, asr=True)
            found[lang] = listed(lang, asr=True)
        else:
            found[lang] = listed(spoken, asr=True, tlang=lang)
    return found


def json3(*events):
    """A json3 track: (start, end, text) as YouTube writes an uploaded cue."""
    return json.dumps(
        {
            "wireMagic": "pb3",
            "pens": [{}],
            "wsWinStyles": [{}],
            "wpWinPositions": [{}],
            "events": [
                {
                    "tStartMs": int(start * 1000),
                    "dDurationMs": int((end - start) * 1000),
                    "segs": [{"utf8": text}],
                }
                for start, end, text in events
            ],
        }
    ).encode()


WORDS = json3(
    (0.0, 4.2, "Welcome to the quarterly results."),
    (4.2, 9.0, "Revenue grew\nin every region."),
    (65.0, 68.0, "Costs were flat."),
)
ROUGH = json3((0.0, 3.0, "welcome to the quarterly results"))
TRANSLATED = json3((0.0, 3.0, "bienvenue aux résultats trimestriels"))


class YouTube:
    """yt-dlp's three jobs with no network: a look at the video, a track's bytes, the sound."""

    def __init__(self, subtitles=None, automatic=None, tracks=None, info=None):
        self.info = dict(INFO if info is None else info)
        self.info["subtitles"] = subtitles or {}
        self.info["automatic_captions"] = automatic or {}
        self.tracks = dict(tracks or {})
        self.looked, self.read_from, self.downloaded = [], [], []

    def look(self, url):
        self.looked.append(url)
        return dict(self.info)

    def read(self, url):
        self.read_from.append(url)
        if url not in self.tracks:
            raise OSError("HTTP Error 404: Not Found")
        return self.tracks[url]

    def __call__(self, url, folder):
        self.downloaded.append(url)
        path = Path(folder) / f"{VIDEO}.m4a"
        path.write_bytes(b"sound")
        return str(path), dict(self.info)


def uploaded_english(**more):
    return YouTube(subtitles={"en": listed("en")}, tracks={address("en"): WORDS}, **more)


def speech(sent=None):
    """The real AzureSpeech, answering through a fake network, keeping what it was sent."""
    sent = [] if sent is None else sent

    def transport(method, url, headers, body, timeout):
        sent.append(body)
        phrases = [{"offsetMilliseconds": 0, "durationMilliseconds": 4200, "text": "Heard."}]
        return 200, {}, json.dumps({"phrases": phrases}).encode()

    made = AzureSpeech("https://s.cognitiveservices.azure.com", "k", transport=transport)
    made.sent = sent
    return made


# =========================================================== the default ===


class TestTheUploadersCaptionsComeFirst:
    def test_they_give_the_words_and_nothing_is_downloaded_or_heard(self):
        youtube, listener = uploaded_english(), speech()
        doc = load_youtube(URL, audio=listener, download=youtube)
        assert doc.text.startswith(
            "Welcome to the quarterly results. Revenue grew in every region."
        )
        assert youtube.looked == [WATCH] and youtube.downloaded == [], "no sound fetched"
        assert listener.sent == [], "and nobody paid to listen"

    def test_they_are_cited_by_the_minute_like_speech(self):
        doc = load_youtube(URL, audio=speech(), download=uploaded_english())
        assert doc.segments == [
            (0.0, 4.2, "Welcome to the quarterly results."),
            (4.2, 9.0, "Revenue grew in every region."),
            (65.0, 68.0, "Costs were flat."),
        ]
        assert [number for _offset, number in doc.pages] == [1, 2], "the second minute is page two"

    def test_json3_is_read_before_any_other_format(self):
        youtube = uploaded_english()
        load_youtube(URL, audio=speech(), download=youtube)
        assert youtube.read_from == [address("en")]

    def test_youtubes_automatic_captions_alone_are_not_trusted_unasked(self):
        """Rougher than Speech: no punctuation, more mistakes. So the sound is read."""
        youtube = YouTube(
            automatic=automatic_in("en", "fr"), tracks={address("en", asr=True): ROUGH}
        )
        listener = speech()
        doc = load_youtube(URL, audio=listener, download=youtube)
        assert youtube.read_from == [] and youtube.downloaded == [WATCH]
        assert len(listener.sent) == 1 and doc.text == "Heard."
        assert doc.metadata["transcript_source"] == "speech"
        assert doc.metadata["captions_unused"] == "the video has no captions by its uploader"

    def test_a_video_with_no_captions_is_read_from_its_sound(self):
        youtube, listener = YouTube(), speech()
        doc = load_youtube(URL, audio=listener, download=youtube)
        assert youtube.downloaded == [WATCH] and len(listener.sent) == 1
        assert doc.metadata["transcript_source"] == "speech"


class TestTheChoice:
    def test_the_choices(self):
        assert CAPTIONS == ("uploaded", "automatic", "translated", "never")

    def test_automatic_takes_youtubes_own_when_the_uploader_made_none(self):
        youtube = YouTube(
            automatic=automatic_in("en", "de", "fr"), tracks={address("en", asr=True): ROUGH}
        )
        listener = speech()
        doc = load_youtube(URL, audio=listener, captions="automatic", download=youtube)
        assert doc.text == "welcome to the quarterly results" and listener.sent == []
        assert youtube.read_from == [address("en", asr=True)], "the original, not a translation"
        assert doc.metadata["caption_automatic"] is True

    def test_automatic_still_takes_the_uploaders_first(self):
        youtube = YouTube(
            subtitles={"en": listed("en")},
            automatic=automatic_in("en", "fr"),
            tracks={address("en"): WORDS, address("en", asr=True): ROUGH},
        )
        doc = load_youtube(URL, audio=speech(), captions="automatic", download=youtube)
        assert youtube.read_from == [address("en")] and doc.metadata["caption_automatic"] is False

    def test_a_translation_is_never_read_unasked(self):
        """YouTube lists one in every language it knows; it is not what was said."""
        youtube = YouTube(
            automatic=automatic_in("en", "fr"),
            tracks={address("en", asr=True, tlang="fr"): TRANSLATED},
        )
        listener = speech()
        doc = load_youtube(
            URL, audio=listener, captions="automatic", language="fr-FR", download=youtube
        )
        assert youtube.read_from == [] and doc.metadata["transcript_source"] == "speech"
        assert b'"locales": ["fr-FR"]' in listener.sent[0], "heard in the language asked for"

    def test_translated_reads_a_machine_translation_into_the_language_asked(self):
        youtube = YouTube(
            automatic=automatic_in("en", "fr"),
            tracks={address("en", asr=True, tlang="fr"): TRANSLATED},
        )
        listener = speech()
        doc = load_youtube(
            URL, audio=listener, captions="translated", language="fr-FR", download=youtube
        )
        assert doc.text == "bienvenue aux résultats trimestriels" and listener.sent == []
        about = doc.metadata
        assert about["caption_language"] == "fr" and about["caption_translated_from"] == "en"
        assert about["caption_automatic"] is True and about["language"] == "fr"

    def test_translated_still_takes_words_in_that_language_first(self):
        french = json3((0.0, 2.0, "Bienvenue."))
        youtube = YouTube(
            subtitles={"fr": listed("fr")},
            automatic=automatic_in("en", "fr"),
            tracks={address("fr"): french, address("en", asr=True, tlang="fr"): TRANSLATED},
        )
        doc = load_youtube(
            URL, audio=speech(), captions="translated", language="fr", download=youtube
        )
        assert doc.text == "Bienvenue." and "caption_translated_from" not in doc.metadata

    def test_never_does_not_even_look(self):
        youtube, listener = uploaded_english(), speech()
        doc = load_youtube(URL, audio=listener, captions="never", download=youtube)
        assert youtube.looked == [] and youtube.downloaded == [WATCH]
        assert len(listener.sent) == 1 and doc.metadata["transcript_source"] == "speech"
        assert "captions_unused" not in doc.metadata, "nothing to explain: the caller said never"

    def test_a_choice_that_is_not_one_is_refused_before_anything_is_fetched(self):
        youtube = uploaded_english()
        with pytest.raises(ValueError, match="captions is one of uploaded, automatic"):
            load_youtube(URL, audio=speech(), captions="any", download=youtube)
        assert youtube.looked == [] and youtube.downloaded == []

    def test_a_download_of_your_own_is_asked_for_the_sound_alone(self):
        """It cannot look at captions, and is never gone around to reach YouTube another way."""
        asked = []

        def fetch(url, folder):
            asked.append(url)
            path = Path(folder) / f"{VIDEO}.m4a"
            path.write_bytes(b"sound")
            return str(path), dict(INFO)

        listener = speech()
        doc = load_youtube(URL, audio=listener, download=fetch)
        assert asked == [WATCH] and len(listener.sent) == 1
        assert doc.metadata["captions_unused"] == "the download handed in fetches the sound alone"


class TestWhichLanguage:
    def test_en_us_takes_an_en_track(self):
        youtube = uploaded_english()
        doc = load_youtube(URL, audio=speech(), language="en-US", download=youtube)
        assert doc.metadata["caption_language"] == "en" and doc.metadata["language"] == "en"

    def test_the_same_region_is_taken_first(self):
        british = json3((0.0, 2.0, "Colour."))
        youtube = YouTube(
            subtitles={"en": listed("en"), "en-GB": listed("en-GB")},
            tracks={address("en"): WORDS, address("en-GB"): british},
        )
        doc = load_youtube(URL, audio=speech(), language="en-GB", download=youtube)
        assert doc.text == "Colour." and doc.metadata["caption_language"] == "en-GB"

    def test_several_languages_are_tried_in_turn(self):
        french = json3((0.0, 2.0, "Bienvenue."))
        youtube = YouTube(
            subtitles={"en": listed("en"), "fr": listed("fr")},
            tracks={address("en"): WORDS, address("fr"): french},
        )
        doc = load_youtube(URL, audio=speech(), language="de-DE, fr-CA, en-US", download=youtube)
        assert doc.text == "Bienvenue."

    def test_a_language_with_no_track_is_heard_not_taken_in_another(self):
        youtube, listener = uploaded_english(), speech()
        doc = load_youtube(URL, audio=listener, language="fr-FR", download=youtube)
        assert youtube.read_from == [] and b'"locales": ["fr-FR"]' in listener.sent[0]
        assert (
            doc.metadata["captions_unused"] == "the video has no captions by its uploader in fr-FR"
        )

    def test_left_out_the_videos_own_language_comes_first(self):
        """A French video, as its automatic captions say: its French track, not its English one."""
        french = json3((0.0, 2.0, "Bienvenue."))
        youtube = YouTube(
            subtitles={"en": listed("en"), "fr": listed("fr")},
            automatic=automatic_in("fr", "en"),
            tracks={address("en"): WORDS, address("fr"): french},
        )
        assert load_youtube(URL, audio=speech(), download=youtube).text == "Bienvenue."

    def test_then_english(self):
        youtube = YouTube(
            subtitles={"de": listed("de"), "en": listed("en")},
            automatic=automatic_in("fr", "en", "de"),
            tracks={address("en"): WORDS},
        )
        doc = load_youtube(URL, audio=speech(), download=youtube)
        assert doc.metadata["caption_language"] == "en"

    def test_then_the_uploaders_first_track(self):
        german = json3((0.0, 2.0, "Willkommen."))
        youtube = YouTube(
            subtitles={"de": listed("de"), "es": listed("es")},
            tracks={address("de"): german},
        )
        assert load_youtube(URL, audio=speech(), download=youtube).text == "Willkommen."

    def test_youtubes_retired_codes_are_understood(self):
        """YouTube still names Hebrew iw; a caller asks for he-IL."""
        hebrew = json3((0.0, 2.0, "ברוכים הבאים"))
        youtube = YouTube(subtitles={"iw": listed("iw")}, tracks={address("iw"): hebrew})
        assert load_youtube(URL, audio=speech(), language="he-IL", download=youtube).text == (
            "ברוכים הבאים"
        )

    def test_a_second_english_track_is_english(self):
        youtube = YouTube(
            subtitles={"en-nP7-2PuUl7o": listed("en")},
            tracks={address("en"): WORDS},
        )
        doc = load_youtube(URL, audio=speech(), language="en-US", download=youtube)
        assert doc.metadata["caption_language"] == "en"


class TestEachTrackIsWhatItIs:
    def test_tlang_marks_a_translation_whatever_it_is_filed_under(self):
        info = {
            "subtitles": {"en": listed("en"), "fr": listed("en", tlang="fr")},
            "automatic_captions": automatic_in("en", "de"),
        }
        kinds = {(t.kind, t.language) for t in _tracks(info)}
        assert kinds == {
            ("uploaded", "en"),
            ("translated", "fr"),
            ("automatic", "en"),
            ("translated", "de"),
        }

    def test_the_original_is_listed_once_though_yt_dlp_files_it_twice(self):
        found = [
            t for t in _tracks({"automatic_captions": automatic_in("en")}) if t.kind == "automatic"
        ]
        assert [t.language for t in found] == ["en"]

    def test_with_no_mark_an_automatic_track_in_another_language_is_a_translation(self):
        """Should YouTube stop marking them, ``-orig`` still says which one was made from the speech."""
        automatic = {
            "en-orig": listed("en", asr=True),
            "en": listed("en", asr=True),
            "fr": listed("en", asr=True),
        }
        assert {(t.kind, t.language) for t in _tracks({"automatic_captions": automatic})} == {
            ("automatic", "en"),
            ("translated", "fr"),
        }

    def test_a_track_in_no_format_read_here_is_left_out(self):
        info = {
            "subtitles": {
                "live_chat": [{"ext": "json", "url": "https://www.youtube.com/live_chat_replay"}],
                "en": listed("en", formats=("srv3", "ttml")),
            }
        }
        assert _tracks(info) == []
        assert _pick_track(info, "uploaded", None) is None

    def test_json3_then_vtt_then_srt(self):
        assert (
            _tracks({"subtitles": {"en": listed("en", formats=("srt", "vtt", "json3"))}})[0].ext
            == "json3"
        )
        assert _tracks({"subtitles": {"en": listed("en", formats=("srt", "vtt"))}})[0].ext == "vtt"
        assert _tracks({"subtitles": {"en": listed("en", formats=("ttml", "srt"))}})[0].ext == "srt"


# ======================================================== reading a track ===


class TestReadingATrack:
    def test_json3_as_the_uploader_wrote_it(self):
        assert _caption_segments(WORDS, "json3")[:2] == [
            (0.0, 4.2, "Welcome to the quarterly results."),
            (4.2, 9.0, "Revenue grew in every region."),
        ]

    def test_json3_as_youtube_heard_it(self):
        """A window placed first, words one at a time, a line break of its own: each line once."""
        rolling = json.dumps(
            {
                "wireMagic": "pb3",
                "events": [
                    {
                        "tStartMs": 0,
                        "dDurationMs": 212000,
                        "id": 1,
                        "wpWinPosId": 1,
                        "wsWinStyleId": 1,
                    },
                    {
                        "tStartMs": 80,
                        "dDurationMs": 4400,
                        "wWinId": 1,
                        "segs": [
                            {"utf8": "welcome", "acAsrConf": 0},
                            {"utf8": " to", "tOffsetMs": 480, "acAsrConf": 0},
                            {"utf8": " the", "tOffsetMs": 640, "acAsrConf": 0},
                        ],
                    },
                    {
                        "tStartMs": 2470,
                        "dDurationMs": 2010,
                        "wWinId": 1,
                        "aAppend": 1,
                        "segs": [{"utf8": "\n"}],
                    },
                    {
                        "tStartMs": 2480,
                        "dDurationMs": 4320,
                        "wWinId": 1,
                        "segs": [{"utf8": "quarterly"}, {"utf8": " results", "tOffsetMs": 480}],
                    },
                    {
                        "tStartMs": 4480,
                        "dDurationMs": 2320,
                        "wWinId": 1,
                        "aAppend": 1,
                        "segs": [{"utf8": "\n"}],
                    },
                    {
                        "tStartMs": 4490,
                        "dDurationMs": 2310,
                        "wWinId": 1,
                        "segs": [{"utf8": "revenue grew"}],
                    },
                ],
            }
        ).encode()
        assert _caption_segments(rolling, "json3") == [
            (0.08, 2.48, "welcome to the"),
            (2.48, 4.49, "quarterly results"),
            (4.49, 6.8, "revenue grew"),
        ], "each line ends where the next begins, not when it scrolled away"

    def test_vtt_as_the_uploader_wrote_it(self):
        vtt = (
            "WEBVTT\n"
            "Kind: captions\n"
            "Language: en\n"
            "\n"
            "00:00:00.000 --> 00:00:04.200\n"
            "Welcome to the <i>quarterly</i> results.\n"
            "\n"
            "intro-2\n"
            "00:00:04.200 --> 00:00:09.000 align:middle\n"
            "<v Narrator>Revenue grew</v>\n"
            "in every region &amp; market.\n"
            "\n"
            "NOTE nobody reads this\n"
            "\n"
            "01:02:03.500 --> 01:02:05.000\n"
            "Thank you.\n"
        ).encode()
        assert _caption_segments(vtt, "vtt") == [
            (0.0, 4.2, "Welcome to the quarterly results."),
            (4.2, 9.0, "Revenue grew in every region & market."),
            (3723.5, 3725.0, "Thank you."),
        ]

    def test_vtt_as_youtube_heard_it(self):
        """Rolling lines: each shown twice, words timed and styled one by one. Each said once, clean."""
        vtt = (
            "WEBVTT\nKind: captions\nLanguage: en\n\n"
            "00:00:00.080 --> 00:00:02.470 align:start position:0%\n"
            " \n"
            "welcome<00:00:00.560><c> to</c><00:00:00.720><c> the</c>\n"
            "\n"
            "00:00:02.470 --> 00:00:02.480 align:start position:0%\n"
            "welcome to the\n"
            " \n"
            "\n"
            "00:00:02.480 --> 00:00:04.480 align:start position:0%\n"
            "welcome to the\n"
            "quarterly<00:00:02.960><c> results</c>\n"
            "\n"
            "00:00:04.480 --> 00:00:04.490 align:start position:0%\n"
            "quarterly results\n"
            " \n"
            "\n"
            "00:00:04.490 --> 00:00:06.800 align:start position:0%\n"
            "quarterly results\n"
            "revenue<00:00:04.890><c> grew</c>\n"
        ).encode()
        assert _caption_segments(vtt, "vtt") == [
            (0.08, 2.47, "welcome to the"),
            (2.48, 4.48, "quarterly results"),
            (4.49, 6.8, "revenue grew"),
        ]

    def test_srt(self):
        srt = (
            "1\r\n00:00:00,000 --> 00:00:04,200\r\nWelcome to the quarterly results.\r\n\r\n"
            "2\r\n00:00:04,200 --> 00:00:09,000\r\nRevenue grew in every region.\r\n"
        ).encode("utf-8-sig")
        assert _caption_segments(srt, "srt") == [
            (0.0, 4.2, "Welcome to the quarterly results."),
            (4.2, 9.0, "Revenue grew in every region."),
        ]

    def test_text_handed_in_as_text_and_times_with_no_hours(self):
        """A fetcher of your own may answer text, and a short file may leave the hours out."""
        assert _caption_segments("WEBVTT\n\n00:01.000 --> 00:02.500\nHi.\n", "vtt") == [
            (1.0, 2.5, "Hi.")
        ]

    def test_a_track_with_no_words_is_read_from_the_sound(self):
        youtube = YouTube(subtitles={"en": listed("en")}, tracks={address("en"): json3()})
        listener = speech()
        doc = load_youtube(URL, audio=listener, download=youtube)
        assert len(listener.sent) == 1
        assert doc.metadata["captions_unused"] == "the uploader's en captions have no words in them"

    def test_a_track_that_is_not_what_it_says_is_read_from_the_sound(self):
        youtube = YouTube(subtitles={"en": listed("en")}, tracks={address("en"): b"<html>"})
        doc = load_youtube(URL, audio=speech(), download=youtube)
        assert doc.metadata["transcript_source"] == "speech"
        assert doc.metadata["captions_unused"].startswith(
            "the uploader's en captions could not be read"
        )

    def test_a_track_that_cannot_be_fetched_is_read_from_the_sound(self):
        youtube = YouTube(subtitles={"en": listed("en")})
        doc = load_youtube(URL, audio=speech(), download=youtube)
        assert youtube.read_from == [address("en")] and youtube.downloaded == [WATCH]
        assert doc.metadata["captions_unused"] == (
            "the uploader's en captions could not be read: HTTP Error 404: Not Found"
        )


# ================================================== what it does not need ===


class TestNoSpeechEngineIsNeeded:
    def test_without_whisper_or_ffmpeg_installed(self, monkeypatch):
        """With captions, nothing that listens or decodes sound is imported, let alone paid."""
        monkeypatch.setitem(sys.modules, "faster_whisper", None)
        monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
        doc = load_youtube(URL, download=uploaded_english())
        assert doc.text.startswith("Welcome to the quarterly results.")
        with pytest.raises(DependencyError, match="faster-whisper"):
            load_youtube(URL, captions="never", download=uploaded_english())

    def test_an_engine_that_would_fail_is_never_called(self):
        def refuses(data, name):
            raise AssertionError("nobody should be listening")

        doc = load_youtube(URL, audio=refuses, download=uploaded_english())
        assert doc.metadata["transcript_source"] == "captions"


# ================================================ what travels with it ===


class TestWhatTravelsWithIt:
    def test_the_video_and_where_its_words_came_from(self):
        about = load_youtube(URL, audio=speech(), download=uploaded_english()).metadata
        assert about["kind"] == "youtube" and about["youtube_id"] == VIDEO
        assert (
            about["title"] == "Quarterly results, explained" and about["filename"] == about["title"]
        )
        assert about["channel"] == "Northwind Bank" and about["duration"] == 212
        assert about["published"] == "20250901" and about["source"] == WATCH
        assert about["transcript_source"] == "captions" and about["caption_language"] == "en"
        assert about["caption_automatic"] is False and about["language"] == "en"
        assert about["transcript"] is True and about["seconds_per_page"] == 60.0
        assert "audio_file" not in about and "captions_unused" not in about, (
            "nothing was downloaded"
        )

    def test_the_minutes_are_the_engines_own(self):
        """An engine set to half-minute pages gets half-minute pages from captions too."""
        listener = speech()
        listener.seconds_per_page = 30.0
        doc = load_youtube(URL, audio=listener, download=uploaded_english())
        assert doc.metadata["seconds_per_page"] == 30.0 and len(doc.pages) == 2

    def test_speech_says_it_is_speech(self):
        about = load_youtube(URL, audio=speech(), download=YouTube()).metadata
        assert about["transcript_source"] == "speech" and about["audio_file"] == f"{VIDEO}.m4a"
        assert "caption_language" not in about and about["title"] == "Quarterly results, explained"


class TestKeepingIt:
    def test_the_transcript_says_its_words_are_the_uploaders_captions(self, tmp_path):
        doc = load_youtube(URL, audio=speech(), save_to=str(tmp_path), download=uploaded_english())
        saved = (tmp_path / f"{VIDEO}_transcript.md").read_text(encoding="utf-8")
        assert doc.metadata["saved_to"] == str(tmp_path / f"{VIDEO}_transcript.md")
        assert saved.startswith("# YouTube: Quarterly results, explained\n")
        assert "- Language: en\n- Words: the uploader's captions\n" in saved
        assert "**[1:05 → 1:08]** Costs were flat." in saved

    def test_and_says_when_youtube_made_or_translated_them(self):
        youtube = YouTube(
            automatic=automatic_in("en", "fr"),
            tracks={
                address("en", asr=True): ROUGH,
                address("en", asr=True, tlang="fr"): TRANSLATED,
            },
        )
        automatic = load_youtube(URL, audio=speech(), captions="automatic", download=youtube)
        assert "- Words: YouTube's automatic captions\n" in transcript_markdown(automatic)
        translated = load_youtube(
            URL, audio=speech(), captions="translated", language="fr", download=youtube
        )
        assert (
            "- Words: YouTube's automatic captions, machine-translated from en\n"
            in transcript_markdown(translated)
        )

    def test_a_transcript_heard_from_the_sound_is_laid_out_as_before(self):
        doc = load_youtube(URL, audio=speech(), download=YouTube())
        assert "- Words:" not in transcript_markdown(doc)

    def test_keep_audio_keeps_nothing_when_nothing_was_downloaded_and_says_so(
        self, tmp_path, caplog
    ):
        caplog.set_level(logging.INFO, logger="vectrixdb.extract")
        doc = load_youtube(
            URL, audio=speech(), save_to=str(tmp_path), keep_audio=True, download=uploaded_english()
        )
        assert [f.name for f in tmp_path.iterdir()] == [f"{VIDEO}_transcript.md"]
        assert "audio_saved_to" not in doc.metadata
        assert "keep_audio kept no sound" in caplog.text and "captions='never'" in caplog.text

    def test_keep_audio_with_never_keeps_the_sound(self, tmp_path):
        doc = load_youtube(
            URL,
            audio=speech(),
            captions="never",
            save_to=str(tmp_path),
            keep_audio=True,
            download=uploaded_english(),
        )
        assert (tmp_path / f"{VIDEO}.m4a").read_bytes() == b"sound"
        assert doc.metadata["audio_saved_to"] == str(tmp_path / f"{VIDEO}.m4a")


# ===================================================== the extraction service ===


class Inline:
    """An executor that runs a job as it is submitted, so a test can read it straight after."""

    def submit(self, fn, *args):
        fn(*args)


KEY = "a-test-key"
SIGNED = {"api-key": KEY}
AS_JSON = {**SIGNED, "Accept": "application/json"}


class TestTheExtractionService:
    @pytest.fixture
    def make(self, monkeypatch, tmp_path):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from vectrixdb.api.extraction import ExtractionService, MemoryJobs, create_extraction_app

        monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
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

        def made(youtube):
            service = ExtractionService(
                audio=speech(),
                jobs=MemoryJobs(tmp_path / "output", executor=Inline()),
                download=youtube,
            )
            return service, TestClient(create_extraction_app(service))

        return made

    def test_a_video_is_read_from_its_captions_by_default(self, make):
        service, client = make(uploaded_english())
        reply = client.post("/transcribe/youtube", json={"url": URL}, headers=AS_JSON).json()
        assert reply["metadata"]["transcript_source"] == "captions"
        assert reply["metadata"]["caption_language"] == "en", "the body's en-US takes an en track"
        assert reply["segments"][0] == [0.0, 4.2, "Welcome to the quarterly results."]
        assert service.audio.sent == [] and service.download.downloaded == []

    def test_the_markdown_says_where_the_words_came_from(self, make):
        _service, client = make(uploaded_english())
        said = client.post("/transcribe/youtube", json={"url": URL}, headers=SIGNED).text
        assert said.startswith("# YouTube: Quarterly results, explained")
        assert "- Words: the uploader's captions" in said

    def test_the_body_chooses(self, make):
        service, client = make(uploaded_english())
        reply = client.post(
            "/transcribe/youtube", json={"url": URL, "captions": "never"}, headers=AS_JSON
        ).json()
        assert reply["metadata"]["transcript_source"] == "speech" and len(service.audio.sent) == 1

    def test_automatic_in_the_body(self, make):
        youtube = YouTube(automatic=automatic_in("en"), tracks={address("en", asr=True): ROUGH})
        _service, client = make(youtube)
        reply = client.post(
            "/transcribe/youtube", json={"url": URL, "captions": "automatic"}, headers=AS_JSON
        ).json()
        assert reply["metadata"]["caption_automatic"] is True

    @pytest.mark.parametrize("route", ["/transcribe/youtube", "/transcribe/youtube_save"])
    def test_a_choice_that_is_not_one_is_refused(self, make, route):
        service, client = make(uploaded_english())
        response = client.post(route, json={"url": URL, "captions": "any"}, headers=SIGNED)
        assert response.status_code == 422 and "uploaded" in response.text
        assert service.download.looked == []

    def test_a_saved_video_says_where_its_words_came_from(self, make, tmp_path):
        _service, client = make(uploaded_english())
        job = client.post("/transcribe/youtube_save", json={"url": URL}, headers=SIGNED).json()[
            "job"
        ]
        result = client.get(f"/jobs/{job}", headers=SIGNED).json()["result"]
        assert result["transcript_source"] == "captions" and result["caption_language"] == "en"
        assert result["caption_automatic"] is False
        assert result["media_deleted"] is None, "nothing was downloaded, so nothing was deleted"
        assert result["video_title"] == "Quarterly results, explained"
        saved = Path(result["transcript_file"]).read_text(encoding="utf-8")
        assert "- Words: the uploader's captions" in saved

    def test_a_saved_video_takes_the_choice_too(self, make):
        service, client = make(uploaded_english())
        body = {"url": URL, "captions": "never"}
        job = client.post("/transcribe/youtube_save", json=body, headers=SIGNED).json()["job"]
        result = client.get(f"/jobs/{job}", headers=SIGNED).json()["result"]
        assert result["transcript_source"] == "speech" and result["caption_language"] is None
        assert result["media_deleted"] == f"{VIDEO}.m4a" and len(service.audio.sent) == 1

    def test_a_job_queued_before_there_was_a_choice_takes_the_default(self, make):
        service, _client = make(uploaded_english())
        result = service.run_job({"kind": "youtube_save", "params": {"url": URL}})
        assert result["transcript_source"] == "captions"

    def test_auto_reads_a_youtube_address_with_the_default(self, make):
        _service, client = make(uploaded_english())
        reply = client.post("/transcribe/auto", json={"url": URL}, headers=AS_JSON).json()
        assert reply["metadata"]["transcript_source"] == "captions"
