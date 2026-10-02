"""Self-service sign-in and named API keys, against the real server.

What is being held to: a change that matters (deleting or sharing a
collection, managing people or keys, changing one's own ways in) needs a
proof from the last ten minutes, and looking never does; a person sees how
they sign in and where, replaces their authenticator and their recovery
codes, and ends sessions that are theirs and nobody else's; a named key is
shown once, kept as a hash, does what its role allows and nothing more, is
named in the access log, and stops the moment it is revoked; an admin's reset
forgets every way in, a change of role keeps what was given, and the last
admin stays; and the first reply the page asks for carries the version, what
is turned on and, for an admin, how many admins there are.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("jwt", reason="the signin extra is not installed")
pytest.importorskip("cryptography", reason="the signin extra is not installed")

sys.path.insert(0, os.path.dirname(__file__))
from fake_idp import ISSUER, FakeIdp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import vectrixdb  # noqa: E402
from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.signin import OidcConfig, SignInConfig, roles, totp  # noqa: E402
from vectrixdb.signin.store import STEP_UP_SECONDS  # noqa: E402


def create_app(**kwargs):
    """The server as it is now. Another test drops and re-imports ``vectrixdb.api``,
    and a name bound at the top of this file would go on pointing at the old copy,
    whose routes reach for a database the new copy's start-up never gave them."""
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


def device_of(user_agent):
    from vectrixdb.api.signin import device_of as current

    return current(user_agent)


SECRET = "k" * 48
PUBLIC = "https://vectors.example.test"
KEY = "the-full-api-key"
PASSWORD = "four ordinary words together"
VECTORS = {"alpha": [1, 0, 0, 0], "beta": [0, 1, 0, 0], "gamma": [0, 0, 1, 0]}
#: Long enough ago that a session opened then has to prove it is the person again.
STALE = STEP_UP_SECONDS + 60

EDGE_ON_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
SAFARI_ON_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
CHROME_ON_ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
CHROME_ON_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
OPERA_ON_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 OPR/111.0.0.0"
FIREFOX_ON_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0"


def _embed(texts):
    return np.array([VECTORS[t] for t in texts], dtype=np.float32)


@pytest.fixture
def data(tmp_path):
    """One collection with two chunks in it, and no policy."""
    root = tmp_path / "db"
    plain = Vectrix("plain", path=str(root), dimension=4, embed_fn=_embed, embedding_cache=False)
    plain.add(
        ["alpha", "beta"],
        ids=["p-alpha", "p-beta"],
        metadata=[{"source": "a.pdf"}, {"source": "b.pdf"}],
    )
    plain.close()
    return root


class Mail:
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
        "users": (
            ("ada@example.com", "admin"),
            ("olu@example.com", "operator"),
            ("vi@example.com", "viewer"),
        ),
        "store_path": root / "auth" / "signin.db",
        "access_log": root / "auth" / "access.jsonl",
        "sender": Mail(),
    }
    base.update(over)
    return SignInConfig(**base)


@pytest.fixture
def serve(data, monkeypatch):
    """Starts the server with these sign-in settings: ``with serve(passwords=True) as (client, config)``."""
    monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
    monkeypatch.delenv("VECTRIXDB_READ_ONLY_API_KEY", raising=False)
    monkeypatch.delenv("VECTRIXDB_AUDIT_JSONL", raising=False)

    @contextmanager
    def start(**over):
        config = _config(data, **over)
        with TestClient(
            create_app(db_path=str(data), enable_dashboard=False, signin=config), base_url=PUBLIC
        ) as client:
            yield client, config

    return start


@pytest.fixture
def server(serve):
    with serve() as served:
        yield served


class Clock:
    """``time.time``, which every sign-in module reads through the time module, moved on at will."""

    def __init__(self, monkeypatch) -> None:
        self._real = time.time
        self.offset = 0.0
        monkeypatch.setattr(time, "time", self.now)

    def now(self) -> float:
        return self._real() + self.offset

    def move(self, seconds: float) -> None:
        self.offset += seconds


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


class Browser(TestClient):
    """A browser of somebody's own. Once they have enrolled it also holds what they were given:
    the authenticator secret, the recovery codes, and the step of the code that finished enrolment."""

    def __init__(self, app, *, agent: str | None = None, address: str = "testclient") -> None:
        super().__init__(
            app,
            base_url=PUBLIC,
            headers={"user-agent": agent} if agent else None,
            client=(address, 50000),
        )
        self.secret = ""
        self.codes: list = []
        self.step = 0


def enrol(
    browser: Browser, config: SignInConfig, email: str, password: str | None = None
) -> Browser:
    """The whole first visit, from the emailed link to the first code."""
    assert browser.post("/auth/email/begin", json={"email": email}).status_code == 200
    begun = browser.post("/auth/email/enrol/begin", json={"token": config.sender.token()}).json()[
        "data"
    ]
    step = totp.step_now()
    done = browser.post(
        "/auth/email/enrol/confirm",
        json={
            "ticket": begun["ticket"],
            "code": totp.code_at(begun["secret"], step),
            "password": password,
        },
    )
    assert done.status_code == 200, done.text
    browser.secret, browser.codes, browser.step = (
        begun["secret"],
        done.json()["data"]["recovery_codes"],
        step,
    )
    return browser


def person(
    served,
    email: str,
    *,
    agent: str | None = None,
    address: str = "testclient",
    password: str | None = None,
) -> Browser:
    """A browser of their own, signed in as this person for the first time."""
    client, config = served
    return enrol(Browser(client.app, agent=agent, address=address), config, email, password)


