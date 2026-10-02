"""The settings a server is started with: one list of them, an env file, a template, and vectrixdb check.

What is held to. Every VECTRIXDB_ name the code reads is on the list, so the
template never leaves one out and a name the list does not know is a typo
worth saying so. An env file fills in what the environment has not set and
never overrides it. vectrixdb check reads the settings through the server's
own code and says what a start would refuse; it exits 1 on an error. The
access log can go to stdout, for a platform that collects the server's
output, and the server refuses any-origin CORS while sign-in is on.
"""

from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vectrixdb import settings
from vectrixdb.check import run
from vectrixdb.cli import app
from vectrixdb.exceptions import ConfigurationError

PACKAGE = Path(__file__).resolve().parents[2] / "vectrixdb"
SECRET = "s" * 40


@pytest.fixture(autouse=True)
def _no_inherited_settings(monkeypatch):
    """No setting from this machine, and none left behind: an env file writes straight into the environment."""
    import os

    before = dict(os.environ)
    for name in list(os.environ):
        if name.startswith("VECTRIXDB_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
    yield
    for name in [n for n in os.environ if n.startswith("VECTRIXDB_") and n not in before]:
        del os.environ[name]


def clear_settings():
    import os

    for name in [n for n in os.environ if n.startswith("VECTRIXDB_") and n != "VECTRIXDB_OFFLINE"]:
        del os.environ[name]


def signin_env(tmp_path: Path, **extra: str) -> dict:
    env = {
        "VECTRIXDB_SIGNIN": "email",
        "VECTRIXDB_SIGNIN_SECRET": SECRET,
        "VECTRIXDB_PUBLIC_URL": "http://localhost:7337",
        "VECTRIXDB_OFFLINE": "1",
    }
    env.update(extra)
    return env


def levels(findings, level):
    return [f.text for f in findings if f.level == level]


# ------------------------------------------------------------------ the list ---


class TestTheList:
    def test_every_setting_the_code_reads_is_listed(self):
        read = set()
        for source in PACKAGE.rglob("*.py"):
            read |= set(re.findall(r"VECTRIXDB_[A-Z0-9_]+", source.read_text(encoding="utf-8", errors="replace")))
        # Named only to be refused: the password itself and the code, which emergency sign-in no longer takes.
        retired = {f"VECTRIXDB_BREAK_GLASS_{name}{twin}" for name in ("PASSWORD", "TOTP") for twin in ("", "_FILE")}
        missing = sorted(read - settings.known() - retired)
        assert missing == [], f"read by the code and not on the list, so the template leaves them out: {missing}"

    def test_the_template_has_every_setting_and_no_secret(self):
        text = settings.template()
        for setting in settings.SETTINGS:
            assert f"# {setting.name}=" in text, setting.name
            if setting.secret:
                assert f"# {setting.name}=\n" in text, f"{setting.name} is a secret and is left empty"
        assert settings.read_env_file  # the template is itself an env file, with every line a comment

    def test_a_name_nothing_reads_is_a_typo_with_its_likely_meaning(self):
        assert settings.unknown(["VECTRIXDB_SIGIN_USERS", "VECTRIXDB_SIGNIN", "OTHER_THING", "VECTRIXDB_ZZZ"]) == [
            ("VECTRIXDB_SIGIN_USERS", "VECTRIXDB_SIGNIN_USERS"),
            ("VECTRIXDB_ZZZ", None),
        ]
        assert settings.unknown(["VECTRIXDB_SIGNIN_SECRET_FILE"]) == [], "a secret's file twin is a setting"


# ------------------------------------------------------------------ env file ---


class TestTheEnvFile:
    def test_lines_comments_export_and_quotes(self, tmp_path):
        env_file = tmp_path / "vectrixdb.env"
        env_file.write_text(
            "# a comment\n\nVECTRIXDB_SIGNIN=email\nexport VECTRIXDB_BRAND_NAME=\"Harbour Labs\"\nVECTRIXDB_OIDC_LABEL='Continue with Harbour ID'\nVECTRIXDB_PUBLIC_URL = https://vectors.example.test\n",
            encoding="utf-8",
        )
        assert settings.read_env_file(env_file) == {
            "VECTRIXDB_SIGNIN": "email",
            "VECTRIXDB_BRAND_NAME": "Harbour Labs",
            "VECTRIXDB_OIDC_LABEL": "Continue with Harbour ID",
            "VECTRIXDB_PUBLIC_URL": "https://vectors.example.test",
        }

    def test_a_line_that_is_not_one_is_named(self, tmp_path):
        env_file = tmp_path / "vectrixdb.env"
        env_file.write_text("VECTRIXDB_SIGNIN=email\nthis is not a setting\n", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="line 2"):
            settings.read_env_file(env_file)
        with pytest.raises(ConfigurationError, match="cannot be read"):
            settings.read_env_file(tmp_path / "missing.env")

    def test_the_environment_wins_over_the_file(self, tmp_path):
        env_file = tmp_path / "vectrixdb.env"
        env_file.write_text("VECTRIXDB_SIGNIN=email\nVECTRIXDB_GUESTS=on\n", encoding="utf-8")
        environ = {"VECTRIXDB_SIGNIN": "oidc"}
        taken = settings.apply_env_file(env_file, environ)
        assert environ == {"VECTRIXDB_SIGNIN": "oidc", "VECTRIXDB_GUESTS": "on"} and taken == {"VECTRIXDB_GUESTS": "on"}


# --------------------------------------------------------------------- check ---


class TestTheCheck:
    def test_a_clean_open_server_on_this_machine(self, tmp_path):
        found = run(str(tmp_path / "data"), {"VECTRIXDB_OFFLINE": "1"})
        assert levels(found, "error") == [] and levels(found, "warn") == []
        assert any("listens on this machine only" in text for text in levels(found, "ok"))

    def test_sign_in_is_read_through_the_servers_own_code(self, tmp_path):
        short = run(str(tmp_path), signin_env(tmp_path, VECTRIXDB_SIGNIN_SECRET="too short"))
        assert any("at least 32 characters" in text for text in levels(short, "error"))
        plain_http = run(str(tmp_path), signin_env(tmp_path, VECTRIXDB_PUBLIC_URL="http://vectors.example.test"))
        assert any("must be https" in text for text in levels(plain_http, "error"))

    def test_no_admin_yet_and_links_that_are_not_sent(self, tmp_path):
        found = run(str(tmp_path), signin_env(tmp_path, VECTRIXDB_PUBLIC_URL="https://vectors.example.test"))
        warned = levels(found, "warn")
        assert any("No admin yet" in text for text in warned)
        assert any("sign-in links are not sent" in text for text in warned)
        assert not (tmp_path / "auth" / "signin.db").exists(), "the check made nothing"

    def test_an_admin_on_the_list_quiets_the_warning(self, tmp_path):
        from vectrixdb.signin import SignInConfig, SignInStore

        env = signin_env(tmp_path)
        config = SignInConfig.from_env(str(tmp_path), env)
        store = SignInStore(config.store_path, config.secrets)
        store.put_person("ada@example.test", "admin")
        store.close()
        assert not any("No admin yet" in text for text in levels(run(str(tmp_path), env), "warn"))

    def test_an_admin_named_in_the_settings_is_one_to_come(self, tmp_path):
        """The server adds VECTRIXDB_SIGNIN_USERS at its start, so an admin named there is not missing."""
        env = signin_env(tmp_path, VECTRIXDB_SIGNIN_USERS="pat@example.test:admin,sam@example.test:viewer")
        found = run(str(tmp_path), env)
        assert not any("No admin yet" in text for text in levels(found, "warn"))
        assert "pat@example.test is made an admin at the first start, from VECTRIXDB_SIGNIN_USERS" in levels(found, "ok")
        viewers_only = run(str(tmp_path), signin_env(tmp_path, VECTRIXDB_SIGNIN_USERS="sam@example.test:viewer"))
        assert any("No admin yet" in text for text in levels(viewers_only, "warn")), "a viewer is not an admin"

    def test_with_single_sign_on_an_admin_comes_from_a_group(self, tmp_path):
        """Nobody is added by hand with single sign-on alone, so the email list's warning would be wrong advice."""
        sso = {
            "VECTRIXDB_SIGNIN": "oidc",
            "VECTRIXDB_OIDC_ISSUER": "https://idp.example.test/tenant",
            "VECTRIXDB_OIDC_CLIENT_ID": "vectrixdb",
            "VECTRIXDB_OIDC_CLIENT_SECRET": "s3cret",
        }
        mapped = run(str(tmp_path), signin_env(tmp_path, **sso, VECTRIXDB_OIDC_ROLE_MAP='{"g-admins": "admin", "g-staff": "viewer"}'))
        assert "Single sign-on makes an admin of anybody in g-admins" in levels(mapped, "ok")
        assert not any("people add" in text for text in levels(mapped, "warn"))
        unmapped = levels(run(str(tmp_path), signin_env(tmp_path, **sso, VECTRIXDB_OIDC_ROLE_MAP='{"g-staff": "operator"}')), "warn")
        assert any("No group is mapped to admin" in text for text in unmapped) and not any("people add" in text for text in unmapped)

    def test_an_admin_named_in_the_settings_and_already_here_as_something_else_is_not_one(self, tmp_path):
        """A start adds who is missing and changes nobody, so an address already on the list keeps its role."""
        from vectrixdb.signin import SignInConfig, SignInStore

        env = signin_env(tmp_path, VECTRIXDB_SIGNIN_USERS="pat@example.test:admin")
        config = SignInConfig.from_env(str(tmp_path), env)
        store = SignInStore(config.store_path, config.secrets)
        store.put_person("pat@example.test", "viewer")
        store.close()
        assert any("No admin yet" in text for text in levels(run(str(tmp_path), env), "warn"))

    def test_typos_and_settings_that_disagree(self, tmp_path):
        secret_file = tmp_path / "secret.txt"
        secret_file.write_text(SECRET, encoding="utf-8")
        both = run(str(tmp_path), signin_env(tmp_path, VECTRIXDB_SIGNIN_SECRET_FILE=str(secret_file)))
        assert any("VECTRIXDB_SIGNIN_SECRET and VECTRIXDB_SIGNIN_SECRET_FILE are both set" in text for text in levels(both, "error"))
        found = run(str(tmp_path), signin_env(
            tmp_path,
            VECTRIXDB_SIGIN_USERS="x@example.test:admin",
            VECTRIXDB_CORS_ORIGINS="*",
            VECTRIXDB_BRAND_ACCENT="teal",
            VECTRIXDB_STORAGE_BACKEND="cosmosdb",
            VECTRIXDB_MAX_UPLOAD_BYTES="lots",
        ))
        errors, warned = " | ".join(levels(found, "error")), " | ".join(levels(found, "warn"))
        assert "Did you mean VECTRIXDB_SIGNIN_USERS?" in warned
        assert "VECTRIXDB_CORS_ORIGINS is * while sign-in is on" in errors
        assert "VECTRIXDB_BRAND_ACCENT" in errors
        assert "cosmosdb needs VECTRIXDB_COSMOS_ENDPOINT" in errors
        assert "VECTRIXDB_MAX_UPLOAD_BYTES is 'lots'" in errors
        assert [f.level for f in found] == sorted((f.level for f in found), key=["ok", "warn", "error"].index), "most serious last"

    def test_a_secret_file_that_is_not_there_is_one_error(self, tmp_path):
        """Sign-in cannot read the secret either, for the same reason: saying it twice reads as two faults."""
        env = signin_env(tmp_path, VECTRIXDB_SIGNIN_SECRET_FILE=str(tmp_path / "gone.txt"))
        del env["VECTRIXDB_SIGNIN_SECRET"]
        errors = levels(run(str(tmp_path), env), "error")
        assert len(errors) == 1 and "VECTRIXDB_SIGNIN_SECRET_FILE names" in errors[0] and "which is not a file here" in errors[0], errors

    def test_the_access_log_on_the_servers_output(self, tmp_path):
        found = run(str(tmp_path), signin_env(tmp_path, VECTRIXDB_ACCESS_LOG="stdout"))
        assert any("server's output" in text for text in levels(found, "ok"))


class TestTheCommand:
    def test_the_template_is_printed(self):
        result = CliRunner().invoke(app, ["check", "--template"])
        assert result.exit_code == 0 and "# VECTRIXDB_SIGNIN_SECRET=\n" in result.output

    def test_an_error_exits_1_and_a_clean_file_exits_0(self, tmp_path):
        bad = tmp_path / "bad.env"
        bad.write_text(f"VECTRIXDB_SIGNIN=email\nVECTRIXDB_SIGNIN_SECRET={SECRET}\nVECTRIXDB_PUBLIC_URL=http://localhost:7337\nVECTRIXDB_CORS_ORIGINS=*\n", encoding="utf-8")
        result = CliRunner().invoke(app, ["check", "--env-file", str(bad), "--path", str(tmp_path / "data")])
        assert result.exit_code == 1 and "1 error" in result.output
        clear_settings()
        good = tmp_path / "good.env"
        good.write_text("VECTRIXDB_BRAND_NAME=Harbour Labs\n", encoding="utf-8")
        result = CliRunner().invoke(app, ["check", "--env-file", str(good), "--path", str(tmp_path / "data")])
        assert result.exit_code == 0 and "Ready to start" in result.output

    def test_serve_reads_the_env_file_and_takes_its_path_from_it(self, tmp_path, monkeypatch):
        calls = []
        module = types.ModuleType("vectrixdb.api.server")
        module.run_server = lambda **kwargs: calls.append(kwargs)
        monkeypatch.setitem(sys.modules, "vectrixdb.api.server", module)
        env_file = tmp_path / "vectrixdb.env"
        env_file.write_text(f"VECTRIXDB_PATH={tmp_path / 'from-file'}\nVECTRIXDB_BRAND_NAME=Harbour Labs\n", encoding="utf-8")
        result = CliRunner().invoke(app, ["serve", "--env-file", str(env_file)])
        assert result.exit_code == 0, result.output
        assert calls[0]["db_path"] == str(tmp_path / "from-file") and "Brand: Harbour Labs" in result.output
        result = CliRunner().invoke(app, ["serve", "--env-file", str(tmp_path / "missing.env")])
        assert result.exit_code == 2 and "cannot be read" in result.output

    def test_people_are_added_to_the_servers_own_list_from_its_env_file(self, tmp_path):
        """On the machine the server runs on, the same file is enough: no settings typed again, no --path."""
        env_file = tmp_path / "vectrixdb.env"
        data = tmp_path / "from-file"
        lines = [f"{name}={value}" for name, value in signin_env(tmp_path, VECTRIXDB_PATH=str(data)).items()]
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = CliRunner().invoke(app, ["people", "add", "ada@example.test", "--role", "admin", "--env-file", str(env_file)])
        assert result.exit_code == 0, result.output
        assert "Added ada@example.test as an admin." in result.output and "/dashboard/#/enrol?token=" in result.output
        clear_settings()
        result = CliRunner().invoke(app, ["people", "list", "--env-file", str(env_file)])
        assert result.exit_code == 0 and "ada@example.test" in result.output, result.output
        assert list(data.rglob("signin*.db")), "the list is kept where the server keeps it"
        clear_settings()
        result = CliRunner().invoke(app, ["people", "list"])
        assert result.exit_code == 2 and "sign-in is not on here" in result.output, "without the file, nothing is guessed"


# ------------------------------------------------------- access log, CORS ---


class TestTheAccessLogOnStdout:
    def test_a_line_goes_to_stdout_marked_and_none_is_listed(self, capsys):
        from vectrixdb.signin.access import AccessLog

        log = AccessLog("stdout")
        log.record("signin", who="ada@example.test", method="email")
        line = json.loads(capsys.readouterr().out.strip())
        assert line["log"] == "vectrixdb.access" and line["event"] == "signin" and line["who"] == "ada@example.test"
        assert log.recent() == [] and log.where == "stdout"
        assert not Path("stdout").exists(), "no file called stdout was made"


class TestBehindAGateway:
    """Two settings decide whether a deployment behind APIM or an API gateway
    behaves, and both are quiet when wrong."""

    def test_a_plain_server_is_told_nothing_about_gateways(self, tmp_path):
        findings = [f for f in run(path=str(tmp_path), env={}) if f.area == "Gateway"]
        assert findings == []

    def test_the_path_it_is_served_under_is_reported(self, tmp_path):
        findings = run(path=str(tmp_path), env={"VECTRIXDB_PUBLIC_URL": "https://apim.company.com/vectrixdb"})
        assert any("Served under /vectrixdb" in t for t in levels(findings, "ok"))

    def test_a_gateway_with_no_trusted_proxies_is_warned_about(self, tmp_path):
        """Every request then counts as coming from the gateway: one person's
        failed sign-ins lock out everybody."""
        findings = run(path=str(tmp_path), env={"VECTRIXDB_PUBLIC_URL": "https://apim.company.com/vectrixdb"})
        assert any("lock out everybody" in t for t in levels(findings, "warn"))

    def test_named_proxies_are_reported_and_end_the_warning(self, tmp_path):
        env = {"VECTRIXDB_PUBLIC_URL": "https://apim.company.com/vectrixdb", "VECTRIXDB_TRUSTED_PROXIES": "10.0.0.0/8"}
        findings = run(path=str(tmp_path), env=env)
        assert any("X-Forwarded-For is believed from 10.0.0.0/8" in t for t in levels(findings, "ok"))
        assert not any("lock out everybody" in t for t in levels(findings, "warn"))

    def test_a_proxy_list_nobody_can_parse_is_an_error(self, tmp_path):
        findings = run(path=str(tmp_path), env={"VECTRIXDB_TRUSTED_PROXIES": "apim.company.com"})
        assert any("apim.company.com" in t for t in levels(findings, "error"))

    def test_two_paths_that_disagree_are_an_error(self, tmp_path):
        env = {"VECTRIXDB_ROOT_PATH": "/vx", "VECTRIXDB_PUBLIC_URL": "https://apim.company.com/vectrixdb"}
        findings = run(path=str(tmp_path), env=env)
        assert any("sends people somewhere the app is not" in t for t in levels(findings, "error"))


class TestAnyOriginWithSignIn:
    def test_the_server_refuses_to_start(self, tmp_path, monkeypatch):
        pytest.importorskip("fastapi")
        from vectrixdb.api.server import create_app
        from vectrixdb.signin import SignInConfig

        monkeypatch.setenv("VECTRIXDB_CORS_ORIGINS", "*")
        config = SignInConfig(methods=("email",), secrets=(SECRET,), public_url="http://localhost:7337", store_path=tmp_path / "auth" / "signin.db", access_log=tmp_path / "auth" / "access.jsonl")
        with pytest.raises(ConfigurationError, match="Name the origins"):
            create_app(db_path=str(tmp_path / "db"), enable_dashboard=False, signin=config)
