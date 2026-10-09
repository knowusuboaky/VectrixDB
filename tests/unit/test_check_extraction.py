"""vectrixdb check says who reads each kind of file, and how to have one read that nothing reads.

What is held to. Each kind of input, documents, scanned pages, pictures,
recordings, videos and YouTube addresses, gets a finding: who reads it on a
server with these settings, or a warning naming the extra and the settings
that would. A file type the extractor routes send to an extraction service
is said to go there, and the rest of its kind is named. A package installed
that does not import, and an ffmpeg that does not run, are errors, since the
first file that needs them fails. What an extraction service started with
these settings reads with is said only when its settings are there. The
machine is a fake here: nothing is imported, run or fetched for real, and
ffmpeg is only ever asked for its version. faster-whisper honours
VECTRIXDB_OFFLINE, reading its model from the cache alone.
"""

from __future__ import annotations

import sys
import types

import pytest

from vectrixdb.check import FFMPEG_ON_PATH, FFMPEG_OWN, run
from vectrixdb.exceptions import ModelDownloadError

DOCUMENTS = ("pypdfium2", "pypdf", "docx", "olefile", "openpyxl", "xlrd")
READERS = DOCUMENTS + ("rapidocr_onnxruntime", "faster_whisper", "imageio_ffmpeg", "yt_dlp")
OWN = "/venv/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2"
VERSION = "ffmpeg version 7.0.2-static https://johnvansickle.com/ffmpeg/  Copyright (c) 2000-2024"


class Machine:
    """A machine with exactly the packages, binaries and models a test names."""

    def __init__(
        self,
        modules=(),
        *,
        broken=None,
        versions=None,
        ffmpeg=(),
        runs=None,
        models=(),
    ):
        self.modules = set(modules)
        self.broken = dict(broken or {})
        self.versions = dict(versions or {})
        self.ffmpeg = list(ffmpeg)
        self.runs = dict(runs or {})
        self.models = set(models)
        self.ran = []

    def installed(self, module):
        return module in self.modules or module in self.broken

    def imports(self, module):
        if module in self.broken:
            return "broken", self.broken[module]
        return ("ok", "") if module in self.modules else ("missing", "")

    def version(self, distribution):
        return self.versions.get(distribution, "")

    def ffmpeg_binaries(self):
        return list(self.ffmpeg)

    def answers(self, argv, timeout=10.0):
        self.ran.append(list(argv))
        return self.runs.get(argv[0], (False, "No such file or directory"))

    def whisper_model_here(self, model):
        return model in self.models


def everything(**changes):
    """Every local reader installed and working, the speech model downloaded."""
    modules = changes.pop("modules", READERS)
    given = {
        "versions": {"yt-dlp": "2025.09.26"},
        "ffmpeg": [(OWN, FFMPEG_OWN)],
        "runs": {OWN: (True, VERSION)},
        "models": {"base"},
    }
    given.update(changes)
    return Machine(modules, **given)


def extraction(tmp_path, machine, **env):
    return [
        (f.level, f.text)
        for f in run(str(tmp_path), env, machine=machine)
        if f.area == "Extraction"
    ]


def said(found, level, *parts):
    """The one finding at this level that holds every part."""
    matches = [text for lvl, text in found if lvl == level and all(p in text for p in parts)]
    assert len(matches) == 1, (level, parts, found)
    return matches[0]


# ------------------------------------------------------------ each kind ---


