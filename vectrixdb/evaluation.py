"""Measure retrieval, and answers, against questions whose answers are known.

Two layers, and they answer different questions.

*Retrieval*: for each question, did the documents that hold the answer come
back, and how high? Recall at k, MRR and nDCG over the ids a person marked as
right. It is cheap, it needs no model, and it is the number to watch while
changing a chunker or an embedder, because it moves when retrieval moves and
for no other reason.

*Answers*: given the answer a model wrote, how good is it? That needs a
judge, which is a callable you bring, and it is scored on the four names
Amazon Bedrock's managed RAG evaluation uses, so the two can be read side by
side: correctness, completeness, faithfulness, helpfulness.

Questions come from your own JSONL, or from the prompt dataset a Bedrock
evaluation job takes (``conversationTurns`` with a prompt and reference
responses). A Bedrock file carries reference answers and no ids, so
retrieval cannot be scored from it until somebody says which documents are
right: :func:`suggest_expected` proposes them for a person to check, and
proposes only. The file can be local, ``s3://bucket/key``, or a Blob
address, and :func:`golden_template` writes one to fill in, a row per
document it samples, with the answer's document already named.

*Setups*: which way of searching should a collection use? :func:`evaluate`
searches every golden question with every setup the collections can do,
every engine, every set of vectors and every method, times each search,
ranks the setups and names three: ``finds_the_most``, ``best_for_balance``
(the fastest within 4 points of it) and ``best_for_time`` (the fastest
within 8). It saves the run where the Evaluate pages read it, and switches
nothing: a person chooses.

*Two things a golden file also settles.* :func:`answer_cutoff` measures the
relevance below which the top result is more likely a near miss than the
answer, which is the number a program needs before it declines to answer.
:func:`sweep` compares what :func:`evaluate` cannot, because it is decided at
ingestion: the chunker, the chunk size and whether the headings were embedded.
It builds the index again for each, in a scratch folder.

*Chunking techniques.* :func:`compare_chunking` asks what :func:`sweep`
cannot: with the same characters handed to the model, which way of cutting
gets the most questions answered right? Structure-aware, parent-child,
recursive, sentence, semantic and fixed-size, each at three sizes, headings
embedded and not; with a model, LLM-based cutting too, and each technique
with a note from the model in front of every chunk; and late chunking where
the embedding model can. Each technique at its best, compared question by
question, a gap that could be luck called a tie. It saves the run where the
Chunking tab reads it.

*Written questions.* :func:`write_golden` drafts a golden file with a model,
the way DeepEval's synthesizer does, run on the collection's own chunks:
passages spread across the sections and scored by a critic, questions of
several kinds, each quote found on its page, copied wording and repeats
turned down, and every row a draft for a person to check.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import math
import os
import random
import re
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)
from urllib.parse import urlparse

from ._eval_chunking import (
    ADDS,
    TECHNIQUES,
    ChunkingStore,
    answer_with,
    chunking_build,
    chunking_choice,
    chunking_options,
    chunking_plan,
    chunking_report,
    chunking_store,
    compare_chunking,
    handed_over,
    judge_with,
    late_possible,
    plan_chunking,
    run_chunking_build,
)
from ._eval_report import PICKS, ReportStore, build_report, choose
from ._eval_tuning import answer_cutoff, sweep, sweep_markdown
from ._eval_writer import ChatWriter, GoldenWriting, WriterUnavailable, write_golden
from ._setups import Target, _engine, _storage_of, describe_target, search_of, setups_of

__all__ = [
    "ADDS",
    "ANSWER_METRICS",
    "ChatWriter",
    "ChunkingStore",
    "GOLDEN_SCHEMA",
    "PICKS",
    "TECHNIQUES",
    "Golden",
    "GoldenCheck",
    "GoldenProblem",
    "GoldenWriting",
    "MissingDocumentsWarning",
    "Question",
    "ReportStore",
    "Target",
    "WriterUnavailable",
    "answer_cutoff",
    "answer_report",
    "answer_with",
    "build_report",
    "check_golden",
    "choose",
    "chunking_build",
    "chunking_choice",
    "chunking_options",
    "chunking_plan",
    "chunking_report",
    "chunking_store",
    "compare_chunking",
    "describe_target",
    "evaluate",
    "golden_template",
    "handed_over",
    "judge_with",
    "late_possible",
    "load_bedrock_jsonl",
    "load_questions",
    "missing_documents",
    "stale_evidence",
    "plan_chunking",
    "read_golden",
    "report_store",
    "retrieval_report",
    "run_chunking_build",
    "run_setup",
    "save_questions",
    "search_of",
    "setups_of",
    "suggest_expected",
    "sweep",
    "sweep_markdown",
    "write_golden",
]


# ============================================================================
# SETTINGS: the answer metrics
# ============================================================================
#
# The measures a judge may score a written answer on.

ANSWER_METRICS = ("correctness", "completeness", "faithfulness", "helpfulness")


# ============================================================================
# QUESTIONS: loaded and saved
# ============================================================================
#
# INPUT   a JSONL path, ours or a Bedrock prompt dataset
# OUTPUT  one question, the answer a person would accept, and the ids
#         expected; questions loaded from either shape, with how many rows are
#         still empty and how many are drafts; questions saved
#
# Bedrock's shape is read so a dataset made for its evaluation can be asked
# here too.


@dataclass
class Question:
    """One question, the answer a person would accept, the ids of the
    documents, or chunks, that hold it, and the words there that answer it.

    ``evidence`` is those words, exactly as they are on the page: the golden
    writer keeps the quotes each question was checked against. With them a
    run can ask whether the words that answer came back, not only a piece of
    the right page.
    """

    text: str
    reference: str = ""
    expected: List[str] = field(default_factory=list)
    id: Optional[str] = None
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "id": self.id,
            "question": self.text,
            "reference": self.reference,
            "expected": list(self.expected),
        }
        if self.evidence:
            out["evidence"] = list(self.evidence)
        return out


def _text_of(block: Any) -> str:
    """The text in a Bedrock content block, a list of them, or a string."""
    if isinstance(block, str):
        return block
    if isinstance(block, Mapping):
        if isinstance(block.get("text"), str):
            return block["text"]
        return _text_of(block.get("content"))
    if isinstance(block, (list, tuple)):
        return " ".join(part for part in (_text_of(b) for b in block) if part)
    return ""


def _bedrock_questions(text: str, where: str) -> List[Question]:
    out: List[Question] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"{where}, line {number}: not JSON: {exc}") from exc
        for turn, item in enumerate(record.get("conversationTurns") or [], start=1):
            prompt = _text_of(item.get("prompt"))
            if not prompt:
                continue
            references = [_text_of(r) for r in item.get("referenceResponses") or []]
            out.append(
                Question(
                    text=prompt,
                    reference=" ".join(r for r in references if r),
                    id=f"q{number}.{turn}",
                )
            )
    return out


def load_bedrock_jsonl(path: Union[str, Path], fetcher: Any = None) -> List[Question]:
    """Questions from a Bedrock RAG evaluation prompt dataset.

    One line is one conversation; every turn with a prompt becomes a
    question, and its reference responses become the reference answer. The
    file has no document ids, so ``expected`` is empty until it is filled.
    """
    return _bedrock_questions(_fetch(path, fetcher).decode("utf-8-sig"), str(path))


def _our_questions(text: str, where: str) -> Tuple[List[Question], int, int]:
    """Our JSONL: the questions, then how many template rows are still empty, then how many are drafts."""
    out: List[Question] = []
    unfilled = drafts = 0
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"{where}, line {number}: not JSON: {exc}") from exc
        question = record.get("question") or record.get("text")
        if not isinstance(question, str) or not question.strip():
            if "hint" in record:  # a row golden_template wrote and nobody has filled in yet
                unfilled += 1
                continue
            raise ValueError(f"{where}, line {number}: no question")
        drafts += 1 if record.get("draft") else 0
        expected = record.get("expected") or []
        evidence = record.get("evidence") or []
        out.append(
            Question(
                text=question,
                reference=str(record.get("reference") or ""),
                expected=[str(e) for e in ([expected] if isinstance(expected, str) else expected)],
                id=record.get("id") or f"q{number}",
                evidence=[
                    " ".join(str(e).split())
                    for e in ([evidence] if isinstance(evidence, str) else evidence)
                    if str(e).strip()
                ],
            )
        )
    return out, unfilled, drafts


def load_questions(path: Union[str, Path], fetcher: Any = None) -> List[Question]:
    """Questions from this library's JSONL: ``question``, ``reference`` and
    ``expected`` per line. A Bedrock file is recognised and read as one.

    ``path`` is a file, ``s3://bucket/key`` or a Blob address; see
    :func:`read_golden`, which also says how many template rows are still
    waiting for a question. Those rows are left out here.
    """
    return read_golden(path, fetcher).questions


def save_questions(questions: Iterable[Question], path: Union[str, Path]) -> None:
    Path(path).write_text(
        "".join(json.dumps(q.to_dict(), ensure_ascii=False) + "\n" for q in questions),
        encoding="utf-8",
    )


# ============================================================================
# WHAT A RESULT COUNTS AS
# ============================================================================
#
# INPUT   a result, what a question expects, and by: chunk, document or page
# OUTPUT  whether the result is one the question expects, the chunk itself,
#         its document, a page it covers, or the words that answer; what it
#         counts as in a ranking
#
# A question answered on a page is counted by pages, so a chunk that covers
# the page counts.

#: What a result is counted right by: its document or page, its chunk id, or the words that answer.
BY = ("doc", "chunk", "evidence")


def _key_of(result: Any, by: str) -> str:
    meta = getattr(result, "metadata", None) or {}
    if by == "doc":
        return str(meta.get("_vx_doc") or str(getattr(result, "id", "")).rsplit(":", 1)[0])
    return str(getattr(result, "id", ""))


#: An expected entry that names one page of a document: ``report.pdf#page=41``.
_PAGE_ENTRY = re.compile(r"^(.+)#page=([1-9][0-9]*)$")


def _where(entry: Any) -> Tuple[str, Optional[int]]:
    """The document an expected entry names, and the page when it names one."""
    found = _PAGE_ENTRY.match(str(entry))
    return (found.group(1), int(found.group(2))) if found else (str(entry), None)


def _span_of(result: Any) -> Optional[Tuple[int, int]]:
    """The first and last page a result's chunk covers, or None when it has no pages."""
    meta = getattr(result, "metadata", None) or {}
    try:
        first = int(meta["page"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        last = int(meta.get("page_end") or first)
    except (TypeError, ValueError):
        last = first
    return first, max(first, last)


def _answers(result: Any, expected: Iterable[Any], by: str, evidence: Iterable[Any] = ()) -> bool:
    """Whether a result is one the question expects: the chunk itself, its document, a page it covers, or the words that answer.

    ``by="chunk"`` compares chunk ids. Otherwise an entry naming a document
    is answered by any chunk of it, and one naming a page, ``report.pdf#page=41``,
    by a chunk of that document whose pages include 41: a report of two
    hundred pages is one document, and "the right document came back" would
    be true of every question asked of it. ``by="evidence"`` goes further
    for a question with ``evidence``: a chunk of an expected document that
    holds one of its quotes, all of it or half of it in a row where the
    chunk ends inside it. A question without evidence is answered as
    ``by="doc"`` answers it.
    """
    if by == "chunk":
        return _key_of(result, "chunk") in {str(e) for e in expected}
    if by == "evidence":
        from ._eval_evidence import holds, usable

        quotes = [str(q) for q in evidence if usable(q)]
        if not quotes:
            return _answers(result, expected, "doc")
        if _key_of(result, "doc") not in {_where(e)[0] for e in expected}:
            return False
        text = str(getattr(result, "text", "") or "")
        return any(holds(text, quote) for quote in quotes)
    doc = _key_of(result, "doc")
    span: Any = False
    for entry in expected:
        name, page = _where(entry)
        if name != doc:
            continue
        if page is None:
            return True
        if span is False:
            span = _span_of(result)
        if span is not None and span[0] <= page <= span[1]:
            return True
    return False


def _unit_of(result: Any, by: str, paged: bool) -> str:
    """What a result counts as in a ranking: its chunk, its document, or, for a question answered on a page, its pages."""
    key = _key_of(result, by)
    if by == "doc" and paged:
        span = _span_of(result)
        if span is not None:
            key += f"#page={span[0]}" + (f"-{span[1]}" if span[1] != span[0] else "")
    return key


# ============================================================================
# THE TWO REPORTS: retrieval, and answers
# ============================================================================
#
# INPUT   a collection and the labelled questions; written answers and a judge
# OUTPUT  for each question, the ids its reference answer retrieves; recall at
#         k, MRR and nDCG over the questions; written answers scored with a
#         judge you bring
#
# Two layers, and they answer different questions: did the documents come
# back, and was the answer right.


def suggest_expected(
    db: Any, questions: Sequence[Question], by: str = "doc", top: int = 1, **search: Any
) -> Dict[str, List[str]]:
    """For each question, the ids its *reference answer* retrieves.

    A starting point for labelling and nothing more. Searching with the
    answer finds the document that says it far more reliably than searching
    with the question, which is why this is a fair way to propose a label and
    an unfair way to score one. Nothing is written to the questions: a person
    reads the suggestions and keeps the ones that are right.
    """
    out: Dict[str, List[str]] = {}
    for q in questions:
        hits = db.search(q.reference or q.text, limit=max(top * 3, 5), **search)
        seen: List[str] = []
        for hit in hits:
            key = _key_of(hit, by)
            if key not in seen:
                seen.append(key)
        out[q.id or q.text] = seen[:top]
    return out


def retrieval_report(
    db: Any,
    questions: Sequence[Question],
    k: Sequence[int] = (1, 3, 5, 10),
    by: str = "doc",
    **search: Any,
) -> Dict[str, Any]:
    """Recall at k, MRR and nDCG at the largest k, over the labelled questions.

    ``by="doc"`` counts a hit when any chunk of an expected document comes
    back, which is the right grain when labels name documents, and when an
    entry names one page of one, ``report.pdf#page=41``, a chunk that covers
    that page; the results are then ranked by the pages they cover rather
    than by document. ``by="chunk"`` wants the chunk ids themselves.
    ``by="evidence"`` ranks chunks, and counts one that holds the words the
    golden row quotes as answering, so the right page is not enough where
    the page is long; a question with no ``evidence`` is counted by its
    documents and pages. ``search`` is passed to ``db.search``, so
    the same questions can be asked with ``mode=``, ``vectors=``,
    ``rerank=`` and the rest, and the reports compared. Questions with no
    ``expected`` are counted and left out; a report over none of them says so
    rather than reporting a perfect score.
    """
    if by not in BY:
        raise ValueError(f"by is 'doc', 'chunk' or 'evidence', got {by!r}")
    ks = sorted({int(x) for x in k if int(x) > 0})
    if not ks:
        raise ValueError("k needs at least one positive number")
    deepest = ks[-1]
    labelled = [q for q in questions if q.expected]
    hits_at = {x: 0 for x in ks}
    reciprocal = 0.0
    gain = 0.0
    misses: List[Dict[str, Any]] = []
    for q in labelled:
        wanted = set(q.expected)
        paged = by == "doc" and any(_where(e)[1] is not None for e in wanted)
        ranked: List[str] = []
        right: Dict[str, bool] = {}
        for hit in db.search(q.text, limit=deepest * (4 if by == "doc" else 1), **search):
            key = _unit_of(hit, by, paged)
            if key not in right:
                ranked.append(key)
                right[key] = _answers(hit, wanted, by, q.evidence)
        ranked = ranked[:deepest]
        positions = [i for i, key in enumerate(ranked, start=1) if right[key]]
        for x in ks:
            if any(p <= x for p in positions):
                hits_at[x] += 1
        if positions:
            reciprocal += 1.0 / positions[0]
            dcg = sum(1.0 / math.log2(p + 1) for p in positions)
            # Several ranked units can answer one expected entry (two chunks
            # of one page), so the ideal ranking holds at least as many
            # relevant units as came back; counting only the entries let
            # nDCG pass 1.
            relevant = max(len(wanted), len(positions))
            ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(relevant, deepest) + 1))
            gain += dcg / ideal if ideal else 0.0
        else:
            misses.append(
                {"id": q.id, "question": q.text, "expected": sorted(wanted), "got": ranked[:5]}
            )
    n = len(labelled)
    return {
        "questions": len(questions),
        "labelled": n,
        "unlabelled": len(questions) - n,
        "by": by,
        "recall": {f"@{x}": round(hits_at[x] / n, 4) if n else None for x in ks},
        "mrr": round(reciprocal / n, 4) if n else None,
        f"ndcg@{deepest}": round(gain / n, 4) if n else None,
        "misses": misses,
        "search": {
            key: value
            for key, value in search.items()
            if isinstance(value, (str, int, float, bool, type(None)))
        },
    }


