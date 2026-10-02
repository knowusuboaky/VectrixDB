"""Guests: every collection's name and size, how the setups scored, and never a search.

What is being held to. With guests off, somebody who has not signed in gets
the 401 that sends them to sign in. With guests on they are a guest: the list
names every collection, each with its size, and a collection that is not
there is not found. A guest reads how the setups scored, which says nothing
about what is stored, and never the golden questions. A guest never
searches: every search route, old or new, answers the 401 that sends them to
sign in, so nothing is searched and nothing is logged as a search. A guest
writes nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("cryptography", reason="the signin extra is not installed")

from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.policy import Overlap, Policy  # noqa: E402
from vectrixdb.signin import SignInConfig, totp  # noqa: E402


def create_app(**kwargs):
    """The server as it is now. Another test drops and re-imports ``vectrixdb.api``,
    and a name bound at the top of this file would go on pointing at the old copy,
    whose routes reach for a database the new copy's start-up never gave them."""
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


SECRET = "g" * 48
PUBLIC = "https://vectors.example.test"
KEY = "the-full-api-key"
KEYED = {"api-key": KEY}

LONG = "Payment is due within thirty days of the invoice date, and interest accrues daily after that. " * 4
SHORT = "Refunds are handled by the billing team."
PRIVATE = "Salary bands for next year are confidential."
VECTORS = {LONG: [1, 0, 0, 0], SHORT: [0, 1, 0, 0], PRIVATE: [0, 0, 1, 0], "acme contract": [0, 0, 0, 1]}

SHARED = "/api/v1/collections/handbook"
SEARCH = SHARED + "/search"
QUERY = {"query": [1, 0, 0, 0], "limit": 5}

#: The searches that cut results on the server, and the older address of the first: once a guest's, now shut like the rest.
GUEST_SEARCHES = [
    ("/api/v1/collections/{}/search", {"query": [1, 0, 0, 0], "limit": 5, "include_vectors": True}),
    ("/api/v1/collections/{}/text-search", {"query_text": "invoice", "limit": 5, "include_vectors": True}),
    ("/api/v1/collections/{}/text-hybrid-search", {"query_text": "invoice", "limit": 5, "include_vectors": True}),
    ("/api/v1/collections/{}/keyword-search", {"query_text": "invoice", "limit": 5}),
    ("/api/collections/{}/search", {"query": [1, 0, 0, 0], "limit": 5, "include_vectors": True}),
]
#: What a guest may ask of one collection: itself and its health.
GUEST_ROUTES = [("GET", "/api/v1/collections/{}", None), ("GET", "/api/v1/collections/{}/health", None)]
#: Reads a guest never gets, of any collection.
NEVER_FOR_GUESTS = [
    ("GET", "/api/v1/collections/{}/points", None),
    ("GET", "/api/v1/collections/{}/points/h-long", None),
    ("GET", "/api/v1/collections/{}/provenance/h-long", None),
    ("GET", "/api/v1/collections/{}/documents", None),
    ("GET", "/api/v1/collections/{}/documents/terms.pdf", None),
    ("GET", "/api/v1/collections/{}/quality", None),
    ("GET", "/api/v1/collections/{}/graph", None),
    ("GET", "/api/v1/collections/{}/policy", None),
    ("GET", "/api/v1/collections/{}/builds", None),
]
#: The older searches, which do not cut results, so a guest does not get them.
OTHER_SEARCHES = [
    ("/api/v1/collections/{}/hybrid-search", {"query": [1, 0, 0, 0], "query_text": "invoice"}),
    ("/api/v1/collections/{}/sparse-search", {"query": {"indices": [0], "values": [1.0]}}),
    ("/api/v1/collections/{}/dense-sparse-search", {"dense_query": [1, 0, 0, 0], "sparse_query": {"indices": [0], "values": [1.0]}}),
    ("/api/v1/collections/{}/search/rerank", {"query": [1, 0, 0, 0]}),
    ("/api/v1/collections/{}/search/facets", {"query": [1, 0, 0, 0]}),
    ("/api/v1/collections/{}/search/acl", {"query": [1, 0, 0, 0]}),
    ("/api/v1/collections/{}/search/enterprise", {"query": [1, 0, 0, 0]}),
]
WRITES = [
    ("POST", "/api/v1/collections", {"name": "made", "dimension": 4}),
    ("POST", "/api/v2/collections", {"name": "made", "dimension": 4}),
    ("POST", "/api/v1/collections/{}/points", {"points": [{"id": "x", "vector": [1, 0, 0, 0]}]}),
    ("POST", "/api/v1/collections/{}/text-upsert", {"points": [{"id": "x", "text": "added by a guest"}]}),
    ("DELETE", "/api/v1/collections/{}/points", {"ids": ["h-long"]}),
    ("POST", "/api/v1/collections/{}/documents", None),
    ("DELETE", "/api/v1/collections/{}/documents/terms.pdf", None),
    ("POST", "/api/v1/collections/{}/rebuild", None),
    ("POST", "/api/v1/collections/{}/graph/extract", None),
    ("PUT", "/api/v1/collections/{}/visibility", {"visibility": "private"}),
    ("DELETE", "/api/v1/collections/{}", None),
    ("DELETE", "/api/v1/cache", None),
]
#: Every request a guest might make of one named collection.
EVERY_ROUTE = GUEST_ROUTES + NEVER_FOR_GUESTS + [("POST", p, b) for p, b in GUEST_SEARCHES + OTHER_SEARCHES] + [w for w in WRITES if "{}" in w[1]]


