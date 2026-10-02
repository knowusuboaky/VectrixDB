"""Conversation memory: remember, recall, feedback, context.

Every test here pins something a plain vector store gets wrong for a chat:
turns are not content-addressed, recency matters, corrections supersede,
feedback reshapes ranking, and a context block respects one budget.
"""

from datetime import timedelta

import pytest

from vectrixdb import Vectrix
from vectrixdb._time import utcnow
from vectrixdb.memory import (
    K_FEEDBACK,
    K_KIND,
    K_ROLE,
    K_SCORING,
    K_SESSION,
    K_SUPERSEDED_BY,
    K_SUPERSEDES,
    K_TURN,
    OUTCOMES,
    ConversationMemory,
    feedback_score,
)


@pytest.fixture
def db(tmp_path) -> Vectrix:
    return Vectrix("chat", path=str(tmp_path))


class TestRemember:
    def test_turns_are_numbered_per_session(self, db):
        db.remember("hello", session="a")
        db.remember("hi there", session="a", role="assistant")
        db.remember("unrelated", session="b")
        turns = db.memory.turns("a")
        assert [t.metadata[K_TURN] for t in turns] == [0, 1]
        assert [t.metadata[K_ROLE] for t in turns] == ["user", "assistant"]
        assert db.memory.turns("b")[0].metadata[K_TURN] == 0

    def test_the_same_text_twice_is_two_turns(self, db):
        """Content ids would fold "ok" and "ok" into one document."""
        a = db.remember("ok", session="a")
        b = db.remember("ok", session="a")
        assert a != b
        assert len(db.memory.turns("a")) == 2

    def test_numbering_continues_after_reopen(self, tmp_path):
        first = Vectrix("chat", path=str(tmp_path))
        first.remember("one", session="a")
        first.remember("two", session="a")

        reopened = Vectrix("chat", path=str(tmp_path))
        reopened.remember("three", session="a")
        assert [t.metadata[K_TURN] for t in reopened.memory.turns("a")] == [0, 1, 2]

    def test_explicit_turn_is_kept(self, db):
        db.remember("late", session="a", turn=7)
        db.remember("after", session="a")
        assert [t.metadata[K_TURN] for t in db.memory.turns("a")] == [7, 8]

    def test_reserved_keys_are_refused(self, db):
        with pytest.raises(ValueError, match="_vx_"):
            db.remember("x", metadata={"_vx_kind": "turn"})

    def test_empty_text_is_refused(self, db):
        with pytest.raises(ValueError):
            db.remember("   ")

    def test_pinned_is_a_kind(self, db):
        mid = db.remember("Ships to Canada only", pinned=True)
        assert db.get(mid)[0].metadata[K_KIND] == "pinned"

    def test_it_is_still_a_plain_collection(self, db):
        db.remember("Marie Curie discovered radium", session="a")
        assert db.count() == 1
        assert db.search("radium").top.metadata[K_SESSION] == "a"


class TestRecall:
    def test_session_scopes_recall(self, db):
        db.remember("the meeting is on Tuesday", session="a")
        db.remember("the meeting is on Friday", session="b")
        got = db.recall("when is the meeting", session="a")
        assert len(got) == 1 and "Tuesday" in got.top.text

    def test_no_session_recalls_everything(self, db):
        db.remember("the meeting is on Tuesday", session="a")
        db.remember("the meeting is on Friday", session="b")
        assert len(db.recall("when is the meeting", score_gap=None)) == 2

    def test_newer_wins_a_tie(self, db):
        now = utcnow()
        old = db.remember("I prefer dark mode", session="a", timestamp=now - timedelta(days=60))
        new = db.remember("I prefer dark mode", session="a", timestamp=now)
        got = db.recall("theme preference", session="a", now=now, score_gap=None)
        assert got.ids == [new, old]
        assert got[0].metadata[K_SCORING]["recency"] > got[1].metadata[K_SCORING]["recency"]

    def test_relevance_still_beats_age(self, db):
        """A clearly better match from March still wins.

        On absolute cosine this failed: the bundled model scores an unrelated
        sentence around 0.76 and the right one around 0.85, so a 0.5 recency
        factor let the fresh sandwich beat the cat. Relevance is normalised
        across the candidates now, which is what makes this hold.
        """
        now = utcnow()
        db.remember("the cat is called Miso", session="a", timestamp=now - timedelta(days=90))
        db.remember("lunch was a sandwich", session="a", timestamp=now)
        assert "Miso" in db.recall("what is the cat's name", session="a", now=now).top.text

    def test_scoring_is_explained(self, db):
        db.remember("x marks the spot", session="a")
        scoring = db.recall("x", session="a").top.metadata[K_SCORING]
        assert set(scoring) == {"relevance", "relevance_norm", "recency", "feedback"}

    def test_budget_and_gap_apply(self, db):
        for i in range(6):
            db.remember(f"note number {i} about the project plan", session="a")
        got = db.recall("project plan", session="a", limit=6, score_gap=None, token_budget=15)
        assert got.truncated and len(got) < 6 and got.mode == "recall"


