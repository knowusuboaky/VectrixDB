"""The command line against a server: --server, login with a key or a company sign-in, whoami, logout.

Every call goes to a real server in this process; the identity provider is a
fake one, answering the device code flow as RFC 8628 says. No network.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import types

import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from vectrixdb import cli_remote  # noqa: E402
from vectrixdb.cli import app  # noqa: E402
from vectrixdb.client import Client, tls  # noqa: E402

KEY = "the-key"
READ_ONLY = "the-read-only-key"
SERVER = "http://localhost:7337"
POSIX = os.name != "nt"


@pytest.fixture(autouse=True)
def _quiet_environment(monkeypatch, tmp_path):
    for name in (
        "VECTRIXDB_URL",
        "VECTRIXDB_KEY",
        "VECTRIXDB_TOKEN",
        "VECTRIXDB_CA_FILE",
        "VECTRIXDB_ALLOW_HTTP",
        "VECTRIXDB_CLIENT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
    monkeypatch.setenv("VECTRIXDB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("VECTRIXDB_CREDENTIALS", "file")


class _Kept:
    """The server's test client, left open when a command closes its client."""

    def __init__(self, http):
        self._http = http

    def request(self, *args, **kwargs):
        return self._http.request(*args, **kwargs)

    def get(self, *args, **kwargs):
        return self._http.get(*args, **kwargs)

    def post(self, *args, **kwargs):
        return self._http.post(*args, **kwargs)

    def close(self):
        pass


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A real server; every client the command line makes talks to it."""
    from vectrixdb.api.server import create_app

    monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
    monkeypatch.setenv("VECTRIXDB_READ_ONLY_API_KEY", READ_ONLY)
    monkeypatch.setenv("VECTRIXDB_KEEP_SOURCE", "1")
    # Closed, as a server on a network should be: every call needs a key.
    monkeypatch.setenv("VECTRIXDB_OPEN_READS", "0")
    application = create_app(db_path=str(tmp_path / "server"), enable_dashboard=False)
    with TestClient(application, base_url=SERVER) as http:
        made = []

        def client(**options):
            made.append(options)
            return _Kept(http)

        shim = types.SimpleNamespace(
            Client=client, TransportError=httpx.TransportError, AsyncClient=None
        )
        monkeypatch.setattr("vectrixdb.client._httpx", lambda: shim)
        yield made


@pytest.fixture
def run():
    runner = CliRunner()

    def invoke(*args, **kwargs):
        return runner.invoke(app, list(args), **kwargs)

    return invoke


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "key"
    path.write_text(KEY + "\n", encoding="utf-8")
    if POSIX:
        path.chmod(0o600)
    return str(path)


# --- the client refuses to send a key where it should not ----------------------


class TestAKeyNeverCrossesANetworkInClearText:
    def test_plain_http_to_another_machine_is_refused_with_a_key(self):
        with pytest.raises(ValueError, match="clear text"):
            Client("http://vectors.example.com", key="k")

    def test_plain_http_to_another_machine_is_refused_with_a_token(self):
        with pytest.raises(ValueError, match="clear text"):
            Client("http://vectors.example.com", token=lambda: "t")

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:7337",
            "http://127.0.0.1:7337",
            "http://[::1]:7337",
            "http://app.localhost",
        ],
    )
    def test_this_machine_over_http_is_fine(self, url):
        Client(url, key="k").close()

    def test_allow_http_lifts_it_for_a_network_you_trust(self):
        Client("http://vectors.internal", key="k", allow_http=True).close()

    def test_with_no_caller_plain_http_sends_nothing_to_protect(self):
        Client("http://vectors.example.com").close()

    def test_a_key_in_the_address_is_refused(self):
        with pytest.raises(ValueError, match="not in the address"):
            Client("https://user:secret@vectors.example.com")

    def test_an_address_that_is_not_http_is_refused(self):
        with pytest.raises(ValueError, match="https://"):
            Client("ftp://vectors.example.com", key="k")

    def test_redirects_are_not_followed_and_the_client_names_itself(self, monkeypatch):
        seen = {}

        def client(**options):
            seen.update(options)
            return _Kept(None)

        monkeypatch.setattr(
            "vectrixdb.client._httpx",
            lambda: types.SimpleNamespace(Client=client, TransportError=httpx.TransportError),
        )
        Client("https://vectors.example.com", key="k")
        assert seen["follow_redirects"] is False
        assert seen["headers"]["User-Agent"].startswith("vectrixdb-python")

    def test_a_redirect_is_reported_not_followed(self):
        from vectrixdb.exceptions import ServerRefused

        asked = []

        def answer(request):
            asked.append(str(request.url))
            return httpx.Response(302, headers={"location": "https://elsewhere.example/api"})

        http = httpx.Client(transport=httpx.MockTransport(answer), base_url="https://v.example")
        with pytest.raises(ServerRefused, match="elsewhere.example"):
            Client("https://v.example", key="k", http=http).collections()
        assert asked == ["https://v.example/api/v1/collections"]


class TestCertificates:
    def test_system_is_the_operating_systems_store(self):
        pytest.importorskip("truststore")
        assert isinstance(tls("system"), ssl.SSLContext)

    def test_a_missing_file_is_named(self, tmp_path):
        with pytest.raises(ValueError, match="no certificate file"):
            tls(str(tmp_path / "company-ca.pem"))

    def test_true_and_false_pass_through(self):
        assert tls(True) is True and tls(False) is False

    def test_a_folder_of_certificates_is_a_context(self, tmp_path):
        assert isinstance(tls(str(tmp_path)), ssl.SSLContext)


# --- where a sign-in is kept -----------------------------------------------------


class TestCredentials:
    def test_kept_read_back_and_forgotten(self, tmp_path):
        store = cli_remote.Credentials(tmp_path / "c", store="file")
        store.put("https://Vectors.Company.com/", {"kind": "key", "key": "k1"})
        assert store.get("https://vectors.company.com") == {"kind": "key", "key": "k1"}
        assert store.last() == "https://Vectors.Company.com"
        assert store.servers() == ["https://vectors.company.com"]
        assert store.forget("https://vectors.company.com") is True
        assert store.get("https://vectors.company.com") is None and store.last() is None
        assert store.forget("https://vectors.company.com") is False

    @pytest.mark.skipif(not POSIX, reason="file modes are POSIX")
    def test_only_this_user_may_read_the_file(self, tmp_path):
        store = cli_remote.Credentials(tmp_path / "c", store="file")
        store.put("https://v.example", {"kind": "key", "key": "k1"})
        assert store.path.stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "c").stat().st_mode & 0o777 == 0o700

    def test_with_a_keychain_the_file_holds_no_secret(self, tmp_path, monkeypatch):
        kept = {}
        fake = types.SimpleNamespace(
            set_password=lambda service, name, value: kept.__setitem__((service, name), value),
            get_password=lambda service, name: kept.get((service, name)),
            delete_password=lambda service, name: kept.pop((service, name)),
        )
        monkeypatch.setattr(cli_remote, "_keyring", lambda: fake)
        store = cli_remote.Credentials(tmp_path / "c", store="keyring")
        store.put(
            "https://v.example",
            {"kind": "token", "access_token": "a", "refresh_token": "r", "expires_at": 9},
        )
        written = store.path.read_text(encoding="utf-8")
        assert '"a"' not in written and '"r"' not in written and "keyring" in written
        assert store.get("https://v.example")["refresh_token"] == "r"
        assert store.where == "the system keychain"
        store.forget("https://v.example")
        assert kept == {}

    def test_keyring_asked_for_and_missing_is_said(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli_remote, "_keyring", lambda: None)
        with pytest.raises(cli_remote.SignInError, match="pip install keyring"):
            cli_remote.Credentials(tmp_path, store="keyring")

    def test_the_platforms_own_folder(self, monkeypatch):
        monkeypatch.delenv("VECTRIXDB_CONFIG_DIR")
        folder = cli_remote.config_dir()
        assert folder.name == "vectrixdb"


class TestKeyFiles:
    def test_the_first_line_trimmed(self, tmp_path):
        path = tmp_path / "k"
        path.write_text("  abc  \nignored\n", encoding="utf-8")
        if POSIX:
            path.chmod(0o600)
        assert cli_remote.read_key_file(str(path)) == "abc"

    @pytest.mark.skipif(not POSIX, reason="file modes are POSIX")
    def test_a_file_others_may_read_is_refused(self, tmp_path):
        path = tmp_path / "k"
        path.write_text("abc", encoding="utf-8")
        path.chmod(0o644)
        with pytest.raises(cli_remote.SignInError, match="chmod 600"):
            cli_remote.read_key_file(str(path))

    def test_an_empty_or_missing_file_is_said(self, tmp_path):
        with pytest.raises(cli_remote.SignInError, match="no key file"):
            cli_remote.read_key_file(str(tmp_path / "nope"))
        empty = tmp_path / "empty"
        empty.write_text("", encoding="utf-8")
        if POSIX:
            empty.chmod(0o600)
        with pytest.raises(cli_remote.SignInError, match="holds no key"):
            cli_remote.read_key_file(str(empty))

    def test_stdin(self, monkeypatch):
        import io

        monkeypatch.setattr(sys, "stdin", io.StringIO("from-stdin\n"))
        assert cli_remote.read_key_file("-") == "from-stdin"


# --- the commands, against a server ------------------------------------------------


class TestCommandsOnAServer:
    def test_create_ingest_list_query_stats(self, server, run, key_file, tmp_path):
        assert (
            run("create", "handbook", "384", "--server", SERVER, "--key-file", key_file).exit_code
            == 0
        )
        doc = tmp_path / "refunds.md"
        doc.write_text(
            "# Refunds\n\nRefunds are paid by the billing team within ten working days.\n",
            encoding="utf-8",
        )
        ingested = run(
            "ingest", str(doc), "--name", "handbook", "--server", SERVER, "--key-file", key_file
        )
        assert ingested.exit_code == 0, ingested.output
        assert "on http://localhost:7337" in ingested.output

        listed = run("list", "--server", SERVER, "--key-file", key_file, "--json")
        assert listed.exit_code == 0, listed.output
        assert "handbook" in [c["name"] for c in json.loads(listed.stdout)]

        found = run(
            "query",
            "when are refunds paid",
            "--name",
            "handbook",
            "--server",
            SERVER,
            "--key-file",
            key_file,
            "--json",
        )
        assert found.exit_code == 0, found.output
        assert "ten working days" in found.stdout

        stats = run("stats", "--name", "handbook", "--server", SERVER, "--key-file", key_file)
        assert stats.exit_code == 0 and "handbook" in stats.output

    def test_the_server_is_named_on_stderr(self, server, run, key_file):
        result = run("list", "--server", SERVER, "--key-file", key_file, "--json")
        assert f"server {SERVER}" in result.stderr
        assert SERVER not in result.stdout.split("\n")[0]

    def test_vectrixdb_url_and_key_from_the_environment(self, server, run, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_URL", SERVER)
        monkeypatch.setenv("VECTRIXDB_KEY", KEY)
        result = run("list", "--json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == []

    def test_ingest_makes_a_new_collection(self, server, run, key_file, tmp_path):
        doc = tmp_path / "travel.md"
        doc.write_text(
            "# Travel\n\nEconomy class, booked by the office manager.\n", encoding="utf-8"
        )
        result = run(
            "ingest", str(doc), "--name", "travel", "--server", SERVER, "--key-file", key_file
        )
        assert result.exit_code == 0, result.output
        assert "Made the collection 'travel'" in result.output

    def test_no_caller_is_told_how_to_sign_in(self, server, run):
        result = run("list", "--server", SERVER)
        assert result.exit_code == 1
        assert "vectrixdb login --server http://localhost:7337" in result.output

    def test_a_refusal_is_one_line_and_exit_1(self, server, run, tmp_path):
        reader = tmp_path / "reader"
        reader.write_text(READ_ONLY, encoding="utf-8")
        if POSIX:
            reader.chmod(0o600)
        result = run("delete", "anything", "--force", "--server", SERVER, "--key-file", str(reader))
        assert result.exit_code == 1
        assert "The server said no (HTTP 403)" in result.output
        assert "Traceback" not in result.output

    def test_plain_http_elsewhere_stops_before_sending(self, run, key_file):
        result = run("list", "--server", "http://vectors.example.com", "--key-file", key_file)
        assert result.exit_code == 2
        assert "clear text" in result.output

    def test_local_only_options_are_refused_with_a_server(self, server, run, key_file):
        result = run("query", "x", "--explain", "--server", SERVER, "--key-file", key_file)
        assert result.exit_code == 2 and "--explain" in result.output

    def test_delete_asks_and_names_the_server(self, server, run, key_file):
        result = run("delete", "handbook", "--server", SERVER, "--key-file", key_file, input="n\n")
        assert "on http://localhost:7337" in result.output
        assert result.exit_code != 0

    def test_a_ca_file_that_is_not_there_is_said(
        self, server, run, key_file, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("VECTRIXDB_CA_FILE", str(tmp_path / "company-ca.pem"))
        result = run("list", "--server", SERVER, "--key-file", key_file)
        assert result.exit_code == 2 and "no certificate file" in result.output


class TestLoginWithAKey:
    def test_login_keeps_it_whoami_uses_it_logout_forgets_it(self, server, run, key_file):
        signed = run("login", "--server", SERVER, "--key-file", key_file)
        assert signed.exit_code == 0, signed.output
        assert "Role: admin" in signed.output
        assert KEY not in signed.output

        who = run("whoami")
        assert who.exit_code == 0, who.output
        assert "Server: http://localhost:7337" in who.output and "Role: admin" in who.output

        listed = run("list", "--server", SERVER, "--json")
        assert listed.exit_code == 0, listed.output

        assert "Forgot the sign-in" in run("logout").output
        assert run("list", "--server", SERVER).exit_code == 1

    def test_a_wrong_key_is_not_kept(self, server, run, tmp_path):
        wrong = tmp_path / "wrong"
        wrong.write_text("not-the-key", encoding="utf-8")
        if POSIX:
            wrong.chmod(0o600)
        result = run("login", "--server", SERVER, "--key-file", str(wrong))
        assert result.exit_code == 1
        assert cli_remote.Credentials().servers() == []

    def test_whoami_json(self, server, run, key_file):
        result = run("whoami", "--server", SERVER, "--key-file", key_file, "--json")
        assert json.loads(result.stdout)["role"] == "admin"


# --- a person's sign-in with the company's identity provider --------------------------


ISSUER = "https://login.company.example"


class _Provider:
    """The server's protected-resource document, and an identity provider doing the device flow."""

    def __init__(self, answers, issuers=(ISSUER,), device=True):
        self.answers = list(answers)
        self.issuers = list(issuers)
        self.device = device
        self.asked = []

    def __call__(self, request):
        url = str(request.url)
        body = dict(httpx.QueryParams(request.content.decode())) if request.content else {}
        self.asked.append((url, body))
        if url.endswith("/.well-known/oauth-protected-resource"):
            return httpx.Response(
                200,
                json={
                    "authorization_servers": self.issuers,
                    "scopes_supported": ["api://vx/search"],
                },
            )
        if url == ISSUER + "/.well-known/openid-configuration":
            found = {"token_endpoint": ISSUER + "/token"}
            if self.device:
                found["device_authorization_endpoint"] = ISSUER + "/device"
            return httpx.Response(200, json=found)
        if url == ISSUER + "/device":
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-1",
                    "user_code": "WDJB-MJHT",
                    "verification_uri": ISSUER + "/activate",
                    "interval": 1,
                    "expires_in": 600,
                },
            )
        if url == ISSUER + "/token":
            status, payload = self.answers.pop(0)
            return httpx.Response(status, json=payload)
        if url.endswith("/api/v1/whoami"):
            assert request.headers["authorization"].startswith("Bearer ")
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {"who": "ama@company.example", "role": "viewer", "method": "oidc"},
                },
            )
        return httpx.Response(404)


