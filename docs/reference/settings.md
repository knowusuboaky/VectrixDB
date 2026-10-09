<!-- Written by scripts/make_reference.py from the code. Edit the code, then run it again. -->

# Settings

Every setting VectrixDB reads, 157 of them, each an environment variable. `vectrixdb check --template` prints them as a file to fill in, and `vectrixdb check` tests a set before a start; see [Deploy the server](../how-to/deploy.md). A secret can also be given as `NAME_FILE`, naming a file that holds it, which is how Docker and Kubernetes secrets arrive; setting both is refused.

## The server

| Setting | What it does | Example or default |
| --- | --- | --- |
| `VECTRIXDB_PATH` | Where the collections are kept: what every command uses when --path is left out. | `./vectrixdb_data` |
| `VECTRIXDB_PUBLIC_URL` | The address people type. Sign-in redirects to it and emails link to it. | `https://vectors.company.com` |
| `VECTRIXDB_ROOT_PATH` | The path a gateway serves this app under. Left out, the path of PUBLIC_URL. | `/vectrixdb` |
| `VECTRIXDB_PREFIX` | The path every route lives under, inside the gateway's paths: acme serves /acme/api/v1/... Answered with or without it. | `acme` |
| `VECTRIXDB_GATEWAY_PATHS` | Each route's own gateway path, as the gateway team hands them over: route=path, comma separated. A route or a family of them; each answers with its path or without, and a name no route falls under stops the start. | `api/v1=/files/search,auth=/files/auth` |
| `VECTRIXDB_KEY_HEADER` | The header an app's key arrives in. | `api-key` |
| `VECTRIXDB_TOKEN_HEADER` | The header a person's access token arrives in. Named otherwise, Authorization is left to the gateway. | `Authorization` |
| `VECTRIXDB_TRUSTED_PROXIES` | Addresses and networks whose X-Forwarded-For says who is calling. None unless set, so the header is ignored. | `10.0.0.0/8` |
| `VECTRIXDB_DASHBOARD` | 0 turns the dashboard off. On unless set. | `1` |
| `VECTRIXDB_CORS_ORIGINS` | Other sites that may call the API from a browser, comma separated. With sign-in on, name them; * is refused. |  |
| `VECTRIXDB_FRAME_ANCESTORS` | https origins that may show the dashboard in a frame, separated by spaces. None unless set. |  |
| `VECTRIXDB_ALLOW_OPEN` | 1 lets serve listen beyond this machine with no key and no sign-in, for a server behind a gateway that asks. |  |
| `VECTRIXDB_LISTEN_PORT` | The port serve listens on when --port is left out. The container image sets it to 7337. Not VECTRIXDB_PORT, which Kubernetes sets in every pod for a Service named vectrixdb. | `7337` |
| `VECTRIXDB_MAX_UPLOAD_BYTES` | The largest document the server reads, in bytes. | `104857600` |
| `VECTRIXDB_OFFLINE` | 1 refuses every download: the bundled models only. |  |
| `VECTRIXDB_MODELS_DIR` | Where models are kept, when not beside the package. |  |
| `VECTRIXDB_AUTO_DOWNLOAD` | 1 fetches a missing model on first use. |  |

## Keys for scripts

| Setting | What it does | Example or default |
| --- | --- | --- |
| `VECTRIXDB_API_KEY` | The full key: every action, as an admin, for scripts. Sign-in is for people. (a secret, or `VECTRIXDB_API_KEY_FILE`) |  |
| `VECTRIXDB_API_KEY_SHA256` | The full key given as its SHA-256, so the key itself is never on the server. |  |
| `VECTRIXDB_READ_ONLY_API_KEY` | A key that reads and never writes. (a secret, or `VECTRIXDB_READ_ONLY_API_KEY_FILE`) |  |
| `VECTRIXDB_READ_ONLY_API_KEY_SHA256` | The read-only key as its SHA-256. |  |
| `VECTRIXDB_OPEN_READS` | With a key and no sign-in: 0 makes every read ask for the full or the read-only key, the dashboard's live feed too. On unless set, so a read needs no key. | `1` |

