# Tips and tricks

The things that are easy to miss, in the library and in the dashboard, each
with the page that has the rest. Nothing here needs a setting you do not
already have.

## Search

**Let your own questions pick the mode.** Hybrid is the default because it
wins more often than it loses, not because it always wins. A golden file of
thirty questions and one command settle it for your documents:

```bash
vectrixdb golden template docs --out golden.jsonl -n 30
vectrixdb evaluate docs --golden golden.jsonl
```

It names three setups, the one that finds the most, the fastest near it, and
the fastest overall, and switches nothing. See
[Evaluate every setup](evaluate-setups.md).

**Threshold on `relevance`, not `score`.** A hybrid score of 0.018 is the top
of its scale; a cosine of 0.8 is not. `relevance` is 0 to 1 and means the same
in every mode and on every engine, so "is this good enough to answer from" has
one number. `relevance_kind` says what it came from, and `matched_by` says
whether meaning, keywords or both found the chunk. See
[Score and relevance](../explanation/relevance.md).

```python
from vectrixdb import Vectrix

db = Vectrix("support")
db.add([
    "Refunds for a cancelled order are made to the card that paid, within ten days.",
    "Wire transfers above 10,000 need a second person to approve them.",
])
hits = db.search("refund window for a cancelled order")
answerable = [h for h in hits if h.relevance and h.relevance >= 0.6]
```

**See why a result ranked where it did.** `explain=True` puts the parts of
each score on the result: the dense similarity, BM25, the fused score, ColBERT,
the reranker and any graph boost.

```python
top = db.search("wire transfer limits", explain=True).top
print(top.explain)
```

**Measure the reranker before keeping it.** Hybrid and ultimate mode rerank
with the bundled cross-encoder unless told not to, and it was trained on web
passages. On scientific claims it lowered nDCG@10 from 0.736 to 0.693; on
support tickets it may well raise it. `rerank=False` turns it off, and saves
most of the search time. The
[benchmarks](../explanation/benchmarks.md) have the numbers.

**Spend a token budget, not a result count.** `token_budget=` stops once the
text would cost more tokens than a model can read, always keeps the top result,
and says what it cut in `Results.truncated` and `Results.cut_count`.
`score_gap=` drops the long tail under a fraction of the top score, so a
specific question gets one strong answer rather than a padded list.

**Help a question the documents phrase differently.** `expand=` takes any
function from a question to other phrasings and fuses the runs; `hyde=` takes
one from a question to a made-up answer and searches with that instead. Bring
any LLM call; nothing is bundled.

**Return the section, not the fragment.** Add documents with `parent_size=`
and search with `parents=True`, and each hit comes back as the section around
it: small chunks find, big chunks answer.

## Ingest

**Keep the Markdown, so a new chunk size never means a new OCR bill.**
`Vectrix(..., keep_source=True)` keeps the text each document was read into.
`rechunk()` cuts the index again from it without calling an extractor, and on
an empty collection over the same store it rebuilds the index from the store
alone. See [Extract, keep, index](extract-keep-index.md).

**Cut on headings, and embed them.** `chunk="markdown"` keeps sections whole,
and `embed_heading=True` hands the model "Terms > Late fees: Interest accrues
monthly." while the chunk stores only the sentence. A chunk with its heading
path finds far more questions about its section.

**Write in bulk inside one save.** Every `add()` rewrites the index file.
Inside `with db.deferred_saves():` it is written once, at the end.

**Skip the near-duplicates.** `add_document(..., dedupe=0.9)` skips every
chunk whose MinHash similarity to text already stored is 0.9 or more, which is
what a mail archive or a set of templates is full of, and `last_add_report`
says what was skipped.

**Bring your own embeddings.** `embed_fn=` takes any function from texts to
vectors; `dense_model="openai:text-embedding-3-small"` takes any OpenAI
compatible endpoint, Ollama, vLLM and Text Embeddings Inference included. The
bundled model stays the offline default.

