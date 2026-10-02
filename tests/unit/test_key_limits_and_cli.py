"""A rate limit several servers share, and keys made from the server's own console.

The guest limit used to be counted in each process, so three servers behind a
load balancer gave every guest three allowances and a restart forgave
everybody. A named key had no limit at all. Both are one count in the sign-in
store now. And a key could only be made in the dashboard or over the API,
which left a single sign-on server with no way to make its first one.
"""

from __future__ import annotations

import os
import re
import time

import numpy as np
import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("jwt", reason="the signin extra is not installed")
pytest.importorskip("cryptography", reason="the signin extra is not installed")
pytest.importorskip("typer", reason="the CLI extra is not installed")

from fastapi.testclient import TestClient  # noqa: E402
from rich.console import Console  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.cli import app  # noqa: E402
from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.signin import SignInConfig, SignInStore  # noqa: E402

SECRET = "k" * 48
PUBLIC = "https://vectors.example.test"
KEY = "the-admin-api-key"
ADMIN = {"api-key": KEY}


def _embed(texts):
    return np.array([[1, 0, 0, 0] for _ in texts], dtype=np.float32)


# ------------------------------------------------------------------ the count


class TestOneCountHoweverManyServers:
    @pytest.fixture
    def servers(self, tmp_path):
        """Two servers over one sign-in file, as two processes behind a load balancer are."""
        one, two = SignInStore(tmp_path / "signin.db", (SECRET,)), SignInStore(tmp_path / "signin.db", (SECRET,))
        yield one, two
        one.close()
        two.close()

    def test_the_allowance_is_spent_once_not_once_a_server(self, servers):
        one, two = servers
        answers = [(one if n % 2 else two).within_rate("guest:203.0.113.9", 4)[0] for n in range(6)]
        assert answers == [True, True, True, True, False, False]

    def test_one_callers_burst_is_not_anothers(self, servers):
        one, _ = servers
        assert [one.within_rate("key:aaa", 1)[0] for _ in range(2)] == [True, False]
        assert one.within_rate("key:bbb", 1)[0] is True

    def test_it_says_how_long_is_left_of_the_minute(self, servers):
        allowed, wait = servers[0].within_rate("guest:x", 1)
        assert allowed and 1 <= wait <= 60

    def test_the_next_minute_is_a_new_allowance(self, servers, monkeypatch):
        from vectrixdb.signin import store as store_module

        one, _ = servers
        assert [one.within_rate("guest:x", 1)[0] for _ in range(2)] == [True, False]

        class Later:
            @staticmethod
            def time():
                return time.time() + 61

        monkeypatch.setattr(store_module, "time", Later)
        assert one.within_rate("guest:x", 1)[0] is True

    def test_the_address_is_not_what_is_kept(self, servers, tmp_path):
        servers[0].within_rate("guest:203.0.113.77", 5)
        assert b"203.0.113.77" not in (tmp_path / "signin.db").read_bytes()


# ------------------------------------------------------------- over the wire


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "db"
    one = Vectrix("handbook", path=str(root), dimension=4, embed_fn=_embed, embedding_cache=False)
    one.add(["alpha"], ids=["a"])
    one.close()
    return root


def serve(data, **over):
    from vectrixdb.api.server import create_app

    config = SignInConfig(
        methods=("email",), secrets=(SECRET,), public_url=PUBLIC, users=(("ada@example.com", "admin"),),
        store_path=data / "auth" / "signin.db", access_log=data / "auth" / "access.jsonl",
        sender=lambda to, subject, text: None, **over,
    )
    return TestClient(create_app(db_path=str(data), enable_dashboard=False, signin=config), base_url=PUBLIC)


