"""vectrixdb.connect: a server's collections with a local Vectrix's calls, against a real server in this process."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import vectrixdb  # noqa: E402
from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.client import OPERATIONS, Client  # noqa: E402
from vectrixdb.easy import Result, Results  # noqa: E402
from vectrixdb.exceptions import (  # noqa: E402
    ServerBusy,
    ServerNotFound,
    ServerPermissionDenied,
    ServerRefused,
    ServerSignInRequired,
    VectrixError,
)

KEY = "the-key"
READ_ONLY = "the-read-only-key"
BASE = "http://vectors.test"
ROOT = Path(__file__).resolve().parents[2]

TEXTS = [
    "Refunds are paid by the billing team within ten working days.",
    "Travel is booked through the office manager, economy class.",
    "Laptops are replaced every three years by the IT desk.",
]


@pytest.fixture
def served(tmp_path, monkeypatch):
    from vectrixdb.api.server import create_app

    monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
    monkeypatch.setenv("VECTRIXDB_READ_ONLY_API_KEY", READ_ONLY)
    monkeypatch.setenv("VECTRIXDB_KEEP_SOURCE", "1")
    app = create_app(db_path=str(tmp_path / "server"), enable_dashboard=False)
    with TestClient(app, base_url=BASE) as http:
        yield app, http


@pytest.fixture
def client(served):
    _, http = served
    return vectrixdb.connect(BASE, key=KEY, http=http)


@pytest.fixture
def handbook(client):
    db = client.create_collection("handbook", description="The staff handbook")
    db.add(
        TEXTS,
        ids=["refunds", "travel", "laptops"],
        metadata=[{"team": "billing"}, {"team": "office"}, {"team": "it"}],
    )
    return db


class TestTheSameCallsAsVectrix:
    def test_search_answers_with_the_libraries_own_results(self, handbook):
        found = handbook.search("when are refunds paid", limit=2)
        assert isinstance(found, Results) and isinstance(found.top, Result)
        assert found.top.id == "refunds" and "ten working days" in found.top.text
        assert 0 < found.top.relevance <= 1
        assert len(found) == 2

    def test_the_same_code_runs_on_this_machine_and_on_the_server(self, handbook, tmp_path):
        local = Vectrix("handbook", path=str(tmp_path / "local"))
        local.add(TEXTS, ids=["refunds", "travel", "laptops"])
        for db in (local, handbook):
            assert db.search("who books travel", limit=1).top.id == "travel"
        local.close()

    @pytest.mark.parametrize("mode", ["hybrid", "dense", "keyword", "rerank"])
    def test_every_mode(self, handbook, mode):
        assert handbook.search("refunds", mode=mode, limit=3).top.id == "refunds"

    def test_a_filter(self, handbook):
        found = handbook.search("who does what", filter={"team": "it"}, limit=3)
        assert [r.id for r in found] == ["laptops"]

    def test_similar_is_more_like_one_and_never_itself(self, handbook):
        found = handbook.similar("refunds", limit=2)
        assert "refunds" not in [r.id for r in found] and len(found) == 2

    def test_a_document_from_markdown_a_path_and_bytes(self, handbook, tmp_path):
        said = handbook.add_document(
            "# Leave\n\nAnnual leave is twenty five days.", doc_id="leave.md"
        )
        assert said["chunks"] >= 1 and said["doc_id"] == "leave.md"
        page = tmp_path / "parking.md"
        page.write_text("# Parking\n\nThe car park opens at seven.", encoding="utf-8")
        assert handbook.add_document(page)["doc_id"] == "parking.md"
        assert (
            handbook.add_document(
                b"# Badges\n\nBadges are collected at reception.", filename="badges.md"
            )["chunks"]
            >= 1
        )
        held = {d["doc_id"] for d in handbook.documents()}
        assert {"leave.md", "parking.md", "badges.md"} <= held
        assert "twenty five days" in handbook.document("leave.md")
        assert handbook.search("how many days of leave", limit=1).top.citation.startswith(
            "leave.md"
        )

    def test_delete_a_document(self, handbook):
        handbook.add_document("# Gone\n\nThis goes.", doc_id="gone.md")
        assert handbook.delete_document("gone.md") >= 1
        with pytest.raises(ServerNotFound):
            handbook.delete_document("gone.md")

    def test_describe_names_the_fields_to_filter_on(self, handbook):
        about = handbook.describe()
        assert about["count"] == 3 and about["description"] == "The staff handbook"
        assert "team" in about["fields"]

    def test_collections_and_whoami(self, client, handbook):
        assert "handbook" in [c["name"] for c in client.collections()]
        me = client.whoami()
        assert me["method"] in ("key", "none") or me["role"]
        client.delete_collection("handbook")
        assert "handbook" not in [c["name"] for c in client.collections()]

    def test_health_and_ready(self, client):
        assert client.health()["status"] == "healthy"
        assert client.ready() is True

    def test_connect_with_a_collection_is_that_collection(self, served, handbook):
        _, http = served
        db = vectrixdb.connect(BASE, key=KEY, collection="handbook", http=http)
        assert db.search("refunds", limit=1).top.id == "refunds"


class TestRefusals:
    def test_no_key_is_sign_in_required(self, served):
        _, http = served
        with pytest.raises(ServerSignInRequired) as caught:
            vectrixdb.connect(BASE, key="wrong", http=http).collections()
        assert caught.value.status == 401

    def test_a_read_only_key_may_search_and_may_not_write(self, served, handbook):
        _, http = served
        reader = vectrixdb.connect(BASE, key=READ_ONLY, collection="handbook", http=http)
        assert reader.search("refunds", limit=1).top.id == "refunds"
        with pytest.raises(ServerPermissionDenied) as caught:
            reader.add(["A new rule."])
        assert "Read-only" in caught.value.said

    def test_a_collection_that_is_not_there(self, client):
        with pytest.raises(ServerNotFound):
            client.collection("nothing").search("anything")

    def test_every_refusal_is_a_vectrix_error(self, client):
        with pytest.raises(VectrixError):
            client.collection("nothing").describe()

    def test_a_key_and_a_token_together(self):
        with pytest.raises(ValueError, match="not both"):
            Client(BASE, key="k", token="t", http=object())

    def test_a_bad_mode_is_caught_before_a_request(self, client):
        with pytest.raises(ValueError, match="mode is one of"):
            client.collection("handbook").search("x", mode="fastest")


class Scripted:
    """An httpx.Client stand-in that answers from a script and records what it was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def request(self, method, path, **kw):
        self.asked.append((method, path, kw))
        status, headers, body = self.answers.pop(0)
        return httpx.Response(
            status,
            headers=headers,
            content=json.dumps(body).encode(),
            request=httpx.Request(method, BASE + path),
        )


