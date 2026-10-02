"""Sign-in end to end, against the real server and a stand-in identity provider.

What is being held to: the four checks in their order (who, forgery, role,
entitlement), nothing readable without signing in, a viewer never sent chunk
text, a policy judging a signed-in person's search and the decision written
down, and every read, search, refusal and sign-in in the access log.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("jwt", reason="the signin extra is not installed")
pytest.importorskip("cryptography", reason="the signin extra is not installed")

sys.path.insert(0, os.path.dirname(__file__))
from fake_idp import ISSUER, FakeIdp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb import Vectrix  # noqa: E402


def create_app(**kwargs):
    """The server as it is now. Another test drops and re-imports ``vectrixdb.api``,
    and a name bound at the top of this file would go on pointing at the old copy,
    whose routes reach for a database the new copy's start-up never gave them."""
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


def refuse_open_server(*args, **kwargs):
    from vectrixdb.api.server import refuse_open_server as current

    return current(*args, **kwargs)


from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.policy import Overlap, Policy  # noqa: E402
from vectrixdb.signin import OidcConfig, SignInConfig, totp  # noqa: E402

SECRET = "k" * 48
PUBLIC = "https://vectors.example.test"
KEY = "the-full-api-key"
VECTORS = {
    "alpha": [1, 0, 0, 0],
    "beta": [0, 1, 0, 0],
    "gamma": [0, 0, 1, 0],
    "delta": [0.9, 0.1, 0, 0],
}


def _embed(texts):
    return np.array([VECTORS[t] for t in texts], dtype=np.float32)


@pytest.fixture
def data(tmp_path):
    """Two collections: one anybody entitled to content may read, one behind a policy."""
    root = tmp_path / "db"
    plain = Vectrix("plain", path=str(root), dimension=4, embed_fn=_embed, embedding_cache=False)
    plain.add(
        ["alpha", "beta"],
        ids=["p-alpha", "p-beta"],
        metadata=[{"source": "a.pdf"}, {"source": "b.pdf"}],
    )
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
        ["alpha", "beta", "gamma", "delta"],
        ids=["w-acme-1", "w-zeta-1", "w-zeta-2", "w-acme-2"],
        metadata=[
            {"client_id": "acme"},
            {"client_id": "zeta"},
            {"client_id": "zeta"},
            {"client_id": "acme"},
        ],
    )
    walled.close()
    return root


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
        "users": (
            ("ada@example.com", "admin"),
            ("olu@example.com", "operator"),
            ("vi@example.com", "viewer"),
        ),
        "store_path": root / "auth" / "signin.db",
        "access_log": root / "auth" / "access.jsonl",
        "sender": Mail(),
    }
    base.update(over)
    return SignInConfig(**base)


@pytest.fixture
def server(data, monkeypatch):
    monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
    monkeypatch.delenv("VECTRIXDB_AUDIT_JSONL", raising=False)
    config = _config(data)
    with TestClient(
        create_app(db_path=str(data), enable_dashboard=False, signin=config), base_url=PUBLIC
    ) as client:
        yield client, config


def enrol(client: TestClient, config: SignInConfig, email: str) -> tuple[str, list]:
    """The whole first visit. Returns the authenticator secret and the recovery codes."""
    assert client.post("/auth/email/begin", json={"email": email}).status_code == 200
    begun = client.post("/auth/email/enrol/begin", json={"token": config.sender.token()}).json()[
        "data"
    ]
    done = client.post(
        "/auth/email/enrol/confirm",
        json={"ticket": begun["ticket"], "code": totp.code_at(begun["secret"], totp.step_now())},
    )
    assert done.status_code == 200, done.text
    return begun["secret"], done.json()["data"]["recovery_codes"]


def person(server, email: str) -> TestClient:
    """A browser of their own, signed in as this person."""
    client, config = server
    browser = TestClient(client.app, base_url=PUBLIC)
    enrol(browser, config, email)
    return browser


def csrf(browser: TestClient) -> dict:
    return {"x-csrf-token": browser.cookies.get("__Host-vx_csrf")}


