"""Every setup, searched with the same golden questions, timed, ranked, and three picked.

What is tested here is what a person relies on when they choose: the three
picks follow their rules to the question, a run is saved where the pages
and the next run read it, a golden file is read from wherever it lives and
a half-filled template does not break a run, and the setups offered are the
ones the handle can really run.
"""

from __future__ import annotations

import hashlib
import json
import warnings

import numpy as np
import pytest

from vectrixdb._eval_report import (
    ReportStore,
    build_report,
    choose,
    frontier,
    neighbours,
    summarise,
)
from vectrixdb.evaluation import (
    Golden,
    MissingDocumentsWarning,
    Question,
    Target,
    evaluate,
    golden_template,
    load_questions,
    missing_documents,
    read_golden,
    report_store,
    run_setup,
    save_questions,
    setups_of,
)

# ---------------------------------------------------------------- helpers ---


def result(
    key,
    top10,
    first,
    ms,
    *,
    n=100,
    engine="vectrixdb",
    method="hybrid_reranked",
    models=("builtin",),
    error=None,
):
    """A setup's result with exactly these counts: ``top10`` and ``first`` are whole questions out of ``n``."""
    ranks = [1] * first + [5] * (top10 - first) + [None] * (n - top10)
    names = {"vectrixdb": "VectrixDB", "azure": "Azure AI Search"}
    labels = {"builtin": ("Built in", "builtin"), "azure-openai": ("Azure OpenAI", "service")}
    return {
        "key": key,
        "target": names[engine],
        "engine": names[engine],
        "engine_short": names[engine],
        "engine_kind": engine,
        "method": method,
        "method_label": {
            "hybrid_reranked": "Hybrid, reranked",
            "hybrid": "Hybrid",
            "hybrid_semantic": "Hybrid + semantic ranker",
            "dense": "Dense",
            "ultimate": "Ultimate",
        }.get(method, method),
        "ranker": "MiniLM, on this machine",
        "models": [
            {"key": m, "label": labels[m][0], "name": m, "kind": labels[m][1]} for m in models
        ],
        "search": {"mode": "hybrid"},
        "ranks": [] if error else ranks,
        "times_ms": [] if error else [float(ms)] * n,
        "error": error,
    }


def summed(*results):
    return [dict(r, summary=summarise(r)) for r in results]


# ------------------------------------------------------------------ picks ---


class TestThePicks:
    def test_the_most_then_the_fastest_within_four_points_then_within_eight(self):
        setups = summed(
            result("most", 96, 72, 296),
            result("close", 95, 70, 255),
            result("balance", 92, 62, 61),
            result("fast", 88, 52, 18),
            result("fastest", 74, 41, 6),
        )
        assert choose(setups) == {
            "finds_the_most": "most",
            "best_for_balance": "balance",
            "best_for_time": "fast",
        }

    def test_within_is_inclusive_and_counted_in_whole_questions(self):
        # 92 of 100 is exactly 4 points under 96 of 100, and it counts.
        setups = summed(
            result("most", 96, 70, 300), result("edge", 92, 60, 50), result("under", 91, 60, 10)
        )
        picks = choose(setups)
        assert picks["best_for_balance"] == "edge"
        assert picks["best_for_time"] == "under", "91 is within 8"

    def test_a_tie_on_the_top_10_goes_to_more_first_then_to_faster(self):
        setups = summed(
            result("slow-first", 95, 70, 300),
            result("fast-first", 95, 70, 200),
            result("fewer-first", 95, 69, 10),
        )
        assert choose(setups)["finds_the_most"] == "fast-first"

    def test_the_rules_are_settings(self):
        setups = summed(
            result("most", 96, 72, 296), result("near", 95, 70, 150), result("balance", 92, 62, 61)
        )
        assert choose(setups, balance=2)["best_for_balance"] == "near"

    def test_a_setup_that_could_not_run_is_never_picked_and_nothing_run_picks_nothing(self):
        setups = summed(result("broken", 0, 0, 0, error="TypeError: no"), result("ok", 80, 40, 20))
        assert set(choose(setups).values()) == {"ok"}
        assert choose(summed(result("broken", 0, 0, 0, error="x"))) == {
            "finds_the_most": None,
            "best_for_balance": None,
            "best_for_time": None,
        }

    def test_all_three_can_be_one_setup(self):
        assert set(choose(summed(result("only", 90, 50, 5))).values()) == {"only"}