## Sign-in

| Setting | What it does | Example or default |
| --- | --- | --- |
| `VECTRIXDB_SIGNIN` | oidc, email, or oidc,email. Off unless set. With oidc nobody keeps a passkey: those are for email alone. oidc with no provider named yet signs the People list in with a code by email until one is. | `email` |
| `VECTRIXDB_SIGNIN_SECRET` | 32 characters or more. While rotating, two, comma separated, newest first. (a secret, or `VECTRIXDB_SIGNIN_SECRET_FILE`) |  |
| `VECTRIXDB_SIGNIN_USERS` | People to add to the People list if missing, address:role, comma separated. Changes nobody already there. With single sign-on on, the list says who may sign in and with what role. | `ada@company.com:admin` |
| `VECTRIXDB_SIGNIN_PASSWORDS` | on: the email way in asks for a password and a code together, never a password alone. |  |
| `VECTRIXDB_SIGNIN_REQUIRE` | passkey: people on the email list sign in with a passkey only. For email alone, not beside oidc. |  |
| `VECTRIXDB_SESSION_HOURS` | How long a sign-in lasts. | `8` |
| `VECTRIXDB_SESSION_IDLE_MINUTES` | Signed out after this long unused. | `120` |
| `VECTRIXDB_COOKIE_SAMESITE` | strict or lax. Strict, or lax while single sign-on is on. |  |
| `VECTRIXDB_ADMINS_USE_SSO` | on: an admin signs in with single sign-on only. The server console stays the way back in. |  |
| `VECTRIXDB_GUESTS` | on: people who have not signed in may look around: the collections and how the setups scored. Searching needs a sign-in. |  |
| `VECTRIXDB_KEY_REQUESTS_PER_MINUTE` | Requests a minute for a named API key that has no number of its own. Empty: no limit. |  |
| `VECTRIXDB_AUTH_PATH` | The folder for the sign-in file and the access log. Default <path>/auth. |  |
| `VECTRIXDB_SIGNIN_STORE` | Where sign-in state is kept: a SQLite file by default, or postgresql://, cosmos:// or dynamodb://. (or `VECTRIXDB_SIGNIN_STORE_FILE`) |  |
| `VECTRIXDB_SIGNIN_STORE_KEY` | That database's password or account key. Never in the address. (a secret, or `VECTRIXDB_SIGNIN_STORE_KEY_FILE`) |  |
| `VECTRIXDB_ACCESS_LOG` | Where sign-ins and reads are recorded: a file; stdout for a platform that collects the server's output; or https://<account>.blob.core.windows.net/<container>/<prefix>, append blobs every server shares, in a container under an immutability policy. Default <path>/auth/access.jsonl. |  |

## Single sign-on

