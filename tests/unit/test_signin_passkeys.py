"""Passkeys, from the bytes a device sends to a session in a browser.

What is being held to. A passkey reply is accepted only when the device
checked the person (present and verified, never a bare touch), the reply comes
from this site's address and names this server's relying party, it answers the
one challenge the server issued for that purpose and that person, and it
carries a signature from the key that was registered. A challenge works once,
and for five minutes. A signature count that does not go up is a copied key,
and is refused. What a device sends is read strictly: anything malformed or
left over is a refusal, never a guess.

On the server: a person sets up a passkey from the emailed link and leaves
with a session and recovery codes; signs in with it on any browser; adds
another; cannot remove their last way in; and confirms it is them with one
before a change that matters. Passkeys belong to people on the server's own
list: single sign-on and API keys are turned away from them, and a person who
is removed, reset or disabled takes their passkeys' power with them.

The device is ``fake_passkey.FakePasskey``: real keys and real signatures, and
replies shaped like the ones the dashboard posts.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("jwt", reason="the signin extra is not installed")
pytest.importorskip("cryptography", reason="the signin extra is not installed")

sys.path.insert(0, os.path.dirname(__file__))
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402
from fake_idp import ISSUER, FakeIdp  # noqa: E402
from fake_passkey import AT, EDDSA, ES256, RS256, UP, UV, FakePasskey, b64u, cbor, cose_key, new_key, unb64u  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb.signin import OidcConfig, SignInConfig, totp  # noqa: E402
from vectrixdb.signin import passkeys as pk  # noqa: E402
from vectrixdb.signin.store import SignInStore  # noqa: E402

SECRET = "k" * 48
PUBLIC = "https://vectors.example.test"
RP_ID = "vectors.example.test"
KEY = "the-full-api-key"
ADA, OLU, VI = "ada@example.com", "olu@example.com", "vi@example.com"
HANDLE = bytes(range(16))
ALGORITHMS, ALGORITHM_NAMES = [ES256, EDDSA, RS256], ["ES256", "EdDSA", "RS256"]
ELEVEN_MINUTES = 11 * 60


# ------------------------------------------------------------------ helpers


def created(device: FakePasskey, **forge) -> tuple[dict, bytes]:
    """A device's answer to a fresh request to make a passkey, and the challenge that request carried."""
    challenge = os.urandom(32)
    options = pk.creation_options(rp_id=RP_ID, rp_name="VectrixDB", user_id=HANDLE, user_name=ADA, display_name=ADA, challenge=challenge)
    return device.create(options, **forge), challenge


def register(device: FakePasskey, **forge) -> pk.NewPasskey:
    credential, challenge = created(device, **forge)
    return pk.verify_registration(credential, challenge=challenge, origin=PUBLIC, rp_id=RP_ID)


def sign_in_check(device: FakePasskey, made: pk.NewPasskey, *, kept=None, **forge) -> int:
    """A sign-in the device answers, checked against what registration kept. The count to keep next."""
    challenge = os.urandom(32)
    credential = device.get(pk.request_options(rp_id=RP_ID, challenge=challenge), **forge)
    return pk.verify_assertion(
        credential, challenge=challenge, origin=PUBLIC, rp_id=RP_ID, public_key=made.public_key,
        sign_count=made.sign_count if kept is None else kept,
    )


def create_app(**kwargs):
    """The server as it is now. Another test drops and re-imports ``vectrixdb.api``,
    and a name bound at the top of this file would go on pointing at the old copy,
    whose routes reach for a database the new copy's start-up never gave them."""
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


class Mail:
    """Stands in for the mail server: keeps what was sent, and finds the set-up link in it."""

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
        "users": ((ADA, "admin"), (OLU, "operator"), (VI, "viewer")),
        "store_path": root / "auth" / "signin.db",
        "access_log": root / "auth" / "access.jsonl",
        "sender": Mail(),
    }
    base.update(over)
    return SignInConfig(**base)


def _oidc() -> OidcConfig:
    return OidcConfig(issuer=ISSUER, client_id="vectrixdb", client_secret="s3cret", role_map={"g-admins": "admin", "g-ops": "operator"})


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
    monkeypatch.delenv("VECTRIXDB_AUDIT_JSONL", raising=False)
    root = tmp_path / "db"
    config = _config(root)
    with TestClient(create_app(db_path=str(root), enable_dashboard=False, signin=config), base_url=PUBLIC) as client:
        yield client, config


@pytest.fixture
def both(tmp_path, monkeypatch):
    """Single sign-on and the email list on one server, with a stand-in identity provider."""
    monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
    monkeypatch.delenv("VECTRIXDB_AUDIT_JSONL", raising=False)
    idp = FakeIdp()
    root = tmp_path / "db"
    config = _config(root, methods=("oidc", "email"), oidc=_oidc())
    app = create_app(db_path=str(root), enable_dashboard=False, signin=config, oidc_transport=idp.transport)
    with TestClient(app, base_url=PUBLIC, follow_redirects=False) as client:
        yield client, config, idp


def browser(server) -> TestClient:
    """Another browser on the same server, with no cookies yet."""
    return TestClient(server[0].app, base_url=PUBLIC)


def csrf(browser: TestClient) -> dict:
    return {"x-csrf-token": browser.cookies.get("__Host-vx_csrf")}


def set_up(browser: TestClient, config: SignInConfig, email: str) -> dict:
    """The emailed link, spent by the button on the page it opens: a ticket, and an authenticator secret."""
    assert browser.post("/auth/email/begin", json={"email": email}).status_code == 200
    begun = browser.post("/auth/email/enrol/begin", json={"token": config.sender.token()})
    assert begun.status_code == 200, begun.text
    return begun.json()["data"]