Judge = Callable[..., float]


def answer_report(
    questions: Sequence[Question],
    answers: Mapping[str, Any],
    judge: Judge,
    metrics: Sequence[str] = ANSWER_METRICS,
) -> Dict[str, Any]:
    """Score written answers with a judge you bring.

    ``answers`` maps a question's id to the answer text, or to a mapping with
    ``answer`` and ``sources``. ``judge(metric=, question=, reference=,
    answer=, sources=)`` returns a number from 0 to 1; anything outside that
    is an error, because a judge that returns 7 has not understood the
    question and averaging it in would hide that. A question with no answer
    is counted and not scored.
    """
    unknown = [m for m in metrics if m not in ANSWER_METRICS]
    if unknown:
        raise ValueError(f"metrics are {', '.join(ANSWER_METRICS)}; got {', '.join(unknown)}")
    totals = {m: 0.0 for m in metrics}
    rows: List[Dict[str, Any]] = []
    scored = 0
    for q in questions:
        given = answers.get(q.id or q.text)
        if given is None:
            continue
        answer = given.get("answer") if isinstance(given, Mapping) else given
        sources = given.get("sources") if isinstance(given, Mapping) else None
        row: Dict[str, Any] = {"id": q.id}
        for metric in metrics:
            score = float(
                judge(
                    metric=metric,
                    question=q.text,
                    reference=q.reference,
                    answer=str(answer),
                    sources=sources,
                )
            )
            if not 0.0 <= score <= 1.0:
                raise ValueError(
                    f"the judge scored {metric} {score} for {q.id}; a score is between 0 and 1"
                )
            row[metric] = round(score, 4)
            totals[metric] += score
        rows.append(row)
        scored += 1
    return {
        "questions": len(questions),
        "scored": scored,
        "unanswered": len(questions) - scored,
        "metrics": {m: round(totals[m] / scored, 4) if scored else None for m in metrics},
        "rows": rows,
    }