def csrf(browser: TestClient) -> dict:
    return {"x-csrf-token": browser.cookies.get("__Host-vx_csrf")}


def code_now(secret: str, ahead: int = 0) -> str:
    """The authenticator's code now, or ``ahead`` steps on. The server takes the next step as well, since phones drift."""
    return totp.code_at(secret, totp.step_now() + ahead)


def sign_in(
    browser: TestClient, email: str, secret: str, clock: Clock, password: str | None = None
):
    """Sign in with the authenticator. A minute passes first, because each code works once."""
    clock.move(60)
    reply = browser.post(
        "/auth/email/verify", json={"email": email, "code": code_now(secret), "password": password}
    )
    assert reply.status_code == 200, reply.text
    return reply


def step_up(browser: Browser, ahead: int = 0):
    return browser.post(
        "/auth/step-up", json={"code": code_now(browser.secret, ahead)}, headers=csrf(browser)
    )


def age(config: SignInConfig, seconds: float = STALE) -> None:
    """Every session's last proof, pushed this far into the past: a session left open on a desk."""
    with closing(sqlite3.connect(str(config.store_path))) as db:
        db.execute(
            "UPDATE records SET data = json_set(data, '$.verified_at', json_extract(data, '$.verified_at') - ?) WHERE kind = 'session'",
            (seconds,),
        )
        db.commit()


def logged(config: SignInConfig, event: str) -> list:
    """The access log's lines for one event, oldest first."""
    lines = Path(config.access_log).read_text(encoding="utf-8").splitlines()
    return [line for line in map(json.loads, lines) if line["event"] == event]


def make_key(admin: Browser, name: str, role: str) -> dict:
    reply = admin.post("/api/v1/keys", json={"name": name, "role": role}, headers=csrf(admin))
    assert reply.status_code == 200, reply.text
    return reply.json()["data"]


@pytest.fixture
def sso(data, monkeypatch):
    monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
    idp = FakeIdp()
    oidc = OidcConfig(
        issuer=ISSUER,
        client_id="vectrixdb",
        client_secret="s3cret",
        role_map={"g-admins": "admin", "g-ops": "operator"},
        allowed_emails=("*",),
    )
    config = _config(data, methods=("oidc",), oidc=oidc, users=())
    app = create_app(
        db_path=str(data), enable_dashboard=False, signin=config, oidc_transport=idp.transport
    )
    with TestClient(app, base_url=PUBLIC, follow_redirects=False) as client:
        yield client, idp, config


def sso_sign_in(client: TestClient, idp: FakeIdp) -> None:
    start = client.get("/auth/oidc/start")
    assert start.status_code == 302
    code, state = idp.authorize(start.headers["location"])
    back = client.get("/auth/oidc/callback", params={"code": code, "state": state})
    assert back.status_code == 302 and "error" not in back.headers["location"], back.headers.get(
        "location"
    )


# ------------------------------------------------------------- 1 · step-up

#: Each change that asks again, as a request an admin might make.
CHANGES = [
    pytest.param("DELETE", "/api/v1/collections/plain", None, id="deleting a collection"),
    pytest.param(
        "POST", "/auth/people", {"email": "sam@example.com", "role": "viewer"}, id="adding a person"
    ),
    pytest.param("POST", "/auth/people/vi@example.com/reset", None, id="resetting a person"),
    pytest.param("DELETE", "/auth/people/vi@example.com", None, id="removing a person"),
    pytest.param(
        "POST", "/api/v1/keys", {"name": "nightly-ingest", "role": "searcher"}, id="making a key"
    ),
    pytest.param("DELETE", "/api/v1/keys/{key}", None, id="revoking a key"),
    pytest.param("POST", "/auth/me/recovery-codes", None, id="new recovery codes"),
    pytest.param("POST", "/auth/me/authenticator/begin", None, id="a new authenticator"),
]


