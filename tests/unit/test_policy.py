"""An entitlement policy a collection cannot be queried without.

The fixture is the commercial lending case this was designed against: two
analysts with the same role and the same clearance, one on the deal team and
one behind an ethical wall, querying one index. The assertions that matter
are the last two, and they are opposites:

* the analyst inside the scope is told something was withheld
* the analyst outside it cannot tell a blocked client from one that does not
  exist, byte for byte

Everything above them exists to make those two hold.
"""

from __future__ import annotations

import pytest

from vectrixdb.exceptions import (
    PolicyDefinitionError,
    PolicyError,
    PrincipalIncomplete,
)
from vectrixdb.policy import (
    AtLeast,
    AtMost,
    Decision,
    Equals,
    Excludes,
    Outcome,
    Overlap,
    Policy,
    Predicate,
    Present,
)

# --------------------------------------------------------------------------
# The case study, as data
# --------------------------------------------------------------------------

LENDING = Policy(
    [
        Overlap("entitlements.allowed_roles", "roles"),
        Equals("entitlements.lob", "lob"),
        AtMost("entitlements.classification_rank", "clearance_rank"),
        # Scope: whether this principal may know the document exists at all.
        Overlap("entitlements.client_id", "client_coverage", scope=True),
        Excludes("entitlements.client_id", "wall_restrictions", scope=True),
    ]
)


def chunk(client, rank, roles=("credit_analyst", "relationship_manager")):
    return {
        "entitlements": {
            "allowed_roles": list(roles),
            "lob": "commercial_banking",
            "classification_rank": rank,
            "client_id": client,
        }
    }


#: On the deal team for CL-40219, cleared to rank 3.
ANALYST_A = {
    "roles": ["credit_analyst"],
    "lob": "commercial_banking",
    "clearance_rank": 3,
    "client_coverage": ["CL-40219", "CL-40220"],
    "wall_restrictions": [],
}

#: Same role, same clearance, different book, and walled off CL-40219.
ANALYST_B = {
    "roles": ["credit_analyst"],
    "lob": "commercial_banking",
    "clearance_rank": 3,
    "client_coverage": ["CL-51330"],
    "wall_restrictions": ["CL-40219"],
}


# --------------------------------------------------------------------------
# A policy that is malformed is not a policy
# --------------------------------------------------------------------------


class TestDefinition:
    def test_an_empty_policy_is_refused(self):
        """It permits everything while looking like a control, which is worse
        than not having written one."""
        with pytest.raises(PolicyDefinitionError, match="at least one rule"):
            Policy([])

    def test_the_base_predicate_is_refused(self):
        with pytest.raises(PolicyDefinitionError, match="base class"):
            Policy([Predicate("a", "b")])

    def test_a_rule_that_reads_the_principal_needs_a_key(self):
        with pytest.raises(PolicyDefinitionError, match="needs a principal key"):
            Policy([Overlap("entitlements.allowed_roles")])

    def test_present_refuses_a_principal_key_it_would_ignore(self):
        """Accepting and ignoring an argument is the defect family this
        codebase spent 2.2 removing."""
        with pytest.raises(PolicyDefinitionError, match="silently ignored"):
            Policy([Present("entitlements.client_id", "client_coverage")])

    def test_not_a_predicate(self):
        with pytest.raises(PolicyDefinitionError, match="not a predicate"):
            Policy([{"field": "x"}])


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


class TestFingerprint:
    def test_it_is_stable_across_constructions(self):
        assert Policy(list(LENDING.rules)).fingerprint == LENDING.fingerprint

    def test_it_changes_when_a_field_changes(self):
        other = Policy(
            [
                Overlap("entitlements.allowed_roles", "roles"),
                Equals("entitlements.lob", "lob"),
                AtMost("entitlements.classification_rank", "clearance_rank"),
                Overlap("entitlements.client_id", "client_coverage", scope=True),
                # The wall now reads a different key.
                Excludes("entitlements.client_id", "conflict_list", scope=True),
            ]
        )
        assert other.fingerprint != LENDING.fingerprint

    def test_it_changes_when_only_the_scope_flag_changes(self):
        """Scope decides what a caller may be told was withheld, so two
        policies differing only in it are not the same policy."""
        relaxed = Policy([Overlap("e.client_id", "coverage", scope=True)])
        strict = Policy([Overlap("e.client_id", "coverage", scope=False)])
        assert relaxed.fingerprint != strict.fingerprint

    def test_it_survives_a_round_trip(self):
        assert Policy.from_dict(LENDING.to_dict()).fingerprint == LENDING.fingerprint

    def test_the_db_role_key_survives_a_round_trip_outside_the_fingerprint(self):
        """The PostgreSQL role key has to come back from the collection's
        metadata, or RLS is never applied after a reopen; it is not a rule,
        so a collection stored without it keeps its fingerprint."""
        with_role = Policy(list(LENDING.rules), db_role_key="pg_role")
        assert Policy.from_dict(with_role.to_dict()).db_role_key == "pg_role"
        assert with_role.fingerprint == LENDING.fingerprint
        assert "db_role_key" not in LENDING.to_dict()

    def test_an_unknown_predicate_refuses_rather_than_dropping_a_rule(self):
        """A collection written by a newer version carries rules this one
        cannot evaluate. Opening it anyway would drop them."""
        data = LENDING.to_dict()
        data["rules"][0]["kind"] = "SomethingFromTheFuture"
        with pytest.raises(PolicyDefinitionError, match="newer VectrixDB"):
            Policy.from_dict(data)

    def test_required_keys(self):
        assert LENDING.required_principal_keys == frozenset(
            {"roles", "lob", "clearance_rank", "client_coverage", "wall_restrictions"}
        )


# --------------------------------------------------------------------------
# Compilation into the filter the engine already has
# --------------------------------------------------------------------------


class TestCompile:
    def test_the_filter_the_engine_gets_is_valid(self):
        from vectrixdb.core.types import Filter

        compiled = Filter.from_dict(LENDING.compile(ANALYST_A))
        assert compiled.matches(chunk("CL-40219", 2))

    def test_two_rules_over_one_field_both_survive(self):
        """A wall sits beside a coverage list on the same field, which a
        field-keyed filter dict cannot express without one overwriting the
        other. This is why compile() emits $and."""
        from vectrixdb.core.types import Filter

        compiled = Filter.from_dict(LENDING.compile(ANALYST_B))
        conditions = [c for f in compiled.nested for c in f.conditions]
        client_ops = [c.operator for c in conditions if c.field == "entitlements.client_id"]
        assert sorted(client_ops) == ["in", "nin"]

    def test_a_principal_matching_nothing_skips_the_search(self):
        """A user in no groups. Returning None lets the caller skip a search
        guaranteed to come back empty, and leaks nothing about documents:
        whether you hold any groups is a fact about you."""
        nobody = dict(ANALYST_A, roles=[])
        assert LENDING.compile(nobody) is None

    def test_an_empty_wall_excludes_nothing(self):
        """The opposite reading of an empty list, and the right one. It needs
        no special case because $nin [] already matches every document."""
        compiled = LENDING.compile(ANALYST_A)
        assert compiled is not None
        clause = [c for c in compiled["$and"] if c["op"] == "nin"][0]
        assert clause["value"] == []

    def test_a_scalar_is_accepted_where_a_list_is_meant(self):
        one_client = dict(ANALYST_A, client_coverage="CL-40219")
        clauses = LENDING.compile(one_client)["$and"]
        assert {"field": "entitlements.client_id", "op": "in", "value": ["CL-40219"]} in clauses


# --------------------------------------------------------------------------
# Failing closed, and failing loudly where it is not a denial
# --------------------------------------------------------------------------


class TestFailsClosed:
    def test_a_document_with_no_entitlements_is_invisible(self):
        """Not universal. The filter engine gives this: an absent field
        matches no operator. There is deliberately no switch to turn it off."""
        decision = LENDING.decide(ANALYST_A, {"title": "unstamped chunk"})
        assert decision.allowed is False

    def test_a_document_missing_only_the_rank_is_still_denied(self):
        partial = chunk("CL-40219", 2)
        del partial["entitlements"]["classification_rank"]
        assert LENDING.decide(ANALYST_A, partial).allowed is False

    def test_a_missing_principal_key_raises_rather_than_denying(self):
        """A resolver fault, not a user without permission. Counting it as a
        denial is how a broken resolver hides behind normal traffic."""
        incomplete = {k: v for k, v in ANALYST_A.items() if k != "clearance_rank"}
        with pytest.raises(PrincipalIncomplete, match="clearance_rank"):
            LENDING.compile(incomplete)

    def test_a_principal_key_resolved_to_none_raises(self):
        with pytest.raises(PrincipalIncomplete, match="resolved to None"):
            LENDING.compile(dict(ANALYST_A, clearance_rank=None))

    def test_clearance_zero_is_a_clearance_and_not_an_absence(self):
        """The truthiness family, in the one place it would be a security
        bug rather than a wrong answer."""
        floor = dict(ANALYST_A, clearance_rank=0)
        assert LENDING.decide(floor, chunk("CL-40219", 0)).allowed is True
        assert LENDING.decide(floor, chunk("CL-40219", 1)).allowed is False

    def test_a_denial_is_never_an_exception(self):
        """The single most important rule in the taxonomy. An exception a
        caller can catch and tell apart from nothing-matched is the side
        channel, whatever the response layer does afterwards."""
        decision = LENDING.decide(ANALYST_B, chunk("CL-40219", 1))
        assert isinstance(decision, Decision)
        assert decision.allowed is False