class TestTheNumbers:
    def test_a_summary_counts_whole_questions(self):
        r = {"ranks": [1, 1, 2, 4, 11, None, 3, 21], "times_ms": [10, 20, 30, 40, 50, 60, 70, 800]}
        s = summarise(r)
        assert s["counts"] == {"1": 2, "3": 4, "5": 5, "10": 5, "20": 6}
        assert s["found"]["10"] == 0.625 and s["landed"] == [2, 2, 1, 0, 1, 2], (
            "first, 2-3, 4-5, 6-10, 11-20, missed"
        )
        assert s["median_ms"] == 45.0 and s["p95_ms"] == 800.0
        assert s["mrr"] == round((1 + 1 + 0.5 + 0.25 + 1 / 11 + 1 / 3 + 1 / 21) / 8, 4)

    def test_nothing_beats_the_frontier_on_both(self):
        setups = summed(
            result("a", 96, 70, 300),
            result("b", 92, 60, 60),
            result("c", 90, 60, 90),
            result("d", 88, 50, 18),
        )
        assert frontier(setups) == ["d", "b", "a"], "c is slower than b and finds less"


class TestNeighbours:
    def test_one_change_away_the_ranker_the_models_and_the_engine(self):
        setups = summed(
            result(
                "sem",
                96,
                72,
                296,
                engine="azure",
                method="hybrid_semantic",
                models=("azure-openai", "builtin"),
            ),
            result(
                "mini",
                95,
                69,
                286,
                engine="azure",
                method="hybrid_reranked",
                models=("azure-openai", "builtin"),
            ),
            result(
                "sem-aoai",
                94,
                70,
                284,
                engine="azure",
                method="hybrid_semantic",
                models=("azure-openai",),
            ),
            result(
                "sem-bge",
                93,
                68,
                232,
                engine="azure",
                method="hybrid_semantic",
                models=("builtin",),
            ),
            result(
                "vx-ult",
                95,
                70,
                255,
                engine="vectrixdb",
                method="ultimate",
                models=("azure-openai", "builtin"),
            ),
        )
        found = {n["key"]: (n["change"], n["detail"]) for n in neighbours(setups[0], setups)}
        assert found["mini"] == ("Without the semantic ranker", "MiniLM reranks instead")
        assert found["sem-aoai"] == ("Without the built-in model", "Azure OpenAI's vectors only")
        assert found["sem-bge"] == ("Without Azure OpenAI", "the built-in model only")
        assert found["vx-ult"] == ("On VectrixDB instead", "Ultimate, the same two models")
        assert [n["key"] for n in neighbours(setups[0], setups)] == [
            "mini",
            "sem-aoai",
            "sem-bge",
            "vx-ult",
        ], "ranker, models, engine"


# ----------------------------------------------------------------- report ---

GOLDEN = {
    "source": "golden.jsonl",
    "sha256": "ab" * 32,
    "questions": 100,
    "labelled": 100,
    "unfilled": 0,
    "drafts": 0,
}


class TestTheReport:
    def test_ranked_picked_and_named_by_time_and_golden_file(self):
        report = build_report(
            [result("b", 90, 50, 20), result("a", 96, 72, 296), result("x", 0, 0, 0, error="no")],
            GOLDEN,
            created=0,
        )
        assert report["id"] == "19700101-000000-ababab"
        assert [s["key"] for s in report["setups"]] == ["a", "b", "x"] and [
            s["rank"] for s in report["setups"]
        ] == [1, 2, None]
        assert report["picks"]["finds_the_most"] == "a" and report["rules"] == {
            "balance_points": 4,
            "time_points": 8,
        }
        assert report["setups"][2]["summary"] == {"questions": 0}

    def test_the_documents_an_index_does_not_hold_are_kept_with_the_run(self):
        gone = {"Azure AI Search": {"documents": ["retired-policy"], "questions": ["q4"]}}
        assert (
            build_report([result("a", 96, 72, 296)], GOLDEN, missing=gone, created=0)["missing"]
            == gone
        )
        assert build_report([result("a", 96, 72, 296)], GOLDEN, created=0)["missing"] == {}


