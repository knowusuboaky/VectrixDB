"""Developer Access: accounts named in the settings and one password, for trying the roles on one machine.

What is being held to: it is off unless VECTRIXDB_DEVELOPER_ACCESS says on;
the server will not start with it unless the public address is this
machine's; it needs its accounts, name:role, and a password, with no default,
each refused by name when missing or wrong, and the password may come from a
file; it answers only a connection from this machine, whatever a header
claims, and 404 otherwise, the same as an address that is not there; the
name and the password are taken together, a wrong try is counted and the
door shuts after five; each sign-in is written down; the session it opens
has the role the settings give the account and says how it came in, cannot
be used from another machine, and ends when Developer Access is turned off;
a change that matters asks for the password again; and the sign-in page is
told about it only on this machine.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")

sys.path.insert(0, os.path.dirname(__file__))
from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.signin import DeveloperAccess, SignInConfig  # noqa: E402

SECRET = "k" * 48
LOCAL = "http://localhost:7337"
PASSWORD = "trying-the-roles-here"
HERE = ("127.0.0.1", 50000)
ELSEWHERE = ("203.0.113.9", 50000)


def create_app(**kwargs):
    """The server as it is now; another test re-imports vectrixdb.api."""
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


def _env(**over) -> dict:
    env = {
        "VECTRIXDB_SIGNIN_SECRET": SECRET,
        "VECTRIXDB_PUBLIC_URL": LOCAL,
        "VECTRIXDB_DEVELOPER_ACCESS": "on",
        "VECTRIXDB_DEVELOPER_USERS": "admin.user:admin,operator.user:operator,viewer.user:viewer",
        "VECTRIXDB_DEVELOPER_PASSWORD": PASSWORD,
    }
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


def _config(root: Path, developer=True, **over) -> SignInConfig:
    base = {
        "methods": (),
        "secrets": (SECRET,),
        "public_url": LOCAL,
        "store_path": root / "auth" / "signin.db",
        "access_log": root / "auth" / "access.jsonl",
        "developer": DeveloperAccess(
            accounts={"admin.user": "admin", "viewer.user": "viewer"}, password=PASSWORD
        )
        if developer
        else None,
    }
    base.update(over)
    return SignInConfig(**base)


@pytest.fixture
def serve(tmp_path, monkeypatch):
    """A server on this machine with Developer Access as the test asks. Call it with where the caller is."""
    monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
    monkeypatch.delenv("VECTRIXDB_AUDIT_JSONL", raising=False)
    built = []

    def build(client=HERE, **over):
        root = tmp_path / "db"
        app = create_app(db_path=str(root), enable_dashboard=False, signin=_config(root, **over))
        built.append(app)
        return TestClient(app, base_url=LOCAL, client=client)

    yield build
    for app in built:
        if app.state.signin is not None:
            app.state.signin.close()


def sign_in(client: TestClient, username: str = "admin.user", password: str = PASSWORD):
    return client.post("/auth/developer", json={"username": username, "password": password})


def csrf(client: TestClient) -> dict:
    return {"x-csrf-token": client.get("/auth/me").json()["data"]["csrf"]}


def logged(client: TestClient, event: str) -> list:
    path = Path(client.app.state.signin.config.access_log)
    lines = (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if path.exists()
        else []
    )
    return [line for line in lines if line.get("event") == event]


# ----------------------------------------------------------- the settings


class TestTheSettings:
    def test_off_unless_asked_for(self, tmp_path):
        assert (
            SignInConfig.from_env(tmp_path, _env(VECTRIXDB_DEVELOPER_ACCESS=None)).developer is None
        )
        assert not SignInConfig.from_env(tmp_path, _env(VECTRIXDB_DEVELOPER_ACCESS="off")).enabled

    def test_on_it_holds_the_accounts_and_keeps_the_password_out_of_sight(self, tmp_path):
        got = SignInConfig.from_env(tmp_path, _env())
        assert got.enabled and got.methods == ()
        assert dict(got.developer.accounts) == {
            "admin.user": "admin",
            "operator.user": "operator",
            "viewer.user": "viewer",
        }
        assert PASSWORD not in repr(got.developer), "a secret never reaches a log line"

    def test_names_are_read_in_any_case(self, tmp_path):
        got = SignInConfig.from_env(
            tmp_path, _env(VECTRIXDB_DEVELOPER_USERS=" Admin.User:admin , ")
        )
        assert dict(got.developer.accounts) == {"admin.user": "admin"}

    @pytest.mark.parametrize(
        "public",
        [
            "https://vectors.company.com",
            "https://gateway.example.com/acme",
            "https://localhost.company.com",
        ],
    )
    def test_the_server_will_not_start_with_it_anywhere_but_this_machine(self, tmp_path, public):
        with pytest.raises(ConfigurationError, match="for this machine only"):
            SignInConfig.from_env(tmp_path, _env(VECTRIXDB_PUBLIC_URL=public))

    @pytest.mark.parametrize(
        "users, says",
        [
            ("", "names nobody"),
            ("admin.user", "should read name:role"),
            ("admin.user:root", "should read name:role"),
            ("admin user:admin", "should read name:role"),
            ("admin.user:admin,Admin.User:viewer", "names Admin.User twice"),
        ],
    )
    def test_the_accounts_are_checked_and_a_wrong_one_is_named(self, tmp_path, users, says):
        with pytest.raises(ConfigurationError, match=says):
            SignInConfig.from_env(tmp_path, _env(VECTRIXDB_DEVELOPER_USERS=users))

    def test_there_is_no_default_password(self, tmp_path):
        with pytest.raises(ConfigurationError, match="There is no default password"):
            SignInConfig.from_env(tmp_path, _env(VECTRIXDB_DEVELOPER_PASSWORD=None))

    def test_the_password_may_come_from_a_file(self, tmp_path):
        (tmp_path / "pw").write_text(PASSWORD + "\n", encoding="utf-8")
        got = SignInConfig.from_env(
            tmp_path,
            _env(
                VECTRIXDB_DEVELOPER_PASSWORD=None,
                VECTRIXDB_DEVELOPER_PASSWORD_FILE=str(tmp_path / "pw"),
            ),
        )
        assert got.developer.password == PASSWORD

    def test_it_goes_with_single_sign_on_set_up_yet_or_not(self, tmp_path):
        for way in ("oidc", "oidc,email"):
            got = SignInConfig.from_env(
                tmp_path, _env(VECTRIXDB_SIGNIN=way, VECTRIXDB_SIGNIN_USERS="ada@example.com:admin")
            )
            assert got.developer is not None and got.sso_pending, way

    def test_the_server_will_not_start_with_it_beside_the_lists_own_ways_alone(self, tmp_path):
        with pytest.raises(
            ConfigurationError, match="stands in for single sign-on on this machine"
        ):
            SignInConfig.from_env(tmp_path, _env(VECTRIXDB_SIGNIN="email"))

    def test_on_its_own_it_still_needs_what_any_sign_in_needs(self, tmp_path):
        with pytest.raises(ConfigurationError, match="VECTRIXDB_SIGNIN_SECRET"):
            SignInConfig.from_env(tmp_path, _env(VECTRIXDB_SIGNIN_SECRET=None))


# ------------------------------------------------------------- the door


class TestOnlyThisMachine:
    def test_off_its_address_is_not_there(self, serve):
        client = serve(developer=False, methods=("email",), users=(("ada@example.com", "admin"),))
        assert client.get("/auth/developer").status_code == 404
        assert sign_in(client).status_code == 404

    def test_from_another_machine_its_address_is_not_there_either(self, serve):
        client = serve(client=ELSEWHERE)
        assert client.get("/auth/developer").status_code == 404
        assert sign_in(client).status_code == 404
        assert logged(client, "signin") == []

    def test_a_header_claiming_this_machine_counts_for_nothing(self, serve, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_TRUSTED_PROXIES", "203.0.113.0/24")
        client = serve(client=ELSEWHERE)
        forged = {
            "X-Forwarded-For": "127.0.0.1",
            "Host": "localhost",
            "Origin": LOCAL,
            "Referer": LOCAL + "/dashboard/",
        }
        assert client.get("/auth/developer", headers=forged).status_code == 404
        assert (
            client.post(
                "/auth/developer",
                json={"username": "admin.user", "password": PASSWORD},
                headers=forged,
            ).status_code
            == 404
        )

    def test_on_this_machine_it_lists_the_accounts_and_nothing_secret(self, serve):
        reply = serve().get("/auth/developer")
        assert reply.status_code == 200
        assert reply.json()["data"] == {
            "accounts": [
                {"username": "admin.user", "role": "admin"},
                {"username": "viewer.user", "role": "viewer"},
            ]
        }
        assert PASSWORD not in reply.text

    def test_the_sign_in_page_is_told_about_it_only_on_this_machine(self, serve):
        assert serve().get("/auth/me").json()["data"]["methods"] == {"developer": {}}
        assert "developer" not in serve(client=ELSEWHERE).get("/auth/me").json()["data"]["methods"]


# --------------------------------------------------------- signing in


class TestSigningIn:
    def test_the_account_signs_in_with_the_role_the_settings_give_it(self, serve):
        client = serve()
        reply = sign_in(client, "viewer.user")
        assert reply.status_code == 200
        me = client.get("/auth/me").json()["data"]
        assert (
            me["person"]["role"] == "viewer"
            and me["person"]["method"] == "developer"
            and me["person"]["name"] == "viewer.user"
        )
        assert me["person"]["email"] is None, "nobody's address: an account for trying a role"
        [line] = logged(client, "signin")
        assert (
            line["who"] == "viewer.user"
            and line["method"] == "developer"
            and line["role"] == "viewer"
        )

    def test_the_name_is_taken_in_any_case(self, serve):
        client = serve()
        assert sign_in(client, " Admin.User ").status_code == 200
        assert client.get("/auth/me").json()["data"]["person"]["role"] == "admin"

    @pytest.mark.parametrize(
        "username, password",
        [("admin.user", "not-the-password"), ("nobody.user", PASSWORD), ("", "")],
    )
    def test_a_wrong_name_or_password_is_refused_with_the_same_words(
        self, serve, username, password
    ):
        client = serve()
        reply = sign_in(client, username, password)
        assert reply.status_code == 401
        assert (
            reply.json()["message"]
            == "That username and password were not accepted together. Check both and try again."
        )
        assert client.get("/auth/me").status_code == 401
        assert logged(client, "signin_failed")[-1]["reason"] == "wrong"

    def test_the_door_shuts_after_five_wrong_tries(self, serve):
        client = serve()
        for _ in range(5):
            assert sign_in(client, password="wrong").status_code == 401
        held = sign_in(client)
        assert held.status_code == 429 and "Too many wrong tries" in held.json()["message"]

    def test_the_session_cannot_be_carried_to_another_machine(self, serve):
        here = serve()
        assert sign_in(here).status_code == 200
        elsewhere = TestClient(
            here.app, base_url=LOCAL, client=ELSEWHERE, cookies=dict(here.cookies)
        )
        assert elsewhere.get("/auth/me").status_code == 401
        assert elsewhere.get("/api/v1/collections").status_code == 401

    def test_turned_off_its_sessions_end_with_it(self, serve, tmp_path):
        client = serve()
        assert sign_in(client).status_code == 200
        cookies = dict(client.cookies)
        client.app.state.signin.close()
        root = tmp_path / "db"
        after = create_app(
            db_path=str(root),
            enable_dashboard=False,
            signin=_config(
                root, developer=False, methods=("email",), users=(("ada@example.com", "admin"),)
            ),
        )
        try:
            again = TestClient(after, base_url=LOCAL, client=HERE, cookies=cookies)
            assert again.get("/auth/me").status_code == 401
        finally:
            after.state.signin.close()


class TestAChangeThatMattersAsksForThePasswordAgain:
    def test_the_step_up_is_the_password(self, serve, monkeypatch):
        import time as clock

        client = serve()
        sign_in(client)
        now = clock.time()
        monkeypatch.setattr("vectrixdb.api.signin.time.time", lambda: now + 11 * 60)
        stale = client.post(
            "/api/v1/keys", json={"name": "etl", "role": "reader"}, headers=csrf(client)
        )
        assert stale.status_code == 403 and stale.json()["data"]["ways"] == ["password"]
        wrong = client.post("/auth/step-up", json={"password": "not-it"}, headers=csrf(client))
        assert (
            wrong.status_code == 401
            and wrong.json()["message"] == "That password was not accepted."
        )
        assert (
            client.post(
                "/auth/step-up", json={"password": PASSWORD}, headers=csrf(client)
            ).status_code
            == 200
        )
        monkeypatch.setattr("vectrixdb.api.signin.time.time", lambda: now + 12 * 60)
        made = client.post(
            "/api/v1/keys", json={"name": "etl", "role": "reader"}, headers=csrf(client)
        )
        assert made.status_code == 200, made.text
