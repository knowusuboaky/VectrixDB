"""A key an app is given: how it arrives, what it reaches, and when it stops.

Three things are being held to. A key may arrive as ``api-key`` or as a Bearer
token, because an app generated from the OpenAPI document reaches for the
second. A key made for one collection reaches that collection and the server's
own description, and a route that cuts across collections is refused even
though no collection is named in its path. And a key given an expiry stops
working on its own, without anybody remembering to revoke it.
"""

from __future__ import annotations

import os
import sys
import time

import numpy as np
import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("jwt", reason="the signin extra is not installed")
pytest.importorskip("cryptography", reason="the signin extra is not installed")

from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.signin import SignInConfig  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))

SECRET = "k" * 48
PUBLIC = "https://vectors.example.test"
KEY = "the-admin-api-key"
VECTORS = {"alpha": [1, 0, 0, 0], "beta": [0, 1, 0, 0], "gamma": [0, 0, 1, 0]}


def _embed(texts):
    return np.array([VECTORS[t] for t in texts], dtype=np.float32)


@pytest.fixture
def data(tmp_path):
    """Two collections, so a key scoped to one has something to be kept out of."""
    root = tmp_path / "db"
    for name, words in (("handbook", ["alpha", "beta"]), ("payroll", ["gamma"])):
        one = Vectrix(name, path=str(root), dimension=4, embed_fn=_embed, embedding_cache=False)
        one.add(words, ids=[f"{name}-{w}" for w in words])
        one.close()
    return root


@pytest.fixture
def server(data, monkeypatch):
    from vectrixdb.api.server import create_app

    monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
    monkeypatch.delenv("VECTRIXDB_AUDIT_JSONL", raising=False)
    config = SignInConfig(
        methods=("email",),
        secrets=(SECRET,),
        public_url=PUBLIC,
        users=(("ada@example.com", "admin"),),
        store_path=data / "auth" / "signin.db",
        access_log=data / "auth" / "access.jsonl",
        sender=lambda to, subject, text: None,
    )
    with TestClient(
        create_app(db_path=str(data), enable_dashboard=False, signin=config), base_url=PUBLIC
    ) as client:
        yield client


ADMIN = {"api-key": KEY}


def make(client: TestClient, **body) -> dict:
    """A named key, made the way the dashboard makes one."""
    body.setdefault("name", "an-app")
    body.setdefault("role", "searcher")
    reply = client.post("/api/v1/keys", json=body, headers=ADMIN)
    assert reply.status_code == 200, reply.text
    return reply.json()["data"]


# --------------------------------------------------------- how a key arrives


class TestTheKeyMayArriveEitherWay:
    def test_a_bearer_token_is_the_same_key(self, server):
        key = make(server)["key"]
        asked = server.get("/api/v1/collections", headers={"Authorization": f"Bearer {key}"})
        assert asked.status_code == 200

    def test_the_header_still_works(self, server):
        key = make(server)["key"]
        assert server.get("/api/v1/collections", headers={"api-key": key}).status_code == 200

    def test_the_api_key_header_is_the_one_read_when_both_are_given(self, server):
        key = make(server)["key"]
        both = {"api-key": key, "Authorization": "Bearer not-a-key"}
        assert server.get("/api/v1/collections", headers=both).status_code == 200
        swapped = {"api-key": "not-a-key", "Authorization": f"Bearer {key}"}
        assert server.get("/api/v1/collections", headers=swapped).status_code == 401

    def test_a_bearer_token_that_is_not_a_key_is_refused(self, server):
        reply = server.get("/api/v1/collections", headers={"Authorization": "Bearer nope"})
        assert reply.status_code == 401 and reply.json()["message"] == "Invalid API key"

    @pytest.mark.parametrize("value", ["Basic abc", "Bearer", "Bearer   ", "", "Token abc"])
    def test_anything_that_is_not_a_bearer_token_is_not_a_key(self, server, value):
        """It reads as nobody, so the reply is the one an unsigned-in caller gets."""
        reply = server.get("/api/v1/collections", headers={"Authorization": value})
        assert reply.status_code == 401 and reply.json()["data"] == {"signin": True}


# ------------------------------------------------------------- what it reaches


