"""The extraction Function App, run without Azure.

``azure.functions`` and the storage SDKs are faked in ``sys.modules``, so the
app is built the way the Functions host builds it, from the environment, and
driven through FastAPI's test client. What is held to is what a person on
call needs from it: it starts with a key and nothing else, says what it was
given without giving anything away, refuses a file without the key, names
every wrong setting in one message, and drops a queue message that is not a
job rather than sending it round.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

AZURE = Path(__file__).resolve().parents[2] / "examples" / "azure"
if not AZURE.exists():
    pytest.skip(
        "examples/ is kept on the machine that runs it, not in the repository",
        allow_module_level=True,
    )

EXTRACTION_APP = AZURE / "extraction_app"
FAKED = ("azure.functions", "azure.storage", "azure.storage.blob", "azure.storage.queue")
#: Settings a deployment carries, two of them keys, which /health/wiring must never repeat.
DEPLOYED = {
    "VECTRIXDB_API_KEY": "the-server-key-7f3a",
    "AZURE_SPEECH_ENDPOINT": "https://speech.example.cognitiveservices.azure.com/",
    "AZURE_SPEECH_KEY": "speech-secret-9c1d",
    "AZURE_TRANSLATOR_KEY": "translator-secret-5e2b",
    "AZURE_TRANSLATOR_REGION": "canadacentral",
    "VECTRIXDB_EXTRACT_URL_HOSTS": "www.td.com, *.td.com",
    "VECTRIXDB_MAX_UPLOAD_BYTES": "1048576",
}


def _import(monkeypatch, env):
    """The app's module, imported fresh with ``env`` as the whole of its VECTRIXDB_ and AZURE_ settings."""
    pytest.importorskip("fastapi")
    pytest.importorskip("azure.functions")
    for name in FAKED:
        monkeypatch.setitem(sys.modules, name, mock.MagicMock())
    monkeypatch.syspath_prepend(str(EXTRACTION_APP))
    monkeypatch.delitem(sys.modules, "function_app", raising=False)
    for name in list(os.environ):
        if name.startswith(("VECTRIXDB_", "AZURE_", "AzureWebJobs")):
            monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    import function_app

    return function_app


@pytest.fixture
def deployed(monkeypatch):
    """The app as 05 deploys it, with Speech and Translator, and nothing of it left behind afterwards."""
    from fastapi.testclient import TestClient

    environment = dict(os.environ)
    before = set(sys.modules)
    try:
        module = _import(monkeypatch, DEPLOYED)
        yield (
            module,
            TestClient(
                module.face_of(module.service, module.library), raise_server_exceptions=False
            ),
        )
    finally:
        os.environ.clear()
        os.environ.update(environment)
        for name in set(sys.modules) - before:
            found = sys.modules.get(name)
            if str(getattr(found, "__file__", "") or "").startswith(str(EXTRACTION_APP)):
                sys.modules.pop(name, None)


class TestItStartsWithAKeyAndNothingElse:
    def test_the_key_alone_is_enough(self, monkeypatch):
        environment = dict(os.environ)
        try:
            module = _import(monkeypatch, {"VECTRIXDB_API_KEY": "the-server-key"})
            assert module.settings_hold() == []
            assert module.what_reads_what(module.service).startswith("documents")
            assert [there for _name, there, _without in module.what_each_reads()] == [False] * 5
        finally:
            os.environ.clear()
            os.environ.update(environment)
            sys.modules.pop("function_app", None)

    def test_without_the_key_it_does_not_start_and_says_what_to_run(self, monkeypatch):
        from vectrixdb.exceptions import ConfigurationError

        environment = dict(os.environ)
        try:
            with pytest.raises(ConfigurationError) as refused:
                _import(monkeypatch, {})
            assert "VECTRIXDB_API_KEY is not set" in str(
                refused.value
            ) and "--settings-only" in str(refused.value)
        finally:
            os.environ.clear()
            os.environ.update(environment)
            sys.modules.pop("function_app", None)


