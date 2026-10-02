"""A chunk's text is sent when somebody asks for that chunk, and not before.

A table of chunks used to be built by fetching every chunk on the page, text
and all, and the quality report and a provenance lookup each carried an
excerpt. So anybody who opened a tab had been sent the content of everything
on it, whether or not they read a word, and the access log could only say
"read points". Listings carry where a chunk came from now. Its text is one
request, for one chunk, and the log names it.

A whole document is a larger disclosure than a chunk, so opening one is its
own permission: an admin has it, and gives it to the operators who need it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import numpy as np
import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("cryptography", reason="the signin extra is not installed")

sys.path.insert(0, os.path.dirname(__file__))
from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.signin import OidcConfig, SignInConfig, roles, totp  # noqa: E402
from vectrixdb.signin.store import SignInStore  # noqa: E402

SECRET = "t" * 48
PUBLIC = "https://vectors.example.test"
# Long enough to be cut to an excerpt, and ordinary prose: one sentence said
# twelve times over, which this used to be, is a loop, and the quality score
# now says so.
LONG = (
    "Payment is due within thirty days of the invoice date. Interest accrues monthly on any balance left unpaid after that. "
    "A reminder goes out on the fifth day, and a second one a fortnight later. Customers in a declared disaster area can "
    "defer their payments for up to ninety days with no late fees. An adviser calls within a day to confirm the new schedule. "
    "Disputes are raised in writing and answered within ten working days. Refunds go back to the account the payment came from. "
    "Statements are issued at the end of each quarter and kept for seven years. Nothing in these terms limits a right the law gives."
)
NOISE = (
    "~~|| x9q#zz }{ ^^ kk3l@@ ]]w [[ 7h7h **%% qqqz ``` <<>> ||| ~~~ zxq9 #### $$$$ ^^^^ }}}} {{{{"
)
VECTORS = {LONG: [1, 0, 0, 0], NOISE: [0, 1, 0, 0]}


def _embed(texts):
    return np.array([VECTORS[t] for t in texts], dtype=np.float32)


def create_app(**kwargs):
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


@pytest.fixture
def root(tmp_path):
    base = tmp_path / "db"
    db = Vectrix("docs", path=str(base), dimension=4, embed_fn=_embed, embedding_cache=False)
    db.add(
        [LONG, NOISE],
        ids=["clean", "noisy"],
        metadata=[
            {"source": "terms.pdf", "_vx_quality": 0.97, "_vx_page": 3},
            {"source": "scan.pdf", "_vx_quality": 0.21},
        ],
    )
    db.close()
    return base


@pytest.fixture
def open_server(root, monkeypatch):
    for name in ("VECTRIXDB_API_KEY", "VECTRIXDB_SIGNIN"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app(db_path=str(root), enable_dashboard=False)) as client:
        yield client


class TestAListingHasNoText:
    def test_rows_say_where_a_chunk_came_from_and_nothing_it_says(self, open_server):
        data = open_server.get("/api/v1/collections/docs/points", params={"index": "true"}).json()[
            "data"
        ]
        by_id = {row["id"]: row for row in data["rows"]}
        assert by_id["clean"] == {
            "id": "clean",
            "quality": 0.97,
            "source": "terms.pdf",
            "document": None,
            "page": 3,
        }
        assert by_id["noisy"]["source"] == "scan.pdf"
        body = json.dumps(data)
        assert "Payment" not in body and "zxq9" not in body

    def test_without_the_parameter_the_reply_is_what_it_always_was(self, open_server):
        data = open_server.get("/api/v1/collections/docs/points").json()["data"]
        assert set(data) == {"ids", "limit", "offset", "total"}


class TestTheQualityReport:
    def test_it_says_why_and_whether_a_chunk_is_really_below_the_line(self, open_server):
        worst = open_server.get(
            "/api/v1/collections/docs/quality", params={"text": "false"}
        ).json()["data"]["worst"]
        assert [w["id"] for w in worst] == ["noisy", "clean"]
        noisy, clean = worst
        assert noisy["below_line"] is True and clean["below_line"] is False, (
            "the lowest of a clean collection is not bad"
        )
        assert noisy["reasons"] and "symbols and stray glyphs" in noisy["reasons"]
        assert clean["reasons"] == []
        assert all("text" not in w for w in worst)

    def test_a_program_that_wants_the_excerpt_still_gets_it(self, open_server):
        worst = open_server.get("/api/v1/collections/docs/quality").json()["data"]["worst"]
        assert all(len(w["text"]) <= 160 for w in worst) and worst[1]["text"].startswith("Payment")


class TestProvenance:
    def test_where_it_came_from_without_what_it_says(self, open_server):
        data = open_server.get(
            "/api/v1/collections/docs/provenance/clean", params={"text": "false"}
        ).json()["data"]
        assert "text" not in data and data["present"] is True
        assert "text" in open_server.get("/api/v1/collections/docs/provenance/clean").json()["data"]


class TestSearchExcerpts:
    SEARCH = "/api/v1/collections/docs/search"
    QUERY = {"query": [1, 0, 0, 0], "limit": 1}

    def test_the_cut_is_made_on_the_server(self, open_server):
        reply = open_server.post(self.SEARCH, params={"snippet": "80"}, json=self.QUERY)
        result = reply.json()["data"]["results"][0]
        assert result["id"] == "clean" and result["snipped"] is True
        assert len(result["text"]) <= 81 and result["text"].endswith("…")
        assert LONG.strip() not in reply.text, (
            "a button in the page would be decoration if the whole chunk had been sent"
        )

    def test_without_it_a_search_returns_the_text_because_programs_want_it(self, open_server):
        result = open_server.post(self.SEARCH, json=self.QUERY).json()["data"]["results"][0]
        assert result["text"] == LONG and "snipped" not in result

    def test_a_chunk_that_fits_is_not_marked_cut(self, open_server):
        result = open_server.post(self.SEARCH, params={"snippet": "2000"}, json=self.QUERY).json()[
            "data"
        ]["results"][0]
        assert result["snipped"] is False and result["text"] == LONG

    def test_a_keyword_result_carries_highlights_and_those_are_cut_too(self, open_server):
        result = open_server.post(
            "/api/v1/collections/docs/keyword-search",
            params={"snippet": "40"},
            json={"query_text": "payment invoice", "limit": 1},
        ).json()["data"]["results"][0]
        assert len(result["highlights"]) <= 3 and all(
            len(mark) <= 41 for mark in result["highlights"]
        )

    def test_a_number_that_is_not_one_changes_nothing(self, open_server):
        result = open_server.post(self.SEARCH, params={"snippet": "lots"}, json=self.QUERY).json()[
            "data"
        ]["results"][0]
        assert result["text"] == LONG


# ------------------------------------------------------- a whole document ---


class TestAWholeDocumentIsItsOwnPermission:
    def test_the_table(self):
        assert not roles.can("operator", "document.read"), "being an operator is not enough"
        assert roles.can("operator", "document.read", ["document.read"])
        assert roles.can("admin", "document.read")
        assert not roles.can("viewer", "document.read", ["document.read"]), (
            "a grant widens what an operator sees; it does not make a viewer into one"
        )
        assert roles.can("reader", "document.read"), (
            "the read-only key has always fetched documents"
        )

    def test_only_what_may_be_given_can_be_given(self):
        assert roles.GRANTABLE == {"document.read"}
        assert not roles.can("operator", "audit.read", ["audit.read"])
        assert not roles.can("operator", "collection.delete", ["collection.delete"])
        assert roles.clean_grants(["document.read", "audit.read", "people.manage", 7]) == [
            "document.read"
        ]

    @pytest.mark.parametrize(
        "method, path, action",
        [
            ("GET", "/api/v1/collections/docs/documents", "content.read"),
            ("GET", "/api/v1/collections/docs/documents/contracts/acme.pdf", "document.read"),
            ("GET", "/api/v1/documents", "content.read"),
            ("GET", "/api/v1/documents/d1", "document.read"),
            ("GET", "/api/v1/documents/d1/chunks", "document.read"),
            ("DELETE", "/api/v1/collections/docs/documents/contracts/acme.pdf", "content.write"),
        ],
    )
    def test_which_requests_open_one(self, method, path, action):
        assert roles.action_for(method, path) == action

    def test_a_group_may_carry_it_and_nothing_else(self):
        base = {"issuer": "https://idp.example.test", "client_id": "c", "default_role": "viewer"}
        assert OidcConfig(**base, grant_map={"g-legal": ["document.read"]}).grant_map
        with pytest.raises(ConfigurationError, match="cannot be given"):
            OidcConfig(**base, grant_map={"g-legal": ["audit.read"]})
        with pytest.raises(ConfigurationError, match="cannot be given"):
            OidcConfig(**base, grant_map={"g-legal": "document.read"})


class TestTheStoreKeepsWhatSomebodyWasGiven:
    def test_it_is_kept_cleaned_and_a_change_ends_their_sessions(self, tmp_path):
        store = SignInStore(tmp_path / "signin.db", [SECRET])
        store.put_person("olu@example.com", "operator", grants=["document.read", "audit.read"])
        assert store.person("olu@example.com").grants == ["document.read"]
        sid, session = store.open_session(
            subject="olu",
            email="olu@example.com",
            name=None,
            role="operator",
            principal={},
            method="email",
            hours=1,
            grants=["document.read"],
        )
        assert session.grants == ["document.read"] and store.session(sid).grants == [
            "document.read"
        ]
        store.put_person("olu@example.com", "operator")
        assert store.session(sid) is not None, "saying nothing about grants changes nothing"
        store.put_person("olu@example.com", "operator", grants=[])
        assert store.session(sid) is None, (
            "a session carries what it was opened with, so taking it away ends the session"
        )
        store.close()

    def test_a_file_from_before_this_is_brought_up_to_date(self, tmp_path):
        path = tmp_path / "old.db"
        old = sqlite3.connect(str(path))
        old.executescript(
            "CREATE TABLE people (email TEXT PRIMARY KEY, role TEXT NOT NULL, principal TEXT NOT NULL DEFAULT '{}', totp_secret TEXT, "
            "totp_confirmed INTEGER NOT NULL DEFAULT 0, last_step INTEGER, disabled INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, last_sign_in REAL);"
            "INSERT INTO people (email, role, created_at) VALUES ('ada@example.com', 'admin', 1.0);"
        )
        old.commit()
        old.close()
        store = SignInStore(path, [SECRET])
        assert store.person("ada@example.com").grants == []
        store.close()


class Mail:
    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, text):
        self.sent.append(text)


@pytest.fixture
def signed(root, monkeypatch):
    monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
    config = SignInConfig(
        methods=("email",),
        secrets=(SECRET,),
        public_url=PUBLIC,
        sender=Mail(),
        users=(
            ("ada@example.com", "admin"),
            ("olu@example.com", "operator"),
            ("vi@example.com", "viewer"),
        ),
        store_path=root / "auth" / "signin.db",
        access_log=root / "auth" / "access.jsonl",
    )
    with TestClient(
        create_app(db_path=str(root), enable_dashboard=False, signin=config), base_url=PUBLIC
    ) as client:
        yield client, config


def sign_in(server, email):
    import re

    client, config = server
    browser = TestClient(client.app, base_url=PUBLIC)
    browser.post("/auth/email/begin", json={"email": email})
    token = re.search(r"token=([\w-]+)", config.sender.sent[-1]).group(1)
    begun = browser.post("/auth/email/enrol/begin", json={"token": token}).json()["data"]
    done = browser.post(
        "/auth/email/enrol/confirm",
        json={"ticket": begun["ticket"], "code": totp.code_at(begun["secret"], totp.step_now())},
    )
    assert done.status_code == 200, done.text
    return browser, begun["secret"]


def token_of(browser):
    return {"x-csrf-token": browser.cookies.get("__Host-vx_csrf")}


class TestSignedIn:
    DOCUMENT = "/api/v1/collections/docs/documents/terms.pdf"

    def test_an_operator_is_refused_a_document_until_an_admin_says_otherwise(self, signed):
        ada, _ = sign_in(signed, "ada@example.com")
        olu, secret = sign_in(signed, "olu@example.com")
        assert olu.get(self.DOCUMENT).status_code == 403
        assert "document.read" not in olu.get("/auth/me").json()["data"]["actions"]
        assert ada.get(self.DOCUMENT).status_code == 404, (
            "an admin passes the door; there is simply no such document here"
        )

        given = ada.post(
            "/auth/people",
            json={
                "email": "olu@example.com",
                "role": "operator",
                "grants": ["document.read", "audit.read"],
            },
            headers=token_of(ada),
        )
        assert given.json()["data"]["grants"] == ["document.read"]
        assert olu.get("/api/v1/collections").status_code == 401, (
            "what they hold changed, so they sign in again"
        )

        again = TestClient(signed[0].app, base_url=PUBLIC)
        assert (
            again.post(
                "/auth/email/verify",
                json={
                    "email": "olu@example.com",
                    "code": totp.code_at(secret, totp.step_now() + 1),
                },
            ).status_code
            == 200
        )
        assert "document.read" in again.get("/auth/me").json()["data"]["actions"]
        assert again.get(self.DOCUMENT).status_code == 404
        assert again.get("/api/v1/audit").status_code == 403, "and it gave them nothing else"

    def test_a_viewer_gets_the_listing_and_never_a_chunk(self, signed):
        vi, _ = sign_in(signed, "vi@example.com")
        data = vi.get("/api/v1/collections/docs/points", params={"index": "true"}).json()["data"]
        assert {row["source"] for row in data["rows"]} == {
            "terms.pdf",
            "scan.pdf",
        } and "Payment" not in json.dumps(data)
        assert vi.get("/api/v1/collections/docs/points/clean").status_code == 403

    def test_the_log_names_the_chunk_and_the_document_that_were_opened(self, signed):
        ada, _ = sign_in(signed, "ada@example.com")
        ada.get("/api/v1/collections/docs/points", params={"index": "true"})
        ada.get("/api/v1/collections/docs/points/clean")
        ada.get(self.DOCUMENT)
        records = ada.get("/api/v1/access").json()["data"]["records"]
        opened = {(r["action"], r.get("item")) for r in records if r["event"] == "read"}
        assert ("content.read", "clean") in opened and ("document.read", "terms.pdf") in opened
        assert ("content.index", None) in opened, "a listing opens no one thing"
        olu, _ = sign_in(signed, "olu@example.com")
        olu.get(self.DOCUMENT)
        refused = [
            r for r in ada.get("/api/v1/access").json()["data"]["records"] if r["event"] == "denied"
        ]
        assert refused[0]["who"] == "olu@example.com" and refused[0]["item"] == "terms.pdf"
