"""Restore the sparse path calling keyword_search without the principal.

The dense and hybrid paths passed it down; this one did not. Nothing leaked,
because `keyword_search` refuses a policied collection handed no principal,
so the failure was closed. What it cost was the mode: `mode="sparse"` raised
`PrincipalRequired` at a caller who had supplied a principal, and no test
noticed because every policy test took the default mode.

Which is the useful lesson in it. A gate that is right in one place and
missing in another is not caught by testing the gate; it is caught by running
the same assertion down every path that reaches it.

    python scripts/replay.py tests/replays/old_sparse_search_dropped_the_principal.py \\
        tests/unit/test_policy.py
"""

from vectrixdb.easy import Vectrix


def _old_sparse_search(self, query, limit, filter=None, explain=False):
    results = self._collection.keyword_search(query_text=query, limit=limit, filter=filter)
    out = []
    for r in results.results:
        item = {"id": r.id, "score": r.score, "metadata": r.metadata}
        if explain:
            item["explain"] = {"bm25": r.score}
        out.append(item)
    return out


def pytest_configure(config):
    Vectrix._sparse_search = _old_sparse_search
