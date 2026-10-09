"""vectrixdb doctor: every part tried, every service asked through a stand-in."""

from __future__ import annotations

import json
import socket
import urllib.error

import pytest

from vectrixdb.doctor import LEVELS, run, summary

ISSUER = "https://login.example.com/tenant-1/v2.0"
KEYS = "https://login.example.com/tenant-1/keys"


class Services:
    """Answers a GET by address, and takes a connection by host; anything else is unreachable."""

    def __init__(self, answers=None, hosts=()):
        self.answers = dict(answers or {})
        self.hosts = set(hosts)
        self.asked = []
        self.headers = []

    def reach(self, url, headers):
        self.asked.append(url)
        self.headers.append(dict(headers))
        if url not in self.answers:
            # What urllib raises for a name that does not resolve.
            raise urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
        status, body = self.answers[url]
        return status, body if isinstance(body, bytes) else json.dumps(body).encode()

    def connect(self, host, port):
        self.asked.append(f"{host}:{port}")
        if host not in self.hosts:
            raise ConnectionRefusedError("refused")


def diagnose(tmp_path, env, services=None, **kw):
    services = services or Services()
    found = run(
        str(tmp_path),
        env,
        quick=True,
        reach=services.reach,
        connect=services.connect,
        settings_check=False,
        **kw,
    )
    return found, services


def lines(found, level=None, area=None):
    return [
        d.text
        for d in found
        if (level is None or d.level == level) and (area is None or d.area == area)
    ]


class TestThisMachine:
    def test_the_folder_is_written_to_and_left_as_it_was(self, tmp_path):
        found, _ = diagnose(tmp_path, {})
        assert any("can be written" in t for t in lines(found, "ok", "Install"))
        assert list(tmp_path.iterdir()) == []

    def test_a_folder_not_made_yet_is_fine(self, tmp_path):
        found = run(str(tmp_path / "later"), {}, quick=True, offline=True, settings_check=False)
        assert any("first collection makes it" in t for t in lines(found, "ok", "Install"))

    def test_the_built_in_readers_read_a_document_each(self, tmp_path):
        found, _ = diagnose(tmp_path, {})
        assert "Markdown, HTML, Plain text: read" in lines(found, "ok", "Readers")

    def test_mcp_on_without_its_package_is_an_error(self, tmp_path, monkeypatch):
        import vectrixdb.doctor as doctor

        real = doctor._installed
        monkeypatch.setattr(
            doctor, "_installed", lambda m: False if m.startswith("mcp") else real(m)
        )
        found, _ = diagnose(tmp_path, {"VECTRIXDB_MCP": "1"})
        (error,) = [d for d in found if d.area == "MCP"]
        assert error.level == "error" and error.fix == "pip install -U 'vectrixdb[mcp]'"

    def test_the_models_load_and_answer(self, tmp_path):
        from vectrixdb.models.embedded import is_models_installed

        if not is_models_installed("dense"):
            pytest.skip("the bundled model is not in this checkout")
        found = run(str(tmp_path), {}, offline=True, settings_check=False)
        assert any(
            t.startswith("Embedding model loads and embeds, 384 dimensions")
            for t in lines(found, "ok")
        )

    def test_quick_leaves_them_unloaded(self, tmp_path):
        found, _ = diagnose(tmp_path, {})
        assert "Not loaded (quick)" in lines(found, "skip", "Models")


class TestTheIdentityProvider:
    def env(self, **more):
        return {"VECTRIXDB_OIDC_ISSUER": ISSUER, "VECTRIXDB_OIDC_CLIENT_ID": "app", **more}

    def answers(self, issuer=ISSUER, keys=({"kid": "1"}, {"kid": "2"})):
        return {
            f"{ISSUER}/.well-known/openid-configuration": (
                200,
                {"issuer": issuer, "jwks_uri": KEYS},
            ),
            KEYS: (200, {"keys": list(keys)}),
        }

    def test_its_description_and_keys_are_read(self, tmp_path):
        found, services = diagnose(tmp_path, self.env(), Services(self.answers()))
        ok = lines(found, "ok", "Sign-in")
        assert any(t.startswith("The identity provider at login.example.com answers") for t in ok)
        assert "Its 2 signing keys read" in ok
        assert services.asked == [f"{ISSUER}/.well-known/openid-configuration", KEYS]

    def test_an_issuer_that_calls_itself_otherwise_is_caught(self, tmp_path):
        found, _ = diagnose(
            tmp_path, self.env(), Services(self.answers(issuer="https://login.example.com/other"))
        )
        (error,) = [d for d in found if d.level == "error"]
        assert "calls itself https://login.example.com/other" in error.text

    def test_entra_common_is_named_for_what_it_is(self, tmp_path):
        found, _ = diagnose(
            tmp_path,
            self.env(),
            Services(self.answers(issuer="https://login.microsoftonline.com/{tenantid}/v2.0")),
        )
        (error,) = [d for d in found if d.level == "error"]
        assert "use your tenant's" in error.fix

    def test_no_keys_is_an_error(self, tmp_path):
        found, _ = diagnose(tmp_path, self.env(), Services(self.answers(keys=())))
        assert "The provider's key list is empty: no token could be checked" in lines(
            found, "error"
        )

    def test_unreachable_says_why(self, tmp_path):
        found, _ = diagnose(tmp_path, self.env())
        (error,) = [d for d in found if d.level == "error"]
        assert error.text.endswith("cannot be reached: the name does not resolve")

    def test_a_missing_client_id(self, tmp_path):
        env = self.env()
        del env["VECTRIXDB_OIDC_CLIENT_ID"]
        found, _ = diagnose(tmp_path, env, Services(self.answers()))
        assert "VECTRIXDB_OIDC_CLIENT_ID is not set" in lines(found, "error")


