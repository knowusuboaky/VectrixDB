<!-- Written by scripts/make_reference.py from the code. Edit the code, then run it again. -->

# REST API

Every documented route the server answers, 104 of them, grouped as the interactive docs at `/docs` group them. The last column is who may call it once sign-in is on: the action the role table places it under, the roles that hold that action, the key roles that do, and whether it needs a check from the last ten minutes. With sign-in off, the API key rules in [Run the REST API](../how-to/rest-api.md) apply instead. A route no action places is for admins only, so an endpoint added later is closed until somebody decides who it is for. Older aliases under `/api/` that the dashboard used are left out.

## Auth

| Method | Path | What it does | Who may call it |
| --- | --- | --- | --- |
| `GET` | `/api/v1/keys` | List Keys | `keys.manage`: admin |
| `POST` | `/api/v1/keys` | Create Key | `keys.manage`: admin; a fresh check |
| `DELETE` | `/api/v1/keys/{key_id}` | Revoke Key | `keys.manage`: admin; a fresh check |
| `POST` | `/auth/email/begin` | Email Begin | anybody |
| `POST` | `/auth/email/enrol/begin` | Spend the emailed link, and hand back what either way of signing in needs. | anybody |
| `POST` | `/auth/email/enrol/confirm` | Enrol Confirm | anybody |
| `POST` | `/auth/email/verify` | Email Verify | anybody |
| `GET` | `/auth/me` | Who is signed in, and what they may do. The dashboard asks this first. | anybody |
| `DELETE` | `/auth/me/authenticator` | Remove your authenticator app. Not your only way in, unless single sign-on still lets you in. | `self.secure`: viewer, operator, admin; a fresh check |
| `POST` | `/auth/me/authenticator/begin` | Replace Authenticator Begin | `self.secure`: viewer, operator, admin; a fresh check |
| `POST` | `/auth/me/authenticator/confirm` | Replace Authenticator Confirm | `self.secure`: viewer, operator, admin; a fresh check |
| `POST` | `/auth/me/passkeys/begin` | Add Passkey Begin | `self.secure`: viewer, operator, admin; a fresh check |
| `POST` | `/auth/me/passkeys/finish` | Add Passkey Finish | `self.secure`: viewer, operator, admin; a fresh check |
| `DELETE` | `/auth/me/passkeys/{credential_id}` | Remove My Passkey | `self.secure`: viewer, operator, admin; a fresh check |
| `POST` | `/auth/me/password` | Set My Password | `self.secure`: viewer, operator, admin; a fresh check |
| `POST` | `/auth/me/recovery-codes` | Make Recovery Codes | `self.secure`: viewer, operator, admin; a fresh check |
| `GET` | `/auth/me/sessions` | My Sessions | `self.manage`: viewer, operator, admin |
| `POST` | `/auth/me/sessions/end-others` | End My Other Sessions | `self.manage`: viewer, operator, admin |
| `DELETE` | `/auth/me/sessions/{session_id}` | End My Session | `self.manage`: viewer, operator, admin |
| `GET` | `/auth/me/ways` | My Ways | `self.manage`: viewer, operator, admin |
| `POST` | `/auth/passkey/begin` | Sign in with a passkey. No address is asked for: the device offers what it holds for this site. | anybody |
| `POST` | `/auth/passkey/enrol/begin` | The first way in, as a passkey: options for the browser, against the ticket the emailed link gave. | anybody |
| `POST` | `/auth/passkey/enrol/finish` | Passkey Enrol Finish | anybody |
| `POST` | `/auth/passkey/finish` | Passkey Finish | anybody |
| `POST` | `/auth/password/forgot` | A link to choose a new password. The same answer for everybody, and the link alone opens nothing. | anybody |
| `POST` | `/auth/password/reset` | Password Reset | anybody |
| `GET` | `/auth/people` | Everybody on the server's own list, or a page of them: ``q`` finds by address or role, ``limit`` 0 is everybody. | `people.manage`: admin |
| `POST` | `/auth/people` | Put Person | `people.manage`: admin; a fresh check |
| `DELETE` | `/auth/people/{email}` | Remove Person | `people.manage`: admin; a fresh check |
| `POST` | `/auth/people/{email}/reset` | Forget every way somebody signs in. Their next sign-in starts with a new link by email. | `people.manage`: admin; a fresh check |
| `POST` | `/auth/signout` | Signout | whoever is signed in |
| `GET` | `/auth/status` | Check if authentication is enabled. | anybody |
| `POST` | `/auth/step-up` | Prove it is still you, with the authenticator code, before a change that matters. | `self.manage`: viewer, operator, admin |
| `POST` | `/auth/step-up/passkey/begin` | Step Up Passkey Begin | `self.manage`: viewer, operator, admin |
| `POST` | `/auth/step-up/passkey/finish` | Step Up Passkey Finish | `self.manage`: viewer, operator, admin |

