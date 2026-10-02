"""The sign-in store on every database it can keep its state in, and a file from before brought over.

The store's own suites run it on a SQLite file. Here the same store runs on
PostgreSQL's statements and on stand-ins for Cosmos DB and DynamoDB, and two
stores sharing one database stand for two servers behind a load balancer.
Whatever must happen once (a code, a recovery code, a link, a challenge)
happens once between them, and nothing one server changes is lost to a change
the other made at the same moment.

Then a file from before records, with every table it kept: all of it comes
over, the old tables go, the file as it was is kept beside it, and a start
that cannot open its secrets changes nothing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import sys
import time

import pytest

pytest.importorskip("cryptography", reason="the signin extra is not installed")

from cryptography.fernet import Fernet  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from fake_records import FakeContainer, FakePostgres, FakeTable  # noqa: E402

from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.signin import SignInConfig, SignInStore, totp  # noqa: E402
from vectrixdb.signin.keys import derive  # noqa: E402
from vectrixdb.signin.passkeys import PasskeyRefused  # noqa: E402
from vectrixdb.signin.records import CosmosRecords, DynamoRecords, Records, SqlRecords  # noqa: E402

SECRET = "s" * 48
ADA, SAM = "ada@example.com", "sam@example.com"


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class Handle(Records):
    """One server's hold on a database others share: closing it leaves the database open for them."""

    def __init__(self, inner: Records):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def get(self, kind, key):
        return self.inner.get(kind, key)

    def create(self, record):
        return self.inner.create(record)

    def replace(self, record):
        return self.inner.replace(record)

    def put(self, record):
        self.inner.put(record)

    def delete(self, kind, key, version=None):
        return self.inner.delete(kind, key, version)

    def query(self, kind, *, ix1=None, ix2=None):
        return self.inner.query(kind, ix1=ix1, ix2=ix2)

    def purge(self):
        self.inner.purge()

    def describe(self):
        return self.inner.describe()

    def close(self):
        pass


@pytest.fixture(params=["sqlite", "postgresql", "cosmos", "dynamodb"])
def database(request, tmp_path):
    made = {
        "sqlite": lambda: SqlRecords.sqlite(tmp_path / "signin.db"),
        "postgresql": lambda: SqlRecords.postgres(
            "postgresql://vx@db.example.test/signin", connect=FakePostgres().connect
        ),
        "cosmos": lambda: CosmosRecords(FakeContainer()),
        "dynamodb": lambda: DynamoRecords(FakeTable()),
    }[request.param]()
    yield made
    made.close()


@pytest.fixture
def servers(database):
    return SignInStore(Handle(database), [SECRET]), SignInStore(Handle(database), [SECRET])


def open_for(store: SignInStore, email: str, role: str, **extra):
    return store.open_session(
        subject=email,
        email=email,
        name=None,
        role=role,
        principal={},
        method="email",
        hours=1,
        **extra,
    )