# ============================================================================
# THE GOLDEN FILE: read from anywhere
# ============================================================================
#
# INPUT   a path, an s3:// address, or a Blob address
# OUTPUT  the golden file as read: its questions, where it came from, and its
#         fingerprint, the sha256 of its bytes
#
# The fingerprint is what a run is filed under, so a report is never read
# against questions it was not asked.


@dataclass
class Golden:
    """A golden file as read: its questions, where it came from, and its fingerprint.

    ``sha256`` is of the bytes, so two saves of the same file are the same
    golden data and a function watching the file can tell a real change from
    a save that changed nothing. ``unfilled`` counts template rows nobody has
    written a question for yet; ``drafts`` counts questions a writer drafted
    and a person has not yet marked as checked. ``raw`` is the bytes as read,
    which a saved run keeps once a version so its questions can be
    downloaded later; questions made in memory have none.
    """

    questions: List[Question]
    source: str = ""
    sha256: str = ""
    unfilled: int = 0
    drafts: int = 0
    raw: bytes = field(default=b"", repr=False, compare=False)

    @property
    def labelled(self) -> List[Question]:
        return [q for q in self.questions if q.expected]

    def describe(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "sha256": self.sha256,
            "questions": len(self.questions),
            "labelled": len(self.labelled),
            "unfilled": self.unfilled,
            "drafts": self.drafts,
        }


def _blob_account(uri: str) -> Optional[str]:
    parsed = urlparse(uri)
    if parsed.scheme in ("https", "http") and ".blob." in parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def _s3_client() -> Any:
    try:
        import boto3
    except ImportError as exc:
        raise ImportError("An s3:// address needs boto3: pip install 'vectrixdb[aws]'") from exc
    return boto3.client("s3")


def _blob_client(account_url: str) -> Any:
    try:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient
    except ImportError as exc:
        raise ImportError(
            "A Blob address needs azure-storage-blob and azure-identity: pip install azure-storage-blob azure-identity"
        ) from exc
    return BlobServiceClient(account_url=account_url, credential=DefaultAzureCredential())


def _fetch(source: Union[str, Path], fetcher: Any = None) -> bytes:
    """The bytes at a path, an ``s3://`` address or a Blob address.

    ``fetcher`` is anything with ``fetch(uri) -> bytes``, the fetchers in
    :mod:`vectrixdb.worker` for instance, built around a client you made;
    left out, S3 uses boto3's default credentials and Blob the default Azure
    credential, which is the managed identity inside an Azure function.
    """
    text = str(source)
    if fetcher is not None:
        data = fetcher.fetch(text)
        return data if isinstance(data, bytes) else bytes(data)
    if text.startswith("s3://"):
        from .worker import S3Fetcher

        return S3Fetcher(_s3_client()).fetch(text)
    account = _blob_account(text)
    if account:
        from .worker import BlobFetcher

        return BlobFetcher(_blob_client(account)).fetch(text)
    if "://" in text and not text.startswith("file://"):
        raise ValueError(f"{text}: a golden file is a path, an s3:// address or a Blob address")
    from .worker import LocalFetcher

    return LocalFetcher().fetch(text)


def _not_found(exc: BaseException) -> bool:
    """Whether a fetch failed because nothing is there: a local file, a blob or an S3 key."""
    if isinstance(exc, FileNotFoundError):
        return True
    said = f"{type(exc).__name__} {exc}"
    return any(
        mark in said for mark in ("ResourceNotFoundError", "BlobNotFound", "NoSuchKey", "Not Found")
    )


def golden_hash(data: bytes) -> str:
    """The fingerprint a run is filed under: the sha256 of the file's bytes."""
    return hashlib.sha256(data).hexdigest()


