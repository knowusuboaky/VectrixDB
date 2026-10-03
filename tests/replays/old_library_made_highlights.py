"""Restore the library's keyword calls that made highlights nothing read.

Hybrid and ultimate fetch ten times the limit from the keyword index, and
every candidate came back with highlights: its whole text tokenized again,
then dropped, because the library never reads them. On SciFact that was nine
tenths of a hybrid query, 1.1 s of 1.2 s at limit=100.
"""

from vectrixdb.core.collection import Collection

_real = Collection.keyword_search


def _old_keyword_search(
    self, query_text, limit=10, filter=None, include_highlights=True, principal=None
):
    return _real(
        self,
        query_text,
        limit=limit,
        filter=filter,
        include_highlights=True,
        principal=principal,
    )


def pytest_configure(config):
    Collection.keyword_search = _old_keyword_search
