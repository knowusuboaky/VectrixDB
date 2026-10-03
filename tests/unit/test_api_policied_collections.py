"""The built-in API does not serve a collection that carries a policy.

It cannot. Serving one honestly means deciding per document, deciding needs a
principal, and this API resolves none: there is no header it trusts and no
directory it calls. Every route that reaches a policied collection refuses,
and the two that never reached one, the collection page and the listing,
withhold the numbers instead.

Those numbers are the part worth being explicit about. Gating the content
reads and then reporting `size_bytes` leaves the same inference standing: a
size grows when documents are written, and a walled reader watching it move
learns that a client they were refused is being written to.
"""

from __future__ import annotations

import pytest

from vectrixdb.policy import Overlap, Policy

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

LENDING = Policy([Overlap("entitlements.allowed_roles", "roles")])


@pytest.fixture
def client(tmp_path):
    from vectrixdb import Vectrix
    from vectrixdb.api import server

    walled = Vectrix("walled", path=str(tmp_path), policy=LENDING)
    walled.add(
        ["a covenant for the other borrower"],
        metadata=[{"entitlements": {"allowed_roles": ["credit_analyst"]}}],
    )
    walled.close()

    plain = Vectrix("plain", path=str(tmp_path))
    plain.add(["an ordinary document"])
    plain.close()

    with TestClient(server.create_app(db_path=str(tmp_path), enable_dashboard=False)) as client:
        yield client


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/collections/walled",
        "/api/v1/collections/walled/points?limit=5",
    ],
)
def test_reading_a_policied_collection_is_refused(client, path):
    response = client.get(path)

    assert response.status_code == 403
    assert "resolve principals" in response.json()["detail"]


def test_the_refusal_does_not_repeat_the_library_s_advice(client):
    """The message the library raises tells a caller to use as_principal,
    which is true and is addressed to whoever wrote the service. The person
    holding an HTTP client cannot act on it."""
    detail = client.get("/api/v1/collections/walled").json()["detail"]

    assert "as_principal" not in detail


def test_the_listing_names_it_and_withholds_the_numbers(client):
    """An operator needs to know the collection is there. How much is in it
    is a different question, and size_bytes answers it just as well as a
    count does."""
    rows = {row["name"]: row for row in client.get("/api/v1/collections").json()["collections"]}

    assert rows["walled"]["entitlement_policy"] is True
    assert "count" not in rows["walled"]
    assert "size_bytes" not in rows["walled"]


def test_an_ordinary_collection_is_served_as_before(client):
    """The whole point of gating rather than disabling: a collection with no
    policy is untouched."""
    body = client.get("/api/v1/collections/plain").json()

    assert body["data"]["count"] == 1
    assert client.get("/api/v1/collections/plain/points?limit=5").status_code == 200


def test_the_listing_still_carries_ordinary_collections_whole(client):
    rows = {row["name"]: row for row in client.get("/api/v1/collections").json()["collections"]}

    assert rows["plain"]["count"] == 1


def test_a_name_nothing_answers_to_is_still_a_404(client):
    """A policied collection and a collection that is not there stay
    distinguishable, deliberately. The wall is over documents, not over which
    collections a deployment has, and an operator debugging a typo needs the
    difference."""
    assert client.get("/api/v1/collections/nope").status_code == 404