## Cache

| Method | Path | What it does | Who may call it |
| --- | --- | --- | --- |
| `DELETE` | `/api/v1/cache` | Clear all cached data. | `cache.clear`: admin |
| `GET` | `/api/v1/cache/stats` | Get cache statistics. | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |

## Collections

| Method | Path | What it does | Who may call it |
| --- | --- | --- | --- |
| `GET` | `/api/v1/collections` | List all collections, with the policied ones named and no more. | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `POST` | `/api/v1/collections` | Create a new collection. | `collection.create`: operator, admin; keys: operator |
| `DELETE` | `/api/v1/collections/{name}` | Delete a collection. | `collection.delete`: admin; a fresh check |
| `GET` | `/api/v1/collections/{name}` | Get collection details. | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `PUT` | `/api/v1/collections/{name}/policy` | Who may search a collection, in the record every server reads. Nobody, until it is set. | `collection.share`: admin; a fresh check |
| `GET` | `/api/v1/policies` | Who may search each collection: its policy, as a count for everyone and as the list for people signed in. | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `POST` | `/api/v2/collections` | Create a collection with advanced options (v2 API). | `collection.create`: operator, admin; keys: operator |

## Documents

| Method | Path | What it does | Who may call it |
| --- | --- | --- | --- |
| `GET` | `/api/v1/collections/{name}/documents` | List Documents | `content.read`: operator, admin; keys: reader, searcher, operator |
| `POST` | `/api/v1/collections/{name}/documents` | Read a file, cut it, embed it and write it, as ``add_document`` does. | `content.write`: operator, admin; keys: operator |
| `DELETE` | `/api/v1/collections/{name}/documents/{doc_id}` | Delete Document | `content.write`: operator, admin; keys: operator |
| `GET` | `/api/v1/collections/{name}/documents/{doc_id}` | The Markdown a document was indexed from, as text. | `document.read`: admin; keys: reader |
| `GET` | `/api/v1/documents` | The document index, or a page of it: ``q`` finds by title, id or type; ``limit`` 0 is every document; ``total`` is how many match. | `content.read`: operator, admin; keys: reader, searcher, operator |
| `POST` | `/api/v1/documents` | Index a new document. | `content.write`: operator, admin; keys: operator |
| `DELETE` | `/api/v1/documents/{doc_id}` | Delete an indexed document. | `content.write`: operator, admin; keys: operator |
| `GET` | `/api/v1/documents/{doc_id}` | Get a document with its tree structure. | `document.read`: admin; keys: reader |
| `GET` | `/api/v1/documents/{doc_id}/chunks` | Get chunks from a document for embedding. | `document.read`: admin; keys: reader |
| `GET` | `/api/v1/extractors` | Which file types this server reads, and who reads them. The Ingest | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |

## Enterprise-search

