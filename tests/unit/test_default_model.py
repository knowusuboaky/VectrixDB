"""The bundled English default is bge-small-en-v1.5; collections from before
the switch still open with the model they were built with."""

from __future__ import annotations

import warnings

import numpy as np

from vectrixdb import Vectrix
from vectrixdb.models import DenseEmbedder


def test_default_is_bge_small(tmp_path):
    db = Vectrix("d", path=str(tmp_path))
    assert db.model_name == "vectrixdb/bge-small-en-v1.5"
    db.add(["Basalt forms when lava cools quickly.", "Sourdough is leavened by wild yeast."])
    assert db.search("volcanic rock", limit=1).top.text.startswith("Basalt")
    assert db.embedding_model == "vectrixdb/bge-small-en-v1.5"
    assert db.model.pooling == "cls"
    db.close()


def test_e5_stays_available_by_alias(tmp_path):
    db = Vectrix("e", path=str(tmp_path), dense_model="e5-small")
    assert db.model_name == "dense_en"
    db.add(["hello world"])
    assert db.count() == 1 and db.model.pooling == "mean"
    db.close()


def test_a_collection_without_a_recorded_model_opens_with_the_old_default(tmp_path):
    # Build with the pre-2.2 model and erase the record, as a 2.1 collection has none.
    db = Vectrix("old", path=str(tmp_path), dense_model="e5-small")
    db.add(["Basalt forms when lava cools quickly.", "Sourdough is leavened by wild yeast."])
    db._collection.set_meta("embedding_model", None)
    db.close()

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # no mismatch warning: the model is inferred
        again = Vectrix("old", path=str(tmp_path))
    assert again.model_name == "vectrixdb/all-MiniLM-L6-v2", "the legacy label, e5 underneath"
    assert again.embedding_model == "vectrixdb/all-MiniLM-L6-v2", "and now recorded"
    assert again.search("volcanic rock", limit=1).top.text.startswith("Basalt")
    # The migration: re-embed with the new default.
    again.model_name = Vectrix._default_model
    again._model = None
    assert again.reembed() == 2
    assert again.embedding_model == Vectrix._default_model
    assert again.search("bread yeast", limit=1).top.text.startswith("Sourdough")
    again.close()


def test_an_empty_collection_gets_the_new_default(tmp_path):
    db = Vectrix("empty", path=str(tmp_path))
    assert db.model_name == Vectrix._default_model
    db.close()


def test_pooling_comes_from_the_model_config():
    assert DenseEmbedder(language="en").pooling == "cls"
    assert DenseEmbedder(model="e5-small").pooling == "mean"


def test_fast_padding_is_the_default_and_the_slow_path_still_exists():
    e = DenseEmbedder(language="en")
    fast = e.embed(["a short note"])
    slow = e.embed(["a short note"], pad_to_longest=False)
    assert fast.shape == slow.shape == (1, 384)
    assert float(np.dot(fast[0], slow[0])) > 0.98