class TestOneSignInForSeveralServers:
    def test_somebody_added_on_one_is_there_on_the_other(self, servers):
        one, two = servers
        one.put_person(ADA, "admin")
        assert (
            two.person(ADA).role == "admin"
            and two.admins() == 1
            and [p.email for p in two.people()] == [ADA]
        )

    def test_a_code_is_spent_once_between_them(self, servers):
        one, two = servers
        one.put_person(ADA, "operator")
        secret = one.begin_enrolment(ADA)
        now = 1_800_000_000.0
        assert two.check_code(
            ADA, totp.code_at(secret, totp.step_now(now)), confirming=True, now=now
        )
        later = now + totp.PERIOD
        code = totp.code_at(secret, totp.step_now(later))
        assert one.check_code(ADA, code, now=later)
        assert not two.check_code(ADA, code, now=later), "the other server knows it was used"

    def test_a_recovery_code_a_link_and_a_challenge_each_answer_once(self, servers):
        one, two = servers
        one.put_person(ADA, "operator")
        codes = one.new_recovery_codes(ADA)
        assert two.use_recovery_code(ADA, codes[0]) and not one.use_recovery_code(ADA, codes[0])
        assert one.recovery_codes_left(ADA) == 9
        token = one.new_link(ADA)
        assert two.peek_link(token, "enrol") == ADA
        assert (
            two.use_link(token) == ADA
            and one.use_link(token) is None
            and one.peek_link(token, "enrol") is None
        )
        challenge = one.new_challenge("passkey-add", ADA)
        assert (
            two.use_challenge(challenge, "passkey-add") == ADA
            and one.use_challenge(challenge, "passkey-add") is None
        )

    def test_a_session_opened_on_one_is_good_on_the_other_until_either_ends_it(self, servers):
        one, two = servers
        one.put_person(ADA, "operator")
        sid, _ = open_for(one, ADA, "operator", user_agent="Firefox")
        assert two.session(sid).role == "operator"
        assert [s["user_agent"] for s in two.sessions_of(ADA)] == ["Firefox"]
        assert two.person(ADA).last_sign_in is not None
        two.put_person(ADA, "viewer")
        assert one.session(sid) is None, "a change of role on one server ends it on both"
        sid, _ = open_for(one, ADA, "viewer")
        assert two.close_sessions_of(ADA) == 1 and one.session(sid) is None

    def test_a_session_whose_person_changed_elsewhere_is_not_honoured(self, servers, database):
        # Ending their sessions can miss one another server opened that same moment, so a session asks too.
        one, two = servers
        one.put_person(ADA, "operator")
        sid, _ = open_for(one, ADA, "operator")
        person = database.get("person", ADA)
        person.data["disabled"] = True
        assert database.replace(person)
        assert two.session(sid) is None

    def test_the_door_count_is_shared(self, servers):
        one, two = servers
        for n in range(4):
            (one if n % 2 else two).failed("code:ada")
        assert not one.locked("code:ada")
        assert (
            two.failed("code:ada") and one.locked("code:ada") and one.lock_minutes("code:ada") == 15
        )
        one.succeeded("code:ada")
        assert not two.locked("code:ada")

    def test_a_rate_limit_is_one_count_across_servers(self, servers):
        one, two = servers
        answers = [(one if n % 2 else two).within_rate("guest:203.0.113.9", 3)[0] for n in range(5)]
        assert answers == [True, True, True, False, False]
        assert two.within_rate("guest:198.51.100.4", 3)[0] is True, (
            "another caller has an allowance of their own"
        )

    def test_keys_are_the_same_everywhere(self, servers):
        one, two = servers
        made, key = one.create_key("nightly-ingest", "searcher", ADA)
        assert two.api_key(key).name == "nightly-ingest" and [k.key_id for k in two.keys()] == [
            made.key_id
        ]
        assert two.revoke_key(made.key_id) == "nightly-ingest"
        assert one.api_key(key) is None and one.revoke_key(made.key_id) is None and one.keys() == []