| Method | Path | What it does | Who may call it |
| --- | --- | --- | --- |
| `POST` | `/api/v1/collections/{name}/search/acl` | Search with ACL-based security filtering. | `search`: operator, admin; keys: searcher, operator |
| `POST` | `/api/v1/collections/{name}/search/enterprise` | Full enterprise search combining all advanced features: | `search`: operator, admin; keys: searcher, operator |
| `POST` | `/api/v1/collections/{name}/search/facets` | Search with faceted aggregations. | `search`: operator, admin; keys: searcher, operator |
| `POST` | `/api/v1/collections/{name}/search/rerank` | Two-stage retrieval: fast ANN search followed by precise re-ranking. | `search`: operator, admin; keys: searcher, operator |

## Evaluations

| Method | Path | What it does | Who may call it |
| --- | --- | --- | --- |
| `GET` | `/api/v1/chunking` | Every saved chunking run, newest first: when, which golden file, which collections it cut, and each technique's count. | `evaluation.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `GET` | `/api/v1/chunking/{run}` | One chunking run: every build, each technique at its best, the head to head between them, and the pick. | `evaluation.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `GET` | `/api/v1/chunking/{run}/golden` | The golden file a chunking run used, exactly as it was read. A signed-in admin only, as a person. | `evaluation.golden`: admin; signed in as a person, never a key |
| `GET` | `/api/v1/evaluations` | Every saved run, newest first: when, which golden file, which collections it searched, and each setup's top 10 count. | `evaluation.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `GET` | `/api/v1/evaluations/{run}` | One run's report: every setup's numbers, the three picks and the rules they were chosen by. | `evaluation.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `GET` | `/api/v1/evaluations/{run}/golden` | The golden file a run used, exactly as it was read: every question, reference answer and expected id. | `evaluation.golden`: admin; signed in as a person, never a key |

## Graph

| Method | Path | What it does | Who may call it |
| --- | --- | --- | --- |
| `GET` | `/api/v1/collections/{name}/graph` | Get knowledge graph data for visualization. | `content.read`: operator, admin; keys: reader, searcher, operator |
| `POST` | `/api/v1/collections/{name}/graph/extract` | Extract entities and relationships from collection documents. | `collection.maintain`: operator, admin; keys: operator |

## Info

| Method | Path | What it does | Who may call it |
| --- | --- | --- | --- |
| `GET` | `/` | Root endpoint. With sign-in on, the version is said only to somebody signed in. | anybody |
| `GET` | `/api/v1` | API v1 root - list available endpoints. | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `GET` | `/api/v1/` | API v1 root - list available endpoints. | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `GET` | `/api/v1/about` | Which VectrixDB this is, its licence, and the NOTICE that travels with it. For admins: About in the account menu. | `about.read`: admin |
| `GET` | `/api/v1/about/licence` | The Apache License, Version 2.0, as the package carries it, in plain text. | `about.read`: admin |
| `GET` | `/api/v1/info` | Get database information. | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `GET` | `/api/v1/info/extended` | Get extended database information including storage, cache, and scaling stats. | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `GET` | `/api/v1/ws/status` | Get WebSocket connection status. | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `GET` | `/health` | Health check. | anybody |

## Inspect

