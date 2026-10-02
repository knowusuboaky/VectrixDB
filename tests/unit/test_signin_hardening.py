"""Sign-in hardening: where secrets come from, how they are sealed, how the door shuts, and passwords.

What is being held to:

* a secret setting comes from the environment or from the file its ``_FILE``
  twin names, never both, and a file that cannot be read or is empty stops
  start-up instead of starting with no secret;
* each job gets its own key, derived from the sign-in secret with HKDF, and an
  API key the server knows only as its SHA-256 still lets its holder in, with
  the role that key carries;
* authenticator secrets are sealed under the newest sign-in secret, sealed
  again at start-up when an older secret or the pre-2.3 scheme sealed them,
  and a start is refused, with the way out in the message, when nothing
  configured can open them;
* five wrong tries shut the door, for longer each time it shuts, until a real
  sign-in or a day with no lock; the person is emailed when it shuts, recovery
  codes are counted on their own, and one address is stopped at fifty wrong
  tries whatever it claims to be forwarding for;
* passwords, on a server that turns them on, are stored as scrypt, never work
  without the code, fail the same way whichever half is wrong, and are reset
  only with the emailed link and a current code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import numpy as np
import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("cryptography", reason="the signin extra is not installed")

from cryptography.fernet import Fernet, InvalidToken  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.signin import SignInConfig, passwords, totp  # noqa: E402
from vectrixdb.signin import store as store_module  # noqa: E402
from vectrixdb.signin.keys import derive, env_secret, hash_key, key_matches  # noqa: E402
from vectrixdb.signin.store import SignInStore  # noqa: E402


def create_app(**kwargs):
    """The server as it is now. Another test drops and re-imports ``vectrixdb.api``,
    and a name bound at the top of this file would go on pointing at the old copy,
    whose routes reach for a database the new copy's start-up never gave them."""
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


SECRET = "k" * 48
OLD_SECRET = "o" * 48
NEW_SECRET = "n" * 48
PUBLIC = "https://vectors.example.test"
KEY = "the-full-api-key"
READ_KEY = "the-read-only-key"
PASSWORD = "plum orbit ledger 42"
NEW_PASSWORD = "kettle meadow cipher 7"
VECTORS = {"alpha": [1, 0, 0, 0], "beta": [0, 1, 0, 0]}
HELD_15 = "Too many wrong tries. Wait 15 minutes and try again."
REFUSED_BOTH = "That password and code were not accepted together. Check both and try again."

#: Settings a developer's machine might carry, which would change what these tests see.
_SETTINGS = (
    "VECTRIXDB_API_KEY",
    "VECTRIXDB_API_KEY_FILE",
    "VECTRIXDB_API_KEY_SHA256",
    "VECTRIXDB_READ_ONLY_API_KEY",
    "VECTRIXDB_READ_ONLY_API_KEY_FILE",
    "VECTRIXDB_READ_ONLY_API_KEY_SHA256",
    "VECTRIXDB_SIGNIN",
    "VECTRIXDB_AUDIT_JSONL",
)


@pytest.fixture(autouse=True)
def nothing_set_on_this_machine(monkeypatch):
    for name in _SETTINGS:
        monkeypatch.delenv(name, raising=False)


def _embed(texts):
    return np.array([VECTORS[t] for t in texts], dtype=np.float32)


@pytest.fixture
def data(tmp_path):
    """One collection with two points, for a key to read and write against."""
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
    """Collects what the server sends, instead of sending it."""

    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, text):
        self.sent.append((to, subject, text))

    def token(self, page: str = "enrol") -> str:
        return re.search(rf"#/{page}\?token=([\w-]+)", self.sent[-1][2]).group(1)

    def warnings_to(self, email: str) -> list:
        """The text of every message that told this person signing in was paused."""
        return [text for to, subject, text in self.sent if to == email and "paused" in subject]


def _config(root: Path, **over) -> SignInConfig:
    base = {
        "methods": ("email",),
        "secrets": (SECRET,),
        "public_url": PUBLIC,
        "users": (
            ("ada@example.com", "admin"),
            ("olu@example.com", "operator"),
            ("grace@example.com", "operator"),
        ),
        "store_path": root / "auth" / "signin.db",
        "access_log": root / "auth" / "access.jsonl",
        "sender": Mail(),
    }
    base.update(over)
    return SignInConfig(**base)


@contextmanager
def serve(root: Path, **over):
    config = _config(root, **over)
    with TestClient(
        create_app(db_path=str(root), enable_dashboard=False, signin=config), base_url=PUBLIC
    ) as client:
        yield client, config


@pytest.fixture
def server(data):
    with serve(data) as running:
        yield running


@pytest.fixture
def password_server(data):
    with serve(data, passwords=True) as running:
        yield running


