"""A server that signs people on its own list in with passkeys only.

``VECTRIXDB_SIGNIN_REQUIRE=passkey``. What is held: a new person sets up a
passkey and is never handed an authenticator secret; somebody who has a
passkey cannot come in with a code instead (and is told so only after the
code proved right, so a guesser learns nothing); recovery codes still work,
because a lost device is the reason they exist. A server switched over with
people who have only an authenticator does not lock them out: their code
gets them as far as making a passkey, and nowhere else until they have.
"""

from __future__ import annotations

import os
import re
import sys

import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("cryptography", reason="the signin extra is not installed")

sys.path.insert(0, os.path.dirname(__file__))
from fake_passkey import FakePasskey, b64u  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.signin import SignInConfig, totp  # noqa: E402

SECRET = "p" * 48
PUBLIC = "https://vectors.example.test"


def create_app(**kwargs):
    """Imported when called: another test drops and re-imports ``vectrixdb.api``."""
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


class Mail:
    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, text):
        self.sent.append((to, subject, text))

    def token(self):
        return re.search(r"#/enrol\?token=([\w-]+)", self.sent[-1][2]).group(1)


def build(tmp_path, monkeypatch, *, require: bool = True, mail: Mail | None = None):
    monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
    config = SignInConfig(
        methods=("email",),
        secrets=(SECRET,),
        public_url=PUBLIC,
        require_passkey=require,
        users=(("ada@example.com", "admin"),),
        sender=mail or Mail(),
        store_path=tmp_path / "auth" / "signin.db",
        access_log=tmp_path / "auth" / "access.jsonl",
    )
    return TestClient(
        create_app(db_path=str(tmp_path / "db"), enable_dashboard=False, signin=config),
        base_url=PUBLIC,
    ), config


def csrf(browser):
    return {"x-csrf-token": browser.cookies.get("__Host-vx_csrf")}


def set_up(browser, config, email="ada@example.com") -> dict:
    assert browser.post("/auth/email/begin", json={"email": email}).status_code == 200
    begun = browser.post("/auth/email/enrol/begin", json={"token": config.sender.token()})
    assert begun.status_code == 200, begun.text
    return begun.json()["data"]


def enrol_with_passkey(browser, config, email="ada@example.com"):
    device = FakePasskey(PUBLIC)
    ticket = set_up(browser, config, email)["ticket"]
    options = browser.post("/auth/passkey/enrol/begin", json={"token": ticket}).json()["data"][
        "options"
    ]
    done = browser.post(
        "/auth/passkey/enrol/finish", json={"ticket": ticket, "credential": device.create(options)}
    )
    assert done.status_code == 200, done.text
    return device, done.json()["data"]


def enrol_with_code(browser, config, email="ada@example.com") -> tuple[str, list]:
    begun = set_up(browser, config, email)
    done = browser.post(
        "/auth/email/enrol/confirm",
        json={"ticket": begun["ticket"], "code": totp.code_at(begun["secret"], totp.step_now())},
    )
    assert done.status_code == 200, done.text
    return begun["secret"], done.json()["data"]["recovery_codes"]


def add_passkey(browser, device):
    options = browser.post("/auth/me/passkeys/begin", headers=csrf(browser)).json()["data"][
        "options"
    ]
    return browser.post(
        "/auth/me/passkeys/finish",
        json={"credential": device.create(options)},
        headers=csrf(browser),
    )


def code_sign_in(browser, secret, step_offset=1):
    return browser.post(
        "/auth/email/verify",
        json={
            "email": "ada@example.com",
            "code": totp.code_at(secret, totp.step_now() + step_offset),
        },
    )


class TestSettingItUp:
    def test_it_needs_the_email_way_in_and_no_passwords(self, tmp_path):
        base = {
            "VECTRIXDB_SIGNIN_SECRET": SECRET,
            "VECTRIXDB_PUBLIC_URL": PUBLIC,
            "VECTRIXDB_SIGNIN_REQUIRE": "passkey",
        }
        assert SignInConfig.from_env(
            tmp_path, {**base, "VECTRIXDB_SIGNIN": "email"}
        ).require_passkey
        with pytest.raises(ConfigurationError, match="own list"):
            SignInConfig.from_env(
                tmp_path,
                {
                    **base,
                    "VECTRIXDB_SIGNIN": "oidc",
                    "VECTRIXDB_OIDC_ISSUER": "https://idp.example.test",
                    "VECTRIXDB_OIDC_CLIENT_ID": "x",
                    "VECTRIXDB_OIDC_DEFAULT_ROLE": "viewer",
                },
            )
        with pytest.raises(ConfigurationError, match="Choose one"):
            SignInConfig.from_env(
                tmp_path, {**base, "VECTRIXDB_SIGNIN": "email", "VECTRIXDB_SIGNIN_PASSWORDS": "on"}
            )
        with pytest.raises(ConfigurationError, match="passkey, or left unset"):
            SignInConfig.from_env(
                tmp_path, {**base, "VECTRIXDB_SIGNIN": "email", "VECTRIXDB_SIGNIN_REQUIRE": "fido"}
            )

    def test_the_page_is_told(self, tmp_path, monkeypatch):
        client, _ = build(tmp_path, monkeypatch)
        with client:
            assert client.get("/auth/me").json()["data"]["methods"]["email"]["require"] == "passkey"


