"""MCP on the server: an assistant searches as the person, or the key, it acts for.

What is being held to. The endpoint is off until asked for. Nobody gets in
without a key or a token, and the refusal tells a client where its person
signs in. A key made for one collection reaches that collection through MCP
and nothing else, a role that may not search may not search through MCP
either, and writing is off until the server allows it and the role does.
A person signed in with the company's identity provider is that person over
MCP: a collection's policy judges the search as theirs, and the access log
records it under their name. A dashboard's session cookie is not a way in,
and a server with no key and no sign-in answers MCP only from its own
machine. Every answer says honestly whether it was cut.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("mcp.server.mcpserver", reason="the mcp extra (version 2) is not installed")
pytest.importorskip("jwt", reason="the signin extra is not installed")
pytest.importorskip("cryptography", reason="the signin extra is not installed")

sys.path.insert(0, os.path.dirname(__file__))
from fake_idp import ISSUER, FakeIdp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.api import mcp as door  # noqa: E402
from vectrixdb.policy import Overlap, Policy  # noqa: E402
from vectrixdb.signin import OidcConfig, SignInConfig  # noqa: E402

SECRET = "k" * 48
PUBLIC = "https://vectors.example.test"
KEY = "the-admin-api-key"
AUDIENCE = "api://vectrixdb"
ACCEPT = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture
def data(tmp_path):
    """Three collections: two plain ones, so a key scoped to one has another to be kept out of, and one behind a policy."""
    root = tmp_path / "db"
    rows = {
        "handbook": (
            ["Refunds are paid by the billing team within ten working days."],
            [{}],
        ),
        "payroll": (["Salaries are paid on the last working day of the month."], [{}]),
    }
    for name, (texts, meta) in rows.items():
        one = Vectrix(name, path=str(root), mode="hybrid", embedding_cache=False)
        one.add(texts, ids=[f"{name}-1"], metadata=meta)
        one.close()
    walled = Vectrix(
        "walled",
        path=str(root),
        mode="hybrid",
        embedding_cache=False,
        policy=Policy([Overlap("client_id", "clients")]),
    )
    walled.add(
        [
            "The acme loan covenant is reviewed every quarter.",
            "The zeta loan covenant is reviewed every year.",
        ],
        ids=["w-acme", "w-zeta"],
        metadata=[{"client_id": "acme"}, {"client_id": "zeta"}],
    )
    walled.close()
    return root


def _signin(data, **over):
    base = {
        "methods": ("email",),
        "secrets": (SECRET,),
        "public_url": PUBLIC,
        "users": (("ada@example.com", "admin"),),
        "store_path": data / "auth" / "signin.db",
        "access_log": data / "auth" / "access.jsonl",
        "sender": lambda to, subject, text: None,
    }
    base.update(over)
    return SignInConfig(**base)


@pytest.fixture
def mcp_on(monkeypatch):
    monkeypatch.setenv("VECTRIXDB_MCP", "1")
    monkeypatch.delenv("VECTRIXDB_MCP_WRITES", raising=False)
    monkeypatch.delenv("VECTRIXDB_AUDIT_JSONL", raising=False)


@pytest.fixture
def server(data, mcp_on, monkeypatch):
    from vectrixdb.api.server import create_app

    monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
    with TestClient(
        create_app(db_path=str(data), enable_dashboard=False, signin=_signin(data)),
        base_url=PUBLIC,
    ) as client:
        yield client


def make_key(client: TestClient, **body) -> str:
    """A named key, made the way the dashboard makes one."""
    body.setdefault("name", "an-assistant")
    body.setdefault("role", "searcher")
    reply = client.post("/api/v1/keys", json=body, headers={"api-key": KEY})
    assert reply.status_code == 200, reply.text
    return reply.json()["data"]["key"]


def rpc(client: TestClient, key_headers: dict, method: str, params: dict = None) -> dict:
    reply = client.post(
        "/mcp",
        headers={**ACCEPT, **key_headers},
        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}),
    )
    assert reply.status_code == 200, reply.text
    return reply.json()["result"]


def call(client: TestClient, key_headers: dict, tool: str, **arguments) -> tuple:
    """A tool call: whether it was refused, and its text."""
    result = rpc(client, key_headers, "tools/call", {"name": tool, "arguments": arguments})
    return result.get("isError", False), result["content"][0]["text"]


def tools(client: TestClient, key_headers: dict) -> list:
    return [t["name"] for t in rpc(client, key_headers, "tools/list")["tools"]]


# ------------------------------------------------------------- the endpoint


class TestTheEndpoint:
    def test_it_is_off_until_asked_for(self, data, monkeypatch):
        from vectrixdb.api.server import create_app

        monkeypatch.delenv("VECTRIXDB_MCP", raising=False)
        monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
        with TestClient(
            create_app(db_path=str(data), enable_dashboard=False), base_url=PUBLIC
        ) as client:
            reply = client.post("/mcp", headers={**ACCEPT, "api-key": KEY}, content="{}")
            assert reply.status_code == 404

    def test_nobody_gets_in_and_is_told_where_to_sign_in(self, server):
        reply = server.post("/mcp", headers=ACCEPT, content="{}")
        assert reply.status_code == 401
        assert reply.headers["www-authenticate"] == (
            f'Bearer resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/mcp"'
        )
        assert "API key" in reply.json()["message"]

    def test_a_wrong_key_is_refused_the_same_way(self, server):
        reply = server.post("/mcp", headers={**ACCEPT, "api-key": "not-a-key"}, content="{}")
        assert reply.status_code == 401 and "resource_metadata" in reply.headers["www-authenticate"]

    def test_the_tools_say_what_they_do_and_that_they_only_read(self, server):
        listed = rpc(server, {"api-key": make_key(server)}, "tools/list")["tools"]
        assert [t["name"] for t in listed] == ["list_collections", "search", "open_source"]
        assert all(t["annotations"]["readOnlyHint"] for t in listed)

    def test_the_answer_from_documents_prompt_is_offered(self, server):
        prompts = rpc(server, {"api-key": make_key(server)}, "prompts/list")["prompts"]
        assert [p["name"] for p in prompts] == ["answer_from_documents"]


# ------------------------------------------------------------------ a key


class TestATeamKey:
    def test_it_searches_and_the_answer_carries_relevance_and_source(self, server):
        key = {"api-key": make_key(server)}
        refused, text = call(
            server, key, "search", collection="handbook", query="when are refunds paid"
        )
        assert not refused, text
        assert text.startswith("[i] 1 result from handbook")
        assert "ten working days" in text and "%]" in text and "id=handbook-1" in text

    def test_a_collection_with_no_text_index_is_searched_by_meaning_and_says_so(self, server):
        # The REST API's plain default makes a collection with no index of exact
        # words, and hybrid search over REST refuses it with a 400 telling the
        # caller to make the collection differently. The assistant made nothing.
        master = {"api-key": KEY}
        status = server.post(
            "/api/v1/collections", json={"name": "plain", "dimension": 384}, headers=master
        ).status_code
        assert status in (200, 201)
        server.post(
            "/api/v1/collections/plain/text-upsert",
            json={"points": [{"id": "p-1", "text": "Reset your password under Settings."}]},
            headers=master,
        ).raise_for_status()
        key = {"api-key": make_key(server)}
        refused, text = call(server, key, "search", collection="plain", query="change my password")
        assert not refused, text
        assert text.startswith(
            "[i] This collection has no index of exact words: searched by meaning."
        )
        assert "password" in text and "id=p-1" in text
        refused, text = call(
            server, key, "search", collection="plain", query="password", mode="keyword"
        )
        assert refused and "index" in text, "what was asked for by name is still refused"

    def test_a_bearer_token_is_the_same_key(self, server):
        refused, _ = call(
            server, {"Authorization": f"Bearer {make_key(server)}"}, "list_collections"
        )
        assert not refused

    def test_one_made_for_one_collection_reaches_that_one_only(self, server):
        key = {"api-key": make_key(server, collections=["handbook"])}
        refused, listing = call(server, key, "list_collections")
        assert not refused and "handbook" in listing and "payroll" not in listing
        refused, text = call(server, key, "search", collection="payroll", query="salaries")
        assert refused and "not found" in text, "the reply a collection that is not there gets"

    def test_a_role_that_may_not_search_may_not_search_through_mcp(self, server):
        key = {"api-key": make_key(server, role="reader")}
        assert tools(server, key)[:2] == ["list_collections", "search"], "a reader may connect"
        refused, text = call(server, key, "search", collection="handbook", query="refunds")
        assert refused and "role does not allow" in text

    def test_what_it_searched_is_in_the_access_log_under_its_name(self, server, data):
        key = {"api-key": make_key(server, name="assistant-for-legal")}
        call(server, key, "search", collection="handbook", query="refunds")
        lines = [
            json.loads(line)
            for line in (data / "auth" / "access.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        searches = [line for line in lines if line.get("event") == "search"]
        assert searches and searches[-1]["who"] == "key:assistant-for-legal"
        assert searches[-1]["collection"] == "handbook"


# ----------------------------------------------------------------- writing


class TestWriting:
    def test_there_is_no_write_tool_until_the_server_allows_it(self, server):
        assert "add_document" not in tools(server, {"api-key": make_key(server, role="operator")})

    def test_with_writes_on_the_role_still_decides(self, data, mcp_on, monkeypatch):
        from vectrixdb.api.server import create_app

        monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
        monkeypatch.setenv("VECTRIXDB_MCP_WRITES", "1")
        with TestClient(
            create_app(db_path=str(data), enable_dashboard=False, signin=_signin(data)),
            base_url=PUBLIC,
        ) as client:
            searcher = {"api-key": make_key(client, name="reads", role="searcher")}
            operator = {"api-key": make_key(client, name="writes", role="operator")}
            assert "add_document" in tools(client, searcher)
            refused, text = call(
                client,
                searcher,
                "add_document",
                collection="handbook",
                text="# Travel\n\nEconomy fares.",
            )
            assert refused and "role does not allow" in text
            refused, text = call(
                client,
                operator,
                "add_document",
                collection="handbook",
                text="# Travel\n\nEconomy fares.",
                title="travel",
            )
            assert not refused and text.startswith("Added travel.md to handbook"), text
            refused, found = call(
                client, operator, "search", collection="handbook", query="economy fares"
            )
            assert not refused and "Economy fares" in found


# --------------------------------------------------- a person, through SSO


@pytest.fixture
def idp(monkeypatch):
    monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
    return FakeIdp()


@pytest.fixture
def sso(data, mcp_on, idp, monkeypatch):
    from vectrixdb.api.server import create_app

    monkeypatch.setenv("VECTRIXDB_OIDC_API_AUDIENCE", AUDIENCE)
    oidc = OidcConfig(
        issuer=ISSUER,
        client_id="vectrixdb",
        client_secret="s3cret",
        api_audience=AUDIENCE,
        role_map={"g-ops": "operator", "acme": "operator"},
        principal_claims={"clients": "groups"},
        allowed_emails=("*",),
    )
    config = _signin(data, methods=("oidc",), oidc=oidc, users=())
    with TestClient(
        create_app(
            db_path=str(data), enable_dashboard=False, signin=config, oidc_transport=idp.transport
        ),
        base_url=PUBLIC,
    ) as client:
        yield client


class TestAPersonSignedInWithTheCompany:
    def test_the_discovery_document_names_the_identity_provider(self, sso):
        found = sso.get("/.well-known/oauth-protected-resource/mcp").json()
        assert found["resource"] == f"{PUBLIC}/mcp"
        assert found["authorization_servers"] == [ISSUER]
        assert found["scopes_supported"] == [f"{AUDIENCE}/.default"]

    def test_the_policy_judges_the_search_as_theirs(self, sso, idp):
        idp.person = {"sub": "u-8", "email": "ama@example.com", "groups": ["acme"]}
        token = {"Authorization": f"Bearer {idp.access_token()}"}
        refused, text = call(
            sso, token, "search", collection="walled", query="loan covenant review"
        )
        assert not refused, text
        assert "w-acme" in text and "w-zeta" not in text, "only what their clients claim allows"
        assert text.startswith("[i] This collection's policy allows search by meaning"), (
            "hybrid is refused under a policy, and the answer says what ran instead"
        )

    def test_keyword_search_is_judged_as_theirs_too(self, sso, idp):
        idp.person = {"sub": "u-8", "email": "ama@example.com", "groups": ["acme"]}
        token = {"Authorization": f"Bearer {idp.access_token()}"}
        refused, text = call(
            sso, token, "search", collection="walled", query="covenant", mode="keyword"
        )
        assert not refused, text
        assert "w-acme" in text and "w-zeta" not in text

    def test_it_is_recorded_under_their_name(self, sso, idp, data):
        idp.person = {"sub": "u-7", "email": "olu@example.com", "groups": ["g-ops"]}
        call(
            sso,
            {"Authorization": f"Bearer {idp.access_token()}"},
            "search",
            collection="handbook",
            query="refunds",
        )
        lines = [
            json.loads(line)
            for line in (data / "auth" / "access.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        search = [line for line in lines if line.get("event") == "search"][-1]
        assert search["who"] == "olu@example.com" and search["method"] == "token"


# ------------------------------------------------------------- the door


class _Caller:
    def __init__(self, method):
        self.method = method


async def _through(state=None, client=("127.0.0.1", 1)):
    """Call the door with a scope as the middleware leaves it: the status it answered and whether MCP was reached."""
    from fastapi import FastAPI

    app = FastAPI()
    state = dict(state or {})
    if "signin" in state:
        app.state.signin = state.pop("signin")
    sent, reached = [], []

    async def handler(scope, receive, send):
        reached.append(True)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"host", b"vectors.example.test")],
        "query_string": b"",
        "scheme": "https",
        "server": ("vectors.example.test", 443),
        "client": client,
        "app": app,
        "state": state,
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await door._Door(handler)(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), 200)
    return status, bool(reached)


class TestTheDoor:
    def test_a_dashboard_session_is_not_a_way_in(self):
        """A page could make an assistant's request for whoever has the dashboard open."""
        status, reached = asyncio.run(_through({"caller": _Caller("email"), "signin": object()}))
        assert status == 401 and not reached

    @pytest.mark.parametrize("method", ["key", "token"])
    def test_a_key_or_a_token_goes_through(self, method):
        assert asyncio.run(_through({"caller": _Caller(method), "signin": object()})) == (200, True)

    def test_a_server_with_nothing_set_answers_only_its_own_machine(self, monkeypatch):
        monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
        assert asyncio.run(_through(client=("127.0.0.1", 5))) == (200, True)
        assert asyncio.run(_through(client=("10.0.0.5", 5))) == (401, False)


# ------------------------------------------------------------- the answer


class TestTheAnswerIsHonest:
    def test_a_cut_list_says_it_was_cut(self):
        hits = [{"id": f"d{n}", "score": 0.5, "text": "word " * 400} for n in range(5)]
        text = door.render_hits("handbook", hits, budget=300)
        assert text.startswith("[!] TRUNCATED: showing 1 of 5 results")

    def test_a_whole_list_says_so(self):
        text = door.render_hits(
            "handbook", [{"id": "d1", "relevance": 0.81, "text": "Refunds."}], 2000
        )
        assert text.startswith("[i] 1 result from handbook") and "[81%]" in text

    def test_nothing_found_says_what_to_try(self):
        assert "Try another mode" in door.render_hits("handbook", [], 2000)