| Setting | What it does | Example or default |
| --- | --- | --- |
| `VECTRIXDB_OIDC_ISSUER` | The identity provider's issuer. | `https://login.microsoftonline.com/<tenant>/v2.0` |
| `VECTRIXDB_OIDC_CLIENT_ID` | The application's client id at the provider. |  |
| `VECTRIXDB_OIDC_CLIENT_SECRET` | The client secret. Left out for a public client; PKCE is always used. (a secret, or `VECTRIXDB_OIDC_CLIENT_SECRET_FILE`) |  |
| `VECTRIXDB_OIDC_CLIENT_KEY` | A private key (PEM) the server signs in to the provider with, in place of a client secret. (a secret, or `VECTRIXDB_OIDC_CLIENT_KEY_FILE`) |  |
| `VECTRIXDB_OIDC_CLIENT_CERT` | Its certificate (PEM): Entra ID finds the key by it. (or `VECTRIXDB_OIDC_CLIENT_CERT_FILE`) |  |
| `VECTRIXDB_OIDC_CLIENT_KEY_ID` | The key's id, for Okta and Keycloak. |  |
| `VECTRIXDB_OIDC_SCOPES` | The scopes asked for. | `openid profile email` |
| `VECTRIXDB_OIDC_GROUPS_CLAIM` | The claim that holds a person's groups. | `groups` |
| `VECTRIXDB_OIDC_ROLE_MAP` | JSON, group to role. | `{"<group id>": "admin"}` |
| `VECTRIXDB_OIDC_DEFAULT_ROLE` | The role of somebody in no mapped group. Unset: refused. |  |
| `VECTRIXDB_OIDC_PRINCIPAL_CLAIMS` | JSON, policy attribute to claim. |  |
| `VECTRIXDB_OIDC_GRANT_MAP` | JSON, group to what its members are given by name. |  |
| `VECTRIXDB_OIDC_GROUPS_URL` | Where to fetch groups that did not fit in the token. |  |
| `VECTRIXDB_OIDC_LABEL` | The words on the sign-in button. | `Continue with SSO` |
| `VECTRIXDB_OIDC_ALLOWED_EMAILS` | Addresses that may sign in beside the People list, which single sign-on also needs: an address or @domain, comma separated. * alone: the groups decide on their own. |  |
| `VECTRIXDB_OIDC_API_AUDIENCE` | The audience of an access token taken as a Bearer token, so an app acts as the person using it: api://vectrixdb. Empty: no token is taken. |  |
| `VECTRIXDB_OIDC_TOKEN_ROLE` | The role an app's access token is given, whoever it is for: reader, searcher, viewer or operator. Unset: their groups'. A token never needs the People list. | `searcher` |
| `VECTRIXDB_SSO_RECHECK_DAYS` | With oidc,email: a passkey or a code works only for somebody who signed in with single sign-on within this many days. | `30` |

## Developer Access

| Setting | What it does | Example or default |
| --- | --- | --- |
| `VECTRIXDB_DEVELOPER_ACCESS` | on: a username and a password for trying the roles on this machine. Refused unless PUBLIC_URL is this machine's; answers only this machine. |  |
| `VECTRIXDB_DEVELOPER_USERS` | Its accounts, name:role, comma separated. | `admin.user:admin,viewer.user:viewer` |
| `VECTRIXDB_DEVELOPER_PASSWORD` | Their password. There is no default. (a secret, or `VECTRIXDB_DEVELOPER_PASSWORD_FILE`) |  |

## Emergency sign-in

| Setting | What it does | Example or default |
| --- | --- | --- |
| `VECTRIXDB_BREAK_GLASS` | on: emergency sign-in at /dashboard/#/break-glass, for while the usual sign-in is down, with any VECTRIXDB_SIGNIN. Nothing links to it. Off, its address answers 404. |  |
| `VECTRIXDB_BREAK_GLASS_UNTIL` | When it turns itself off, in UTC. Needed while it is on; its sessions end then too. | `2026-09-28T02:00Z` |
| `VECTRIXDB_BREAK_GLASS_ADMIN` | The one username it takes. It signs in as an admin, recorded as break_glass_used. | `emergency.admin` |
| `VECTRIXDB_BREAK_GLASS_PASSWORD_HASH` | The hash of its password, from vectrixdb break-glass hash. The password itself stays in a key vault, and works for one emergency. (a secret, or `VECTRIXDB_BREAK_GLASS_PASSWORD_HASH_FILE`) |  |

## Email

| Setting | What it does | Example or default |
| --- | --- | --- |
| `VECTRIXDB_SMTP_URL` | The mail server sign-in links are sent through: smtp://user:password@host:587 or smtps://host:465. (a secret, or `VECTRIXDB_SMTP_URL_FILE`) |  |
| `VECTRIXDB_MAIL_FROM` | The address sign-in emails come from. |  |