# ------------------------------------------------------------------ 1 · who


class TestNothingWithoutSigningIn:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/collections",
            "/api/v1/collections/plain",
            "/api/v1/collections/plain/points",
            "/api/v1/info",
            "/api/v1/audit",
            "/api/v1/access",
        ],
    )
    def test_a_read_is_a_401_not_a_page_of_data(self, server, path):
        reply = server[0].get(path)
        assert reply.status_code == 401 and reply.json()["data"] == {"signin": True}

    def test_the_page_is_told_how_people_sign_in(self, server):
        reply = server[0].get("/auth/me")
        assert reply.status_code == 401 and set(reply.json()["data"]["methods"]) == {"email"}

    def test_health_and_the_front_door_stay_open(self, server):
        assert server[0].get("/health").status_code == 200 and server[0].get("/auth/status").json()[
            "data"
        ]["signin"] == ["email"]

    def test_a_forged_or_foreign_cookie_is_nobody(self, server):
        client, _ = server
        for value in ("made-up", "made-up.signature", ""):
            client.cookies.set("__Host-vx_sid", value)
            assert client.get("/api/v1/collections").status_code == 401

    def test_the_key_still_works_for_machines(self, server):
        client, _ = server
        assert client.get("/api/v1/collections", headers={"api-key": KEY}).status_code == 200
        assert client.get("/api/v1/collections", headers={"api-key": "wrong"}).status_code == 401


