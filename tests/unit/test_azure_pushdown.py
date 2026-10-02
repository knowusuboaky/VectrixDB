"""Entitlement filters run inside Azure AI Search when the fields are promoted.

Promote the metadata paths a policy names to real index fields and the
policy's scope rules are compiled to OData and handed to the service, so a
document outside the scope never leaves it; the redaction rules are still
decided here where they can be counted. ``pushdown_mode`` says ENGINE, and
``require_pushdown=True`` opens. Leave the fields in the JSON blob and
nothing changes: the collection filters what came back and says POST.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("azure.search.documents")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_azure_search import FakeIndexClient, FakeSearchClient, _match_filter  # noqa: E402

from vectrixdb.core.storage import StorageBackend, StorageConfig  # noqa: E402
from vectrixdb.core.storage_azure import AzureSearchStorage, _field_name  # noqa: E402
from vectrixdb.core.types import FilterPushdown  # noqa: E402
from vectrixdb.exceptions import PushdownUnavailable  # noqa: E402
from vectrixdb.policy import AtMost, Excludes, Overlap, Policy, Present  # noqa: E402

FIELDS = {
    "client_id": "string",
    "roles": "strings",
    "classification": "number",
    "public": "boolean",
    "entitlements.allowed_roles": "strings",
}


def fake_storage(fields=FIELDS) -> AzureSearchStorage:
    fake = FakeIndexClient()
    storage = AzureSearchStorage(
        StorageConfig(
            backend=StorageBackend.AZURE_SEARCH,
            azure_search_index_prefix="t",
            azure_search_filter_fields=fields,
        ),
        index_client=fake,
        client_factory=fake.get_search_client,
    )
    storage.connect()
    return storage


class TestCompile:
    def setup_method(self):
        self.storage = fake_storage()

    def test_a_policy_compiles_to_guarded_odata(self):
        policy = Policy([Overlap("client_id", "clients", scope=True), AtMost("classification", "clearance")])
        compiled = policy.compile({"clients": ["acme", "bolt"], "clearance": 2})
        odata = self.storage.compile_filter("c", compiled)
        assert odata == (
            "((f_client_id ne null and search.in(f_client_id, 'acme,bolt', ',')) "
            "and (f_classification ne null and f_classification le 2.0))"
        )

    def test_scope_only_leaves_the_redaction_rules_out(self):
        policy = Policy([Overlap("client_id", "clients", scope=True), AtMost("classification", "clearance")])
        scope = policy.compile({"clients": ["acme"], "clearance": 2}, scope_only=True)
        assert [c["field"] for c in scope["$and"]] == ["client_id"]

    def test_lists_walls_presence_and_booleans(self):
        s = self.storage
        assert s.compile_filter("c", {"field": "roles", "op": "in", "value": ["a", "b"]}) == (
            "(f_roles/any() and f_roles/any(x: search.in(x, 'a,b', ',')))"
        )
        assert s.compile_filter("c", {"field": "roles", "op": "nin", "value": ["zeta"]}) == (
            "(f_roles/any() and not f_roles/any(x: search.in(x, 'zeta', ',')))"
        )
        assert s.compile_filter("c", {"field": "client_id", "op": "exists", "value": True}) == "f_client_id ne null"
        assert s.compile_filter("c", {"field": "public", "op": "eq", "value": True}) == "(f_public ne null and f_public eq true)"
        assert s.compile_filter("c", {"field": "entitlements.allowed_roles", "op": "in", "value": ["x"]}).startswith(
            "(f_entitlements__allowed_roles/any()"
        )

    def test_a_value_with_a_comma_picks_another_separator(self):
        out = self.storage.compile_filter("c", {"field": "client_id", "op": "in", "value": ["a,b", "c"]})
        assert "search.in(f_client_id, 'a,b|c', '|')" in out

    def test_quotes_are_doubled(self):
        out = self.storage.compile_filter("c", {"field": "client_id", "op": "eq", "value": "o'neil"})
        assert "f_client_id eq 'o''neil'" in out

    @pytest.mark.parametrize(
        "clause",
        [
            {"field": "topic", "op": "eq", "value": "x"},  # not promoted
            {"field": "roles", "op": "eq", "value": "x"},  # a list is not equal to a scalar
            {"field": "classification", "op": "eq", "value": "high"},  # a string is not a rank
            {"field": "classification", "op": "eq", "value": True},  # nor is a boolean
            {"field": "client_id", "op": "contains", "value": "a"},  # no honest translation
            {"$and": []},
        ],
    )
    def test_what_the_service_cannot_take_is_none(self, clause):
        assert self.storage.compile_filter("c", clause) is None

    def test_field_keyed_form_works_too(self):
        out = self.storage.compile_filter("c", {"client_id": "acme", "classification": {"$lte": 2}})
        assert "f_client_id eq 'acme'" in out and "f_classification le 2.0" in out

    def test_filter_fields_are_the_promoted_paths(self):
        assert self.storage.filter_fields("c") == frozenset(FIELDS)
        assert fake_storage(fields=None).filter_fields("c") == frozenset()

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError, match="expected one of"):
            fake_storage(fields={"client_id": "text"})._promoted()


class TestIndexAndRows:
    def test_promoted_fields_are_declared_filterable(self):
        storage = fake_storage()
        index = storage._collection_index("memos", 4)
        by_name = {f.name: f for f in index.fields}
        assert by_name[_field_name("client_id")].filterable is True
        assert by_name[_field_name("roles")].type == "Collection(Edm.String)"
        assert by_name[_field_name("entitlements.allowed_roles")].name == "f_entitlements__allowed_roles"

    def test_values_are_copied_beside_the_payload_and_nulls_left_out(self):
        storage = fake_storage()
        row = storage._to_row(
            "d1",
            {
                "client_id": "acme",
                "roles": ["a", 7],
                "classification": 2,
                "public": False,
                "entitlements": {"allowed_roles": ["r1"]},
                "text_content": "t",
                "dense_embedding": [0.1, 0.2],
            },
        )
        assert row["f_client_id"] == "acme"
        assert row["f_roles"] == ["a", "7"]
        assert row["f_classification"] == 2.0
        assert row["f_public"] is False
        assert row["f_entitlements__allowed_roles"] == ["r1"]
        assert "acme" in row["payload"], "the payload still carries everything"
        sparse = storage._to_row("d2", {"classification": "high", "public": "yes"})
        assert "f_classification" not in sparse and "f_public" not in sparse

    def test_the_fake_evaluates_what_the_backend_emits(self):
        storage = fake_storage()
        doc = {"f_client_id": "acme", "f_roles": ["a", "b"], "f_classification": 2.0, "f_public": True}
        ok = storage.compile_filter("c", {"$and": [
            {"field": "client_id", "op": "in", "value": ["acme"]},
            {"field": "roles", "op": "nin", "value": ["zeta"]},
            {"field": "classification", "op": "lte", "value": 2},
            {"field": "public", "op": "eq", "value": True},
        ]})
        assert _match_filter(ok, doc) is True
        assert _match_filter(ok, {**doc, "f_roles": ["zeta"]}) is False
        assert _match_filter(ok, {**doc, "f_classification": None}) is False
        assert _match_filter(ok, {**doc, "f_roles": []}) is False, "an empty list has nothing to pass a wall with"


@pytest.fixture
def azure(monkeypatch):
    """A VectrixDB on the fake service, with the policy's fields promoted."""
    from vectrixdb.core.database import VectrixDB

    storage = fake_storage()
    monkeypatch.setattr("vectrixdb.core.database.create_storage", lambda config: storage)
    db = VectrixDB.with_azure_search("https://svc.search.windows.net", key="k", filter_fields=FIELDS)
    yield db, storage
    db.close()


