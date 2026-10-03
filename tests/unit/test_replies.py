"""Every refusal the server sends is one shape, whichever layer refused.

There were two: the door answered ``{"ok", "message", "data"}`` and a route
answered FastAPI's ``{"detail"}``. A client had to know which layer had said
no to find the sentence. Now every refusal has all four keys, ``message`` is
always a sentence, and ``detail`` is still what it was, so nothing that read
either shape broke.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")

from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.api.replies import collection_not_found, message_of, refusal_content  # noqa: E402

KEYS = {"ok", "message", "data", "detail"}
KEY = "the-admin-api-key"
ADMIN = {"api-key": KEY}


def _embed(texts):
    return np.array([[1, 0, 0, 0] for _ in texts], dtype=np.float32)


@pytest.fixture
def client(tmp_path, monkeypatch):
    from vectrixdb.api.server import create_app

    monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
    monkeypatch.delenv("VECTRIXDB_AUDIT_JSONL", raising=False)
    one = Vectrix(
        "handbook", path=str(tmp_path), dimension=4, embed_fn=_embed, embedding_cache=False
    )
    one.add(["alpha"], ids=["a"])
    one.close()
    with TestClient(create_app(db_path=str(tmp_path), enable_dashboard=False)) as made:
        yield made


class TestTheSentence:
    def test_a_string_is_its_own_sentence(self):
        assert message_of("Collection 'nope' not found") == "Collection 'nope' not found"

    def test_field_errors_are_said_in_words_without_where_fastapi_looked(self):
        detail = [
            {"type": "missing", "loc": ["body", "query_text"], "msg": "Field required"},
            {
                "type": "int_parsing",
                "loc": ["query", "limit"],
                "msg": "Input should be a valid integer",
            },
        ]
        assert (
            message_of(detail)
            == "query_text: Field required; limit: Input should be a valid integer"
        )

    def test_a_nested_field_keeps_its_path(self):
        assert (
            message_of([{"loc": ["body", "points", 0, "id"], "msg": "Field required"}])
            == "points.0.id: Field required"
        )

    @pytest.mark.parametrize(
        "detail, said",
        [
            (None, "The request was refused"),
            ([], "The request is not valid"),
            ({"message": "said so"}, "said so"),
            (42, "42"),
        ],
    )
    def test_anything_else_still_gives_a_sentence(self, detail, said):
        assert message_of(detail) == said

    def test_the_body_has_all_four_keys_and_detail_defaults_to_the_message(self):
        assert refusal_content("no") == {"ok": False, "message": "no", "data": None, "detail": "no"}
        assert refusal_content("no", detail=[1], data={"a": 1}) == {
            "ok": False,
            "message": "no",
            "data": {"a": 1},
            "detail": [1],
        }


class TestOneShapeFromEveryLayer:
    def ask(self, client):
        return {
            "a route that found nothing": client.get("/api/v1/collections/nope", headers=ADMIN),
            "a route that found no point": client.get(
                "/api/v1/collections/handbook/points/zzz", headers=ADMIN
            ),
            "a body that is the wrong shape": client.post(
                "/api/v1/collections/handbook/text-search", json={"limit": 2}, headers=ADMIN
            ),
            "a query that is the wrong type": client.get(
                "/api/v1/collections/handbook/points?limit=many", headers=ADMIN
            ),
            "the door, with a wrong key": client.post(
                "/api/v1/collections",
                json={"name": "x", "dimension": 4},
                headers={"api-key": "wrong"},
            ),
            "the door, with no key": client.post(
                "/api/v1/collections", json={"name": "x", "dimension": 4}
            ),
            "a delete of what is not there": client.delete(
                "/api/v1/collections/nope", headers=ADMIN
            ),
            "a route that does not exist": client.get("/api/v1/no-such-route", headers=ADMIN),
            "a method the route does not take": client.put("/api/v1/info", headers=ADMIN),
        }

    def test_every_refusal_has_the_four_keys(self, client):
        for what, reply in self.ask(client).items():
            assert reply.status_code >= 400, what
            body = reply.json()
            assert set(body) == KEYS, f"{what}: {body}"
            assert body["ok"] is False and isinstance(body["message"], str) and body["message"], (
                what
            )

    def test_detail_is_still_what_a_client_of_the_old_shape_read(self, client):
        asked = self.ask(client)
        assert asked["a route that found nothing"].json()["detail"] == "Collection 'nope' not found"
        wrong = asked["a body that is the wrong shape"]
        assert wrong.status_code == 422 and wrong.json()["detail"][0]["loc"] == [
            "body",
            "query_text",
        ]
        assert wrong.json()["message"] == "query_text: Field required"

    def test_a_validation_reply_does_not_send_the_callers_input_back(self, client):
        reply = client.post(
            "/api/v1/collections/handbook/text-search",
            json={"limit": 2, "secret": "s3cr3t"},
            headers=ADMIN,
        )
        assert reply.status_code == 422 and "s3cr3t" not in reply.text

    def test_a_401_still_says_how_to_authenticate(self, client):
        reply = client.post(
            "/api/v1/collections", json={"name": "x", "dimension": 4}, headers={"api-key": "wrong"}
        )
        assert reply.status_code in (401, 403) and set(reply.json()) == KEYS

    def test_not_found_is_one_reply_whoever_sends_it(self, client):
        import json

        from_the_route = client.get("/api/v1/collections/nope", headers=ADMIN).json()
        assert from_the_route == json.loads(collection_not_found("nope").body)


def test_no_module_builds_a_refusal_by_hand():
    """A refusal written out where it is sent is how the second shape came about."""
    api = Path(__file__).resolve().parents[2] / "vectrixdb" / "api"
    by_hand = re.compile(r'content=\{\s*"(detail|ok)"\s*:\s*(False|f?")')
    found = [
        f"{path.name}:{n}"
        for path in sorted(api.glob("*.py"))
        if path.name != "replies.py"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if by_hand.search(line)
    ]
    assert not found, f"use vectrixdb.api.replies here: {found}"