class TestEmailSignIn:
    def test_first_visit_then_every_visit_after(self, server):
        client, config = server
        secret, codes = enrol(client, config, "ada@example.com")
        assert len(codes) == 10
        me = client.get("/auth/me").json()["data"]
        assert (
            me["person"]["email"] == "ada@example.com"
            and me["person"]["role"] == "admin"
            and me["sees_content"]
        )
        assert client.post("/auth/signout", headers=csrf(client)).status_code == 200
        assert client.get("/api/v1/collections").status_code == 401

        later = time.time() + 60
        again = client.post(
            "/auth/email/verify",
            json={"email": "ADA@example.com", "code": totp.code_at(secret, totp.step_now(later))},
        )
        # the code for a minute from now is outside the window, so this is refused, as it should be
        assert again.status_code == 401
        good = client.post(
            "/auth/email/verify",
            json={"email": "ADA@example.com", "code": totp.code_at(secret, totp.step_now() + 1)},
        )
        assert good.status_code == 200 and client.get("/api/v1/collections").status_code == 200

    def test_the_cookie_is_the_kind_script_cannot_read(self, server):
        client, config = server
        client.post("/auth/email/begin", json={"email": "ada@example.com"})
        begun = client.post(
            "/auth/email/enrol/begin", json={"token": config.sender.token()}
        ).json()["data"]
        done = client.post(
            "/auth/email/enrol/confirm",
            json={
                "ticket": begun["ticket"],
                "code": totp.code_at(begun["secret"], totp.step_now()),
            },
        )
        cookies = done.headers.get_list("set-cookie")
        sid = next(c for c in cookies if c.startswith("__Host-vx_sid="))
        forgery = next(c for c in cookies if c.startswith("__Host-vx_csrf="))
        assert "HttpOnly" in sid and "Secure" in sid and "samesite=strict" in sid.lower(), (
            "strict: no single sign-on redirect to allow for"
        )
        assert "path=/" in sid.lower() and "domain=" not in sid.lower(), (
            "__Host- binds it to this host and every path"
        )
        assert "HttpOnly" not in forgery, "the page has to read this one to put it in a header"

    def test_the_answer_is_the_same_for_every_address(self, server):
        client, config = server
        listed = client.post("/auth/email/begin", json={"email": "ada@example.com"})
        stranger = client.post("/auth/email/begin", json={"email": "mallory@example.com"})
        assert listed.json() == stranger.json()
        assert [to for to, _, _ in config.sender.sent] == ["ada@example.com"], (
            "and only a listed address is written to"
        )

    def test_the_link_is_in_the_part_of_an_address_a_server_never_sees(self, server):
        client, config = server
        client.post("/auth/email/begin", json={"email": "ada@example.com"})
        link = re.search(r"https://\S+", config.sender.sent[-1][2]).group(0)
        assert (
            link.startswith(PUBLIC + "/dashboard/#/enrol?token=")
            and "token" not in urlsplit(link).query
        )

    def test_a_link_works_once(self, server):
        client, config = server
        client.post("/auth/email/begin", json={"email": "ada@example.com"})
        token = config.sender.token()
        assert client.post("/auth/email/enrol/begin", json={"token": token}).status_code == 200
        assert client.post("/auth/email/enrol/begin", json={"token": token}).status_code == 400

    def test_a_mistyped_code_at_set_up_can_be_tried_again(self, server):
        client, config = server
        client.post("/auth/email/begin", json={"email": "ada@example.com"})
        begun = client.post(
            "/auth/email/enrol/begin", json={"token": config.sender.token()}
        ).json()["data"]
        wrong = client.post(
            "/auth/email/enrol/confirm", json={"ticket": begun["ticket"], "code": "000000"}
        )
        assert wrong.status_code == 401
        right = client.post(
            "/auth/email/enrol/confirm",
            json={
                "ticket": wrong.json()["data"]["ticket"],
                "code": totp.code_at(begun["secret"], totp.step_now()),
            },
        )
        assert right.status_code == 200

    def test_five_wrong_codes_and_the_right_one_no_longer_works(self, server):
        client, config = server
        secret, _ = enrol(client, config, "ada@example.com")
        for _ in range(5):
            assert (
                client.post(
                    "/auth/email/verify", json={"email": "ada@example.com", "code": "000000"}
                ).status_code
                == 401
            )
        locked = client.post(
            "/auth/email/verify",
            json={"email": "ada@example.com", "code": totp.code_at(secret, totp.step_now() + 1)},
        )
        assert locked.status_code == 429

    def test_a_recovery_code_signs_in_once(self, server):
        client, config = server
        _, codes = enrol(client, config, "ada@example.com")
        fresh = TestClient(client.app, base_url=PUBLIC)
        first = fresh.post(
            "/auth/email/verify", json={"email": "ada@example.com", "code": codes[0]}
        )
        assert first.status_code == 200 and first.json()["data"]["recovery_codes_left"] == 9
        assert (
            TestClient(client.app, base_url=PUBLIC)
            .post("/auth/email/verify", json={"email": "ada@example.com", "code": codes[0]})
            .status_code
            == 401
        )

    def test_a_stranger_cannot_enrol_or_sign_in(self, server):
        client, _ = server
        assert (
            client.post(
                "/auth/email/verify", json={"email": "mallory@example.com", "code": "123456"}
            ).status_code
            == 401
        )
        assert client.post("/auth/email/enrol/begin", json={"token": "guess"}).status_code == 400


# -------------------------------------------------------------- 2 · forgery


class TestForgery:
    def test_a_change_needs_the_token_as_well_as_the_cookie(self, server):
        ada = person(server, "ada@example.com")
        body = {"name": "made", "dimension": 4}
        assert ada.post("/api/v1/collections", json=body).status_code == 403
        assert (
            ada.post(
                "/api/v1/collections", json=body, headers={"x-csrf-token": "guess"}
            ).status_code
            == 403
        )
        assert ada.post("/api/v1/collections", json=body, headers=csrf(ada)).status_code == 200

    def test_a_search_is_a_post_so_it_needs_it_too(self, server):
        olu = person(server, "olu@example.com")
        assert (
            olu.post(
                "/api/v1/collections/plain/search", json={"query": VECTORS["alpha"], "limit": 2}
            ).status_code
            == 403
        )

    def test_somebody_signed_out_gets_the_401_that_sends_them_to_sign_in(self, server):
        ada = person(server, "ada@example.com")
        token = csrf(ada)
        ada.post("/auth/signout", headers=token)
        assert (
            ada.post(
                "/api/v1/collections", json={"name": "x", "dimension": 4}, headers=token
            ).status_code
            == 401
        )

    def test_a_key_needs_no_token_because_no_browser_sends_one_by_itself(self, server):
        reply = server[0].post(
            "/api/v1/collections", json={"name": "bykey", "dimension": 4}, headers={"api-key": KEY}
        )
        assert reply.status_code == 200


