"""Single sign-on asked for before its provider is set up.

What is being held to: VECTRIXDB_SIGNIN=oidc with no issuer and no client
named starts, where it used to refuse; the sign-in box is told to draw the
single sign-on button as not set up yet; people on the People list sign in
with their work email and the code from their authenticator app; nobody
keeps a passkey, and every passkey route refuses, not only the page; one of
the two settings without the other is still a mistake said at start; with
both set the email way is gone again; and with oidc,email the same is drawn
while people keep their passkeys.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")

sys.path.insert(0, os.path.dirname(__file__))
from fake_idp import ISSUER  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.signin import SignInConfig, totp  # noqa: E402

SECRET = "k" * 48
PUBLIC = "https://vectors.example.test"
ADA = "ada@example.com"
DASH = Path(__file__).resolve().parents[2] / "vectrixdb" / "dashboard"
JS = (DASH / "app.js").read_text(encoding="utf-8")


def env(**over):
    base = {
        "VECTRIXDB_SIGNIN": "oidc",
        "VECTRIXDB_SIGNIN_SECRET": SECRET,
        "VECTRIXDB_PUBLIC_URL": PUBLIC,
        "VECTRIXDB_SIGNIN_USERS": f"{ADA}:admin",
    }
    base.update(over)
    return {name: value for name, value in base.items() if value is not None}


class Mail:
    """Sign-in emails, kept instead of sent."""

    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, text):
        self.sent.append((to, subject, text))

    def token(self):
        return self.sent[-1][2].split("token=")[1].split()[0]


@pytest.fixture
def serve(tmp_path, monkeypatch):
    monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
    monkeypatch.delenv("VECTRIXDB_AUDIT_JSONL", raising=False)
    built = []

    def build(**over):
        from vectrixdb.api.server import create_app

        root = tmp_path / f"db{len(built)}"
        config = SignInConfig.from_env(root, env=env(**over))
        config.sender = Mail()
        client = TestClient(
            create_app(db_path=str(root), enable_dashboard=False, signin=config),
            base_url=PUBLIC,
            follow_redirects=False,
        )
        client.__enter__()
        built.append(client)
        return client, config

    yield build
    for client in built:
        client.__exit__(None, None, None)


def methods(client):
    reply = client.get("/auth/me")
    assert reply.status_code == 401
    return reply.json()["data"]["methods"]


def enrolled(client, config, email=ADA):
    """Through the emailed link to an authenticator app. Its secret."""
    assert client.post("/auth/email/begin", json={"email": email}).status_code == 200
    begun = client.post("/auth/email/enrol/begin", json={"token": config.sender.token()}).json()[
        "data"
    ]
    assert not begun.get("passkey_only")
    done = client.post(
        "/auth/email/enrol/confirm",
        json={"ticket": begun["ticket"], "code": totp.code_at(begun["secret"], totp.step_now())},
    )
    assert done.status_code == 200, done.text
    return begun["secret"]


def csrf(client):
    return {"x-csrf-token": client.cookies.get("__Host-vx_csrf")}


class TestTheSettings:
    def test_single_sign_on_alone_with_nothing_named_starts_with_email_and_no_passkeys(
        self, tmp_path
    ):
        config = SignInConfig.from_env(tmp_path, env=env())
        assert config.sso_pending and config.oidc is None
        assert config.methods == ("email",) and not config.own_passkeys and config.email_stands_in

    def test_with_both_ways_asked_the_email_way_is_drawn_from_the_start_and_still_no_passkeys(
        self, tmp_path
    ):
        config = SignInConfig.from_env(tmp_path, env=env(VECTRIXDB_SIGNIN="oidc,email"))
        assert (
            config.sso_pending
            and config.methods == ("email",)
            and not config.own_passkeys
            and not config.email_stands_in
        )

    def test_with_the_provider_named_it_is_single_sign_on_alone_again(self, tmp_path):
        config = SignInConfig.from_env(
            tmp_path,
            env=env(
                VECTRIXDB_OIDC_ISSUER=ISSUER,
                VECTRIXDB_OIDC_CLIENT_ID="vectrixdb",
                VECTRIXDB_OIDC_DEFAULT_ROLE="viewer",
            ),
        )
        assert (
            not config.sso_pending
            and config.methods == ("oidc",)
            and config.oidc is not None
            and not config.own_passkeys
        )

    @pytest.mark.parametrize(
        "half", [{"VECTRIXDB_OIDC_ISSUER": ISSUER}, {"VECTRIXDB_OIDC_CLIENT_ID": "vectrixdb"}]
    )
    def test_one_without_the_other_is_a_mistake_said_at_start(self, tmp_path, half):
        with pytest.raises(
            ConfigurationError, match="VECTRIXDB_OIDC_ISSUER and VECTRIXDB_OIDC_CLIENT_ID"
        ):
            SignInConfig.from_env(tmp_path, env=env(**half))

    def test_the_label_is_kept_for_the_button(self, tmp_path):
        assert (
            SignInConfig.from_env(
                tmp_path, env=env(VECTRIXDB_OIDC_LABEL="Continue with Northwind")
            ).sso_label
            == "Continue with Northwind"
        )

    def test_what_leans_on_single_sign_on_waits_for_it(self, tmp_path):
        config = SignInConfig.from_env(
            tmp_path,
            env=env(
                VECTRIXDB_SIGNIN="oidc,email",
                VECTRIXDB_ADMINS_USE_SSO="on",
                VECTRIXDB_SSO_RECHECK_DAYS="30",
            ),
        )
        assert not config.admins_use_sso, "or no admin could sign in at all"
        assert config.sso_recheck_days is None, "nobody can have signed in with it lately"

    def test_passkeys_only_is_not_asked_of_a_server_that_keeps_none(self, tmp_path):
        with pytest.raises(ConfigurationError):
            SignInConfig.from_env(tmp_path, env=env(VECTRIXDB_SIGNIN_REQUIRE="passkey"))

    def test_the_cookie_is_strict_while_no_provider_sends_anybody_back(self, tmp_path):
        assert SignInConfig.from_env(tmp_path, env=env()).samesite == "strict"


class TestTheServer:
    def test_the_page_is_told_to_draw_the_button_as_not_set_up(self, serve):
        client, _ = serve()
        told = methods(client)
        assert told["oidc"] == {"label": "Continue with SSO", "pending": True}
        assert told["email"]["passkeys"] is False and told["email"]["stands_in"] is True

    def test_nobody_is_sent_to_a_provider_that_is_not_named(self, serve):
        client, _ = serve()
        assert client.get("/auth/oidc/start").status_code == 404

    def test_somebody_on_the_list_signs_in_with_their_email_and_a_code(self, serve):
        client, config = serve()
        secret = enrolled(client, config)
        told = client.get("/auth/me").json()["data"]
        assert (
            told["person"]["email"] == ADA
            and told["passkeys"] is False
            and told["sso_pending"] is True
        )
        again = TestClient(client.app, base_url=PUBLIC, follow_redirects=False)
        assert again.post("/auth/email/begin", json={"email": ADA}).status_code == 200
        signed = again.post(
            "/auth/email/verify",
            json={"email": ADA, "code": totp.code_at(secret, totp.step_now() + 1)},
        )
        assert signed.status_code == 200, signed.text

    def test_the_email_offers_the_authenticator_and_no_passkey(self, serve):
        client, config = serve()
        assert client.post("/auth/email/begin", json={"email": ADA}).status_code == 200
        text = config.sender.sent[-1][2]
        assert "set up the authenticator app" in text and "passkey" not in text

    def test_somebody_not_on_the_list_is_sent_nothing(self, serve):
        client, config = serve()
        assert (
            client.post("/auth/email/begin", json={"email": "mallory@example.com"}).status_code
            == 200
        ), "one answer for every address"
        assert config.sender.sent == []

    def test_no_passkey_route_is_there(self, serve):
        client, config = serve()
        for route, body in [
            ("/auth/passkey/begin", None),
            ("/auth/passkey/finish", {"credential": {}}),
            ("/auth/passkey/enrol/begin", {"token": "t"}),
            ("/auth/passkey/enrol/finish", {"ticket": "t", "credential": {}}),
        ]:
            reply = client.post(route, json=body)
            assert reply.status_code == 404, route
        enrolled(client, config)
        for route, body in [
            ("/auth/me/passkeys/begin", None),
            ("/auth/me/passkeys/finish", {"credential": {}}),
            ("/auth/step-up/passkey/begin", None),
            ("/auth/step-up/passkey/finish", {"credential": {}}),
        ]:
            reply = client.post(route, json=body, headers=csrf(client))
            assert reply.status_code == 404, route

    def test_their_ways_say_no_passkeys_are_kept(self, serve):
        client, config = serve()
        enrolled(client, config)
        assert client.get("/auth/me/ways").json()["data"]["own_passkeys"] is False

    def test_with_both_ways_asked_the_passkey_routes_are_not_there_either(self, serve):
        client, _ = serve(VECTRIXDB_SIGNIN="oidc,email")
        told = methods(client)
        assert (
            told["oidc"]["pending"] is True
            and told["email"]["passkeys"] is False
            and "stands_in" not in told["email"]
        )
        assert client.post("/auth/passkey/begin").status_code == 404

    def test_the_settings_check_says_so(self, tmp_path, monkeypatch):
        from vectrixdb import check

        for name in [n for n in os.environ if n.startswith("VECTRIXDB_")]:
            monkeypatch.delenv(name)
        found = check.run(str(tmp_path), env=env())
        assert any(
            "Single sign-on is asked for and not set up" in f.text
            and "nobody keeps a passkey" in f.text
            for f in found
            if f.level == "warn"
        )


class TestThePage:
    GATE = JS[JS.index("function showGate()") : JS.index("/* Developer Access: accounts named")]
    CHECK = JS[JS.index("function checkSso()") : JS.index("function leaveSso()")]

    def test_the_button_is_pressed_as_ever_and_nothing_in_the_box_says_it_is_not_configured(self):
        assert "const pending = !!(m.oidc && m.oidc.pending);" in self.GATE
        assert "${on('click', ['pressSso'])}" in self.GATE
        assert "return m.oidc && m.oidc.pending ? checkSso() : startSso();" in JS
        assert (
            "class=\"btn${pending && state.pastSsoCheck ? ' primary' : ''}\" type=\"submit\">Continue with email"
            in self.GATE
        ), "once opened, the email way is the one in colour"
        assert (
            "not configured" not in self.GATE
            and "isn't set up" not in self.GATE
            and "disabled" not in self.GATE.split("const passkey")[0]
        )

    def test_pressing_it_checks_says_so_and_opens_the_email_way(self):
        assert (
            self.CHECK.index("Checking single sign-on")
            < self.CHECK.index("Single sign-on not configured")
            < self.CHECK.index("Opening email sign-in")
        )
        assert self.CHECK.count('class="spin"') == 2 and "setTimeout(openEmailWay" in self.CHECK
        assert "state.pastSsoCheck = true" in self.CHECK and "showGate()" in self.CHECK
        assert (
            "'checkSso'" in JS[JS.index("const ACTIONS = new Set([") :][:400]
            and "'openEmailWay'" in JS[JS.index("const ACTIONS = new Set([") :][:400]
        )

    def test_single_sign_on_alone_draws_the_email_way_only_once_it_is_opened(self):
        assert (
            "const closed = pending && m.email && m.email.stands_in && !state.pastSsoCheck;"
            in self.GATE
        )
        assert "let email = m.email && !closed ?" in self.GATE

    def test_no_passkey_button_where_none_are_kept(self):
        assert (
            "const passkey = m.email && m.email.passkeys !== false && window.PublicKeyCredential ?"
            in self.GATE
        )

    def test_on_this_machine_the_spinner_opens_developer_access_instead(self):
        press = JS[JS.index("function pressSso()") : JS.index("function localDetected()")]
        assert press.index("if (m.developer) return localDetected();") < press.index("checkSso()")

    def test_the_first_visit_offers_the_authenticator_alone(self):
        enrol = JS[
            JS.index("function gateEnrol(token)") : JS.index("async function gateEnrolBegin")
        ]
        assert (
            "state.methods.email.passkeys === false" in enrol
            and "const passkeys = kept && !!window.PublicKeyCredential;" in enrol
        )
        assert "Set up your authenticator app" in enrol

    def test_how_you_sign_in_lists_no_passkeys(self):
        assert "if (w.own_passkeys !== false) body += wayGroup('Passkeys'" in JS
