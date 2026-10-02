# Roadmap

What is planned, what is deliberately not, and how to argue for a change. Items move from here into `CHANGELOG.md` when they ship. Effort is a guess: S under a day, M a few days, L a week or more.

## Principles that shape it

1. **A library, not a server.** Authentication, tenancy, metering, rate limits and health checks belong to whatever embeds VectrixDB. They will not be added here. See the note below on where authorization sits, which is a different question and one this roadmap answers differently.
2. **Offline first.** Nothing downloads at import or first use unless asked by name. Bundled models stay bundled.
3. **Every number measured.** A quality or performance claim ships with the script that produced it, and a regression test that fails when it drops.
4. **Honest results.** Truncation, degradation and fallbacks are reported on the result, never hidden in a shorter list.
5. **Fail closed on anything that gates access.** A document with no entitlement metadata is invisible, not universal. A principal with no groups matches nothing, not everything. An operator that cannot be evaluated denies. These three were live bugs in 2.1; making them defaults is the only way they stay fixed.

### The line under Principle 1

The Entitlements block below looks like it reverses the first principle. It does not, and the distinction is worth stating precisely because the whole block rests on it.

**Authentication stays out, permanently.** VectrixDB will never validate a token, call a directory, resolve a group, hold a session, or learn who anyone is. A principal arrives as a plain dictionary that the host resolved. The library has no opinion on where it came from and no way to check it.

**Authorization over data the library already holds is a data concern.** VectrixDB already decides which rows a filter returns. A policy is a filter the collection cannot be queried without. That is the same category of work as the filter engine, not the same category as a login endpoint.

The practical test: if the feature needs a network call to an identity system, it belongs to the host. If it is a predicate over metadata already in the collection, it belongs here.

## Shipped in 2.2 (unreleased)

Token budgets and conversation memory; the debts that capped the rating (Delta Lake bind parameters, HNSW batching, graph stage skipping, a typed graph-unavailable error); library ergonomics (4 ms import, context managers, pandas and polars, offline by default, Python 3.14); retrieval (bundled cross-encoder reranker, real BM25 with field boosts, float32 vector storage, memory-mapped read-only opens, AsyncVectrix, filters reference, snapshots, explain, fusion control, query expansion, CJK tokenisation, online index rebuild); the Testing block (contract suite, property and fuzz tests, crash and concurrency tests, 2.1.7 compatibility fixture, recall and golden-graph baselines, docs executed in CI, coverage and mypy ratchets, replays); Trust (benchmarks against Chroma, LanceDB and Qdrant, a BEIR harness, the docs site); Ingestion (four chunkers, five loaders, parent-child retrieval, near-duplicate detection, model versioning, an embedding cache); Memory (TTLs, forgetting, consolidation, contradiction detection, LoCoMo and LongMemEval); an Azure AI Search backend and an OpenSearch backend that passes the same contract (mocked on every push, live nightly when a domain secret is set); a dashboard smoke test; Developer experience (CLI, LangChain and LlamaIndex adapters, OpenAI-compatible embeddings, plugins, timing hooks, release automation, wheel and audit CI); Graph quality (incremental community detection, entity schemas, relationship supersession, graph_path and graph_explain, a community-coloured dashboard); a dense model chosen by measurement (bge-small-en-v1.5); a package that type-checks clean, with CI failing on any new error; sealed shards, so a collection can be written past RAM; 85.1% test coverage, from 50.4%, measured where CI measures it; this examples gallery and these governance files.

A decision record for every policied read, an append-only PostgreSQL sink whose grants are the deliverable, a decision id on the answer so a record can be reached from what it describes, an index build id minted on every write, every read path through the easy API gated or refusing (`similar()` was a straight bypass), enforcement in `Collection` so the REST server is covered too, with the withheld counts falling out of the search loop exactly, a sink interface whose failure policy has no default, a keyed query fingerprint, the principal snapshot stored inline rather than as a pointer, and a flag saying whether the withheld counts are exact or lower bounds. Entitlement policies: a rule set declared once against the collection, persisted with it, and impossible to search without. Six predicates over dotted field paths, a principal the host resolves and the library never validates, fail-closed on a missing document field and on an empty principal value, a `PrincipalIncomplete` that pages rather than denying, a scope flag that keeps a withheld count from confirming a document exists, and a fingerprint over the rule set so a past decision can be attributed. No `AccessDenied`: a denial is an empty result, because a catchable exception is the side channel.

The server grew up in the same release. People sign in as themselves, by single sign-on, a passkey or an email list with an authenticator code, and a role decides what each may do; guests search what an admin shares; a collection can mask email addresses, phone numbers and card numbers for the people it shows them to; every read is in an access log; every reply carries security headers and the dashboard runs no inline script. The dashboard was redrawn at three widths, the Evaluate pages pick a setup from a golden file, and `vectrixdb check` tests the settings before a start. The last round ran the suite in clean environments that install exactly what CI installs, which found a missing test dependency that stopped every CI job before a test ran, an undeclared `requests`, the MCP server failing on mcp 2, and OpenSearch scores reported inverted; see the changelog.

