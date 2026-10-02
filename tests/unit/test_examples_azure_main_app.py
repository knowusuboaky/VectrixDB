"""The main function app's ten steps, run: a file waiting for its cut, the two picks, the cut applied, and a question answered.

``function_app.py`` needs the Functions runtime and three Azure SDKs to be
imported, and a test installs none of them, so it is loaded here against
small stand-ins for them. The evals container is a dictionary, the queue a
list, and the collections are real ones on this machine, embedded with a
fake. What is held to:

While no cut is in force a file is kept at step three and nothing is
indexed; a cut pinned is the cut used, and recorded so the cut applied knows
the document is done. A chunking run moves the cut only over checked
questions of every collection and only past luck, writes picks.json, and
asks for the cut to be applied. The cut applied brings every kept document
to it once, leaves one that will not cut to the queue's next try, hands the
rest on when its time is up, and asks step nine again. A retrieval run is
followed only over checked questions and unless a way is pinned, and keeps
the cut picked meanwhile. A question is handed what step five measured, with
where each part came from, and a collection with a policy is asked by
somebody. The routes say what is in force, and refuse what cannot be done
with how to put it right.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest

MAIN_FUNCTION_APP = Path(__file__).resolve().parents[2] / "examples" / "azure" / "main_function_app"
if not MAIN_FUNCTION_APP.exists():
    pytest.skip("examples/ is kept on the machine that runs it, not in the repository", allow_module_level=True)

ACCOUNT = "https://a.blob.core.windows.net"

TERMS = (
    "# Terms\n\nPayment is due within thirty days of the invoice date. "
    "Invoices are issued monthly and sent to the billing contact on file.\n\n"
    "## Late fees\n\nInterest accrues monthly on any overdue balance. "
    "A reminder is sent after ten days and a second one after twenty.\n"
)
COVENANTS = (
    "# Covenants\n\nThe leverage covenant is tested every quarter against the trailing year. "
    "A breach is cured by an equity injection within fifteen business days.\n"
)


def embed(texts):
    out = np.zeros((len(texts), 8), dtype=np.float32)
    for i, text in enumerate(texts):
        for word in text.lower().split():
            out[i, hash(word) % 8] += 1.0
        out[i] /= np.linalg.norm(out[i]) or 1.0
    return out


class Evals:
    """The evals container: a blob client for each name, over what it holds."""

    def __init__(self):
        self.held = {}

    def get_blob_client(self, name):
        held = self.held

        class Blob:
            url = f"{ACCOUNT}/evals/{name}"

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


class Queue:
    """The ingest queue: every message sent, read back as what it says."""

    def __init__(self):
        self.sent = []

    def send_message(self, body, **kw):
        self.sent.append(json.loads(body))
        self.last_kw = kw


def stand_ins():
    """Modules for what function_app imports and a test does not have: the Functions runtime, azure-identity, azure-storage-blob."""
    functions = types.ModuleType("azure.functions")

    class AsgiFunctionApp:
        def __init__(self, app, http_auth_level):
            self.asgi = app

        def queue_trigger(self, **binding):
            return lambda handler: handler

    functions.AsgiFunctionApp = AsgiFunctionApp
    functions.AuthLevel = types.SimpleNamespace(ANONYMOUS="anonymous")
    functions.QueueMessage = object
    identity = types.ModuleType("azure.identity")
    identity.DefaultAzureCredential = lambda: None
    storage = types.ModuleType("azure.storage")
    storage.__path__ = []
    blob = types.ModuleType("azure.storage.blob")
    blob.BlobServiceClient = lambda *a, **k: None
    modules = {"azure.functions": functions, "azure.identity": identity, "azure.storage": storage, "azure.storage.blob": blob}
    if importlib.util.find_spec("azure") is None:
        # No Azure package at all, as in a job that installs only dev, api and signin: the namespace too.
        namespace = types.ModuleType("azure")
        namespace.__path__ = []
        modules = {"azure": namespace, **modules}
    return modules


@pytest.fixture
def app(monkeypatch, tmp_path):
    """function_app loaded against the stand-ins, with the evals container and the queue as fakes."""
    for name, module in stand_ins().items():
        monkeypatch.setitem(sys.modules, name, module)
    settings = {
        "INGEST_COLLECTIONS": "financial,media",
        "INGEST_QUEUE": "ingest",
        "INGEST_PATH": str(tmp_path / "scratch"),
        "VECTRIXDB_PATH": str(tmp_path / "api"),
        "VECTRIXDB_STORAGE_BACKEND": "azure_search",
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    for name in (
        "INGEST_CHUNKING", "RETRIEVAL_SETUP", "VECTRIXDB_EXTRACTOR_URL", "VECTRIXDB_WRITER_URL", "VECTRIXDB_WRITER_MODEL",
        "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_WRITER_DEPLOYMENT", "AZURE_OPENAI_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    # The API is the library's and tested there; here it only has to fail to be built, as the app allows.
    import vectrixdb.api.server as server

    def no_api(**_):
        raise RuntimeError("no API in these tests")

    monkeypatch.setattr(server, "create_app", no_api)
    monkeypatch.syspath_prepend(str(MAIN_FUNCTION_APP))
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    spec = importlib.util.spec_from_file_location("function_app_under_test", MAIN_FUNCTION_APP / "function_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from test_documents import FakeBlobService

    evals, queue, blobs = Evals(), Queue(), FakeBlobService()
    folder = module.GoldenFolder(evals)
    monkeypatch.setattr(module, "_golden_folder", lambda: folder)
    monkeypatch.setattr(module, "_queue", lambda: queue)
    monkeypatch.setattr(module, "_blobs", lambda: blobs)
    opened = {}
    monkeypatch.setattr(module, "collection", lambda name: opened[name])
    here = types.SimpleNamespace(module=module, folder=folder, evals=evals, queue=queue, blobs=blobs, opened=opened, tmp=tmp_path)
    yield here
    for db in opened.values():
        db.close()


def keeping(here, name, **options):
    """A collection on this machine that keeps its Markdown, as step three does, opened as the app's own."""
    from vectrixdb import Vectrix
    from vectrixdb.documents import DocumentStore, LocalFiles

    db = Vectrix(
        name, path=str(here.tmp / name), embed_fn=embed, dimension=8, mode="hybrid",
        keep_source=DocumentStore(LocalFiles(here.tmp / f"{name}-markdown")), markdown_first=True, **options,
    )
    here.opened[name] = db
    return db