class TestPasskeysOnEveryDatabase:
    def test_one_added_is_listed_found_and_removed(self, servers):
        one, two = servers
        one.put_person(ADA, "operator")
        added = one.add_passkey(ADA, b"cred-1", b"public-key", -7, 0, ["internal"], "Laptop")
        assert [k["name"] for k in two.passkeys(ADA)] == ["Laptop"] and two.person(ADA).enrolled
        assert two.passkey(added["id"])["email"] == ADA and two.passkey_ids(ADA) == [b"cred-1"]
        two.touch_passkey(added["id"], 7)
        assert (
            one.passkey(added["id"])["sign_count"] == 7
            and one.passkeys(ADA)[0]["last_used"] is not None
        )
        assert (
            one.remove_passkey(ADA, added["id"])
            and two.passkey(added["id"]) is None
            and not two.person(ADA).enrolled
        )

    def test_one_already_set_up_is_refused_plainly(self, servers):
        one, two = servers
        one.put_person(ADA, "operator")
        one.put_person(SAM, "operator")
        one.add_passkey(ADA, b"cred-1", b"public-key", -7, 0, [], "Laptop")
        with pytest.raises(PasskeyRefused, match="already set up"):
            two.add_passkey(SAM, b"cred-1", b"another-key", -7, 0, [], "Theirs")
        assert two.passkey(b64u(b"cred-1"))["email"] == ADA and two.passkeys(SAM) == []

    def test_one_for_somebody_not_on_the_list_is_not_kept(self, servers):
        one, _ = servers
        with pytest.raises(PasskeyRefused, match="Nobody"):
            one.add_passkey("ghost@example.com", b"cred-9", b"public-key", -7, 0, [], "Laptop")
        assert one.passkey(b64u(b"cred-9")) is None

    def test_a_passkey_its_owner_no_longer_lists_does_not_open_the_door(self, servers, database):
        one, _ = servers
        one.put_person(ADA, "operator")
        added = one.add_passkey(ADA, b"cred-1", b"public-key", -7, 0, [], "Laptop")
        person = database.get("person", ADA)
        person.data["passkeys"] = []
        assert database.replace(person)
        assert one.passkey(added["id"]) is None

    def test_removing_somebody_takes_everything_of_theirs(self, servers, database):
        one, two = servers
        one.put_person(ADA, "operator")
        one.add_passkey(ADA, b"cred-1", b"public-key", -7, 0, [], "Laptop")
        open_for(one, ADA, "operator")
        one.new_link(ADA)
        assert two.remove_person(ADA)
        assert [database.query(kind, ix1=ADA) for kind in ("session", "link", "passkey")] == [
            [],
            [],
            [],
        ]
        assert database.get("passkey", b64u(b"cred-1")) is None and not two.remove_person(ADA)


class TestTheSecretOnEveryDatabase:
    def test_a_rotation_seals_again_under_the_new_secret_and_a_refused_start_changes_nothing(
        self, database
    ):
        old, new = "o" * 48, "n" * 48
        first = SignInStore(Handle(database), [old])
        first.put_person(ADA, "operator")
        secret = first.begin_enrolment(ADA)
        before = database.get("person", ADA).data["totp_secret"]
        with pytest.raises(ConfigurationError, match="put the old one back as the second value"):
            SignInStore(Handle(database), [new])
        assert database.get("person", ADA).data["totp_secret"] == before
        SignInStore(Handle(database), [new, old])
        assert database.get("person", ADA).data["totp_secret"] != before
        assert SignInStore(Handle(database), [new]).pending_secret(ADA) == secret, (
            "the old secret can go"
        )


class TestTwoChangesAtOnce:
    def test_neither_is_lost(self, database):
        raced: list = []

        class Racing(Handle):
            def replace(self, record):
                # Another server writes the same person between this one's read and its write.
                if record.kind == "person" and not raced:
                    raced.append(True)
                    theirs = database.get("person", record.key)
                    theirs.data["grants"] = ["document.read"]
                    assert database.replace(theirs)
                return self.inner.replace(record)

        store = SignInStore(Racing(database), [SECRET])
        store.put_person(ADA, "operator")
        store.set_disabled(ADA, True)
        data = database.get("person", ADA).data
        assert raced and data["disabled"] is True and data["grants"] == ["document.read"]


# ------------------------------------------------------ a file from before ---

