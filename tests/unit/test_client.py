"""The Python client walks sdk/CONTRACT.md's conformance walk against a real server.

The server runs in this process on a free port, so the sync client goes over
a socket like a user's would; the async client runs the same walk through
httpx's in-process transport.
"""

from __future__ import annotations

import socket
import threading
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from vectrixdb import connect  # noqa: E402
from vectrixdb.client import (  # noqa: E402
    AsyncVectrixClient,
    AuthError,
    BusyError,
    ConnectionFailed,
    InvalidError,
    NotFoundError,
    RequestError,
    VectrixClient,
)

KEY = "the-admin-api-key"
HANDBOOK = Path(__file__).resolve().parents[2] / "sdk" / "conformance" / "handbook.md"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """A server on a free port that keeps each document's Markdown."""
    import os

    import uvicorn

    from vectrixdb.api.server import create_app

    os.environ["VECTRIXDB_API_KEY"] = KEY
    os.environ["VECTRIXDB_KEEP_SOURCE"] = "1"
    port = _free_port()
    app = create_app(db_path=str(tmp_path_factory.mktemp("db")), enable_dashboard=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def walk(db: VectrixClient, url: str) -> None:
    """The conformance walk, step for step as the contract numbers them."""
    name = f"walk-{uuid.uuid4().hex[:8]}"
    assert db.health() is True  # 1
    assert db.ready() is True
    made = db.create_collection(name)  # 2
    assert made.has_text_index and made.count == 0
    assert name in [c.name for c in db.collections()]  # 3
    assert db.describe(name).name == name
    added = db.add_document(name, HANDBOOK.read_bytes(), "handbook.md", doc_id="handbook.md")  # 4
    assert added.chunks >= 2 and added.kept and "handbook.md#Refunds" in added.citations
    count = db.add_texts(  # 5
        name,
        [
            {
                "id": "t1",
                "text": "Parking permits are issued by reception.",
                "metadata": {"team": "facilities"},
            },
            {
                "id": "t2",
                "text": "Salaries are paid on the last working day of the month.",
                "metadata": {"team": "payroll"},
            },
        ],
    )
    assert count == 2
    hits = db.search(name, "refunds", limit=3)  # 6
    assert hits[0].citation == "handbook.md#Refunds" and "ten working days" in hits[0].text
    assert hits.query == "refunds" and hits.mode == "meaning"
    hybrid = db.search(name, "salaries", mode="hybrid", rerank=True, limit=2)  # 7
    assert hybrid[0].id == "t2"
    only = db.search(name, "pay", limit=5, filter={"team": "payroll"})
    assert [hit.id for hit in only] == ["t2"]
    assert [d.doc_id for d in db.documents(name)] == ["handbook.md"]  # 8
    assert db.open_document(name, "handbook.md").startswith("# Refunds")
    assert db.sources(name) == []  # 9
    assert db.refresh_sources(name).added == 0
    assert db.delete_document(name, "handbook.md") >= 2  # 10
    assert db.documents(name) == []
    with pytest.raises(NotFoundError) as refused:  # 11
        db.describe("no-such-collection")
    assert refused.value.status == 404 and "not found" in refused.value.message
    with pytest.raises(InvalidError) as invalid:  # 12
        db.create_collection("walk-bad", dimension=0)
    assert invalid.value.status == 422
    assert any("dimension" in str(item.get("loc")) for item in invalid.value.detail)
    with pytest.raises(AuthError) as denied:  # 13
        with VectrixClient(url, key="wrong") as stranger:
            stranger.describe(name)
    assert denied.value.status == 401
    db.delete_collection(name)  # 14
    assert name not in [c.name for c in db.collections()]


def test_the_sync_client_walks_the_contract(served):
    with connect(served, key=KEY) as db:
        walk(db, served)


@pytest.mark.asyncio
async def test_the_async_client_walks_the_contract(served):
    name = f"walk-{uuid.uuid4().hex[:8]}"
    async with AsyncVectrixClient(served, key=KEY) as db:
        assert await db.ready()
        made = await db.create_collection(name)
        assert made.has_text_index
        added = await db.add_document(
            name, HANDBOOK.read_bytes(), "handbook.md", doc_id="handbook.md"
        )
        assert "handbook.md#Refunds" in added.citations
        assert (
            await db.add_texts(
                name, [("t2", "Salaries are paid on the last working day.", {"team": "payroll"})]
            )
            == 1
        )
        hits = await db.search(name, "refunds", limit=2)
        assert hits[0].citation == "handbook.md#Refunds"
        assert (await db.open_document(name, "handbook.md")).startswith("# Refunds")
        assert await db.delete_document(name, "handbook.md") >= 2
        with pytest.raises(NotFoundError):
            await db.describe("no-such-collection")
        await db.delete_collection(name)
        assert name not in [c.name for c in await db.collections()]


def test_a_document_id_with_a_slash_round_trips(served):
    name = f"walk-{uuid.uuid4().hex[:8]}"
    with connect(served, key=KEY) as db:
        db.create_collection(name)
        added = db.add_document(
            name,
            b"# Policy\n\nThe policy text, long enough to keep.\n",
            "policy.md",
            doc_id="hr/policy.md",
        )
        assert added.doc_id == "hr/policy.md"
        assert db.open_document(name, "hr/policy.md").startswith("# Policy")
        assert db.delete_document(name, "hr/policy.md") >= 1
        db.delete_collection(name)


def test_a_server_that_is_not_there_says_so():
    with pytest.raises(ConnectionFailed) as failed:
        connect("http://127.0.0.1:9", key="x", timeout=2).health()
    assert "127.0.0.1:9" in str(failed.value)


def test_a_busy_server_is_retried_with_retry_after(monkeypatch):
    """Two 503s with Retry-After, then an answer: the client waits as told and succeeds."""
    import httpx

    waits = []
    monkeypatch.setattr("vectrixdb.client.time.sleep", lambda seconds: waits.append(seconds))
    answers = iter(
        [
            httpx.Response(
                503, headers={"retry-after": "2"}, json={"ok": False, "message": "busy"}
            ),
            httpx.Response(503, json={"ok": False, "message": "busy"}),
            httpx.Response(200, json={"status": "healthy"}),
        ]
    )
    transport = httpx.MockTransport(lambda request: next(answers))
    with VectrixClient("http://server.test", key="k", transport=transport) as db:
        assert db.health() is True
    assert waits == [2.0, 2.0]


def test_a_server_that_stays_busy_raises_after_the_retries():
    import httpx

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            429, headers={"retry-after": "0"}, json={"ok": False, "message": "slow down"}
        )
    )
    with VectrixClient("http://server.test", key="k", transport=transport) as db:
        with pytest.raises(BusyError) as busy:
            db.collections()
    assert busy.value.status == 429 and busy.value.message == "slow down"


def test_a_proxy_page_becomes_a_status_and_the_address():
    import httpx

    transport = httpx.MockTransport(
        lambda request: httpx.Response(502, text="<html>bad gateway</html>")
    )
    with VectrixClient("http://server.test", key="k", transport=transport) as db:
        with pytest.raises(RequestError) as refused:
            db.collections()
    assert refused.value.status == 502 and refused.value.message.startswith(
        "502 from http://server.test"
    )


def test_the_key_and_the_token_go_in_their_headers():
    import httpx

    seen = []

    def answer(request):
        seen.append(
            (
                request.headers.get("api-key"),
                request.headers.get("authorization"),
                request.headers.get("user-agent"),
            )
        )
        return httpx.Response(200, json={"status": "healthy"})

    with VectrixClient("http://s.test", key="k1", transport=httpx.MockTransport(answer)) as db:
        db.health()
    with VectrixClient("http://s.test", token="t1", transport=httpx.MockTransport(answer)) as db:
        db.health()
    assert seen[0][0] == "k1" and seen[0][1] is None
    assert seen[1][0] is None and seen[1][1] == "Bearer t1"
    assert seen[0][2].startswith("vectrixdb-python/")
