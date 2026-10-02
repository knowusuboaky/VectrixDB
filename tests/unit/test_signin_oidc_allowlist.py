"""Single sign-on with a list of addresses beside the security group.

What is being held to: VECTRIXDB_OIDC_ALLOWED_EMAILS takes addresses and
@domain entries separated by commas or semicolons, tidies them, and refuses an
entry that is neither; an address is let in when it is on the list or its
domain is, and with no list the groups alone decide; both locks must open, so
somebody in the right group but not on the list is refused as not_listed, and
somebody on the list but in no mapped group is still refused for having no
role; the button reads "Continue with SSO" unless told otherwise; and
/auth/oidc/start passes prompt=select_account or prompt=login on to the
provider and nothing else.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("jwt", reason="the signin extra is not installed")
pytest.importorskip("cryptography", reason="the signin extra is not installed")

sys.path.insert(0, os.path.dirname(__file__))
from fake_idp import ISSUER, FakeIdp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.signin import OidcClient, OidcConfig, SignInConfig  # noqa: E402

SECRET = "k" * 48
PUBLIC = "https://vectors.example.test"
ROLES = {"g-admins": "admin", "g-ops": "operator"}


def create_app(**kwargs):
    """The server as it is now. Another test drops and re-imports ``vectrixdb.api``,
    and a name bound at the top of this file would go on pointing at the old copy."""
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


class Mail:
    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, text):
        self.sent.append((to, subject, text))

    def token(self):
        return re.search(r"#/enrol\?token=([\w-]+)", self.sent[-1][2]).group(1)


def _config(root: Path, **over) -> SignInConfig:
    base = {
        "methods": ("email",),
        "secrets": (SECRET,),
        "public_url": PUBLIC,
        "users": (),
        "store_path": root / "auth" / "signin.db",
        "access_log": root / "auth" / "access.jsonl",
        "sender": Mail(),
    }
    base.update(over)
    return SignInConfig(**base)


def _env(**over) -> dict:
    """Settings for single sign-on, as the server reads them from its environment."""
    env = {
        "VECTRIXDB_SIGNIN": "oidc",
        "VECTRIXDB_SIGNIN_SECRET": SECRET,
        "VECTRIXDB_PUBLIC_URL": PUBLIC,
        "VECTRIXDB_OIDC_ISSUER": ISSUER,
        "VECTRIXDB_OIDC_CLIENT_ID": "vectrixdb",
        "VECTRIXDB_OIDC_ROLE_MAP": '{"g-admins": "admin"}',
    }
    env.update(over)
    return env


def _nowhere(*args, **kwargs):
    raise AssertionError("deciding who is on the list never asks the identity provider")


def listed(*allowed: str) -> OidcClient:
    return OidcClient(OidcConfig(issuer=ISSUER, client_id="vectrixdb", role_map=ROLES, allowed_emails=allowed), transport=_nowhere)


@pytest.fixture
def sso(tmp_path, monkeypatch):
    """A server with single sign-on and a stand-in for the company's provider. Call it with the list."""
    monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
    built = []

    def serve(*allowed: str):
        idp = FakeIdp()
        oidc = OidcConfig(issuer=ISSUER, client_id="vectrixdb", client_secret="s3cret", role_map=ROLES, allowed_emails=allowed)
        config = _config(tmp_path / f"db{len(built)}", methods=("oidc",), oidc=oidc)
        app = create_app(db_path=str(tmp_path / f"db{len(built)}"), enable_dashboard=False, signin=config, oidc_transport=idp.transport)
        built.append(app)
        return TestClient(app, base_url=PUBLIC, follow_redirects=False), idp

    yield serve
    for app in built:
        app.state.signin.close()


def sign_in(client: TestClient, idp: FakeIdp, to: str = "/dashboard/#/search"):
    start = client.get("/auth/oidc/start", params={"to": to})
    assert start.status_code == 302
    code, state = idp.authorize(start.headers["location"])
    return client.get("/auth/oidc/callback", params={"code": code, "state": state})


def refused(reply) -> str:
    assert reply.status_code == 302 and reply.headers["location"].startswith("/dashboard/#/signin?error=")
    return reply.headers["location"].split("error=")[1]


def signed_in_as(client: TestClient):
    me = client.get("/auth/me")
    return me.json()["data"]["person"]["email"] if me.status_code == 200 else None


def asked_of_the_provider(start) -> dict:
    assert start.status_code == 302
    return parse_qs(urlsplit(start.headers["location"]).query)


# ----------------------------------------------------------- the setting


