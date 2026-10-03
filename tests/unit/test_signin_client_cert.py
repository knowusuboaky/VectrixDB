"""Single sign-on where the server proves itself with a key, not a secret.

In place of ``client_secret`` the server signs a short assertion for the
provider's token address (RFC 7523), and the provider checks it against the
certificate registered for the application. No secret is sent or stored, so
none can leak. What is held here: the assertion's claims and header (the
thumbprints Entra ID matches on, ``kid`` for providers that look keys up by
id), a fresh assertion every time, and a mistake in the key or certificate
stopping the server at start rather than at somebody's sign-in.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import sys

import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("jwt", reason="the signin extra is not installed")
pytest.importorskip("cryptography", reason="the signin extra is not installed")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from fake_idp import ISSUER, FakeIdp  # noqa: E402

from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.signin import OidcConfig, SignInConfig  # noqa: E402

SECRET = "c" * 48
PUBLIC = "https://vectors.example.test"
ROLES = {"g-admins": "admin"}


def create_app(**kwargs):
    """Imported when called: another test drops and re-imports ``vectrixdb.api``."""
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


def pem_key(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def pem_cert(key, *, days: float = 365, start_days_ago: float = 1) -> str:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vectrixdb")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=start_days_ago))
        .not_valid_after(now + datetime.timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def server(tmp_path, monkeypatch, oidc: OidcConfig, idp: FakeIdp):
    monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
    config = SignInConfig(
        methods=("oidc",),
        secrets=(SECRET,),
        public_url=PUBLIC,
        oidc=dataclasses.replace(oidc, allowed_emails=("*",)),
        store_path=tmp_path / "auth" / "signin.db",
        access_log=tmp_path / "auth" / "access.jsonl",
    )
    app = create_app(
        db_path=str(tmp_path / "db"),
        enable_dashboard=False,
        signin=config,
        oidc_transport=idp.transport,
    )
    return TestClient(app, base_url=PUBLIC, follow_redirects=False)


def sign_in(client: TestClient, idp: FakeIdp):
    start = client.get("/auth/oidc/start")
    code, state = idp.authorize(start.headers["location"])
    return client.get("/auth/oidc/callback", params={"code": code, "state": state})


def signed_in(client: TestClient) -> bool:
    return client.get("/auth/me").status_code == 200


class TestTheWholeWayRound:
    def test_a_certificate_and_key_sign_somebody_in_with_no_secret_anywhere(
        self, tmp_path, monkeypatch, rsa_key
    ):
        idp = FakeIdp(client_secret=None)
        idp.client_public_key = rsa_key.public_key()
        oidc = OidcConfig(
            issuer=ISSUER,
            client_id="vectrixdb",
            role_map=ROLES,
            client_key=pem_key(rsa_key),
            client_cert=pem_cert(rsa_key),
        )
        with server(tmp_path, monkeypatch, oidc, idp) as client:
            assert sign_in(client, idp).headers["location"].startswith("/dashboard/")
            assert signed_in(client)
        header, claims = idp.assertions[-1]
        assert header["alg"] == "RS256" and header["x5t"] and header["x5t#S256"]
        assert claims["iss"] == claims["sub"] == "vectrixdb" and claims["aud"] == ISSUER + "/token"
        assert claims["exp"] - claims["iat"] <= 300

    def test_every_sign_in_signs_a_new_assertion(self, tmp_path, monkeypatch, rsa_key):
        idp = FakeIdp(client_secret=None)
        idp.client_public_key = rsa_key.public_key()
        oidc = OidcConfig(
            issuer=ISSUER, client_id="vectrixdb", role_map=ROLES, client_key=pem_key(rsa_key)
        )
        with server(tmp_path, monkeypatch, oidc, idp) as client:
            sign_in(client, idp)
            sign_in(client, idp)
        assert (
            len(idp.assertions) == 2 and idp.assertions[0][1]["jti"] != idp.assertions[1][1]["jti"]
        ), "a provider refuses an assertion it has seen"

    def test_an_ec_key_signs_es256_and_a_key_id_travels_as_kid(self, tmp_path, monkeypatch):
        key = ec.generate_private_key(ec.SECP256R1())
        idp = FakeIdp(client_secret=None)
        idp.client_public_key = key.public_key()
        oidc = OidcConfig(
            issuer=ISSUER,
            client_id="vectrixdb",
            role_map=ROLES,
            client_key=pem_key(key),
            client_key_id="vx-2026",
        )
        with server(tmp_path, monkeypatch, oidc, idp) as client:
            sign_in(client, idp)
            assert signed_in(client)
        header = idp.assertions[-1][0]
        assert header["alg"] == "ES256" and header["kid"] == "vx-2026" and "x5t" not in header

    def test_a_key_the_provider_does_not_know_gets_nobody_in(self, tmp_path, monkeypatch, rsa_key):
        idp = FakeIdp(client_secret=None)
        idp.client_public_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        ).public_key()
        oidc = OidcConfig(
            issuer=ISSUER, client_id="vectrixdb", role_map=ROLES, client_key=pem_key(rsa_key)
        )
        with server(tmp_path, monkeypatch, oidc, idp) as client:
            back = sign_in(client, idp)
            assert back.headers["location"].startswith(
                "/dashboard/#/signin?error="
            ) and not signed_in(client)


class TestMistakesStopTheServer:
    def test_a_key_and_a_secret_together(self, rsa_key):
        with pytest.raises(ConfigurationError, match="Choose one"):
            OidcConfig(
                issuer=ISSUER,
                client_id="vectrixdb",
                role_map=ROLES,
                client_secret="s3cret",
                client_key=pem_key(rsa_key),
            )

    def test_a_certificate_without_its_key(self, rsa_key):
        with pytest.raises(ConfigurationError, match="without VECTRIXDB_OIDC_CLIENT_KEY"):
            OidcConfig(
                issuer=ISSUER, client_id="vectrixdb", role_map=ROLES, client_cert=pem_cert(rsa_key)
            )

    def test_a_certificate_of_another_key(self, rsa_key):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with pytest.raises(ConfigurationError, match="public keys differ"):
            OidcConfig(
                issuer=ISSUER,
                client_id="vectrixdb",
                role_map=ROLES,
                client_key=pem_key(rsa_key),
                client_cert=pem_cert(other),
            )

    def test_an_expired_certificate(self, rsa_key):
        with pytest.raises(ConfigurationError, match="expired"):
            OidcConfig(
                issuer=ISSUER,
                client_id="vectrixdb",
                role_map=ROLES,
                client_key=pem_key(rsa_key),
                client_cert=pem_cert(rsa_key, days=-1, start_days_ago=30),
            )

    def test_a_certificate_about_to_run_out_is_warned_about(self, rsa_key, caplog):
        with caplog.at_level("WARNING", logger="vectrixdb.signin"):
            OidcConfig(
                issuer=ISSUER,
                client_id="vectrixdb",
                role_map=ROLES,
                client_key=pem_key(rsa_key),
                client_cert=pem_cert(rsa_key, days=10),
            )
        assert "expires in" in caplog.text

    def test_a_short_rsa_key(self):
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        with pytest.raises(ConfigurationError, match="2048 bits"):
            OidcConfig(
                issuer=ISSUER, client_id="vectrixdb", role_map=ROLES, client_key=pem_key(weak)
            )

    def test_something_that_is_not_a_key(self):
        with pytest.raises(ConfigurationError, match="not a private key"):
            OidcConfig(issuer=ISSUER, client_id="vectrixdb", role_map=ROLES, client_key="not a key")


class TestFromTheEnvironment:
    def test_the_key_can_come_from_a_file_or_with_its_newlines_escaped(self, tmp_path, rsa_key):
        path = tmp_path / "client.key"
        path.write_text(pem_key(rsa_key))
        env = {
            "VECTRIXDB_SIGNIN": "oidc",
            "VECTRIXDB_SIGNIN_SECRET": SECRET,
            "VECTRIXDB_PUBLIC_URL": PUBLIC,
            "VECTRIXDB_OIDC_ISSUER": ISSUER,
            "VECTRIXDB_OIDC_CLIENT_ID": "vectrixdb",
            "VECTRIXDB_OIDC_ROLE_MAP": '{"g-admins": "admin"}',
            "VECTRIXDB_OIDC_CLIENT_KEY_FILE": str(path),
            "VECTRIXDB_OIDC_CLIENT_KEY_ID": "k1",
        }
        config = SignInConfig.from_env(tmp_path, env)
        assert (
            config.oidc is not None
            and config.oidc._signer.algorithm == "RS256"
            and config.oidc._signer.header["kid"] == "k1"
        )
        escaped = dict(env)
        del escaped["VECTRIXDB_OIDC_CLIENT_KEY_FILE"]
        escaped["VECTRIXDB_OIDC_CLIENT_KEY"] = pem_key(rsa_key).replace("\n", "\\n")
        assert SignInConfig.from_env(tmp_path, escaped).oidc._signer is not None, (
            "some platforms hand a PEM over on one line"
        )

    def test_the_key_is_never_printed(self, rsa_key):
        oidc = OidcConfig(
            issuer=ISSUER, client_id="vectrixdb", role_map=ROLES, client_key=pem_key(rsa_key)
        )
        assert "PRIVATE KEY" not in repr(oidc)
