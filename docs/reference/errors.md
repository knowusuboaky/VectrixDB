<!-- Written by scripts/make_reference.py from the code. Edit the code, then run it again. -->

# Errors

Every way the server and the extraction service say no, read off the code, so a refusal added without its row fails a test. Each refusal a route sends has the one shape, `{"ok": false, "message": ..., "data": ..., "detail": ...}`: `message` is one sentence a person can act on, `detail` is what the route gave, kept for clients that read it, and `data` carries what a client needs to go on, such as `signin` when the answer is to sign in, `retry_after` on a rate limit, or `code` when a policy refused. The status says what kind of no it is:

| Status | What it means | What to do |
| --- | --- | --- |
| 400 | The request is well formed and one of its values is wrong. | Fix the value the message names. |
| 401 | Nobody is signed in, or the key is wrong. | Sign in, or send the key the server was given. |
| 403 | Somebody is here, and this is not theirs to do. | A role, a scope, a fresh check, or a collection they may not retrieve from. |
| 404 | It is not there, or it is not yours to know about. | A collection that is private to others answers exactly as one that does not exist. |
| 409 | It would clash with what is there already. | Read what is there, then decide. |
| 413 | The file is too big. | `VECTRIXDB_MAX_UPLOAD_BYTES` says how big. |
| 415 | The body is in a form this server cannot take. | Send the file as the request body, or give the server python-multipart. |
| 422 | The request is not the shape the route takes, or a file could not be read. | `detail` names the field, or the file. |
| 429 | Too many, too fast. | Wait `retry_after` seconds. |
| 500 | The server's own fault, and it says what. | Read the message; it names the setting or the piece. |
| 501 | Not on this server. | A reader this build does not include. |
| 502 | A service the server called answered badly. | The message names the service and its status. |
| 503 | A service or a store the server needs is not there right now, and nothing was done. | Try again; the message names what was missing. |

Exceptions the library raises in Python are on [Exceptions](exceptions.md); the table at the end lists them. The server turns a `PolicyError` into 403 (or 503 for a records store that cannot be read), a route's `HTTPException` into its own status, and a body that is not the shape asked for into 422. The extraction service turns a `DependencyError` into 503, an `ExtractionError` into 502 when a service answered badly and 422 when the file could not be read, and a `ConfigurationError` in what was asked into 422.

## What a route can send, 144 refusals

### Sign-in and the door

`vectrixdb/api/signin.py`

