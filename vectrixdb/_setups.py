"""Every way a collection handle can be searched, found by asking the handle.

A setup is one engine, one set of dense vectors and one search method: the
unit an evaluation times and ranks. What a handle can do is decided when it
is opened, not per search, so this module reads the handle rather than a
list of wishes:

* the mode it was opened with is a ceiling. A collection opened with
  ``mode="hybrid"`` can be searched dense, by keyword and hybrid, and never
  ultimate, so open it with ``mode="ultimate"`` to have every method;
* keyword search is the local BM25 index while that holds anything, and the
  store's own keyword search on a handle that holds nothing locally;
* Azure AI Search's semantic ranker is chosen per search, on an index that
  has a semantic configuration, so one index is searched with it, with MiniLM
  instead, and with neither. Keyword search there is the service's own;
* ColBERT and the graph are local, so a store-backed handle offers neither;
* the dense vectors are whatever the store or the collection holds: one, or
  two with each alone and both fused. A local collection's other models are
  searched by meaning alone, so on their own they are dense setups only.

Nothing here searches. :func:`vectrixdb.evaluation.evaluate` does, and a
setup that fails its first search is reported as one that could not run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["METHODS", "Target", "describe_target", "search_of", "setups_of"]


# ============================================================================
# SETTINGS: the methods, the levels and the engines
# ============================================================================
#
# Every search method a handle may offer, the levels they sit at, and the
# engines a handle may be backed by.

#: Method key: (label, how it ranks, the method's level in the mode ceiling).
METHODS: Dict[str, Tuple[str, str, int]] = {
    "keyword": ("Keyword", "BM25 over the words, no model", 1),
    "keyword_semantic": ("Keyword + semantic ranker", "Azure's semantic ranker", 1),
    "dense": ("Dense", "the vectors only", 1),
    "hybrid": ("Hybrid", "words and vectors fused by rank, not reranked", 2),
    "hybrid_reranked": ("Hybrid, reranked", "MiniLM, on this machine", 2),
    "hybrid_semantic": ("Hybrid + semantic ranker", "Azure's semantic ranker", 2),
    "hybrid_bedrock": ("Hybrid + Bedrock rerank", "Bedrock's reranker", 2),
    "ultimate": ("Ultimate", "ColBERT, then MiniLM", 3),
    "graph": ("Graph", "linked entities, then MiniLM", 4),
}

_LEVELS = {"dense": 1, "sparse": 1, "hybrid": 2, "ultimate": 3, "graph": 4}
_ENGINES = {
    "AzureSearchStorage": ("azure", "Azure AI Search", "Azure"),
    "OpenSearchStorage": ("opensearch", "OpenSearch", "OpenSearch"),
}


# ============================================================================
# A TARGET
# ============================================================================
#
# INPUT   a handle, and the name people will read for it
# OUTPUT  one handle to evaluate
#
# The unit an evaluation is given.


@dataclass
class Target:
    """One handle to evaluate, and the name people will read for it.

    ``name`` defaults to the engine's own name ("VectrixDB", "Azure AI
    Search"); give one when two targets share an engine, two indexes built
    with different settings, say. ``collection`` is the name the
    server knows this collection by, so the Evaluate page can open it in
    Search; it defaults to the handle's own name. ``models`` relabels a
    vector: ``{"vectrixdb": {"label": "Hugging Face", "name":
    "bge-large-en-v1.5", "kind": "hf"}}``.
    """

    db: Any
    name: Optional[str] = None
    collection: Optional[str] = None
    models: Optional[Dict[str, Dict[str, str]]] = None


# ============================================================================
# WHAT A HANDLE CAN DO: its engine, models, vectors and methods
# ============================================================================
#
# INPUT   the handle
# OUTPUT  its engine; its own dense model, as the pages name it; each vector
#         alone, then all of them fused; whether its text index and a semantic
#         configuration are there; the methods it can be searched with, and
#         the search() arguments for each
#
# Decided when the handle is opened, not per search, so an evaluation times
# what a user would get.


def _storage_of(db: Any) -> Any:
    backend = getattr(db, "storage_backend", None)
    if backend is None or not getattr(db, "_using_storage_backend", backend is not None):
        return None
    return getattr(backend, "_storage", backend)


def _engine(db: Any) -> Tuple[str, str, str]:
    storage = _storage_of(db)
    if storage is None:
        return ("vectrixdb", "VectrixDB", "VectrixDB")
    kind = type(storage).__name__
    if kind in _ENGINES:
        return _ENGINES[kind]
    plain = kind.replace("Storage", "") or kind
    return (plain.lower(), plain, plain)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "x"


def _own_model(db: Any) -> Dict[str, str]:
    """The collection's own dense model, as the pages name it."""
    kind_of = str(getattr(db, "model_type", "") or "")
    name = str(getattr(db, "embedding_model", None) or getattr(db, "model_name", None) or "")
    if kind_of == "embedded":
        return {
            "key": "builtin",
            "label": "Built in",
            "name": name or "bge-small-en-v1.5",
            "kind": "builtin",
        }
    if kind_of in ("fastembed", "sentence-transformers") or "/" in name:
        return {"key": "hf", "label": "Hugging Face", "name": name, "kind": "hf"}
    if name.startswith("azure-openai:"):
        return {
            "key": "azure-openai",
            "label": "Azure OpenAI",
            "name": name.split(":", 1)[1],
            "kind": "service",
        }
    return {
        "key": "own",
        "label": "Your model",
        "name": name if name not in ("", "custom") else "an embed_fn",
        "kind": "service",
    }


