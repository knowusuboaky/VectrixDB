"""An in-memory stand-in for the opensearch-py client the OpenSearch backend uses.

It implements the subset of ``OpenSearch`` that ``OpenSearchStorage`` calls,
with the service's observable behaviour where the contract depends on it:
documents get an auto-generated ``_id`` and carry the caller's id in the body
(Serverless refuses custom ids, which is why the backend does this); ``get``
is by ``_id``; ``search`` understands the five query shapes the backend
sends (``term``, ``match``, ``match_all``, ``knn`` and ``bool``), plus
``size``, ``from``, ``_source: False`` and scrolling; ``bulk`` takes the
action-then-document pairs; ``count`` counts; the ``indices`` namespace
creates, checks, deletes and lists. A missing index raises the same
``NotFoundError`` the real client raises, with ``status_code`` 404.

Scoring is not OpenSearch's (BM25 and HNSW there; token overlap and exact
cosine here) but every ordering property the contract asserts holds.
"""

from __future__ import annotations

import itertools
import math
import re
from typing import Any, Dict, List, Optional

_TOKEN = re.compile(r"\w+")


class NotFoundError(Exception):
    """Shaped like opensearchpy.exceptions.NotFoundError."""

    def __init__(self, message: str = "not found") -> None:
        super().__init__(message)
        self.status_code = 404
        self.info = {"status": 404}