# --------------------------------------------------------------------------
# Scope: what a caller may be told
# --------------------------------------------------------------------------


class TestScope:
    def test_inside_the_scope_a_redaction_is_disclosable(self):
        """A is on the deal team and loses one chunk to clearance. Telling
        them so reveals nothing: they already know the client exists."""
        decision = LENDING.decide(ANALYST_A, chunk("CL-40219", 5))
        assert decision.in_scope is True
        assert decision.allowed is False
        assert decision.disclosable is True
        assert decision.failed == ("AtMost(entitlements.classification_rank <- clearance_rank)",)

    def test_outside_the_scope_nothing_is_disclosable(self):
        """B is walled. "One result withheld" would confirm the client is a
        client, which is the thing a wall exists to prevent."""
        decision = LENDING.decide(ANALYST_B, chunk("CL-40219", 1))
        assert decision.in_scope is False
        assert decision.disclosable is False

    def test_a_document_out_of_scope_is_not_evaluated_further(self):
        """Fewer rules run against something the principal may not know
        exists, so there is less to leak through timing. The failed list
        names the scope rule only, never the clearance rule underneath."""
        decision = LENDING.decide(ANALYST_B, chunk("CL-40219", 99))
        assert all("classification_rank" not in f for f in decision.failed)

    def test_partition_counts_the_two_kinds_apart(self):
        candidates = [
            chunk("CL-40219", 1),  # A: allowed
            chunk("CL-40219", 2),  # A: allowed
            chunk("CL-40219", 5),  # A: redacted, in scope
            chunk("CL-99999", 1),  # A: out of scope
        ]
        allowed, disclosable, undisclosable = LENDING.partition(ANALYST_A, candidates)
        assert (len(allowed), disclosable, undisclosable) == (2, 1, 1)

    def test_for_a_walled_principal_everything_is_undisclosable(self):
        """The count a decision record must carry as zero-disclosable. If a
        caller is handed one suppressed number and renders it, the wall is
        gone."""
        candidates = [chunk("CL-40219", r) for r in (1, 2, 3, 4, 5, 6, 7)]
        allowed, disclosable, undisclosable = LENDING.partition(ANALYST_B, candidates)
        assert (len(allowed), disclosable, undisclosable) == (0, 0, 7)


# --------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------


class TestOutcome:
    @pytest.mark.parametrize(
        "returned,disclosable,undisclosable,expected",
        [
            (3, 0, 0, Outcome.ALLOWED),
            (3, 1, 0, Outcome.ALLOWED_WITH_REDACTION),
            (0, 2, 0, Outcome.DENIED_IN_SCOPE),
            (0, 0, 7, Outcome.DENIED_OUT_OF_SCOPE),
            (0, 0, 0, Outcome.DENIED_OUT_OF_SCOPE),
        ],
    )
    def test_mapping(self, returned, disclosable, undisclosable, expected):
        assert LENDING.outcome(returned, disclosable, undisclosable) is expected

    def test_nothing_found_and_everything_walled_are_the_same_outcome(self):
        """Deliberate. They must be indistinguishable to the caller, so they
        cannot be different values on the way out."""
        assert LENDING.outcome(0, 0, 0) is LENDING.outcome(0, 0, 9)

    def test_every_policy_exception_is_a_refusal_not_a_denial(self):
        """There is no AccessDenied, and there must never be one."""
        import vectrixdb.exceptions as exc

        assert not hasattr(exc, "AccessDenied")
        for name in ("PrincipalRequired", "PrincipalIncomplete", "PolicyMismatch"):
            assert issubclass(getattr(exc, name), PolicyError)


# --------------------------------------------------------------------------
# Against a real collection
# --------------------------------------------------------------------------


class TestAgainstACollection:
    """Two collections, identical except that in one of them CL-40219 is a
    client and in the other it never was. That pair is the only way to state
    the wall property honestly: B's view has to be the same in both."""

    CORPUS_40219 = [
        (
            "Facility covenant: fixed charge coverage ratio not less than 1.15x",
            chunk("CL-40219", 2),
        ),
        ("Covenant schedule: tested quarterly against the borrower accounts", chunk("CL-40219", 2)),
        ("Internal credit memo: covenant headroom is thin into Q3", chunk("CL-40219", 5)),
    ]
    #: B's own book, present in both collections so B always has something.
    CORPUS_B = [
        ("Facility covenant for the other borrower, tested semi-annually", chunk("CL-51330", 2)),
    ]

    def _collection(self, path, rows):
        from vectrixdb import Vectrix

        db = Vectrix("lending", path=str(path))
        db.add([text for text, _ in rows], metadata=[meta for _, meta in rows])
        return db

    @pytest.fixture
    def db(self, tmp_path):
        db = self._collection(tmp_path / "full", self.CORPUS_40219 + self.CORPUS_B)
        yield db
        db.close()

    def test_the_compiled_filter_drives_a_real_search(self, db):
        """A is on the deal team and cleared to 3, so the rank-5 credit memo
        is withheld from inside a scope they otherwise hold."""
        results = db.search("covenant thresholds", limit=10, filter=LENDING.compile(ANALYST_A))
        texts = [r.text for r in results]
        assert len(texts) == 2
        assert all("credit memo" not in t for t in texts)

    def test_the_walled_analyst_sees_only_their_own_book(self, db):
        results = db.search("covenant thresholds", limit=10, filter=LENDING.compile(ANALYST_B))
        assert [r.text for r in results] == [self.CORPUS_B[0][0]]

    def test_blocked_is_byte_identical_to_does_not_exist(self, tmp_path):
        """The wall assertion, and the property the whole design rests on.

        One collection holds CL-40219, the other never heard of it. B is
        walled from CL-40219 and covers CL-51330 in both. Nothing B can
        observe may differ, because "no documents found for that client" has
        already confirmed whether a relationship exists.
        """
        from vectrixdb.testing import assert_indistinguishable

        walled = self._collection(tmp_path / "walled", self.CORPUS_40219 + self.CORPUS_B)
        absent = self._collection(tmp_path / "absent", self.CORPUS_B)
        try:
            query = "covenant thresholds for this facility"
            assert_indistinguishable(
                lambda: walled.search(query, limit=10, filter=LENDING.compile(ANALYST_B)),
                lambda: absent.search(query, limit=10, filter=LENDING.compile(ANALYST_B)),
                "a walled client and one that was never onboarded",
            )
        finally:
            walled.close()
            absent.close()

    def test_the_counts_a_record_would_carry_differ_even_though_the_answers_do_not(self):
        """The user learns nothing; the audit log carries the full picture.
        That asymmetry is why the audit store needs tighter access control
        than the index it audits, not looser."""
        candidates = [meta for _, meta in self.CORPUS_40219 + self.CORPUS_B]

        a_allowed, a_disclosable, a_undisclosable = LENDING.partition(ANALYST_A, candidates)
        b_allowed, b_disclosable, b_undisclosable = LENDING.partition(ANALYST_B, candidates)

        # A: two covenant chunks, the memo redacted in scope, B's book unseen.
        assert (len(a_allowed), a_disclosable, a_undisclosable) == (2, 1, 1)
        # B: their own chunk, and three they may not know exist. Zero
        # disclosable is the number that keeps the wall standing.
        assert (len(b_allowed), b_disclosable, b_undisclosable) == (1, 0, 3)


# --------------------------------------------------------------------------
# Bound to the collection, which is what makes it a control
# --------------------------------------------------------------------------


