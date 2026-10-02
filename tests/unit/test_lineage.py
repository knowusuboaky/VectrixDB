"""Where an answer came from, and whether it can be shown again.

One question drives every test here: the assistant told someone something
wrong, where did it come from? Each answers a piece of it, from a chunk back
to the write that stored it, from a decision id back to the set of chunks that
principal could reach, and from the collection to the pack of files a
validator asks for.
"""

from __future__ import annotations

import json

import pytest

from vectrixdb.audit import DENY, MemorySink
from vectrixdb.policy import AtMost, Excludes, Overlap, Policy

LENDING = Policy(
    [
        Overlap("entitlements.allowed_roles", "roles"),
        AtMost("entitlements.classification_rank", "clearance_rank"),
        Overlap("entitlements.client_id", "client_coverage", scope=True),
        Excludes("entitlements.client_id", "wall_restrictions", scope=True),
    ]
)
ANALYST = {
    "roles": ["credit_analyst"],
    "clearance_rank": 3,
    "client_coverage": ["CL-40219"],
    "wall_restrictions": [],
}
WALLED = {
    "roles": ["credit_analyst"],
    "clearance_rank": 3,
    "client_coverage": ["CL-51330"],
    "wall_restrictions": ["CL-40219"],
}
MEMO = (
    "# Covenant memo\n\nFacility covenant: coverage ratio not less than 1.15x.\n\n"
    "## Schedule\n\nCovenant schedule tested quarterly against the borrower accounts.\n"
)


def chunk(rank=2, client="CL-40219"):
    return {
        "entitlements": {
            "allowed_roles": ["credit_analyst"],
            "classification_rank": rank,
            "client_id": client,
        }
    }


@pytest.fixture
def sink():
    return MemorySink(query_key=b"deployment-key", on_failure=DENY)


@pytest.fixture
def db(tmp_path, sink):
    from vectrixdb import Vectrix

    db = Vectrix("lending", path=str(tmp_path / "db"), policy=LENDING, on_retrieval=sink)
    memo = tmp_path / "memo.md"
    memo.write_text(MEMO, encoding="utf-8")
    db.add_document(memo, chunk_size=60, overlap=10, metadata=chunk(2))
    db.add(["Internal credit memo: covenant headroom is thin"], metadata=[chunk(5)])
    yield db
    db.close()


# --------------------------------------------------------------------------
# A build id on every mutation, and on every chunk
# --------------------------------------------------------------------------


class TestBuildIdMovesOnEveryMutation:
    """Reproduction rests on one invariant: same build id, same index. Only
    add() and revoke() minted one, so a delete, a clear, a rebuild or a
    re-embed changed what a query could answer and left the id where it was.
    A reproduction run afterwards would have called itself exact against an
    index that had lost documents.
    """

    def test_delete_moves_it(self, db):
        before = db.index_build_id
        db.delete(db.as_principal(ANALYST).search("covenant", limit=1).ids)
        assert db.index_build_id != before

    def test_clear_moves_it(self, db):
        before = db.index_build_id
        db.clear()
        assert db.index_build_id != before

    def test_rebuild_moves_it(self, db):
        before = db.index_build_id
        db.rebuild_index()
        assert db.index_build_id != before

    def test_reembed_moves_it(self, db):
        before = db.index_build_id
        db.reembed()
        assert db.index_build_id != before

    def test_delete_document_moves_it(self, db):
        before = db.index_build_id
        doc = db.provenance(db.as_principal(ANALYST).search("covenant", limit=1).ids)[0]
        db.delete_document(doc.document_id)
        assert db.index_build_id != before

    def test_every_chunk_carries_the_build_that_stored_it(self, db, sink):
        """The join in the other direction: from a chunk to its ingestion
        record, without going through whichever build happens to be current."""
        ingestion_ids = {r["ingestion_id"] for r in sink.raw if r.get("record_kind") == "ingestion"}
        for _, _, metadata in db._collection._iter_documents_raw():
            assert metadata["_vx_build"] in ingestion_ids

    def test_the_chunk_names_its_own_write_not_the_latest(self, db, sink):
        first_ids = db.as_principal(ANALYST).search("covenant schedule", limit=10).ids
        first_builds = {p.build_id for p in db.provenance(first_ids)}
        db.add(["A later covenant"], metadata=[chunk(2)])

        assert db.index_build_id not in first_builds
        assert {p.build_id for p in db.provenance(first_ids)} == first_builds