class TestTheStore:
    def test_runs_are_saved_listed_newest_first_and_read_back(self, tmp_path):
        store = ReportStore(tmp_path)
        first = store.save(build_report([result("a", 90, 50, 20)], GOLDEN, created=0))
        second = store.save(build_report([result("a", 92, 50, 20)], GOLDEN, created=3600))
        assert store.ids() == [second, first]
        assert store.get("latest")["id"] == second and store.get(first)["id"] == first
        assert [p.name for p in (tmp_path / "retrieval" / "runs" / second).iterdir()] == [
            "report.json"
        ], "one file a run, and it holds the picks"
        assert (
            json.loads(
                (tmp_path / "retrieval" / "runs" / second / "report.json").read_text(
                    encoding="utf-8"
                )
            )["picks"]["finds_the_most"]
            == "a"
        )
        history = store.history()
        assert [h["id"] for h in history] == [second, first] and history[0]["top10"] == {"a": 92}
        assert history[0]["collections"] == [], "a report built without targets names no collection"

    def test_a_run_records_the_collections_it_ran_over_and_the_history_says_them(self, tmp_path):
        store = ReportStore(tmp_path)
        targets = {
            "memos": {"name": "memos", "engine": "vectrixdb", "collection": "memos"},
            "notes-on-search": {
                "name": "notes-on-search",
                "engine": "azure_search",
                "collection": "notes",
            },
        }
        report = build_report([result("a", 90, 50, 20)], GOLDEN, created=0, targets=targets)
        assert report["collections"] == ["memos", "notes"], (
            "read off the targets, sorted, each once"
        )
        run = store.save(report)
        assert store.history()[0]["collections"] == ["memos", "notes"] and store.get(run)[
            "collections"
        ] == ["memos", "notes"]
        # A run saved before it recorded them is read off its targets when listed.
        older = build_report([result("a", 90, 50, 20)], GOLDEN, created=3600, targets=targets)
        del older["collections"]
        store.save(older)
        assert store.history()[0]["collections"] == ["memos", "notes"]

    def test_a_missing_run_or_a_path_is_a_key_error(self, tmp_path):
        store = ReportStore(tmp_path)
        assert store.latest() is None
        for bad in ("nope", "../etc", ".hidden"):
            with pytest.raises(KeyError):
                store.get(bad)

    def test_the_history_reads_each_report_once(self, tmp_path):
        store = ReportStore(tmp_path)
        run = store.save(build_report([result("a", 90, 50, 20)], GOLDEN, created=0))
        assert store.history()[0]["top10"] == {"a": 90}
        (tmp_path / "retrieval" / "runs" / run / "report.json").write_text("{}", encoding="utf-8")
        assert store.history()[0]["top10"] == {"a": 90}, (
            "a saved run never changes, so it is not read again"
        )

    def test_a_bucket_or_a_container_holds_runs_the_same_way(self, tmp_path, monkeypatch):
        import vectrixdb.evaluation as evaluation

        s3, blob = FakeS3(), FakeBlobService()
        monkeypatch.setattr(evaluation, "_s3_client", lambda: s3)
        monkeypatch.setattr(evaluation, "_blob_client", lambda account: blob)
        for where in ("s3://evals/handbook", "https://acct.blob.core.windows.net/evals/handbook"):
            store = report_store(where)
            run = store.save(build_report([result("a", 90, 50, 20)], GOLDEN, created=0))
            assert store.ids() == [run] and store.get("latest")["picks"]["finds_the_most"] == "a"
        assert ("evals", "handbook/retrieval/runs/19700101-000000-ababab/report.json") in s3.objects
        assert ("evals", "handbook/retrieval/runs/19700101-000000-ababab/report.json") in blob.blobs
        assert isinstance(report_store(tmp_path / "local"), ReportStore)
        raw = b'{"id": "q1", "question": "Can I defer?", "expected": ["deferment"]}\n'
        sha = hashlib.sha256(raw).hexdigest()
        for where in ("s3://evals/handbook", "https://acct.blob.core.windows.net/evals/handbook"):
            store = report_store(where)
            store.save(
                build_report([result("a", 90, 50, 20)], dict(GOLDEN, sha256=sha), created=60),
                golden=raw,
            )
            assert store.has_golden(sha) and store.golden(sha) == raw
        assert s3.objects[("evals", f"handbook/golden_dataset/{sha}.jsonl")] == raw
        assert blob.blobs[("evals", f"handbook/golden_dataset/{sha}.jsonl")] == raw

    def test_the_golden_file_is_kept_once_a_version_and_only_the_one_named(self, tmp_path):
        raw = b'{"id": "q1", "question": "Can I defer a payment?", "expected": ["deferment"]}\n'
        sha = hashlib.sha256(raw).hexdigest()
        store = ReportStore(tmp_path)
        first = store.save(
            build_report([result("a", 90, 50, 20)], dict(GOLDEN, sha256=sha), created=0), golden=raw
        )
        second = store.save(
            build_report([result("a", 91, 50, 20)], dict(GOLDEN, sha256=sha), created=60),
            golden=raw,
        )
        assert first != second and store.golden(sha) == raw and store.has_golden(sha)
        assert [p.name for p in (tmp_path / "golden_dataset").iterdir()] == [f"{sha}.jsonl"], (
            "once a version, not once a run"
        )
        store.save(build_report([result("a", 90, 50, 20)], GOLDEN, created=120), golden=raw)
        assert not store.has_golden(GOLDEN["sha256"]), (
            "bytes that are not the ones the report names are not kept"
        )
        for missing in (GOLDEN["sha256"], "../runs", ""):
            with pytest.raises(KeyError):
                store.golden(missing)

    def test_a_copy_kept_in_the_folder_before_is_still_read_and_not_kept_twice(self, tmp_path):
        raw = b'{"id": "q1", "question": "Can I defer a payment?", "expected": ["deferment"]}\n'
        sha = hashlib.sha256(raw).hexdigest()
        (tmp_path / "golden").mkdir()
        (tmp_path / "golden" / f"{sha}.jsonl").write_bytes(raw)
        store = ReportStore(tmp_path)
        assert store.has_golden(sha) and store.golden(sha) == raw
        store.save(
            build_report([result("a", 90, 50, 20)], dict(GOLDEN, sha256=sha), created=0), golden=raw
        )
        assert not (tmp_path / "golden_dataset").exists(), "kept already, where it was"
        other = b'{"id": "q2", "question": "Who pays the fee?", "expected": ["fees"]}\n'
        store.save(
            build_report(
                [result("a", 90, 50, 20)],
                dict(GOLDEN, sha256=hashlib.sha256(other).hexdigest()),
                created=60,
            ),
            golden=other,
        )
        assert (
            tmp_path / "golden_dataset" / f"{hashlib.sha256(other).hexdigest()}.jsonl"
        ).read_bytes() == other