def read_golden(source: Union[str, Path], fetcher: Any = None) -> Golden:
    """Read a golden file from a path, ``s3://bucket/key`` or a Blob address.

    Our JSONL and Bedrock's prompt dataset are both read. Rows that
    :func:`golden_template` wrote and nobody has filled in are left out and
    counted, so a file half written can still be evaluated for the half
    that is.
    """
    data = _fetch(source, fetcher)
    text = data.decode("utf-8-sig")
    first = next((line for line in text.splitlines() if line.strip()), "")
    if '"conversationTurns"' in first:
        questions, unfilled, drafts = _bedrock_questions(text, str(source)), 0, 0
    else:
        questions, unfilled, drafts = _our_questions(text, str(source))
    return Golden(
        questions=questions,
        source=str(source),
        sha256=golden_hash(data),
        unfilled=unfilled,
        drafts=drafts,
        raw=data,
    )


# ============================================================================
# THE SCHEMA, AND THE CHECK
# ============================================================================
#
# INPUT   a golden file, and the collection it will be asked of
# OUTPUT  the schema one row is held to; every way the file falls short, found
#         before a run rather than in one, with the line each is on
#
# The problems are read off the schema so the two cannot disagree, and a key
# not in it is told what it was most likely meant to be.

#: One document id, or one page of it: ``td/report.pdf`` or ``td/report.pdf#page=41``.
_EXPECTED = r"^(?:(?!#page=).)+(?:#page=[1-9][0-9]*)?$"

#: One row of this library's golden JSONL, as JSON Schema. :func:`check_golden`
#: holds a file to it before a run, and ``docs/reference/golden.schema.json``
#: publishes it for any other tool to check a file with.
GOLDEN_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "VectrixDB golden question",
    "description": (
        "One line of a golden file: a question, where the answer is, and the answer a person would accept. "
        "A row golden_template wrote and nobody has filled in yet has an empty question and a hint."
    ),
    "type": "object",
    "required": ["question", "expected"],
    "additionalProperties": False,
    "properties": {
        "id": {
            "type": ["string", "null"],
            "minLength": 1,
            "description": "The question's name in reports. Numbered by its line when left out.",
        },
        "question": {
            "type": "string",
            "description": "What a person would ask. Empty only on a template row, which has a hint.",
        },
        "expected": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "pattern": _EXPECTED},
            "description": (
                "Where the answer is: a document by its id, td/report.pdf, or one page of it, "
                "td/report.pdf#page=41, the page's place in the file as its citation gives it."
            ),
        },
        "reference": {
            "type": "string",
            "description": "The answer a person would accept, for scoring written answers.",
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": (
                "The words on the expected pages that answer the question, exactly as they are there, one quote an item: "
                "what the golden writer checked each question against. A run can then ask whether these words came back, "
                "not only a piece of the right page. A quote of fewer than three words is not used."
            ),
        },
        "hint": {
            "type": "string",
            "description": (
                "Where the answer is, for a person: on a template row, how the document or page starts, for whoever "
                "writes the question; on a written draft, the page and the words that answer it, for whoever checks it."
            ),
        },
        "draft": {
            "type": "boolean",
            "description": "True while a person has not checked a row that a model or suggest_expected wrote.",
        },
    },
    "if": {"properties": {"question": {"pattern": "^\\s*$"}}, "required": ["question"]},
    "then": {"required": ["hint"]},
}

#: How a JSON kind is said to a person.
_KIND = {
    "string": "text",
    "boolean": "true or false",
    "array": "a list",
    "object": "an object",
    "null": "null",
    "integer": "a number",
    "number": "a number",
}