def _store_model(storage: Any, vector: str) -> Dict[str, str]:
    config = getattr(storage, "config", None)
    if vector == "azure":
        spec = getattr(config, "azure_search_vectorizer", None) or {}
        name = str(spec.get("model") or spec.get("deployment") or "an Azure OpenAI deployment")
        return {"key": "azure-openai", "label": "Azure OpenAI", "name": name, "kind": "service"}
    if vector == "bedrock":
        fn = getattr(config, "opensearch_embed_fn", None)
        name = str(getattr(fn, "model_id", None) or getattr(fn, "model", None) or "a Bedrock model")
        return {"key": "bedrock", "label": "Bedrock", "name": name, "kind": "service"}
    return {"key": _slug(vector), "label": vector, "name": vector, "kind": "service"}


def _vector_choices(
    db: Any, relabel: Optional[Dict[str, Dict[str, str]]]
) -> List[Tuple[Optional[str], List[Dict[str, str]]]]:
    """``(vectors=, models)`` pairs: each vector alone, then all of them fused."""
    relabel = relabel or {}
    storage = _storage_of(db)
    try:
        names = list(db.vector_names())
    except Exception:  # an older handle: one vector, its own
        names = []
    own = _own_model(db)

    def model(name: str) -> Dict[str, str]:
        if name in relabel:
            return {"key": _slug(relabel[name].get("label", name)), **relabel[name]}
        if name in ("vectrixdb", "own"):
            return own
        if storage is not None:
            return _store_model(storage, name)
        return {
            "key": _slug(name),
            "label": name,
            "name": name,
            "kind": "hf" if "/" in name or "-" in name else "service",
        }

    if len(names) <= 1:
        return [(None, [model(names[0] if names else "vectrixdb")])]
    choices: List[Tuple[Optional[str], List[Dict[str, str]]]] = [
        (name, [model(name)]) for name in names
    ]
    choices.append(("both", [model(name) for name in names]))
    return choices


def _text_indexed(db: Any) -> bool:
    try:
        index = db._collection._text_index
    except Exception:
        return False
    return index is not None and int(getattr(index, "_doc_count", 0) or 0) > 0


def _semantic_ready(db: Any, engine_kind: str) -> bool:
    """Whether this handle's Azure index has a semantic configuration to rank with."""
    if engine_kind != "azure":
        return False
    name_of = getattr(_storage_of(db), "semantic_name", None)
    try:
        return bool(callable(name_of) and name_of(str(getattr(db, "name", ""))))
    except Exception:
        return False


def _methods(db: Any, engine_kind: str) -> List[str]:
    level = _LEVELS.get(str(getattr(db, "default_mode", "dense") or "dense"), 1)
    storage = _storage_of(db)
    remote = engine_kind in ("azure", "opensearch")
    semantic = _semantic_ready(db, engine_kind)
    out: List[str] = []
    if _text_indexed(db) or (remote and hasattr(storage, "text_search")):
        out.append("keyword")
        if semantic:
            out.append("keyword_semantic")
    out.append("dense")
    if level >= 2 and (_text_indexed(db) or remote):
        out.append("hybrid")
        external = getattr(db, "_external_reranker", None)
        if external is not None:
            bedrock = (
                "bedrock" in (type(external).__name__ + str(getattr(external, "label", ""))).lower()
            )
            out.append("hybrid_bedrock" if bedrock else "hybrid_reranked")
        elif getattr(db, "reranker_model_name", None):
            out.append("hybrid_reranked")
        if semantic:
            out.append("hybrid_semantic")
    if level >= 3 and not remote and getattr(db, "late_interaction_model_name", None):
        out.append("ultimate")
    if level >= 4 and not remote:
        out.append("graph")
    return out