# --------------------------------------------------------------------------
# Provenance: chunk -> write
# --------------------------------------------------------------------------


class TestProvenance:
    def test_a_chunk_from_add_document_knows_where_it_came_from(self, db, tmp_path):
        hit = db.as_principal(ANALYST).search("coverage ratio", limit=1).ids[0]
        p = db.provenance(hit)[0]

        assert p.present
        assert p.source == str(tmp_path / "memo.md")
        assert p.document_version is not None and len(p.document_version) == 16
        assert (p.chunk_strategy, p.chunk_size, p.chunk_overlap) == ("recursive", 60, 10)
        assert p.chunk_index is not None
        assert p.build_id.startswith("build_")

    def test_the_version_is_the_text_not_the_file(self, tmp_path, sink):
        """Two ingestions of the same bytes share a version; a changed file
        does not. It is what tells "we re-ingested" from "the source moved"."""
        from vectrixdb import Vectrix

        db = Vectrix("v", path=str(tmp_path / "v"), on_retrieval=sink)
        try:
            memo = tmp_path / "memo.md"
            memo.write_text(MEMO, encoding="utf-8")
            db.add_document(memo)
            first = sink.raw[-1]["document_version"]
            db.add_document(memo)
            assert sink.raw[-1]["document_version"] == first
            memo.write_text(MEMO + "\nAmended.\n", encoding="utf-8")
            db.add_document(memo)
            assert sink.raw[-1]["document_version"] != first
        finally:
            db.close()

    def test_a_plain_add_has_a_build_and_nothing_to_invent(self, db):
        # The plain add is the rank-5 memo, which ANALYST cannot reach, so
        # find it the way the host would rather than through a search.
        hit = next(id_ for id_, _, m in db._collection._iter_documents_raw() if "_vx_doc" not in m)
        p = db.provenance(hit)[0]

        assert p.present and p.build_id
        assert p.document_id is None and p.source is None and p.chunk_strategy is None

    def test_a_deleted_chunk_says_gone(self, db):
        hit = db.as_principal(ANALYST).search("coverage ratio", limit=1).ids[0]
        db.delete(hit)
        p = db.provenance(hit)[0]

        assert not p.present
        assert p.build_id is None

    def test_a_principal_is_refused(self, db):
        """Which chunks exist and where they came from is the host's question."""
        from vectrixdb.exceptions import PolicyError

        with pytest.raises(PolicyError):
            db.as_principal(ANALYST).provenance(["anything"])


# --------------------------------------------------------------------------
# The records carry what lineage needs
# --------------------------------------------------------------------------


class TestTheRecords:
    def test_the_ingestion_record_carries_the_provenance(self, db, sink, tmp_path):
        record = next(r for r in sink.raw if r.get("record_kind") == "ingestion")
        assert record["source"] == str(tmp_path / "memo.md")
        assert record["document_id"]
        assert record["chunking"] == {"strategy": "recursive", "chunk_size": 60, "overlap": 10}
        assert len(record["ids_written"]) == record["documents_written"]

    def test_a_plain_add_records_none_rather_than_a_guess(self, db, sink):
        record = [r for r in sink.raw if r.get("record_kind") == "ingestion"][-1]
        assert record["source"] is None
        assert record["chunking"] is None
        assert record["ids_written"]

    def test_the_decision_record_says_which_chunks_were_handed_over(self, db, sink):
        results = db.as_principal(ANALYST).search("covenant", limit=10)
        record = sink.find(results.decision_id)

        assert record["result_ids"] == results.ids
        assert record["results_returned"] == len(results.ids)

    def test_the_record_keeps_true_latency_apart_from_the_padded_figure(self, tmp_path, sink):
        """Sleeping and then writing the padded number as the duration would
        lose what the timing floor cost, which is the one number the host
        paying for it is entitled to."""
        from vectrixdb import Vectrix

        db = Vectrix(
            "timed",
            path=str(tmp_path / "timed"),
            policy=LENDING,
            on_retrieval=sink,
            timing_floor=0.05,
        )
        try:
            db.add(["covenant"], metadata=[chunk(2)])
            results = db.as_principal(ANALYST).search("covenant", limit=10)
            record = sink.find(results.decision_id)

            assert record["padded_to_ms"] == results.time_ms
            assert record["padded_to_ms"] % 50.0 == 0.0
            assert record["duration_ms"] < record["padded_to_ms"]
        finally:
            db.close()

    def test_without_a_floor_there_is_nothing_padded(self, db, sink):
        results = db.as_principal(ANALYST).search("covenant", limit=10)
        assert sink.find(results.decision_id)["padded_to_ms"] is None