def _kind_of(value: Any) -> str:
    """The JSON kind of a value as read, in the schema's words."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "integer" if isinstance(value, int) else "number"


def _meant(key: str, fields: Sequence[str]) -> Optional[str]:
    """The field a key not in the schema was most likely meant to be: ``text`` is the old name for ``question``."""
    if key == "text":
        return "question"
    close = difflib.get_close_matches(key, list(fields), n=1, cutoff=0.6)
    return close[0] if close else None


def _unknown_field(key: str, fields: Sequence[str]) -> str:
    meant = _meant(key, fields)
    if key == "text":
        return 'text is the old name for question: rename it "question"'
    if meant:
        return f'"{key}" is not a field. Did you mean "{meant}"?'
    return f'"{key}" is not a field. The fields are {", ".join(fields[:-1])} and {fields[-1]}'


def _list_problems(key: str, value: List[Any], rule: Mapping[str, Any]) -> List[str]:
    out: List[str] = []
    if len(value) < rule.get("minItems", 0):
        out.append(f"{key} is empty: name the document that answers it")
    items = rule.get("items") or {}
    pattern = re.compile(items["pattern"]) if items.get("pattern") else None
    held, one = ("quotes", "quote") if key == "evidence" else ("document ids", "id")
    seen = set()
    for item in value:
        if not isinstance(item, str):
            out.append(f"{key} should hold {held} as text, not {_KIND[_kind_of(item)]}")
            continue
        if len(item) < items.get("minLength", 0) or (key == "evidence" and not item.strip()):
            out.append(f"{key} has an empty {one} in it")
        elif pattern is not None and not pattern.search(item):
            out.append(
                f"{item} should end in a page number from 1, like #page=41, or name the document alone"
            )
        elif rule.get("uniqueItems") and item in seen:
            out.append(f"{key} names {item} twice")
        seen.add(item)
    return out


def _row_problems(record: Mapping[str, Any]) -> List[str]:
    """What is wrong with one row, read off :data:`GOLDEN_SCHEMA` so the two cannot disagree."""
    fields = GOLDEN_SCHEMA["properties"]
    unknown = [key for key in record if key not in fields]
    out = [_unknown_field(key, list(fields)) for key in unknown]
    # A field missing because its name is misspelt is one mistake, already said.
    misspelt = {_meant(key, list(fields)) for key in unknown}
    for key in GOLDEN_SCHEMA["required"]:
        if key not in record and key not in misspelt:
            out.append(
                f"{key} is missing"
                + (": name the document that answers it" if key == "expected" else "")
            )
    for key, rule in fields.items():
        if key not in record:
            continue
        value, kinds = (
            record[key],
            rule["type"] if isinstance(rule["type"], list) else [rule["type"]],
        )
        kind = _kind_of(value)
        if kind not in kinds:
            example = (
                f', like ["{value}"]'
                if key in ("expected", "evidence") and kind == "string"
                else ""
            )
            out.append(
                f"{key} should be {' or '.join(_KIND[k] for k in kinds)}, not {_KIND[kind]}{example}"
            )
        elif kind == "string" and len(value) < rule.get("minLength", 0):
            out.append(f"{key} is empty")
        elif kind == "array":
            out.extend(_list_problems(key, value, rule))
    # The schema's if and then: a question left empty is a template row, which has a hint.
    question = record.get("question")
    if isinstance(question, str) and not question.strip() and "hint" not in record:
        out.append("question is empty")
    return out


@dataclass
class GoldenProblem:
    """One thing wrong with a golden file, or worth knowing about it, and the line it is on."""

    line: Optional[int]
    message: str

    def __str__(self) -> str:
        return f"line {self.line}  {self.message}" if self.line else self.message


@dataclass
class GoldenCheck:
    """What :func:`check_golden` found. ``ok`` when nothing stands in the way of a run.

    ``rows`` counts the lines with anything on them; ``ready`` the questions
    that can be scored, ``unfilled`` the template rows still waiting for a
    question and ``drafts`` the questions nobody has checked. ``golden`` is
    the file as :func:`read_golden` reads it, there only when it is ``ok``.
    """

    source: str
    rows: int = 0
    ready: int = 0
    unfilled: int = 0
    drafts: int = 0
    #: No file there at all: a fresh deployment before anyone wrote a question.
    missing: bool = False
    errors: List[GoldenProblem] = field(default_factory=list)
    warnings: List[GoldenProblem] = field(default_factory=list)
    golden: Optional[Golden] = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        """The problems, then what is worth knowing, one a line, as a person reads them."""
        name = re.split(r"[\\/]", self.source.rstrip("/\\"))[-1] or self.source
        count = len(self.errors)
        head = (
            f"{name}: {count} problem{'s' if count != 1 else ''}"
            if count
            else f"{name}: {self.ready} question{'s' if self.ready != 1 else ''} ready"
        )
        width = max((len(str(p.line)) for p in self.errors + self.warnings if p.line), default=0)
        lines = [head]
        for p in self.errors:
            lines.append(f"  line {p.line:<{width}}  {p.message}" if p.line else f"  {p.message}")
        for p in self.warnings:
            lines.append(
                f"  note  line {p.line}: {p.message}" if p.line else f"  note  {p.message}"
            )
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "ok": self.ok,
            "rows": self.rows,
            "ready": self.ready,
            "unfilled": self.unfilled,
            "drafts": self.drafts,
            "errors": [{"line": p.line, "message": p.message} for p in self.errors],
            "warnings": [{"line": p.line, "message": p.message} for p in self.warnings],
        }


def check_golden(source: Union[str, Path], db: Any = None, *, fetcher: Any = None) -> GoldenCheck:
    """Every way a golden file falls short of :data:`GOLDEN_SCHEMA`, found before a run rather than in one.

    Every line is checked, so a file with five mistakes is put right in one
    go: a line that is not JSON, a field the schema does not have (a typo
    like ``expeted`` would otherwise drop the row from the scores without a
    word), a question or ``expected`` missing or empty, a value of the wrong
    kind, a page number that is not one. Then the file as a whole: an id
    used twice, the same question twice, and whether any question is ready.
    Those are ``errors``, and a run should not start with any.

    ``warnings`` do not stop a run: template rows still waiting for a
    question, drafts nobody has checked, and, given ``db``, the documents
    the collection does not hold, which no setup can find (see
    :func:`missing_documents`). A Bedrock prompt dataset names no documents
    at all, so it cannot be scored for retrieval until it is labelled, and
    the check says so. ``source`` is what :func:`read_golden` takes, and
    ``fetcher`` too.

    No file there at all is not an error to read: it is ``missing``, with
    one problem that says how to get questions, whatever holds the file. A
    store's own "not found" (a page of XML from Blob storage) is not put in
    front of a person.
    """
    check = GoldenCheck(source=str(source))
    try:
        data = _fetch(source, fetcher)
    except Exception as exc:
        if not _not_found(exc):
            raise
        check.missing = True
        check.errors.append(
            GoldenProblem(
                None,
                f"there are no golden questions at {source} yet: write some first "
                "(vectrixdb golden write, or a filled-in vectrixdb golden template), then run again",
            )
        )
        return check
    text = data.decode("utf-8-sig")
    lines = [
        (number, line) for number, line in enumerate(text.splitlines(), start=1) if line.strip()
    ]
    check.rows = len(lines)
    if not lines:
        check.errors.append(
            GoldenProblem(None, "the file is empty: one question a line, each a JSON object")
        )
        return check
    if '"conversationTurns"' in lines[0][1]:
        check.errors.append(
            GoldenProblem(
                None,
                "a Bedrock prompt dataset names no documents, so there is nothing to score retrieval against. "
                "Label it first: vectrixdb golden label",
            )
        )
        return check
    from ._eval_evidence import usable

    ids: Dict[str, int] = {}
    asked: Dict[str, int] = {}
    drafts: List[str] = []
    short: List[str] = []
    for number, line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            check.errors.append(
                GoldenProblem(number, f"is not JSON: {exc.msg.lower()} at character {exc.colno}")
            )
            continue
        if not isinstance(record, dict):
            check.errors.append(
                GoldenProblem(number, "is not a JSON object: each line is one question, in braces")
            )
            continue
        problems = _row_problems(record)
        # An id or a question seen before is looked for on every row, so it
        # is said now rather than after the row's other mistakes are put right.
        row_id = record.get("id")
        if isinstance(row_id, str) and row_id:
            if row_id in ids:
                problems.append(f"id {row_id} is used twice, first on line {ids[row_id]}")
            else:
                ids[row_id] = number
        asking = record.get("question", record.get("text"))
        question = " ".join(asking.split()) if isinstance(asking, str) else ""
        if question:
            if question.casefold() in asked:
                problems.append(f"the same question as line {asked[question.casefold()]}")
            else:
                asked[question.casefold()] = number
        if problems:
            check.errors.extend(GoldenProblem(number, p) for p in problems)
            continue
        if not question:
            check.unfilled += 1
            continue
        check.ready += 1
        if record.get("draft"):
            drafts.append(str(number))
        if any(not usable(quote) for quote in record.get("evidence") or []):
            short.append(str(number))
    check.drafts = len(drafts)
    if short:
        check.warnings.append(
            GoldenProblem(
                None,
                f"evidence of fewer than three words, on line{'s' if len(short) > 1 else ''} {_some(short, 'and')}: "
                "a quote that short is found anywhere and proves nothing, so it is not used and those pages decide",
            )
        )
    if drafts:
        check.warnings.append(
            GoldenProblem(
                None,
                f"drafts nobody has checked, on line{'s' if len(drafts) > 1 else ''} {_some(drafts, 'and')}: "
                "read each against its document, then delete draft",
            )
        )
    if check.unfilled and (check.ready or check.errors):
        check.warnings.append(
            GoldenProblem(
                None,
                f"{check.unfilled} template row{'s are' if check.unfilled != 1 else ' is'} still waiting for a question",
            )
        )
    if not check.errors and not check.ready:
        check.errors.append(
            GoldenProblem(
                None,
                f"no question is ready yet: {check.unfilled} template row{'s are' if check.unfilled != 1 else ' is'} "
                "waiting for one. Fill some in and try again",
            )
        )
    if check.errors:
        return check
    questions, unfilled, drafted = _our_questions(text, str(source))
    check.golden = Golden(
        questions=questions,
        source=str(source),
        sha256=golden_hash(data),
        unfilled=unfilled,
        drafts=drafted,
        raw=data,
    )
    if db is not None:
        # Said here, in the check's own notes, rather than again as a warning.
        if len(_targets(db)) > 1:
            gone_from = _missing_from_all(db, check.golden.labelled)
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", MissingDocumentsWarning)
                gone_from = missing_documents(db, check.golden.labelled)
        for name, gone in gone_from.items():
            check.warnings.append(GoldenProblem(None, _missing_message(name, gone)))
        for name, stale in stale_evidence(db, check.golden.labelled).items():
            check.warnings.append(GoldenProblem(None, _stale_message(name, stale)))
    return check


def report_store(where: Any) -> ReportStore:
    """Where runs are saved and read: a folder, ``s3://bucket/prefix``, a Blob address, or a store.

    A Blob address is ``https://<account>.blob.core.windows.net/<container>/<prefix>``.
    Anything with ``read``, ``write`` and ``list`` is used as it is, which
    is how a store around a client you built is passed.
    """
    if isinstance(where, ReportStore):
        return where
    if not isinstance(where, (str, os.PathLike)):
        return ReportStore(where)
    text = str(where)
    from .documents import BlobFiles, LocalFiles, S3Files

    if text.startswith("s3://"):
        parsed = urlparse(text)
        return ReportStore(S3Files(_s3_client(), parsed.netloc, parsed.path.lstrip("/")))
    account = _blob_account(text)
    if account:
        parts = urlparse(text).path.lstrip("/").split("/", 1)
        if not parts[0]:
            raise ValueError(f"{text}: a Blob address names a container")
        return ReportStore(
            BlobFiles(_blob_client(account), parts[0], parts[1] if len(parts) > 1 else "")
        )
    return ReportStore(LocalFiles(text))


# ============================================================================
# WHERE RUNS GO, AND THE TEMPLATE
# ============================================================================
#
# INPUT   a folder, s3://bucket/prefix, a Blob address or a store; a
#         collection and how many rows
# OUTPUT  where runs are saved and read; rows of a golden file to fill in, one
#         sampled document each with its id already in expected, or a row a
#         page with text on it
#
# A template row shows the page's number, what it is printed as, the heading
# over it and how it starts, so a person can write the question.


def _documents_of(db: Any) -> Dict[str, str]:
    """Each document the handle holds, by id, with the start of its text.

    The kept Markdown when the collection keeps its documents; otherwise the
    first chunk of each document, in the order they went in.
    """
    store = getattr(db, "documents", None)
    out: Dict[str, str] = {}
    if store is not None:
        for doc_id in store.ids():
            try:
                out[str(doc_id)] = store.markdown(doc_id)
            except Exception:  # an entry whose file went missing
                continue
        if out:
            return out
    collection = getattr(db, "_collection", None)
    rows = getattr(collection, "_iter_documents_raw", None)
    if not callable(rows):
        return out
    for chunk_id, text, metadata in rows():
        doc_id = str((metadata or {}).get("_vx_doc") or str(chunk_id).rsplit(":", 1)[0])
        if doc_id not in out:
            out[doc_id] = text or ""
    return out


def _hint(text: str, size: int = 240) -> str:
    body = text
    if body.startswith("---"):  # front matter, which says nothing a person needs here
        end = body.find("\n---", 3)
        body = body[end + 4 :] if end > 0 else body
    words = " ".join(body.split())
    return words if len(words) <= size else words[: size - 3].rsplit(" ", 1)[0] + "..."


#: A page with less text than this is a cover, a photograph or a divider: no place to ask a question of.
_THIN_PAGE = 200


def _pages_in(markdown: str) -> List[Dict[str, Any]]:
    """The pages of a kept document, from its front matter: number, printed number, heading over it, text.

    Empty for a recording, whose parts are times rather than pages, and for
    a document of one page, which is the document itself.
    """
    from .ingest import split_front_matter

    try:
        front, body = split_front_matter(markdown)
    except Exception:  # not kept Markdown: the first chunk of a document, say
        return []
    if not isinstance(front, Mapping) or front.get("segments"):
        return []
    marks = sorted((int(o), int(n)) for o, n in (front.get("pages") or []))
    if len(marks) < 2:
        return []
    labels = {str(k): str(v) for k, v in (front.get("page_labels") or {}).items()}
    heads = sorted((int(h[0]), str(h[1])) for h in (front.get("headings") or []) if len(h) >= 2)
    out: List[Dict[str, Any]] = []
    for i, (start, page) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        over = [title for offset, title in heads if offset < end]
        out.append(
            {
                "page": page,
                "label": labels.get(str(page)),
                "heading": over[-1] if over else None,
                "text": body[start:end],
            }
        )
    return out


def _page_hint(page: Mapping[str, Any]) -> str:
    """Which page, what it is printed as, the heading it sits under, and how it starts."""
    where = f"Page {page['page']}"
    if page.get("label") and page["label"] != str(page["page"]):
        where += f", printed {page['label']}"
    if page.get("heading"):
        where += f", under {page['heading']}"
    return f"{where}: {_hint(page['text'])}"


def golden_template(
    db: Any,
    path: Optional[Union[str, Path]] = None,
    n: int = 50,
    *,
    seed: int = 0,
    writer: Optional[Callable[[str], str]] = None,
    by: str = "doc",
) -> List[Dict[str, Any]]:
    """Rows of a golden file to fill in: one sampled document each, its id already in ``expected``.

    A person reads the ``hint``, the start of the document, and writes a
    question that document answers, and a reference answer if answers are to
    be scored too. Rows left empty are skipped when the file is read, so it
    can be filled a few at a time and evaluated as it goes.

    ``writer`` is a callable you bring, a model call say, from a document's
    text to a question. What it writes is marked ``"draft": true`` for a
    person to check and unmark; a question a model wrote about a document
    it just read is easier than the questions people ask.

    The sample is ``n`` documents chosen with ``seed``, so the same seed on
    the same collection gives the same rows. Written as JSONL to ``path``
    when one is given, and returned either way. ``db`` may be a list of
    handles, several collections sampled together into one golden file.

    ``by="page"`` gives a row a page instead, ``expected`` naming the page,
    ``report.pdf#page=41``, and ``hint`` saying which page it is, what it is
    printed as, the heading it sits under and how it starts. A report of two
    hundred pages is one document, so every setup finds "the document" for
    every question about it; the page is what tells them apart. The ``n``
    pages are spread evenly through the collection's pages from a start
    ``seed`` picks, so every part of a long report is asked about, and pages
    with next to no text on them, covers and dividers, are left out. A
    document with no pages to give, a recording or a picture, keeps its one
    row. Pages come from the kept Markdown, so the collection keeps its
    documents (``keep_source``).
    """
    if by not in ("doc", "page"):
        raise ValueError(f"by is 'doc' or 'page', got {by!r}")
    documents: Dict[str, str] = {}
    for handle in db if isinstance(db, (list, tuple)) else [db]:
        documents.update(_documents_of(handle))
    if not documents:
        raise ValueError(
            "no documents found: the collection is empty, or it lives in a store and was opened in another "
            "process. Open it with keep_source, or where its chunks were written."
        )
    if by == "page":
        return _page_template(documents, path, n, seed=seed, writer=writer)
    chosen = sorted(documents)
    if n < len(chosen):
        chosen = sorted(random.Random(seed).sample(chosen, n))
    rows: List[Dict[str, Any]] = []
    for number, doc_id in enumerate(chosen, start=1):
        text = documents[doc_id]
        row: Dict[str, Any] = {
            "id": f"g{number}",
            "question": "",
            "expected": [doc_id],
            "reference": "",
            "hint": _hint(text),
        }
        if writer is not None:
            drafted = str(writer(text) or "").strip()
            if drafted:
                row["question"] = drafted
                row["draft"] = True
        rows.append(row)
    if path is not None:
        Path(path).write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )
    return rows


def _page_template(
    documents: Mapping[str, str],
    path: Optional[Union[str, Path]],
    n: int,
    *,
    seed: int,
    writer: Optional[Callable[[str], str]],
) -> List[Dict[str, Any]]:
    """golden_template(by="page"): a row a page with text on it, spread evenly, and a row a document without pages."""
    candidates: List[Tuple[str, str, str]] = []
    for doc_id, text in sorted(documents.items()):
        pages = [p for p in _pages_in(text) if len(" ".join(p["text"].split())) >= _THIN_PAGE]
        if not pages:
            candidates.append((doc_id, _hint(text), text))
            continue
        candidates += [(f"{doc_id}#page={p['page']}", _page_hint(p), p["text"]) for p in pages]
    chosen = candidates
    if 0 < n < len(candidates):
        step = len(candidates) / n
        start = random.Random(seed).random() * step
        chosen = [candidates[int(start + i * step)] for i in range(n)]
    rows: List[Dict[str, Any]] = []
    for number, (entry, hint, text) in enumerate(chosen, start=1):
        row: Dict[str, Any] = {
            "id": f"g{number}",
            "question": "",
            "expected": [entry],
            "reference": "",
            "hint": hint,
        }
        if writer is not None:
            drafted = str(writer(text) or "").strip()
            if drafted:
                row["question"] = drafted
                row["draft"] = True
        rows.append(row)
    if path is not None:
        Path(path).write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )
    return rows


# ============================================================================
# ONE SETUP RUN
# ============================================================================
#
# INPUT   a target, a setup, and the questions
# OUTPUT  every labelled question searched with one setup, and each search
#         timed, with the cache out of the way
#
# A cached answer would time nothing.

DEPTH = 20
_WARM_UP = "a question that warms the models up before the clock starts"


@contextlib.contextmanager
def _uncached(db: Any) -> Iterator[None]:
    """The collection's search cache, out of the way: a cached answer would time nothing."""
    collection: Any = getattr(db, "_collection", None)
    saved = getattr(collection, "_cache", None)
    if saved is not None:
        collection._cache = None
    try:
        yield
    finally:
        if saved is not None:
            collection._cache = saved