class TestWhoReadsEachKind:
    def test_a_machine_with_every_reader_reads_every_kind_here(self, tmp_path):
        machine = everything()
        found = extraction(tmp_path, machine)
        said(found, "ok", "PDFs with a text layer", "read here by the built-in readers")
        said(found, "ok", "Pictures are read here by RapidOCR")
        said(found, "ok", "Recordings are read here by faster-whisper", "base model is already")
        said(found, "ok", "Videos are read here: ffmpeg 7.0.2-static takes the sound out")
        said(found, "ok", "YouTube addresses are read with yt-dlp 2025.09.26")
        assert [level for level, _ in found].count("error") == 0
        assert machine.ran == [[OWN, "-version"]], "ffmpeg is asked its version and nothing else"

    def test_scanned_pages_are_read_by_nobody_on_the_server_even_with_rapidocr(self, tmp_path):
        """The server's PDF reader is given no OCR, so a scan is left out whatever is installed."""
        text = said(
            extraction(tmp_path, everything()), "warn", "Nothing reads scanned PDF pages here"
        )
        assert (
            "RapidOCR included" in text and "AZURE_DOCINTEL_ENDPOINT and AZURE_DOCINTEL_KEY" in text
        )

    def test_a_bare_machine_is_told_the_extra_and_the_service_for_each_kind(self, tmp_path):
        found = extraction(tmp_path, Machine())
        said(found, "warn", ".pdf", ".xls files are refused here", "vectrixdb[documents]")
        said(found, "warn", "Nothing reads pictures here", "vectrixdb[ocr]", "AZURE_DOCINTEL_KEY")
        said(found, "warn", "Nothing reads recordings here", "vectrixdb[asr]", "AZURE_SPEECH_KEY")
        said(found, "warn", "Nothing reads videos here", "vectrixdb[video]")
        said(found, "warn", "Nothing reads YouTube addresses here", "vectrixdb[youtube]")
        assert not [text for level, text in found if level == "error"], (
            "a reader nobody installed is a choice, said as a warning"
        )

    def test_a_package_installed_that_does_not_import_is_an_error(self, tmp_path):
        why = "ImportError: libGL.so.1: cannot open shared object file"
        found = extraction(
            tmp_path,
            everything(
                broken={"rapidocr_onnxruntime": why, "docx": "ImportError: lxml"},
                modules=tuple(m for m in READERS if m not in ("rapidocr_onnxruntime", "docx")),
            ),
        )
        assert "libGL.so.1" in said(found, "error", "rapidocr-onnxruntime is installed")
        said(found, "error", "python-docx is installed", ".docx, .docm, .dotx and .dotm files fail")

    def test_a_pdf_reader_is_any_of_the_three_the_pdf_reader_tries(self, tmp_path):
        found = extraction(tmp_path, everything(modules=("pypdf",) + READERS[2:]))
        assert not any(".pdf" in text for level, text in found if level != "ok")

    def test_youtube_says_the_version_and_a_broken_yt_dlp_is_an_error(self, tmp_path):
        found = extraction(
            tmp_path,
            everything(
                modules=tuple(m for m in READERS if m != "yt_dlp"),
                broken={"yt_dlp": "SyntaxError: bad"},
            ),
        )
        said(found, "error", "yt-dlp is installed and does not import")


class TestTheSpeechModel:
    """faster-whisper fetches its model from Hugging Face at the first recording, outside the library's download rules."""

    def test_the_first_recording_downloading_its_model_is_a_warning_with_the_command(
        self, tmp_path
    ):
        text = said(
            extraction(tmp_path, everything(models=set())), "warn", "the first one downloads"
        )
        assert "base model from Hugging Face" in text
        assert "from faster_whisper import download_model; download_model('base')" in text

    def test_offline_and_not_downloaded_nothing_reads_a_recording(self, tmp_path):
        found = extraction(tmp_path, everything(models=set()), VECTRIXDB_OFFLINE="1")
        text = said(found, "warn", "Nothing reads recordings here")
        assert (
            "VECTRIXDB_OFFLINE refuses to download it" in text and "download_model('base')" in text
        )
        said(found, "warn", "Nothing reads videos here", "nothing reads the sound")


