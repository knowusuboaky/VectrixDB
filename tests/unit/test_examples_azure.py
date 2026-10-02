"""The Azure walkthrough, run without an Azure account.

Every script here shells out to ``az``, so none of them can be run in the
suite. What can be held to is everything that decides whether a novice's
account survives contact with them: that the settings turn into the right
names, that ``--dry-run`` runs nothing, that the delete really does cover
everything, and that the function code only uses names the library has.

The one thing that costs money if it is wrong is the resource group: every
resource goes into one so that deleting is a single command. A test holds
that, because a resource made outside it would be billed for ever.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import sys
import zipfile

import vectrixdb
from pathlib import Path

import pytest

AZURE = Path(__file__).resolve().parents[2] / "examples" / "azure"
if not AZURE.exists():
    pytest.skip(
        "examples/ is kept on the machine that runs it, not in the repository",
        allow_module_level=True,
    )

MAIN_FUNCTION_APP = AZURE / "main_function_app"
EXTRACTION_APP = AZURE / "extraction_app"
EXTRACT_SCRIPT = "05_create_extraction_function_app.py"
SCRIPTS = sorted(p for p in AZURE.glob("*.py") if p.name[0].isdigit())


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """The walkthrough with its own settings file, so nothing reads yours."""
    sys.path.insert(0, str(AZURE))
    import _common

    monkeypatch.setattr(_common, "SETTINGS", tmp_path / "settings.env")
    monkeypatch.setattr(_common, "STATE", tmp_path / "state.json")
    (tmp_path / "settings.env").write_text("VX_PREFIX=vxtest1234\n", encoding="utf-8")
    yield _common
    sys.path.remove(str(AZURE))


def flat(source: str) -> str:
    """A script's text with ruff's line breaks undone, so a call reads as one line.

    The formatter puts each argument of a long call on its own line, with a
    trailing comma; a test that looks for the call as a person would write it
    looks here.
    """
    one = re.sub(r"\s+", " ", source)
    one = re.sub(r"([(\[{]) ", r"\1", one)
    one = re.sub(r",? ([)\]}])", r"\1", one)
    return one


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), AZURE / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheSettings:
    def test_one_word_names_everything(self, folder):
        config = folder.settings()
        assert config["VX_RESOURCE_GROUP"] == "vxtest1234-rg"
        assert config["VX_STORAGE"] == "vxtest1234store", "no dashes: a storage account takes none"
        assert len(config["VX_STORAGE"]) <= 24, "a storage account name is at most 24 characters"
        assert (
            config["VX_SEARCH"] == "vxtest1234-search"
            and config["VX_FUNCTION_APP"] == "vxtest1234-fn"
            and config["VX_QUERY_APP"] == "vxtest1234-query"
        )

    def test_a_name_azure_would_refuse_is_refused_here_first(self, folder):
        for bad, why in (
            ("vx", "too short"),
            ("9vxtest", "starts with a digit"),
            ("vx-test-1", "has dashes"),
            ("", "empty"),
        ):
            folder.SETTINGS.write_text(f"VX_PREFIX={bad}\n", encoding="utf-8")
            with pytest.raises(SystemExit):
                folder.settings()

    def test_the_tier_is_one_of_two_and_says_so(self, folder):
        folder.SETTINGS.write_text(
            "VX_PREFIX=vxtest1234\nVX_SEARCH_TIER=standard\n", encoding="utf-8"
        )
        with pytest.raises(SystemExit):
            folder.settings()

    def test_a_name_can_be_overridden_one_at_a_time(self, folder):
        folder.SETTINGS.write_text(
            "VX_PREFIX=vxtest1234\nVX_STORAGE=somethingelse\n", encoding="utf-8"
        )
        config = folder.settings()
        assert (
            config["VX_STORAGE"] == "somethingelse" and config["VX_SEARCH"] == "vxtest1234-search"
        )

    def test_no_settings_file_says_which_one_to_copy(self, folder):
        folder.SETTINGS.unlink()
        with pytest.raises(SystemExit):
            folder.settings()

    def test_yes_is_read_the_way_a_person_writes_it(self, folder):
        for written in ("yes", "Yes", "true", "1", "on"):
            assert folder.wants({"VX_OPENAI": written}, "VX_OPENAI") is True
        for written in ("no", "No", "false", "", "0"):
            assert folder.wants({"VX_OPENAI": written}, "VX_OPENAI") is False


class TestNothingHappensInADryRun:
    def test_no_command_is_ever_run(self, folder, capsys):
        az = folder.Az(pretend=True)
        assert az("group", "create", "--name", "x") is None
        assert az("group", "show", "--name", "x", reads=True) is None
        assert az.exists("group", "show", "--name", "x") is False, (
            "nothing exists, so every script makes everything"
        )
        printed = capsys.readouterr().out
        assert "$ az group create --name x" in printed, "and every command is printed to be read"

    def test_a_look_reads_in_a_dry_run_and_the_dry_run_goes_on_pretending(
        self, folder, monkeypatch
    ):
        """The two pushes look before they plan: a read that runs even in a dry run."""
        az = folder.Az(pretend=True)
        az.path = "az"
        during = []

        def call(self, *args, **kwargs):
            during.append((self.pretend, args, kwargs))
            return {"answer": 1}

        monkeypatch.setattr(folder.Az, "__call__", call)
        assert az.look("storage", "blob", "show", "--name", "x") == {"answer": 1}
        assert during == [
            (
                False,
                ("storage", "blob", "show", "--name", "x"),
                {"reads": True, "quiet": True, "allow_fail": True},
            )
        ], "quiet, and never a stop"
        assert az.pretend is True

    def test_a_look_with_no_command_line_answers_nothing(self, folder):
        az = folder.Az(pretend=True)
        az.path = None
        assert az.look("storage", "account", "keys", "list") is None

    def test_every_script_takes_it(self):
        for script in SCRIPTS:
            assert "--dry-run" in script.read_text(
                encoding="utf-8"
            ) or "arguments(" in script.read_text(encoding="utf-8"), script.name


class TestTheKeysAreNotWrittenDown:
    def test_state_holds_addresses_and_never_a_key(self, folder):
        folder.remember(
            search_endpoint="https://x.search.windows.net",
            blob_account="https://y.blob.core.windows.net",
        )
        written = folder.STATE.read_text(encoding="utf-8")
        assert "search_endpoint" in written
        for script in SCRIPTS:
            source = script.read_text(encoding="utf-8")
            assert (
                "remember(" not in source
                or "key" not in source.split("remember(")[1].split(")")[0].lower()
            ), script.name

    def test_a_key_shown_on_screen_is_shown_as_dots(self):
        """And a secret: the sign-in secret, the SSO client secret and the emergency password and code are printed with the settings too."""
        source = (AZURE / "06_create_main_function_app.py").read_text(encoding="utf-8")
        assert "shown = {k: shown_as(k, v) for k, v in every.items()}" in source
        script = load("06_create_main_function_app.py")
        for name in (
            "AZURE_SEARCH_KEY",
            "VECTRIXDB_SIGNIN_SECRET",
            "VECTRIXDB_OIDC_CLIENT_SECRET",
            "VECTRIXDB_BREAK_GLASS_PASSWORD",
            "VECTRIXDB_BREAK_GLASS_TOTP",
        ):
            assert script.shown_as(name, "held") == "...", name
        assert script.shown_as("VECTRIXDB_PUBLIC_URL", "https://x") == "https://x"


class TestWhatTheFirstLiveRunFound:
    """Two faults a real subscription found that no mock had. Both are held here now."""

    def test_a_secret_is_never_printed_in_a_command(self, folder, capsys):
        """The first live run printed a storage account key in full, three times."""
        az = folder.Az(pretend=True)
        az("storage", "container", "create", "--name", "x", "--account-key", "A-REAL-SECRET-KEY")
        az(
            "functionapp",
            "config",
            "appsettings",
            "set",
            "--settings",
            "AZURE_SEARCH_KEY=ANOTHER-SECRET",
            "PORT=8000",
        )
        printed = capsys.readouterr().out
        assert "A-REAL-SECRET-KEY" not in printed and "ANOTHER-SECRET" not in printed
        assert "--account-key ..." in printed, "the flag is still shown, so the command reads true"
        assert "AZURE_SEARCH_KEY=..." in printed and "PORT=8000" in printed, (
            "and what is not a secret is not hidden"
        )

    def test_every_flag_that_carries_a_secret_is_covered(self, folder):
        for flag in (
            "--account-key",
            "--password",
            "--key",
            "--secret",
            "--admin-key",
            "--connection-string",
        ):
            assert flag in folder.SECRET_FLAGS, flag
        assert folder._looks_secret("AZURE_SEARCH_KEY") and folder._looks_secret("api-token")
        assert not folder._looks_secret("INGEST_QUEUE") and not folder._looks_secret("PORT")

    def test_a_command_that_answers_exists_false_is_not_a_finding(self, folder, monkeypatch):
        """az storage queue exists succeeds and answers {"exists": false}, so 01
        skipped making the queue and said it was already there. Event Grid then
        had nowhere to write, and the whole thing would have quietly done nothing."""
        # The Azure command line need not be installed for this: every call is faked.
        monkeypatch.setattr(folder.shutil, "which", lambda name: "az")
        az = folder.Az(pretend=False)
        monkeypatch.setattr(folder.Az, "__call__", lambda self, *a, **k: {"exists": False})
        assert az.exists("storage", "queue", "exists", "--name", "ingest") is False
        monkeypatch.setattr(folder.Az, "__call__", lambda self, *a, **k: {"exists": True})
        assert az.exists("storage", "queue", "exists", "--name", "ingest") is True
        monkeypatch.setattr(folder.Az, "__call__", lambda self, *a, **k: {"name": "a-thing"})
        assert az.exists("group", "show", "--name", "x") is True, (
            "an ordinary show still means it is there"
        )
        monkeypatch.setattr(folder.Az, "__call__", lambda self, *a, **k: None)
        assert az.exists("group", "show", "--name", "x") is False


class TestANewSubscriptionHasNothingSwitchedOn:
    """Every provider is off on a new subscription, and Azure says SubscriptionNotFound."""

    def test_every_service_this_makes_has_its_provider_listed(self, folder):
        for namespace in (
            "Microsoft.Storage",
            "Microsoft.Search",
            "Microsoft.Web",
            "Microsoft.CognitiveServices",
            "Microsoft.EventGrid",
        ):
            assert namespace in folder.PROVIDERS, namespace
        assert all(why for why in folder.PROVIDERS.values()), "each says what it is for"

    def test_the_first_script_that_makes_anything_registers_them(self):
        source = (AZURE / "01_create_resources.py").read_text(encoding="utf-8")
        assert "register(az)" in source
        assert source.index("register(az)") < source.index('step("The resource group")'), (
            "before the first create"
        )

    def test_it_waits_rather_than_asking_once(self):
        source = (AZURE / "_common.py").read_text(encoding="utf-8")
        body = source[source.index("def register(") :]
        assert "time.sleep" in body and "Registered" in body, "registering is asynchronous"

    def test_00_looks_but_does_not_change(self):
        source = (AZURE / "00_login.py").read_text(encoding="utf-8")
        assert "PROVIDERS" in source and "provider register" not in source, "00 makes nothing"


class TestEverythingIsInOneGroup:
    """The whole of the money protection: one group, so one delete covers it."""

    #: Things made inside something else, which take their group from it.
    INSIDE = (
        "storage container",
        "storage queue",
        "storage blob",
        "eventgrid event-subscription",
        "role assignment",
    )

    def test_every_create_names_the_resource_group(self):
        """A resource made without one lands somewhere the delete will never look."""
        for script in SCRIPTS:
            for call in self.az_calls(script):
                if "create" not in call:
                    continue
                first_two = " ".join(call[:2])
                if first_two in self.INSIDE or first_two.startswith("group"):
                    continue
                assert "--resource-group" in call, (
                    f"{script.name}: az {' '.join(call[:4])} does not name the group"
                )

    @staticmethod
    def az_calls(script: Path):
        """Every ``az(...)`` in a script, as the list of words it passes."""
        tree = ast.parse(script.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            named = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if named not in ("az", "__call__"):
                continue
            yield [
                a.value
                for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            ]

    def test_the_delete_removes_the_group_itself(self):
        source = (AZURE / "99_delete_everything.py").read_text(encoding="utf-8")
        assert '"group", "delete", "--name", group, "--yes"' in flat(source)
        assert "typed != group" in source, "and it asks you to type the name first"

    def test_the_delete_lists_what_it_will_take_with_it(self):
        source = (AZURE / "99_delete_everything.py").read_text(encoding="utf-8")
        assert '"resource", "list"' in source


class TestTheFunctionCode:
    def test_it_only_uses_names_the_library_has(self):
        import vectrixdb

        for name in ("function_app.py", "readers.py", "ingest_queue.py", "golden_files.py"):
            tree = ast.parse((MAIN_FUNCTION_APP / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.startswith("vectrixdb")
                ):
                    module = importlib.import_module(node.module)
                    for alias in node.names:
                        assert hasattr(module, alias.name) or alias.name in getattr(
                            vectrixdb, "__all__", []
                        ), f"{name}: {node.module}.{alias.name}"

    def test_the_queue_reader_is_the_same_file_as_the_reference(self):
        """Two copies that drift are worse than one, so they are held equal."""
        reference = AZURE.parent / "deploy" / "azure_ingest_queue" / "ingest_queue.py"
        assert (MAIN_FUNCTION_APP / "ingest_queue.py").read_bytes() == reference.read_bytes()

    def test_one_message_at_a_time_with_an_hour_for_it(self):
        host = json.loads((MAIN_FUNCTION_APP / "host.json").read_text(encoding="utf-8"))
        assert host["functionTimeout"] == "01:00:00", "an hour of audio does not fit in ten minutes"
        queues = host["extensions"]["queues"]
        assert queues["batchSize"] == 1 and queues["maxDequeueCount"] == 5

    def test_a_failed_read_goes_round_again(self):
        source = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        assert "raise ExtractionError" in source and "should_go_round_again" in source

    def test_no_secret_is_in_the_example_settings(self):
        values = json.loads(
            (MAIN_FUNCTION_APP / "local.settings.example.json").read_text(encoding="utf-8")
        )["Values"]
        assert not any("key" in name.lower() and value for name, value in values.items())

    def test_what_it_installs_is_what_it_imports(self):
        needs = (MAIN_FUNCTION_APP / "requirements.txt").read_text(encoding="utf-8")
        for package in (
            "azure-functions",
            "azure-identity",
            "azure-storage-blob",
            "azure-storage-queue",
            "vectrixdb[",
            "pillow",
        ):
            assert package in needs, package
        wheel = next(line for line in needs.splitlines() if line.startswith("./vectrixdb-"))
        assert "ffmpeg" in wheel.split("[", 1)[1].rstrip("]").split(","), (
            "ffmpeg comes through the library's own extra"
        )
        assert "\nimageio-ffmpeg" not in needs, "so the library, not this file, says which version"
        assert "faster-whisper" not in needs, (
            "Azure Speech transcribes, so the deployment stays small"
        )


class TestPicturesInsideADocument:
    """The reader that used to live here is gone: the library keeps them now."""

    def test_no_workaround_is_left_behind(self):
        source = (MAIN_FUNCTION_APP / "readers.py").read_text(encoding="utf-8")
        assert "PdfWithPictures" not in source and ".pdf" not in str(
            source.split("def extractors_from_environment")[1]
        )

    def test_the_worker_is_what_keeps_them_now(self):
        import inspect

        from vectrixdb import worker

        assert "wants_images" in inspect.getsource(worker.IngestWorker.handle)


class TestTheDescriberComesFromTheLibrary:
    """This folder used to carry its own, and it was the worse half of one."""

    def test_the_example_does_not_reimplement_it(self):
        source = (MAIN_FUNCTION_APP / "readers.py").read_text(encoding="utf-8")
        assert "class VisionDescriber" not in source, (
            "120 lines that the library already had, better"
        )
        assert "from vectrixdb.extract.describers import describer_from_environment" in source

    def test_what_it_hands_back_is_what_the_library_asked_for(self):
        """A string throws away three of the four things describe_figures can use."""
        from vectrixdb.extract.engines import AzureImageAnalysis

        asking = AzureImageAnalysis(
            "https://v.cognitiveservices.azure.com",
            "k",
            transport=lambda url, image: {
                "captionResult": {"text": "a bar chart", "confidence": 0.7},
                "readResult": {
                    "blocks": [
                        {
                            "lines": [
                                {
                                    "text": "Q1",
                                    "boundingPolygon": [
                                        {"x": 10, "y": 40},
                                        {"x": 25, "y": 40},
                                        {"x": 25, "y": 50},
                                        {"x": 10, "y": 50},
                                    ],
                                },
                                {
                                    "text": "Revenue",
                                    "boundingPolygon": [
                                        {"x": 0, "y": 0},
                                        {"x": 60, "y": 0},
                                        {"x": 60, "y": 10},
                                        {"x": 0, "y": 10},
                                    ],
                                },
                            ]
                        }
                    ]
                },
            },
        )
        said = asking(b"PNG", {"caption": ""})
        assert said["caption"] == "a bar chart", "a figure with no caption is given one"
        assert "Revenue; Q1" in said["description"], (
            "read in the order a person reads it, not as emitted"
        )

    def test_it_is_the_librarys_chain_built_from_the_settings(self):
        """Vision alone today; a chat model that can see goes in front of it the day one is deployed."""
        sys.path.insert(0, str(MAIN_FUNCTION_APP))
        try:
            import readers

            vision = {
                "AZURE_VISION_ENDPOINT": "https://v.cognitiveservices.azure.com",
                "AZURE_VISION_KEY": "k",
            }
            assert readers.describer_from_environment({}) is None
            assert (
                readers.describer_from_environment({"AZURE_VISION_ENDPOINT": "https://v.x.com"})
                is None
            )
            assert readers.describer_from_environment(vision).labels == ["azure-image-analysis"]
            model = {
                "AZURE_OPENAI_ENDPOINT": "https://o.openai.azure.com",
                "AZURE_OPENAI_KEY": "k",
                "AZURE_OPENAI_VISION_DEPLOYMENT": "gpt-4o",
            }
            assert readers.describer_from_environment({**vision, **model}).labels == [
                "gpt-4o",
                "azure-image-analysis",
            ]
        finally:
            sys.path.remove(str(MAIN_FUNCTION_APP))


class TestAScannedPageIsRead:
    """The hole the walkthrough had: a PDF with no text layer was detected and not fixed."""

    def test_nothing_is_registered_for_pdf(self):
        """Registering one would replace the reader and take every figure with it."""
        sys.path.insert(0, str(MAIN_FUNCTION_APP))
        try:
            import readers

            # Speech alone: the Document Intelligence half wants azure.ai,
            # which a machine that only runs the tests does not have.
            wired = readers.extractors_from_environment(
                {
                    "AZURE_SPEECH_ENDPOINT": "https://s.cognitiveservices.azure.com",
                    "AZURE_SPEECH_KEY": "k",
                }
            )
            assert ".pdf" not in wired and ".wav" in wired and ".mp4" in wired
        finally:
            sys.path.remove(str(MAIN_FUNCTION_APP))
        # And the picture half claims only the picture suffixes.
        wiring = (MAIN_FUNCTION_APP / "readers.py").read_text(encoding="utf-8")
        assert "for suffix in IMAGE_SUFFIXES" in wiring
        assert '".pdf"' not in wiring.split("def extractors_from_environment")[1]

    def test_a_page_reader_is_built_from_document_intelligence_and_nothing_new(self):
        source = (MAIN_FUNCTION_APP / "readers.py").read_text(encoding="utf-8")
        assert "def page_reader_from_environment" in source
        assert "AZURE_DOCINTEL_ENDPOINT" in source.split("def page_reader_from_environment")[1]
        assert "prebuilt-read" in source.split("def page_reader_from_environment")[1]

    def test_the_collection_is_opened_with_it(self):
        source = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        readers = source.split("def file_readers(")[1].split("\ndef ")[0]
        assert '"page_ocr": page_reader_from_environment()' in readers
        assert "reading = file_readers()" in source and "**reading," in source


class TestWhoReadsWhat:
    def test_with_no_services_nothing_is_claimed_and_the_library_reads_it_all(self):
        sys.path.insert(0, str(MAIN_FUNCTION_APP))
        try:
            from readers import extractors_from_environment

            assert extractors_from_environment({}) == {}, (
                "a PDF and a Word file are the library's own job"
            )
        finally:
            sys.path.remove(str(MAIN_FUNCTION_APP))

    def test_speech_claims_the_audio_and_the_video(self):
        sys.path.insert(0, str(MAIN_FUNCTION_APP))
        try:
            from readers import extractors_from_environment, what_reads_what

            readers = extractors_from_environment(
                {
                    "AZURE_SPEECH_ENDPOINT": "https://s.cognitiveservices.azure.com",
                    "AZURE_SPEECH_KEY": "k",
                }
            )
            assert ".wav" in readers and ".mp4" in readers and ".pdf" not in readers
            assert type(readers[".mp4"]).__name__ == "Video", "a video goes through ffmpeg first"
            assert "video" in what_reads_what(readers)
        finally:
            sys.path.remove(str(MAIN_FUNCTION_APP))


class TestWhichCollectionABlobBelongsTo:
    """A guess here puts a client's report in an index anybody can search."""

    ENV = {"INGEST_COLLECTIONS": "financial,media,misc"}

    def where(self):
        sys.path.insert(0, str(MAIN_FUNCTION_APP))
        try:
            import collections_of

            return collections_of
        finally:
            sys.path.remove(str(MAIN_FUNCTION_APP))

    def test_the_folder_names_the_collection_and_a_folder_under_it_is_only_part_of_the_name(self):
        routing = self.where()
        account = "https://acct.blob.core.windows.net"
        assert (
            routing.collection_of(f"{account}/ingestion/raw/financial/td/ar2025.pdf", self.ENV)
            == "financial"
        )
        assert (
            routing.collection_of(f"{account}/ingestion/raw/media/toddler.mp4", self.ENV) == "media"
        )
        assert routing.collection_of(f"{account}/ingestion/raw/misc/office.png", self.ENV) == "misc"
        assert (
            routing.doc_id_of(f"{account}/ingestion/raw/financial/td/ar2025.pdf") == "td/ar2025.pdf"
        )

    def test_a_blob_that_names_no_collection_is_left_alone(self):
        routing = self.where()
        account = "https://acct.blob.core.windows.net"
        for uri in (
            f"{account}/ingestion/raw/loose.pdf",
            f"{account}/ingestion/raw/unknown/x.pdf",
            f"{account}/ingestion/markdown/financial/td/ar2025.pdf.md",
            f"{account}/ingestion",
        ):
            assert routing.collection_of(uri, self.ENV) is None, uri

    def test_a_name_with_spaces_in_it_is_read_as_it_was_written(self):
        routing = self.where()
        found = routing.collection_of(
            "https://a.blob.core.windows.net/ingestion/raw/financial/td/q3%20report.pdf", self.ENV
        )
        assert found == "financial"

    def test_a_chunk_carries_its_collection_and_nothing_a_rule_reads(self):
        """Who may retrieve is the record's, on the server: nothing on a chunk decides it."""
        routing = self.where()
        account = "https://acct.blob.core.windows.net"
        assert routing.metadata_for(f"{account}/ingestion/raw/financial/td/x.pdf", self.ENV) == {
            "collection": "financial"
        }
        assert routing.metadata_for(f"{account}/ingestion/raw/media/x.mp4", self.ENV) == {
            "collection": "media"
        }
        assert routing.metadata_for(f"{account}/ingestion/raw/nowhere/x.pdf", self.ENV) == {}
        assert not hasattr(routing, "policy_for") and not hasattr(routing, "policied"), (
            "no rule is built from a setting"
        )

    def test_the_settings_and_the_function_agree_on_the_names(self, folder):
        """Two lists that can disagree is a document written nowhere."""
        folder.SETTINGS.write_text(
            "VX_PREFIX=vxtest1234\nVX_COLLECTIONS=financial,media,misc\n", encoding="utf-8"
        )
        config = folder.settings()
        every = folder.env_of(config, {}, {})
        assert every["INGEST_COLLECTIONS"] == "financial,media,misc"
        assert not any(name in every for name in ("INGEST_POLICIED", "INGEST_CLIENT")), (
            "no collection is policied by a setting"
        )
        assert folder.collections(config) == ["financial", "media", "misc"]