class TestAChangeThatMattersAsksAgain:
    def test_the_changes_that_ask_again_and_how_long_a_proof_lasts(self):
        assert roles.STEP_UP == {
            "collection.delete",
            "collection.share",
            "people.manage",
            "keys.manage",
            "self.secure",
        }
        assert STEP_UP_SECONDS == 600
        for action in roles.STEP_UP:
            assert all(
                roles.needs_step_up(action, method)
                for method in ("POST", "PUT", "PATCH", "DELETE", "delete")
            )
            assert not any(
                roles.needs_step_up(action, method) for method in ("GET", "HEAD", "OPTIONS")
            )
        assert not roles.needs_step_up("content.write", "POST") and not roles.needs_step_up(
            "self.manage", "POST"
        )

    @pytest.mark.parametrize("method, path, body", CHANGES)
    def test_a_stale_session_is_asked_to_prove_it_and_the_retry_goes_through(
        self, server, clock, method, path, body
    ):
        ada = person(server, "ada@example.com")
        key = make_key(ada, "to-revoke", "reader")["id"]
        clock.move(STALE)

        def send():
            return ada.request(method, path.format(key=key), json=body, headers=csrf(ada))

        refused = send()
        assert refused.status_code == 403
        assert refused.json()["data"] == {"step_up": True, "ways": ["code"]}
        assert step_up(ada).status_code == 200
        again = send()
        assert again.status_code == 200, again.text

    def test_what_was_refused_did_not_happen(self, server, clock):
        ada = person(server, "ada@example.com")
        clock.move(STALE)
        assert ada.delete("/api/v1/collections/plain", headers=csrf(ada)).status_code == 403
        assert (
            ada.post(
                "/api/v1/keys", json={"name": "sneaky", "role": "operator"}, headers=csrf(ada)
            ).status_code
            == 403
        )
        assert (
            ada.post(
                "/auth/people",
                json={"email": "sam@example.com", "role": "admin"},
                headers=csrf(ada),
            ).status_code
            == 403
        )
        assert ada.get("/api/v1/collections/plain").status_code == 200
        assert ada.get("/api/v1/keys").json()["data"]["keys"] == []
        assert "sam@example.com" not in {
            p["email"] for p in ada.get("/auth/people").json()["data"]["people"]
        }

    def test_looking_never_asks_again(self, server, clock):
        ada = person(server, "ada@example.com")
        clock.move(STALE)
        assert ada.get("/api/v1/keys").status_code == 200
        assert ada.get("/auth/people").status_code == 200

    def test_a_fresh_sign_in_is_a_proof_of_its_own(self, server, clock):
        ada = person(server, "ada@example.com")
        make_key(ada, "first", "reader")  # signing in a moment ago is proof enough
        clock.move(STALE)
        assert (
            ada.post(
                "/api/v1/keys", json={"name": "second", "role": "reader"}, headers=csrf(ada)
            ).status_code
            == 403
        )
        sign_in(ada, "ada@example.com", ada.secret, clock)
        assert (
            ada.post(
                "/api/v1/keys", json={"name": "second", "role": "reader"}, headers=csrf(ada)
            ).status_code
            == 200
        )

    def test_a_proof_lasts_ten_minutes_from_the_sign_in_or_the_code_and_no_longer(
        self, server, clock
    ):
        _, config = server
        ada = person(server, "ada@example.com")
        clock.move(STEP_UP_SECONDS - 10)
        assert (
            ada.post(
                "/api/v1/keys", json={"name": "inside", "role": "reader"}, headers=csrf(ada)
            ).status_code
            == 200
        )
        clock.move(20)
        assert (
            ada.post(
                "/api/v1/keys", json={"name": "outside", "role": "reader"}, headers=csrf(ada)
            ).status_code
            == 403
        )

        assert step_up(ada).status_code == 200
        clock.move(STEP_UP_SECONDS - 10)
        assert (
            ada.post(
                "/api/v1/keys", json={"name": "renewed", "role": "reader"}, headers=csrf(ada)
            ).status_code
            == 200
        )
        clock.move(20)
        assert (
            ada.post(
                "/api/v1/keys", json={"name": "late", "role": "reader"}, headers=csrf(ada)
            ).status_code
            == 403
        )
        assert [(r["who"], r["method"]) for r in logged(config, "step_up")] == [
            ("ada@example.com", "email")
        ]

    def test_a_wrong_code_or_a_spent_one_proves_nothing(self, server):
        _, config = server
        ada = person(server, "ada@example.com")
        age(config)
        spent = totp.code_at(ada.secret, ada.step)  # it finished the enrolment
        for wrong in ("000000", spent, "12345"):
            assert (
                ada.post("/auth/step-up", json={"code": wrong}, headers=csrf(ada)).status_code
                == 401
            )
        assert ada.delete("/api/v1/collections/plain", headers=csrf(ada)).status_code == 403
        assert [r["reason"] for r in logged(config, "signin_failed")] == ["step_up"] * 3
        assert step_up(ada, ahead=1).status_code == 200
        assert ada.delete("/api/v1/collections/plain", headers=csrf(ada)).status_code == 200

    def test_five_wrong_codes_at_the_prompt_shut_the_door_as_five_at_sign_in_do(
        self, server, clock
    ):
        _, config = server
        ada = person(server, "ada@example.com")
        clock.move(STALE)
        for _ in range(5):
            assert (
                ada.post("/auth/step-up", json={"code": "000000"}, headers=csrf(ada)).status_code
                == 401
            )
        assert step_up(ada).status_code == 429, "not even the right code, while the door is shut"
        assert (
            config.sender.sent[-1][0] == "ada@example.com" and "paused" in config.sender.sent[-1][1]
        )

    def test_a_recovery_code_is_a_proof_too_and_is_spent_by_it(self, server, clock):
        ada = person(server, "ada@example.com")
        clock.move(STALE)
        assert (
            ada.post("/auth/step-up", json={"code": ada.codes[0]}, headers=csrf(ada)).status_code
            == 200
        )
        assert ada.get("/auth/me/ways").json()["data"]["recovery_codes_left"] == 9
        assert (
            ada.post("/auth/step-up", json={"code": ada.codes[0]}, headers=csrf(ada)).status_code
            == 401
        )

    def test_a_proof_counts_only_in_the_browser_that_gave_it(self, server, clock):
        client, _ = server
        laptop = person(server, "ada@example.com")
        phone = Browser(client.app)
        sign_in(phone, "ada@example.com", laptop.secret, clock)
        clock.move(STALE)
        assert step_up(laptop).status_code == 200
        assert (
            laptop.post(
                "/api/v1/keys",
                json={"name": "from-the-laptop", "role": "reader"},
                headers=csrf(laptop),
            ).status_code
            == 200
        )
        refused = phone.post(
            "/api/v1/keys", json={"name": "from-the-phone", "role": "reader"}, headers=csrf(phone)
        )
        assert refused.status_code == 403 and refused.json()["data"]["step_up"] is True

    def test_the_servers_own_key_is_never_asked_because_there_is_nobody_to_ask(self, server):
        client, _ = server
        assert (
            client.delete("/api/v1/collections/plain", headers={"api-key": KEY}).status_code == 200
        )

    def test_single_sign_on_is_sent_back_to_the_provider_and_a_new_sign_in_is_the_proof(self, sso):
        client, idp, config = sso
        sso_sign_in(client, idp)
        age(config)
        refused = client.post(
            "/api/v1/keys", json={"name": "nightly", "role": "reader"}, headers=csrf(client)
        )
        assert refused.status_code == 403 and refused.json()["data"] == {
            "step_up": True,
            "ways": ["sso"],
        }
        told = client.post("/auth/step-up", json={"code": "123456"}, headers=csrf(client))
        assert told.status_code == 400 and told.json()["data"] == {"sso": True}
        assert (
            client.post(
                "/api/v1/keys", json={"name": "nightly", "role": "reader"}, headers=csrf(client)
            ).status_code
            == 403
        )
        sso_sign_in(client, idp)
        assert (
            client.post(
                "/api/v1/keys", json={"name": "nightly", "role": "reader"}, headers=csrf(client)
            ).status_code
            == 200
        )