# ----------------------------------------------------------------- 3 · role


class TestRoles:
    def test_a_viewer_sees_that_things_exist_and_never_what_they_say(self, server):
        vi = person(server, "vi@example.com")
        assert vi.get("/api/v1/collections").status_code == 200
        assert vi.get("/api/v1/collections/plain/health").status_code == 200
        listing = vi.get("/api/v1/collections/plain/points").json()["data"]
        assert sorted(listing["ids"]) == ["p-alpha", "p-beta"] and listing["text_hidden"] is True
        provenance = vi.get("/api/v1/collections/plain/provenance/p-alpha").json()["data"]
        assert provenance["text_hidden"] is True and "text" not in provenance
        for refused in (
            vi.get("/api/v1/collections/plain/points/p-alpha"),
            vi.post(
                "/api/v1/collections/plain/search",
                json={"query": VECTORS["alpha"]},
                headers=csrf(vi),
            ),
            vi.post("/api/v1/collections/plain/points", json={"points": []}, headers=csrf(vi)),
            vi.get("/api/v1/audit"),
        ):
            assert refused.status_code == 403
        everything = json.dumps([listing, provenance])
        assert "alpha" not in everything.replace("p-alpha", ""), "chunk text reached a viewer"

    def test_an_operator_reads_and_searches_and_is_not_an_admin(self, server):
        olu = person(server, "olu@example.com")
        assert olu.get("/api/v1/collections/plain/points/p-alpha").json()["data"]["id"] == "p-alpha"
        found = olu.post(
            "/api/v1/collections/plain/search",
            json={"query": VECTORS["alpha"], "limit": 1},
            headers=csrf(olu),
        )
        assert found.status_code == 200 and found.json()["data"]["results"][0]["id"] == "p-alpha"
        assert "text" in olu.get("/api/v1/collections/plain/provenance/p-alpha").json()["data"]
        assert olu.delete("/api/v1/collections/plain", headers=csrf(olu)).status_code == 403
        assert (
            olu.get("/api/v1/audit").status_code == 403
            and olu.get("/api/v1/access").status_code == 403
            and olu.get("/auth/people").status_code == 403
        )

    def test_a_role_in_a_request_is_not_a_role(self, server):
        vi = person(server, "vi@example.com")
        reply = vi.get(
            "/api/v1/collections/plain/points/p-alpha",
            headers={"x-role": "admin", "role": "admin"},
            params={"role": "admin"},
        )
        assert reply.status_code == 403

    def test_the_read_only_key_reads_and_does_not_search_or_write(self, server, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_READ_ONLY_API_KEY", "ro-key")
        client, _ = server
        ro = {"api-key": "ro-key"}
        assert client.get("/api/v1/collections/plain/points/p-alpha", headers=ro).status_code == 200
        assert (
            client.post(
                "/api/v1/collections/plain/points", json={"points": []}, headers=ro
            ).status_code
            == 403
        )
        assert client.get("/api/v1/audit", headers=ro).status_code == 403


class TestPeople:
    def test_an_admin_adds_changes_resets_and_removes(self, server):
        client, config = server
        ada = person(server, "ada@example.com")
        assert {p["email"] for p in ada.get("/auth/people").json()["data"]["people"]} == {
            "ada@example.com",
            "olu@example.com",
            "vi@example.com",
        }
        assert (
            ada.post(
                "/auth/people",
                json={
                    "email": "New@Example.com",
                    "role": "viewer",
                    "principal": {"clients": ["acme"]},
                },
                headers=csrf(ada),
            ).status_code
            == 200
        )
        assert (
            ada.post(
                "/auth/people", json={"email": "x@example.com", "role": "root"}, headers=csrf(ada)
            ).status_code
            == 400
        )

        olu = person(server, "olu@example.com")
        assert ada.post("/auth/people/olu@example.com/reset", headers=csrf(ada)).status_code == 200
        assert olu.get("/api/v1/collections").status_code == 401, "a reset signs them out"
        olu.post("/auth/email/begin", json={"email": "olu@example.com"})
        assert config.sender.sent[-1][0] == "olu@example.com", (
            "and their next visit starts with a new link"
        )

        assert ada.delete("/auth/people/new@example.com", headers=csrf(ada)).status_code == 200
        assert ada.delete("/auth/people/nobody@example.com", headers=csrf(ada)).status_code == 404

    def test_the_last_admin_cannot_be_removed_or_demoted(self, server):
        ada = person(server, "ada@example.com")
        assert ada.delete("/auth/people/ada@example.com", headers=csrf(ada)).status_code == 409
        assert (
            ada.post(
                "/auth/people",
                json={"email": "ada@example.com", "role": "viewer"},
                headers=csrf(ada),
            ).status_code
            == 409
        )


# ---------------------------------------------------------- 4 · entitlement


class TestAPolicyJudgesWhoeverSignedIn:
    @pytest.fixture
    def walled(self, data, monkeypatch, tmp_path):
        monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
        monkeypatch.setenv("VECTRIXDB_AUDIT_JSONL", str(tmp_path / "audit.jsonl"))
        config = _config(data)
        with TestClient(
            create_app(db_path=str(data), enable_dashboard=False, signin=config), base_url=PUBLIC
        ) as client:
            ada = TestClient(client.app, base_url=PUBLIC)
            enrol(ada, config, "ada@example.com")
            ada.post(
                "/auth/people",
                json={
                    "email": "olu@example.com",
                    "role": "operator",
                    "principal": {"clients": ["acme"]},
                },
                headers=csrf(ada),
            )
            olu = TestClient(client.app, base_url=PUBLIC)
            enrol(olu, config, "olu@example.com")
            yield client, olu, tmp_path / "audit.jsonl"

    def test_a_key_is_nobody_so_the_collection_stays_shut_to_it(self, walled):
        client, _, _ = walled
        assert (
            client.post(
                "/api/v1/collections/walled/search",
                json={"query": VECTORS["alpha"]},
                headers={"api-key": KEY},
            ).status_code
            == 403
        )

    def test_a_person_gets_what_they_are_entitled_to_and_nothing_says_what_else_exists(
        self, walled
    ):
        _, olu, _ = walled
        reply = olu.post(
            "/api/v1/collections/walled/search",
            json={"query": VECTORS["beta"], "limit": 10},
            headers=csrf(olu),
        )
        assert reply.status_code == 200
        body = reply.json()["data"]
        assert {r["id"] for r in body["results"]} == {"w-acme-1", "w-acme-2"}
        assert "total_searched" not in body and "zeta" not in reply.text
        listing = olu.get("/api/v1/collections/walled/points").json()["data"]
        assert sorted(listing["ids"]) == ["w-acme-1", "w-acme-2"] and listing["total"] == 2

    def test_not_there_and_not_yours_are_one_answer(self, walled):
        _, olu, _ = walled
        assert olu.get("/api/v1/collections/walled/points/w-acme-1").status_code == 200
        theirs = olu.get("/api/v1/collections/walled/points/w-zeta-1")
        missing = olu.get("/api/v1/collections/walled/points/no-such-point")
        assert theirs.status_code == missing.status_code == 404

    def test_the_decision_is_written_down_under_their_name(self, walled):
        _, olu, audit = walled
        olu.post(
            "/api/v1/collections/walled/search",
            json={"query": VECTORS["beta"], "limit": 10},
            headers=csrf(olu),
        )
        record = json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])
        assert record["principal_id"] == "olu@example.com" and record["collection"] == "walled"
        assert (
            record["results_returned"] == 2
            and record["withheld_disclosable"] + record["withheld_undisclosable"] == 2
        )
        assert record["principal_snapshot"]["clients"] == ["acme"]

    def test_a_search_that_cannot_be_recorded_is_not_served(self, walled, monkeypatch, tmp_path):
        _, olu, _ = walled
        blocked = tmp_path / "is-a-folder"
        blocked.mkdir()
        monkeypatch.setenv("VECTRIXDB_AUDIT_JSONL", str(blocked))
        reply = olu.post(
            "/api/v1/collections/walled/search", json={"query": VECTORS["beta"]}, headers=csrf(olu)
        )
        assert reply.status_code == 503 and "results" not in reply.text