class TestTheListIsReadFromTheEnvironment:
    def test_addresses_and_domains_split_on_commas_or_semicolons_and_tidied(self, tmp_path):
        env = _env(VECTRIXDB_OIDC_ALLOWED_EMAILS=" Ama@Company.com;@Contractors.Company.com , bo@partner.example ;; ,")
        config = SignInConfig.from_env(tmp_path, env=env)
        assert config.oidc.allowed_emails == ("ama@company.com", "@contractors.company.com", "bo@partner.example")

    @pytest.mark.parametrize("value", [None, "", "  ", " ; , ;"])
    def test_unset_or_blank_means_there_is_no_list(self, tmp_path, value):
        env = _env() if value is None else _env(VECTRIXDB_OIDC_ALLOWED_EMAILS=value)
        assert SignInConfig.from_env(tmp_path, env=env).oidc.allowed_emails == ()

    @pytest.mark.parametrize("entry", ["company.com", "ama@", "@", "ama @company.com"])
    def test_an_entry_that_is_neither_an_address_nor_a_domain_is_refused(self, tmp_path, entry):
        env = _env(VECTRIXDB_OIDC_ALLOWED_EMAILS=f"ok@company.com;{entry}")
        with pytest.raises(ConfigurationError, match="VECTRIXDB_OIDC_ALLOWED_EMAILS has"):
            SignInConfig.from_env(tmp_path, env=env)

    def test_a_list_given_in_code_is_tidied_the_same_way(self):
        config = OidcConfig(issuer=ISSUER, client_id="vectrixdb", role_map=ROLES, allowed_emails=["  AMA@Company.com ", "", "@Partner.Example"])
        assert config.allowed_emails == ("ama@company.com", "@partner.example")

    def test_a_star_alone_is_read_as_the_groups_deciding_on_their_own(self, tmp_path):
        assert SignInConfig.from_env(tmp_path, env=_env(VECTRIXDB_OIDC_ALLOWED_EMAILS=" * ")).oidc.allowed_emails == ("*",)

    @pytest.mark.parametrize("value", ["*,ama@company.com", "@company.com;*"])
    def test_a_star_beside_anything_else_is_refused_since_it_would_make_the_rest_mean_nothing(self, tmp_path, value):
        with pytest.raises(ConfigurationError, match="alone"):
            SignInConfig.from_env(tmp_path, env=_env(VECTRIXDB_OIDC_ALLOWED_EMAILS=value))


class TestWhoTheListLetsIn:
    def test_with_no_list_nobody_is_on_it(self):
        nobody = listed()
        assert not nobody.allows("anybody@anywhere.example") and not nobody.allows(None)

    def test_a_star_lets_the_groups_decide_on_their_own(self):
        everybody = listed("*")
        assert everybody.groups_alone and everybody.allows("anybody@anywhere.example") and everybody.allows(None)

    def test_a_listed_address_in_any_case_and_with_stray_spaces(self):
        ama = listed("ama@company.com")
        assert ama.allows("ama@company.com") and ama.allows("  AMA@Company.COM ")
        assert not ama.allows("bo@company.com")

    def test_a_domain_lets_in_anybody_there_and_nobody_at_a_look_alike(self):
        company = listed("@company.com")
        assert company.allows("ama@company.com") and company.allows("Bo@COMPANY.com")
        for elsewhere in ("ama@sub.company.com", "ama@evilcompany.com", "ama@company.com.evil.test", "ama@company.com@evil.test"):
            assert not company.allows(elsewhere), elsewhere

    def test_an_address_and_a_domain_on_one_list(self):
        both = listed("ama@company.com", "@partner.example")
        assert both.allows("ama@company.com") and both.allows("zed@partner.example")
        assert not both.allows("bo@company.com")

    @pytest.mark.parametrize("nothing", [None, "", "   ", "company.com", "ama"])
    def test_something_that_is_not_an_address_is_on_no_list(self, nothing):
        assert not listed("@company.com").allows(nothing)


# ------------------------------------------------------- both locks, end to end