# ------------------------------------------------------- 2 · how you sign in


class TestHowYouSignIn:
    def test_the_page_lists_what_is_set_up_and_nothing_secret(self, server):
        ada = person(server, "ada@example.com")
        reply = ada.get("/auth/me/ways")
        ways = reply.json()["data"]
        assert set(ways) == {
            "local",
            "listed",
            "method",
            "passwords",
            "passkeys",
            "authenticator",
            "recovery_codes_left",
            "password",
            "require_passkey",
            "own_passkeys",
        }
        assert ways == {
            **ways,
            "local": True,
            "method": "email",
            "passwords": False,
            "passkeys": [],
            "recovery_codes_left": 10,
            "password": None,
        }
        assert set(ways["authenticator"]) == {"set_up_at", "used_at"}
        assert ways["authenticator"]["set_up_at"] == ways["authenticator"]["used_at"], (
            "the code that set it up was its first use"
        )
        assert ada.secret not in reply.text and not any(c in reply.text for c in ada.codes)

    def test_the_authenticator_says_when_it_was_last_used(self, server, clock):
        client, _ = server
        ada = person(server, "ada@example.com")
        before = ada.get("/auth/me/ways").json()["data"]["authenticator"]
        sign_in(Browser(client.app), "ada@example.com", ada.secret, clock)
        after = ada.get("/auth/me/ways").json()["data"]["authenticator"]
        assert (
            after["set_up_at"] == before["set_up_at"] and after["used_at"] >= before["used_at"] + 60
        )

    def test_a_password_is_listed_when_passwords_are_on(self, serve):
        with serve(passwords=True) as served:
            ways = (
                person(served, "ada@example.com", password=PASSWORD)
                .get("/auth/me/ways")
                .json()["data"]
            )
        assert ways["passwords"] is True and isinstance(ways["password"]["set_at"], float)

    def test_a_passkey_is_listed_by_its_name_and_offered_when_asked_to_prove_it(self, server):
        client, config = server
        ada = person(server, "ada@example.com")
        client.app.state.signin.store.add_passkey(
            "ada@example.com", b"credential-1", b"public-key", -7, 0, ["internal"], "Work laptop"
        )
        passkeys = ada.get("/auth/me/ways").json()["data"]["passkeys"]
        assert [(k["name"], k["last_used"]) for k in passkeys] == [("Work laptop", None)]
        age(config)
        refused = ada.delete("/api/v1/collections/plain", headers=csrf(ada))
        assert refused.status_code == 403 and refused.json()["data"]["ways"] == ["passkey", "code"]

    def test_single_sign_on_has_no_ways_in_kept_here(self, sso):
        client, idp, _ = sso
        sso_sign_in(client, idp)
        assert client.get("/auth/me/ways").json()["data"] == {
            "local": False,
            "listed": False,
            "method": "oidc",
            "passwords": False,
            "require_passkey": False,
            "own_passkeys": False,
        }

    def test_a_new_authenticator_takes_over_only_once_a_code_from_it_is_confirmed(
        self, server, clock
    ):
        client, config = server
        ada = person(server, "ada@example.com")
        set_up = ada.get("/auth/me/ways").json()["data"]["authenticator"]["set_up_at"]
        new = ada.post("/auth/me/authenticator/begin", headers=csrf(ada)).json()["data"]
        assert (
            new["secret"] != ada.secret
            and new["uri"].startswith("otpauth://totp/")
            and f"secret={new['secret']}" in new["uri"]
        )

        # Until it is confirmed the old one is still the one, so a phone lost half way locks nobody out.
        clock.move(60)
        early = Browser(client.app).post(
            "/auth/email/verify", json={"email": "ada@example.com", "code": code_now(new["secret"])}
        )
        assert early.status_code == 401
        sign_in(Browser(client.app), "ada@example.com", ada.secret, clock)

        assert (
            ada.post(
                "/auth/me/authenticator/confirm",
                json={"code": code_now(new["secret"])},
                headers=csrf(ada),
            ).status_code
            == 200
        )
        clock.move(60)
        old = Browser(client.app).post(
            "/auth/email/verify", json={"email": "ada@example.com", "code": code_now(ada.secret)}
        )
        assert old.status_code == 401
        sign_in(Browser(client.app), "ada@example.com", new["secret"], clock)
        assert ada.get("/auth/me/ways").json()["data"]["authenticator"]["set_up_at"] > set_up
        assert [(r["who"], r["method"]) for r in logged(config, "authenticator_replaced")] == [
            ("ada@example.com", "email")
        ]

    def test_the_new_authenticators_code_is_drawn_in_the_dashboards_look(self, server):
        """The same drawing as at enrolment: the dashboard's mark in the middle, its ink on its paper."""
        pytest.importorskip("qrcode", reason="the signin extra is not installed")
        from vectrixdb.signin import qr

        client, config = server
        ada = person(server, "ada@example.com")
        new = ada.post("/auth/me/authenticator/begin", headers=csrf(ada)).json()["data"]
        assert new["qr"] == qr.svg(new["uri"], **client.app.state.brand.code_look())
        assert f'fill="{qr.MARK_FILL}"' in new["qr"], (
            "no brand here, so the layers mark in VectrixDB's gold"
        )

    def test_a_code_from_the_old_authenticator_does_not_confirm_the_new_one(self, server, clock):
        client, config = server
        ada = person(server, "ada@example.com")
        new = ada.post("/auth/me/authenticator/begin", headers=csrf(ada)).json()["data"]["secret"]
        wrong = ada.post(
            "/auth/me/authenticator/confirm",
            json={"code": code_now(ada.secret, ahead=1)},
            headers=csrf(ada),
        )
        assert wrong.status_code == 401
        sign_in(Browser(client.app), "ada@example.com", ada.secret, clock)
        assert (
            ada.post(
                "/auth/me/authenticator/confirm", json={"code": code_now(new)}, headers=csrf(ada)
            ).status_code
            == 200
        )
        assert len(logged(config, "authenticator_replaced")) == 1

    def test_new_recovery_codes_replace_the_whole_old_set(self, server):
        client, config = server
        ada = person(server, "ada@example.com")
        made = ada.post("/auth/me/recovery-codes", headers=csrf(ada))
        fresh = made.json()["data"]["recovery_codes"]
        assert len(fresh) == 10 and not set(fresh) & set(ada.codes)
        assert all(
            re.fullmatch(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}", c) for c in fresh
        )
        assert (
            Browser(client.app)
            .post("/auth/email/verify", json={"email": "ada@example.com", "code": ada.codes[0]})
            .status_code
            == 401
        )
        used = Browser(client.app).post(
            "/auth/email/verify", json={"email": "ada@example.com", "code": fresh[0]}
        )
        assert used.status_code == 200 and used.json()["data"]["recovery_codes_left"] == 9
        assert ada.get("/auth/me/ways").json()["data"]["recovery_codes_left"] == 9
        assert [(r["who"], r["method"]) for r in logged(config, "recovery_codes_made")] == [
            ("ada@example.com", "email")
        ]