@pytest.fixture
def azure_plain(monkeypatch):
    from vectrixdb.core.database import VectrixDB

    storage = fake_storage(fields=None)
    monkeypatch.setattr("vectrixdb.core.database.create_storage", lambda config: storage)
    db = VectrixDB.with_azure_search("https://svc.search.windows.net", key="k")
    yield db, storage
    db.close()


LENDING = Policy(
    [Overlap("client_id", "clients", scope=True), AtMost("classification", "clearance")],
    require_pushdown=True,
)

MEMOS = [
    ("Covenant tested quarterly for acme.", {"client_id": "acme", "classification": 1}),
    ("Restricted acme board memo.", {"client_id": "acme", "classification": 3}),
    ("A zeta covenant nobody at acme may see.", {"client_id": "zeta", "classification": 1}),
]


class TestThroughVectrix:
    def test_a_policy_over_promoted_fields_opens_and_runs_in_the_engine(self, azure, tmp_path, monkeypatch):
        from vectrixdb import Vectrix

        db, storage = azure
        seen = []
        original = FakeSearchClient.search

        def spy(self, *args, **kwargs):
            seen.append(kwargs.get("filter"))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(FakeSearchClient, "search", spy)

        memos = Vectrix("memos", storage_backend=db, path=str(tmp_path), mode="dense", policy=LENDING)
        assert memos.pushdown_mode is FilterPushdown.ENGINE
        memos.add([t for t, _ in MEMOS], metadata=[m for _, m in MEMOS])

        analyst = {"clients": ["acme"], "clearance": 2}
        hits = memos.as_principal(analyst).search("covenant", limit=10)
        assert [h.metadata["client_id"] for h in hits] == ["acme"]
        assert hits.top.metadata["classification"] == 1

        pushed = [f for f in seen if f]
        assert pushed, "no filter reached the service"
        assert "f_client_id" in pushed[-1] and "f_classification" not in pushed[-1], (
            "the scope rule goes to the engine and the redaction rule stays here"
        )

    def test_the_record_says_engine_and_the_counts_say_what_they_can(self, azure, tmp_path):
        from vectrixdb import Vectrix
        from vectrixdb.audit import DENY, MemorySink

        db, _ = azure
        sink = MemorySink(query_key=b"k", on_failure=DENY)
        memos = Vectrix(
            "memos", storage_backend=db, path=str(tmp_path), mode="dense", policy=LENDING, on_retrieval=sink
        )
        memos.add([t for t, _ in MEMOS], metadata=[m for _, m in MEMOS])
        memos.as_principal({"clients": ["acme"], "clearance": 2}).search("covenant", limit=10)
        record = sink.records[-1]
        assert record.pushdown_mode == "engine"
        # The board memo is in scope and redacted, so it is counted; the zeta
        # memo never left the service, so there is nothing to count.
        assert record.withheld_disclosable == 1
        assert record.withheld_undisclosable == 0

    def test_a_null_promoted_field_fails_closed_even_for_a_wall(self, azure, tmp_path):
        from vectrixdb import Vectrix

        db, _ = azure
        walled = Policy([Excludes("client_id", "walls", scope=True)], require_pushdown=True)
        memos = Vectrix("walled", storage_backend=db, path=str(tmp_path), mode="dense", policy=walled)
        memos.add(
            ["acme memo", "memo with a null client"],
            metadata=[{"client_id": "acme"}, {"client_id": None}],
        )
        hits = memos.as_principal({"walls": ["zeta"]}).search("memo", limit=10)
        assert [h.metadata["client_id"] for h in hits] == ["acme"]

    def test_presence_rules_push_down_too(self, azure, tmp_path):
        from vectrixdb import Vectrix

        db, _ = azure
        policy = Policy([Present("client_id", scope=True)], require_pushdown=True)
        memos = Vectrix("present", storage_backend=db, path=str(tmp_path), mode="dense", policy=policy)
        assert memos.pushdown_mode is FilterPushdown.ENGINE

    def test_without_promoted_fields_it_is_post_and_the_option_refuses(self, azure_plain, tmp_path):
        from vectrixdb import Vectrix

        db, _ = azure_plain
        with pytest.raises(PushdownUnavailable, match="AzureSearchStorage"):
            Vectrix("memos", storage_backend=db, path=str(tmp_path), mode="dense", policy=LENDING)
        relaxed = Policy([Overlap("client_id", "clients", scope=True)])
        memos = Vectrix("memos2", storage_backend=db, path=str(tmp_path), mode="dense", policy=relaxed)
        assert memos.pushdown_mode is FilterPushdown.POST
        memos.add([t for t, _ in MEMOS], metadata=[m for _, m in MEMOS])
        hits = memos.as_principal({"clients": ["acme"]}).search("covenant", limit=10)
        assert {h.metadata["client_id"] for h in hits} == {"acme"}, "POST still enforces, it just cannot say the engine did"

    def test_a_policy_naming_an_unpromoted_field_is_post(self, azure, tmp_path):
        from vectrixdb import Vectrix

        db, _ = azure
        mixed = Policy([Overlap("client_id", "clients", scope=True), AtMost("tier", "level")])
        memos = Vectrix("mixed", storage_backend=db, path=str(tmp_path), mode="dense", policy=mixed)
        assert memos.pushdown_mode is FilterPushdown.POST

    def test_a_plain_filter_is_pushed_when_the_store_serves(self, azure, tmp_path, monkeypatch):
        """Without a policy the local copy serves and a filter is a ranking
        matter, not a security one. When the store serves, the filter goes
        with the query."""
        import numpy as np

        from vectrixdb import Vectrix

        db, _ = azure
        seen = []
        original = FakeSearchClient.search
        monkeypatch.setattr(
            FakeSearchClient, "search", lambda self, *a, **k: (seen.append(k.get("filter")), original(self, *a, **k))[1]
        )
        docs = Vectrix("docs", storage_backend=db, path=str(tmp_path), mode="dense")
        docs.add([t for t, _ in MEMOS], metadata=[m for _, m in MEMOS])
        coll = docs._collection
        hits = coll.search(
            query=np.zeros(coll.dimension, dtype=np.float32), limit=10, filter={"client_id": "zeta"}, use_backend=True
        )
        assert [h.metadata["client_id"] for h in hits.results] == ["zeta"]
        assert any(f and "f_client_id eq 'zeta'" in f for f in seen)