| Status | Message | Raised by |
| --- | --- | --- |
| 400 | Confirm with single sign-on | `step_up` |
| 400 | That is this browser. Sign out instead. | `end_my_session` |
| 400 | the error's own words | `put_person`, `create_key` |
| 400 | the reason the provider gave | `add_passkey_finish` |
| 400 | This is for somebody on this server's People list, signed in at the dashboard, not a key | `_local_caller` |
| 400 | This is for somebody signed in to the dashboard | `_session_caller` |
| 400 | This link has been used or has expired. Ask for a new one from the sign-in page. | `enrol_begin`, `password_reset` |
| 400 | This set-up has expired. Ask for a new link from the sign-in page. | `enrol_confirm`, `passkey_enrol_begin`, `passkey_enrol_finish` |
| 400 | what is wrong with the password, in words | `password_reset`, `set_my_password` |
| 401 | API key required for write operations. Provide api-key header. | `_keys_only` |
| 401 | API key required. Provide api-key header. | `_keys_only` |
| 401 | Developer Access is off. Sign in again. | `step_up` |
| 401 | Emergency sign-in is off. Sign in again. | `step_up` |
| 401 | Invalid API key | `dispatch` |
| 401 | Sign in to continue | `dispatch`, `_guest` |
| 401 | That code was not accepted. Codes change every 30 seconds, and each works once. | `password_reset`, `replace_authenticator_confirm`, `step_up` |
| 401 | That code was not accepted. Codes change every 30 seconds, and each works once., or That password and code were not accepted together. Check both and try again. when passwords are on | `email_verify` |
| 401 | That passkey is not registered here. | `passkey_finish` |
| 401 | That password was not accepted. | `step_up` |
| 401 | That username and password were not accepted together. Check both and try again. | `developer_signin` |
| 401 | That username and password were not accepted together. Check both and try again., or That password was used in an earlier emergency, and a password works for one. An operator sets a new one. when spent | `break_glass_signin` |
| 401 | the reason the provider gave | `passkey_finish`, `step_up_passkey_finish`, `dispatch` |
| 403 | `said` | `_sso_first` |
| 403 | Add a passkey to continue: this server signs people in with passkeys. | `dispatch` |
| 403 | Admins sign in with SSO on this server. | `_admin_needs_sso` |
| 403 | Confirm it's you to make this change | `dispatch` |
| 403 | Read-only API key cannot perform write operations | `_keys_only` |
| 403 | Sign in with your passkey: this server signs people in with passkeys. | `email_verify` |
| 403 | This change needs the person themselves, signed in at the dashboard. An app acting for them cannot make it. | `dispatch` |
| 403 | This key reaches {named} and nothing else, so {method} {path} is not its to make. | `outside_the_scope` |
| 403 | This request did not carry the session's forgery token | `dispatch` |
| 403 | This server signs people in with passkeys. Add a passkey instead. | `replace_authenticator_begin`, `replace_authenticator_confirm` |
| 403 | This server signs people in with passkeys. Make one to finish setting up. | `enrol_confirm` |
| 403 | why the policy refused, with its code in data | `_gate` |
| 403 | Your role does not allow this | `dispatch` |
| 404 | email sign-in is not turned on | `_email_on` |
| 404 | no authenticator app is set up | `remove_my_authenticator` |
| 404 | no such key | `revoke_key` |
| 404 | no such passkey | `remove_my_passkey` |
| 404 | no such session | `end_my_session` |
| 404 | nobody by that address | `remove_my_passkey`, `remove_my_authenticator`, `remove_person`, `reset_person` |
| 404 | Not Found | `_emergency`, `_developer` |
| 404 | Nothing is gated on this server: set VECTRIXDB_COLLECTION_STORE to where each collection's record is kept | `access_check` |
| 404 | Passkeys are not used on this server | `_passkeys_on` |
| 404 | passwords are not turned on for this server | `_passwords_on`, `set_my_password` |
| 404 | sign-in is not turned on for this server | `_need` |
| 404 | single sign-on is not turned on | `oidc_start`, `oidc_callback` |
| 404 | there is no People list on this server | `_people_on` |
| 409 | This is the only admin. Make somebody else an admin first. | `put_person`, `remove_person` |
| 409 | This is your only passkey, and this server signs people in with passkeys. Add another first. | `remove_my_passkey` |
| 409 | This is your only way in. Add a passkey first. | `remove_my_authenticator` |
| 409 | This is your only way in. Add another passkey or an authenticator app first. | `remove_my_passkey` |
| 429 | the rate limit's words, with retry_after in data | `_too_many` |
| 429 | Too many tries from here. Wait a few minutes and try again. | `passkey_begin` |
| 429 | Too many tries. Wait a while, then ask for a new link from the sign-in page. | `passkey_enrol_finish` |
| 429 | Too many wrong codes. Wait {runtime.store.lock_minutes(attempts)} minutes, then ask for a new link from the sign-in page. | `enrol_confirm` |
| 429 | Too many wrong codes. Wait {runtime.store.lock_minutes(key)} minutes and try again. | `replace_authenticator_confirm` |
| 429 | Too many wrong tries. Wait {runtime.store.lock_minutes(held)} minutes and try again. | `_locked_out` |
| 500 | the error's own words | `access_check`, `_gate` |
| 503 | The access log cannot be written, so this request was not served | `_timed_search`, `dispatch` |
| 503 | the error's own words | `access_check`, `_gate` |
| `status` | the rate limit's words, with retry_after in data | `_refuse` |