# --------------------------------------------------- 3 · where you signed in


@pytest.mark.parametrize(
    "agent, device",
    [
        (EDGE_ON_WINDOWS, "Edge on Windows"),
        (OPERA_ON_WINDOWS, "Opera on Windows"),
        (CHROME_ON_MAC, "Chrome on Mac"),
        (CHROME_ON_ANDROID, "Chrome on Android"),
        (SAFARI_ON_IPHONE, "Safari on iPhone"),
        (FIREFOX_ON_LINUX, "Firefox on Linux"),
        ("curl/8.7.1", "A browser"),
        (None, "A browser"),
    ],
)
def test_a_device_is_named_by_its_browser_and_its_system(agent, device):
    assert device_of(agent) == device


class TestWhereYouAreSignedIn:
    def test_each_session_is_listed_by_device_with_where_and_when_and_this_one_is_marked(
        self, server, clock
    ):
        client, _ = server
        laptop = person(server, "ada@example.com", agent=EDGE_ON_WINDOWS, address="203.0.113.7")
        phone = Browser(client.app, agent=SAFARI_ON_IPHONE, address="198.51.100.20")
        sign_in(phone, "ada@example.com", laptop.secret, clock)
        clock.move(300)
        assert phone.get("/api/v1/collections").status_code == 200

        listed = laptop.get("/auth/me/sessions").json()["data"]["sessions"]
        assert sorted((s["device"], s["address"], s["method"], s["current"]) for s in listed) == [
            ("Edge on Windows", "203.0.113.7", "email", True),
            ("Safari on iPhone", "198.51.100.20", "email", False),
        ]
        on_the_phone = next(s for s in listed if s["device"] == "Safari on iPhone")
        assert on_the_phone["last_seen"] >= on_the_phone["created_at"] + 300, (
            "last seen moves with use"
        )
        cookies = laptop.cookies.get("__Host-vx_sid") + phone.cookies.get("__Host-vx_sid")
        for s in listed:
            assert set(s) == {
                "id",
                "method",
                "created_at",
                "last_seen",
                "address",
                "device",
                "current",
            }
            assert re.fullmatch(r"[0-9a-f]{16}", s["id"]) and s["id"] not in cookies, (
                "nothing listed signs anybody in"
            )
        from_the_phone = {
            s["device"]: s["current"]
            for s in phone.get("/auth/me/sessions").json()["data"]["sessions"]
        }
        assert from_the_phone == {"Edge on Windows": False, "Safari on iPhone": True}

    def test_ending_one_session_signs_out_that_browser_and_no_other(self, server, clock):
        client, config = server
        laptop = person(server, "ada@example.com", agent=EDGE_ON_WINDOWS)
        phone = Browser(client.app, agent=SAFARI_ON_IPHONE)
        sign_in(phone, "ada@example.com", laptop.secret, clock)
        target = next(
            s["id"]
            for s in laptop.get("/auth/me/sessions").json()["data"]["sessions"]
            if not s["current"]
        )
        assert laptop.delete(f"/auth/me/sessions/{target}", headers=csrf(laptop)).status_code == 200
        assert phone.get("/api/v1/collections").status_code == 401
        assert laptop.get("/api/v1/collections").status_code == 200
        assert [
            s["current"] for s in laptop.get("/auth/me/sessions").json()["data"]["sessions"]
        ] == [True]
        assert [(r["who"], r["reason"]) for r in logged(config, "sessions_ended")] == [
            ("ada@example.com", "one")
        ]

    def test_this_browser_is_left_to_sign_out_and_an_unknown_session_is_not_found(self, server):
        ada = person(server, "ada@example.com")
        this = ada.get("/auth/me/sessions").json()["data"]["sessions"][0]["id"]
        assert ada.delete(f"/auth/me/sessions/{this}", headers=csrf(ada)).status_code == 400
        assert (
            ada.delete("/auth/me/sessions/0123456789abcdef", headers=csrf(ada)).status_code == 404
        )
        assert ada.get("/api/v1/collections").status_code == 200

    def test_signing_out_everywhere_else_keeps_this_browser(self, server, clock):
        client, config = server
        here = person(server, "ada@example.com")
        elsewhere = [Browser(client.app), Browser(client.app)]
        for browser in elsewhere:
            sign_in(browser, "ada@example.com", here.secret, clock)
        ended = here.post("/auth/me/sessions/end-others", headers=csrf(here))
        assert ended.status_code == 200 and ended.json()["data"] == {"ended": 2}
        assert [b.get("/api/v1/collections").status_code for b in elsewhere] == [401, 401]
        assert here.get("/api/v1/collections").status_code == 200
        assert [s["current"] for s in here.get("/auth/me/sessions").json()["data"]["sessions"]] == [
            True
        ]
        assert [r["reason"] for r in logged(config, "sessions_ended")] == ["2 others"]

    def test_nobody_can_end_somebody_elses_session(self, server):
        ada = person(server, "ada@example.com")
        olu = person(server, "olu@example.com")
        theirs = olu.get("/auth/me/sessions").json()["data"]["sessions"][0]["id"]
        assert theirs not in {
            s["id"] for s in ada.get("/auth/me/sessions").json()["data"]["sessions"]
        }
        assert ada.delete(f"/auth/me/sessions/{theirs}", headers=csrf(ada)).status_code == 404, (
            "not even an admin, from here"
        )
        assert ada.post("/auth/me/sessions/end-others", headers=csrf(ada)).json()["data"] == {
            "ended": 0
        }
        assert olu.get("/api/v1/collections").status_code == 200


