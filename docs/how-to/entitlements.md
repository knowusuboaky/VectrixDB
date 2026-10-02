# Restrict what a search can see

Embeddings do not carry permissions. Once text is chunked and vectorised, the
index will return any chunk to any caller that matches semantically, which is
fine until two groups of people query the same collection and are entitled to
different parts of it.

VectrixDB answers that with a **policy**: a rule set declared once against the
collection, persisted with it, and impossible to search without.

## The problem with building the filter yourself

You can already pass a filter on every search, and if you build it correctly
every time, from the caller's token rather than from anything the caller sent,
it works. The trouble is the words "every time". A filter the caller assembles
is a control that holds until one call site forgets, and a forgotten filter
returns everything without a word.

A policy moves that decision to the collection. Once one is attached, a search
with no principal raises rather than answering.

## Declaring one

```python
from vectrixdb import Vectrix, Policy, Overlap, Equals, AtMost, Excludes

policy = Policy([
    Overlap("entitlements.allowed_roles", "roles"),
    Equals("entitlements.lob", "lob"),
    AtMost("entitlements.classification_rank", "clearance_rank"),
    # scope=True: these decide whether this principal may know the
    # document exists at all, as against redacting something inside a
    # scope they already hold. See "What a caller may be told" below.
    Overlap("entitlements.client_id", "client_coverage", scope=True),
    Excludes("entitlements.client_id", "wall_restrictions", scope=True),
])

db = Vectrix("lending", path="./lending_data", policy=policy)

db.add(
    [
        "Facility covenant: fixed charge coverage ratio not less than 1.15x",
        "Internal credit memo: covenant headroom is thin into Q3",
    ],
    metadata=[
        {"entitlements": {
            "allowed_roles": ["credit_analyst"], "lob": "commercial_banking",
            "classification_rank": 2, "client_id": "CL-40219",
        }},
        {"entitlements": {
            "allowed_roles": ["credit_analyst"], "lob": "commercial_banking",
            "classification_rank": 5, "client_id": "CL-40219",
        }},
    ],
)
```

Each rule relates a dotted path into the document's metadata to a key in the
principal. Six predicates cover it:

| Predicate | Passes when |
| --- | --- |
| `Overlap(doc, key)` | the document's field shares a value with the principal's |
| `Equals(doc, key)` | the document's field is exactly the principal's value |
| `AtMost(doc, key)` | the document's field is no greater than the principal's |
| `AtLeast(doc, key)` | the document's field is no less than the principal's |
| `Excludes(doc, key)` | the document's field is none of the principal's values |
| `Present(doc)` | the document has the field at all |

`AtMost` is why a classification wants a numeric rank beside its label.
"Cleared to at least this level" is an ordering question, and set membership
cannot answer it without enumerating every level below.

## Searching as a principal

A principal is a plain mapping that **you** resolved, from your directory,
your CRM, your control room. VectrixDB does not validate it and cannot: a
library that believed a principal handed to it would be claiming an authority
it does not have.

```python
analyst = {
    "roles": ["credit_analyst"],
    "lob": "commercial_banking",
    "clearance_rank": 3,
    "client_coverage": ["CL-40219"],
    "wall_restrictions": [],
}

for hit in db.as_principal(analyst).search("covenant thresholds"):
    print(hit.text)
```

`as_principal` returns a view sharing the same index and model, so it is
cheap. The collection itself never acquires a principal, so an unqualified
search still refuses:

```python
from vectrixdb import PrincipalRequired

try:
    db.search("covenant thresholds")
except PrincipalRequired as exc:
    print("refused:", exc)
```

A caller may still pass their own `filter=`; it narrows and can never widen,
even if it names a field the policy uses.

## What fails closed, and what does not

Three behaviours are worth knowing before you trust this with anything.

**A document with a missing entitlement field is invisible, not universal.**
If the policy names `entitlements.classification_rank` and a chunk arrives
without one, no principal sees it. There is no option to turn that off. If you
have genuinely public documents in the same collection, say so in a field and
write a rule that permits it, which is a statement a reviewer can read, rather
than a global fail-open nobody can see.

**An empty principal value matches nothing.** A user in no groups sees no
documents, rather than all of them. An empty `Excludes` list is the opposite
and correct: excluding nothing is not a grant.

**A principal missing a key the policy names raises.** This one is not a
denial:

```python
from vectrixdb import PrincipalIncomplete

broken = {"roles": ["credit_analyst"]}  # the resolver dropped four keys

try:
    db.as_principal(broken).search("covenant thresholds")
except PrincipalIncomplete as exc:
    print("resolver fault:", exc.key)
```

Failing closed is right, but it belongs on a pager rather than in the denial
count, where it would look like a busy afternoon while your entitlement
resolver is down.

## What a caller may be told

`scope=True` on a rule marks it as deciding whether the principal may know the
document exists at all. That distinction cannot be worked out from the rules
themselves, and getting it wrong defeats an ethical wall.

An analyst **inside** a scope who loses a chunk to a clearance rule can safely
be told something was withheld: they already know the client exists. An
analyst **outside** the scope cannot be told anything, because "one result
withheld" confirms the client is a client.

```python
candidates = [doc.metadata for doc in db.as_principal(analyst).search("covenant", limit=50)]
allowed, disclosable, undisclosable = policy.partition(analyst, candidates)

print(f"{len(allowed)} returned")
print(f"{disclosable} withheld, safe to surface")
print(f"{undisclosable} out of scope, audit only, never surface")
```

Surface `disclosable`. Never surface `undisclosable`. They are separate
numbers precisely so that a single "suppressed" count cannot be rendered by
mistake.

## Testing your policy

A policy is a security control written as data, which makes it the kind of
thing that goes subtly wrong and stays quiet about it. A rule that is too
tight shows up as an empty result somebody blames on the index. A rule that is
too loose shows up as nothing at all.

`vectrixdb.testing` ships with the library rather than living in this
project's own tests, because the policies that matter are yours.

### A table a reviewer can read

```python
from vectrixdb.testing import assert_visibility

def chunk(rank, client):
    return {"entitlements": {
        "allowed_roles": ["credit_analyst"], "lob": "commercial_banking",
        "classification_rank": rank, "client_id": client,
    }}

covenant = chunk(2, "CL-40219")
memo = chunk(5, "CL-40219")
other = chunk(2, "CL-51330")

rival_desk = {
    "roles": ["credit_analyst"],
    "lob": "commercial_banking",
    "clearance_rank": 3,
    "client_coverage": ["CL-51330"],
    "wall_restrictions": ["CL-40219"],
}

assert_visibility(
    policy,
    principals={"on the deal team": analyst, "walled off": rival_desk},
    documents={"covenant": covenant, "memo": memo, "other client": other},
    expected={
        "on the deal team": {"covenant"},
        "walled off": {"other client"},
    },
)
```

Every principal needs a row. Leaving one out fails rather than passing by
omission, because the alternative is a policy change that widens access while
the table stays green.

A mismatch prints the whole table with the disagreements marked, which is the
form a reviewer, or a model risk validator, can actually check:

```text
the visibility table does not match:

  [x] on the deal team  saw ['covenant']
                        wanted ['covenant', 'memo']
                        - memo should have been visible
  [ ] walled off        saw ['other client']
```

A `-` is a document that should have been visible. A `+` is one that should
have been withheld, and that is the direction worth reading twice.

### One document, and why

```python
from vectrixdb.testing import assert_allows, assert_denies, explain

assert_allows(policy, analyst, covenant)
assert_denies(policy, analyst, memo, disclosable=True)
assert_denies(policy, rival_desk, covenant, disclosable=False)

print(explain(policy, analyst, memo))
```

`disclosable` asserts **which kind** of withholding, and it is worth being
explicit about. In scope means the caller may be told something was held back.
Out of scope means telling them anything confirms the document exists. Getting
those two the wrong way round is how an ethical wall fails while every test
still passes.

`explain` is what to reach for when a decision surprises you: the verdict,
then every rule, with the ones that failed marked and both sides of each
comparison printed.

```text
WITHHELD, in scope
  [ ] Overlap(entitlements.allowed_roles <- roles)
        document entitlements.allowed_roles=['credit_analyst']  principal roles=['credit_analyst']
  [ ] Equals(entitlements.lob <- lob)
        document entitlements.lob='commercial_banking'  principal lob='commercial_banking'
  [x] AtMost(entitlements.classification_rank <- clearance_rank)
        document entitlements.classification_rank=5  principal clearance_rank=3
  [ ] Overlap(entitlements.client_id <- client_coverage)
        document entitlements.client_id='CL-40219'  principal client_coverage=['CL-40219']
  [ ] Excludes(entitlements.client_id <- wall_restrictions)
        document entitlements.client_id='CL-40219'  principal wall_restrictions=[]
```