class TestHealthAndWiringNeedNoKey:
    def test_health_is_the_librarys_and_open(self, deployed):
        _module, client = deployed
        assert client.get("/health").status_code == 200

    def test_wiring_is_ours_and_open(self, deployed):
        _module, client = deployed
        answer = client.get("/health/wiring")
        assert answer.status_code == 200
        said = answer.json()
        assert (
            said["role"] == "extraction"
            and said["door"] == "the library's key, in api-key or as a Bearer token"
        )
        assert (
            said["readers"]["speech"]["given"] is True
            and said["readers"]["translator"]["given"] is True
        )
        assert said["readers"]["vision"] == {
            "given": False,
            "without": "a picture is read for its words and not described",
        }
        assert said["masking"]["engine"] and set(said["masking"]["languages"]) == {"en", "fr"}
        assert said["jobs"]["backend"] == "memory" and said["jobs"]["storage"] is False
        assert (
            said["url_hosts"] == ["www.td.com", "*.td.com"] and said["max_upload_bytes"] == 1048576
        )
        assert said["paths"] == {"prefix": "", "gateway_paths": {}}

    def test_a_file_route_needs_the_key(self, deployed):
        _module, client = deployed
        headers = {"X-Filename": "a.txt"}
        assert client.post("/extract/txt", content=b"hello", headers=headers).status_code == 401
        assert (
            client.post(
                "/extract/txt", content=b"hello", headers={**headers, "api-key": "wrong"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/extract/txt",
                content=b"hello",
                headers={**headers, "api-key": DEPLOYED["VECTRIXDB_API_KEY"]},
            ).status_code
            == 200
        )

    def test_a_path_nobody_serves_is_still_not_found(self, deployed):
        _module, client = deployed
        assert client.get("/health/wiring/more").status_code == 404
        assert client.get("/nothing").status_code == 404


class TestWiringNeverCarriesASecret:
    def test_no_value_of_a_key_setting_appears(self, deployed):
        _module, client = deployed
        text = client.get("/health/wiring").text
        for name, value in DEPLOYED.items():
            if name.endswith("_KEY"):
                assert value not in text, name
        assert "cognitiveservices" not in text, "nor an endpoint"

    def test_the_source_asks_only_whether_a_key_is_there(self):
        source = (EXTRACTION_APP / "function_app.py").read_text(encoding="utf-8")
        body = source[source.index("def wiring(") : source.index("def face_of(")]
        assert "_KEY" not in body.replace("VECTRIXDB_EXTRACT_JOBS", ""), (
            "no key is named inside the wiring reply but through given()"
        )
        assert 'given(found, "VECTRIXDB_EXTRACT_STORAGE")' in body, "a connection is a yes or no"


class TestEveryWrongSettingIsNamedTogether:
    BAD = {
        "VECTRIXDB_EXTRACT_JOBS": "azure",
        "VECTRIXDB_EXTRACT_URL_HOSTS": "https://www.td.com, td.com",
        "VECTRIXDB_MASKING_ENGINE": "nlp",
        "AZURE_SPEECH_KEY": "half-of-it",
        "AZURE_TRANSLATOR_KEY": "a-key-with-no-region",
        "VECTRIXDB_MAX_UPLOAD_BYTES": "lots",
        "VECTRIXDB_EXTRACT_PDF": "ocr",
    }

    def test_each_is_one_line_and_none_is_lost(self, deployed):
        module, _client = deployed
        wrong = module.settings_hold(self.BAD)
        said = "\n".join(wrong)
        for expected in (
            "VECTRIXDB_API_KEY is not set",
            "VECTRIXDB_EXTRACT_JOBS=azure needs VECTRIXDB_EXTRACT_STORAGE or AzureWebJobsStorage",
            "'https://www.td.com', which is not a host",
            "VECTRIXDB_MASKING_ENGINE is 'nlp'",
            "AZURE_SPEECH_KEY is set and AZURE_SPEECH_ENDPOINT is not",
            "AZURE_TRANSLATOR_REGION is not",
            "VECTRIXDB_MAX_UPLOAD_BYTES is 'lots'",
            "VECTRIXDB_EXTRACT_PDF is 'ocr'",
        ):
            assert expected in said, expected
        assert "td.com'" not in said.replace("'https://www.td.com'", ""), "a plain host is a host"
        assert len(wrong) == 8

    def test_a_key_is_named_and_never_quoted(self, deployed):
        module, _client = deployed
        said = "\n".join(module.settings_hold(self.BAD))
        assert "half-of-it" not in said and "a-key-with-no-region" not in said

    def test_hold_raises_once_with_them_all(self, deployed):
        from vectrixdb.exceptions import ConfigurationError

        module, _client = deployed
        with pytest.raises(ConfigurationError) as refused:
            module.hold(self.BAD)
        assert (
            "8 things to change" in str(refused.value) and str(refused.value).count("\n  - ") == 8
        )

    def test_the_deployed_settings_hold(self, deployed):
        module, _client = deployed
        assert module.settings_hold(DEPLOYED) == []
        assert (
            module.settings_hold(
                {
                    **DEPLOYED,
                    "VECTRIXDB_EXTRACT_JOBS": "azure",
                    "AzureWebJobsStorage": "DefaultEndpointsProtocol=https;...",
                }
            )
            == []
        )

    def test_the_app_is_held_before_it_is_built(self):
        source = (EXTRACTION_APP / "function_app.py").read_text(encoding="utf-8")
        assert (
            source.index("\nhold()\n")
            < source.index("\nservice = readers()")
            < source.index("create_extraction_app(service)")
        )


class TestTheQueueTriggerDropsWhatIsNotAJob:
    def test_a_body_that_is_not_a_job_is_logged_by_length_and_dropped(self, deployed, caplog):
        module, _client = deployed
        ran = []
        with mock.patch.object(module, "run_extraction_job", lambda body: ran.append(body)):
            with caplog.at_level(logging.WARNING, logger="vectrixdb.extraction_app"):
                assert (
                    module.run_job(b"please transcribe https://secret.example/x", "m-41") is False
                )
                assert module.run_job(b'{"other": "thing"}') is False
                assert module.run_job(b"") is False
        assert ran == []
        said = caplog.text
        assert "m-41" in said and "42 bytes" in said and "dropped" in said
        assert "secret.example" not in said, "the length, never the content"

    def test_a_job_is_handed_to_the_library(self, deployed):
        module, _client = deployed
        ran = []
        with mock.patch.object(module, "run_extraction_job", lambda body: ran.append(body)):
            assert module.run_job(b'{"job": "j_3f9a0c2e7b1d4e65"}', "m-1") is True
        assert ran == [b'{"job": "j_3f9a0c2e7b1d4e65"}']

    def test_the_choice_is_justified_where_the_trigger_is(self):
        source = (EXTRACTION_APP / "function_app.py").read_text(encoding="utf-8")
        trigger = source[source.index("def extract_job(") :]
        assert "extract-jobs-poison" in trigger and "rejected on purpose" in trigger
        assert "run_job(message.get_body()" in trigger


class TestTheLogSaysWhatEachReaderDoesWithoutIt:
    def test_one_line_a_reader_present_or_absent(self, monkeypatch, caplog):
        environment = dict(os.environ)
        try:
            with caplog.at_level(logging.INFO, logger="vectrixdb.extraction_app"):
                module = _import(monkeypatch, DEPLOYED)
            lines = [
                record.getMessage()
                for record in caplog.records
                if record.getMessage().startswith("reader ")
            ]
            assert "reader speech: present" in lines
            assert (
                "reader document_intelligence: absent, so pictures answer 503, and a scanned PDF page comes back without its text"
                in lines
            )
            assert len(lines) == len(module.READERS) == 5
            assert any(
                record.getMessage().startswith("the extraction app reads ")
                for record in caplog.records
            ), "the one combined line is kept"
        finally:
            os.environ.clear()
            os.environ.update(environment)
            sys.modules.pop("function_app", None)

    def test_the_docstring_lists_the_wiring_route_and_the_check(self):
        import ast

        source = (EXTRACTION_APP / "function_app.py").read_text(encoding="utf-8")
        module = ast.get_docstring(ast.parse(source)) or ""
        assert "/health/wiring" in module and "settings_hold" in module
        settings = source[source.index("# SETTINGS: every variable") : source.index("READERS:")]
        for name in (
            "VECTRIXDB_VIDEO_FRAMES",
            "VECTRIXDB_SPEECH_SPEAKERS",
            "VECTRIXDB_EXTRACT_JOBS",
            "AZURE_TRANSLATOR_ENDPOINT, _KEY, _REGION",
        ):
            assert name in settings, f"{name} is read and the settings block does not list it"


class TestWhatItSaysIsJson:
    def test_the_wiring_reply_round_trips(self, deployed):
        module, _client = deployed
        said = module.wiring(module.service, module.library)
        assert json.loads(json.dumps(said)) == said
