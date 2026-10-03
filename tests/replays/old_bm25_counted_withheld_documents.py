"""Restore BM25 statistics taken over the whole index.

The subtlest leak in the entitlement work, because nothing about it looks
like a leak. The right documents came back, and only the right documents.
What carried was the arithmetic: BM25 weights a term by how many documents
contain it, counted over everything in the index, so a document the principal
was refused changed the score of a document they were allowed.

Two consequences, and the second is the one that makes hiding the score
useless. A visible document scored 0.1131 in the index that held the walled
client and 0.1823 in the one that had never heard of it. And with a pile of
withheld documents about covenants, the visible covenant document fell below
the visible escrow document, so the *order* differed too, which rrf then
fused into hybrid.

Under this replay the statistics come from the whole index again, whatever
subset is being scored.
"""

from math import log

from vectrixdb.core.collection import TextIndex


def _old_search(self, query, limit=10, doc_ids=None, statistics_over_subset=False):
    query_tokens = self._tokenize(query)
    if not query_tokens:
        return []

    scores = {}
    original_tokens = self._tokenize_raw(query)
    all_query_tokens = list(set(query_tokens) | set(original_tokens))

    for token in all_query_tokens:
        if token not in self._inverted_index:
            continue

        # The whole index, whoever is asking.
        df = len(self._inverted_index[token])
        idf = log(1.0 + (self._doc_count - df + 0.5) / (df + 0.5))

        for doc_id in self._inverted_index[token]:
            if doc_ids is not None and doc_id not in doc_ids:
                continue
            doc = self._docs[doc_id]
            tf = doc["term_freq"].get(token, 0)
            if tf == 0:
                continue
            numerator = tf * (self.k1 + 1)
            denominator = tf + self.k1 * (
                1 - self.b + self.b * doc["length"] / (self._avg_doc_length or 1.0)
            )
            scores[doc_id] = scores.get(doc_id, 0) + idf * numerator / denominator

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]


def pytest_configure(config):
    TextIndex.search = _old_search