@pytest.fixture
def provider(monkeypatch):
    holder = {}

    def install(answers, **options):
        fake = _Provider(answers, **options)
        holder["fake"] = fake

        def client(**kwargs):
            return httpx.Client(
                transport=httpx.MockTransport(fake), base_url=kwargs.get("base_url", "")
            )

        monkeypatch.setattr(
            "vectrixdb.client._httpx",
            lambda: types.SimpleNamespace(Client=client, TransportError=httpx.TransportError),
        )
        monkeypatch.setattr(cli_remote, "_http", lambda verify, timeout=30.0: client())
        return fake

    return install


class TestDeviceSignIn:
    def test_pending_then_slow_down_then_signed_in(self, provider, tmp_path):
        fake = provider(
            [
                (400, {"error": "authorization_pending"}),
                (400, {"error": "slow_down"}),
                (200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}),
            ]
        )
        said, waits = [], []
        store = cli_remote.Credentials(tmp_path / "c", store="file")
        who = cli_remote.login_with_device(
            "https://vectors.company.example",
            "cli-app",
            say=said.append,
            store=store,
            sleep=waits.append,
            now=lambda: 1000.0,
        )
        assert who["who"] == "ama@company.example"
        assert said == [f"Open {ISSUER}/activate and enter the code WDJB-MJHT"]
        assert waits == [1.0, 1.0, 6.0]
        device_ask = next(body for url, body in fake.asked if url.endswith("/device"))
        assert device_ask["client_id"] == "cli-app"
        assert "api://vx/search" in device_ask["scope"] and "offline_access" in device_ask["scope"]
        kept = store.get("https://vectors.company.example")
        assert (
            kept["kind"] == "token"
            and kept["access_token"] == "at-1"
            and kept["expires_at"] == 4600.0
        )

    def test_declined_in_the_browser(self, provider, tmp_path):
        provider([(400, {"error": "access_denied"})])
        with pytest.raises(cli_remote.SignInError, match="declined"):
            cli_remote.login_with_device(
                "https://v.example",
                "cli-app",
                say=lambda _: None,
                store=cli_remote.Credentials(tmp_path, store="file"),
                sleep=lambda _: None,
            )

    def test_a_server_with_keys_only_says_so(self, provider, tmp_path):
        provider([], issuers=())
        with pytest.raises(cli_remote.SignInError, match="takes keys only"):
            cli_remote.login_with_device(
                "https://v.example",
                "cli-app",
                say=lambda _: None,
                store=cli_remote.Credentials(tmp_path, store="file"),
            )

    def test_a_provider_without_the_device_flow_says_so(self, provider, tmp_path):
        provider([], device=False)
        with pytest.raises(cli_remote.SignInError, match="device code flow"):
            cli_remote.login_with_device(
                "https://v.example",
                "cli-app",
                say=lambda _: None,
                store=cli_remote.Credentials(tmp_path, store="file"),
            )

    def test_a_provider_over_plain_http_is_refused(self, provider, tmp_path):
        provider([], issuers=("http://login.company.example",))
        with pytest.raises(cli_remote.SignInError, match="must be https"):
            cli_remote.login_with_device(
                "https://v.example",
                "cli-app",
                say=lambda _: None,
                store=cli_remote.Credentials(tmp_path, store="file"),
            )

    def test_no_client_id_is_said(self, tmp_path):
        with pytest.raises(cli_remote.SignInError, match="VECTRIXDB_CLIENT_ID"):
            cli_remote.login_with_device("https://v.example", "", say=lambda _: None)

    def test_a_sign_in_running_out_is_renewed_and_kept(self, provider, tmp_path):
        fake = provider([(200, {"access_token": "at-2", "expires_in": 3600})])
        store = cli_remote.Credentials(tmp_path, store="file")
        entry = {
            "kind": "token",
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "expires_at": 1030.0,
            "client_id": "cli-app",
            "token_endpoint": ISSUER + "/token",
            "scope": "api://vx/search offline_access",
        }
        store.put("https://v.example", entry)
        token = cli_remote._token_function(
            "https://v.example", entry, store, True, now=lambda: 1000.0
        )
        assert token() == "at-2"
        asked = fake.asked[-1][1]
        assert asked["grant_type"] == "refresh_token" and asked["refresh_token"] == "rt-1"
        kept = store.get("https://v.example")
        # Not rotated by the provider: the refresh token there was is kept.
        assert kept["access_token"] == "at-2" and kept["refresh_token"] == "rt-1"

    def test_a_fresh_sign_in_is_not_renewed(self, provider, tmp_path):
        fake = provider([])
        entry = {"kind": "token", "access_token": "at-1", "expires_at": 5000.0}
        token = cli_remote._token_function(
            "https://v.example",
            entry,
            cli_remote.Credentials(tmp_path, store="file"),
            True,
            now=lambda: 1000.0,
        )
        assert token() == "at-1" and fake.asked == []

    def test_run_out_with_nothing_to_renew_says_login(self, tmp_path):
        entry = {"kind": "token", "access_token": "at-1", "expires_at": 10.0}
        token = cli_remote._token_function(
            "https://v.example",
            entry,
            cli_remote.Credentials(tmp_path, store="file"),
            True,
            now=lambda: 1000.0,
        )
        with pytest.raises(cli_remote.SignInError, match="vectrixdb login"):
            token()

    def test_the_login_command_prints_the_code_and_who(self, provider, run, monkeypatch):
        provider([(200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600})])
        monkeypatch.setattr(cli_remote.time, "sleep", lambda _: None)
        result = run(
            "login", "--server", "https://vectors.company.example", "--client-id", "cli-app"
        )
        assert result.exit_code == 0, result.output
        assert "WDJB-MJHT" in result.output and "Signed in as: ama@company.example" in result.output
        assert "at-1" not in result.output and "rt-1" not in result.output