def _embed(texts):
    return np.array([VECTORS[t] for t in texts], dtype=np.float32)


class QueryEmbedder:
    """Stands in for the model the text routes embed a query with. Every query points at the long chunk."""

    def embed(self, text):
        return np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32)


@pytest.fixture(autouse=True)
def quiet_environment(monkeypatch):
    """Nothing from this machine's environment: the key is ours, nothing reaches a live service, no model loads."""
    for name in (
        "VECTRIXDB_API_KEY_FILE", "VECTRIXDB_API_KEY_SHA256", "VECTRIXDB_READ_ONLY_API_KEY", "VECTRIXDB_READ_ONLY_API_KEY_FILE",
        "VECTRIXDB_READ_ONLY_API_KEY_SHA256", "VECTRIXDB_AUDIT_JSONL", "VECTRIXDB_STORAGE_BACKEND", "VECTRIXDB_CACHE_BACKEND",
        "VECTRIXDB_SCALING_STRATEGY", "VECTRIXDB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
    from vectrixdb.api import server as current

    monkeypatch.setattr(current, "get_text_embedder", lambda model=None: QueryEmbedder())


@pytest.fixture
def data(tmp_path):
    """Three collections: one to share, one that stays private, and one behind a policy."""
    root = tmp_path / "db"
    handbook = Vectrix("handbook", path=str(root), dimension=4, embed_fn=_embed, embedding_cache=False)
    # The text is in the metadata as well, as text-upsert stores it, so both places are held to the cut.
    handbook.add([LONG, SHORT], ids=["h-long", "h-short"], metadata=[{"source": "terms.pdf", "text": LONG}, {"source": "faq.pdf"}])
    handbook.close()
    secret = Vectrix("secret", path=str(root), dimension=4, embed_fn=_embed, embedding_cache=False)
    secret.add([PRIVATE], ids=["s-1"], metadata=[{"source": "hr.pdf"}])
    secret.close()
    walled = Vectrix("walled", path=str(root), dimension=4, embed_fn=_embed, embedding_cache=False, policy=Policy([Overlap("client_id", "clients")]))
    walled.add(["acme contract"], ids=["w-1"], metadata=[{"client_id": "acme"}])
    walled.close()
    return root


class Mail:
    """The mail server: keeps what was sent, so a test can follow the link in it."""

    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, text):
        self.sent.append((to, subject, text))

    def token(self):
        return re.search(r"#/enrol\?token=([\w-]+)", self.sent[-1][2]).group(1)


