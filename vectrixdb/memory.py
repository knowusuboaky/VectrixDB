"""Conversation memory on top of a ``Vectrix`` collection.

A vector store remembers everything as equally true forever, which is the
wrong shape for a conversation. Three things change when the documents are
turns of a chat:

* **Recency matters.** What the user said ten minutes ago usually beats what
  they said in March, so relevance is scaled by a half-life decay. It is
  blended rather than multiplied outright, so an old memory that is far more
  relevant still wins.

* **Facts get corrected.** "I prefer dark mode" is superseded by "actually,
  light mode". A correction is stored as a new memory that points back at the
  old one, and the old one drops out of recall. Nothing is deleted, so the
  history stays auditable.

* **Recall gets graded.** Whether a retrieved memory helped is a signal the
  caller has and the store does not. ``feedback()`` records it and a signed,
  time-decayed score folds into later ranking. This is Graphify's
  ``reflect.py`` idea (``useful`` / ``dead_end`` / ``corrected`` with a
  half-life) applied to prose.

* **Memory is finite.** Turns expire on a time-to-live per kind, a
  ``forget()`` deletes on demand, and ``consolidate()`` turns a run of old
  turns into standalone facts and drops the turns. That last one is the
  real long-term token saver: a month of chat becomes a dozen sentences.

* **Facts conflict.** A ``judge`` callable, given a new fact and the stored
  facts nearest to it, says which of them it contradicts; those are marked
  superseded on write, the same link ``feedback("corrected")`` makes.

Consolidation and contradiction detection take any LLM callable and bundle
no model: the library does the bookkeeping, you bring the judgement.

Everything is stored as ordinary documents with reserved ``_vx_*`` metadata
keys, so a memory collection is still a plain collection: ``search()``,
``delete()`` and filters all work on it unchanged.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Sequence

from ._ranking import apply_score_gap, fit_to_budget
from ._time import ensure_aware, parse_iso, utcnow
from ._tokens import estimate_tokens
from .exceptions import ConfigurationError


__all__ = [
    "K_KIND",
    "K_SESSION",
    "K_ROLE",
    "K_TURN",
    "K_TS",
    "K_FEEDBACK",
    "K_SUPERSEDED_BY",
    "K_SUPERSEDES",
    "K_SCORING",
    "K_EXPIRES",
    "K_CONSOLIDATED_FROM",
    "KIND_TURN",
    "KIND_FACT",
    "KIND_PINNED",
    "KINDS",
    "OUTCOMES",
    "feedback_score",
    "ContextBlock",
    "ConsolidationReport",
    "ConversationMemory",
]

if TYPE_CHECKING:  # pragma: no cover
    from .core.collection import Collection
    from .easy import Result, Results, SearchMode, Vectrix


# ============================================================================
# SETTINGS: the metadata keys, the kinds, and the outcomes
# ============================================================================
#
# The keys a memory carries in its metadata, the three kinds of memory, and
# the outcomes feedback may record.

#: Reserved metadata keys. Prefixed so they cannot collide with a caller's own.
K_KIND = "_vx_kind"
K_SESSION = "_vx_session"
K_ROLE = "_vx_role"
K_TURN = "_vx_turn"
K_TS = "_vx_ts"
K_FEEDBACK = "_vx_feedback"
K_SUPERSEDED_BY = "_vx_superseded_by"
K_SUPERSEDES = "_vx_supersedes"
K_SCORING = "_vx_scoring"
K_EXPIRES = "_vx_expires"
K_CONSOLIDATED_FROM = "_vx_consolidated_from"

KIND_TURN = "turn"
KIND_FACT = "fact"
KIND_PINNED = "pinned"
KINDS = (KIND_TURN, KIND_FACT, KIND_PINNED)

#: What a caller may say about a recalled memory, and how each counts.
OUTCOMES = ("useful", "dead_end", "corrected")
_OUTCOME_SIGN = {"useful": 1.0, "dead_end": -1.0, "corrected": -1.0}


# ============================================================================
# IDS, AGE, EXPIRY, DECAY, AND FEEDBACK
# ============================================================================
#
# INPUT   a memory's metadata, and now
# OUTPUT  an id from the session, turn, time and text, since ok said twice is
#         two turns; its age in days; whether it has expired; a weight that
#         halves every half-life; the signed, time-decayed sum of every
#         outcome recorded on it
#
# Content ids are wrong for turns, so a memory's id is made from when and
# where it was said.


def _memory_id(session: Optional[str], turn: Optional[int], ts: datetime, text: str) -> str:
    """Content ids are wrong for turns: "ok" said twice is two turns."""
    raw = f"{session}|{turn}|{ts.isoformat()}|{text}"
    return "mem_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _age_days(meta: Dict[str, Any], now: datetime) -> Optional[float]:
    ts = parse_iso(meta.get(K_TS)) if meta.get(K_TS) else None
    if ts is None:
        return None
    return max(0.0, (now - ensure_aware(ts)).total_seconds() / 86400.0)


def _expired(meta: Dict[str, Any], now: datetime) -> bool:
    """True when the memory carries an expiry that has passed."""
    raw = meta.get(K_EXPIRES)
    if not raw:
        return False
    when = parse_iso(raw)
    return when is not None and ensure_aware(when) <= now


def _decay(age_days: Optional[float], half_life_days: float) -> float:
    """Weight in (0, 1] that halves every ``half_life_days``; undated is 1."""
    if age_days is None or half_life_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def feedback_score(meta: Dict[str, Any], now: datetime, half_life_days: float) -> float:
    """Signed, time-decayed sum of every outcome recorded on a memory."""
    total = 0.0
    for entry in meta.get(K_FEEDBACK) or ():
        sign = _OUTCOME_SIGN.get(entry.get("outcome"))
        if sign is None:
            continue
        age = None
        if entry.get("ts"):
            ts = parse_iso(entry["ts"])
            if ts is not None:
                age = max(0.0, (now - ensure_aware(ts)).total_seconds() / 86400.0)
        total += sign * _decay(age, half_life_days)
    return total


# ============================================================================
# THE CONTEXT BLOCK, THE REPORT, AND THE MEMORY
# ============================================================================
#
# INPUT   a query, a session and a token budget; text to remember; feedback;
#         what to forget
# OUTPUT  a ready-to-inject slice of memory under one budget; what
#         consolidate() did; remember, recall, grade, forget, consolidate and
#         assemble, over a Vectrix collection
#
# A vector store remembers everything as equally true forever, which is the
# wrong shape for a conversation: memories decay, feedback moves them, and a
# turn can be superseded.


@dataclass
class ContextBlock:
    """A ready-to-inject slice of memory, assembled under one token budget.

    Order is deliberate: pinned facts first because they always apply, then
    the most recent turns so the model has the thread, then whatever else the
    query pulled in with the tokens that remain.
    """

    query: str
    session: Optional[str]
    pinned: List["Result"] = field(default_factory=list)
    recent: List["Result"] = field(default_factory=list)
    retrieved: List["Result"] = field(default_factory=list)
    token_budget: Optional[int] = None
    token_estimate: int = 0
    truncated: bool = False
    cut_count: int = 0

    @property
    def text(self) -> str:
        """The block as plain text, ready to prepend to a prompt."""
        parts: List[str] = []
        if self.pinned:
            parts.append("Pinned facts:\n" + "\n".join(f"- {r.text}" for r in self.pinned))
        if self.recent:
            lines = []
            for r in self.recent:
                role = r.metadata.get(K_ROLE) or "user"
                lines.append(f"{role}: {r.text}")
            parts.append("Recent conversation:\n" + "\n".join(lines))
        if self.retrieved:
            parts.append("Relevant memories:\n" + "\n".join(f"- {r.text}" for r in self.retrieved))
        return "\n\n".join(parts)

    def __len__(self) -> int:
        return len(self.pinned) + len(self.recent) + len(self.retrieved)


@dataclass
class ConsolidationReport:
    """What ``consolidate()`` did."""

    session: Optional[str]
    turn_ids: List[str] = field(default_factory=list)
    fact_ids: List[str] = field(default_factory=list)
    turns_removed: int = 0

    def __len__(self) -> int:
        return len(self.fact_ids)


class ConversationMemory:
    """Remember, recall, grade, forget, consolidate and assemble memory.

    Reached as ``db.memory``; ``db.remember()``, ``db.recall()``,
    ``db.feedback()``, ``db.context()``, ``db.forget()`` and
    ``db.consolidate()`` delegate here.
    """

    def __init__(
        self,
        db: "Vectrix",
        half_life_days: float = 7.0,
        recency_weight: float = 0.5,
        feedback_weight: float = 0.25,
        feedback_half_life_days: float = 30.0,
        ttl_days: Optional[Dict[str, Optional[float]]] = None,
        judge: Optional[Callable[[str, List[str]], Sequence[bool]]] = None,
        judge_candidates: int = 5,
    ) -> None:
        """
        Args:
            db: The collection memories live in.
            ttl_days: Time to live per kind, e.g. ``{"turn": 30}``. A memory
                past its TTL is invisible to recall and context at once and
                is deleted by ``expire()``. Missing or None means forever;
                the default keeps everything.
            judge: Contradiction detector for facts. Called on every
                ``remember()`` of a fact or pinned fact as
                ``judge(new_text, [nearest stored fact texts])`` and must
                return one bool per candidate, True where the old fact is
                contradicted by the new one. Contradicted facts are marked
                superseded by the new memory. Any LLM call fits; nothing is
                bundled.
            judge_candidates: How many nearest facts the judge sees.
            half_life_days: Relevance of a memory halves every this many days.
                Seven suits a chat product; a knowledge base wants longer.
            recency_weight: How much a brand-new memory gains over a very old
                one, on the same 0-to-1 scale relevance is measured on. At 0.5
                a memory must be clearly more relevant, not slightly, to beat
                a fresher one; ties go to the newer.
            feedback_weight: How much one unit of feedback moves the result on
                that scale. Bounded to plus or minus two units, so a memory
                graded useful many times cannot outrank a plainly better match.
            feedback_half_life_days: Feedback ages too. A dead end reported
                last month should not still bury a memory today.
        """
        if not 0 <= recency_weight <= 1:
            raise ValueError(f"recency_weight must be in [0, 1], got {recency_weight!r}")
        self._db = db
        self.half_life_days = half_life_days
        self.recency_weight = recency_weight
        self.feedback_weight = feedback_weight
        self.feedback_half_life_days = feedback_half_life_days
        self.ttl_days: Dict[str, Optional[float]] = dict(ttl_days or {})
        for kind in self.ttl_days:
            if kind not in KINDS:
                raise ValueError(f"ttl_days keys must be one of {KINDS}, got {kind!r}")
        self.judge = judge
        self.judge_candidates = judge_candidates
        self._next_turns: Dict[Optional[str], int] = {}

    # ------------------------------------------------------------------ write

    def remember(
        self,
        text: str,
        session: Optional[str] = None,
        role: str = "user",
        turn: Optional[int] = None,
        timestamp: Optional[datetime] = None,
        pinned: bool = False,
        kind: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        id: Optional[str] = None,
        ttl_days: Optional[float] = None,
        check: Optional[bool] = None,
    ) -> str:
        """Store one memory and return its id.

        A ``turn`` is numbered automatically within its session when not
        given. ``pinned=True`` stores a fact that ``context()`` always
        includes; ``kind="fact"`` stores a standalone fact that is recalled
        by relevance but not pinned.

        ``ttl_days`` overrides the per-kind TTL for this memory. ``check``
        runs the contradiction ``judge`` (default: when one is configured and
        the memory is a fact or pinned); ``check=False`` skips it for a
        memory you know is new.
        """
        if not text or not text.strip():
            raise ValueError("A memory needs some text")
        kind = KIND_PINNED if pinned else (kind or KIND_TURN)
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
        if metadata and any(k.startswith("_vx_") for k in metadata):
            raise ValueError("Metadata keys starting with '_vx_' are reserved")

        ts = ensure_aware(timestamp) if timestamp is not None else utcnow()
        if turn is None and kind == KIND_TURN:
            turn = self._claim_turn(session)
        elif turn is not None and kind == KIND_TURN:
            self._next_turns[session] = max(self._next_turns.get(session, 0), turn + 1)

        meta: Dict[str, Any] = dict(metadata or {})
        meta[K_KIND] = kind
        meta[K_SESSION] = session
        meta[K_ROLE] = role
        meta[K_TURN] = turn
        meta[K_TS] = ts.isoformat()
        ttl = ttl_days if ttl_days is not None else self.ttl_days.get(kind)
        if ttl is not None:
            if ttl <= 0:
                raise ValueError("ttl_days must be positive")
            meta[K_EXPIRES] = (ts + timedelta(days=float(ttl))).isoformat()

        memory_id = id or _memory_id(session, turn, ts, text)

        contradicted: List[str] = []
        run_judge = check if check is not None else (self.judge is not None and kind != KIND_TURN)
        if run_judge:
            if self.judge is None:
                raise ConfigurationError("check=True needs a judge on the memory")
            contradicted = self._contradicted(text, session, kind)
        if contradicted:
            meta[K_SUPERSEDES] = contradicted if len(contradicted) > 1 else contradicted[0]

        self._db.add([text], metadata=[meta], ids=[memory_id])
        if contradicted:
            collection = self._collection()
            for old_id in contradicted:
                collection.update_metadata(old_id, {K_SUPERSEDED_BY: memory_id}, merge=True)
        return memory_id

    def _contradicted(self, text: str, session: Optional[str], kind: str) -> List[str]:
        """Ids of live stored facts the judge says ``text`` contradicts."""
        assert self.judge is not None
        candidates = self.recall(
            text,
            session=session,
            limit=self.judge_candidates,
            kinds=(KIND_FACT, KIND_PINNED),
        )
        pool = [r for r in candidates if r.text and r.text.strip() != text.strip()]
        if not pool:
            return []
        verdicts = list(self.judge(text, [r.text for r in pool]))
        if len(verdicts) != len(pool):
            raise ValueError(f"judge returned {len(verdicts)} verdicts for {len(pool)} candidates")
        return [r.id for r, hit in zip(pool, verdicts) if hit]

    def feedback(
        self,
        id: str,
        outcome: str,
        correction: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> Optional[str]:
        """Record how a recalled memory turned out.

        ``useful`` lifts it in later recall, ``dead_end`` lowers it, and
        ``corrected`` lowers it and, when ``correction`` text is given, stores
        the corrected fact as a new memory that supersedes this one. Returns
        the new memory's id in that case, otherwise ``None``.
        """
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
        collection = self._collection()
        point = collection.get(id)
        if point is None:
            raise KeyError(f"No memory with id {id!r}")

        ts = ensure_aware(timestamp) if timestamp is not None else utcnow()
        meta = dict(point.metadata or {})
        history = list(meta.get(K_FEEDBACK) or [])
        entry: Dict[str, Any] = {"outcome": outcome, "ts": ts.isoformat()}
        if correction:
            entry["correction"] = correction
        history.append(entry)
        update: Dict[str, Any] = {K_FEEDBACK: history}

        new_id: Optional[str] = None
        if correction:
            new_id = self.remember(
                correction,
                session=meta.get(K_SESSION),
                role=meta.get(K_ROLE) or "user",
                timestamp=ts,
                kind=KIND_PINNED if meta.get(K_KIND) == KIND_PINNED else KIND_FACT,
                metadata={},
            )
            # The link runs both ways: the old memory knows it was replaced,
            # the new one knows what it replaced. Written after remember() so
            # a failed insert leaves the old memory live.
            collection.update_metadata(new_id, {K_SUPERSEDES: id}, merge=True)
            update[K_SUPERSEDED_BY] = new_id

        collection.update_metadata(id, update, merge=True)
        return new_id

    # ---------------------------------------------------------------- forget

    def forget(
        self,
        session: Optional[str] = None,
        kinds: Sequence[str] = (KIND_TURN,),
        older_than: Optional[Any] = None,
        ids: Optional[Iterable[str]] = None,
        superseded: bool = False,
        now: Optional[datetime] = None,
    ) -> int:
        """Delete memories and return how many went.

        ``ids`` names them outright. Otherwise the selection is the ``kinds``
        in ``session`` (None means every session) older than ``older_than``,
        a number of days or a datetime; ``superseded=True`` selects only
        memories that a correction or a contradiction has already replaced,
        which is the audit trail you may keep or clear. Deleting is real:
        this is the one memory operation that loses information on purpose.
        """
        if ids is not None:
            targets = list(ids)
            if targets:
                self._db.delete(targets)
            return len(targets)

        now = ensure_aware(now) if now is not None else utcnow()
        cutoff: Optional[datetime] = None
        if isinstance(older_than, (int, float)):
            cutoff = now - timedelta(days=float(older_than))
        elif isinstance(older_than, datetime):
            cutoff = ensure_aware(older_than)
        elif older_than is not None:
            raise TypeError("older_than is a number of days or a datetime")

        targets = []
        for kind in kinds:
            if kind not in KINDS:
                raise ValueError(f"kinds must be from {KINDS}, got {kind!r}")
            filt: Dict[str, Any] = {K_KIND: kind}
            if session is not None:
                filt[K_SESSION] = session
            for p in self._scan(filt):
                meta = p.metadata or {}
                if superseded and not meta.get(K_SUPERSEDED_BY):
                    continue
                if cutoff is not None:
                    ts = parse_iso(meta.get(K_TS)) if meta.get(K_TS) else None
                    if ts is None or ensure_aware(ts) >= cutoff:
                        continue
                targets.append(p.id)
        if targets:
            self._db.delete(targets)
        return len(targets)

    def expire(self, now: Optional[datetime] = None) -> int:
        """Delete every memory past its time to live. Returns the count.

        Recall and context already skip expired memories, so this is about
        space and the audit trail, not correctness; run it whenever suits.
        """
        now = ensure_aware(now) if now is not None else utcnow()
        targets = [p.id for p in self._scan({}) if _expired(p.metadata or {}, now)]
        if targets:
            self._db.delete(targets)
        return len(targets)

    def consolidate(
        self,
        session: Optional[str],
        summarize: Callable[[List[Dict[str, str]]], Sequence[str]],
        keep_recent: int = 6,
        older_than: Optional[Any] = None,
        batch_turns: int = 40,
        delete: bool = True,
        pinned: bool = False,
        now: Optional[datetime] = None,
    ) -> ConsolidationReport:
        """Turn old turns into standalone facts and drop the turns.

        ``summarize`` receives a list of ``{"role", "text", "ts"}`` dicts,
        oldest first, and returns the facts worth keeping as separate
        strings, each one complete on its own ("The user prefers PDF
        reports", not "they said PDF"). Any LLM call fits. It is called once
        per ``batch_turns`` turns so a long history never becomes one prompt.

        The last ``keep_recent`` turns stay, so ``context()`` keeps its
        thread; ``older_than`` (days or a datetime) narrows further. Facts are
        stored with ``kind="fact"`` (``pinned=True`` to pin them), stamped
        with the turn ids they came from, and dated at the newest turn they
        summarise, so recency keeps meaning something. Turns are deleted only
        after every fact of their batch is stored; ``delete=False`` keeps
        them for a dry run.
        """
        now = ensure_aware(now) if now is not None else utcnow()
        turns = self.turns(session)
        if keep_recent > 0:
            turns = turns[:-keep_recent] if len(turns) > keep_recent else []
        if older_than is not None:
            cutoff = (
                now - timedelta(days=float(older_than))
                if isinstance(older_than, (int, float))
                else ensure_aware(older_than)
            )
            turns = [
                t
                for t in turns
                if (ts := parse_iso(t.metadata.get(K_TS) or "")) is not None
                and ensure_aware(ts) < cutoff
            ]
        report = ConsolidationReport(session=session)
        if not turns:
            return report

        for start in range(0, len(turns), max(1, batch_turns)):
            batch = turns[start : start + max(1, batch_turns)]
            payload = [
                {
                    "role": t.metadata.get(K_ROLE) or "user",
                    "text": t.text,
                    "ts": t.metadata.get(K_TS) or "",
                }
                for t in batch
            ]
            facts = [f.strip() for f in summarize(payload) if f and f.strip()]
            newest = max((t.metadata.get(K_TS) or "" for t in batch), default="")
            when = parse_iso(newest) if newest else None
            batch_ids = [t.id for t in batch]
            for fact in facts:
                fact_id = self.remember(
                    fact,
                    session=session,
                    role="summary",
                    timestamp=ensure_aware(when) if when else now,
                    kind=KIND_PINNED if pinned else KIND_FACT,
                    metadata={},
                    check=False,
                )
                self._collection().update_metadata(
                    fact_id, {K_CONSOLIDATED_FROM: batch_ids}, merge=True
                )
                report.fact_ids.append(fact_id)
            report.turn_ids.extend(batch_ids)
            if delete:
                self._db.delete(batch_ids)
                report.turns_removed += len(batch_ids)
        # Turn numbering keeps counting from where it was: a consolidated
        # session must not reuse numbers that its facts point back at, and
        # _next_turns already holds the high-water mark.
        return report

    # ------------------------------------------------------------------- read

    def turns(self, session: Optional[str], limit: Optional[int] = None) -> List["Result"]:
        """A session's turns in order; the last ``limit`` of them if given."""
        from .easy import Result

        now = utcnow()
        points = [
            p
            for p in self._scan({K_SESSION: session, K_KIND: KIND_TURN})
            if not _expired(p.metadata or {}, now)
        ]
        points.sort(key=lambda p: (p.metadata.get(K_TURN) or 0, p.metadata.get(K_TS) or ""))
        if limit is not None:
            points = points[-limit:] if limit > 0 else []
        return [
            Result(id=p.id, text=p.text or "", score=1.0, metadata=dict(p.metadata or {}))
            for p in points
        ]

    def recall(
        self,
        query: str,
        session: Optional[str] = None,
        limit: int = 10,
        token_budget: Optional[int] = None,
        score_gap: Optional[float] = None,
        kinds: Sequence[str] = KINDS,
        include_superseded: bool = False,
        mode: Optional["SearchMode"] = None,
        now: Optional[datetime] = None,
        exclude_ids: Iterable[str] = (),
    ) -> "Results":
        """Relevance, blended with recency and feedback, cut to a budget.

        ``session=None`` recalls across every session.

        Relevance is normalised across the candidates before blending. The
        bundled embedding models put every cosine score in a narrow band, so
        an unrelated sentence can sit at 0.76 while the right one sits at
        0.85: on absolute scores a multiplicative recency factor let a fresh
        but irrelevant turn beat a relevant old one. Measured against the
        spread the candidates actually have, "clearly more relevant" means
        what it says regardless of model.
        """
        from .easy import Results

        now = ensure_aware(now) if now is not None else utcnow()
        filt: Dict[str, Any] = {}
        if session is not None:
            filt[K_SESSION] = session
        if len(kinds) == 1:
            filt[K_KIND] = kinds[0]
        skip = set(exclude_ids)

        # Over-fetch so the rescoring has something to reorder and the
        # superseded ones can drop without leaving the list short.
        # rerank=False: in hybrid, ultimate and graph modes search() would run
        # the cross-encoder over its candidates, and this method throws that
        # ordering away for its own blend of relevance, recency and feedback.
        # On long turns the reranker was ten seconds of a recall that needs one.
        raw = self._db.search(
            query, limit=max(limit * 4, 20), mode=mode, filter=filt or None, rerank=False
        )

        candidates = []
        for r in raw:
            meta = r.metadata or {}
            if r.id in skip or meta.get(K_KIND) not in kinds:
                continue
            if meta.get(K_SUPERSEDED_BY) and not include_superseded:
                continue
            if _expired(meta, now):
                continue
            candidates.append(r)

        lo = min((r.score for r in candidates), default=0.0)
        hi = max((r.score for r in candidates), default=0.0)
        spread = hi - lo

        rescored = []
        for r in candidates:
            meta = r.metadata or {}
            relevance = (r.score - lo) / spread if spread > 0 else 1.0
            recency = _decay(_age_days(meta, now), self.half_life_days)
            fb = max(-2.0, min(2.0, feedback_score(meta, now, self.feedback_half_life_days)))
            final = relevance + self.recency_weight * recency + self.feedback_weight * fb
            explained = dict(meta)
            explained[K_SCORING] = {
                "relevance": r.score,
                "relevance_norm": round(relevance, 4),
                "recency": round(recency, 4),
                "feedback": round(fb, 4),
            }
            r.metadata = explained
            r.score = final
            rescored.append(r)

        rescored.sort(key=lambda r: r.score, reverse=True)
        rescored = apply_score_gap(rescored, score_gap, lambda r: r.score)[:limit]
        kept, cut, tokens = fit_to_budget(
            rescored, token_budget, lambda r: r.text, self._db.token_counter
        )
        return Results(
            items=kept,
            query=query,
            mode="recall",
            time_ms=raw.time_ms,
            truncated=cut > 0,
            cut_count=cut,
            token_estimate=tokens,
            token_budget=token_budget,
        )

    def context(
        self,
        query: str,
        session: Optional[str] = None,
        token_budget: Optional[int] = 1500,
        recent_turns: int = 6,
        limit: int = 10,
        now: Optional[datetime] = None,
        **recall_kwargs: Any,
    ) -> ContextBlock:
        """Assemble pinned facts, recent turns and recalled memories under one budget.

        Pinned facts are never cut: they are the caller's own declaration of
        what always matters, and they play the role the top result plays in
        ``search()``. If they alone exceed the budget the block says so rather
        than dropping one. Recent turns are cut oldest-first. Whatever is left
        goes to recall, and a recalled memory that does not fit is left out
        rather than shipped over budget.
        """
        counter = self._db.token_counter
        block = ContextBlock(query=query, session=session, token_budget=token_budget)
        seen: set = set()
        spent = 0

        pinned = self._pinned(session)
        for r in pinned:
            seen.add(r.id)
            spent += estimate_tokens(r.text, counter)
        block.pinned = pinned

        recent = self.turns(session, limit=recent_turns) if session is not None else []
        kept_recent: List["Result"] = []
        for r in reversed(recent):  # newest first, so the cut lands on the oldest
            cost = estimate_tokens(r.text, counter)
            if token_budget is not None and spent + cost > token_budget:
                break
            kept_recent.append(r)
            spent += cost
        block.cut_count += len(recent) - len(kept_recent)
        block.recent = list(reversed(kept_recent))
        seen.update(r.id for r in block.recent)

        remaining = None if token_budget is None else max(0, token_budget - spent)
        if remaining is None or remaining > 0:
            recalled = self.recall(
                query,
                session=session,
                limit=limit,
                token_budget=remaining,
                now=now,
                exclude_ids=seen,
                **recall_kwargs,
            )
            block.retrieved = list(recalled.items)
            block.cut_count += recalled.cut_count
            # recall() always ships its top result; here the pinned facts
            # already hold that guarantee, so trim to the budget instead.
            retrieved_tokens = recalled.token_estimate
            while (
                block.retrieved
                and token_budget is not None
                and spent + retrieved_tokens > token_budget
            ):
                dropped = block.retrieved.pop()
                retrieved_tokens -= estimate_tokens(dropped.text, counter)
                block.cut_count += 1
            spent += retrieved_tokens

        block.token_estimate = spent
        block.truncated = block.cut_count > 0 or (token_budget is not None and spent > token_budget)
        return block

    # -------------------------------------------------------------- internals

    def _pinned(self, session: Optional[str]) -> List["Result"]:
        """Pinned facts for the session plus the ones pinned for every session."""
        from .easy import Result

        points = self._scan({K_KIND: KIND_PINNED})
        now = utcnow()
        out = []
        for p in points:
            meta = p.metadata or {}
            if meta.get(K_SUPERSEDED_BY) or _expired(meta, now):
                continue
            if meta.get(K_SESSION) in (None, session):
                out.append(Result(id=p.id, text=p.text or "", score=1.0, metadata=dict(meta)))
        out.sort(key=lambda r: r.metadata.get(K_TS) or "")
        return out

    def _scan(self, filter: Dict[str, Any]) -> list:
        """Every point matching ``filter``.

        ``Collection.scroll`` applies the filter after the SQL ``LIMIT``, so
        one page can come back short or empty while later pages still match.
        Paging to the end is the only way to be sure.
        """
        collection = self._collection()
        points: list = []
        offset = 0
        while True:
            # Memory bookkeeping is the collection owner's path, and the
            # easy API refuses recall on a policied collection before it
            # ever reaches here.
            page, next_offset = collection._scroll_raw(limit=500, offset=offset, filter=filter)
            points.extend(page)
            if next_offset is None:
                return points
            offset = next_offset

    def _collection(self) -> "Collection":
        collection = self._db._collection
        if collection is None:
            raise ConfigurationError("The collection is not open")
        return collection

    def _claim_turn(self, session: Optional[str]) -> int:
        if session not in self._next_turns:
            existing = self._scan({K_SESSION: session, K_KIND: KIND_TURN})
            highest = max((p.metadata.get(K_TURN) or -1 for p in existing), default=-1)
            self._next_turns[session] = highest + 1
        turn = self._next_turns[session]
        self._next_turns[session] = turn + 1
        return turn
