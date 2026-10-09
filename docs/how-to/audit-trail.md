# Audit trail

The server keeps two records, and they answer different questions.

| | Access log | Audit trail |
| --- | --- | --- |
| Answers | Who was looking: who signed in, read, searched, wrote or was refused | Why a search returned what it did: what the policy decided |
| One line for | Every sign-in, read, search, write and refusal, and every change to people, keys and policies | Every search a collection's entitlement policy judged |
| Holds | The person, the action, the collection, the one chunk or document opened | The person's entitlements at that moment, the policy's fingerprint, the rules, a keyed hash of the query, how many results were returned and withheld |
| Never holds | A query, a chunk's text, what a search returned | The query itself, or any chunk's text |
| Needs | Sign-in on | Sign-in on, a collection with an [entitlement policy](entitlements.md), and `VECTRIXDB_AUDIT_STORE` |
| Kept in | A file, the server's output, or Azure Blob | A file, Azure Blob, S3 under Object Lock, or PostgreSQL |
| Read with | The Access page, `GET /api/v1/access` | The Audit page, `GET /api/v1/audit` |
| Who may read | An admin: `access.read` | An admin: `audit.read` |

Both fail closed. A read that cannot be written to the access log is refused
with a `503`, and so is a search whose decision cannot be written to the audit
trail.

## The access log

On by default with sign-in, at `<database path>/auth/access.jsonl`, or in the
folder `VECTRIXDB_AUTH_PATH` names. `VECTRIXDB_ACCESS_LOG` puts it elsewhere:

| `VECTRIXDB_ACCESS_LOG` | Where the lines go |
| --- | --- |
| a path | A file on this machine |
| `stdout` | The server's output, each line marked `"log": "vectrixdb.access"`, for a platform that collects it. The Access page cannot list it then. |
| `https://<account>.blob.core.windows.net/<container>/<prefix>` | One append blob a UTC day, `<prefix>2026/09/23.jsonl`, shared by every server |

```json
{"access_id": "acc_5f0c…", "at": 1789742000.1, "event": "search", "who": "ada@example.com", "role": "operator", "method": "oidc", "action": "search", "collection": "contracts", "route": "POST /api/v1/collections/contracts/text-search", "status": 200, "took_ms": 41.3}
{"access_id": "acc_77d1…", "at": 1789742090.4, "event": "denied", "who": "grace@example.com", "role": "operator", "method": "oidc", "action": "search", "collection": "contracts", "route": "POST /api/v1/collections/contracts/text-search", "status": 403, "reason": "not_on_list"}
```

`GET /api/v1/access` lists it newest first: `limit` and `offset` for the page,
`q` to find anything a line says, `event` and `who` to narrow it. The events
are sign-ins and sign-outs, changes to people, passkeys, authenticators,
passwords and recovery codes, `key_created` and `key_revoked`,
`policy_changed`, `access_checked`, and `read`, `search`, `write` and
`denied`. A refusal by a [collection policy](collection-policies.md) carries
its code as the `reason`.