**Text that is not English needs two things.** The models in the wheel are
English: `vectrixdb download-models` fetches the multilingual dense model and
reranker once, about 230 MB, and `Vectrix(name, language="multi")` uses them.
And set `text_language=` on the collection, so keyword search stops stemming
its words as English. See [Search modes](../explanation/search-modes.md#languages).

**Rebuild when a fifth of the index is deleted rows.** Deletes leave
tombstones that every search still walks. A collection's health says when to
rebuild, past 20 percent, and `rebuild_index()` clears them.

## The server and the dashboard

**Check the settings before a start.** `vectrixdb check --env-file vectrixdb.env`
says what a start would refuse and names the setting a typo probably meant;
`vectrixdb check --template` prints every setting there is. See
[Deploy the server](deploy.md).

**Press `/` to search from anywhere in the dashboard.** The search page opens
with the cursor in the query.

**A chunk's text is one click, and one line in the access log.** Lists show
ids, sources and quality. **Show text** opens one chunk, and the log names it,
so the record says what somebody read and not only that they looked.

**Say who may search a collection on its Policy tab.** People by email, a
domain for everyone there, or security groups from the sign-in token narrowed
to a list of people: in one of the groups and on the list, both, because not
everyone in a group may read. Groups with no list are refused on save, with
"Add the people who may search. A group alone would let everyone in it
search." Anyone else is refused and the refusal is logged, `not_in_token` for
somebody in none of the groups and `not_on_list` for somebody in one and not
on the list. The list tag says both, as in "2 groups, 3 people". Guests see every collection's name
and size and never search. Guest browsing itself is `VECTRIXDB_GUESTS=on`, a
setting on the server, so one click cannot open a private server to the
internet.

**Mask identifiers for the people who only need to read.** The same card masks
email addresses, phone numbers and card numbers in everything people and
guests are shown of that collection, while a script with an API key gets the
text as stored. It lowers casual exposure and is not a guarantee: see
[Sign people in](sign-in.md#masking-identifiers).

**Give a script a key of its own.** An admin makes named keys under **API keys
for scripts** in the account menu, each with only the role it needs, reading,
searching or writing. A key is revoked on its own and named in the access log,
which a shared server key never is.

**A key for somebody else's app should be narrower than a role.** The same
form picks the collections it may reach and the day it stops working. A scoped
key reaches those collections and nothing else: another collection reads as
one that is not there, and a route that cuts across collections is refused.
Nobody has to remember to revoke a key that ends on its own. See
[Build an app on it](build-an-app.md).

**Give a key an allowance, and make keys from a script.**
`vectrixdb keys add handbook-bot --collection handbook --days 90 --per-minute 120`
makes one from the server's console, and `VECTRIXDB_KEY_REQUESTS_PER_MINUTE`
covers every key with no number of its own. One count, however many servers.

**Check a gateway from where the callers are.**
`vectrixdb check --url https://apim.company.com/vectrixdb` says whether the
path is published, whether the key headers arrive, and whose error page comes
back. See [Put it behind a gateway](behind-a-gateway.md#checking-it-from-outside).

**Need two builds to be the same index?** `VECTRIXDB_BUILD_THREADS=1`. Slower
to build, and every build of the same vectors answers every query the same.

**A key works as `Authorization: Bearer` too.** `api-key` is what the
dashboard sends, but a client generated from `/openapi.json`, an HTTP library
and most gateways reach for Bearer, and it is the same key.

**Open an older evaluation run.** The menu at the end of the golden data line
opens any run, and an admin can download the exact golden file each run read,
even after it has changed.

**Send the access log where the platform keeps logs.** In a container, a file
on its disk goes with it. `VECTRIXDB_ACCESS_LOG=stdout` writes every line to
the server's output instead.

## Keep it honest

**Know what a number is.** Every figure on the benchmarks page is printed by a
script in `scripts/` and pasted unchanged, with the machine and versions it ran
on. Run it on your own hardware before believing it there.

**Work offline on purpose.** `VECTRIXDB_OFFLINE=1` refuses every download,
including explicit ones. The English models are in the wheel, and
`vectrixdb models-info` says what is on the machine. See
[Run without a network](offline.md).

**Read what it does not do.** [What VectrixDB does not do](../explanation/limits.md)
is the short list of limits, kept current, so nothing here is a surprise later.