def _config(root: Path, **over) -> SignInConfig:
    base = {
        "methods": ("email",),
        "secrets": (SECRET,),
        "public_url": PUBLIC,
        "users": (("ada@example.com", "admin"), ("olu@example.com", "operator"), ("vi@example.com", "viewer")),
        "store_path": root / "auth" / "signin.db",
        "access_log": root / "auth" / "access.jsonl",
        "sender": Mail(),
        "guests": True,
    }
    base.update(over)
    return SignInConfig(**base)


@pytest.fixture
def server(data):
    """Sign-in on, and guests allowed to browse what is shared."""
    config = _config(data)
    with TestClient(create_app(db_path=str(data), enable_dashboard=False, signin=config), base_url=PUBLIC) as client:
        yield client, config


@pytest.fixture
def members_only(data):
    """Sign-in on, and guests not allowed."""
    config = _config(data, guests=False)
    with TestClient(create_app(db_path=str(data), enable_dashboard=False, signin=config), base_url=PUBLIC) as client:
        yield client, config


def enrol(client: TestClient, config: SignInConfig, email: str) -> str:
    """The whole first visit. Returns the authenticator secret."""
    assert client.post("/auth/email/begin", json={"email": email}).status_code == 200
    begun = client.post("/auth/email/enrol/begin", json={"token": config.sender.token()}).json()["data"]
    done = client.post("/auth/email/enrol/confirm", json={"ticket": begun["ticket"], "code": totp.code_at(begun["secret"], totp.step_now())})
    assert done.status_code == 200, done.text
    return begun["secret"]


def _browser(server, address=None) -> TestClient:
    where = {} if address is None else {"client": (address, 50000)}
    return TestClient(server[0].app, base_url=PUBLIC, **where)


def person(server, email: str, address=None) -> TestClient:
    """A browser of their own, signed in as this person."""
    browser = _browser(server, address)
    enrol(browser, server[1], email)
    return browser


def guest(server, address=None) -> TestClient:
    """Somebody who has not signed in and holds no key."""
    return _browser(server, address)


def csrf(browser: TestClient) -> dict:
    return {"x-csrf-token": browser.cookies.get("__Host-vx_csrf")}


def names(browser: TestClient, path: str = "/api/v1/collections") -> list:
    reply = browser.get(path)
    assert reply.status_code == 200, reply.text
    return [c["name"] for c in reply.json()["collections"]]


def access(server, **query) -> list:
    """The access log, newest first, read with the key."""
    reply = server[0].get("/api/v1/access", params={"limit": 1000, **query}, headers=KEYED)
    assert reply.status_code == 200, reply.text
    return reply.json()["data"]["records"]


def signin_refusal(reply) -> bool:
    return reply.status_code == 401 and reply.json()["data"] == {"signin": True}


# --------------------------------------------------------------- guests off