# ------------------------------------------------------------ the access log


class TestTheAccessLog:
    def test_who_signed_in_who_read_who_searched_and_who_was_refused(self, server):
        ada, vi = person(server, "ada@example.com"), person(server, "vi@example.com")
        ada.get("/api/v1/collections/plain/points/p-alpha")
        ada.post(
            "/api/v1/collections/plain/search", json={"query": VECTORS["alpha"]}, headers=csrf(ada)
        )
        vi.get("/api/v1/collections/plain/points/p-alpha")
        TestClient(server[0].app, base_url=PUBLIC).post(
            "/auth/email/verify", json={"email": "ada@example.com", "code": "000000"}
        )

        records = ada.get("/api/v1/access").json()["data"]["records"]
        seen = {(r["event"], r.get("who"), r.get("collection")) for r in records}
        assert ("signin", "ada@example.com", None) in seen and (
            "enrolled",
            "vi@example.com",
            None,
        ) in seen
        assert ("read", "ada@example.com", "plain") in seen and (
            "search",
            "ada@example.com",
            "plain",
        ) in seen
        assert ("denied", "vi@example.com", "plain") in seen and (
            "signin_failed",
            "ada@example.com",
            None,
        ) in seen
        assert "alpha" not in json.dumps(records).replace("p-alpha", ""), (
            "the log repeats nothing it guards"
        )

    def test_a_read_that_cannot_be_recorded_does_not_happen(self, server, monkeypatch):
        ada = person(server, "ada@example.com")
        runtime = server[0].app.state.signin
        monkeypatch.setattr(
            runtime.access, "path", runtime.access.path.parent
        )  # a folder: it cannot be appended to
        assert ada.get("/api/v1/collections/plain/points/p-alpha").status_code == 503
        assert ada.get("/api/v1/collections").status_code == 200, (
            "what is not logged is not held up"
        )


