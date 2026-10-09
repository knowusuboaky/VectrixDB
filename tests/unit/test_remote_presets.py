"""A company's presets for the command and the client: a machine-wide file, the wrapper's
command in every hint, its name in User-Agent, sign-ins in the keychain, and the
operating system's certificates.

No network, no keychain and no machine-wide file of the machine running the
tests: each is a stand-in or a temporary file.
"""

from __future__ import annotations

import json
import os
import ssl
import types

import pytest

httpx = pytest.importorskip("httpx")

from typer.testing import CliRunner  # noqa: E402

from vectrixdb import remote  # noqa: E402
from vectrixdb.cli import app  # noqa: E402
from vectrixdb.client import VectrixClient  # noqa: E402
from vectrixdb.exceptions import ConfigurationError  # noqa: E402

POSIX = os.name != "nt"
#: The platform's own place, before a test points it somewhere nothing is.
REAL_MACHINE_DEFAULTS = remote.machine_defaults


@pytest.fixture(autouse=True)
def _a_clean_environment(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("VECTRIXDB_"):
            monkeypatch.delenv(name)
    # The platform's own presets file is somewhere nothing is.
    monkeypatch.setenv("VECTRIXDB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(remote, "machine_defaults", _nowhere(tmp_path, remote.machine_defaults))


def _nowhere(tmp_path, real):
    def where(env=None):
        values = os.environ if env is None else env
        if values.get("VECTRIXDB_DEFAULTS_FILE"):
            return real(values)
        return tmp_path / "no-presets-here.env"

    return where


def _presets(tmp_path, monkeypatch, text):
    path = tmp_path / "defaults.env"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("VECTRIXDB_DEFAULTS_FILE", str(path))
    return path


def _seen():
    """A transport that answers whoami and keeps every request."""
    asked = []

    def answer(request):
        asked.append(request)
        return httpx.Response(200, json={"ok": True, "data": {"who": "ama", "role": "viewer"}})

    return asked, httpx.MockTransport(answer)


class TestTheMachineWidePresets:
    def test_a_preset_is_taken_and_the_environment_wins(self, tmp_path, monkeypatch):
        _presets(
            tmp_path,
            monkeypatch,
            "VECTRIXDB_URL=https://vectors.acme.example\nVECTRIXDB_COMMAND=acme vectors\n",
        )
        monkeypatch.setenv("VECTRIXDB_COMMAND", "mine")
        environ = dict(os.environ)
        taken = remote.apply_machine_defaults(environ)
        assert taken == {"VECTRIXDB_URL": "https://vectors.acme.example"}
        assert environ["VECTRIXDB_COMMAND"] == "mine"

    def test_only_vectrixdb_settings(self, tmp_path, monkeypatch):
        _presets(tmp_path, monkeypatch, "HTTPS_PROXY=http://evil.example:8080\n")
        with pytest.raises(ConfigurationError, match="VECTRIXDB_ settings alone"):
            remote.apply_machine_defaults(dict(os.environ))

    def test_a_named_file_that_is_not_there_is_said(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_DEFAULTS_FILE", str(tmp_path / "nope.env"))
        with pytest.raises(ConfigurationError, match="not a file"):
            remote.apply_machine_defaults(dict(os.environ))

    def test_no_file_in_the_platforms_place_is_nothing(self):
        assert remote.apply_machine_defaults(dict(os.environ)) == {}

    def test_the_platforms_place(self):
        found = REAL_MACHINE_DEFAULTS({})
        assert found.name == "defaults.env" and found.parent.name == "vectrixdb"

    def test_every_command_reads_them_first(self, tmp_path, monkeypatch):
        _presets(tmp_path, monkeypatch, "PATH=/tmp\n")
        result = CliRunner().invoke(app, ["version"])
        assert result.exit_code == 2
        assert "VECTRIXDB_ settings alone" in result.output


class TestTheWrappersCommandInHints:
    def test_unset_is_vectrixdb(self):
        assert remote.command() == "vectrixdb"

    def test_a_hint_names_the_wrappers_command(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VECTRIXDB_COMMAND", "acme vectors")
        logins = remote.Logins(tmp_path / "logins", keychain=None)
        logins.save(
            remote.Login(
                url="https://vectors.acme.example",
                issuer="https://login.acme.example",
                client_id="cli",
                token_endpoint="https://login.acme.example/token",
                access_token="at",
                expires_at=1.0,
            )
        )
        with pytest.raises(ConfigurationError, match="acme vectors login --url"):
            logins.fresh("https://vectors.acme.example")

    def test_control_characters_are_taken_out(self, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_COMMAND", "acme\x1b[31m vectors")
        assert remote.command() == "acme[31m vectors"


class TestTheWrappersNameInUserAgent:
    def test_it_goes_before_the_clients_own(self):
        asked, transport = _seen()
        VectrixClient(
            "https://v.example", key="k", user_agent="acme-vectors/1.4", transport=transport
        ).whoami()
        agent = asked[0].headers["user-agent"]
        assert agent.startswith("acme-vectors/1.4 vectrixdb-python/")

    def test_without_one_it_is_the_clients_own(self):
        asked, transport = _seen()
        VectrixClient("https://v.example", key="k", transport=transport).whoami()
        assert asked[0].headers["user-agent"].startswith("vectrixdb-python/")

    def test_a_line_break_in_it_is_refused(self):
        with pytest.raises(ConfigurationError, match="user agent"):
            VectrixClient("https://v.example", key="k", user_agent="acme\r\nX-Evil: 1")

    def test_the_command_reads_it_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_USER_AGENT", "acme-vectors/1.4")
        monkeypatch.setenv("VECTRIXDB_KEY", "k")
        server = remote.server_from("https://v.example")
        assert server.options["user_agent"] == "acme-vectors/1.4"


class TestTheSystemsCertificates:
    def test_system_is_the_operating_systems_store(self):
        pytest.importorskip("truststore")
        from vectrixdb.client import _tls

        assert isinstance(_tls("system", None), ssl.SSLContext)

    def test_the_command_takes_system_as_its_bundle(self, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_CA_BUNDLE", "system")
        monkeypatch.setenv("VECTRIXDB_KEY", "k")
        assert remote.server_from("https://v.example").options["verify"] == "system"

    def test_a_bundle_that_is_not_there_is_still_refused(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VECTRIXDB_CA_BUNDLE", str(tmp_path / "company-ca.pem"))
        with pytest.raises(ConfigurationError, match="not a file"):
            remote.server_from("https://v.example")


class _Keychain:
    """The keyring module's three calls, kept in a dict."""

    def __init__(self):
        self.kept = {}

    def set_password(self, service, name, value):
        self.kept[(service, name)] = value

    def get_password(self, service, name):
        return self.kept.get((service, name))

    def delete_password(self, service, name):
        del self.kept[(service, name)]


def _login(**extra):
    return remote.Login(
        url="https://vectors.acme.example",
        issuer="https://login.acme.example",
        client_id="cli",
        token_endpoint="https://login.acme.example/token",
        access_token="the-access-token",
        refresh_token="the-refresh-token",
        expires_at=4102444800.0,
        **extra,
    )


class TestSignInsInTheKeychain:
    def test_the_file_holds_no_token(self, tmp_path):
        keychain = _Keychain()
        logins = remote.Logins(tmp_path, keychain=keychain)
        logins.save(_login())
        written = (tmp_path / "logins.json").read_text(encoding="utf-8")
        assert "the-access-token" not in written and "the-refresh-token" not in written
        assert json.loads(written)["https://vectors.acme.example"]["in"] == "keychain"
        kept = json.loads(keychain.kept[("vectrixdb", "https://vectors.acme.example")])
        assert kept == {"access_token": "the-access-token", "refresh_token": "the-refresh-token"}

    def test_it_reads_back_whole_and_logout_takes_it_from_the_keychain(self, tmp_path):
        keychain = _Keychain()
        logins = remote.Logins(tmp_path, keychain=keychain)
        logins.save(_login())
        found = logins.get("https://vectors.acme.example")
        assert found.access_token == "the-access-token"
        assert found.refresh_token == "the-refresh-token"
        assert logins.remove("https://vectors.acme.example") is True
        assert keychain.kept == {}

    def test_a_keychain_sign_in_without_the_keychain_says_how(self, tmp_path):
        remote.Logins(tmp_path, keychain=_Keychain()).save(_login())
        with pytest.raises(ConfigurationError, match="VECTRIXDB_CREDENTIALS=keyring"):
            remote.Logins(tmp_path, keychain=None).get("https://vectors.acme.example")

    def test_the_file_is_the_default(self, tmp_path):
        logins = remote.Logins(tmp_path)
        assert logins.keychain is None
        logins.save(_login())
        assert "the-access-token" in (tmp_path / "logins.json").read_text(encoding="utf-8")

    def test_keyring_asked_for_and_not_installed_is_said(self, monkeypatch):
        import builtins

        real = builtins.__import__

        def no_keyring(name, *args, **kwargs):
            if name.startswith("keyring"):
                raise ImportError(name)
            return real(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_keyring)
        with pytest.raises(ConfigurationError, match="pip install keyring"):
            remote._keychain({"VECTRIXDB_CREDENTIALS": "keyring"})

    def test_a_keychain_with_nothing_behind_it_is_said(self, monkeypatch):
        class Fail:
            pass

        fake = types.SimpleNamespace(get_keyring=lambda: Fail())
        backends = types.SimpleNamespace(fail=types.SimpleNamespace(Keyring=Fail))
        monkeypatch.setitem(__import__("sys").modules, "keyring", fake)
        monkeypatch.setitem(__import__("sys").modules, "keyring.backends", backends)
        monkeypatch.setitem(__import__("sys").modules, "keyring.backends.fail", backends.fail)
        fake.backends = backends
        with pytest.raises(ConfigurationError, match="no keychain"):
            remote._keychain({"VECTRIXDB_CREDENTIALS": "keyring"})

    def test_any_other_value_is_refused(self):
        with pytest.raises(ConfigurationError, match="keyring or file"):
            remote._keychain({"VECTRIXDB_CREDENTIALS": "vault"})

    @pytest.mark.skipif(not POSIX, reason="file modes are POSIX")
    def test_the_file_is_still_its_owners_alone(self, tmp_path):
        logins = remote.Logins(tmp_path, keychain=_Keychain())
        logins.save(_login())
        assert (tmp_path / "logins.json").stat().st_mode & 0o777 == 0o600
