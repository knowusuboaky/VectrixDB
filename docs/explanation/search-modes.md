# Search modes

VectrixDB offers four modes. They differ in what signal they use, and therefore
in what kind of question they answer well.

## Dense

Embeds the query with the bundled model and finds the nearest vectors by cosine
distance. This is semantic search: it matches meaning, not words. "How do I stop
the server?" will find a passage about shutting down a service even with no words
in common.

Its weakness is exact tokens. Product codes, error numbers and rare proper nouns
embed poorly, because the model never learned them.

## Hybrid

Runs dense search alongside sparse lexical scoring, combines the two rankings
with Reciprocal Rank Fusion, and then reranks the top of that list with the
bundled cross-encoder. RRF merges by rank rather than score, which avoids
having to calibrate two incomparable scales against each other; the reranker
then reads query and document together rather than comparing two
independently-computed vectors.

The reranker is part of hybrid, not a step above it. This page used to say
otherwise and describe hybrid as fusion alone.

This is the sensible default for a mixed corpus, and it covers the case dense
search misses: an exact identifier appearing verbatim.

## Ultimate

Hybrid, plus a late-interaction pass with the bundled ColBERT model before the
reranker. Late interaction scores every query token against every document
token instead of comparing two single vectors, which catches matches that a
one-vector-per-document comparison flattens away.

It is the most expensive mode by a wide margin, and on the set below it buys
nothing over hybrid. Reach for it only with evidence from your own corpus.

## Graph (GraphRAG)

Extracts entities and relationships into a knowledge graph, clusters it into
communities, and searches over that structure. Local search answers questions
about specific entities; global search answers questions about the corpus as a
whole, of the "what are the main themes" sort, which plain retrieval handles
badly because no single chunk contains the answer.

Indexing is much more expensive, since it needs an extraction pass over the
corpus first.

## Choosing

| Question looks like | Mode |
|---|---|
| Conceptual, paraphrased | Dense |
| Mixed prose and identifiers | Hybrid |
| Needs the best few results | Ultimate |
| About themes, or how things relate | Graph |

## Which mode earns its cost

Measured by `scripts/compare_modes.py`: 48 hand-written documents and
questions phrased to share few words with their answers, scored by mean
reciprocal rank and recall at 1. Small on purpose, and regenerated rather
than typed.

| Mode | MRR@10 | Recall@1 | ms per query |
| --- | ---: | ---: | ---: |
| dense | 0.951 | 0.92 | 9 |
| hybrid, rerank=False | 0.758 | 0.62 | 12 |
| hybrid | 0.927 | 0.90 | 150 |
| ultimate | 0.927 | 0.90 | 527 |

On this set dense ranks best and costs least. Per query, hybrid without the reranker costs 1.3 times what dense does, hybrid 16 times and ultimate 57 times.

The two hybrid rows differ only by the cross-encoder, which hybrid and
ultimate run by default: on the same candidates it raises MRR by 0.169 and takes
92% of hybrid's time. `search(rerank=False)` turns it off.

That is one small set of short documents, so it argues for measuring on
your own corpus rather than for any one mode everywhere: the case hybrid
exists for, an exact identifier in prose, is exactly what forty-eight
hand-written questions are least likely to contain.

## Languages

The sparse half of hybrid search is BM25 over the text index, and BM25 has no model: the only language-dependent part is how text is cut into terms. Words in any script are indexed as words, accents included, and scripts written without spaces (Chinese, Japanese, Korean, Thai, Lao, Khmer, Myanmar) are cut into overlapping character bigrams, which is the standard dictionary-free segmentation and needs nothing downloaded.

What does depend on the language is stemming and stopwords, and both are English. So say what the text is:

```python
from vectrixdb import Vectrix

db = Vectrix("vertraege", mode="hybrid", text_language="de")
```

`"en"`, the default, stems ASCII words with Porter and drops English stopwords. Anything else turns both off, because Porter on German turns "garantie" into "garanti" and the English stopword list drops nothing useful in French; unstemmed BM25 in the document's own language beats both. The setting is persisted with the collection, so a collection cut for German is never reopened with English stemming.

The dense half is a separate question: the models in the wheel are English, and other languages need the multilingual dense model, fetched once. See [Run without a network](../how-to/offline.md).

## More than one dense model

`dense_model` takes a list. The first is the collection's own model, as ever; each of the others gets an index of its own beside the collection, a write reaches all of them, and a search fuses them by rank or asks for one by name.

```python
from vectrixdb import Vectrix

db = Vectrix("papers", dense_model=["bge-small", "your-second-model"])
db.search("covenant thresholds")                                    # both, fused by rank
db.search("covenant thresholds", vectors="your-second-model")       # one of them
db.search("covenant thresholds", vectors="own", explain=True)
```

An entry can also be `(label, embed_fn, dimension)`, which is how an embedder you call yourself, an OpenAI-compatible endpoint say, becomes the second vector. The gain comes from models that cover different ground, a multilingual one beside an English one, or one for code beside one for prose. Two general English models gain less, though not nothing: on LongMemEval, conversation memory with both bundled-size models finds 0.814 of the evidence where they find 0.721 and 0.797 alone; see [Conversation memory](../how-to/conversation-memory.md#measured). It doubles the embedding cost, the disk and most of the query time, so it is off unless asked for, and it is something to measure on your own questions before keeping.

`image_embedder=` adds one more, `image`, holding each figure's picture and asked for by name: see [Figures](../how-to/extract-keep-index.md#found-by-what-it-looks-like).

The list is part of the collection: open it again with the same list, so writes keep reaching every index. It is not offered on a collection that carries an entitlement policy, because results fused from two indexes would not be the results the decision record names. On Azure AI Search, `embeddings="both"` fuses inside the service under one filter, and that is the supported way to have two vectors under a policy; see [storage backends](../how-to/storage-backends.md).