## Brand

| Setting | What it does | Example or default |
| --- | --- | --- |
| `VECTRIXDB_BRAND_NAME` | Your name, shown where VectrixDB's would be. 40 characters or fewer. |  |
| `VECTRIXDB_BRAND_LOGO` | A square logo: SVG, PNG or JPEG, as a file or a data: address. An SVG that could run anything is refused. |  |
| `VECTRIXDB_BRAND_LOGO_DARK` | The same logo for the dark theme. Optional. |  |
| `VECTRIXDB_BRAND_ACCENT` | One colour, #rrggbb. VectrixDB works out the tint, the text shade and the ink on it. |  |
| `VECTRIXDB_BRAND_WORDMARK` | on: the name is set as a wordmark, larger and in the accent. |  |
| `VECTRIXDB_BRAND_COPYRIGHT` | Whose the line under the sidebar names: Northwind reads © 2026 Northwind. | `Northwind` |
| `VECTRIXDB_BRAND_PALETTE` | The grounds, inks and lines for each theme, as JSON or a JSON file. Every ink must read 4.5 to 1 on its grounds. | `{"light": {"page": "#f4f6f4"}}` |

## Documents

| Setting | What it does | Example or default |
| --- | --- | --- |
| `VECTRIXDB_EXTRACTOR_URL` | A service that reads file types the server does not. |  |
| `VECTRIXDB_EXTRACTOR_ROUTES` | JSON, file suffix to route on that service. | `{".pdf": "/extract/pdf"}` |
| `VECTRIXDB_EXTRACTOR_BODY` | raw or multipart. | `raw` |
| `VECTRIXDB_EXTRACTOR_KEY` | The key the service asks for. (a secret, or `VECTRIXDB_EXTRACTOR_KEY_FILE`) |  |
| `VECTRIXDB_EXTRACTOR_KEY_HEADER` | The header that key goes in. | `x-api-key` |
| `VECTRIXDB_EXTRACTOR_TIMEOUT` | Seconds to wait for the service. | `300` |
| `VECTRIXDB_EXTRACTOR_RETRIES` | How many more times to ask the service after a timeout, a connection error or a 408, 429, 502, 503 or 504, waiting longer each time with jitter; Retry-After is honoured. | `3` |
| `VECTRIXDB_EXTRACTOR_MASK` | Ask the service to mask identifiers as it reads, so the index never holds them: 1 for the identifiers, all, or the types comma separated. Empty: not asked. What was masked is kept with the document, as counts. |  |
| `VECTRIXDB_KEEP_SOURCE` | 1 keeps each document's Markdown beside its collection, so it can be opened and cut again. A folder, s3://bucket/prefix or a Blob address serves what another process keeps there, one folder a collection. |  |

## Extraction service