class TestScoresAndTheWall:
    """A score is a number the caller can read, so it is part of the answer.

    Which makes where it comes from a question about the wall. A dense score
    is a similarity against a vector fixed when the document was written, so
    nothing withheld can move it. BM25 is not like that: it weights a term by
    how many documents contain it, counted over the index, so a document the
    principal was refused changed the score of one they were allowed and
    could reorder two of them. Hiding the score would not have helped,
    because the ranking carried it too, and rrf fuses that ranking into
    hybrid. The statistics are computed over the visible subset now.

    Both collections here add one document per call, because a text embedded
    in a batch of four lands up to 0.015 from the vector it gets alone, and
    that difference has nothing to do with entitlements. Controlling for it
    is what makes a difference below a finding rather than noise.
    """

    WALLED = [
        ("Facility covenant: fixed charge coverage ratio not less than 1.15x", "CL-40219", 2),
        ("Internal credit memo: covenant headroom is thin into Q3", "CL-40219", 5),
    ]
    OWN = [
        ("Covenant thresholds for the other borrower, ratio 1.40x", "CL-51330", 2),
        ("Quarterly account schedule for that borrower", "CL-51330", 2),
    ]
    QUERY = "covenant thresholds"

    def _pair(self, tmp_path, walled=None, own=None):
        from vectrixdb import Vectrix

        def build(name, rows):
            db = Vectrix(name, path=str(tmp_path / name), policy=LENDING, mode="hybrid")
            for text, client, rank in rows:
                db.add([text], metadata=[chunk(client, rank)])
            return db

        walled = self.WALLED if walled is None else walled
        own = self.OWN if own is None else own
        return build("holds", walled + own), build("never", own)

    @pytest.mark.parametrize(
        "options",
        [
            pytest.param({"mode": "dense"}, id="dense"),
            pytest.param({"mode": "sparse"}, id="sparse"),
            pytest.param({"mode": "hybrid", "fusion": "rrf"}, id="hybrid-rrf"),
            pytest.param({"mode": "hybrid", "fusion": "weighted"}, id="hybrid-weighted"),
        ],
    )
    def test_the_withheld_corpus_does_not_move_what_the_caller_sees(self, tmp_path, options):
        from vectrixdb.testing import assert_indistinguishable

        holds, never = self._pair(tmp_path)
        try:
            assert_indistinguishable(
                lambda: holds.as_principal(ANALYST_B).search(self.QUERY, limit=10, **options),
                lambda: never.as_principal(ANALYST_B).search(self.QUERY, limit=10, **options),
                f"the wall under {options}",
            )
        finally:
            holds.close()
            never.close()

    def test_a_pile_of_withheld_documents_cannot_reorder_the_visible_ones(self, tmp_path):
        """The case that found this, kept because it is the one that breaks.

        Two visible documents, one about covenants and one about escrow, and
        a dozen withheld documents that all say covenant. Weighted over the
        whole index, "covenant" becomes a cheap term, the covenant document
        falls, and the escrow document climbs over it. The caller is handed a
        different order for a reason they are not allowed to know, and no
        score has to be shown for them to see it.
        """
        from vectrixdb.testing import assert_indistinguishable

        walled = [
            (f"covenant review number {n} for the other client", "CL-40219", 2) for n in range(12)
        ]
        own = [
            ("covenant terms for this borrower", "CL-51330", 2),
            ("escrow terms for this borrower", "CL-51330", 2),
        ]
        holds, never = self._pair(tmp_path, walled=walled, own=own)
        try:
            for options in ({"mode": "sparse"}, {"mode": "hybrid"}):
                assert_indistinguishable(
                    lambda o=options: holds.as_principal(ANALYST_B).search(
                        "covenant escrow terms", limit=10, **o
                    ),
                    lambda o=options: never.as_principal(ANALYST_B).search(
                        "covenant escrow terms", limit=10, **o
                    ),
                    f"a dozen withheld covenants under {options}",
                )
        finally:
            holds.close()
            never.close()


class TestSubsetStatistics:
    """The three statistics, checked against the arithmetic directly.

    Through a collection this needs an embedding model and a pair of indexes
    to say anything; here it is a dictionary and a sum, so the boundary
    conditions are cheap to state.
    """

    @pytest.fixture
    def index(self):
        from vectrixdb.core.collection import TextIndex

        index = TextIndex(use_stemming=False)
        index.add("visible", "covenant terms")
        index.add("other", "escrow terms")
        for n in range(10):
            index.add(f"withheld{n}", "covenant covenant covenant review")
        return index

    def test_a_tie_falls_by_id_whatever_else_the_index_holds(self):
        """Two visible documents that score alike come back in the same order with or without the withheld ones.

        Their order once followed the order a set handed the query's words
        over, which changes from run to run, so a pile of withheld documents
        could swap two visible ones on one machine and not another.
        """
        from vectrixdb.core.collection import TextIndex

        def index(*withheld):
            built = TextIndex(use_stemming=False)
            for n, text in enumerate(withheld):
                built.add(f"withheld{n}", text)
            built.add("b-escrow", "escrow terms for this borrower")
            built.add("a-covenant", "covenant terms for this borrower")
            return built

        visible = {"a-covenant", "b-escrow"}
        holds = index(*["covenant review for the other client"] * 12).search(
            "covenant escrow terms", doc_ids=visible, statistics_over_subset=True
        )
        never = index().search(
            "covenant escrow terms", doc_ids=visible, statistics_over_subset=True
        )
        assert holds == never
        assert holds[0][1] == holds[1][1], "the two must tie for this to test anything"
        assert [doc for doc, _ in holds] == ["a-covenant", "b-escrow"]

    def test_the_whole_index_is_still_the_default(self, index):
        """Changing what an ordinary filtered search scores would be a
        ranking change for everybody, and this is a security fix."""
        # Twelve documents hold the term, so a limit of ten would drop the
        # one this compares and the failure would look like a difference.
        wide = dict(index.search("covenant", limit=20))
        narrow = dict(index.search("covenant", limit=20, doc_ids={"visible"}))

        assert narrow["visible"] == wide["visible"]

    def test_the_subset_changes_the_score(self, index):
        subset = dict(
            index.search("covenant", limit=10, doc_ids={"visible"}, statistics_over_subset=True)
        )
        wide = dict(index.search("covenant", limit=10, doc_ids={"visible"}))

        assert subset["visible"] != wide["visible"]

    def test_the_withheld_documents_are_out_of_the_statistics(self, index):
        """The property that matters: what the visible document scores does
        not depend on what else is in the index."""
        with_pile = index.search(
            "covenant", limit=10, doc_ids={"visible", "other"}, statistics_over_subset=True
        )
        for n in range(10):
            index.remove(f"withheld{n}")
        without = index.search(
            "covenant", limit=10, doc_ids={"visible", "other"}, statistics_over_subset=True
        )

        assert with_pile == without

    def test_an_empty_subset_finds_nothing(self, index):
        assert index.search("covenant", limit=10, doc_ids=set(), statistics_over_subset=True) == []

    def test_a_subset_naming_only_deleted_documents_finds_nothing(self, index):
        assert (
            index.search("covenant", limit=10, doc_ids={"gone"}, statistics_over_subset=True) == []
        )

    def test_it_refuses_to_guess_at_the_subset(self, index):
        """Defaulting to the whole index here would be the bug it exists to
        prevent, quietly."""
        with pytest.raises(ValueError, match="needs a doc_ids set"):
            index.search("covenant", limit=10, statistics_over_subset=True)

    def test_a_visible_document_with_no_tokens_does_not_divide_by_zero(self):
        from vectrixdb.core.collection import TextIndex

        index = TextIndex(use_stemming=False)
        index.add("empty", "")
        index.add("real", "covenant terms")

        assert (
            index.search("covenant", limit=10, doc_ids={"empty"}, statistics_over_subset=True) == []
        )