# ------------------------------------------------------------ single sign-on


@pytest.fixture
def sso(data, monkeypatch):
    monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
    idp = FakeIdp()
    oidc = OidcConfig(
        issuer=ISSUER,
        client_id="vectrixdb",
        client_secret="s3cret",
        role_map={"g-admins": "admin", "g-readers": "viewer", "g-ops": "operator"},
        principal_claims={"clients": "groups"},
        groups_url="https://graph.example.test/groups",
        allowed_emails=("*",),
    )
    config = _config(data, methods=("oidc",), oidc=oidc, users=())
    with TestClient(
        create_app(
            db_path=str(data), enable_dashboard=False, signin=config, oidc_transport=idp.transport
        ),
        base_url=PUBLIC,
        follow_redirects=False,
    ) as client:
        yield client, idp


def sign_in(client: TestClient, idp: FakeIdp, to: str = "/dashboard/#/search"):
    start = client.get("/auth/oidc/start", params={"to": to})
    assert start.status_code == 302
    code, state = idp.authorize(start.headers["location"])
    return client.get("/auth/oidc/callback", params={"code": code, "state": state})


def refused(reply) -> str:
    assert reply.status_code == 302 and reply.headers["location"].startswith(
        "/dashboard/#/signin?error="
    )
    return reply.headers["location"].split("error=")[1]