| Setting | What it does | Example or default |
| --- | --- | --- |
| `VECTRIXDB_EXTRACT_LISTEN_PORT` | The port extract-serve listens on when --port is left out. The container image sets it to 7338. | `7338` |
| `VECTRIXDB_EXTRACT_PREFIX` | The path every route lives under, the deployment's choice: /api gives /api/extract/pdf. |  |
| `VECTRIXDB_EXTRACT_GATEWAY_PATHS` | Comma separated route=path: the path a gateway publishes each endpoint under, as its team hands them over. |  |
| `VECTRIXDB_EXTRACT_URL_HOSTS` | Comma separated: the only hosts its address routes may fetch from; *.example.com for subdomains. |  |
| `VECTRIXDB_EXTRACT_JOBS` | memory, or azure for a job record in a container and the work on a queue. | `memory` |
| `VECTRIXDB_EXTRACT_OUTPUT` | Where memory jobs write what they make. | `./output` |
| `VECTRIXDB_EXTRACT_STORAGE` | Azure jobs: a storage connection string, or an account URL for the managed identity. (a secret) |  |
| `VECTRIXDB_EXTRACT_CONTAINER` | Azure jobs: the container records and results go in. | `extraction` |
| `VECTRIXDB_EXTRACT_QUEUE` | Azure jobs: the queue the work waits on. | `extract-jobs` |
| `VECTRIXDB_EXTRACT_PDF` | How a PDF is read: text, its own text layer with OCR for scanned pages only; vision, the same with the pages the rules are unsure of (charts, big figures set apart, parts drawn out of order) read by a chat model that can see them and held to each page's own words; or layout, Document Intelligence's layout model, for tables as rows, headings, the numbers printed on the pages and charts cut out to describe. vision is paid for the pages it reads, layout for every page. | `text` |
| `VECTRIXDB_VISION_PAGES` | With EXTRACT_PDF=vision: hard, the pages the rules are unsure of; or all, every page with words on it. | `hard` |
| `VECTRIXDB_SPEECH_LOCALES` | Comma separated: the languages a recording may be in, when a call names none. Azure Speech hears which one it is. | `en-US,fr-CA` |
| `VECTRIXDB_SPEECH_SPEAKERS` | Up to how many voices Azure Speech tells apart, each phrase starting Speaker 1:, Speaker 2:. 0 asks for none. | `4` |
| `VECTRIXDB_VIDEO_FRAMES` | The most frames of a video read for what is on screen, taken where the picture changes and spread over its length, each read by the picture reader. 0 reads the sound alone. | `12` |
| `VECTRIXDB_DESCRIBER_URL` | A chat model that can see, asked first to describe pictures in detail: its chat completions address. AZURE_OPENAI_VISION_DEPLOYMENT with AZURE_OPENAI_ENDPOINT does the same for Azure OpenAI. |  |
| `VECTRIXDB_DESCRIBER_MODEL` | The model to ask, for a service that serves more than one. |  |
| `VECTRIXDB_DESCRIBER_KEY` | The key that service asks for. (a secret) |  |
| `VECTRIXDB_DESCRIBER_KEY_HEADER` | The header the key goes in; Authorization sends it as a Bearer token. | `Authorization` |

## Masking

| Setting | What it does | Example or default |
| --- | --- | --- |
| `VECTRIXDB_MASKING_ENGINE` | How identifiers in a document are found before it is indexed, and by /mask on the extraction service: auto, regex, presidio, language or comprehend. auto picks Azure AI Language when AZURE_LANGUAGE_ENDPOINT is set, with AZURE_LANGUAGE_KEY or the managed identity, else Presidio when it is installed, else the patterns. comprehend is Amazon's, used when named, with AWS_REGION. The patterns run after every engine. | `auto` |
| `VECTRIXDB_MASKING_LANGUAGES` | The languages the documents are in, comma separated: what Presidio loads a model for, and what an engine is asked to cover. | `en,fr` |
| `AZURE_LANGUAGE_ENDPOINT` | The Azure AI Language resource that finds names, addresses and national ids: https://<name>.cognitiveservices.azure.com. Set, auto picks it. |  |
| `AZURE_LANGUAGE_KEY` | Its key. Left empty, the managed identity is used. (a secret, or `AZURE_LANGUAGE_KEY_FILE`) |  |

## Sources

| Setting | What it does | Example or default |
| --- | --- | --- |
| `VECTRIXDB_SOURCES_HOSTS` | Comma separated: the only hosts the feeds and pages a collection keeps up with may be fetched from, redirects included; *.example.com for subdomains. Unset, any public host. |  |
| `VECTRIXDB_SOURCES_INTERNAL_HOSTS` | Comma separated: intranet hosts a source may fetch although they resolve to private addresses. Link-local addresses, where cloud metadata services answer, stay refused for them too. |  |

## Audit and evaluation

