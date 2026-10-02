"""The headers every reply carries, and the names and rules of the sign-in cookies.

The dashboard's policy lets it load its own files, the fonts and the one chart
library, and nothing else, and forbids framing it. It runs no inline script:
the page has none, and a control says what it does as data that one listener
reads, so the policy refuses inline script outright. A reply that is not a page
may do nothing. A browser revalidating the page is answered "not modified",
and it copies that answer's headers onto the page it kept, so that answer
carries no policy: the page keeps its own. That was found in a browser, where
the dashboard could not load its own scripts until it was fixed.

Over https the session and forgery cookies are ``__Host-``, bound to this host
and every path, and SameSite is strict unless single sign-on is on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("cryptography", reason="the signin extra is not installed")

from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.signin import SignInConfig, totp  # noqa: E402

SECRET = "s" * 48
PUBLIC = "https://vectors.example.test"


def create_app(**kwargs):
    """Imported when called: another test drops and re-imports ``vectrixdb.api``."""
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


@pytest.fixture(autouse=True)
def plain_env(monkeypatch):
    for name in (
        "VECTRIXDB_API_KEY",
        "VECTRIXDB_FRAME_ANCESTORS",
        "VECTRIXDB_PUBLIC_URL",
        "VECTRIXDB_SIGNIN",
        "VECTRIXDB_BRAND_NAME",
        "VECTRIXDB_BRAND_LOGO",
        "VECTRIXDB_BRAND_ACCENT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")


@pytest.fixture
def open_server(tmp_path):
    with TestClient(create_app(db_path=str(tmp_path / "db"))) as client:
        yield client


class Mail:
    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, text):
        self.sent.append((to, subject, text))


def signin_server(tmp_path, **over):
    base = {
        "methods": ("email",),
        "secrets": (SECRET,),
        "public_url": PUBLIC,
        "users": (("ada@example.com", "admin"),),
        "store_path": tmp_path / "auth" / "signin.db",
        "access_log": tmp_path / "auth" / "access.jsonl",
        "sender": Mail(),
    }
    base.update(over)
    config = SignInConfig(**base)
    return TestClient(
        create_app(db_path=str(tmp_path / "db"), enable_dashboard=False, signin=config),
        base_url=config.public_url,
    ), config


def enrolled_cookies(client, config):
    import re

    client.post("/auth/email/begin", json={"email": "ada@example.com"})
    token = re.search(r"#/enrol\?token=([\w-]+)", config.sender.sent[-1][2]).group(1)
    begun = client.post("/auth/email/enrol/begin", json={"token": token}).json()["data"]
    done = client.post(
        "/auth/email/enrol/confirm",
        json={"ticket": begun["ticket"], "code": totp.code_at(begun["secret"], totp.step_now())},
    )
    assert done.status_code == 200, done.text
    return done.headers.get_list("set-cookie")


class TestThePage:
    def test_the_dashboard_may_load_only_what_it_names_and_may_not_be_framed(self, open_server):
        reply = open_server.get("/dashboard/")
        policy = reply.headers["content-security-policy"]
        assert "default-src 'self'" in policy and "frame-ancestors 'none'" in policy
        assert "script-src 'self' https://cdnjs.cloudflare.com;" in policy, (
            "no inline script, and no eval"
        )
        assert (
            "object-src 'none'" in policy
            and "base-uri 'none'" in policy
            and "form-action 'self'" in policy
        )
        assert "unsafe-eval" not in policy
        assert (
            reply.headers["x-frame-options"] == "DENY"
            and reply.headers["cross-origin-opener-policy"] == "same-origin"
        )
        assert (
            reply.headers["x-content-type-options"] == "nosniff"
            and reply.headers["referrer-policy"] == "no-referrer"
        )

    def test_its_live_connection_is_let_back_in(self, open_server):
        policy = open_server.get("/dashboard/").headers["content-security-policy"]
        assert "connect-src 'self' ws://testserver wss://testserver" in policy

    def test_a_not_modified_answer_leaves_the_page_its_own_policy(self, open_server):
        first = open_server.get("/dashboard/")
        again = open_server.get("/dashboard/", headers={"If-None-Match": first.headers["etag"]})
        assert again.status_code == 304
        assert "content-security-policy" not in again.headers, (
            "the browser would put this policy on the page it kept"
        )
        assert again.headers["x-content-type-options"] == "nosniff"


class TestTheApi:
    def test_a_reply_that_is_not_a_page_may_do_nothing_and_is_not_kept(self, open_server):
        reply = open_server.get("/api/v1/collections")
        assert (
            reply.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
        )
        assert reply.headers["cache-control"] == "no-store"

    def test_the_api_reference_keeps_its_own_rules(self, open_server):
        reply = open_server.get("/docs")
        assert reply.status_code == 200 and "content-security-policy" not in reply.headers
        assert reply.headers["x-frame-options"] == "DENY"

    def test_a_header_a_route_set_is_left_alone(self, tmp_path, monkeypatch):
        logo = tmp_path / "logo.svg"
        logo.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect width="8" height="8"/></svg>'
        )
        monkeypatch.setenv("VECTRIXDB_BRAND_LOGO", str(logo))
        with TestClient(create_app(db_path=str(tmp_path / "db"))) as client:
            assert (
                client.get("/brand/logo").headers["content-security-policy"]
                == "default-src 'none'; style-src 'unsafe-inline'; sandbox"
            )


class TestTransport:
    def test_https_is_remembered_for_a_year_and_plain_http_is_not_told(self, open_server):
        assert "strict-transport-security" not in open_server.get("/dashboard/").headers
        behind = open_server.get("/dashboard/", headers={"x-forwarded-proto": "https"})
        assert behind.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"

    def test_a_server_whose_address_is_https_says_so_on_every_reply(self, tmp_path):
        client, _ = signin_server(tmp_path)
        with client:
            assert (
                client.get("/auth/status")
                .headers["strict-transport-security"]
                .startswith("max-age=31536000")
            )


class TestFraming:
    def test_a_portal_named_may_show_the_dashboard(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_FRAME_ANCESTORS", "https://portal.example.com")
        with TestClient(create_app(db_path=str(tmp_path / "db"))) as client:
            reply = client.get("/dashboard/")
        assert (
            "frame-ancestors https://portal.example.com" in reply.headers["content-security-policy"]
        )
        assert "x-frame-options" not in reply.headers, (
            "the old header cannot name a portal, so it steps aside"
        )

    @pytest.mark.parametrize(
        "value",
        [
            "http://portal.example.com",
            "*",
            "portal.example.com",
            "https://a.example.com;script-src *",
        ],
    )
    def test_anything_but_https_origins_is_refused_at_start(self, tmp_path, monkeypatch, value):
        monkeypatch.setenv("VECTRIXDB_FRAME_ANCESTORS", value)
        with pytest.raises(ConfigurationError, match="VECTRIXDB_FRAME_ANCESTORS"):
            create_app(db_path=str(tmp_path / "db"))


class TestCookies:
    def test_over_https_the_session_is_bound_to_this_host(self, tmp_path):
        client, config = signin_server(tmp_path)
        with client:
            cookies = enrolled_cookies(client, config)
        session = next(c for c in cookies if c.startswith("__Host-vx_sid="))
        forgery = next(c for c in cookies if c.startswith("__Host-vx_csrf="))
        for cookie in (session, forgery):
            low = cookie.lower()
            assert "secure" in low and "path=/" in low and "domain=" not in low
        assert "httponly" in session.lower() and "httponly" not in forgery.lower()
        assert not any(c.startswith(("vx_sid=", "vx_csrf=")) for c in cookies), (
            "no plain copy a subdomain could shadow"
        )

    def test_strict_without_single_sign_on_and_lax_with_it(self, tmp_path):
        assert SignInConfig(methods=("email",)).samesite == "strict"
        assert SignInConfig(methods=("oidc", "email")).samesite == "lax", (
            "the provider's redirect back is a cross-site navigation"
        )
        assert SignInConfig(methods=("oidc",), cookie_samesite="strict").samesite == "strict"
        client, config = signin_server(tmp_path)
        with client:
            session = next(
                c for c in enrolled_cookies(client, config) if c.startswith("__Host-vx_sid=")
            )
        assert "samesite=strict" in session.lower()

    def test_a_plain_name_is_not_read_over_https(self, tmp_path):
        client, config = signin_server(tmp_path)
        with client:
            enrolled_cookies(client, config)
            value = client.cookies.get("__Host-vx_sid")
            assert client.get("/auth/me").status_code == 200
            stranger = TestClient(client.app, base_url=PUBLIC)
            stranger.cookies.set("vx_sid", value)
            assert stranger.get("/auth/me").status_code == 401, (
                "a cookie a subdomain could have set is not the session"
            )

    def test_on_a_laptop_the_names_are_plain(self, tmp_path):
        client, config = signin_server(tmp_path, public_url="http://localhost:7337")
        with client:
            cookies = enrolled_cookies(client, config)
        assert any(c.startswith("vx_sid=") for c in cookies) and not any(
            c.startswith("__Host-") for c in cookies
        )

    @pytest.mark.parametrize("value", ["none", "loose"])
    def test_only_strict_or_lax_may_be_set(self, tmp_path, value):
        env = {
            "VECTRIXDB_SIGNIN": "email",
            "VECTRIXDB_SIGNIN_SECRET": SECRET,
            "VECTRIXDB_PUBLIC_URL": PUBLIC,
            "VECTRIXDB_COOKIE_SAMESITE": value,
        }
        with pytest.raises(ConfigurationError, match="VECTRIXDB_COOKIE_SAMESITE"):
            SignInConfig.from_env(tmp_path, env)


def test_the_server_does_not_say_what_it_is_built_on(monkeypatch):
    import types

    calls = []
    monkeypatch.setitem(
        sys.modules, "uvicorn", types.SimpleNamespace(run=lambda *a, **kw: calls.append(kw))
    )
    for name in ("VECTRIXDB_PATH", "VECTRIXDB_DASHBOARD"):
        monkeypatch.setenv(name, "placeholder")
    from vectrixdb.api import server

    server.run_server(host="127.0.0.1", db_path=str(Path("unused")))
    assert calls and calls[0]["server_header"] is False


class TestNoInlineScript:
    """The page runs no inline script, so its policy refuses all of it.

    It used to allow inline script, because every button had an inline
    handler, and that is how a collection named ``x');alert(1);('``, a legal
    name, ran script for whoever clicked Rebuild on it. Now a control says what
    it does in ``data-on-<event>``, a JSON list of calls that one listener
    reads: nothing in a value is ever run. The listener calls only the
    functions named in ACTIONS, so markup that got onto the page some other way
    cannot call ``eval`` or ``fetch`` with it. The one script from another host
    is pinned by its hash.
    """

    @staticmethod
    def dashboard():
        import vectrixdb

        return Path(vectrixdb.__file__).parent / "dashboard"

    def read(self, name):
        return (self.dashboard() / name).read_text(encoding="utf-8")

    def test_no_handler_attribute_and_no_inline_script_anywhere(self):
        import re

        for name in ("index.html", "app.js", "evaluate.js", "chunking.js"):
            assert not re.search(r'\bon[a-z]+="', self.read(name)), f"{name} has an inline handler"
        page = self.read("index.html")
        for tag in re.findall(r"<script\b[^>]*>", page):
            assert "src=" in tag or 'type="application/json"' in tag, f"inline script: {tag}"

    def test_the_script_from_another_host_is_pinned_by_its_hash(self):
        import re

        page = self.read("index.html")
        assert not [tag for tag in re.findall(r"<script\b[^>]*>", page) if 'src="http' in tag], (
            "the page loads nothing from another host: the script fetches the graph library when the tab is opened"
        )
        script = self.read("app.js")
        body = script[
            script.index("function ensureCytoscape()") : script.index("async function loadGraph()")
        ]
        assert "s.src = 'https://cdnjs.cloudflare.com/" in body
        assert (
            re.search(r"s\.integrity = 'sha(256|384|512)-[A-Za-z0-9+/=]+'", body)
            and "s.crossOrigin = 'anonymous'" in body
        ), "pinned by its hash, like the tag it replaced"

    def test_every_control_calls_a_function_on_the_list_and_every_name_on_it_is_one(self):
        import json
        import re

        script = (
            self.read("app.js") + "\n" + self.read("evaluate.js") + "\n" + self.read("chunking.js")
        )
        allowed = set(
            json.loads(
                re.search(r"const ACTIONS = new Set\((\[.*?\])\);", script)
                .group(1)
                .replace("'", '"')
            )
        )
        called = set(re.findall(r"\$\{on\('[a-z]+', \['(\w+)'", script))
        called |= {
            c.split("&quot;")[0] for c in re.findall(r'data-on-[a-z]+="\[\[&quot;(\w+)', script)
        }
        for value in re.findall(r"data-on-[a-z]+='(\[.*?\])'", self.read("index.html")):
            called |= {call[0] for call in json.loads(value)}
        assert called and called <= allowed, sorted(called - allowed)
        declared = set(re.findall(r"^(?:async )?function (\w+)\(", script, flags=re.M))
        assert allowed <= declared, (
            f"on the list and not a function the page declares: {sorted(allowed - declared)}"
        )

    def test_nothing_hides_the_helper_that_writes_a_control(self):
        """A local ``on`` hides ``on()``, and every control drawn in its scope throws.

        It happened once: the run menu kept whether a run was the one on screen
        in ``const on``, and the Evaluate page never finished drawing.
        """
        import re

        for name in ("app.js", "evaluate.js", "chunking.js"):
            script = self.read(name)
            assert not re.search(r"\b(?:const|let|var)\s+on\b", script), (
                f"{name} declares a local on"
            )
            assert not re.search(
                r"\(\s*(?:[\w$]+\s*,\s*)*on\s*(?:,\s*[\w$]+\s*)*\)\s*=>", script
            ), f"{name} has a parameter named on"
            assert not re.search(r"function\s*[\w$]*\s*\([^)]*\bon\b[^)]*\)", script), (
                f"{name} has a parameter named on"
            )

    def test_the_brand_arrives_as_data(self, tmp_path):
        from vectrixdb.brand import Brand

        page = Brand(name="Acme </script><script>alert(1)</script>").render_index(
            self.read("index.html")
        )
        assert "<script>alert(1)</script>" not in page, "a name cannot end the data block"
        assert '<script type="application/json" id="vx-brand-data">' in page