### Collections, points and search

`vectrixdb/api/server.py`

| Status | Message | Raised by |
| --- | --- | --- |
| 400 | Hybrid search requires text index. Create collection with enable_text_index=True | `text_hybrid_search` |
| 400 | Invalid metric: {request.metric} | `create_collection`, `create_collection_v2` |
| 400 | Missing required field: {e} | `add_points_v2`, `add_points_with_sparse` |
| 400 | the error's own words | `create_collection`, `create_collection_v2`, `set_policy`, `add_points`, `add_points_v2`, `search`, `text_search`, `text_upsert`, `hybrid_search`, `text_hybrid_search`, `keyword_search`, `sparse_search`, `dense_sparse_search`, `add_points_with_sparse`, `search_with_rerank`, `search_with_facets`, `search_with_acl`, `enterprise_search` |
| 400 | {name} was not made for graph search, so it has no knowledge graph. A collection gets one when it is made with mode="graph" in the library, --mode graph with vectrixdb ingest, or the Graph tag over the API. | `_no_graph` |
| 403 | this collection carries an entitlement policy, and this API does not resolve principals. Serve it from a tier that does. | `_policy_refusal` |
| 404 | Collection '{name}' not found | `_servable`, `_servable_as`, `get_collection`, `delete_collection`, `set_policy` |
| 404 | Document '{doc_id}' not found | `get_document`, `delete_document` |
| 404 | Document '{doc_id}' not found or has no chunks | `get_document_chunks` |
| 404 | no logo is set | `_logo_response` |
| 404 | Nothing is gated on this server: set VECTRIXDB_COLLECTION_STORE to where each collection's record is kept | `set_policy` |
| 404 | Point '{point_id}' not found | `get_point` |
| 404 | the licence file is not in this install | `about_licence` |
| 409 | the error's own words | `create_collection`, `create_collection_v2` |
| 422 | what did not validate, in words, with the fields in detail | `_not_the_shape_asked_for` |
| 500 | Error deleting collection: {str(e)} | `delete_collection` |
| 500 | Error extracting entities: {str(e)} | `extract_graph_entities` |
| 500 | Error loading graph: {str(e)} | `get_collection_graph` |
| 500 | Failed to delete document: {str(e)} | `delete_document` |
| 500 | Failed to embed and insert: {str(e)} | `text_upsert` |
| 500 | Failed to embed query: {str(e)} | `text_search`, `text_hybrid_search` |
| 500 | Failed to get chunks: {str(e)} | `get_document_chunks` |
| 500 | Failed to get document: {str(e)} | `get_document` |
| 500 | Failed to index document: {str(e)} | `index_document` |
| 500 | Failed to list documents: {str(e)} | `list_documents` |
| 500 | GraphRAG module not available | `get_collection_graph` |
| 500 | GraphRAG module not available: {str(e)} | `extract_graph_entities` |
| 500 | Text embedder not available: {str(e)} | `text_upsert` |
| 503 | Text embedder not available. Run: vectrixdb download-models. Error: {str(e)} | `get_text_embedder` |
| 503 | the audit trail cannot be written, so this search was not served | `_record_decision` |
| 503 | the error's own words | `_policy_refusal` |
| `exc.status_code` | `message_of(exc.detail)` | `_route_refusal` |

### Documents

`vectrixdb/api/documents.py`

