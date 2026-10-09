"""The vectrixdb command pointed at a server, and the safety rules that let it point anywhere.

Every command that works on a folder takes ``--url`` (or ``VECTRIXDB_URL``)
and works on a server instead, through the Python client. A real server runs
in this process on a free port, with sign-in on so named keys can be made.
The sign-in flows run against stand-ins for the identity provider: what is
held to is what this machine sends and keeps, never a provider's behaviour.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("jwt", reason="the signin extra is not installed")

from typer.testing import CliRunner  # noqa: E402

from vectrixdb.cli import app  # noqa: E402
from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.remote import (  # noqa: E402
    Login,
    Logins,
    Provider,
    clean,
    discover,
    login_with_browser,
    login_with_device,
    server_from,
)

KEY = "the-admin-api-key"
SECRET = "k" * 48
HANDBOOK = Path(__file__).resolve().parents[2] / "sdk" / "conformance" / "handbook.md"
POSIX = os.name != "nt"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """A server with sign-in on, so named keys can be made and revoked over the API."""
    import uvicorn

    from vectrixdb.api.server import create_app
    from vectrixdb.signin import SignInConfig

    data = tmp_path_factory.mktemp("db")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("VECTRIXDB_API_KEY", KEY)
        patch.delenv("VECTRIXDB_AUDIT_JSONL", raising=False)
        config = SignInConfig(
            methods=("email",),
            secrets=(SECRET,),
            public_url=url,
            users=(("ada@example.com", "admin"),),
            store_path=data / "auth" / "signin.db",
            access_log=data / "auth" / "access.jsonl",
            sender=lambda to, subject, text: None,
        )
        application = create_app(db_path=str(data), enable_dashboard=False, signin=config)
        server = uvicorn.Server(
            uvicorn.Config(application, host="127.0.0.1", port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        yield url
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """Run the command with a clean environment: no stray server, key or saved sign-in."""
    for name in list(os.environ):
        if name.startswith("VECTRIXDB_"):
            monkeypatch.delenv(name)
    # The server runs in this process and reads its own key from here at each request.
    monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
    monkeypatch.setenv("VECTRIXDB_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    def run(args, **env):
        result = runner.invoke(app, args, env={k: str(v) for k, v in env.items()})
        return result.exit_code, result.output

    return run


def flat(text: str) -> str:
    return " ".join(text.split())


# --------------------------------------------------------------- every command


def test_a_collection_made_filled_searched_and_deleted_on_a_server(served, cli, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "handbook.md").write_bytes(HANDBOOK.read_bytes())
    key = {"VECTRIXDB_KEY": KEY}

    code, out = cli(["ingest", str(folder), "--name", "handbook", "--url", served], **key)
    assert code == 0, out
    assert "Created handbook" in out and "handbook.md: 3 chunks" in flat(out)

    code, out = cli(["list", "--json"], VECTRIXDB_URL=served, **key)
    assert code == 0, out
    assert "handbook" in [c["name"] for c in json.loads(out)]

    code, out = cli(
        ["query", "how long do refunds take?", "--name", "handbook", "--json", "--url", served],
        **key,
    )
    assert code == 0, out
    first = json.loads(out)["items"][0]
    assert first["citation"] == "handbook.md#Refunds"

    code, out = cli(
        ["query", "refunds", "--name", "handbook", "--mode", "hybrid", "--url", served], **key
    )
    assert code == 0 and "ten working days" in flat(out), out

    code, out = cli(["stats", "--name", "handbook", "--json", "--url", served], **key)
    assert code == 0 and json.loads(out)["count"] >= 2, out

    code, out = cli(["info", "--json", "--url", served], **key)
    assert code == 0 and json.loads(out)["collections"] >= 1, out

    code, out = cli(["delete", "handbook", "--url", served], **key)
    assert code != 0  # no answer to the question: nothing is deleted
    code, out = cli(["delete", "handbook", "--force", "--url", served], **key)
    assert code == 0 and "Deleted handbook" in out, out
    code, out = cli(["stats", "--name", "handbook", "--url", served], **key)
    assert code == 1 and "404" in out


def test_adding_a_file_again_replaces_it(served, cli, tmp_path):
    doc = tmp_path / "policy.md"
    doc.write_text("# Leave\n\nLeave is requested two weeks ahead.\n", encoding="utf-8")
    args = ["ingest", str(doc), "--name", "again", "--url", served]
    assert cli(args, VECTRIXDB_KEY=KEY)[0] == 0
    code, out = cli(args, VECTRIXDB_KEY=KEY)
    assert code == 0 and "replaced" in out, out
    cli(["delete", "again", "--force", "--url", served], VECTRIXDB_KEY=KEY)


def test_whoami_says_who_the_server_sees_and_by_what(served, cli):
    code, out = cli(["whoami", "--url", served], VECTRIXDB_KEY=KEY)
    assert code == 0 and served in out and "VECTRIXDB_KEY" in out, out
    code, out = cli(["whoami"])
    assert code == 2 and "VECTRIXDB_URL" in out


def test_an_admin_makes_a_key_for_one_collection_and_revokes_it(served, cli, tmp_path):
    admin = {"VECTRIXDB_KEY": KEY}
    doc = tmp_path / "a.md"
    doc.write_text("# Refunds\n\nTen working days.\n", encoding="utf-8")
    assert cli(["ingest", str(doc), "--name", "scoped", "--url", served], **admin)[0] == 0

    code, out = cli(
        ["keys", "add", "handbook-bot", "--collection", "scoped", "--days", "30", "--url", served],
        **admin,
    )
    assert code == 0, out
    made = out.strip().splitlines()[-1].strip()
    assert len(made) > 20 and "scoped" in out

    code, out = cli(["keys", "list", "--json", "--url", served], **admin)
    rows = json.loads(out)
    row = next(r for r in rows if r["name"] == "handbook-bot")
    assert made not in out  # listed by name and scope; the key itself is shown once only

    bot = {"VECTRIXDB_KEY": made}
    assert cli(["query", "refunds", "--name", "scoped", "--url", served], **bot)[0] == 0
    code, out = cli(["keys", "add", "another", "--url", served], **bot)
    assert code == 1 and "refused" in out, out

    key_id = row.get("key_id") or row.get("id")
    code, out = cli(["keys", "revoke", key_id, "--url", served], **admin)
    assert code == 0 and "Revoked" in out, out
    code, out = cli(["query", "refunds", "--name", "scoped", "--url", served], **bot)
    assert code == 1 and "refused" in out
    cli(["delete", "scoped", "--force", "--url", served], **admin)


def test_a_wrong_key_is_a_refusal_with_exit_code_1(served, cli):
    code, out = cli(["list", "--url", served], VECTRIXDB_KEY="wrong")
    assert code == 1 and "refused" in out and "VECTRIXDB_KEY" in out and "wrong" not in out


def test_what_only_a_folder_has_is_refused_on_a_server(served, cli, tmp_path):
    key = {"VECTRIXDB_KEY": KEY}
    assert cli(["query", "x", "--url", served, "--parents"], **key)[0] == 2
    assert cli(["query", "x", "--url", served, "--mode", "graph"], **key)[0] == 2
    assert cli(["ingest", str(HANDBOOK), "--url", served, "--dedupe", "0.9"], **key)[0] == 2
    code, out = cli(["list", str(tmp_path), "--url", served], **key)
    assert code == 2 and "--path" in out


def test_what_a_server_sends_cannot_drive_the_terminal(served, cli, tmp_path):
    doc = tmp_path / "trick.md"
    doc.write_text("# Trick\n\nRefunds \x1b[2J\x1b]0;owned\x07 are paid [red]in red[/red].\n")
    key = {"VECTRIXDB_KEY": KEY}
    assert cli(["ingest", str(doc), "--name", "tricks", "--url", served], **key)[0] == 0
    code, out = cli(["query", "refunds", "--name", "tricks", "--url", served], **key)
    assert code == 0 and "\x1b" not in out and "\x07" not in out
    assert "[red]in" in out and "red[/red]" in out  # printed as written, not as colour
    cli(["delete", "tricks", "--force", "--url", served], **key)
    assert clean("a\x1b[31mb\x9bc\td\ne") == "a[31mbc\td\ne"


# -------------------------------------------------------- who you are, and how


def test_a_key_is_never_an_option_and_a_key_file_must_be_private(served, cli, tmp_path):
    code, out = cli(["list", "--url", served, "--key", KEY])
    assert code == 2  # there is no --key: it would sit in the shell's history
    secret = tmp_path / "key"
    secret.write_text(KEY + "\n")
    if POSIX:
        secret.chmod(0o644)
        code, out = cli(["list", "--url", served, "--key-file", str(secret)])
        assert code == 2 and "chmod 600" in out
    secret.chmod(0o600)
    code, out = cli(["list", "--url", served, "--key-file", str(secret)])
    assert code == 0, out
    code, out = cli(["list", "--key-file", str(secret)])
    assert code == 2 and "--url" in out


def test_two_credentials_at_once_are_refused_not_chosen_between(served, cli):
    code, out = cli(["list", "--url", served], VECTRIXDB_KEY=KEY, VECTRIXDB_TOKEN="t")
    assert code == 2 and "VECTRIXDB_KEY and VECTRIXDB_TOKEN" in out


def test_a_key_crosses_the_network_only_over_https(cli):
    code, out = cli(["list", "--url", "http://vectors.example.com"], VECTRIXDB_KEY="k")
    assert code == 2 and "VECTRIXDB_ALLOW_HTTP" in flat(out) and "k\n" not in out
    code, out = cli(["list", "--url", "https://user:pass@vectors.example.com"])
    assert code == 2 and "not in the address" in out
    code, out = cli(["list", "--url", "vectors.example.com"])
    assert code == 2 and "not an address" in out


def test_the_gateway_and_the_ca_come_from_the_settings(tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("not read here")
    server = server_from(
        "https://gateway.example.com",
        env={
            "VECTRIXDB_KEY": "k",
            "VECTRIXDB_KEY_HEADER": "Ocp-Apim-Subscription-Key",
            "VECTRIXDB_PREFIX": "/acme",
            "VECTRIXDB_GATEWAY_PATHS": "api/v1=/files/search",
            "VECTRIXDB_CA_BUNDLE": str(bundle),
        },
        logins=Logins(tmp_path / "none"),
    )
    assert server is not None
    assert server.options["key_header"] == "Ocp-Apim-Subscription-Key"
    assert server.options["prefix"] == "/acme" and server.options["verify"] == str(bundle)
    assert "key='k'" not in repr(server) and "token=" not in repr(server)
    with pytest.raises(ConfigurationError, match="not a file"):
        server_from(
            "https://g.example.com",
            env={"VECTRIXDB_CA_BUNDLE": str(tmp_path / "missing.pem")},
            logins=Logins(tmp_path / "none"),
        )
    assert server_from(None, env={}) is None


# --------------------------------------------------------------- saved sign-ins


def _login(url="https://vectors.example.com", **over) -> Login:
    fields = dict(
        url=url,
        issuer="https://login.example.com",
        client_id="cli",
        token_endpoint="https://login.example.com/token",
        access_token="access-1",
        expires_at=time.time() + 3600,
        refresh_token="refresh-1",
        scope="openid offline_access",
        who="ada@example.com",
    )
    fields.update(over)
    return Login(**fields)


def test_a_sign_in_is_kept_readable_by_its_owner_alone(tmp_path):
    store = Logins(tmp_path / "config")
    store.save(_login())
    if POSIX:
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.folder.stat().st_mode) == 0o700
        store.path.chmod(0o644)
        with pytest.raises(ConfigurationError, match="chmod 600"):
            store.get("https://vectors.example.com")
        store.path.chmod(0o600)
    assert "access-1" not in repr(store.get("https://vectors.example.com"))
    assert not list(store.folder.glob(".logins-*"))


def test_a_sign_in_is_sent_only_to_the_address_it_was_made_for(tmp_path):
    store = Logins(tmp_path / "config")
    store.save(_login())
    env = {}
    used = server_from("https://vectors.example.com", env=env, logins=store)
    assert used is not None and used.token == "access-1" and "saved" in used.who
    other = server_from("https://vectors.example.com.evil.test", env=env, logins=store)
    assert other is not None and other.token is None and other.key is None


def test_an_expired_sign_in_is_refreshed_once_and_kept(tmp_path):
    store = Logins(tmp_path / "config")
    store.save(_login(expires_at=time.time() - 10))
    asked = []

    def post(url, form):
        asked.append((url, dict(form)))
        return {"access_token": "access-2", "expires_in": 3600}

    fresh = store.fresh("https://vectors.example.com", post=post)
    assert fresh is not None and fresh.access_token == "access-2"
    assert fresh.refresh_token == "refresh-1"  # kept when the provider sends no new one
    assert asked[0][0] == "https://login.example.com/token"
    assert asked[0][1]["grant_type"] == "refresh_token"
    assert store.get("https://vectors.example.com").access_token == "access-2"
    store.save(_login(expires_at=time.time() - 10, refresh_token=""))
    with pytest.raises(ConfigurationError, match="vectrixdb login"):
        store.fresh("https://vectors.example.com", post=post)


def test_logout_forgets_one_or_every_sign_in(cli, tmp_path):
    store = Logins(tmp_path / "config")
    store.save(_login())
    store.save(_login(url="https://other.example.com"))
    code, out = cli(["logout", "--url", "https://vectors.example.com"])
    assert code == 0 and "Forgot" in out
    assert store.urls() == ["https://other.example.com"]
    code, out = cli(["logout", "--all"])
    assert code == 0 and store.urls() == []


def test_login_needs_the_command_lines_client_id(cli):
    code, out = cli(["login", "--url", "https://vectors.example.com"])
    assert code == 2 and "VECTRIXDB_LOGIN_CLIENT_ID" in flat(out)


# --------------------------------------------------------------- signing in


PROVIDER = Provider(
    issuer="https://login.example.com",
    scopes=["api://vectors/.default"],
    authorization_endpoint="https://login.example.com/authorize",
    token_endpoint="https://login.example.com/token",
    device_authorization_endpoint="https://login.example.com/device",
)


def test_the_server_names_the_provider_and_the_provider_must_agree():
    answers = {
        "https://v.example.com/.well-known/oauth-protected-resource": {
            "authorization_servers": ["https://login.example.com"],
            "scopes_supported": ["api://vectors/.default"],
        },
        "https://login.example.com/.well-known/openid-configuration": {
            "issuer": "https://login.example.com",
            "authorization_endpoint": "https://login.example.com/authorize",
            "token_endpoint": "https://login.example.com/token",
        },
    }
    found = discover("https://v.example.com", get=lambda url, what: answers[url])
    assert found.scopes == ["api://vectors/.default"]
    assert found.token_endpoint == "https://login.example.com/token"

    answers["https://login.example.com/.well-known/openid-configuration"]["issuer"] = (
        "https://evil.test"
    )
    with pytest.raises(ConfigurationError, match="not who the server named"):
        discover("https://v.example.com", get=lambda url, what: answers[url])

    answers["https://v.example.com/.well-known/oauth-protected-resource"] = {
        "authorization_servers": ["http://login.example.com"]
    }
    with pytest.raises(ConfigurationError, match="https"):
        discover("https://v.example.com", get=lambda url, what: answers[url])

    answers["https://v.example.com/.well-known/oauth-protected-resource"] = {
        "authorization_servers": []
    }
    with pytest.raises(ConfigurationError, match="API key"):
        discover("https://v.example.com", get=lambda url, what: answers[url])


def test_a_device_sign_in_waits_slows_down_and_keeps_the_tokens():
    answers = iter(
        [
            {
                "device_code": "dev-1",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://login.example.com/device",
                "interval": 1,
            },
            {"error": "authorization_pending"},
            {"error": "slow_down"},
            {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 600},
        ]
    )
    sent, said, waits = [], [], []

    def post(url, form):
        sent.append((url, dict(form)))
        return next(answers)

    login = login_with_device(
        "https://v.example.com",
        "cli",
        provider=PROVIDER,
        say=said.append,
        post=post,
        sleep=waits.append,
    )
    assert "ABCD-EFGH" in said[0] and "https://login.example.com/device" in said[0]
    assert sent[0][1]["scope"].split()[:3] == ["openid", "profile", "offline_access"]
    assert "api://vectors/.default" in sent[0][1]["scope"]
    assert sent[1][1]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert waits == [1.0, 1.0, 6.0]
    assert login.access_token == "access-1" and login.refresh_token == "refresh-1"
    assert login.url == "https://v.example.com" and login.issuer == "https://login.example.com"


def test_a_browser_sign_in_checks_the_state_and_sends_the_verifier():
    import httpx

    sent = []

    def open_browser(address):
        query = {k: v[0] for k, v in parse_qs(urlsplit(address).query).items()}
        assert query["code_challenge_method"] == "S256" and query["response_type"] == "code"
        redirect = query["redirect_uri"]
        assert redirect.startswith("http://localhost:")

        def answer():
            # A request with another state is turned away, and the real one still lands.
            httpx.get(redirect.replace("localhost", "127.0.0.1") + "?code=x&state=forged")
            httpx.get(
                redirect.replace("localhost", "127.0.0.1")
                + f"?code=the-code&state={query['state']}"
            )

        threading.Thread(target=answer, daemon=True).start()
        sent.append(query)
        return True

    def post(url, form):
        sent.append(dict(form))
        return {"access_token": "access-1", "expires_in": 600}

    login = login_with_browser(
        "https://v.example.com",
        "cli",
        provider=PROVIDER,
        open_browser=open_browser,
        say=lambda t: None,
        post=post,
        wait=10,
    )
    exchange = sent[-1]
    assert exchange["grant_type"] == "authorization_code" and exchange["code"] == "the-code"
    assert (
        len(exchange["code_verifier"]) >= 43 and exchange["redirect_uri"] == sent[0]["redirect_uri"]
    )
    assert login.access_token == "access-1"


def test_a_refused_browser_sign_in_says_what_the_provider_said():
    import httpx

    def open_browser(address):
        query = {k: v[0] for k, v in parse_qs(urlsplit(address).query).items()}
        target = query["redirect_uri"].replace("localhost", "127.0.0.1")
        threading.Thread(
            target=lambda: httpx.get(
                f"{target}?error=access_denied&error_description=Not+in+the+group%1b[2J&state={query['state']}"
            ),
            daemon=True,
        ).start()
        return True

    with pytest.raises(ConfigurationError, match="Not in the group") as refused:
        login_with_browser(
            "https://v.example.com",
            "cli",
            provider=PROVIDER,
            open_browser=open_browser,
            say=lambda t: None,
            wait=10,
        )
    assert "\x1b" not in str(refused.value)