| Setting | What it does | Example or default |
| --- | --- | --- |
| `VECTRIXDB_AUDIT_STORE` | Where search decisions under a policy are recorded: a file; https://<account>.blob.core.windows.net/<container>/<prefix>, append blobs in a container under an immutability policy; s3://<bucket>/<prefix>, under Object Lock; or postgresql://, a table written with INSERT only. (or `VECTRIXDB_AUDIT_STORE_FILE`) |  |
| `VECTRIXDB_AUDIT_RETAIN_DAYS` | How many days S3 must refuse to change each record. Only S3 is told this per record, and it has no default. |  |
| `VECTRIXDB_AUDIT_JSONL` | The older name for AUDIT_STORE when it is a file, one line a decision. |  |
| `VECTRIXDB_AUDIT_QUERY_KEY` | The key queries are hashed with in that file. Derived from the sign-in secret when unset. (a secret) |  |
| `VECTRIXDB_EVALUATIONS` | Where evaluation runs are read from: a folder, s3://bucket/prefix or a Blob address. Default <path>/evaluations. |  |
| `VECTRIXDB_GRAPH_STORE` | Where each collection's knowledge graph is kept for every server: a folder, s3://bucket/prefix or a Blob address, one <collection>/graph.json each. Default <path>/graph. |  |
| `VECTRIXDB_WRITER_URL` | A chat model that drafts golden questions: its chat completions address. AZURE_OPENAI_WRITER_DEPLOYMENT with AZURE_OPENAI_ENDPOINT does the same for Azure OpenAI. |  |
| `VECTRIXDB_WRITER_MODEL` | The model to ask, for a service that serves more than one. |  |
| `VECTRIXDB_WRITER_KEY` | The key that service asks for. (a secret) |  |
| `VECTRIXDB_WRITER_KEY_HEADER` | The header the key goes in; Authorization sends it as a Bearer token. | `Authorization` |
| `VECTRIXDB_TRACING` | 1 sends a span for every search, ingestion and evaluation run to OpenTelemetry; 0 keeps it off even with an endpoint set. Off unless set or OTEL_EXPORTER_OTLP_ENDPOINT is. A span carries counts and timings, never query or document text. Needs vectrixdb[tracing]. |  |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Where spans go, an OTLP/HTTP collector: Jaeger, Grafana Tempo, Honeycomb, Datadog, Azure Monitor's collector. Setting it turns tracing on. | `http://localhost:4318` |
| `OTEL_SERVICE_NAME` | The service name spans carry. | `vectrixdb` |

## Storage