OLD_TABLES = """
CREATE TABLE people (email TEXT PRIMARY KEY, role TEXT NOT NULL, principal TEXT NOT NULL DEFAULT '{}',
    totp_secret TEXT, totp_confirmed INTEGER NOT NULL DEFAULT 0, last_step INTEGER, disabled INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, last_sign_in REAL, grants TEXT NOT NULL DEFAULT '[]', totp_pending TEXT, totp_set_at REAL,
    totp_used_at REAL, password_hash TEXT, password_set_at REAL, user_handle TEXT);
CREATE TABLE sessions (sid_hash TEXT PRIMARY KEY, subject TEXT NOT NULL, email TEXT, name TEXT, role TEXT NOT NULL,
    principal TEXT NOT NULL, method TEXT NOT NULL, csrf TEXT NOT NULL, created_at REAL NOT NULL, last_seen REAL NOT NULL,
    expires_at REAL NOT NULL, grants TEXT NOT NULL DEFAULT '[]', verified_at REAL NOT NULL DEFAULT 0, user_agent TEXT, address TEXT);
CREATE TABLE links (token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, purpose TEXT NOT NULL, expires_at REAL NOT NULL, used_at REAL);
CREATE TABLE recovery (email TEXT NOT NULL, code_hash TEXT NOT NULL, used_at REAL, PRIMARY KEY (email, code_hash));
CREATE TABLE attempts (key TEXT PRIMARY KEY, count INTEGER NOT NULL, first_at REAL NOT NULL, locked_until REAL, level INTEGER NOT NULL DEFAULT 0);
CREATE TABLE passkeys (credential_id TEXT PRIMARY KEY, email TEXT NOT NULL, public_key TEXT NOT NULL, alg INTEGER NOT NULL,
    sign_count INTEGER NOT NULL DEFAULT 0, transports TEXT NOT NULL DEFAULT '[]', name TEXT NOT NULL, created_at REAL NOT NULL, last_used REAL);
CREATE TABLE challenges (challenge_hash TEXT PRIMARY KEY, purpose TEXT NOT NULL, email TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE api_keys (key_id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL, created_by TEXT, created_at REAL NOT NULL, last_used REAL, revoked_at REAL);
CREATE TABLE visibility (collection TEXT PRIMARY KEY, created TEXT NOT NULL, audience TEXT NOT NULL, guest_text TEXT NOT NULL,
    changed_by TEXT, changed_at REAL NOT NULL);
"""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def old_file(path) -> dict:
    """A sign-in file as the release before records left it, one of everything in it."""
    now = time.time()
    facts = {
        "secret": totp.new_secret(),
        "sid": "a-session-id",
        "token": "a-link-token",
        "codes": totp.new_recovery_codes(),
        "key": "vx_0a1b2c3d_" + "k" * 43,
        "handle": b"sixteen-bytes-id",
        "challenge": b"c" * 32,
        "created": "2026-09-18T10:00:00",
    }
    sealed = (
        Fernet(base64.urlsafe_b64encode(derive(SECRET, "sealing")))
        .encrypt(facts["secret"].encode())
        .decode()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.executescript(OLD_TABLES)
    db.execute(
        "INSERT INTO people (email, role, grants, totp_secret, totp_confirmed, created_at, totp_set_at, user_handle) VALUES (?, 'admin', ?, ?, 1, 1.0, 2.0, ?)",
        (ADA, json.dumps(["document.read"]), sealed, b64u(facts["handle"])),
    )
    db.execute(
        "INSERT INTO people (email, role, disabled, created_at) VALUES (?, 'viewer', 1, 1.0)",
        (SAM,),
    )
    db.execute(
        "INSERT INTO sessions (sid_hash, subject, email, role, principal, method, csrf, created_at, last_seen, expires_at, grants, verified_at) "
        "VALUES (?, ?, ?, 'admin', '{}', 'email', 'csrf-token', ?, ?, ?, ?, ?)",
        (sha(facts["sid"]), ADA, ADA, now, now, now + 3600, json.dumps(["document.read"]), now),
    )
    db.execute(
        "INSERT INTO sessions (sid_hash, subject, email, role, principal, method, csrf, created_at, last_seen, expires_at) "
        "VALUES ('long-gone', ?, ?, 'admin', '{}', 'email', 'x', 1.0, 1.0, 2.0)",
        (ADA, ADA),
    )
    db.execute(
        "INSERT INTO links (token_hash, email, purpose, expires_at) VALUES (?, ?, 'enrol', ?)",
        (sha(facts["token"]), ADA, now + 600),
    )
    for n, code in enumerate(facts["codes"]):
        db.execute(
            "INSERT INTO recovery (email, code_hash, used_at) VALUES (?, ?, ?)",
            (ADA, sha(ADA + "|" + totp.normalise_recovery(code)), 5.0 if n == 0 else None),
        )
    db.execute(
        "INSERT INTO attempts (key, count, first_at, locked_until, level) VALUES ('code:sam@example.com', 0, ?, ?, 1)",
        (now, now + 600),
    )
    db.execute(
        "INSERT INTO passkeys (credential_id, email, public_key, alg, sign_count, transports, name, created_at) VALUES (?, ?, ?, -7, 3, '[\"usb\"]', 'Key', 3.0)",
        (b64u(b"cred-1"), ADA, base64.b64encode(b"public-key").decode()),
    )
    db.execute(
        "INSERT INTO challenges (challenge_hash, purpose, email, expires_at) VALUES (?, 'passkey-add', ?, ?)",
        (hashlib.sha256(facts["challenge"]).hexdigest(), ADA, now + 120),
    )
    db.execute(
        "INSERT INTO api_keys (key_id, name, role, key_hash, prefix, created_by, created_at) VALUES ('0a1b2c3d', 'nightly', 'searcher', ?, ?, ?, 4.0)",
        (sha(facts["key"]), facts["key"][:11], ADA),
    )
    db.execute(
        "INSERT INTO visibility (collection, created, audience, guest_text, changed_at) VALUES ('docs', ?, 'guests', 'excerpts', 6.0)",
        (facts["created"],),
    )
    db.commit()
    db.close()
    return facts


def tables_in(path) -> set:
    db = sqlite3.connect(str(path))
    try:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        db.close()


class TestAFileFromBefore:
    def test_everything_comes_over_and_the_old_tables_go(self, tmp_path):
        path = tmp_path / "auth" / "signin.db"
        facts = old_file(path)
        store = SignInStore(path, [SECRET])
        try:
            ada = store.person(ADA)
            assert (ada.role, ada.grants, ada.authenticator, ada.passkeys, ada.enrolled) == (
                "admin",
                ["document.read"],
                True,
                1,
                True,
            )
            assert store.person(SAM).disabled and store.admins() == 1
            session = store.session(facts["sid"])
            assert (
                session.email == ADA
                and session.grants == ["document.read"]
                and session.csrf == "csrf-token"
            ), "nobody is signed out"
            assert [s["id"] for s in store.sessions_of(ADA)] == [sha(facts["sid"])[:16]], (
                "what had run out stays behind"
            )
            assert store.recovery_codes_left(ADA) == 9 and not store.use_recovery_code(
                ADA, facts["codes"][0]
            )
            assert store.use_recovery_code(ADA, facts["codes"][1])
            assert store.peek_link(facts["token"], "enrol") == ADA
            assert store.passkey(b64u(b"cred-1")) == {
                "email": ADA,
                "public_key": b"public-key",
                "sign_count": 3,
                "name": "Key",
            }
            assert store.use_challenge(facts["challenge"], "passkey-add") == ADA
            assert store.api_key(facts["key"]).name == "nightly"
            assert store.locked("code:sam@example.com")
            assert store.user_handle(ADA) == facts["handle"]
            now = time.time()
            assert store.check_code(
                ADA, totp.code_at(facts["secret"], totp.step_now(now)), now=now
            ), "the authenticator still works"
        finally:
            store.close()
        assert tables_in(path) == {"records"}
        kept = path.with_name("signin.v1.db")
        assert "people" in tables_in(kept) and "records" not in tables_in(kept), (
            "the file as it was, kept beside it"
        )
        SignInStore(path, [SECRET]).close()
        assert tables_in(path) == {"records"}, "brought over once"

    def test_a_start_that_cannot_open_its_secrets_changes_nothing(self, tmp_path):
        path = tmp_path / "auth" / "signin.db"
        old_file(path)
        with pytest.raises(ConfigurationError, match="put the old one back as the second value"):
            SignInStore(path, ["n" * 48])
        assert _OLD <= tables_in(path)
        db = sqlite3.connect(str(path))
        try:
            assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 2
        finally:
            db.close()
        assert not path.with_name("signin.v1.db").exists()
        store = SignInStore(path, ["n" * 48, SECRET])
        try:
            assert store.person(ADA).authenticator, "with the old secret back second, it comes over"
        finally:
            store.close()


_OLD = {
    "people",
    "sessions",
    "links",
    "recovery",
    "attempts",
    "passkeys",
    "challenges",
    "api_keys",
    "visibility",
}


# ------------------------------------------------------------ the setting ---

BASE = {
    "VECTRIXDB_SIGNIN": "email",
    "VECTRIXDB_SIGNIN_SECRET": SECRET,
    "VECTRIXDB_PUBLIC_URL": "https://vx.example.test",
}


class TestSayingWhere:
    def test_the_auth_folder_holds_the_file_and_the_access_log_unless_told_otherwise(
        self, tmp_path
    ):
        config = SignInConfig.from_env(tmp_path / "db", BASE)
        assert (
            config.store_path == tmp_path / "db" / "auth" / "signin.db" and config.store_url is None
        )
        moved = SignInConfig.from_env(
            tmp_path / "db", {**BASE, "VECTRIXDB_AUTH_PATH": str(tmp_path / "state")}
        )
        assert (
            moved.store_path == tmp_path / "state" / "signin.db"
            and moved.access_log == tmp_path / "state" / "access.jsonl"
        )

    def test_the_store_and_its_key_can_be_set_or_read_from_files_and_are_never_shown(
        self, tmp_path
    ):
        assert (
            SignInConfig.from_env(tmp_path, {**BASE, "VECTRIXDB_SIGNIN_STORE": "sqlite"}).store_url
            is None
        )
        address = "postgresql://vx@db.example.test/signin"
        assert (
            SignInConfig.from_env(tmp_path, {**BASE, "VECTRIXDB_SIGNIN_STORE": address}).store_url
            == address
        )
        (tmp_path / "store").write_text(address + "\n")
        (tmp_path / "key").write_text("a-password\n")
        config = SignInConfig.from_env(
            tmp_path,
            {
                **BASE,
                "VECTRIXDB_SIGNIN_STORE_FILE": str(tmp_path / "store"),
                "VECTRIXDB_SIGNIN_STORE_KEY_FILE": str(tmp_path / "key"),
            },
        )
        assert (config.store_url, config.store_key) == (address, "a-password")
        assert address not in repr(config) and "a-password" not in repr(config)

    def test_the_server_and_the_command_line_keep_sign_in_where_they_are_told(
        self, tmp_path, monkeypatch
    ):
        pytest.importorskip("fastapi", reason="the API extra is not installed")
        from vectrixdb.api.signin import SignInRuntime
        from vectrixdb.cli import _signin_store

        target = tmp_path / "elsewhere" / "state.db"
        env = {**BASE, "VECTRIXDB_SIGNIN_STORE": f"sqlite:///{target.as_posix()}"}
        runtime = SignInRuntime(SignInConfig.from_env(tmp_path / "db", env))
        try:
            assert runtime.store.where == f"SQLite file {target}" and target.exists()
            runtime.store.put_person(ADA, "admin")
        finally:
            runtime.close()
        assert not (tmp_path / "db" / "auth" / "signin.db").exists()
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        _, store = _signin_store(str(tmp_path / "db"))
        try:
            assert store.person(ADA).role == "admin", (
                "vectrixdb people works on the same store as the server"
            )
        finally:
            store.close()