class _Body:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        return {"Body": _Body(self.objects[(Bucket, Key)])}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        return {
            "Contents": [
                {"Key": k} for b, k in sorted(self.objects) if b == Bucket and k.startswith(Prefix)
            ]
        }


class FakeBlobService:
    def __init__(self):
        self.blobs = {}

    def get_blob_client(self, container, blob):
        service = self

        class Client:
            def upload_blob(self, data, overwrite=False):
                service.blobs[(container, blob)] = data

            def download_blob(self):
                class Stream:
                    def readall(inner):
                        return service.blobs[(container, blob)]

                return Stream()

            def exists(self):
                return (container, blob) in service.blobs

        return Client()

    def get_container_client(self, container):
        service = self

        class Container:
            def list_blobs(self, name_starts_with=""):
                return [
                    {"name": b}
                    for c, b in sorted(service.blobs)
                    if c == container and b.startswith(name_starts_with)
                ]

        return Container()


# ------------------------------------------------------------ golden files ---


class TestGoldenFiles:
    def test_the_same_bytes_are_the_same_golden_data(self, tmp_path):
        path = tmp_path / "golden.jsonl"
        path.write_text(
            '{"id": "g1", "question": "What is deferred?", "expected": ["deferment"]}\n',
            encoding="utf-8",
        )
        one, two = read_golden(path), read_golden(str(path))
        assert one.sha256 == two.sha256 and len(one.sha256) == 64
        path.write_text(
            '{"id": "g1", "question": "What is deferred now?", "expected": ["deferment"]}\n',
            encoding="utf-8",
        )
        assert read_golden(path).sha256 != one.sha256

    def test_template_rows_nobody_filled_are_left_out_and_counted(self, tmp_path):
        path = tmp_path / "golden.jsonl"
        rows = [
            {"id": "g1", "question": "", "expected": ["a"], "hint": "The start of a."},
            {
                "id": "g2",
                "question": "Where is b?",
                "expected": ["b"],
                "hint": "The start of b.",
                "draft": True,
            },
            {"id": "g3", "question": "What is c?", "expected": ["c"]},
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        golden = read_golden(path)
        assert (
            [q.id for q in golden.questions] == ["g2", "g3"]
            and golden.unfilled == 1
            and golden.drafts == 1
        )
        assert golden.describe()["labelled"] == 2 and [q.id for q in load_questions(path)] == [
            "g2",
            "g3",
        ]

    def test_a_blank_question_that_is_not_a_template_row_is_still_an_error(self, tmp_path):
        path = tmp_path / "golden.jsonl"
        path.write_text('{"id": "g1", "question": " ", "expected": ["a"]}\n', encoding="utf-8")
        with pytest.raises(ValueError, match="line 1: no question"):
            read_golden(path)

    def test_a_golden_file_in_a_bucket_is_read_through_the_fetcher(self):
        class Fetcher:
            def __init__(self):
                self.asked = []

            def fetch(self, uri):
                self.asked.append(uri)
                return b'{"question": "Where?", "expected": ["x"]}\n'

        fetcher = Fetcher()
        golden = read_golden("s3://evals/handbook/golden.jsonl", fetcher=fetcher)
        assert fetcher.asked == ["s3://evals/handbook/golden.jsonl"] and golden.questions[
            0
        ].expected == ["x"]
        assert golden.source == "s3://evals/handbook/golden.jsonl"

    def test_an_address_that_is_not_a_file_a_bucket_or_a_container_says_so(self):
        with pytest.raises(ValueError, match="a path, an s3:// address or a Blob address"):
            read_golden("ftp://example.com/golden.jsonl")


# ----------------------------------------------------------- a collection ---

DOCS = {
    "emergency-fund": "Emergency fund access and fee relief: expedited access to funds and waived fees for displaced customers.",
    "deferment": "Payment deferment: customers in disaster areas can defer loan payments with no late fees.",
    "divestment": "Early divestment guidance for customers who need to sell investments before maturity.",
}
WORDS = {
    "cash": 0,
    "fund": 0,
    "emergency": 0,
    "fee": 0,
    "payment": 1,
    "deferment": 1,
    "loan": 1,
    "late": 1,
    "divestment": 2,
    "investments": 2,
}
QUESTIONS = [
    Question("Can I postpone a loan payment?", expected=["deferment"], id="q1"),
    Question("I lost my home and need cash now", expected=["emergency-fund"], id="q2"),
    Question("selling investments early", expected=["divestment"], id="q3"),
]
RETIRED = Question("Where is the old travel policy?", expected=["retired-policy"], id="q4")


def embed(texts):
    out = np.zeros((len(texts), 3), dtype=np.float32)
    for i, text in enumerate(texts):
        for word in text.lower().replace(".", " ").replace(",", " ").replace(":", " ").split():
            if word in WORDS:
                out[i, WORDS[word]] += 1.0
        if not out[i].any():
            out[i] = [0.1, 0.1, 0.1]
    return out


class WordOverlapReranker:
    """A reranker you bring, the way BedrockReranker is brought: no model to load."""

    label = "word-overlap"

    def rerank(self, query, texts, top=None):
        words = set(query.lower().split())
        scored = sorted(
            ((i, len(words & set(t.lower().split())) / 10.0) for i, t in enumerate(texts)),
            key=lambda p: -p[1],
        )
        return scored[:top] if top else scored


def open_db(tmp_path, mode="hybrid", **kw):
    from vectrixdb import Vectrix

    db = Vectrix(
        "policies",
        path=str(tmp_path),
        embed_fn=embed,
        dimension=3,
        mode=mode,
        embedding_cache=False,
        **kw,
    )
    for doc_id, text in DOCS.items():
        db.add_document(text, doc_id=doc_id)
    return db


class TestTheTemplate:
    def test_one_row_a_document_its_id_already_expected(self, tmp_path):
        db = open_db(tmp_path, mode="dense")
        try:
            rows = golden_template(db, tmp_path / "golden.jsonl", n=2, seed=7)
            again = golden_template(db, n=2, seed=7)
        finally:
            db.close()
        assert len(rows) == 2 and rows == again, "the same seed gives the same rows"
        assert all(
            r["question"] == "" and len(r["expected"]) == 1 and r["expected"][0] in DOCS
            for r in rows
        )
        assert rows[0]["hint"] == DOCS[rows[0]["expected"][0]][: len(rows[0]["hint"])]
        golden = read_golden(tmp_path / "golden.jsonl")
        assert golden.questions == [] and golden.unfilled == 2, (
            "nothing to evaluate until somebody writes"
        )

    def test_a_writer_drafts_and_a_draft_is_marked(self, tmp_path):
        db = open_db(tmp_path, mode="dense")
        try:
            rows = golden_template(
                db,
                n=5,
                writer=lambda text: "What does this say about " + text.split(":")[0].lower() + "?",
            )
        finally:
            db.close()
        assert len(rows) == 3 and all(
            r["draft"] is True and r["question"].startswith("What does") for r in rows
        )

    def test_an_empty_collection_says_where_to_look(self, tmp_path):
        from vectrixdb import Vectrix

        db = Vectrix("empty", path=str(tmp_path), embed_fn=embed, dimension=3)
        try:
            with pytest.raises(ValueError, match="no documents found"):
                golden_template(db)
        finally:
            db.close()


class TestWhatAHandleCanDo:
    def test_the_mode_it_was_opened_with_is_the_ceiling(self, tmp_path):
        db = open_db(tmp_path, mode="dense")
        try:
            assert [s["method"] for s in setups_of(Target(db))] == ["keyword", "dense"]
        finally:
            db.close()

    def test_hybrid_offers_the_fused_list_and_the_reranked_one(self, tmp_path):
        db = open_db(tmp_path, mode="hybrid", reranker=WordOverlapReranker())
        try:
            setups = setups_of(Target(db))
        finally:
            db.close()
        by_method = {s["method"]: s for s in setups}
        assert list(by_method) == ["keyword", "dense", "hybrid", "hybrid_reranked"]
        assert by_method["hybrid"]["search"] == {"mode": "hybrid", "rerank": False}
        assert by_method["hybrid_reranked"]["ranker"] == "word-overlap", (
            "the reranker you brought, by its name"
        )
        assert (
            by_method["keyword"]["models"] == []
            and by_method["keyword"]["key"] == "vectrixdb.keyword.words"
        )
        assert by_method["dense"]["models"][0]["label"] == "Your model", "an embed_fn is yours"


class TestTwoModelsInOneCollection:
    """A local collection with a list of dense models searches its own index in
    the method asked for and every other model's index by meaning alone. A
    second model on its own is a dense setup and nothing more, and keyword
    search names the collection's own index so the others are not fused in."""

    def test_the_second_model_alone_is_dense_only(self, tmp_path):
        from vectrixdb import Vectrix

        def second(texts):
            return embed(texts)[:, ::-1].copy()

        db = Vectrix(
            "policies",
            path=str(tmp_path),
            embed_fn=embed,
            dimension=3,
            mode="hybrid",
            dense_model=[None, ("second", second, 3)],
            reranker=WordOverlapReranker(),
            embedding_cache=False,
        )
        try:
            for doc_id, text in DOCS.items():
                db.add_document(text, doc_id=doc_id)
            setups = setups_of(Target(db))
            found = sorted((s["method"], s["search"].get("vectors")) for s in setups)
            report = evaluate(Target(db), QUESTIONS)
        finally:
            db.close()
        assert found == [
            ("dense", "both"),
            ("dense", "own"),
            ("dense", "second"),
            ("hybrid", "both"),
            ("hybrid", "own"),
            ("hybrid_reranked", "both"),
            ("hybrid_reranked", "own"),
            ("keyword", "own"),
        ]
        assert all(not s["error"] for s in report["setups"]) and len(report["setups"]) == 8


class TestARun:
    def test_every_setup_is_searched_timed_ranked_and_saved(self, tmp_path):
        db = open_db(tmp_path / "db", mode="hybrid", reranker=WordOverlapReranker())
        seen = []
        try:
            report = evaluate(
                {"VectrixDB": db},
                QUESTIONS,
                save_to=tmp_path / "evaluations",
                progress=lambda s, n, total: seen.append((n, total)),
            )
        finally:
            db.close()
        assert seen == [(1, 4), (2, 4), (3, 4), (4, 4)]
        assert [s["rank"] for s in report["setups"]] == [1, 2, 3, 4]
        for s in report["setups"]:
            assert s["error"] is None and len(s["ranks"]) == 3 and len(s["times_ms"]) == 3
            assert all(t >= 0 for t in s["times_ms"])
        assert set(report["picks"].values()) <= {s["key"] for s in report["setups"]}
        assert report["golden"]["labelled"] == 3 and report["question_ids"] == ["q1", "q2", "q3"]
        assert report["targets"]["VectrixDB"]["chunks"] == 3
        stored = ReportStore(tmp_path / "evaluations")
        assert stored.ids() == [report["id"]] and stored.get("latest")["picks"] == report["picks"]

    def test_a_run_from_a_golden_file_keeps_that_file_and_one_from_memory_keeps_none(
        self, tmp_path
    ):
        db = open_db(tmp_path / "db", mode="dense")
        path = tmp_path / "golden.jsonl"
        save_questions(QUESTIONS, path)
        try:
            from_file = evaluate(db, path, save_to=tmp_path / "evaluations")
            in_memory = evaluate(db, QUESTIONS, save_to=tmp_path / "evaluations")
        finally:
            db.close()
        store = ReportStore(tmp_path / "evaluations")
        assert store.golden(from_file["golden"]["sha256"]) == path.read_bytes()
        assert in_memory["golden"]["sha256"] == "" and [
            p.name for p in (tmp_path / "evaluations" / "golden_dataset").iterdir()
        ] == [f"{from_file['golden']['sha256']}.jsonl"]

    def test_only_limits_the_run(self, tmp_path):
        db = open_db(tmp_path, mode="dense")
        try:
            report = evaluate(db, Golden(questions=QUESTIONS), only=["vectrixdb.dense.own"])
        finally:
            db.close()
        assert [s["key"] for s in report["setups"]] == ["vectrixdb.dense.own"]

    def test_no_labelled_questions_is_refused(self, tmp_path):
        db = open_db(tmp_path, mode="dense")
        try:
            with pytest.raises(ValueError, match="no labelled questions"):
                evaluate(db, [Question("anything")])
        finally:
            db.close()

    def test_a_setup_that_fails_is_reported_and_never_picked(self, tmp_path):
        db = open_db(tmp_path, mode="dense")
        try:
            broken = dict(
                setups_of(Target(db))[-1],
                key="broken",
                search={"mode": "dense", "vectors": "nowhere"},
            )
            out = run_setup(db, broken, QUESTIONS)
        finally:
            db.close()
        assert out["ranks"] == [] and out["error"].startswith("ConfigurationError")
        report = build_report([out], GOLDEN, created=0)
        assert report["picks"]["finds_the_most"] is None

    def test_a_question_that_fails_twice_fails_the_setup(self):
        class Flaky:
            def __init__(self):
                self.calls = 0

            def search(self, query, limit=10, **search):
                self.calls += 1
                if query.startswith("Can I"):
                    raise TimeoutError("the service took too long")
                return []

        out = run_setup(Flaky(), {"key": "k", "search": {"mode": "dense"}}, QUESTIONS)
        assert out["error"] == "q1: TimeoutError: the service took too long" and out["ranks"] == []

    def test_the_search_cache_is_kept_out_of_the_timing_and_put_back(self, tmp_path):
        db = open_db(tmp_path, mode="dense")
        try:
            cache = db._collection._cache
            seen = []
            original = db.search

            def watching(*args, **kwargs):
                seen.append(db._collection._cache)
                return original(*args, **kwargs)

            db.search = watching
            run_setup(db, setups_of(Target(db))[1], QUESTIONS)
            assert all(c is None for c in seen) and db._collection._cache is cache
        finally:
            db.close()


SPEC = {
    "endpoint": "https://aoai.example.test",
    "deployment": "text-embedding-3-small",
    "dimensions": 3,
}


def theirs(texts):
    """The Azure OpenAI deployment: the same words, weighed the other way round."""
    return [[float(x) for x in row] for row in embed(texts)[:, ::-1]]


def fake_service():
    pytest.importorskip("azure.search.documents")
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fake_azure_search import FakeIndexClient

    return FakeIndexClient(vectorize=lambda text: theirs([text])[0])


def azure_handle(
    monkeypatch, fake, path, *, semantic=False, embeddings="vectrixdb", fill=False, **options
):
    """A handle on the fake service, with its own store object, the way another process opens the index."""
    from vectrixdb import Vectrix
    from vectrixdb.core.database import VectrixDB
    from vectrixdb.core.storage import StorageBackend, StorageConfig
    from vectrixdb.core.storage_azure import AzureSearchStorage

    two = embeddings != "vectrixdb"
    storage = AzureSearchStorage(
        StorageConfig(
            backend=StorageBackend.AZURE_SEARCH,
            azure_search_index_prefix="t",
            azure_search_semantic=semantic,
            azure_search_embeddings=embeddings,
            azure_search_vectorizer=SPEC if two else None,
            azure_search_embed_fn=theirs if two else None,
        ),
        index_client=fake,
        client_factory=fake.get_search_client,
    )
    storage.connect()
    monkeypatch.setattr("vectrixdb.core.database.create_storage", lambda config: storage)
    path.mkdir(parents=True, exist_ok=True)
    backend = VectrixDB.with_azure_search("https://svc.search.windows.net", key="k")
    db = Vectrix(
        "docs",
        storage_backend=backend,
        path=str(path),
        mode="hybrid",
        embed_fn=embed,
        dimension=3,
        **options,
    )
    if fill:
        for doc_id, text in DOCS.items():
            db.add_document(text, doc_id=doc_id)
    return db


class TestAStoreOpenedToSearch:
    """A function that only searches opens the index somebody else filled, and
    a handle opened that way holds nothing locally. It still gets every setup:
    its hybrid search asks the service for every candidate (it once asked for
    one, since the local count is nothing), its keyword search is the
    service's own, and Azure's semantic ranker is chosen per search."""

    def test_a_fresh_handle_gets_every_candidate_and_the_services_keyword_search(
        self, tmp_path, monkeypatch
    ):
        fake = fake_service()
        writer = azure_handle(monkeypatch, fake, tmp_path / "a", fill=True)
        reader = azure_handle(monkeypatch, fake, tmp_path / "b")
        try:
            assert reader.count() == 0, "the fresh handle holds nothing locally"
            assert (
                len(reader.search("loan payment fees", limit=20, mode="hybrid", rerank=False)) == 3
            )
            report = evaluate(Target(reader, name="Azure AI Search"), QUESTIONS)
        finally:
            reader.close()
            writer.close()
        by_method = {s["method"]: s for s in report["setups"]}
        assert set(by_method) == {"keyword", "dense", "hybrid", "hybrid_reranked"}
        assert {s["engine"] for s in report["setups"]} == {"Azure AI Search"} and all(
            not s["error"] for s in report["setups"]
        )
        assert by_method["keyword"]["ranks"] == [1, 1, 1], (
            "the service's keyword search found every answer first"
        )

    def test_one_index_gives_every_combination(self, tmp_path, monkeypatch):
        """Two sets of vectors and a semantic configuration: fourteen setups from
        one index, reranked by the service, by the cross-encoder, and by neither."""
        fake = fake_service()
        writer = azure_handle(
            monkeypatch, fake, tmp_path / "a", semantic=True, embeddings="both", fill=True
        )
        # Opened with the ranker off, as a function that only searches might:
        # the index keeps its configuration, and the ranker is still a setup.
        reader = azure_handle(
            monkeypatch, fake, tmp_path / "b", embeddings="both", reranker=WordOverlapReranker()
        )
        try:
            setups = setups_of(Target(reader, name="Azure AI Search"))
            report = evaluate(Target(reader, name="Azure AI Search"), QUESTIONS)
        finally:
            reader.close()
            writer.close()
        found = sorted((s["method"], s["search"].get("vectors") or "") for s in setups)
        assert found == sorted(
            [("keyword", ""), ("keyword_semantic", "")]
            + [
                (m, v)
                for m in ("dense", "hybrid", "hybrid_reranked", "hybrid_semantic")
                for v in ("vectrixdb", "azure", "both")
            ]
        )
        assert len(report["setups"]) == 14
        assert [s["error"] for s in report["setups"] if s["error"]] == []
        semantic = [
            s for s in report["setups"] if s["method"] in ("keyword_semantic", "hybrid_semantic")
        ]
        assert len(semantic) == 4 and all(s["summary"]["found"]["10"] == 1.0 for s in semantic)
        assert all(s["search"]["rerank"] == "semantic" for s in semantic)

    def test_expected_documents_are_looked_up_in_the_service(self, tmp_path, monkeypatch):
        fake = fake_service()
        writer = azure_handle(monkeypatch, fake, tmp_path / "a", fill=True)
        reader = azure_handle(monkeypatch, fake, tmp_path / "b")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", MissingDocumentsWarning)
                assert missing_documents(Target(reader, name="Azure AI Search"), QUESTIONS) == {}, (
                    "all there, though none of it is here"
                )
            with pytest.warns(
                MissingDocumentsWarning,
                match="Azure AI Search has no document called retired-policy",
            ):
                gone = missing_documents(
                    Target(reader, name="Azure AI Search"), QUESTIONS + [RETIRED]
                )
        finally:
            reader.close()
            writer.close()
        assert gone == {"Azure AI Search": {"documents": ["retired-policy"], "questions": ["q4"]}}


class TestMissingDocuments:
    """A question whose document was removed, renamed or never added is a miss
    on every setup. It is found before any searching, warned about and kept
    with the run, and the question stays in, so the scores say what searching
    that index really finds."""

    def test_a_document_the_collection_does_not_hold_is_warned_about_and_kept_with_the_run(
        self, tmp_path
    ):
        db = open_db(tmp_path, mode="dense")
        try:
            with pytest.warns(
                MissingDocumentsWarning,
                match=r"VectrixDB has no document called retired-policy, so 1 question \(q4\) cannot be found by any setup",
            ):
                report = evaluate(db, QUESTIONS + [RETIRED])
        finally:
            db.close()
        assert report["missing"] == {
            "VectrixDB": {"documents": ["retired-policy"], "questions": ["q4"]}
        }
        assert all(len(s["ranks"]) == 4 and s["ranks"][3] is None for s in report["setups"]), (
            "the question is still scored"
        )

    def test_everything_there_warns_nothing(self, tmp_path):
        db = open_db(tmp_path, mode="dense")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", MissingDocumentsWarning)
                report = evaluate(db, QUESTIONS)
        finally:
            db.close()
        assert report["missing"] == {}

    def test_a_question_with_another_document_there_can_still_be_found(self, tmp_path):
        db = open_db(tmp_path, mode="dense")
        both = Question("Can I defer a payment?", expected=["deferment", "old-deferment"], id="q5")
        try:
            with pytest.warns(MissingDocumentsWarning) as caught:
                gone = missing_documents(db, [both])
        finally:
            db.close()
        assert gone == {"VectrixDB": {"documents": ["old-deferment"], "questions": []}}
        assert str(caught[0].message) == (
            "VectrixDB has no document called old-deferment. Correct the expected id in the golden file, or add the document."
        )

    def test_chunks_under_other_ids_are_found_by_their_document(self, tmp_path):
        db = open_db(tmp_path, mode="dense")
        try:
            db.add(
                ["A memo on travel allowances for field staff."],
                ids=["memo-7f3a"],
                metadata=[{"_vx_doc": "travel-memo"}],
            )
            with warnings.catch_warnings():
                warnings.simplefilter("error", MissingDocumentsWarning)
                assert (
                    missing_documents(
                        db, [Question("Travel allowances?", expected=["travel-memo"], id="q6")]
                    )
                    == {}
                )
        finally:
            db.close()

    def test_by_chunk_looks_up_the_chunks_themselves(self, tmp_path):
        db = open_db(tmp_path, mode="dense")
        try:
            with pytest.warns(MissingDocumentsWarning):
                gone = missing_documents(
                    db,
                    [Question("Late fees?", expected=["deferment:0", "deferment:9"], id="c1")],
                    by="chunk",
                )
        finally:
            db.close()
        assert gone == {"VectrixDB": {"documents": ["deferment:9"], "questions": []}}

    def test_a_check_that_cannot_run_says_so_and_the_run_goes_on(self):
        class Unreachable:
            def get(self, ids):
                raise ConnectionError("the service did not answer")

        with pytest.warns(
            MissingDocumentsWarning,
            match="Could not check which expected documents Remote holds: ConnectionError",
        ):
            gone = missing_documents(Target(Unreachable(), name="Remote"), QUESTIONS)
        assert gone == {"Remote": {"error": "ConnectionError: the service did not answer"}}

    def test_a_long_list_is_cut_short(self):
        from vectrixdb.evaluation import _missing_message

        text = _missing_message(
            "VectrixDB", {"documents": [f"d{i}" for i in range(8)], "questions": ["q1", "q2"]}
        )
        assert text == (
            "VectrixDB has no document called d0, d1, d2, d3, d4 and 3 more, so 2 questions (q1 and q2) cannot be found "
            "by any setup, and every score is lower for it. Correct the expected ids in the golden file, or add the documents."
        )