class TestTheMirrorIsTheRoute:
    """Where a file sits on disk is where it goes in the container, because the tree is the container's."""

    def test_every_file_is_listed_under_a_collection_folder(self, folder):
        listed = json.loads((AZURE / ".local" / "sources.json").read_text(encoding="utf-8"))
        named = set(folder.settings()["VX_COLLECTIONS"].split(","))
        for batch in ("start", "later"):
            for entry in listed[batch]:
                where = entry.get("collection", "")
                assert where, f"{entry['name']} has no folder, so 04 could not place it"
                assert where.split("/")[0] in named, (
                    f"{entry['name']} is under {where}, not a collection"
                )

    def test_the_files_walk_is_recursive_or_a_tree_is_invisible(self, folder, tmp_path):
        tmp_path = tmp_path / "batch"
        (tmp_path / "financial" / "td").mkdir(parents=True)
        (tmp_path / "financial" / "td" / "a.pdf").write_text("x", encoding="utf-8")
        (tmp_path / "media").mkdir()
        (tmp_path / "media" / "b.wav").write_text("x", encoding="utf-8")
        (tmp_path / ".gitignore").write_text("x", encoding="utf-8")
        found = [p.relative_to(tmp_path).as_posix() for p in folder.local_files(tmp_path)]
        assert found == ["financial/td/a.pdf", "media/b.wav"], found

    def test_the_blob_name_is_the_path_under_the_mirror(self):
        """No lookup: the path is the route, so moving a file moves the document."""
        source = (AZURE / "_common.py").read_text(encoding="utf-8")
        start = source.index("def push_raw(")
        uploader = source[start:].split("\ndef ")[0]
        assert "path.relative_to(where).as_posix()" in uploader, (
            "the path under the mirror is the blob's"
        )
        assert "sources.json" not in uploader, "the uploader reads the tree, not a manifest"

    def test_a_file_in_no_collection_folder_is_refused_not_guessed(self):
        source = (AZURE / "_common.py").read_text(encoding="utf-8")
        assert "sits in no collection folder" in source and "is not uploaded" in source
        assert "which is not one of" in source, "and a folder that is not a collection is named"

    def test_04_writes_into_the_collection_folder(self):
        source = (AZURE / "04_push_local_to_blob_cosmosdb.py").read_text(encoding="utf-8")
        assert "folder = root / where if where else root" in source
        assert 'INTO = {"start": RAW_FILES, "later": LATER}' in source, (
            "the first drop into the mirror, the second onto the shelf"
        )

    def test_the_mirror_is_laid_out_as_the_container_is(self, folder):
        """The path under blob/ is the blob's own path, so a push is a copy and nothing is worked out."""
        assert folder.DATA == "data_db", (
            "the account folder under blob/, and the records' database under cosmosdb/"
        )
        assert not hasattr(folder, "ACCESS"), (
            "the access database is gone; the records live in data_db"
        )
        assert (
            folder.RAW_FILES == folder.LOCAL / "blob" / folder.DATA / folder.INGESTION / folder.RAW
        )
        assert (
            folder.GOLDEN_FILES
            == folder.LOCAL / "blob" / folder.DATA / folder.EVALS / "golden_dataset"
        )
        assert (
            folder.RECORD_FILES
            == folder.LOCAL / "cosmosdb" / folder.DATA / folder.COLLECTION_RECORDS
        )
        assert folder.LATER == folder.LOCAL / "later", (
            "the shelf is not under blob/, because it is not in the container"
        )

    def test_only_the_two_files_that_explain_the_mirror_are_committed(self):
        ignored = (AZURE / ".local" / ".gitignore").read_text(encoding="utf-8")
        rules = [
            line.strip()
            for line in ignored.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        for name in ("blob/", "later/"):
            assert name in rules, f"{name} holds somebody else's files, or a run's"
        for kept in ("README.md", "sources.json"):
            assert kept not in rules, f"{kept} is the folder's own and stays in git"

    def test_the_shelf_moves_into_the_mirror_before_anything_is_sent(self):
        """Otherwise the mirror would show a file the container has not got."""
        source = (AZURE / "07_push_new_files_from local_to_blob_cosmosdb.py").read_text(
            encoding="utf-8"
        )
        assert "shutil.move(str(path), str(target))" in source
        main = source.split("\ndef main(")[1]
        assert main.index("off_the_shelf(args.dry_run)") < main.index("files(az, config")


class TestADryRunOfThePushesLooksFirst:
    """07's dry run listed every file as going up and ended "5 files uploaded", though the container held all five and nothing moved."""

    class Az:
        pretend = True

        def __init__(self, blobs=None, key="the-key"):
            self.blobs, self.key = blobs if blobs is not None else {}, key
            self.looked, self.printed = [], []

        def look(self, *args):
            self.looked.append(args)
            if args[:4] == ("storage", "account", "keys", "list"):
                return [{"value": self.key}] if self.key else None
            if args[:3] == ("storage", "blob", "show"):
                name = args[args.index("--name") + 1]
                return (
                    {"properties": {"contentLength": self.blobs[name]}}
                    if name in self.blobs
                    else None
                )
            return None

        def __call__(self, *args, **_):
            self.printed.append(args)

    @staticmethod
    def mirror(root, files):
        for inside, text in files.items():
            made = root / inside
            made.parent.mkdir(parents=True, exist_ok=True)
            made.write_text(text, encoding="utf-8")
        return root

    def test_what_is_there_at_its_size_is_said_and_only_the_rest_is_planned(
        self, folder, tmp_path, capsys
    ):
        where = self.mirror(
            tmp_path / "raw", {"financial/td/a.pdf": "aaaa", "media/b.wav": "bb", "misc/c.png": "c"}
        )
        shelf = self.mirror(tmp_path / "later", {"media/d.mp4": "dddd"})
        az = self.Az(blobs={"raw/financial/td/a.pdf": 4, "raw/media/b.wav": 99})
        planned = folder.push_raw(
            az, folder.settings(), folder=where, also=[(shelf / "media" / "d.mp4", "media/d.mp4")]
        )
        said = capsys.readouterr().out
        assert planned == 3, "b at another size, c not there, d still on the shelf"
        assert "have raw/financial/td/a.pdf, same size already there" in said
        for blob in ("raw/media/b.wav", "raw/misc/c.png", "raw/media/d.mp4"):
            assert f"would upload {blob}," in said, blob
        assert "  ok   " not in said, "nothing is said to have gone up"
        assert all(
            args[:3] == ("storage", "blob", "show")
            or args[:4] == ("storage", "account", "keys", "list")
            for args in az.looked
        ), "a look only reads"
        assert [args[:3] for args in az.printed] == [("storage", "blob", "upload")] * 3, (
            "the uploads are only printed"
        )

    def test_one_that_cannot_look_plans_every_file_and_says_so(self, folder, tmp_path, capsys):
        where = self.mirror(tmp_path / "raw", {"financial/a.pdf": "a", "misc/c.png": "c"})
        az = self.Az(key=None)
        assert folder.push_raw(az, folder.settings(), folder=where) == 2
        said = capsys.readouterr().out
        assert "could not look inside" in said and said.count("would upload ") == 2
        assert len(az.looked) == 1, "with no key, no blob is asked about"

    def test_07_plans_the_shelf_and_says_would_and_04_says_would(self):
        seven = (AZURE / "07_push_new_files_from local_to_blob_cosmosdb.py").read_text(
            encoding="utf-8"
        )
        files = flat(seven.split("def files(")[1].split("\ndef ")[0])
        assert (
            "if pretend else []" in files
            and "push_raw(az, config, again=again, also=shelf)" in files
        )
        main = seven.split("\ndef main(")[1]
        assert (
            "moved = off_the_shelf(args.dry_run)" in main
            and "a dry run, so nothing moved:" in main
            and "would go up to" in main
        )
        four = (AZURE / "04_push_local_to_blob_cosmosdb.py").read_text(encoding="utf-8")
        assert "a dry run, so nothing was sent:" in four and "would go up, each an event" in four


class TestTheRetrievalHelper:
    """What every retrieval through the server holds to, whichever pick it asks for."""

    def test_every_method_a_run_can_name_has_a_route(self):
        """A pick the scripts cannot run is a pick nobody can try."""
        sys.path.insert(0, str(AZURE))
        try:
            from _retrieve import ROUTES
            from vectrixdb._setups import _search_kwargs

            for method in ROUTES:
                assert _search_kwargs(method), method
            for method in (
                "dense",
                "hybrid",
                "hybrid_reranked",
                "hybrid_semantic",
                "keyword",
                "keyword_semantic",
            ):
                assert method in ROUTES, f"a run can pick {method} and nothing here runs it"
        finally:
            sys.path.remove(str(AZURE))

    def test_they_search_through_the_server_which_is_what_fills_the_dashboard(self):
        source = (AZURE / "_retrieve.py").read_text(encoding="utf-8")
        assert "localhost:{port}" in source and "access log" in source
        assert 'f"retrieve-{pick.replace' in source, "a key a pick, so Access shows three callers"

    def test_the_gap_in_what_rest_carries_is_said_and_not_hidden(self):
        source = (AZURE / "_retrieve.py").read_text(encoding="utf-8")
        assert 'wanted.get("vectors")' in source and "do not carry that choice" in source


class TestTheBudget:
    """The only script here that protects the money rather than spending it."""

    SCRIPT = AZURE / "00b_set_budget.py"

    def test_it_runs_before_anything_that_can_spend(self):
        order = [p.name for p in SCRIPTS]
        assert order.index("00b_set_budget.py") < order.index("01_create_resources.py")
        assert "00b_set_budget.py" in (AZURE / "00_login.py").read_text(encoding="utf-8"), (
            "and 00 says to run it"
        )

    def test_it_covers_the_subscription_not_one_resource_group(self):
        """A resource made by hand outside the group is the kind that gets forgotten."""
        source = self.SCRIPT.read_text(encoding="utf-8")
        assert "/subscriptions/{subscription}/providers/Microsoft.Consumption/budgets/" in source
        assert "resourceGroups" not in source

    def test_the_alerts_include_one_on_the_forecast(self):
        """Actual spend tells you afterwards; the forecast tells you while there is credit left."""
        import importlib.util

        spec = importlib.util.spec_from_file_location("budget", self.SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(AZURE))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(AZURE))
        made = module.alerts(25.0, ["you@example.com"])
        kinds = {n["thresholdType"] for n in made.values()}
        assert kinds == {"Actual", "Forecasted"}, kinds
        assert len(made) == len(module.SPENT_AT) + 1
        assert all(
            n["enabled"] and n["contactEmails"] == ["you@example.com"] for n in made.values()
        )

    def test_a_monthly_budget_starts_on_the_first_which_azure_insists_on(self):
        import importlib.util
        from datetime import datetime, timezone

        spec = importlib.util.spec_from_file_location("budget2", self.SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(AZURE))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(AZURE))
        when = module.months(datetime(2026, 9, 20, 13, 45, tzinfo=timezone.utc))
        assert (
            when["startDate"] == "2026-09-01T00:00:00Z"
            and when["endDate"] == "2027-09-01T00:00:00Z"
        )

    def test_a_budget_with_nobody_to_tell_is_refused(self):
        source = self.SCRIPT.read_text(encoding="utf-8")
        assert "A budget with nobody to tell is a budget that tells nobody." in source

    def test_one_budget_replaced_rather_than_many(self):
        source = self.SCRIPT.read_text(encoding="utf-8")
        assert '"--method", "put"' in flat(source), "a put replaces; a post would add a second"
        assert 'NAME = "vectrixdb-walkthrough"' in source

    def test_removing_it_does_not_remove_anything_that_costs_money(self):
        source = self.SCRIPT.read_text(encoding="utf-8")
        assert "99_delete_everything.py is what removes those" in source


class TestEveryStepSaysWhereToLook:
    """Somewhere to click after every step, so a person can see it for themselves."""

    def test_every_script_ends_with_somewhere_to_look(self):
        for script in SCRIPTS:
            source = script.read_text(encoding="utf-8")
            if "_retrieve import" in source:
                continue  # the three wrappers; the work, and the links, are in _retrieve.py
            assert "links(" in source, f"{script.name} finishes without saying where to look"
        assert "links(" in (AZURE / "_retrieve.py").read_text(encoding="utf-8")

    def test_a_portal_link_goes_to_the_resource_and_not_a_search_box(self, folder):
        folder.remember(subscription="sub-1", resource_group="rg-1")
        made = folder.portal("search", "a-search", {"VX_RESOURCE_GROUP": "rg-1"})
        assert (
            made.startswith("https://portal.azure.com/")
            and "/subscriptions/sub-1/resourceGroups/rg-1/" in made
        )
        assert made.endswith("/providers/Microsoft.Search/searchServices/a-search/overview")
        assert folder.portal(
            "search", "a-search", {"VX_RESOURCE_GROUP": "rg-1"}, page="indexes"
        ).endswith("/a-search/indexes")

    def test_a_link_is_left_out_rather_than_written_broken(self, folder):
        assert folder.portal("search", "a-search", {"VX_RESOURCE_GROUP": ""}, kept={}) == ""
        assert folder.subscription_link("budgets", {}) == ""
        assert folder.folder_link(Path("nowhere-at-all")) == ""

    def test_a_folder_on_this_machine_is_a_clickable_address(self, tmp_path, folder):
        assert folder.folder_link(tmp_path).startswith("file:///")

    def test_nothing_with_an_empty_address_is_printed(self, folder, capsys):
        folder.links(("here", "https://example.test"), ("nowhere", ""))
        printed = capsys.readouterr().out
        assert "here" in printed and "nowhere" not in printed


class TestTheWalkthroughReadsAsOne:
    def test_the_scripts_are_numbered_in_the_order_they_are_run(self):
        """A folder lists them by name, so by name is the order they run in.

        Two may share a number when their names sort in the order they run:
        00_login before 00b_set_budget, underscore before a letter.
        """

        def number(script):
            found = re.match(r"(\d+)([a-z]?)(?:_(\d+))?_", script.name)
            assert found, f"{script.name} starts with no step number"
            return int(found.group(1)), found.group(2), int(found.group(3) or 0)

        numbers = sorted(number(p) for p in SCRIPTS)
        assert numbers[0][0] == 0 and numbers[-1][0] == 99
        readme = (AZURE / "README.md").read_text(encoding="utf-8")
        # The steps block only, and a quoted name is still a name: one shown
        # earlier as an example, like the budget's --show, is not the order
        # anything runs in.
        steps = readme.split("## The steps", 1)[1].split("\n## ", 1)[0]
        run = []
        for name in re.findall(r'^python "?(\d[^"\n]*?\.py)"?', steps, re.MULTILINE):
            if name not in run:
                run.append(name)
        assert run == [p.name for p in SCRIPTS], (
            "the order the README runs them in is the order a folder lists them"
        )

    def test_each_one_says_what_to_run_next(self):
        """Except the three retrievals, which are alternatives to each other, and the last."""
        for script in SCRIPTS:
            source = script.read_text(encoding="utf-8")
            if "_retrieve import" in source:
                continue
            assert "finish(" in source, script.name

    def test_the_readme_lists_every_script(self):
        readme = (AZURE / "README.md").read_text(encoding="utf-8")
        for script in SCRIPTS:
            assert script.name in readme, script.name

    def test_the_readme_says_what_it_costs_and_how_to_stop_it(self):
        readme = (AZURE / "README.md").read_text(encoding="utf-8")
        assert "budget" in readme.lower() and "billed by the hour" in readme
        assert "99_delete_everything.py" in readme

    def test_every_source_has_a_url_or_a_path_and_a_reason(self):
        listed = json.loads((AZURE / ".local" / "sources.json").read_text(encoding="utf-8"))
        assert set(listed) >= {"start", "later"}
        for batch in ("start", "later"):
            assert listed[batch], batch
            for entry in listed[batch]:
                assert entry["name"] and entry["what"], entry
                assert bool(entry.get("url")) != bool(entry.get("path")), (
                    f"{entry['name']}: one or the other"
                )

    def test_the_files_themselves_are_not_in_git(self):
        ignored = (AZURE / ".local" / ".gitignore").read_text(encoding="utf-8")
        assert "blob/" in ignored and "later/" in ignored, (
            "the mirror holds somebody else's files, or a run's"
        )
        assert "settings.env" in (AZURE / ".gitignore").read_text(encoding="utf-8")


