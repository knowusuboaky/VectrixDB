"""The enterprise way in: single sign-on and the list's own ways together, from one People list.

What is being held to: the People list says who may sign in whichever way
they come, and what their role is, while the group is still asked; somebody
turned off on it is refused at the provider's door too; the settings' list
lets in beside it, with the groups' role; a session that came through single
sign-on for somebody on the list ends when their record changes or they are
taken off; nobody keeps a passkey beside single sign-on; after signing in
with single sign-on they may add an authenticator app from their account, sign in with it later, confirm a
change with it, and remove it again, the last one too, because single
sign-on still lets them in, while somebody single sign-on has never let in
keeps their only way; with VECTRIXDB_SSO_RECHECK_DAYS a passkey or a code
works only for somebody who signed in with single sign-on lately; an app's
token needs no place on the list, and VECTRIXDB_OIDC_TOKEN_ROLE gives it its
role, never an admin's; and the People list is there with single sign-on
alone.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")

sys.path.insert(0, os.path.dirname(__file__))
from fake_idp import ISSUER, FakeIdp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.signin import OidcConfig, SignInConfig, totp  # noqa: E402

SECRET = "k" * 48
PUBLIC = "https://vectors.example.test"
ADA = "ada@example.com"  # who the stand-in provider signs in, in g-admins
OLU = "olu@example.com"
ROLES = {"g-admins": "admin", "g-ops": "operator"}
ELEVEN_MINUTES = 11 * 60
DAY = 86400


def create_app(**kwargs):
    """The server as it is now; another test re-imports vectrixdb.api."""
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


class Mail:
    """Sign-in emails, kept instead of sent."""

    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, text):
        self.sent.append((to, subject, text))

    def token(self):
        return self.sent[-1][2].split("token=")[1].split()[0]


def _config(root: Path, **over) -> SignInConfig:
    base = {
        "methods": ("oidc", "email"),
        "secrets": (SECRET,),
        "public_url": PUBLIC,
        "users": ((ADA, "admin"), (OLU, "operator")),
        "store_path": root / "auth" / "signin.db",
        "access_log": root / "auth" / "access.jsonl",
        "sender": Mail(),
        "oidc": OidcConfig(issuer=ISSUER, client_id="vectrixdb", client_secret="s3cret", role_map=ROLES),
    }
    base.update(over)
    return SignInConfig(**base)


@pytest.fixture
def serve(tmp_path, monkeypatch):
    """A server with both ways on and a stand-in provider. Call it with what the test changes."""
    monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
    monkeypatch.delenv("VECTRIXDB_AUDIT_JSONL", raising=False)
    built = []

    def build(**over):
        idp = FakeIdp()
        root = tmp_path / f"db{len(built)}"
        config = _config(root, **over)
        app = create_app(db_path=str(root), enable_dashboard=False, signin=config, oidc_transport=idp.transport)
        client = TestClient(app, base_url=PUBLIC, follow_redirects=False)
        client.__enter__()  # the database opens with the app, as it does when the server starts
        built.append(client)
        return client, idp, config

    yield build
    for client in built:
        client.__exit__(None, None, None)


def sso(client: TestClient, idp: FakeIdp):
    start = client.get("/auth/oidc/start")
    code, state = idp.authorize(start.headers["location"])
    return client.get("/auth/oidc/callback", params={"code": code, "state": state})


def refused(reply) -> str:
    assert reply.status_code == 302 and "#/signin?error=" in reply.headers["location"], reply.headers.get("location")
    return reply.headers["location"].split("error=")[1]


def me(client: TestClient):
    reply = client.get("/auth/me")
    return reply.json()["data"]["person"] if reply.status_code == 200 else None


def csrf(client: TestClient) -> dict:
    return {"x-csrf-token": client.cookies.get("__Host-vx_csrf")}


def browser(client: TestClient) -> TestClient:
    return TestClient(client.app, base_url=PUBLIC, follow_redirects=False)


def later(monkeypatch, seconds: float) -> None:
    """Every clock the server and the provider read runs this many seconds ahead from now on."""
    now = time.time
    monkeypatch.setattr(time, "time", lambda: now() + seconds)


def add_authenticator(client: TestClient) -> str:
    """From their account: a new authenticator app, confirmed with a code from it. Its secret."""
    begun = client.post("/auth/me/authenticator/begin", headers=csrf(client))
    assert begun.status_code == 200, begun.text
    secret = begun.json()["data"]["secret"]
    confirmed = client.post("/auth/me/authenticator/confirm", json={"code": totp.code_at(secret, totp.step_now())}, headers=csrf(client))
    assert confirmed.status_code == 200, confirmed.text
    return secret


def code_sign_in(client: TestClient, email: str, secret: str, step_offset: int = 1):
    return client.post("/auth/email/verify", json={"email": email, "code": totp.code_at(secret, totp.step_now() + step_offset)})


def events(client: TestClient, event: str) -> list:
    return client.app.state.signin.access.recent(event=event)


# ------------------------------------------------------------- one list


class TestOneListForBothWays:
    def test_the_people_list_alone_is_enough_for_single_sign_on_to_start(self, serve):
        client, idp, config = serve()
        assert config.oidc.allowed_emails == ()
        assert sso(client, idp).status_code == 302 and me(client)["email"] == ADA

    def test_somebody_on_the_list_has_the_role_on_their_record_not_their_groups(self, serve):
        client, idp, _ = serve(users=((ADA, "viewer"), (OLU, "admin")))
        assert idp.person["groups"] == ["g-admins"], "a group that would make an admin"
        sso(client, idp)
        assert me(client)["role"] == "viewer"
        assert events(client, "signin")[0]["role"] == "viewer"

    def test_the_group_is_still_asked(self, serve):
        client, idp, _ = serve()
        idp.person["groups"] = ["unrelated"]
        assert refused(sso(client, idp)) == "no_role"

    def test_somebody_turned_off_on_the_list_is_refused_at_the_providers_door_too(self, serve):
        client, idp, _ = serve()
        client.app.state.signin.store.set_disabled(ADA, True)
        assert refused(sso(client, idp)) == "turned_off"
        assert me(client) is None

    def test_somebody_on_neither_list_is_refused_however_right_their_group(self, serve):
        client, idp, _ = serve()
        idp.person.update(sub="u-9", email="grace@example.com")
        assert refused(sso(client, idp)) == "not_listed"

    def test_the_settings_list_lets_in_beside_it_with_the_groups_role(self, serve):
        oidc = OidcConfig(issuer=ISSUER, client_id="vectrixdb", client_secret="s3cret", role_map=ROLES, allowed_emails=("@partner.example",))
        client, idp, _ = serve(oidc=oidc)
        idp.person.update(sub="u-5", email="bo@partner.example", groups=["g-ops"])
        sso(client, idp)
        assert me(client)["role"] == "operator"
        assert client.get("/auth/me/ways").json()["data"]["listed"] is False, "nothing of their own is kept here"


class TestTheirSessionFollowsTheirRecord:
    def test_a_change_of_role_ends_their_single_sign_on_session(self, serve):
        client, idp, _ = serve()
        sso(client, idp)
        assert me(client)["role"] == "admin"
        client.app.state.signin.store.put_person(ADA, "operator")
        assert me(client) is None, "a session carries the role it was opened with, so it ends"
        sso(client, idp)
        assert me(client)["role"] == "operator"

    def test_taken_off_the_list_their_session_ends_and_they_cannot_come_back(self, serve):
        client, idp, _ = serve()
        sso(client, idp)
        client.app.state.signin.store.remove_person(ADA)
        assert me(client) is None
        assert refused(sso(client, idp)) == "not_listed"

    def test_a_session_from_the_settings_list_holds_whatever_the_people_list_says(self, serve):
        oidc = OidcConfig(issuer=ISSUER, client_id="vectrixdb", client_secret="s3cret", role_map=ROLES, allowed_emails=("@partner.example",))
        client, idp, _ = serve(oidc=oidc)
        idp.person.update(sub="u-5", email="bo@partner.example", groups=["g-ops"])
        sso(client, idp)
        client.app.state.signin.store.put_person(OLU, "viewer")
        assert me(client)["email"] == "bo@partner.example"


# ---------------------------------------------------------- their own ways


class TestTheirOwnWays:
    def test_after_single_sign_on_they_add_an_authenticator_and_sign_in_with_it_later(self, serve):
        client, idp, _ = serve()
        sso(client, idp)
        assert client.get("/auth/me/ways").json()["data"]["listed"] is True
        secret = add_authenticator(client)
        assert client.get("/auth/me/ways").json()["data"]["authenticator"] is not None
        elsewhere = browser(client)
        signed = code_sign_in(elsewhere, ADA, secret)
        assert signed.status_code == 200, signed.text
        assert me(elsewhere)["method"] == "email" and me(elsewhere)["role"] == "admin"

    def test_nobody_keeps_a_passkey_beside_single_sign_on(self, serve):
        client, idp, config = serve()
        assert not config.own_passkeys
        assert client.get("/auth/me").json()["data"]["methods"]["email"]["passkeys"] is False
        assert client.post("/auth/passkey/begin").status_code == 404
        sso(client, idp)
        assert client.post("/auth/me/passkeys/begin", headers=csrf(client)).status_code == 404
        assert client.post("/auth/step-up/passkey/begin", headers=csrf(client)).status_code == 404
        assert client.get("/auth/me/ways").json()["data"]["own_passkeys"] is False

    def test_they_may_remove_the_last_one_because_single_sign_on_still_lets_them_in(self, serve):
        client, idp, _ = serve()
        sso(client, idp)
        add_authenticator(client)
        removed = client.delete("/auth/me/authenticator", headers=csrf(client))
        assert removed.status_code == 200, removed.text
        assert client.get("/auth/me/ways").json()["data"]["authenticator"] is None
        assert [e["who"] for e in events(client, "authenticator_removed")] == [ADA]

    def test_somebody_single_sign_on_has_never_let_in_keeps_their_only_way(self, serve):
        client, _, config = serve()
        own = browser(client)
        assert own.post("/auth/email/begin", json={"email": OLU}).status_code == 200
        begun = own.post("/auth/email/enrol/begin", json={"token": config.sender.token()}).json()["data"]
        done = own.post("/auth/email/enrol/confirm", json={"ticket": begun["ticket"], "code": totp.code_at(begun["secret"], totp.step_now())})
        assert done.status_code == 200, done.text
        kept = own.delete("/auth/me/authenticator", headers=csrf(own))
        assert kept.status_code == 409 and "only way in" in kept.json()["message"]

    def test_there_is_no_authenticator_to_remove_until_one_is_added(self, serve):
        client, idp, _ = serve()
        sso(client, idp)
        assert client.delete("/auth/me/authenticator", headers=csrf(client)).status_code == 404

    def test_they_confirm_a_change_with_their_own_code_as_well_as_single_sign_on(self, serve, monkeypatch):
        client, idp, _ = serve()
        sso(client, idp)
        secret = add_authenticator(client)
        later(monkeypatch, ELEVEN_MINUTES)
        stale = client.post("/api/v1/keys", json={"name": "nightly", "role": "reader"}, headers=csrf(client))
        assert stale.status_code == 403 and stale.json()["data"]["ways"] == ["sso", "code"]
        confirmed = client.post("/auth/step-up", json={"code": totp.code_at(secret, totp.step_now() + 1)}, headers=csrf(client))
        assert confirmed.status_code == 200, confirmed.text
        assert client.post("/api/v1/keys", json={"name": "nightly", "role": "reader"}, headers=csrf(client)).status_code == 200

    def test_with_nothing_of_their_own_they_confirm_with_single_sign_on(self, serve, monkeypatch):
        client, idp, _ = serve()
        sso(client, idp)
        later(monkeypatch, ELEVEN_MINUTES)
        stale = client.post("/api/v1/keys", json={"name": "nightly", "role": "reader"}, headers=csrf(client))
        assert stale.json()["data"]["ways"] == ["sso"]
        assert client.post("/auth/step-up", json={"code": "123456"}, headers=csrf(client)).json()["data"]["sso"] is True


# ------------------------------------------------------------ the recheck


class TestAskingTheProviderAgain:
    def test_the_setting_is_for_a_server_with_both_ways(self, tmp_path):
        base = {
            "VECTRIXDB_SIGNIN_SECRET": SECRET, "VECTRIXDB_PUBLIC_URL": PUBLIC, "VECTRIXDB_OIDC_ISSUER": ISSUER,
            "VECTRIXDB_OIDC_CLIENT_ID": "vectrixdb", "VECTRIXDB_OIDC_ROLE_MAP": '{"g-admins": "admin"}',
        }
        assert SignInConfig.from_env(tmp_path, {**base, "VECTRIXDB_SIGNIN": "oidc,email", "VECTRIXDB_SSO_RECHECK_DAYS": "30"}).sso_recheck_days == 30
        with pytest.raises(ConfigurationError, match="oidc,email"):
            SignInConfig.from_env(tmp_path, {**base, "VECTRIXDB_SIGNIN": "email", "VECTRIXDB_SSO_RECHECK_DAYS": "30"})
        for wrong in ("0", "-3", "a month"):
            with pytest.raises(ConfigurationError, match="1 or more"):
                SignInConfig.from_env(tmp_path, {**base, "VECTRIXDB_SIGNIN": "oidc,email", "VECTRIXDB_SSO_RECHECK_DAYS": wrong})

    def test_within_the_days_their_code_works(self, serve):
        client, idp, _ = serve(sso_recheck_days=30)
        sso(client, idp)
        secret = add_authenticator(client)
        # When they last came in with single sign-on, set back: the provider's tokens are checked against the real clock.
        client.app.state.signin.store.stamp_sso(ADA, now=time.time() - 29 * DAY)
        assert code_sign_in(browser(client), ADA, secret).status_code == 200

    def test_after_the_days_their_code_asks_for_single_sign_on_first(self, serve, monkeypatch):
        client, idp, _ = serve(sso_recheck_days=30)
        sso(client, idp)
        secret = add_authenticator(client)
        client.app.state.signin.store.stamp_sso(ADA, now=time.time() - 31 * DAY)
        elsewhere = browser(client)
        asked = code_sign_in(elsewhere, ADA, secret)
        assert asked.status_code == 403 and asked.json()["data"]["code"] == "sso_recheck" and asked.json()["data"]["sso"] is True
        assert "more than 30 days" in asked.json()["message"] and me(elsewhere) is None
        assert events(client, "signin_failed")[0]["reason"] == "sso_recheck"
        sso(elsewhere, idp)
        later(monkeypatch, 90)  # the next code, since the one above was spent
        assert code_sign_in(browser(client), ADA, secret, step_offset=0).status_code == 200, "a new sign-in with it starts the days again"

    def test_somebody_it_has_never_let_in_is_asked_to_use_it_before_anything_is_set_up(self, serve):
        client, _, config = serve(sso_recheck_days=30)
        own = browser(client)
        own.post("/auth/email/begin", json={"email": OLU})
        asked = own.post("/auth/email/enrol/begin", json={"token": config.sender.token()})
        assert asked.status_code == 403 and asked.json()["data"]["code"] == "sso_recheck"
        assert "Sign in with single sign-on first" in asked.json()["message"]
        assert client.app.state.signin.store.person(OLU).enrolled is False, "nothing was made, so no recovery code was lost"


# ----------------------------------------------------------------- tokens


class TestAnAppsToken:
    AUDIENCE = "api://vectrixdb"

    def serve_tokens(self, serve, **oidc_over):
        oidc = OidcConfig(**{"issuer": ISSUER, "client_id": "vectrixdb", "client_secret": "s3cret", "role_map": ROLES, "api_audience": self.AUDIENCE, **oidc_over})
        return serve(oidc=oidc, users=((OLU, "admin"),))

    def test_it_needs_no_place_on_the_platforms_list(self, serve):
        client, idp, _ = self.serve_tokens(serve)
        idp.person.update(sub="u-7", email="kojo@example.com", groups=["g-ops"])
        reply = client.get("/api/v1/collections", headers={"Authorization": f"Bearer {idp.access_token(self.AUDIENCE)}"})
        assert reply.status_code == 200, reply.text
        assert refused(sso(client, idp)) == "not_listed", "while the same person may not sign in to the platform"

    def test_the_token_role_is_given_whoever_it_is_for(self, serve):
        client, idp, _ = self.serve_tokens(serve, token_role="searcher")
        token = idp.access_token(self.AUDIENCE)  # Ada, in g-admins
        head = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/v1/collections", headers=head).status_code == 200
        refused_delete = client.delete("/api/v1/collections/anything", headers=head)
        assert refused_delete.status_code == 403 and refused_delete.json()["data"]["role"] == "searcher"

    def test_the_token_role_needs_no_group_at_all(self, serve):
        client, idp, _ = self.serve_tokens(serve, token_role="reader")
        idp.person.update(sub="u-8", email="kojo@example.com", groups=[])
        assert client.get("/api/v1/collections", headers={"Authorization": f"Bearer {idp.access_token(self.AUDIENCE)}"}).status_code == 200

    @pytest.mark.parametrize("role", ["admin", "guest", "owner"])
    def test_a_token_is_never_an_admin(self, role):
        with pytest.raises(ConfigurationError, match="never an admin"):
            OidcConfig(issuer=ISSUER, client_id="vectrixdb", role_map=ROLES, token_role=role)

    def test_it_is_read_from_the_environment(self, tmp_path):
        env = {
            "VECTRIXDB_SIGNIN": "oidc", "VECTRIXDB_SIGNIN_SECRET": SECRET, "VECTRIXDB_PUBLIC_URL": PUBLIC, "VECTRIXDB_OIDC_ISSUER": ISSUER,
            "VECTRIXDB_OIDC_CLIENT_ID": "vectrixdb", "VECTRIXDB_OIDC_ROLE_MAP": '{"g-admins": "admin"}', "VECTRIXDB_OIDC_TOKEN_ROLE": " Searcher ",
        }
        assert SignInConfig.from_env(tmp_path, env).oidc.token_role == "searcher"


# -------------------------------------------------- single sign-on alone


class TestThePeopleListWithSingleSignOnAlone:
    def test_an_admin_on_it_manages_it_and_reset_is_not_there(self, serve):
        client, idp, _ = serve(methods=("oidc",))
        sso(client, idp)
        assert client.get("/auth/me").json()["data"]["people"] is True
        listed = client.get("/auth/people")
        assert listed.status_code == 200 and {p["email"] for p in listed.json()["data"]["people"]} == {ADA, OLU}
        added = client.post("/auth/people", json={"email": "sam@example.com", "role": "viewer"}, headers=csrf(client))
        assert added.status_code == 200, added.text
        assert client.post(f"/auth/people/{OLU}/reset", headers=csrf(client)).status_code == 404, "nothing of their own is kept here to reset"

    def test_the_list_says_when_somebody_last_came_in_with_single_sign_on(self, serve):
        client, idp, _ = serve(methods=("oidc",))
        sso(client, idp)
        rows = {p["email"]: p for p in client.get("/auth/people").json()["data"]["people"]}
        assert rows[ADA]["last_sso_at"] and rows[OLU]["last_sso_at"] is None
