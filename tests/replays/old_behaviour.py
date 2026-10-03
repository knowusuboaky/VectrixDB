"""Pytest plugin restoring the pre-fix stubs, to prove the new tests catch them."""

from vectrixdb.core.graphrag.graph.storage import GraphStorage


def pytest_configure(config):
    GraphStorage.save_hierarchy = lambda self, hierarchy: None
    GraphStorage.load_hierarchy = lambda self: None