class TestBehindAGatewayPath:
    """A gateway may serve the app under a path, and then the path a request
    arrives with carries the prefix. Every layer around the routes reads that
    path to decide: the role table places a route, the guest rules match the
    collection routes, and the masking layer recognises a collection. A prefix
    made all three miss, and none of them said so: a route nothing places is
    admin only, so operators, viewers and guests were refused, and
    identifiers went out unmasked.
    """

    @pytest.fixture
    def prefixed(self, data, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_ROOT_PATH", "/vectrixdb")
        config = _config(data)
        with TestClient(create_app(db_path=str(data), enable_dashboard=False, signin=config), base_url=PUBLIC) as client:
            yield client, config

    def test_a_guest_sees_a_collection_through_the_prefix_and_is_sent_to_sign_in_for_a_search(self, prefixed):
        client, _ = prefixed
        seen = client.get("/vectrixdb/api/v1/collections/handbook")
        assert seen.status_code == 200 and seen.json()["data"]["name"] == "handbook", seen.text
        reply = client.post("/vectrixdb/api/v1/collections/handbook/text-search", json={"query_text": "refund", "limit": 2})
        assert signin_refusal(reply), reply.text
        assert "results" not in reply.text

    def test_every_collection_is_there_for_a_guest_through_the_prefix_and_a_missing_one_is_not(self, prefixed):
        client, _ = prefixed
        assert client.get("/vectrixdb/api/v1/collections/secret").status_code == 200
        assert client.get("/vectrixdb/api/v1/collections/nope").status_code == 404

    def test_the_prefix_does_not_get_past_the_key(self, prefixed):
        """A write still needs the key, whichever shape the path arrives in."""
        client, _ = prefixed
        for path in ("/vectrixdb/api/v1/collections", "/api/v1/collections"):
            reply = client.post(path, json={"name": "new-one", "dimension": 4})
            assert reply.status_code in (401, 403), (path, reply.status_code)


class TestWithGuestsOff:
    def test_somebody_not_signed_in_gets_the_401_that_sends_them_to_sign_in(self, members_only):
        client, _ = members_only
        assert client.get("/api/v1/policies", headers=KEYED).json()["data"]["guests"] is False
        stranger = TestClient(client.app, base_url=PUBLIC)
        for reply in (
            stranger.get("/api/v1/collections"),
            stranger.get("/api/collections"),
            stranger.get(SHARED),
            stranger.get(SHARED + "/health"),
            stranger.post(SEARCH, json=QUERY),
            stranger.post(SHARED + "/keyword-search", json={"query_text": "invoice"}),
        ):
            assert signin_refusal(reply), reply.text
            assert "Payment" not in reply.text

    def test_the_page_is_not_told_that_guests_may_browse(self, members_only):
        me = TestClient(members_only[0].app, base_url=PUBLIC).get("/auth/me")
        assert me.status_code == 401 and "guests" not in me.json()["data"]


# ------------------------------------------------------- what a guest sees


class TestWhatAGuestSees:
    def test_the_list_names_every_collection(self, server):
        visitor = guest(server)
        assert names(visitor) == ["handbook", "secret", "walled"] and names(visitor, "/api/collections") == ["handbook", "secret", "walled"]
        assert visitor.get("/api/v1/collections").json()["total"] == 3
        assert visitor.get("/api/v1/models").status_code == 200, "which models are here says nothing about what is stored"

    def test_the_page_is_told_that_guests_may_browse(self, server):
        me = guest(server).get("/auth/me")
        assert me.status_code == 401 and me.json()["data"]["guests"] is True

    def test_a_collection_and_its_health_are_a_guests_to_look_at(self, server):
        visitor = guest(server)
        for name in ("handbook", "secret"):
            info = visitor.get(f"/api/v1/collections/{name}")
            assert info.status_code == 200 and info.json()["data"]["name"] == name
            assert visitor.get(f"/api/v1/collections/{name}/health").status_code == 200

    def test_on_every_route_a_guest_may_use_a_collection_that_is_not_there_is_not_found(self, server):
        visitor = guest(server)
        for method, path, body in GUEST_ROUTES:
            reply = visitor.request(method, path.format("nope"), json=body)
            assert reply.status_code == 404, f"{method} {path}: {reply.text}"
            assert reply.json() == {"ok": False, "message": "Collection 'nope' not found", "data": None, "detail": "Collection 'nope' not found"}, f"{method} {path}"

    def test_a_guest_never_lists_or_opens_a_chunk(self, server):
        visitor = guest(server)
        for method, path, body in NEVER_FOR_GUESTS:
            reply = visitor.request(method, path.format("handbook"), json=body)
            assert signin_refusal(reply), f"{method} {path}: {reply.text}"
            assert "Payment" not in reply.text

    def test_who_may_search_is_told_to_a_guest_as_counts_alone(self, server):
        reply = guest(server).get("/api/v1/policies")
        assert reply.status_code == 200, reply.text
        data = reply.json()["data"]
        assert data["guests"] is True and data["gated"] is False, "nothing is gated on this server: no collection store"
        assert sorted(data["collections"]) == ["handbook", "secret", "walled"]
        assert all("policy" not in entry for entry in data["collections"].values()), "the list itself is for people signed in"

    def test_a_guest_reads_the_counts_a_day_without_the_sign_in_counts_and_what_was_written(self, server):
        visitor = guest(server)
        counts = visitor.get("/api/v1/access/daily")
        assert counts.status_code == 200, counts.text
        data = counts.json()["data"]
        assert data["available"] is True and "searches" in data and "search_ms_median" in data
        assert not {"signins", "refused", "denied"} & set(data), "who signed in and who was turned away is about people"
        written = visitor.get("/api/v1/growth?days=7")
        assert written.status_code == 200, written.text
        assert set(written.json()["data"]) >= {"days", "written", "low"} and "handbook" not in written.text

    def test_nothing_else_about_the_server_is_open_to_a_guest(self, server):
        visitor = guest(server)
        for path in ("/api/v1/info", "/api/v1/audit", "/api/v1/access", "/api/v1/keys", "/auth/people", "/api/v1/documents", "/api/v1/cache/stats"):
            assert signin_refusal(visitor.get(path)), path


# ------------------------------------------------------ what a guest reads


class TestWhatAGuestReads:
    """What is shared, and how the setups scored. Never a search, whichever route."""

    @pytest.mark.parametrize(
        "path, body", GUEST_SEARCHES, ids=["search", "text-search", "text-hybrid-search", "keyword-search", "search at the older address"]
    )
    def test_every_search_sends_a_guest_to_sign_in_even_on_a_shared_collection(self, server, path, body):
        reply = guest(server).post(path.format("handbook"), params={"snippet": "5000"}, json=body)
        assert signin_refusal(reply), reply.text
        assert "results" not in reply.text and LONG.strip() not in reply.text and SHORT not in reply.text
        assert not [r for r in access(server, event="search") if r.get("who") == "guest"], "and nothing was searched"

    def test_the_same_search_answers_a_key_with_the_whole_chunk_and_its_vector(self, server):
        body = {"query": [1, 0, 0, 0], "limit": 1, "include_vectors": True}
        ours = server[0].post(SEARCH, json=body, headers=KEYED).json()["data"]["results"][0]
        assert ours["id"] == "h-long" and ours["text"] == LONG and len(ours["vector"]) == 4, "the route works; it is shut to a guest"


# ------------------------------------------------- what a guest may not do


class TestWhatAGuestMayNotDo:
    def test_the_older_searches_are_shut_to_a_guest_too(self, server):
        visitor = guest(server)
        for path, body in OTHER_SEARCHES:
            reply = visitor.post(path.format("handbook"), json=body)
            assert signin_refusal(reply), f"{path}: {reply.text}"
            assert "results" not in reply.text
        hybrid, body = OTHER_SEARCHES[0]
        assert server[0].post(hybrid.format("handbook"), json=body, headers=KEYED).status_code == 200, "the route works; it is shut to a guest"
        assert not [r for r in access(server, event="search") if r.get("who") == "guest"], "and nothing was searched"

    def test_a_guest_writes_nothing(self, server):
        visitor = guest(server)
        for method, path, body in WRITES:
            reply = visitor.request(method, path.format("handbook"), json=body)
            assert signin_refusal(reply), f"{method} {path}: {reply.text}"
        client = server[0]
        assert client.get(SHARED, headers=KEYED).json()["data"]["count"] == 2
        assert client.get("/api/v1/collections/made", headers=KEYED).status_code == 404
        assert names(visitor) == ["handbook", "secret", "walled"], "still there"


class TestTheAccessLog:
    def test_a_guests_refused_search_is_not_a_search_in_the_log_and_repeats_no_query(self, server):
        visitor = guest(server)
        assert signin_refusal(visitor.post(SEARCH, json=QUERY))
        assert signin_refusal(visitor.post(SHARED + "/keyword-search", json={"query_text": "invoice"}))
        assert not [r for r in access(server, event="search") if r.get("who") == "guest"], "nothing was searched"
        assert "invoice" not in json.dumps(access(server)), "the log repeats no query and no text"


class TestEvaluationRuns:
    """How every setup scored is for everybody who reaches the page, a guest included: it says nothing about what is stored. The golden questions are not."""

    SETUP = {"key": "vectrixdb.dense.builtin", "target": "VectrixDB", "engine": "VectrixDB", "engine_kind": "vectrixdb", "method": "dense",
             "method_label": "Dense", "models": [], "search": {"mode": "dense"}, "ranks": [1, None], "times_ms": [3.0, 4.0], "error": None}
    RAW = b'{"id": "g1", "question": "When is payment due?", "expected": ["terms.pdf"]}\n{"id": "g2", "question": "Who handles refunds?", "expected": ["faq.pdf"]}\n'

    def saved(self, data, raw=None):
        from vectrixdb._eval_report import ReportStore, build_report

        sha = hashlib.sha256(raw).hexdigest() if raw else "ef" * 32
        return ReportStore(data / "evaluations").save(build_report([self.SETUP], {"sha256": sha, "questions": 2, "labelled": 2}, created=0), golden=raw), sha

    def test_a_viewer_and_a_guest_read_the_runs(self, server, data):
        run, _ = self.saved(data)
        viewer = person(server, "vi@example.com")
        assert viewer.get("/api/v1/evaluations/latest").json()["data"]["id"] == run
        assert viewer.get("/api/v1/evaluations").status_code == 200
        stranger = guest(server)
        assert stranger.get("/api/v1/evaluations/latest").json()["data"]["id"] == run
        assert stranger.get("/api/v1/evaluations").status_code == 200
        assert stranger.get("/api/v1/evaluations/latest").json()["data"]["golden_download"] is False

    def test_only_an_admin_signed_in_as_a_person_downloads_the_golden_questions(self, server, data):
        run, sha = self.saved(data, self.RAW)
        admin = person(server, "ada@example.com")
        assert admin.get("/api/v1/evaluations/latest").json()["data"]["golden_download"] is True
        got = admin.get(f"/api/v1/evaluations/{run}/golden")
        assert got.status_code == 200 and got.content == self.RAW
        assert got.headers["content-disposition"] == f'attachment; filename="golden-{sha[:8]}.jsonl"'
        assert "no-store" in got.headers["cache-control"]
        assert [r["route"] for r in access(server) if r.get("action") == "evaluation.golden"] == [f"GET /api/v1/evaluations/{run}/golden"], "written down, like any read"
        for email in ("olu@example.com", "vi@example.com"):
            browser = person(server, email)
            assert browser.get("/api/v1/evaluations/latest").json()["data"]["golden_download"] is False, email
            assert browser.get(f"/api/v1/evaluations/{run}/golden").status_code == 403, email
        # The server's own key holds the admin role, and it is a script, not a person.
        assert server[0].get(f"/api/v1/evaluations/{run}/golden", headers=KEYED).status_code == 403
        assert server[0].get("/api/v1/evaluations/latest", headers=KEYED).json()["data"]["golden_download"] is False
        assert signin_refusal(guest(server).get(f"/api/v1/evaluations/{run}/golden"))

    def test_a_run_whose_questions_were_not_kept_offers_no_download(self, server, data):
        run, _ = self.saved(data)
        admin = person(server, "ada@example.com")
        assert admin.get("/api/v1/evaluations/latest").json()["data"]["golden_download"] is False
        assert admin.get(f"/api/v1/evaluations/{run}/golden").status_code == 404
