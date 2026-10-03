"""Emergency sign-in: one named admin and a password from a vault, while single sign-on is down.

What is being held to: it is off unless VECTRIXDB_BREAK_GLASS says on, and
off its address answers 404, the same as an address that is not there; it
is for a server that signs people in with single sign-on, and needs its time
limit, its admin and the hash of its password, each one refused by name when
missing or wrong; the password itself is never a setting, and one put there
stops the start, as an authenticator secret does, since no code is asked for;
the hash is made by a command that takes the password unseen and refuses a
weak or short one; the username and the password are taken together, a wrong
try is counted and the door shuts after five; each sign-in is written as
break_glass_used; a password works for one emergency, so once the emergency
it was used in is over, by its time or by being turned off, it is refused on
every server and a server set with it will not start; the session it opens is
an admin's, says how it came in, and ends when emergency sign-in is turned
off or its time runs out, the step-up asking for the password again; and
admins are told it is on, with when it turns itself off.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")

sys.path.insert(0, os.path.dirname(__file__))
from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.signin import (
    BreakGlass,
    OidcConfig,
    SignInConfig,
    SignInStore,
    break_glass_hash,
    passwords,
)  # noqa: E402
from vectrixdb.signin.access import EVENTS  # noqa: E402

SECRET = "k" * 48
PUBLIC = "https://vectors.example.test"
ISSUER = "https://login.example.test/tenant/v2.0"
ADMIN = "emergency.admin"
PASSWORD = "harbour-lantern-violet-47-tides"
HASH = break_glass_hash(PASSWORD, ADMIN)
ANOTHER = "meadow-copper-signal-82-orchard"
HOUR = 3600


def create_app(**kwargs):
    """The server as it is now; another test re-imports vectrixdb.api."""
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


def _env(**over) -> dict:
    env = {
        "VECTRIXDB_SIGNIN": "oidc",
        "VECTRIXDB_SIGNIN_SECRET": SECRET,
        "VECTRIXDB_PUBLIC_URL": PUBLIC,
        "VECTRIXDB_OIDC_ISSUER": ISSUER,
        "VECTRIXDB_OIDC_CLIENT_ID": "vectrixdb",
        "VECTRIXDB_OIDC_ROLE_MAP": '{"g-admins": "admin"}',
        "VECTRIXDB_BREAK_GLASS": "on",
        "VECTRIXDB_BREAK_GLASS_UNTIL": "2099-01-01T02:00Z",
        "VECTRIXDB_BREAK_GLASS_ADMIN": ADMIN,
        "VECTRIXDB_BREAK_GLASS_PASSWORD_HASH": HASH,
    }
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


def _nowhere(*args, **kwargs):
    raise AssertionError(
        "emergency sign-in never asks the identity provider: it is for while that is down"
    )


def glass(until: float, hashed: str = HASH) -> BreakGlass:
    return BreakGlass(admin=ADMIN, password_hash=hashed, until=until)


@pytest.fixture
def serve(tmp_path, monkeypatch):
    """A server with single sign-on, and emergency sign-in set as the test asks. Nothing listens.

    Every server a test builds keeps its sign-in in the same place, as the
    servers of one deployment do, and as one server does across a restart.
    """
    monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
    built = []
    root = tmp_path / "db"

    def build(break_glass=None):
        config = SignInConfig(
            methods=("oidc",),
            secrets=(SECRET,),
            public_url=PUBLIC,
            oidc=OidcConfig(
                issuer=ISSUER,
                client_id="vectrixdb",
                role_map={"g-admins": "admin"},
                allowed_emails=("*",),
            ),
            store_path=root / "auth" / "signin.db",
            access_log=root / "auth" / "access.jsonl",
            break_glass=break_glass,
        )
        app = create_app(
            db_path=str(root), enable_dashboard=False, signin=config, oidc_transport=_nowhere
        )
        built.append(app)
        return TestClient(app, base_url=PUBLIC)

    yield build
    for app in built:
        app.state.signin.close()


def logged(client: TestClient, event: str) -> list:
    path = Path(client.app.state.signin.config.access_log)
    lines = (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if path.exists()
        else []
    )
    return [line for line in lines if line.get("event") == event]


def sign_in(client: TestClient, **over):
    body = {"username": ADMIN, "password": PASSWORD}
    body.update(over)
    return client.post("/auth/break-glass", json=body)


# ----------------------------------------------------------- the settings


class TestTheSettings:
    def test_off_unless_asked_for(self, tmp_path):
        assert SignInConfig.from_env(tmp_path, _env(VECTRIXDB_BREAK_GLASS=None)).break_glass is None
        assert (
            SignInConfig.from_env(tmp_path, _env(VECTRIXDB_BREAK_GLASS="off")).break_glass is None
        )

    def test_on_it_holds_the_admin_the_hash_and_when_it_ends(self, tmp_path):
        got = SignInConfig.from_env(tmp_path, _env()).break_glass
        assert got.admin == ADMIN and got.until_iso == "2099-01-01T02:00:00Z" and got.open()
        assert got.password_hash == HASH and HASH not in repr(got), (
            "not even the hash reaches a log line"
        )
        assert len(got.mark) == 24 and got.mark not in HASH, (
            "what a used password is known by opens nothing"
        )

    @pytest.mark.parametrize(
        "given, expected",
        [
            ("2099-01-01T02:00Z", "2099-01-01T02:00:00Z"),
            ("2099-01-01T02:00:00+00:00", "2099-01-01T02:00:00Z"),
            ("2099-01-01T04:00:00+02:00", "2099-01-01T02:00:00Z"),
            ("2099-01-01T02:00", "2099-01-01T02:00:00Z"),
        ],
    )
    def test_the_time_limit_is_read_in_utc(self, tmp_path, given, expected):
        assert (
            SignInConfig.from_env(
                tmp_path, _env(VECTRIXDB_BREAK_GLASS_UNTIL=given)
            ).break_glass.until_iso
            == expected
        )

    def test_it_goes_with_every_way_in_and_needs_one(self, tmp_path):
        for way in ("email", "oidc,email", "oidc"):
            assert (
                SignInConfig.from_env(tmp_path, _env(VECTRIXDB_SIGNIN=way)).break_glass is not None
            ), way
        with pytest.raises(ConfigurationError, match="sign-in is not"):
            SignInConfig.from_env(tmp_path, {"VECTRIXDB_BREAK_GLASS": "on"})

    @pytest.mark.parametrize(
        "missing",
        [
            "VECTRIXDB_BREAK_GLASS_UNTIL",
            "VECTRIXDB_BREAK_GLASS_ADMIN",
            "VECTRIXDB_BREAK_GLASS_PASSWORD_HASH",
        ],
    )
    def test_every_part_is_needed_and_a_missing_one_is_named(self, tmp_path, missing):
        with pytest.raises(ConfigurationError, match=missing):
            SignInConfig.from_env(tmp_path, _env(**{missing: None}))

    @pytest.mark.parametrize(
        "setting, value, says",
        [
            (
                "VECTRIXDB_BREAK_GLASS_ADMIN",
                "emergency admin",
                "letters, digits, dots, dashes and @",
            ),
            ("VECTRIXDB_BREAK_GLASS_PASSWORD_HASH", PASSWORD, "is not a hash this server made"),
            (
                "VECTRIXDB_BREAK_GLASS_PASSWORD_HASH",
                "sha256$abcdef",
                "is not a hash this server made",
            ),
            ("VECTRIXDB_BREAK_GLASS_UNTIL", "tomorrow at two", "Write it in UTC"),
        ],
    )
    def test_a_wrong_part_stops_the_start_with_what_is_wrong(self, tmp_path, setting, value, says):
        with pytest.raises(ConfigurationError, match=says) as stopped:
            SignInConfig.from_env(tmp_path, _env(**{setting: value}))
        assert PASSWORD not in str(stopped.value), (
            "a password put where the hash goes is not repeated"
        )

    @pytest.mark.parametrize(
        "name", ["VECTRIXDB_BREAK_GLASS_PASSWORD", "VECTRIXDB_BREAK_GLASS_PASSWORD_FILE"]
    )
    def test_the_password_itself_in_the_settings_stops_the_start_and_names_the_command(
        self, tmp_path, name
    ):
        with pytest.raises(ConfigurationError, match="the settings keep only its hash") as stopped:
            SignInConfig.from_env(tmp_path, _env(**{name: PASSWORD}))
        assert "vectrixdb break-glass hash" in str(stopped.value) and PASSWORD not in str(
            stopped.value
        )

    def test_an_authenticator_secret_stops_the_start_since_no_code_is_asked_for(self, tmp_path):
        with pytest.raises(ConfigurationError, match="takes no authenticator code"):
            SignInConfig.from_env(
                tmp_path, _env(VECTRIXDB_BREAK_GLASS_TOTP="JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP")
            )

    def test_a_time_already_past_starts_the_server_with_it_off_and_says_so(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="vectrixdb.signin"):
            got = SignInConfig.from_env(
                tmp_path, _env(VECTRIXDB_BREAK_GLASS_UNTIL="2020-01-01T00:00Z")
            ).break_glass
        assert got is not None and not got.open()
        assert "turned itself off at 2020-01-01T00:00:00Z" in caplog.text

    def test_the_hash_may_come_from_a_file(self, tmp_path):
        (tmp_path / "hash").write_text(HASH + "\n", encoding="utf-8")
        env = _env(
            VECTRIXDB_BREAK_GLASS_PASSWORD_HASH=None,
            VECTRIXDB_BREAK_GLASS_PASSWORD_HASH_FILE=str(tmp_path / "hash"),
        )
        assert SignInConfig.from_env(tmp_path, env).break_glass.password_hash == HASH

    def test_the_event_is_in_the_logs_vocabulary(self):
        assert "break_glass_used" in EVENTS


class TestMakingTheHash:
    def test_it_checks_the_password_and_cannot_be_read_back_as_one(self):
        assert HASH.startswith("scrypt$") and PASSWORD not in HASH
        assert passwords.check(PASSWORD, HASH) and not passwords.check(PASSWORD + "x", HASH)
        assert break_glass_hash(PASSWORD) != HASH, (
            "salted: the same password never hashes the same twice"
        )

    @pytest.mark.parametrize(
        "password, says",
        [
            ("short-one-12", "It is|is 12 characters. Use 24 or more"),
            ("a" * 30, "one of the first anybody tries|too easy|repeat"),
            ("emergency.admin-harbour-lantern", "Leave your"),
        ],
    )
    def test_a_short_or_weak_one_is_refused(self, password, says):
        with pytest.raises(ConfigurationError) as stopped:
            break_glass_hash(password, ADMIN)
        assert password not in str(stopped.value)

    def test_the_command_takes_it_unseen_and_prints_the_hash_alone(self):
        from typer.testing import CliRunner

        from vectrixdb.cli import app

        result = CliRunner().invoke(
            app, ["break-glass", "hash", "--admin", ADMIN], input=f"{PASSWORD}\n{PASSWORD}\n"
        )
        assert result.exit_code == 0, result.output
        assert PASSWORD not in result.output, "typed unseen, and never printed"
        line = next(
            part
            for part in result.output.split()
            if part.startswith("VECTRIXDB_BREAK_GLASS_PASSWORD_HASH=")
        )
        assert passwords.check(PASSWORD, line.split("=", 1)[1])

    def test_the_command_refuses_a_short_one_with_code_2(self):
        from typer.testing import CliRunner

        from vectrixdb.cli import app

        result = CliRunner().invoke(app, ["break-glass", "hash"], input="too-short\ntoo-short\n")
        assert result.exit_code == 2 and "Use 24 or more" in " ".join(result.output.split())
        assert "scrypt$" not in result.output


# ------------------------------------------------------------ the address


class TestOffItIsNotThere:
    @pytest.mark.parametrize("state", ["off", "run out"])
    def test_both_ways_answer_404_like_an_address_that_does_not_exist(self, serve, state):
        client = serve(None if state == "off" else glass(time.time() - 1))
        missing = client.get("/auth/email/no-such-thing")
        assert missing.status_code == 404
        for reply in (client.get("/auth/break-glass"), sign_in(client)):
            assert reply.status_code == 404 and reply.json()["message"] == missing.json()["message"]
        assert not logged(client, "break_glass_used")

    def test_with_sign_in_off_it_is_not_there_either(self, tmp_path):
        client = TestClient(
            create_app(db_path=str(tmp_path / "db"), enable_dashboard=False), base_url=PUBLIC
        )
        assert client.get("/auth/break-glass").status_code == 404


class TestOnItTakesBothTogether:
    def test_it_says_when_it_turns_itself_off(self, serve):
        reply = serve(glass(4070916000.0)).get("/auth/break-glass")
        assert reply.status_code == 200 and reply.json()["data"] == {
            "until": "2099-01-01T02:00:00Z"
        }

    def test_the_two_right_sign_the_admin_in_and_the_log_says_so(self, serve):
        client = serve(glass(time.time() + HOUR))
        reply = sign_in(client)
        assert reply.status_code == 200
        person = reply.json()["data"]["person"]
        assert (
            person["role"] == "admin"
            and person["method"] == "break_glass"
            and person["name"] == "Emergency admin"
        )
        used = logged(client, "break_glass_used")
        assert len(used) == 1 and used[0]["who"] == ADMIN and used[0]["method"] == "break_glass"
        me = client.get("/auth/me").json()["data"]
        assert me["person"]["subject"] == f"break-glass:{ADMIN}" and "about.read" in me["actions"]

    def test_no_code_is_asked_for_and_one_sent_by_an_old_page_is_ignored(self, serve):
        client = serve(glass(time.time() + HOUR))
        assert sign_in(client, code="000000").status_code == 200

    @pytest.mark.parametrize(
        "wrong",
        [
            {"username": "someone.else"},
            {"password": PASSWORD + "x"},
            {"password": ""},
            {"username": f" {ADMIN.upper()} "},
        ],
    )
    def test_either_one_wrong_refuses_both_without_saying_which(self, serve, wrong):
        client = serve(glass(time.time() + HOUR))
        reply = sign_in(client, **wrong)
        assert reply.status_code == 401
        assert (
            reply.json()["message"]
            == "That username and password were not accepted together. Check both and try again."
        )
        assert client.get("/auth/me").status_code == 401 and not logged(client, "break_glass_used")
        failed = logged(client, "signin_failed")
        assert failed and failed[-1]["method"] == "break_glass" and failed[-1]["reason"] == "wrong"

    def test_the_hash_itself_is_not_the_password(self, serve):
        client = serve(glass(time.time() + HOUR))
        assert sign_in(client, password=HASH).status_code == 401, (
            "somebody who read the settings has not read the password"
        )

    def test_five_wrong_tries_shut_the_door_and_the_right_two_wait(self, serve):
        client = serve(glass(time.time() + HOUR))
        for _ in range(5):
            assert sign_in(client, password="wrong-password-guess").status_code == 401
        held = sign_in(client)
        assert held.status_code == 429 and "Wait 15 minutes" in held.json()["message"]
        assert logged(client, "signin_failed")[-1]["reason"] == "locked"

    def test_admins_are_told_it_is_on_and_until_when(self, serve):
        client = serve(glass(4070916000.0))
        assert sign_in(client).status_code == 200
        assert client.get("/auth/me").json()["data"]["break_glass"] == {
            "until": "2099-01-01T02:00:00Z"
        }


# ------------------------------------------------- one password an emergency


class TestAPasswordWorksForOneEmergency:
    def test_while_the_emergency_lasts_it_goes_on_working(self, serve):
        client = serve(glass(time.time() + HOUR))
        assert sign_in(client).status_code == 200
        assert sign_in(TestClient(client.app, base_url=PUBLIC)).status_code == 200, (
            "a second browser, the same emergency"
        )

    def test_the_same_emergency_given_more_time_keeps_its_password(self, serve):
        assert sign_in(serve(glass(time.time() + HOUR))).status_code == 200
        longer = serve(glass(time.time() + 3 * HOUR))
        assert sign_in(longer).status_code == 200, "its time was moved while it was still on"

    def test_turned_off_and_on_again_the_old_password_is_refused_and_the_server_will_not_start_with_it(
        self, serve
    ):
        assert sign_in(serve(glass(time.time() + HOUR))).status_code == 200
        serve(None)  # single sign-on is back, and emergency sign-in is turned off
        with pytest.raises(ConfigurationError, match="used in an earlier emergency") as stopped:
            serve(glass(time.time() + HOUR))
        assert "vectrixdb break-glass hash" in str(stopped.value)

    def test_once_its_time_has_run_out_it_is_spent_on_a_server_still_running(self, tmp_path):
        store = SignInStore(tmp_path / "signin.db", (SECRET,))
        try:
            first = glass(1000.0)
            assert store.use_emergency(ADMIN, first.mark, first.until, now=500.0)
            assert not store.emergency_spent(ADMIN, first.mark, now=900.0)
            assert store.emergency_spent(ADMIN, first.mark, now=1001.0)
            assert not store.use_emergency(ADMIN, first.mark, 5000.0, now=1001.0), (
                "a later end does not bring it back"
            )
            assert not store.use_emergency(ADMIN, first.mark, 5000.0, now=1002.0), (
                "and it stays spent"
            )
        finally:
            store.close()

    def test_a_password_said_to_be_spent_is_said_only_to_somebody_who_has_it(self, serve):
        client = serve(glass(time.time() + HOUR))
        assert sign_in(client).status_code == 200
        client.app.state.signin.store.close_emergencies()
        other = TestClient(client.app, base_url=PUBLIC)
        spent = sign_in(other)
        assert (
            spent.status_code == 401 and "used in an earlier emergency" in spent.json()["message"]
        )
        assert logged(client, "signin_failed")[-1]["reason"] == "spent"
        guess = sign_in(other, password="wrong-password-guess")
        assert (
            guess.json()["message"]
            == "That username and password were not accepted together. Check both and try again."
        )

    def test_a_new_password_opens_the_next_emergency_and_the_old_one_never_comes_back(self, serve):
        assert sign_in(serve(glass(time.time() + HOUR))).status_code == 200
        serve(None)
        new = break_glass_hash(ANOTHER, ADMIN)
        second = serve(glass(time.time() + HOUR, new))
        assert sign_in(second, password=PASSWORD).status_code == 401, (
            "the old password is not this emergency's"
        )
        assert sign_in(second, password=ANOTHER).status_code == 200
        serve(None)
        with pytest.raises(ConfigurationError, match="used in an earlier emergency"):
            serve(glass(time.time() + HOUR))
        with pytest.raises(ConfigurationError, match="used in an earlier emergency"):
            serve(glass(time.time() + HOUR, new))

    def test_a_password_never_used_is_not_spent_by_being_turned_off(self, serve):
        serve(glass(time.time() + HOUR))
        serve(None)
        assert sign_in(serve(glass(time.time() + HOUR))).status_code == 200


class TestItsSessionEndsWithIt:
    def test_when_its_time_runs_out_the_session_is_gone(self, serve):
        client = serve(glass(time.time() + HOUR))
        assert sign_in(client).status_code == 200
        runtime = client.app.state.signin
        runtime.config.break_glass = glass(time.time() - 1)
        assert client.get("/auth/me").status_code == 401
        assert client.get("/api/v1/collections").status_code == 401

    def test_when_it_is_turned_off_the_session_is_gone(self, serve):
        client = serve(glass(time.time() + HOUR))
        assert sign_in(client).status_code == 200
        client.app.state.signin.config.break_glass = None
        assert client.get("/auth/me").status_code == 401

    def test_the_session_is_never_longer_than_emergency_sign_in_is_on(self, serve):
        ends = time.time() + 1800
        client = serve(glass(ends))
        person = sign_in(client).json()["data"]["person"]
        assert person["expires_at"] <= ends + 1

    def test_a_change_that_matters_asks_for_the_password_again(self, serve, monkeypatch):
        client = serve(glass(time.time() + HOUR))
        assert sign_in(client).status_code == 200
        csrf = {
            "X-CSRF-Token": client.cookies.get("__Host-vx_csrf") or client.cookies.get("vx_csrf")
        }
        now = time.time()
        monkeypatch.setattr("vectrixdb.api.signin.time.time", lambda: now + 11 * 60)
        stale = client.post(
            "/api/v1/keys", json={"name": "nightly", "role": "reader"}, headers=csrf
        )
        assert stale.status_code == 403 and stale.json()["data"]["ways"] == ["password"]
        wrong = client.post("/auth/step-up", json={"password": "not-the-password"}, headers=csrf)
        assert (
            wrong.status_code == 401
            and wrong.json()["message"] == "That password was not accepted."
        )
        assert (
            client.post("/auth/step-up", json={"code": "000000"}, headers=csrf).status_code == 401
        ), "a code confirms nothing here"
        assert (
            client.post("/auth/step-up", json={"password": PASSWORD}, headers=csrf).status_code
            == 200
        )
        steps = logged(client, "step_up")
        assert steps and steps[-1]["who"] == ADMIN and steps[-1]["method"] == "break_glass"
