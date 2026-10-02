"""Who may retrieve from a collection: the policy in its record, and the check before every search.

A collection answers the people its policy names, by security group from
their token or by the address they signed in with on the collection's own
list, and nobody else: not a collection with no policy, not a stranger, not a
key that was not made for it. What is held to. The decision is one function,
the same for a real search and for the dashboard's "check someone". On the
server, somebody a policy does not name
is not told the collection exists; an admin is told why; a guest sees a
public collection as excerpts; deleting a collection takes its record with it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import types
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake_records import FakeContainer, FakePostgres, FakeTable  # noqa: E402

from vectrixdb.collection_access import AccessPolicy, decide  # noqa: E402
from vectrixdb.collection_records import CollectionRecord, CollectionRecords  # noqa: E402
from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.signin.records import CosmosRecords, DynamoRecords, Records, SqlRecords  # noqa: E402

HR, RISK, OPS = "00000000-0000-0000-0000-000000000101", "00000000-0000-0000-0000-000000000102", "00000000-0000-0000-0000-000000000103"
#: In HR or RiskTeam AND on the list: the people the tests below let in.
HR_LISTED = [{"email": e} for e in ("ama@company.com", "kofi@company.com", "olu@example.com", "ada@example.com")]
HR_GROUPS = AccessPolicy("token", [{"id": HR, "name": "HR"}, {"id": RISK, "name": "RiskTeam"}], people=HR_LISTED)
PEOPLE = AccessPolicy("store", [{"email": "Ama@Company.com"}, {"email": "kofi@company.com"}, {"domain": "partner.example"}])
AT_EXAMPLE = AccessPolicy("store", [{"domain": "example.com"}])

BACKENDS = {
    "sqlite": lambda tmp: SqlRecords.sqlite(tmp / "members.db"),
    "postgresql": lambda tmp: SqlRecords.postgres("postgresql://vx@db.example.test/access", connect=FakePostgres().connect),
    "cosmos": lambda tmp: CosmosRecords(FakeContainer()),
    "dynamodb": lambda tmp: DynamoRecords(FakeTable()),
}


class Flaky(Records):
    """A store that stops answering when told to."""

    def __init__(self, inner: Records):
        self.inner, self.down = inner, False

    def _up(self):
        if self.down:
            raise ConnectionError("the database is not answering")

    def get(self, kind, key):
        self._up()
        return self.inner.get(kind, key)

    def create(self, record):
        return self.inner.create(record)

    def replace(self, record):
        return self.inner.replace(record)

    def put(self, record):
        self.inner.put(record)

    def delete(self, kind, key, version=None):
        return self.inner.delete(kind, key, version)

    def query(self, kind, *, ix1=None, ix2=None):
        self._up()
        return self.inner.query(kind, ix1=ix1, ix2=ix2)

    def purge(self):
        self.inner.purge()

    def describe(self):
        return "the test's store"

    def close(self):
        self.inner.close()


# ---------------------------------------------------------------- the policy ---


class TestThePolicy:
    def test_the_two_methods_as_a_person_writes_them(self):
        assert HR_GROUPS.to_dict() == {"method": "token", "allow": [{"id": HR, "name": "HR"}, {"id": RISK, "name": "RiskTeam"}], "people": HR_LISTED}
        assert PEOPLE.to_dict() == {"method": "store", "allow": [{"email": "ama@company.com"}, {"email": "kofi@company.com"}, {"domain": "partner.example"}]}, "lower case, and no names on people"
        for policy in (HR_GROUPS, PEOPLE, AT_EXAMPLE):
            assert AccessPolicy.from_dict(policy.to_dict()) == policy
        assert AccessPolicy.from_dict({"method": "Token", "allow": [{"id": " x "}], "people": [{"email": " Ama@Company.com "}]}).to_dict() == {"method": "token", "allow": [{"id": "x"}], "people": [{"email": "ama@company.com"}]}
        twice = AccessPolicy("store", [{"email": "ama@company.com"}, {"email": "AMA@company.com"}, {"domain": "@company.com"}])
        assert twice.to_dict()["allow"] == [{"email": "ama@company.com"}, {"domain": "company.com"}], "listed twice is listed once"

    def test_the_earlier_spellings_still_read(self):
        assert AccessPolicy.from_dict({"method": "groups", "source": "token", "allow": [{"id": HR, "name": "HR"}], "people": [{"email": "ama@company.com"}]}) == AccessPolicy("token", [{"id": HR, "name": "HR"}], people=[{"email": "ama@company.com"}])
        assert AccessPolicy.from_dict({"method": "people", "allow": [{"email": "ama@company.com", "name": "Ama"}]}) == AccessPolicy("store", [{"email": "ama@company.com"}]), "people is the store, and the name goes"

    def test_in_words(self):
        assert HR_GROUPS.describe() == "security groups HR, RiskTeam, narrowed to 4 people on the list"
        assert PEOPLE.describe() == "ama@company.com, kofi@company.com, everyone at partner.example"
        assert AT_EXAMPLE.describe() == "everyone at example.com"

    @pytest.mark.parametrize(
        "bad, said",
        [
            ({"method": "roles"}, "token or store"),
            ({"method": "everyone"}, "an earlier spelling"),
            ({"method": "token"}, "at least one entry"),
            ({"method": "store"}, "at least one entry"),
            ({"method": "token", "allow": [{"name": "HR"}]}, "needs its id"),
            ({"method": "store", "allow": [{"email": "ama"}]}, "not an email"),
            ({"method": "store", "allow": [{"email": "ama@company.com", "domain": "company.com"}]}, "not both"),
            ({"method": "store", "allow": [{"domain": "@company"}]}, "not a domain"),
            ({"method": "token", "allow": ["hr"]}, "an object"),
            ({"method": "token", "allow": [{"id": "x"}]}, "Add the people who may search. A group alone would let everyone in it search."),
            ({"method": "token", "allow": [{"id": "x"}], "people": []}, "A group alone would let everyone in it search"),
            ({"method": "token", "allow": [{"id": "x"}], "people": [{"domain": "company.com"}]}, "a domain would let everyone in the group"),
            ({"method": "token", "allow": [{"id": "x"}], "people": [{"email": "ama"}]}, "not an email"),
            ({"method": "token", "allow": [{"id": "x"}], "people": ["ama@company.com"]}, "an object with an email"),
        ],
    )
    def test_what_cannot_be_a_policy_is_refused_where_it_is_written(self, bad, said):
        with pytest.raises(ConfigurationError, match=said):
            AccessPolicy.from_dict(bad)


# -------------------------------------------------------------- the decision ---


class TestTheDecision:
    def person(self, policy, **who):
        return decide(policy, collection="hr", method="oidc", **who)

    def test_no_policy_answers_nobody(self):
        refused = self.person(None, email="ama@company.com", groups=[HR])
        assert not refused.allowed and refused.code == "no_policy" and "unavailable to anyone yet" in refused.because

    def test_groups_from_the_token(self):
        yes = self.person(HR_GROUPS, email="ama@company.com", groups=[OPS, HR.upper()])
        assert yes.allowed and yes.code == "in_token" and yes.because == "ama@company.com is in HR and on the list, which may retrieve from hr"
        unlisted = self.person(HR_GROUPS, email="lin@company.com", groups=[HR])
        assert not unlisted.allowed and unlisted.code == "not_on_list", "in the group is not enough: not everyone in it may search"
        assert unlisted.because == "lin@company.com is in HR, but not on the list for hr"
        listed_elsewhere = self.person(HR_GROUPS, email="ama@company.com", groups=[OPS])
        assert listed_elsewhere.code == "not_in_token", "on the list is not enough either: the group comes first"
        assert yes.method == "token" and "source" not in yes.to_dict()
        no = self.person(HR_GROUPS, email="kofi@company.com", groups=[OPS])
        assert not no.allowed and no.code == "not_in_token"
        none = self.person(HR_GROUPS, email="kofi@company.com", groups=[])
        assert none.code == "not_in_token" and "the token names no groups" in none.because

    def test_the_store_is_a_list_of_people_by_email_or_domain(self):
        assert self.person(PEOPLE, email="AMA@company.com").code == "on_list"
        at = self.person(PEOPLE, email="kwame@partner.example")
        assert at.allowed and "everyone there" in at.because
        assert self.person(PEOPLE, email="ama@elsewhere.example").code == "not_on_list"
        assert self.person(PEOPLE, email="ama@partner.example.evil.test").code == "not_on_list"
        assert self.person(PEOPLE, subject="u-1").code == "not_on_list", "a token with no email cannot be on a list of emails"
        assert decide(PEOPLE, collection="hr", method="guest").code == "not_signed_in"
        assert decide(PEOPLE, collection="hr", method="oidc").code == "not_signed_in", "nobody at all"

    def test_a_key_is_nobody_unless_it_is_the_hosts_or_made_for_the_collection(self):
        assert decide(HR_GROUPS, collection="hr", method="key", host_key=True).code == "host_key"
        assert decide(HR_GROUPS, collection="hr", method="key", key_scope=["hr", "misc"]).code == "key_scoped"
        refused = decide(HR_GROUPS, collection="hr", method="key", key_scope=["misc"])
        assert not refused.allowed and refused.code == "key_not_scoped"
        assert decide(AT_EXAMPLE, collection="hr", method="key").code == "key_not_scoped", "even a whole domain: a key is not somebody"
        assert decide(None, collection="hr", method="key", host_key=True).code == "no_policy", "and no policy is nobody, the host included"


# ---------------------------------------------------------------- the record ---


class TestTheRecord:
    def test_as_a_person_writes_it(self):
        written = {"name": "financial", "path": "raw/financial/", "policy": HR_GROUPS.to_dict()}
        record = CollectionRecord.from_json(written)
        assert record.policy_object() == HR_GROUPS
        assert record.to_json() == written
        assert CollectionRecord.from_json(json.dumps(written)) == record

    def test_the_older_shape_still_reads(self):
        old = {
            "id": "financial", "folder": "raw/financial/", "index": "financial",
            "policy": {"version": 1, "label": "client coverage", "rules": [{"kind": "Overlap", "doc": "client_id", "principal": "clients", "scope": True}]},
            "visibility": {"audience": "everyone", "guest_text": "chunks"},
        }
        record = CollectionRecord.from_json({**old, "masking": True})
        assert record.path == "raw/financial/" and record.policy is None, "a policy with rules was a per-document policy, which reads as nobody yet"
        assert record.to_json() == {"name": "financial", "path": "raw/financial/", "policy": None}, "visibility and masking are read past"

    def test_what_cannot_be_a_record_is_refused(self, tmp_path):
        with pytest.raises(ConfigurationError, match="the last folder is the collection"):
            CollectionRecord("financial", "g", path="raw/media/")
        with pytest.raises(ConfigurationError, match="null is nobody yet"):
            CollectionRecord("financial", "g", policy={})
        (tmp_path / "media.json").write_text(json.dumps({"name": "financial"}), encoding="utf-8")
        with pytest.raises(ConfigurationError, match="media.json says it is the record for 'financial'"):
            CollectionRecord.from_json(tmp_path / "media.json")

    def test_the_store_keeps_the_policy_and_who_set_it(self, tmp_path):
        store = CollectionRecords(SqlRecords.sqlite(tmp_path / "c.db"), fresh_for=0)
        store.set_policy("hr", HR_GROUPS, by="ada@example.com")
        assert store.get("hr").policy_object() == HR_GROUPS and store.get("hr").changed_by == "ada@example.com"
        store.set_policy("hr", AT_EXAMPLE)
        assert store.get("hr").policy_object() == AT_EXAMPLE
        store.set_policy("hr", None)
        assert store.get("hr").policy is None, "nobody yet"
        assert store.delete("hr") and store.get("hr") is None


# -------------------------------------------------------------- the server ---

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

from fastapi.testclient import TestClient  # noqa: E402

from fake_idp import ISSUER, FakeIdp  # noqa: E402

from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.signin import OidcConfig, SignInConfig, totp  # noqa: E402

SECRET = "k" * 48
PUBLIC = "https://vectors.example.test"
KEY = "the-full-api-key"
VECTORS = {"alpha": [1, 0, 0, 0], "beta": [0, 1, 0, 0]}


def _embed(texts):
    return np.array([VECTORS[t] for t in texts], dtype=np.float32)


class Mail:
    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, text):
        self.sent.append((to, subject, text))

    def token(self):
        return re.search(r"#/enrol\?token=([\w-]+)", self.sent[-1][2]).group(1)


def create_app(**kwargs):
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


@pytest.fixture
def data(tmp_path):
    """Three collections with nothing to hide in them: what is gated is who may search them at all."""
    root = tmp_path / "db"
    for name in ("hr", "handbook", "finance"):
        db = Vectrix(name, path=str(root), dimension=4, embed_fn=_embed, embedding_cache=False)
        db.add(["alpha", "beta"], ids=[f"{name}-alpha", f"{name}-beta"])
        db.close()
    return root


def _config(root: Path, **over) -> SignInConfig:
    base = {
        "methods": ("email",),
        "secrets": (SECRET,),
        "public_url": PUBLIC,
        # Two admins and two operators, so one of each can be in HR and one not; a viewer, who may look and never search.
        "users": (("ada@example.com", "admin"), ("bea@example.com", "admin"), ("olu@example.com", "operator"), ("kofi@example.com", "operator"), ("vi@example.com", "viewer")),
        "store_path": root / "auth" / "signin.db",
        "access_log": root / "auth" / "access.jsonl",
        "sender": Mail(),
    }
    base.update(over)
    return SignInConfig(**base)


@pytest.fixture
def gated(data, monkeypatch):
    """A server whose collections are gated by their records, with sign-in on and guests allowed."""
    monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
    for name in ("VECTRIXDB_AUDIT_JSONL", "VECTRIXDB_COLLECTION_STORE"):
        monkeypatch.delenv(name, raising=False)
    flaky = Flaky(SqlRecords.sqlite(data / "auth" / "collections.db"))
    records = CollectionRecords(flaky, fresh_for=0)
    records.set_policy("hr", HR_GROUPS)
    records.set_policy("handbook", AT_EXAMPLE)
    config = _config(data, guests=True)
    app = create_app(db_path=str(data), enable_dashboard=False, signin=config, collection_store=records)
    with TestClient(app, base_url=PUBLIC) as client:
        yield types.SimpleNamespace(client=client, config=config, records=records, flaky=flaky, root=data)


def enrol(client: TestClient, config: SignInConfig, email: str) -> None:
    assert client.post("/auth/email/begin", json={"email": email}).status_code == 200
    begun = client.post("/auth/email/enrol/begin", json={"token": config.sender.token()}).json()["data"]
    done = client.post("/auth/email/enrol/confirm", json={"ticket": begun["ticket"], "code": totp.code_at(begun["secret"], totp.step_now())})
    assert done.status_code == 200, done.text


def person(gated, email: str, groups=()) -> TestClient:
    """A browser of their own, signed in as this person, in these groups on the server's own list."""
    store = gated.client.app.state.signin.store
    found = store.person(email)
    store.put_person(email, found.role, {**found.principal, "groups": list(groups)})
    browser = TestClient(gated.client.app, base_url=PUBLIC)
    enrol(browser, gated.config, email)
    return browser