# ------------------------------------------------------- 4 · named API keys


class TestNamedKeys:
    def test_only_an_admin_lists_makes_or_revokes_keys(self, server):
        client, _ = server
        ada = person(server, "ada@example.com")
        made = make_key(ada, "nightly-ingest", "operator")
        for somebody in (person(server, "olu@example.com"), person(server, "vi@example.com")):
            assert somebody.get("/api/v1/keys").status_code == 403
            assert (
                somebody.post(
                    "/api/v1/keys", json={"name": "mine", "role": "reader"}, headers=csrf(somebody)
                ).status_code
                == 403
            )
            assert (
                somebody.delete(f"/api/v1/keys/{made['id']}", headers=csrf(somebody)).status_code
                == 403
            )
        by_key = {"api-key": made["key"]}
        assert client.get("/api/v1/keys", headers=by_key).status_code == 403
        assert (
            client.post(
                "/api/v1/keys", json={"name": "child", "role": "reader"}, headers=by_key
            ).status_code
            == 403
        )
        assert [k["name"] for k in ada.get("/api/v1/keys").json()["data"]["keys"]] == [
            "nightly-ingest"
        ]

    def test_a_key_is_shown_once_and_kept_only_as_its_hash(self, server):
        _, config = server
        ada = person(server, "ada@example.com")
        made = make_key(ada, "nightly-ingest", "searcher")
        shape = re.fullmatch(r"vx_([0-9a-f]{8})_([A-Za-z0-9_-]{43})", made["key"])
        assert (
            shape
            and shape.group(1) == made["id"]
            and made["prefix"] == made["key"][:11] == f"vx_{made['id']}"
        )
        assert made == {
            **made,
            "name": "nightly-ingest",
            "role": "searcher",
            "created_by": "ada@example.com",
            "last_used": None,
        }
        listed = ada.get("/api/v1/keys")
        assert listed.json()["data"]["keys"] == [{k: v for k, v in made.items() if k != "key"}]
        assert shape.group(2) not in listed.text, "shown once, when it was made"
        assert shape.group(2).encode() not in Path(config.store_path).read_bytes()
        with closing(sqlite3.connect(str(config.store_path))) as db:
            assert db.execute("SELECT key FROM records WHERE kind = 'apikey'").fetchall() == [
                (hashlib.sha256(made["key"].encode()).hexdigest(),)
            ]

    def test_a_key_is_a_reader_a_searcher_or_an_operator_and_nothing_else(self, server):
        ada = person(server, "ada@example.com")
        assert ada.get("/api/v1/keys").json()["data"]["roles"] == [
            {"role": "reader", "means": "Read only"},
            {"role": "searcher", "means": "Read and search"},
            {"role": "operator", "means": "Read, search and write"},
        ]
        for role in ("admin", "viewer", "guest", "root"):
            assert (
                ada.post(
                    "/api/v1/keys", json={"name": "too-much", "role": role}, headers=csrf(ada)
                ).status_code
                == 400
            ), role
        assert (
            ada.post(
                "/api/v1/keys", json={"name": "   ", "role": "reader"}, headers=csrf(ada)
            ).status_code
            == 400
        )
        assert ada.get("/api/v1/keys").json()["data"]["keys"] == []

    @pytest.mark.parametrize(
        "role, searches, writes",
        [("reader", False, False), ("searcher", True, False), ("operator", True, True)],
    )
    def test_what_each_kind_of_key_may_do(self, server, role, searches, writes):
        client, _ = server
        ada = person(server, "ada@example.com")
        key = {"api-key": make_key(ada, f"a-{role}", role)["key"]}
        assert (
            client.get("/api/v1/collections/plain/points/p-alpha", headers=key).status_code == 200
        )
        search = client.post(
            "/api/v1/collections/plain/search",
            json={"query": VECTORS["alpha"], "limit": 1},
            headers=key,
        )
        assert search.status_code == (200 if searches else 403)
        write = client.post(
            "/api/v1/collections/plain/points",
            json={"points": [{"id": "p-gamma", "vector": VECTORS["gamma"]}]},
            headers=key,
        )
        assert write.status_code == (200 if writes else 403)
        for path in ("/auth/people", "/api/v1/keys", "/api/v1/audit", "/api/v1/access"):
            assert client.get(path, headers=key).status_code == 403, path
        assert (
            client.post(
                "/auth/people", json={"email": "sam@example.com", "role": "admin"}, headers=key
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/keys", json={"name": "child", "role": "operator"}, headers=key
            ).status_code
            == 403
        )
        assert client.delete("/api/v1/collections/plain", headers=key).status_code == 403

    def test_a_key_has_no_ways_in_and_no_sessions_of_its_own(self, server):
        client, _ = server
        ada = person(server, "ada@example.com")
        key = {"api-key": make_key(ada, "nightly-ingest", "operator")["key"]}
        for method, path in (
            ("GET", "/auth/me/ways"),
            ("GET", "/auth/me/sessions"),
            ("POST", "/auth/me/sessions/end-others"),
            ("POST", "/auth/me/recovery-codes"),
            ("POST", "/auth/me/authenticator/begin"),
        ):
            assert client.request(method, path, headers=key).status_code == 400, path
        assert client.post("/auth/step-up", json={"code": "123456"}, headers=key).status_code == 400

    def test_when_a_key_was_last_used_is_kept(self, server, clock):
        client, _ = server
        ada = person(server, "ada@example.com")
        made = make_key(ada, "nightly-ingest", "reader")

        def last_used():
            return next(
                k["last_used"]
                for k in ada.get("/api/v1/keys").json()["data"]["keys"]
                if k["id"] == made["id"]
            )

        assert last_used() is None
        assert (
            client.get("/api/v1/collections", headers={"api-key": made["key"]}).status_code == 200
        )
        first = last_used()
        assert first is not None and first >= made["created_at"]
        clock.move(120)
        client.get("/api/v1/collections", headers={"api-key": made["key"]})
        assert last_used() >= first + 120

    def test_a_revoked_key_stops_working_at_once(self, server):
        client, _ = server
        ada = person(server, "ada@example.com")
        made = make_key(ada, "nightly-ingest", "searcher")
        key = {"api-key": made["key"]}
        assert client.get("/api/v1/collections", headers=key).status_code == 200
        assert ada.delete(f"/api/v1/keys/{made['id']}", headers=csrf(ada)).status_code == 200
        gone = client.get("/api/v1/collections", headers=key)
        assert gone.status_code == 401 and gone.json()["message"] == "Invalid API key"
        assert ada.get("/api/v1/keys").json()["data"]["keys"] == []
        assert ada.delete(f"/api/v1/keys/{made['id']}", headers=csrf(ada)).status_code == 404
        assert ada.delete("/api/v1/keys/00000000", headers=csrf(ada)).status_code == 404

    def test_the_access_log_names_the_key_and_never_holds_it(self, server):
        client, config = server
        ada = person(server, "ada@example.com")
        searcher = make_key(ada, "nightly-ingest", "searcher")
        reader = make_key(ada, "dashboard-export", "reader")
        client.post(
            "/api/v1/collections/plain/search",
            json={"query": VECTORS["alpha"]},
            headers={"api-key": searcher["key"]},
        )
        client.post(
            "/api/v1/collections/plain/search",
            json={"query": VECTORS["alpha"]},
            headers={"api-key": reader["key"]},
        )
        ada.delete(f"/api/v1/keys/{reader['id']}", headers=csrf(ada))

        assert [(r["who"], r["role"], r["by"]) for r in logged(config, "key_created")] == [
            ("nightly-ingest", "searcher", "ada@example.com"),
            ("dashboard-export", "reader", "ada@example.com"),
        ]
        assert [
            (r["who"], r["role"], r["method"], r["collection"]) for r in logged(config, "search")
        ] == [("key:nightly-ingest", "searcher", "key", "plain")]
        assert [(r["who"], r["action"]) for r in logged(config, "denied")] == [
            ("key:dashboard-export", "search")
        ]
        assert [(r["who"], r["by"]) for r in logged(config, "key_revoked")] == [
            ("dashboard-export", "ada@example.com")
        ]
        everything = Path(config.access_log).read_text(encoding="utf-8")
        assert searcher["key"] not in everything and reader["key"] not in everything