class Clock:
    """Stands in for ``time.time``: still, until a test moves it."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    """``time.time`` as the store calls it, held still. The code module calls the same
    function, so a code made here is the one the server expects, and the thirty second
    step moves only when a test moves it."""
    held = Clock(float(int(time.time())))
    monkeypatch.setattr(store_module.time, "time", held)
    return held


def next_step(clock: Clock) -> None:
    """On to the next thirty seconds, where the authenticator shows a code nobody has used."""
    clock.advance(totp.PERIOD)


def current_code(secret: str) -> str:
    return totp.code_at(secret, totp.step_now())


def wrong_code(secret: str) -> str:
    """Six digits that no step the server accepts right now would give."""
    step = totp.step_now()
    live = {totp.code_at(secret, s) for s in range(step - totp.WINDOW, step + totp.WINDOW + 1)}
    return next(code for code in (f"{n:06d}" for n in range(1000)) if code not in live)


def enrol(
    client: TestClient, config: SignInConfig, email: str, password: Optional[str] = None
) -> tuple[str, list]:
    """The whole first visit. Returns the authenticator secret and the recovery codes."""
    assert client.post("/auth/email/begin", json={"email": email}).status_code == 200
    begun = client.post("/auth/email/enrol/begin", json={"token": config.sender.token()}).json()[
        "data"
    ]
    body = {"ticket": begun["ticket"], "code": current_code(begun["secret"])}
    if password is not None:
        body["password"] = password
    done = client.post("/auth/email/enrol/confirm", json=body)
    assert done.status_code == 200, done.text
    return begun["secret"], done.json()["data"]["recovery_codes"]


def verify(browser: TestClient, email: str, code: str, password: Optional[str] = None):
    body = {"email": email, "code": code}
    if password is not None:
        body["password"] = password
    return browser.post("/auth/email/verify", json=body)


def another_browser(client: TestClient, address: str = "testclient") -> TestClient:
    """A browser with no cookies, connecting from ``address``."""
    return TestClient(client.app, base_url=PUBLIC, client=(address, 50000))


def _column(path: Path, column: str, email: str = "ada@example.com"):
    """What the file itself holds for this person, read around the store."""
    db = sqlite3.connect(str(path))
    try:
        return json.loads(
            db.execute(
                "SELECT data FROM records WHERE kind = 'person' AND key = ?", (email,)
            ).fetchone()[0]
        )[column]
    finally:
        db.close()


def sealing_key(secret: str) -> Fernet:
    """How authenticator secrets are sealed now: the HKDF key for sealing, from the sign-in secret."""
    return Fernet(base64.urlsafe_b64encode(derive(secret, "sealing")))


def legacy_key(secret: str) -> Fernet:
    """How they were sealed before 2.3: the SHA-256 of a label and the sign-in secret, as a Fernet key."""
    return Fernet(
        base64.urlsafe_b64encode(
            hashlib.sha256(b"vectrixdb authenticator secrets|" + secret.encode()).digest()
        )
    )


# --------------------------------------------------------- 1 · secrets and keys


class TestWhereASecretComesFrom:
    def test_from_the_environment_without_the_spaces_around_it(self):
        assert (
            env_secret({"VECTRIXDB_API_KEY": "  inline-key \n"}, "VECTRIXDB_API_KEY")
            == "inline-key"
        )

    @pytest.mark.parametrize(
        "written",
        [b"from-a-file\n", b"from-a-file\r\n", b"  from-a-file\n\n"],
        ids=["a newline", "a windows line end", "spaces and blank lines"],
    )
    def test_from_the_file_its_twin_names_without_what_an_editor_leaves_around_it(
        self, tmp_path, written
    ):
        path = tmp_path / "api-key"
        path.write_bytes(written)
        assert (
            env_secret({"VECTRIXDB_API_KEY_FILE": str(path)}, "VECTRIXDB_API_KEY") == "from-a-file"
        )

    def test_neither_set_is_nothing_and_a_blank_value_does_not_count_as_set(self, tmp_path):
        assert env_secret({}, "VECTRIXDB_API_KEY") is None
        assert (
            env_secret(
                {"VECTRIXDB_API_KEY": "  ", "VECTRIXDB_API_KEY_FILE": ""}, "VECTRIXDB_API_KEY"
            )
            is None
        )
        path = tmp_path / "api-key"
        path.write_text("from-a-file", encoding="utf-8")
        assert (
            env_secret(
                {"VECTRIXDB_API_KEY": " ", "VECTRIXDB_API_KEY_FILE": str(path)}, "VECTRIXDB_API_KEY"
            )
            == "from-a-file"
        )

    def test_both_set_is_refused_because_nobody_can_say_which_was_meant(self, tmp_path):
        path = tmp_path / "api-key"
        path.write_text("from-a-file", encoding="utf-8")
        with pytest.raises(
            ConfigurationError, match="VECTRIXDB_API_KEY and VECTRIXDB_API_KEY_FILE are both set"
        ):
            env_secret(
                {"VECTRIXDB_API_KEY": "inline", "VECTRIXDB_API_KEY_FILE": str(path)},
                "VECTRIXDB_API_KEY",
            )

    @pytest.mark.parametrize("missing", [True, False], ids=["a file that is not there", "a folder"])
    def test_a_file_that_cannot_be_read_is_refused_and_named(self, tmp_path, missing):
        target = tmp_path / "not-there" if missing else tmp_path
        with pytest.raises(
            ConfigurationError, match="VECTRIXDB_SIGNIN_SECRET_FILE names .*which cannot be read"
        ):
            env_secret({"VECTRIXDB_SIGNIN_SECRET_FILE": str(target)}, "VECTRIXDB_SIGNIN_SECRET")

    @pytest.mark.parametrize(
        "written", ["", "\n", " \r\n\t"], ids=["nothing", "a newline", "only whitespace"]
    )
    def test_an_empty_file_is_refused_rather_than_read_as_no_secret(self, tmp_path, written):
        path = tmp_path / "signin-secret"
        path.write_text(written, encoding="utf-8")
        with pytest.raises(ConfigurationError, match="which is empty"):
            env_secret({"VECTRIXDB_SIGNIN_SECRET_FILE": str(path)}, "VECTRIXDB_SIGNIN_SECRET")


class TestOneKeyForEachJob:
    def test_each_job_and_each_secret_gets_its_own_key_and_the_same_one_every_time(self):
        cookies, sealing = derive(SECRET, "cookies"), derive(SECRET, "sealing")
        assert len(cookies) == len(sealing) == 32
        assert cookies != sealing, "a value made for one job is useless for the other"
        assert derive(OLD_SECRET, "cookies") != cookies and derive(OLD_SECRET, "sealing") != sealing
        assert derive(SECRET, "cookies") == cookies and derive(SECRET, "sealing") == sealing, (
            "or a restart could not read what the last run wrote"
        )

    @pytest.mark.parametrize("purpose, length", [("cookies", 32), ("sealing", 32), ("sealing", 80)])
    def test_it_is_rfc_5869_hkdf_over_sha256_with_the_salt_and_label_sealed_files_depend_on(
        self, purpose, length
    ):
        expected = HKDF(
            algorithm=hashes.SHA256(),
            length=length,
            salt=b"vectrixdb sign-in",
            info=b"vectrixdb|" + purpose.encode(),
        ).derive(SECRET.encode())
        assert derive(SECRET, purpose, length) == expected

    def test_a_cookie_is_signed_with_the_derived_key_and_never_with_the_secret_itself(self):
        store = SignInStore(":memory:", [SECRET])
        try:
            mac = hmac.new(derive(SECRET, "cookies"), b"value", hashlib.sha256).digest()
            assert store.sign("value") == "value." + base64.urlsafe_b64encode(mac).decode().rstrip(
                "="
            )
        finally:
            store.close()


class TestAnApiKeyGivenAsItsHash:
    def test_the_hash_is_the_sha256_of_the_key_in_hex(self):
        assert hash_key(KEY) == hashlib.sha256(KEY.encode("utf-8")).hexdigest()

    def test_the_key_matches_as_itself_or_as_its_hash_and_nothing_else_does(self):
        hashed = hash_key(KEY)
        assert key_matches(KEY, KEY, None) and key_matches(KEY, None, hashed)
        assert key_matches(KEY, "another-key", hashed), "either form is enough"
        assert not key_matches("wrong", KEY, hashed) and not key_matches(KEY + " ", None, hashed)
        assert not key_matches(hashed, None, hashed), (
            "the hash is what the server keeps, not something to present"
        )
        assert not key_matches(None, KEY, hashed) and not key_matches("", KEY, hashed)
        assert not key_matches(KEY, None, None)

    def test_a_hash_pasted_in_capitals_or_with_a_newline_still_matches(self):
        assert key_matches(KEY, None, "  " + hash_key(KEY).upper() + "\n")


class TestAServerThatKnowsOnlyTheHash:
    def test_with_sign_in_on_the_key_opens_what_the_full_key_opens(self, data, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_API_KEY_SHA256", hash_key(KEY))
        with serve(data) as (client, _):
            full = {"api-key": KEY}
            assert client.get("/api/v1/collections", headers=full).status_code == 200
            assert (
                client.post(
                    "/api/v1/collections", json={"name": "bykey", "dimension": 4}, headers=full
                ).status_code
                == 200
            )
            for wrong in (hash_key(KEY), KEY.upper(), "wrong"):
                assert (
                    client.get("/api/v1/collections", headers={"api-key": wrong}).status_code == 401
                ), wrong

    def test_the_read_only_key_given_as_its_hash_reads_and_does_not_write(self, data, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_API_KEY_SHA256", hash_key(KEY))
        monkeypatch.setenv("VECTRIXDB_READ_ONLY_API_KEY_SHA256", hash_key(READ_KEY).upper())
        with serve(data) as (client, _):
            ro = {"api-key": READ_KEY}
            assert (
                client.get("/api/v1/collections/plain/points/p-alpha", headers=ro).status_code
                == 200
            )
            assert (
                client.post(
                    "/api/v1/collections/plain/points", json={"points": []}, headers=ro
                ).status_code
                == 403
            )
            assert client.get("/api/v1/audit", headers=ro).status_code == 403

    def test_with_sign_in_off_the_hash_alone_turns_the_key_on(self, data, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_API_KEY_SHA256", hash_key(KEY))
        monkeypatch.setenv("VECTRIXDB_READ_ONLY_API_KEY_SHA256", hash_key(READ_KEY))
        from vectrixdb.api.server import _full_key_configured, get_api_key, get_read_only_key

        assert get_api_key() is None and get_read_only_key() is None, (
            "the server holds no key, only its hash"
        )
        assert _full_key_configured()
        with TestClient(create_app(db_path=str(data), enable_dashboard=False)) as client:
            status = client.get("/auth/status").json()["data"]
            assert status["auth_enabled"] is True and status["read_only_key_enabled"] is True
            made = {"name": "made", "dimension": 4}
            assert client.get("/api/v1/collections/plain/points/p-alpha").status_code == 200, (
                "reads never needed the key"
            )
            assert client.post("/api/v1/collections", json=made).status_code == 401
            assert (
                client.post(
                    "/api/v1/collections", json=made, headers={"api-key": READ_KEY}
                ).status_code
                == 403
            )
            assert (
                client.post("/api/v1/collections", json=made, headers={"api-key": KEY}).status_code
                == 200
            )
            assert client.get("/api/v1/collections/plain/policy").status_code == 403
            assert (
                client.get("/api/v1/collections/plain/policy", headers={"api-key": KEY}).status_code
                == 200
            )

    def test_a_key_read_from_a_file_works_without_the_newline_the_file_ends_with(
        self, data, monkeypatch, tmp_path
    ):
        path = tmp_path / "api-key"
        path.write_text(KEY + "\n", encoding="utf-8")
        monkeypatch.setenv("VECTRIXDB_API_KEY_FILE", str(path))
        with serve(data) as (client, _):
            assert client.get("/api/v1/collections", headers={"api-key": KEY}).status_code == 200

    @pytest.mark.parametrize(
        "settings, says",
        [
            ({"VECTRIXDB_API_KEY": KEY, "VECTRIXDB_API_KEY_FILE": "key"}, "both set"),
            ({"VECTRIXDB_READ_ONLY_API_KEY_FILE": "not-there"}, "cannot be read"),
            ({"VECTRIXDB_API_KEY_FILE": "empty"}, "is empty"),
        ],
        ids=["a key and a key file", "a key file that is not there", "an empty key file"],
    )
    def test_a_mistake_in_a_key_setting_stops_the_server_before_it_starts(
        self, data, monkeypatch, tmp_path, settings, says
    ):
        (tmp_path / "key").write_text(KEY, encoding="utf-8")
        (tmp_path / "empty").write_text("\n", encoding="utf-8")
        for name, value in settings.items():
            monkeypatch.setenv(name, str(tmp_path / value) if name.endswith("_FILE") else value)
        with pytest.raises(ConfigurationError, match=says):
            create_app(db_path=str(data), enable_dashboard=False)


# ------------------------------------------------------------------ 2 · sealing


class TestSealing:
    def test_the_setting_is_every_value_comma_separated_newest_first_from_the_environment_or_a_file(
        self, tmp_path
    ):
        base = {"VECTRIXDB_SIGNIN": "email", "VECTRIXDB_PUBLIC_URL": PUBLIC}
        inline = SignInConfig.from_env(
            tmp_path, {**base, "VECTRIXDB_SIGNIN_SECRET": f" {NEW_SECRET} , {OLD_SECRET} ,"}
        )
        assert inline.secrets == (NEW_SECRET, OLD_SECRET)
        path = tmp_path / "signin-secret"
        path.write_text(f"{NEW_SECRET},{OLD_SECRET}\n", encoding="utf-8")
        from_file = SignInConfig.from_env(
            tmp_path, {**base, "VECTRIXDB_SIGNIN_SECRET_FILE": str(path)}
        )
        assert from_file.secrets == (NEW_SECRET, OLD_SECRET)

    def test_a_secret_is_sealed_with_the_key_derived_from_the_first_value(self, tmp_path):
        path = tmp_path / "auth" / "signin.db"
        store = SignInStore(path, [NEW_SECRET, OLD_SECRET])
        store.put_person("ada@example.com", "operator")
        secret = store.begin_enrolment("ada@example.com")
        store.close()
        sealed = _column(path, "totp_secret").encode()
        assert secret.encode() not in sealed
        assert sealing_key(NEW_SECRET).decrypt(sealed) == secret.encode()
        the_secret_itself = Fernet(base64.urlsafe_b64encode(NEW_SECRET.encode()[:32]))
        for other in (sealing_key(OLD_SECRET), legacy_key(NEW_SECRET), the_secret_itself):
            with pytest.raises(InvalidToken):
                other.decrypt(sealed)

    def test_rotating_seals_everything_again_under_the_new_secret_so_the_old_one_can_go(
        self, tmp_path
    ):
        path = tmp_path / "auth" / "signin.db"
        now = 1_800_000_000.0
        first = SignInStore(path, [OLD_SECRET])
        first.put_person("ada@example.com", "operator")
        secret = first.begin_enrolment("ada@example.com")
        assert first.check_code(
            "ada@example.com", totp.code_at(secret, totp.step_now(now)), confirming=True, now=now
        )
        replacement = first.begin_replacement("ada@example.com")
        first.close()
        before = [_column(path, column) for column in ("totp_secret", "totp_pending")]

        SignInStore(path, [NEW_SECRET, OLD_SECRET]).close()
        after = [_column(path, column) for column in ("totp_secret", "totp_pending")]
        assert after[0] != before[0] and after[1] != before[1]
        assert [sealing_key(NEW_SECRET).decrypt(value.encode()).decode() for value in after] == [
            secret,
            replacement,
        ]

        only_new = SignInStore(path, [NEW_SECRET])
        try:
            later = now + totp.PERIOD
            assert only_new.check_code(
                "ada@example.com", totp.code_at(secret, totp.step_now(later)), now=later
            ), "a code still checks"
            assert only_new.confirm_replacement(
                "ada@example.com", totp.code_at(replacement, totp.step_now(later)), now=later
            )
        finally:
            only_new.close()

    def test_a_start_nothing_configured_can_open_is_refused_and_says_to_put_the_old_secret_back_second(
        self, tmp_path
    ):
        path = tmp_path / "auth" / "signin.db"
        store = SignInStore(path, [OLD_SECRET])
        store.put_person("ada@example.com", "operator")
        store.begin_enrolment("ada@example.com")
        store.close()
        before = _column(path, "totp_secret")
        with pytest.raises(
            ConfigurationError, match="put the old one back as the second value"
        ) as refused:
            SignInStore(path, [NEW_SECRET])
        assert "VECTRIXDB_SIGNIN_SECRET=new,old" in str(refused.value)
        assert _column(path, "totp_secret") == before, "a refused start changes nothing"
        SignInStore(path, [NEW_SECRET, OLD_SECRET]).close()
        assert sealing_key(NEW_SECRET).decrypt(_column(path, "totp_secret").encode()), (
            "with the old one back second, it starts"
        )

    @pytest.mark.parametrize(
        "configured",
        [[OLD_SECRET], [NEW_SECRET, OLD_SECRET]],
        ids=["the same secret", "the old secret as the second value"],
    )
    def test_a_secret_sealed_the_way_it_was_before_2_3_is_read_and_sealed_again_the_new_way(
        self, tmp_path, configured
    ):
        path = tmp_path / "auth" / "signin.db"
        secret = totp.new_secret()
        legacy = legacy_key(OLD_SECRET).encrypt(secret.encode()).decode()
        # A file from then: the people table as the first release made it, and nothing else yet.
        path.parent.mkdir(parents=True)
        db = sqlite3.connect(str(path))
        db.execute(
            "CREATE TABLE people (email TEXT PRIMARY KEY, role TEXT NOT NULL, principal TEXT NOT NULL DEFAULT '{}', "
            "totp_secret TEXT, totp_confirmed INTEGER NOT NULL DEFAULT 0, last_step INTEGER, "
            "disabled INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, last_sign_in REAL)"
        )
        db.execute(
            "INSERT INTO people (email, role, totp_secret, totp_confirmed, created_at) VALUES ('ada@example.com', 'operator', ?, 1, 0)",
            (legacy,),
        )
        db.commit()
        db.close()

        SignInStore(path, configured).close()
        resealed = _column(path, "totp_secret")
        assert resealed != legacy
        assert sealing_key(configured[0]).decrypt(resealed.encode()).decode() == secret

        now = 1_800_000_000.0
        store = SignInStore(path, [configured[0]])
        try:
            assert store.person("ada@example.com").enrolled
            assert store.check_code(
                "ada@example.com", totp.code_at(secret, totp.step_now(now)), now=now
            )
        finally:
            store.close()


# ----------------------------------------------------------------- 3 · the door

KEY_ADA = "code:ada@example.com"


@pytest.fixture
def store(tmp_path):
    s = SignInStore(tmp_path / "auth" / "signin.db", [SECRET])
    yield s
    s.close()


def shut(store: SignInStore, key: str, **how) -> None:
    """Four wrong tries that leave the door open, then the fifth, which shuts it."""
    for _ in range(4):
        assert store.failed(key, **how) is False
    assert not store.locked(key)
    assert store.failed(key, **how) is True
    assert store.locked(key)


class TestTheDoorStaysShutLongerEachTime:
    def test_fifteen_minutes_then_an_hour_then_four_hours_then_a_day_and_a_day_after_that(
        self, clock, store
    ):
        for minutes in (15, 60, 240, 1440, 1440):
            shut(store, KEY_ADA)
            assert store.lock_minutes(KEY_ADA) == minutes
            clock.advance(minutes * 60 - 1)
            assert store.locked(KEY_ADA), "still shut a second before the time is up"
            clock.advance(2)
            assert not store.locked(KEY_ADA)

    def test_a_real_sign_in_starts_the_count_again(self, clock, store):
        shut(store, KEY_ADA)
        clock.advance(15 * 60 + 1)
        shut(store, KEY_ADA)
        assert store.lock_minutes(KEY_ADA) == 60
        clock.advance(60 * 60 + 1)
        store.succeeded(KEY_ADA)
        shut(store, KEY_ADA)
        assert store.lock_minutes(KEY_ADA) == 15

    def test_a_day_with_no_lock_starts_it_again_and_a_second_less_does_not(self, clock, store):
        patient, late = "code:patient@example.com", "code:late@example.com"
        shut(store, patient)
        shut(store, late)
        clock.advance(15 * 60 + 24 * 3600 - 1)  # a second short of a day since both locks ran out
        shut(store, patient)
        assert store.lock_minutes(patient) == 60, "a lock that simply ran out is still remembered"
        clock.advance(2)
        shut(store, late)
        assert store.lock_minutes(late) == 15

    def test_a_lock_that_does_not_escalate_is_always_the_first_step(self, clock, store):
        for _ in range(4):
            shut(store, "begin:ada@example.com", escalate=False)
            assert store.lock_minutes("begin:ada@example.com") == 15
            clock.advance(15 * 60 + 1)

    def test_a_key_can_carry_a_limit_of_its_own(self, clock, store):
        key = "from:203.0.113.7"
        for _ in range(49):
            assert store.failed(key, limit=50, escalate=False) is False
        assert not store.locked(key)
        assert store.failed(key, limit=50, escalate=False) is True
        assert store.locked(key) and store.lock_minutes(key) == 15

    def test_the_minutes_left_are_rounded_up_so_the_wait_is_never_understated(self, clock, store):
        shut(store, KEY_ADA)
        clock.advance(61)
        assert store.lock_minutes(KEY_ADA) == 14
        clock.advance(15 * 60 - 61 - 1)
        assert store.lock_minutes(KEY_ADA) == 1


class TestWrongCodesAtTheServer:
    def test_the_fifth_wrong_code_shuts_the_door_the_reply_says_for_how_long_and_the_person_is_told(
        self, clock, server
    ):
        client, config = server
        secret, _ = enrol(client, config, "ada@example.com")
        next_step(clock)
        for tries in range(5):
            assert config.sender.warnings_to("ada@example.com") == [], (
                f"nobody is warned after {tries} wrong codes"
            )
            assert verify(client, "ada@example.com", wrong_code(secret)).status_code == 401
        warnings = config.sender.warnings_to("ada@example.com")
        assert len(warnings) == 1 and "for 15 minutes" in warnings[0]
        held = verify(client, "ada@example.com", current_code(secret))
        assert held.status_code == 429 and held.json()["message"] == HELD_15, (
            "the right code waits too"
        )
        assert len(config.sender.warnings_to("ada@example.com")) == 1, (
            "one warning when it shuts, not one per try"
        )
        locked = client.app.state.signin.access.recent(event="locked")
        assert [(r["who"], r["reason"]) for r in locked] == [("ada@example.com", "code")]

    def test_each_time_it_shuts_it_stays_shut_longer_until_a_real_sign_in(self, clock, server):
        client, config = server
        secret, _ = enrol(client, config, "ada@example.com")
        next_step(clock)
        for minutes in (15, 60, 240, 1440, 1440):
            for _ in range(5):
                assert verify(client, "ada@example.com", wrong_code(secret)).status_code == 401
            held = verify(client, "ada@example.com", current_code(secret))
            assert held.status_code == 429 and f"Wait {minutes} minutes" in held.json()["message"]
            assert f"for {minutes} minutes" in config.sender.warnings_to("ada@example.com")[-1]
            clock.advance(minutes * 60 + 1)
        assert verify(client, "ada@example.com", current_code(secret)).status_code == 200
        for _ in range(5):
            assert verify(client, "ada@example.com", wrong_code(secret)).status_code == 401
        assert verify(client, "ada@example.com", current_code(secret)).json()["message"] == HELD_15
        assert len(config.sender.warnings_to("ada@example.com")) == 6

    def test_an_address_nobody_listed_is_held_the_same_way_and_nobody_is_written_to(
        self, clock, server
    ):
        client, config = server
        for _ in range(5):
            assert verify(client, "mallory@example.com", "000000").status_code == 401
        held = verify(client, "mallory@example.com", "000000")
        assert held.status_code == 429 and held.json()["message"] == HELD_15
        assert config.sender.sent == []

    def test_a_recovery_code_still_opens_the_door_that_wrong_codes_shut(self, clock, server):
        client, config = server
        secret, codes = enrol(client, config, "ada@example.com")
        next_step(clock)
        for _ in range(5):
            assert verify(client, "ada@example.com", wrong_code(secret)).status_code == 401
        assert verify(client, "ada@example.com", current_code(secret)).status_code == 429
        runtime = client.app.state.signin
        assert runtime.store.locked("code:ada@example.com") and not runtime.store.locked(
            "recovery:ada@example.com"
        )
        recovered = verify(another_browser(client), "ada@example.com", codes[0])
        assert recovered.status_code == 200 and recovered.json()["data"]["recovery_codes_left"] == 9

    def test_wrong_recovery_codes_are_counted_on_their_own(self, clock, server):
        client, config = server
        secret, codes = enrol(client, config, "ada@example.com")
        next_step(clock)
        for _ in range(5):
            assert verify(client, "ada@example.com", "AAAA-BBBB-CCCC").status_code == 401
        runtime = client.app.state.signin
        assert runtime.store.locked("recovery:ada@example.com") and not runtime.store.locked(
            "code:ada@example.com"
        )
        assert verify(client, "ada@example.com", codes[0]).status_code == 429, (
            "a right recovery code waits too"
        )
        assert runtime.store.recovery_codes_left("ada@example.com") == 10, (
            "and is not used up by waiting"
        )
        assert len(config.sender.warnings_to("ada@example.com")) == 1
        assert [r["reason"] for r in runtime.access.recent(event="locked")] == ["recovery"]
        assert (
            verify(another_browser(client), "ada@example.com", current_code(secret)).status_code
            == 200
        )

    def test_one_address_trying_many_accounts_is_stopped_at_the_fiftieth_wrong_try(
        self, clock, server
    ):
        client, config = server
        secret, _ = enrol(client, config, "ada@example.com")
        next_step(clock)
        guesser = another_browser(client, "203.0.113.7")
        for n in range(50):
            # Another account every time, and another address claimed in a header every time.
            reply = guesser.post(
                "/auth/email/verify",
                json={"email": f"guess{n}@example.com", "code": "000000"},
                headers={"x-forwarded-for": f"198.51.100.{n}"},
            )
            assert reply.status_code == 401, f"try {n + 1} was held"
        held = verify(guesser, "ada@example.com", current_code(secret))
        assert held.status_code == 429 and held.json()["message"] == HELD_15
        elsewhere = verify(
            another_browser(client, "192.0.2.44"), "ada@example.com", current_code(secret)
        )
        assert elsewhere.status_code == 200, "the address waits, not the person"
        addresses = {
            r["address"] for r in client.app.state.signin.access.recent(event="signin_failed")
        }
        assert addresses == {"203.0.113.7"}, (
            "the address is the connection's, whatever a header claims"
        )


# -------------------------------------------------------------- 4 · passwords


def _unpack(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class TestAPasswordAsStored:
    def test_it_is_scrypt_with_its_cost_salt_and_hash_written_beside_it(self):
        stored = passwords.hash_password(PASSWORD)
        assert re.fullmatch(r"scrypt\$32768\$8\$1\$[\w-]{22}\$[\w-]{43}", stored), stored
        _, n, r, p, salt, digest = stored.split("$")
        assert len(_unpack(salt)) == 16
        again = hashlib.scrypt(
            PASSWORD.encode("utf-8"),
            salt=_unpack(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
            maxmem=64 * 1024 * 1024,
        )
        assert again == _unpack(digest)
        assert passwords.hash_password(PASSWORD) != stored, "a fresh salt every time"

    def test_only_the_password_that_was_stored_checks(self):
        stored = passwords.hash_password(PASSWORD)
        assert passwords.check(PASSWORD, stored)
        for wrong in ("plum orbit ledger 43", PASSWORD.upper(), PASSWORD + " ", "", None):
            assert not passwords.check(wrong, stored), repr(wrong)
        assert not passwords.check("", passwords.hash_password("")), (
            "an empty password opens nothing, even an empty one stored"
        )

    def test_nothing_stored_costs_the_same_work_as_a_wrong_password(self, monkeypatch):
        stored = passwords.hash_password(PASSWORD)
        work = []
        real = hashlib.scrypt

        def counted(password, **how):
            work.append((how["n"], how["r"], how["p"], how["dklen"]))
            return real(password, **how)

        monkeypatch.setattr(passwords.hashlib, "scrypt", counted)
        assert not passwords.check("not the password", stored)
        wrong = list(work)
        assert wrong == [(2**15, 8, 1, 32)]
        for nothing in (None, "", "bcrypt$2b$12$somebody-elses-format"):
            work.clear()
            assert not passwords.check("not the password", nothing)
            assert work == wrong, f"{nothing!r}: the time taken says nothing about what is stored"


class TestWhatANewPasswordMayBe:
    @pytest.mark.parametrize(
        "password, says",
        [
            ("short", "at least 12 characters"),
            ("elevenchars", "at least 12 characters"),
            ("x" * 257, "at most 256 characters"),
            ("Password1234", "first anybody tries"),
            ("P a s s w o r d 1 2 3 4", "first anybody tries"),
            ("correct horse battery staple", "first anybody tries"),
            ("abababababab", "first anybody tries"),
            ("bcdefghijklmnop", "run of letters or numbers"),
            ("3456789012345", "run of letters or numbers"),
            ("Grace-Hopper-1906", "email address"),
        ],
        ids=[
            "five characters",
            "eleven characters",
            "over 256",
            "a common one",
            "a common one spaced out",
            "a famous one",
            "two characters repeated",
            "a run of letters",
            "a run of digits",
            "the name in the address",
        ],
    )
    def test_an_easy_password_is_refused_with_the_reason(self, password, says):
        assert says in (passwords.problem_with(password, "grace@example.com") or "")

    def test_the_name_in_an_address_is_held_only_against_that_address(self):
        assert passwords.problem_with("Grace-Hopper-1906", "someone@example.com") is None

    def test_twelve_ordinary_characters_are_enough(self):
        assert passwords.problem_with("tangerine-42", "grace@example.com") is None
        assert passwords.problem_with(PASSWORD, "grace@example.com") is None


class TestSettingUpWithPasswordsOn:
    def test_the_sign_in_page_is_told_passwords_are_on(self, password_server):
        client, _ = password_server
        assert client.get("/auth/me").json()["data"]["methods"]["email"]["passwords"] is True

    def test_set_up_needs_a_good_password_and_keeps_only_its_hash(self, clock, password_server):
        client, config = password_server
        client.post("/auth/email/begin", json={"email": "grace@example.com"})
        begun = client.post(
            "/auth/email/enrol/begin", json={"token": config.sender.token()}
        ).json()["data"]
        assert begun["passwords"] is True
        code, ticket = current_code(begun["secret"]), begun["ticket"]
        weak = (
            (None, "at least 12 characters"),
            ("short", "at least 12 characters"),
            ("Password1234", "first anybody tries"),
            ("grace-is-my-name", "email address"),
        )
        for password, says in weak:
            body = {
                "ticket": ticket,
                "code": code,
                **({} if password is None else {"password": password}),
            }
            refused = client.post("/auth/email/enrol/confirm", json=body)
            assert refused.status_code == 400 and says in refused.json()["message"], password
            ticket = refused.json()["data"]["ticket"]
        done = client.post(
            "/auth/email/enrol/confirm", json={"ticket": ticket, "code": code, "password": PASSWORD}
        )
        assert done.status_code == 200, (
            "a refused password spent neither the code nor the chance to try again"
        )
        assert client.get("/auth/me/ways").json()["data"]["password"]["set_at"] is not None
        stored = _column(config.store_path, "password_hash", "grace@example.com")
        assert (
            stored.startswith("scrypt$")
            and PASSWORD not in stored
            and passwords.check(PASSWORD, stored)
        )


class TestSigningInWithPasswordsOn:
    def test_it_takes_the_code_and_the_password_and_either_one_wrong_is_the_same_refusal(
        self, clock, password_server
    ):
        client, config = password_server
        secret, _ = enrol(client, config, "ada@example.com", password=PASSWORD)
        door = another_browser(client)
        next_step(clock)
        wrong_password = verify(
            door, "ada@example.com", current_code(secret), "plum orbit ledger 43"
        )
        next_step(clock)
        no_password = verify(door, "ada@example.com", current_code(secret))
        wrong_code_reply = verify(door, "ada@example.com", wrong_code(secret), PASSWORD)
        nobody = verify(door, "nobody@example.com", "123456", PASSWORD)
        replies = [wrong_password, no_password, wrong_code_reply, nobody]
        assert [r.status_code for r in replies] == [401] * 4
        assert all(r.json() == replies[0].json() for r in replies), (
            "nothing says which half was wrong"
        )
        assert replies[0].json()["message"] == REFUSED_BOTH
        assert door.get("/api/v1/collections").status_code == 401
        next_step(clock)
        assert verify(door, "ada@example.com", current_code(secret), PASSWORD).status_code == 200
        assert door.get("/api/v1/collections").status_code == 200

    def test_the_password_is_checked_after_a_wrong_code_and_for_an_address_nobody_listed(
        self, clock, password_server, monkeypatch
    ):
        client, config = password_server
        secret, _ = enrol(client, config, "ada@example.com", password=PASSWORD)
        work = []
        real = hashlib.scrypt
        monkeypatch.setattr(
            passwords.hashlib,
            "scrypt",
            lambda password, **how: work.append(how["n"]) or real(password, **how),
        )
        for email, code in (
            ("ada@example.com", wrong_code(secret)),
            ("nobody@example.com", "123456"),
        ):
            work.clear()
            assert verify(client, email, code, PASSWORD).status_code == 401
            assert work == [2**15], (
                f"{email}: one scrypt, so the time taken says nothing about which half was wrong"
            )

    def test_a_recovery_code_needs_the_password_too(self, clock, password_server):
        client, config = password_server
        _, codes = enrol(client, config, "ada@example.com", password=PASSWORD)
        refused = verify(
            another_browser(client), "ada@example.com", codes[1], "plum orbit ledger 43"
        )
        assert refused.status_code == 401 and refused.json()["message"] == REFUSED_BOTH
        assert (
            verify(another_browser(client), "ada@example.com", codes[0], PASSWORD).status_code
            == 200
        )


class TestAForgottenPassword:
    def test_forgot_sends_a_link_only_to_somebody_with_an_authenticator_and_says_the_same_to_everybody(
        self, clock, password_server
    ):
        client, config = password_server
        enrol(client, config, "ada@example.com", password=PASSWORD)
        before = len(config.sender.sent)
        replies = [
            client.post("/auth/password/forgot", json={"email": email}).json()
            for email in ("ada@example.com", "olu@example.com", "nobody@example.com")
        ]
        assert replies == [{"ok": True, "data": {"note": "reset_link"}}] * 3
        sent = config.sender.sent[before:]
        assert [to for to, _, _ in sent] == ["ada@example.com"], (
            "olu has not set up an authenticator, and nobody is not listed"
        )
        assert "new password" in sent[0][1]
        assert re.search(re.escape(PUBLIC) + r"/dashboard/#/password\?token=[\w-]+", sent[0][2])

    def test_a_reset_needs_the_link_and_a_current_code_and_a_mistyped_code_gets_a_fresh_link(
        self, clock, password_server
    ):
        client, config = password_server
        secret, _ = enrol(client, config, "ada@example.com", password=PASSWORD)
        client.post("/auth/password/forgot", json={"email": "ada@example.com"})
        token = config.sender.token("password")
        browser = another_browser(client)

        def reset(link: str, code: str):
            return browser.post(
                "/auth/password/reset", json={"token": link, "code": code, "password": NEW_PASSWORD}
            )

        next_step(clock)
        assert reset("made-up", current_code(secret)).status_code == 400, "no link, no reset"
        mistyped = reset(token, wrong_code(secret))
        assert mistyped.status_code == 401 and mistyped.json()["message"].startswith(
            "That code was not accepted"
        )
        retry = mistyped.json()["data"]["token"]
        assert retry != token and reset(token, current_code(secret)).status_code == 400, (
            "the first link worked once"
        )
        done = reset(retry, current_code(secret))
        assert done.status_code == 200 and browser.get("/auth/me").status_code == 200
        next_step(clock)
        assert (
            verify(
                another_browser(client), "ada@example.com", current_code(secret), PASSWORD
            ).status_code
            == 401
        ), "the old password is gone"
        next_step(clock)
        assert (
            verify(
                another_browser(client), "ada@example.com", current_code(secret), NEW_PASSWORD
            ).status_code
            == 200
        )

    def test_a_weak_new_password_gets_a_fresh_link_and_spends_neither_the_code_nor_a_try(
        self, clock, password_server
    ):
        client, config = password_server
        secret, _ = enrol(client, config, "ada@example.com", password=PASSWORD)
        client.post("/auth/password/forgot", json={"email": "ada@example.com"})
        next_step(clock)
        weak = client.post(
            "/auth/password/reset",
            json={
                "token": config.sender.token("password"),
                "code": current_code(secret),
                "password": "short",
            },
        )
        assert weak.status_code == 400 and "at least 12 characters" in weak.json()["message"]
        assert client.app.state.signin.access.recent(event="signin_failed") == []
        done = client.post(
            "/auth/password/reset",
            json={
                "token": weak.json()["data"]["token"],
                "code": current_code(secret),
                "password": NEW_PASSWORD,
            },
        )
        assert done.status_code == 200

    def test_somebody_who_set_up_before_passwords_were_on_chooses_one_with_the_link(
        self, clock, data
    ):
        with serve(data) as (client, config):
            secret, _ = enrol(client, config, "ada@example.com")
        with serve(data, passwords=True) as (client, config):
            next_step(clock)
            assert verify(client, "ada@example.com", current_code(secret)).status_code == 401, (
                "a code alone is no longer enough"
            )
            client.post("/auth/password/forgot", json={"email": "ada@example.com"})
            next_step(clock)
            reset = {
                "token": config.sender.token("password"),
                "code": current_code(secret),
                "password": NEW_PASSWORD,
            }
            assert client.post("/auth/password/reset", json=reset).status_code == 200

    def test_with_passwords_off_there_is_nothing_to_reset(self, server):
        client, _ = server
        assert (
            client.post("/auth/password/forgot", json={"email": "ada@example.com"}).status_code
            == 404
        )
        reset = {"token": "made-up", "code": "123456", "password": NEW_PASSWORD}
        assert client.post("/auth/password/reset", json=reset).status_code == 404