class TestFfmpeg:
    """The video reader finds ffmpeg through imageio-ffmpeg, which tries its own, conda's, then PATH's."""

    def test_its_own_that_does_not_run_is_said_and_the_one_on_path_is_used(self, tmp_path):
        machine = everything(
            ffmpeg=[(OWN, FFMPEG_OWN), ("/usr/bin/ffmpeg", FFMPEG_ON_PATH)],
            runs={OWN: (False, "Exec format error"), "/usr/bin/ffmpeg": (True, VERSION)},
        )
        found = extraction(tmp_path, machine)
        text = said(found, "warn", "does not run here (Exec format error)")
        assert "videos go through /usr/bin/ffmpeg, the one on PATH" in text
        said(found, "ok", "Videos are read here")
        assert machine.ran == [[OWN, "-version"], ["/usr/bin/ffmpeg", "-version"]]

    def test_none_that_runs_is_an_error(self, tmp_path):
        found = extraction(
            tmp_path, everything(runs={OWN: (False, "it did not answer within 10 seconds")})
        )
        assert "did not answer within 10 seconds" in said(
            found, "error", "finds no ffmpeg that runs"
        )
        found = extraction(tmp_path, everything(ffmpeg=[]))
        said(found, "error", "ships no ffmpeg for this machine and there is none on PATH")

    def test_one_named_by_imageio_ffmpeg_exe_is_tried_as_it_is_used(self, tmp_path):
        """imageio-ffmpeg takes IMAGEIO_FFMPEG_EXE untested, so the check runs it."""
        found = extraction(tmp_path, everything(), IMAGEIO_FFMPEG_EXE="/opt/ffmpeg")
        said(found, "error", "IMAGEIO_FFMPEG_EXE names /opt/ffmpeg", "No such file or directory")
        machine = everything(runs={"/opt/ffmpeg": (True, VERSION)})
        found = extraction(tmp_path, machine, IMAGEIO_FFMPEG_EXE="/opt/ffmpeg")
        said(found, "ok", "Videos are read here")
        assert machine.ran == [["/opt/ffmpeg", "-version"]], "its own is not tried after it"

    def test_without_imageio_ffmpeg_the_ffmpeg_on_path_is_not_used(self, tmp_path):
        machine = everything(
            modules=tuple(m for m in READERS if m != "imageio_ffmpeg"),
            ffmpeg=[("/usr/bin/ffmpeg", FFMPEG_ON_PATH)],
        )
        text = said(extraction(tmp_path, machine), "warn", "Nothing reads videos here")
        assert "does not use the ffmpeg on PATH without it" in text and "vectrixdb[video]" in text
        assert machine.ran == [], "nothing is run that the reader would not run"


class TestAnExtractionServiceTheServerReadsThrough:
    def test_routed_types_go_there_and_the_rest_of_each_kind_is_named(self, tmp_path):
        pytest.importorskip("fastapi")
        found = extraction(tmp_path, Machine(), VECTRIXDB_EXTRACTOR_URL="https://ai.internal:9001")
        said(found, "ok", "Scanned PDF pages go to ai.internal with their PDF")
        text = said(found, "warn", ".png, .jpg and .jpeg go to ai.internal; nothing reads the rest")
        assert "(.tif, .tiff, .bmp, .webp and .gif)" in text
        assert "add them to VECTRIXDB_EXTRACTOR_ROUTES" in text
        said(found, "warn", ".mp4 goes to ai.internal; nothing reads the rest")

    def test_every_type_routed_goes_there(self, tmp_path):
        pytest.importorskip("fastapi")
        found = extraction(
            tmp_path,
            Machine(),
            VECTRIXDB_EXTRACTOR_URL="https://ai.internal:9001",
            VECTRIXDB_EXTRACTOR_ROUTES='{"*": "/extract/any"}',
        )
        for kind in ("Documents", "Pictures", "Recordings", "Videos"):
            said(found, "ok", f"{kind} go to ai.internal")

    def test_routes_the_server_cannot_use_are_one_error(self, tmp_path):
        pytest.importorskip("fastapi")
        found = extraction(
            tmp_path,
            Machine(),
            VECTRIXDB_EXTRACTOR_URL="https://ai.internal:9001",
            VECTRIXDB_EXTRACTOR_ROUTES='{"": "/x"}',
        )
        said(found, "error", "is not a suffix", "Every file sent to the server is refused")
        assert not any("pictures" in text or "recordings" in text for _, text in found), (
            "with every upload refused, who would read which kind is beside the point"
        )