class TestAKeyMadeForOneCollection:
    @pytest.fixture
    def scoped(self, server):
        return {"api-key": make(server, collections=["handbook"])["key"]}

    def test_it_reaches_the_collection_it_was_made_for(self, server, scoped):
        assert server.get("/api/v1/collections/handbook", headers=scoped).status_code == 200
        found = server.post(
            "/api/v1/collections/handbook/search", json={"query": VECTORS["alpha"]}, headers=scoped
        )
        assert found.status_code == 200

    def test_another_collection_reads_as_one_that_is_not_there(self, server, scoped):
        reply = server.get("/api/v1/collections/payroll", headers=scoped)
        assert reply.status_code == 404
        # The refusal says what was asked for and nothing about what is here.
        assert "payroll" in reply.json()["message"] and "handbook" not in reply.text

    def test_it_is_the_reply_a_missing_collection_gets_to_the_letter(self, server, scoped):
        """Any difference between the two is how a caller learns what else is here."""
        kept_out = server.get("/api/v1/collections/payroll", headers=scoped)
        not_there = server.get("/api/v1/collections/nowhere", headers=ADMIN)
        assert kept_out.status_code == not_there.status_code == 404
        assert kept_out.text.replace("payroll", "nowhere") == not_there.text
        assert server.get("/api/v1/collections/nowhere", headers=scoped).text == not_there.text

    def test_it_cannot_search_a_collection_it_was_not_made_for(self, server, scoped):
        found = server.post(
            "/api/v1/collections/payroll/search", json={"query": VECTORS["gamma"]}, headers=scoped
        )
        assert found.status_code == 404

    def test_the_listing_holds_only_its_own(self, server, scoped):
        names = [
            c["name"]
            for c in server.get("/api/v1/collections", headers=scoped).json()["collections"]
        ]
        assert names == ["handbook"]
        everything = [
            c["name"]
            for c in server.get("/api/v1/collections", headers=ADMIN).json()["collections"]
        ]
        assert sorted(everything) == ["handbook", "payroll"]

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/documents",
            "/api/v1/policies",
            "/api/v1/evaluations",
            "/api/v1/audit",
            "/api/v1/info/extended",
        ],
    )
    def test_a_route_that_cuts_across_collections_is_refused(self, server, scoped, path):
        """None of these names a collection in its path, and every one of them
        would hand back something about the collections this key is kept out of."""
        reply = server.get(path, headers=scoped)
        assert reply.status_code == 403 and "handbook" in reply.json()["message"]

    def test_it_cannot_make_a_collection(self, server, scoped):
        made = server.post(
            "/api/v1/collections", json={"name": "new", "dimension": 4}, headers=scoped
        )
        assert made.status_code == 403

    @pytest.mark.parametrize(
        "path", ["/health", "/api/v1/info", "/api/v1/models", "/api/v1/extractors", "/openapi.json"]
    )
    def test_it_may_still_read_the_server_describing_itself(self, server, scoped, path):
        assert server.get(path, headers=scoped).status_code == 200

    def test_a_key_with_no_scope_is_the_key_it_always_was(self, server):
        wide = {"api-key": make(server, name="wide")["key"]}
        assert server.get("/api/v1/collections/payroll", headers=wide).status_code == 200
        assert server.get("/api/v1/documents", headers=wide).status_code == 200
        names = [
            c["name"] for c in server.get("/api/v1/collections", headers=wide).json()["collections"]
        ]
        assert sorted(names) == ["handbook", "payroll"]


class TestWhatTheScopeRuleDecides:
    """The rule on its own, without a server in the way."""

    @pytest.fixture
    def rule(self):
        from vectrixdb.api.signin import outside_the_scope

        return lambda path, method="GET": outside_the_scope(path, method, ("handbook",))

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/collections/handbook",
            "/api/v1/collections/handbook/search",
            "/api/v1/collections/handbook/points/one",
            "/api/collections/handbook",
            "/api/v2/collections/handbook/points",
        ],
    )
    def test_its_own_collection_in_any_shape(self, rule, path):
        assert rule(path, "POST") is None

    def test_the_path_is_read_as_the_server_decoded_it(self, rule):
        """The server decodes a path once, before this rule sees it. Decoding
        it again let a key for one collection reach another whose name was
        that one's percent-escape."""
        from vectrixdb.api.signin import outside_the_scope

        assert outside_the_scope("/api/v1/collections/two words", "GET", ("two words",)) is None
        assert (
            outside_the_scope("/api/v1/collections/handbook%41", "GET", ("handbookA",)).status_code
            == 404
        )
        assert outside_the_scope("/api/v1/collections/handbook%41", "GET", ("handbook%41",)) is None
        assert rule("/api/v1/collections/pay/roll").status_code == 404

    @pytest.mark.parametrize(
        "path", ["/api/v1/collections/payroll", "/api/v1/collections/payroll/search"]
    )
    def test_another_collection_is_a_404(self, rule, path):
        assert rule(path, "POST").status_code == 404

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/documents",
            "/api/v1/keys",
            "/auth/me",
            "/dashboard/",
            "/api/v1/cache/stats",
            "/api/v1/ws/status",
        ],
    )
    def test_everything_else_is_refused(self, rule, path):
        assert rule(path).status_code == 403

    def test_the_short_list_it_may_read_is_read_only(self, rule):
        assert rule("/api/v1/collections", "GET") is None
        assert rule("/api/v1/collections", "POST").status_code == 403

    def test_a_route_added_later_is_refused_until_somebody_decides(self, rule):
        """The rule is written the closed way round, so a new route is out."""
        assert rule("/api/v1/something-invented-next-year").status_code == 403


