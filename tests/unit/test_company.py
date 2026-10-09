"""A company's defaults: a wrapper package's, the machine's file, and where each is sent.

The headers and the certificate authority go to the company's own server and
to no other address; a typo in a file every machine reads is refused by name.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

httpx = pytest.importorskip("httpx")

import vectrixdb  # noqa: E402
from vectrixdb import company  # noqa: E402
from vectrixdb.client import Client  # noqa: E402
from vectrixdb.company import machine_file as REAL_MACHINE_FILE  # noqa: E402
from vectrixdb.exceptions import ConfigurationError  # noqa: E402

HOME = "https://vectors.acme.example"


def _package(monkeypatch, *packages):
    """Installed wrapper packages, each (name, defaults)."""
    points = [types.SimpleNamespace(name=name, load=lambda d=data: d) for name, data in packages]
    monkeypatch.setattr(company, "_entry_points", lambda: points)


def _file(monkeypatch, tmp_path, text, suffix=".toml"):
    path = tmp_path / f"defaults{suffix}"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(company, "machine_file", lambda env=None: path)
    return path


@pytest.fixture(autouse=True)
def _a_clean_environment(monkeypatch):
    for name in ("VECTRIXDB_URL", "VECTRIXDB_KEY", "VECTRIXDB_TOKEN", "ACME_APIM_KEY"):
        monkeypatch.delenv(name, raising=False)


class TestWhereDefaultsComeFrom:
    def test_nothing_installed_is_nothing(self):
        found = company.load()
        assert found.server == "" and found.headers == {} and found.program == "vectrixdb"

    def test_a_wrapper_package(self, monkeypatch):
        _package(monkeypatch, ("acme", {"server": HOME, "command": "acme-vectors"}))
        found = company.load()
        assert found.server == HOME and found.program == "acme-vectors"
        assert found.came_from["server"] == "acme (package)"

    def test_a_package_may_give_a_function(self, monkeypatch):
        _package(monkeypatch, ("acme", lambda: {"server": HOME}))
        assert company.load().server == HOME

    def test_the_machines_file_wins_over_the_package(self, monkeypatch, tmp_path):
        _package(
            monkeypatch,
            ("acme", {"server": HOME, "headers": {"X-Team": "search", "X-Region": "ca"}}),
        )
        path = _file(
            monkeypatch,
            tmp_path,
            '[vectrixdb]\nserver = "https://vectors.eu.acme.example"\n[vectrixdb.headers]\nX-Region = "eu"\n',
        )
        found = company.load()
        assert found.server == "https://vectors.eu.acme.example"
        assert found.headers == {"X-Team": "search", "X-Region": "eu"}
        assert found.came_from["server"] == str(path)

    def test_json_too(self, monkeypatch, tmp_path):
        _file(monkeypatch, tmp_path, json.dumps({"server": HOME}), suffix=".json")
        assert company.load().server == HOME

    def test_a_field_nobody_knows_is_refused_by_name(self, monkeypatch, tmp_path):
        _file(monkeypatch, tmp_path, 'server = "https://v.example"\nservr = "x"\n')
        with pytest.raises(ConfigurationError, match="servr"):
            company.load()

    def test_two_packages_must_be_chosen_between(self, monkeypatch):
        _package(
            monkeypatch, ("acme", {"server": HOME}), ("globex", {"server": "https://g.example"})
        )
        with pytest.raises(ConfigurationError, match="acme, globex"):
            company.load()
        monkeypatch.setenv("VECTRIXDB_DEFAULTS", "globex")
        assert company.load().server == "https://g.example"

    def test_a_server_that_is_not_an_address_is_refused(self, monkeypatch):
        _package(monkeypatch, ("acme", {"server": "vectors.acme.example"}))
        with pytest.raises(ConfigurationError, match="https://"):
            company.load()

    def test_a_named_file_that_is_not_there_is_said(self, monkeypatch, tmp_path):
        monkeypatch.setattr(company, "machine_file", REAL_MACHINE_FILE)
        monkeypatch.setenv("VECTRIXDB_DEFAULTS_FILE", str(tmp_path / "nope.toml"))
        with pytest.raises(ConfigurationError, match="not there"):
            company.load()

    def test_the_machines_own_place(self, monkeypatch):
        place = REAL_MACHINE_FILE({})
        assert place.name == "defaults.toml" and place.parent.name == "vectrixdb"
        if sys.platform == "win32":
            assert "ProgramData" in str(place) or "PROGRAMDATA" in str(place).upper()


class TestTheCompanysServerOnly:
    def test_headers_read_their_variables(self, monkeypatch):
        monkeypatch.setenv("ACME_APIM_KEY", "sub-123")
        assert company.expand({"Ocp-Apim-Subscription-Key": "${ACME_APIM_KEY}"}) == {
            "Ocp-Apim-Subscription-Key": "sub-123"
        }

    def test_a_variable_that_is_not_set_is_named_not_its_value(self):
        with pytest.raises(ConfigurationError, match="ACME_APIM_KEY is not set"):
            company.expand({"Ocp-Apim-Subscription-Key": "${ACME_APIM_KEY}"})

    def test_options_go_to_the_home_server_and_nowhere_else(self, monkeypatch):
        monkeypatch.setenv("ACME_APIM_KEY", "sub-123")
        found = company.Defaults(
            server=HOME,
            ca_file="system",
            key_header="x-acme-key",
            headers={"Ocp-Apim-Subscription-Key": "${ACME_APIM_KEY}"},
            user_agent="acme-vectors/1.4",
        )
        assert found.for_server(HOME + "/") == {
            "verify": "system",
            "key_header": "x-acme-key",
            "headers": {"Ocp-Apim-Subscription-Key": "sub-123"},
            "user_agent": "acme-vectors/1.4",
        }
        assert found.for_server("https://vectors.acme.example:443/api") != {}
        assert found.for_server("https://elsewhere.example") == {}
        assert found.for_server("http://vectors.acme.example") == {}
        assert found.for_server("https://vectors.acme.example:8443") == {}


class _Seen:
    """Every request a client makes, and the options it was made with."""

    def __init__(self, monkeypatch):
        self.requests, self.options = [], []

        def answer(request):
            self.requests.append(request)
            return httpx.Response(200, json={"ok": True, "data": {"who": "ama", "role": "viewer"}})

        def client(**options):
            self.options.append(dict(options))
            options.pop("verify", None)
            return httpx.Client(transport=httpx.MockTransport(answer), **options)

        monkeypatch.setattr(
            "vectrixdb.client._httpx",
            lambda: types.SimpleNamespace(Client=client, TransportError=httpx.TransportError),
        )
        monkeypatch.setattr("vectrixdb.client.tls", lambda verify=True: verify)


class TestConnect:
    def test_no_address_goes_to_the_companys_server_with_its_headers(self, monkeypatch):
        monkeypatch.setenv("ACME_APIM_KEY", "sub-123")
        _package(
            monkeypatch,
            (
                "acme",
                {
                    "server": HOME,
                    "ca_file": "system",
                    "headers": {"Ocp-Apim-Subscription-Key": "${ACME_APIM_KEY}"},
                    "user_agent": "acme-vectors/1.4",
                },
            ),
        )
        seen = _Seen(monkeypatch)
        vectrixdb.connect(token="t").whoami()
        sent = seen.requests[0]
        assert str(sent.url) == HOME + "/api/v1/whoami"
        assert sent.headers["ocp-apim-subscription-key"] == "sub-123"
        assert sent.headers["authorization"] == "Bearer t"
        assert sent.headers["user-agent"].startswith("acme-vectors/1.4 vectrixdb-python")
        assert seen.options[0]["verify"] == "system"

    def test_another_address_gets_none_of_the_companys_headers(self, monkeypatch):
        monkeypatch.setenv("ACME_APIM_KEY", "sub-123")
        _package(
            monkeypatch,
            (
                "acme",
                {"server": HOME, "headers": {"Ocp-Apim-Subscription-Key": "${ACME_APIM_KEY}"}},
            ),
        )
        seen = _Seen(monkeypatch)
        vectrixdb.connect("https://partner.example", key="k").whoami()
        assert "ocp-apim-subscription-key" not in seen.requests[0].headers
        assert seen.options[0]["verify"] is True

    def test_vectrixdb_url_wins_over_the_company(self, monkeypatch):
        _package(monkeypatch, ("acme", {"server": HOME}))
        monkeypatch.setenv("VECTRIXDB_URL", "https://staging.acme.example")
        seen = _Seen(monkeypatch)
        vectrixdb.connect(key="k").whoami()
        assert str(seen.requests[0].url).startswith("https://staging.acme.example/")

    def test_the_callers_own_options_win(self, monkeypatch):
        _package(monkeypatch, ("acme", {"server": HOME, "headers": {"X-Team": "search"}}))
        seen = _Seen(monkeypatch)
        vectrixdb.connect(key="k", headers={"X-Team": "ops", "X-Run": "1"}).whoami()
        sent = seen.requests[0].headers
        assert sent["x-team"] == "ops" and sent["x-run"] == "1"

    def test_no_address_anywhere_says_where_one_goes(self):
        with pytest.raises(ValueError, match="VECTRIXDB_URL"):
            vectrixdb.connect(key="k")

    def test_a_header_may_not_carry_the_caller(self):
        with pytest.raises(ValueError, match="key= or token="):
            Client(HOME, key="k", headers={"Authorization": "Bearer x"})
        with pytest.raises(ValueError, match="key= or token="):
            Client(HOME, key="k", headers={"API-Key": "x"})


class TestTheCommandLine:
    def test_the_companys_server_and_the_wrappers_name_in_every_hint(self, monkeypatch, tmp_path):
        from typer.testing import CliRunner

        from vectrixdb.cli import app

        monkeypatch.setenv("VECTRIXDB_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("VECTRIXDB_CREDENTIALS", "file")
        _package(monkeypatch, ("acme", {"server": HOME, "command": "acme-vectors"}))

        def answer(request):
            return httpx.Response(401, json={"detail": "Sign in"})

        monkeypatch.setattr(
            "vectrixdb.client._httpx",
            lambda: types.SimpleNamespace(
                Client=lambda **o: httpx.Client(
                    transport=httpx.MockTransport(answer), base_url=o["base_url"]
                ),
                TransportError=httpx.TransportError,
            ),
        )
        result = CliRunner().invoke(app, ["list"])
        assert result.exit_code == 1
        assert f"server {HOME}" in result.stderr
        assert f"acme-vectors login --server {HOME}" in result.output

    def test_a_broken_defaults_file_is_one_line(self, monkeypatch, tmp_path):
        from typer.testing import CliRunner

        from vectrixdb.cli import app

        _file(monkeypatch, tmp_path, 'servr = "x"\n')
        result = CliRunner().invoke(app, ["whoami"])
        assert result.exit_code == 2
        assert "servr" in result.output and "Traceback" not in result.output