class TestAnExtractionServiceStartedWithTheseSettings:
    SERVICE = "An extraction service started with these settings"

    def test_nothing_is_said_of_a_service_whose_settings_are_not_there(self, tmp_path):
        assert not any(
            "extraction service started" in text for _, text in extraction(tmp_path, Machine())
        )

    def test_document_intelligence_and_speech(self, tmp_path):
        both = {
            "AZURE_DOCINTEL_ENDPOINT": "https://di.example.test",
            "AZURE_DOCINTEL_KEY": "k",
            "AZURE_SPEECH_ENDPOINT": "https://speech.example.test",
            "AZURE_SPEECH_KEY": "k",
        }
        found = extraction(tmp_path, Machine(["azure.ai.documentintelligence"]), **both)
        said(found, "ok", self.SERVICE, "Document Intelligence at di.example.test")
        said(found, "ok", self.SERVICE, "Azure Speech at speech.example.test")
        found = extraction(tmp_path, Machine(), **both)
        said(found, "error", "stops at its start", "vectrixdb[ocr-azure]")
        found = extraction(
            tmp_path,
            Machine(),
            AZURE_DOCINTEL_KEY="k",
            AZURE_SPEECH_ENDPOINT="http://speech.example.test",
            AZURE_SPEECH_KEY="k",
        )
        said(found, "warn", "AZURE_DOCINTEL_KEY is set and AZURE_DOCINTEL_ENDPOINT is not")
        said(found, "error", "AZURE_SPEECH_ENDPOINT is 'http://speech.example.test'")

    def test_what_fails_at_the_first_picture_or_translation_is_warned(self, tmp_path):
        found = extraction(
            tmp_path,
            Machine(),
            AZURE_OPENAI_VISION_DEPLOYMENT="gpt-4o",
            AZURE_OPENAI_ENDPOINT="https://oa.example.test",
            AZURE_TRANSLATOR_KEY="k",
            VECTRIXDB_EXTRACT_PDF="vision",
        )
        said(found, "warn", "no AZURE_OPENAI_KEY", "needs azure-identity")
        said(found, "warn", "without AZURE_TRANSLATOR_REGION", "refuses the first translation")
        said(found, "warn", "VECTRIXDB_EXTRACT_PDF=vision needs a chat model that can see")
        found = extraction(
            tmp_path,
            Machine(["azure.identity"]),
            AZURE_OPENAI_VISION_DEPLOYMENT="gpt-4o",
            AZURE_OPENAI_ENDPOINT="https://oa.example.test",
            AZURE_VISION_ENDPOINT="https://vision.example.test",
            AZURE_VISION_KEY="k",
            AZURE_TRANSLATOR_KEY="k",
            AZURE_TRANSLATOR_REGION="canadacentral",
        )
        said(
            found,
            "ok",
            "describes pictures with the gpt-4o deployment, as the managed identity, then Azure Vision at vision.example.test",
        )
        said(found, "ok", "translates with Azure AI Translator, in canadacentral")
        found = extraction(tmp_path, Machine(), VECTRIXDB_DESCRIBER_URL="chat.example.test")
        said(found, "error", "stops at its start", "VECTRIXDB_DESCRIBER_URL is 'chat.example.test'")


# ------------------------------------------------------------- masking ---


def masking(tmp_path, machine, **env):
    return [
        (f.level, f.text) for f in run(str(tmp_path), env, machine=machine) if f.area == "Masking"
    ]


