"""An in-memory stand-in for the two Azure AI Search clients the backend uses.

It implements the subset of ``SearchIndexClient`` and ``SearchClient`` that
``AzureSearchStorage`` calls, with the service's observable behaviour where
it matters for the storage contract: keys are exact strings, documents are
whole records, ``search`` takes ``search_text`` plus optional
``vector_queries``, a ``filter`` in OData, ``select``, ``top``, ``skip`` and
``order_by``, and returns dicts carrying ``@search.score``. Deleting a
document that does not exist succeeds, as it does on the service.

Scoring is not Azure's (BM25 and HNSW there; token overlap and exact cosine
here) but the ordering properties the contract asserts hold either way.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Optional

_TOKEN = re.compile(r"\w+")


class FakeIndex:
    def __init__(
        self,
        name: str,
        fields: List[Any],
        vectorizers: Optional[List[Any]] = None,
        semantic_search: Any = None,
    ) -> None:
        self.name = name
        self.fields = fields
        self.vectorizers = list(vectorizers or [])
        # The service keeps what the last definition said, so an update that
        # leaves the semantic configuration out takes it away.
        self.semantic_search = semantic_search
        self.docs: Dict[str, Dict[str, Any]] = {}


class FakeIndexClient:
    def __init__(self, vectorize: Optional[Any] = None) -> None:
        self.indexes: Dict[str, FakeIndex] = {}
        # What the service's vectorizer would do with a question: text to a
        # vector. The real one calls an Azure OpenAI deployment.
        self.vectorize = vectorize
        self.vectorized: List[str] = []

    def create_or_update_index(self, index):
        existing = self.indexes.get(index.name)
        vectorizers = list(
            getattr(getattr(index, "vector_search", None), "vectorizers", None) or []
        )
        semantic = getattr(index, "semantic_search", None)
        if existing is None:
            self.indexes[index.name] = FakeIndex(
                index.name, list(index.fields), vectorizers, semantic
            )
        else:
            existing.fields = list(index.fields)
            existing.vectorizers = vectorizers
            existing.semantic_search = semantic
        return index

    def get_index(self, name: str):
        if name not in self.indexes:
            from azure.core.exceptions import ResourceNotFoundError

            raise ResourceNotFoundError(f"no index {name}")
        return self.indexes[name]

    def delete_index(self, name: str) -> None:
        self.indexes.pop(name, None)

    def list_index_names(self) -> Iterable[str]:
        return list(self.indexes)

    def get_search_client(self, name: str) -> "FakeSearchClient":
        return FakeSearchClient(self, name)


_TOK = re.compile(
    r"\s*(?:(\()|(\))|('(?:[^']|'')*')|(-?\d+(?:\.\d+)?)|([A-Za-z_][A-Za-z0-9_./:]*))"
)


def _tokens(expr: str) -> List[Any]:
    out: List[Any] = []
    pos = 0
    while pos < len(expr):
        m = _TOK.match(expr, pos)
        if not m or m.end() == pos:
            if expr[pos:].strip() == "":
                break
            if expr[pos] == ",":
                out.append(("punct", ","))
                pos += 1
                continue
            raise ValueError(f"fake filter cannot parse {expr!r} at {pos}")
        pos = m.end()
        if m.group(1) or m.group(2):
            out.append(("punct", m.group(1) or m.group(2)))
        elif m.group(3):
            out.append(("str", m.group(3)[1:-1].replace("''", "'")))
        elif m.group(4):
            out.append(("num", float(m.group(4))))
        else:
            out.append(("word", m.group(5)))
    return out


class _Parser:
    """The OData the backend emits: and/or/not, parentheses, comparisons with
    string, number, boolean and null literals, ``search.in`` and ``/any``."""

    def __init__(
        self, expr: str, doc: Dict[str, Any], bound: Optional[Dict[str, Any]] = None
    ) -> None:
        self.toks = _tokens(expr)
        self.i = 0
        self.doc = doc
        self.bound = bound or {}

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self, kind=None, value=None):
        tok = self.peek()
        if tok[0] is None or (kind and tok[0] != kind) or (value is not None and tok[1] != value):
            raise ValueError(f"fake filter: expected {kind} {value!r}, got {tok!r}")
        self.i += 1
        return tok

    def expr(self) -> bool:
        left = self.term()
        while self.peek() == ("word", "or"):
            self.take()
            right = self.term()
            left = left or right
        return left

    def term(self) -> bool:
        left = self.factor()
        while self.peek() == ("word", "and"):
            self.take()
            right = self.factor()
            left = left and right
        return left

    def factor(self) -> bool:
        tok = self.peek()
        if tok == ("word", "not"):
            self.take()
            return not self.factor()
        if tok == ("punct", "("):
            self.take()
            v = self.expr()
            self.take("punct", ")")
            return v
        return self.atom()

    def value_of(self, name: str) -> Any:
        if name in self.bound:
            return self.bound[name]
        return self.doc.get(name)

    def literal(self) -> Any:
        kind, val = self.take()
        if kind == "str" or kind == "num":
            return val
        if kind == "word":
            return {"true": True, "false": False, "null": None}[val]
        raise ValueError("fake filter: bad literal")

    def atom(self) -> bool:
        kind, val = self.take("word")
        if val == "search.in":
            self.take("punct", "(")
            field = self.take("word")[1]
            self.take("punct", ",")
            values = self.take("str")[1]
            self.take("punct", ",")
            sep = self.take("str")[1]
            self.take("punct", ")")
            v = self.value_of(field)
            return v is not None and str(v) in values.split(sep)
        if "/" in val:
            field, fn = val.split("/", 1)
            assert fn == "any"
            self.take("punct", "(")
            items = self.value_of(field) or []
            if self.peek() == ("punct", ")"):
                self.take()
                return bool(items)
            var = self.take("word")[1].rstrip(":")
            if self.peek() == ("punct", ":"):
                self.take()
            start = self.i
            result = False
            for item in items:
                self.i = start
                sub = _Parser("", self.doc, {**self.bound, var: item})
                sub.toks, sub.i = self.toks, self.i
                if sub.expr():
                    result = True
                self.i = sub.i
            if not items:
                depth = 0
                while True:
                    k, v2 = self.take()
                    if (k, v2) == ("punct", "("):
                        depth += 1
                    elif (k, v2) == ("punct", ")"):
                        if depth == 0:
                            self.i -= 1
                            break
                        depth -= 1
            self.take("punct", ")")
            return result
        field = val
        op = self.take("word")[1]
        rhs = self.literal()
        lhs = self.value_of(field)
        if op == "eq":
            return lhs == rhs
        if op == "ne":
            return lhs != rhs
        if lhs is None or rhs is None:
            return False
        try:
            return {"gt": lhs > rhs, "ge": lhs >= rhs, "lt": lhs < rhs, "le": lhs <= rhs}[op]
        except TypeError:
            return False


def _match_filter(expr: Optional[str], doc: Dict[str, Any]) -> bool:
    if not expr:
        return True
    p = _Parser(expr, doc)
    result = p.expr()
    if p.i != len(p.toks):
        raise ValueError(f"fake filter cannot parse {expr!r}: trailing {p.toks[p.i :]}")
    return result


class FakeSearchClient:
    def __init__(self, index_client: FakeIndexClient, index_name: str) -> None:
        self._ic = index_client
        self._name = index_name

    @property
    def _docs(self) -> Dict[str, Dict[str, Any]]:
        return self._ic.get_index(self._name).docs

    # -- writes -------------------------------------------------------------

    def upload_documents(self, documents: List[Dict[str, Any]]):
        for d in documents:
            self._docs[d["id"]] = dict(d)
        return [type("R", (), {"succeeded": True, "key": d["id"]})() for d in documents]

    merge_or_upload_documents = upload_documents

    def merge_documents(self, documents: List[Dict[str, Any]]):
        from azure.core.exceptions import ResourceNotFoundError

        for d in documents:
            if d["id"] not in self._docs:
                raise ResourceNotFoundError(f"no document {d['id']}")
            self._docs[d["id"]].update(d)
        return [type("R", (), {"succeeded": True, "key": d["id"]})() for d in documents]

    def delete_documents(self, documents: List[Dict[str, Any]]):
        for d in documents:
            self._docs.pop(d["id"], None)
        return [type("R", (), {"succeeded": True, "key": d["id"]})() for d in documents]

    # -- reads --------------------------------------------------------------

    def get_document(self, key: str, selected_fields: Optional[List[str]] = None) -> Dict[str, Any]:
        from azure.core.exceptions import ResourceNotFoundError

        doc = self._docs.get(key)
        if doc is None:
            raise ResourceNotFoundError(f"no document {key}")
        if selected_fields:
            return {k: doc.get(k) for k in selected_fields}
        return dict(doc)

    def get_document_count(self) -> int:
        return len(self._docs)

    def search(
        self,
        search_text: Optional[str] = None,
        *,
        vector_queries: Optional[List[Any]] = None,
        filter: Optional[str] = None,
        select: Optional[List[str]] = None,
        top: Optional[int] = None,
        skip: int = 0,
        order_by: Optional[List[str]] = None,
        query_type: Optional[str] = None,
        semantic_configuration_name: Optional[str] = None,
        include_total_count: bool = False,
    ):
        if query_type == "semantic":
            # The service refuses a semantic query the index has no configuration for.
            semantic = self._ic.get_index(self._name).semantic_search
            names = [c.name for c in (getattr(semantic, "configurations", None) or [])]
            if semantic_configuration_name not in names:
                raise ValueError(
                    f"semantic configuration {semantic_configuration_name!r} is not defined on {self._name}"
                )
        docs = [d for d in self._docs.values() if _match_filter(filter, d)]
        scores: Dict[str, float] = {}
        text_scores: Dict[str, float] = {}
        vec_scores: Dict[str, float] = {}
        if search_text and search_text != "*":
            q = {t.lower() for t in _TOKEN.findall(search_text)}
            for d in docs:
                words = [t.lower() for t in _TOKEN.findall(d.get("text_content") or "")]
                if not words:
                    continue
                overlap = sum(1 for w in words if w in q)
                if overlap:
                    text_scores[d["id"]] = overlap / math.sqrt(len(words))
        vector_rankings: List[Any] = []
        if vector_queries:
            for vq in vector_queries:
                field = vq.fields
                if getattr(vq, "vector", None) is None:
                    # A question sent as words: the index's vectorizer embeds it.
                    index = self._ic.get_index(self._name)
                    if not index.vectorizers or self._ic.vectorize is None:
                        raise ValueError(f"no vectorizer is defined for the field {field}")
                    self._ic.vectorized.append(vq.text)
                    qv = list(self._ic.vectorize(vq.text))
                else:
                    qv = list(vq.vector)
                vec_scores = {}
                qn = math.sqrt(sum(x * x for x in qv)) or 1.0
                for d in docs:
                    v = d.get(field)
                    if not v:
                        continue
                    dn = math.sqrt(sum(x * x for x in v)) or 1.0
                    cos = sum(a * b for a, b in zip(qv, v)) / (qn * dn)
                    vec_scores[d["id"]] = max(vec_scores.get(d["id"], -1.0), cos)
                k = getattr(vq, "k_nearest_neighbors", None) or len(docs)
                keep = sorted(vec_scores, key=vec_scores.get, reverse=True)[:k]
                vec_scores = {i: vec_scores[i] for i in keep}
                vector_rankings.append((vec_scores, float(getattr(vq, "weight", None) or 1.0)))
            if len(vector_rankings) > 1:
                # Several vectors: the service fuses them by rank, weighted.
                fused: Dict[str, float] = {}
                for ranking, weight in vector_rankings:
                    for rank, doc_id in enumerate(sorted(ranking, key=ranking.get, reverse=True)):
                        fused[doc_id] = fused.get(doc_id, 0.0) + weight / (60 + rank + 1)
                vec_scores = fused
        if text_scores and vec_scores:
            # Azure fuses hybrid results with RRF; so do we.
            for ranking in (text_scores, vec_scores):
                for rank, doc_id in enumerate(sorted(ranking, key=ranking.get, reverse=True)):
                    scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (60 + rank + 1)
        elif text_scores:
            scores = dict(text_scores)
        elif vec_scores and len(vector_rankings) == 1:
            # One vector: Azure reports 1 / (1 + cosine_distance), not the
            # cosine ("Vector relevance and ranking", Microsoft Learn).
            scores = {doc_id: 1.0 / (1.0 + (1.0 - cos)) for doc_id, cos in vec_scores.items()}
        elif vec_scores:
            scores = dict(vec_scores)
        else:
            scores = {d["id"]: 1.0 for d in docs}

        ordered = list(scores)
        if order_by:
            field = order_by[0].split()[0]
            reverse = order_by[0].lower().endswith(" desc")
            ordered.sort(key=lambda i: str(self._docs[i].get(field)), reverse=reverse)
        else:
            ordered.sort(key=lambda i: (-scores[i], i))
        ordered = ordered[skip:]
        if top is not None:
            ordered = ordered[:top]
        out = []
        for doc_id in ordered:
            doc = self._docs[doc_id]
            row = {k: doc.get(k) for k in select} if select else dict(doc)
            row["@search.score"] = scores[doc_id]
            if query_type == "semantic":
                # The service's reranker scores 0 to 4 and keeps the order here.
                row["@search.reranker_score"] = round(
                    4.0 * scores[doc_id] / max(scores.values()), 4
                )
            out.append(row)
        return _Paged(out, len(scores) if include_total_count else None)


class _Paged(list):
    def __init__(self, rows, total):
        super().__init__(rows)
        self._total = total

    def get_count(self):
        return self._total
