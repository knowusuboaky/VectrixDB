"""Restore the OpenSearch store that handed the collection the engine's raw score.

The collection reads a store's third value as ``1 - score``, the way the
Azure store hands it over, and turns it back. The OpenSearch store handed
``_score`` itself, so every served score came out as one minus the engine's,
a ``score_threshold`` kept the worst matches and dropped the best, and with
two vectors fused the scores ran uphill.
"""

from vectrixdb.core import relevance as _relevance
from vectrixdb.core.storage import OpenSearchStorage


def _old_knn_search(self, collection, field_name, vector, limit, filter):
    body = {"size": limit, "query": self._knn(vector, limit, filter, field_name)}
    result = self._client.search(index=self._index_name(collection), body=body)
    hits = result["hits"]["hits"]
    formula = self._score_formula(hits, field_name, vector)
    out = []
    for hit in hits:
        data = self._from_doc(hit["_source"], include_vector=False)
        found = _relevance.from_opensearch_cosine_score(hit.get("_score"), formula)
        if found is not None:
            data["_vx_relevance"] = found
        out.append((hit["_source"].get("id", hit["_id"]), data, hit["_score"]))
    return out


def pytest_configure(config):
    OpenSearchStorage._knn_search = _old_knn_search