class TestMaskingNeedsWhatItsFirstDocumentNeeds:
    """The engine is chosen as engine_from_env chooses it; what it needs to run shows only at the first document."""

    def test_auto_takes_presidio_only_for_the_languages_whose_model_is_here(self, tmp_path):
        found = masking(tmp_path, Machine(["presidio_analyzer"]))
        said(found, "warn", "no spaCy model for en, fr", "the patterns alone mask")
        found = masking(tmp_path, Machine(["presidio_analyzer", "en_core_web_lg"]))
        said(found, "warn", "Presidio in this process, for en. fr has no spaCy model here")
        found = masking(
            tmp_path, Machine(["presidio_analyzer", "en_core_web_lg", "fr_core_news_lg"])
        )
        said(found, "ok", "Presidio in this process, for en, fr")

    def test_a_named_engine_missing_what_it_runs_with_is_an_error(self, tmp_path):
        found = masking(
            tmp_path, Machine(["presidio_analyzer"]), VECTRIXDB_MASKING_ENGINE="presidio"
        )
        said(found, "error", "en_core_web_lg, fr_core_news_lg are not installed", "first document")
        found = masking(
            tmp_path,
            Machine(),
            VECTRIXDB_MASKING_ENGINE="language",
            AZURE_LANGUAGE_ENDPOINT="https://lang.example.test",
        )
        said(found, "error", "managed identity", "azure-identity")
        found = masking(
            tmp_path, Machine(), VECTRIXDB_MASKING_ENGINE="comprehend", AWS_REGION="ca-central-1"
        )
        said(found, "error", "boto3", "first document masked fails")
        found = masking(
            tmp_path,
            Machine(["boto3"]),
            VECTRIXDB_MASKING_ENGINE="comprehend",
            AWS_REGION="ca-central-1",
        )
        said(found, "ok", "Amazon Comprehend in ca-central-1")


class TestCountsThatMayBeNone:
    def test_counts_are_whole_numbers_of_0_or_more(self, tmp_path):
        found = [
            f.text
            for f in run(
                str(tmp_path),
                {
                    "VECTRIXDB_SPEECH_SPEAKERS": "lots",
                    "VECTRIXDB_VIDEO_FRAMES": "-1",
                    "VECTRIXDB_EXTRACTOR_RETRIES": "0",
                },
                machine=Machine(),
            )
            if f.level == "error"
        ]
        assert "VECTRIXDB_SPEECH_SPEAKERS is 'lots', which is not a number" in found
        assert "VECTRIXDB_VIDEO_FRAMES is -1. It has to be 0 or more" in found
        assert not any("RETRIES" in text for text in found)


# ------------------------------------------------- the library's own fix ---


class TestWhisperOffline:
    """faster-whisper asks Hugging Face for its model when it loads; with VECTRIXDB_OFFLINE it may not."""

    @pytest.fixture
    def faster_whisper(self, monkeypatch):
        made = []

        class WhisperModel:
            def __init__(self, model, **options):
                made.append((model, options))
                if options.get("local_files_only") and model not in cached:
                    raise FileNotFoundError("Cannot find an appropriate cached snapshot folder")

            def transcribe(self, path, language=None):
                return [types.SimpleNamespace(start=0.0, end=1.5, text="Bonjour")], None

        cached = {"base"}
        module = types.ModuleType("faster_whisper")
        module.WhisperModel = WhisperModel
        monkeypatch.setitem(sys.modules, "faster_whisper", module)
        return types.SimpleNamespace(made=made, cached=cached)

    def test_offline_reads_the_model_from_the_cache_alone(self, faster_whisper, monkeypatch):
        from vectrixdb.extract.engines import Whisper

        monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
        assert Whisper().transcribe_file("call.wav") == [(0.0, 1.5, "Bonjour")]
        assert faster_whisper.made == [("base", {"local_files_only": True})]

    def test_offline_with_no_model_here_names_the_command_that_fetches_it(
        self, faster_whisper, monkeypatch
    ):
        from vectrixdb.extract.engines import Whisper

        monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
        faster_whisper.cached.clear()
        with pytest.raises(ModelDownloadError) as caught:
            Whisper().transcribe_file("call.wav")
        assert "download_model('base')" in str(caught.value)
        assert "VECTRIXDB_OFFLINE refuses to download it" in str(caught.value)

    def test_with_a_network_it_loads_as_it_always_did(self, faster_whisper, monkeypatch):
        from vectrixdb.extract.engines import Whisper

        monkeypatch.delenv("VECTRIXDB_OFFLINE", raising=False)
        Whisper().transcribe_file("call.wav")
        assert faster_whisper.made == [("base", {})]
