"""Restore the early return on an empty graph, and the search() that raised."""

from vectrixdb.core.graphrag.pipeline import GraphRAGPipeline

_fixed_rebuild = GraphRAGPipeline._rebuild_searchers
_fixed_search = GraphRAGPipeline.search


def _old_rebuild(self):
    if self.graph.is_empty():
        return
    _fixed_rebuild(self)


def _old_search(self, query, query_vector=None, k=10, search_type=None):
    if not self._is_built:
        raise RuntimeError("Graph not built. Call add_documents() first.")
    if self.graph.is_empty():
        raise RuntimeError("Hybrid searcher not initialized")
    return _fixed_search(self, query, query_vector=query_vector, k=k, search_type=search_type)


def pytest_configure(config):
    GraphRAGPipeline._rebuild_searchers = _old_rebuild
    GraphRAGPipeline.search = _old_search
