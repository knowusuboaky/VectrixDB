"""Masking email addresses, phone numbers and card numbers in what people are shown.

What is held to. The shapes are found where they stand and the sentence still
reads: an address keeps its first letter and its domain, a phone number and a
card their last four digits. Dates, times, decimals, versions and ids are not
touched, and a sixteen-digit number that fails the Luhn check is not a card.
On the server it is per collection, set by an admin beside who can see it,
and applied to every reply of that collection's routes that goes to a person
or a guest, metadata included, while an API key is sent the text as stored.
Ids stay ids, so the page can still open what it was shown. A collection
deleted and made again starts unmasked.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from vectrixdb.masking import MASK, mask_text, mask_value

M = MASK


# ------------------------------------------------------------------ text ---


class TestTheShapes:
    @pytest.mark.parametrize(
        "text, shown",
        [
            ("write to ada@example.com today", f"write to a{M * 3}@example.com today"),
            ("j.doe+tag@mail.company.co.uk", f"j{M * 3}@mail.company.co.uk"),
            ("+1 416 555 0199", f"+{M} {M * 3} {M * 3} 0199"),
            ("(416) 555-0199", f"({M * 3}) {M * 3}-0199"),
            ("416.555.0199", f"{M * 3}.{M * 3}.0199"),
            ("4165550199", f"{M * 6}0199"),
            ("+44 20 7946 0958", f"+{M * 2} {M * 2} {M * 4} 0958"),
            ("card 4111 1111 1111 1111", f"card {M * 4} {M * 4} {M * 4} 1111"),
            ("5500-0000-0000-0004", f"{M * 4}-{M * 4}-{M * 4}-0004"),
        ],
    )
    def test_what_is_masked_keeps_enough_to_recognise(self, text, shown):
        assert mask_text(text) == shown

    @pytest.mark.parametrize(
        "text",
        [
            "Pi is 3.14159265",
            "Meeting 2026-09-19 10:30:00 in room 4",
            "Invoice 2026-09-19, due 2026/10/19",
            "Version 2.2.0, port 7337, year 2026",
            "build_5c1e0a97b2d44f10 and chunk deferment:0",
            "order 12345678",
            "a non-card 1234 5678 9012 3456",
            "2025 2024 2023 Total revenue",
            "the plan runs through 2027-2028",
            "fiscal 2025 / 2024 / 2023 (2022)",
            "",
        ],
    )
    def test_what_is_not_an_identifier_is_left_as_it_is(self, text):
        assert mask_text(text) == text

    def test_a_whole_result_is_masked_and_its_ids_are_not(self):
        result = {
            "id": "ada@example.com:0",
            "doc_id": "ada@example.com",
            "text": "Call 416-555-0199",
            "metadata": {
                "email": "ada@example.com",
                "tags": ["vip", "ada@example.com"],
                "count": 3,
            },
            "highlights": ["ada@example.com"],
        }
        shown = mask_value(result, keep=lambda key: key == "id" or key.endswith("_id"))
        assert shown["id"] == "ada@example.com:0" and shown["doc_id"] == "ada@example.com", (
            "an id is sent back to open the chunk"
        )
        assert shown["text"] == f"Call {M * 3}-{M * 3}-0199"
        assert shown["metadata"] == {
            "email": f"a{M * 3}@example.com",
            "tags": ["vip", f"a{M * 3}@example.com"],
            "count": 3,
        }
        assert shown["highlights"] == [f"a{M * 3}@example.com"]


# ---------------------------------------------------------------- server ---

fastapi = pytest.importorskip("fastapi", reason="the API extra is not installed")
pytest.importorskip("cryptography", reason="the signin extra is not installed")

from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.signin import SignInConfig, totp  # noqa: E402

SECRET = "m" * 48
PUBLIC = "https://vectors.example.test"
KEY = "the-full-api-key"
KEYED = {"api-key": KEY}
CALL = "Ada Lovelace called from 416-555-0199 about card 4111 1111 1111 1111; write to ada@example.com."
QUIET = "Refunds are handled by the billing team."
VECTORS = {CALL: [1, 0, 0, 0], QUIET: [0, 1, 0, 0]}


def create_app(**kwargs):
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


class Mail:
    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, text):
        self.sent.append((to, subject, text))

    def token(self):
        return re.search(r"#/enrol\?token=([\w-]+)", self.sent[-1][2]).group(1)


@pytest.fixture(autouse=True)
def quiet_environment(monkeypatch):
    for name in (
        "VECTRIXDB_API_KEY_FILE",
        "VECTRIXDB_API_KEY_SHA256",
        "VECTRIXDB_READ_ONLY_API_KEY",
        "VECTRIXDB_PATH",
        "VECTRIXDB_STORAGE_BACKEND",
        "VECTRIXDB_CACHE_BACKEND",
        "VECTRIXDB_AUDIT_JSONL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)


def _embed(texts):
    return np.array([VECTORS[t] for t in texts], dtype=np.float32)


def _collection(root: Path) -> None:
    calls = Vectrix("calls", path=str(root), dimension=4, embed_fn=_embed, embedding_cache=False)
    calls.add(
        [CALL, QUIET],
        ids=["c-1", "c-2"],
        metadata=[{"source": "calls.txt", "email": "ada@example.com"}, {"source": "faq.txt"}],
    )
    calls.close()


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "db"
    _collection(root)
    config = SignInConfig(
        methods=("email",),
        secrets=(SECRET,),
        public_url=PUBLIC,
        guests=True,
        sender=Mail(),
        users=(("olu@example.com", "operator"),),
        store_path=root / "auth" / "signin.db",
        access_log=root / "auth" / "access.jsonl",
    )
    with TestClient(
        create_app(db_path=str(root), enable_dashboard=False, signin=config), base_url=PUBLIC
    ) as client:
        yield client, config, root


def person(server) -> TestClient:
    """A browser of its own, signed in as the operator."""
    client, config, _ = server
    browser = TestClient(client.app, base_url=PUBLIC)
    assert browser.post("/auth/email/begin", json={"email": "olu@example.com"}).status_code == 200
    begun = browser.post("/auth/email/enrol/begin", json={"token": config.sender.token()}).json()[
        "data"
    ]
    done = browser.post(
        "/auth/email/enrol/confirm",
        json={"ticket": begun["ticket"], "code": totp.code_at(begun["secret"], totp.step_now())},
    )
    assert done.status_code == 200, done.text
    return browser


def point(browser, headers=None) -> dict:
    reply = browser.get("/api/v1/collections/calls/points/c-1", headers=headers or {})
    assert reply.status_code == 200, reply.text
    return reply.json()["data"]


def everything(value) -> str:
    return repr(value)


class TestOnTheServer:
    def test_a_person_sees_it_masked_and_the_ids_stay(self, server):
        seen = point(person(server))
        said = everything(seen)
        for raw in ("416-555-0199", "4111 1111 1111 1111", "ada@example.com"):
            assert raw not in said, raw
        assert f"a{M * 3}@example.com" in said and "0199" in said and "1111" in said
        assert seen["id"] == "c-1", "the id is what opens the chunk again"

    def test_a_search_is_masked_for_a_person_metadata_included(self, server):
        browser = person(server)
        token = {"x-csrf-token": browser.cookies.get("__Host-vx_csrf")}
        reply = browser.post(
            "/api/v1/collections/calls/search",
            json={"query": [1, 0, 0, 0], "limit": 2},
            headers=token,
        )
        assert reply.status_code == 200, reply.text
        results = reply.json()["data"]["results"]
        assert [r["id"] for r in results][0] == "c-1"
        assert "ada@example.com" not in everything(results) and "416-555-0199" not in everything(
            results
        )

    def test_a_key_is_sent_the_text_as_stored(self, server):
        assert "416-555-0199" in everything(point(server[0], headers=KEYED)), (
            "a script feeding a pipeline needs the text"
        )

    def test_a_guest_cannot_search_so_there_is_nothing_to_mask(self, server):
        guest = TestClient(server[0].app, base_url=PUBLIC)
        reply = guest.post(
            "/api/v1/collections/calls/search", json={"query": [1, 0, 0, 0], "limit": 1}
        )
        assert reply.status_code == 401 and reply.json()["data"] == {"signin": True}, reply.text
        assert "ada@example.com" not in reply.text and "416-555-0199" not in reply.text


class TestMarkdownOnTheWayOut:
    """A kept document comes back as Markdown, not JSON, and is masked the same way."""

    def test_text_replies_are_masked(self):
        from starlette.applications import Starlette
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        from vectrixdb.api.masked import MaskingMiddleware

        class Runtime:
            def masked(self, name):
                return name == "calls"

        class Caller:
            method = "email"

        class Who(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.caller = Caller()
                return await call_next(request)

        async def document(request):
            return PlainTextResponse(CALL, media_type="text/markdown; charset=utf-8")

        app = Starlette(routes=[Route("/api/v1/collections/{name}/documents/{doc}", document)])
        app.add_middleware(Who)
        app.add_middleware(MaskingMiddleware)
        app.state.signin = Runtime()
        client = TestClient(app)
        masked = client.get("/api/v1/collections/calls/documents/call.md")
        assert (
            masked.status_code == 200
            and "ada@example.com" not in masked.text
            and "0199" in masked.text
        )
        assert masked.headers["content-type"].startswith("text/markdown")
        assert (
            "ada@example.com" not in client.get("/api/v1/collections/other/documents/call.md").text
        ), "every collection, always"
