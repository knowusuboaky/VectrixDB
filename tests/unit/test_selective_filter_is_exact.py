"""A selective filter finds every document that matches, whatever the
approximate index happens to return.

Asked for every node, an HNSW index returns most of them and not all: on a
thousand near-duplicate vectors it came back with between 959 and 995. The
search window grows until it is the whole index, and that was where it
stopped, so a filter matching ten documents in a thousand could come back
with nine, or with none, and a short result looks like an ordinary outcome.
It showed up as one test failing about one full run in three.

Near-duplicate vectors make the index drop nodes every time, so this does
not wait for bad luck.
"""

from __future__ import annotations

import numpy as np
import pytest

from vectrixdb.core.database import VectrixDB
from vectrixdb.policy import Overlap, Policy

N = 1000


@pytest.fixture
def collection(tmp_path):
    rng = np.random.default_rng(7)
    base = rng.normal(size=64).astype(np.float32)
    vectors = (base + 0.02 * rng.normal(size=(N, 64))).astype(np.float32)
    database = VectrixDB(str(tmp_path))
    coll = database.create_collection(name="near", dimension=64, metric="cosine")
    coll.add(
        ids=[f"d{i}" for i in range(N)],
        vectors=vectors,
        metadata=[
            {"rare": i % 100 == 0, "client_id": "acme" if i % 100 == 0 else "zeta"}
            for i in range(N)
        ],
        texts=[f"record {i}" for i in range(N)],
    )
    yield coll, vectors
    database.close()


def test_the_index_really_does_drop_nodes(collection):
    coll, vectors = collection
    found = coll._index.search(vectors[0], N)
    assert len(set(found.keys.flatten().tolist())) < N, (
        "if this ever returns all of them the tests below prove nothing"
    )


def test_every_matching_document_is_found(collection):
    coll, vectors = collection
    expected = {f"d{i}" for i in range(0, N, 100)}
    for seed in range(12):
        hits = coll.search(
            vectors[(seed * 83) % N], limit=10, filter={"rare": True}, use_cache=False
        )
        assert {r.id for r in hits.results} == expected, (
            f"query {seed} found {len(hits.results)} of 10"
        )
        scores = [r.score for r in hits.results]
        assert scores == sorted(scores, reverse=True), "and they are still in order"


def test_a_policy_is_as_exact_and_its_counts_are_not_inflated_by_widening(tmp_path):
    from vectrixdb import Vectrix
    from vectrixdb.audit import DENY, MemorySink

    rng = np.random.default_rng(11)
    base = rng.normal(size=64).astype(np.float32)
    table = {
        f"record {i}": (base + 0.02 * rng.normal(size=64)).astype(np.float32) for i in range(N)
    }
    sink = MemorySink(query_key=b"k", on_failure=DENY)
    db = Vectrix(
        "walled",
        path=str(tmp_path),
        dimension=64,
        embedding_cache=False,
        embed_fn=lambda texts: np.vstack([table[t] for t in texts]),
        policy=Policy([Overlap("client_id", "clients")]),
        on_retrieval=sink,
    )
    try:
        db.add(
            list(table),
            ids=[f"d{i}" for i in range(N)],
            metadata=[{"client_id": "acme" if i % 100 == 0 else "zeta"} for i in range(N)],
        )
        hits = db.as_principal({"clients": ["acme"]}).search("record 3", limit=10, mode="dense")
        assert {h.id for h in hits} == {f"d{i}" for i in range(0, N, 100)}
        record = sink.records[-1]
        assert record.candidates_examined <= N, (
            "a widened search counted the same candidates more than once"
        )
        withheld = record.withheld_disclosable + record.withheld_undisclosable
        assert 0 < withheld <= N - 10, "more withheld than there are documents to withhold"
    finally:
        db.close()