class TestBoundToTheCollection:
    """A policy the caller has to remember is a policy that gets forgotten.
    These are the tests that forgetting it cannot silently return everything.
    """

    ROWS = [
        ("Facility covenant: fixed charge coverage ratio not less than 1.15x", "CL-40219", 2),
        ("Covenant schedule: tested quarterly against borrower accounts", "CL-40219", 2),
        ("Internal credit memo: covenant headroom is thin into Q3", "CL-40219", 5),
        ("Facility covenant for the other borrower, semi-annual", "CL-51330", 2),
    ]

    def _build(self, path, policy=None):
        from vectrixdb import Vectrix

        db = Vectrix("lending", path=str(path), policy=policy)
        if db._count_all() == 0:
            db.add(
                [text for text, _, _ in self.ROWS],
                metadata=[chunk(client, rank) for _, client, rank in self.ROWS],
            )
        return db

    def test_a_search_without_a_principal_refuses(self, tmp_path):
        from vectrixdb.exceptions import PrincipalRequired

        db = self._build(tmp_path, LENDING)
        try:
            with pytest.raises(PrincipalRequired, match="needs a principal"):
                db.search("covenant thresholds")
        finally:
            db.close()

    def test_clear_keeps_the_policy_and_the_model(self, tmp_path):
        """clear() recreates the collection; the policy and the recorded
        model belong to it, not to its documents."""
        from vectrixdb import Vectrix
        from vectrixdb.exceptions import PrincipalRequired

        db = self._build(tmp_path, LENDING)
        model = db.embedding_model
        db.clear()
        db.add([self.ROWS[0][0]], metadata=[chunk("CL-40219", 2)])
        try:
            with pytest.raises(PrincipalRequired):
                db.search("covenant")
            assert db.embedding_model == model
        finally:
            db.close()
        db = Vectrix("lending", path=str(tmp_path))
        try:
            assert db.policy is not None
            assert db.policy.fingerprint == LENDING.fingerprint
            with pytest.raises(PrincipalRequired):
                db.search("covenant")
        finally:
            db.close()

    def test_get_by_id_refuses_too(self, tmp_path):
        """An id lookup is a read. Leaving it open would make the policy a
        speed bump: ids are guessable, sequential, or come back from somewhere
        else in the system."""
        from vectrixdb.exceptions import PrincipalRequired

        db = self._build(tmp_path, LENDING)
        try:
            with pytest.raises(PrincipalRequired):
                db.get("anything")
        finally:
            db.close()

    def test_get_by_id_cannot_reach_past_the_policy(self, tmp_path):
        """Holding an id is not an entitlement. A denial and a miss are the
        same empty list, because saying "that exists but is not yours"
        confirms the document."""
        db = self._build(tmp_path, LENDING)
        try:
            everything = db.as_principal(ANALYST_A).search("covenant", limit=10)
            memo_ids = [
                doc_id
                for doc_id in db.as_principal(dict(ANALYST_A, clearance_rank=9))
                .search("credit memo headroom", limit=10)
                .ids
                if doc_id not in everything.ids
            ]
            assert memo_ids, "fixture should have a chunk above A's clearance"

            assert db.as_principal(ANALYST_A).get(memo_ids) == []
            assert db.as_principal(dict(ANALYST_A, clearance_rank=9)).get(memo_ids) != []
        finally:
            db.close()

    def test_a_view_reads_as_its_principal(self, tmp_path):
        db = self._build(tmp_path, LENDING)
        try:
            a = db.as_principal(ANALYST_A).search("covenant thresholds", limit=10)
            b = db.as_principal(ANALYST_B).search("covenant thresholds", limit=10)
            assert len(a) == 2
            assert [r.text for r in b] == [self.ROWS[3][0]]
        finally:
            db.close()

    def test_a_view_does_not_leak_back_onto_the_collection(self, tmp_path):
        """as_principal returns a view. The collection itself never acquires
        a principal, so a later unqualified search still refuses."""
        from vectrixdb.exceptions import PrincipalRequired

        db = self._build(tmp_path, LENDING)
        try:
            db.as_principal(ANALYST_A).search("covenant", limit=1)
            assert db.principal is None
            with pytest.raises(PrincipalRequired):
                db.search("covenant")
        finally:
            db.close()

    def test_a_callers_filter_narrows_and_cannot_widen(self, tmp_path):
        """A caller passing a filter over the same field the policy uses must
        not be able to reach past it, which a merged field-keyed dict would
        allow."""
        db = self._build(tmp_path, LENDING)
        try:
            view = db.as_principal(ANALYST_B)
            reached = view.search(
                "covenant thresholds",
                limit=10,
                filter={"entitlements.client_id": "CL-40219"},
            )
            assert list(reached) == []
        finally:
            db.close()

    def test_the_policy_survives_reopening_without_it(self, tmp_path):
        """The failure this whole feature exists to remove: an argument left
        off a constructor turning the control off."""
        from vectrixdb.exceptions import PrincipalRequired

        first = self._build(tmp_path, LENDING)
        first.close()

        reopened = self._build(tmp_path)  # no policy argument
        try:
            assert reopened.policy is not None
            assert reopened.policy.fingerprint == LENDING.fingerprint
            with pytest.raises(PrincipalRequired):
                reopened.search("covenant thresholds")
        finally:
            reopened.close()

    def test_reopening_under_a_different_policy_refuses(self, tmp_path):
        """Either a deployment that has drifted, or an attempt to relax a
        control by reopening. Neither is a silent success."""
        from vectrixdb.exceptions import PolicyMismatch

        first = self._build(tmp_path, LENDING)
        first.close()

        relaxed = Policy([Overlap("entitlements.allowed_roles", "roles")])
        with pytest.raises(PolicyMismatch, match="was created with policy"):
            self._build(tmp_path, relaxed)

    def test_a_principal_matching_nothing_returns_empty_without_searching(self, tmp_path):
        """Proved by watching the collection rather than by reading the clock.

        This asserted time_ms == 0.0, which was true and was also the tell:
        a caller could read "no work was done" straight off the result, and
        every other empty answer came back with a real duration beside it.
        The reported time is padded now, so the test asks the question it
        actually means, which is whether the index was touched.
        """
        db = self._build(tmp_path, LENDING)
        searched = []
        db._collection.search = lambda *a, **k: searched.append(k) or []
        try:
            results = db.as_principal(dict(ANALYST_A, roles=[])).search("covenant", limit=10)

            assert list(results) == []
            assert searched == []
        finally:
            db.close()

    def test_a_collection_with_no_policy_is_unchanged(self, tmp_path):
        """Everything above is opt-in. A collection without a policy behaves
        exactly as it always has."""
        db = self._build(tmp_path)
        try:
            assert db.policy is None
            assert len(db.search("covenant thresholds", limit=10)) == 4
        finally:
            db.close()

    def test_a_principal_must_be_a_mapping(self, tmp_path):
        db = self._build(tmp_path, LENDING)
        try:
            with pytest.raises(TypeError, match="a principal is a mapping"):
                db.as_principal(["credit_analyst"])
        finally:
            db.close()


# --------------------------------------------------------------------------
# Every read path, not just the two that were obvious
# --------------------------------------------------------------------------


class TestEveryReadPath:
    """`search()` and `get()` were gated and four other ways in were not. A
    control with a way round it is a control nobody should rely on, so the
    rule now is that a read path is either policied or it refuses.
    """

    ROWS = [
        ("Facility covenant: coverage ratio not less than 1.15x", "CL-40219", 2),
        ("Covenant schedule tested quarterly", "CL-40219", 2),
        ("Internal credit memo: headroom thin into Q3", "CL-40219", 5),
        ("Facility covenant for the other borrower", "CL-51330", 2),
    ]

    @pytest.fixture
    def db(self, tmp_path):
        from vectrixdb import Vectrix

        db = Vectrix("reads", path=str(tmp_path), policy=LENDING)
        db.add(
            [text for text, _, _ in self.ROWS],
            metadata=[chunk(client, rank) for _, client, rank in self.ROWS],
        )
        yield db
        db.close()

    # -- count ----------------------------------------------------------

    def test_count_answers_for_the_principal_not_the_collection(self, db):
        """The total is a fact about documents a principal may not know
        exist. A walled analyst watching the number move learns that a client
        they were refused is being written to."""
        assert db._count_all() == 4
        assert db.as_principal(ANALYST_A).count() == 2
        assert db.as_principal(ANALYST_B).count() == 1

    def test_the_host_still_counts_its_own_collection(self, db):
        """The base object is the host's handle by construction: as_principal
        returns a copy and nothing leads back to the original, so a principal
        only ever holds a view. Locking the host out of its own collection
        would buy nothing and break every caller that counts before seeding."""
        assert db.count() == 4
        assert len(db) == 4

    def test_len_follows_count(self, db):
        assert len(db.as_principal(ANALYST_B)) == 1

    def test_a_write_the_principal_cannot_see_does_not_move_their_count(self, db):
        """The leak this closes, stated directly."""
        before = db.as_principal(ANALYST_B).count()
        db.add(["A further CL-40219 covenant"], metadata=[chunk("CL-40219", 2)])
        assert db.as_principal(ANALYST_B).count() == before
        assert db._count_all() == 5

    def test_an_unpoliced_collection_counts_as_it_always_did(self, tmp_path):
        from vectrixdb import Vectrix

        plain = Vectrix("plain", path=str(tmp_path))
        try:
            plain.add(["one", "two"])
            assert plain.count() == 2 == len(plain)
        finally:
            plain.close()

    def test_repr_does_not_raise_on_a_policied_collection(self, db):
        """A repr is for whoever holds the object, which is the host, and it
        must not blow up while somebody is debugging."""
        assert "reads" in repr(db)

    # -- similar, which was a straight bypass ----------------------------

    def test_similar_cannot_pivot_off_a_document_the_principal_cannot_see(self, db):
        """It pulled the document's vector straight from the collection and
        ran an unfiltered dense search, so an id was enough to reach the
        neighbours of a walled document, which are the documents most likely
        to be about the same client."""
        walled_id = db.as_principal(ANALYST_A).search("coverage ratio", limit=1).ids[0]

        assert list(db.as_principal(ANALYST_B).similar(walled_id, limit=5)) == []

    def test_similar_filters_what_it_returns_as_well(self, db):
        """Both ends: the source has to be visible and so do the neighbours.
        A sees their own client's other chunk and never B's book."""
        own_id = db.as_principal(ANALYST_A).search("coverage ratio", limit=1).ids[0]
        neighbours = db.as_principal(ANALYST_A).similar(own_id, limit=5)

        assert [r.text for r in neighbours] == [self.ROWS[1][0]]

    def test_similar_treats_a_denial_as_a_miss(self, db):
        """Same answer as a document that is not there, deliberately. Telling
        a caller an id exists but is not theirs confirms the document."""
        walled_id = db.as_principal(ANALYST_A).search("coverage ratio", limit=1).ids[0]
        view = db.as_principal(ANALYST_B)

        assert list(view.similar(walled_id, limit=5)) == list(view.similar("no_such_id", limit=5))

    def test_similar_without_a_principal_refuses(self, db):
        from vectrixdb.exceptions import PrincipalRequired

        any_id = db.as_principal(ANALYST_A).search("coverage ratio", limit=1).ids[0]
        with pytest.raises(PrincipalRequired):
            db.similar(any_id)

    # -- paths a policy over document metadata cannot decide --------------

    @pytest.mark.parametrize(
        "operation",
        [
            pytest.param(lambda db, tmp: db.export(tmp / "x.zip"), id="export"),
            pytest.param(lambda db, tmp: db.recall("q"), id="recall"),
            pytest.param(lambda db, tmp: db.context("q"), id="context"),
            pytest.param(lambda db, tmp: db.graph_path("a", "b"), id="graph_path"),
            pytest.param(lambda db, tmp: db.graph_explain("a"), id="graph_explain"),
        ],
    )
    def test_what_cannot_be_decided_refuses_rather_than_answering(self, db, tmp_path, operation):
        """Fail closed is the whole point, and a path that cannot be decided
        is exactly where quietly answering would do the damage. A snapshot is
        the whole collection; conversation memory searches through its own
        path; a graph traversal walks entities, which the policy has nothing
        to say about."""
        from vectrixdb.exceptions import PolicyError

        with pytest.raises(PolicyError, match="not available to a principal"):
            operation(db.as_principal(ANALYST_A), tmp_path)

    def test_the_refusal_says_where_to_go_instead(self, db, tmp_path):
        from vectrixdb.exceptions import PolicyError

        with pytest.raises(PolicyError, match="through its host"):
            db.as_principal(ANALYST_A).export(tmp_path / "x.zip")

    def test_administrative_work_still_belongs_to_the_host(self, db, tmp_path):
        """export and the graph traversals refuse a principal and answer the
        host, because they are not per-principal questions at all."""
        assert db.export(tmp_path / "host.zip").exists()

    def test_memory_refuses_the_host_too(self, db):
        """The one tier with no host exemption. recall and context search
        through their own path, which the policy does not reach, so there is
        no principal that would make them safe and no handle that would."""
        from vectrixdb.exceptions import PolicyError

        with pytest.raises(PolicyError):
            db.recall("covenant")
        with pytest.raises(PolicyError):
            db.context("covenant")

    def test_content_reads_still_refuse_the_host(self, db):
        """The headline property, unchanged by the tiering: search, get and
        similar are what a request handler calls, and a forgotten
        as_principal there is the leak this exists to prevent."""
        from vectrixdb.exceptions import PrincipalRequired

        for call in (
            lambda: db.search("covenant"),
            lambda: db.get("any_id"),
            lambda: db.similar("any_id"),
        ):
            with pytest.raises(PrincipalRequired):
                call()

    def test_none_of_that_touches_an_unpoliced_collection(self, tmp_path):
        """Everything above is opt-in with the policy. Without one these all
        work exactly as they always have."""
        from vectrixdb import Vectrix

        plain = Vectrix("plain", path=str(tmp_path / "db"))
        try:
            plain.add(["one covenant", "two covenant"])
            assert plain.export(tmp_path / "x.zip").exists()
            assert plain.count() == 2
            assert len(plain.similar(plain.search("covenant", limit=1).ids[0], limit=5)) == 1
        finally:
            plain.close()