| Method | Path | What it does | Who may call it |
| --- | --- | --- | --- |
| `GET` | `/api/v1/access` | The access log, newest first, a page at a time: ``q`` finds in anything a line says, ``total`` is how many match. | `access.read`: admin |
| `POST` | `/api/v1/access/check` | Whether somebody may retrieve from a collection, and why. Nothing is searched. | `access.check`: admin |
| `GET` | `/api/v1/access/daily` | Searches a day and how long they took, for everyone; sign-ins, refusals and policy denials too, for whoever may read the access log. | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `GET` | `/api/v1/access/readers` | Who searched and read most in the last days, the busiest first, a page at a time. | `access.read`: admin |
| `GET` | `/api/v1/audit` | The most recent audit records, newest first, a page at a time, from where the server records them. | `audit.read`: admin |
| `GET` | `/api/v1/collections/{name}/builds` | Every index build that still has chunks in the collection. | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `GET` | `/api/v1/collections/{name}/growth` | Chunks written on each of the last days, today last, counted from the chunks that are here. | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `GET` | `/api/v1/collections/{name}/health` | What state a collection is in, as the collections page shows it. | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `GET` | `/api/v1/collections/{name}/policy` | The entitlement policy as data: rules, kinds and the fingerprint. | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `GET` | `/api/v1/collections/{name}/provenance/{point_id}` | Where one chunk came from, as its metadata records it. | `content.index`: viewer, operator, admin; keys: reader, searcher, operator |
| `GET` | `/api/v1/collections/{name}/quality` | Extraction quality across the collection, from the ``_vx_quality`` stamps. | `content.read`: operator, admin; keys: reader, searcher, operator |
| `POST` | `/api/v1/collections/{name}/rebuild` | Rebuild the ANN index from the live vectors, dropping tombstones. | `collection.maintain`: operator, admin; keys: operator |
| `GET` | `/api/v1/growth` | Chunks written on each of the last days across every collection, today last, with how many of them read badly. | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |
| `GET` | `/api/v1/models` | Every model the library knows, and whether it is here. | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |

## Monitoring

| Method | Path | What it does | Who may call it |
| --- | --- | --- | --- |
| `GET` | `/api/v1/resources` | Get current resource utilization stats. | `meta.read`: viewer, operator, admin; keys: reader, searcher, operator |

## Points

| Method | Path | What it does | Who may call it |
| --- | --- | --- | --- |
| `DELETE` | `/api/v1/collections/{name}/points` | Delete points from a collection. | `content.write`: operator, admin; keys: operator |
| `GET` | `/api/v1/collections/{name}/points` | List points in a collection, a page at a time. | `content.index`: viewer, operator, admin; keys: reader, searcher, operator |
| `POST` | `/api/v1/collections/{name}/points` | Add points to a collection. | `content.write`: operator, admin; keys: operator |
| `GET` | `/api/v1/collections/{name}/points/{point_id}` | Get a point by ID. | `content.read`: operator, admin; keys: reader, searcher, operator |
| `POST` | `/api/v1/collections/{name}/text-upsert` | Insert points with automatic text embedding. | `content.write`: operator, admin; keys: operator |
| `POST` | `/api/v2/collections/{name}/points` | Add points with text for hybrid search (v2 API). | `content.write`: operator, admin; keys: operator |
| `POST` | `/api/v2/collections/{name}/points/sparse` | Add points with sparse vectors for hybrid dense+sparse search. | `content.write`: operator, admin; keys: operator |

## Search

| Method | Path | What it does | Who may call it |
| --- | --- | --- | --- |
| `POST` | `/api/v1/collections/{name}/dense-sparse-search` | Hybrid search combining dense and sparse vectors (Qdrant-style). | `search`: operator, admin; keys: searcher, operator |
| `POST` | `/api/v1/collections/{name}/hybrid-search` | Hybrid search combining vector similarity and keyword matching. | `search`: operator, admin; keys: searcher, operator |
| `POST` | `/api/v1/collections/{name}/keyword-search` | Full-text keyword search using BM25 ranking. | `search`: operator, admin; keys: searcher, operator |
| `POST` | `/api/v1/collections/{name}/search` | Search for similar vectors. | `search`: operator, admin; keys: searcher, operator |
| `POST` | `/api/v1/collections/{name}/sparse-search` | Search using sparse vectors only. | `search`: operator, admin; keys: searcher, operator |
| `POST` | `/api/v1/collections/{name}/text-hybrid-search` | Hybrid search with automatic text embedding. | `search`: operator, admin; keys: searcher, operator |
| `POST` | `/api/v1/collections/{name}/text-search` | Semantic search using text query. | `search`: operator, admin; keys: searcher, operator |