def enrol_options(browser: TestClient, ticket: str) -> dict:
    reply = browser.post("/auth/passkey/enrol/begin", json={"token": ticket})
    assert reply.status_code == 200, reply.text
    return reply.json()["data"]["options"]


def enrol_with_passkey(browser: TestClient, config: SignInConfig, email: str, device=None) -> tuple[FakePasskey, dict]:
    """The whole first visit, choosing a passkey. The device, and what the server answered."""
    device = device or FakePasskey(PUBLIC)
    ticket = set_up(browser, config, email)["ticket"]
    done = browser.post("/auth/passkey/enrol/finish", json={"ticket": ticket, "credential": device.create(enrol_options(browser, ticket))})
    assert done.status_code == 200, done.text
    return device, done.json()["data"]


def enrol_with_code(browser: TestClient, config: SignInConfig, email: str) -> None:
    """The whole first visit, choosing an authenticator app."""
    begun = set_up(browser, config, email)
    done = browser.post("/auth/email/enrol/confirm", json={"ticket": begun["ticket"], "code": totp.code_at(begun["secret"], totp.step_now())})
    assert done.status_code == 200, done.text


def person(server, email: str) -> tuple[TestClient, FakePasskey]:
    """A browser of their own, signed in as this person, who set up with a passkey."""
    own = browser(server)
    device, _ = enrol_with_passkey(own, server[1], email)
    return own, device


def sign_in(browser: TestClient, device: FakePasskey, **forge):
    """Sign in with a passkey, as the page's button does: ask for a challenge, let the device answer it."""
    options = browser.post("/auth/passkey/begin").json()["data"]["options"]
    return browser.post("/auth/passkey/finish", json={"credential": device.get(options, **forge)})


def add_passkey(browser: TestClient, device: FakePasskey, **extra):
    options = browser.post("/auth/me/passkeys/begin", headers=csrf(browser)).json()["data"]["options"]
    return browser.post("/auth/me/passkeys/finish", json={"credential": device.create(options), **extra}, headers=csrf(browser))


def remove(browser: TestClient, device: FakePasskey):
    return browser.delete(f"/auth/me/passkeys/{b64u(device.credential_id)}", headers=csrf(browser))


def step_up(browser: TestClient, device: FakePasskey, **forge):
    options = browser.post("/auth/step-up/passkey/begin", headers=csrf(browser)).json()["data"]["options"]
    return browser.post("/auth/step-up/passkey/finish", json={"credential": device.get(options, **forge)}, headers=csrf(browser))


def make_a_key(browser: TestClient):
    """A change that needs a fresh check: an admin making an API key."""
    return browser.post("/api/v1/keys", json={"name": "nightly", "role": "reader"}, headers=csrf(browser))


def passkey_ids(browser: TestClient) -> set:
    return {k["id"] for k in browser.get("/auth/me/ways").json()["data"]["passkeys"]}


def logged(server) -> set:
    return {(r["event"], r.get("who"), r.get("method")) for r in server[0].app.state.signin.access.recent()}


def later(monkeypatch, seconds: float) -> None:
    """Every clock the server reads runs this many seconds ahead from now on."""
    now = time.time
    monkeypatch.setattr(time, "time", lambda: now() + seconds)


def stop_clock(monkeypatch, at: float) -> None:
    """Every clock the server reads stands still at this moment from now on."""
    monkeypatch.setattr(time, "time", lambda: at)


def sso_sign_in(client: TestClient, idp: FakeIdp) -> None:
    start = client.get("/auth/oidc/start")
    assert start.status_code == 302
    code, state = idp.authorize(start.headers["location"])
    back = client.get("/auth/oidc/callback", params={"code": code, "state": state})
    assert back.status_code == 302 and "error" not in back.headers["location"], back.headers["location"]


# ================================================================ the parts


class TestMakingAPasskey:
    def test_a_good_registration_gives_back_the_id_the_public_key_and_the_count(self):
        device = FakePasskey(PUBLIC)
        made = register(device, sign_count=7)
        assert made.credential_id == device.credential_id
        assert pk.cbor_decode(made.public_key) == device.cose_key(), "the public half, as the device wrote it"
        assert made.alg == ES256 and made.sign_count == 7
        assert made.transports == ["internal", "hybrid"]

    @pytest.mark.parametrize("flags", [UP | AT, UV | AT, AT], ids=["present but not verified", "verified but not present", "neither"])
    def test_a_device_that_did_not_check_the_person_is_refused(self, flags):
        with pytest.raises(pk.PasskeyRefused, match="did not check that it was you"):
            register(FakePasskey(PUBLIC), flags=flags)

    @pytest.mark.parametrize(
        "origin",
        ["https://evil.example.test", "http://vectors.example.test", "https://vectors.example.test:8443", "https://vectors.example.test.evil.example"],
        ids=["another site", "plain http", "another port", "a look-alike"],
    )
    def test_a_reply_from_another_address_is_refused(self, origin):
        with pytest.raises(pk.PasskeyRefused, match="different address"):
            register(FakePasskey(PUBLIC), origin=origin)

    def test_a_reply_made_inside_a_frame_on_another_site_is_refused(self):
        with pytest.raises(pk.PasskeyRefused, match="different address"):
            register(FakePasskey(PUBLIC), cross_origin=True)

    @pytest.mark.parametrize("rp_id", ["evil.example.test", "example.test"], ids=["another site", "the parent domain"])
    def test_a_passkey_for_another_relying_party_is_refused(self, rp_id):
        with pytest.raises(pk.PasskeyRefused, match="different site"):
            register(FakePasskey(PUBLIC), rp_id=rp_id)

    def test_a_reply_to_another_challenge_is_refused(self):
        with pytest.raises(pk.PasskeyRefused, match="different request"):
            register(FakePasskey(PUBLIC), challenge=os.urandom(32))

    def test_a_sign_in_reply_is_not_a_registration(self):
        with pytest.raises(pk.PasskeyRefused):
            register(FakePasskey(PUBLIC), client_type="webauthn.get")

    def test_an_id_that_is_not_the_one_in_the_authenticator_data_is_refused(self):
        with pytest.raises(pk.PasskeyRefused):
            register(FakePasskey(PUBLIC), raw_id=os.urandom(16))

    def test_a_reply_that_carries_no_new_credential_is_refused(self):
        with pytest.raises(pk.PasskeyRefused):
            register(FakePasskey(PUBLIC), flags=UP | UV)

    def test_a_reply_that_is_not_a_public_key_credential_is_refused(self):
        credential, challenge = created(FakePasskey(PUBLIC))
        with pytest.raises(pk.PasskeyRefused):
            pk.verify_registration({**credential, "type": "password"}, challenge=challenge, origin=PUBLIC, rp_id=RP_ID)