class TestEverySearchMode:
    """Every mode carries the principal, not just the default one.

    `mode="sparse"` called `keyword_search` without one. Nothing leaked,
    because that method refuses a policied collection handed no principal,
    but it refused callers who had supplied one: sparse search was unusable
    under a policy while dense and hybrid worked, and no test noticed because
    they all took the default mode.
    """

    @pytest.fixture
    def db(self, tmp_path):
        from vectrixdb import Vectrix

        db = Vectrix("modes", path=str(tmp_path), policy=LENDING, mode="hybrid")
        db.add(
            [text for text, _, _ in TestEveryReadPath.ROWS],
            metadata=[chunk(client, rank) for _, client, rank in TestEveryReadPath.ROWS],
        )
        yield db
        db.close()

    @pytest.mark.parametrize("mode", ["dense", "sparse", "hybrid"])
    def test_a_principal_can_search_in_any_mode(self, db, mode):
        found = db.as_principal(ANALYST_B).search("covenant", limit=10, mode=mode)

        assert [r.text for r in found] == [TestEveryReadPath.ROWS[3][0]]

    @pytest.mark.parametrize("mode", ["dense", "sparse", "hybrid"])
    def test_no_mode_answers_without_a_principal(self, db, mode):
        from vectrixdb.exceptions import PrincipalRequired

        with pytest.raises(PrincipalRequired):
            db.search("covenant", limit=10, mode=mode)


class TestEnforcedInTheCollection:
    """Policies used to be enforced by `Vectrix`, so the REST server and
    everything else built on `VectrixDB` reached a `Collection` directly and
    saw every document the query matched. A warning made that loud. This is
    the test that it now stops rather than complains.
    """

    def _built(self, tmp_path):
        from vectrixdb import Vectrix

        db = Vectrix("lending", path=str(tmp_path), policy=LENDING)
        db.add(
            [text for text, _, _ in TestEveryReadPath.ROWS],
            metadata=[chunk(c, r) for _, c, r in TestEveryReadPath.ROWS],
        )
        vector = db._embed(["covenant"])[0]
        db.close()
        return vector

    def _collection(self, tmp_path):
        from vectrixdb import VectrixDB

        raw = VectrixDB(path=str(tmp_path))
        return raw, raw.get_collection("lending")

    def test_the_collection_carries_the_policy_itself(self, tmp_path):
        """Read from its own metadata, so it applies to every caller rather
        than only the ones that came through Vectrix."""
        self._built(tmp_path)
        raw, collection = self._collection(tmp_path)
        try:
            assert collection.policy is not None
            assert collection.policy.fingerprint == LENDING.fingerprint
        finally:
            raw.close()

    def test_searching_it_directly_refuses(self, tmp_path):
        """What the REST server does. It used to return all four documents."""
        from vectrixdb.exceptions import PrincipalRequired

        vector = self._built(tmp_path)
        raw, collection = self._collection(tmp_path)
        try:
            with pytest.raises(PrincipalRequired):
                collection.search(query=vector, limit=10)
        finally:
            raw.close()

    def test_the_sparse_half_refuses_too(self, tmp_path):
        """keyword_search feeds the sparse side of a hybrid search, so an
        ungated one puts documents the principal may not see into the fusion
        and out through the answer."""
        from vectrixdb.exceptions import PrincipalRequired

        self._built(tmp_path)
        raw, collection = self._collection(tmp_path)
        try:
            with pytest.raises(PrincipalRequired):
                collection.keyword_search("covenant", limit=10)
        finally:
            raw.close()

    def test_with_a_principal_it_filters(self, tmp_path):
        vector = self._built(tmp_path)
        raw, collection = self._collection(tmp_path)
        try:
            found = collection.search(query=vector, limit=10, principal=ANALYST_B)
            assert len(found.results) == 1
        finally:
            raw.close()

    def test_it_reports_the_two_withheld_counts(self, tmp_path):
        """The loop already sees every candidate before deciding, so the
        counts come out of the same pass and are exact rather than bounded by
        an audit window."""
        vector = self._built(tmp_path)
        raw, collection = self._collection(tmp_path)
        try:
            found = collection.search(query=vector, limit=10, principal=ANALYST_A)
            assert found.policy_withheld_disclosable == 1
            assert found.policy_withheld_undisclosable == 1
            assert found.policy_candidates == 4
        finally:
            raw.close()

    def test_the_counts_are_not_in_the_serialised_result(self, tmp_path):
        """They are audit material. The undisclosable one confirms documents
        exist outside a scope the caller was refused."""
        import json

        vector = self._built(tmp_path)
        raw, collection = self._collection(tmp_path)
        try:
            found = collection.search(query=vector, limit=10, principal=ANALYST_B)
            assert "undisclosable" not in json.dumps(found.to_dict())
        finally:
            raw.close()

    def test_the_acl_surfaces_refuse_rather_than_running_one_control(self, tmp_path):
        """`/search/acl` reaches these. ACL principals and an entitlement
        policy are two different controls, and running only one of them
        silently is the hazard."""
        from vectrixdb.exceptions import PolicyError

        vector = self._built(tmp_path)
        raw, collection = self._collection(tmp_path)
        try:
            with pytest.raises(PolicyError, match="two different controls"):
                collection.search_with_acl(query=vector, user_principals=["group:anyone"])
            with pytest.raises(PolicyError, match="two different controls"):
                collection.enterprise_search(query=vector, user_principals=["group:anyone"])
        finally:
            raw.close()

    def test_a_collection_with_no_policy_is_untouched(self, tmp_path):
        from vectrixdb import Vectrix, VectrixDB

        db = Vectrix("plain", path=str(tmp_path))
        db.add(["covenant ratio", "covenant schedule"])
        vector = db._embed(["covenant"])[0]
        db.close()

        raw = VectrixDB(path=str(tmp_path))
        try:
            found = raw.get_collection("plain").search(query=vector, limit=10)
            assert len(found.results) == 2
            assert found.policy_candidates is None
            assert found.policy_withheld_undisclosable is None
        finally:
            raw.close()

    def test_the_id_lookups_are_gated_too(self, tmp_path):
        """They used to warn, because the collection's own bookkeeping calls
        them and refusing would have broken it looking after itself. The
        bookkeeping says what it is doing now, through the raw accessors, and
        everybody else is checked."""
        from vectrixdb.exceptions import PrincipalRequired

        self._built(tmp_path)
        raw, collection = self._collection(tmp_path)
        try:
            with pytest.raises(PrincipalRequired):
                collection.get("any_id")
            with pytest.raises(PrincipalRequired):
                collection.get_batch(["any_id"])
            with pytest.raises(PrincipalRequired):
                list(collection.iter_documents())
        finally:
            raw.close()

    def test_the_id_lookups_answer_for_a_principal(self, tmp_path):
        self._built(tmp_path)
        raw, collection = self._collection(tmp_path)
        try:
            seen = list(collection.iter_documents(principal=ANALYST_B))
            assert len(seen) == 1
            only_id = seen[0][0]
            assert collection.get(only_id, principal=ANALYST_B) is not None
            # A denial and a miss are the same answer, deliberately.
            assert collection.get(only_id, principal=ANALYST_A) is None
        finally:
            raw.close()

    def test_the_raw_accessors_are_the_named_exception(self, tmp_path):
        """The bookkeeping path. Naming it is what keeps it reviewable: a
        grep for _raw is the list of places that deliberately see everything."""
        self._built(tmp_path)
        raw, collection = self._collection(tmp_path)
        try:
            assert len(list(collection._iter_documents_raw())) == 4
        finally:
            raw.close()

    def test_a_closed_collection_answers_nothing(self, tmp_path):
        """It used to answer a repeat search from the result cache, which is
        stale data from a handle the caller has said they are done with. That
        only came to light when a policy turned the cache off and one of the
        docs examples started failing on exactly that."""
        from vectrixdb import Vectrix, VectrixError

        db = Vectrix("plain", path=str(tmp_path))
        db.add(["covenant ratio"])
        vector = db._embed(["covenant"])[0]
        collection = db._collection

        assert len(collection.search(query=vector, limit=5).results) == 1
        collection.close()

        with pytest.raises(VectrixError, match="which is closed"):
            collection.search(query=vector, limit=5)
        with pytest.raises(VectrixError, match="which is closed"):
            collection.keyword_search("covenant", limit=5)