# --------------------------------------------------------------------------
# find(): where a reproduction starts
# --------------------------------------------------------------------------


class TestFind:
    def test_memory_finds_a_decision_and_an_ingestion(self, db, sink):
        results = db.as_principal(ANALYST).search("covenant", limit=10)
        assert sink.find(results.decision_id)["decision_id"] == results.decision_id
        assert sink.find(db.index_build_id)["record_kind"] == "ingestion"
        assert sink.find("dec_nothing") is None

    def test_jsonl_finds_one(self, tmp_path):
        from vectrixdb import Vectrix
        from vectrixdb.audit import JSONLSink

        sink = JSONLSink(tmp_path / "audit.jsonl", query_key=b"k", on_failure=DENY)
        db = Vectrix("j", path=str(tmp_path / "j"), policy=LENDING, on_retrieval=sink)
        try:
            db.add(["covenant"], metadata=[chunk(2)])
            results = db.as_principal(ANALYST).search("covenant", limit=10)
            assert sink.find(results.decision_id)["result_ids"] == results.ids
            assert sink.find("dec_nothing") is None
        finally:
            db.close()

    def test_a_sink_that_cannot_read_says_so(self):
        """An insert-only store has nothing to read with, and the answer is
        to fetch the record yourself, not a silent None."""
        from vectrixdb.audit import AuditSink

        class WriteOnly(AuditSink):
            def _emit(self, record):
                pass

            def _emit_raw(self, payload):
                pass

        with pytest.raises(NotImplementedError, match="pass it to reproduce"):
            WriteOnly(query_key=b"k", on_failure=DENY).find("dec_x")


# --------------------------------------------------------------------------
# Reproduction: decision -> reachable set
# --------------------------------------------------------------------------