| Status | Message | Raised by |
| --- | --- | --- |
| 400 | chunk is recursive, sentence, markdown or fixed | `add_document` |
| 400 | metadata is a JSON object | `add_document` |
| 400 | metadata is not JSON: {exc} | `add_document` |
| 400 | Name the file in X-Filename, for example X-Filename: report.pdf | `_upload` |
| 400 | the error's own words | `add_document` |
| 400 | The form has no file field | `_upload` |
| 400 | The request has no file in it | `_upload` |
| 404 | No document {doc_id} | `delete_document` |
| 404 | No kept document {doc_id} | `get_document` |
| 404 | This server does not keep documents. Start it with VECTRIXDB_KEEP_SOURCE=1 and the Markdown each document was indexed from is kept and served here. | `_kept` |
| 413 | The file is larger than {MAX_UPLOAD_BYTES} bytes | `_upload` |
| 415 | A multipart form needs python-multipart on the server. Send the file as the request body with its name in X-Filename instead, which needs nothing. | `_upload` |
| 422 | Nothing could be read from {filename} | `add_document` |
| 422 | the error's own words | `add_document` |
| 500 | Text embedder not available: {exc} | `add_document` |
| 500 | the error's own words | `configured_extractors` |
| 500 | VECTRIXDB_KEEP_SOURCE is a Blob address with no container in it | `_files_at` |
| 502 or 422 | the error's own words | `add_document` |
| 503 | the error's own words | `add_document` |

### Inspection, provenance and the audit trail

`vectrixdb/api/inspection.py`

| Status | Message | Raised by |
| --- | --- | --- |
| 400 | the error's own words | `rebuild_collection` |
| 403 | the audit trail is never served from a server without an API key | `audit_trail` |
| 403 | the audit trail needs the API key | `audit_trail` |
| 403 | the policy needs the API key | `collection_policy` |
| 404 | Collection '{name}' not found | `collection_health`, `collection_policy` |

### Evaluations

`vectrixdb/api/evaluations.py`

| Status | Message | Raised by |
| --- | --- | --- |
| 403 | Only an admin signed in as a person can download the golden questions | `download_golden`, `download_chunking_golden` |
| 404 | No run '{run}', or which run there is no such of when run == 'latest' | `_report` |
| 404 | This run's golden questions were not kept | `_golden_file` |
| 404 | which run there is no such of | `_report` |
| 503 | the error's own words | `_store` |

### The one answer for a collection that is not there

`vectrixdb/api/replies.py`

| Status | Message | Raised by |
| --- | --- | --- |
| 404 | Collection '{name}' not found | `collection_not_found` |

### The extraction service

`vectrixdb/api/extraction.py`

| Status | Message | Raised by |
| --- | --- | --- |
| 400 | the request has no body: send the file as it is | `_body` |
| 403 | no address may be fetched: set VECTRIXDB_EXTRACT_URL_HOSTS to the hosts that may | `fetch` |
| 403 | {req.full_url} redirected to {newurl}, which is not an allowed host | `redirect_request` |
| 403 | {urlparse(url).hostname or url} is not one of the hosts this service fetches from | `fetch` |
| 404 | there is no job {job} | `job_status` |
| 413 | the file is larger than {service.max_bytes} bytes | `_body` |
| 413 | {url} is larger than {self.max_bytes} bytes | `fetch` |
| 422 | output_dir is one folder name, letters, digits, spaces, dots, dashes and underscores | `transcribe_youtube_save` |
| 422 | the error's own words | `_bad_value`, `_answer`, `mask_route` |
| 422 | what did not validate, in words, with the fields in detail | `_shape` |
| 422 | {body.url} is not the address of one YouTube video | `transcribe_youtube_save` |
| 502 | the error's own words | `_translation` |
| 502 | {url} answered {status} | `fetch` |
| 502 or 422 | the error's own words | `_unreadable` |
| 503 | nothing reads pictures: set AZURE_DOCINTEL_ENDPOINT and AZURE_DOCINTEL_KEY | `read_image` |
| 503 | nothing reads sound: set AZURE_SPEECH_ENDPOINT and AZURE_SPEECH_KEY | `listening_in` |
| 503 | the error's own words | `_missing`, `_unreadable` |
| 503 | translation is not set up: set AZURE_TRANSLATOR_KEY, and AZURE_TRANSLATOR_REGION for a regional resource | `translator` |
| `exc.status_code` | `message_of(exc.detail)` | `_http` |
| `exc.status` | the error's own words | `_refused` |