class TestCheckingASignIn:
    def test_a_good_sign_in_gives_the_count_to_keep(self):
        device = FakePasskey(PUBLIC)
        made = register(device)
        assert sign_in_check(device, made) == 1
        assert sign_in_check(device, made, kept=1) == 2

    @pytest.mark.parametrize("flags", [UP, UV, 0], ids=["present but not verified", "verified but not present", "neither"])
    def test_a_device_that_did_not_check_the_person_is_refused(self, flags):
        device = FakePasskey(PUBLIC)
        made = register(device)
        with pytest.raises(pk.PasskeyRefused, match="did not check that it was you"):
            sign_in_check(device, made, flags=flags)

    def test_a_reply_from_another_address_or_for_another_site_is_refused(self):
        device = FakePasskey(PUBLIC)
        made = register(device)
        with pytest.raises(pk.PasskeyRefused, match="different address"):
            sign_in_check(device, made, origin="https://vectors.example.test.evil.example")
        with pytest.raises(pk.PasskeyRefused, match="different site"):
            sign_in_check(device, made, rp_id="evil.example.test")

    def test_a_reply_to_another_challenge_is_refused(self):
        device = FakePasskey(PUBLIC)
        made = register(device)
        with pytest.raises(pk.PasskeyRefused, match="different request"):
            sign_in_check(device, made, challenge=os.urandom(32))

    def test_a_registration_reply_is_not_a_sign_in(self):
        device = FakePasskey(PUBLIC)
        made = register(device)
        with pytest.raises(pk.PasskeyRefused):
            sign_in_check(device, made, client_type="webauthn.create")

    @pytest.mark.parametrize(
        "signature",
        [b"", b"\x30\x06\x02\x01\x01\x02\x01\x01", b"\x5a" * 72],
        ids=["empty", "well formed and wrong", "noise"],
    )
    def test_a_signature_that_is_not_one_is_refused(self, signature):
        device = FakePasskey(PUBLIC)
        made = register(device)
        with pytest.raises(pk.PasskeyRefused):
            sign_in_check(device, made, signature=signature)

    def test_a_signature_from_any_other_key_is_refused(self):
        device = FakePasskey(PUBLIC)
        made = register(device)
        with pytest.raises(pk.PasskeyRefused):
            sign_in_check(device, made, key=new_key())

    def test_a_signature_covers_exactly_what_was_sent(self):
        device = FakePasskey(PUBLIC)
        made = register(device)
        challenge = os.urandom(32)
        options = pk.request_options(rp_id=RP_ID, challenge=challenge)
        first, second = device.get(options), device.get(options)
        second["response"]["signature"] = first["response"]["signature"]  # good, but over the first reply's data

        def check(credential):
            return pk.verify_assertion(credential, challenge=challenge, origin=PUBLIC, rp_id=RP_ID, public_key=made.public_key, sign_count=0)

        with pytest.raises(pk.PasskeyRefused):
            check(second)
        assert check(first) == 1

    @pytest.mark.parametrize(
        "kept, sent, good",
        [(0, 0, True), (0, 1, True), (5, 6, True), (5, 5, False), (5, 4, False), (5, 0, False)],
        ids=["a device that keeps no count", "the first count", "one more", "the same again", "one less", "back to nothing"],
    )
    def test_a_count_that_does_not_go_up_is_a_copied_key(self, kept, sent, good):
        device = FakePasskey(PUBLIC)
        made = register(device)
        if good:
            assert sign_in_check(device, made, kept=kept, sign_count=sent) == sent
        else:
            with pytest.raises(pk.PasskeyRefused, match="copy"):
                sign_in_check(device, made, kept=kept, sign_count=sent)