| Setting | What it does | Example or default |
| --- | --- | --- |
| `VECTRIXDB_STORAGE_BACKEND` | sqlite, memory, azure_search, cosmosdb, lakebase or delta_lake. | `sqlite` |
| `VECTRIXDB_COSMOS_ENDPOINT` | The Cosmos DB account address. |  |
| `VECTRIXDB_COSMOS_KEY` | Its key. (a secret) |  |
| `VECTRIXDB_COSMOS_DATABASE` | The database. | `vectrixdb` |
| `VECTRIXDB_LAKEBASE_HOST` | The Lakebase host. |  |
| `VECTRIXDB_LAKEBASE_PORT` | Its port. | `5432` |
| `VECTRIXDB_LAKEBASE_DATABASE` | The database. | `vectrixdb` |
| `VECTRIXDB_LAKEBASE_USER` | The user. |  |
| `VECTRIXDB_LAKEBASE_PASSWORD` | The password. (a secret) |  |
| `VECTRIXDB_LAKEBASE_TOKEN` | A token, in place of a password. (a secret) |  |
| `VECTRIXDB_LAKEBASE_SSL` | true or false. | `true` |
| `VECTRIXDB_DELTA_WORKSPACE_URL` | The Databricks workspace address. |  |
| `VECTRIXDB_DELTA_TOKEN` | Its token. (a secret) |  |
| `VECTRIXDB_DELTA_CATALOG` | The catalog. | `main` |
| `VECTRIXDB_DELTA_SCHEMA` | The schema. | `vectrixdb` |
| `VECTRIXDB_DELTA_WAREHOUSE_ID` | The SQL warehouse. |  |
| `VECTRIXDB_DELTA_HTTP_PATH` | The warehouse's HTTP path, in place of its id. |  |
| `VECTRIXDB_AZURE_SEARCH_ENDPOINT` | The search service, like https://<service>.search.windows.net. |  |
| `VECTRIXDB_AZURE_SEARCH_KEY` | An admin key. Left empty, the managed identity is used. (a secret) |  |
| `VECTRIXDB_AZURE_SEARCH_INDEX_PREFIX` | Index names are <prefix>-<collection>. | `vectrix` |
| `VECTRIXDB_AZURE_SEARCH_SEMANTIC` | true adds Azure's semantic ranker, on a tier that has it. | `false` |
| `VECTRIXDB_AZURE_SEARCH_EMBEDDINGS` | vectrixdb, azure, or both vectors in one index. | `vectrixdb` |
| `VECTRIXDB_AZURE_SEARCH_FILTER_FIELDS` | JSON: metadata paths to promote to filterable fields. |  |
| `VECTRIXDB_AZURE_SEARCH_VECTOR_WEIGHTS` | JSON: how much each vector counts in the fusion. |  |
| `VECTRIXDB_AZURE_OPENAI_ENDPOINT` | The Azure OpenAI account behind the second vector. |  |
| `VECTRIXDB_AZURE_OPENAI_EMBED_DEPLOYMENT` | Its embedding deployment. Naming it asks for both vectors. |  |
| `VECTRIXDB_AZURE_OPENAI_KEY` | Its key, when it is not keyless. (a secret) |  |
| `VECTRIXDB_PARENT_STORE` | Where parent sections live: a path, or cosmos://<account>.documents.azure.com/<db>/<container>. |  |
| `VECTRIXDB_CHUNK_STORE` | Where the collection pages read every chunk from when more than one process writes: cosmos://<account>.documents.azure.com/<db>/<container>. Left out, the table beside this process. |  |
| `VECTRIXDB_CHUNK_STORE_KEY` | That Cosmos account's key. Left empty, the managed identity is used. (a secret, or `VECTRIXDB_CHUNK_STORE_KEY_FILE`) |  |
| `VECTRIXDB_COLLECTION_STORE` | Where every collection's record is kept for every server: who may retrieve from it, who may see it, and masking. A path, or postgresql://, cosmos:// or dynamodb://. Left out, nothing is gated and collections are served as they always were. (or `VECTRIXDB_COLLECTION_STORE_FILE`) |  |
| `VECTRIXDB_COLLECTION_STORE_KEY` | That database's password or account key. Never in the address. Left empty, a cloud store uses the managed identity. (a secret, or `VECTRIXDB_COLLECTION_STORE_KEY_FILE`) |  |

## Cache and memory

| Setting | What it does | Example or default |
| --- | --- | --- |
| `VECTRIXDB_CACHE_BACKEND` | memory, redis, hybrid or none. | `memory` |
| `VECTRIXDB_REDIS_HOST` | The Redis host. | `localhost` |
| `VECTRIXDB_REDIS_PORT` | Its port. | `6379` |
| `VECTRIXDB_REDIS_PASSWORD` | Its password. (a secret) |  |
| `VECTRIXDB_REDIS_SSL` | true or false. | `false` |
| `VECTRIXDB_SCALING_STRATEGY` | none, or how indexes are moved out of memory under pressure. | `none` |
| `VECTRIXDB_MAX_MEMORY_PERCENT` | The share of memory the server tries to stay under. | `85` |
| `VECTRIXDB_BUILD_THREADS` | Threads that insert into the vector index. 1 makes every build of the same vectors the same graph, and is slower; empty is every core. |  |