def _search_kwargs(method: str) -> Dict[str, Any]:
    """The ``search()`` arguments for a method. Every reranker is named, so a
    store opened with the semantic ranker on still runs the others as they are."""
    known: Dict[str, Dict[str, Any]] = {
        "keyword": {"mode": "sparse", "rerank": False},
        "keyword_semantic": {"mode": "sparse", "rerank": "semantic"},
        "dense": {"mode": "dense"},
        "hybrid": {"mode": "hybrid", "rerank": False},
        "hybrid_reranked": {"mode": "hybrid", "rerank": "cross-encoder"},
        "hybrid_bedrock": {"mode": "hybrid", "rerank": "cross-encoder"},
        "hybrid_semantic": {"mode": "hybrid", "rerank": "semantic"},
    }
    return dict(known.get(method) or {"mode": method})


def search_of(way: str) -> Dict[str, Any]:
    """The ``search()`` arguments for one way of searching, named the way a person pins one.

    ``<method>`` or ``<method>.<vectors>``: a method of :data:`METHODS`, and
    for any method but keyword search, which vectors, as ``vectors=`` names
    them, ``both`` for all of them fused. ``hybrid_semantic.both`` is
    ``{"mode": "hybrid", "rerank": "semantic", "vectors": "both"}``. No
    collection is named, so one pin reads the same in every collection.
    ValueError for a method it does not know, or vectors given to keyword
    search, which reads none.
    """
    method, _, vectors = str(way or "").strip().partition(".")
    if method not in METHODS:
        raise ValueError(
            f"{way!r} is no way of searching. The methods are {', '.join(METHODS)}, then .<vectors> for any but keyword"
        )
    if vectors and method in ("keyword", "keyword_semantic"):
        raise ValueError(
            f"{way!r}: keyword search reads the words and no vectors, so it is {method} alone"
        )
    search = _search_kwargs(method)
    if vectors:
        search["vectors"] = vectors
    return search


# ============================================================================
# DESCRIBING AND LISTING
# ============================================================================
#
# INPUT   a target
# OUTPUT  what a report says about it, its engine, what it holds and how it
#         was opened; every setup it can be searched with, as plain data
#
# A setup is one engine, one set of dense vectors and one search method: the
# unit an evaluation times and ranks.


def describe_target(target: Target) -> Dict[str, Any]:
    """What a report says about one target: its engine, what it holds, how it was opened.

    Two runs whose targets read the same were searching the same index, so
    a change here between runs is drawn on the run-by-run line.
    """
    db = target.db
    kind, engine, _short = _engine(db)
    try:
        count = int(db.count())
    except Exception:
        count = None
    return {
        "name": target.name or engine,
        "engine": engine,
        "engine_kind": kind,
        "collection": target.collection or getattr(db, "name", None),
        "chunks": count,
        "model": getattr(db, "embedding_model", None) or getattr(db, "model_name", None),
        "mode": getattr(db, "default_mode", None),
        "vectors": [c[0] for c in _vector_choices(db, target.models) if c[0]],
    }


def setups_of(target: Target) -> List[Dict[str, Any]]:
    """Every setup this target can be searched with, as plain data.

    Each is a dict a report keeps as it is: ``key``, the ``target`` it
    belongs to, the ``engine`` in full and ``engine_short``, the
    ``method`` and its ``method_label``, the ``ranker`` in words, the
    ``models`` its vectors come from, and ``search``, the keyword arguments
    that make ``db.search`` run it. Keyword search, with Azure's semantic
    ranker or without, is one setup whatever the vectors, since it reads
    none of them.
    """
    db = target.db
    kind, engine, short = _engine(db)
    name = target.name or engine
    # A local collection with a list of dense models searches its own index
    # in the method asked for, and each other model's index by meaning
    # alone. So another model on its own is a dense setup and nothing else,
    # and keyword search names its own index, or the others would be fused in.
    local_list = _storage_of(db) is None and bool(getattr(db, "_named", None))
    out: List[Dict[str, Any]] = []
    for method in _methods(db, kind):
        label, ranker, _level = METHODS[method]
        if method in ("keyword", "keyword_semantic"):
            choices: List[Tuple[Optional[str], List[Dict[str, str]]]] = [
                ("own" if local_list else None, [])
            ]
        else:
            choices = _vector_choices(db, target.models)
            if local_list and method != "dense":
                choices = [c for c in choices if c[0] in ("own", "both")]
        if method == "hybrid_reranked" and getattr(db, "_external_reranker", None) is not None:
            external = db._external_reranker
            ranker = str(getattr(external, "label", None) or type(external).__name__)
        for vectors, models in choices:
            search = _search_kwargs(method)
            if vectors is not None:
                search["vectors"] = vectors
            models_key = "+".join(sorted(m["key"] for m in models)) or "words"
            out.append(
                {
                    "key": f"{_slug(name)}.{method}.{models_key}",
                    "target": name,
                    "engine": engine,
                    "engine_short": short,
                    "engine_kind": kind,
                    "method": method,
                    "method_label": label,
                    "ranker": ranker,
                    "models": [dict(m) for m in models],
                    "search": search,
                }
            )
    return out