def build(key, outcomes, chunks=100):
    """One build as a run makes it: its key, and each question's outcome, [found, answered right]."""
    from vectrixdb.evaluation import chunking_build

    return {
        **chunking_build(key),
        "questions": len(outcomes),
        "chunks": chunks,
        "found": sum(1 for o in outcomes.values() if o[0]),
        "answered": sum(1 for o in outcomes.values() if o[1]),
        "outcomes": outcomes,
        "error": None,
    }


def right(first, last):
    """Questions q<first> to q<last - 1> answered right, every other of 100 missed."""
    return {f"q{i}": [first <= i < last, first <= i < last] for i in range(100)}


class TestAFileWaitsForItsCut:
    def test_with_no_cut_in_force_it_is_kept_and_nothing_is_indexed(self, app):
        from vectrixdb.worker import IngestEvent

        db = keeping(app, "media")
        app.blobs.blobs[("ingestion", "raw/media/terms.md")] = TERMS.encode()
        cut = app.module.chunking()
        assert cut == {"index": False}, "auto, and no run has picked a cut"
        outcome = app.module.worker("media", cut).handle(IngestEvent("created", f"{ACCOUNT}/ingestion/raw/media/terms.md", version="0x1"))
        assert outcome.chunks == 0 and db.count() == 0
        entry = db.documents.entry("terms.md")
        assert entry["user_metadata"]["collection"] == "media" and "chunking" not in entry, "kept, waiting to be cut"

    def test_a_pinned_cut_is_used_and_recorded_the_way_the_cut_applied_reads_it(self, app, monkeypatch):
        from vectrixdb.evaluation import chunking_options
        from vectrixdb.worker import IngestEvent

        monkeypatch.setenv("INGEST_CHUNKING", "markdown-200-n")
        db = keeping(app, "media")
        app.blobs.blobs[("ingestion", "raw/media/terms.md")] = TERMS.encode()
        cut = app.module.chunking()
        assert cut == chunking_options("markdown-200-n") and app.evals.held == {}, "a pin reads no picks.json"
        app.module.worker("media", cut).handle(IngestEvent("created", f"{ACCOUNT}/ingestion/raw/media/terms.md", version="0x1"))
        assert db.count() > 0
        assert db.documents.entry("terms.md")["chunking"] == app.module.recorded_cut("markdown-200-n"), "so the cut applied leaves it alone"

    def test_the_worker_is_made_again_when_the_cut_changes(self, app):
        from vectrixdb.evaluation import chunking_options

        keeping(app, "media")
        waiting = app.module.worker("media", {"index": False})
        assert app.module.worker("media", {"index": False}) is waiting
        cutting = app.module.worker("media", chunking_options("markdown-200-n"))
        assert cutting is not waiting and cutting.options["chunk_size"] == 200