class TestBothLocksMustOpen:
    def test_in_the_group_and_on_the_list_signs_in(self, sso):
        client, idp = sso("ada@example.com")
        back = sign_in(client, idp)
        assert back.status_code == 302 and back.headers["location"] == "/dashboard/#/search"
        assert signed_in_as(client) == "ada@example.com"

    def test_in_the_right_group_but_not_on_the_list_is_refused_as_not_listed(self, sso):
        client, idp = sso("ama@company.com", "@partner.example")
        assert idp.person["groups"] == ["g-admins"], "the group that would make them an admin"
        assert refused(sign_in(client, idp)) == "not_listed"
        assert signed_in_as(client) is None and "__Host-vx_sid" not in client.cookies
        failures = client.app.state.signin.access.recent(event="signin_failed")
        assert [f["reason"] for f in failures] == ["not_listed"]

    def test_on_the_list_but_in_no_mapped_group_is_still_refused_for_having_no_role(self, sso):
        client, idp = sso("ada@example.com")
        idp.person["groups"] = ["unrelated"]
        assert refused(sign_in(client, idp)) == "no_role"
        assert signed_in_as(client) is None

    def test_a_domain_on_the_list_lets_in_anybody_there_who_is_in_the_group(self, sso):
        client, idp = sso("@example.com")
        idp.person.update(sub="u-2", email="Grace@Example.com", name="Grace Hopper", groups=["g-ops"])
        sign_in(client, idp)
        assert signed_in_as(client) == "grace@example.com"
        other = TestClient(client.app, base_url=PUBLIC, follow_redirects=False)
        idp.person.update(sub="u-3", email="grace@example.org")
        assert refused(sign_in(other, idp)) == "not_listed"

    def test_with_a_star_the_group_alone_decides(self, sso):
        client, idp = sso("*")
        idp.person["email"] = "anybody@anywhere.example"
        sign_in(client, idp)
        assert signed_in_as(client) == "anybody@anywhere.example"

    def test_with_no_list_and_nobody_on_the_people_list_the_server_will_not_start(self, sso):
        with pytest.raises(ConfigurationError, match="nobody is named who may sign in") as stopped:
            sso()
        for way_out in ("VECTRIXDB_SIGNIN_USERS", "vectrixdb people add", "VECTRIXDB_OIDC_ALLOWED_EMAILS=*"):
            assert way_out in str(stopped.value), way_out

    def test_an_address_the_provider_says_it_did_not_check_is_on_no_list(self, sso):
        client, idp = sso("ada@example.com")
        idp.person["email_verified"] = False
        assert refused(sign_in(client, idp)) == "not_listed"
        idp.person["email_verified"] = True
        sign_in(client, idp)
        assert signed_in_as(client) == "ada@example.com"

    def test_the_address_may_come_as_the_preferred_username(self, sso):
        client, idp = sso("ada@example.com")
        del idp.person["email"]
        idp.person["preferred_username"] = "ada@example.com"
        sign_in(client, idp)
        assert signed_in_as(client) == "ada@example.com"

    def test_a_token_with_no_address_is_on_no_list(self, sso):
        client, idp = sso("@example.com")
        del idp.person["email"]
        assert refused(sign_in(client, idp)) == "not_listed"


# ---------------------------------------------------------------- the button


class TestTheButton:
    def test_it_reads_continue_with_sso_unless_told_otherwise(self, tmp_path):
        assert OidcConfig(issuer=ISSUER, client_id="vectrixdb", default_role="viewer").label == "Continue with SSO"
        assert SignInConfig.from_env(tmp_path, env=_env()).oidc.label == "Continue with SSO"
        assert SignInConfig.from_env(tmp_path, env=_env(VECTRIXDB_OIDC_LABEL="   ")).oidc.label == "Continue with SSO"
        named = SignInConfig.from_env(tmp_path, env=_env(VECTRIXDB_OIDC_LABEL=" Sign in with Contoso "))
        assert named.oidc.label == "Sign in with Contoso"

    def test_the_page_is_told_the_label_before_anybody_signs_in(self, sso):
        client, _ = sso("*")
        reply = client.get("/auth/me")
        assert reply.status_code == 401 and reply.json()["data"]["methods"] == {"oidc": {"label": "Continue with SSO"}}


# ---------------------------------------------------------------- the prompt


class TestAskingTheProviderToAskAgain:
    @pytest.mark.parametrize("prompt", ["select_account", "login"])
    def test_select_account_and_login_are_passed_on(self, sso, prompt):
        client, idp = sso("*")
        start = client.get("/auth/oidc/start", params={"prompt": prompt})
        assert asked_of_the_provider(start)["prompt"] == [prompt]
        code, state = idp.authorize(start.headers["location"])
        back = client.get("/auth/oidc/callback", params={"code": code, "state": state})
        assert back.status_code == 302 and back.headers["location"] == "/dashboard/"
        assert signed_in_as(client) == "ada@example.com", "and the way round still works"

    @pytest.mark.parametrize("prompt", ["none", "consent", "LOGIN", "login consent", "select_account&prompt=none", ""])
    def test_anything_else_is_left_out(self, sso, prompt):
        client, _ = sso("*")
        assert "prompt" not in asked_of_the_provider(client.get("/auth/oidc/start", params={"prompt": prompt}))

    def test_no_prompt_asked_for_is_no_prompt_sent(self, sso):
        client, _ = sso("*")
        assert "prompt" not in asked_of_the_provider(client.get("/auth/oidc/start"))
