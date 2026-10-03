"""An app acting as the person using it: an access token from the identity provider.

A key is nobody in particular, so a collection with an entitlement policy is
closed to it, and what it reads is recorded under the key's name. An app that
holds the signed-in person's token is that person: the role their groups give
them, the policy judging the search as theirs, and their name in the access
log. What is being held to is that a token is believed only when the provider
signed it, for this server's audience, and never when it was issued for the
dashboard's sign-in.
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np
import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("jwt", reason="the signin extra is not installed")
pytest.importorskip("cryptography", reason="the signin extra is not installed")

sys.path.insert(0, os.path.dirname(__file__))
from fake_idp import ISSUER, FakeIdp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.policy import Overlap, Policy  # noqa: E402
from vectrixdb.signin import OidcConfig, SignInConfig  # noqa: E402

SECRET = "k" * 48
PUBLIC = "https://vectors.example.test"
AUDIENCE = "api://vectrixdb"
VECTORS = {"alpha": [1, 0, 0, 0], "beta": [0, 1, 0, 0]}


def _embed(texts):
    return np.array([VECTORS[t] for t in texts], dtype=np.float32)


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "db"
    plain = Vectrix("plain", path=str(root), dimension=4, embed_fn=_embed, embedding_cache=False)
    plain.add(["alpha", "beta"], ids=["p-alpha", "p-beta"])
    plain.close()
    walled = Vectrix(
        "walled",
        path=str(root),
        dimension=4,
        embed_fn=_embed,
        embedding_cache=False,
        policy=Policy([Overlap("client_id", "clients")]),
    )
    walled.add(
        ["alpha", "beta"],
        ids=["w-acme", "w-zeta"],
        metadata=[{"client_id": "acme"}, {"client_id": "zeta"}],
    )
    walled.close()
    return root


def serve(data, idp, **oidc_over):
    from vectrixdb.api.server import create_app

    oidc = OidcConfig(
        **{
            "issuer": ISSUER,
            "client_id": "vectrixdb",
            "client_secret": "s3cret",
            "api_audience": AUDIENCE,
            "role_map": {
                "g-admins": "admin",
                "g-readers": "viewer",
                "g-ops": "operator",
                "acme": "operator",
            },
            "principal_claims": {"clients": "groups"},
            "allowed_emails": ("*",),
            **oidc_over,
        }
    )
    config = SignInConfig(
        methods=("oidc",),
        secrets=(SECRET,),
        public_url=PUBLIC,
        oidc=oidc,
        users=(),
        store_path=data / "auth" / "signin.db",
        access_log=data / "auth" / "access.jsonl",
    )
    return TestClient(
        create_app(
            db_path=str(data), enable_dashboard=False, signin=config, oidc_transport=idp.transport
        ),
        base_url=PUBLIC,
    )


@pytest.fixture
def idp(monkeypatch):
    monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
    monkeypatch.delenv("VECTRIXDB_AUDIT_JSONL", raising=False)
    return FakeIdp()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestAnAppIsThePersonUsingIt:
    def test_the_token_is_who_it_says_with_the_role_their_groups_give(self, data, idp):
        idp.person = {"sub": "u-7", "email": "olu@example.com", "name": "Olu", "groups": ["g-ops"]}
        with serve(data, idp) as client:
            head = bearer(idp.access_token())
            assert client.get("/api/v1/collections", headers=head).status_code == 200
            found = client.post(
                "/api/v1/collections/plain/search", json={"query": VECTORS["alpha"]}, headers=head
            )
            assert (
                found.status_code == 200 and found.json()["data"]["results"][0]["id"] == "p-alpha"
            )

    def test_a_policy_judges_the_search_as_theirs(self, data, idp):
        """The thing a key can never do: the collection is closed to a key and open to a person."""
        idp.person = {"sub": "u-8", "email": "ama@example.com", "groups": ["acme"]}
        with serve(data, idp) as client:
            found = client.post(
                "/api/v1/collections/walled/search",
                json={"query": VECTORS["alpha"], "limit": 10},
                headers=bearer(idp.access_token()),
            )
            assert found.status_code == 200, found.text
            assert [r["id"] for r in found.json()["data"]["results"]] == ["w-acme"], (
                "only what their clients claim allows"
            )

    def test_what_they_read_is_recorded_under_their_name(self, data, idp):
        idp.person = {"sub": "u-7", "email": "olu@example.com", "groups": ["g-ops"]}
        with serve(data, idp) as client:
            client.post(
                "/api/v1/collections/plain/search",
                json={"query": VECTORS["alpha"]},
                headers=bearer(idp.access_token()),
            )
        lines = [
            json.loads(line)
            for line in (data / "auth" / "access.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        search = [line for line in lines if line.get("event") == "search"][-1]
        assert (
            search["who"] == "olu@example.com"
            and search["method"] == "token"
            and search["role"] == "operator"
        )

    def test_a_viewer_is_still_a_viewer(self, data, idp):
        idp.person = {"sub": "u-9", "email": "vi@example.com", "groups": ["g-readers"]}
        with serve(data, idp) as client:
            head = bearer(idp.access_token())
            assert client.get("/api/v1/collections", headers=head).status_code == 200
            assert (
                client.post(
                    "/api/v1/collections/plain/search",
                    json={"query": VECTORS["alpha"]},
                    headers=head,
                ).status_code
                == 403
            )

    def test_a_write_needs_no_forgery_token_because_nothing_rides_on_a_cookie(self, data, idp):
        idp.person = {"sub": "u-7", "email": "olu@example.com", "groups": ["g-ops"]}
        with serve(data, idp) as client:
            made = client.post(
                "/api/v1/collections",
                json={"name": "fresh", "dimension": 4},
                headers=bearer(idp.access_token()),
            )
            assert made.status_code == 200, made.text

    def test_a_change_that_wants_the_person_themselves_is_refused_to_an_app(self, data, idp):
        with serve(data, idp) as client:  # Ada is an admin
            reply = client.delete("/api/v1/collections/plain", headers=bearer(idp.access_token()))
            assert (
                reply.status_code == 403 and "signed in at the dashboard" in reply.json()["message"]
            )
            assert (
                client.get(
                    "/api/v1/collections/plain", headers=bearer(idp.access_token())
                ).status_code
                == 200
            )


class TestATokenThatIsNotBelieved:
    def refused(self, data, idp, token, **oidc_over):
        with serve(data, idp, **oidc_over) as client:
            reply = client.get("/api/v1/collections", headers=bearer(token))
        assert reply.status_code == 401, reply.text
        assert set(reply.json()) == {"ok", "message", "data", "detail"}
        return reply.json()["message"]

    def test_one_issued_for_the_dashboards_sign_in(self, data, idp):
        """An identity token's audience is the sign-in client. Taken here, any app the
        person ever signed in to with this provider could read their collections."""
        assert "did not verify" in self.refused(data, idp, idp.access_token(audience="vectrixdb"))

    def test_one_for_another_api(self, data, idp):
        assert "did not verify" in self.refused(
            data, idp, idp.access_token(audience="api://payroll")
        )

    def test_one_that_has_expired(self, data, idp):
        idp.issued_ago, idp.expires_in = 7200, 300
        assert "did not verify" in self.refused(data, idp, idp.access_token())

    def test_one_from_another_issuer(self, data, idp):
        idp.token_issuer = "https://evil.example.test/tenant"
        assert "did not verify" in self.refused(data, idp, idp.access_token())

    def test_one_signed_with_a_key_the_provider_does_not_publish(self, data, idp):
        idp.sign_with_unpublished_key = True
        assert "did not verify" in self.refused(data, idp, idp.access_token())

    def test_one_that_is_not_signed_at_all(self, data, idp):
        idp.unsigned = True
        assert "does not accept" in self.refused(data, idp, idp.access_token())

    def test_somebody_in_no_group_that_has_access(self, data, idp):
        idp.person = {"sub": "u-0", "email": "nobody@example.com", "groups": ["g-strangers"]}
        assert "not in a group" in self.refused(data, idp, idp.access_token())

    def test_the_platforms_own_list_says_nothing_about_a_token(self, data, idp):
        """The list is for the people who sign in to the platform. A token is somebody using a
        collection through an app, and what they read is the collection's policy's to decide."""
        with serve(data, idp, allowed_emails=("@company.com",)) as client:
            reply = client.get("/api/v1/collections", headers=bearer(idp.access_token()))
        assert reply.status_code == 200, reply.text

    def test_groups_that_did_not_fit_in_the_token(self, data, idp):
        idp.overflow = True
        assert "more groups than fit" in self.refused(data, idp, idp.access_token())

    def test_a_refusal_is_written_down_without_the_token(self, data, idp):
        token = idp.access_token(audience="api://payroll")
        self.refused(data, idp, token)
        log = (data / "auth" / "access.jsonl").read_text(encoding="utf-8")
        assert '"signin_failed"' in log and '"token"' in log and token not in log

    def test_with_no_audience_set_no_token_is_taken(self, data, idp):
        with serve(data, idp, api_audience=None) as client:
            reply = client.get("/api/v1/collections", headers=bearer(idp.access_token()))
        assert reply.status_code == 401 and reply.json()["message"] == "Invalid API key"

    def test_a_token_in_the_key_header_is_not_a_token(self, data, idp):
        with serve(data, idp) as client:
            reply = client.get("/api/v1/collections", headers={"api-key": idp.access_token()})
        assert reply.status_code == 401 and reply.json()["message"] == "Invalid API key"

    @pytest.mark.parametrize("given", ["a.b", "a..c", "vx_aaaaaaaa_x.y.z", "plainkey"])
    def test_what_is_not_shaped_like_a_token_is_a_wrong_key(self, data, idp, given):
        with serve(data, idp) as client:
            reply = client.get("/api/v1/collections", headers=bearer(given))
        assert reply.status_code == 401 and reply.json()["message"] == "Invalid API key"


def test_the_setting_is_read_from_the_environment(tmp_path):
    env = {
        "VECTRIXDB_SIGNIN": "oidc",
        "VECTRIXDB_SIGNIN_SECRET": SECRET,
        "VECTRIXDB_PUBLIC_URL": PUBLIC,
        "VECTRIXDB_OIDC_ISSUER": ISSUER,
        "VECTRIXDB_OIDC_CLIENT_ID": "vectrixdb",
        "VECTRIXDB_OIDC_CLIENT_SECRET": "s3cret",
        "VECTRIXDB_OIDC_DEFAULT_ROLE": "viewer",
        "VECTRIXDB_OIDC_API_AUDIENCE": " api://vectrixdb ",
    }
    assert SignInConfig.from_env(tmp_path, env).oidc.api_audience == "api://vectrixdb"
    del env["VECTRIXDB_OIDC_API_AUDIENCE"]
    assert SignInConfig.from_env(tmp_path, env).oidc.api_audience is None