class TestStepFiveMovesTheCut:
    def test_the_first_run_over_checked_questions_picks_and_asks_for_it_to_be_applied(self, app):
        results = [build("markdown-1000-h", right(0, 60)), build("fixed-500-n", right(0, 40))]
        said = app.module._pick_a_cut(app.folder, results, {"id": "run-1", "questions": 100}, drafts=0, compared=["financial", "media"])
        assert (said["state"], said["key"], said["was"]) == ("switched", "markdown-1000-h", None)
        cut = app.module.the_cut()
        assert (cut["key"], cut["by"], cut["run"]) == ("markdown-1000-h", "auto", "run-1")
        assert app.queue.sent == [{"vectrixdb": "apply", "key": "markdown-1000-h", "collections": ["financial", "media"]}]
        assert app.folder.read_status(app.module.APPLY_STATUS)["state"] == "asked"

    def test_a_win_luck_could_make_keeps_the_cut_in_force(self, app):
        app.folder.write_picks(app.module.with_chunking_pick({}, key="markdown-1000-h", run="run-1", why="the best", questions=100))
        results = [build("recursive-500-n", right(0, 52)), build("markdown-1000-h", right(0, 50))]
        said = app.module._pick_a_cut(app.folder, results, {"id": "run-2", "questions": 100}, drafts=0, compared=["financial", "media"])
        assert said["state"] == "kept" and "luck" in said["why"]
        assert app.module.the_cut()["key"] == "markdown-1000-h" and app.queue.sent == []

    @pytest.mark.parametrize(
        "drafts, compared, pinned, state",
        [(3, ["financial", "media"], None, "not moved"), (0, ["media"], None, "not moved"), (0, ["financial", "media"], "fixed-500-n", "pinned")],
        ids=["drafts", "a collection left out", "a cut pinned"],
    )
    def test_nothing_else_moves_it(self, app, monkeypatch, drafts, compared, pinned, state):
        if pinned:
            monkeypatch.setenv("INGEST_CHUNKING", pinned)
        results = [build("markdown-1000-h", right(0, 90)), build("fixed-500-n", right(0, 10))]
        said = app.module._pick_a_cut(app.folder, results, {"id": "run-3", "questions": 100}, drafts=drafts, compared=compared)
        assert said["state"] == state and said["why"]
        assert "picks.json" not in app.evals.held and app.queue.sent == []

    def test_a_pick_is_kept_when_applying_it_cannot_be_asked_for(self, app, monkeypatch):
        def down():
            raise ConnectionError("the queue is not there")

        monkeypatch.setattr(app.module, "_queue", down)
        said = app.module._pick_a_cut(app.folder, [build("markdown-1000-h", right(0, 60))], {"id": "run-4"}, drafts=0, compared=["financial", "media"])
        assert said["state"] == "switched" and "POST /api/v1/chunking/apply" in said["apply"]
        assert app.module.the_cut()["key"] == "markdown-1000-h"


class TestTheCutApplied:
    def test_every_kept_document_is_cut_once_then_step_nine_is_asked_again(self, app):
        media, financial = keeping(app, "media"), keeping(app, "financial")
        media.add_document(TERMS, doc_id="terms.md", index=False)
        media.add_document(COVENANTS, doc_id="covenants.md", index=False)
        financial.add_document(COVENANTS, doc_id="td/covenants.md", metadata={"collection": "financial"}, index=False)
        app.evals.held[app.module.GOLDEN] = b'{"id": "q1", "question": "When is payment due?", "expected": ["terms.md"]}\n'
        app.module.apply_the_cut({"key": "markdown-200-n", "collections": ["financial", "media"]})
        wanted = app.module.recorded_cut("markdown-200-n")
        for db, ids in ((media, ["covenants.md", "terms.md"]), (financial, ["td/covenants.md"])):
            assert db.count() > 0
            for doc_id in ids:
                assert db.documents.entry(doc_id)["chunking"] == wanted
        assert all(m.get("collection") == "financial" for _, _, m in financial._collection._iter_documents_raw()), "the metadata it came with"
        done = app.folder.read_status(app.module.APPLY_STATUS)
        assert (done["state"], done["cut"]) == ("done", 3)
        assert app.queue.sent == [{"vectrixdb": "evaluate", "golden": f"{ACCOUNT}/evals/{app.module.GOLDEN}", "collections": ["financial", "media"]}]
        app.module.apply_the_cut({"key": "markdown-200-n", "collections": ["financial", "media"]})
        assert app.folder.read_status(app.module.APPLY_STATUS)["cut"] == 0, "handed over twice, it cuts nothing twice"

    def test_one_document_that_will_not_cut_leaves_the_others_and_goes_round_again(self, app, monkeypatch):
        media = keeping(app, "media")
        media.add_document(TERMS, doc_id="terms.md", index=False)
        media.add_document(COVENANTS, doc_id="covenants.md", index=False)
        real = media.rechunk

        def rechunk(doc_id, **options):
            if doc_id == "covenants.md":
                raise RuntimeError("the index is down")
            return real(doc_id, **options)

        monkeypatch.setattr(media, "rechunk", rechunk)
        with pytest.raises(RuntimeError, match="1 documents did not cut"):
            app.module.apply_the_cut({"key": "markdown-200-n", "collections": ["media"]})
        assert media.documents.entry("terms.md")["chunking"] == app.module.recorded_cut("markdown-200-n")
        said = app.folder.read_status(app.module.APPLY_STATUS)
        assert said["state"] == "failed" and said["failed"][0].startswith("media/covenants.md: RuntimeError")
        assert app.queue.sent == [], "step nine waits for every document"

    def test_out_of_time_it_hands_the_rest_to_the_next_message(self, app, monkeypatch):
        media = keeping(app, "media")
        media.add_document(TERMS, doc_id="terms.md", index=False)
        monkeypatch.setattr(app.module, "APPLY_SECONDS", -1)
        app.module.apply_the_cut({"key": "markdown-200-n", "collections": ["media"]})
        assert app.queue.sent == [{"vectrixdb": "apply", "key": "markdown-200-n", "collections": ["media"]}]
        assert app.folder.read_status(app.module.APPLY_STATUS)["state"] == "running" and media.count() == 0

    def test_a_cut_that_needs_a_model_with_none_named_is_said_and_not_tried_again(self, app):
        keeping(app, "media").add_document(TERMS, doc_id="terms.md", index=False)
        app.module.apply_the_cut({"key": "llm-1000-h", "collections": ["media"]})
        said = app.folder.read_status(app.module.APPLY_STATUS)
        assert said["state"] == "failed" and "AZURE_OPENAI_WRITER_DEPLOYMENT" in said["message"]