class TestReproduction:
    def _decide(self, db, sink, principal=ANALYST, query="covenant"):
        results = db.as_principal(principal).search(query, limit=10)
        return results, sink.find(results.decision_id)

    def test_against_the_same_index_it_is_exact(self, db, sink):
        results, record = self._decide(db, sink)
        rep = db.reproduce(record)

        assert rep.exact and rep.same_build and rep.same_policy
        assert rep.results_known
        assert set(rep.returned) == set(results.ids)
        assert set(rep.still_reachable) == set(results.ids)
        assert rep.no_longer_reachable == () and rep.missing == ()
        assert set(results.ids) <= set(rep.reachable_now)
        assert "EXACT" in rep.report()

    def test_it_takes_the_record_object_as_well_as_the_dict(self, db, sink):
        results, _ = self._decide(db, sink)
        record = next(
            r for r in sink.records if getattr(r, "decision_id", None) == results.decision_id
        )
        assert db.reproduce(record).exact

    def test_the_reachable_set_is_the_principal_s_not_the_collection_s(self, db, sink):
        """The rank-5 memo is in the collection and not in A's reach, and a
        walled principal reaches nothing at all."""
        _, record = self._decide(db, sink)
        rep = db.reproduce(record)
        assert len(rep.reachable_now) == db._count_all() - 1

        _, walled = self._decide(db, sink, principal=WALLED)
        assert db.reproduce(walled).reachable_now == ()

    def test_after_a_write_it_is_a_re_run_and_says_so(self, db, sink):
        results, record = self._decide(db, sink)
        db.delete(results.ids[:1])
        rep = db.reproduce(record)

        assert not rep.exact and not rep.same_build and rep.same_policy
        assert rep.missing == (results.ids[0],)
        assert set(rep.still_reachable) == set(results.ids[1:])
        text = rep.report()
        assert "RE-RUN" in text and "the index has moved" in text
        assert f"{results.ids[0]}: gone" in text

    def test_a_revocation_shows_as_no_longer_reachable(self, db, sink):
        """The document is still there; this principal can no longer reach
        it. Different from missing, and the difference is the finding."""
        results, record = self._decide(db, sink)
        db.revoke(
            where={"entitlements.client_id": "CL-40219"}, set={"entitlements.client_id": "CL-9"}
        )
        rep = db.reproduce(record)

        assert set(rep.no_longer_reachable) == set(results.ids)
        assert rep.missing == ()

    def test_a_policy_change_is_named(self, tmp_path, sink):
        from vectrixdb import Vectrix

        db = Vectrix("p", path=str(tmp_path / "p"), policy=LENDING, on_retrieval=sink)
        db.add(["covenant"], metadata=[chunk(2)])
        results = db.as_principal(ANALYST).search("covenant", limit=10)
        record = sink.find(results.decision_id)
        db.close()

        # Reopening under a different policy refuses, which is its own test.
        # Here the record's fingerprint is compared against what the
        # collection carries, so a record from a differently fingerprinted
        # policy is a re-run and says which half changed.
        db = Vectrix("p", path=str(tmp_path / "p"), policy=LENDING, on_retrieval=sink)
        try:
            forged = dict(record, policy_fingerprint="sha256:somethingelse")
            rep = db.reproduce(forged)
            assert not rep.same_policy and not rep.exact
            assert "the policy has changed" in rep.report()
        finally:
            db.close()

    def test_a_record_without_result_ids_says_nothing_is_known(self, db, sink):
        """A record from before result ids were kept: the reachable set can
        still be computed, but which chunks were returned cannot be, and the
        report says so rather than reporting zero returned."""
        _, record = self._decide(db, sink)
        older = {k: v for k, v in record.items() if k != "result_ids"}
        rep = db.reproduce(older)

        assert not rep.results_known
        assert rep.returned == ()
        assert "not recorded" in rep.report()
        assert rep.reachable_now

    def test_from_a_snapshot_of_that_build_it_is_exact_again(self, db, sink, tmp_path):
        """How a reproduction stays exact after the index has moved: keep an
        export per build and hand the right one back."""
        results, record = self._decide(db, sink)
        snapshot = db.export(tmp_path / "build.zip")
        db.delete(results.ids[:1])
        assert not db.reproduce(record).exact

        rep = db.reproduce(record, snapshot=snapshot)

        assert rep.exact
        assert set(rep.still_reachable) == set(results.ids)
        assert any("snapshot" in note for note in rep.notes)

    def test_without_a_policy_nothing_can_be_decided(self, tmp_path, sink):
        from vectrixdb import Vectrix

        db = Vectrix("plain", path=str(tmp_path / "plain"), on_retrieval=sink)
        try:
            db.add(["covenant"])
            rep = db.reproduce({"decision_id": "dec_x", "principal_snapshot": ANALYST})
            assert not rep.exact and rep.reachable_now == ()
            assert any("no policy" in n for n in rep.notes)
        finally:
            db.close()

    def test_a_principal_is_refused(self, db, sink):
        """Which chunks a principal could reach is the map of the wall."""
        from vectrixdb.exceptions import PolicyError

        _, record = self._decide(db, sink)
        with pytest.raises(PolicyError):
            db.as_principal(ANALYST).reproduce(record)

    def test_something_that_is_not_a_record_is_refused(self, db):
        with pytest.raises(TypeError, match="RetrievalRecord or the dict"):
            db.reproduce("dec_752d298922934cbc")

    def test_to_dict_round_trips_through_json(self, db, sink):
        _, record = self._decide(db, sink)
        payload = json.loads(json.dumps(db.reproduce(record).to_dict()))
        assert payload["exact"] is True
        assert payload["provenance"][0]["present"] is True


# --------------------------------------------------------------------------
# The evidence pack
# --------------------------------------------------------------------------


