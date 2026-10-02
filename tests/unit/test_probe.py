"""vectrixdb check --url: a deployed server asked, from outside, what a caller would ask.

The server is real and the gateway is a stand-in: a function between the probe
and the app that does what gateways do. Forwarding the path, stripping it,
and each of the ways a policy breaks a deployment without anything failing
loudly: a header stripped, the gateway's own error page, CORS added twice, a
server that was never told its public address.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("typer", reason="the CLI extra is not installed")

from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.probe import probe  # noqa: E402

PUBLIC = "https://apim.company.test/vectrixdb"
KEY = "the-admin-api-key"


def _embed(texts):
    return np.array([[1, 0, 0, 0] for _ in texts], dtype=np.float32)


@pytest.fixture
def app_behind(tmp_path, monkeypatch):
    """The server, told its public address, and a way to make gateways in front of it."""
    from vectrixdb.api.server import create_app

    one = Vectrix(
        "handbook", path=str(tmp_path), dimension=4, embed_fn=_embed, embedding_cache=False
    )
    one.add(["alpha"], ids=["a"])
    one.close()

    def make(
        public=PUBLIC,
        strips_prefix=False,
        drop=(),
        error_page=False,
        extra_cors=False,
        dashboard=True,
    ):
        monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
        monkeypatch.delenv("VECTRIXDB_AUDIT_JSONL", raising=False)
        if public:
            monkeypatch.setenv("VECTRIXDB_PUBLIC_URL", public)
        else:
            monkeypatch.delenv("VECTRIXDB_PUBLIC_URL", raising=False)
        monkeypatch.setenv("VECTRIXDB_ALLOW_OPEN", "1")
        client = TestClient(
            create_app(db_path=str(tmp_path), enable_dashboard=dashboard), follow_redirects=False
        )
        client.__enter__()

        def gateway(url, headers):
            path = url[len("https://apim.company.test") :]
            if strips_prefix:
                path = path[len("/vectrixdb") :] or "/"
            sent = {k: v for k, v in headers.items() if k.lower() not in drop}
            reply = client.get(path, headers=sent)
            out = {k.lower(): v for k, v in reply.headers.items()}
            body = reply.content
            if error_page and reply.status_code >= 400:
                body = b"<html><body>Access denied due to invalid subscription key.</body></html>"
            if extra_cors and "origin" in {k.lower() for k in sent}:
                out["access-control-allow-origin"] = "*, https://portal.company.test"
            return reply.status_code, out, body

        return gateway, client

    made = []

    def factory(**options):
        gateway, client = make(**options)
        made.append(client)
        return gateway

    yield factory
    for client in made:
        client.__exit__(None, None, None)


def levels(findings):
    return [f.level for f in findings]


def said(findings, level):
    return " | ".join(f.text for f in findings if f.level == level)


class TestADeploymentThatIsRight:
    def test_a_gateway_that_forwards_the_path(self, app_behind):
        findings = probe(PUBLIC, app_behind())
        assert "error" not in levels(findings) and "warn" not in levels(findings), said(
            findings, "error"
        ) + said(findings, "warn")
        everything = said(findings, "ok")
        for expected in (
            "reaches the server",
            "under /vectrixdb",
            "sent as api-key is refused by the server itself",
            "sent as Authorization: Bearer is refused by the server itself",
            "dashboard is served",
            "redirect keeps the path",
        ):
            assert expected in everything, expected

    def test_a_gateway_that_strips_the_path(self, app_behind):
        findings = probe(PUBLIC, app_behind(strips_prefix=True))
        assert "error" not in levels(findings), said(findings, "error")

    def test_only_the_api_published_is_a_note_not_a_failure(self, app_behind):
        findings = probe(PUBLIC, app_behind(dashboard=False))
        assert "error" not in levels(findings), said(findings, "error")
        assert "Fine if only the API is published" in said(findings, "warn")


class TestWhatAGatewayGetsWrong:
    def test_the_key_header_stripped_on_the_way_in(self, app_behind):
        findings = probe(PUBLIC, app_behind(drop=("api-key",)))
        assert "sent as api-key got 200" in said(findings, "warn"), (
            "a keys-only server lets a read through with no key"
        )
        assert "sent as Authorization: Bearer is refused by the server itself" in said(
            findings, "ok"
        ), "the other header still arrives"

    def test_the_gateways_own_error_page_in_place_of_the_servers_refusal(self, app_behind):
        findings = probe(PUBLIC, app_behind(error_page=True))
        assert "not in the server's words" in said(findings, "warn")

    def test_cors_added_twice(self, app_behind):
        findings = probe(PUBLIC, app_behind(extra_cors=True))
        assert "both adding CORS" in said(findings, "error")

    def test_a_server_never_told_its_public_address(self, app_behind):
        findings = probe(PUBLIC, app_behind(public=None, strips_prefix=True))
        assert "does not say the API is under /vectrixdb" in said(findings, "error")
        assert "VECTRIXDB_PUBLIC_URL=" + PUBLIC in said(findings, "error")

    def test_a_path_that_is_not_published(self, app_behind):
        gateway = app_behind()
        findings = probe("https://apim.company.test/somewhere-else", gateway)
        assert levels(findings) == ["error"] and "/health answered 404" in findings[0].text

    def test_nothing_answering(self):
        def down(url, headers):
            raise ConnectionRefusedError("refused")

        findings = probe(PUBLIC, down)
        assert levels(findings) == ["error"] and "did not get an answer" in findings[0].text

    @pytest.mark.parametrize("given", ["", "apim.company.test/vectrixdb", "ftp://x/y"])
    def test_what_is_not_an_address_is_said_so(self, given):
        findings = probe(given, lambda url, headers: (200, {}, b"{}"))
        assert levels(findings) == ["error"] and "is not an address" in findings[0].text


def test_every_request_is_a_get_and_the_key_is_wrong_on_purpose(app_behind):
    inner = app_behind()
    seen = []

    def watching(url, headers):
        seen.append(dict(headers))
        return inner(url, headers)

    probe(PUBLIC, watching)
    keys = [h.get("api-key") or h.get("Authorization", "") for h in seen]
    assert all(KEY not in k for k in keys) and any("wrong-on-purpose" in k for k in keys)


def test_the_command_exits_1_on_an_error(monkeypatch):
    from typer.testing import CliRunner

    import vectrixdb.probe as module
    from vectrixdb.cli import app

    monkeypatch.setattr(module, "_fetch", lambda url, headers: (502, {}, b"Bad gateway"))
    result = CliRunner().invoke(app, ["check", "--url", PUBLIC])
    assert result.exit_code == 1 and "/health answered 502" in " ".join(result.output.split())
