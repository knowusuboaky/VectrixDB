"""Memory lifecycle: time to live, forgetting, consolidation and contradiction.

Every behaviour here loses or rewrites information on purpose, so each test
checks both what went and what stayed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from vectrixdb import Vectrix
from vectrixdb._time import utcnow
from vectrixdb.exceptions import ConfigurationError
from vectrixdb.memory import (
    K_CONSOLIDATED_FROM,
    K_EXPIRES,
    K_SUPERSEDED_BY,
    K_SUPERSEDES,
    KIND_FACT,
    ConversationMemory,
)


@pytest.fixture
def db(tmp_path) -> Vectrix:
    return Vectrix("chat", path=str(tmp_path))


class TestTTL:
    def test_turns_expire_and_facts_do_not(self, db):
        db._memory = ConversationMemory(db, ttl_days={"turn": 30})
        old = utcnow() - timedelta(days=40)
        t = db.remember("we talked about the roof repair", session="s", timestamp=old)
        f = db.remember("The roof was repaired in June", session="s", kind="fact", timestamp=old)
        assert db._collection.get(t).metadata[K_EXPIRES]
        assert K_EXPIRES not in db._collection.get(f).metadata

        # Invisible to recall, turns and context before expire() runs.
        assert t not in {r.id for r in db.recall("roof", session="s")}
        assert f in {r.id for r in db.recall("roof", session="s")}
        assert db.memory.turns("s") == []
        assert db.count() == 2

        assert db.memory.expire() == 1
        assert db.count() == 1 and db._collection.get(f) is not None

    def test_per_memory_ttl_and_validation(self, db):
        fresh = db.remember("short lived", session="s", ttl_days=1)
        assert db._collection.get(fresh).metadata[K_EXPIRES]
        with pytest.raises(ValueError):
            db.remember("bad", session="s", ttl_days=0)
        with pytest.raises(ValueError):
            ConversationMemory(db, ttl_days={"nope": 1})

    def test_not_yet_expired_is_visible(self, db):
        db._memory = ConversationMemory(db, ttl_days={"turn": 30})
        t = db.remember("still fresh", session="s", timestamp=utcnow() - timedelta(days=10))
        assert t in {r.id for r in db.memory.turns("s")}
        assert db.memory.expire() == 0


class TestForget:
    def test_by_age_and_session(self, db):
        now = utcnow()
        old = db.remember("old turn", session="a", timestamp=now - timedelta(days=100))
        new = db.remember("new turn", session="a", timestamp=now - timedelta(days=1))
        other = db.remember("other session, old", session="b", timestamp=now - timedelta(days=100))
        assert db.forget(session="a", older_than=90) == 1
        ids = {p.id for p in db._collection.scroll(limit=100)[0]}
        assert old not in ids and new in ids and other in ids

    def test_by_ids_and_kinds(self, db):
        t = db.remember("a turn", session="a")
        f = db.remember("a fact", session="a", kind="fact")
        assert db.forget(ids=[t]) == 1
        assert db.forget(session="a", kinds=("fact",)) == 1
        assert db.count() == 0
        assert db.forget(ids=[]) == 0

    def test_superseded_only(self, db):
        old = db.remember("Customer wants PDF", session="a", kind="fact")
        db.feedback(old, "corrected", correction="Customer wants DOCX")
        assert db.forget(session="a", kinds=("fact",), superseded=True) == 1
        left = [r.text for r in db.recall("format", session="a")]
        assert left == ["Customer wants DOCX"]

    def test_bad_arguments(self, db):
        with pytest.raises(TypeError):
            db.forget(older_than="yesterday")
        with pytest.raises(ValueError):
            db.forget(kinds=("nope",))


class TestConsolidate:
    def test_old_turns_become_facts_and_are_dropped(self, db):
        base = utcnow() - timedelta(days=10)
        for i, line in enumerate(
            [
                "I'd like the quarterly report as a PDF",
                "Sure, PDF it is",
                "Actually make the charts blue",
                "Blue charts, noted",
                "Send it to finance too",
                "Will do",
                "Thanks",
                "You're welcome",
            ]
        ):
            db.remember(
                line,
                session="u",
                role="user" if i % 2 == 0 else "assistant",
                timestamp=base + timedelta(minutes=i),
            )

        seen = []

        def summarize(turns):
            seen.append(turns)
            assert all(set(t) == {"role", "text", "ts"} for t in turns)
            return [
                "The user wants the quarterly report as a PDF with blue charts",
                "The report also goes to finance",
                "",
            ]

        report = db.consolidate("u", summarize, keep_recent=2)
        assert len(seen) == 1 and len(seen[0]) == 6
        assert seen[0][0]["text"].startswith("I'd like") and seen[0][0]["role"] == "user"
        assert len(report.fact_ids) == 2 and report.turns_removed == 6
        assert [t.text for t in db.memory.turns("u")] == ["Thanks", "You're welcome"]
        fact = db._collection.get(report.fact_ids[0])
        assert fact.metadata["_vx_kind"] == KIND_FACT
        assert fact.metadata[K_CONSOLIDATED_FROM] == report.turn_ids
        assert fact.metadata["_vx_ts"] == (base + timedelta(minutes=5)).isoformat()
        assert db.count() == 4

        block = db.context("what format did they want", session="u", token_budget=500)
        assert any("PDF" in r.text for r in block.retrieved)
        # Numbering continues past the consolidated turns.
        db.remember("one more", session="u")
        assert db.memory.turns("u")[-1].metadata["_vx_turn"] == 8

    def test_batches_keep_recent_and_dry_run(self, db):
        for i in range(10):
            db.remember(f"turn {i}", session="u")
        calls = []

        def summarize(turns):
            calls.append(len(turns))
            return [f"summary of {len(turns)}"]

        report = db.consolidate("u", summarize, keep_recent=4, batch_turns=4, delete=False)
        assert calls == [4, 2]
        assert report.turns_removed == 0 and len(report.fact_ids) == 2
        assert len(db.memory.turns("u")) == 10

    def test_nothing_to_do(self, db):
        db.remember("only", session="u")
        report = db.consolidate("u", lambda turns: ["x"], keep_recent=6)
        assert len(report) == 0 and report.turns_removed == 0

    def test_older_than_and_pinned(self, db):
        now = utcnow()
        db.remember("ancient", session="u", timestamp=now - timedelta(days=60))
        db.remember("recent", session="u", timestamp=now - timedelta(days=1))
        report = db.consolidate(
            "u", lambda turns: ["An ancient fact"], keep_recent=0, older_than=30, pinned=True
        )
        assert report.turns_removed == 1
        assert [r.text for r in db.memory.turns("u")] == ["recent"]
        assert db.context("anything", session="u").pinned[0].text == "An ancient fact"


class TestContradiction:
    def test_judge_supersedes_contradicted_facts(self, db):
        asked = []

        def judge(new, candidates):
            asked.append((new, list(candidates)))
            # A DOCX preference contradicts a PDF preference; nothing else does.
            return ["DOCX" in new and "PDF" in c for c in candidates]

        db._memory = ConversationMemory(db, judge=judge)
        old = db.remember("The user wants the report as a PDF", session="u", kind="fact")
        unrelated = db.remember("The user is in Toronto", session="u", kind="fact")
        new = db.remember("The user wants the report as a DOCX", session="u", kind="fact")

        assert asked[-1][0].endswith("DOCX") and any("PDF" in c for c in asked[-1][1])
        assert db._collection.get(old).metadata[K_SUPERSEDED_BY] == new
        assert db._collection.get(new).metadata[K_SUPERSEDES] == old
        assert K_SUPERSEDED_BY not in db._collection.get(unrelated).metadata
        assert [r.id for r in db.recall("report format", session="u")] == [new] or new in {
            r.id for r in db.recall("report format", session="u")
        }
        assert old not in {r.id for r in db.recall("report format", session="u")}

    def test_turns_are_not_judged_and_check_can_be_forced_or_skipped(self, db):
        calls = []

        def judge(new, candidates):
            calls.append(new)
            return [False] * len(candidates)

        db._memory = ConversationMemory(db, judge=judge)
        db.remember("fact one", session="u", kind="fact")
        db.remember("just a turn", session="u")
        assert calls == ["fact one"] or calls == []  # no candidates on the first fact
        db.remember("fact two", session="u", kind="fact", check=False)
        db.remember("a checked turn", session="u", check=True)
        assert "a checked turn" in calls and "fact two" not in calls

    def test_check_without_a_judge_is_a_configuration_error(self, db):
        with pytest.raises(ConfigurationError):
            db.remember("x", session="u", kind="fact", check=True)

    def test_judge_must_answer_for_every_candidate(self, db):
        db._memory = ConversationMemory(db, judge=lambda new, cands: [False])
        db.remember("fact a", session="u", kind="fact")
        db.remember("fact b", session="u", kind="fact")  # one candidate, one verdict
        with pytest.raises(ValueError, match="verdicts"):
            db.remember("fact c", session="u", kind="fact")  # two candidates, one verdict
