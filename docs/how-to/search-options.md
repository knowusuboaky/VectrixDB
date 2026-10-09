# Search options

`Vectrix.search` takes a question and answers with `Results`: the chunks that
match, best first, each with its text, its relevance and where it came from.
Everything else is an option. This page is every option, every field of what
comes back, and what the REST API offers for each.

```python
from vectrixdb import Vectrix

db = Vectrix("handbook", mode="hybrid")
db.add(
    [
        "Refunds are paid within ten working days of the request.",
        "A refund goes back to the card that paid.",
        "Parental leave is eighteen weeks, on full pay.",
        "Expenses are claimed within thirty days.",
    ],
    metadata=[
        {"team": "finance", "year": 2026},
        {"team": "finance", "year": 2025},
        {"team": "people", "year": 2026},
        {"team": "finance", "year": 2026},
    ],
)

found = db.search("how long do refunds take", limit=3)
top = found.top
print(top.text, top.relevance, top.relevance_kind, top.readable_citation)
```

## Every option

| Option | Default | What it does |
| --- | --- | --- |
| `query` | | The question, in words |
| `limit` | `10` | The most results to return; at least 1 |
| `mode` | the collection's | `"dense"`, `"sparse"`, `"hybrid"`, `"ultimate"` or `"graph"`. A search may use the collection's mode or one below it: dense and sparse, then hybrid, ultimate and graph. See [Search modes](../explanation/search-modes.md) |
| `rerank` | how the mode does it | In dense and sparse modes nothing reranks unless asked: `"mmr"`, `"exact"` or `"cross-encoder"`. Hybrid, ultimate and graph rerank with the cross-encoder; `False` turns that off. `"semantic"` is Azure AI Search's semantic ranker, for sparse and hybrid searches on a collection stored there |
| `filter` | none | Keeps results whose metadata matches: `{"team": "finance"}`. The whole grammar is in [Filters](../reference/filters.md) |
| `diversity` | `0.7` | For `rerank="mmr"`, how far to trade relevance for variety, 0 to 1 |
| `token_budget` | none | Stops adding results once their text would cost more than this many tokens. The top result always comes back |
| `score_gap` | none | Drops results scoring below this fraction of the top score, above 0 and up to 1. Cosine scores sit in a narrow band, so 0.9 is typical there; keyword and graph scores want about 0.2 |
| `explain` | `False` | Puts the parts of each score in `Result.explain`: dense similarity, BM25, the fused score, ColBERT, the reranker, any graph boost |
| `fusion` | `"rrf"` | How hybrid and ultimate modes combine meaning and words: `"rrf"`, by rank, or `"weighted"`, scores blended by `alpha` |
| `alpha` | `0.5` | With `fusion="weighted"`, the share of meaning, 0 to 1. Raise it when keyword matches drag results |
| `expand` | none | A function from the question to other phrasings. Each is searched and the lists are fused by rank. Pass any model call; nothing is bundled |
| `hyde` | none | A function from the question to a made-up answer, which is embedded in place of the question. Pass any model call; nothing is bundled |
| `parents` | `False` | For a document added with `parent_size=`, returns the section each hit belongs to instead of the chunk, once a section. A chunk with no section comes back as it is |
| `vectors` | every one | On a collection with more than one dense index, which answers: a model's name, `"own"`, `"image"`, or `"all"`. On a store that holds two vectors, `"vectrixdb"`, `"azure"` or `"both"` |
| `homes` | the usual routing | On a collection kept on Azure AI Search or OpenSearch: `"store"`, `"local"` or `"both"`. See [Two homes](storage-backends.md#two-homes) |

Who is searching is not an option. On a collection with an entitlement policy,
a search reads as a principal, given once to a view of the collection:
`db.as_principal({"roles": ["analyst"]}).search(...)`. See
[Restrict what a search can see](entitlements.md).

```python
db.search("refunds", mode="dense")
db.search("refunds", mode="sparse")
db.search("refunds", rerank=False)
db.search("refunds", filter={"team": "finance", "year": {"$gte": 2026}})

cut = db.search("refunds", token_budget=20)
print(cut.truncated, cut.cut_count, cut.token_estimate)

strong = db.search("refunds", score_gap=0.9)
why = db.search("refunds", explain=True).top.explain
print(sorted(why))
```

### Small chunks that find, sections that answer

```python
policies = Vectrix("policies")
policies.add_document(
    "# Refunds\n\nRefunds are paid within ten working days. A refund goes back to the card that paid. "
    "A card that has expired is refunded by bank transfer instead.\n\n"
    "# Leave\n\nParental leave is eighteen weeks on full pay. It can be taken in one block or in two.\n",
    doc_id="policies.md",
    chunk_size=100,
    overlap=0,
    parent_size=1000,
)
print(policies.search("what if my card expired").top.text)                 # the chunk that matched
print(policies.search("what if my card expired", parents=True).top.text)   # its whole section
```

Several hits in one section come back as that section once, at the best of
their scores.

## What comes back

`Results` is a list of `Result`, best first. Iterate it, index it, take its
length.

| `Results` | What it is |
| --- | --- |
| `.top` | The best result, or `None` when nothing matched |
| `.texts`, `.ids`, `.scores` | Lists of each, in order |
| `.citations()` | Every distinct citation among the results, best first |
| `.sources(max_chars=None)` | The results as a block for a prompt, one `[citation]: text` a result |
| `.figures(top=None)` | The results that are figures, for a model that can see |
| `.query`, `.mode`, `.time_ms` | What was asked, in which mode, and how long it took |
| `.truncated`, `.cut_count` | Whether `token_budget` cut anything, and how many results |
| `.token_estimate`, `.token_budget` | What the results cost in tokens, and the budget asked for. The estimate is there without a budget too |
| `.degraded` | Why a search answered with less than it was asked for: graph mode from vectors alone, a store that did not answer under `homes="both"`, an index slowed by deleted vectors. `None` otherwise |
| `.decision_id` | Set when a search under a policy wrote a decision record: the id of that record in the audit trail |
| `.to_dict()`, `Results.from_dict()` | Everything as plain data, and back |
| `.to_pandas()`, `.to_polars()` | A table of id, score, text and metadata; needs pandas or polars |

| `Result` | What it is |
| --- | --- |
| `.id`, `.text`, `.metadata` | The chunk |
| `.score` | What orders the list. Its scale depends on the mode and the engine, so do not put a threshold on it |
| `.relevance` | How well it matches, 0 to 1, the same on every mode and engine, or `None` when there is no honest number. Put thresholds here |
| `.relevance_kind` | What `relevance` is: `"reranker"`, `"similarity"`, `"relative"` or `"distance"` |
| `.similarity` | The cosine similarity on its own, whenever it is known |
| `.matched_by` | What found it: `["meaning"]`, `["keywords"]` or both |
| `.relevances` | The similarity from each dense vector, on a store that holds two |
| `.citation` | What to write in square brackets to cite it: the citation `add_document` stamped, or one built from its source, page and heading, or its id |
| `.readable_citation` | The same place as a person reads it: `report.pdf, pp. 39-40`, `slide 3`, `11:05` in a recording |
| `.explain` | The parts of the score, with `explain=True` |

```python
for r in found:
    print(r.id, r.relevance, r.matched_by, r.citation)

if not found or (found.top.relevance or 0) < 0.45:
    print("I could not find that.")
```

[Score and relevance](../explanation/relevance.md) says what each kind of
relevance means and where to put a threshold.

## Over REST

A server's search routes take a question in words and embed it for you. Each
answers with `results`, each with `id`, `score`, `text`, `metadata`,
`relevance`, `relevance_kind` and `matched_by`.

| In the library | Over REST |
| --- | --- |
| `mode="dense"` | `POST /api/v1/collections/{name}/text-search` |
| `mode="sparse"` | `POST /api/v1/collections/{name}/keyword-search` |
| `mode="hybrid"` | `POST /api/v1/collections/{name}/text-hybrid-search`, on a collection made with its text index |
| `rerank="cross-encoder"` | `"rerank": true` on `text-search` |
| `query` | `"query_text"` |
| `limit` | `"limit"`, from 1 to 1000 |
| `filter` | `"filter"`, the same grammar |
| none | `"score_threshold"` on `text-search`: drops results scoring below it |
| none | `"include_highlights"` on `keyword-search`: the matched words in each result |
| none | `?snippet=N` on any search: each result's text cut to `N` characters, 40 to 2000 |
| `.decision_id` | `decision_id`, when a policy judged the search |
| `as_principal(...)` | Who the request comes from: its key, or the person's token, judged by the server |

`ultimate` and `graph` modes, `token_budget`, `score_gap`, `explain`, `fusion`,
`alpha`, `expand`, `hyde`, `parents`, `vectors` and `homes` are the library's
alone. Every route and body is in the [REST reference](../reference/rest-api.md),
and [Call a server from your code](clients.md) has clients that call these
routes for you.
