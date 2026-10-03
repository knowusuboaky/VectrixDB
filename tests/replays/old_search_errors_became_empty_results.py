"""Restore searches that printed the error and answered normally.

``vector_search`` returned [] and ``hybrid_search`` quietly downgraded to a
dense-only search, so a connection failure was indistinguishable from a
collection with nothing in it.
"""

from vectrixdb.core.storage import LakebaseStorage

_fixed_vector = LakebaseStorage.vector_search
_fixed_hybrid = LakebaseStorage.hybrid_search


def _old_vector_search(self, collection, query_vector, limit=10):
    try:
        return _fixed_vector(self, collection, query_vector, limit)
    except Exception as e:
        print(f"Vector search error: {e}")
        return []


def _old_hybrid_search(self, collection, dense_query, sparse_query, limit=10):
    try:
        return _fixed_hybrid(self, collection, dense_query, sparse_query, limit)
    except Exception as e:
        print(f"Hybrid search error: {e}")
        return self.vector_search(collection, dense_query, limit)


def pytest_configure(config):
    LakebaseStorage.vector_search = _old_vector_search
    LakebaseStorage.hybrid_search = _old_hybrid_search