class TestTheLastFourReadPaths:
    """count, list_ids, scroll and sparse_search reached the documents.

    Found by auditing every public method on Collection rather than by a
    failing test, which is the uncomfortable part: search, keyword_search,
    get, get_batch and iter_documents had all been gated, and the block was
    marked done. The REST server calls three of these four, so a policied
    collection served over HTTP answered a collection page, an id listing and
    a sparse query for anybody who could reach it.

    ROWS holds three CL-40219 documents and one CL-51330, and B covers only
    CL-51330 and is walled from the other.
    """

    @pytest.fixture
    def collection(self, tmp_path):
        from vectrixdb import Vectrix, VectrixDB

        db = Vectrix("lending", path=str(tmp_path), policy=LENDING)
        db.add(
            [text for text, _, _ in TestEveryReadPath.ROWS],
            metadata=[chunk(c, r) for _, c, r in TestEveryReadPath.ROWS],
        )
        db.close()
        raw = VectrixDB(path=str(tmp_path))
        yield raw.get_collection("lending")
        raw.close()

    # -- count ----------------------------------------------------------

    def test_count_refuses_without_a_principal(self, collection):
        from vectrixdb.exceptions import PrincipalRequired

        with pytest.raises(PrincipalRequired):
            collection.count()

    def test_count_answers_for_the_principal(self, collection):
        assert collection.count(ANALYST_A) == 2
        assert collection.count(ANALYST_B) == 1

    def test_the_raw_count_is_still_the_whole_collection(self, collection):
        """What the library's own bookkeeping uses, and the reason gating the
        public one does not break prefetch sizing or a repr."""
        assert collection._count_raw() == 4

    # -- list_ids -------------------------------------------------------

    def test_list_ids_refuses_without_a_principal(self, collection):
        from vectrixdb.exceptions import PrincipalRequired

        with pytest.raises(PrincipalRequired):
            collection.list_ids()

    def test_list_ids_lists_only_what_the_principal_may_see(self, collection):
        assert len(collection.list_ids(principal=ANALYST_A)) == 2
        assert len(collection.list_ids(principal=ANALYST_B)) == 1

    def test_list_ids_pages_over_the_visible_documents(self, collection):
        """Offset counts visible documents, not rows. Paging over rows would
        hand back a short page whenever a withheld document fell inside it,
        and the gaps would map the wall."""
        first = collection.list_ids(limit=1, offset=0, principal=ANALYST_A)
        second = collection.list_ids(limit=1, offset=1, principal=ANALYST_A)

        assert len(first) == len(second) == 1
        assert first != second

    # -- scroll ---------------------------------------------------------

    def test_scroll_refuses_without_a_principal(self, collection):
        from vectrixdb.exceptions import PrincipalRequired

        with pytest.raises(PrincipalRequired):
            collection.scroll()

    def test_scroll_returns_only_what_the_principal_may_see(self, collection):
        points, _ = collection.scroll(limit=100, principal=ANALYST_B)

        assert [p.text for p in points] == [TestEveryReadPath.ROWS[3][0]]

    def test_scroll_pages_without_leaking_the_gaps(self, collection):
        """A page is a page of visible documents. Two of A's two come back
        across two pages of one, with no empty page in between."""
        seen = []
        offset = 0
        while True:
            page, offset = collection.scroll(limit=1, offset=offset, principal=ANALYST_A)
            seen.extend(p.id for p in page)
            if offset is None:
                break

        assert len(seen) == 2 == len(set(seen))

    def test_the_raw_scroll_still_sees_everything(self, collection):
        points, _ = collection._scroll_raw(limit=100)

        assert len(points) == 4

    # -- sparse_search --------------------------------------------------

    def test_sparse_search_refuses_without_a_principal(self, collection):
        from vectrixdb.exceptions import PrincipalRequired

        with pytest.raises(PrincipalRequired):
            collection.sparse_search({0: 1.0}, limit=10)

    def test_dense_sparse_search_refuses_too(self, collection):
        """It refuses through the dense half, which calls search(). Pinned
        because that is luck rather than design, and the next person to
        reorder those two calls should hear about it."""
        from vectrixdb.exceptions import PrincipalRequired

        with pytest.raises(PrincipalRequired):
            collection.dense_sparse_search(
                dense_query=[0.0] * collection.dimension,
                sparse_query={0: 1.0},
                limit=10,
            )

    # -- and the ordinary case ------------------------------------------

    def test_an_unpoliced_collection_is_untouched(self, tmp_path):
        from vectrixdb import Vectrix, VectrixDB

        db = Vectrix("plain", path=str(tmp_path / "plain"))
        db.add(["one", "two"])
        db.close()
        raw = VectrixDB(path=str(tmp_path / "plain"))
        try:
            plain = raw.get_collection("plain")

            assert plain.count() == 2
            assert len(plain.list_ids()) == 2
            assert len(plain.scroll(limit=10)[0]) == 2
        finally:
            raw.close()


class TestTimingFloor:
    """The over-fetch loop takes longer for a principal who may see less, and
    no filter closes that: the work really is different. Rounding every
    answer up to a whole number of floors bounds what leaks to which
    multiple, at the cost of latency on every query.
    """

    def _db(self, tmp_path, **kwargs):
        from vectrixdb import Vectrix

        db = Vectrix("timed", path=str(tmp_path), policy=LENDING, **kwargs)
        db.add(
            [text for text, _, _ in TestEveryReadPath.ROWS],
            metadata=[chunk(c, r) for _, c, r in TestEveryReadPath.ROWS],
        )
        return db

    # -- the arithmetic -------------------------------------------------

    @pytest.mark.parametrize(
        "elapsed_ms,expected",
        [
            (0.0, 250.0),
            (1.0, 250.0),
            (249.9, 250.0),
            (250.0, 500.0),
            (251.0, 500.0),
            (900.0, 1000.0),
        ],
    )
    def test_it_rounds_up_to_the_next_whole_floor(
        self, tmp_path, monkeypatch, elapsed_ms, expected
    ):
        """Up to the next multiple, not to a single deadline. Padding to one
        deadline would report the true duration of anything that overran it,
        which is the whole distribution above the floor."""
        import time as clock

        db = self._db(tmp_path, timing_floor=0.25)
        try:
            slept = []
            monkeypatch.setattr(clock, "sleep", slept.append)

            assert db._pad_to_floor(elapsed_ms) == expected
            assert slept == [pytest.approx((expected - elapsed_ms) / 1000.0)]
        finally:
            db.close()

    def test_without_a_floor_the_true_time_is_reported(self, tmp_path):
        db = self._db(tmp_path)
        try:
            assert db._pad_to_floor(17.5) == 17.5
        finally:
            db.close()

    def test_a_floor_of_zero_or_less_is_refused(self, tmp_path):
        """Zero would read as "on" and do nothing, which is the worst of the
        three states to be in."""
        from vectrixdb import Vectrix
        from vectrixdb.exceptions import ConfigurationError

        for bad in (0, -1.0):
            with pytest.raises(ConfigurationError, match="above zero"):
                Vectrix("bad", path=str(tmp_path / "bad"), policy=LENDING, timing_floor=bad)

    # -- what the caller sees -------------------------------------------

    def test_the_reported_time_is_the_padded_one(self, tmp_path):
        """The part that would make this theatre if it were wrong. Sleeping
        and then handing back Results.time_ms with the true duration leaves
        the channel open in a field, which is worse than not padding at all
        because it looks closed."""
        db = self._db(tmp_path, timing_floor=0.05)
        try:
            results = db.as_principal(ANALYST_B).search("covenant", limit=10)

            assert results.time_ms % 50.0 == 0.0
        finally:
            db.close()

    def test_a_denial_and_an_answer_report_the_same_shape(self, tmp_path):
        """Both are multiples of the floor, so the number beside the answer
        stops being a measure of how much was allowed."""
        db = self._db(tmp_path, timing_floor=0.05)
        try:
            allowed = db.as_principal(ANALYST_A).search("covenant", limit=10)
            walled = db.as_principal(ANALYST_B).search("covenant", limit=10)
            nothing = db.as_principal(dict(ANALYST_A, roles=[])).search("covenant", limit=10)

            for results in (allowed, walled, nothing):
                assert results.time_ms % 50.0 == 0.0
                assert results.time_ms >= 50.0
        finally:
            db.close()

    def test_the_wholesale_denial_no_longer_reports_zero(self, tmp_path):
        """It returned instantly with time_ms=0.0, which a caller could read
        straight off the result while every other empty answer carried a real
        duration. True, and a tell."""
        db = self._db(tmp_path)
        try:
            results = db.as_principal(dict(ANALYST_A, roles=[])).search("covenant", limit=10)

            assert list(results) == []
            assert results.time_ms > 0.0
        finally:
            db.close()

    def test_similar_is_padded_too(self, tmp_path):
        """It reported a constant zero, which said nothing to a reader and
        "no work was done" to a timer."""
        db = self._db(tmp_path, timing_floor=0.05)
        try:
            known = db.as_principal(ANALYST_A).search("coverage ratio", limit=1).ids[0]
            found = db.as_principal(ANALYST_A).similar(known, limit=5)
            missing = db.as_principal(ANALYST_A).similar("no_such_id", limit=5)

            assert found.time_ms % 50.0 == 0.0
            assert missing.time_ms % 50.0 == 0.0
        finally:
            db.close()

    def test_it_really_sleeps(self, tmp_path):
        """One integration check that the padding is not just arithmetic on
        the reported number. A floor of a fifth of a second against a search
        that takes milliseconds: if nothing slept, this comes back at once."""
        import time as clock

        db = self._db(tmp_path, timing_floor=0.2)
        try:
            started = clock.perf_counter()
            db.as_principal(ANALYST_B).search("covenant", limit=10)
            took = clock.perf_counter() - started

            assert took >= 0.2
        finally:
            db.close()

    def test_an_unpoliced_collection_can_use_it_too(self, tmp_path):
        """No reason to refuse it. The reason it exists is a policy, but a
        floor is a floor."""
        from vectrixdb import Vectrix

        db = Vectrix("plain", path=str(tmp_path / "plain"), timing_floor=0.05)
        try:
            db.add(["one"])

            assert db.search("one", limit=1).time_ms % 50.0 == 0.0
        finally:
            db.close()