# ----------------------------------------------------------------- 5 · people


class TestPeople:
    def test_a_reset_forgets_every_way_in_and_signs_them_out(self, serve, clock):
        with serve(passwords=True) as served:
            client, config = served
            store = client.app.state.signin.store
            ada = person(served, "ada@example.com", password=PASSWORD)
            olu = person(served, "olu@example.com", password=PASSWORD)
            store.add_passkey(
                "olu@example.com",
                b"credential-1",
                b"public-key",
                -7,
                0,
                ["internal"],
                "Olu's laptop",
            )
            before = store.ways("olu@example.com")
            assert (
                before["authenticator"]
                and before["password"]
                and before["passkeys"]
                and before["recovery_codes_left"] == 10
            )

            assert (
                ada.post("/auth/people/olu@example.com/reset", headers=csrf(ada)).status_code == 200
            )
            assert store.ways("olu@example.com") == {
                "passkeys": [],
                "authenticator": None,
                "recovery_codes_left": 0,
                "password": None,
            }
            listed = next(
                p
                for p in ada.get("/auth/people").json()["data"]["people"]
                if p["email"] == "olu@example.com"
            )
            assert (listed["enrolled"], listed["authenticator"], listed["passkeys"]) == (
                False,
                False,
                0,
            )
            assert olu.get("/api/v1/collections").status_code == 401, (
                "every session they had is over"
            )

            clock.move(60)
            for old in (code_now(olu.secret), olu.codes[0]):
                again = Browser(client.app).post(
                    "/auth/email/verify",
                    json={"email": "olu@example.com", "code": old, "password": PASSWORD},
                )
                assert again.status_code == 401
            assert [(r["who"], r["by"]) for r in logged(config, "authenticator_reset")] == [
                ("olu@example.com", "ada@example.com")
            ]
            Browser(client.app).post("/auth/email/begin", json={"email": "olu@example.com"})
            assert (
                config.sender.sent[-1][0] == "olu@example.com"
                and "#/enrol?token=" in config.sender.sent[-1][2]
            )

    def test_a_change_of_role_keeps_what_was_given_unless_it_is_given_again(self, server, clock):
        ada = person(server, "ada@example.com")
        olu = person(server, "olu@example.com")

        def change(**body):
            reply = ada.post(
                "/auth/people", json={"email": "olu@example.com", **body}, headers=csrf(ada)
            )
            assert reply.status_code == 200, reply.text
            return reply.json()["data"]

        assert change(role="operator", grants=["document.read"], principal={"clients": ["acme"]})[
            "grants"
        ] == ["document.read"]
        assert olu.get("/api/v1/collections").status_code == 401, (
            "a session carries what was given, so a change ends it"
        )

        demoted = change(role="viewer")
        assert (demoted["role"], demoted["grants"], demoted["principal"]) == (
            "viewer",
            ["document.read"],
            {"clients": ["acme"]},
        )
        sign_in(olu, "olu@example.com", olu.secret, clock)
        assert "document.read" not in olu.get("/auth/me").json()["data"]["actions"], (
            "a grant widens an operator, it makes no viewer one"
        )

        assert change(role="operator")["grants"] == ["document.read"]
        sign_in(olu, "olu@example.com", olu.secret, clock)
        assert "document.read" in olu.get("/auth/me").json()["data"]["actions"]

        cleared = change(role="operator", grants=[])
        assert (cleared["grants"], cleared["principal"]) == ([], {"clients": ["acme"]})

    def test_an_admin_may_leave_only_while_another_admin_stays(self, server):
        client, _ = server
        ada = person(server, "ada@example.com")
        for address in ("ada@example.com", "ADA@Example.com"):
            assert ada.delete(f"/auth/people/{address}", headers=csrf(ada)).status_code == 409
            assert (
                ada.post(
                    "/auth/people", json={"email": address, "role": "operator"}, headers=csrf(ada)
                ).status_code
                == 409
            )
        me = ada.get("/auth/me").json()["data"]
        assert (me["person"]["role"], me["admins"]) == ("admin", 1), (
            "and nothing about them changed"
        )

        assert (
            ada.post(
                "/auth/people",
                json={"email": "grace@example.com", "role": "admin"},
                headers=csrf(ada),
            ).status_code
            == 200
        )
        assert ada.delete("/auth/people/ada@example.com", headers=csrf(ada)).status_code == 200
        assert ada.get("/api/v1/collections").status_code == 401, "leaving signs them out"
        assert [p.email for p in client.app.state.signin.store.people() if p.role == "admin"] == [
            "grace@example.com"
        ]