## Exceptions the library raises

| Exception | Derives from | What it means |
| --- | --- | --- |
| `VectrixError` | `Exception` | Base class for every error raised by VectrixDB. |
| `ConfigurationError` | `VectrixError` | A configuration value is missing, malformed, or mutually exclusive. |
| `DependencyError` | `VectrixError` | An optional dependency is required for this code path but not installed. |
| `ExtractionError` | `VectrixError` | Turning a file into text failed, and nothing was written. |
| `TranslationError` | `VectrixError` | A translation, a language detection or the language list failed. |
| `StorageError` | `VectrixError` | Base class for storage backend failures. |
| `StorageConnectionError` | `StorageError` | The storage backend could not be reached or authenticated against. |
| `StorageOperationError` | `StorageError` | A storage operation failed. |
| `CollectionNotFoundError` | `VectrixError` | The requested collection does not exist. |
| `CollectionAlreadyExistsError` | `VectrixError` | A collection with this name already exists. |
| `DocumentNotFoundError` | `VectrixError` | The requested document does not exist. |
| `DimensionMismatchError` | `VectrixError` | A vector's dimension does not match the collection's dimension. |
| `SearchError` | `VectrixError` | A search could not be completed. |
| `GraphUnavailable` | `SearchError` | Graph search could not run because there is no graph to run it on. |
| `QuantizationError` | `VectrixError` | A vector could not be quantized or dequantized. |
| `IndexBuildError` | `VectrixError` | An index could not be built or loaded. |
| `ModelError` | `VectrixError` | Base class for embedded model failures. |
| `ModelNotFoundError` | `ModelError, FileNotFoundError` | A requested model is not bundled and is not present on disk. |
| `ModelDownloadError` | `ModelError` | A model could not be downloaded. |
| `ModelMismatchWarning` | `UserWarning` | A collection was opened with a different embedding model than it was built with. Queries embed with one model and documents with another, so scores are meaningless until ``reembed()`` runs. |
| `InvalidCollectionName` | `VectrixError, ValueError` | A collection name cannot be used as a directory name. |
| `SparseModelUnavailableWarning` | `UserWarning` | A named sparse model cannot be the one that actually runs. |
| `CollectionLoadWarning` | `UserWarning` | A registered collection would not open, so it is not in the database. |
| `PolicyError` | `VectrixError` | A policied collection could not answer, and said so. |
| `PolicyDefinitionError` | `PolicyError, ValueError` | The policy itself is malformed, so nothing was evaluated. |
| `PrincipalRequired` | `PolicyError` | This collection carries a policy and the search supplied no principal. |
| `PrincipalIncomplete` | `PolicyError` | The principal is missing a key the policy names. |
| `PolicyMismatch` | `PolicyError` | The collection's stored policy is not the one it was opened with. |
| `CollectionStoreUnavailable` | `PolicyError` | The collection records could not be read, and nothing read earlier can stand in. |
| `PushdownUnavailable` | `PolicyError` | The policy requires engine-side filtering and this backend cannot. |
| `AuditUnavailable` | `PolicyError` | A decision record could not be stored, and the policy is to deny. |
| `PolicyNotEnforcedWarning` | `UserWarning` | A collection with an entitlement policy was searched without it. |
| `MetadataContractError` | `PolicyError, ValueError` | A document arrived that the policy could never show anybody. |
| `MetadataContractWarning` | `UserWarning` | A document was written without a field the policy decides by. |
| `ExtractionQualityError` | `VectrixError, ValueError` | A document arrived whose text reads as a failed extraction. |
| `ExtractionQualityWarning` | `UserWarning` | A document whose text reads as a failed extraction was written anyway. |