class TestTheServices:
    def test_the_extraction_service_is_asked_for_its_health_with_its_key(self, tmp_path):
        services = Services({"https://extract.example.com/health": (200, b"ok")})
        found, _ = diagnose(
            tmp_path,
            {
                "VECTRIXDB_EXTRACTOR_URL": "https://extract.example.com",
                "VECTRIXDB_EXTRACTOR_KEY": "s3cret",
            },
            services,
        )
        assert any(
            t.startswith("The extraction service at extract.example.com answers")
            for t in lines(found, "ok")
        )
        assert services.headers == [{"x-api-key": "s3cret"}]
        assert "s3cret" not in json.dumps(summary(found)), "a key is never printed"

    def test_a_service_failing_is_told_apart_from_one_missing(self, tmp_path):
        services = Services({"https://extract.example.com/health": (503, b"")})
        found, _ = diagnose(
            tmp_path, {"VECTRIXDB_EXTRACTOR_URL": "https://extract.example.com"}, services
        )
        assert "answers 503: it is there and failing" in " ".join(lines(found, "error"))

    def test_a_key_refusal_still_means_it_is_there(self, tmp_path):
        services = Services({"https://extract.example.com/health": (401, b"")})
        found, _ = diagnose(
            tmp_path, {"VECTRIXDB_EXTRACTOR_URL": "https://extract.example.com"}, services
        )
        assert any("(it asks for a key)" in t for t in lines(found, "ok"))

    def test_the_mail_server_takes_a_connection_and_its_password_is_never_shown(self, tmp_path):
        url = "smtp://mailer:hunter2@smtp.example.com:2525"
        found, services = diagnose(
            tmp_path, {"VECTRIXDB_SMTP_URL": url}, Services(hosts={"smtp.example.com"})
        )
        assert "smtp.example.com:2525" in services.asked
        assert "hunter2" not in json.dumps(summary(found))

    def test_a_postgres_store_is_reached_without_its_password_shown(self, tmp_path):
        url = "postgresql://vx:pa55@db.example.com:6432/vectrixdb"
        found, services = diagnose(tmp_path, {"VECTRIXDB_AUDIT_STORE": url})
        assert "db.example.com:6432" in services.asked
        (error,) = [d for d in found if d.level == "error"]
        assert (
            error.text
            == "The audit store at db.example.com:6432 cannot be reached: the connection was refused"
        )
        assert "pa55" not in json.dumps(summary(found))

    def test_a_chat_model_is_asked_at_its_host_alone(self, tmp_path):
        services = Services({"https://models.example.com/": (404, b"")})
        found, _ = diagnose(
            tmp_path,
            {"VECTRIXDB_WRITER_URL": "https://models.example.com/v1/chat/completions?key=abc"},
            services,
        )
        assert services.asked == ["https://models.example.com/"]
        assert any(
            t.startswith("The model that drafts golden questions") for t in lines(found, "ok")
        )

    def test_the_trace_collector(self, tmp_path):
        found, services = diagnose(
            tmp_path,
            {"VECTRIXDB_TRACING": "otlp", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
            Services(hosts={"collector"}),
        )
        assert "collector:4318" in services.asked

    def test_offline_asks_nothing(self, tmp_path):
        services = Services()
        found, _ = diagnose(
            tmp_path,
            {"VECTRIXDB_OIDC_ISSUER": ISSUER, "VECTRIXDB_EXTRACTOR_URL": "https://x.example.com"},
            services,
            offline=True,
        )
        assert services.asked == []
        assert "Offline: no service the settings name was asked" in lines(found, "skip")

    def test_vectrixdb_offline_means_offline(self, tmp_path):
        services = Services()
        diagnose(tmp_path, {"VECTRIXDB_OFFLINE": "1", "VECTRIXDB_OIDC_ISSUER": ISSUER}, services)
        assert services.asked == []


class TestTheReport:
    def test_most_serious_last(self, tmp_path):
        found, _ = diagnose(tmp_path, {"VECTRIXDB_EXTRACTOR_URL": "https://x.example.com"})
        ranks = [LEVELS.index(d.level) for d in found]
        assert ranks == sorted(ranks) and found[-1].level == "error"

    def test_the_settings_check_comes_with_it(self, tmp_path):
        found = run(
            str(tmp_path),
            {"VECTRIXDB_STORAGE_BACKEND": "nonsense"},
            quick=True,
            offline=True,
        )
        assert any("VECTRIXDB_STORAGE_BACKEND is 'nonsense'" in t for t in lines(found, "error"))

    def test_summary(self, tmp_path):
        found, _ = diagnose(tmp_path, {})
        report = summary(found)
        assert report["healthy"] is True
        assert set(report["counts"]) == set(LEVELS)

    def test_the_command(self, tmp_path):
        from typer.testing import CliRunner

        from vectrixdb.cli import app

        result = CliRunner().invoke(
            app,
            ["doctor", "--path", str(tmp_path), "--offline", "--quick", "--json"],
            env={"VECTRIXDB_OFFLINE": "1"},
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["healthy"] is True