def _first_right(
    hits: Iterable[Any], wanted: set, by: str, evidence: Iterable[Any] = ()
) -> Optional[int]:
    quotes = list(evidence)
    for position, hit in enumerate(hits, start=1):
        if _answers(hit, wanted, by, quotes):
            return position
    return None


def run_setup(
    target: Any,
    setup: Mapping[str, Any],
    questions: Sequence[Question],
    *,
    by: str = "doc",
    depth: int = DEPTH,
) -> Dict[str, Any]:
    """Search every labelled question with one setup and time each search.

    Returns the setup with ``ranks``, the position of the first right result
    for each question within ``depth`` results as they come back (a document
    that fills three places takes three), and ``times_ms``, the wall time of
    each ``search()`` call, query embedding and reranking included. One
    untimed search first loads the models, and the search cache is kept out
    of it. A setup whose first search fails, or whose graph turns out to be
    missing, comes back with ``error`` and no numbers; so does one where a
    question fails twice.
    """
    db = target.db if isinstance(target, Target) else target
    result: Dict[str, Any] = dict(setup)
    search = dict(setup.get("search") or {})
    labelled = [q for q in questions if q.expected]
    with _uncached(db):
        try:
            warm = db.search(_WARM_UP, limit=depth, **search)
            degraded = getattr(warm, "degraded", None)
            if degraded and setup.get("method") == "graph":
                raise RuntimeError(str(degraded))
        except Exception as exc:
            result.update(ranks=[], times_ms=[], error=f"{type(exc).__name__}: {exc}")
            return result
        ranks: List[Optional[int]] = []
        times: List[float] = []
        for q in labelled:
            for attempt in (1, 2):
                try:
                    started = time.perf_counter()
                    hits = db.search(q.text, limit=depth, **search)
                    elapsed = (time.perf_counter() - started) * 1000.0
                    break
                except Exception as exc:
                    if attempt == 2:
                        result.update(
                            ranks=[], times_ms=[], error=f"{q.id}: {type(exc).__name__}: {exc}"
                        )
                        return result
            ranks.append(_first_right(hits, set(q.expected), by, q.evidence))
            times.append(round(elapsed, 2))
    result.update(ranks=ranks, times_ms=times, error=None)
    return result