class TestANewPerson:
    def test_no_authenticator_secret_is_handed_out(self, tmp_path, monkeypatch):
        client, config = build(tmp_path, monkeypatch)
        with client:
            begun = set_up(client, config)
        assert begun["passkey_only"] is True and "secret" not in begun and "qr" not in begun

    def test_a_code_cannot_finish_setting_up(self, tmp_path, monkeypatch):
        client, config = build(tmp_path, monkeypatch)
        with client:
            ticket = set_up(client, config)["ticket"]
            refused = client.post(
                "/auth/email/enrol/confirm", json={"ticket": ticket, "code": "123456"}
            )
        assert refused.status_code == 403 and "passkeys" in refused.json()["message"]

    def test_a_passkey_sets_them_up_with_recovery_codes(self, tmp_path, monkeypatch):
        client, config = build(tmp_path, monkeypatch)
        with client:
            _, done = enrol_with_passkey(client, config)
            me = client.get("/auth/me").json()["data"]
        assert len(done["recovery_codes"]) == 10
        assert me["require_passkey"] is True and me["must_add_passkey"] is False
        assert client.get("/auth/me/ways").json()["data"]["require_passkey"] is True


class TestSomebodyWithAPasskey:
    def test_a_right_code_is_not_the_way_in_and_a_wrong_one_says_nothing_more(
        self, tmp_path, monkeypatch
    ):
        mail = Mail()
        before, config = build(tmp_path, monkeypatch, require=False, mail=mail)
        with before:
            secret, _ = enrol_with_code(before, config)
            assert add_passkey(before, FakePasskey(PUBLIC)).status_code == 200
        after, _ = build(tmp_path, monkeypatch, mail=mail)
        with after:
            wrong = after.post(
                "/auth/email/verify", json={"email": "ada@example.com", "code": "000000"}
            )
            assert wrong.status_code == 401 and "passkey" not in wrong.json()["message"].lower()
            right = code_sign_in(after, secret)
            assert right.status_code == 403 and right.json()["data"] == {"passkey_only": True}
            assert after.get("/auth/me").status_code == 401, "and nobody is signed in by it"

    def test_a_recovery_code_still_gets_them_in(self, tmp_path, monkeypatch):
        client, config = build(tmp_path, monkeypatch)
        with client:
            _, done = enrol_with_passkey(client, config)
            client.post("/auth/signout", headers=csrf(client))
            back = client.post(
                "/auth/email/verify",
                json={"email": "ada@example.com", "code": done["recovery_codes"][0]},
            )
            assert back.status_code == 200 and client.get("/auth/me").status_code == 200

    def test_the_only_passkey_stays_even_beside_an_authenticator(self, tmp_path, monkeypatch):
        mail = Mail()
        before, config = build(tmp_path, monkeypatch, require=False, mail=mail)
        with before:
            enrol_with_code(before, config)
            device = FakePasskey(PUBLIC)
            assert add_passkey(before, device).status_code == 200
            cookies = dict(before.cookies)
        after, _ = build(tmp_path, monkeypatch, mail=mail)
        with after:
            after.cookies.update(cookies)
            refused = after.delete(
                f"/auth/me/passkeys/{b64u(device.credential_id)}", headers=csrf(after)
            )
            assert refused.status_code == 409 and "only passkey" in refused.json()["message"]
            assert (
                after.post("/auth/me/authenticator/begin", headers=csrf(after)).status_code == 403
            )

    def test_a_fresh_check_is_a_passkey_only(self, tmp_path, monkeypatch):
        mail = Mail()
        before, config = build(tmp_path, monkeypatch, require=False, mail=mail)
        with before:
            enrol_with_code(before, config)
            add_passkey(before, FakePasskey(PUBLIC))
            cookies = dict(before.cookies)
        after, _ = build(tmp_path, monkeypatch, mail=mail)
        runtime = after.app.state.signin
        from vectrixdb.api.signin import Caller

        with after:
            after.cookies.update(cookies)
            caller = Caller(
                who="ada@example.com", role="admin", method="email", email="ada@example.com"
            )
            assert runtime.step_up_ways(caller) == ["passkey"]


class TestSwitchingAServerOver:
    def test_a_code_gets_somebody_as_far_as_making_a_passkey(self, tmp_path, monkeypatch):
        mail = Mail()
        before, config = build(tmp_path, monkeypatch, require=False, mail=mail)
        with before:
            secret, _ = enrol_with_code(before, config)
        after, _ = build(tmp_path, monkeypatch, mail=mail)
        with after:
            assert code_sign_in(after, secret).status_code == 200
            me = after.get("/auth/me").json()["data"]
            assert me["must_add_passkey"] is True
            blocked = after.get("/api/v1/collections")
            assert blocked.status_code == 403 and blocked.json()["data"] == {
                "must_add_passkey": True
            }
            assert after.get("/auth/me/ways").status_code == 200, (
                "their own page stays open, to add the passkey from"
            )
            assert add_passkey(after, FakePasskey(PUBLIC)).status_code == 200
            assert after.get("/auth/me").json()["data"]["must_add_passkey"] is False
            assert after.get("/api/v1/collections").status_code == 200

    def test_signing_out_is_never_blocked(self, tmp_path, monkeypatch):
        mail = Mail()
        before, config = build(tmp_path, monkeypatch, require=False, mail=mail)
        with before:
            secret, _ = enrol_with_code(before, config)
        after, _ = build(tmp_path, monkeypatch, mail=mail)
        with after:
            code_sign_in(after, secret)
            assert after.post("/auth/signout", headers=csrf(after)).status_code == 200
