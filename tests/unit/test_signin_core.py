"""The parts of sign-in that need no server: codes, the store, roles and configuration."""

from __future__ import annotations

import sqlite3
import time

import pytest

from vectrixdb.exceptions import ConfigurationError
from vectrixdb.signin import SignInConfig, roles, totp
from vectrixdb.signin.access import AccessLog
from vectrixdb.signin.store import MAX_ATTEMPTS, SignInStore

pytest.importorskip("cryptography", reason="the signin extra is not installed")

SECRET = "x" * 40
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890", the RFC 6238 test key


class TestCodes:
    @pytest.mark.parametrize("at, code", [(59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")])
    def test_the_rfc_6238_vectors(self, at, code):
        assert totp.code_at(RFC_SECRET, at // 30) == code

    def test_a_code_is_good_for_the_step_either_side_and_no_further(self):
        now = 1_700_000_000
        for drift, good in ((-60, False), (-30, True), (0, True), (30, True), (60, False)):
            code = totp.code_at(RFC_SECRET, (now + drift) // 30)
            assert (totp.verify(RFC_SECRET, code, now=now) is not None) is good, drift

    def test_a_code_works_once(self):
        now = 1_700_000_000
        code = totp.code_at(RFC_SECRET, now // 30)
        step = totp.verify(RFC_SECRET, code, now=now)
        assert step is not None
        assert totp.verify(RFC_SECRET, code, last_step=step, now=now) is None, "read over a shoulder, it is already spent"

    @pytest.mark.parametrize("junk", ["", "12345", "1234567", "abcdef", None])
    def test_what_is_not_six_digits_is_not_a_code(self, junk):
        assert totp.verify(RFC_SECRET, junk) is None

    def test_the_address_an_authenticator_app_reads(self):
        uri = totp.provisioning_uri("ABC", "ada@example.com")
        assert uri.startswith("otpauth://totp/VectrixDB%3Aada%40example.com?") and "secret=ABC" in uri and "issuer=VectrixDB" in uri

    def test_recovery_codes(self):
        codes = totp.new_recovery_codes()
        assert len(codes) == 10 == len(set(codes))
        assert all(len(c) == 14 and not set("01OI") & set(c) for c in codes)
        assert totp.normalise_recovery(" abcd-efgh-jklm ") == "ABCDEFGHJKLM"


@pytest.fixture
def store(tmp_path):
    s = SignInStore(tmp_path / "auth" / "signin.db", [SECRET])
    yield s
    s.close()


class TestTheStore:
    def test_a_short_secret_is_refused(self, tmp_path):
        with pytest.raises(ConfigurationError, match="32 characters"):
            SignInStore(tmp_path / "s.db", ["short"])

    def test_nothing_that_opens_a_door_is_kept_as_itself(self, store, tmp_path):
        store.put_person("Ada@Example.com", "admin")
        secret = store.begin_enrolment("ada@example.com")
        token = store.new_link("ada@example.com")
        codes = store.new_recovery_codes("ada@example.com")
        sid, _ = store.open_session(subject="ada", email="ada@example.com", name=None, role="admin", principal={}, method="email", hours=1)
        raw = sqlite3.connect(str(tmp_path / "auth" / "signin.db"))
        dump = "\n".join(str(row) for row in raw.execute("SELECT * FROM records"))
        raw.close()
        for bearer in (secret, token, sid, *codes, *(totp.normalise_recovery(c) for c in codes)):
            assert bearer not in dump

    def test_a_signature_is_checked_and_two_secrets_is_a_rotation(self, tmp_path):
        old = SignInStore(":memory:", ["a" * 40])
        signed = old.sign("value")
        assert old.unsign(signed) == "value"
        assert old.unsign(signed[:-2] + "zz") is None and old.unsign("value") is None and old.unsign(None) is None
        rotating = SignInStore(":memory:", ["b" * 40, "a" * 40])
        assert rotating.unsign(signed) == "value", "the old secret still verifies"
        assert SignInStore(":memory:", ["b" * 40]).unsign(signed) is None, "until it is taken away"

    def test_enrolment_then_sign_in(self, store):
        store.put_person("ada@example.com", "operator")
        secret = store.begin_enrolment("ada@example.com")
        assert store.pending_secret("ada@example.com") == secret
        now = time.time()
        assert not store.check_code("ada@example.com", totp.code_at(secret, totp.step_now(now)), now=now), "not confirmed yet, so it cannot sign in"
        assert store.check_code("ada@example.com", totp.code_at(secret, totp.step_now(now)), confirming=True, now=now)
        assert store.person("ada@example.com").enrolled and store.pending_secret("ada@example.com") is None
        assert not store.check_code("ada@example.com", totp.code_at(secret, totp.step_now(now)), now=now), "the confirming code is spent"
        later = now + 30
        assert store.check_code("ada@example.com", totp.code_at(secret, totp.step_now(later)), now=later)

    def test_a_link_works_once_and_not_late(self, store):
        store.put_person("ada@example.com", "viewer")
        token = store.new_link("ada@example.com")
        assert store.use_link(token, "confirm") is None, "a link is for one purpose"
        assert store.use_link(token) == "ada@example.com"
        assert store.use_link(token) is None
        late = store.new_link("ada@example.com", minutes=-1)
        assert store.use_link(late) is None

    def test_a_recovery_code_works_once_and_only_for_its_owner(self, store):
        store.put_person("ada@example.com", "viewer")
        store.put_person("sam@example.com", "viewer")
        code = store.new_recovery_codes("ada@example.com")[0]
        assert not store.use_recovery_code("sam@example.com", code)
        assert store.use_recovery_code("ada@example.com", code.lower().replace("-", " "))
        assert not store.use_recovery_code("ada@example.com", code)
        assert store.recovery_codes_left("ada@example.com") == 9

    def test_five_wrong_tries_shut_the_door(self, store):
        for _ in range(MAX_ATTEMPTS - 1):
            store.failed("code:ada@example.com")
        assert not store.locked("code:ada@example.com")
        store.failed("code:ada@example.com")
        assert store.locked("code:ada@example.com")
        store.succeeded("code:ada@example.com")
        assert not store.locked("code:ada@example.com")

    def test_a_session_ends_by_the_clock_by_idleness_and_by_sign_out(self, store):
        sid, session = store.open_session(subject="s", email=None, name="S", role="viewer", principal={"clients": ["a"]}, method="oidc", hours=1)
        found = store.session(sid)
        assert found.role == "viewer" and found.principal == {"clients": ["a"]} and found.csrf == session.csrf
        assert store.session("not-a-session") is None and store.session(None) is None
        assert store.session(sid, idle_minutes=-1) is None, "idle too long, and gone for good"
        assert store.session(sid) is None
        expired, _ = store.open_session(subject="s", email=None, name=None, role="viewer", principal={}, method="oidc", hours=-1)
        assert store.session(expired) is None
        sid2, _ = store.open_session(subject="s", email=None, name=None, role="viewer", principal={}, method="oidc", hours=1)
        store.close_session(sid2)
        assert store.session(sid2) is None

    def test_a_role_nobody_defined_cannot_be_given(self, store):
        with pytest.raises(ConfigurationError):
            store.put_person("ada@example.com", "root")
        with pytest.raises(ConfigurationError):
            store.put_person("not an address", "admin")
        with pytest.raises(ConfigurationError):
            store.open_session(subject="s", email=None, name=None, role="root", principal={}, method="oidc", hours=1)

    def test_changing_a_role_resetting_and_removing_all_end_sessions(self, store):
        store.put_person("ada@example.com", "operator")
        sid, _ = store.open_session(subject="ada@example.com", email="ada@example.com", name=None, role="operator", principal={}, method="email", hours=1)
        store.put_person("ada@example.com", "viewer")
        assert store.session(sid) is None, "a session carries the role it was opened with"
        sid, _ = store.open_session(subject="ada@example.com", email="ada@example.com", name=None, role="viewer", principal={}, method="email", hours=1)
        store.reset_authenticator("ada@example.com")
        assert store.session(sid) is None and not store.person("ada@example.com").enrolled
        sid, _ = store.open_session(subject="ada@example.com", email="ada@example.com", name=None, role="viewer", principal={}, method="email", hours=1)
        assert store.remove_person("ada@example.com") and store.session(sid) is None and store.person("ada@example.com") is None

    def test_seeding_adds_whoever_is_missing_and_changes_nobody(self, store):
        store.put_person("ada@example.com", "viewer")
        assert store.seed([("ada@example.com", "admin"), ("sam@example.com", "operator")]) == 1
        assert store.person("ada@example.com").role == "viewer" and store.person("sam@example.com").role == "operator"


class TestRoles:
    @pytest.mark.parametrize(
        "method, path, action",
        [
            ("GET", "/api/v1/collections", "meta.read"),
            ("GET", "/api/v1/collections/docs", "meta.read"),
            ("GET", "/api/v1/collections/docs/health", "meta.read"),
            ("GET", "/api/v1/collections/docs/points", "content.index"),
            ("GET", "/api/v1/collections/docs/points/abc", "content.read"),
            ("GET", "/api/v1/collections/docs/provenance/abc", "content.index"),
            ("GET", "/api/v1/collections/docs/documents/a/b.pdf", "document.read"),
            ("GET", "/api/v1/collections/docs/documents", "content.read"),
            ("POST", "/api/v1/collections/docs/text-search", "search"),
            ("POST", "/api/collections/docs/search", "search"),
            ("POST", "/api/v1/collections/docs/search/rerank", "search"),
            ("POST", "/api/v1/collections/docs/points", "content.write"),
            ("POST", "/api/collections/docs/text-upsert", "content.write"),
            ("DELETE", "/api/v1/collections/docs/points", "content.write"),
            ("POST", "/api/v1/collections/docs/rebuild", "collection.maintain"),
            ("POST", "/api/v2/collections", "collection.create"),
            ("DELETE", "/api/v1/collections/docs", "collection.delete"),
            ("GET", "/api/v1/audit", "audit.read"),
            ("GET", "/api/v1/access", "access.read"),
            ("POST", "/auth/people", "people.manage"),
            ("DELETE", "/auth/people/ada@example.com", "people.manage"),
            ("DELETE", "/api/v1/cache", "cache.clear"),
            ("GET", "/api/v1/something/new", None),
            ("PATCH", "/api/v1/collections/docs", None),
        ],
    )
    def test_a_request_names_an_action(self, method, path, action):
        assert roles.action_for(method, path) == action

    def test_the_table(self):
        assert roles.can("viewer", "meta.read") and roles.can("viewer", "content.index")
        assert not roles.can("viewer", "content.read") and not roles.can("viewer", "search")
        assert roles.can("operator", "search") and roles.can("operator", "content.write")
        assert not roles.can("operator", "collection.delete") and not roles.can("operator", "audit.read") and not roles.can("operator", "people.manage")
        assert all(roles.can("admin", action) for action in roles.ACTIONS)
        assert roles.can("reader", "content.read") and not roles.can("reader", "search") and not roles.can("reader", "content.write")

    def test_denied_by_default_twice(self):
        assert not roles.can("operator", None), "a route nobody placed is closed to everyone but an admin"
        assert roles.can("admin", None)
        for role in ("root", "", None, "Admin"):
            assert not roles.can(role, "meta.read"), "a role nobody defined holds nothing"

    def test_every_action_in_the_table_is_described_and_every_grant_is_an_action(self):
        granted = set().union(*roles.GRANTS.values())
        assert granted == set(roles.ACTIONS)
        routed = {action for _, _, action in roles._ROUTES}
        assert routed <= set(roles.ACTIONS)

    def test_every_route_the_server_has_is_placed(self):
        pytest.importorskip("fastapi")
        from vectrixdb.api.server import create_app

        def every(routes):
            # A newer FastAPI keeps an included router whole instead of copying its routes out,
            # and this test then saw none of them and passed for want of anything to check.
            for route in routes:
                inner = getattr(route, "original_router", None)
                yield from (every(inner.routes) if inner is not None else [route])

        routes = list(every(create_app(enable_dashboard=False).routes))
        assert len(routes) > 60, "the walk found the routes"
        # Emergency sign-in is a way in, like the others, and answers 404 while it is off.
        open_to_all = {
            "/", "/health", "/auth/status", "/auth/me", "/auth/signout", "/ws", "/brand.json", "/brand.css", "/brand/logo", "/brand/logo-dark",
            "/auth/break-glass", "/auth/developer",
        }
        unplaced = []
        for route in routes:
            path = getattr(route, "path", "")
            if path in open_to_all or path.startswith(("/auth/oidc/", "/auth/email/", "/auth/passkey/", "/auth/password/", "/docs", "/redoc", "/openapi")):
                continue
            concrete = path.replace("{name}", "docs").replace("{point_id}", "p").replace("{doc_id:path}", "d").replace("{doc_id}", "d").replace("{email}", "a@b.c")
            for method in getattr(route, "methods", None) or ():
                if method not in ("HEAD", "OPTIONS") and roles.action_for(method, concrete) is None:
                    unplaced.append(f"{method} {path}")
        assert not unplaced, f"only an admin can call these until somebody decides who they are for: {unplaced}"


class TestConfiguration:
    BASE = {"VECTRIXDB_SIGNIN": "email", "VECTRIXDB_SIGNIN_SECRET": SECRET, "VECTRIXDB_PUBLIC_URL": "https://vectors.example.com/"}

    def test_off_unless_asked_for(self, tmp_path):
        assert not SignInConfig.from_env(tmp_path, {}).enabled

    def test_email(self, tmp_path):
        config = SignInConfig.from_env(tmp_path, {**self.BASE, "VECTRIXDB_SIGNIN_USERS": "ada@example.com:admin, sam@example.com:viewer"})
        assert config.enabled and config.users == (("ada@example.com", "admin"), ("sam@example.com", "viewer"))
        assert config.public_url == "https://vectors.example.com" and config.secure_cookies
        assert config.store_path == tmp_path / "auth" / "signin.db"

    @pytest.mark.parametrize(
        "change, says",
        [
            ({"VECTRIXDB_SIGNIN": "password"}, "ways to sign in"),
            ({"VECTRIXDB_SIGNIN_SECRET": ""}, "VECTRIXDB_SIGNIN_SECRET"),
            ({"VECTRIXDB_PUBLIC_URL": ""}, "VECTRIXDB_PUBLIC_URL"),
            ({"VECTRIXDB_PUBLIC_URL": "http://vectors.example.com"}, "https"),
            ({"VECTRIXDB_SIGNIN_USERS": "ada@example.com:root"}, "address:role"),
            ({"VECTRIXDB_SIGNIN": "oidc", "VECTRIXDB_OIDC_CLIENT_ID": "c"}, "VECTRIXDB_OIDC_ISSUER"),
            ({"VECTRIXDB_SIGNIN": "oidc", "VECTRIXDB_OIDC_ISSUER": "https://idp", "VECTRIXDB_OIDC_CLIENT_ID": "c"}, "nobody who signs in has a role"),
            ({"VECTRIXDB_SIGNIN": "oidc", "VECTRIXDB_OIDC_ISSUER": "https://idp", "VECTRIXDB_OIDC_CLIENT_ID": "c", "VECTRIXDB_OIDC_ROLE_MAP": '{"g": "root"}'}, "not a role"),
            ({"VECTRIXDB_SIGNIN": "oidc", "VECTRIXDB_OIDC_ISSUER": "https://idp", "VECTRIXDB_OIDC_CLIENT_ID": "c", "VECTRIXDB_OIDC_ROLE_MAP": "not json"}, "must be JSON"),
        ],
    )
    def test_a_mistake_is_said_at_start_up(self, tmp_path, change, says):
        with pytest.raises(ConfigurationError, match=says):
            SignInConfig.from_env(tmp_path, {**self.BASE, **change})

    def test_a_laptop_may_use_http(self, tmp_path):
        config = SignInConfig.from_env(tmp_path, {**self.BASE, "VECTRIXDB_PUBLIC_URL": "http://localhost:7337"})
        assert not config.secure_cookies


class TestTheAccessLog:
    def test_a_line_per_event_newest_first_and_nothing_it_should_not_hold(self, tmp_path):
        log = AccessLog(tmp_path / "auth" / "access.jsonl")
        log.record("signin", who="ada@example.com", role="admin", method="email")
        log.record("search", who="ada@example.com", role="admin", action="search", collection="docs", route="POST /x")
        log.record("denied", who="sam@example.com", role="viewer", action="search", status=403)
        recent = log.recent()
        assert [r["event"] for r in recent] == ["denied", "search", "signin"]
        assert log.recent(who="sam@example.com")[0]["status"] == 403 and len(log.recent(event="search")) == 1
        assert not {"query", "text", "result_ids", "ids"} & set().union(*recent)

    def test_an_event_nobody_defined_is_a_bug(self, tmp_path):
        with pytest.raises(ValueError):
            AccessLog(tmp_path / "a.jsonl").record("peeked")