Late in the cycle, a clean-install round: psutil was imported at module scope and declared nowhere, so a plain `pip install vectrixdb` could not create a collection, and no test had ever installed the built wheel into an empty environment. Fixed by making it optional, and guarded by an AST sweep of every module-level import in the package that exempts nothing by name. Alongside it: `vectrixdb-mcp` answering a missing extra with a stack trace rather than the one line naming what to install; a README that linked three of twenty-one documentation pages and none of the six process files; and `[project.urls]` sending PyPI's Documentation link back at the README. uv was verified end to end and documented.

## Open

Effort is S under a day, M a few days, L a week or more. Dependencies are in the Sequencing section at the end rather than repeated per row.

### Entitlements

The block that decides whether VectrixDB can sit under a regulated retrieval path or only beside one. Today an entitlement filter is *constructed* by the caller on every query. Everything here is about moving it to *enforced*. The driving question: if the service layer builds the filter wrong once, does anything leak?

The policy itself has landed: predicates, the collection binding, the scope split, fingerprinting and the taxonomy, with three replays holding the fail-closed behaviour down. Enforcement lives in `Collection` now, so it applies to the REST server and anything else built on `VectrixDB` rather than only to callers who came through the easy API, and every read path there is gated. Enforcement below the filter has landed for the PostgreSQL backends: a policy can name the database role a read runs as, `SET LOCAL ROLE` inside a transaction lets row level security decide, and a nightly job proves against a real PostgreSQL that the database then refuses the rows, hands the connection back as it was, and does so even when the query inside raises.

The defence in depth above it has landed too. A shipped test kit asserts a policy does what its author thinks, including the one property nothing else can check: that a walled client and a client that was never onboarded are indistinguishable. Writing it found two leaks that no test had. BM25 weighted terms over the whole index, so a document the principal was refused changed the score, and the ranking, of one they were allowed; the statistics are computed over the visible subset now. And four read paths on `Collection` were never gated, three of which the REST server calls. A timing floor closes the last channel a filter cannot, at a cost stated plainly, and the write path refuses a value no rule could ever decide on. Every backend declares where its filter runs. Azure AI Search says `ENGINE` once the fields a policy names are promoted to filterable index fields, and runs the scope rules inside the service; every other backend says `POST`, and `require_pushdown=True` refuses there rather than letting a deployment assume enforcement it does not have.

| Item | Effort | Notes |
| --- | --- | --- |

### Audit

The library emits, the host stores, and the library never picks a location. An embedded library writing audit records somewhere the host did not ask for is the stdout problem with worse consequences.

The record has landed, along with the sink interface, the keyed query fingerprint, the inline principal snapshot and the schema version. `JSONLSink`, the local spool and `PostgresAuditSink` exist. The PostgreSQL one is the one most deployments should use, and its point is the grants rather than the SQL: a separate database, a role with `INSERT` and neither `UPDATE` nor `DELETE`. And `ObjectLockSink` writes each record under a compliance-mode retention lock, which is what satisfies a regulator asking whether a record could have been altered: the store refuses, whoever asks. Nothing is left open here.

| Item | Effort | Notes |
| --- | --- | --- |

### Lineage and model risk

Driven by one question: this assistant told someone something wrong, where did it come from? Lineage here is harder than warehouse lineage because the transformation includes chunking and embedding, not just joins.

The block has landed. Every mutation mints an `index_build_id` and every chunk carries the one that stored it, so the join between a chunk and its ingestion record runs both ways. `add_document` stamps a hash of the text and the chunking configuration. `db.reproduce(record)` restates a decision, exact when the build and the policy on the record are the ones the collection holds and labelled a re-run when they are not, with `snapshot=` to run against an export of the build the record names; keeping an export per build is the retention decision. `db.evidence_pack(directory)` writes the artifacts a validator asks for, and says in its README what is deliberately absent and why.

Nothing is left open here. The retention policy itself, how many builds to keep for how long, belongs to the deployment.

### Data quality for AI

Schema correctness is solved and boring. The interesting failure is a chunk that is structurally valid and semantically worthless.

Both halves are done. A document missing a field the policy decides by, or stamping a value no rule can decide on, is refused at the write with `on_incomplete_document` as the failure policy. And the part no schema check can see, a scanned page that OCRed into nonsense, is scored at the write by `extraction_quality`, with a threshold set from a labelled set that `scripts/quality_eval.py` rebuilds and `calibrate()` for a deployment whose documents are not English prose. It was held back until it could be measured, and the measurement is quoted beside the threshold.

Nothing is left open here.

### Retrieval