class TestRevocation:
    """Entitlements are denormalised onto the chunk, which is what makes a
    query fast and a permission change expensive: one person leaving a deal
    team means every chunk of every document for that client. Doing that as a
    loop over update_metadata is doing it by hand, and the hand slips.
    """

    TEAM = Policy(
        [
            Overlap("entitlements.allowed_roles", "roles"),
            Overlap("entitlements.need_to_know", "teams", scope=True),
        ]
    )

    def _rows(self):
        return [
            ("Facility covenant ratio 1.15x", "CL-40219", "deal_40219"),
            ("Covenant schedule quarterly", "CL-40219", "deal_40219"),
            ("Other borrower facility", "CL-51330", "deal_51330"),
        ]

    def _meta(self, client, team):
        return {
            "entitlements": {
                "allowed_roles": ["credit_analyst"],
                "client_id": client,
                "need_to_know": team,
            }
        }

    @pytest.fixture
    def db(self, tmp_path):
        from vectrixdb import Vectrix

        db = Vectrix("lending", path=str(tmp_path), policy=self.TEAM)
        db.add(
            [text for text, _, _ in self._rows()],
            metadata=[self._meta(c, t) for _, c, t in self._rows()],
        )
        yield db
        db.close()

    ON_TEAM = {"roles": ["credit_analyst"], "teams": ["deal_40219"]}

    def test_revoking_takes_effect_on_the_next_search(self, db):
        """The whole point. Removing the field the scope rule reads makes
        those chunks invisible, because an absent field denies."""
        assert len(db.as_principal(self.ON_TEAM).search("covenant", limit=10)) == 2

        report = db.revoke(
            where={"entitlements.client_id": "CL-40219"},
            unset=["entitlements.need_to_know"],
        )

        assert (report.matched, report.changed) == (2, 2)
        assert len(db.as_principal(self.ON_TEAM).search("covenant", limit=10)) == 0

    def test_matched_and_changed_are_different_numbers(self, db):
        """The gap between them is the interesting part: matched many and
        changed none means it had already been applied. One combined count
        would hide that."""
        where = {"entitlements.client_id": "CL-40219"}
        first = db.revoke(where=where, unset=["entitlements.need_to_know"])
        assert (first.matched, first.changed) == (2, 2)

        again = db.revoke(where=where, unset=["entitlements.need_to_know"])
        assert (again.matched, again.changed) == (2, 0), "already applied, nothing to do"

    def test_unset_removes_rather_than_nulls(self, db):
        """Not the same thing. The filter engine tells an absent field from a
        null one, and under a policy the first denies while the second is a
        value like any other."""
        db.revoke(
            where={"entitlements.client_id": "CL-40219"},
            unset=["entitlements.need_to_know"],
        )
        collection = db._collection
        for doc_id, _, metadata in collection._iter_documents_raw():
            if metadata["entitlements"]["client_id"] == "CL-40219":
                assert "need_to_know" not in metadata["entitlements"]

    def test_set_merges_a_new_value(self, db):
        report = db.revoke(
            where={"entitlements.client_id": "CL-40219"},
            set={"entitlements.need_to_know": "deal_40219_wound_down"},
        )
        assert report.changed == 2

        moved = {"roles": ["credit_analyst"], "teams": ["deal_40219_wound_down"]}
        assert len(db.as_principal(moved).search("covenant", limit=10)) == 2
        assert len(db.as_principal(self.ON_TEAM).search("covenant", limit=10)) == 0

    def test_it_leaves_everything_else_alone(self, db):
        db.revoke(
            where={"entitlements.client_id": "CL-40219"},
            unset=["entitlements.need_to_know"],
        )
        other = {"roles": ["credit_analyst"], "teams": ["deal_51330"]}
        assert len(db.as_principal(other).search("borrower facility", limit=10)) == 1

    def test_an_empty_filter_is_refused(self, db):
        """It matches every document, so this would rewrite the entitlements
        on the whole collection. The truthiness family, in the one place it
        would be a mass edit."""
        with pytest.raises(ValueError, match="needs a filter"):
            db.revoke(where={}, unset=["entitlements.need_to_know"])

    def test_it_needs_something_to_do(self, db):
        with pytest.raises(ValueError, match="something to set or unset"):
            db.revoke(where={"entitlements.client_id": "CL-40219"})

    def test_a_principal_cannot_revoke_through_their_own_view(self, db):
        """Administrative, like export. Changing entitlements is not
        something a principal does to the view they were given."""
        from vectrixdb.exceptions import PolicyError

        with pytest.raises(PolicyError):
            db.as_principal(self.ON_TEAM).revoke(
                where={"entitlements.client_id": "CL-40219"},
                unset=["entitlements.need_to_know"],
            )

    def test_the_report_says_when_and_what(self, db):
        report = db.revoke(
            where={"entitlements.client_id": "CL-40219"},
            unset=["entitlements.need_to_know"],
        )
        assert report.fields == ("entitlements.need_to_know",)
        assert report.at.tzinfo is not None
        assert report.to_dict()["matched"] == 2

    def test_it_moves_the_index_build_id(self, db):
        """A revocation changes what the index answers, so a decision record
        made after it must not name the build from before."""
        before = db.index_build_id
        db.revoke(
            where={"entitlements.client_id": "CL-40219"},
            unset=["entitlements.need_to_know"],
        )
        assert db.index_build_id != before


class TestTheMetadataContract:
    """Every rule names a document field, and an absent field denies. So a
    chunk written without one is invisible to every principal, for ever, and
    the symptom reads as a permissions problem: somebody spends an afternoon
    in the entitlement resolver looking for a bug that is in the pipeline.

    Catching it at the write is the difference between that afternoon and a
    stack trace pointing at the pipeline.
    """

    RULES = [
        Overlap("entitlements.allowed_roles", "roles"),
        AtMost("entitlements.classification_rank", "clearance_rank"),
        Overlap("entitlements.client_id", "client_coverage", scope=True),
    ]

    def _db(self, tmp_path, **kw):
        from vectrixdb import Vectrix

        return Vectrix("lending", path=str(tmp_path), policy=Policy(self.RULES, **kw))

    def test_the_required_fields_are_the_ones_the_rules_read(self):
        assert Policy(self.RULES).required_document_fields == (
            "entitlements.allowed_roles",
            "entitlements.classification_rank",
            "entitlements.client_id",
        )

    def test_two_rules_over_one_field_name_it_once(self):
        """A wall and a coverage list share a field; the contract is a set of
        paths, not a list of rules."""
        policy = Policy(
            [
                Overlap("e.client", "coverage", scope=True),
                Excludes("e.client", "walls", scope=True),
            ]
        )
        assert policy.required_document_fields == ("e.client",)

    def test_an_incomplete_document_is_refused(self, tmp_path):
        from vectrixdb.exceptions import MetadataContractError

        db = self._db(tmp_path)
        try:
            with pytest.raises(MetadataContractError) as info:
                db.add(
                    ["a covenant"],
                    metadata=[{"entitlements": {"allowed_roles": ["credit_analyst"]}}],
                )
            assert info.value.missing == [
                "entitlements.classification_rank",
                "entitlements.client_id",
            ]
        finally:
            db.close()

    def test_the_refusal_says_it_is_a_pipeline_bug(self, tmp_path):
        """Because the alternative symptom sends people to the wrong place."""
        from vectrixdb.exceptions import MetadataContractError

        db = self._db(tmp_path)
        try:
            with pytest.raises(MetadataContractError, match="permissions problem"):
                db.add(["a covenant"], metadata=[{}])
        finally:
            db.close()

    def test_nothing_is_written_when_one_document_fails(self, tmp_path):
        """The check runs before the write, so a batch with a bad document in
        it does not land half of itself."""
        from vectrixdb.exceptions import MetadataContractError

        db = self._db(tmp_path)
        try:
            with pytest.raises(MetadataContractError):
                db.add(
                    ["good covenant", "bad covenant"],
                    metadata=[chunk("CL-40219", 2), {}],
                )
            assert db._count_all() == 0
        finally:
            db.close()

    def test_a_complete_document_is_accepted(self, tmp_path):
        db = self._db(tmp_path)
        try:
            db.add(["a covenant"], metadata=[chunk("CL-40219", 2)])
            assert db._count_all() == 1
        finally:
            db.close()

    def test_a_null_value_satisfies_the_contract(self, tmp_path):
        """Absent and null are different, and only absent denies. A field
        deliberately set to null is a value the policy can decide on."""
        db = self._db(tmp_path)
        try:
            meta = chunk("CL-40219", 2)
            meta["entitlements"]["client_id"] = None
            db.add(["a covenant"], metadata=[meta])
            assert db._count_all() == 1
        finally:
            db.close()

    def test_warn_writes_it_and_says_so(self, tmp_path):
        """For backfilling a collection that already has such chunks."""
        from vectrixdb.exceptions import MetadataContractWarning

        db = self._db(tmp_path, on_incomplete_document="warn")
        try:
            with pytest.warns(MetadataContractWarning, match="no principal will see it"):
                db.add(["a covenant"], metadata=[{}])
            assert db._count_all() == 1
        finally:
            db.close()

    def test_allow_is_silent(self, tmp_path):
        import warnings

        db = self._db(tmp_path, on_incomplete_document="allow")
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                db.add(["a covenant"], metadata=[{}])
            assert not [w for w in caught if "MetadataContract" in w.category.__name__]
            assert db._count_all() == 1
        finally:
            db.close()

    def test_a_written_incomplete_document_really_is_invisible(self, tmp_path):
        """The thing the contract exists to prevent, demonstrated."""
        db = self._db(tmp_path, on_incomplete_document="allow")
        try:
            db.add(["a covenant nobody will see"], metadata=[{}])
            db.add(["a covenant they will"], metadata=[chunk("CL-40219", 2)])

            seen = db.as_principal(ANALYST_A).search("covenant", limit=10)
            assert [r.text for r in seen] == ["a covenant they will"]
            assert db._count_all() == 2, "it is in the collection, just unreachable"
        finally:
            db.close()

    def test_an_unknown_mode_is_refused_at_construction(self):
        with pytest.raises(PolicyDefinitionError, match="reject"):
            Policy(self.RULES, on_incomplete_document="ignore")

    def test_a_collection_with_no_policy_has_no_contract(self, tmp_path):
        from vectrixdb import Vectrix

        db = Vectrix("plain", path=str(tmp_path))
        try:
            db.add(["anything at all"], metadata=[{}])
            assert db._count_all() == 1
        finally:
            db.close()