class TestTheKindsOfKeyAPasskeyMayUse:
    @pytest.mark.parametrize("alg", ALGORITHMS, ids=ALGORITHM_NAMES)
    def test_each_kind_is_read_from_its_cose_key(self, alg):
        device = FakePasskey(PUBLIC, alg=alg)
        read, key = pk.public_key_from_cose(device.cose_key())
        spki = (Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        assert read == alg
        assert key.public_bytes(*spki) == device.key.public_key().public_bytes(*spki)

    @pytest.mark.parametrize("alg", ALGORITHMS, ids=ALGORITHM_NAMES)
    def test_each_kind_registers_and_then_signs_in(self, alg):
        device = FakePasskey(PUBLIC, alg=alg)
        made = register(device)
        assert made.alg == alg and sign_in_check(device, made) == 1

    def test_an_rsa_key_shorter_than_2048_bits_is_refused(self):
        with pytest.raises(pk.PasskeyRefused):
            pk.public_key_from_cose(cose_key(RS256, new_key(RS256, rsa_bits=1024).public_key()))

    def test_a_kind_of_key_the_server_does_not_take_is_refused_in_words(self):
        p384 = ec.generate_private_key(ec.SECP384R1()).public_key().public_numbers()
        es384 = {1: 2, 3: -35, -1: 2, -2: p384.x.to_bytes(48, "big"), -3: p384.y.to_bytes(48, "big")}
        with pytest.raises(pk.PasskeyRefused, match="kind of key this server does not accept"):
            pk.public_key_from_cose(es384)

    def test_a_point_that_is_not_on_the_curve_is_refused(self):
        with pytest.raises(pk.PasskeyRefused):
            pk.public_key_from_cose({1: 2, 3: ES256, -1: 1, -2: bytes(32), -3: b"\x01" * 32})


class TestReadingWhatADeviceSends:
    def test_what_a_device_writes_the_server_reads_back(self):
        value = {
            "fmt": "none",
            "attStmt": {},
            1: 2,
            3: -7,
            -2: bytes(32),
            "sizes": [0, 23, 24, 255, 256, 65535, 65536, 2**32 - 1, 2**32, -1, -24, -25, -257, -(2**40)],
            "text": "clé",
        }
        assert pk.cbor_decode(cbor(value)) == value

    @pytest.mark.parametrize(
        "raw",
        [
            cbor(1) + b"\x00",
            b"\x5f\x41\x00\xff",
            b"\xa2\x01\x01\x01\x02",
            b"\x58\x10\x00\x01",
            b"\x81" * 17 + b"\x00",
            b"\x62\xff\xfe",
            b"\xa1\x41\x00\x01",
            b"",
        ],
        ids=[
            "a byte left over", "an indefinite length", "one key twice in a map", "a length past the end",
            "nested seventeen deep", "text that is not UTF-8", "a map key that is neither a number nor text", "nothing at all",
        ],
    )
    def test_anything_malformed_or_left_over_is_a_refusal(self, raw):
        with pytest.raises(pk.PasskeyRefused):
            pk.cbor_decode(raw)

    def test_authenticator_data_is_read_field_by_field(self):
        device = FakePasskey(PUBLIC)
        credential, _ = created(device, sign_count=3)
        parsed = pk.parse_auth_data(pk.cbor_decode(unb64u(credential["response"]["attestationObject"]))["authData"])
        assert parsed.rp_id_hash == hashlib.sha256(RP_ID.encode()).digest()
        assert parsed.user_present and parsed.user_verified and parsed.sign_count == 3
        assert parsed.credential_id == device.credential_id
        assert pk.cbor_decode(parsed.public_key) == device.cose_key()

    def test_authenticator_data_cut_short_or_with_bytes_after_it_is_refused(self):
        credential, _ = created(FakePasskey(PUBLIC))
        auth = pk.cbor_decode(unb64u(credential["response"]["attestationObject"]))["authData"]
        for broken in (auth[:36], auth[:60], auth[:-1], auth + b"\x00"):
            with pytest.raises(pk.PasskeyRefused):
                pk.parse_auth_data(broken)


class TestWhatTheBrowserIsAskedFor:
    def test_a_new_passkey_must_live_on_the_device_and_ask_for_its_pin(self):
        options = pk.creation_options(rp_id=RP_ID, rp_name="VectrixDB", user_id=HANDLE, user_name=ADA, display_name=ADA, challenge=b"c" * 32, exclude=[b"had"])
        assert options["authenticatorSelection"] == {"residentKey": "required", "requireResidentKey": True, "userVerification": "required"}
        assert [p["alg"] for p in options["pubKeyCredParams"]] == [ES256, EDDSA, RS256]
        assert options["attestation"] == "none" and options["rp"]["id"] == RP_ID
        assert unb64u(options["user"]["id"]) == HANDLE and unb64u(options["challenge"]) == b"c" * 32
        assert options["excludeCredentials"] == [{"type": "public-key", "id": b64u(b"had")}]

    def test_a_sign_in_asks_for_the_pin_too_and_names_no_passkeys(self):
        options = pk.request_options(rp_id=RP_ID, challenge=b"c" * 32)
        assert options["userVerification"] == "required" and options["rpId"] == RP_ID
        assert options["allowCredentials"] == [], "the device offers what it holds for this site"

    @pytest.mark.parametrize(
        "public, origin, rp_id",
        [
            ("https://vectors.example.test", "https://vectors.example.test", "vectors.example.test"),
            ("https://vectors.example.test:443/search", "https://vectors.example.test", "vectors.example.test"),
            ("https://Vectors.Example.test:8443", "https://vectors.example.test:8443", "vectors.example.test"),
            ("http://localhost:7337", "http://localhost:7337", "localhost"),
        ],
    )
    def test_the_origin_and_the_relying_party_come_from_the_public_address(self, public, origin, rp_id):
        assert pk.origin_of(public) == origin and pk.rp_id_of(public) == rp_id


class TestTheChallengesTheServerKeeps:
    def test_a_challenge_is_kept_as_a_hash_and_answers_once_for_its_own_purpose(self, tmp_path):
        store = SignInStore(tmp_path / "auth" / "signin.db", [SECRET])
        try:
            store.put_person(OLU, "operator")
            challenge = store.new_challenge("passkey-add", OLU)
            raw = sqlite3.connect(str(tmp_path / "auth" / "signin.db"))
            try:
                kept = repr(raw.execute("SELECT * FROM records WHERE kind = 'challenge'").fetchall())
            finally:
                raw.close()
            assert hashlib.sha256(challenge).hexdigest() in kept
            assert challenge.hex() not in kept and b64u(challenge) not in kept
            assert store.use_challenge(challenge, "passkey-add") == OLU
            assert store.use_challenge(challenge, "passkey-add") is None, "once"
            assert store.use_challenge(store.new_challenge("passkey-stepup", OLU), "passkey-signin") is None, "for its own purpose"
            assert store.use_challenge(store.new_challenge("passkey-signin"), "passkey-signin") == "", "a sign-in challenge is for anybody"
        finally:
            store.close()


# ============================================================= on the server


class TestSettingUpWithAPasskey:
    def test_a_person_sets_up_from_the_emailed_link_and_leaves_signed_in_with_recovery_codes(self, server):
        client, config = server
        device, done = enrol_with_passkey(client, config, OLU)
        codes = done["recovery_codes"]
        assert len(codes) == 10 == len(set(codes))
        assert done["person"]["email"] == OLU and done["person"]["method"] == "passkey"
        me = client.get("/auth/me")
        assert me.status_code == 200 and me.json()["data"]["person"]["method"] == "passkey"
        ways = client.get("/auth/me/ways").json()["data"]
        assert [k["id"] for k in ways["passkeys"]] == [b64u(device.credential_id)]
        assert ways["authenticator"] is None and ways["recovery_codes_left"] == 10
        assert {("enrolled", OLU, "passkey"), ("passkey_added", OLU, "passkey")} <= logged(server)
        client.post("/auth/email/begin", json={"email": OLU})
        assert len(config.sender.sent) == 1, "a passkey is a way in, so no second set-up link is sent"

    def test_the_link_is_spent_by_the_button_and_the_ticket_by_finishing(self, server):
        client, config = server
        client.post("/auth/email/begin", json={"email": OLU})
        token = config.sender.token()
        begun = client.post("/auth/email/enrol/begin", json={"token": token})
        assert begun.status_code == 200
        assert client.post("/auth/email/enrol/begin", json={"token": token}).status_code == 400, "the link works once"
        ticket = begun.json()["data"]["ticket"]
        enrol_options(client, ticket)
        options = enrol_options(client, ticket)  # a closed prompt, then Try again: asking twice spends nothing
        assert client.post("/auth/passkey/enrol/finish", json={"ticket": ticket, "credential": FakePasskey(PUBLIC).create(options)}).status_code == 200
        again = client.post("/auth/passkey/enrol/finish", json={"ticket": ticket, "credential": FakePasskey(PUBLIC).create(options)})
        assert again.status_code == 400 and again.json()["data"] is None
        assert len(client.get("/auth/me/ways").json()["data"]["passkeys"]) == 1

    def test_the_authenticator_secret_offered_beside_it_is_forgotten(self, server):
        client, config = server
        store = client.app.state.signin.store
        begun = set_up(client, config, OLU)
        assert store.pending_secret(OLU) == begun["secret"]
        answered = FakePasskey(PUBLIC).create(enrol_options(client, begun["ticket"]))
        assert client.post("/auth/passkey/enrol/finish", json={"ticket": begun["ticket"], "credential": answered}).status_code == 200
        assert store.pending_secret(OLU) is None
        found = store.person(OLU)
        assert found.enrolled and found.passkeys == 1 and not found.authenticator

    def test_the_options_carry_a_random_handle_not_the_address_and_ask_for_the_pin(self, server):
        client, config = server
        ticket = set_up(client, config, OLU)["ticket"]
        options = enrol_options(client, ticket)
        handle = unb64u(options["user"]["id"])
        assert len(handle) == 16 and OLU.encode() not in handle
        assert unb64u(enrol_options(client, ticket)["user"]["id"]) == handle, "the same handle each time"
        assert options["rp"]["id"] == RP_ID and options["user"]["name"] == OLU
        assert options["authenticatorSelection"]["userVerification"] == "required"
        assert options["excludeCredentials"] == []

    def test_a_refused_passkey_hands_back_a_fresh_ticket_to_try_again(self, server):
        client, config = server
        ticket = set_up(client, config, OLU)["ticket"]
        no_pin = FakePasskey(PUBLIC).create(enrol_options(client, ticket), flags=UP | AT)
        refused = client.post("/auth/passkey/enrol/finish", json={"ticket": ticket, "credential": no_pin})
        assert refused.status_code == 400 and "did not check that it was you" in refused.json()["message"]
        assert client.get("/auth/me").status_code == 401, "and nobody was signed in"
        assert client.post("/auth/passkey/enrol/begin", json={"token": ticket}).status_code == 400, "the old ticket is spent"
        fresh = refused.json()["data"]["ticket"]
        done = client.post("/auth/passkey/enrol/finish", json={"ticket": fresh, "credential": FakePasskey(PUBLIC).create(enrol_options(client, fresh))})
        assert done.status_code == 200, done.text

    def test_a_challenge_issued_to_one_person_does_not_set_up_another(self, server):
        client, config = server
        ada_ticket = set_up(client, config, ADA)["ticket"]
        olu_ticket = set_up(client, config, OLU)["ticket"]
        answered = FakePasskey(PUBLIC).create(enrol_options(client, ada_ticket))
        wrong = client.post("/auth/passkey/enrol/finish", json={"ticket": olu_ticket, "credential": answered})
        assert wrong.status_code == 400 and "run out" in wrong.json()["message"]
        assert client.app.state.signin.store.person(OLU).passkeys == 0

    def test_nobody_sets_up_without_a_ticket(self, server):
        client, _ = server
        answered = FakePasskey(PUBLIC).create({"challenge": b64u(os.urandom(32))})
        assert client.post("/auth/passkey/enrol/begin", json={"token": "made-up"}).status_code == 400
        assert client.post("/auth/passkey/enrol/finish", json={"ticket": "made-up", "credential": answered}).status_code == 400
        assert client.post("/auth/passkey/enrol/finish", json={"credential": answered}).status_code == 400


class TestSigningInWithAPasskey:
    def test_the_passkey_signs_its_owner_in_on_any_browser(self, server):
        client, config = server
        device, _ = enrol_with_passkey(client, config, OLU)
        elsewhere = browser(server)
        options = elsewhere.post("/auth/passkey/begin").json()["data"]["options"]
        assert options["rpId"] == RP_ID and options["userVerification"] == "required" and options["allowCredentials"] == []
        reply = elsewhere.post("/auth/passkey/finish", json={"credential": device.get(options)})
        assert reply.status_code == 200, reply.text
        me = elsewhere.get("/auth/me").json()["data"]["person"]
        assert me["email"] == OLU and me["role"] == "operator" and me["method"] == "passkey"
        assert elsewhere.get("/api/v1/collections").status_code == 200
        assert elsewhere.get("/auth/me/ways").json()["data"]["passkeys"][0]["last_used"] is not None
        assert ("signin", OLU, "passkey") in logged(server)

    def test_a_challenge_works_once(self, server):
        client, config = server
        device, _ = enrol_with_passkey(client, config, OLU)
        options = client.post("/auth/passkey/begin").json()["data"]["options"]
        assert browser(server).post("/auth/passkey/finish", json={"credential": device.get(options)}).status_code == 200
        replay = browser(server).post("/auth/passkey/finish", json={"credential": device.get(options)})  # signed afresh, with a higher count
        assert replay.status_code == 401 and "run out" in replay.json()["message"]

    def test_a_challenge_answered_just_inside_five_minutes_works(self, server, monkeypatch):
        client, config = server
        device, _ = enrol_with_passkey(client, config, OLU)
        before = time.time()
        options = client.post("/auth/passkey/begin").json()["data"]["options"]
        stop_clock(monkeypatch, before + 299)
        assert browser(server).post("/auth/passkey/finish", json={"credential": device.get(options)}).status_code == 200

    def test_a_challenge_older_than_five_minutes_is_refused(self, server, monkeypatch):
        client, config = server
        device, _ = enrol_with_passkey(client, config, OLU)
        options = client.post("/auth/passkey/begin").json()["data"]["options"]
        stop_clock(monkeypatch, time.time() + 301)
        late = browser(server).post("/auth/passkey/finish", json={"credential": device.get(options)})
        assert late.status_code == 401 and "run out" in late.json()["message"]

    def test_a_copied_passkey_is_refused_when_its_count_does_not_go_up(self, server):
        client, config = server
        device, _ = enrol_with_passkey(client, config, OLU)
        copy = device.copy()
        assert sign_in(browser(server), device).status_code == 200
        stolen = sign_in(browser(server), copy)
        assert stolen.status_code == 401 and "copy" in stolen.json()["message"]
        assert sign_in(browser(server), device).status_code == 200, "the device that kept counting carries on"

    def test_a_device_that_keeps_no_count_signs_in_every_time(self, server):
        client, config = server
        device, _ = enrol_with_passkey(client, config, OLU, FakePasskey(PUBLIC, counts=False))
        assert sign_in(browser(server), device).status_code == 200
        assert sign_in(browser(server), device).status_code == 200

    @pytest.mark.parametrize(
        "forge",
        [
            {"origin": "https://vectors.example.test.evil.example"},
            {"rp_id": "evil.example.test"},
            {"flags": UP},
            {"flags": UV},
            {"key": new_key()},
            {"user_handle": b"somebody else!!!"},
        ],
        ids=["a look-alike address", "another site's passkey", "no PIN asked", "nobody there", "another key", "another person's handle"],
    )
    def test_a_reply_wrong_in_any_one_way_signs_nobody_in(self, server, forge):
        client, config = server
        device, _ = enrol_with_passkey(client, config, OLU)
        elsewhere = browser(server)
        assert sign_in(elsewhere, device, **forge).status_code == 401
        assert elsewhere.get("/auth/me").status_code == 401 and "__Host-vx_sid" not in elsewhere.cookies

    def test_a_passkey_nobody_registered_signs_nobody_in(self, server):
        stranger = sign_in(browser(server), FakePasskey(PUBLIC))
        assert stranger.status_code == 401 and "not registered here" in stranger.json()["message"]
        assert ("signin_failed", None, "passkey") in logged(server)

    def test_somebody_who_loses_their_only_passkey_gets_in_with_a_recovery_code_and_makes_a_new_one(self, server):
        client, config = server
        _, done = enrol_with_passkey(client, config, OLU)
        fresh = browser(server)
        back = fresh.post("/auth/email/verify", json={"email": OLU, "code": done["recovery_codes"][0]})
        assert back.status_code == 200 and back.json()["data"]["recovery_codes_left"] == 9
        replacement = FakePasskey(PUBLIC)
        assert add_passkey(fresh, replacement).status_code == 200
        assert sign_in(browser(server), replacement).status_code == 200


class TestAddingAndRemovingPasskeys:
    def test_a_signed_in_person_adds_a_second_passkey_and_either_one_signs_in(self, server):
        olu, first = person(server, OLU)
        options = olu.post("/auth/me/passkeys/begin", headers=csrf(olu)).json()["data"]["options"]
        assert [c["id"] for c in options["excludeCredentials"]] == [b64u(first.credential_id)], "so the device that has one makes no second"
        assert unb64u(options["user"]["id"]) == first.user_handle, "one person, one handle"
        second = FakePasskey(PUBLIC)
        added = olu.post("/auth/me/passkeys/finish", json={"credential": second.create(options), "name": "Work laptop"}, headers=csrf(olu))
        assert added.status_code == 200, added.text
        assert added.json()["data"]["id"] == b64u(second.credential_id) and added.json()["data"]["name"] == "Work laptop"
        assert passkey_ids(olu) == {b64u(first.credential_id), b64u(second.credential_id)}
        assert sign_in(browser(server), first).status_code == 200
        assert sign_in(browser(server), second).status_code == 200

    def test_a_passkey_with_no_name_is_called_after_the_browser_it_was_made_in(self, server):
        olu, _ = person(server, OLU)
        edge = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 Edg/126.0"}
        options = olu.post("/auth/me/passkeys/begin", headers=csrf(olu)).json()["data"]["options"]
        added = olu.post("/auth/me/passkeys/finish", json={"credential": FakePasskey(PUBLIC).create(options)}, headers={**csrf(olu), **edge})
        assert added.json()["data"]["name"] == "Edge on Windows"

    def test_adding_one_needs_a_session_and_the_forgery_token(self, server):
        olu, _ = person(server, OLU)
        assert browser(server).post("/auth/me/passkeys/begin").status_code == 401
        assert olu.post("/auth/me/passkeys/begin").status_code == 403

    def test_a_request_to_add_a_passkey_for_one_person_does_not_add_it_for_another(self, server):
        olu, _ = person(server, OLU)
        vi, _ = person(server, VI)
        options = olu.post("/auth/me/passkeys/begin", headers=csrf(olu)).json()["data"]["options"]
        wrong = vi.post("/auth/me/passkeys/finish", json={"credential": FakePasskey(PUBLIC).create(options)}, headers=csrf(vi))
        assert wrong.status_code == 400
        assert len(passkey_ids(olu)) == 1 and len(passkey_ids(vi)) == 1

    def test_the_only_way_in_cannot_be_removed(self, server):
        olu, device = person(server, OLU)
        refused = remove(olu, device)
        assert refused.status_code == 409 and "only way in" in refused.json()["message"]
        assert sign_in(browser(server), device).status_code == 200

    def test_one_of_two_can_be_removed_and_then_it_opens_nothing(self, server):
        olu, first = person(server, OLU)
        second = FakePasskey(PUBLIC)
        assert add_passkey(olu, second).status_code == 200
        assert remove(olu, first).status_code == 200
        assert passkey_ids(olu) == {b64u(second.credential_id)}
        assert sign_in(browser(server), first).status_code == 401
        assert sign_in(browser(server), second).status_code == 200
        assert remove(olu, second).status_code == 409, "and the one left is now the last"
        assert ("passkey_removed", OLU, "passkey") in logged(server)

    def test_somebody_with_an_authenticator_app_may_remove_their_only_passkey(self, server):
        olu = browser(server)
        enrol_with_code(olu, server[1], OLU)
        device = FakePasskey(PUBLIC)
        assert add_passkey(olu, device).status_code == 200
        assert remove(olu, device).status_code == 200
        assert passkey_ids(olu) == set()

    def test_nobody_removes_somebody_elses_passkey(self, server):
        _, device = person(server, OLU)
        vi = browser(server)
        enrol_with_code(vi, server[1], VI)  # an authenticator, so the only-way-in rule does not answer first
        assert remove(vi, device).status_code == 404
        assert sign_in(browser(server), device).status_code == 200


class TestConfirmingItIsYouWithAPasskey:
    def test_a_passkey_confirms_it_is_you_and_the_change_goes_through(self, server, monkeypatch):
        ada, device = person(server, ADA)
        later(monkeypatch, ELEVEN_MINUTES)
        asked = make_a_key(ada)
        assert asked.status_code == 403
        assert asked.json()["data"]["step_up"] is True and asked.json()["data"]["ways"] == ["passkey"]
        options = ada.post("/auth/step-up/passkey/begin", headers=csrf(ada)).json()["data"]["options"]
        assert [c["id"] for c in options["allowCredentials"]] == [b64u(device.credential_id)]
        confirmed = ada.post("/auth/step-up/passkey/finish", json={"credential": device.get(options)}, headers=csrf(ada))
        assert confirmed.status_code == 200, confirmed.text
        assert make_a_key(ada).status_code == 200
        assert ("step_up", ADA, "passkey") in logged(server)

    def test_the_ways_offered_are_the_ones_the_person_has(self, server, monkeypatch):
        olu = browser(server)
        enrol_with_code(olu, server[1], OLU)
        assert add_passkey(olu, FakePasskey(PUBLIC)).status_code == 200
        later(monkeypatch, ELEVEN_MINUTES)
        asked = olu.post("/auth/me/passkeys/begin", headers=csrf(olu))
        assert asked.status_code == 403 and asked.json()["data"]["ways"] == ["passkey", "code"]

    def test_a_session_left_open_cannot_change_its_passkeys_without_a_fresh_check(self, server, monkeypatch):
        olu, device = person(server, OLU)
        later(monkeypatch, ELEVEN_MINUTES)
        for asked in (olu.post("/auth/me/passkeys/begin", headers=csrf(olu)), remove(olu, device)):
            assert asked.status_code == 403 and asked.json()["data"]["step_up"] is True
        assert step_up(olu, device).status_code == 200
        assert add_passkey(olu, FakePasskey(PUBLIC)).status_code == 200

    def test_somebody_elses_passkey_does_not_confirm_it_is_you(self, server, monkeypatch):
        ada, _ = person(server, ADA)
        _, olus_passkey = person(server, OLU)
        later(monkeypatch, ELEVEN_MINUTES)
        refused = step_up(ada, olus_passkey)
        assert refused.status_code == 401
        assert make_a_key(ada).status_code == 403

    def test_a_challenge_answers_only_the_request_it_was_issued_for(self, server, monkeypatch):
        ada, device = person(server, ADA)
        later(monkeypatch, ELEVEN_MINUTES)
        for_signing_in = browser(server).post("/auth/passkey/begin").json()["data"]["options"]
        assert ada.post("/auth/step-up/passkey/finish", json={"credential": device.get(for_signing_in)}, headers=csrf(ada)).status_code == 401
        assert make_a_key(ada).status_code == 403
        for_confirming = ada.post("/auth/step-up/passkey/begin", headers=csrf(ada)).json()["data"]["options"]
        elsewhere = browser(server)
        assert elsewhere.post("/auth/passkey/finish", json={"credential": device.get(for_confirming)}).status_code == 401
        assert elsewhere.get("/auth/me").status_code == 401


class TestPasskeysAreForPeopleOnTheList:
    def test_with_only_single_sign_on_there_is_no_passkey_door(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
        root = tmp_path / "db"
        config = _config(root, methods=("oidc",), oidc=_oidc(), users=((ADA, "admin"),))
        app = create_app(db_path=str(root), enable_dashboard=False, signin=config, oidc_transport=FakeIdp().transport)
        with TestClient(app, base_url=PUBLIC) as client:
            assert "email" not in client.get("/auth/me").json()["data"]["methods"], "so the page shows no passkey button"
            answered = FakePasskey(PUBLIC).create({"challenge": b64u(os.urandom(32))})
            assert client.post("/auth/passkey/begin").status_code == 404
            assert client.post("/auth/passkey/finish", json={"credential": answered}).status_code == 404
            assert client.post("/auth/passkey/enrol/begin", json={"token": "t"}).status_code == 404
            assert client.post("/auth/passkey/enrol/finish", json={"ticket": "t", "credential": answered}).status_code == 404

    def test_beside_single_sign_on_there_are_no_passkeys_for_anybody(self, both, monkeypatch):
        client, config, idp = both
        assert client.post("/auth/passkey/begin").status_code == 404
        sso_sign_in(client, idp)
        assert client.get("/auth/me").json()["data"]["person"]["method"] == "oidc"
        ways = client.get("/auth/me/ways").json()["data"]
        assert ways["local"] is False and ways["listed"] is True and ways["own_passkeys"] is False
        assert client.post("/auth/me/passkeys/begin", headers=csrf(client)).status_code == 404
        assert client.post("/auth/step-up/passkey/begin", headers=csrf(client)).status_code == 404
        later(monkeypatch, ELEVEN_MINUTES)
        stale = make_a_key(client)
        assert stale.status_code == 403 and stale.json()["data"]["ways"] == ["sso"], "the provider again: they keep no passkey to offer"

    def test_somebody_let_in_by_the_settings_list_alone_has_no_passkeys_here(self, tmp_path, monkeypatch):
        """On VECTRIXDB_OIDC_ALLOWED_EMAILS and not on the People list: single sign-on is their only way."""
        monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
        idp = FakeIdp()
        root = tmp_path / "db"
        oidc = OidcConfig(
            issuer=ISSUER, client_id="vectrixdb", client_secret="s3cret", role_map={"g-admins": "admin", "g-ops": "operator"},
            allowed_emails=(ADA,),
        )
        config = _config(root, methods=("oidc", "email"), oidc=oidc, users=((OLU, "admin"),))
        app = create_app(db_path=str(root), enable_dashboard=False, signin=config, oidc_transport=idp.transport)
        with TestClient(app, base_url=PUBLIC, follow_redirects=False) as client:
            sso_sign_in(client, idp)
            assert client.get("/auth/me").json()["data"]["person"]["email"] == ADA
            assert client.post("/auth/me/passkeys/begin", headers=csrf(client)).status_code == 400
            assert client.post("/auth/step-up/passkey/begin", headers=csrf(client)).status_code == 400
            ways = client.get("/auth/me/ways").json()["data"]
            assert ways["local"] is False and ways["listed"] is False and "passkeys" not in ways
            later(monkeypatch, ELEVEN_MINUTES)
            stale = make_a_key(client)
            assert stale.status_code == 403 and stale.json()["data"]["ways"] == ["sso"]

    def test_an_api_key_is_not_a_person_and_has_no_passkeys(self, server):
        client, _ = server
        for path in ("/auth/me/passkeys/begin", "/auth/step-up/passkey/begin"):
            assert client.post(path, headers={"api-key": KEY}).status_code == 400

    def test_a_reset_takes_the_passkeys_with_it(self, server):
        ada, _ = person(server, ADA)
        _, olus_passkey = person(server, OLU)
        assert ada.post(f"/auth/people/{OLU}/reset", headers=csrf(ada)).status_code == 200
        assert sign_in(browser(server), olus_passkey).status_code == 401

    def test_a_person_removed_and_added_again_starts_with_no_passkeys(self, server):
        ada, _ = person(server, ADA)
        _, vis_passkey = person(server, VI)
        assert ada.delete(f"/auth/people/{VI}", headers=csrf(ada)).status_code == 200
        assert sign_in(browser(server), vis_passkey).status_code == 401
        assert ada.post("/auth/people", json={"email": VI, "role": "viewer"}, headers=csrf(ada)).status_code == 200
        assert sign_in(browser(server), vis_passkey).status_code == 401, "the address came back, the old passkey did not"

    def test_a_disabled_person_is_not_signed_in_by_their_passkey(self, server):
        olu, device = person(server, OLU)
        server[0].app.state.signin.store.set_disabled(OLU, True)
        assert olu.get("/auth/me").status_code == 401, "disabling ends the session they had"
        refused = sign_in(browser(server), device)
        assert refused.status_code == 401 and "__Host-vx_sid" not in refused.cookies

    def test_a_passkey_made_before_single_sign_on_came_stops_working_with_it(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
        root = tmp_path / "db"
        config = _config(root)
        with TestClient(create_app(db_path=str(root), enable_dashboard=False, signin=config), base_url=PUBLIC) as first:
            adas_passkey, _ = enrol_with_passkey(first, config, ADA)
        first.app.state.signin.close()
        # The server starts again with single sign-on. The passkey is still in the file, and no route takes it.
        with_sso = _config(root, methods=("oidc", "email"), oidc=_oidc())
        app = create_app(db_path=str(root), enable_dashboard=False, signin=with_sso, oidc_transport=FakeIdp().transport)
        with TestClient(app, base_url=PUBLIC) as client:
            assert client.post("/auth/passkey/begin").status_code == 404
            assert client.post("/auth/passkey/finish", json={"credential": {}}).status_code == 404
            assert client.get("/auth/me").status_code == 401
