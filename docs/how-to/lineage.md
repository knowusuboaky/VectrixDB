# Trace an answer to its source

One question drives all of this: the assistant told someone something wrong,
where did it come from? It is harder than warehouse lineage because the
transformation includes chunking and embedding, not just joins, and it is
answered in three steps. A chunk back to the write that stored it. A decision
back to the set of chunks that principal could reach. And a directory of
files a model risk validator can read.

Everything here is the host's question. A principal's view refuses all of
it, because which chunks a principal could reach is the map of the wall.

## The thread: a build id on everything

Every mutation of a collection mints a new `index_build_id`: an add, a
delete, a clear, a revocation, a re-embed, an index rebuild. Not just an add,
because a delete changes what a query can answer just as surely, and the
reproduction below rests on one invariant: same build id, same index.

Every chunk carries the build that stored it, in `_vx_build`. So the join
runs both ways. From a chunk to the ingestion record for its write, whatever
build happens to be current. And from an ingestion record, through
`ids_written`, to everything that write put there.

```python
from pathlib import Path

from vectrixdb import Vectrix, Policy, Overlap, AtMost, JSONLSink, DENY

policy = Policy([
    Overlap("entitlements.allowed_roles", "roles"),
    AtMost("entitlements.classification_rank", "clearance_rank"),
])
audit = JSONLSink("./audit/lineage.jsonl", query_key=b"replace-me", on_failure=DENY)
db = Vectrix("lending", path="./lending_lineage", policy=policy, on_retrieval=audit)

Path("memo.md").write_text(
    "# Covenant memo\n\nFacility covenant: coverage ratio not less than 1.15x.\n\n"
    "## Schedule\n\nCovenant schedule tested quarterly against the borrower accounts.\n"
)
db.add_document(
    "memo.md",
    chunk_size=60,
    overlap=10,
    metadata={"entitlements": {"allowed_roles": ["credit_analyst"], "classification_rank": 2}},
)

ingestion = audit.find(db.index_build_id)
print(ingestion["source"], ingestion["document_version"], ingestion["chunking"])
print(ingestion["ids_written"])
```

`add_document` stamps three more things on every chunk: `_vx_doc_version`, a
hash of the text as loaded, so two ingestions of the same file can be told
from an ingestion of a changed one; and the chunking configuration, because
the same text cut differently is a different set of chunks. A plain `add()`
of texts has none of these and records `None` rather than a guess.

## From a chunk to its write

```python
analyst = {"roles": ["credit_analyst"], "clearance_rank": 3}
results = db.as_principal(analyst).search("coverage ratio", limit=1)

for chunk in db.provenance(results.ids):
    print(chunk.source, chunk.document_version, chunk.chunk_index, chunk.build_id)
```

A chunk that has been deleted comes back with `present=False` and nothing
else, because a deleted chunk has no provenance to offer and inventing one
from a later write would be worse than saying so.

## From a decision to what was reachable

The decision record now carries `result_ids`: which chunks were handed over,
not only how many. Without those, a record can say that seven documents were
returned and not which seven, and the whole question is which.

```python
record = audit.find(results.decision_id)
reproduction = db.reproduce(record)
print(reproduction.report())
```

```text
decision dec_c0e05e36f6d14cf2 at 2026-09-17T22:51:23+00:00
  EXACT: index build build_ce670973bfa043c4 and policy sha256:8498ee95... are the ones the decision was made against
  reachable by this principal now: 3 chunk(s)
  returned then: 3, of which 3 still reachable, 0 no longer reachable, 0 no longer in the collection
    0188a6e6026e:1: memo.md vb2eb6701fb9e80c6, chunk 1, build build_933edf8560eb4b14
```

`exact` is the claim a validator wants, and the one this is careful about.
It is `True` only when the build id and the policy fingerprint on the record
are the ones the reproduction ran against, because then the reachable set
computed now is the reachable set that existed when the decision was made.
When either differs, the result is labelled a re-run rather than a
reproduction, and it still says which of the returned chunks are still
reachable, which this principal can no longer reach, and which are gone from
the collection. Those are three different findings: a document that is still
there but out of reach is a revocation, not a deletion.

### Keeping it exact after the index moves

The index moves. To restate a decision from last quarter, keep an export per
build and hand the right one back:

```python
snapshot = db.export("./builds/" + db.index_build_id + ".zip")
db.delete(results.ids)                                  # the live index moves on

print(db.reproduce(record).exact)                       # False: a re-run
print(db.reproduce(record, snapshot=snapshot).exact)    # True: that build
```

The snapshot carries the collection's own metadata, so its build id and its
policy are the ones that were exported. This is the retention decision the
roadmap called the hardest one: reproducing rather than explaining means
keeping the builds, and how many to keep for how long is yours.

## The evidence pack

```python
pack = db.evidence_pack("./evidence")
print([f.name for f in pack.files])
```

Six files, generated from the live objects rather than assembled by hand:
what the model is, what it answers from, the policy as data, which controls
are in force and what fails closed, and a summary of the audit trail. The
README in the directory says what each answers and what is deliberately
absent: the audit records themselves, which say which restricted documents
exist and who was refused them, and the total withheld out of scope, which
would confirm documents exist outside a scope somebody was refused. Both stay
in the audit store, under its grants.

It is artifacts, not a compliance claim. The judgement about whether they are
enough for a model's tier is the validator's, and nothing in the directory
makes it.

When the attached sink cannot read back, which is what a sink pointed at an
insert-only PostgreSQL table is, pass the records in:

```python
pack = db.evidence_pack("./evidence", records=audit.read_all())
db.close()
```

## What the record schema change means for an existing table

Record schema version 2 adds `result_ids` to decisions and `source`,
`document_id`, `document_version`, `chunking` and `ids_written` to
ingestions. A PostgreSQL table created by an earlier VectrixDB refuses at
construction and prints the `ALTER TABLE` statements to add the columns,
rather than recreating the table, which would take the history with it.
`PostgresAuditSink.SCHEMA_UPGRADE` and `INGESTION_SCHEMA_UPGRADE` hold them.
