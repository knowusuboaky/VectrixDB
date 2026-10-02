# Keep conversation memory without wasting tokens

A vector store treats every document as equally true forever. A conversation is not like that: what the user said a minute ago matters more than what they said in March, a preference gets corrected, and whether a recalled memory actually helped is something the caller knows and the store does not.

`Vectrix` has four methods for this. They store ordinary documents with reserved `_vx_*` metadata keys, so the collection stays a plain collection: `search()`, `delete()` and filters keep working on it.

## Remember turns and facts

```python
from vectrixdb import Vectrix

db = Vectrix("chat", path="./data")

db.remember("I'd like the report as a PDF", session="u42", role="user")
db.remember("Sure, PDF it is", session="u42", role="assistant")

# A fact that should always be in context for this user
db.remember("Customer is on the annual plan", session="u42", pinned=True)

# A fact that is recalled by relevance but not pinned
db.remember("Export greys out when a filter is active", session="u42", kind="fact")
```

Turns are numbered within their session automatically, and numbering continues across restarts. Ids are not content-addressed: "ok" said twice is two turns.

## Recall under a budget

```python
memories = db.recall("what format did they want", session="u42", token_budget=300)
memories.truncated  # True when something was cut to fit
memories.cut_count  # how many
memories.token_estimate
```

Ranking is relevance blended with recency and feedback. Relevance is normalised across the candidates first, because the bundled embedding models put every cosine score in a narrow band and "clearly more relevant" has to mean something regardless of model. The blend is additive: a brand-new memory gains `recency_weight` (default 0.5) over a very old one on the same 0-to-1 scale, so an old memory still wins when it is clearly the better match, and ties go to the newer.

Every recalled result carries the breakdown in `metadata["_vx_scoring"]`:

```python
{"relevance": 0.83, "relevance_norm": 1.0, "recency": 0.97, "feedback": 0.0}
```

`session=None` recalls across every session. Tune the weights through `db.memory`:

```python
from vectrixdb.memory import ConversationMemory

db._memory = ConversationMemory(db, half_life_days=30, recency_weight=0.3)
```

## Grade what came back

```python
memory_id = db.recall("what format did they want", session="u42").top.id

db.feedback(memory_id, "useful")
db.feedback(memory_id, "dead_end")
new_id = db.feedback(memory_id, "corrected", correction="Actually, DOCX please")
```

Feedback is a signed, time-decayed score folded into later ranking, so a dead end reported last month does not bury a memory today. A correction stores the corrected text as a new memory that supersedes the old one; the old one drops out of recall but is never deleted, so the history stays auditable. Corrected pins stay pinned.

## Build the context block

This is what a chat product actually calls before each model turn:

```python
user_message = "Can you send that report over now?"

block = db.context("what format did they want", session="u42", token_budget=1200)
prompt = block.text + "\n\n" + user_message
```

`block.text` is plain text in a fixed order: pinned facts, then the most recent turns of the session, then whatever else the query pulled in with the tokens that remain. Pinned facts are never cut; if they alone exceed the budget, `block.truncated` says so rather than dropping one. Recent turns are cut oldest-first. Recalled memories fit the remaining budget or are left out.

```python
block.pinned, block.recent, block.retrieved  # the three sections as Result lists
block.token_estimate, block.truncated, block.cut_count
```

## Forgetting

Memory is finite. Three tools, from gentlest to bluntest:

```python
from vectrixdb.memory import ConversationMemory

# Turns live 30 days; facts and pinned facts live forever.
db._memory = ConversationMemory(db, ttl_days={"turn": 30})
db.remember("a passing remark", session="u42")                 # expires in 30 days
db.remember("a shorter one", session="u42", ttl_days=1)        # per-memory override

db.memory.expire()                        # delete everything past its TTL; returns the count
db.forget(session="u42", older_than=90)   # turns older than 90 days, this session
db.forget(session="u42", kinds=("fact",), superseded=True)   # clear the audit trail of corrected facts
db.forget(ids=[memory_id])
```

An expired memory is invisible to `recall()`, `context()` and `turns()` the moment its time is up, whether or not `expire()` has run; `expire()` is about space and the audit trail, so run it whenever suits.

## Consolidation

A month of chat is thousands of turns and a dozen facts. `consolidate()` hands the old turns of a session to your model, stores what it returns as standalone facts, and drops the turns. This is the real long-term token saver:

```python
def summarize(turns):
    # turns: [{"role": "user", "text": "...", "ts": "..."}, ...], oldest first
    transcript = "\n".join(f"{t['role']}: {t['text']}" for t in turns)
    answer = my_llm(f"List the facts worth remembering from this chat, one per line, each complete on its own:\n{transcript}")
    return [line.strip("- ").strip() for line in answer.splitlines() if line.strip()]

report = db.consolidate("u42", summarize, keep_recent=6)
report.turns_removed, report.fact_ids
```

The last `keep_recent` turns stay so `context()` keeps its thread; `older_than` (days or a datetime) narrows further; `batch_turns` bounds how many turns one call sees. Each fact is dated at the newest turn it came from, so recency still means something, and carries the turn ids under `_vx_consolidated_from`. Turns are deleted only after every fact of their batch is stored; `delete=False` is a dry run. Nothing is bundled: `summarize` is any callable.

## Contradiction on write

"The user wants a PDF" and "the user wants a DOCX" cannot both be live. Give the memory a judge and it checks every new fact against the nearest stored ones:

```python
def judge(new, candidates):
    # Return one bool per candidate: True where the new fact contradicts it.
    answer = my_llm(f"New fact: {new}\nFor each of these, answer yes if the new fact contradicts it:\n"
                    + "\n".join(f"{i}. {c}" for i, c in enumerate(candidates)))
    return ["yes" in line.lower() for line in answer.splitlines()[: len(candidates)]]

db._memory = ConversationMemory(db, judge=judge)
db.remember("The user wants the report as a PDF", session="u42", kind="fact")
db.remember("The user wants the report as a DOCX", session="u42", kind="fact")
# The PDF fact is now superseded by the DOCX fact and drops out of recall.
```

Contradicted facts are marked superseded by the new one, the same link a `feedback("corrected")` makes, so the history stays auditable and `forget(superseded=True)` can clear it later. Turns are never judged; `check=True` on a turn or `check=False` on a fact overrides that per call.

## Measured

`scripts/memory_bench.py` runs LoCoMo and LongMemEval by one command. Both are long multi-session conversations with questions whose answers sit in particular turns, and answering needs an LLM; the script measures the part the library owns, which is whether `context()` under a token budget puts the evidence turns in front of the model. Evidence recall is the share of a question's evidence turns that made the block; plain search is `search()` over the same turns at the same size, for comparison.

```bash
python scripts/memory_bench.py locomo
python scripts/memory_bench.py longmemeval --limit 150
```

| Benchmark | questions | evidence recall, memory | evidence recall, plain search | block tokens |
| --- | ---: | ---: | ---: | ---: |
| LoCoMo, 10 conversations | 1536 | 0.623 | 0.620 | 334 |
| LongMemEval oracle, first 150 | 150 | 0.721 | 0.721 | 1237 |

Hybrid mode, a 1,500-token budget, no recent turns forced in, so the number is retrieval alone; bge-small-en-v1.5, run on 2026-09-19, with the raw figures in `benchmarks/memory_locomo.json` and `benchmarks/memory_longmemeval.json`. The gap between the memory column and the plain-search column is what recency weighting and the memory ranking buy on the same vectors; the absolute number is what a 384-dimensional model gets before any answer model sees the block. On LongMemEval the two columns are equal, which the split explains: the oracle history holds only the sessions the answer is in, so there is little for a ranking to push down.

**The model 2.2 ships is the weaker one here.** bge-small-en-v1.5 was chosen
on SciFact, where it beats e5-small-v2 by six points of nDCG@10, and this is
the benchmark where that choice costs something. Run over the same 150
questions with the same code, evidence recall is 0.721 with bge-small-en-v1.5
and 0.797 with e5-small-v2. It is not the ranking or the index: the whole
difference is in the questions whose evidence is spread over several sessions,
0.494 against 0.642, and those about a fact that changed later, 0.824 against
0.941; questions about when something happened are slightly better with the
new model, 0.881 against 0.864. Searching wider does not recover it, and a
collection this small is searched almost exhaustively either way.

LoCoMo does not see the difference: 0.623 with the new model and 0.628 with
the old one, half a point apart. Its questions are answered from one or two
turns in a single conversation, where either model finds the turn. So the
model matters on the benchmark that asks for evidence from several sessions
and not on the one that does not, which is worth knowing before reading either
number as a verdict. (LoCoMo did read 0.649 in September, and the two points
between then and now are neither the model nor the search width; nothing else
was held still enough between the two runs to say what they are.)

**A collection that carries both models beats either.** Over the same 150
questions, with both models' lists fused by rank:

| LongMemEval, first 150 | all | several sessions | a fact that changed | when it happened |
| --- | ---: | ---: | ---: | ---: |
| bge-small-en-v1.5 | 0.721 | 0.494 | 0.824 | 0.881 |
| e5-small-v2 | 0.797 | 0.642 | 0.941 | 0.864 |
| both | 0.814 | 0.677 | 0.912 | 0.890 |

Most of it is in the hardest questions, the ones whose evidence is spread over
several sessions, where each model finds turns the other misses. It is nine
points over the model in the wheel and under two over e5-small-v2 alone, and
it costs what a second model costs: every turn is embedded twice and kept
twice, the second model is fetched once on first use, and a collection that
carries an entitlement policy cannot have it. Run on 2026-09-20, raw figures
in `benchmarks/memory_longmemeval_both.json`.

```bash
python scripts/memory_bench.py longmemeval --limit 150 --dense-model e5-small
python scripts/memory_bench.py longmemeval --limit 150 --dense-model bge-small --dense-model e5-small
```

For memory-heavy work, measure on your own conversations.
`Vectrix(name, dense_model="e5-small")` builds a collection with the older
model, `Vectrix(name, dense_model=["bge-small", "e5-small"])` one that carries
both, and `reembed()` moves an existing collection.

## Token counts

Without a tokenizer, tokens are estimated at four characters each, which is within about twenty percent for English prose and needs no download. For exact counts, pass a counter once:

```python
import tiktoken

enc = tiktoken.get_encoding("cl100k_base")
db = Vectrix("chat", token_counter=lambda s: len(enc.encode(s)))
```

Every budget in the package then uses it.

## Plain search has the same controls

```python
db.search("export button", limit=10, token_budget=400, score_gap=0.9)
```

`token_budget` stops returning results once their text would cost more than that; the top result always ships and `Results.truncated` reports the cut. `score_gap` drops results below a fraction of the top score, so a specific question returns one strong answer instead of a padded list. The right value depends on the score scale: cosine from the bundled models sits in a narrow band, so 0.9 is typical there; BM25 or graph scores want around 0.2.