class TestAValueThePolicyCannotDecideOn:
    """The half of the write path a schema check cannot see.

    "high" is a perfectly good string. It is not greater than any clearance
    and not less than one either, so AtMost is false for every principal and
    the document is invisible to everybody, for ever, with the symptom of a
    permissions problem. Absent and undecidable are one bug with two
    spellings, so they are caught in one place and refused the same way.
    """

    RULES = [
        Overlap("entitlements.allowed_roles", "roles"),
        AtMost("entitlements.classification_rank", "clearance_rank"),
        Excludes("entitlements.client_id", "wall_restrictions", scope=True),
    ]

    def _db(self, tmp_path, **kw):
        from vectrixdb import Vectrix

        return Vectrix("lending", path=str(tmp_path), policy=Policy(self.RULES, **kw))

    def _meta(self, **overrides):
        base = {
            "allowed_roles": ["credit_analyst"],
            "classification_rank": 2,
            "client_id": "CL-40219",
        }
        base.update(overrides)
        return {"entitlements": base}

    # -- what each rule can decide on -----------------------------------

    @pytest.mark.parametrize(
        "rule,value,decidable",
        [
            (AtMost("e.rank", "clearance"), 2, True),
            (AtMost("e.rank", "clearance"), 2.5, True),
            (AtMost("e.rank", "clearance"), None, True),
            (AtMost("e.rank", "clearance"), "high", False),
            (AtMost("e.rank", "clearance"), ["2"], False),
            (AtLeast("e.rank", "clearance"), "high", False),
            (Overlap("e.roles", "roles"), "analyst", True),
            (Overlap("e.roles", "roles"), ["analyst"], True),
            (Overlap("e.roles", "roles"), [], True),
            (Overlap("e.roles", "roles"), None, True),
            (Overlap("e.roles", "roles"), {"role": "analyst"}, False),
            (Excludes("e.client", "walls"), {"id": "x"}, False),
            (Equals("e.lob", "lob"), {"anything": True}, True),
            (Present("e.stamp"), {"anything": True}, True),
        ],
    )
    def test_what_each_rule_can_reach_a_verdict_on(self, rule, value, decidable):
        assert rule.accepts(value) is decidable

    def test_a_rank_of_true_is_refused(self):
        """Booleans are numbers in Python, so True would compare as level one
        and pass. Nobody means that."""
        assert AtMost("e.rank", "clearance").accepts(True) is False

    def test_an_empty_list_is_a_statement_not_a_mistake(self):
        """A document nobody may see is a thing somebody legitimately writes,
        the same reasoning that lets a null through."""
        assert Overlap("e.roles", "roles").accepts([]) is True

    # -- at the write ---------------------------------------------------

    def test_a_rank_written_as_a_word_is_refused(self, tmp_path):
        from vectrixdb.exceptions import MetadataContractError

        db = self._db(tmp_path)
        try:
            with pytest.raises(MetadataContractError, match="needs a number"):
                db.add(["a covenant"], metadata=[self._meta(classification_rank="high")])
        finally:
            db.close()

    def test_the_message_names_the_field_and_the_value(self, tmp_path):
        """So the person reading it can go straight to the pipeline that
        wrote it, which is the entire reason this check exists."""
        from vectrixdb.exceptions import MetadataContractError

        db = self._db(tmp_path)
        try:
            with pytest.raises(MetadataContractError) as info:
                db.add(["a covenant"], metadata=[self._meta(classification_rank="high")])

            message = str(info.value)
            assert "entitlements.classification_rank='high'" in message
            assert "pipeline bug" in message
        finally:
            db.close()

    def test_both_faults_are_reported_together(self, tmp_path):
        """A pipeline that dropped one field and mistyped another should hear
        about both, rather than fixing one and running again."""
        from vectrixdb.exceptions import MetadataContractError

        db = self._db(tmp_path)
        try:
            broken = {"entitlements": {"classification_rank": "high", "client_id": "CL-40219"}}
            with pytest.raises(MetadataContractError) as info:
                db.add(["a covenant"], metadata=[broken])

            assert "is missing ['entitlements.allowed_roles']" in str(info.value)
            assert "needs a number" in str(info.value)
        finally:
            db.close()

    def test_the_exception_carries_both_lists(self, tmp_path):
        from vectrixdb.exceptions import MetadataContractError

        db = self._db(tmp_path)
        try:
            with pytest.raises(MetadataContractError) as info:
                db.add(["a covenant"], metadata=[self._meta(classification_rank="high")])

            assert info.value.missing == []
            assert info.value.undecidable == [
                ("entitlements.classification_rank", "high", "a number")
            ]
        finally:
            db.close()

    def test_warn_writes_it_and_says_so(self, tmp_path):
        from vectrixdb.exceptions import MetadataContractWarning

        db = self._db(tmp_path, on_incomplete_document="warn")
        try:
            with pytest.warns(MetadataContractWarning, match="needs a number"):
                db.add(["a covenant"], metadata=[self._meta(classification_rank="high")])

            assert db._count_all() == 1
        finally:
            db.close()

    def test_allow_is_silent(self, tmp_path):
        import warnings as _warnings

        db = self._db(tmp_path, on_incomplete_document="allow")
        try:
            with _warnings.catch_warnings():
                _warnings.simplefilter("error")
                db.add(["a covenant"], metadata=[self._meta(classification_rank="high")])

            assert db._count_all() == 1
        finally:
            db.close()

    def test_a_good_document_is_untouched(self, tmp_path):
        db = self._db(tmp_path)
        try:
            db.add(["a covenant"], metadata=[self._meta()])

            assert db._count_all() == 1
        finally:
            db.close()

    def test_the_document_it_refuses_really_is_invisible(self, tmp_path):
        """The claim the check rests on, checked rather than asserted in a
        docstring: written anyway, nobody can see it."""
        db = self._db(tmp_path, on_incomplete_document="allow")
        try:
            db.add(["a covenant"], metadata=[self._meta(classification_rank="high")])
            principal = {
                "roles": ["credit_analyst"],
                "clearance_rank": 99,
                "wall_restrictions": [],
            }

            assert list(db.as_principal(principal).search("covenant", limit=10)) == []
        finally:
            db.close()


def test_sparse_search_withholds_what_the_policy_withholds(tmp_path):
    """sparse_search asked for a principal and then never applied the policy,
    so the principal was answered with documents withheld from them, and a
    withheld document could take a place within the limit."""
    import json

    from vectrixdb.core.database import VectrixDB
    from vectrixdb.policy import Equals, Policy

    db = VectrixDB(str(tmp_path))
    c = db.create_collection("t", dimension=4)
    c.add(
        ids=["secret", "open"],
        vectors=[[1, 0, 0, 0], [0, 1, 0, 0]],
        metadata=[{"lob": "x"}, {"lob": "y"}],
        sparse_vectors=[{1: 1.0}, {1: 0.5}],
    )
    c.set_meta("entitlement_policy", json.dumps(Policy([Equals("lob", "lob")]).to_dict()))
    c._policy_loaded = False
    assert [r.id for r in c.sparse_search({1: 1.0}, limit=1, principal={"lob": "y"}).results] == [
        "open"
    ]
    db.close()