class TestFeedback:
    def test_dead_end_sinks_a_memory(self, db):
        now = utcnow()
        a = db.remember("password reset is under settings", session="a", timestamp=now)
        b = db.remember("password reset is under settings", session="a", timestamp=now)
        db.feedback(a, "dead_end", timestamp=now)
        assert db.recall("password reset", session="a", now=now, score_gap=None).ids == [b, a]

    def test_useful_lifts_a_memory(self, db):
        now = utcnow()
        a = db.remember("password reset is under settings", session="a", timestamp=now)
        b = db.remember("password reset is under settings", session="a", timestamp=now)
        db.feedback(b, "useful", timestamp=now)
        assert db.recall("password reset", session="a", now=now, score_gap=None).ids == [b, a]

    def test_feedback_decays(self):
        now = utcnow()
        fresh = {K_FEEDBACK: [{"outcome": "useful", "ts": now.isoformat()}]}
        stale = {K_FEEDBACK: [{"outcome": "useful", "ts": (now - timedelta(days=30)).isoformat()}]}
        assert feedback_score(fresh, now, 30.0) == pytest.approx(1.0)
        assert feedback_score(stale, now, 30.0) == pytest.approx(0.5)

    def test_correction_supersedes(self, db):
        old = db.remember("I prefer dark mode", session="a")
        new = db.feedback(old, "corrected", correction="I prefer light mode")
        assert new is not None

        assert db.get(old)[0].metadata[K_SUPERSEDED_BY] == new
        assert db.get(new)[0].metadata[K_SUPERSEDES] == old

        got = db.recall("theme", session="a", score_gap=None)
        assert got.ids == [new], "the superseded memory must drop out of recall"
        with_old = db.recall("theme", session="a", score_gap=None, include_superseded=True)
        assert set(with_old.ids) == {old, new}

    def test_a_corrected_pin_stays_pinned(self, db):
        old = db.remember("Ships to Canada only", pinned=True)
        new = db.feedback(old, "corrected", correction="Ships to Canada and the US")
        assert db.get(new)[0].metadata[K_KIND] == "pinned"
        assert [r.id for r in db.memory._pinned(None)] == [new]

    def test_history_accumulates(self, db):
        mid = db.remember("x", session="a")
        db.feedback(mid, "useful")
        db.feedback(mid, "dead_end")
        history = db.get(mid)[0].metadata[K_FEEDBACK]
        assert [h["outcome"] for h in history] == ["useful", "dead_end"]

    def test_bad_outcome_is_refused(self, db):
        mid = db.remember("x", session="a")
        with pytest.raises(ValueError, match=str(OUTCOMES[0])):
            db.feedback(mid, "great")

    def test_unknown_id_is_an_error(self, db):
        with pytest.raises(KeyError):
            db.feedback("nope", "useful")


class TestContext:
    @pytest.fixture
    def seeded(self, db):
        now = utcnow()
        db.remember("Customer is on the annual plan", pinned=True, timestamp=now)
        for i in range(8):
            db.remember(
                f"turn {i}: talking about the export button",
                session="a",
                role="user" if i % 2 == 0 else "assistant",
                timestamp=now + timedelta(seconds=i),
            )
        db.remember("The export button greys out when a filter is active", session="a", kind="fact")
        db.remember("Billing runs on the first of the month", session="a", kind="fact")
        return db

    def test_sections_come_in_order(self, seeded):
        block = seeded.context("why is export greyed out", session="a", recent_turns=3)
        text = block.text
        assert text.index("Pinned facts:") < text.index("Recent conversation:")
        assert text.index("Recent conversation:") < text.index("Relevant memories:")

    def test_recent_turns_are_the_latest_in_order(self, seeded):
        block = seeded.context("export", session="a", recent_turns=3)
        assert [r.metadata[K_TURN] for r in block.recent] == [5, 6, 7]
        assert [r.metadata[K_ROLE] for r in block.recent] == ["assistant", "user", "assistant"]

    def test_nothing_appears_twice(self, seeded):
        block = seeded.context("export button", session="a", recent_turns=4, limit=10)
        ids = [r.id for r in block.pinned + block.recent + block.retrieved]
        assert len(ids) == len(set(ids))

    def test_pinned_is_never_cut(self, seeded):
        block = seeded.context("export", session="a", token_budget=1)
        assert len(block.pinned) == 1
        assert block.truncated
        assert block.recent == [] and block.retrieved == []

    def test_budget_is_respected_when_it_can_be(self, seeded):
        block = seeded.context("export", session="a", token_budget=60, recent_turns=8)
        assert block.token_estimate <= 60
        assert block.truncated and block.cut_count > 0

    def test_no_session_means_no_recent_turns(self, seeded):
        block = seeded.context("export")
        assert block.recent == []
        assert block.pinned, "global pins still apply"

    def test_len_and_empty_text(self, db):
        block = db.context("anything", session="none")
        assert len(block) == 0 and block.text == ""


class TestRecallCost:
    def test_recall_never_runs_the_cross_encoder(self, db, monkeypatch):
        db.remember("The report is due Friday", session="s")
        seen = {}
        original = db.search

        def capture(query, **kwargs):
            seen.update(kwargs)
            return original(query, **kwargs)

        monkeypatch.setattr(db, "search", capture)
        db.recall("when is the report due", session="s")
        assert seen.get("rerank") is False, "recall re-scores itself; the reranker is wasted work"


class TestConfiguration:
    def test_recency_weight_is_validated(self, db):
        with pytest.raises(ValueError):
            ConversationMemory(db, recency_weight=1.5)

    def test_memory_is_built_once(self, db):
        assert db.memory is db.memory