def csrf(browser: TestClient) -> dict:
    return {"x-csrf-token": browser.cookies.get("__Host-vx_csrf")}


def search(browser: TestClient, name: str, **headers):
    """A search as this browser: with its forgery token when it is somebody's session, or the headers given."""
    if not headers and browser.cookies.get("__Host-vx_csrf"):
        headers = csrf(browser)
    return browser.post(f"/api/v1/collections/{name}/search", json={"query": VECTORS["alpha"], "limit": 5}, headers=headers or None)


def names(browser: TestClient, **headers) -> list:
    reply = browser.get("/api/v1/collections", headers=headers or None)
    assert reply.status_code == 200, reply.text
    return sorted(c["name"] for c in reply.json()["collections"])


def access(gated, **query) -> list:
    reply = gated.client.get("/api/v1/access", params={"limit": 1000, **query}, headers={"api-key": KEY})
    assert reply.status_code == 200, reply.text
    return reply.json()["data"]["records"]


class TestWhoMayRetrieve:
    def test_somebody_in_the_group_searches_and_somebody_who_is_not_finds_nothing_there(self, gated):
        olu = person(gated, "olu@example.com", groups=[HR])
        found = search(olu, "hr")
        assert found.status_code == 200 and found.json()["data"]["results"][0]["id"] == "hr-alpha"
        kofi = person(gated, "kofi@example.com", groups=[OPS])
        refused = search(kofi, "hr")
        assert refused.status_code == 403 and refused.json()["data"]["code"] == "not_in_token"
        bea = person(gated, "bea@example.com", groups=[HR])
        unlisted = search(bea, "hr")
        assert unlisted.status_code == 403 and unlisted.json()["data"]["code"] == "not_on_list", "in HR and not on the list"
        assert "hr" in refused.json()["message"], "told why: the collection is there for everyone to see, and its answers are not"
        assert kofi.get("/api/v1/collections/hr").status_code == 200, "its name and its size are everyone's"
        nowhere = search(kofi, "nowhere")
        assert nowhere.status_code == 403 and nowhere.json()["data"]["code"] == "no_policy", "a name with no record answers nobody, whether or not it exists"
        denied = {r["who"]: r for r in access(gated, event="denied") if r.get("collection") == "hr"}
        assert denied["kofi@example.com"]["reason"] == "not_in_token" and denied["kofi@example.com"]["status"] == 403
        assert denied["bea@example.com"]["reason"] == "not_on_list" and denied["bea@example.com"]["status"] == 403, "the unlisted refusal is logged too"

    def test_a_collection_with_no_policy_answers_nobody_and_says_so_to_an_admin(self, gated):
        ada = person(gated, "ada@example.com", groups=[HR])
        refused = search(ada, "finance")
        assert refused.status_code == 403
        assert refused.json()["data"]["code"] == "no_policy" and "unavailable to anyone yet" in refused.json()["message"]
        assert search(ada, "hr").status_code == 200, "an admin is gated like anybody, and is in HR"
        bea = person(gated, "bea@example.com", groups=[OPS])
        assert search(bea, "hr").json()["data"]["code"] == "not_in_token", "an admin who is not is told why, not told it is not there"
        assert bea.get("/api/v1/collections/hr").status_code == 200, "and still manages it"

    def test_the_list_names_every_collection_for_everyone(self, gated):
        for browser in (person(gated, "vi@example.com", groups=[HR]), person(gated, "olu@example.com", groups=[OPS]), person(gated, "ada@example.com")):
            assert names(browser) == ["finance", "handbook", "hr"], "that a collection exists is everyone's; its answers are the policy's"
        assert names(TestClient(gated.client.app, base_url=PUBLIC)) == ["finance", "handbook", "hr"], "a guest too"

    def test_who_may_search_is_told_as_the_list_to_people_and_as_counts_to_a_guest(self, gated):
        kofi = person(gated, "kofi@example.com")
        assert search(kofi, "handbook").status_code == 200
        guest = TestClient(gated.client.app, base_url=PUBLIC)
        assert search(guest, "handbook").status_code == 401, "a guest never searches"
        seen = person(gated, "ada@example.com").get("/api/v1/policies").json()["data"]
        assert seen["gated"] is True
        assert seen["collections"]["handbook"] == {"method": "store", "people": 0, "domains": 1, "groups": 0, "policied": False, "policy": AT_EXAMPLE.to_dict()}
        assert seen["collections"]["hr"]["groups"] == 2 and seen["collections"]["finance"]["method"] is None
        counts = guest.get("/api/v1/policies")
        assert counts.status_code == 200 and counts.json()["data"]["collections"]["handbook"] == {"method": "store", "people": 0, "domains": 1, "groups": 0, "policied": False}

    def test_the_store_is_the_list_of_people(self, gated):
        gated.records.set_policy("finance", AccessPolicy("store", [{"email": "ada@example.com"}]))
        ada = person(gated, "ada@example.com")
        assert search(ada, "finance").status_code == 200, "on the list"
        gated.records.set_policy("finance", AccessPolicy("store", [{"email": "bea@example.com"}]))
        assert search(ada, "finance").json()["data"]["code"] == "not_on_list", "taken off, and the next question is refused"
        gated.records.set_policy("finance", AT_EXAMPLE)
        assert search(ada, "finance").status_code == 200, "everyone at the domain"

    def test_a_key_is_nobody_unless_the_hosts_or_made_for_the_collection(self, gated):
        assert search(gated.client, "hr", **{"api-key": KEY}).status_code == 200, "the server's own key is the host's hand"
        ada = person(gated, "ada@example.com")
        made = ada.post("/api/v1/keys", json={"name": "hr-nightly", "role": "searcher", "collections": ["hr"]}, headers=csrf(ada))
        assert made.status_code == 200, made.text
        scoped = made.json()["data"]["key"]
        assert search(gated.client, "hr", **{"api-key": scoped}).status_code == 200
        assert search(gated.client, "handbook", **{"api-key": scoped}).status_code == 404, "its scope, and no further: a key is not told what it cannot reach"
        wide = ada.post("/api/v1/keys", json={"name": "reports", "role": "searcher"}, headers=csrf(ada)).json()["data"]["key"]
        assert search(gated.client, "hr", **{"api-key": wide}).status_code == 403, "a key for every collection was made for none of them"

    def test_a_records_store_that_is_down_is_a_503(self, gated):
        kofi = person(gated, "kofi@example.com")
        gated.flaky.down = True
        down = search(kofi, "ledger")
        assert down.status_code == 503 and "could not be read" in down.json()["message"], "a record never read cannot stand in, so nothing is searched"
        gated.flaky.down = False

    def test_a_policy_is_set_through_the_api_and_every_change_is_recorded(self, gated):
        ada = person(gated, "ada@example.com")
        vi = person(gated, "vi@example.com")
        assert vi.put("/api/v1/collections/finance/policy", json={"policy": AT_EXAMPLE.to_dict()}, headers=csrf(vi)).status_code == 403
        bad = ada.put("/api/v1/collections/finance/policy", json={"policy": {"method": "token"}}, headers=csrf(ada))
        assert bad.status_code == 400 and "at least one entry" in bad.json()["detail"]
        alone = ada.put("/api/v1/collections/finance/policy", json={"policy": {"method": "token", "allow": [{"id": HR, "name": "HR"}]}}, headers=csrf(ada))
        assert alone.status_code == 400 and alone.json()["detail"] == "Add the people who may search. A group alone would let everyone in it search."
        set_ = ada.put("/api/v1/collections/finance/policy", json={"policy": HR_GROUPS.to_dict()}, headers=csrf(ada))
        assert set_.status_code == 200 and set_.json()["data"]["policy"]["allow"] == HR_GROUPS.to_dict()["allow"]
        assert len(set_.json()["data"]["policy"]["people"]) == 4, "its list comes back masked, as every collection route's reply to a person does"
        assert gated.records.get("finance").policy_object() == HR_GROUPS
        assert ada.put("/api/v1/collections/nowhere/policy", json={"policy": None}, headers=csrf(ada)).status_code == 404
        changed = access(gated, event="policy_changed")
        assert changed and changed[0]["collection"] == "finance" and changed[0]["reason"] == "security groups HR, RiskTeam, narrowed to 4 people on the list" and changed[0]["by"] == "ada@example.com"

    def test_deleting_a_collection_takes_its_record_with_it(self, gated):
        ada = person(gated, "ada@example.com", groups=[HR])
        assert ada.delete("/api/v1/collections/hr", headers=csrf(ada)).status_code == 200
        assert gated.records.get("hr") is None
        made = ada.post("/api/v1/collections", json={"name": "hr", "dimension": 4}, headers=csrf(ada))
        assert made.status_code == 200, made.text
        assert search(ada, "hr").json()["data"]["code"] == "no_policy", "made again, it answers nobody until it is given a policy"