def _targets(given: Any) -> List[Target]:
    if isinstance(given, Target):
        return [given]
    if isinstance(given, Mapping):
        return [v if isinstance(v, Target) else Target(v, name=str(k)) for k, v in given.items()]
    if isinstance(given, (list, tuple)):
        return [g if isinstance(g, Target) else Target(g) for g in given]
    return [Target(given)]


# ============================================================================
# MISSING DOCUMENTS, AND THE EVALUATION
# ============================================================================
#
# INPUT   the targets and the golden file
# OUTPUT  the expected documents each target does not hold, and the questions
#         that leaves unfindable; every golden question searched with every
#         setup, the setups ranked, and three picked
#
# A collection that does not hold a document a question expects is warned
# about, not silently scored as a miss.


class MissingDocumentsWarning(UserWarning):
    """Golden questions expect a document a collection does not hold, so no setup can find them."""


def _probes(expected: str, by: str) -> List[str]:
    """The ids a document is looked up by: its own, and its first chunks'."""
    return (
        [expected, f"{expected}:0", f"{expected}:1", f"{expected}:2"]
        if by != "chunk"
        else [expected]
    )


def _not_held(db: Any, questions: Sequence[Question], by: str) -> List[str]:
    """The expected ids this handle cannot find, looked up the cheap way first.

    A page is looked up by its document: ``report.pdf#page=41`` is there when
    ``report.pdf`` is.
    """
    wanted = sorted(
        {_where(e)[0] if by != "chunk" else str(e) for q in questions for e in q.expected}
    )
    owners: Dict[str, List[str]] = {}
    for expected in wanted:
        for probe in _probes(expected, by):
            owners.setdefault(probe, []).append(expected)
    left = set(wanted)

    def found(ids: Iterable[Any]) -> None:
        for chunk_id in ids:
            for expected in owners.get(str(chunk_id), ()):
                left.discard(expected)

    get = getattr(db, "get", None)
    if left and callable(get):
        found(r.id for r in get(sorted(owners)))
    # A handle opened only to search an index another process filled holds
    # nothing here, so the store is asked for what is still missing.
    storage = _storage_of(db)
    collection = getattr(db, "_collection", None)
    if left and storage is not None and hasattr(storage, "get_batch"):
        ask = sorted(p for p, of in owners.items() if any(e in left for e in of))
        name = str(getattr(collection, "name", None) or getattr(db, "name", ""))
        found(p for p, row in zip(ask, storage.get_batch(name, ask)) if row is not None)
    # Chunks written under other ids still carry their document's.
    rows = getattr(collection, "_iter_documents_raw", None)
    if left and by != "chunk" and callable(rows):
        for chunk_id, _text, metadata in rows():
            left.discard(str((metadata or {}).get("_vx_doc") or str(chunk_id).rsplit(":", 1)[0]))
            if not left:
                break
    return sorted(left)


def _some(items: Sequence[str], joiner: str = "or", limit: int = 5) -> str:
    shown = [str(i) for i in items[:limit]]
    if len(items) > limit:
        return ", ".join(shown) + f" and {len(items) - limit} more"
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + f" {joiner} {shown[-1]}"


def stale_evidence(targets: Any, questions: Sequence[Question]) -> Dict[str, List[str]]:
    """The questions whose evidence no chunk of their documents holds any more, by target name.

    Evidence is written from the chunks as they were. A document read again
    since, with a better reader or other chunking, may word its pages
    differently, and a quote no chunk holds counts every setup wrong on
    that question however well it searched. This looks each question's
    quotes up in the chunks of its expected documents, in the collection's
    own record of what it indexed, and names the questions none of whose
    usable quotes is there. A document the target does not hold at all is
    :func:`missing_documents`' to report, and is left out here; a target
    whose chunks cannot be read is left out too. Empty when every quote is
    still on its pages.
    """
    from ._eval_evidence import holds, usable
    from ._eval_writer import _rows_of

    asked = [
        (q, [str(e) for e in q.evidence if usable(e)], {_where(e)[0] for e in q.expected})
        for q in questions
    ]
    asked = [(q, quotes, docs) for q, quotes, docs in asked if quotes and docs]
    out: Dict[str, List[str]] = {}
    if not asked:
        return out
    for target in _targets(targets):
        name = target.name or _engine(target.db)[1]
        try:
            rows = list(_rows_of(target.db))
        except Exception:  # noqa: BLE001 - a store that cannot be read is not a stale file
            continue
        texts: Dict[str, List[str]] = {}
        for chunk_id, text, meta in rows:
            if ":parent:" in str(chunk_id):
                continue
            doc = str((meta or {}).get("_vx_doc") or str(chunk_id).rsplit(":", 1)[0])
            texts.setdefault(doc, []).append(str(text or ""))
        stale = [
            str(q.id) if q.id else f"question {n}"
            for n, (q, quotes, docs) in enumerate(asked, start=1)
            if any(doc in texts for doc in docs)
            and not any(
                holds(text, quote)
                for doc in docs
                for text in texts.get(doc, ())
                for quote in quotes
            )
        ]
        if stale:
            out[name] = stale
    return out


