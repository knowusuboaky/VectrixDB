"""More than one dense model on a local collection: each gets an index of its
own, a write reaches all of them, and a search fuses them by rank or asks
for one by name.
"""

from __future__ import annotations

import numpy as np
import pytest

from vectrixdb.exceptions import ConfigurationError

A = "Alpha memo about the covenant schedule."
B = "Beta memo about the repayment holiday."
C = "Gamma memo about both of them."
QUESTION = "which memo"

OURS = {A: [1, 0, 0, 0], B: [0, 0, 1, 0], C: [0.8, 0.6, 0, 0], QUESTION: [1, 0, 0, 0]}
OTHER = {A: [0, 0, 1, 0, 0, 0], B: [0, 1, 0, 0, 0, 0], C: [0, 0.8, 0.6, 0, 0, 0], QUESTION: [0, 1, 0, 0, 0, 0]}


def ours(texts):
    return np.asarray([OURS.get(t, [0, 0, 0, 1]) for t in texts], dtype=np.float32)


def other(texts):
    return np.asarray([OTHER.get(t, [0, 0, 0, 0, 0, 1]) for t in texts], dtype=np.float32)


def open_db(tmp_path, **options):
    from vectrixdb import Vectrix

    options.setdefault("dense_model", [None, ("large", other, 6)])
    return Vectrix("memos", path=str(tmp_path / "db"), embed_fn=ours, dimension=4, **options)


def test_a_write_reaches_every_index_and_a_search_can_ask_for_one(tmp_path):
    db = open_db(tmp_path)
    try:
        db.add([A, B, C], ids=["a", "b", "c"], metadata=[{"n": 1}, {"n": 2}, {"n": 3}])
        assert db.vector_names() == ["own", "large"]
        assert db.search(QUESTION, limit=1, vectors="own").top.id == "a"
        assert db.search(QUESTION, limit=1, vectors="large").top.id == "b"
        assert db.search(QUESTION, limit=1, vectors="large").top.metadata["n"] == 2
        fused = db.search(QUESTION, limit=3, explain=True)
        assert {h.id for h in fused} == {"a", "b", "c"} and fused.items[2].id == "c"
        assert fused.items[2].explain["vector_ranks"] == {"own": 2, "large": 2}
        assert [h.id for h in db.search(QUESTION, limit=3, vectors="both")] == [h.id for h in fused]
    finally:
        db.close()


def test_a_delete_a_clear_and_a_reopen(tmp_path):
    db = open_db(tmp_path)
    db.add([A, B, C], ids=["a", "b", "c"])
    db.delete("b")
    assert [h.id for h in db.search(QUESTION, limit=3, vectors="large")] == ["c", "a"]
    db.close()

    again = open_db(tmp_path)
    try:
        assert again.search(QUESTION, limit=1, vectors="large").top.id == "c"
        again.clear()
        assert again.search(QUESTION, limit=3).items == []
        again.add([B], ids=["b"])
        assert again.search(QUESTION, limit=1, vectors="large").top.id == "b"
    finally:
        again.close()


def test_the_list_is_part_of_the_collection(tmp_path):
    db = open_db(tmp_path)
    db.add([A], ids=["a"])
    db.close()
    from vectrixdb import Vectrix

    with pytest.raises(ConfigurationError, match="was built with named dense vectors \\['large'\\]; open it with the same"):
        Vectrix("memos", path=str(tmp_path / "db"), embed_fn=ours, dimension=4)
    with pytest.raises(ConfigurationError, match="was opened with \\['huge'\\]"):
        open_db(tmp_path, dense_model=[None, ("huge", other, 6)])


def test_a_budget_and_a_filter_still_apply(tmp_path):
    db = open_db(tmp_path)
    try:
        db.add([A, B, C], ids=["a", "b", "c"], metadata=[{"team": "x"}, {"team": "y"}, {"team": "x"}])
        assert {h.id for h in db.search(QUESTION, limit=3, filter={"team": "x"})} == {"a", "c"}
        tight = db.search(QUESTION, limit=3, token_budget=12)
        assert tight.truncated and len(tight) < 3
    finally:
        db.close()


def test_what_is_refused(tmp_path):
    from vectrixdb import Vectrix
    from vectrixdb.policy import Overlap, Policy

    with pytest.raises(ConfigurationError, match="not one of this collection's dense vectors: own, large"):
        db = open_db(tmp_path)
        try:
            db.add([A], ids=["a"])
            db.search(QUESTION, vectors="azure")
        finally:
            db.close()
    with pytest.raises(ConfigurationError, match="a list that starts with one"):
        Vectrix("x", path=str(tmp_path / "x"), dense_model=[])
    with pytest.raises(ConfigurationError, match="model name, or \\(label, embed_fn, dimension\\)"):
        Vectrix("y", path=str(tmp_path / "y"), embed_fn=ours, dimension=4, dense_model=[None, 42])
    with pytest.raises(ConfigurationError, match="cannot name a dense vector here"):
        Vectrix("z", path=str(tmp_path / "z"), embed_fn=ours, dimension=4, dense_model=[None, ("both", other, 6)])
    with pytest.raises(ConfigurationError, match="named dense vectors are not offered under one"):
        Vectrix("w", path=str(tmp_path / "w"), embed_fn=ours, dimension=4, dense_model=[None, ("large", other, 6)],
                policy=Policy([Overlap("client_id", "clients")]))