An absent field prints as `<absent>` rather than `None`, because the two are
different and only absent denies.

### The wall assertion

The property the whole design rests on, and the one nothing else can check. A
principal walled off from a client and a principal asking about a client that
was never onboarded have to get the same answer, because "no documents found
for that client" has already confirmed whether a relationship exists.

So the test is two collections, identical except that one of them holds the
walled client's documents:

```python
from vectrixdb.testing import assert_indistinguishable

rival_document = ("Facility covenant for the other borrower, ratio 1.40x", other)
walled_documents = [
    ("Facility covenant: fixed charge coverage ratio not less than 1.15x", covenant),
    ("Internal credit memo: covenant headroom is thin into Q3", memo),
]

def collection(path, rows):
    one = Vectrix("wall_check", path=path, policy=policy)
    one.add([text for text, _ in rows], metadata=[meta for _, meta in rows])
    return one

holds_the_client = collection("./wall_with", walled_documents + [rival_document])
never_onboarded = collection("./wall_without", [rival_document])

assert_indistinguishable(
    lambda: holds_the_client.as_principal(rival_desk).search("covenant thresholds", limit=10),
    lambda: never_onboarded.as_principal(rival_desk).search("covenant thresholds", limit=10),
    "a walled client and one that was never onboarded",
)
```

Both sides are callables, so that a raise counts as an observation. An
exception from one and a result from the other is a difference, and it is the
one people miss: a catchable `AccessDenied` would be a perfectly good side
channel, which is why this library does not have one.

What it compares is the documents, their order, the count, the truncation and
degradation signals, and the scores.

Scores go in because a score is a number the caller reads, and it is compared
to a tolerance rather than exactly, for a reason worth knowing. Embedding is
not bit stable across batches: the same sentence embedded alongside others
comes back with a vector up to 0.015 from the one it gets alone, which moved
a cosine score by as much as 2 percent in the texts this was measured on.
Nothing about that concerns entitlements, and a security test that fails on
noise is one somebody turns off. Ten percent is the default, five times above
that noise; pass `score_tolerance` to change it or `None` to skip the check.