| Item | Effort | Notes |
| --- | --- | --- |
| Dense as the default mode | M | Dense scored highest on the fixture set (MRR 0.955 against 0.924 hybrid) but hybrid won on SciFact (0.736 against 0.707 with bge-small, and 0.693 once hybrid's default reranker has run). Needs a larger, domain-mixed set before the default changes. |
| Pre-filtered ANN search | L | Today a filtered search over-fetches and applies the filter to fetched metadata, widening the window until it has enough or exhausts reachable candidates. That closed the recall problem; it did not close the latency one. True in-index filtering would close both, and would make `timing_uniformity` unnecessary rather than mandatory. Large, and possibly out of reach without leaving usearch. |

### Library

Nothing is left open here. The wheel was 89.8 MiB of the 100 MiB limit with 21.5 MiB of it e5-small-v2, kept only so pre-2.2 collections would open offline; every remaining bundled model was larger than the room that left. It is fetched once on first use now, and the error on a machine that cannot fetch names the other way out, `reembed()` to the current default.

### Operations

| Item | Effort | Notes |
| --- | --- | --- |
| Turn on GitHub Pages | S | One repository setting: Pages, Source, GitHub Actions. The docs workflow already builds on every pull request and deploys on push to main. Until the setting is flipped, the documentation site and the PyPI Documentation link both 404, and the README links to a site that is not there. |

## Sequencing

The Entitlements block is not independently orderable; most of it depends on one item.

```
  Collection-bound policy  [DONE]
  Predicates, scope split, fingerprint, taxonomy  [DONE]
        |
        +--> Decision record + sink interface + PostgreSQL sink  [DONE]
        +--> Decision id on the answer, index build id on the record  [DONE]
        |         +--> Object-lock sink  [DONE]
        |
        +--> Every read path gated or refusing  [DONE]
        +--> Enforcement in Collection, not Vectrix  [DONE]  <-- the REST server
        |
        +--> Policy test kit  [DONE]
        +--> Timing uniformity  [DONE]
        +--> Bulk revocation (S)
        |
        +--> Backend pushdown capability (M)
                  |
                  v
        Aurora per-request role switching  [DONE]   <-- enforcement moves below the filter
                  |
                  v
        Proved against a real PostgreSQL  [DONE]  <-- nightly, service container
```

Everything in this block has landed, `filter_sql` included: it was deprecated with the removal set for 2.4 and then removed here instead, because holding an injection surface open for two releases to honour a notice period is the wrong trade when nothing in the library calls it. Lineage and model risk can start in parallel, since `index_build_id` is an ingestion concern that only meets the policy work at the decision record.

The first milestone is done: a policy that cannot be bypassed, and an audit trail that survives. The trail has somewhere to live that the audited application cannot rewrite. The object store is in too, for the stronger claim that a record could not have been altered at all. That is enough to demonstrate a policy that cannot be bypassed and an audit trail that survives, which is the pair a reviewer asks about first. The Aurora item is the second milestone and the more valuable one, because it is the only item that changes what VectrixDB *is* in the architecture.

## Deliberately not planned

Sharding and replication, GPU embedding, a managed cloud, and every server concern listed under the first principle. If your problem needs one of these, the [why VectrixDB](https://knowusuboaky.github.io/VectrixDB/explanation/why-vectrixdb/) page names the products that do them well.

Three more, specific to the blocks above:

**Authentication of any kind.** No token validation, no directory lookup, no group resolution, no sessions. A principal is a dictionary the host resolved and hands over. The library cannot check it and will not try.

**A policy expression language.** Six predicates cover every case study that has been mapped against them. An expression language would cover more, and would be unauditable: the point of a small vocabulary is that a reviewer can read a policy and be certain what it does. If a case study genuinely needs a seventh predicate, add the predicate.

**Encryption at rest, per-tenant keys, or key management.** These belong to the storage backend and the platform underneath it, both of which do them better.

**Translating a policy into SQL to push it down.** This was on the list and has been taken off. The filter grammar is twenty-two operators, including geo, regex, and the absent-versus-null-versus-empty distinctions that took a round of bug fixes to get right this release. Translating all of that into SQL means writing the policy a second time in a second language, where a subtle disagreement between the two is a security bug rather than a wrong answer, and where the only available test harness is a stand-in written in the same sitting. Row level security needs no translation at all: the rule is written once, in SQL, by whoever owns the database, and VectrixDB switches role. That is the path.

## Proposing a change

Open a feature request with the situation you are in, not the feature you imagine; the template asks for both. Anything that changes search results should come with a number from `scripts/recall_baseline.py`, `scripts/beir_eval.py` or `scripts/memory_bench.py`. Anything that adds a runtime dependency needs a reason the standard library cannot cover.

For anything in the Entitlements or Audit blocks, bring the case study: the schema, the principal shape, and the one query that must return nothing. Those three make the difference between a predicate that generalises and one that encodes a single organisation's org chart.
