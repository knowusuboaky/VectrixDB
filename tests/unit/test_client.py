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
    with VectrixClient("https://server.test", key="k", transport=transport) as db:
        assert db.health() is True
    assert waits == [2.0, 2.0]


def test_a_server_that_stays_busy_raises_after_the_retries():
    import httpx

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            429, headers={"retry-after": "0"}, json={"ok": False, "message": "slow down"}
        )
    )
    with VectrixClient("https://server.test", key="k", transport=transport) as db:
        with pytest.raises(BusyError) as busy:
            db.collections()
    assert busy.value.status == 429 and busy.value.message == "slow down"


def test_a_proxy_page_becomes_a_status_and_the_address():
    import httpx

    transport = httpx.MockTransport(
        lambda request: httpx.Response(502, text="<html>bad gateway</html>")
    )
    with VectrixClient("https://server.test", key="k", transport=transport) as db:
        with pytest.raises(RequestError) as refused:
            db.collections()
    assert refused.value.status == 502 and refused.value.message.startswith(
        "502 from https://server.test"
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

    with VectrixClient("https://s.test", key="k1", transport=httpx.MockTransport(answer)) as db:
        db.health()
    with VectrixClient("https://s.test", token="t1", transport=httpx.MockTransport(answer)) as db:
        db.health()
    assert seen[0][0] == "k1" and seen[0][1] is None
    assert seen[1][0] is None and seen[1][1] == "Bearer t1"
    assert seen[0][2].startswith("vectrixdb-python/")


# --- safety and the company network: sdk/CONTRACT.md, the same cases in every language ---

GATEWAY = "api/v1=/files/search, auth=/files/auth"


@pytest.mark.parametrize(
    "paths, prefix, route, sent",
    [
        (GATEWAY, "/acme", "/api/v1/collections", "/files/search/acme/api/v1/collections"),
        (GATEWAY, "/acme", "/auth/me", "/files/auth/acme/auth/me"),
        (GATEWAY, "/acme", "/health", "/acme/health"),
        (GATEWAY, "/acme", "/api/v1x", "/acme/api/v1x"),
        ("api=/a, api/v1=/b", "", "/api/v1/c", "/b/api/v1/c"),
        ("api=/a, api/v1=/b", "", "/api/other", "/a/api/other"),
        ("/api//v1/ = files//search/", " acme/ ", "/api/v1/x", "/files/search/acme/api/v1/x"),
    ],
)
def test_each_route_goes_to_its_gateway_path(paths, prefix, route, sent):
    from vectrixdb.client import _names, _route_path, read_gateway_paths

    assert _route_path(route, _names(prefix, "the prefix"), read_gateway_paths(paths)) == sent


@pytest.mark.parametrize(
    "written", ["api/v1", "=/x", "api/v1=", "../x=/y", "api/v1=/a, api/v1/=/b"]
)
def test_a_gateway_list_that_cannot_be_read_is_refused(written):
    from vectrixdb.client import read_gateway_paths
    from vectrixdb.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError):
        read_gateway_paths(written)


def test_the_client_reads_the_gateway_list_as_the_server_does():
    pytest.importorskip("fastapi")
    from vectrixdb.api.gateway import Gateway
    from vectrixdb.api.gateway import read_gateway_paths as server_reads
    from vectrixdb.client import _names, _route_path, read_gateway_paths

    written = "api/v1=/files/search, auth=/files/auth, api/v1/collections/x=/one"
    assert read_gateway_paths(written) == server_reads(written)
    server = Gateway(prefix="/acme", paths=server_reads(written))
    for route in (
        "/api/v1/collections",
        "/api/v1/collections/x/text-search",
        "/auth/me",
        "/health",
    ):
        assert _route_path(
            route, _names("acme", "p"), read_gateway_paths(written)
        ) == server.visible(route)


@pytest.mark.parametrize(
    "url, allowed",
    [
        ("http://vectors.example.com", False),
        ("http://localhost.evil.com", False),
        ("http://localhost:8000", True),
        ("http://127.0.0.5", True),
        ("http://[::1]:9", True),
        ("https://vectors.example.com", True),
    ],
)
def test_a_key_crosses_the_network_only_over_https(url, allowed):
    from vectrixdb.exceptions import ConfigurationError

    if allowed:
        VectrixClient(url, key="k").close()
        return
    with pytest.raises(ConfigurationError) as refused:
        VectrixClient(url, key="k")
    assert "allow_http" in str(refused.value) and "k" not in str(refused.value).split()
    VectrixClient(url, key="k", allow_http=True).close()
    VectrixClient(url).close()


def test_a_redirect_is_never_followed_so_the_key_stays_put():
    import httpx

    asked = []

    def answer(request):
        asked.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://elsewhere.example/x"})

    with VectrixClient("https://s.test", key="k1", transport=httpx.MockTransport(answer)) as db:
        with pytest.raises(RequestError) as refused:
            db.collections()
    assert asked == ["https://s.test/api/v1/collections"]
    assert refused.value.status == 302 and "https://elsewhere.example/x" in refused.value.message
    assert "k1" not in str(refused.value) and "k1" not in repr(db)


def test_a_gateway_gets_its_headers_and_its_paths():
    import httpx

    seen = []

    def answer(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "data": {"collections": []}})

    with VectrixClient(
        "https://gateway.example.com",
        key="k1",
        key_header="Ocp-Apim-Subscription-Key",
        headers={"x-team": "search", "api-key": "not-this", "user-agent": "nor-this"},
        prefix="acme",
        gateway_paths=GATEWAY,
        transport=httpx.MockTransport(answer),
    ) as db:
        db.collections()
    sent = seen[0]
    assert sent.url.path == "/files/search/acme/api/v1/collections"
    assert sent.headers["ocp-apim-subscription-key"] == "k1"
    assert sent.headers["x-team"] == "search" and sent.headers["user-agent"].startswith(
        "vectrixdb-python/"
    )
    with VectrixClient(
        "https://s.test", token="t1", token_header="x-token", transport=httpx.MockTransport(answer)
    ) as db:
        db.health()
    assert seen[1].headers["x-token"] == "Bearer t1" and "authorization" not in seen[1].headers


def test_certificates_cannot_be_switched_off_and_a_header_name_is_checked():
    from vectrixdb.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError, match="always checked"):
        VectrixClient("https://s.test", verify=False)
    with pytest.raises(ConfigurationError, match="HTTP header"):
        VectrixClient("https://s.test", key="k", key_header="bad header")