class TestStepNineIsFollowed:
    REPORT = {
        "id": "run-9",
        "setups": [
            {"key": "media.hybrid.bge", "search": {"mode": "hybrid", "rerank": False}, "method_label": "Hybrid"},
            {"key": "media.dense.bge", "search": {"mode": "dense"}, "method_label": "Dense"},
        ],
        "picks": {"finds_the_most": "media.hybrid.bge", "best_for_balance": "media.hybrid.bge", "best_for_time": "media.dense.bge"},
    }

    def test_a_run_over_checked_questions_is_followed_and_the_cut_is_kept(self, app):
        app.folder.write_picks(app.module.with_chunking_pick({}, key="markdown-1000-h", run="run-1", why="the best", questions=100))
        said = app.module._pick_a_way(app.folder, "media", self.REPORT, questions=40, drafts=0)
        assert (said["state"], said["role"]) == ("followed", "best_for_balance")
        way = app.module.the_way("media")
        assert (way["search"], way["setup"], way["run"]) == ({"mode": "hybrid", "rerank": False}, "media.hybrid.bge", "run-9")
        assert app.module.the_cut()["key"] == "markdown-1000-h", "written into the picks as they are, so the cut stays"

    def test_a_pick_by_name_follows_that_one(self, app, monkeypatch):
        monkeypatch.setenv("RETRIEVAL_SETUP", "best_for_time")
        app.module._pick_a_way(app.folder, "media", self.REPORT, questions=40, drafts=0)
        assert app.module.the_way("media")["search"] == {"mode": "dense"}

    @pytest.mark.parametrize("drafts, pinned, state", [(5, None, "not moved"), (0, "hybrid_semantic.both", "pinned")], ids=["drafts", "a way pinned"])
    def test_drafts_or_a_pin_are_not_followed(self, app, monkeypatch, drafts, pinned, state):
        if pinned:
            monkeypatch.setenv("RETRIEVAL_SETUP", pinned)
        assert app.module._pick_a_way(app.folder, "media", self.REPORT, questions=40, drafts=drafts)["state"] == state
        assert "picks.json" not in app.evals.held

    def test_with_no_run_yet_it_searches_the_way_step_five_measured(self, app):
        way = app.module.the_way("media")
        assert way["search"] is None and "step five" in way["why"]


class TestStepTenAnswers:
    def cut(self, app, monkeypatch, db, key="markdown-200-n"):
        from vectrixdb.evaluation import chunking_options

        monkeypatch.setenv("INGEST_CHUNKING", key)
        for doc_id, text in (("terms.md", TERMS), ("covenants.md", COVENANTS)):
            db.add_document(text, doc_id=doc_id, **chunking_options(key))

    def test_with_no_cut_in_force_there_is_nothing_to_answer_from(self, app):
        with pytest.raises(app.module.NothingIsCut, match="no chunking run"):
            app.module.retrieve("media", "When is payment due?")

    def test_it_is_handed_what_step_five_measured_with_where_each_part_came_from(self, app, monkeypatch):
        from vectrixdb.evaluation import handed_over

        db = keeping(app, "media")
        self.cut(app, monkeypatch, db)
        said = app.module.retrieve("media", "When is payment due?")
        measured = handed_over(db, "When is payment due?", "markdown-200-n")
        assert [s["text"] for s in said["sources"]] == [text for _, text in measured] and said["sources"]
        assert sum(len(s["text"]) for s in said["sources"]) <= app.module.retrieval_rules()["budget"]
        first = said["sources"][0]
        assert first["doc"] in ("terms.md", "covenants.md") and first["citation"] and first["cited_as"]
        assert said["answer"] is None and "AZURE_OPENAI_WRITER_DEPLOYMENT" in said["no_answer"]
        assert said["searched"]["cut"] == {"key": "markdown-200-n", "by": "pinned", "run": None}
        assert said["searched"]["way"]["by"] == "auto" and "step five" in said["searched"]["way"]["why"]

    def test_the_model_answers_from_what_was_handed_over_and_nothing_else(self, app, monkeypatch):
        db = keeping(app, "media")
        self.cut(app, monkeypatch, db)
        asked = []

        def chat(messages):
            asked.append(messages[-1]["content"])
            return '{"answer": "Within thirty days of the invoice date."}'

        monkeypatch.setattr(app.module, "golden_writer", lambda: chat)
        said = app.module.retrieve("media", "When is payment due?")
        assert said["answer"] == "Within thirty days of the invoice date." and "no_answer" not in said
        assert all(s["text"] in asked[0] for s in said["sources"]), "every part handed over is in what the model read"

    def test_a_pinned_way_of_searching_is_the_way_searched(self, app, monkeypatch):
        db = keeping(app, "media")
        self.cut(app, monkeypatch, db)
        monkeypatch.setenv("RETRIEVAL_SETUP", "dense")
        assert app.module.retrieve("media", "late fees")["searched"]["way"] == {"setup": "dense", "by": "pinned", "search": {"mode": "dense"}, "why": "RETRIEVAL_SETUP names it"}