class TestSingleSignOn:
    def test_the_whole_way_round(self, sso):
        client, idp = sso
        back = sign_in(client, idp)
        assert back.status_code == 302 and back.headers["location"] == "/dashboard/#/search"
        me = client.get("/auth/me").json()["data"]["person"]
        assert me == {
            **me,
            "email": "ada@example.com",
            "name": "Ada Lovelace",
            "role": "admin",
            "method": "oidc",
            "subject": "u-1",
        }
        assert client.get("/api/v1/collections").status_code == 200

    def test_the_request_to_the_provider(self, sso):
        client, idp = sso
        query = parse_qs(urlsplit(client.get("/auth/oidc/start").headers["location"]).query)
        assert query["redirect_uri"] == [PUBLIC + "/auth/oidc/callback"], (
            "from configuration, never from the request"
        )
        assert (
            query["code_challenge_method"] == ["S256"]
            and len(query["state"][0]) >= 24
            and len(query["nonce"][0]) >= 24
        )

    def test_the_highest_role_among_the_groups_wins_and_no_group_means_no_entry(self, sso):
        client, idp = sso
        idp.person["groups"] = ["g-readers", "g-ops", "unrelated"]
        sign_in(client, idp)
        assert client.get("/auth/me").json()["data"]["person"]["role"] == "operator"
        idp.person["groups"] = ["unrelated"]
        other = TestClient(client.app, base_url=PUBLIC, follow_redirects=False)
        assert refused(sign_in(other, idp)) == "no_role"
        assert other.get("/api/v1/collections").status_code == 401

    def test_groups_become_the_principal_a_policy_judges(self, sso):
        client, idp = sso
        idp.person["groups"] = ["g-ops", "acme"]
        sign_in(client, idp)
        found = client.post(
            "/api/v1/collections/walled/search",
            json={"query": VECTORS["beta"], "limit": 10},
            headers=csrf(client),
        )
        assert {r["id"] for r in found.json()["data"]["results"]} == {"w-acme-1", "w-acme-2"}

    def test_groups_that_did_not_fit_are_fetched_across_pages(self, sso):
        client, idp = sso
        idp.overflow, idp.graph_groups, idp.graph_pages = True, ["g-1", "g-2", "g-ops", "g-4"], 2
        sign_in(client, idp)
        assert client.get("/auth/me").json()["data"]["person"]["role"] == "operator"
        assert (
            sum(1 for _, url in idp.calls if url.startswith("https://graph.example.test/groups"))
            == 2
        )

    def test_groups_that_did_not_fit_and_nowhere_to_fetch_them_is_a_refusal_not_a_guess(self, sso):
        client, idp = sso
        idp.overflow = True
        client.app.state.signin.oidc.config.groups_url = None
        assert refused(sign_in(client, idp)) == "groups_overflow"

    @pytest.mark.parametrize(
        "sabotage",
        [
            lambda idp: setattr(idp, "token_issuer", "https://evil.example.test"),
            lambda idp: setattr(idp, "token_audience", "another-application"),
            lambda idp: setattr(idp, "expires_in", -600),
            lambda idp: setattr(idp, "issued_ago", 3600) or setattr(idp, "expires_in", 7200),
            lambda idp: setattr(idp, "sign_with_unpublished_key", True),
            lambda idp: setattr(idp, "unsigned", True),
            lambda idp: setattr(idp, "drop_nonce", True),
        ],
        ids=[
            "another issuer",
            "another audience",
            "expired",
            "minted an hour ago",
            "a key nobody published",
            "alg none",
            "no nonce",
        ],
    )
    def test_a_token_that_is_wrong_in_any_way_signs_nobody_in(self, sso, sabotage):
        client, idp = sso
        sabotage(idp)
        refused(sign_in(client, idp))
        assert (
            client.get("/api/v1/collections").status_code == 401
            and "__Host-vx_sid" not in client.cookies
        )

    def test_a_callback_nobody_started_and_a_state_from_elsewhere(self, sso):
        client, idp = sso
        assert (
            refused(client.get("/auth/oidc/callback", params={"code": "c", "state": "s"}))
            == "expired"
        )
        start = client.get("/auth/oidc/start")
        code, _ = idp.authorize(start.headers["location"])
        assert (
            refused(
                client.get("/auth/oidc/callback", params={"code": code, "state": "somebody-elses"})
            )
            == "state"
        )

    def test_a_code_cannot_be_redeemed_in_another_browser(self, sso):
        client, idp = sso
        code, state = idp.authorize(client.get("/auth/oidc/start").headers["location"])
        thief = TestClient(client.app, base_url=PUBLIC, follow_redirects=False)
        thief.get(
            "/auth/oidc/start"
        )  # their own verifier, which does not match the challenge the code was issued for
        _, their_state = (
            None,
            parse_qs(urlsplit(thief.get("/auth/oidc/start").headers["location"]).query)["state"][0],
        )
        refused(thief.get("/auth/oidc/callback", params={"code": code, "state": their_state}))
        assert thief.get("/api/v1/collections").status_code == 401

    @pytest.mark.parametrize(
        "to",
        ["https://evil.example.test/", "//evil.example.test", "/\\evil", "javascript:alert(1)"],
    )
    def test_signing_in_never_sends_somebody_to_another_site(self, sso, to):
        client, idp = sso
        assert sign_in(client, idp, to=to).headers["location"] == "/dashboard/"

    def test_the_provider_being_down_is_said_plainly(self, sso):
        client, idp = sso
        idp.down = True
        assert refused(client.get("/auth/oidc/start")) == "provider_unreachable"

    def test_a_new_session_every_time(self, sso):
        client, idp = sso
        sign_in(client, idp)
        first = client.cookies.get("__Host-vx_sid")
        sign_in(client, idp)
        assert client.cookies.get("__Host-vx_sid") != first
        stale = TestClient(client.app, base_url=PUBLIC)
        stale.cookies.set("__Host-vx_sid", first)
        assert stale.get("/api/v1/collections").status_code == 401, (
            "the old one is dead, not merely replaced"
        )