class TestEvidencePack:
    def test_it_writes_the_six_files(self, db, tmp_path):
        pack = db.evidence_pack(tmp_path / "pack")
        assert [f.name for f in pack.files] == [
            "README.md",
            "model.json",
            "lineage.json",
            "policy.json",
            "controls.json",
            "decisions.json",
        ]
        assert all(f.exists() for f in pack.files)

    def test_it_is_not_a_compliance_claim_and_says_so(self, db, tmp_path):
        readme = (db.evidence_pack(tmp_path / "pack").directory / "README.md").read_text()
        assert "not a compliance claim" in readme
        assert "SR 11-7" in readme and "OSFI E-23" in readme

    def test_the_policy_and_the_controls_are_the_live_ones(self, db, tmp_path):
        out = db.evidence_pack(tmp_path / "pack").directory
        policy = json.loads((out / "policy.json").read_text())
        controls = json.loads((out / "controls.json").read_text())

        assert policy["fingerprint"] == LENDING.fingerprint
        assert controls["entitlement_policy"]["rules"] == [r.describe() for r in LENDING.rules]
        assert controls["enforcement"]["pushdown_mode"] == db.pushdown_mode.value
        assert controls["audit"]["sink"] == "MemorySink"
        assert controls["audit"]["on_failure"] == "_Deny"

    def test_lineage_comes_from_the_chunks(self, db, tmp_path, sink):
        lineage = json.loads(
            (db.evidence_pack(tmp_path / "pack").directory / "lineage.json").read_text()
        )
        ingestions = {r["ingestion_id"] for r in sink.raw if r.get("record_kind") == "ingestion"}

        assert lineage["chunks"] == db._count_all()
        assert lineage["chunks_without_a_build_id"] == 0
        assert set(lineage["builds_seen"]) == ingestions
        assert lineage["chunking_configurations"] == [
            {"strategy": "recursive", "chunk_size": 60, "overlap": 10}
        ]
        assert lineage["documents"] == 1 and lineage["distinct_sources"] == 1

    def test_decisions_are_summarised_not_copied(self, db, tmp_path):
        returned = db.as_principal(ANALYST).search("covenant", limit=10).ids
        db.as_principal(WALLED).search("covenant", limit=10)
        decisions = json.loads(
            (db.evidence_pack(tmp_path / "pack").directory / "decisions.json").read_text()
        )

        assert decisions["decisions"] == 2
        assert sum(decisions["by_outcome"].values()) == 2
        assert decisions["ingestions"] == 2
        assert "withheld_undisclosable_total" not in decisions
        # A summary, so no chunk id appears in it: the records stay in the store.
        assert not any(id_ in json.dumps(decisions) for id_ in returned)

    def test_the_undisclosable_total_is_deliberately_absent(self, db, tmp_path):
        """A summary ends up on a dashboard, and that number confirms
        documents exist outside a scope somebody was refused."""
        db.as_principal(WALLED).search("covenant", limit=10)
        text = (db.evidence_pack(tmp_path / "pack").directory / "decisions.json").read_text()
        assert "undisclosable" not in text

    def test_records_can_be_supplied_when_the_sink_cannot_read(self, db, sink, tmp_path):
        db.as_principal(ANALYST).search("covenant", limit=10)
        supplied = list(sink.raw)
        db.on_retrieval = None
        out = db.evidence_pack(tmp_path / "pack", records=supplied).directory
        decisions = json.loads((out / "decisions.json").read_text())

        assert decisions["decisions"] == 1
        assert "note" not in decisions

    def test_no_records_is_said_rather_than_guessed(self, tmp_path):
        from vectrixdb import Vectrix

        db = Vectrix("quiet", path=str(tmp_path / "quiet"))
        try:
            db.add(["one"])
            decisions = json.loads(
                (db.evidence_pack(tmp_path / "pack").directory / "decisions.json").read_text()
            )
            assert decisions["decisions"] == 0
            assert "Pass records=" in decisions["note"]
        finally:
            db.close()

    def test_a_principal_is_refused(self, db, tmp_path):
        from vectrixdb.exceptions import PolicyError

        with pytest.raises(PolicyError):
            db.as_principal(ANALYST).evidence_pack(tmp_path / "pack")