class TestAKeyHasALimit:
    @pytest.fixture(autouse=True)
    def admin_key(self, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
        monkeypatch.delenv("VECTRIXDB_AUDIT_JSONL", raising=False)

    def make(self, client, **body):
        reply = client.post("/api/v1/keys", json={"name": "an-app", "role": "searcher", **body}, headers=ADMIN)
        assert reply.status_code == 200, reply.text
        return reply.json()["data"]

    def test_a_key_with_a_number_stops_at_it_and_says_how_long_to_wait(self, data):
        with serve(data) as client:
            made = self.make(client, requests_per_minute=3)
            assert made["requests_per_minute"] == 3
            key = {"api-key": made["key"]}
            codes = [client.get("/api/v1/collections", headers=key).status_code for _ in range(5)]
            assert codes == [200, 200, 200, 429, 429]
            refused = client.get("/api/v1/collections", headers=key)
            assert 1 <= int(refused.headers["retry-after"]) <= 60
            body = refused.json()
            assert set(body) == {"ok", "message", "data", "detail"} and body["data"]["retry_after"] >= 1
            assert "an-app" in body["message"] and "3 requests a minute" in body["message"]

    def test_the_servers_setting_covers_a_key_with_no_number(self, data):
        with serve(data, key_requests_per_minute=2) as client:
            key = {"api-key": self.make(client)["key"]}
            assert [client.get("/api/v1/collections", headers=key).status_code for _ in range(3)] == [200, 200, 429]

    def test_the_keys_own_number_wins_over_the_servers(self, data):
        with serve(data, key_requests_per_minute=1) as client:
            key = {"api-key": self.make(client, requests_per_minute=3)["key"]}
            assert [client.get("/api/v1/collections", headers=key).status_code for _ in range(4)] == [200, 200, 200, 429]

    def test_with_neither_a_key_is_as_unlimited_as_it_always_was(self, data):
        with serve(data) as client:
            made = self.make(client)
            assert made["requests_per_minute"] is None
            key = {"api-key": made["key"]}
            assert {client.get("/api/v1/collections", headers=key).status_code for _ in range(40)} == {200}

    def test_the_servers_own_key_is_never_limited(self, data):
        with serve(data, key_requests_per_minute=1) as client:
            assert {client.get("/api/v1/collections", headers=ADMIN).status_code for _ in range(5)} == {200}

    @pytest.mark.parametrize("number", [0, -5, 100001])
    def test_a_number_that_is_no_number_is_refused(self, data, number):
        with serve(data) as client:
            reply = client.post("/api/v1/keys", json={"name": "x", "role": "reader", "requests_per_minute": number}, headers=ADMIN)
            assert reply.status_code == 422

    def test_the_store_refuses_it_too(self, tmp_path):
        store = SignInStore(tmp_path / "signin.db", (SECRET,))
        with pytest.raises(ConfigurationError, match="no requests is no key"):
            store.create_key("x", "reader", None, per_minute=0)
        store.close()

    def test_a_key_written_before_this_has_no_number(self):
        old = {"key_id": "abc", "name": "old", "role": "reader", "prefix": "vx_abc_", "created_at": 1.0}
        assert SignInStore._key(old, None).per_minute is None


class TestTheSetting:
    def test_it_is_read_from_the_environment(self, tmp_path):
        env = {"VECTRIXDB_SIGNIN": "email", "VECTRIXDB_SIGNIN_SECRET": SECRET, "VECTRIXDB_PUBLIC_URL": PUBLIC, "VECTRIXDB_KEY_REQUESTS_PER_MINUTE": "120"}
        assert SignInConfig.from_env(tmp_path, env).key_requests_per_minute == 120

    def test_left_out_there_is_no_limit(self, tmp_path):
        env = {"VECTRIXDB_SIGNIN": "email", "VECTRIXDB_SIGNIN_SECRET": SECRET, "VECTRIXDB_PUBLIC_URL": PUBLIC}
        assert SignInConfig.from_env(tmp_path, env).key_requests_per_minute is None

    @pytest.mark.parametrize("given", ["0", "-1", "many", "1.5"])
    def test_a_value_that_is_not_a_number_of_requests_stops_the_start(self, tmp_path, given):
        env = {"VECTRIXDB_SIGNIN": "email", "VECTRIXDB_SIGNIN_SECRET": SECRET, "VECTRIXDB_PUBLIC_URL": PUBLIC, "VECTRIXDB_KEY_REQUESTS_PER_MINUTE": given}
        with pytest.raises(ConfigurationError, match="VECTRIXDB_KEY_REQUESTS_PER_MINUTE"):
            SignInConfig.from_env(tmp_path, env)


# ------------------------------------------------------------- the console


@pytest.fixture
def signin_on(monkeypatch):
    for name in [n for n in os.environ if n.startswith("VECTRIXDB_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("VECTRIXDB_SIGNIN", "email")
    monkeypatch.setenv("VECTRIXDB_SIGNIN_SECRET", SECRET)
    monkeypatch.setenv("VECTRIXDB_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
    import vectrixdb.cli

    monkeypatch.setattr(vectrixdb.cli, "console", Console(width=220))


def flat(output: str) -> str:
    return " ".join(output.split())


class TestKeysFromTheConsole:
    def keys(self, db, *args):
        return CliRunner().invoke(app, ["keys", *args, "--path", str(db)])

    def test_a_key_is_made_shown_once_and_works(self, signin_on, data, monkeypatch):
        made = self.keys(data, "add", "handbook-bot", "--collection", "handbook", "--days", "90", "--per-minute", "60")
        assert made.exit_code == 0, made.output
        said = flat(made.output)
        assert "handbook-bot" in said and "searcher" in said and "handbook" in said and "60 a minute" in said
        key = re.search(r"vx_[0-9a-f]{8}_[\w-]+", made.output).group(0)

        monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
        with serve(data) as client:
            assert client.get("/api/v1/collections/handbook", headers={"Authorization": f"Bearer {key}"}).status_code == 200
            listed = client.get("/api/v1/keys", headers=ADMIN).json()["data"]["keys"][0]
            assert listed["created_by"] == "server console" and listed["collections"] == ["handbook"]

    def test_the_key_is_not_in_the_listing_and_not_in_the_file(self, signin_on, data):
        made = self.keys(data, "add", "nightly")
        key = re.search(r"vx_[0-9a-f]{8}_[\w-]+", made.output).group(0)
        listed = self.keys(data, "list")
        assert "nightly" in listed.output and "every collection" in flat(listed.output) and key not in listed.output
        assert key.encode() not in (data / "auth" / "signin.db").read_bytes()

    def test_making_one_is_written_down(self, signin_on, data):
        self.keys(data, "add", "nightly", "--collection", "handbook")
        log = (data / "auth" / "access.jsonl").read_text(encoding="utf-8")
        assert '"key_created"' in log and "server console" in log and "handbook" in log

    def test_revoking_stops_it_and_a_wrong_id_says_so(self, signin_on, data):
        self.keys(data, "add", "nightly")
        store = SignInStore(data / "auth" / "signin.db", (SECRET,))
        key_id = store.keys()[0].key_id
        store.close()
        assert self.keys(data, "revoke", "nope0000").exit_code == 1
        done = self.keys(data, "revoke", key_id)
        assert done.exit_code == 0 and "Revoked nightly" in done.output
        assert "No keys yet" in self.keys(data, "list").output

    @pytest.mark.parametrize("args, said", [
        (["add", "x", "--role", "admin"], "not a role a key can have"),
        (["add", "x", "--days", "0"], "--days"),
        (["add", "x", "--per-minute", "0"], "no requests is no key"),
    ])
    def test_what_cannot_be_made_is_refused_in_words(self, signin_on, data, args, said):
        result = self.keys(data, *args)
        assert result.exit_code == 2 and said in flat(result.output)

    def test_with_sign_in_off_it_says_what_to_set(self, data, monkeypatch):
        for name in [n for n in os.environ if n.startswith("VECTRIXDB_")]:
            monkeypatch.delenv(name)
        result = self.keys(data, "list")
        assert result.exit_code == 2 and "VECTRIXDB_SIGNIN" in flat(result.output)