The whole of the access log, from what it holds to the search time on the
Overview page, is in [The access log](sign-in.md#the-access-log).

## When a decision is recorded

A search writes one decision record when all of these hold:

- the collection carries an entitlement policy;
- a person signed in, or an app acting with their token, made the search, so
  there is a principal to judge;
- the server has an audit store, `VECTRIXDB_AUDIT_STORE`.

The routes that write one are `search`, `text-search`, `keyword-search` and
`similar`. An API key never gets this far: a collection with an entitlement
policy is closed to every key.

The server writes with the failure policy `DENY`. If the record cannot be
written, the search is not served, and the reply is a `503` saying the audit
trail cannot be written.

## Where the trail is kept

```bash
export VECTRIXDB_AUDIT_STORE=s3://audit-example/vectrixdb/
export VECTRIXDB_AUDIT_RETAIN_DAYS=2555
export VECTRIXDB_AUDIT_QUERY_KEY="<a long random key, kept away from the audit store>"
```

| `VECTRIXDB_AUDIT_STORE` | How each record is kept | Read back by the server |
| --- | --- | --- |
| a path | One JSON line a record, in a file | yes |
| `https://<account>.blob.core.windows.net/<container>/<prefix>` | Lines in append blobs, one a UTC day. The container must carry an immutability policy, or the log refuses to open. | yes |
| `s3://<bucket>/<prefix>` | One object a record, under Object Lock in compliance mode, for `VECTRIXDB_AUDIT_RETAIN_DAYS` days. The bucket must have Object Lock on. | yes |
| `postgresql://<user>@<host>/<database>` | One row a record, in a table the server's role may only `INSERT` into | no |

- **A file** is honest for one server with a disk that lasts. It is not a
  system of record: it rotates away, dies with a scale-in, and whatever can
  write it can rewrite it.
- **Blob** keeps a line that cannot be changed or deleted until the
  container's retention runs out, by the server or anybody else. Several
  servers append to the same day's blob safely.
- **S3** keeps each record as long as `VECTRIXDB_AUDIT_RETAIN_DAYS` says, and
  nobody can shorten it. The setting has no default, because how long a trail
  is kept is a business decision. `vectrixdb check` reports an `s3://` store
  without it, and the server cannot open one. Without a prefix in the address, records go under `vectrixdb-audit/`.
- **PostgreSQL** is written with `INSERT` only, which is the point, so the
  server cannot read it back. Read it where it is kept. Setting up the table
  and its grants is in [Putting it in PostgreSQL](entitlements.md#putting-it-in-postgresql).

`VECTRIXDB_AUDIT_STORE_FILE` reads the address from a file, for one that holds
a password. `VECTRIXDB_AUDIT_JSONL` is the older name for a file, and still
works; when both are set, `VECTRIXDB_AUDIT_STORE` wins.

Put the trail somewhere the server it audits cannot rewrite. It is more
sensitive than the index: it says, person by person, which restricted
documents exist and who was refused them.

## Queries are hashed

A query often names what it is about, so the record never holds it. It holds
`query_fingerprint`, an HMAC-SHA256 of the query under a key:

```text
"query_fingerprint": "hmac-sha256:3f9c…"
```

The key is `VECTRIXDB_AUDIT_QUERY_KEY`, or one derived from the sign-in secret
when that is unset. A plain SHA-256 of a short query could be reversed by
anybody holding the log, by hashing likely queries until one matches; with a
key kept away from the log, the same query gives the same fingerprint, so two
records can be matched, and the text cannot be recovered. Keep the key out of
the audit store's reach.

## A record

```json
{"record_schema_version": 2,
 "decision_id": "dec_752d298922934cbc",
 "decided_at": "2026-10-09T14:03:11.402000+00:00",
 "collection": "contracts",
 "index_build_id": "build_fb4ba1391d82493d",
 "pushdown_mode": "post",
 "principal_id": "ada@example.com",
 "principal_type": "user",
 "policy_fingerprint": "sha256:…",
 "rules_evaluated": ["…"],
 "query_fingerprint": "hmac-sha256:…",
 "candidates_examined": 40,
 "results_returned": 5,
 "withheld_disclosable": 1,
 "duration_ms": 38.2,
 "outcome": "allowed_with_redaction"}
```

The full record also holds the principal snapshot, the ids of the chunks
returned, and `withheld_undisclosable`: how many documents outside the
person's scope matched. That last number must never reach the person, since
it confirms that documents they were refused exist. What each field means is
in [Recording the decision](entitlements.md#recording-the-decision).

The server writes four outcomes:

| `outcome` | Means |
| --- | --- |
| `allowed` | Everything the person matched was returned |
| `allowed_with_redaction` | Returned, with some documents in their scope withheld by a rule |
| `denied_in_scope` | They matched the scope, and every candidate was withheld |
| `denied_out_of_scope` | They did not match the scope |

## Read the trail

![Audit: five totals, decisions a day by outcome, the same by collection, and the newest records](../images/dashboard/audit.png)

The dashboard's **Audit** page shows the totals, the decisions a day by
outcome, the same by collection, and the records, newest first. Or ask the
API:

```bash
curl -s "https://vectors.example.com/api/v1/audit?collection=contracts&limit=50" \
  -H "api-key: $ADMIN_KEY"
```

| Parameter | What it does | Default |
| --- | --- | --- |
| `limit` | Records in the page, 1 to 1000 | 100 |
| `offset` | Where the page starts | 0 |
| `q` | Only records that hold this in a value: an id, a person, an outcome | |
| `collection` | Only this collection | |
| `days` | How far back the counts a day go, 1 to 90 | 14 |

The reply carries `counts` (decisions, ingestions, denied, undecidable,
refused), `daily` and `by_collection` over every record in the window,
`records` for the page and `total`. Records go out with an allowlist of
fields: the principal snapshot, the result ids and `withheld_undisclosable`
stay in the store, and a field added to the record later stays there too
until somebody decides it may be shown.

With sign-in on, it needs `audit.read`, an admin's. With sign-in off, it needs
`VECTRIXDB_API_KEY` set and sent, and a server with no key never serves it.
A server with no audit store, or one in PostgreSQL, answers
`"available": false` with the reason.

## Look a decision up

Every record has a `decision_id`, `dec_` and sixteen hex digits. It names a
decision and never a document, so it is safe to show, to log, and to put in a
support ticket. In the library, a search's results carry it as
`results.decision_id`; see
[Tying an answer back to its record](entitlements.md#tying-an-answer-back-to-its-record).

Given an id, find its record:

```bash
curl -s "https://vectors.example.com/api/v1/audit?q=dec_752d298922934cbc" \
  -H "api-key: $ADMIN_KEY"
```

A search over the REST API writes its record without naming the id in the
reply, so find it there by collection, person and time.

The record's `index_build_id` names the ingestion that built the index which
answered, and the record can be replayed against it to restate which chunks
that person could reach. See [Trace an answer to its source](lineage.md).

## See also

- [Restrict what a search can see](entitlements.md): the policies that make
  decisions, and audit sinks in the library.
- [Sign people in](sign-in.md#the-access-log): the access log in full.
- [Collection policies](collection-policies.md): who may search a collection.
- [Settings](../reference/settings.md#audit-and-evaluation): every audit
  setting.
