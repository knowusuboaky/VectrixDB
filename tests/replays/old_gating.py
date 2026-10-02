"""Restore the old `if self.hierarchy:` gate around the global/hybrid searchers.

The original built the two hierarchy-backed searchers only when the hierarchy
was truthy, so a graph with no communities got neither. This reproduces that by
clearing them again after the (fixed) rebuild whenever the hierarchy is absent
or empty.
"""

from vectrixdb.core.graphrag.pipeline import GraphRAGPipeline

_fixed = GraphRAGPipeline._rebuild_searchers


def _gated(self):
    _fixed(self)
    if not self.hierarchy or not self.hierarchy.total_communities:
        self.global_searcher = None
        self.hybrid_searcher = None


def pytest_configure(config):
    GraphRAGPipeline._rebuild_searchers = _gated
