# Score and relevance

Every search result has a `score`. It has always meant something different
depending on where it came from, and that is fine for what it is for, which is
putting results in order. It is no good for the other question people ask of
it: is this result any good?

| Where it came from | What `score` is | Range |
| --- | --- | --- |
| Dense, local index | Cosine similarity | -1 to 1 |
| Keyword | BM25 weight | 0 upward, no ceiling |
| Hybrid | Sum of reciprocal ranks, `0.5 / (60 + rank)` per list, with a lift for being in both | tops out near 0.018 |
| Azure AI Search, one vector | `1 / (1 + cosine_distance)` | 0.333 to 1 |
| OpenSearch, one vector | `1 / (1 + d)` or `(2 - d) / 2`, by version and engine | 0.333 to 1, or 0 to 1 |
| Either service, hybrid or two vectors | Fused ranks | tiny |

In a hybrid search the best result there can possibly be scores 0.018. That
reads as terrible, and it is the top of the scale. Worse, the top of a ranking
is the top whatever is in it: ask a collection of contracts about zebras and
the first result still scores 0.018.

## `relevance`

So every result also carries `relevance`, from 0 to 1, and `relevance_kind`,
which says what the number is.

| `relevance_kind` | What it is | When |
| --- | --- | --- |
| `reranker` | A cross-encoder read the question and the chunk together and judged the pair. Strict: a chunk on the right topic that does not answer the question scores low. | A rerank ran: `mode="hybrid"` and above in the library by default, Rerank in the dashboard, Azure's semantic ranker. |
| `similarity` | The cosine similarity, with anything below zero shown as zero. The same on every engine. | Dense and hybrid searches that did not rerank. |
| `relative` | The score as a share of the best hit. The top hit is always 1.0, which says it won and not that it is good. | Keyword searches, because BM25 has no ceiling. |
| `distance` | `1 / (1 + distance)`. Ordered and bounded, not comparable with a similarity. | A collection whose metric is not cosine. |
| None | There is no honest number. | A store whose scoring is not known here. |

`Result.similarity` is the cosine on its own whenever it is known, whatever
`relevance` turned out to be. A reranker's verdict and a similarity live on
different scales, so if your searches sometimes rerank and sometimes do not,
put your threshold on `similarity`.

`matched_by` says which search found the chunk: `["meaning"]`,
`["keywords"]` or both. Being found by meaning is being among the nearest with
some likeness at all, not merely being in a wide prefetch.

## What it is for

```python
from vectrixdb import Vectrix

db = Vectrix("handbook", path="./relevance_demo")
db.add([
    "Payment is due within thirty days of the invoice date.",
    "Interest accrues monthly on any overdue balance.",
])


def answer(question, floor=0.55):
    hits = list(db.search(question, mode="dense", limit=1))
    best = hits[0] if hits else None
    if best is None or (best.similarity or 0) < floor:
        return "I could not find that in the documents."
    return best.text


print(answer("When is payment due?"))   # the first sentence
print(answer("How do zebras sleep?"))   # still has a top result, and declines
db.close()
```

The right cut-off depends on the embedding model and on your documents.
Similarities from the bundled models bunch between about 0.5 and 0.9, so
measure it on your own questions rather than borrowing a number: see
[Measure retrieval](../how-to/measure-retrieval.md).

In a hybrid search the relevances need not run downhill. Keywords can lift a
chunk above one that is closer in meaning, and `matched_by` is what explains
it. The order is still the better order; the numbers are still the truth about
each chunk.

## Azure AI Search and OpenSearch

Both report a transform of the cosine, and both are turned back into it.

Azure documents `@search.score = 1 / (1 + cosine_distance)` and the way back,
and documents the semantic ranker's score as 4 (answers completely) to 0
(irrelevant), which becomes `reranker` on a 0 to 1 scale. A hybrid search and a
search over two vectors come back as fused ranks with no similarity in them, so
the store asks the collection's own vector one more question to get it. That is
one extra query; `VectrixDB.with_azure_search(..., relevance=False)` or
`azure_search_relevance=False` on the storage configuration saves it, and
`relevance` is then None for those searches.

OpenSearch documented `1 / (1 + d)` for nmslib and faiss and `(2 - d) / 2` for
Lucene through 2.17, and `(2 - d) / 2` for every engine from 2.19. A managed or
serverless cluster may not say which it is, so the store works it out: the
first hit whose stored vector it can read gives the true cosine, and only one
of the two formulas turns that hit's score into it. Until a hit settles it
there is no number. `opensearch_score_formula="half"` or `"reciprocal"` states
it outright.

Where a store holds two vectors per chunk, the collection's own and the
service's, `relevance` is the collection's own, because a threshold is set
against one model and not a blend of two, and `relevances` carries both:
`{"vectrixdb": 0.71, "bedrock": 0.64}`.