class TestTryingAgain:
    @pytest.fixture(autouse=True)
    def no_waiting(self, monkeypatch):
        waited = []
        monkeypatch.setattr("vectrixdb.client.time.sleep", waited.append)
        return waited

    def test_a_busy_server_is_asked_again_after_what_it_asked(self, no_waiting):
        http = Scripted(
            (503, {"retry-after": "4"}, {"detail": "busy"}),
            (200, {}, {"ok": True, "data": {"status": "healthy"}}),
        )
        assert Client(BASE, key=KEY, http=http).health() == {"status": "healthy"}
        assert no_waiting == [4.0] and len(http.asked) == 2

    def test_still_busy_after_every_try_is_server_busy(self, no_waiting):
        http = Scripted(*[(429, {}, {"detail": "slow down"})] * 4)
        with pytest.raises(ServerBusy, match="slow down"):
            Client(BASE, key=KEY, http=http, retries=3).collections()
        assert len(http.asked) == 4 and no_waiting == [0.5, 1.0, 2.0]

    def test_a_refusal_is_not_asked_again(self):
        http = Scripted((403, {}, {"detail": "Your role does not allow this"}))
        with pytest.raises(ServerPermissionDenied):
            Client(BASE, key=KEY, http=http).collections()
        assert len(http.asked) == 1

    def test_a_token_function_is_asked_before_every_request(self):
        issued = iter(["first", "second"])
        http = Scripted((200, {}, {"ok": True, "data": {}}), (200, {}, {"ok": True, "data": {}}))
        client = Client(BASE, token=lambda: next(issued), http=http)
        client.whoami()
        client.whoami()
        assert [kw["headers"]["Authorization"] for _, _, kw in http.asked] == [
            "Bearer first",
            "Bearer second",
        ]

    def test_a_key_goes_in_the_header_the_server_reads(self):
        http = Scripted((200, {}, {"ok": True, "data": {}}))
        Client(BASE, key="k", key_header="x-vectrix-key", http=http).whoami()
        assert http.asked[0][2]["headers"]["x-vectrix-key"] == "k"


class TestAsync:
    def test_the_same_calls_awaited(self, served):
        app, _ = served

        async def run():
            http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)
            client = vectrixdb.connect_async(BASE, key=KEY, http=http)
            db = await client.create_collection("notes")
            await db.add(TEXTS, ids=["refunds", "travel", "laptops"])
            found = await db.search("refunds", limit=1)
            near = await db.similar("refunds", limit=1)
            said = await db.add_document("# Leave\n\nTwenty five days.", doc_id="leave.md")
            names = [c["name"] for c in await client.collections()]
            await http.aclose()
            return found, near, said, names

        found, near, said, names = asyncio.run(run())
        assert found.top.id == "refunds" and near.top.id != "refunds"
        assert said["doc_id"] == "leave.md" and "notes" in names

    def test_an_async_token_function(self):
        from vectrixdb.client import AsyncClient

        async def token():
            return "fresh"

        async def run():
            client = AsyncClient(BASE, token=token, http=object())
            return await client._headers()

        assert asyncio.run(run()) == {"Authorization": "Bearer fresh"}


class TestTheContract:
    def test_every_request_the_client_makes_is_in_the_openapi_document(self):
        document = json.loads(
            (ROOT / "docs" / "reference" / "openapi.json").read_text(encoding="utf-8")
        )
        paths = {
            (method.upper(), path.replace("{doc_id:path}", "{doc_id}"))
            for path, methods in document["paths"].items()
            for method in methods
        }
        missing = [op for op in OPERATIONS if op[0] != "GET" or op[1] not in ("/health", "/ready")]
        missing = [op for op in missing if op not in paths]
        assert missing == [], f"the client asks for routes the server does not publish: {missing}"

    def test_a_refusal_carries_the_servers_words(self):
        error = ServerRefused(500, "The disk is full")
        assert error.status == 500 and str(error) == "The server answered 500: The disk is full"