class TestCheckSomeone:
    def test_an_admin_tries_an_address_and_groups_and_nothing_is_searched(self, gated):
        ada = person(gated, "ada@example.com")
        check = lambda **body: ada.post("/api/v1/access/check", json=body, headers=csrf(ada))  # noqa: E731
        yes = check(collection="hr", email="kofi@company.com", groups=[HR]).json()["data"]
        assert yes == {"collection": "hr", "allowed": True, "code": "in_token", "because": "kofi@company.com is in HR and on the list, which may retrieve from hr", "method": "token"}
        assert check(collection="hr", email="kofi@company.com").json()["data"]["code"] == "not_in_token"
        assert check(collection="finance", email="kofi@company.com").json()["data"]["code"] == "no_policy"
        assert check(collection="handbook", email="anyone@anywhere.test").json()["data"]["code"] == "not_on_list", "the store is the list, and they are not on it"
        assert check(collection="handbook", email="olu@example.com").json()["data"]["code"] == "on_list"
        gated.records.set_policy("finance", PEOPLE)
        assert check(collection="finance", email="kwame@partner.example").json()["data"]["allowed"] is True
        assert check(collection="finance", email="kojo@elsewhere.example", groups=[HR]).json()["data"]["code"] == "not_on_list", "groups given mean nothing to a list of emails"
        checked = access(gated, event="access_checked")
        assert checked and checked[0]["who"] == "ada@example.com" and checked[0]["collection"] == "finance" and "reason" in checked[0]
        assert not [r for r in access(gated, event="search") if r.get("who") == "kofi@company.com"], "nothing was searched"

    def test_only_an_admin_checks(self, gated):
        vi = person(gated, "vi@example.com")
        assert vi.post("/api/v1/access/check", json={"collection": "hr", "email": "x@y.test"}, headers=csrf(vi)).status_code == 403