def _stale_message(name: str, stale: Sequence[str]) -> str:
    """What a person reads about one target's questions whose evidence is gone from its pages."""
    return (
        f"{name} no longer holds the evidence of {len(stale)} question{'s' if len(stale) != 1 else ''} "
        f"({_some(list(stale), 'and')}): the documents were read again since the quotes were written, "
        "so every setup is counted wrong on them. Write the questions again, or check each quote against its page"
    )


def _missing_from_all(targets: Any, questions: Sequence[Question]) -> Dict[str, Dict[str, Any]]:
    """The documents no target holds, as :func:`missing_documents` reports them for one.

    One golden file over several collections names every collection's
    documents, and each collection lacks the others', which is no loss: a
    run searches every target. So the note is one entry, "every
    collection", for the documents none of them holds, and a target that
    could not be checked keeps an entry of its own. Each target is asked
    directly, since two collections may share a name.
    """
    out: Dict[str, Dict[str, Any]] = {}
    found: List[set] = []
    for n, target in enumerate(_targets(targets), start=1):
        name = target.name or _engine(target.db)[1]
        try:
            found.append(set(_not_held(target.db, questions, "doc")))
        except Exception as exc:  # the check goes on, and says what it could not check
            out[f"{name} ({n})"] = {"error": f"{type(exc).__name__}: {exc}"}
    everywhere = set.intersection(*found) if found else set()
    if everywhere:
        out["every collection"] = {
            "documents": sorted(everywhere),
            "questions": [
                str(q.id) if q.id else f"question {n}"
                for n, q in enumerate(questions, start=1)
                if q.expected and all(_where(e)[0] in everywhere for e in q.expected)
            ],
        }
    return out


def _missing_message(name: str, gone: Mapping[str, Any]) -> str:
    """What a person reads about one target's missing documents."""
    if gone.get("error"):
        return f"Could not check which expected documents {name} holds: {gone['error']}"
    docs = list(gone.get("documents") or [])
    lost = list(gone.get("questions") or [])
    text = f"{name} has no document called {_some(docs)}"
    if lost:
        text += (
            f", so {len(lost)} question{'s' if len(lost) != 1 else ''} ({_some(lost, 'and')}) "
            "cannot be found by any setup, and every score is lower for it"
        )
    ids, them = ("id", "the document") if len(docs) == 1 else ("ids", "the documents")
    return text + f". Correct the expected {ids} in the golden file, or add {them}."


def missing_documents(
    targets: Any, questions: Sequence[Question], *, by: str = "doc"
) -> Dict[str, Dict[str, Any]]:
    """The expected documents each target does not hold, and the questions that leaves unfindable.

    A document removed, renamed or never added is a miss on every setup:
    it lowers every score and says nothing about the setups, so this runs
    before any searching and warns with :class:`MissingDocumentsWarning`.
    Each expected id is looked up by its own id and its first chunks'
    (``id:0`` to ``id:2``), in the collection and then in its store, which
    is how a handle opened only to search an index another process filled
    finds them, and last by the ``_vx_doc`` its chunks carry.

    ``targets`` takes what :func:`evaluate` takes. The answer is what a
    report keeps, by target name, and names only the targets with something
    missing: ``documents``, every expected id not found, and ``questions``,
    the questions none of whose documents was found. A target the check
    could not run on has ``error`` instead. Empty when everything is there.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for target in _targets(targets):
        name = target.name or _engine(target.db)[1]
        try:
            docs = _not_held(target.db, questions, by)
        except Exception as exc:  # the run goes on, and says what it could not check
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
        else:
            if not docs:
                continue
            lost = set(docs)
            out[name] = {
                "documents": docs,
                "questions": [
                    str(q.id) if q.id else f"question {n}"
                    for n, q in enumerate(questions, start=1)
                    if q.expected
                    and all((_where(e)[0] if by != "chunk" else str(e)) in lost for e in q.expected)
                ],
            }
        warnings.warn(_missing_message(name, out[name]), MissingDocumentsWarning, stacklevel=2)
    return out


def evaluate(
    targets: Any,
    golden: Union[str, Path, Golden, Sequence[Question]],
    *,
    save_to: Any = None,
    only: Optional[Iterable[str]] = None,
    skip: Iterable[str] = (),
    balance: float = 4,
    time_points: float = 8,
    by: str = "doc",
    fetcher: Any = None,
    progress: Optional[Callable[[Dict[str, Any], int, int], None]] = None,
) -> Dict[str, Any]:
    """Search every golden question with every setup, rank the setups, and pick three.

    ``targets`` is a collection handle, a list of them, a mapping from a name
    to one, or :class:`Target` objects: the same documents on each engine
    you want compared, VectrixDB here and an Azure AI Search index, say. A
    handle offers what it was opened for, so open local collections with
    ``mode="ultimate"`` to include every method. ``golden`` is a path, an
    ``s3://`` or Blob address, a :class:`Golden`, or a list of questions.

    The report ranks every setup by how many questions found the right
    answer in the top 10, then by how many found it first, then by speed,
    and names ``finds_the_most``, ``best_for_balance`` (the fastest within
    ``balance`` points of the most) and ``best_for_time`` (the fastest within
    ``time_points``). With ``save_to``, a folder, an ``s3://`` or Blob
    address or a store, the run is saved there, which is what the server's
    Evaluate pages read. ``only`` limits the run to these setup keys.
    ``skip`` leaves out every setup of these methods, ``("hybrid_reranked",)``
    say, for a reranker that runs on a host too small for it: on Azure Flex
    Consumption the cross-encoder took 13 to 19 seconds a search and found
    less than the same search without it.
    ``by="evidence"`` counts a result right only when it is a chunk that
    holds the words the golden row quotes as answering, where the row has
    them, rather than any chunk of the right page: see
    :func:`retrieval_report`. Nothing is switched: the picks are for a
    person to choose between.

    Before any searching, every expected document is looked up on every
    target (see :func:`missing_documents`). One a target does not hold is
    warned about and kept in the report's ``missing``; the questions stay
    in, so the scores say what searching that index really finds.
    """
    if isinstance(golden, Golden):
        gold = golden
    elif isinstance(golden, (str, Path)):
        gold = read_golden(golden, fetcher)
    else:
        gold = Golden(questions=list(golden), source="in memory")
    labelled = gold.labelled
    if not labelled:
        raise ValueError(
            "no labelled questions: every question needs the ids of the documents that answer it in 'expected'"
        )
    if by not in BY:
        raise ValueError(f"by is 'doc', 'chunk' or 'evidence', got {by!r}")
    wanted = set(only) if only is not None else None
    chosen = _targets(targets)
    missing = missing_documents(chosen, labelled, by=by)
    skipped = {str(m) for m in skip}
    plan = [
        (t, s)
        for t in chosen
        for s in setups_of(t)
        if (wanted is None or s["key"] in wanted) and s["method"] not in skipped
    ]
    results = []
    for number, (target, setup) in enumerate(plan, start=1):
        if progress is not None:
            progress(setup, number, len(plan))
        results.append(run_setup(target, setup, labelled, by=by))
    described = {}
    for t in chosen:
        info = describe_target(t)
        described[info["name"]] = info
    report = build_report(
        results,
        gold.describe(),
        question_ids=[str(q.id) for q in labelled],
        targets=described,
        missing=missing,
        balance=balance,
        time_points=time_points,
    )
    # How a result was counted right: by its page, its chunk id, or the words that answer.
    report["by"] = by
    if save_to is not None:
        report_store(save_to).save(report, golden=gold.raw or None)
    return report