# ---------------------------------------------------------------- when it ends


class Later:
    """The store's clock, moved on."""

    def __init__(self, seconds):
        self.by = seconds

    def time(self):
        return time.time() + self.by


class TestAKeyThatExpires:
    def test_it_works_until_the_day_and_not_after(self, server, monkeypatch):
        from vectrixdb.signin import store as store_module

        key = {"api-key": make(server, expires_in_days=30)["key"]}
        assert server.get("/api/v1/collections", headers=key).status_code == 200
        monkeypatch.setattr(store_module, "time", Later(31 * 86400))
        reply = server.get("/api/v1/collections", headers=key)
        assert reply.status_code == 401 and reply.json()["message"] == "Invalid API key"

    def test_the_listing_says_which_keys_are_spent(self, server, monkeypatch):
        from vectrixdb.signin import store as store_module

        make(server, name="ends", expires_in_days=30)
        make(server, name="stays")
        monkeypatch.setattr(store_module, "time", Later(31 * 86400))
        keys = {
            k["name"]: k for k in server.get("/api/v1/keys", headers=ADMIN).json()["data"]["keys"]
        }
        assert keys["ends"]["expired"] is True and keys["stays"]["expired"] is False
        assert keys["stays"]["expires_at"] is None

    def test_the_day_it_stops_is_part_of_what_was_made(self, server):
        made = make(server, expires_in_days=7)
        assert made["expires_at"] == pytest.approx(time.time() + 7 * 86400, abs=60)
        assert made["expired"] is False

    @pytest.mark.parametrize("days", [0, -1, 3651])
    def test_a_length_that_is_no_length_is_refused(self, server, days):
        reply = server.post(
            "/api/v1/keys",
            json={"name": "x", "role": "reader", "expires_in_days": days},
            headers=ADMIN,
        )
        assert reply.status_code == 422

    def test_what_was_asked_for_is_written_down(self, server):
        make(server, name="for-an-app", collections=["handbook"], expires_in_days=30)
        lines = server.get("/api/v1/access", headers=ADMIN).json()["data"]["records"]
        made = [line for line in lines if line.get("event") == "key_created"][0]
        assert made["who"] == "for-an-app" and "handbook" in made["reason"]


# ------------------------------------------------------------------- the store


class TestTheStoreKeepsTheScope:
    @pytest.fixture
    def store(self, tmp_path):
        from vectrixdb.signin.store import SignInStore

        return SignInStore(tmp_path / "signin.db", (SECRET,))

    def test_a_key_remembers_what_it_was_made_for(self, store):
        made, key = store.create_key(
            "app",
            "reader",
            "ada@example.com",
            collections=["handbook"],
            expires_at=time.time() + 60,
        )
        assert made.collections == ("handbook",) and store.api_key(key).collections == ("handbook",)
        assert made.public()["collections"] == ["handbook"]

    def test_the_scope_is_tidied_and_keeps_its_order(self, store):
        made, _ = store.create_key("app", "reader", None, collections=[" b ", "a", "b", "", None])
        assert made.collections == ("b", "a")

    def test_no_scope_means_every_collection(self, store):
        made, _ = store.create_key("app", "reader", None)
        assert made.collections == () and made.expires_at is None
        assert made.may_reach("anything") and made.may_reach(None)

    def test_a_scoped_key_knows_what_is_not_its(self, store):
        made, _ = store.create_key("app", "reader", None, collections=["handbook"])
        assert made.may_reach("handbook") and not made.may_reach("payroll")
        assert made.may_reach(None), "a route that names no collection is decided elsewhere"

    def test_an_expiry_already_gone_is_refused(self, store):
        with pytest.raises(ConfigurationError, match="already expired"):
            store.create_key("app", "reader", None, expires_at=time.time() - 1)

    def test_a_scope_too_long_to_be_a_scope_is_refused(self, store):
        with pytest.raises(ConfigurationError, match="at most 64"):
            store.create_key("app", "reader", None, collections=[f"c{n}" for n in range(65)])
        with pytest.raises(ConfigurationError, match="too long"):
            store.create_key("app", "reader", None, collections=["x" * 129])

    def test_a_key_written_before_any_of_this_reads_as_what_it_was(self, store):
        from vectrixdb.signin.store import SignInStore

        old = {
            "key_id": "abc",
            "name": "old",
            "role": "reader",
            "prefix": "vx_abc_",
            "created_at": 1.0,
        }
        key = SignInStore._key(old, None)
        assert key.collections == () and key.expires_at is None and key.expired is False
        assert key.public()["collections"] == []