class TestOtherSites:
    def test_no_other_site_may_read_this_one_as_whoever_is_signed_in(self, server):
        ada = person(server, "ada@example.com")
        reply = ada.get("/api/v1/collections", headers={"Origin": "https://evil.example.test"})
        assert "access-control-allow-origin" not in {k.lower() for k in reply.headers}


class TestAnOpenServerStaysOnTheMachine:
    def test_it_will_not_listen_to_the_network_with_no_key_and_no_sign_in(self, monkeypatch):
        for name in ("VECTRIXDB_API_KEY", "VECTRIXDB_SIGNIN", "VECTRIXDB_ALLOW_OPEN"):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(ConfigurationError, match="refusing to listen on 0.0.0.0"):
            refuse_open_server("0.0.0.0")
        for host in ("127.0.0.1", "localhost", "::1"):
            refuse_open_server(host)
        refuse_open_server("0.0.0.0", api_key="k")
        monkeypatch.setenv("VECTRIXDB_SIGNIN", "oidc")
        refuse_open_server("0.0.0.0")
        monkeypatch.delenv("VECTRIXDB_SIGNIN")
        monkeypatch.setenv("VECTRIXDB_ALLOW_OPEN", "1")
        refuse_open_server("0.0.0.0")

    def test_the_default_is_this_machine(self):
        import inspect

        from vectrixdb.api.server import run_server

        assert inspect.signature(run_server).parameters["host"].default == "127.0.0.1"


class TestWithSignInOffNothingChanged:
    def test_open_with_no_key_and_reads_without_one_when_there_is(self, data, monkeypatch):
        monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
        monkeypatch.delenv("VECTRIXDB_SIGNIN", raising=False)
        with TestClient(create_app(db_path=str(data), enable_dashboard=False)) as client:
            assert client.get("/auth/me").json()["data"] == {"signin": False}
            assert client.get("/api/v1/collections/plain/points/p-alpha").status_code == 200
            assert (
                client.post(
                    "/api/v1/collections", json={"name": "open", "dimension": 4}
                ).status_code
                == 200
            )
            monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
            assert client.get("/api/v1/collections/plain/points/p-alpha").status_code == 200
            assert (
                client.post(
                    "/api/v1/collections", json={"name": "shut", "dimension": 4}
                ).status_code
                == 401
            )
            assert (
                client.post(
                    "/api/v1/collections",
                    json={"name": "shut", "dimension": 4},
                    headers={"api-key": KEY},
                ).status_code
                == 200
            )