class TestSingleSignOn:
    def test_the_groups_in_the_token_are_what_the_policy_reads(self, data, monkeypatch):
        monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
        monkeypatch.delenv("VECTRIXDB_AUDIT_JSONL", raising=False)
        records = CollectionRecords(SqlRecords.sqlite(data / "auth" / "collections.db"), fresh_for=0)
        records.set_policy("hr", HR_GROUPS)
        idp = FakeIdp()
        oidc = OidcConfig(
            issuer=ISSUER, client_id="vectrixdb", client_secret="s3cret", api_audience="api://vectrixdb", role_map={"g-ops": "operator"}, allowed_emails=("*",),
        )
        config = SignInConfig(methods=("oidc",), secrets=(SECRET,), public_url=PUBLIC, oidc=oidc, users=(), store_path=data / "auth" / "signin.db", access_log=data / "auth" / "access.jsonl")
        app = create_app(db_path=str(data), enable_dashboard=False, signin=config, oidc_transport=idp.transport, collection_store=records)
        with TestClient(app, base_url=PUBLIC) as client:
            idp.person = {"sub": "u-7", "email": "olu@example.com", "name": "Olu", "groups": ["g-ops", HR]}
            head = {"Authorization": f"Bearer {idp.access_token()}"}
            assert search(client, "hr", **head).status_code == 200
            idp.person["groups"] = ["g-ops"]
            assert search(client, "hr", **{"Authorization": f"Bearer {idp.access_token()}"}).status_code == 403
            assert names(client, **head) == ["finance", "handbook", "hr"], "every collection is listed; the token decides what answers"


class TestWithSignInOff:
    def test_a_gated_server_without_sign_in_answers_its_own_key_and_nobody_else(self, data, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
        monkeypatch.setenv("VECTRIXDB_READ_ONLY_API_KEY", "read-only")
        monkeypatch.delenv("VECTRIXDB_SIGNIN", raising=False)
        records = CollectionRecords(SqlRecords.sqlite(data / "auth" / "collections.db"), fresh_for=0)
        records.set_policy("hr", AT_EXAMPLE)
        with TestClient(create_app(db_path=str(data), enable_dashboard=False, signin=SignInConfig(methods=()), collection_store=records), base_url=PUBLIC) as client:
            assert search(client, "hr", **{"api-key": KEY}).status_code == 200
            assert search(client, "hr", **{"api-key": "read-only"}).status_code == 403, "a key is nobody, even where a whole domain may"
            assert search(client, "hr").status_code == 403
            assert search(client, "finance", **{"api-key": KEY}).json()["data"]["code"] == "no_policy"
