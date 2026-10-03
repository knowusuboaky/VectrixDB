"""The test kit, tested.

Its whole value is in what a failure says, so most of these assert on the
message rather than on the raise. A kit that says "assert False" is a kit
nobody reaches for twice.
"""

from __future__ import annotations

import pytest

from vectrixdb.policy import AtMost, Excludes, Overlap, Policy
from vectrixdb.testing import (
    assert_allows,
    assert_denies,
    assert_indistinguishable,
    assert_visibility,
    explain,
)

LENDING = Policy(
    [
        Overlap("entitlements.allowed_roles", "roles"),
        AtMost("entitlements.classification_rank", "clearance_rank"),
        Overlap("entitlements.client_id", "client_coverage", scope=True),
        Excludes("entitlements.client_id", "wall_restrictions", scope=True),
    ]
)


def chunk(client="CL-40219", rank=2):
    return {
        "entitlements": {
            "allowed_roles": ["credit_analyst"],
            "classification_rank": rank,
            "client_id": client,
        }
    }


ON_TEAM = {
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


class TestExplain:
    def test_it_marks_the_rule_that_decided(self):
        text = explain(LENDING, ON_TEAM, chunk(rank=5))
        assert "WITHHELD, in scope" in text
        assert "[x] AtMost(entitlements.classification_rank <- clearance_rank)" in text

    def test_it_shows_both_sides_of_the_comparison(self):
        """The useful output. A decision that is wrong is usually wrong in
        one rule, and knowing which is half of it; knowing what the two sides
        actually held is the other half."""
        text = explain(LENDING, ON_TEAM, chunk(rank=5))
        assert "document entitlements.classification_rank=5" in text
        assert "principal clearance_rank=3" in text

    def test_an_absent_field_says_absent_rather_than_none(self):
        """None is a value the policy can decide on. Absent is not, and
        conflating them in the output would hide the difference the whole
        filter engine is careful about."""
        text = explain(LENDING, ON_TEAM, {"entitlements": {}})
        assert "<absent>" in text
        assert "=None" not in text


class TestSingleDocument:
    def test_allows_passes_quietly(self):
        assert_allows(LENDING, ON_TEAM, chunk())

    def test_allows_explains_when_it_fails(self):
        with pytest.raises(AssertionError, match=r"\[x\] AtMost"):
            assert_allows(LENDING, ON_TEAM, chunk(rank=5))

    def test_denies_passes_quietly(self):
        assert_denies(LENDING, WALLED, chunk())

    def test_denies_fails_when_the_document_is_visible(self):
        with pytest.raises(AssertionError, match="expected this document to be withheld"):
            assert_denies(LENDING, ON_TEAM, chunk())

    def test_the_kind_of_withholding_can_be_asserted(self):
        assert_denies(LENDING, ON_TEAM, chunk(rank=5), disclosable=True)
        assert_denies(LENDING, WALLED, chunk(), disclosable=False)

    def test_the_wrong_kind_is_a_failure(self):
        """Getting these two the wrong way round is how an ethical wall fails
        while every test still passes."""
        with pytest.raises(AssertionError, match="expected in scope, so disclosable"):
            assert_denies(LENDING, WALLED, chunk(), disclosable=True)

    def test_a_custom_message_survives(self):
        with pytest.raises(AssertionError, match="the deal team should see this"):
            assert_allows(LENDING, ON_TEAM, chunk(rank=5), "the deal team should see this")


class TestVisibilityTable:
    PRINCIPALS = {"on the deal team": ON_TEAM, "walled off": WALLED}
    DOCUMENTS = {"covenant": chunk(), "memo": chunk(rank=5), "other": chunk("CL-51330")}

    def test_a_matching_table_passes(self):
        assert_visibility(
            LENDING,
            self.PRINCIPALS,
            self.DOCUMENTS,
            {"on the deal team": {"covenant"}, "walled off": {"other"}},
        )

    def test_it_names_what_should_have_been_withheld(self):
        with pytest.raises(AssertionError, match=r"\+ covenant should have been withheld"):
            assert_visibility(
                LENDING,
                self.PRINCIPALS,
                self.DOCUMENTS,
                {"on the deal team": set(), "walled off": {"other"}},
            )

    def test_it_names_what_should_have_been_visible(self):
        with pytest.raises(AssertionError, match=r"- memo should have been visible"):
            assert_visibility(
                LENDING,
                self.PRINCIPALS,
                self.DOCUMENTS,
                {"on the deal team": {"covenant", "memo"}, "walled off": {"other"}},
            )

    def test_it_prints_the_whole_table_not_just_the_failure(self):
        """A reviewer reads the table, not the diff."""
        with pytest.raises(AssertionError) as info:
            assert_visibility(
                LENDING,
                self.PRINCIPALS,
                self.DOCUMENTS,
                {"on the deal team": set(), "walled off": {"other"}},
            )
        message = str(info.value)
        assert "on the deal team" in message
        assert "walled off" in message

    def test_a_principal_with_no_row_is_refused(self):
        """Otherwise a policy change could widen access and still pass,
        because nobody had said what that principal ought to see."""
        with pytest.raises(AssertionError, match="no expectation given"):
            assert_visibility(
                LENDING, self.PRINCIPALS, self.DOCUMENTS, {"on the deal team": {"covenant"}}
            )

    def test_an_expectation_naming_an_unknown_document_is_refused(self):
        with pytest.raises(AssertionError, match="not given"):
            assert_visibility(
                LENDING,
                self.PRINCIPALS,
                self.DOCUMENTS,
                {"on the deal team": {"typo"}, "walled off": set()},
            )


class TestIndistinguishable:
    """The ethical wall assertion, and the one nothing else can check."""

    def _results(self, texts, **fields):
        from vectrixdb.easy import Result, Results

        return Results(
            items=[Result(id=t, text=t, score=1.0, metadata={}) for t in texts],
            query="q",
            mode="dense",
            time_ms=0.0,
            **fields,
        )

    def test_two_empty_results_are_indistinguishable(self):
        assert_indistinguishable(lambda: self._results([]), lambda: self._results([]))

    def test_different_lengths_are_not(self):
        with pytest.raises(AssertionError, match="1 results against 0"):
            assert_indistinguishable(lambda: self._results(["a"]), lambda: self._results([]))

    def test_different_texts_are_not(self):
        with pytest.raises(AssertionError, match="the texts differ"):
            assert_indistinguishable(lambda: self._results(["a"]), lambda: self._results(["b"]))

    def test_a_difference_in_truncation_is_not(self):
        """Everything a caller can observe counts, not just the documents."""
        with pytest.raises(AssertionError, match="cut_count"):
            assert_indistinguishable(
                lambda: self._results([], cut_count=3),
                lambda: self._results([], cut_count=0),
            )

    def test_one_raising_and_one_not_is_a_difference(self):
        """The one people miss. A catchable exception is itself an answer,
        which is why there is no AccessDenied anywhere in this library."""

        def raises():
            raise RuntimeError("denied")

        with pytest.raises(AssertionError, match="is itself an answer"):
            assert_indistinguishable(raises, lambda: self._results([]))

    def test_both_raising_the_same_thing_is_not_a_difference(self):
        def raises():
            raise RuntimeError("nothing found")

        assert_indistinguishable(raises, raises)

    def test_a_score_that_moves_is_a_difference(self):
        """Because a sparse score is a corpus statistic. Inverse document
        frequency counts every document in the index, the withheld ones
        included, so the number beside a visible document moves with what the
        caller was refused."""
        left = self._results(["a"])
        right = self._results(["a"])
        right.items[0].score = 0.5

        with pytest.raises(AssertionError, match="corpus statistic"):
            assert_indistinguishable(lambda: left, lambda: right)

    def test_embedding_noise_is_below_the_tolerance(self):
        """Why it is a tolerance and not an equality.

        The same text embedded beside others lands up to 0.015 from the
        vector it gets alone, which moved a cosine score by as much as 2
        percent over the texts the default was measured on. A test that
        failed on that would be failing on noise, and a noisy security test
        is one somebody turns off.
        """
        left = self._results(["a"])
        right = self._results(["a"])
        right.items[0].score = left.items[0].score * 0.98

        assert_indistinguishable(lambda: left, lambda: right)

    def test_the_check_can_be_turned_off(self):
        left = self._results(["a"])
        right = self._results(["a"])
        right.items[0].score = 0.5

        assert_indistinguishable(lambda: left, lambda: right, score_tolerance=None)

    def test_raising_different_exceptions_is_a_difference(self):
        def one():
            raise RuntimeError("x")

        def other():
            raise ValueError("y")

        with pytest.raises(AssertionError, match="different exceptions"):
            assert_indistinguishable(one, other)


class TestAgainstRealCollections:
    """The form a user would actually write: two collections identical except
    that one of them has the walled client in it."""

    ROWS = [
        ("Facility covenant ratio 1.15x", "CL-40219", 2),
        ("Internal credit memo", "CL-40219", 5),
    ]
    OWN = [("Facility covenant for the other borrower", "CL-51330", 2)]

    def _collection(self, path, rows):
        from vectrixdb import Vectrix

        db = Vectrix("lending", path=str(path), policy=LENDING)
        db.add(
            [text for text, _, _ in rows],
            metadata=[chunk(client, rank) for _, client, rank in rows],
        )
        return db

    def test_a_wall_is_indistinguishable_from_a_client_that_never_existed(self, tmp_path):
        walled = self._collection(tmp_path / "walled", self.ROWS + self.OWN)
        absent = self._collection(tmp_path / "absent", self.OWN)
        try:
            assert_indistinguishable(
                lambda: walled.as_principal(WALLED).search("covenant thresholds", limit=10),
                lambda: absent.as_principal(WALLED).search("covenant thresholds", limit=10),
                "a walled client and one that was never onboarded",
            )
        finally:
            walled.close()
            absent.close()

    def test_the_assertion_catches_a_wall_that_is_not_working(self, tmp_path):
        """The check earning its keep: a policy missing the wall rule looks
        fine on every other test and fails this one."""
        from vectrixdb import Vectrix

        leaky = Policy(
            [
                Overlap("entitlements.allowed_roles", "roles"),
                AtMost("entitlements.classification_rank", "clearance_rank"),
            ]
        )
        with_client = Vectrix("leaky", path=str(tmp_path / "a"), policy=leaky)
        without = Vectrix("leaky", path=str(tmp_path / "b"), policy=leaky)
        try:
            for db, rows in ((with_client, self.ROWS + self.OWN), (without, self.OWN)):
                db.add(
                    [text for text, _, _ in rows],
                    metadata=[chunk(c, r) for _, c, r in rows],
                )

            with pytest.raises(AssertionError):
                assert_indistinguishable(
                    lambda: with_client.as_principal(WALLED).search("covenant", limit=10),
                    lambda: without.as_principal(WALLED).search("covenant", limit=10),
                )
        finally:
            with_client.close()
            without.close()