Writing this assertion is what turned up the sparse leak described under
[Scoring against what the principal can see](#scoring-against-what-the-principal-can-see),
which is the argument for having it.

The one thing it leaves alone is **time**, and that has its own answer below.

## Scoring against what the principal can see

A dense score is a similarity against a vector fixed when the document was
written, so nothing withheld can move it. BM25 is not like that. It weights a
term by how many documents contain it, counted over the index, so a document
the principal was refused changes the score of one they were allowed:

| | the visible document scored |
| --- | --- |
| index also holds the walled client | `0.1131` |
| that client was never onboarded | `0.1823` |

Same document, same text, same rank. Worse, with a dozen withheld documents
all about covenants the term goes cheap, and a visible covenant document
falls below a visible escrow one, so the **order** differs too. Hiding the
score would not have closed that, and `rrf` fuses the sparse ranking into
hybrid, so hybrid carried it as well.

So on a policied collection the three statistics, document frequency, the
document count and the average length, are computed over the documents the
principal may see. The policy decides before the scoring rather than over the
results.

That costs a pass over the collection's metadata per keyword query, which a
dense query does not pay. It is the price of the guarantee, and there is no
cheaper correct version: scoring against a corpus and then hiding part of it
leaves the corpus in the arithmetic.

An ordinary filtered search is unchanged. `filter=` still scores against the
whole index, because narrowing the statistics to a filter is a ranking
decision rather than a security one, and this is a security fix.

## How long an answer took

The last thing a caller can measure. A principal who may see one document in
a thousand sends the over-fetch loop round again; a principal who may see
most of them does not. The answer arrives later for the first and sooner for
the second, and no filter closes that, because the work really is different.

`timing_floor` is the blunt instrument that does:

```python
timed = Vectrix("lending_timed", path="./lending_timed", policy=policy, timing_floor=0.25)
```

Every read is then held until it has taken a whole number of floors: a query
finishing in 30 ms returns at 250 ms, one taking 260 ms returns at 500 ms.
Rounding up to the next multiple rather than padding to one deadline matters,
because a single deadline reports the true duration of everything that
overruns it, which is the whole distribution above the floor.

`Results.time_ms` reports the padded figure. It has to: sleeping and then
handing the caller the real duration in a field would leave the channel open
while looking closed. The decision record keeps the true latency, because the
host paying for it is entitled to know what it bought.

It costs what it says. Every query takes at least a floor, whether or not
anything was withheld, which is why it is off unless you ask for it. Turn it
on where a caller must not be able to tell a walled client from one that was
never onboarded, and leave it off where the policy is about redaction inside
a scope everybody already knows exists.

## The contract on the way in

Every rule names a document field, and an absent field denies. So a chunk
written without one is invisible to every principal, for ever, and the
symptom reads as a permissions problem: somebody spends an afternoon in the
entitlement resolver looking for a bug that is in the ingestion pipeline.

A policied collection refuses that write instead:

```pycon
>>> db.add(["a covenant"], metadata=[{"entitlements": {"allowed_roles": ["credit_analyst"]}}])
MetadataContractError: document 0 (f97c5d29941b) is missing
['entitlements.classification_rank', 'entitlements.client_id'], which this
collection's entitlement policy decides by. Written as it is, no principal
would ever see it, and the reason would look like a permissions problem
rather than the pipeline bug it is.
```

`policy.required_document_fields` is the contract: every path the rules read,
named once even when two rules share a field. The check runs before the write,
so a batch with one bad document in it does not land half of itself.

A field deliberately set to **null** satisfies the contract. Absent and null
are different, and only absent denies; a null is a value the policy can decide
on like any other.

### A value no rule can decide on

The same failure with a different spelling, and the one a schema check will
never catch, because `"high"` is a perfectly good string:

```pycon
>>> db.add(["a covenant"], metadata=[{"entitlements": {
...     "allowed_roles": ["credit_analyst"], "classification_rank": "high",
...     "client_id": "CL-40219",
... }}])
MetadataContractError: document 0 (6770a135844a) stamps
entitlements.classification_rank='high', where the rule needs a number. This
collection's entitlement policy decides by those fields, so written as it is,
no principal would ever see this document, and the reason would look like a
permissions problem rather than the pipeline bug it is.
```

`"high"` is not greater than any clearance and not less than one either, so
`AtMost` is false for every principal, for ever. Absent and undecidable are
one bug, so they are caught in one place and refused the same way.

What each rule can reach a verdict on:

| Rule | Needs |
| --- | --- |
| `AtMost`, `AtLeast` | a number. A boolean is refused: `True` would compare as level one, and nobody means that |
| `Overlap`, `Excludes` | a value, or a list of them |
| `Equals`, `Present` | anything |

An empty list passes. A document nobody may see is a thing somebody
legitimately writes, the same reasoning that lets a null through: what is
refused is a shape the comparison can only ever be false against, which is a
mistake rather than a statement.

This is the write path's half of the policy, and the honest limit of it. The
library can tell you that an ingestion stamped something no rule can decide
on. It cannot tell you whether the ingestion was *entitled* to stamp
`client_id: "CL-40219"` at all, because that is a question about the service
doing the writing, which the host authenticates and the library never sees.

Three modes, for the three situations:

| `on_incomplete_document` | |
| --- | --- |
| `"reject"` (default) | refuse the write |
| `"warn"` | write it and say so, for backfilling a collection that already has such chunks |
| `"allow"` | silent |

## Revoking

Entitlements are denormalised onto the chunk, which is what makes a query fast
and a permission change expensive: one person leaving a deal team means every
chunk of every document for that client.

```python
report = db.revoke(
    where={"entitlements.client_id": "CL-40219"},
    unset=["entitlements.need_to_know_group"],
)
print(report.matched, report.changed)
```

`unset` **removes** a field rather than setting it to null, and those are not
the same thing: the filter engine tells an absent field from a null one, and
under a policy an absent field denies while a null one is a value like any
other. `set` merges a new value in, which is how you move a deal team rather
than dissolve it.

Two counts rather than one, because the gap between them is usually what you
wanted to know. Matched many and changed none means the revocation had already
been applied. Matched none means the filter is wrong. A single "affected"
number hides both.

It takes effect on the next search: there is no cache to wait out, because a
policied collection does not use the result cache. An empty `where` is refused
rather than treated as "everything", and like `export` it belongs to whoever
holds the collection rather than to a principal.

## The policy travels with the collection

```python
db.close()

reopened = Vectrix("lending", path="./lending_data")   # no policy argument
print(reopened.policy.fingerprint == policy.fingerprint)
reopened.close()
```

Leaving the argument off does not turn the control off. Passing a *different*
policy raises `PolicyMismatch`, because that is either a deployment that has
drifted or an attempt to relax a control by reopening, and neither should be
a silent success.

`fingerprint` is a hash of the rule set rather than a version number, so it
cannot be forgotten when somebody edits a rule. Record it against every access
decision you log: without it, a past decision that no longer reproduces gives
you no way to tell a policy change from a data change.

## Letting the database decide

Everything above decides in Python. The policy compiles to a filter, the
collection applies it, and the documents that come back are the right ones.
What that is not is enforcement: a bug in the filter, a call path that slipped
the gate, a future refactor, and the wrong rows come back, because nothing
underneath ever said no.

On the PostgreSQL backends, row level security can say no. Your DBA writes one
policy in SQL, attached to the table:

```pycon
>>> from vectrixdb.core.storage import AuroraPostgreSQLStorage
>>> print(AuroraPostgreSQLStorage.RLS_SETUP.format(table="points", role="vectrix_app"))
ALTER TABLE points ENABLE ROW LEVEL SECURITY;
ALTER TABLE points FORCE ROW LEVEL SECURITY;
CREATE POLICY points_read ON points FOR SELECT USING (...);
GRANT SELECT ON points TO vectrix_app;
```

`FORCE` matters: without it the table owner bypasses the policy entirely.
VectrixDB never issues any of this, because a connection that can create a
policy can drop one.

Then tell the policy which principal key holds the role to read as:

```pycon
>>> policy = Policy(rules, db_role_key="db_role")
>>> view = db.as_principal({**analyst, "db_role": "analyst_u10442"})
>>> view.search("covenant thresholds")     # runs as analyst_u10442
```

Each read runs `SET LOCAL ROLE` inside a transaction, and the transaction
ending is what gives the role back. `SET LOCAL` rather than `SET` is the whole
point: a role that outlived the request would be worse than none, because the
next caller on that pooled connection would run as somebody else. The Aurora
backend runs in autocommit, where there is no transaction for `SET LOCAL` to
belong to, so one is opened explicitly rather than assumed.

The role name is validated rather than escaped. It goes into the statement as
an identifier, which cannot be a bind parameter, so anything that is not a
plain identifier is refused: a role name with a quote in it is not a role name
anybody meant to use. A backend that cannot assume a role raises instead of
carrying on, because silently not assuming it leaves you believing the
database is enforcing when nothing is.

What this library can prove is that the right statements are issued, in a
transaction, in the right order, and that the role is handed back. Whether
PostgreSQL then refuses the rows is PostgreSQL's, and testing that needs a
live database with policies your DBA wrote.

## What this is not

**It is enforced by the collection, not by `Vectrix`.** That matters because
the REST server and everything else built on `VectrixDB` reach a `Collection`
directly. A policied collection searched that way refuses without a principal
rather than answering, so the control does not depend on which door you came
through.

Two consequences worth knowing. The existing `/search/acl` endpoint takes
`user_principals`, `acl_field` and `default_allow` from the request body, so
whoever reaches it names their own entitlements and can switch the fail-open
on; on a collection with a policy those endpoints now refuse outright, because
ACL principals and an entitlement policy are two different controls and
running only one of them silently is the hazard. Separately, the API key
middleware allows everything when no key is configured, which is worth
checking in any deployment.

## Which reads are gated

| Path | With a policy |
| --- | --- |
| `search`, `get`, `get_one`, `similar` | refuse without a principal, filtered with one |
| `count`, `len()` | the host counts the collection; a principal counts their own share |
| `export`, `graph_path`, `graph_explain` | the host only; a principal is refused |
| `recall`, `context` | refused outright, host or not |
| `Collection.search`, `keyword_search`, `sparse_search` | refuse without a `principal=` |
| `Collection.hybrid_search`, `search_with_acl`, `enterprise_search` | refuse outright |
| `Collection.get`, `get_batch`, `iter_documents` | refuse without a `principal=` |
| `Collection.count`, `list_ids`, `scroll` | refuse without a `principal=`, answer for that principal with one |
| `Collection._get_raw`, `_iter_documents_raw`, `_count_raw`, `_scroll_raw` | deliberately ungated, for the collection's own bookkeeping |

That last group of three was found by reading every public method on
`Collection` rather than by a failing test, and the REST server called all
three: a collection page, an id listing and a sparse query answered anybody
who could reach them. If you are serving a policied collection over the
built-in API, take the version that carries this table.

The raw pair is named rather than hidden on purpose: a grep for `_raw` is the
list of places that deliberately see everything, and every one of them is a
write path or an internal index.

The three tiers are not the same problem. Content a policy can decide refuses
without a principal, because those are what a request handler calls and a
forgotten `as_principal` there is the leak this exists to prevent. Content a
policy *cannot* decide refuses whenever a policy exists, because no principal
would make it safe. Administrative work belongs to the host: `as_principal`
returns a copy and nothing leads back to the original, so a principal only
ever holds a view.

**It is not authentication.** Nothing here validates a token, calls a
directory, resolves a group or holds a session. That is the host's job and it
will stay the host's job.

**Enforcement below the filter is the backend's to offer.** A policy compiles
to a filter. The local index, and every backend that hands rows back, apply
it to candidates after they are fetched; a backend that can run the fields
the policy names inside its own query is handed the scope rules and runs them
there, so a document outside the scope never leaves the store. You can ask
which you have:

```pycon
>>> db.pushdown_mode
<FilterPushdown.POST: 'post'>

>>> strict = Policy(rules, require_pushdown=True)
>>> Vectrix("lending", path="./lending_data", policy=strict)
PushdownUnavailable: this policy requires the filter to run in the engine, and
the local index applies it after fetching. A backend pushes a policy down only
when every field the policy names is one it can filter on inside the store (on
Azure AI Search, azure_search_filter_fields). Promote those fields, or build
the policy without require_pushdown and treat the filter as correct rather
than as enforcement.
```

Two backends push down: Azure AI Search and OpenSearch, when the metadata
paths the policy names are promoted to real filterable index fields with
`filter_fields=` (see [Storage backends](storage-backends.md#azure-ai-search)).
Then `pushdown_mode` is `ENGINE`, `require_pushdown=True` opens, the store
serves every policied search even though the local index holds a copy of the
vectors, and every decision record says `pushdown_mode: "engine"`. Only the
scope rules go to the store; the redaction rules are still decided here, over
what came back, so the withheld-disclosable count stays exact. The out-of-scope
count is zero by construction, since those documents never left the service,
and it is never disclosed anyway. A policy naming one field the store cannot
filter on is `POST` in full: one rule decided after the rows came back is one
rule the store did not enforce, whatever the others did.

Everywhere else `pushdown_mode` is `POST`, `require_pushdown=True` refuses,
and the record says so. That is the honest answer and the reason the option
exists: a deployment that needs engine-side enforcement should be told it does
not have it rather than assume it from the fact that a policy is attached.
The documents that come back are correct, but the number of candidates
examined varies with how selective the entitlements are, which is observable
as latency. Where a caller must not be able to tell a blocked client from one
that does not exist, put the collection on a backend that pushes the policy
down, or set a `timing_floor`.

**It does not write your audit record.** What was returned, what was withheld,
under which policy and which entitlement snapshot, is yours to log. See the
Audit block in `ROADMAP.md` for where that is going.

### Over the built-in REST API

It does not serve a policied collection, and that is the whole answer. Every
route refuses with a 403, the listing names such a collection without its
count or size, and a collection with no policy is untouched.

The reason is not squeamishness. Serving one honestly means deciding per
document, deciding needs a principal, and this API resolves none: there is no
header it trusts and no directory it calls. Half-serving it is worse than
refusing, because gating the documents and still reporting `size_bytes`
leaves the inference standing: a size grows when documents are written, and a
walled reader watching it move learns that a client they were refused is
being written to.

Serve a policied collection from a tier that resolves principals, and give
that tier a `Collection` or a `Vectrix` directly.

## Recording the decision

Attach a sink and every policied search writes one record. The library builds
it and never picks a destination, because these records are evidence and their
retention usually outlives the data they describe.

```python
from datetime import datetime, timezone

from vectrixdb import Vectrix, JSONLSink, AuditContext, DENY

# In a real deployment this comes from your secret store, and it lives
# somewhere the audit store itself cannot reach.
DEPLOYMENT_KEY = b"replace-me-with-a-real-key"

audit = JSONLSink(
    "./audit/vectrixdb.jsonl",
    query_key=DEPLOYMENT_KEY,
    on_failure=DENY,            # or Spool("./spool/audit.jsonl")
)

audited = Vectrix(
    "lending", path="./lending_data", policy=policy, on_retrieval=audit
)

view = audited.as_principal(analyst, audit=AuditContext(
    principal_id="u_10442",
    trace_id="req_9f3a21",
    snapshot_taken_at=datetime.now(timezone.utc),
))
view.search("covenant thresholds")

record = audit.read_all()[-1]
print(record["outcome"], record["results_returned"], record["withheld_disclosable"])
```

`on_failure` has no default. Whether a failed audit write should deny the
answer or buffer it and continue is a decision about your business, not about
this code, so the sink refuses to be built without one. `Spool` writes to a
local file and `drain()` replays it once the real store is back; a drain that
hits a store still down stops and keeps the backlog rather than losing it.

`query_key` is required too. The record stores an HMAC of the query, not the
query, because "covenant thresholds for CL-40219" names the client. A bare
SHA-256 over a low-entropy query space can be enumerated by anyone holding the
log, which would make it a correlation key that reads like a redaction.

### The two counts, again

```
withheld_disclosable     safe to surface to the caller
withheld_undisclosable   audit only, never surface
```

This is the same split as `partition()` and it matters more here, because a
record is the thing people build dashboards on. Surfacing the second number
confirms that documents exist outside a scope somebody was refused.

### Tying an answer back to its record

```python
results = view.search("covenant thresholds")
print(results.decision_id)      # dec_752d298922934cbc
audited.close()
```

That is the `decision_id` of the record this search wrote. Put it in your
response, your logs, your support tickets. Without it, "the assistant told me
this" has to be matched to a row by wall-clock time, which is not a
resolution. It is opaque and safe to show: it names a decision, never a
document.

An unaudited search has `None` rather than an id, because there is no record
for one to point at.

The record also carries `index_build_id`, which names the ingestion that
produced the index that answered. Every write mints a new one, whether or not
anything is auditing, so the field is populated the day you switch auditing on
rather than from whenever somebody remembered to. It is read per search rather
than cached, so a long-lived view cannot keep naming the build that preceded a
write. `db.index_build_id` is the current one.

### Two fields worth wiring up properly

`snapshot_taken_at` is what produces `principal_snapshot_age_ms`, and that is
the field an access decision is challenged on months later: was the
entitlement in force at query time current? Without it the age is `None`,
which is honest, rather than zero, which would read as "resolved this instant".

`candidates_exhausted` says whether the withheld counts are exact or lower
bounds. An audited search examines a wider window than it returns so the
policy has something to reject and count; if that window filled, there may be
more. A count you cannot testify to should not look like one you can.

### Putting it in PostgreSQL

`JSONLSink` is honest about not being a system of record. This is the one that
is, and the reason is the grants rather than the code.

```bash
# As a DBA, once. The application role never runs this.
psql "$AUDIT_DSN" -c "$(python -c '
from vectrixdb import PostgresAuditSink as S
t = "public.retrieval_decisions"
print(S.SCHEMA.format(table=t) + S.GRANTS.format(table=t, writer="vectrix_app"))
')"
```

That prints a `CREATE TABLE` and then:

```sql
REVOKE ALL ON public.retrieval_decisions FROM PUBLIC;
GRANT INSERT ON public.retrieval_decisions TO vectrix_app;
-- Deliberately no UPDATE, no DELETE, no TRUNCATE, and no ownership.
```

Then point a sink at it:

```python
from vectrixdb import PostgresAuditSink, Spool

audit = PostgresAuditSink(
    dsn="postgresql://vectrix_app@audit-host/audit",
    query_key=DEPLOYMENT_KEY,
    on_failure=Spool("/var/spool/vectrixdb-audit.jsonl"),
)
```

Three things about that, in order of how much they matter.

**A separate database from the index.** If the audit lives beside the data it
audits, under the same credentials, then whatever compromises the query path
can erase its own trail. Separate host if you can, separate database and
separate role at minimum.

**`INSERT` and nothing else.** The library cannot give you this and does not
try: it never issues DDL, because a role that can create the table can drop
it. Provision it as a DBA and give the application the one grant it needs.

**`Spool` rather than `DENY`, usually.** The audit database is a different
machine from the index, and a network blip between them should not stop every
search in the product. `DENY` is right where an unaudited answer is worse than
no answer at all. Both are defensible; neither is a default.

The sink verifies the table at construction rather than on the first search,
because a missing audit table discovered on the first policied query is
discovered in production, under a failure policy, at the worst possible
moment. If the table is missing it tells you, with the DDL in the message. If
the table exists but an upgrade added a column, it names the column and tells
you to add it rather than recreate the table, which would take the history
with it.

Draining a spool is idempotent: the insert is `ON CONFLICT (decision_id) DO
NOTHING`, because some of a backlog may already have landed and a duplicate is
not a second decision.

A connection that dies is dropped rather than kept, so the next write
reconnects. Without that, one blip would poison the sink for the life of the
process and every later record would spool for no reason.

### The write path is a different question

A decision record says whether somebody should have seen what they saw.
Nothing said whether the documents were supposed to be in the index at all.
With a sink attached, every write produces an `IngestionRecord` too:

```
record_kind        ingestion
ingestion_id       build_fb4ba1391d82493d
principal_type     service          not a user: a different audit question
documents_written  2
documents_skipped  1                near-duplicate detection, a normal outcome
embedding_model    ...              the vectors mean nothing without it
policy_fingerprint sha256:...       the policy in force when written
```

The two records join on one field: `ingestion_id` is the same value a later
decision record carries as `index_build_id`. From an answer, to the decision
that produced it, to the ingestion that put the documents there.

It goes through the same failure policy as a decision, so `DENY` stops a write
it cannot record. An audit that stays up for reads and lets unrecorded writes
through is not an audit.

An `add()` that writes nothing, because everything was a near-duplicate,
mints no build and produces no record: an ingestion record describes a build,
and there wasn't one.

`PostgresAuditSink` puts these in `ingestion_events`, a separate table with
the same grants, because the shape is different and a nullable half of the
decisions table would be worse than two tables.

### Under an object lock

`PostgresAuditSink` makes the trail something the audited application cannot
edit. `ObjectLockSink` makes the stronger claim: nobody can, because the
store itself refuses. Every record is written as an object under a retention
lock in compliance mode, and S3 will not overwrite or delete it until the
retention date passes, whatever credentials are presented.

```python
from vectrixdb import ObjectLockSink, S3ObjectLockStore, DENY

audit = ObjectLockSink(
    S3ObjectLockStore("your-audit-bucket"),   # a bucket created with Object Lock on
    query_key=DEPLOYMENT_KEY,
    on_failure=DENY,
    retain_days=7 * 365,
)
```

Three things it insists on. The bucket has to have Object Lock enabled, and
the sink checks at construction and refuses otherwise, because the same put
lands in an unlocked bucket and stays exactly as long as anybody with delete
permission wants it to, which is a log. `retain_days` has no default, since
how long a trail is kept is a business decision. And `mode` is `COMPLIANCE`
unless you say `GOVERNANCE`, because governance mode answers the regulator's
question with "nobody except us".

One object per record by default. `batch_size` above one is available and
means what it says: a record in a batch that has not been flushed is not in
the store yet, so batching needs `on_failure=Spool(...)` to hold what the
store has not accepted, and a process that dies with a batch in memory loses
that batch. Every key carries the date, a sequence number and the hash of the
body, so an object that does not hash to its own name was not the one written
under it, and `read_all()` refuses to return it.

The fake store the suite runs against enforces the lock; the claim that S3
does is made by S3, in a gated test behind `VECTRIXDB_LIVE_BACKENDS=s3_object_lock`.

### Where to put it

Not a file on the application server: it rotates away, it dies with a
scale-in, and whatever can write it can rewrite it. `PostgresAuditSink` above,
or `ObjectLockSink` below, which is what satisfies a regulator asking whether
the record could have been altered. Forward to your SIEM
as well, but do not make the SIEM the system of record: its retention is
usually shorter than the regulatory requirement and it is built for detection
rather than proof.

One thing people get backwards: **the audit log is more sensitive than the
index it audits.** It records, per principal, which restricted documents exist
and who was refused them, so somebody holding it can reconstruct the whole
wall structure. It needs stricter access control than the collection, not the
same and certainly not looser.

### What attaching a sink costs

The withheld counts need the documents the policy rejected, and a filter the
engine applies throws those away. So a search with a sink attached fetches a
wider window with your filter alone and evaluates the policy in Python. That
is what the local backend does internally regardless; on a backend that pushes
filters into SQL it trades the pushdown for the counts. The results are the
same either way, which the test suite asserts directly.

## From the dashboard and the REST API

The server refuses a collection that carries a policy to an API key, because a
key is nobody in particular and the policy has nobody to judge. Turn
[sign-in](sign-in.md) on and a person's principal comes from their identity
provider's groups, or from what an admin wrote beside their address, so the
same policy judges their searches in the dashboard, and the decision record
is written under their name.
