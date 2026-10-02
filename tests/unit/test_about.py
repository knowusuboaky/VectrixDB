"""About: which VectrixDB a server runs, its licence, and the NOTICE that travels with it.

What is being held to: the page says VectrixDB and the version, the Apache
licence in a line and the NOTICE file whole, read from the checkout or from
the installed package's metadata; with sign-in on it is an admin's, and a
viewer, a guest and somebody not signed in are refused; with sign-in off it
is anybody's, like the version always was; the licence itself is served in
plain text; and a dashboard some other service sends can take the brand's
colours from /brand.css.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import vectrixdb
from vectrixdb import __version__
from vectrixdb.about import LICENCE_LINE, about, licence_text
from vectrixdb.brand import Brand
from vectrixdb.signin import SignInConfig, roles

pytest.importorskip("fastapi", reason="the API extra is not installed")
from fastapi.testclient import TestClient  # noqa: E402

REPO = Path(vectrixdb.__file__).resolve().parents[1]
PUBLIC = "https://vectors.example.test"
SECRET = "k" * 48


def create_app(**kwargs):
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


@pytest.fixture
def serve(tmp_path, monkeypatch):
    for name in ("VECTRIXDB_API_KEY", "VECTRIXDB_SIGNIN", "VECTRIXDB_BRAND_ACCENT", "VECTRIXDB_BRAND_PALETTE", "VECTRIXDB_BRAND_NAME"):
        monkeypatch.delenv(name, raising=False)
    built = []

    def build(signin=None, **settings):
        for name, value in settings.items():
            monkeypatch.setenv(name, value)
        app = create_app(db_path=str(tmp_path / f"db{len(built)}"), enable_dashboard=False, signin=signin)
        built.append(app)
        return TestClient(app, base_url=PUBLIC)

    yield build
    for app in built:
        if app.state.signin is not None:
            app.state.signin.close()


def _config(root: Path) -> SignInConfig:
    return SignInConfig(
        methods=("email",), secrets=(SECRET,), public_url=PUBLIC, guests=True,
        store_path=root / "auth" / "signin.db", access_log=root / "auth" / "access.jsonl", sender=lambda *a: None,
    )


def as_role(client: TestClient, role: str) -> TestClient:
    """Signed in with that role, by a session opened straight in the store: how somebody got in is not what is tested."""
    runtime = client.app.state.signin
    sid, session = runtime.store.open_session(subject=f"{role}@example.com", email=None, name=role, role=role, principal={}, method="oidc", hours=1)
    client.cookies.set("__Host-vx_sid", runtime.store.sign(sid))
    return client


class TestWhatAboutSays:
    def test_the_name_the_version_the_licence_and_the_notice_whole(self):
        said = about()
        assert said["name"] == "VectrixDB" and said["version"] == __version__
        assert said["licence"] == "Apache-2.0" and said["licence_line"] == LICENCE_LINE == "Licensed under the Apache License, Version 2.0."
        assert said["notice"] == (REPO / "NOTICE").read_text(encoding="utf-8"), "the file as it is, every model with it"

    def test_the_licence_is_the_apache_licence_as_carried(self):
        text = licence_text()
        assert text == (REPO / "LICENSE").read_text(encoding="utf-8") and "Apache License" in text and "Version 2.0" in text

    def test_an_install_reads_them_from_the_packages_metadata(self, monkeypatch, tmp_path):
        """A wheel keeps them under licenses/ in its metadata, which is where an install finds them."""
        import vectrixdb.about as module

        class Installed:
            def read_text(self, name):
                return {"licenses/NOTICE": "the notice", "licenses/LICENSE": "the licence"}.get(name)

        monkeypatch.setattr(module, "__file__", str(tmp_path / "site-packages" / "vectrixdb" / "about.py"))
        monkeypatch.setattr(module.metadata, "distribution", lambda name: Installed())
        assert module.about()["notice"] == "the notice" and module.licence_text() == "the licence"


class TestWhoMayReadIt:
    def test_an_admin_reads_it_and_the_licence(self, serve, tmp_path):
        client = as_role(serve(signin=_config(tmp_path / "a")), roles.ADMIN)
        reply = client.get("/api/v1/about")
        assert reply.status_code == 200 and reply.json()["data"] == about()
        licence = client.get("/api/v1/about/licence")
        assert licence.status_code == 200 and licence.headers["content-type"].startswith("text/plain") and "Apache License" in licence.text

    @pytest.mark.parametrize("role", [roles.VIEWER, roles.OPERATOR])
    def test_anybody_else_signed_in_is_refused(self, serve, tmp_path, role):
        client = as_role(serve(signin=_config(tmp_path / role)), role)
        assert client.get("/api/v1/about").status_code == 403
        assert client.get("/api/v1/about/licence").status_code == 403

    def test_a_guest_and_a_stranger_are_asked_to_sign_in(self, serve, tmp_path):
        client = serve(signin=_config(tmp_path / "g"))
        assert client.get("/api/v1/about").status_code == 401
        assert __version__ not in client.get("/").text

    def test_with_sign_in_off_it_is_anybodys_like_the_version_always_was(self, serve):
        client = serve()
        assert client.get("/api/v1/about").json()["data"]["version"] == __version__

    def test_only_admins_hold_it_in_the_table(self):
        assert [role for role in roles.GRANTS if "about.read" in roles.GRANTS[role]] == [roles.ADMIN]
        assert roles.action_for("GET", "/api/v1/about") == roles.action_for("GET", "/api/v1/about/licence") == "about.read"


class TestTheBrandsColoursAsAStylesheet:
    def test_a_palette_and_an_accent_come_as_css_for_anybody(self, serve, tmp_path):
        client = serve(signin=_config(tmp_path / "b"), VECTRIXDB_BRAND_ACCENT="#1f6f54", VECTRIXDB_BRAND_PALETTE='{"light": {"page": "#f4f6f4"}}')
        reply = client.get("/brand.css")
        assert reply.status_code == 200 and reply.headers["content-type"].startswith("text/css")
        assert reply.text == client.app.state.brand.css() and re.search(r":root\[data-theme=\"light\"\] \{ --g0: #f4f6f4;", reply.text)

    def test_with_no_brand_it_is_empty(self, serve):
        reply = serve().get("/brand.css")
        assert reply.status_code == 200 and reply.text == "" == Brand().css()