# ---------------------------------------------------- 6 · the page's first ask


class TestWhatThePageIsToldFirst:
    @pytest.mark.parametrize(
        "on", [False, True], ids=["guests and passwords off", "guests and passwords on"]
    )
    def test_me_carries_the_version_and_whether_guests_and_passwords_are_on(self, serve, on):
        with serve(guests=on, passwords=on) as served:
            me = (
                person(served, "olu@example.com", password=PASSWORD if on else None)
                .get("/auth/me")
                .json()["data"]
            )
        assert (me["version"], me["guests"], me["passwords"]) == (vectrixdb.__version__, on, on)

    def test_the_version_is_for_people_who_signed_in(self, server):
        client, _ = server
        anybody = client.get("/auth/me")
        assert anybody.status_code == 401 and "version" not in anybody.json()["data"]

    def test_an_admin_is_told_how_many_admins_there_are_and_nobody_else_is(self, server):
        ada = person(server, "ada@example.com")
        olu = person(server, "olu@example.com")
        assert ada.get("/auth/me").json()["data"]["admins"] == 1, "the page warns a lone admin"
        assert "admins" not in olu.get("/auth/me").json()["data"]
        assert (
            ada.post(
                "/auth/people",
                json={"email": "grace@example.com", "role": "admin"},
                headers=csrf(ada),
            ).status_code
            == 200
        )
        assert ada.get("/auth/me").json()["data"]["admins"] == 2

    def test_with_single_sign_on_alone_there_is_no_list_of_admins_to_count(self, sso):
        client, idp, _ = sso
        sso_sign_in(client, idp)
        me = client.get("/auth/me").json()["data"]
        assert me["person"]["role"] == "admin" and "admins" not in me