def _triggers():
    """Every function the host will try to index, with its decorators."""
    tree = ast.parse((MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                call = decorator if isinstance(decorator, ast.Call) else None
                attribute = getattr(call.func if call else decorator, "attr", "")
                if attribute in ("route", "queue_trigger", "blob_trigger", "timer_trigger"):
                    yield node, attribute, {kw.arg: kw for kw in (call.keywords if call else [])}


class TestTheAppHostsTheLibrarysApi:
    """Sixty routes the library already serves, rather than two written here."""

    def source(self):
        return (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")

    def test_it_is_an_asgi_app_around_create_app(self):
        source = self.source()
        assert "func.AsgiFunctionApp(" in source
        assert "from vectrixdb.api.server import create_app" in source

    def test_azure_is_not_the_door(self):
        """A function key is one shared secret with no scope and no expiry."""
        source = self.source()
        assert "http_auth_level=func.AuthLevel.ANONYMOUS" in source
        assert "auth_level=func.AuthLevel.FUNCTION" not in source, (
            "the library's key layer is the door"
        )

    def test_the_host_prefix_is_off_or_every_route_moves(self):
        host = json.loads((MAIN_FUNCTION_APP / "host.json").read_text(encoding="utf-8"))
        assert host["extensions"]["http"]["routePrefix"] == "", (
            "otherwise the library answers at /api/api/v1"
        )

    def test_what_it_installs_covers_the_api_it_serves(self):
        needs = (MAIN_FUNCTION_APP / "requirements.txt").read_text(encoding="utf-8")
        assert "api" in needs.split("vectrixdb-")[1].split("]")[0], (
            "FastAPI comes from the api extra"
        )
        assert "signin" in needs.split("vectrixdb-")[1].split("]")[0], (
            "who may retrieve needs somebody to judge"
        )

    def test_it_serves_the_index_the_worker_writes_to(self):
        """Without this the API opens an empty SQLite file beside the process."""
        source = (AZURE / "_common.py").read_text(encoding="utf-8")
        assert '"VECTRIXDB_STORAGE_BACKEND": "azure_search"' in source

    def test_it_is_given_somewhere_writable(self):
        """Everything but /tmp is read-only, and the default is a folder beside the process."""
        assert '"VECTRIXDB_PATH": "/tmp/' in (AZURE / "_common.py").read_text(encoding="utf-8")
        assert 'setdefault("VECTRIXDB_PATH"' in (MAIN_FUNCTION_APP / "function_app.py").read_text(
            encoding="utf-8"
        )

    def test_the_one_route_the_library_has_none_of(self):
        """It serves runs and golden files; nothing in it starts a run."""
        source = self.source()
        assert '"/api/v1/evaluations/run"' in source
        assert "finds_the_most" in source or "picks" in source

    def test_a_golden_file_is_held_to_the_schema_before_anything_is_queued(self):
        """A file written straight into the blob is checked too, not only one 08 uploaded."""
        source = self.source()
        route = (
            flat(source)
            .split("async def run_evaluation(")[1]
            .split('@api.get("/api/v1/evaluations/run/status"')[0]
        )
        assert (
            route.index("check = checked(where)")
            < route.index("if not check.ok:")
            < route.index("return refused(check)")
            < route.index("_queue().send_message(evaluation_message(")
        )
        helpers = source.split("    def checked(")[1].split("    def misconfigured(")[0]
        assert "check_golden(golden, fetcher=BlobFetcher(_blobs()))" in helpers, (
            "one check, for both runs"
        )
        assert (
            "Nothing was run." in helpers
            and '"problems": [str(p) for p in check.errors]' in helpers
        )
        assert "evaluate(" not in route and "collection(" not in route, (
            "the searching is the queue trigger's, not the request's"
        )


def _text_pdf(pages):
    """A PDF with these lines on its pages."""
    import io

    import pypdf
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = pypdf.PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for lines in pages:
        page = writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        stream.set_data(
            "\n".join(
                ["BT", "/F1 11 Tf", "15 TL", "54 720 Td"]
                + [f"({line}) Tj T*" for line in lines]
                + ["ET"]
            ).encode("latin-1")
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        page.replace_contents(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class TestIngestionDoesNotDependOnTheApi:
    """One function that will not load takes the whole app's indexing down.

    That is not a figure of speech: the deployment answered 404 on
    everything and ``/admin/functions`` was ``[]``, because one HTTP
    function's parameter was called ``request``. So the API is built behind
    a guard and the queue trigger never depends on it.
    """

    def test_a_broken_api_still_serves_something_and_still_ingests(self):
        source = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        assert "def _explaining(" in source
        built = source.split("def _api(")[1].split("\ndef ")[0]
        assert "except Exception" in built and "_explaining(" in built

    def test_the_thing_that_explains_is_a_whole_asgi_app(self):
        """A half one hangs the host: lifespan is a protocol, not an option."""
        source = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        explaining = source.split("def _explaining(")[1].split("\ndef ")[0]
        for needed in (
            "lifespan.startup.complete",
            "lifespan.shutdown.complete",
            "http.response.start",
            "http.response.body",
        ):
            assert needed in explaining, needed
        assert "503" in explaining

    def test_a_queue_function_calls_its_parameter_what_the_binding_says(self):
        for node, kind, keywords in _triggers():
            if kind == "queue_trigger":
                bound = ast.literal_eval(keywords["arg_name"].value)
                assert [a.arg for a in node.args.args] == [bound], (
                    f"{node.name} does not take {bound}"
                )

    def test_the_queue_trigger_is_the_only_function_this_file_declares(self):
        """Everything else is a route inside the ASGI app, which cannot fail to index."""
        assert [node.name for node, _, _ in _triggers()] == ["ingest_one"]


class _Evals:
    """The evals container: a blob client for each name, over what it holds."""

    def __init__(self):
        self.held = {}

    def get_blob_client(self, name):
        held = self.held

        class Blob:
            url = f"https://acct.blob.core.windows.net/evals/{name}"

            def exists(self):
                return name in held

            def download_blob(self):
                return type("Download", (), {"readall": lambda self: held[name]})()

            def upload_blob(self, data, overwrite=True):
                if name in held and not overwrite:
                    raise FileExistsError(name)
                held[name] = bytes(data)

            def delete_blob(self):
                del held[name]

        return Blob()


class TestStepFourTheGoldenDataset:
    """A hundred questions drafted in the app from the kept Markdown, off the request path, never over a file somebody checked."""

    def source(self):
        return (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")

    def step(self, name):
        whole = self.source()
        start = whole.index(f"def {name}(")
        return whole[start : whole.index("\ndef ", start + 1)]

    def files(self):
        sys.path.insert(0, str(MAIN_FUNCTION_APP))
        try:
            import golden_files

            return golden_files
        finally:
            sys.path.remove(str(MAIN_FUNCTION_APP))

    def test_it_comes_after_the_markdown_and_before_anything_is_cut(self):
        source = self.source()
        assert (
            source.index("# STEP THREE: MARKDOWN")
            < source.index("# STEP FOUR: GOLDEN DATA")
            < source.index("# STEP FIVE: CHUNKING COMPARED")
        )
        guide = " ".join(source.split('"""')[1].split())
        assert "STEP FOUR: GOLDEN DATA write_golden_dataset" in guide, (
            "the guide at the top names it"
        )

    def test_it_writes_from_the_kept_markdown_cut_its_own_way(self):
        """Nothing is cut yet, so the passages are the Markdown step three kept, cut a way no build of step five is."""
        from vectrixdb.evaluation import TECHNIQUES, chunking_plan

        passages = self.step("_passages")
        assert 'written_to(_blobs(), name)["keep_source"]' in passages and "**PASSAGES" in passages
        assert '.get("user_metadata")' in passages, (
            "each document with the metadata it came with, which the policy reads"
        )
        tree = ast.parse(self.source())
        cut = next(
            n
            for n in tree.body
            if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "PASSAGES"
        )
        assert ast.literal_eval(cut.value) == {
            "chunk": "markdown",
            "chunk_size": 3000,
            "overlap": 0,
        }
        compared = {
            (b["chunk"], b["size"])
            for b in chunking_plan(list(TECHNIQUES), context=True, late=True)
        }
        assert ("markdown", 3000) not in compared, (
            "so the questions favour none of the cuts step five compares"
        )
        job = self.step("write_golden_dataset")
        assert (
            job.index("finally:")
            < job.index("db.close()")
            < job.index('shutil.rmtree(_scratch("_golden", "passages")')
        )
        assert job.index("if not handles:") < job.index("write_golden("), (
            "nothing kept is said, not tried five times"
        )

    def test_a_hundred_questions_from_short_to_long(self):
        from vectrixdb._eval_writer import MIX

        tree = ast.parse(self.source())
        body = next(
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "golden_questions"
        ).body
        plan = ast.literal_eval(next(n for n in body if isinstance(n, ast.Return)).value)
        mix = plan["mix"]
        assert plan["n"] == 100 and sum(mix.values()) == 100 and set(mix) == set(MIX)
        assert mix["search"] + mix["fact"] == 50, "half short"
        assert mix["long"] == 15 and mix["two_page"] == 15 and plan["evolve"] == 1

    def test_the_model_is_the_azure_openai_deployment_the_settings_name(self):
        from vectrixdb.evaluation import ChatWriter

        assert "return ChatWriter.from_environment()" in self.step("golden_writer")
        writer = ChatWriter.from_environment(
            {
                "AZURE_OPENAI_WRITER_DEPLOYMENT": "gpt-5.4-mini",
                "AZURE_OPENAI_ENDPOINT": "https://vx.openai.azure.com/",
                "AZURE_OPENAI_KEY": "the-key",
                "AZURE_OPENAI_API_VERSION": "2025-01-01-preview",
            }
        )
        assert (
            writer.url
            == "https://vx.openai.azure.com/openai/deployments/gpt-5.4-mini/chat/completions?api-version=2025-01-01-preview"
        )
        assert writer.key_header == "api-key" and "the-key" not in repr(writer)

    def test_a_settings_file_with_an_old_name_says_what_to_rename(self, folder, capsys):
        with (folder.SETTINGS).open("a", encoding="utf-8") as settings_file:
            settings_file.write(
                "VX_WRITER_DEPLOYMENT=gpt-5.4-mini\nVX_EMBED_DEPLOYMENT=text-embedding-3-small\n"
            )
        with pytest.raises(SystemExit):
            folder.settings()
        said = capsys.readouterr().err
        assert (
            "VX_WRITER_DEPLOYMENT  is now  AZURE_OPENAI_WRITER_DEPLOYMENT" in said
            and "VX_EMBED_DEPLOYMENT  is now  AZURE_OPENAI_EMBED_DEPLOYMENT" in said
        )

    def test_the_names_are_the_ones_the_apps_use(self, folder):
        config = folder.settings()
        assert (
            config["AZURE_OPENAI_WRITER_DEPLOYMENT"],
            config["AZURE_OPENAI_EMBED_DEPLOYMENT"],
        ) == ("gpt-5.4-mini", "text-embedding-3-small")
        assert "INGEST_CLIENT" not in config and "VX_POLICIED" not in config, (
            "nothing is policied or scoped by a setting"
        )
        example = (AZURE / "settings.example.env").read_text(encoding="utf-8")
        for old in folder.RENAMED:
            assert old not in example, old
        for kept in (
            "VX_EXTRACT_PREFIX",
            "VX_EXTRACT_GATEWAY_PATHS",
            "VX_EXTRACT_URL_HOSTS",
            "VX_EXTRACT_PDF",
            "VX_COLLECTIONS",
        ):
            assert f"{kept}=" in example, kept

    def test_a_second_vector_unless_settings_say_no(self, folder):
        kept = {
            "openai_endpoint": "https://vx.openai.azure.com/",
            "blob_account": "https://vx.blob.core.windows.net",
        }
        config = folder.settings()
        assert config["VX_SECOND_VECTOR"] == "yes", "yes when settings.env does not say"
        assert (
            folder.env_of(config, kept, {})["AZURE_OPENAI_EMBED_DEPLOYMENT"]
            == "text-embedding-3-small"
        )
        alone = folder.env_of({**config, "VX_SECOND_VECTOR": "no"}, kept, {})
        assert "AZURE_OPENAI_EMBED_DEPLOYMENT" not in alone, (
            "step seven then embeds with the built-in model alone"
        )
        assert alone["AZURE_OPENAI_ENDPOINT"] == kept["openai_endpoint"], (
            "the resource stays, for step four's writer"
        )

    def test_03_deploys_the_chat_model_and_the_embedding_model_only_for_a_second_vector(
        self, folder
    ):
        three = load("03_create_ai_services.py")
        config = folder.settings()
        assert [d[0] for d in three.deployments(config)] == [
            "gpt-5.4-mini",
            "text-embedding-3-small",
        ]
        assert [d[0] for d in three.deployments({**config, "VX_SECOND_VECTOR": "no"})] == [
            "gpt-5.4-mini"
        ], "the golden questions still need it"
        assert "for deployment, model, what, setting, capacity in deployments(config):" in (
            AZURE / "03_create_ai_services.py"
        ).read_text(encoding="utf-8")

    def test_detailed_pictures_use_the_same_chat_model_unless_another_is_named(self, folder):
        three = load("03_create_ai_services.py")
        detailed = {**folder.settings(), "VX_DETAILED_PICTURES": "yes"}
        (chat, _embed) = three.deployments(detailed)
        assert chat[0] == "gpt-5.4-mini" and chat[2].endswith(
            "describes every picture in detail"
        ), "one deployment, both jobs"
        named = three.deployments({**detailed, "AZURE_OPENAI_VISION_DEPLOYMENT": "gpt-4o"})
        assert [d[0] for d in named] == ["gpt-5.4-mini", "gpt-4o", "text-embedding-3-small"]

    def test_the_extraction_app_is_given_the_chat_model_for_pictures_only_when_asked(self, folder):
        from vectrixdb.extract.describers import describer_from_environment

        script = load(EXTRACT_SCRIPT)
        kept = {
            "vision_endpoint": "https://v.cognitiveservices.azure.com",
            "openai_endpoint": "https://vx.openai.azure.com/",
        }
        keys = {"AZURE_VISION_KEY": "v", "AZURE_OPENAI_KEY": "o"}
        plain = script.extraction_env(folder.settings(), kept, {"AZURE_VISION_KEY": "v"}, "the-key")
        assert (
            "AZURE_OPENAI_VISION_DEPLOYMENT" not in plain and "AZURE_OPENAI_ENDPOINT" not in plain
        )
        detailed = script.extraction_env(
            {**folder.settings(), "VX_DETAILED_PICTURES": "yes"}, kept, keys, "the-key"
        )
        assert (
            detailed["AZURE_OPENAI_VISION_DEPLOYMENT"] == "gpt-5.4-mini"
            and detailed["AZURE_OPENAI_ENDPOINT"] == kept["openai_endpoint"]
        )
        assert describer_from_environment(detailed).labels == [
            "gpt-5.4-mini",
            "azure-image-analysis",
        ], "the chat model first, Vision behind it"

    def test_the_settings_reach_the_app(self, folder):
        config = {
            **folder.settings(),
            "AZURE_OPENAI_WRITER_DEPLOYMENT": "gpt-5.4-mini",
            "AZURE_OPENAI_API_VERSION": "2025-01-01-preview",
        }
        wired = folder.env_of(
            config,
            {
                "openai_endpoint": "https://vx.openai.azure.com/",
                "blob_account": "https://vx.blob.core.windows.net",
            },
            {},
        )
        assert (wired["AZURE_OPENAI_WRITER_DEPLOYMENT"], wired["AZURE_OPENAI_API_VERSION"]) == (
            "gpt-5.4-mini",
            "2025-01-01-preview",
        )
        bare = folder.env_of(
            folder.settings(), {"openai_endpoint": "https://vx.openai.azure.com/"}, {}
        )
        assert (
            bare["AZURE_OPENAI_WRITER_DEPLOYMENT"] == "gpt-5.4-mini"
            and "AZURE_OPENAI_API_VERSION" not in bare
        )

    def test_the_queue_trigger_hands_a_request_for_it_to_step_four(self):
        trigger = self.source().split("def ingest_one(")[1]
        assert (
            trigger.index("golden_request(body)")
            < trigger.index("write_golden_dataset(asked)")
            < trigger.index("handle_events(message)")
        )

    def test_the_request_is_queued_and_never_written_in_the_request(self):
        """Some six hundred calls to the model, and Azure ends a request at 230 seconds."""
        source = flat(self.source())
        route = source[
            source.index('@api.post("/api/v1/golden/write"') : source.index(
                '@api.get("/api/v1/golden/write"'
            )
        ]
        assert "_queue().send_message(golden_message(wanted))" in route and "202)" in route
        assert "write_golden(" not in route and "write_golden_dataset(" not in route
        for refused in (
            "if golden_writer() is None:",
            "if folder.exists(GOLDEN):",
            "if folder.busy():",
        ):
            assert route.index(refused) < route.index("_queue().send_message("), refused

    def test_it_never_writes_over_a_golden_file(self):
        job = self.step("write_golden_dataset")
        assert (
            job.index("if folder.exists(GOLDEN):")
            < job.index("writer = golden_writer()")
            < job.index("write_golden(")
        )
        assert "folder.write(GOLDEN, made.read(), overwrite=False)" in job

    def test_the_answers_are_kept_as_it_goes_and_a_refusal_is_not_tried_again(self):
        job = self.step("write_golden_dataset")
        assert "folder.fetch_answers(answers)" in job and "cache=answers" in job
        assert "if made % 10 == 0 or made == wanted:" in job
        refused = job.split("except WriterUnavailable as exc:")[1].split("except Exception")[0]
        assert "return" in refused and "raise" not in refused, (
            "five more tries would be refused five more times"
        )
        other = job.split("except Exception as exc:")[1].split(
            "folder.keep_answers(answers)\n    with open"
        )[0]
        assert "raise" in other, "anything else goes round again, from the answers kept"

    def test_a_collection_is_read_with_its_record_and_as_nobody_in_particular(self):
        passages = self.step("_passages")
        assert "collection_store=_collection_store()" in passages, (
            "the throwaway copy reads the record, as the real one does"
        )
        assert "policy_for(" not in passages and "as_principal(" not in passages, (
            "no rule is built here and no job pretends to be somebody"
        )
        assert "_passages(named())" in self.step("write_golden_dataset")

    def test_a_request_is_told_from_a_blob_event(self):
        files = self.files()
        import base64

        asked = files.golden_message(100)
        assert files.golden_request(asked) == {"n": 100}
        assert files.golden_request(asked.encode()) == {"n": 100}
        assert files.golden_request(base64.b64encode(asked.encode()).decode()) == {"n": 100}, (
            "however the host hands it over"
        )
        assert files.golden_request('{"vectrixdb": "golden"}') == {}, "no count: the step's own"
        assert files.golden_request(files.golden_message(files.MOST + 1)) == {}
        event = json.dumps(
            {
                "eventType": "Microsoft.Storage.BlobCreated",
                "subject": "/blobServices/default/containers/ingestion/blobs/raw/a.pdf",
            }
        )
        assert files.golden_request(event) is None and files.golden_request("not json") is None

    def test_the_queue_is_beside_the_blobs(self):
        assert (
            self.files().queue_account("https://vx.blob.core.windows.net")
            == "https://vx.queue.core.windows.net"
        )

    def test_the_folder_its_files_and_its_status(self, tmp_path):
        files = self.files()
        evals = _Evals()
        service = type(
            "Service",
            (),
            {"get_container_client": lambda self, name: evals if name == "evals" else None},
        )()
        folder = files.GoldenFolder.at("https://vx.blob.core.windows.net/evals", service)
        assert (
            not folder.exists(files.GOLDEN)
            and folder.examples() == []
            and folder.read_status() is None
        )
        evals.held[files.EXAMPLES] = b"How much did the bank earn?\n\n  Why were losses higher?  \n"
        assert folder.examples() == ["How much did the bank earn?", "Why were losses higher?"]
        local = tmp_path / "answers.jsonl"
        local.write_text("from an earlier run\n", encoding="utf-8")
        folder.fetch_answers(str(local))
        assert not local.exists(), (
            "the container has none, so an old copy on the instance does not answer"
        )
        local.write_text('{"key": "k", "answer": "a"}\n', encoding="utf-8")
        folder.keep_answers(str(local))
        folder.fetch_answers(str(tmp_path / "again.jsonl"))
        assert (tmp_path / "again.jsonl").read_text(
            encoding="utf-8"
        ) == '{"key": "k", "answer": "a"}\n'
        said = folder.status("writing", written=10, wanted=100)
        assert folder.read_status() == said and said["state"] == "writing"
        assert folder.busy(), "one at a time"
        evals.held[files.STATUS] = json.dumps(
            {"state": "writing", "at": "2020-01-01T00:00:00+00:00"}
        ).encode()
        assert not folder.busy(), "a run that died long ago does not block the next"
        folder.status("done", written=100, wanted=100)
        assert not folder.busy()

    def test_the_scratch_copy_has_the_folder_made_for_it(self):
        step = self.step("write_golden_dataset")
        assert step.index("os.makedirs(os.path.dirname(drafts), exist_ok=True)") < step.index(
            "folder.fetch_answers(answers)"
        )


class TestStepNineTheRetrievalCompared:
    """Each collection asked its own golden questions every way, off the request path, three named, and one followed by step ten."""

    def source(self):
        return (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")

    def step(self, name):
        whole = self.source()
        start = whole.index(f"def {name}(")
        return whole[start : whole.index("\ndef ", start + 1)]

    def files(self):
        sys.path.insert(0, str(MAIN_FUNCTION_APP))
        try:
            import golden_files

            return golden_files
        finally:
            sys.path.remove(str(MAIN_FUNCTION_APP))

    def test_it_comes_after_indexing_and_before_retrieval(self):
        source = self.source()
        assert (
            source.index("# STEP EIGHT: INDEXING")
            < source.index("# STEP NINE: RETRIEVAL COMPARED")
            < source.index("# STEP TEN: RETRIEVAL")
            < source.index("# THE API")
        )
        guide = " ".join(source.split('"""')[1].split())
        assert "STEP NINE: RETRIEVAL COMPARED evaluate_collections" in guide, (
            "the guide at the top names it"
        )

    def test_step_ten_follows_a_run_over_checked_questions_unless_a_way_is_pinned(self):
        job = self.step("evaluate_collections")
        assert (
            '"step_ten": _pick_a_way(folder, name, report, questions=len(own), drafts=gold.drafts)'
            in flat(job)
        )
        way = self.step("_pick_a_way")
        assert (
            way.index("if setting not in (AUTO, *ROLES):")
            < way.index("if drafts:")
            < way.index("folder.write_picks(")
        )
        assert "with_retrieval_picks(folder.read_picks()," in way, (
            "read again just before the write, so a cut step five picked meanwhile is kept"
        )

    def test_the_three_are_named_by_the_librarys_rules(self):
        import inspect

        from vectrixdb.evaluation import evaluate

        tree = ast.parse(self.source())
        body = next(
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "picking_rules"
        ).body
        rules = ast.literal_eval(next(n for n in body if isinstance(n, ast.Return)).value)
        defaults = {
            k: v.default for k, v in inspect.signature(evaluate).parameters.items() if k in rules
        }
        assert rules == {"balance": 4, "time_points": 8} == defaults
        assert "**picking_rules()" in self.step("evaluate_collections")

    def test_each_collection_is_asked_its_own_questions(self):
        job = self.step("evaluate_collections")
        assert (
            job.index("missing_documents([target], gold.labelled)")
            < job.index("own = [q for q in gold.labelled if str(q.id) not in elsewhere]")
            < job.index("evaluate(")
        )
        assert "Golden(questions=own," in flat(job), (
            "the run is asked the collection's own questions"
        )
        assert '"left_out": len(elsewhere)' in job, "and how many were left out is said"

    def test_a_run_a_collection_lands_where_the_evaluate_page_reads(self):
        job = self.step("evaluate_collections")
        assert 'save_to=os.environ.get("VECTRIXDB_EVALUATIONS") or None' in job
        assert "for name in names:" in job and job.index("for name in names:") < job.index(
            "evaluate("
        )

    def test_a_collection_is_run_as_nobody_in_particular(self):
        job = self.step("evaluate_collections")
        assert "as_principal(" not in job and "INGEST_CLIENT" not in job

    def test_a_bad_file_or_a_failure_is_said_and_not_tried_again(self):
        job = self.step("evaluate_collections")
        assert (
            job.index("if not check.ok:") < job.index('"refused"') < job.index("for name in names:")
        )
        failed = job.split("except Exception as exc:")[1]
        assert (
            '"failed"' in failed
            and "return" in failed
            and not re.search(r"^\s*raise\b", failed, re.MULTILINE)
        ), "a retry would run every collection again"
        assert 'folder.status("done", into=EVALUATION_STATUS, golden=where, runs=runs)' in job

    def test_the_route_queues_and_answers_at_once(self):
        source = self.source()
        route = (
            flat(source)
            .split("async def run_evaluation(")[1]
            .split('@api.get("/api/v1/evaluations/run/status"')[0]
        )
        assert (
            "_queue().send_message(evaluation_message(where, names))" in route and "202)" in route
        )
        assert route.index("if folder.busy(into=EVALUATION_STATUS):") < route.index(
            "_queue().send_message("
        )

    def test_its_status_is_not_at_a_path_the_library_answers(self):
        """The library's /api/v1/evaluations/{run} would take GET /api/v1/evaluations/run, as a run called run."""
        source = self.source()
        assert '@api.get("/api/v1/evaluations/run/status"' in flat(source)
        assert '@api.get("/api/v1/evaluations/run"' not in flat(source)
        pytest.importorskip("fastapi")
        from vectrixdb.api.evaluations import router

        paths = [route.path for route in router.routes]
        assert "/api/v1/evaluations/{run}" in paths and not any(
            p.count("/") == 5 and p.endswith("/status") for p in paths
        )

    def test_the_queue_trigger_hands_each_request_to_its_step(self):
        trigger = self.source().split("def ingest_one(")[1]
        order = [
            "golden_request(body)",
            "write_golden_dataset(asked)",
            "evaluation_request(body)",
            "evaluate_collections(run)",
            "handle_events(message)",
        ]
        assert [trigger.index(said) for said in order] == sorted(
            trigger.index(said) for said in order
        )

    def test_an_evaluation_is_told_from_the_other_messages(self):
        files = self.files()
        import base64

        asked = files.evaluation_message(
            "https://vx.blob.core.windows.net/evals/golden_dataset/golden.jsonl",
            ["financial", "media"],
        )
        wanted = {
            "golden": "https://vx.blob.core.windows.net/evals/golden_dataset/golden.jsonl",
            "collections": ["financial", "media"],
        }
        assert files.evaluation_request(asked) == wanted
        assert files.evaluation_request(base64.b64encode(asked.encode()).decode()) == wanted
        assert files.evaluation_request('{"vectrixdb": "evaluate"}') == {
            "golden": "",
            "collections": [],
        }, "every collection, the step's own file"
        assert (
            files.golden_request(asked) is None
            and files.evaluation_request(files.golden_message(100)) is None
        )
        event = json.dumps(
            {
                "eventType": "Microsoft.Storage.BlobCreated",
                "subject": "/blobServices/default/containers/ingestion/blobs/raw/a.pdf",
            }
        )
        assert files.evaluation_request(event) is None

    def test_its_status_is_a_file_of_its_own(self):
        files = self.files()
        evals = _Evals()
        folder = files.GoldenFolder(evals)
        folder.status(
            "running", into=files.EVALUATION_STATUS, collection="financial", setup=2, of=6
        )
        assert folder.read_status(files.EVALUATION_STATUS)["collection"] == "financial"
        assert folder.read_status() is None, "the golden dataset's own status is not touched"
        assert folder.busy(into=files.EVALUATION_STATUS) and not folder.busy()


class TestStepFiveTheChunkingCompared:
    """The kept Markdown cut every way, a build a queue message, the run saved where the Chunking tab reads it, and the cut moved past luck."""

    def source(self):
        return (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")

    def step(self, name):
        whole = self.source()
        start = whole.index(f"def {name}(")
        return whole[start : whole.index("\ndef ", start + 1)]

    def files(self):
        sys.path.insert(0, str(MAIN_FUNCTION_APP))
        try:
            import golden_files

            return golden_files
        finally:
            sys.path.remove(str(MAIN_FUNCTION_APP))

    def test_it_is_step_five_before_anything_is_cut(self):
        source = self.source()
        assert (
            source.index("# STEP FOUR: GOLDEN DATA")
            < source.index("# STEP FIVE: CHUNKING COMPARED")
            < source.index("def compare_techniques(")
            < source.index("# STEP SIX: CHUNKS")
        )
        guide = " ".join(source.split('"""')[1].split())
        assert "STEP FIVE: CHUNKING COMPARED compare_techniques" in guide, (
            "the guide at the top names it"
        )
        assert "# STEP EIGHT, THE CHUNKING" not in source, (
            "a step of its own, not the searching's other half"
        )

    def test_a_run_moves_the_cut_only_past_luck_over_checked_questions_of_every_collection(self):
        pick = self.step("_pick_a_cut")
        order = [
            "if setting != AUTO:",
            "if drafts:",
            "if missing:",
            "chunking_choice(results, current)",
            "folder.write_picks(",
            'apply_message(choice["pick"], named())',
        ]
        assert [pick.index(said) for said in order] == sorted(pick.index(said) for said in order)
        assert '"picked": _pick_a_cut(' in self.step("compare_techniques"), (
            "the status says what the run did to the cut"
        )

    def test_the_budget_is_the_librarys(self):
        import inspect

        from vectrixdb._eval_chunking import BUDGET
        from vectrixdb.evaluation import compare_chunking

        body = next(
            n
            for n in ast.parse(self.source()).body
            if isinstance(n, ast.FunctionDef) and n.name == "chunking_rules"
        ).body
        rules = ast.literal_eval(next(n for n in body if isinstance(n, ast.Return)).value)
        assert (
            rules
            == {"budget": BUDGET}
            == {"budget": inspect.signature(compare_chunking).parameters["budget"].default}
        )
        job = self.step("compare_techniques")
        assert job.count('budget=rules["budget"]') == 2, (
            "the builds and the report say the same budget"
        )

    def test_the_documents_are_the_markdown_step_three_kept(self):
        kept = self.step("_kept_markdown")
        assert (
            'written_to(_blobs(), name)["keep_source"]' in kept
            and "store.get(doc_id) for doc_id in store.ids()" in kept
        )
        assert "_kept_markdown(names)" in self.step("compare_techniques")

    def test_the_run_lands_where_the_chunking_tab_reads(self):
        job = self.step("compare_techniques")
        assert (
            'chunking_store(os.environ["VECTRIXDB_EVALUATIONS"]).save(report, golden=gold.raw or None)'
            in flat(job)
        )
        assert "chunking_report(results, gold.describe()" in flat(job)

    def test_a_bad_file_or_a_failure_is_said_and_not_tried_again(self):
        job = self.step("compare_techniques")
        assert job.index("if not check.ok:") < job.index('"refused"') < job.index("chunking_turn(")
        assert "except WriterUnavailable as exc:" in job.split("chunking_turn(")[1]
        failed = job.split("except Exception as exc:\n")[1]
        assert (
            '"failed"' in failed
            and "return" in failed
            and not re.search(r"^\s*raise\b", failed, re.MULTILINE)
        )

    def test_one_build_that_cannot_run_leaves_the_others(self):
        build = self.step("compare_techniques").split("def build(")[1].split("def finish(")[0]
        assert "except WriterUnavailable:\n                raise" in build, (
            "a model that refuses stops the comparison"
        )
        assert '"error": f"{type(exc).__name__}: {exc}"' in build

    def test_the_route_checks_the_file_and_queues_the_first_build(self):
        route = (
            self.source()
            .split("async def run_chunking(")[1]
            .split('@api.get("/api/v1/chunking/run/status"')[0]
        )
        assert (
            route.index("check = checked(where)")
            < route.index("if not check.ok:")
            < route.index("if folder.busy(into=CHUNKING_STATUS):")
            < route.index("_queue().send_message(chunking_message(where, names, job))")
        )
        assert "202," in route

    def test_its_status_is_not_at_a_path_the_library_answers(self):
        source = flat(self.source())
        assert (
            '@api.get("/api/v1/chunking/run/status"' in source
            and '@api.get("/api/v1/chunking/run"' not in source
        )
        assert (
            '@api.get("/api/v1/chunking/apply/status"' in source
            and '@api.get("/api/v1/chunking/apply"' not in source
        ), "nor applying the cut's"
        pytest.importorskip("fastapi")
        from vectrixdb.api.evaluations import router

        paths = [route.path for route in router.routes]
        assert "/api/v1/chunking/{run}" in paths and "/api/v1/chunking/{run}/golden" in paths
        assert not any(p.startswith("/api/v1/chunking/") and p.endswith("/status") for p in paths)

    def test_the_queue_trigger_hands_it_the_chunking_messages(self):
        trigger = self.source().split("def ingest_one(")[1]
        order = [
            "golden_request(body)",
            "chunking_request(body)",
            "compare_techniques(cutting)",
            "apply_request(body)",
            "apply_the_cut(applying)",
            "evaluation_request(body)",
            "evaluate_collections(run)",
            "handle_events(message)",
        ]
        assert [trigger.index(said) for said in order] == sorted(
            trigger.index(said) for said in order
        ), "the jobs in the order of their steps, then the files"

    def test_a_chunking_message_is_told_from_the_others(self):
        import base64

        files = self.files()
        asked = files.chunking_message(
            "https://vx.blob.core.windows.net/evals/golden_dataset/golden.jsonl",
            ["financial"],
            "20260922-101500",
            3,
        )
        wanted = {
            "golden": "https://vx.blob.core.windows.net/evals/golden_dataset/golden.jsonl",
            "collections": ["financial"],
            "job": "20260922-101500",
            "at": 3,
        }
        assert files.chunking_request(asked) == wanted
        assert files.chunking_request(base64.b64encode(asked.encode()).decode()) == wanted
        assert files.chunking_request('{"vectrixdb": "chunking"}') == {
            "golden": "",
            "collections": [],
            "job": "",
            "at": 0,
        }
        assert (
            files.chunking_request('{"vectrixdb": "chunking", "job": "../../x", "at": "later"}')[
                "job"
            ]
            == "x"
        ), "a job names a folder, nothing above it"
        assert files.evaluation_request(asked) is None and files.golden_request(asked) is None
        assert files.chunking_request(files.evaluation_message("g", [])) is None

    def plan(self):
        return [
            {
                "key": k,
                "technique": "markdown",
                "chunk": "markdown",
                "size": s,
                "overlap": s // 5,
                "headings": True,
                "parent_size": None,
            }
            for k, s in (
                ("markdown-600-h", 600),
                ("markdown-900-h", 900),
                ("markdown-1500-h", 1500),
            )
        ]

    def chain(self, files, folder, plan, build, finish, first):
        """Every message the job sends, handed back to it the way the queue would, until it is done."""
        sent, said = [first], None
        while sent:
            said = files.chunking_turn(
                folder,
                files.chunking_request(sent.pop(0)),
                plan,
                build=build,
                finish=finish,
                send=sent.append,
            )
        return said

    def test_a_build_a_message_then_the_run(self):
        files = self.files()
        folder = files.GoldenFolder(_Evals())
        made, finished = [], []

        def build(one):
            made.append(one["key"])
            return {**one, "questions": 2, "found": 2}

        def finish(results):
            finished.append([r["key"] for r in results])
            return {
                "id": "20260922-101500-abcdef",
                "scored": "answered",
                "techniques": [{"rank": 1, "name": "Structure-aware", "right": 2, "questions": 2}],
                "builds": [],
            }

        said = self.chain(
            files,
            folder,
            self.plan(),
            build,
            finish,
            files.chunking_message("g", ["financial"], "job1"),
        )
        assert made == ["markdown-600-h", "markdown-900-h", "markdown-1500-h"] and finished == [
            made
        ]
        assert (
            said["state"] == "done"
            and said["run"] == "20260922-101500-abcdef"
            and said["best"] == "Structure-aware"
        )
        assert folder.read_status(files.CHUNKING_STATUS)["techniques"] == [
            "1. Structure-aware, 2 of 2"
        ]
        assert not [n for n in folder.container.held if n.startswith(files.CHUNKING_WORK)], (
            "the work folder is cleared"
        )

    def test_a_message_handed_over_twice_does_not_build_again(self):
        files = self.files()
        folder = files.GoldenFolder(_Evals())
        made, sent = [], []

        def build(one):
            made.append(one["key"])
            return {**one, "questions": 1}

        first = files.chunking_request(files.chunking_message("g", [], "job2", 0))
        files.chunking_turn(
            folder, first, self.plan(), build=build, finish=lambda r: {}, send=sent.append
        )
        files.chunking_turn(
            folder, first, self.plan(), build=build, finish=lambda r: {}, send=sent.append
        )
        assert made == ["markdown-600-h"], "kept, so the second goes straight on"
        assert [files.chunking_request(m)["at"] for m in sent] == [1, 1]
        running = folder.read_status(files.CHUNKING_STATUS)
        assert running["state"] == "running" and running["build"] == 1 and running["of"] == 3
        assert folder.busy(into=files.CHUNKING_STATUS) and not folder.busy(
            into=files.EVALUATION_STATUS
        )

    def test_the_whole_step_on_fakes_is_what_the_chunking_tab_reads(self, tmp_path, monkeypatch):
        """Real builds, embedded with a fake, over two pages; the run is then served by the library's own route."""
        import numpy as np

        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from vectrixdb.api.server import create_app
        from vectrixdb.evaluation import (
            Question,
            chunking_report,
            chunking_store,
            run_chunking_build,
        )
        from vectrixdb.ingest import LoadedDocument

        def embed(texts):
            out = np.full((len(texts), 2), 0.1, dtype=np.float32)
            for i, text in enumerate(texts):
                out[i, 0] += text.lower().count("fee")
                out[i, 1] += text.lower().count("loan")
            return out

        filler = "Nothing here answers anything. " * 20
        text = f"# Fees\n\nEvery fee is waived while cash is needed. {filler}\n\n# Loans\n\nA late loan payment has no charge. {filler}\n\n"
        doc = LoadedDocument(
            text=text,
            pages=[(0, 1), (text.index("# Loans"), 2)],
            headings=[(0, "Fees", 1), (text.index("# Loans"), "Loans", 1)],
        )
        questions = [
            Question("Which fee is waived?", expected=["guide.pdf#page=1"], id="q1"),
            Question("Is a late loan payment charged?", expected=["guide.pdf#page=2"], id="q2"),
        ]
        where = tmp_path / "evals"
        files = self.files()
        folder = files.GoldenFolder(_Evals())
        golden = {
            "source": "golden.jsonl",
            "sha256": "ef" * 32,
            "questions": 2,
            "labelled": 2,
            "unfilled": 0,
            "drafts": 0,
        }

        def build(one):
            return run_chunking_build(
                {"financial": {"guide.pdf": doc}},
                questions,
                one,
                budget=800,
                open_with={"embed_fn": embed, "dimension": 2},
                workdir=tmp_path / "scratch",
            )

        def finish(results):
            report = chunking_report(results, golden, budget=800)
            chunking_store(str(where)).save(report)
            return report

        said = self.chain(
            files,
            folder,
            self.plan(),
            build,
            finish,
            files.chunking_message("g", ["financial"], "job3"),
        )
        assert (
            said["state"] == "done"
            and said["scored"] == "found"
            and said["best"] == "Structure-aware"
        )
        monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
        monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
        monkeypatch.setenv("VECTRIXDB_EVALUATIONS", str(where))
        with TestClient(create_app(db_path=str(tmp_path / "db"), enable_dashboard=False)) as client:
            report = client.get("/api/v1/chunking/latest").json()["data"]
            assert report["id"] == said["run"] and report["kind"] == "chunking"
            assert [b["key"] for b in report["builds"]] == [
                one["key"] for one in self.plan()
            ] and report["questions"] == 2
            assert report["techniques"][0]["found"] == 2, (
                "each question's page was in what was handed over"
            )
            assert client.get("/api/v1/chunking").json()["data"]["runs"][0]["id"] == said["run"]

    def test_an_old_wheel_is_refused_before_it_is_published(self):
        needs = load("06_create_main_function_app.py").NEEDS
        assert needs["vectrixdb/_eval_chunking.py"] == "def handed_over", (
            "newer than run_chunking_build, in the same file"
        )
        assert (needs["vectrixdb/_setups.py"], needs["vectrixdb/easy.py"]) == (
            "def search_of",
            "index: bool = True",
        )
        library = Path(vectrixdb.__file__).parent.parent
        for path, line in needs.items():
            assert line in (library / path).read_text(encoding="utf-8"), f"{path} has no {line}"


class TestTheWiringRoute:
    """Ours, and deliberately not called /health, which the library already serves."""

    def source(self):
        whole = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        start = whole.index("    async def wiring(")
        return whole[start : whole.index("    @api.post(", start)]

    def test_it_does_not_take_a_path_the_library_already_has(self):
        """A route registered second does not win, and nothing warns you."""
        whole = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        assert '@api.get("/health/wiring"' in whole
        # The routes added to the library's API, from _ours on: /health is the library's there. The ingest
        # app's own /health sits in _lean, where no library API is mounted to lose against.
        ours = whole[whole.index("def _ours(") :]
        assert '@api.get("/health"' not in ours
        lean = whole[whole.index("def _lean(") : whole.index("def _ours(")]
        assert '@api.get("/health"' in lean and "create_app(" not in lean

        import re

        import vectrixdb

        library = (Path(vectrixdb.__file__).parent / "api" / "server.py").read_text(
            encoding="utf-8"
        )
        theirs = set(re.findall(r'@(?:app|router)\.(?:get|post|put|delete)\(\s*"([^"]+)"', library))
        ours = set(
            re.findall(
                r'@api\.(?:get|post|put|delete)\(\s*"([^"]+)"', whole[whole.index("def _ours(") :]
            )
        )
        assert not (ours & theirs), (
            f"these paths are already the library's: {sorted(ours & theirs)}"
        )

    def test_it_opens_nothing_and_calls_nothing(self):
        """Cheap, so polling it costs nothing and an outage elsewhere is not reported as one here."""
        body = self.source()
        for expensive in (
            "collection(",
            "worker(",
            "BlobServiceClient",
            "evaluate(",
            "search_index(",
            "DefaultAzureCredential",
        ):
            assert expensive not in body, (
                f"health calls {expensive}, so it is not a cheap check any more"
            )

    def test_it_says_whether_a_setting_is_there_and_never_what_it_is(self):
        body = self.source()
        assert "os.environ.get(name" in body, "the only read is the one inside given()"
        assert body.count("given(") >= 7, "every wired line goes through given()"

    def test_warm_is_the_one_live_fact_in_it(self):
        assert "sorted(_open)" in self.source(), (
            "which collections this instance has opened is how you see a cold start"
        )

    def test_the_settings_it_reports_on_are_ones_the_app_reads(self):
        """A health check that watches a setting nothing uses is worse than none."""
        import re

        read = set()
        for name in ("function_app.py", "readers.py", "collections_of.py"):
            read |= set(
                re.findall(
                    r'["\']([A-Z][A-Z_0-9]{3,})["\']',
                    (MAIN_FUNCTION_APP / name).read_text(encoding="utf-8"),
                )
            )
        for watched in re.findall(r"given\(([^)]*)\)", self.source()):
            for setting in re.findall(r'"([A-Z_0-9]+)"', watched):
                assert setting in read, f"health watches {setting}, which nothing in the app reads"


class TestWhatTheLiveAppReported:
    """Claims in the docstring that the running deployment contradicted."""

    def test_flex_does_not_report_a_content_editing_state(self):
        """``az functionapp show`` on the Flex app has no such property at all."""
        whole = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        assert "A Flex app does not report it at all" in whole
        assert "the app reports ``functionAppContentEditingState: NotAllowed``" not in whole

    def test_owner_is_not_enough_to_read_the_package(self):
        # Flowed, because a docstring wraps and the phrase spans two lines.
        whole = " ".join(
            (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8").split()
        )
        assert "Storage Blob Data Reader" in whole
        assert "being Owner of the subscription does not include the data plane" in whole

    def test_the_cold_start_is_named_so_nobody_reads_it_as_broken(self):
        whole = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        assert "always-ready" in whole and "eight or nine seconds" in whole

    def test_a_policied_collection_needs_somebody_and_the_docstring_says_so(self):
        whole = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        assert "needs sign-in" in whole and "403" in whole


class TestTheParentSectionsHaveSomewhereShared:
    """A file on one instance is invisible to the next, and is not kept."""

    def test_step_03_makes_the_account(self):
        source = (AZURE / "03_create_ai_services.py").read_text(encoding="utf-8")
        assert "cosmosdb" in source and "--partition-key-path" in source
        assert "/doc" in source, "partitioned by document, so forgetting one is one partition"

    def test_the_address_is_handed_to_the_app(self):
        assert "VECTRIXDB_PARENT_STORE" in (AZURE / "_common.py").read_text(encoding="utf-8")
        source = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        assert '"parent_store": os.environ.get("VECTRIXDB_PARENT_STORE")' in source

    def test_the_wiring_route_says_whether_it_has_one(self):
        assert "shared_parents" in (MAIN_FUNCTION_APP / "function_app.py").read_text(
            encoding="utf-8"
        )


class TestTheChunksThePagesCountHaveSomewhereShared:
    """The dashboard is not the instance that ingested, so what its collection pages count is a copy every instance writes."""

    COSMOS = "cosmos://vxtest1234-cosmos.documents.azure.com/data_db"

    def test_03_makes_a_chunks_container_partitioned_by_collection(self, folder, capsys):
        made = load("03_create_ai_services.py").cosmos(
            folder.Az(pretend=True), "vxtest1234-cosmos", "vxtest1234-rg", "canadacentral", True
        )
        assert made == {
            "parent_store": f"{self.COSMOS}/parent_sections",
            "chunk_store": f"{self.COSMOS}/chunk_records",
            "collection_store": f"{self.COSMOS}/collection_records",
            "signin_store": f"{self.COSMOS}/signin_records",
        }, "one database, data_db, holds every container"
        (chunks,) = [
            line for line in capsys.readouterr().out.splitlines() if "--name chunk_records" in line
        ]
        assert (
            "--partition-key-path /collection" in chunks and "--idx" in chunks and ".json" in chunks
        )

    def test_the_names_are_what_they_hold_and_none_is_the_librarys(self, folder):
        assert (folder.INGESTION, folder.PARENT_SECTIONS, folder.CHUNK_RECORDS) == (
            "ingestion",
            "parent_sections",
            "chunk_records",
        )
        assert folder.CHUNK_RECORDS != folder.CHUNKS, (
            "the Blob folder of chunk files is another thing"
        )
        source = (AZURE / "03_create_ai_services.py").read_text(encoding="utf-8")
        assert (
            '"vectrixdb"' not in source and '"parents"' not in source and '"chunks"' not in source
        )

    def test_its_indexing_is_the_librarys_and_goes_to_az_as_a_file(self, folder):
        from vectrixdb.chunk_store import INDEXING

        script = load("03_create_ai_services.py")
        assert script.CHUNKS_INDEXING == INDEXING
        with script._indexing_file() as path:
            assert json.loads(Path(path).read_text(encoding="utf-8")) == INDEXING
        assert not Path(path).exists()

    def test_the_app_is_given_the_store_and_the_markdown_the_worker_keeps(self, folder):
        kept = {
            "chunk_store": f"{self.COSMOS}/chunk_records",
            "blob_account": "https://vx.blob.core.windows.net",
        }
        every = folder.env_of(folder.settings(), kept, {})
        assert every["VECTRIXDB_CHUNK_STORE"] == f"{self.COSMOS}/chunk_records"
        assert (
            every["VECTRIXDB_KEEP_SOURCE"] == "https://vx.blob.core.windows.net/ingestion/markdown"
        )
        assert "VECTRIXDB_CHUNK_STORE" not in folder.env_of(folder.settings(), {}, {})

    def test_the_documents_page_reads_where_the_worker_writes(self, folder, monkeypatch):
        from vectrixdb import evaluation
        from vectrixdb.api import documents

        sys.path.insert(0, str(MAIN_FUNCTION_APP))
        try:
            import collections_of
        finally:
            sys.path.remove(str(MAIN_FUNCTION_APP))
        monkeypatch.setattr(documents, "_files", {})
        monkeypatch.setattr(evaluation, "_blob_client", lambda account: "blobs")
        where = folder.env_of(
            folder.settings(), {"blob_account": "https://vx.blob.core.windows.net"}, {}
        )["VECTRIXDB_KEEP_SOURCE"]
        read = documents._files_at(where, "financial")
        written = collections_of.written_to("blobs", "financial")["keep_source"].files
        assert (read.container, read.prefix) == (written.container, written.prefix)

    def test_every_collection_the_app_opens_writes_to_it(self):
        source = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        assert '"chunk_store": os.environ.get("VECTRIXDB_CHUNK_STORE") or None' in source
        assert '"shared_chunks": given("VECTRIXDB_CHUNK_STORE")' in source
        assert '"kept_markdown": given("VECTRIXDB_KEEP_SOURCE")' in source

    def test_a_wheel_from_before_the_store_is_not_published(self, folder):
        """The app passes chunk_store= to every collection, and a wheel without it would open none."""
        needs = load("06_create_main_function_app.py").NEEDS
        assert needs["vectrixdb/chunk_store.py"] == "class CosmosChunks"
        for path, line in needs.items():
            assert line in (AZURE.parents[1] / path).read_text(encoding="utf-8"), path

    def test_06_gives_the_app_a_cosmos_data_role_once(self, folder):
        script = load("06_create_main_function_app.py")

        class Recorder:
            pretend = False

            def __init__(self, held):
                self.held, self.calls = held, []

            def __call__(self, *args, **_):
                self.calls.append(args)
                return (
                    self.held
                    if args[:5] == ("cosmosdb", "sql", "role", "assignment", "list")
                    else None
                )

        fresh = Recorder([])
        script.grant_cosmos(fresh, "vxtest1234-cosmos", "vxtest1234-rg", "principal-1")
        (created,) = [call for call in fresh.calls if "create" in call]
        assert (
            created[created.index("--role-definition-id") + 1]
            == "00000000-0000-0000-0000-000000000002"
        )
        assert (
            created[created.index("--principal-id") + 1] == "principal-1"
            and created[created.index("--scope") + 1] == "/"
        )
        role = "/subscriptions/s/resourceGroups/g/providers/Microsoft.DocumentDB/databaseAccounts/a/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
        held = Recorder([{"principalId": "principal-1", "roleDefinitionId": role}])
        script.grant_cosmos(held, "vxtest1234-cosmos", "vxtest1234-rg", "principal-1")
        assert not [call for call in held.calls if "create" in call], (
            "asking again is refused, so it is not asked"
        )
        source = (AZURE / "06_create_main_function_app.py").read_text(encoding="utf-8")
        assert (
            'if kept.get("parent_store") or kept.get("chunk_store") or kept.get("collection_store"):'
            in source
            and "grant_cosmos(az" in source
        )


class TestWhatCreatingCosmosTaught:
    """Every one of these was a live failure, not a guess."""

    def source(self):
        return (AZURE / "03_create_ai_services.py").read_text(encoding="utf-8")

    def test_being_there_is_not_being_healthy(self):
        """A failed account still answers `show`, so `exists` said yes and we wrote down a dead address."""
        common = (AZURE / "_common.py").read_text(encoding="utf-8")
        assert "def state_of" in common and "provisioningState" in common
        assert 'state == "Succeeded"' in self.source(), "existence is not enough for Cosmos"

    def test_an_account_still_being_made_is_waited_for_not_deleted(self):
        """One run deleted an account three minutes into being created by another."""
        source = self.source()
        assert 'BUSY = ("Creating", "Updating", "Deleting")' in source
        assert "state in BUSY" in source and "_settled(" in source

    def test_a_failed_account_is_deleted_and_the_delete_is_waited_for(self):
        """Azure refuses to create over it, and the delete returns before it is gone."""
        source = self.source()
        removed = source.split("def _removed(")[1].split("def cosmos(")[0]
        assert "delete" in removed and "time.sleep" in removed
        assert "state_of" in removed, "it waits for the account to actually disappear"

    def test_a_region_with_no_room_is_not_blamed_on_the_free_tier(self):
        """Canada Central refused outright: "high demand ... cannot fulfill your request"."""
        source = self.source()
        assert "VX_COSMOS_ELSEWHERE" in source or "elsewhere" in source
        assert "the free tier is one account a subscription and yours is spent" not in source, (
            "it claimed to know why, and it was wrong"
        )


class FakeAz:
    """The command line as a script sees it, answering from a table and keeping what it was asked."""

    pretend = False

    def __init__(self, refuse=(), usage=None, models=None, deleted=(), account=None):
        self.refuse, self.usage, self.models = tuple(refuse), usage or {}, models or {}
        self.deleted, self.account, self.asked, self.state, self.made = (
            list(deleted),
            account,
            [],
            "",
            set(),
        )

    def __call__(self, *args, reads=False, quiet=False, allow_fail=False):
        self.asked.append(args)
        said = " ".join(args)
        if said.startswith("cosmosdb create"):
            region = [a for a in args if a.startswith("regionName=")][0].split("=")[1]
            if region in self.refuse:
                self.state = "Failed"
                return None
            self.state = "Succeeded"
            return {}
        if said.startswith("cosmosdb delete"):
            self.state = ""
            return {}
        if said.startswith("cosmosdb show"):
            return {"capabilities": [{"name": "EnableServerless"}]}
        if said.startswith("cognitiveservices usage list"):
            return self.usage.get(args[args.index("--location") + 1])
        if said.startswith("cognitiveservices model list"):
            return self.models.get(args[args.index("--location") + 1], [])
        if said.startswith("cognitiveservices account list-deleted"):
            return self.deleted
        if said.startswith("cognitiveservices account show"):
            return self.account
        if args[-3:-1] == ("--location",) or "create" in args:
            where = [
                args[i + 1]
                for i, a in enumerate(args)
                if a in ("--location", "--flexconsumption-location")
            ]
            if where and where[0] not in self.refuse:
                self.made.add(args[args.index("--name") + 1])
        return {}

    def exists(self, *args):
        return "--name" in args and args[args.index("--name") + 1] in self.made

    def state_of(self, *args):
        return self.state

    def made_in(self):
        return [
            a.split("=")[1]
            for args in self.asked
            if args[:2] == ("cosmosdb", "create")
            for a in args
            if a.startswith("regionName=")
        ]


def _model(name, version="2024-07-18", skus=("GlobalStandard",)):
    return {
        "model": {
            "name": name,
            "format": "OpenAI",
            "version": version,
            "skus": [{"name": s} for s in skus],
        }
    }


def _quota(model, limit, used=0, sku="GlobalStandard"):
    return {"name": {"value": f"OpenAI.{sku}.{model}"}, "limit": limit, "currentValue": used}


class TestTheRegionsAreTriedInTheOrderWritten:
    """Canada Central, then East US, then West US: what a person writes is what is tried."""

    def test_the_order_written_is_the_order_tried_and_none_is_tried_twice(self):
        regions = load("03_create_ai_services.py").regions
        assert regions("canadacentral", "eastus, westus") == ["canadacentral", "eastus", "westus"]
        assert regions("canadacentral", "East US,canadacentral,,westus") == [
            "canadacentral",
            "eastus",
            "westus",
        ]
        assert regions("canadacentral", "") == ["canadacentral"] and regions(
            "canadacentral", None
        ) == ["canadacentral"]

    def test_cosmos_goes_to_the_third_when_two_have_no_room(self, folder, monkeypatch, capsys):
        step = load("03_create_ai_services.py")
        monkeypatch.setattr(step.time, "sleep", lambda seconds: None)
        az = FakeAz(refuse=("canadacentral", "eastus"))
        made = step.cosmos(
            az,
            "vxtest1234-cosmos",
            "vxtest1234-rg",
            "canadacentral",
            True,
            elsewhere="eastus,westus",
        )
        assert az.made_in() == ["canadacentral", "canadacentral", "eastus", "westus"], (
            "free, then serverless, then each region once"
        )
        assert made and made["collection_store"].endswith("/data_db/collection_records")
        said = capsys.readouterr().out
        assert "trying eastus" in said and "trying westus" in said

    def test_cosmos_says_no_when_every_region_has_none(self, folder, monkeypatch):
        step = load("03_create_ai_services.py")
        monkeypatch.setattr(step.time, "sleep", lambda seconds: None)
        az = FakeAz(refuse=("canadacentral", "eastus", "westus"))
        assert (
            step.cosmos(
                az,
                "vxtest1234-cosmos",
                "vxtest1234-rg",
                "canadacentral",
                False,
                elsewhere="eastus,westus",
            )
            is None
        )
        assert az.made_in() == ["canadacentral", "eastus", "westus"]

    def test_the_example_settings_name_both_and_the_script_says_why(self, folder):
        example = (AZURE / "settings.example.env").read_text(encoding="utf-8")
        assert "# VX_COSMOS_ELSEWHERE=" in example and "# VX_OPENAI_ELSEWHERE=" in example
        assert "VX_ELSEWHERE=eastus,westus" in example
        found = folder.settings()
        assert found["VX_ELSEWHERE"] == "eastus,westus"
        assert found["VX_COSMOS_ELSEWHERE"] == found["VX_OPENAI_ELSEWHERE"] == "eastus,westus", (
            "left out, each follows the one order"
        )
        source = (AZURE / "03_create_ai_services.py").read_text(encoding="utf-8")
        assert "VX_OPENAI_ELSEWHERE" in source and "tried in the order written" in source


class TestEveryResourceFollowsTheOrder:
    """Storage, search, the AI services and the apps: asked for at home, then in each region after it."""

    def test_the_first_region_that_takes_it_is_where_it_goes(self, folder, capsys):
        az, asked = FakeAz(refuse=("canadacentral", "eastus")), []

        def create(where):
            asked.append(where)
            az("thing", "create", "--name", "x", "--location", where)

        went = folder.somewhere(
            az,
            ["canadacentral", "eastus", "westus"],
            create,
            lambda: az.exists("thing", "show", "--name", "x"),
            "x",
        )
        assert went == "westus" and asked == ["canadacentral", "eastus", "westus"]
        said = capsys.readouterr().out
        assert (
            "canadacentral would not take x; trying eastus" in said
            and "eastus would not take x; trying westus" in said
        )

    def test_home_is_asked_once_when_home_takes_it(self, folder):
        az, asked = FakeAz(), []
        went = folder.somewhere(
            az,
            ["canadacentral", "eastus"],
            lambda where: (
                asked.append(where),
                az("thing", "create", "--name", "x", "--location", where),
            ),
            lambda: az.exists("thing", "show", "--name", "x"),
            "x",
        )
        assert went == "canadacentral" and asked == ["canadacentral"]

    def test_none_is_said_as_none_and_what_was_left_is_cleared(self, folder):
        az, cleared = FakeAz(refuse=("canadacentral", "eastus")), []
        went = folder.somewhere(
            az,
            ["canadacentral", "eastus"],
            lambda where: az("thing", "create", "--name", "x", "--location", where),
            lambda: False,
            "x",
            lambda: cleared.append(1),
        )
        assert went == "" and len(cleared) == 2
        assert "VX_ELSEWHERE" in folder._no_region("x", ["canadacentral", "eastus"])

    def test_a_dry_run_stays_home(self, folder):
        assert (
            folder.somewhere(
                folder.Az(pretend=True),
                ["canadacentral", "eastus"],
                lambda where: None,
                lambda: False,
                "x",
            )
            == "canadacentral"
        )

    def test_an_ai_account_goes_to_the_next_region(self, capsys):
        step = load("03_create_ai_services.py")
        az = FakeAz(refuse=("canadacentral",))
        assert step.cognitive(
            az,
            "vxtest1234-speech",
            "vxtest1234-rg",
            "canadacentral",
            "SpeechServices",
            "F0",
            "free tier",
            "eastus,westus",
        )
        where = [
            a[a.index("--location") + 1]
            for a in az.asked
            if a[:3] == ("cognitiveservices", "account", "create")
        ]
        assert where == ["canadacentral", "eastus"]
        assert "vxtest1234-speech, free tier, in eastus" in capsys.readouterr().out

    def test_an_ai_account_nobody_takes_is_false(self):
        step = load("03_create_ai_services.py")
        az = FakeAz(refuse=("canadacentral", "eastus", "westus"))
        assert not step.cognitive(
            az,
            "vxtest1234-speech",
            "vxtest1234-rg",
            "canadacentral",
            "SpeechServices",
            "F0",
            "free tier",
            "eastus,westus",
        )

    def test_every_step_that_makes_something_uses_it(self):
        for name in (
            "01_create_resources.py",
            "02_create_search.py",
            "03_create_ai_services.py",
            "05_create_extraction_function_app.py",
            "06_create_main_function_app.py",
        ):
            source = (AZURE / name).read_text(encoding="utf-8")
            assert "somewhere(" in source and 'config["VX_ELSEWHERE"]' in source, name
        assert "regions(" in (AZURE / "00_login.py").read_text(encoding="utf-8"), (
            "and 00 says the order before anything is made"
        )

    def test_a_cosmos_account_that_was_made_is_not_read_as_refused(
        self, folder, monkeypatch, capsys
    ):
        step = load("03_create_ai_services.py")
        monkeypatch.setattr(step.time, "sleep", lambda seconds: None)
        az = FakeAz()
        assert step.cosmos(
            az,
            "vxtest1234-cosmos",
            "vxtest1234-rg",
            "canadacentral",
            True,
            elsewhere="eastus,westus",
        )
        assert az.made_in() == ["canadacentral"]
        said = capsys.readouterr().out
        assert (
            "refused" not in said and "trying" not in said and "free tier in canadacentral" in said
        )


class TestAzureOpenAIGoesWhereTheQuotaIs:
    """Offered is not allowed: a region can list a model and give the subscription nothing for it."""

    MODELS = {
        r: [_model("gpt-5.4-mini"), _model("text-embedding-3-small", "1")]
        for r in ("canadacentral", "eastus", "westus")
    }

    def test_quota_is_what_is_left_and_no_line_is_no_quota(self):
        room = load("03_create_ai_services.py").room
        az = FakeAz(
            usage={
                "eastus": [
                    _quota("gpt-5.4-mini", 2000),
                    _quota("text-embedding-3-small", 1000, used=990),
                ],
                "canadacentral": [_quota("gpt-5.4-mini-transcribe", 600)],
            }
        )
        assert room(az, "eastus", "gpt-5.4-mini", "GlobalStandard", 50)
        assert not room(az, "eastus", "text-embedding-3-small", "GlobalStandard", 20), (
            "ten left is not twenty"
        )
        assert not room(az, "canadacentral", "gpt-5.4-mini", "GlobalStandard", 50), (
            "a line for another model is not a line for this one"
        )
        assert room(az, "westus", "gpt-5.4-mini", "GlobalStandard", 50), (
            "an answer that could not be read is not read as no"
        )

    def test_it_goes_to_the_first_region_with_every_model(self, folder, capsys):
        step = load("03_create_ai_services.py")
        both = [_quota("gpt-5.4-mini", 2000), _quota("text-embedding-3-small", 1000)]
        az = FakeAz(
            models=self.MODELS,
            usage={
                "canadacentral": [_quota("text-embedding-3-small", 1000)],
                "eastus": both,
                "westus": both,
            },
        )
        assert (
            step.home_of_openai(az, folder.settings(), ["canadacentral", "eastus", "westus"])
            == "eastus"
        )
        assert (
            "canadacentral has no quota left for gpt-5.4-mini as GlobalStandard; trying eastus"
            in capsys.readouterr().out
        )

    def test_home_is_kept_when_home_has_it(self, folder):
        step = load("03_create_ai_services.py")
        both = [_quota("gpt-5.4-mini", 2000), _quota("text-embedding-3-small", 1000)]
        az = FakeAz(models=self.MODELS, usage={"canadacentral": both, "eastus": both})
        assert (
            step.home_of_openai(az, folder.settings(), ["canadacentral", "eastus", "westus"])
            == "canadacentral"
        )

    def test_with_none_anywhere_it_stays_home_and_says_so(self, folder, capsys):
        step = load("03_create_ai_services.py")
        az = FakeAz(models=self.MODELS, usage={"canadacentral": [], "eastus": [], "westus": []})
        assert (
            step.home_of_openai(az, folder.settings(), ["canadacentral", "eastus", "westus"])
            == "canadacentral"
        )
        assert (
            "no region of canadacentral, eastus, westus has every model" in capsys.readouterr().out
        )

    def test_a_dry_run_asks_nothing_and_stays_home(self, folder):
        step = load("03_create_ai_services.py")
        assert (
            step.home_of_openai(
                folder.Az(pretend=True), folder.settings(), ["canadacentral", "eastus"]
            )
            == "canadacentral"
        )


class TestAModelOnItsWayOutIsNotOffered:
    """Azure listed gpt-4o-mini with its skus and its quota, and refused every new deployment of it."""

    def test_a_deprecating_version_is_passed_over_for_one_that_is_not(self):
        step = load("03_create_ai_services.py")
        going = _model("chat", "2024-07-18")
        going["model"]["lifecycleStatus"] = "Deprecating"
        staying = _model("chat", "2024-01-01")
        staying["model"]["lifecycleStatus"] = "GenerallyAvailable"
        assert step.offered(FakeAz(models={"eastus": [going, staying]}), "eastus", "chat") == (
            "2024-01-01",
            "GlobalStandard",
        )
        assert step.offered(FakeAz(models={"eastus": [going]}), "eastus", "chat") is None

    def test_a_version_that_says_nothing_of_itself_is_offered(self):
        step = load("03_create_ai_services.py")
        assert step.offered(FakeAz(models={"eastus": [_model("chat", "1")]}), "eastus", "chat") == (
            "1",
            "GlobalStandard",
        )

    def test_the_default_is_a_model_azure_still_deploys(self, folder):
        config = folder.settings()
        assert (
            config["AZURE_OPENAI_WRITER_DEPLOYMENT"]
            == config["AZURE_OPENAI_VISION_DEPLOYMENT"]
            == "gpt-5.4-mini"
        )

    def test_a_refusal_is_given_in_azures_words(self, folder):
        az = FakeAz()
        az.said = "ERROR: (ServiceModelDeprecating) The model is going.\nCode: ServiceModelDeprecating\nMessage: The model is in deprecating state and cannot be used for new deployments."
        assert (
            folder.reason(az)
            == "The model is in deprecating state and cannot be used for new deployments."
        )
        az.said = "ERROR: something else"
        assert folder.reason(az) == "something else"
        az.said = ""
        assert folder.reason(az) == ""
        source = (AZURE / "03_create_ai_services.py").read_text(encoding="utf-8")
        assert "Azure said: {why}" in source and "quota page says what is left" not in source, (
            "it guessed quota, and it was wrong"
        )


class TestEveryPartSaysWhatItNeeds:
    """Step 04 stopped on a machine with no Cosmos client, and nothing in the folder said what to install."""

    PARTS = ("", "main_function_app", "extraction_app", "dashboard", "dashboard/Backend")

    @staticmethod
    def wanted(part):
        lines = (AZURE / part / "requirements.txt").read_text(encoding="utf-8").splitlines()
        return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]

    def test_every_part_with_python_in_it_has_a_list(self):
        for part in self.PARTS:
            assert self.wanted(part), part or "the steps"

    def test_the_steps_get_the_library_from_this_repository_with_what_they_use(self):
        (library,) = [line for line in self.wanted("") if "[" in line]
        assert library.startswith("-e ../..["), "this library, not the one on PyPI"
        extras = library.split("[")[1].rstrip("]").split(",")
        assert {"azure", "api", "documents", "ocr-azure"} <= set(extras)
        assert {"build", "wheel", "pillow"} <= set(self.wanted("")), (
            "and what builds the wheel 05 and 06 publish"
        )

    def test_every_extra_a_list_asks_for_is_one_the_library_offers(self):
        import tomllib

        offered = set(
            tomllib.loads((AZURE.parents[1] / "pyproject.toml").read_text(encoding="utf-8"))[
                "project"
            ]["optional-dependencies"]
        )
        for part in self.PARTS:
            for line in self.wanted(part):
                if "[" in line and "vectrixdb" in line or line.startswith("-e ../..["):
                    asked = set(line.split("[")[1].split("]")[0].split(","))
                    assert asked <= offered, (part, asked - offered)

    def test_the_dashboard_reads_its_backends_list_and_keeps_none_of_its_own(self):
        assert self.wanted("dashboard") == ["-r Backend/requirements.txt"], (
            "one list, read from two places"
        )

    def test_the_readme_and_the_refusals_point_at_it(self):
        assert "pip install -r requirements.txt" in (AZURE / "README.md").read_text(
            encoding="utf-8"
        )
        common = (AZURE / "_common.py").read_text(encoding="utf-8")
        assert (
            common.count("pip install -r requirements.txt") == 2
            and "pip install azure-cosmos" not in common
        )


class TestTheTranslateRoutesHaveSomethingToAnswerWith:
    """03 made no Translator, so two routes of the extraction app answered 503 on every deployment."""

    def test_the_settings_name_it_and_ask_for_it(self, folder):
        config = folder.settings()
        assert (
            config["VX_TRANSLATOR"] == "yes"
            and config["VX_TRANSLATOR_NAME"] == "vxtest1234-translator"
        )

    def test_03_makes_it_on_the_free_tier_in_the_region_order(self):
        source = (AZURE / "03_create_ai_services.py").read_text(encoding="utf-8")
        made = source.split('if wants(config, "VX_TRANSLATOR"):')[1].split(
            'if not wants(config, "VX_OPENAI"):'
        )[0]
        assert '"TextTranslation", "F0"' in flat(made) and 'config["VX_ELSEWHERE"]' in made
        assert 'kept["translator_region"] = region_of(az, name, group) or region' in made, (
            "the region it landed in, not the one asked for"
        )

    def test_the_region_is_read_as_a_command_writes_it(self):
        step = load("03_create_ai_services.py")
        assert step.region_of(FakeAz(account={"location": "East US"}), "n", "g") == "eastus"
        assert step.region_of(FakeAz(account=None), "n", "g") == ""

    def test_05_sends_the_key_and_the_region_together_or_neither(self, folder):
        step = load("05_create_extraction_function_app.py")
        config = folder.settings()
        both = step.extraction_env(
            config, {"translator_region": "canadacentral"}, {"AZURE_TRANSLATOR_KEY": "k"}, "door"
        )
        assert (both["AZURE_TRANSLATOR_KEY"], both["AZURE_TRANSLATOR_REGION"]) == (
            "k",
            "canadacentral",
        )
        assert "AZURE_TRANSLATOR_ENDPOINT" not in both, (
            "every Translator is called at the one shared address"
        )
        no_region = step.extraction_env(config, {}, {"AZURE_TRANSLATOR_KEY": "k"}, "door")
        assert (
            "AZURE_TRANSLATOR_KEY" not in no_region and "AZURE_TRANSLATOR_REGION" not in no_region
        ), "a key with no region is refused on the first call"
        no_key = step.extraction_env(config, {"translator_region": "canadacentral"}, {}, "door")
        assert "AZURE_TRANSLATOR_REGION" not in no_key

    def test_05_reads_the_key_from_azure_only_when_it_is_wanted(self, folder):
        step = load("05_create_extraction_function_app.py")

        class Keys(FakeAz):
            def __call__(self, *args, **kw):
                self.asked.append(args)
                return {"key1": "k-" + args[args.index("--name") + 1]} if "keys" in args else {}

        config = folder.settings()
        assert (
            step.service_keys(Keys(), config)["AZURE_TRANSLATOR_KEY"] == "k-vxtest1234-translator"
        )
        assert "AZURE_TRANSLATOR_KEY" not in step.service_keys(
            Keys(), {**config, "VX_TRANSLATOR": "no"}
        )

    def test_the_one_host_named_is_where_the_walkthroughs_files_come_from(self):
        import json
        import re

        example = (AZURE / "settings.example.env").read_text(encoding="utf-8")
        (hosts,) = [
            line.split("=", 1)[1]
            for line in example.splitlines()
            if line.startswith("VX_EXTRACT_URL_HOSTS=")
        ]
        sources = json.dumps(
            json.loads((AZURE / ".local" / "sources.json").read_text(encoding="utf-8"))
        )
        fetched = {found.split("/")[2] for found in re.findall(r"https?://[^ \"]+", sources)}
        assert {h.strip() for h in hosts.split(",")} == fetched == {"www.td.com"}
        assert "*" not in hosts, "no wildcard: a list of who is trusted"


class TestANameAzureStillHoldsIsPurgedFirst:
    """Deleting the group does not purge: the name is held for 48 hours."""

    HELD = {
        "name": "vxtest1234-openai",
        "location": "canadacentral",
        "id": "/subscriptions/s/providers/Microsoft.CognitiveServices/locations/canadacentral/resourceGroups/vxtest1234-rg/deletedAccounts/vxtest1234-openai",
    }

    def test_it_is_purged_from_where_it_was_and_then_made(self, capsys):
        step = load("03_create_ai_services.py")
        az = FakeAz(deleted=[self.HELD])
        assert step.cognitive(
            az, "vxtest1234-openai", "vxtest1234-rg", "eastus", "OpenAI", "S0", "the chat model"
        )
        said = [" ".join(a) for a in az.asked]
        purge = [s for s in said if s.startswith("cognitiveservices account purge")]
        create = [s for s in said if s.startswith("cognitiveservices account create")]
        assert (
            len(purge) == 1
            and "--location canadacentral" in purge[0]
            and "--resource-group vxtest1234-rg" in purge[0]
        )
        assert len(create) == 1 and "--location eastus" in create[0]
        assert said.index(purge[0]) < said.index(create[0])
        assert "Azure still holds the name" in capsys.readouterr().out

    def test_a_name_nobody_holds_is_only_made(self):
        step = load("03_create_ai_services.py")
        az = FakeAz(deleted=[dict(self.HELD, name="somebody-elses")])
        assert step.cognitive(
            az, "vxtest1234-openai", "vxtest1234-rg", "eastus", "OpenAI", "S0", "the chat model"
        )
        assert not [a for a in az.asked if a[:3] == ("cognitiveservices", "account", "purge")]


class TestAnIndexIsNamedAfterItsCollection:
    """financial, media, misc. Not vectrix-financial.

    The watcher reported an empty service for twenty-five minutes while
    three indexes filled, because it asked for a name nothing answered to.
    Making the index the collection removes the thing it got wrong.
    """

    def test_every_way_in_opens_the_store_with_no_prefix(self):
        for name in ("main_function_app/function_app.py",):
            source = (AZURE / name).read_text(encoding="utf-8")
            if "with_azure_search(" not in source:
                continue
            opened = source.split("with_azure_search(")[1][:400]
            assert 'index_prefix=""' in opened, f"{name} would look for vectrix-financial"

    def test_the_app_is_told_the_same_thing(self):
        assert '"AZURE_SEARCH_INDEX_PREFIX": ""' in (AZURE / "_common.py").read_text(
            encoding="utf-8"
        )

    def test_the_library_builds_that_name(self):
        """An empty prefix joined with a dash gives -financial, which Azure refuses."""
        from vectrixdb.core.storage import StorageBackend, StorageConfig
        from vectrixdb.core.storage_azure import AzureSearchStorage

        def named(prefix, collection):
            config = StorageConfig(
                backend=StorageBackend.AZURE_SEARCH,
                azure_search_endpoint="https://x.search.windows.net",
                azure_search_index_prefix=prefix,
            )
            return AzureSearchStorage._index_name(type("S", (), {"config": config})(), collection)

        assert named("", "financial") == "financial"
        assert named("", "misc") == "misc"
        assert named("vectrix", "financial") == "vectrix-financial", (
            "the default is unchanged for everyone else"
        )

    def test_the_api_can_be_told_an_empty_prefix(self):
        """Present and empty is a choice, and a different one from absent."""
        source = (Path(vectrixdb.__file__).parent / "api" / "server.py").read_text(encoding="utf-8")
        assert "AZURE_SEARCH_INDEX_PREFIX" in source and "in os.environ" in source

    def test_the_empty_setting_azure_drops_is_put_back_before_the_api_starts(self):
        """Azure hands the app no setting whose value is empty, so the API saw none and listed vectrix-collections."""
        source = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        put_back = 'os.environ.setdefault("AZURE_SEARCH_INDEX_PREFIX", "")'
        assert put_back in source and source.index(put_back) < source.index("api = create_app(")


class TestRawAndMarkdownShareOneContainer:
    """ingestion/raw/ is uploaded, ingestion/markdown/ is written, and only raw/ is ever read.

    Sharing a container is what makes the loop possible: every Markdown file
    the function writes is a new blob, and an event for it would be read,
    written again, and go round for ever. Two guards stop it, and both are
    held here.
    """

    ENV = {"INGEST_COLLECTIONS": "financial,media,misc"}
    ACCOUNT = "https://a.blob.core.windows.net"

    def routing(self):
        sys.path.insert(0, str(MAIN_FUNCTION_APP))
        try:
            import collections_of

            return collections_of
        finally:
            sys.path.remove(str(MAIN_FUNCTION_APP))

    def test_the_event_is_filtered_to_raw_and_nothing_else(self):
        """The first guard, and the one that matters: no event, no read."""
        source = (AZURE / "06_create_main_function_app.py").read_text(encoding="utf-8")
        assert 'f"/blobServices/default/containers/{INGESTION}/blobs/{RAW}/"' in source

    def test_the_router_refuses_what_the_function_wrote(self):
        """The second guard: an event for markdown/ or chunks/ that slips through costs nothing."""
        routing = self.routing()
        for written in (
            f"{self.ACCOUNT}/ingestion/markdown/financial/td/ar2025.pdf.md",
            f"{self.ACCOUNT}/ingestion/markdown/media/male.wav.md",
            f"{self.ACCOUNT}/ingestion/markdown/financial/_index.json",
            f"{self.ACCOUNT}/ingestion/chunks/financial/td/ar2025.pdf.jsonl",
            f"{self.ACCOUNT}/ingestion/chunks/media/male.wav.jsonl",
        ):
            assert routing.collection_of(written, self.ENV) is None, written
            assert routing.metadata_for(written, self.ENV) == {}, written

    def test_raw_is_routed_by_the_folder_after_it(self):
        routing = self.routing()
        assert (
            routing.collection_of(f"{self.ACCOUNT}/ingestion/raw/financial/td/ar2025.pdf", self.ENV)
            == "financial"
        )
        assert (
            routing.collection_of(f"{self.ACCOUNT}/ingestion/raw/media/male.wav", self.ENV)
            == "media"
        )

    def test_a_blob_straight_in_the_container_is_not_raw(self):
        """Uploaded without the raw/ folder is a mistake, and a mistake is left alone."""
        routing = self.routing()
        assert (
            routing.collection_of(f"{self.ACCOUNT}/ingestion/financial/td/ar2025.pdf", self.ENV)
            is None
        )

    def test_a_document_is_named_by_its_path_inside_its_collection(self):
        """Not the blob address, which carried the storage account's host name."""
        routing = self.routing()
        assert (
            routing.doc_id_of(
                f"{self.ACCOUNT}/ingestion/raw/financial/td/td-annual-report-2025.pdf"
            )
            == "td/td-annual-report-2025.pdf"
        )
        assert routing.doc_id_of(f"{self.ACCOUNT}/ingestion/raw/media/male.wav") == "male.wav"

    def test_the_id_survives_the_account_being_made_again(self):
        routing = self.routing()
        one = routing.doc_id_of(
            "https://vxtest1234store.blob.core.windows.net/ingestion/raw/misc/office.png"
        )
        two = routing.doc_id_of(
            "https://somethingelse.blob.core.windows.net/ingestion/raw/misc/office.png"
        )
        assert one == two == "office.png", (
            "a golden file written against the first still matches the second"
        )

    def test_the_markdown_mirrors_the_upload(self):
        """raw/financial/td/x.pdf on one side is markdown/financial/td/x.pdf.md on the other."""
        from vectrixdb.documents import stored_name

        routing = self.routing()
        for collection, inside in (
            ("financial", "td/td-annual-report-2025.pdf"),
            ("media", "male.wav"),
            ("misc", "office.png"),
        ):
            doc_id = routing.doc_id_of(f"{self.ACCOUNT}/ingestion/raw/{collection}/{inside}")
            assert (
                f"markdown/{collection}/{stored_name(doc_id)}"
                == f"markdown/{collection}/{inside}.md"
            )

    def test_the_chunks_mirror_it_too(self):
        """raw/financial/td/x.pdf on one side is chunks/financial/td/x.pdf.jsonl on the other."""
        from vectrixdb.documents import chunks_name

        routing = self.routing()
        for collection, inside in (
            ("financial", "td/td-annual-report-2025.pdf"),
            ("media", "male.wav"),
            ("misc", "office.png"),
        ):
            doc_id = routing.doc_id_of(f"{self.ACCOUNT}/ingestion/raw/{collection}/{inside}")
            assert (
                f"chunks/{collection}/{chunks_name(doc_id)}"
                == f"chunks/{collection}/{inside}.jsonl"
            )

    def test_the_function_keeps_the_markdown_there_and_names_documents_that_way(self):
        source = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        assert (
            "return written_to(blobs, name)" in source and "**blob_folders(blobs, name)," in source
        )
        assert "doc_id_of=doc_id_of" in source
        routing = (MAIN_FUNCTION_APP / "collections_of.py").read_text(encoding="utf-8")
        assert 'BlobFiles(blobs, CONTAINER, prefix=f"{MARKDOWN}/{name}")' in routing
        assert 'BlobFiles(blobs, CONTAINER, prefix=f"{CHUNKS}/{name}")' in routing

    def test_uploads_go_under_raw(self):
        source = (AZURE / "_common.py").read_text(encoding="utf-8")
        assert 'blob = f"{RAW}/{inside}"' in source

    def test_two_containers_and_no_third(self):
        common = (AZURE / "_common.py").read_text(encoding="utf-8")
        assert 'INGESTION, EVALS = "ingestion", "evals"' in common
        assert 'RAW, MARKDOWN, CHUNKS = "raw", "markdown", "chunks"' in common
        assert "INGEST_KEPT_CONTAINER" not in common, (
            "the Markdown is no longer a container of its own"
        )
        routing = (MAIN_FUNCTION_APP / "collections_of.py").read_text(encoding="utf-8")
        assert 'RAW, MARKDOWN, CHUNKS = "raw", "markdown", "chunks"' in routing, (
            "the app and the scripts name them alike"
        )


class TestMarkdownThenChunksThenTheIndex:
    """The main app writes each step down before the next, and a file deleted from raw/ takes all of it.

    Run through the collection's real options, ``written_to``, against a fake
    blob service: the Markdown in markdown/<collection>/, the chunks in
    chunks/<collection>/, the index last.
    """

    ENV = {"INGEST_COLLECTIONS": "financial,media,misc"}
    RAW = "https://a.blob.core.windows.net/ingestion/raw/financial/td/ar2025.pdf"
    MARKDOWN = ("ingestion", "markdown/financial/td/ar2025.pdf.md")
    CHUNKS = ("ingestion", "chunks/financial/td/ar2025.pdf.jsonl")

    def routing(self):
        sys.path.insert(0, str(MAIN_FUNCTION_APP))
        try:
            import collections_of

            return collections_of
        finally:
            sys.path.remove(str(MAIN_FUNCTION_APP))

    def opened(self, tmp_path, reader):
        import numpy as np

        sys.path.insert(0, str(Path(__file__).parent))
        from test_documents import FakeBlobService

        from vectrixdb import Vectrix
        from vectrixdb.worker import BlobFetcher, IngestWorker

        def embed(texts):
            out = np.zeros((len(texts), 8), dtype=np.float32)
            for i, text in enumerate(texts):
                for word in text.lower().split():
                    out[i, hash(word) % 8] += 1.0
                out[i] /= np.linalg.norm(out[i]) or 1.0
            return out

        routing, blobs = self.routing(), FakeBlobService()
        blobs.blobs[("ingestion", "raw/financial/td/ar2025.pdf")] = b"%PDF the bytes"
        db = Vectrix(
            "financial",
            path=str(tmp_path / "db"),
            embed_fn=embed,
            dimension=8,
            extractors={".pdf": reader},
            **routing.written_to(blobs, "financial"),
        )
        worker = IngestWorker(
            db,
            BlobFetcher(blobs),
            on_error="record",
            doc_id_of=routing.doc_id_of,
            metadata_of=lambda event: routing.metadata_for(event.uri, self.ENV),
            chunk="markdown",
            chunk_size=1000,
            overlap=200,
        )
        return db, worker, blobs

    def test_a_file_dropped_in_raw_is_written_as_markdown_then_chunks(self, tmp_path):
        from vectrixdb.worker import IngestEvent

        db, worker, blobs = self.opened(
            tmp_path, lambda data, name: "# Covenants\n\nThe covenant is tested quarterly."
        )
        try:
            outcome = worker.handle(IngestEvent("created", self.RAW, version="0x8DC1"))
            assert outcome.action == "created"
            assert b"The covenant is tested quarterly." in blobs.blobs[self.MARKDOWN]
            lines = [
                json.loads(line) for line in blobs.blobs[self.CHUNKS].decode("utf-8").splitlines()
            ]
            indexed = {i: t for i, t, _m in db._collection._iter_documents_raw()}
            assert {line["id"]: line["text"] for line in lines} == indexed
            assert all(
                line["metadata"]["collection"] == "financial"
                and "client_id" not in line["metadata"]
                for line in lines
            ), "a chunk says which collection, and nothing a rule reads"
        finally:
            db.close()

    def test_a_retry_starts_from_the_markdown(self, tmp_path, monkeypatch):
        from vectrixdb.worker import IngestEvent

        read = []
        db, worker, blobs = self.opened(
            tmp_path, lambda data, name: read.append(name) or "The covenant is tested quarterly."
        )
        try:
            real_add = db.add
            monkeypatch.setattr(
                db, "add", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the index is down"))
            )
            with pytest.raises(RuntimeError):
                worker.handle(IngestEvent("created", self.RAW, version="0x8DC1"))
            assert self.MARKDOWN in blobs.blobs and self.CHUNKS in blobs.blobs, (
                "both were written before the index"
            )
            monkeypatch.setattr(db, "add", real_add)
            assert (
                worker.handle(IngestEvent("created", self.RAW, version="0x8DC1")).action
                == "created"
            )
            assert read == ["ar2025.pdf"], "the second try did not read the file again"
        finally:
            db.close()

    def test_a_file_deleted_from_raw_takes_its_markdown_and_chunks_with_it(self, tmp_path):
        from vectrixdb.worker import IngestEvent

        db, worker, blobs = self.opened(
            tmp_path, lambda data, name: "The covenant is tested quarterly."
        )
        try:
            worker.handle(IngestEvent("created", self.RAW, version="0x8DC1"))
            del blobs.blobs[("ingestion", "raw/financial/td/ar2025.pdf")]
            assert worker.handle(IngestEvent("deleted", self.RAW)).action == "deleted"
            assert self.MARKDOWN not in blobs.blobs and self.CHUNKS not in blobs.blobs
            left = sorted(name for _c, name in blobs.blobs)
            assert left == ["markdown/financial/_index.json"], f"no copy kept anywhere: {left}"
            assert not list(db._collection._iter_documents_raw()), (
                "and nothing of it is left in the index"
            )
        finally:
            db.close()


class TestTheExtractionApp:
    """The library's extraction service as a Function App of its own: a utility API that stands alone."""

    def test_it_is_named_from_the_one_word_like_everything_else(self, folder):
        assert folder.settings()["VX_EXTRACT_APP"] == "vxtest1234-extract"

    def test_its_code_only_uses_names_the_library_has(self):
        tree = ast.parse((EXTRACTION_APP / "function_app.py").read_text(encoding="utf-8"))
        imported = [
            (node.module, alias.name)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("vectrixdb")
            for alias in node.names
        ]
        assert imported, "it is the library's app, so it imports it"
        for module, name in imported:
            assert hasattr(importlib.import_module(module), name), f"{module}.{name}"

    def test_the_queue_it_reads_is_the_one_the_library_writes(self):
        import inspect

        from vectrixdb.api.extraction import AzureJobs

        source = (EXTRACTION_APP / "function_app.py").read_text(encoding="utf-8")
        assert (
            'queue_name="extract-jobs"' in source and 'connection="AzureWebJobsStorage"' in source
        )
        writes = inspect.getsource(AzureJobs.from_environment)
        assert '"extract-jobs"' in writes and '"AzureWebJobsStorage"' in writes, (
            "the library's defaults, which the app relies on"
        )

    def test_its_docstring_lists_every_route_the_library_serves(self):
        """The module docstring's WHAT IS PUBLISHED is typed by hand, so it is held to the library's routes."""
        import ast
        import re

        import vectrixdb
        from vectrixdb.api.extraction import DOCUMENT_KINDS

        module = (
            ast.get_docstring(
                ast.parse((EXTRACTION_APP / "function_app.py").read_text(encoding="utf-8"))
            )
            or ""
        )
        published = module[module.index("WHAT IS PUBLISHED") : module.index("HOW A CALLER GETS IN")]
        library = (Path(vectrixdb.__file__).parent / "api" / "extraction.py").read_text(
            encoding="utf-8"
        )
        routes = set(re.findall(r'@router\.(?:get|post)\(\s*"([^"]+)"', library)) | {
            f"/extract/{kind}" for kind in DOCUMENT_KINDS
        }
        assert len(routes) > 20, routes
        extract_line = next(line for line in published.splitlines() if "/extract/<kind>" in line)
        missing = []
        for path in sorted(routes):
            if path.startswith("/extract/"):
                said = (
                    re.search(r"\b" + re.escape(path.split("/")[-1]) + r"\b", extract_line)
                    is not None
                )
            elif path.endswith("_url"):
                said = path in published or path.split("/")[-1] in published
            else:
                said = path in published
            if not said:
                missing.append(path)
        assert not missing, f"routes the library serves that the docstring does not list: {missing}"

    def test_its_routes_are_at_the_root_with_an_hour_for_a_job(self):
        host = json.loads((EXTRACTION_APP / "host.json").read_text(encoding="utf-8"))
        assert host["extensions"]["http"]["routePrefix"] == "", (
            "or every route answers under /api and a caller finds nothing"
        )
        assert host["functionTimeout"] == "01:00:00"
        queues = host["extensions"]["queues"]
        assert queues["messageEncoding"] == "none", (
            "the library writes the job message as plain JSON"
        )
        assert queues["batchSize"] == 1

    def test_what_it_installs_is_what_it_needs(self):
        needs = (EXTRACTION_APP / "requirements.txt").read_text(encoding="utf-8")
        wheel = next(line for line in needs.splitlines() if line.startswith("./vectrixdb-"))
        extras = wheel.split("[", 1)[1].rstrip("]").split(",")
        assert set(extras) == {"api", "documents", "ocr-azure", "youtube", "jobs-azure", "ffmpeg"}
        for package in ("azure-functions", "pillow"):
            assert package in needs, package
        assert "\nimageio-ffmpeg" not in needs, "ffmpeg comes through the library's own extra"

    def test_the_wheel_it_installs_is_not_kept_with_the_source(self):
        assert "*.whl" in (EXTRACTION_APP / ".gitignore").read_text(encoding="utf-8")

    def test_it_stands_alone(self):
        """A utility API for any work: it needs nothing of the ingest app, and sends none of its settings."""
        source = (AZURE / EXTRACT_SCRIPT).read_text(encoding="utf-8")
        for theirs in (
            "search_endpoint",
            "eventgrid",
            "INGEST_",
            "AZURE_SEARCH",
            "02_create_search",
        ):
            assert theirs not in source, theirs

    def test_its_settings(self, folder):
        script = load(EXTRACT_SCRIPT)
        config = folder.settings()
        config.update(
            VX_EXTRACT_PREFIX="/acme",
            VX_EXTRACT_GATEWAY_PATHS="extract/pdf=/files/pdf",
            VX_EXTRACT_URL_HOSTS="www.example.com",
        )
        kept = {
            "docintel_endpoint": "https://d.example.com",
            "speech_endpoint": "https://s.example.com",
        }
        every = script.extraction_env(config, kept, {"AZURE_DOCINTEL_KEY": "k1"}, "the-key")
        assert (
            every["VECTRIXDB_API_KEY"] == "the-key" and every["VECTRIXDB_EXTRACT_JOBS"] == "azure"
        )
        assert every["VECTRIXDB_EXTRACT_PREFIX"] == "/acme"
        assert every["VECTRIXDB_EXTRACT_GATEWAY_PATHS"] == "extract/pdf=/files/pdf"
        assert every["VECTRIXDB_EXTRACT_URL_HOSTS"] == "www.example.com"
        assert (
            every["AZURE_DOCINTEL_ENDPOINT"] == "https://d.example.com"
            and every["AZURE_DOCINTEL_KEY"] == "k1"
        )
        assert "AZURE_VISION_ENDPOINT" not in every, (
            "a service 03 did not make is left out, and its routes say so"
        )
        assert (
            every["VECTRIXDB_MASKING_ENGINE"] == "auto"
            and every["VECTRIXDB_MASKING_LANGUAGES"] == "en,fr"
        ), "masking is always on, by the patterns at least"
        assert "AZURE_LANGUAGE_ENDPOINT" not in every, "03 made no Language here"

    def test_language_reaches_the_extraction_app_and_nothing_else(self, folder):
        script = load(EXTRACT_SCRIPT)
        kept = {"language_endpoint": "https://l.cognitiveservices.azure.com"}
        every = script.extraction_env(
            {**folder.settings(), "VX_MASKING_LANGUAGES": "fr, en"},
            kept,
            {"AZURE_LANGUAGE_KEY": "lk"},
            "the-key",
        )
        assert (
            every["AZURE_LANGUAGE_ENDPOINT"] == kept["language_endpoint"]
            and every["AZURE_LANGUAGE_KEY"] == "lk"
        )
        assert every["VECTRIXDB_MASKING_LANGUAGES"] == "fr, en"
        assert any(
            name == "VX_LANGUAGE_NAME" and setting == "AZURE_LANGUAGE"
            for name, _kept, setting, _what in script.SERVICES
        ), "its key is read from Azure like the other services'"
        assert (
            folder.settings()["VX_LANGUAGE_NAME"] == "vxtest1234-language"
            and folder.settings()["VX_LANGUAGE"] == "yes"
        )
        source = (AZURE / "03_create_ai_services.py").read_text(encoding="utf-8")
        assert '"TextAnalytics", "F0"' in flat(source) and 'kept["language_endpoint"]' in source, (
            "03 makes it on the free tier and remembers where it is"
        )

    def test_a_path_left_empty_is_not_sent(self, folder):
        every = load(EXTRACT_SCRIPT).extraction_env(folder.settings(), {}, {}, "the-key")
        assert not any(
            name.startswith(("VECTRIXDB_EXTRACT_PREFIX", "VECTRIXDB_EXTRACT_GATEWAY"))
            for name in every
        )

    def test_a_dry_run_makes_the_app_and_never_shows_its_key(self, folder, monkeypatch, capsys):
        script = load(EXTRACT_SCRIPT)
        monkeypatch.setattr(script.secrets, "token_urlsafe", lambda n=32: "THE-GENERATED-KEY")
        monkeypatch.setattr(sys, "argv", [EXTRACT_SCRIPT, "--dry-run"])
        assert script.main() == 0
        printed = capsys.readouterr().out
        assert (
            "$ az functionapp create --name vxtest1234-extract --resource-group vxtest1234-rg"
            in printed
        )
        assert "VECTRIXDB_API_KEY=..." in printed and "THE-GENERATED-KEY" not in printed
        assert "func azure functionapp publish vxtest1234-extract" in printed

    def test_a_second_run_keeps_the_key_its_callers_have(self, folder, monkeypatch):
        script = load(EXTRACT_SCRIPT)
        answer = [
            {"name": "VECTRIXDB_API_KEY", "value": "kept"},
            {"name": "VECTRIXDB_EXTRACT_JOBS", "value": "azure"},
        ]
        monkeypatch.setattr(folder.Az, "__call__", lambda self, *a, **k: answer)
        assert (
            script.current_settings(folder.Az(pretend=True), "app", "group")["VECTRIXDB_API_KEY"]
            == "kept"
        )

    def test_paths_the_app_would_refuse_stop_it_here(self, folder):
        """Or the app loads nothing, and a function app that loads nothing answers 404 to everything."""
        script = load(EXTRACT_SCRIPT)
        config = folder.settings()
        config["VX_EXTRACT_GATEWAY_PATHS"] = "extract/pfd=/files/pdf"
        with pytest.raises(SystemExit):
            script.paths_hold(config)
        config["VX_EXTRACT_GATEWAY_PATHS"] = "extract/pdf=/files/pdf"
        script.paths_hold(config)

    def test_it_checks_its_wheel_before_it_publishes(self):
        source = (AZURE / EXTRACT_SCRIPT).read_text(encoding="utf-8")
        assert source.index("wheel_ready(EXTRACTION_APP, NEEDS") < source.index('step("The code")')


class TestTheWheelBothAppsInstall:
    """Looked inside before it is sent, because a wheel that is too old installs without complaint."""

    NAME = "vectrixdb-9.9.9-py3-none-any.whl"

    def app(self, folder, tmp_path, monkeypatch, asks="api,ffmpeg"):
        app = tmp_path / "extraction_app"
        app.mkdir(exist_ok=True)
        (app / "requirements.txt").write_text(f"./{self.NAME}[{asks}]\n", encoding="utf-8")
        for name, value in (
            ("EXTRACTION_APP", app),
            ("REPO", tmp_path),
            ("MAIN_FUNCTION_APP", tmp_path / "nowhere"),
        ):
            monkeypatch.setattr(folder, name, value)
        return app

    def wheel(
        self,
        where,
        holds="def read_gateway_paths(value): ...",
        offers=("api", "ffmpeg"),
        requires=(),
    ):
        with zipfile.ZipFile(where / self.NAME, "w") as made:
            made.writestr("vectrixdb/api/extraction.py", holds)
            made.writestr(
                "vectrixdb-9.9.9.dist-info/METADATA",
                "Metadata-Version: 2.4\nName: vectrixdb\n"
                + "".join(f"Requires-Dist: {requirement}\n" for requirement in requires)
                + "".join(f"Provides-Extra: {extra}\n" for extra in offers),
            )

    NEEDS = {"vectrixdb/api/extraction.py": "def read_gateway_paths"}

    def test_one_with_what_the_app_imports_and_the_extras_it_asks_for_is_sent(
        self, folder, tmp_path, monkeypatch
    ):
        app = self.app(folder, tmp_path, monkeypatch)
        self.wheel(app)
        folder.wheel_ready(app, self.NEEDS)

    def test_one_built_before_what_the_app_imports_is_refused(self, folder, tmp_path, monkeypatch):
        """It would install, and the app would load nothing and answer 404 to everything."""
        app = self.app(folder, tmp_path, monkeypatch)
        self.wheel(app, holds="")
        with pytest.raises(SystemExit):
            folder.wheel_ready(app, self.NEEDS)

    def test_one_without_an_extra_the_app_asks_for_is_refused(
        self, folder, tmp_path, monkeypatch, capsys
    ):
        """pip would install it without ffmpeg and say so only in a warning."""
        app = self.app(folder, tmp_path, monkeypatch)
        self.wheel(app, offers=("api",))
        with pytest.raises(SystemExit):
            folder.wheel_ready(app, self.NEEDS)
        assert "offers no ffmpeg extra" in capsys.readouterr().err

    def test_no_wheel_at_all_says_how_to_build_one(self, folder, tmp_path, monkeypatch, capsys):
        app = self.app(folder, tmp_path, monkeypatch)
        with pytest.raises(SystemExit):
            folder.wheel_ready(app, self.NEEDS)
        assert "python -m build --no-isolation --wheel" in capsys.readouterr().err

    def test_a_newer_build_is_copied_in(self, folder, tmp_path, monkeypatch):
        app = self.app(folder, tmp_path, monkeypatch)
        (tmp_path / "dist").mkdir()
        self.wheel(tmp_path / "dist")
        folder.wheel_ready(app, self.NEEDS)
        assert (app / self.NAME).exists()

    def test_a_dry_run_copies_nothing_and_checks_nothing(
        self, folder, tmp_path, monkeypatch, capsys
    ):
        app = self.app(folder, tmp_path, monkeypatch)
        (tmp_path / "dist").mkdir()
        self.wheel(tmp_path / "dist")
        folder.wheel_ready(app, self.NEEDS, dry_run=True)
        assert not (app / self.NAME).exists() and "$ copy" in capsys.readouterr().out

    def test_the_main_app_checks_its_wheel_too(self):
        source = (AZURE / "06_create_main_function_app.py").read_text(encoding="utf-8")
        source = flat(source)
        assert source.index("wheel_ready(MAIN_FUNCTION_APP, NEEDS") < source.index(
            'step("The code")'
        )
        for needed in ("def extraction_routes", "def batched", "def from_environment"):
            assert needed in source, needed

    SECOND = {"openai": "the second vector is embedded with"}
    AZURE_EXTRA = (
        'openai>=1.0.0; extra == "azure"',
        'azure-search-documents>=11.4.0; extra == "azure"',
    )

    def test_a_package_brought_by_an_extra_the_app_asks_for_is_enough(
        self, folder, tmp_path, monkeypatch, capsys
    ):
        app = self.app(folder, tmp_path, monkeypatch, asks="api,azure")
        self.wheel(app, offers=("api", "azure"), requires=self.AZURE_EXTRA)
        folder.wheel_ready(app, self.NEEDS, installs=self.SECOND)
        assert "openai, which the second vector is embedded with" in capsys.readouterr().out

    def test_an_extra_that_asks_for_another_brings_that_ones_packages(
        self, folder, tmp_path, monkeypatch
    ):
        """video brings vectrixdb[ffmpeg], and ffmpeg brings imageio-ffmpeg, spelt either way."""
        app = self.app(folder, tmp_path, monkeypatch, asks="video")
        self.wheel(
            app,
            offers=("video", "ffmpeg"),
            requires=(
                'imageio-ffmpeg>=0.4.9; extra == "ffmpeg"',
                'vectrixdb[ffmpeg]; extra == "video"',
            ),
        )
        folder.wheel_ready(
            app, self.NEEDS, installs={"imageio_ffmpeg": "takes the sound out of a video"}
        )

    def test_a_package_on_a_line_of_its_own_is_enough(self, folder, tmp_path, monkeypatch):
        app = self.app(folder, tmp_path, monkeypatch, asks="api")
        (app / "requirements.txt").write_text(
            f"# the library\n./{self.NAME}[api]\nOpenAI>=1.0  # the second vector\n",
            encoding="utf-8",
        )
        self.wheel(app, offers=("api",))
        folder.wheel_ready(app, self.NEEDS, installs=self.SECOND)

    def test_a_package_neither_brings_is_refused_before_it_is_published(
        self, folder, tmp_path, monkeypatch, capsys
    ):
        """The wheel offers it with an extra the app does not ask for, which installs nothing."""
        app = self.app(folder, tmp_path, monkeypatch, asks="api")
        self.wheel(app, offers=("api", "azure"), requires=self.AZURE_EXTRA)
        with pytest.raises(SystemExit):
            folder.wheel_ready(app, self.NEEDS, installs=self.SECOND)
        said = capsys.readouterr().err
        assert "installs no openai, which the second vector is embedded with" in said
        assert "add it on a line of its own: openai" in said

    def test_the_main_app_asks_for_openai_when_it_is_given_the_second_vector(self, folder):
        source = (AZURE / "06_create_main_function_app.py").read_text(encoding="utf-8")
        assert '"AZURE_OPENAI_EMBED_DEPLOYMENT" in env_of(config, kept, {})' in source
        assert "installs=SECOND_VECTOR if second else None" in source
        lines = (
            (AZURE / "main_function_app" / "requirements.txt")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert "openai" in [line.strip() for line in lines]


class TestTheMainAppReadsThroughTheExtractionApp:
    """Every file goes to the extraction app, at the route its suffix names, and nothing is read here."""

    EXTRACTION = {
        "VECTRIXDB_EXTRACTOR_URL": "https://vxtest1234-extract.azurewebsites.net",
        "VECTRIXDB_EXTRACTOR_KEY": "the-extraction-key",
        "VECTRIXDB_EXTRACTOR_KEY_HEADER": "api-key",
        "VECTRIXDB_EXTRACTOR_BODY": "raw",
        "VECTRIXDB_EXTRACTOR_TIMEOUT": "230",
    }

    def readers(self):
        sys.path.insert(0, str(MAIN_FUNCTION_APP))
        import readers

        sys.path.remove(str(MAIN_FUNCTION_APP))
        return readers

    def test_every_suffix_the_extraction_app_reads_goes_to_it(self):
        from vectrixdb.api.extraction import extraction_routes

        readers = self.readers().extractors_from_environment(
            {
                **self.EXTRACTION,
                "AZURE_SPEECH_ENDPOINT": "https://s.example.com",
                "AZURE_SPEECH_KEY": "k",
            }
        )
        assert set(readers) == set(extraction_routes()), (
            "Speech is the extraction app's now, not a reader here"
        )
        assert type(readers[".pdf"]).__name__ == "Batched", "a long file goes in pieces"
        service = readers[".pdf"].reader
        assert (
            service.route_for("q3.pdf") == "/extract/pdf"
            and service.route_for("call.flac") == "/transcribe/audio"
        )
        assert (
            service.headers == {"api-key": "the-extraction-key"}
            and service.body == "raw"
            and service.timeout == 230
        )

    def test_the_paths_are_the_ones_the_extraction_app_answers_at(self):
        """Worked out from the same prefix and gateway list the extraction app is given, so the two agree."""
        service = self.readers().extraction_app_from_environment(
            {
                **self.EXTRACTION,
                "EXTRACTION_PREFIX": "/acme",
                "EXTRACTION_GATEWAY_PATHS": "extract/pdf=/files/pdf",
            }
        )
        assert service.route_for("q3.pdf") == "/files/pdf/acme/extract/pdf"
        assert service.route_for("q3.docx") == "/acme/extract/docx"

    def test_a_file_nothing_reads_is_left_alone_and_one_it_reads_is_not(self):
        unread = self.readers().unread
        blob = "https://vxtest1234store.blob.core.windows.net/ingestion/raw/misc/"
        assert unread(blob + "archive.zip", self.EXTRACTION) == ".zip"
        assert unread(blob + "README", self.EXTRACTION) == "(no suffix)"
        assert unread(blob + "q3.pdf", self.EXTRACTION) is None
        assert unread(blob + "Q3%20Report.PDF", self.EXTRACTION) is None, (
            "an address is decoded, and a suffix read in any case"
        )

    def test_without_an_extraction_app_nothing_is_called_unread(self):
        """The library's own readers take whatever they know then, and say so themselves."""
        assert (
            self.readers().unread(
                "https://x.blob.core.windows.net/ingestion/raw/misc/archive.zip", {}
            )
            is None
        )

    def test_an_unread_file_is_skipped_before_the_worker_and_not_retried(self):
        source = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        body = source[source.index("def handle_events(") :]
        assert body.index("unread(event.uri)") < body.index("by_collection.setdefault")
        assert "handle_events(message)" in source[source.index("def ingest_one(") :], (
            "the trigger hands its message on"
        )

    def test_the_wiring_route_shows_the_table(self):
        source = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        assert '"routes": dict(sorted(service.routes.items()))' in source
        assert (
            '"extraction_app": given("VECTRIXDB_EXTRACTOR_URL", "VECTRIXDB_EXTRACTOR_KEY")'
            in source
        )

    def test_its_settings_name_the_extraction_app_and_none_of_the_reading_services(self, folder):
        config = folder.settings()
        config.update(VX_EXTRACT_PREFIX="/acme", VX_EXTRACT_GATEWAY_PATHS="extract/pdf=/files/pdf")
        kept = {
            "extract_host": "https://vxtest1234-extract.azurewebsites.net",
            "docintel_endpoint": "https://d.example.com",
            "speech_endpoint": "https://s.example.com",
            "vision_endpoint": "https://v.example.com",
        }
        every = folder.env_of(config, kept, {"VECTRIXDB_EXTRACTOR_KEY": "k"})
        assert (
            every["VECTRIXDB_EXTRACTOR_URL"] == kept["extract_host"]
            and every["VECTRIXDB_EXTRACTOR_KEY"] == "k"
        )
        assert (
            every["VECTRIXDB_EXTRACTOR_KEY_HEADER"] == "api-key"
            and every["VECTRIXDB_EXTRACTOR_TIMEOUT"] == "230"
        )
        assert (
            every["EXTRACTION_PREFIX"] == "/acme"
            and every["EXTRACTION_GATEWAY_PATHS"] == "extract/pdf=/files/pdf"
        )
        assert every["VECTRIXDB_EXTRACTOR_MASK"] == "1", "every file is masked as it is read"
        assert not [
            name
            for name in every
            if name.startswith(
                (
                    "AZURE_DOCINTEL",
                    "AZURE_SPEECH",
                    "AZURE_VISION",
                    "AZURE_LANGUAGE",
                    "VECTRIXDB_MASKING",
                )
            )
        ], "the extraction app masks; this one only asks"

    def test_it_is_made_after_the_extraction_app_and_takes_its_key_from_it(self):
        source = (AZURE / "06_create_main_function_app.py").read_text(encoding="utf-8")
        assert 'if not kept.get("extract_host") and not args.dry_run' in source
        assert (
            '"--name", config["VX_EXTRACT_APP"]' in flat(source)
            and 'secrets["VECTRIXDB_EXTRACTOR_KEY"]' in source
        )
        for gone in ('"AZURE_DOCINTEL_KEY"', '"AZURE_SPEECH_KEY"', '"AZURE_VISION_KEY"'):
            assert gone not in source, gone

    def test_an_upload_goes_where_the_same_file_dropped_in_raw_goes(self, monkeypatch):
        """The documents route reads VECTRIXDB_EXTRACTOR_ROUTES, which the app fills from its own table."""
        from vectrixdb.api.documents import configured_extractors

        source = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        api = source[source.index("def _api(") :]
        assert api.index('os.environ.setdefault("VECTRIXDB_EXTRACTOR_ROUTES"') < api.index(
            "create_app("
        )
        queue = self.readers().extraction_app_from_environment(self.EXTRACTION)
        for name, value in {
            **self.EXTRACTION,
            "VECTRIXDB_EXTRACTOR_ROUTES": json.dumps(queue.routes),
        }.items():
            monkeypatch.setenv(name, value)
        assert configured_extractors().routes == queue.routes

    def test_a_long_file_goes_in_pieces_of_the_sizes_it_is_given(self):
        readers = self.readers().extractors_from_environment(
            {**self.EXTRACTION, "EXTRACTION_BATCH_MINUTES": "5", "EXTRACTION_BATCH_PAGES": "12"}
        )
        pieces = readers[".mp3"]
        assert pieces.minutes == 5.0 and pieces.pages == 12 and pieces is readers[".pdf"]
        default = self.readers().extractors_from_environment(self.EXTRACTION)[".pdf"]
        assert default.minutes == 10.0 and default.pages == 20

    def test_the_sizes_come_from_settings_env(self, folder):
        config = folder.settings()
        config.update(EXTRACTION_BATCH_MINUTES="5", EXTRACTION_BATCH_PAGES="12")
        every = folder.env_of(
            config, {"extract_host": "https://vxtest1234-extract.azurewebsites.net"}, {}
        )
        assert every["EXTRACTION_BATCH_MINUTES"] == "5" and every["EXTRACTION_BATCH_PAGES"] == "12"


RECORDS = AZURE / ".local" / "cosmosdb" / "data_db" / "collection_records"


class TestEachCollectionsRecord:
    """Where a collection's policy is written, and how it reaches every server.

    One file a collection in the mirror, pushed by 04 and 07 into Cosmos
    ``access/collection_records``, where the function app and the hosted API
    both read it. The policy follows its file, and the dashboard's Policy tab
    edits the same record.
    """

    def seeds(self):
        from vectrixdb.collection_records import CollectionRecord

        return {
            path.stem: CollectionRecord.from_json(path) for path in sorted(RECORDS.glob("*.json"))
        }

    def test_every_collection_has_a_record_that_names_who_may_search_it(self):
        seeds = self.seeds()
        assert sorted(seeds) == ["financial", "media", "misc"]
        for name, record in seeds.items():
            assert record.path == f"raw/{name}/"
            assert record.policy is not None, "each seed names who may search it"
            assert set(record.to_json()) == {"name", "path", "policy"}, (
                "who may search it is the whole rule"
            )

    def test_the_records_are_committed_and_nothing_else_under_cosmosdb_is(self):
        ignored = (AZURE / ".local" / ".gitignore").read_text(encoding="utf-8")
        rules = [
            line.strip()
            for line in ignored.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert "cosmosdb/*" in rules and "cosmosdb/data_db/*" in rules
        assert (
            rules.index("!cosmosdb/data_db/")
            < rules.index("cosmosdb/data_db/*")
            < rules.index("!cosmosdb/data_db/collection_records/")
        )

    def test_a_record_written_from_settings_names_nobody_until_the_file_does(
        self, folder, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(folder, "LOCAL", tmp_path / ".local")
        monkeypatch.setattr(
            folder,
            "RECORD_FILES",
            tmp_path / ".local" / "cosmosdb" / "data_db" / "collection_records",
        )
        written = folder.record_files(folder.settings())
        assert [path.name for path in written] == ["financial.json", "media.json", "misc.json"]
        for path in written:
            record = json.loads(path.read_text(encoding="utf-8"))
            assert record == {"name": path.stem, "path": f"raw/{path.stem}/", "policy": None}

    def test_the_seeds_name_a_person_and_a_domain_and_media_names_its_people_until_sso(self):
        seeds = self.seeds()
        owner = seeds["financial"].policy_object().describe()
        assert seeds["financial"].policy_object().method == "store" and owner.count("@") == 1, (
            "one person, by their address"
        )
        assert seeds["media"].policy_object().method == "store", (
            "a group needs single sign-on, which is not set up"
        )
        assert seeds["media"].policy_object().describe() == owner, (
            "the people who were on the group's list"
        )
        assert seeds["misc"].policy_object().describe() == f"everyone at {owner.split('@')[1]}"
        assert not (RECORDS.parent / "members").exists(), (
            "the store is the list in the record; nothing is kept per person"
        )

    def test_the_group_record_the_readme_gives_media_is_one_the_library_takes(self):
        from vectrixdb.collection_access import AccessPolicy

        readme = (AZURE / "README.md").read_text(encoding="utf-8")
        shown = readme[readme.index("Then its record") :]
        given = json.loads(
            shown[shown.index("```json") + 7 : shown.index("```", shown.index("```json") + 7)]
        )
        policy = AccessPolicy.from_dict(given)
        assert policy.method == "token" and given["people"] == [{"email": "you@company.com"}], (
            "a group, narrowed to a list"
        )

    @pytest.fixture
    def pushing(self, folder, tmp_path, monkeypatch):
        """push_records against a records store on this machine standing in for Cosmos, and az standing in for az."""
        import types

        from vectrixdb import collection_records
        from vectrixdb.signin.records import SqlRecords

        monkeypatch.setattr(folder, "LOCAL", tmp_path / ".local")
        monkeypatch.setattr(
            folder,
            "RECORD_FILES",
            tmp_path / ".local" / "cosmosdb" / "data_db" / "collection_records",
        )
        if importlib.util.find_spec("azure") is None:  # an install without the Azure packages
            monkeypatch.setitem(sys.modules, "azure", types.ModuleType("azure"))
        monkeypatch.setitem(sys.modules, "azure.cosmos", types.ModuleType("azure.cosmos"))
        opened = []

        def opening(cls, where, key=None, fresh_for=30.0):
            opened.append((where, key))
            return cls(SqlRecords.sqlite(tmp_path / "cosmos.db"), fresh_for=0)

        monkeypatch.setattr(collection_records.CollectionRecords, "open", classmethod(opening))

        class Az:
            pretend = False

            def __init__(self):
                self.calls = []

            def __call__(self, *args, **_):
                self.calls.append(args)
                return (
                    {"primaryMasterKey": "the-account-key"}
                    if args[:3] == ("cosmosdb", "keys", "list")
                    else None
                )

        def cosmos():
            return collection_records.CollectionRecords(
                SqlRecords.sqlite(tmp_path / "cosmos.db"), fresh_for=0
            )

        return types.SimpleNamespace(
            common=folder,
            config=folder.settings(),
            Az=Az,
            opened=opened,
            cosmos=cosmos,
            root=tmp_path,
        )

    def test_the_first_push_writes_every_record_and_the_second_writes_none(self, pushing, capsys):
        az = pushing.Az()
        assert (
            pushing.common.push_records(az, pushing.config, by="04_push_local_to_blob_cosmosdb.py")
            == 3
        )
        assert pushing.opened == [
            (
                "cosmos://vxtest1234-cosmos.documents.azure.com/data_db/collection_records",
                "the-account-key",
            )
        ]
        kept = pushing.cosmos().get("financial")
        assert kept.to_json() == {"name": "financial", "path": "raw/financial/", "policy": None}, (
            "written from settings.env, so it names nobody yet"
        )
        assert kept.changed_by == "04_push_local_to_blob_cosmosdb.py"
        assert pushing.common.push_records(az, pushing.config, by="07") == 0
        assert "the same already there" in capsys.readouterr().out
        assert "the-account-key" not in " ".join(" ".join(call) for call in az.calls), (
            "the key is read, never sent as an argument"
        )

    def test_a_policy_changed_in_its_file_is_written(self, pushing):
        az = pushing.Az()
        pushing.common.push_records(az, pushing.config, by="04")
        media = pushing.common.RECORD_FILES / "media.json"
        record = json.loads(media.read_text(encoding="utf-8"))
        record["policy"] = {"method": "store", "allow": [{"domain": "company.com"}]}
        media.write_text(json.dumps(record), encoding="utf-8")
        assert pushing.common.push_records(az, pushing.config, by="07") == 1
        kept = pushing.cosmos().get("media")
        assert (
            kept.policy_object().describe() == "everyone at company.com" and kept.changed_by == "07"
        )
        assert pushing.common.push_records(az, pushing.config, by="07") == 0, (
            "and the second push has nothing to write"
        )

    def test_what_is_not_a_collection_here_is_not_sent_and_what_is_only_in_cosmos_is_left(
        self, pushing, capsys
    ):
        az = pushing.Az()
        pushing.common.RECORD_FILES.mkdir(parents=True)
        (pushing.common.RECORD_FILES / "legal.json").write_text(
            json.dumps({"id": "legal"}), encoding="utf-8"
        )
        pushing.cosmos().set_policy("retired", None)
        assert pushing.common.push_records(az, pushing.config, by="04") == 3
        said = capsys.readouterr().out
        assert (
            "legal.json is the record for legal, which is not one of: financial, media, misc"
            in said
        )
        assert "in Cosmos and not in the mirror, so left as they are: retired" in said
        assert pushing.cosmos().get("legal") is None and pushing.cosmos().get("retired") is not None

    def test_a_file_that_cannot_be_a_record_stops_the_push_and_names_the_file(self, pushing):
        pushing.common.RECORD_FILES.mkdir(parents=True)
        (pushing.common.RECORD_FILES / "financial.json").write_text(
            json.dumps({"id": "financial", "path": "raw/media/"}), encoding="utf-8"
        )
        with pytest.raises(SystemExit):
            pushing.common.push_records(pushing.Az(), pushing.config, by="04")
        assert pushing.opened == [], "nothing was written"

    def test_without_cosmos_nothing_is_opened(self, pushing):
        az = pushing.Az()
        assert pushing.common.push_records(az, {**pushing.config, "VX_COSMOS": "no"}, by="04") == 0
        assert pushing.opened == [] and az.calls == []
        assert sorted(path.name for path in pushing.common.RECORD_FILES.glob("*.json")) == [
            "financial.json",
            "media.json",
            "misc.json",
        ]

    @staticmethod
    def dry(pushing, container=True):
        """az for a dry run: every look answered, as a records container that is there, or is not."""

        class Dry(pushing.Az):
            pretend = True

            def __init__(self):
                super().__init__()
                self.looked = []

            def look(self, *args):
                self.looked.append(args)
                if args[:4] == ("cosmosdb", "sql", "container", "show"):
                    return {"id": "collection_records"} if container else None
                if args[:3] == ("cosmosdb", "keys", "list"):
                    return {"primaryMasterKey": "the-account-key"}
                return None

        return Dry()

    def test_a_dry_run_reads_the_records_and_writes_none(self, pushing, capsys):
        assert pushing.common.push_records(pushing.Az(), pushing.config, by="04") == 3
        media = pushing.common.RECORD_FILES / "media.json"
        record = json.loads(media.read_text(encoding="utf-8"))
        record["policy"] = {"method": "store", "allow": [{"email": "kofi@partner.example"}]}
        media.write_text(json.dumps(record), encoding="utf-8")
        capsys.readouterr()
        dry = self.dry(pushing)
        assert pushing.common.push_records(dry, pushing.config, by="07") == 1, (
            "the one whose policy changed in its file"
        )
        said = capsys.readouterr().out
        assert (
            "would write collection.media, policy now" in said
            and said.count("the same already there") == 2
        )
        assert dry.calls == [], "nothing but a look"
        assert pushing.cosmos().get("media").policy != record["policy"], "and nothing was written"
        assert all(args[0] == "cosmosdb" and args[1] in ("sql", "keys") for args in dry.looked)

    def test_a_dry_run_with_no_records_container_yet_makes_none(self, pushing, capsys):
        dry = self.dry(pushing, container=False)
        assert pushing.common.push_records(dry, pushing.config, by="04") == 3
        said = capsys.readouterr().out
        assert (
            "could not look at the records" in said and said.count("would write collection.") == 3
        )
        assert pushing.opened == [], (
            "the store is never opened, since opening it makes the container"
        )
        assert [args[:4] for args in dry.looked] == [("cosmosdb", "sql", "container", "show")], (
            "and no key is read"
        )

    @pytest.mark.parametrize(
        "script",
        ["04_push_local_to_blob_cosmosdb.py", "07_push_new_files_from local_to_blob_cosmosdb.py"],
    )
    def test_both_pushes_read_as_the_function_app_does_and_send_the_records_first(self, script):
        source = (AZURE / script).read_text(encoding="utf-8")
        headings = re.findall(r"^# (SETTINGS|STEP [A-Z]+|MAIN SCRIPT)", source, re.MULTILINE)
        assert headings == ["SETTINGS", "STEP ONE", "STEP TWO", "STEP THREE", "MAIN SCRIPT"], (
            headings
        )
        assert "# STEP TWO: RECORDS" in source and "# STEP THREE: FILES" in source
        main = source.split("\ndef main(")[1]
        assert main.index("records(az, config, args.again)") < main.index(
            "files(az, config, args.again"
        ), "a collection's rules before its first file"
        assert "push_records(az, config, by=BY, again=again)" in source

    def test_03_makes_one_database_inside_the_free_tier(self, folder, capsys):
        script = load("03_create_ai_services.py")
        script.cosmos(
            folder.Az(pretend=True), "vxtest1234-cosmos", "vxtest1234-rg", "canadacentral", True
        )
        printed = capsys.readouterr().out.splitlines()
        assert not any("--name members" in line for line in printed), (
            "no per-person store: the list is in the record"
        )
        for container in ("collection_records", "signin_records"):
            (line,) = [line for line in printed if f"--name {container}" in line]
            assert (
                "--database-name data_db" in line
                and "--partition-key-path /kind" in line
                and "--ttl -1" in line
            ), line
        for container, key in (("parent_sections", "/doc"), ("chunk_records", "/collection")):
            (line,) = [line for line in printed if f"--name {container}" in line]
            assert "--database-name data_db" in line and f"--partition-key-path {key}" in line, line
        databases = {
            re.search(r"--name (\w+)", line).group(1): line
            for line in printed
            if "sql database create" in line
        }
        assert list(databases) == ["data_db"] and "--throughput 1000" in databases["data_db"]
        assert script.SHARED == 1000, "the free tier's 1000 RU/s, and not one more"

    def test_03_says_an_ingestion_database_from_before_is_read_by_nothing(self, folder, capsys):
        """Its throughput is billed past the free tier once data_db has the whole 1000."""
        script = load("03_create_ai_services.py")

        class Azure:
            pretend = False

            def __init__(self, found):
                self.found = found

            def __call__(self, *args, **_):
                return self.found if "show" in args and "ingestion" in args else None

        script._says_what_it_costs(Azure({"id": "ingestion"}), "vxtest1234-cosmos", "vxtest1234-rg")
        said = capsys.readouterr().out
        assert "no app reads it now" in said and "delete it in the portal" in said
        script._says_what_it_costs(Azure(None), "vxtest1234-cosmos", "vxtest1234-rg")
        assert capsys.readouterr().out == "", "an account made since says nothing"

    def test_the_app_is_told_where_the_records_and_sign_in_are(self, folder):
        kept = {
            "collection_store": "cosmos://vxtest1234-cosmos.documents.azure.com/data_db/collection_records",
            "signin_store": "cosmos://vxtest1234-cosmos.documents.azure.com/data_db/signin_records",
        }
        every = folder.env_of(folder.settings(), kept, {})
        assert every["VECTRIXDB_COLLECTION_STORE"] == kept["collection_store"]
        assert every["VECTRIXDB_SIGNIN_STORE"] == kept["signin_store"]
        bare = folder.env_of(folder.settings(), {}, {})
        assert not any(
            name in bare for name in ("VECTRIXDB_COLLECTION_STORE", "VECTRIXDB_SIGNIN_STORE")
        )

    def test_the_function_app_reads_each_collections_rules_from_its_record(self):
        source = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        assert source.count("collection_store=_collection_store()") == 3, (
            "every collection it opens, and the hosted API"
        )
        assert "name == policied()" not in source, (
            "whether a collection is policied is its record's to say"
        )
        assert "metadata_for(event.uri)" in source and "policy_for" not in source, (
            "a chunk carries its collection; the record says who"
        )


class TestTheAuditContainer:
    """What was decided and who looked, appended where nothing can change it, and still deletable at the end of a day."""

    def test_01_makes_it_with_a_retention_policy_that_allows_appends_and_leaves_it_unlocked(
        self, folder, capsys
    ):
        script = load("01_create_resources.py")
        source = (AZURE / "01_create_resources.py").read_text(encoding="utf-8")
        assert "(AUDIT, " in source and "remember(" in source and "audit_container=AUDIT" in source
        creates = [
            call
            for call in TestEverythingIsInOneGroup.az_calls(AZURE / "01_create_resources.py")
            if "immutability-policy" in call
        ]
        (made,) = [call for call in creates if "create" in call]
        assert "--allow-protected-append-writes" in made and "--period" in made
        assert not [call for call in creates if "lock" in call], (
            "a locked policy would keep 99 from deleting anything"
        )
        assert folder.settings()["VX_AUDIT_DAYS"] == "7"

        class Answers:
            pretend = False

            def __init__(self, answer):
                self.answer = answer

            def __call__(self, *args, **_):
                return self.answer

        assert (
            script.retention_of(
                Answers({"immutabilityPeriodSinceCreationInDays": 0, "etag": "e"}), "a", "g"
            )
            == {}
        )
        held = script.retention_of(
            Answers(
                {
                    "properties": {"immutabilityPeriodSinceCreationInDays": 7, "state": "Unlocked"},
                    "etag": "e",
                }
            ),
            "a",
            "g",
        )
        assert held["immutabilityPeriodSinceCreationInDays"] == 7 and held["etag"] == "e"

    def test_the_app_writes_decisions_and_access_there(self, folder):
        kept = {
            "blob_account": "https://vxtest1234store.blob.core.windows.net",
            "audit_container": "audit",
        }
        every = folder.env_of(folder.settings(), kept, {})
        assert (
            every["VECTRIXDB_AUDIT_STORE"]
            == "https://vxtest1234store.blob.core.windows.net/audit/decisions"
        )
        assert (
            every["VECTRIXDB_ACCESS_LOG"]
            == "https://vxtest1234store.blob.core.windows.net/audit/access"
        )
        before = folder.env_of(folder.settings(), {"blob_account": kept["blob_account"]}, {})
        assert "VECTRIXDB_AUDIT_STORE" not in before, (
            "an account made before the container is not pointed at nothing"
        )

    def test_the_query_key_stays_the_same_once_the_app_has_one(self, folder):
        script = load("06_create_main_function_app.py")

        class Settings:
            pretend = False

            def __init__(self, held):
                self.held = held

            def __call__(self, *args, **_):
                return self.held

        config = folder.settings()
        assert (
            script.query_key(
                Settings([{"name": "VECTRIXDB_AUDIT_QUERY_KEY", "value": "kept"}]), config
            )
            == "kept"
        )
        made = script.query_key(Settings([]), config)
        assert re.fullmatch(r"[0-9a-f]{64}", made) and made != script.query_key(
            Settings([]), config
        )

    def test_99_takes_an_unlocked_policy_off_and_says_so_of_a_locked_one(self, folder, capsys):
        script = load("99_delete_everything.py")

        class Recorder:
            pretend = False

            def __init__(self, policy):
                self.policy, self.calls = policy, []

            def __call__(self, *args, **_):
                self.calls.append(args)
                return self.policy if "show" in args else None

        unlocked = Recorder(
            {"immutabilityPeriodSinceCreationInDays": 7, "state": "Unlocked", "etag": '"0x8D"'}
        )
        script.let_go(unlocked, "vxtest1234store", "vxtest1234-rg", "7")
        (deleted,) = [call for call in unlocked.calls if "delete" in call]
        assert (
            deleted[deleted.index("--if-match") + 1] == '"0x8D"'
            and deleted[deleted.index("--container-name") + 1] == "audit"
        )
        locked = Recorder(
            {"immutabilityPeriodSinceCreationInDays": 7, "state": "Locked", "etag": "e"}
        )
        script.let_go(locked, "vxtest1234store", "vxtest1234-rg", "7")
        assert not [call for call in locked.calls if "delete" in call]
        assert "it is locked" in capsys.readouterr().out
        source = (AZURE / "99_delete_everything.py").read_text(encoding="utf-8")
        assert flat(source).index("let_go(az,") < flat(source).index('"group", "delete"'), (
            "the policy first, then the group"
        )

    def test_the_steps_record_their_decisions_where_the_api_does(self):
        source = (MAIN_FUNCTION_APP / "function_app.py").read_text(encoding="utf-8")
        assert "on_retrieval=_audit_sink()" in source
        assert (
            'os.environ.get("VECTRIXDB_AUDIT_STORE"' in source
            and "audit_sink_at(where, query_key=key.encode(), on_failure=DENY)" in source
        )


class TestSigningInToTheQueryApp:
    """Said to the app, because a Function App serving the API with no key and no sign-in answers anybody."""

    STORE = "cosmos://vxtest1234-cosmos.documents.azure.com/data_db/signin_records"

    def test_the_query_apps_address_asks_people_to_sign_in(self, folder):
        config = folder.settings()
        every = folder.env_of(config, {"signin_store": self.STORE}, {})
        assert (
            every["VECTRIXDB_SIGNIN"] == "email" and every["VECTRIXDB_SIGNIN_STORE"] == self.STORE
        )
        assert (
            every["VECTRIXDB_PUBLIC_URL"] == f"https://{config['VX_QUERY_APP']}.azurewebsites.net"
        )

    def test_somebody_not_signed_in_is_a_guest_unless_the_settings_say_no(self, folder, tmp_path):
        from vectrixdb.signin import SignInConfig

        config = folder.settings()
        every = folder.env_of(
            config, {"signin_store": self.STORE}, {"VECTRIXDB_SIGNIN_SECRET": "s" * 64}
        )
        assert (
            every["VECTRIXDB_GUESTS"] == "on" and SignInConfig.from_env(tmp_path, env=every).guests
        )
        config["VX_GUESTS"] = "no"
        assert "VECTRIXDB_GUESTS" not in folder.env_of(config, {"signin_store": self.STORE}, {})
        assert "VECTRIXDB_GUESTS" not in folder.env_of(folder.settings(), {}, {}), (
            "no guests where nobody signs in"
        )

    def test_no_sign_in_without_its_store_or_when_the_settings_say_no(self, folder):
        assert "VECTRIXDB_SIGNIN" not in folder.env_of(folder.settings(), {}, {}), (
            "a sign-in kept on one disk is a sign-in on one instance"
        )
        config = folder.settings()
        config["VX_SIGNIN"] = "no"
        assert "VECTRIXDB_SIGNIN" not in folder.env_of(config, {"signin_store": self.STORE}, {})

    def test_the_library_starts_sign_in_from_what_the_app_is_given(self, folder, tmp_path):
        from vectrixdb.signin import SignInConfig

        every = folder.env_of(
            folder.settings(), {"signin_store": self.STORE}, {"VECTRIXDB_SIGNIN_SECRET": "s" * 64}
        )
        signin = SignInConfig.from_env(tmp_path, env=every)
        assert signin.enabled and signin.methods == ("email",)
        assert signin.public_url.startswith("https://") and signin.store_url == self.STORE

    def test_the_secret_is_kept_once_made_and_never_printed(self, folder):
        script = load("06_create_main_function_app.py")

        class Azure:
            pretend = False

            def __init__(self, held):
                self.held = held

            def __call__(self, *args, **_):
                return self.held if "appsettings" in args else None

        config, kept = folder.settings(), {"signin_store": self.STORE}
        assert (
            script.keys_for(
                Azure([{"name": "VECTRIXDB_SIGNIN_SECRET", "value": "kept"}]), config, kept
            )["VECTRIXDB_SIGNIN_SECRET"]
            == "kept"
        )
        assert re.fullmatch(
            r"[0-9a-f]{64}", script.keys_for(Azure([]), config, kept)["VECTRIXDB_SIGNIN_SECRET"]
        )
        assert "VECTRIXDB_SIGNIN_SECRET" not in script.keys_for(Azure([]), config, {})
        shown = folder._printable(
            [
                "functionapp",
                "config",
                "appsettings",
                "set",
                "--settings",
                "VECTRIXDB_SIGNIN_SECRET=abc123",
            ]
        )
        assert "VECTRIXDB_SIGNIN_SECRET=..." in shown and "abc123" not in " ".join(shown), (
            "the command is printed before it runs"
        )

    def people(self, monkeypatch, tmp_path, script, secret="the-link-secret"):
        """az standing in for az, and the library's command standing in for itself."""
        import types

        ran = []

        class Azure:
            pretend = False

            def __call__(self, *args, **_):
                if "appsettings" in args:
                    return [{"name": "VECTRIXDB_SIGNIN_SECRET", "value": secret}] if secret else []
                if "cosmosdb" in args:
                    return {"primaryMasterKey": "the-cosmos-key"}
                return None

        monkeypatch.setattr(script, "CONSOLE_ACCESS", tmp_path / "console-access.jsonl")
        monkeypatch.setattr(
            script.subprocess,
            "run",
            lambda command, env: ran.append((command, env)) or types.SimpleNamespace(returncode=0),
        )
        return Azure(), ran

    def test_the_first_admin_is_added_by_the_librarys_own_command(
        self, folder, monkeypatch, tmp_path, capsys
    ):
        script = load("06_create_main_function_app.py")
        az, ran = self.people(monkeypatch, tmp_path, script)
        config = folder.settings()
        assert script.add_person(az, config, {"signin_store": self.STORE}, "ada@example.com") == 0
        ((command, env),) = ran
        assert command[1:8] == [
            "-m",
            "vectrixdb.cli",
            "people",
            "add",
            "ada@example.com",
            "--role",
            "admin",
        ]
        assert env["VECTRIXDB_SIGNIN"] == "email" and env["VECTRIXDB_SIGNIN_STORE"] == self.STORE
        assert env["VECTRIXDB_PUBLIC_URL"] == f"https://{config['VX_QUERY_APP']}.azurewebsites.net"
        assert (
            env["VECTRIXDB_SIGNIN_SECRET"] == "the-link-secret"
            and env["VECTRIXDB_SIGNIN_STORE_KEY"] == "the-cosmos-key"
        )
        assert env["VECTRIXDB_ACCESS_LOG"] == str(tmp_path / "console-access.jsonl")
        assert env["PYTHONPATH"].split(script.os.pathsep)[0] == str(script.REPO), (
            "the library the apps run, not whichever this Python has"
        )
        said = capsys.readouterr()
        assert (
            "the-link-secret" not in said.out + said.err
            and "the-cosmos-key" not in said.out + said.err
        )
        assert not [
            part for part in command if "the-link-secret" in part or "the-cosmos-key" in part
        ], "secrets go in the environment, never the arguments"

    def test_nobody_is_added_where_sign_in_is_off(self, folder, monkeypatch, tmp_path, capsys):
        script = load("06_create_main_function_app.py")
        az, ran = self.people(monkeypatch, tmp_path, script)
        with pytest.raises(SystemExit):
            script.add_person(az, folder.settings(), {}, "ada@example.com")
        assert not ran and "Sign-in is not on" in capsys.readouterr().err

    def test_no_link_is_made_before_the_app_has_its_secret(
        self, folder, monkeypatch, tmp_path, capsys
    ):
        """A link signed with a secret the app does not hold would not open there."""
        script = load("06_create_main_function_app.py")
        az, ran = self.people(monkeypatch, tmp_path, script, secret=None)
        with pytest.raises(SystemExit):
            script.add_person(
                az, folder.settings(), {"signin_store": self.STORE}, "ada@example.com"
            )
        assert not ran and "--settings-only" in capsys.readouterr().err

    def test_a_dry_run_says_the_command_and_runs_nothing(
        self, folder, monkeypatch, tmp_path, capsys
    ):
        script = load("06_create_main_function_app.py")
        az, ran = self.people(monkeypatch, tmp_path, script)
        assert (
            script.add_person(
                az, folder.settings(), {"signin_store": self.STORE}, "ada@example.com", dry_run=True
            )
            == 0
        )
        assert (
            not ran
            and "vectrixdb people add ada@example.com --role admin" in capsys.readouterr().out
        )


class TestTheWayInForThePlatformsOwners:
    """How the team that owns the platform signs in: the query app's list, or single sign-on with the People list, both ways or alone."""

    STORE = TestSigningInToTheQueryApp.STORE
    SSO = {
        "VX_SIGNIN": "sso",
        "VX_OIDC_ISSUER": "https://login.example.test/tenant/v2.0",
        "VX_OIDC_CLIENT_ID": "vectrixdb",
        "VX_OIDC_ROLE_MAP": '{"g-owners": "admin"}',
        "VX_OIDC_ALLOWED_EMAILS": "ama@northwind.example,kofi@northwind.example",
    }
    GLASS = {
        "VX_BREAK_GLASS": "yes",
        "VX_BREAK_GLASS_UNTIL": "2099-01-01T02:00Z",
        "VX_BREAK_GLASS_ADMIN": "emergency.admin",
        # The hash of a password made for this test, as 06 --new-break-glass writes it.
        "VX_BREAK_GLASS_PASSWORD_HASH": "scrypt$32768$8$1$c2FsdHNhbHRzYWx0c2FsdA$" + "A" * 43,
    }

    def test_with_no_single_sign_on_passwords_are_asked_with_the_code(self, folder, tmp_path):
        from vectrixdb.signin import SignInConfig

        config = {**folder.settings(), "VX_SIGNIN_PASSWORDS": "yes"}
        every = folder.env_of(
            config, {"signin_store": self.STORE}, {"VECTRIXDB_SIGNIN_SECRET": "s" * 64}
        )
        assert every["VECTRIXDB_SIGNIN"] == "email" and every["VECTRIXDB_SIGNIN_PASSWORDS"] == "on"
        assert SignInConfig.from_env(tmp_path, env=every).passwords
        assert "VECTRIXDB_SIGNIN_PASSWORDS" not in folder.env_of(
            folder.settings(), {"signin_store": self.STORE}, {}
        ), "off unless asked"

    def test_single_sign_on_goes_with_its_list_and_the_library_reads_it(self, folder, tmp_path):
        from vectrixdb.signin import SignInConfig

        every = folder.env_of(
            {**folder.settings(), **self.SSO},
            {"signin_store": self.STORE},
            {"VECTRIXDB_SIGNIN_SECRET": "s" * 64},
        )
        assert every["VECTRIXDB_SIGNIN"] == "oidc,email", (
            "the enterprise way: the company's sign-in and their own passkeys, from one list"
        )
        assert every["VECTRIXDB_OIDC_ALLOWED_EMAILS"] == self.SSO["VX_OIDC_ALLOWED_EMAILS"]
        assert "VECTRIXDB_SIGNIN_PASSWORDS" not in every and "VECTRIXDB_BREAK_GLASS" not in every
        signin = SignInConfig.from_env(tmp_path, env=every)
        assert signin.methods == ("oidc", "email") and signin.oidc.allowed_emails == (
            "ama@northwind.example",
            "kofi@northwind.example",
        )

    def test_sso_only_is_the_companys_sign_in_alone(self, folder, tmp_path):
        from vectrixdb.signin import SignInConfig

        config = {
            **folder.settings(),
            **self.SSO,
            "VX_SIGNIN": "sso-only",
            "VX_SSO_RECHECK_DAYS": "30",
        }
        every = folder.env_of(
            config, {"signin_store": self.STORE}, {"VECTRIXDB_SIGNIN_SECRET": "s" * 64}
        )
        assert every["VECTRIXDB_SIGNIN"] == "oidc" and "VECTRIXDB_SSO_RECHECK_DAYS" not in every, (
            "nobody keeps a way of their own to recheck"
        )
        assert SignInConfig.from_env(tmp_path, env=every).methods == ("oidc",)

    def test_before_the_provider_is_set_up_the_list_signs_in_by_email_with_no_passkeys(
        self, folder, tmp_path, capsys
    ):
        from vectrixdb.signin import SignInConfig

        script = load("06_create_main_function_app.py")
        config = {
            **folder.settings(),
            "VX_SIGNIN": "sso-only",
            "VX_SIGNIN_USERS": "ama@northwind.example:admin",
        }
        for name in [n for n in config if n.startswith("VX_OIDC_")]:
            config[name] = ""
        script.held_before_sent(config, {})
        every = folder.env_of(
            config, {"signin_store": self.STORE}, {"VECTRIXDB_SIGNIN_SECRET": "s" * 64}
        )
        assert every["VECTRIXDB_SIGNIN"] == "oidc" and not [
            name for name in every if name.startswith("VECTRIXDB_OIDC_")
        ]
        signin = SignInConfig.from_env(tmp_path, env=every)
        assert signin.sso_pending and signin.methods == ("email",) and not signin.own_passkeys
        config.pop("VX_SIGNIN_USERS")
        with pytest.raises(SystemExit):
            script.held_before_sent(config, {})
        assert "needs the People list" in capsys.readouterr().err

    def test_the_query_app_takes_sixteen_at_once_and_keeps_none_ready_unless_told(self, folder):
        script = load("06_create_main_function_app.py")
        config = {
            name: value
            for name, value in folder.settings().items()
            if not name.startswith("VX_QUERY_A")
        }
        assert script.query_scale(config) == {"VX_QUERY_AT_ONCE": 16, "VX_QUERY_ALWAYS_READY": 0}
        assert script.query_scale(
            {**config, "VX_QUERY_AT_ONCE": "32", "VX_QUERY_ALWAYS_READY": " 1 "}
        ) == {"VX_QUERY_AT_ONCE": 32, "VX_QUERY_ALWAYS_READY": 1}

    @pytest.mark.parametrize(
        "name, value",
        [
            ("VX_QUERY_AT_ONCE", "0"),
            ("VX_QUERY_AT_ONCE", "many"),
            ("VX_QUERY_ALWAYS_READY", "-1"),
            ("VX_QUERY_ALWAYS_READY", "1.5"),
        ],
    )
    def test_06_stops_a_scale_that_is_not_a_whole_number_in_range(
        self, folder, capsys, name, value
    ):
        script = load("06_create_main_function_app.py")
        with pytest.raises(SystemExit):
            script.held_before_sent({**folder.settings(), name: value}, {})
        assert name in capsys.readouterr().err

    def test_the_scale_is_sent_to_the_query_app_and_to_no_other(self, folder):
        script = load("06_create_main_function_app.py")
        calls = []

        def az(*args, **_):
            calls.append(args)

        script.send_query_scale(
            az,
            {**folder.settings(), "VX_QUERY_AT_ONCE": "16", "VX_QUERY_ALWAYS_READY": "1"},
            "vxtest1234-query",
            "vxtest1234-rg",
        )
        assert [call[:4] for call in calls] == [
            ("functionapp", "scale", "config", "set"),
            ("functionapp", "scale", "config", "always-ready"),
        ]
        assert all("vxtest1234-query" in call for call in calls)
        assert "perInstanceConcurrency=16" in calls[0] and "http=1" in calls[1]

    def test_the_people_list_is_enough_for_single_sign_on_and_goes_as_signin_users(
        self, folder, tmp_path
    ):
        from vectrixdb.signin import SignInConfig

        script = load("06_create_main_function_app.py")
        config = {
            **folder.settings(),
            **self.SSO,
            "VX_SIGNIN_USERS": "ama@northwind.example:admin, kofi@northwind.example:operator",
        }
        config.pop("VX_OIDC_ALLOWED_EMAILS")
        script.held_before_sent(config, {})
        every = folder.env_of(
            config, {"signin_store": self.STORE}, {"VECTRIXDB_SIGNIN_SECRET": "s" * 64}
        )
        assert "VECTRIXDB_OIDC_ALLOWED_EMAILS" not in every
        assert SignInConfig.from_env(tmp_path, env=every).users == (
            ("ama@northwind.example", "admin"),
            ("kofi@northwind.example", "operator"),
        )

    def test_an_apps_token_goes_with_its_audience_and_role(self, folder, tmp_path):
        from vectrixdb.signin import SignInConfig

        config = {
            **folder.settings(),
            **self.SSO,
            "VX_OIDC_API_AUDIENCE": "api://vectrixdb",
            "VX_OIDC_TOKEN_ROLE": "searcher",
            "VX_SSO_RECHECK_DAYS": "30",
        }
        every = folder.env_of(
            config, {"signin_store": self.STORE}, {"VECTRIXDB_SIGNIN_SECRET": "s" * 64}
        )
        assert (
            every["VECTRIXDB_OIDC_API_AUDIENCE"] == "api://vectrixdb"
            and every["VECTRIXDB_OIDC_TOKEN_ROLE"] == "searcher"
        )
        signin = SignInConfig.from_env(tmp_path, env=every)
        assert signin.oidc.token_role == "searcher" and signin.sso_recheck_days == 30

    @pytest.mark.parametrize(
        "over, says",
        [
            ({"VX_OIDC_TOKEN_ROLE": "admin"}, "never an admin"),
            ({"VX_SSO_RECHECK_DAYS": "a month"}, "a whole number of days"),
            ({"VX_SIGNIN": "sso-only", "VX_SSO_RECHECK_DAYS": "30"}, "needs VX_SIGNIN=sso"),
        ],
    )
    def test_06_stops_what_the_app_would_refuse(self, folder, capsys, over, says):
        script = load("06_create_main_function_app.py")
        with pytest.raises(SystemExit):
            script.held_before_sent({**folder.settings(), **self.SSO, **over}, {})
        assert says in capsys.readouterr().err

    def test_emergency_sign_in_goes_with_every_way_in(self, folder, tmp_path):
        from vectrixdb.signin import SignInConfig

        every = folder.env_of(
            {**folder.settings(), **self.SSO, **self.GLASS},
            {"signin_store": self.STORE},
            {"VECTRIXDB_SIGNIN_SECRET": "s" * 64},
        )
        assert (
            every["VECTRIXDB_BREAK_GLASS"] == "on"
            and every["VECTRIXDB_BREAK_GLASS_ADMIN"] == "emergency.admin"
        )
        assert (
            every["VECTRIXDB_BREAK_GLASS_PASSWORD_HASH"]
            == self.GLASS["VX_BREAK_GLASS_PASSWORD_HASH"]
        )
        assert not {"VECTRIXDB_BREAK_GLASS_PASSWORD", "VECTRIXDB_BREAK_GLASS_TOTP"} & set(every), (
            "the app is never given the password, and asks for no code"
        )
        assert (
            SignInConfig.from_env(tmp_path, env=every).break_glass.until_iso
            == "2099-01-01T02:00:00Z"
        )
        alone = folder.env_of(
            {**folder.settings(), **self.GLASS},
            {"signin_store": self.STORE},
            {"VECTRIXDB_SIGNIN_SECRET": "s" * 64},
        )
        assert alone["VECTRIXDB_SIGNIN"] == "email" and alone["VECTRIXDB_BREAK_GLASS"] == "on", (
            "the usual sign-in can be down whichever it is"
        )
        assert SignInConfig.from_env(tmp_path, env=alone).break_glass is not None
        off = folder.env_of(
            {**folder.settings(), **self.GLASS, "VX_SIGNIN": "no"}, {"signin_store": self.STORE}, {}
        )
        assert "VECTRIXDB_BREAK_GLASS" not in off

    @pytest.mark.parametrize(
        "missing", ["VX_OIDC_ALLOWED_EMAILS", "VX_OIDC_ISSUER", "VX_OIDC_ROLE_MAP"]
    )
    def test_06_stops_single_sign_on_without_what_it_needs_the_list_first(
        self, folder, missing, capsys
    ):
        script = load("06_create_main_function_app.py")
        config = {**folder.settings(), **self.SSO}
        config.pop(missing)
        with pytest.raises(SystemExit):
            script.held_before_sent(config, {})
        assert missing in capsys.readouterr().err

    def test_06_stops_emergency_sign_in_that_is_half_set_or_has_no_sign_in(self, folder, capsys):
        script = load("06_create_main_function_app.py")
        half = {**folder.settings(), **self.SSO, **self.GLASS}
        half.pop("VX_BREAK_GLASS_UNTIL")
        with pytest.raises(SystemExit):
            script.held_before_sent(half, {})
        assert "VX_BREAK_GLASS_UNTIL" in capsys.readouterr().err
        with pytest.raises(SystemExit):
            script.held_before_sent({**folder.settings(), **self.GLASS, "VX_SIGNIN": "no"}, {})
        assert "VX_SIGNIN is no" in capsys.readouterr().err
        script.held_before_sent({**folder.settings(), **self.GLASS}, {})

    def test_06_stops_with_no_hash_and_names_the_command_that_writes_it(self, folder, capsys):
        script = load("06_create_main_function_app.py")
        config = {**folder.settings(), **self.SSO, **self.GLASS}
        config.pop("VX_BREAK_GLASS_PASSWORD_HASH")
        with pytest.raises(SystemExit):
            script.held_before_sent(config, {})
        said = capsys.readouterr().err
        assert "VX_BREAK_GLASS_PASSWORD_HASH" in said and "--new-break-glass" in said

    @pytest.mark.parametrize("old", ["VX_BREAK_GLASS_PASSWORD", "VX_BREAK_GLASS_TOTP"])
    def test_06_stops_a_password_or_an_authenticator_secret_left_in_the_settings(
        self, folder, capsys, old
    ):
        script = load("06_create_main_function_app.py")
        with pytest.raises(SystemExit):
            script.held_before_sent(
                {**folder.settings(), **self.SSO, **self.GLASS, old: "left-from-before"}, {}
            )
        said = capsys.readouterr().err
        assert f"Take {old} out" in said and "left-from-before" not in said

    def new_password(self, folder, monkeypatch, tmp_path, script, vault_answers=0):
        """az and the library standing in for themselves: what each was given, and the file the password went by."""
        import types

        seen = {"az": [], "library": [], "file": None}

        class Azure:
            pretend = False

            def __call__(self, *args, **_):
                seen["az"].append(args)
                held = Path(args[args.index("--file") + 1])
                seen["file"] = (held, held.read_text(encoding="utf-8") if held.exists() else None)
                if vault_answers:
                    raise SystemExit(1)

        def library(command, input, env, capture_output, text):  # noqa: A002 - subprocess.run's own name for it
            seen["library"].append((command, input))
            return types.SimpleNamespace(
                returncode=0, stdout="scrypt$32768$8$1$c2FsdA$made-by-the-library\n", stderr=""
            )

        monkeypatch.setattr(script, "LOCAL", tmp_path / ".local")
        monkeypatch.setattr(script.subprocess, "run", library)
        return Azure(), seen

    def test_a_new_password_goes_to_the_vault_and_only_its_hash_to_settings_env(
        self, folder, monkeypatch, tmp_path, capsys
    ):
        script = load("06_create_main_function_app.py")
        az, seen = self.new_password(folder, monkeypatch, tmp_path, script)
        folder.SETTINGS.write_text(
            "VX_PREFIX=vxtest1234\nVX_KEY_VAULT=northwind-vault\n# VX_BREAK_GLASS_PASSWORD_HASH=\nVX_GUESTS=yes\n",
            encoding="utf-8",
        )
        assert script.new_break_glass(az, folder.settings()) == 0
        ((command, password),) = seen["library"]
        assert len(password) == 32 and password not in " ".join(command), (
            "given on its input, never as an argument"
        )
        (sent,) = seen["az"]
        assert sent[:7] == (
            "keyvault",
            "secret",
            "set",
            "--vault-name",
            "northwind-vault",
            "--name",
            "vx-break-glass",
        )
        assert password not in sent and sent[-2:] == ("--output", "none"), (
            "az is not asked to print the secret back"
        )
        held, was = seen["file"]
        assert was == password and not held.exists(), (
            "the file is there for the one command, and gone after"
        )
        lines = folder.SETTINGS.read_text(encoding="utf-8").splitlines()
        assert lines == [
            "VX_PREFIX=vxtest1234",
            "VX_KEY_VAULT=northwind-vault",
            "VX_BREAK_GLASS_PASSWORD_HASH=scrypt$32768$8$1$c2FsdA$made-by-the-library",
            "VX_GUESTS=yes",
        ]
        said = capsys.readouterr()
        assert password not in said.out + said.err and "scrypt$" not in said.out + said.err, (
            "neither is shown"
        )

    def test_a_vault_that_refuses_leaves_no_file_and_no_hash(self, folder, monkeypatch, tmp_path):
        script = load("06_create_main_function_app.py")
        az, seen = self.new_password(folder, monkeypatch, tmp_path, script, vault_answers=1)
        folder.SETTINGS.write_text(
            "VX_PREFIX=vxtest1234\nVX_KEY_VAULT=northwind-vault\n", encoding="utf-8"
        )
        with pytest.raises(SystemExit):
            script.new_break_glass(az, folder.settings())
        assert not seen["file"][0].exists()
        assert "VX_BREAK_GLASS_PASSWORD_HASH" not in folder.SETTINGS.read_text(encoding="utf-8")

    def test_it_needs_a_vault_named_and_a_dry_run_makes_nothing(
        self, folder, monkeypatch, tmp_path, capsys
    ):
        script = load("06_create_main_function_app.py")
        az, seen = self.new_password(folder, monkeypatch, tmp_path, script)
        with pytest.raises(SystemExit):
            script.new_break_glass(az, folder.settings())
        assert "VX_KEY_VAULT" in capsys.readouterr().err and not seen["az"]
        assert (
            script.new_break_glass(
                az, {**folder.settings(), "VX_KEY_VAULT": "northwind-vault"}, dry_run=True
            )
            == 0
        )
        assert not seen["library"] and seen["file"][1] is None, "no password was made"

    def test_a_setting_is_kept_on_its_own_line_wherever_that_is(self, folder):
        folder.SETTINGS.write_text("VX_PREFIX=vxtest1234\n", encoding="utf-8")
        folder.keep_setting("VX_BREAK_GLASS_PASSWORD_HASH", "scrypt$one")
        folder.keep_setting("VX_BREAK_GLASS_PASSWORD_HASH", "scrypt$two")
        assert (
            folder.SETTINGS.read_text(encoding="utf-8")
            == "VX_PREFIX=vxtest1234\nVX_BREAK_GLASS_PASSWORD_HASH=scrypt$two\n"
        )

    def test_adding_somebody_with_single_sign_on_gives_the_command_the_providers_settings_and_not_emergency_sign_ins(
        self, folder, monkeypatch, tmp_path
    ):
        """The command reads the same sign-in the app does, and its link is written the way people reach the dashboard."""
        script = load("06_create_main_function_app.py")
        az, ran = TestSigningInToTheQueryApp().people(monkeypatch, tmp_path, script)
        config = {
            **folder.settings(),
            **self.SSO,
            **self.GLASS,
            "VX_GATEWAY_URL": "https://gateway.example.com",
            "VX_QUERY_PREFIX": "acme",
            "VX_QUERY_GATEWAY_PATHS": "api/v1=/files/search, dashboard=/files/dash",
        }
        assert (
            script.add_person(az, config, {"signin_store": self.STORE}, "ama@northwind.example")
            == 0
        )
        ((_, env),) = ran
        assert (
            env["VECTRIXDB_SIGNIN"] == "oidc,email"
            and env["VECTRIXDB_OIDC_ISSUER"] == self.SSO["VX_OIDC_ISSUER"]
        )
        assert env["VECTRIXDB_PREFIX"] == "acme" and env["VECTRIXDB_GATEWAY_PATHS"].startswith(
            "api/v1="
        )
        assert env["VECTRIXDB_PUBLIC_URL"] == "https://gateway.example.com"
        assert not [name for name in env if name.startswith("VECTRIXDB_BREAK_GLASS")], (
            "the people command has no use for them"
        )

    def test_the_password_and_the_code_are_never_shown(self, folder):
        script = load("06_create_main_function_app.py")
        for name in ("VECTRIXDB_BREAK_GLASS_PASSWORD_HASH", "VECTRIXDB_OIDC_CLIENT_SECRET"):
            assert script.shown_as(name, "held") == "..."
            assert (
                folder._printable(
                    ["functionapp", "config", "appsettings", "set", "--settings", f"{name}=held"]
                )[-1]
                == f"{name}=..."
            )


class TestTheQueryAppBehindAGateway:
    """The gateway's address, the prefix and each route's own path, set in settings.env and read by the library."""

    GATEWAY = {
        "VX_GATEWAY_URL": "https://gateway.example.com",
        "VX_QUERY_PREFIX": "acme",
        "VX_QUERY_GATEWAY_PATHS": "api/v1=/files/search, auth=/files/auth, dashboard=/files/dash",
        "VX_TRUSTED_PROXIES": "10.0.0.0/8",
        "VX_KEY_HEADER": "x-search-key",
        "VX_TOKEN_HEADER": "x-user-token",
    }

    def test_nothing_set_nothing_sent_and_the_app_is_its_own_address(self, folder):
        every = folder.env_of(
            {**folder.settings(), "VX_SIGNIN": "yes"},
            {"signin_store": TestSigningInToTheQueryApp.STORE},
            {},
        )
        assert not {
            "VECTRIXDB_PREFIX",
            "VECTRIXDB_GATEWAY_PATHS",
            "VECTRIXDB_KEY_HEADER",
            "VECTRIXDB_TOKEN_HEADER",
        } & set(every)
        assert every["VECTRIXDB_PUBLIC_URL"].endswith(".azurewebsites.net")

    def test_every_part_goes_and_the_library_reads_it(self, folder):
        from vectrixdb.api.gateway import Gateway

        every = folder.env_of({**folder.settings(), **self.GATEWAY}, {}, {})
        assert every["VECTRIXDB_PUBLIC_URL"] == "https://gateway.example.com", (
            "set even with sign-in off: it is where callers are"
        )
        gateway = Gateway.from_env(every)
        assert gateway.prefix == "/acme" and gateway.paths["auth"] == "/files/auth"
        assert gateway.key_header == "x-search-key" and gateway.token_header == "x-user-token"
        assert (
            gateway.address("/dashboard/")
            == "https://gateway.example.com/files/dash/acme/dashboard/"
        )
        assert every["VECTRIXDB_TRUSTED_PROXIES"] == "10.0.0.0/8"

    def test_with_sign_in_on_it_is_where_single_sign_on_returns(self, folder):
        every = folder.env_of(
            {**folder.settings(), **TestTheWayInForThePlatformsOwners.SSO, **self.GATEWAY},
            {"signin_store": TestSigningInToTheQueryApp.STORE},
            {},
        )
        assert every["VECTRIXDB_PUBLIC_URL"] == "https://gateway.example.com"

    def test_the_dashboard_address_the_steps_print(self, folder):
        assert (
            folder.dashboard_address({**folder.settings(), **self.GATEWAY})
            == "https://gateway.example.com/files/dash/acme/dashboard/"
        )
        plain = folder.settings()
        assert (
            folder.dashboard_address(plain)
            == f"https://{plain['VX_QUERY_APP']}.azurewebsites.net/dashboard/"
        )

    def test_06_reads_it_with_the_library_before_anything_is_sent(self, folder, capsys):
        script = load("06_create_main_function_app.py")
        config = {**folder.settings(), **self.GATEWAY}
        script.held_before_sent(config, folder.env_of(config, {}, {}))
        assert "the dashboard at /files/dash/acme/dashboard/" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "over, says",
        [
            (
                {"VX_QUERY_GATEWAY_PATHS": "api/v9=/files/nine"},
                "api/v9, which no route here falls under",
            ),
            ({"VX_KEY_HEADER": "search key"}, "cannot be the name of an HTTP header"),
            ({"VX_TRUSTED_PROXIES": "not a network"}, "VECTRIXDB_TRUSTED_PROXIES"),
            ({"VX_GATEWAY_URL": "http://gateway.example.com"}, "the gateway's https address"),
        ],
    )
    def test_06_stops_what_would_stop_the_app(self, folder, capsys, over, says):
        script = load("06_create_main_function_app.py")
        config = {**folder.settings(), **self.GATEWAY, **over}
        with pytest.raises(SystemExit):
            script.held_before_sent(config, folder.env_of(config, {}, {}))
        assert says in capsys.readouterr().err

    def test_the_example_names_no_real_company(self):
        example = (AZURE / "settings.example.env").read_text(encoding="utf-8")
        for key in self.GATEWAY:
            assert f"\n{key}=\n" in example, f"{key} is there, empty"
        block = example[
            example.index("# A gateway in front of the query app") : example.index(
                "VX_EXTRACT_GATEWAY_PATHS="
            )
        ]
        hosts = set(re.findall(r"https?://([^/\s:]+)", block))
        assert hosts == {"gateway.example.com"}, f"only a reserved example host: {hosts}"
        assert "VX_QUERY_PREFIX=acme" in block, "and a made-up prefix"


class TestTheCompanysLookOnBothDashboards:
    """Set once in settings.env, sent to the query app inside its settings, and shown by both dashboards."""

    PALETTE = {"light": {"page": "#f4f6f4", "text": "#15241c"}, "dark": {"page": "#050806"}}
    SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect width="40" height="40" rx="8" fill="#1f6f54"/></svg>'

    def brand(self, folder, tmp_path, **over):
        logo = tmp_path / "mark.svg"
        logo.write_text(self.SVG, encoding="utf-8")
        config = {
            **folder.settings(),
            "VX_BRAND_NAME": "Northwind",
            "VX_BRAND_LOGO": str(logo),
            "VX_BRAND_ACCENT": "#0b5cad",
            "VX_BRAND_WORDMARK": "yes",
            "VX_BRAND_COPYRIGHT": "Northwind",
            "VX_BRAND_PALETTE": json.dumps(self.PALETTE),
            **over,
        }
        return folder.env_of(config, {}, {})

    def test_nothing_set_nothing_sent(self, folder):
        assert not [
            name
            for name in folder.env_of(folder.settings(), {}, {})
            if name.startswith("VECTRIXDB_BRAND_")
        ]

    def test_every_part_goes_and_the_library_reads_it_back_as_it_was(self, folder, tmp_path):
        from vectrixdb.brand import Brand

        every = self.brand(folder, tmp_path)
        assert every["VECTRIXDB_BRAND_LOGO"].startswith("data:image/svg+xml;base64,")
        assert every["VECTRIXDB_BRAND_PALETTE"].startswith("data:application/json;base64,")
        assert '"' not in every["VECTRIXDB_BRAND_PALETTE"], (
            "no quote for the command line to mangle on its way to Azure"
        )
        brand = Brand.from_env({k: v for k, v in every.items() if k.startswith("VECTRIXDB_BRAND_")})
        assert (
            brand.name == "Northwind" and brand.wordmark and brand.copyright == "© 2026 Northwind"
        )
        assert brand.logo.data == self.SVG.encode("utf-8") and brand.palette == self.PALETTE

    def test_a_palette_file_goes_the_same_way(self, folder, tmp_path):
        from vectrixdb.brand import Brand

        where = tmp_path / "palette.json"
        where.write_text(json.dumps(self.PALETTE), encoding="utf-8")
        every = self.brand(folder, tmp_path, VX_BRAND_PALETTE=str(where))
        assert (
            Brand.from_env({"VECTRIXDB_BRAND_PALETTE": every["VECTRIXDB_BRAND_PALETTE"]}).palette
            == self.PALETTE
        )

    @pytest.mark.parametrize(
        "setting, value, says",
        [
            ("VX_BRAND_LOGO", "C:/nowhere/mark.svg", "cannot be read"),
            ("VX_BRAND_PALETTE", "{not json", "VX_BRAND_PALETTE is not JSON"),
        ],
    )
    def test_a_mistake_stops_here_saying_which_line(
        self, folder, tmp_path, setting, value, says, capsys
    ):
        with pytest.raises(SystemExit):
            self.brand(folder, tmp_path, **{setting: value})
        assert says in capsys.readouterr().err

    def test_the_terminal_shows_a_logo_by_its_kind_and_size(self, folder, tmp_path):
        script = load("06_create_main_function_app.py")
        every = self.brand(folder, tmp_path)
        shown = script.shown_as("VECTRIXDB_BRAND_LOGO", every["VECTRIXDB_BRAND_LOGO"])
        assert shown.startswith("data:image/svg+xml;base64,...(") and shown.endswith(" characters)")
        printed = folder._printable(
            [
                "functionapp",
                "config",
                "appsettings",
                "set",
                "--settings",
                f"VECTRIXDB_BRAND_LOGO={every['VECTRIXDB_BRAND_LOGO']}",
            ]
        )
        assert printed[-1].startswith("VECTRIXDB_BRAND_LOGO=data:image/svg+xml;base64,...(")

    def test_06_has_the_library_read_the_brand_before_it_is_sent(
        self, folder, tmp_path, monkeypatch, capsys
    ):
        import types

        script = load("06_create_main_function_app.py")
        ran = []
        monkeypatch.setattr(
            script.subprocess,
            "run",
            lambda command, env, capture_output, text: (
                ran.append(env)
                or types.SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="Traceback\nConfigurationError: VECTRIXDB_BRAND_PALETTE: in the light theme, muted #8fa0b6 on page #f7f5f1 is 2.5 to 1\n",
                )
            ),
        )
        every = self.brand(folder, tmp_path)
        with pytest.raises(SystemExit):
            script.held_before_sent(folder.settings(), every)
        assert "muted #8fa0b6 on page #f7f5f1" in capsys.readouterr().err
        (env,) = ran
        assert env["VECTRIXDB_BRAND_NAME"] == "Northwind" and env["PYTHONPATH"].split(
            script.os.pathsep
        )[0] == str(script.REPO)

    def test_the_example_names_every_brand_setting_with_a_made_up_company(self):
        example = (AZURE / "settings.example.env").read_text(encoding="utf-8")
        for name in (
            "VX_BRAND_NAME",
            "VX_BRAND_LOGO",
            "VX_BRAND_ACCENT",
            "VX_BRAND_WORDMARK",
            "VX_BRAND_COPYRIGHT",
            "VX_BRAND_PALETTE",
            "VX_SIGNIN_PASSWORDS",
            "VX_OIDC_ALLOWED_EMAILS",
            "VX_BREAK_GLASS",
            "VX_BREAK_GLASS_UNTIL",
        ):
            assert f"{name}=" in example, name
        (name,) = [
            line.split("=", 1)[1]
            for line in example.splitlines()
            if line.startswith("# VX_BRAND_NAME=")
        ]
        assert name == "Northwind", "the example names a made-up company, never a real one"


class TestTheIngestAppAsksForTheKey:
    """The ingest app's face is a bare FastAPI without the library's key layer,
    and _ours adds a collection's delete and setup to it. With the server's key
    set, those answered anyone who found the address."""

    @pytest.fixture
    def lean(self, monkeypatch):
        pytest.importorskip("fastapi")
        from unittest import mock

        from fastapi.testclient import TestClient

        for name in (
            "azure.functions",
            "azure.storage",
            "azure.storage.blob",
            "azure.storage.queue",
        ):
            monkeypatch.setitem(sys.modules, name, mock.MagicMock())
        monkeypatch.syspath_prepend(str(MAIN_FUNCTION_APP))
        monkeypatch.delitem(sys.modules, "function_app", raising=False)
        monkeypatch.setenv("VECTRIXDB_API_KEY", "the-server-key")
        # Importing the app sets the defaults a deployment runs with into the
        # environment (the storage backend among them); none may outlive this.
        environment = dict(os.environ)
        before = set(sys.modules)
        try:
            import function_app

            yield TestClient(function_app._lean(), raise_server_exceptions=False)
        finally:
            os.environ.clear()
            os.environ.update(environment)
            for name in set(sys.modules) - before:
                module = sys.modules.get(name)
                if str(getattr(module, "__file__", "") or "").startswith(str(MAIN_FUNCTION_APP)):
                    sys.modules.pop(name, None)

    def test_without_the_key_its_routes_are_refused(self, lean):
        assert lean.post("/api/v1/collections/financial/setup").status_code == 401
        assert lean.post("/api/v1/golden/write").status_code == 401
        assert lean.post("/api/v1/golden/write", headers={"api-key": "wrong"}).status_code == 401

    def test_health_needs_no_key(self, lean):
        assert lean.get("/health").status_code == 200

    @pytest.mark.parametrize(
        "headers", [{"api-key": "the-server-key"}, {"Authorization": "Bearer the-server-key"}]
    )
    def test_the_key_goes_through(self, lean, headers):
        assert lean.post("/api/v1/golden/write", headers=headers).status_code != 401


class TestALeftoverIndexIsTidied:
    """A run from before the prefix was set empty left vectrix-collections, 0 documents, beside the collections the app reads."""

    class Service:
        """The search service as az answers for it: an admin key, a list of indexes, and a count for each."""

        pretend = False

        def __init__(self, held):
            self.held, self.calls = held, []

        def __call__(self, *args, reads=False, quiet=False, allow_fail=False):
            self.calls.append(args)
            if args[:3] == ("search", "admin-key", "show"):
                return {"primaryKey": "ADMIN-KEY-SECRET"}
            if args[:1] == ("rest",):
                url = args[args.index("--url") + 1]
                if "--method" in args and args[args.index("--method") + 1] == "get":
                    if "/indexes?" in url:
                        return {"value": [{"name": name} for name in self.held]}
                    name = url.split("/indexes/", 1)[1].split("/")[0]
                    return {"documentCount": self.held[name], "storageSize": self.held[name] * 1000}
            return None

        def deleted(self):
            return [
                args[args.index("--url") + 1]
                for args in self.calls
                if args[:1] == ("rest",) and args[args.index("--method") + 1] == "delete"
            ]

    def test_an_empty_one_is_deleted_and_the_key_is_shown_as_dots(self, folder, capsys):
        az = self.Service(
            {"collections": 3, "financial": 40, "media": 12, "misc": 2, "vectrix-collections": 0}
        )
        assert folder.tidy_old_catalog(az, folder.settings(), "") == "deleted"
        (gone,) = az.deleted()
        assert gone.startswith(
            "https://vxtest1234-search.search.windows.net/indexes/vectrix-collections?api-version="
        )
        said = capsys.readouterr().out
        assert "vectrix-collections deleted: empty" in said and "the app reads collections" in said
        assert "ADMIN-KEY-SECRET" not in said
        (delete,) = [
            args
            for args in az.calls
            if args[:1] == ("rest",) and args[args.index("--method") + 1] == "delete"
        ]
        printed = " ".join(folder._printable(delete))
        assert "ADMIN-KEY-SECRET" not in printed and "api-key=..." in printed, (
            "the admin key is printed as dots, like every key"
        )
        assert "--skip-authorization-header" in printed, (
            "the service is reached with its own key, not a token for ARM"
        )

    def test_one_holding_documents_is_never_deleted(self, folder, capsys):
        az = self.Service({"collections": 3, "vectrix-collections": 7})
        assert folder.tidy_old_catalog(az, folder.settings(), "") == "kept"
        assert az.deleted() == []
        assert "holds 7 documents, so it is left alone" in capsys.readouterr().out

    def test_nothing_to_do_with_no_leftover_or_with_a_prefix(self, folder):
        az = self.Service({"collections": 3, "financial": 40})
        assert folder.tidy_old_catalog(az, folder.settings(), "") is None and az.deleted() == []
        az = self.Service({"vectrix-collections": 0})
        assert folder.tidy_old_catalog(az, folder.settings(), "vectrix") is None, (
            "with that prefix it is the catalog in use"
        )
        assert az.calls == [], "nothing is even asked"

    def test_a_dry_run_asks_nothing_and_says_it_cannot(self, folder, capsys):
        assert folder.tidy_old_catalog(folder.Az(pretend=True), folder.settings(), "") == "unknown"
        assert "rest" not in capsys.readouterr().out

    def test_06_tidies_after_the_settings_where_the_prefix_is_known(
        self, folder, monkeypatch, capsys
    ):
        script = load("06_create_main_function_app.py")
        monkeypatch.setattr(sys, "argv", ["06_create_main_function_app.py", "--dry-run"])
        assert script.main() == 0
        said = capsys.readouterr().out
        assert (
            "A leftover vectrix-collections index" in said
            and "cannot be asked in a dry run" in said
        )
        source = (AZURE / "06_create_main_function_app.py").read_text(encoding="utf-8")
        assert (
            source.index("send_query_scale(az, config")
            < source.index('tidy_old_catalog(az, config, every.get("AZURE_SEARCH_INDEX_PREFIX"')
            < source.index('step("The wheel it installs")')
        )

    def test_99_lists_every_index_with_its_count_so_a_leftover_is_visible(self, folder, capsys):
        import io

        script = load("99_delete_everything.py")
        held = {"collections": 3, "financial": 40, "vectrix-collections": 0}

        class Group(self.Service):
            def __call__(self, *args, **kw):
                if args[:2] == ("group", "show"):
                    return {"name": "vxtest1234-rg"}
                if args[:2] == ("resource", "list"):
                    return [
                        {"type": "Microsoft.Search/searchServices", "name": "vxtest1234-search"},
                        {"type": "Microsoft.Storage/storageAccounts", "name": "vxtest1234store"},
                    ]
                return super().__call__(*args, **kw)

        az = Group(held)
        az.pretend = True
        folder.Az.exists.__get__(
            az
        )  # the real exists would answer False in pretend mode; the listing is what is under test
        config = folder.settings()
        inside = az(
            "resource",
            "list",
            "--resource-group",
            config["VX_RESOURCE_GROUP"],
            reads=True,
            quiet=True,
        )
        listed = folder.search_indexes(az, config)
        assert listed == [
            {"name": "collections", "documents": 3, "bytes": 3000},
            {"name": "financial", "documents": 40, "bytes": 40000},
            {"name": "vectrix-collections", "documents": 0, "bytes": 0},
        ]
        source = (AZURE / "99_delete_everything.py").read_text(encoding="utf-8")
        body = source[source.index('step("What is in it")') : source.index("if not args.yes")]
        assert (
            "search_indexes(az, config)" in body
            and "documents, " in body
            and "a leftover from before the prefix" in body
        )
        assert "ADMIN-KEY-SECRET" not in capsys.readouterr().out
        del inside, io

    def test_the_readme_says_what_the_leftover_is(self):
        readme = (AZURE / "README.md").read_text(encoding="utf-8")
        section = readme.split("## Three collections", 1)[1].split("\n## ", 1)[0]
        assert (
            "`vectrix-collections`" in section
            and "AZURE_SEARCH_INDEX_PREFIX" in section
            and "99_delete_everything.py" in section
        )


class TestTheThreeRetrievalScripts:
    """_retrieve.py serves three scripts, and each one has to exist, take --dry-run, and go through the dashboard's own pages."""

    THREE = (
        "10_retrieve_finds_the_most.py",
        "11_retrieve_best_for_balance.py",
        "12_retrieve_best_for_time.py",
    )

    def test_each_names_a_pick_the_readme_lists_and_calls_retrieve(self):
        readme = (AZURE / "README.md").read_text(encoding="utf-8")
        for name in self.THREE:
            assert (AZURE / name).exists(), name
            source = (AZURE / name).read_text(encoding="utf-8")
            pick = re.search(r'STEP, PICK = "(\d+)", "([a-z_]+)"', source)
            assert pick and name.startswith(pick.group(1)) and f"`{pick.group(2)}`" in readme, name
            assert "from _retrieve import options, retrieve" in source and "retrieve(PICK," in flat(
                source
            )
            assert name in readme

    @pytest.mark.parametrize("name", THREE)
    def test_a_dry_run_asks_nothing_and_exits_0(self, folder, monkeypatch, capsys, name):
        import urllib.request

        monkeypatch.setattr(
            urllib.request, "urlopen", lambda *a, **k: pytest.fail("a dry run asks nothing")
        )
        script = load(name)
        monkeypatch.setattr(sys, "argv", [name, "--dry-run"])
        assert script.main() == 0
        said = capsys.readouterr().out
        assert (
            f"\n{name[:2]}  Retrieve: {script.PICK}" in said
            and "asks nothing" in said
            and "Nothing was asked" in said
        )

    @pytest.mark.parametrize("name", THREE)
    def test_help_works(self, monkeypatch, capsys, name):
        script = load(name)
        monkeypatch.setattr(sys, "argv", [name, "--help"])
        with pytest.raises(SystemExit) as left:
            script.main()
        assert left.value.code == 0
        said = capsys.readouterr().out
        assert (
            "--port" in said
            and "--question" in said
            and "--collection" in said
            and "--dry-run" in said
        )

    def test_the_links_are_the_dashboards_own_pages(self):
        source = (AZURE / "_retrieve.py").read_text(encoding="utf-8")
        start = source.index("    links(")
        shown = source[start : source.index("    finish(", start)]
        assert "/dashboard/#/" not in shown, "the custom dashboard serves its pages at the root"
        assert (
            'f"http://localhost:{port}/#/access"' in shown
            and 'f"http://localhost:{port}/#/overview"' in shown
        )

    def test_the_key_is_minted_with_a_key_and_never_with_nothing(self, folder, monkeypatch):
        sys.path.insert(0, str(AZURE))
        try:
            import _retrieve

            assert 'key=""' not in (AZURE / "_retrieve.py").read_text(encoding="utf-8")
            config = folder.settings()
            config["VX_API_KEY"] = "from-settings"
            assert _retrieve.server_key(folder.Az(pretend=True), config) == "from-settings"

            class Settings:
                pretend = False

                def __init__(self, held):
                    self.held, self.calls = held, []

                def __call__(self, *args, **_):
                    self.calls.append(args)
                    return self.held

            config = folder.settings()
            az = Settings([{"name": "VECTRIXDB_API_KEY", "value": "from-the-query-app"}])
            assert _retrieve.server_key(az, config) == "from-the-query-app"
            (asked,) = az.calls
            assert asked[asked.index("--name") + 1] == "vxtest1234-query", (
                "the query app's settings, read the way 06 reads a setting back"
            )
            with pytest.raises(SystemExit):
                _retrieve.server_key(Settings([]), config)
        finally:
            sys.path.remove(str(AZURE))

    def test_with_no_key_it_stops_and_says_what_to_set(self, folder, capsys):
        sys.path.insert(0, str(AZURE))
        try:
            import _retrieve

            class Nothing:
                pretend = False

                def __call__(self, *args, **_):
                    return None

            with pytest.raises(SystemExit):
                _retrieve.server_key(Nothing(), folder.settings())
            said = capsys.readouterr().err
            assert (
                "VX_API_KEY" in said and "VECTRIXDB_API_KEY" in said and "vxtest1234-query" in said
            )
        finally:
            sys.path.remove(str(AZURE))

    def test_the_minted_key_is_sent_as_the_api_key(self, folder, monkeypatch):
        sys.path.insert(0, str(AZURE))
        try:
            import _retrieve

            sent = {}

            def post(url, body, key, timeout=0):
                sent.update(url=url, body=body, key=key)
                return {"data": {"key": "minted"}}, 1.0

            monkeypatch.setattr(_retrieve, "post", post)
            config = folder.settings()
            config["VX_API_KEY"] = "from-settings"
            assert (
                _retrieve.key_for(folder.Az(pretend=True), config, "retrieve-best-for-time", 8000)
                == "minted"
            )
            assert sent == {
                "url": "http://localhost:8000/api/v1/keys",
                "body": {"name": "retrieve-best-for-time", "role": "operator"},
                "key": "from-settings",
            }
        finally:
            sys.path.remove(str(AZURE))


class TestNothingPersonalIsCommitted:
    """The committed sources and records are the walkthrough's, so they name nobody's folder and nobody's address."""

    def test_the_sources_carry_a_placeholder_and_not_a_persons_folder(self):
        text = (AZURE / ".local" / "sources.json").read_text(encoding="utf-8")
        assert "Downloads" not in text and "Users/" not in text and "/home/" not in text
        listed = json.loads(text)
        for batch in ("start", "later"):
            for entry in listed[batch]:
                if entry.get("path"):
                    assert entry["path"].startswith("<your folder>/"), entry["path"]

    def test_the_records_name_an_example_address(self):
        for record in sorted(
            (AZURE / ".local" / "cosmosdb" / "data_db" / "collection_records").glob("*.json")
        ):
            text = record.read_text(encoding="utf-8")
            assert "@" not in text or "you@example.com" in text, record.name
            assert "example.com" in text, record.name
        assert "you@example.com" in (AZURE / "README.md").read_text(encoding="utf-8")

    def test_04_says_a_placeholder_is_one_rather_than_missing(
        self, folder, tmp_path, monkeypatch, capsys
    ):
        script = load("04_push_local_to_blob_cosmosdb.py")
        monkeypatch.setattr(
            script, "INTO", {"start": tmp_path / "raw", "later": tmp_path / "later"}
        )
        monkeypatch.setattr(script, "LOCAL", tmp_path)
        listed = {
            "start": [
                {
                    "name": "office.png",
                    "path": "<your folder>/office.png",
                    "collection": "misc",
                    "what": "a photograph",
                }
            ],
            "later": [
                {
                    "name": "video.mp4",
                    "path": str(tmp_path / "nowhere" / "video.mp4"),
                    "collection": "media",
                    "what": "the trigger test",
                }
            ],
        }
        missing = script.gather(listed, check=False)
        said = capsys.readouterr().out
        assert missing == ["office.png", "video.mp4"]
        assert (
            "still the placeholder <your folder>/office.png" in said
            and "put your own file's path there" in said
        )
        assert "there is no " in said and "nowhere" in said, (
            "a path somebody pointed at and is not there is still missing"
        )
        assert "there is no <your folder>" not in said
        missing = script.gather(listed, check=True)
        assert "need office.png: a placeholder" in capsys.readouterr().out and missing == [
            "office.png",
            "video.mp4",
        ]
        assert (
            script.placeholder("<your folder>/x.png")
            and script.placeholder("<somewhere>/x")
            and not script.placeholder("~/x.png")
        )


class TestTheWheelLineIsPinnedToTheWheelThatIsThere:
    """requirements.txt said 2.2.0 by hand, so a bump of the library would publish the old wheel, or none, without a word."""

    def app(self, folder, tmp_path, monkeypatch, says="vectrixdb-2.2.0-py3-none-any.whl"):
        app = tmp_path / "extraction_app"
        app.mkdir(exist_ok=True)
        (app / "requirements.txt").write_text(
            f"azure-functions\n./{says}[api,ffmpeg]\npillow\n", encoding="utf-8"
        )
        for name, value in (
            ("EXTRACTION_APP", app),
            ("REPO", tmp_path),
            ("MAIN_FUNCTION_APP", tmp_path / "nowhere"),
        ):
            monkeypatch.setattr(folder, name, value)
        return app

    @staticmethod
    def wheel(where, name):
        where.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(where / name, "w") as made:
            made.writestr("vectrixdb/api/extraction.py", "def read_gateway_paths(value): ...")
            made.writestr(
                f"{name.split('-py3')[0]}.dist-info/METADATA",
                "Metadata-Version: 2.4\nName: vectrixdb\nProvides-Extra: api\nProvides-Extra: ffmpeg\n",
            )

    NEEDS = {"vectrixdb/api/extraction.py": "def read_gateway_paths"}

    def test_the_newest_wheel_in_dist_is_what_the_line_comes_to_say(
        self, folder, tmp_path, monkeypatch, capsys
    ):
        app = self.app(folder, tmp_path, monkeypatch)
        self.wheel(tmp_path / "dist", "vectrixdb-2.3.0-py3-none-any.whl")
        self.wheel(tmp_path / "dist", "vectrixdb-2.10.0-py3-none-any.whl")
        folder.wheel_ready(app, self.NEEDS)
        text = (app / "requirements.txt").read_text(encoding="utf-8")
        assert "./vectrixdb-2.10.0-py3-none-any.whl[api,ffmpeg]" in text and "2.2.0" not in text, (
            "by number, not by letter"
        )
        assert "azure-functions\n" in text and "\npillow\n" in text, (
            "the rest of the file is as it was"
        )
        said = capsys.readouterr().out
        assert (
            "$ pin extraction_app/requirements.txt to vectrixdb-2.10.0-py3-none-any.whl   (it said vectrixdb-2.2.0-py3-none-any.whl)"
            in said
        )
        assert (app / "vectrixdb-2.10.0-py3-none-any.whl").exists(), (
            "and that is the wheel copied in and checked"
        )

    def test_a_line_that_already_says_so_is_left_alone(self, folder, tmp_path, monkeypatch, capsys):
        app = self.app(folder, tmp_path, monkeypatch)
        self.wheel(tmp_path / "dist", "vectrixdb-2.2.0-py3-none-any.whl")
        before = (app / "requirements.txt").read_text(encoding="utf-8")
        folder.wheel_ready(app, self.NEEDS)
        assert (app / "requirements.txt").read_text(
            encoding="utf-8"
        ) == before and "$ pin" not in capsys.readouterr().out

    def test_with_no_dist_the_wheel_beside_the_app_is_the_one(self, folder, tmp_path, monkeypatch):
        app = self.app(folder, tmp_path, monkeypatch, says="vectrixdb-1.0.0-py3-none-any.whl")
        self.wheel(app, "vectrixdb-2.2.0-py3-none-any.whl")
        folder.wheel_ready(app, self.NEEDS)
        assert "./vectrixdb-2.2.0-py3-none-any.whl[api,ffmpeg]" in (
            app / "requirements.txt"
        ).read_text(encoding="utf-8")

    def test_with_no_wheel_anywhere_the_library_version_is_named(
        self, folder, tmp_path, monkeypatch
    ):
        app = self.app(folder, tmp_path, monkeypatch, says="vectrixdb-1.0.0-py3-none-any.whl")
        assert (
            folder.newest_wheel(app, "vectrixdb-1.0.0-py3-none-any.whl")
            == f"vectrixdb-{vectrixdb.__version__}-py3-none-any.whl"
        )

    def test_a_dry_run_says_what_it_would_pin_and_writes_nothing(
        self, folder, tmp_path, monkeypatch, capsys
    ):
        app = self.app(folder, tmp_path, monkeypatch)
        self.wheel(tmp_path / "dist", "vectrixdb-2.3.0-py3-none-any.whl")
        before = (app / "requirements.txt").read_text(encoding="utf-8")
        folder.wheel_ready(app, self.NEEDS, dry_run=True)
        assert (app / "requirements.txt").read_text(encoding="utf-8") == before
        said = capsys.readouterr().out
        assert (
            "$ pin extraction_app/requirements.txt to vectrixdb-2.3.0-py3-none-any.whl" in said
            and "$ copy" in said
        )

    def test_both_scripts_go_through_it(self):
        for script in ("05_create_extraction_function_app.py", "06_create_main_function_app.py"):
            assert "wheel_ready(" in (AZURE / script).read_text(encoding="utf-8"), script
        common = (AZURE / "_common.py").read_text(encoding="utf-8")
        assert common.index("fresh = newest_wheel(app, name)") < common.index(
            "here = app / name"
        ), "pinned before the copy looks for it"