class _Indices:
    def __init__(self, client: "FakeOpenSearch") -> None:
        self._c = client

    def exists(self, index: str) -> bool:
        return index in self._c._indices

    def create(self, index: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if index in self._c._indices:
            raise RuntimeError(f"index {index} already exists")
        self._c._indices[index] = {"docs": {}, "mapping": body or {}}
        return {"acknowledged": True, "index": index}

    def delete(self, index: str) -> Dict[str, Any]:
        if index not in self._c._indices:
            raise NotFoundError(f"no such index {index}")
        del self._c._indices[index]
        return {"acknowledged": True}

    def get_alias(self, index: str = "*") -> Dict[str, Any]:
        pattern = "^" + re.escape(index).replace("\\*", ".*") + "$"
        return {name: {"aliases": {}} for name in self._c._indices if re.match(pattern, name)}


class FakeOpenSearch:
    """Enough of ``opensearchpy.OpenSearch`` for the backend, in memory."""

    def __init__(self) -> None:
        self._indices: Dict[str, Dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self._scrolls: Dict[str, List[Dict[str, Any]]] = {}
        self.indices = _Indices(self)
        #: How a cosine distance becomes a score. OpenSearch documented
        #: 1 / (1 + d) for nmslib and faiss through 2.17 ("reciprocal") and
        #: (2 - d) / 2 for Lucene then and for every engine from 2.19 ("half").
        self.cosine_score = "reciprocal"

    # -- helpers ------------------------------------------------------------

    def _docs(self, index: str) -> Dict[str, Dict[str, Any]]:
        if index not in self._indices:
            raise NotFoundError(f"no such index {index}")
        return self._indices[index]["docs"]

    def _new_id(self) -> str:
        return f"os{next(self._ids):06d}"

    # -- writes -------------------------------------------------------------

    def index(
        self, index: str, body: Dict[str, Any], id: Optional[str] = None, **_: Any
    ) -> Dict[str, Any]:
        if index not in self._indices:
            # The real service creates an index on first write.
            self._indices[index] = {"docs": {}, "mapping": {}}
        doc_id = id or self._new_id()
        self._indices[index]["docs"][doc_id] = dict(body)
        return {"_id": doc_id, "result": "created"}

    def bulk(self, body: List[Dict[str, Any]], **_: Any) -> Dict[str, Any]:
        items = []
        pending_action: Optional[Dict[str, Any]] = None
        for line in body:
            if pending_action is None:
                pending_action = line
                continue
            ((op, meta),) = pending_action.items()
            index = meta["_index"]
            if op == "index":
                result = self.index(index, line, id=meta.get("_id"))
                items.append({"index": {"_index": index, "_id": result["_id"], "status": 201}})
            elif op == "delete":
                self._docs(index).pop(meta["_id"], None)
                items.append({"delete": {"_index": index, "_id": meta["_id"], "status": 200}})
            pending_action = None
        return {"errors": False, "items": items}

    def update(self, index: str, id: str, body: Dict[str, Any], **_: Any) -> Dict[str, Any]:
        docs = self._docs(index)
        if id not in docs:
            raise NotFoundError(f"no document {id}")
        docs[id].update(body.get("doc", {}))
        return {"_id": id, "result": "updated"}

    def delete(self, index: str, id: str, **_: Any) -> Dict[str, Any]:
        docs = self._docs(index)
        if id not in docs:
            raise NotFoundError(f"no document {id}")
        del docs[id]
        return {"_id": id, "result": "deleted"}

    # -- reads --------------------------------------------------------------

    def get(self, index: str, id: str, **_: Any) -> Dict[str, Any]:
        docs = self._docs(index)
        if id not in docs:
            raise NotFoundError(f"no document {id}")
        return {"_id": id, "_index": index, "found": True, "_source": dict(docs[id])}

    def count(self, index: str, body: Optional[Dict[str, Any]] = None, **_: Any) -> Dict[str, Any]:
        docs = self._docs(index)
        if body and "query" in body:
            return {"count": len(self._match(docs, body["query"]))}
        return {"count": len(docs)}

    def scroll(self, scroll_id: str, **_: Any) -> Dict[str, Any]:
        remaining = self._scrolls.get(scroll_id, [])
        page, rest = remaining[: self._scroll_size], remaining[self._scroll_size :]
        self._scrolls[scroll_id] = rest
        return {
            "_scroll_id": scroll_id,
            "hits": {"hits": page, "total": {"value": len(rest) + len(page)}},
        }

    def search(
        self,
        index: str,
        body: Optional[Dict[str, Any]] = None,
        scroll: Optional[str] = None,
        **_: Any,
    ) -> Dict[str, Any]:
        docs = self._docs(index)
        body = body or {}
        query = body.get("query", {"match_all": {}})
        scored = self._match(docs, query)
        size = int(body.get("size", 10))
        offset = int(body.get("from", 0))
        include_source = body.get("_source", True) is not False
        hits = [
            {
                "_index": index,
                "_id": doc_id,
                "_score": score,
                **({"_source": dict(docs[doc_id])} if include_source else {}),
            }
            for doc_id, score in scored
        ]
        if scroll:
            scroll_id = f"scroll{next(self._ids)}"
            self._scroll_size = size
            first, rest = hits[:size], hits[size:]
            self._scrolls[scroll_id] = rest
            return {"_scroll_id": scroll_id, "hits": {"hits": first, "total": {"value": len(hits)}}}
        return {"hits": {"hits": hits[offset : offset + size], "total": {"value": len(hits)}}}

    # -- query evaluation ---------------------------------------------------

    def _match(self, docs: Dict[str, Dict[str, Any]], query: Dict[str, Any]) -> List[tuple]:
        """(doc_id, score) pairs in rank order for one query clause."""
        if "match_all" in query:
            return [(doc_id, 1.0) for doc_id in docs]
        if "term" in query:
            ((field, value),) = query["term"].items()
            if isinstance(value, dict):
                value = value.get("value")
            # A keyword field can hold a list; a term matches any of its values.
            return [
                (doc_id, 1.0)
                for doc_id, d in docs.items()
                if (value in d[field] if isinstance(d.get(field), list) else d.get(field) == value)
            ]
        if "exists" in query:
            field = query["exists"]["field"]
            # A null is not indexed, so to OpenSearch it does not exist.
            return [(doc_id, 1.0) for doc_id, d in docs.items() if d.get(field) not in (None, [], "")]
        if "terms" in query:
            ((field, values),) = query["terms"].items()
            wanted = set(values)
            out = []
            for doc_id, d in docs.items():
                have = d.get(field)
                held = set(have) if isinstance(have, list) else ({have} if have is not None else set())
                if held & wanted:
                    out.append((doc_id, 1.0))
            return out
        if "range" in query:
            ((field, bounds),) = query["range"].items()
            tests = {"gt": lambda v, b: v > b, "gte": lambda v, b: v >= b, "lt": lambda v, b: v < b, "lte": lambda v, b: v <= b}
            return [
                (doc_id, 1.0)
                for doc_id, d in docs.items()
                if d.get(field) is not None and all(tests[op](d[field], bound) for op, bound in bounds.items())
            ]
        if "match" in query:
            ((field, spec),) = query["match"].items()
            text = spec["query"] if isinstance(spec, dict) else spec
            wanted = {t.lower() for t in _TOKEN.findall(str(text))}
            out = []
            for doc_id, d in docs.items():
                words = [t.lower() for t in _TOKEN.findall(str(d.get(field) or ""))]
                overlap = sum(1 for w in words if w in wanted)
                if overlap and words:
                    out.append((doc_id, overlap / math.sqrt(len(words))))
            return sorted(out, key=lambda x: (-x[1], x[0]))
        if "knn" in query:
            ((field, spec),) = query["knn"].items()
            qv = [float(x) for x in spec["vector"]]
            k = int(spec.get("k", 10))
            if spec.get("filter") is not None:
                # lucene and faiss filter while they search: the k nearest of what passes.
                allowed = {doc_id for doc_id, _ in self._match(docs, spec["filter"])}
                docs = {doc_id: d for doc_id, d in docs.items() if doc_id in allowed}
            qn = math.sqrt(sum(x * x for x in qv)) or 1.0
            out = []
            for doc_id, d in docs.items():
                v = d.get(field)
                if not v:
                    continue
                dn = math.sqrt(sum(float(x) * float(x) for x in v)) or 1.0
                cos = sum(a * float(b) for a, b in zip(qv, v)) / (qn * dn)
                distance = 1.0 - cos
                formula = getattr(self, "cosine_score", "reciprocal")
                out.append((doc_id, (2.0 - distance) / 2.0 if formula == "half" else 1.0 / (1.0 + distance)))
            return sorted(out, key=lambda x: (-x[1], x[0]))[:k]
        if "bool" in query:
            clauses = query["bool"]
            must = clauses.get("must", [])
            if isinstance(must, dict):
                must = [must]
            ids: Optional[set] = None
            scores: Dict[str, float] = {}
            for clause in must:
                matched = dict(self._match(docs, clause))
                ids = set(matched) if ids is None else ids & set(matched)
                for doc_id, score in matched.items():
                    scores[doc_id] = scores.get(doc_id, 0.0) + score
            for clause in clauses.get("filter", []):
                matched_ids = {doc_id for doc_id, _ in self._match(docs, clause)}
                ids = matched_ids if ids is None else ids & matched_ids
            should_ids: set = set()
            for clause in clauses.get("should", []):
                for doc_id, score in self._match(docs, clause):
                    scores[doc_id] = scores.get(doc_id, 0.0) + score
                    should_ids.add(doc_id)
            if clauses.get("minimum_should_match"):
                ids = should_ids if ids is None else ids & should_ids
            if ids is None:
                ids = set(scores) if scores else set(docs)
            for clause in clauses.get("must_not", []):
                ids = ids - {doc_id for doc_id, _ in self._match(docs, clause)}
            return sorted(((i, scores.get(i, 0.0)) for i in ids), key=lambda x: (-x[1], x[0]))
        raise ValueError(f"fake OpenSearch cannot evaluate query {list(query)!r}")