class Poison:
    """The poison queue: what sits there, peeked or received, and what a person deleted."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.deleted = []

    def peek_messages(self, max_messages=32):
        return list(self.messages[:max_messages])

    def receive_messages(self, messages_per_page=32, visibility_timeout=30):
        return list(self.messages[:messages_per_page])

    def delete_message(self, message):
        self.messages = [m for m in self.messages if m.id != message.id]
        self.deleted.append(message.id)


def poisoned(message_id, collection, file, tries=5):
    """One message as Event Grid wrote it and the host moved it: the blob event of a file under raw/<collection>/."""
    url = f"{ACCOUNT}/ingestion/raw/{collection}/{file}"
    event = {"eventType": "Microsoft.Storage.BlobCreated", "subject": f"/blobServices/default/containers/ingestion/blobs/raw/{collection}/{file}", "data": {"url": url, "eTag": "0x1", "contentLength": 12}}
    return types.SimpleNamespace(id=message_id, content=json.dumps(event), dequeue_count=tries, inserted_on=None)


class TestTheRoutes:
    def client(self, app):
        pytest.importorskip("fastapi")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        api = FastAPI()
        app.module._ours(api)
        return TestClient(api)

    def test_the_files_that_would_not_read_are_listed_in_words_and_retried_or_dropped(self, app, monkeypatch):
        import dead_letters

        poison = Poison([poisoned("m1", "financial", "td/scan-0417.pdf"), poisoned("m2", "media", "town-hall.mp4")])
        monkeypatch.setattr(app.module, "_poison_queue", lambda: poison)
        blobs = app.blobs
        for folder, rel in (("raw", "td/scan-0417.pdf"), ("markdown", "td/scan-0417.pdf.md"), ("chunks", "td/scan-0417.pdf.jsonl")):
            blobs.get_blob_client("ingestion", f"{folder}/financial/{rel}").upload_blob(b"x", overwrite=True)
        # The worker's record of the failure, as _note_outcome writes it on each try.
        outcome = types.SimpleNamespace(uri=f"{ACCOUNT}/ingestion/raw/financial/td/scan-0417.pdf", action="failed", error="Document Intelligence read 0 of 12 pages")
        dead_letters.record_failure(blobs, outcome, 5, now=0)
        client = self.client(app)
        listed = client.get("/api/v1/ingest/failed").json()
        assert listed["total"] == 2 and listed["queue"] == "ingest-poison" and [r["file"] for r in listed["files"]] == ["td/scan-0417.pdf", "town-hall.mp4"], listed
        first = listed["files"][0]
        assert first["collection"] == "financial" and first["tries"] == 5 and first["last_tried"] == "1970-01-01T00:00:00Z"
        assert first["said"] == "Nothing could be read from it" and "OCR found nothing" in first["why"] and first["error"].startswith("Document Intelligence")
        second = listed["files"][1]
        assert second["said"] == "read failed" and second["tries"] == 5, "a message with no record still says what the queue knows"
        assert client.get("/api/v1/ingest/failed?limit=1&offset=1").json()["files"][0]["id"] == "m2", "ten a page, from an offset"
        # Retry: the message goes back on the ingest queue as it was.
        assert client.post("/api/v1/ingest/failed/m2/retry").json()["retried"] == "town-hall.mp4"
        assert app.queue.sent[-1]["data"]["url"].endswith("/raw/media/town-hall.mp4") and poison.deleted == ["m2"]
        # Drop: the file, its Markdown, its chunks and its record go, and the message with them.
        dropped = client.post("/api/v1/ingest/failed/m1/drop").json()
        assert dropped == {"dropped": "td/scan-0417.pdf", "collection": "financial", "gone": {"raw": 1, "markdown": 1, "chunks": 1, "failed": 1}}
        assert not [b for c, b in blobs.blobs if "scan-0417" in b] and poison.deleted == ["m2", "m1"]
        assert client.post("/api/v1/ingest/failed/m1/drop").status_code == 404, "gone is gone"
        assert client.get("/api/v1/ingest/failed").json() == {"files": [], "total": 0, "offset": 0, "queue": "ingest-poison"}

    def test_the_ingest_app_answers_health_and_its_own_routes_and_nothing_a_query_waits_on(self, app, monkeypatch):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        monkeypatch.setenv("INGEST_ROLE", "ingest")
        api = app.module._api()
        client = TestClient(api)
        assert client.get("/health").json() == {"status": "ok", "role": "ingest"}
        wired = client.get("/health/wiring").json()
        assert wired["role"] == "ingest" and wired["status"] == "ok"
        assert client.get("/api/v1/collections").status_code == 404, "the library's API is the query app's"
        assert client.get("/api/v1/ingest/state").status_code == 200, "its own routes are here"
        monkeypatch.setenv("INGEST_ROLE", "sideways")
        assert app.module.role() == "both", "an unknown role runs as both, with a warning"

    def test_the_query_app_never_reads_a_file_and_gives_the_message_back(self, app, monkeypatch):
        monkeypatch.setenv("INGEST_ROLE", "query")
        message = types.SimpleNamespace(id="m1", dequeue_count=1, get_body=lambda: b'{"eventType": "Microsoft.Storage.BlobCreated", "data": {"url": "x"}}')
        app.module.ingest_one(message)
        assert app.queue.sent == [{"eventType": "Microsoft.Storage.BlobCreated", "data": {"url": "x"}}] and app.queue.last_kw == {"visibility_timeout": 60}

    def test_ingestion_says_whether_it_is_paused(self, app):
        import breaker

        client = self.client(app)
        assert client.get("/api/v1/ingest/state").json()["ingestion"]["paused"] is False
        gate = breaker.Breaker(breaker.state_files(app.blobs), "extraction app", threshold=1, open_for=300, clock=lambda: 1000.0)
        gate.record_failure("/extract/pdf could not be reached: refused")
        said = client.get("/api/v1/ingest/state").json()
        assert said["queue"] == "ingest" and said["ingestion"]["paused"] is True and said["ingestion"]["since"] == "1970-01-01T00:16:40Z"

    def test_every_route_reads_request_as_the_request_not_as_a_query(self, app):
        """Postponed annotations and a FastAPI imported inside _ours made request: Request a required query parameter: a 422 on every call."""
        pytest.importorskip("fastapi")
        from fastapi import FastAPI

        api = FastAPI()
        app.module._ours(api)
        asked = {route.path: [p.name for p in route.dependant.query_params] for route in api.routes if hasattr(route, "dependant")}
        assert asked and not [path for path, names in asked.items() if "request" in names], asked

    def test_a_question_of_a_collection_nothing_answers_to_is_not_found(self, app):
        reply = self.client(app).post("/api/v1/collections/nowhere/search/answer", json={"question": "anything"})
        assert reply.status_code == 404

    def test_with_no_cut_in_force_a_question_is_refused_with_how_to_get_one(self, app):
        reply = self.client(app).post("/api/v1/collections/media/search/answer", json={"question": "When is payment due?"})
        assert reply.status_code == 409 and "POST /api/v1/chunking/run" in reply.json()["message"]

    def test_a_question_is_answered_with_its_sources(self, app, monkeypatch):
        from vectrixdb.evaluation import chunking_options

        db = keeping(app, "media")
        monkeypatch.setenv("INGEST_CHUNKING", "markdown-200-n")
        db.add_document(TERMS, doc_id="terms.md", **chunking_options("markdown-200-n"))
        reply = self.client(app).post("/api/v1/collections/media/search/answer", json={"question": "When is payment due?"})
        assert reply.status_code == 200 and reply.json()["sources"][0]["doc"] == "terms.md"

    def test_the_picks_say_what_is_in_force_and_why(self, app):
        said = self.client(app).get("/api/v1/picks").json()
        assert said["chunking"]["setting"] == "auto" and said["chunking"]["key"] is None and "step three" in said["chunking"]["why"]
        assert set(said["retrieval"]) == {"financial", "media"} and said["retrieval"]["media"]["role"] == "best_for_balance"

    def test_a_setting_that_names_nothing_is_the_servers_to_put_right(self, app, monkeypatch):
        monkeypatch.setenv("INGEST_CHUNKING", "chapters-1000-h")
        reply = self.client(app).get("/api/v1/picks")
        assert reply.status_code == 500 and "markdown-1000-h" in reply.json()["message"], "it says what a build is"

    def test_applying_asks_for_the_cut_in_force_once_at_a_time(self, app, monkeypatch):
        client = self.client(app)
        assert client.post("/api/v1/chunking/apply").status_code == 409, "no cut in force yet"
        monkeypatch.setenv("INGEST_CHUNKING", "markdown-200-n")
        assert client.post("/api/v1/chunking/apply").status_code == 202
        assert app.queue.sent == [{"vectrixdb": "apply", "key": "markdown-200-n", "collections": ["financial", "media"]}]
        assert client.post("/api/v1/chunking/apply").status_code == 409, "one at a time"
        assert client.get("/api/v1/chunking/apply/status").json()["state"] == "asked"

    def test_the_wiring_says_what_the_picks_are_set_to(self, app, monkeypatch):
        monkeypatch.setenv("RETRIEVAL_SETUP", "best_for_time")
        assert self.client(app).get("/health/wiring").json()["picks"] == {"INGEST_CHUNKING": "auto", "RETRIEVAL_SETUP": "best_for_time"}


class TestTheWalkthroughSetsThePicks:
    def common(self, monkeypatch):
        monkeypatch.syspath_prepend(str(MAIN_FUNCTION_APP.parent))
        import _common

        return _common

    def test_auto_unless_settings_env_says_otherwise(self, monkeypatch):
        common = self.common(monkeypatch)
        assert common.picks_of({}) == {"INGEST_CHUNKING": "auto", "RETRIEVAL_SETUP": "auto"}
        wired = common.env_of({"VX_COLLECTIONS": "financial,media", "INGEST_CHUNKING": "parent-500-h", "RETRIEVAL_SETUP": "best_for_time"}, {}, {})
        assert (wired["INGEST_CHUNKING"], wired["RETRIEVAL_SETUP"]) == ("parent-500-h", "best_for_time")

    @pytest.mark.parametrize("setting, value", [("INGEST_CHUNKING", "chapters-1000-h"), ("RETRIEVAL_SETUP", "keyword.both")])
    def test_a_pin_that_names_nothing_stops_before_the_app_is_published(self, monkeypatch, capsys, setting, value):
        common = self.common(monkeypatch)
        with pytest.raises(SystemExit):
            common.picks_of({setting: value})
        assert "settings.env" in capsys.readouterr().err


class TestTheDashboardsOwnFlows:
    """Making a collection from the dashboard, the way the app makes one: its policy first, then its files."""

    HR = {"method": "token", "allow": [{"id": "00000000-0000-0000-0000-000000000101", "name": "HR"}], "people": [{"email": "ama@company.com"}]}

    @pytest.fixture
    def flows(self, app, monkeypatch):
        pytest.importorskip("fastapi")
        import collections_of
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from vectrixdb.collection_records import CollectionRecords
        from vectrixdb.signin.records import SqlRecords

        records = CollectionRecords(SqlRecords.sqlite(app.tmp / "collections.db"), fresh_for=0)
        monkeypatch.setattr(app.module, "_collection_store", lambda: records)
        monkeypatch.setattr(collections_of, "_records", records)
        api = FastAPI()
        app.module._ours(api)
        return types.SimpleNamespace(client=TestClient(api), records=records, app=app)

    def test_a_collection_is_made_with_its_policy_first_and_is_then_one_of_the_collections(self, flows):
        assert flows.client.post("/api/v1/collections/Legal_Memos/setup", json={"policy": self.HR}).status_code == 400, "a name is lowercase letters, digits and dashes"
        no_policy = flows.client.post("/api/v1/collections/legal/setup", json={})
        assert no_policy.status_code == 400 and "required" in no_policy.json()["message"]
        made = flows.client.post("/api/v1/collections/legal/setup", json={"policy": self.HR})
        assert made.status_code == 200, made.text
        assert made.json() == {**made.json(), "name": "legal", "path": "raw/legal/", "policy": self.HR, "status": "/api/v1/collections/legal/setup/status"}
        record = flows.records.get("legal")
        assert record.policy_object().describe() == "security group HR, narrowed to 1 person on the list" and record.path == "raw/legal/"
        assert flows.client.post("/api/v1/collections/legal/setup", json={"policy": self.HR}).status_code == 409
        assert "legal" in flows.app.module.named(), "known from its record, with no line in INGEST_COLLECTIONS"
        assert flows.app.module.collection_of("https://acct.blob.core.windows.net/ingestion/raw/legal/nda.pdf") == "legal"

    def test_files_go_to_raw_and_typed_text_becomes_markdown(self, flows):
        assert flows.client.post("/api/v1/collections/legal/files", content=b"x", headers={"X-Filename": "nda.pdf"}).status_code == 404, "no record, no collection"
        flows.client.post("/api/v1/collections/legal/setup", json={"policy": self.HR})
        dropped = flows.client.post("/api/v1/collections/legal/files", content=b"%PDF-1.7 ...", headers={"X-Filename": "../q2 memo.pdf", "Content-Type": "application/octet-stream"})
        assert dropped.status_code == 200 and dropped.json()["blob"] == "raw/legal/q2 memo.pdf", "the name, cleaned of any path"
        typed = flows.client.post("/api/v1/collections/legal/files", json={"title": "Board minutes: Q2", "text": "# Minutes\n\nApproved."})
        assert typed.status_code == 200 and typed.json()["file"] == "board-minutes-q2.md"
        assert flows.app.blobs.blobs[("ingestion", "raw/legal/board-minutes-q2.md")] == b"# Minutes\n\nApproved."
        assert flows.client.post("/api/v1/collections/legal/files", content=b"", headers={"X-Filename": "empty.pdf"}).status_code == 400
        assert flows.client.post("/api/v1/collections/legal/files", content=b"x").status_code == 400, "a file needs a name"
        assert flows.app.module.collection_setup.files_of(flows.app.blobs, "legal") == ["board-minutes-q2.md", "q2 memo.pdf"]

    def test_the_status_walks_the_steps_the_spinner_shows(self, flows, monkeypatch):
        from golden_files import CHUNKING_STATUS, EVALUATION_STATUS

        status = lambda: flows.client.get("/api/v1/collections/legal/setup/status").json()  # noqa: E731
        assert status()["step"] == "Policy" and "no record" in status()["line"]
        flows.client.post("/api/v1/collections/legal/setup", json={"policy": self.HR})
        assert status() == {**status(), "step": "Policy", "files": 0, "ready": False}
        flows.client.post("/api/v1/collections/legal/files", content=b"x", headers={"X-Filename": "nda.pdf"})
        assert status()["step"] == "Reading" and "0 of 1 files read" in status()["line"]
        kept = flows.app.module.written_to(flows.app.blobs, "legal")["keep_source"]
        from vectrixdb.ingest import markdown_document

        kept.put("nda.pdf", markdown_document("# NDA\n\nTerms."), source_version="etag-1")
        assert status()["step"] == "Markdown", "read, and waiting for a cut; nothing says it was masked"
        masked = markdown_document("# NDA\n\nCall [PHONE].")
        masked.metadata["masking"] = {"counts": {"phone": 1, "email": 2}, "score": 0.6, "engine": "language", "language": "en", "regex_only": False}
        kept.put("nda.pdf", masked, source_version="etag-2")
        said = status()
        assert said["step"] == "Masking" and said["line"].startswith("3 identifiers masked in 1 of 1 file before anything was kept: 2 emails, 1 phone numbers. The index never holds them.")
        assert said["masking"] == {"files": 1, "masked": 1, "total": 3, "counts": {"phone": 1, "email": 2}, "patterns_only": 0, "words": said["masking"]["words"]}
        monkeypatch.setenv("INGEST_CHUNKING", "markdown-200-n")
        assert status()["step"] == "Chunks"
        db = keeping(flows.app, "legal")
        from vectrixdb.evaluation import chunking_options

        db.add_document("# NDA\n\nTerms of the agreement.", doc_id="nda.pdf", **chunking_options("markdown-200-n"))
        flows.app.module._open["legal"] = db  # opened, as the app opens one on its first file
        flows.app.folder.status("running", into=CHUNKING_STATUS, build=21, of=49)
        said = status()
        assert said["step"] == "Chunking compared" and "Build 21 of 49" in said["line"] and said["chunks"] >= 1
        flows.app.folder.status("done", into=CHUNKING_STATUS)
        flows.app.folder.status("failed", into=EVALUATION_STATUS, message="the golden file could not be read")
        assert status()["failed"] is True and "Retrieval compared failed" in status()["line"]
        flows.app.folder.status("done", into=EVALUATION_STATUS)
        assert status()["ready"] is True and status()["step"] == "Ready" and "security group HR" in status()["line"]
        assert status()["line"].endswith("Nothing in the index holds an identifier.")

    def test_deleting_takes_everything_and_keeps_no_copy(self, flows):
        flows.client.post("/api/v1/collections/legal/setup", json={"policy": self.HR})
        flows.client.post("/api/v1/collections/legal/files", content=b"x", headers={"X-Filename": "nda.pdf"})
        blobs = flows.app.blobs.blobs
        blobs[("ingestion", "markdown/legal/nda.pdf.md")] = b"# NDA"
        blobs[("ingestion", "chunks/legal/nda.pdf.jsonl")] = b"{}"
        blobs[("ingestion", "raw/media/keep.wav")] = b"..."
        flows.app.module._open["legal"] = keeping(flows.app, "legal")
        gone = flows.client.post("/api/v1/collections/legal/delete")
        assert gone.status_code == 200, gone.text
        said = gone.json()
        assert said["deleted"] is True and said["removed"] == {"raw": 1, "markdown": 1, "chunks": 1}
        assert [s["step"] for s in said["steps"]] == ["New files refused", "Documents removed", "Index removed", "Records removed", "Taken off the list", "Recorded in audit"]
        assert flows.records.get("legal") is None and "legal" not in flows.app.module._open
        assert [b for c, b in blobs if b.startswith("raw/legal/") or b.startswith("markdown/legal/") or b.startswith("chunks/legal/")] == []
        assert ("ingestion", "raw/media/keep.wav") in blobs, "another collection's files are not touched"
        assert "legal" not in flows.app.module.named()


class TestWhatASetupHoldsTo:
    def test_a_name_is_a_folder_an_index_and_a_file(self):
        import collection_setup

        from vectrixdb.exceptions import ConfigurationError

        assert collection_setup.check_name(" lending-memos ") == "lending-memos"
        for bad in ("Lending", "-memos", "memos-", "memos/2026", "x" * 64, ""):
            with pytest.raises(ConfigurationError):
                collection_setup.check_name(bad)

    def test_a_file_keeps_its_name_and_typed_text_gets_one_from_its_title(self):
        import collection_setup

        assert collection_setup.file_name("C:\\Users\\ama\\Q2 report (final).pdf") == "Q2 report -final-.pdf"
        assert collection_setup.file_name(None, title="Board minutes: Q2!", typed=True) == "board-minutes-q2.md"
        assert collection_setup.file_name(None, typed=True) == "typed-text.md"

    def test_what_the_masking_stage_says(self):
        import collection_setup

        nothing = collection_setup.masking_of([{"file": "a.md"}, None])
        assert nothing["files"] == 0 and nothing["words"] == ""
        clean = collection_setup.masking_of([{"masking": {"counts": {}, "score": 0.0}}])
        assert clean["words"] == "Nothing to mask in 1 file: no identifier was found."
        said = collection_setup.masking_of([
            {"masking": {"counts": {"email": 9, "phone": 3}, "regex_only": False}},
            {"masking": {"counts": {"credit_card": 2}, "regex_only": False}},
            {"masking": {"counts": {}, "regex_only": True}},
        ])
        assert said["total"] == 14 and said["masked"] == 2 and said["files"] == 3 and said["patterns_only"] == 1
        assert said["words"] == "14 identifiers masked in 2 of 3 files before anything was kept: 9 emails, 3 phone numbers, 2 cards. The index never holds them. 1 file waited for the patterns alone, its language not covered."

    def test_what_deleting_says(self):
        import collection_setup

        steps = collection_setup.steps_of_delete({"raw": 3, "markdown": 3, "chunks": 3}, True, True)
        assert steps[1]["line"] == "3 uploaded, 3 Markdown and 3 chunk files deleted."
        assert collection_setup.steps_of_delete({}, False, False)[2]["line"] == "There was no search index to remove."

