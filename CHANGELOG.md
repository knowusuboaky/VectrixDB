# Changelog

All notable changes to VectrixDB are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Deprecation policy

Anything reachable from `import vectrixdb` is public API. Public API is removed
only after it has emitted a `DeprecationWarning` naming its replacement for at
least one full minor release. Names prefixed with an underscore are internal and
may change at any time.

## [2.2.0] - Unreleased

### Added

- **One release, every registry.** The Release workflow runs each client's
  own checks and its walk against a real server (the new SDKs workflow, also
  run on every pull request that touches a client or the server), then, once
  PyPI has the package, publishes the TypeScript client to npm and the Rust
  client to crates.io at the same version, and tags `sdk/go/v<version>` for
  Go, next to the release's own `v<version>`. A version a registry already
  has is skipped. `python scripts/release.py X.Y.Z` stamps the version in
  every client too, and a release is refused while any client says another
  one; a pre-release such as 2.3.0rc1 goes to npm, crates.io and Go as
  2.3.0-rc.1, and to npm under `next`.
- **The documentation's home page shows every way in:** the command line, the
  four clients and MCP, each with its install line, its clip and its guide.
- **Every picture and clip in the documentation opens full size on a click**,
  zoomable and draggable, with the page's other pictures a swipe away.
- **A client in four languages, one surface.** `vectrixdb.connect(url,
  key=...)` returns a `VectrixClient` with the same calls as `Vectrix`:
  `search` returns the same `Results` with the same citations, `add_document`
  sends a file for the server to read and embed, `add_texts`, `documents`,
  `open_document`, `delete_document`, `collections`, `describe`,
  `create_collection`, `sources`, `add_source`, `refresh_sources`, `whoami`,
  `health` and `ready`. `AsyncVectrixClient` is the same with `await`. A key
  goes in the `api-key` header, a company sign-in token as a bearer token; a
  429 or 503 is retried three times honouring `Retry-After`; every refusal is
  a `RequestError` with `status`, `message` and `detail`, and one named kind
  per status (`AuthError`, `ForbiddenError`, `NotFoundError`,
  `InvalidError`, `BusyError`, ...). The `client` extra brings httpx. The
  TypeScript, Go and Rust clients under `sdk/` offer the same calls, are
  built from `docs/reference/openapi.json`, and each runs the same
  conformance walk (`sdk/CONTRACT.md`) against a real server, which
  `sdk/conformance/serve.py` starts.
- **Clients safe to point anywhere.** All four clients refuse to send a key
  or token over plain HTTP to another machine (`allow_http` says otherwise),
  never follow a redirect, so a key goes only to the address it was given
  for, always check certificates, and never print a key. A name or id of
  `.` or `..`, which would reach another route, a key with a control
  character and an address with a password in it are refused before
  anything is sent. They take a
  gateway's key header and token header, extra headers, its `prefix` and
  `gateway_paths` (read exactly as the server's `VECTRIXDB_GATEWAY_PATHS`),
  a private CA and a client certificate, and honour `HTTPS_PROXY`. The Go
  module is `github.com/knowusuboaky/VectrixDB/sdk/go/v2`, as a 2.x release
  needs.
- **The `vectrixdb` command on a server.** Every command that works on a
  folder takes `--url` (or `VECTRIXDB_URL`) and works on a server instead:
  `list`, `info`, `stats`, `create`, `delete`, `ingest`, `query`, `sources`
  and `keys`. Who you are comes from `--key-file` (refused when others can
  read it), `VECTRIXDB_KEY`, `VECTRIXDB_KEY_FILE`, `VECTRIXDB_TOKEN`, or
  `vectrixdb login`, which signs a person in with the identity provider the
  server names (a browser with PKCE, or `--device` from SSH and containers)
  and keeps the sign-in for that address alone, readable by its owner, and
  refreshed before it expires. `login` says where you sign in and what for
  and asks first (`VECTRIXDB_LOGIN_SCOPES` pins the scopes instead), takes
  only a server that names itself, and never goes over plain HTTP. There is
  no `--key`, which shell history
  would keep. `vectrixdb whoami`, `vectrixdb logout`, `--json` for scripts,
  exit codes 0, 1 and 2, and `VECTRIXDB_CA_BUNDLE`,
  `VECTRIXDB_CLIENT_CERT` and the gateway settings for a company network.
  What a server sends back is printed with terminal control characters
  taken out.
- **Run it inside a company's registry.** `VECTRIXDB_MODELS_URL` fetches
  models from a company's mirror of the releases (an Artifactory or Nexus
  remote) in place of GitHub, checked against the same checksums, with
  `VECTRIXDB_MODELS_TOKEN` sent to the mirror alone and never on to a
  redirect. The images build with the company's package index and model
  mirror as build secrets (`--secret id=pip`, `--secret id=mirrors`), never
  written into a layer. `VECTRIXDB_OIDC_API_CLIENTS` takes access tokens from
  the apps it names alone, a company's own wrapper and its MCP clients, and
  writes any other to the access log as `app_not_allowed`. Two new how-tos:
  shipping a company's own client or command on it, and running it inside
  JFrog Artifactory from the repositories to the person at the keyboard.
- **`vectrixdb doctor` tries every part of an install.** `vectrixdb check`
  reads the settings; `doctor` runs it and then tries each part for real: it
  writes and removes a file in the data folder, loads the embedding model and
  the reranker and times an answer from each, reads a small document of each
  built-in kind, and asks each
  service the settings name whether it is there: the extraction service, the
  identity provider (its description, its signing keys, and that it calls
  itself the issuer set, with Entra's shared `common` address named for what
  it is), the mail server, the stores, the chat models and the trace
  collector. Every answer that is not ok says what to do. Nothing is changed
  anywhere, no key is printed and an address shows only its host.
  `--offline`, `--quick` and `--json`; it exits 1 while there is an error.
- **A collection keeps up with feeds and pages.** `db.sources.add(url,
  every="6h")` and a scheduled `db.sources.refresh()`, `vectrixdb sources
  refresh` or `POST /api/v1/collections/{name}/sources/refresh` read RSS
  2.0, RSS 1.0, Atom and JSON Feed, podcasts and pages again and write only
  what changed: a `304` costs one request, an unchanged entry is left alone,
  a changed one replaces its document under the same id, and entries that
  share a link are each a document of their own. Every chunk carries the
  entry's title, link, author and dates. A podcast is transcribed when the
  collection has an audio engine and is its show notes otherwise; a page is
  its main text, and one that answers `404` or `410` is reported gone. Every
  fetch is guarded: http and https only, never a private, loopback or cloud
  metadata address however the name resolves, every redirect checked again
  and a key or cookie never sent on to another site, size and time capped
  however slowly a server answers, plain http refused through a proxy,
  robots.txt and `Crawl-delay` obeyed (wildcards and the end anchor on
  every Python, including 3.13.14 and later, whose robotparser keeps them
  raw), and a site that asks for time left
  alone until then, a week at the most, by every refresh the process runs, a
  forced one too. A secret is written into an address as `${NAME}` and read
  from the environment, never kept or shown. A licensed feed is a `Source`
  of your own, registered in code or by entry point. A lease keeps two
  refreshes off one source, on one server or several, and sources are kept
  beside the collection records, in Cosmos DB for an Azure AI Search
  deployment. `pip install "vectrixdb[feeds]"` reads XML feeds. See
  [Keep a collection in step with feeds and pages](docs/how-to/sources.md).

- **A release is one button and one approval.** Actions > Release > Run
  workflow checks that the changelog's section for the version is dated and
  no tag for it exists, runs the suite, builds the package and checks its
  version, metadata and size, then tags it, writes the GitHub release from
  the changelog and, after one approval on the `pypi` environment, uploads
  it. `scripts/release_notes.py` reads the notes; a section too long for a
  GitHub release becomes each entry's lead-in and a link to the rest. A
  release published by hand still goes the same way.

- **Container images, for Intel and ARM, signed.** Each release publishes
  `ghcr.io/knowusuboaky/vectrixdb` (the server; `-full` with the
  multilingual embedding model and reranker too) and
  `ghcr.io/knowusuboaky/vectrixdb-extract` (the extraction service), built
  from the wheel PyPI has. The server answers MCP at `/mcp` as it is. They run as a user that is not root on a
  read-only root filesystem, carry their models so nothing is downloaded at
  run time, and will not start without a key or sign-in. The server reads
  feeds as it is, for the [sources](docs/how-to/sources.md) a collection
  keeps up with. Before anything is tagged, each is run the way the docs
  have a reader run it (`scripts/container_smoke.py`: a search over MCP with a key the
  dashboard would make, an intranet feed kept current and the cloud's
  metadata address refused among it), held to a size
  budget and scanned with Trivy, and a high or critical vulnerability with a
  fix stops the release; the published images are scanned again every
  week. Each carries its SBOM and build provenance, is signed with cosign by
  the workflow that built it, with no key to keep, and is attested on GitHub.
  `docker/compose.yaml` runs the server and the extraction service on one
  machine, `docker/compose.tracing.yaml` adds Jaeger, and
  `docker/kubernetes` is a Kustomize setup whose pods meet the restricted
  Pod Security Standard and whose network policy lets only the server call
  the extraction service. See [Run it in containers](docs/how-to/containers.md),
  whose two clips `scripts/container_shots.py` films from the running
  containers: Compose bringing them up, a scan read and found, and that
  scan's trace in Jaeger.

- **`vectrixdb extract-serve` starts the extraction service** on port 7338,
  or `VECTRIXDB_EXTRACT_LISTEN_PORT`, under `VECTRIXDB_EXTRACT_PREFIX` when
  set, so a host needs no `main.py` of its own. `vectrixdb serve` likewise
  reads its port from `VECTRIXDB_LISTEN_PORT` when `--port` is left out.
  Neither is `VECTRIXDB_PORT`, which Kubernetes sets in every pod of a
  namespace with a Service named `vectrixdb`.

- **The key for an extraction service can come from a file.**
  `VECTRIXDB_EXTRACTOR_KEY_FILE` is read as the other secrets' `_FILE`
  twins are, which is how Docker and Kubernetes mount secrets; setting it
  and `VECTRIXDB_EXTRACTOR_KEY` together is refused.

- **`vectrixdb check` knows the variables Kubernetes sets.** A pod gets
  `<NAME>_SERVICE_HOST`, `<NAME>_PORT=tcp://...` and the rest for every
  Service in its namespace, and the check no longer calls one named
  `vectrixdb...` a misspelt setting. When one lands on a setting VectrixDB
  reads, a Service named `vectrixdb-redis` setting `VECTRIXDB_REDIS_PORT`
  for one, the check says which Service did it and that
  `enableServiceLinks: false` on the pod stops it.

- **Searches, ingestion and evaluation runs can be traced, off until asked.**
  `pip install "vectrixdb[tracing]"` and `OTEL_EXPORTER_OTLP_ENDPOINT` (or
  `VECTRIXDB_TRACING=1`, or `vectrixdb.tracing.enable()` in an app that set
  up OpenTelemetry itself) send a span for every `search`, `add_document`,
  `rechunk`, `write_golden` and `evaluate`, every search and upload the
  REST server runs, and every file the extraction service reads, to
  Jaeger, Tempo, Honeycomb, Datadog, Azure Monitor or any OTLP collector.
  A span carries the collection, the mode, the counts, the top relevance
  and the time taken; never query text, document text, file names,
  metadata values or keys, and an exception by its type alone. Only the
  names in `tracing.SAFE_ATTRIBUTES` can be set. Off, a call costs one
  boolean check and OpenTelemetry is not imported. A request with a
  `traceparent` header joins the caller's trace, and a file the server
  sends to the extraction service is read in the same trace. Neither
  server makes a span for each request, so FastAPI's own, which from 0.142
  carry the request's path and query and send logs with exception messages
  wherever `OTEL_EXPORTER_OTLP_ENDPOINT` points, are turned off. The
  dashboard's settings say whether it is on and the host spans go to. See
  [Trace searches and ingestion](docs/how-to/tracing.md).

- **MCP on the server, for a team or a company.** With `VECTRIXDB_MCP=1`,
  `vectrixdb serve` answers MCP at `/mcp`, and an assistant searches as the
  person or the key it acts for. Every tool call is made to the REST API
  with the caller's own key or token, so it is held to everything the server
  already decides: the role, a key's collections, each collection's policy,
  masking, the access log and the audit trail. A person connects with their
  company's sign-in: the client finds the identity provider at
  `/.well-known/oauth-protected-resource/mcp` (RFC 9728) and comes back with
  their access token. A team connects with named keys, `reader`, `searcher`
  or `operator`, one collection or all. The tools are `list_collections`,
  `search` (hybrid, dense, keyword or rerank, with filters, relevance and
  citations; on a collection with a policy or without an index of exact
  words, hybrid searches by meaning and the answer says so), `open_source`,
  and `add_document` only with
  `VECTRIXDB_MCP_WRITES=1` and a role that may write. A dashboard session is
  not a way in, a server with no key and no sign-in answers only its own
  machine, and the endpoint is stateless, so copies behind a load balancer
  share the work. With tracing on, each tool call is a span,
  `vectrixdb.mcp.tool`, naming the tool and the collection and whether it
  was refused, over the search it ran. Off unless set; the container image
  sets it. Keys get a new action, `mcp.connect`. See
  [Connect an assistant over MCP](docs/how-to/mcp-server.md).

- **`vectrixdb mcp` over HTTP serves the reading tools only** unless
  `--allow-writes` is given: anything on the machine that reaches the port
  could otherwise remember, grade and forget. Over stdio nothing changes.

- **A rechunk can be previewed.** `rechunk_preview()` takes `rechunk()`'s
  arguments and cuts each kept document with the settings it would get, and
  returns, per document, its chunks now and after and the settings before
  and after, with totals, `changed` and `to_dict()`. Nothing is written,
  deleted or embedded.

- **The docs have an index for coding agents and a prompt to give one.**
  [llms.txt](https://knowusuboaky.github.io/VectrixDB/llms.txt) lists every
  page with a line on what it is for and the rules an agent should keep;
  [Add it with your coding agent](docs/how-to/coding-agents.md) is a prompt
  for Claude Code, Cursor or Copilot that has the agent read it, plan
  before installing, pick a mode by evaluation, and ask before re-chunking,
  deleting or turning tracing on. A test keeps the index in step with the
  docs nav.
  The same rules come as a skill an agent loads by itself,
  `skills/vectrixdb/SKILL.md`, to put in a project's `.claude/skills/`; a
  test looks up every name it uses in the code.

- **The test-question writer spreads its questions and judges harder.**
  `write_golden` now gives every collection, then every document, a floor of
  questions, so a three-file media collection beside a 240-page report is
  still asked about. A passage the reading's own quality check scored under
  0.5 is passed over as `poor` rather than judged. The critic holds two
  floors a good mean cannot carry: `answered` at 0.7, the passages answer it
  and the answer is right and complete with its units, a comparison comparing
  like with like, and `standalone` at 0.6, a search standing alone when it
  names what it looks for. A draft answered by the same words as one already
  written is sent back as a `repeat`; one naming what the passages never do is
  `named`. The summary counts each.
- **`stale_evidence()` says which questions' quotes have left the index.** A
  question whose expected document is held but none of whose quotes any
  chunk still holds is named, with the document, and `check_golden` notes it;
  over several collections a document is missing only when none holds it.
- **Moving pictures of the dashboard.** `scripts/dashboard_shots.py` films
  the pages it already photographs: `tour-search`, `tour-ingest`,
  `tour-pages`, `tour-collections`, `tour-evaluate`, `tour-access` and
  `tour-console`, each a GIF of eleven to sixteen seconds, each with a test
  that it is on disk and in the docs, and two of an assistant over MCP,
  `tour-mcp` and `tour-mcp-keys`: real calls to the sample server and the
  answers that came back, a key made for one collection seeing only it, a
  reader refused a search, and no key at all told where to sign in. The
  README opens with one, the documentation's home page shows them all, and
  the site wears the dashboard's own mark, amber and type.

- **`VectrixSync.cdc()` carries deletes to the target.** `full()` and
  `incremental()` only ever copied, so a chunk deleted or revoked in the
  governed Delta Lake source stayed searchable in the Lakebase copy for good.
  `cdc()` reads Delta Lake's change data feed from the version the last pass
  reached and applies inserts, updates and deletes alike; on any other source,
  and on a collection's first pass, it copies every row and deletes the target
  rows the source lacks. A feed that cannot answer, off at that version or
  vacuumed, is compared the same way and named in the new
  `SyncResult.fallbacks`; `SyncResult.rows_deleted` counts what left.
  `start_cdc(interval_seconds=30)` runs it in the background, replacing the
  `NotImplementedError` it raised, and `stop_cdc()` stops it. Delta Lake
  collection tables are created with the feed on, and
  `DeltaLakeStorage.enable_change_feed()` turns it on for one made before.
  The test fake for databricks-sql-connector records a feed as Delta does;
  nothing here has run against a live warehouse.

- **`scripts/dashboard_shots.py` retakes the dashboard's pictures.** The
  twelve pictures in the docs were taken by hand and went stale as the pages
  changed. The script fills a sample server in a folder of its own, with four
  small collections, three people, two keys, fourteen days of activity and
  one evaluation run, signs in the way a person does, and saves each picture
  from a headless Edge or Chrome at the size the docs show it. `--list` names
  them. A test fails when the docs show a picture the script does not take,
  or the script takes one the docs do not show.

- **Single sign-on, before its provider is set up.** `VECTRIXDB_SIGNIN=oidc`
  with `VECTRIXDB_OIDC_ISSUER` and `VECTRIXDB_OIDC_CLIENT_ID` both empty used
  to refuse to start. It starts now: the sign-in box draws the single sign-on
  button alone, and pressing it checks, says "Single sign-on not configured",
  and opens the email way by itself, so people on the People list sign in
  with their work email and the code from their authenticator app. With the
  two set, it is single sign-on alone again. One of the two without the other
  is still said at start. With `oidc,email` the email way is in the box from
  the start. `vectrixdb check` warns while it is so, and
  `VECTRIXDB_ADMINS_USE_SSO` and `VECTRIXDB_SSO_RECHECK_DAYS` wait for it.

- **A security group is narrowed to a list of people.** A collection's
  policy that names groups from the sign-in token now names people too, by
  work email, and somebody gets answers only when they are in a group and on
  the list: not everyone in a group may read. Groups with no list are refused
  on save ("Add the people who may search. A group alone would let everyone
  in it search."), and so is a domain on a group's list. The refusal is
  `not_on_list` for somebody in a group and not listed, beside `not_in_token`.
  A record kept from before, a group policy with no list, reads as nobody
  until people are added, with a warning. The Policy tab lists the groups and
  the people, the list tag reads "2 groups, 3 people", and the New collection
  dialog of the Azure walkthrough's own dashboard asks for both.

- **Emergency sign-in.** `VECTRIXDB_BREAK_GLASS=on`, for a server that signs
  people in with single sign-on, while the identity provider is down: one
  named admin at `/dashboard/#/break-glass` and a password kept in a key
  vault, until `VECTRIXDB_BREAK_GLASS_UNTIL`. No code is asked for. The
  password is never a setting: the server is given its hash,
  `VECTRIXDB_BREAK_GLASS_PASSWORD_HASH`, made by `vectrixdb break-glass hash`,
  which takes a password of 24 characters or more unseen. A password works for
  one emergency: once the emergency it was used in is over, it is refused and
  a server set with it will not start. Off, its route answers 404. Each use
  writes `break_glass_used` to the access log, five wrong tries lock it, its
  sessions end when it does, a change that matters asks for the password
  again, and admins see a banner while it is on. The sign-in page never links
  to it. In the Azure walkthrough, `06 --new-break-glass` makes the password,
  keeps it in the key vault and writes its hash to `settings.env`, showing
  neither.

- **Single sign-on and the People list, together.** `VECTRIXDB_SIGNIN=oidc,email`
  is the enterprise shape: one People list says who may sign in, whichever
  way, and each one's role. Single sign-on is checked against it, so somebody
  on it has the role their record gives while their group is still asked, and
  somebody turned off on it is refused ("turned_off"); `VECTRIXDB_OIDC_ALLOWED_EMAILS`
  lets in more addresses or a domain beside it, with their groups' role, and
  an address the provider marks `email_verified: false` is on no list.
  Somebody who came in with single sign-on may add a passkey or an
  authenticator app from **How you sign in**, sign in with it later, confirm a
  change with it, and remove it again, the last one too (`DELETE
  /auth/me/authenticator` is new). Changing their record, turning them off or
  taking them off the list ends their sessions, however they came in.
  `VECTRIXDB_SSO_RECHECK_DAYS` lets a passkey or a code work only for somebody
  single sign-on let in within that many days. The People list and `vectrixdb
  people add`, `list` and `remove` work with single sign-on alone.

- **Single sign-on needs somebody named.** A server with single sign-on and
  nobody on the People list or in `VECTRIXDB_OIDC_ALLOWED_EMAILS` no longer
  starts, since a security group is usually broader than the people who
  should run the platform; `VECTRIXDB_OIDC_ALLOWED_EMAILS=*` says outright that
  the groups decide on their own, with a warning at every start. `vectrixdb
  check` says so before the start.

- **An app's token needs no place on the list.** The People list is for the
  people who run the platform; an access token taken with
  `VECTRIXDB_OIDC_API_AUDIENCE` is somebody using a collection through an app,
  judged by the collection's policy. `VECTRIXDB_OIDC_TOKEN_ROLE` gives every
  such token one role, `reader`, `searcher`, `viewer` or `operator`, never
  `admin`.

- **Developer Access.** `VECTRIXDB_DEVELOPER_ACCESS=on` with
  `VECTRIXDB_DEVELOPER_USERS` (name:role) and `VECTRIXDB_DEVELOPER_PASSWORD`,
  for trying each role on a developer's own machine: the server will not start
  with it unless the public address is this machine's, `/auth/developer`
  answers only a connection from this machine and 404 anywhere else, and there
  is no default password. With no single sign-on the sign-in page says "Single
  sign-on isn't set up here" and opens it; with single sign-on it is a link
  under the email form. Its sessions work from this machine only and end when
  it is turned off. It replaces the username form this release had drawn: with
  passwords on, the email way in asks for the password and the code after the
  work email.

- **A path for each endpoint behind a gateway.** `VECTRIXDB_PREFIX` puts every
  route under a path of your choice, and `VECTRIXDB_GATEWAY_PATHS` gives each
  route, or family of routes, the gateway path its team handed over:
  `api/v1=/files/search, auth=/files/auth`. Each answers with its gateway path
  and the prefix in front or without them, a gateway path opens only its own
  routes, nothing is redirected for a slash, and a name no route falls under
  stops the start. The return address single sign-on uses, its cookie's path,
  the links in sign-in emails and `vectrixdb people add`, and a map the
  dashboard reads are all written the way callers reach them.
  `VECTRIXDB_KEY_HEADER` and `VECTRIXDB_TOKEN_HEADER` name the headers a key
  and a token arrive in, for a gateway that keeps `api-key` and
  `Authorization` for itself. `vectrixdb check --url` takes `--prefix` and
  `--gateway-paths`, or the settings, asks each route where it is published,
  and says whether every gateway path reaches the server. The extraction
  service reads its own two settings with the same reader.

- **`vectrixdb check` says what reads each kind of file.** A reader that is
  missing stops nothing at a start; the first file that needs it is refused.
  Under **Extraction** the check says who reads PDFs with a text layer,
  scanned pages, pictures, recordings, videos and YouTube addresses on this
  server, which file types go to the extraction service in
  `VECTRIXDB_EXTRACTOR_URL`, and, for each kind nothing reads, the extra to
  install or the settings to set. It asks the machine and changes nothing:
  each reader's package is imported, so one installed that does not import is
  an error, ffmpeg is run with `-version`, and the speech model is looked for
  in the Hugging Face cache, never fetched. The settings of an extraction
  service are held to what would stop one at its start or fail its first
  file: Document Intelligence, Azure Speech, the picture describers and
  Translator. So is the masking engine: `presidio` with a language whose
  spaCy model is not installed, Azure AI Language with no key and no
  `azure-identity`, Amazon Comprehend without boto3. See
  [Deploy the server](docs/how-to/deploy.md#what-reads-each-kind-of-file).

- **The restricted note says why.** A search a collection's policy turns away
  says whether the collection has no policy yet, the person is in none of its
  groups, or is in a group and not on its list; the refusal carries `policy`,
  the kind of policy that said it. An admin is offered **Open its Policy tab**,
  anybody else "Its admin can add you".

- **The brand goes further.** `VECTRIXDB_BRAND_COPYRIGHT` names whose the line
  under the sidebar is ("© 2026 Northwind"); `VECTRIXDB_BRAND_WORDMARK` sets
  the name larger and in the accent, beside a symbol logo;
  `VECTRIXDB_BRAND_PALETTE` sets each theme's grounds, inks and lines, each ink
  held to 4.5 to 1 on its grounds; and a logo or a palette can arrive as a
  `data:` address, for a host that keeps settings and no files. `/brand.css`
  serves the colours to a dashboard some other service sends.

- **About, for admins.** The account menu's About shows VectrixDB's version,
  the Apache licence and the NOTICE file, served by `GET /api/v1/about` and
  `GET /api/v1/about/licence`. The sidebar's line no longer carries the
  version, and nobody who has not signed in sees the name VectrixDB on a
  branded dashboard.

- **A NOTICE file travels with VectrixDB.** It carries the copyright line
  and the Apache 2.0 licence Section 4 asks to go wherever the work goes,
  and every model the library carries or downloads, with the licence on its
  model card: nine MIT, three Apache-2.0, and `Babelscape/mrebel-base`,
  which the knowledge graph's relation extraction uses, under CC BY-NC-SA
  4.0, for non-commercial use only. The wheel ships it beside `LICENSE`, and
  a test fails when a model is added without a line.

- **A web page is read as its content.** The HTML reader keeps to `<main>`,
  or `role="main"`, or a page's one `<article>`, and everywhere leaves out
  navigation, banners, footers, dialogs, form controls, cookie notices,
  breadcrumbs, screen-reader-only text and anything hidden. A list of only
  links outside the content is a menu and is left out. A `<main>` that a
  script fills in later is a shell, and then the whole page is read. A
  bank's About page went from 12,774 characters on 3,189 lines, 2,841 of
  them blank, to 2,753 characters of its own text. Both the extraction
  service's `/transcribe/webpage` and ingesting an `.html` file read this way.

- **A PDF is read by PDFium, from where its text sits.** Columns are read
  one after the other, in the order the file wrote them when that reads
  coherently and by position when it does not. A running head or foot is
  left out by its place in the margin, so a table's `Total` row, in the
  same place on every page, stays; a 244-page annual report went from 118
  running footers left in its text to 2. A word broken by the line is
  joined, keeping its hyphen only where the document writes it so
  elsewhere; a raised footnote number is `[^3]`; a line set larger, or bold
  and alone, is a heading, nested under the bookmarks. A table's rows carry
  the header over each figure, `Net interest income; 2025 Oct. 31: $ 8,545`,
  a year printed once over its quarters heading each of them. A filled
  form's values sit beside their labels and a reviewer's notes are kept as
  comments. White or invisible text that no picture lies under is left out
  and counted, `hidden_text_left_out`, since that is how a file hides
  instructions from whatever reads it, and a page with no text that nothing
  read is named in `pages_without_text`. Every call into PDFium holds one
  lock: two ingest threads reading two PDFs at once corrupted memory that
  failed later somewhere else. Without `pypdfium2`, `pypdf` reads as before.

- **A chart a PDF draws is described.** A chart drawn with rectangles and
  lines has no picture to describe and no values in its text. With pictures
  asked for, each is drawn as a picture of its own region, title, scales and
  legend included, and becomes a figure named by its page, `p8-chart1.png`:
  bars standing on one line with room between them, a line through its
  values, a panel of charts as one picture, and a diagram of boxes. A
  table, a page of text with shapes about it and boxes of sentences are not
  taken for charts. On a 244-page annual report: 13 pictures, 10 charts and
  3 diagrams, and none on its 97-page financial statements. The figure
  stands where the chart is on the page, so its describer reads what is
  around it, and the chart's printed words leave the page's text for one
  line under the figure, `Words in the picture: ...`, which the description
  replaces; before, they were a column of loose figures in the text. A chart
  nobody describes, or one taken for decoration, keeps that line.

- **The pages the rules are unsure of are read by sight.**
  `VECTRIXDB_EXTRACT_PDF=vision`, or `load(..., page_reader=PageReader(...))`,
  gives a chat model that can see a picture of each PDF page the rules are
  unsure of, with the page's own words, and it writes the page out as a
  person reads it: a figure kept with what it measures, `- 4.6% 2025 dividend
  yield`, where the rules wrote `4.6%` and `2025 Dividend Yield` in paragraphs
  apart; tables as rows; charts described in place. The rules name the pages:
  one with a chart, big figures set apart from their labels, parts drawn out
  of order, or text on its side; 28 of a 244-page annual report, 3 of its
  97-page financial statements. Every number the model writes must be
  printed on the page or it is taken out, and so is a value that is only a
  tick on a chart's scale, read off a bar, or one the model says it guessed;
  a reading with more than a tenth of its numbers invented, given as fact
  and printed nowhere, or missing more than three in ten of the page's
  words, is not used; the page keeps its reading by the rules, and the
  metadata says which pages were read which way. `VECTRIXDB_VISION_PAGES=all`
  sends every page; `AZURE_OPENAI_PAGE_DEPLOYMENT` names a model for pages
  alone. `scripts/extraction_facts.py` counts the facts a reading keeps with
  what they measure: the rules keep 6 of the 24 in its set for that report,
  and none of the 18 on its two designed pages.

- **A file is known by its bytes.** A PDF named `.docx` is read as a PDF,
  and a file whose bytes are not the Office file its name says is refused
  saying so, not with "Package not found". Text is decoded by its byte order
  mark, then UTF-8, then Windows-1252, and a form feed starts a page.
  `/transcribe/auto` knows a picture, a recording or a video by its bytes
  when the address sends a generic content type.

- **Spreadsheets read as they print.** A number reads as the sheet shows
  it, `$1,200.00`, `12.5%`, `(350)`, `Oct 2025`. A header is found by what
  its cells hold, two header rows are joined per column, a title over a
  table and two tables on one sheet each get their own header, and a
  two-column form reads `Name: value`. Hidden sheets are left out and
  counted, a formula saved with no value reads as its formula, charts give
  the values they were drawn with, and a chartsheet is read. `.xls` is read
  through `xlrd`, and a CSV in whatever delimiter and encoding it has.

- **Word and PowerPoint read as they print.** Word: list and heading
  numbers as Word shows them, `1.1`, `(a)`, `Article I -`; the table of
  contents left out; the rows Word repeats on each page as a table's
  header; comments, equations, charts, SmartArt and alt text kept; the page
  header and footer as metadata; a large document read in 1.7 seconds that
  took 23. PowerPoint: a slide read top to bottom and left to right whatever
  order its shapes were added in, bullet levels kept, charts, SmartArt and
  alt text read, the date, footer and slide-number placeholders and hidden
  slides left out.

- **RTF and OpenDocument files are read.** `.rtf` with its tables as rows,
  hidden and deleted text left out; `.odt`, `.ods` and `.odp` with their
  headings, lists and tables. Markdown's headings are found outside its code
  blocks only, underlined and `<h2>` headings count, front matter may be
  TOML, and inline HTML leaves its words.

- **Who said what, in the languages it was said in.** Azure Speech tells up
  to four voices apart, `VECTRIXDB_SPEECH_SPEAKERS`, and when two or more
  speak each phrase starts `Speaker 1:` on a line of its own; a locale that
  cannot is asked again without. It is told every language a recording may
  be in, `VECTRIXDB_SPEECH_LOCALES`, `en-US,fr-CA` by default, and the
  transcript's `language` is the one it heard most; `?language=` may name
  several. A 429 or 503 is asked again when the service says, three times.

- **What a video shows.** With a picture reader, a video is looked over
  twice a second for the moments its picture changes, a slide coming in or
  a cut, and a frame half a second after each is read, at most
  `VECTRIXDB_VIDEO_FRAMES`, twelve by default, spread over its whole length.
  Each joins the transcript at its second, `On screen: Q3 RESULTS`, a slide
  still showing is not read twice, and a video with no sound track is read
  for its screen alone.

- **A picture's values come as rows.** `/transcribe/image` answers with the
  description, a chart's values or a table's rows written as a sheet's rows
  are, and the picture reader's own words, which are exact; the describer's
  rougher list of them is left out when the reader found some. A chart's
  category with no value printed is not a row, since `Year: 2017` alone says
  nothing; a table's label alone is kept, since it can head the rows under it.
  A chart's rows hold only what it prints: a value the describer says it
  guessed, "about 7,100", is left out, and for a chart a PDF draws, so is one
  not among the words it prints or only a tick on its scale. A live reading
  of a bank's annual report had written 2025 personal deposits as "about
  450" where the bars show about 310.

- **The walkthrough tries regions in the order you write them.**
  `VX_ELSEWHERE`, `eastus,westus` by default, is where a resource is asked
  for when `VX_LOCATION` will not take it, and every step follows it: the
  resource group, storage, search, the AI services, Cosmos and the three
  function apps. Each says which region took it. Azure OpenAI is asked what
  it offers and what quota is left before its account is made, so it goes
  in the first region that has every model wanted. `VX_COSMOS_ELSEWHERE`
  and `VX_OPENAI_ELSEWHERE` give either an order of its own. A Cognitive
  Services name Azure is still holding for a deleted account is purged
  before it is made again.

- **The walkthrough makes a Translator.** Step 03 makes an Azure AI
  Translator on its free tier, `VX_TRANSLATOR=yes` by default, and step 05
  hands the extraction app its key and the region it landed in, so
  `/translate/text` and `/translate/detect` answer instead of 503. The
  example settings name `www.td.com` in `VX_EXTRACT_URL_HOSTS`, where the
  walkthrough's own reports come from, so the address routes have one
  host they may read. Left empty, every address is refused as before.

- **The steps have a requirements file.** `examples/azure/requirements.txt`
  is what the machine that runs 00 to 99 needs: the library from this
  repository with the extras the steps use, Pillow, and what builds the
  wheel. The three deployed parts keep their own lists beside their code,
  and `dashboard/requirements.txt` reads its Backend's.

### Changed

- **The `mcp` extra needs mcp 2 or later.** The server's `/mcp` endpoint is
  built on mcp 2's `MCPServer`, and `mcp>=1.0.0` let an install keep a 1.x
  that cannot serve it, and the endpoint makes each tool's REST call through
  httpx, which the extra now installs (mcp 2 brings httpx2, a different
  package). `vectrixdb mcp` on one machine still runs on 1.x.

- **A YouTube video with captions is read from them, not transcribed.** This
  changes what `load_youtube` does: it used to download every video's sound
  and pay a speech engine for it, and now it reads the captions the uploader
  made, with no download, no ffmpeg and no speech engine, and downloads and
  transcribes the sound only for a video without them. YouTube's automatic
  captions have no punctuation and more mistakes than Azure Speech, so they
  are read only with `captions="automatic"`, a machine translation only with
  `captions="translated"`, and `captions="never"` transcribes every video as
  before. `language` picks the track, `en-US` taking an `en` one. The
  metadata's `transcript_source` says `captions` or `speech`, with
  `caption_language` and `caption_automatic`, and the saved transcript has a
  `- Words:` line; `keep_audio` keeps a sound only when one was downloaded,
  and a `download=` function of your own is still asked for the sound alone.
  The extraction service's `/transcribe/youtube` and `/transcribe/youtube_save`
  take `"captions"` in the body, `"uploaded"` by default, and a finished job
  says where its words came from. See
  [A YouTube video](docs/how-to/extract-keep-index.md#a-youtube-video) and
  [YouTube videos](docs/how-to/extraction-service.md#youtube-videos).

- **The authenticator's QR code is drawn as the dashboard's own.** Round
  dots, corner eyes with softened corners, and the dashboard's logo in the
  middle, or the layers mark in its accent, dark on the light theme's page
  on either theme. Each choice was read back before it was kept: drawn by a
  browser at 1x and 2x on a white card and a dark one, and read by OpenCV's
  QR and ArUco detectors and by WeChat's. What all three read is what
  ships: error correction at level Q, dots 0.92 of a module, eye corners
  rounded by half a module, a mark over at most 7.5 per cent of the code, a
  margin of three modules, and 200 pixels for the usual address, larger for
  a long one and never less dense. Fully round eyes were read by one reader
  in three, light dots on a dark card by one, and level H made the usual
  address too dense to read at 200 pixels. The `otpauth://` address leaves
  out `algorithm`, `digits` and `period`, which every app assumes, so the
  code is smaller and its dots bigger. A palette whose light text and page
  are less than 7 to 1 apart gets VectrixDB's ink and paper for the code.
  `vectrixdb.signin.qr` draws it, and `totp.qr_svg` still answers, in the
  same look.

- **The walkthrough sets how the query app scales.** Flex Consumption gives
  a Python app one web request an instance unless told, so a dashboard page
  started an instance a file, each cold, and one request in twenty took
  fifteen seconds. Step 06 sets `VX_QUERY_AT_ONCE`, 16 unless set, and
  `VX_QUERY_ALWAYS_READY`, 0 unless set, on the query app, and
  `06 --settings-only` sends a change.

- **Three ways to set sign-in, and passkeys with one of them.**
  `VECTRIXDB_SIGNIN=oidc` is single sign-on alone, `oidc,email` is single
  sign-on or a work email and a code, and `email` is a passkey or a work
  email and a code. Beside single sign-on nobody keeps a passkey: the page
  offers none and the passkey routes answer 404, as anything the server
  does not have. A passkey made before stays in the store and signs nobody
  in; its owner sets up an authenticator app from a new link.
  `VECTRIXDB_SIGNIN_REQUIRE=passkey` is for `email` alone.

- **Emergency sign-in goes with every way in.** It needed single sign-on
  and now needs only that sign-in is on. Nothing links to it, and off or
  past its time its address answers 404. A password spent in one emergency
  is closed at the next start whatever the way in.

- **Developer Access is found by the spinner.** Nothing in the sign-in box
  names it. The single sign-on button is pressed, the page says "Local
  development detected", then "Single sign-on not configured" and opens
  it, or offers single sign-on and Developer Access when a provider is set
  up. It goes with single sign-on or on its own: beside `email` alone the
  server will not start with it.

- **GraphRAG on Bedrock asks with Converse.** The one request shape every
  Bedrock text model takes, in place of a body only one family of models
  read. `with_bedrock()` defaults to `amazon.nova-lite-v1:0`.

- **The examples are not in the repository.** `examples/` is kept on the
  machine that runs it. The README and the how-to pages say what to build
  in words and no longer link to its files, the error catalogue lists the
  server's and the extraction service's refusals and the library's
  exceptions, and the tests of the examples skip where the folder is not.

- **Types are checked as Python 3.12**, the version CI checks on, since
  numpy's own stubs stop an older target. Ruff still holds the syntax to 3.9.

- **The walkthrough's chat model is `gpt-5.4-mini`.** Azure takes no new
  deployment of `gpt-4o-mini`, which is in a deprecating state, in any
  region. Step 03 no longer offers a model version that is deprecating,
  deprecated or retired, and when a deployment is refused it gives
  Azure's own reason. `AZURE_OPENAI_WRITER_DEPLOYMENT` and
  `AZURE_OPENAI_VISION_DEPLOYMENT` name another model as before. The
  library's own defaults are untouched.

- **The `documents` extra brings `pypdfium2` and `xlrd`,** the PDF reader
  and the old Excel reader. Both install from wheels, with nothing else to
  set up on the machine.

- **Extraction quality judges prose.** A table's rows, pipe rows and figure
  lines are short keyed pieces, not sentences, and no longer pull a page of
  tables under the threshold; a page that is all table passes.

- **A masked JSON reply masks everything in it,** the segments and headings
  as well as the text, in one call to the engine. Dates are their own type,
  `date`, weighed lightly in the risk score.

- **A busy service answers 503.** A 429 or 503 from a service behind the
  extraction app is answered as 503 rather than 502, so a caller's retry
  treats it as the passing thing it is.

### Fixed

- **A signed link stays secret.** An address with an Azure SAS signature, an
  S3, Cloud Storage or CDN signature, or a token, key or code in its query,
  a path parameter or a `#/` route is still fetched whole, but what is kept
  and repeated, a document's `source` and citations, an error's words, the
  transport's own among them, the extraction service's replies and the
  ingest worker's document ids, is the address without them. They are
  dropped, not masked, so a document keeps one address however often its
  link is signed again, and a source added with one is refused and asked for
  `${NAME}` instead. See
  [Run an extraction service](docs/how-to/extraction-service.md#the-addresses-it-may-fetch).

- **A bot check is refused, not indexed.** A Cloudflare, DataDome, HUMAN
  (PerimeterX), Imperva, Akamai or AWS WAF challenge, or a page that only
  asks to enable JavaScript and cookies, sent in place of what was asked
  for, used to be read as the page and cited. `load_url`, the extraction
  service's address routes and sources now refuse it, naming the site and
  what it sent, and only when sure: an article that quotes "Just a
  moment..." is still read. A `401` or `403` says the site refused, a `429`
  or `503` that it asked for time and how long, and the extraction service
  answers those two with a `503` to come back to.

- **Three optional models are found where they were published.** The
  English BGE base, its reranker and ColBERTv2 were looked for under release
  tags nobody made, so the fallback when Hugging Face is out of reach always
  missed; they point at the 1.9.0 release that holds their files.

- **Two results that score alike come back in the same order every time.**
  Keyword scoring walked the query's words in a set's order, which changes
  from run to run, and broke ties by that order, so under a policy a pile of
  withheld documents could swap two visible ones on one machine and not
  another. The words are taken in a fixed order and a tie falls by id.
- **Years and figures are not phone numbers.** The masking engine took a
  row of years, `2025 2024 ... 2016`, and a column of figures for phone
  numbers and wrote bullets over them; runs of years, steady steps, decimals
  and short groups are now left alone whichever engine found them, and a busy
  Language service is retried rather than failed.
- **A table's stacked column headings head their own columns.** A heading
  written one word a line, `Land`, `Buildings`, `Computer equipment`, used to
  merge into its neighbour, and two figures a figure's width apart used to
  merge into one cell, so a bank's property note read as `Furniture,
  fixtures, and other Leasehold depreciable equipment assets`. Each word
  heads the column under it, and a cell ends where a whole figure ends.
- **A footnote mark glued to its word is cut loose on every page,** after a
  figure's description is merged in, not only on the first; a page reader
  the model throttles waits and reads again instead of dropping the page.
- **Keyword and hybrid search run in the store that holds the text.** A
  search over a store that keeps the text in one place and the vectors in
  another ran the keyword half against an empty local copy and found
  nothing; it runs where the text is, and a store that fails says so as a
  502 in words.
- **`VECTRIXDB_OFFLINE` holds for the speech model.** faster-whisper fetched
  its model from Hugging Face the first time a recording was read, even on a
  host set to refuse every download. With `VECTRIXDB_OFFLINE` it is read from
  the Hugging Face cache alone, and a model that is not there raises
  `ModelDownloadError` with the command that fetches it. See
  [Run without a network](docs/how-to/offline.md#the-speech-model).

- **The audit before release.** Found by reading the tree and running it, each
  with a test:
  - `must_not` in a Qdrant-style filter was built as NOT(a AND b), so a
    document matching only one excluded condition got through; it is NOT(a OR b).
  - Keys beside `$and`, `$or` or `$not` in a filter were dropped silently, a
    double `$not` stayed negated, and a `range` with `gte` and `lte` kept only
    the first bound. Every part applies now.
  - Date filters crashed on a `datetime` bound and never matched a naive
    `datetime` field.
  - A reopened collection answered `sparse_search()` with nothing: the sparse
    index was never saved, and the flag that says it holds vectors started
    False on every open. It is saved with the collection, and `delete()`
    removes from it.
  - `add()` with no ids raised `IndexError`; a wrong-dimension batch committed
    its rows and failed only at the index, leaving points with no vector; ids
    and vectors of different lengths were not refused. All three are checked
    before anything is written.
  - The search cache's key left out `score_threshold` and `ef`, so a second
    search with a different threshold was served the first one's results; a
    set operand (`{"$in": {"a", "b"}}`) made the key raise; and cached results
    were handed out by reference, so a caller that edited them changed what
    the next caller got.
  - `dense_sparse_search()` never applied the collection's policy.
  - `rebuild_index()` dropped vectors written while the new index was built;
    they are carried over at the swap, and two rebuilds at once are serialised.
  - A usearch index of at most 4,096 vectors is searched exactly: it costs
    what the graph walk costs and misses nothing, where HNSW built on several
    threads missed a true neighbour now and then even at forty vectors.
  - A read-only open wrote: the version row, the collection's settings and
    the deletion of demo collections. It writes nothing now.
  - `enable_text_index=False` was not persisted, so every reopened collection
    came back with a text index.
  - A collection named `_vectrixdb`, `_meta`, `_documents` or `_nodes` shared
    VectrixDB's own files and deleted them with itself; a name ending in
    `.db` or `.documents` collided with another collection's files. All are
    refused, and `delete_collection()` never unlinks an internal file.
  - A collection that failed to load could neither be made again (the same
    broken files opened) nor deleted. `create_collection()` says so and
    `delete_collection()` removes it.
  - A SQLite connection closed by `delete_collection()` or `close()` from
    another thread left that thread failing with "Cannot operate on a closed
    database"; it opens afresh.
  - In a sharded index, removing a key that had been re-added brought its
    sealed copy back, and a stale copy in a sealed shard could be returned
    when the live copy was too far away to make its shard's list. The merge
    runs on arrays, which halves a search across fifty shards with tombstones.
  - A citation's source name with square brackets in it ended the citation
    early, and a Windows path was cited whole on Linux; the brackets go and
    the last part is the name.
  - `ColBERT` token vectors for the second and later pieces of a document
    were read from the wrong offset.
  - A long line of text passed to `add()` raised `ENAMETOOLONG` on Linux
    while being checked for being a file name.
  - `reembed()` did not persist the new model's name or the policy, so a
    reopen picked another model and the documents added next were open to
    every caller. A policy stored before its role key was persisted gets it.
  - `quick_search()` wrote a `_quick_search` collection into the working
    directory and two calls at once cleared each other's; it is in memory.
  - nDCG in `retrieval_report()` could pass 1 when two chunks of one page
    both answered an entry.
  - Recursive chunk spans on text with repeated words could point at an
    earlier repeat; the chunker is mirrored with offsets, so they are exact.
  - `ConversationMemory.forget(ids=...)` deleted any document named, memory
    or not, and counted what was asked for rather than what went; turn 0 was
    read as no turn, so the next turn was 0 again.
  - A model manifest recorded from a Windows checkout failed to verify on
    Linux: text files are matched with either line ending.
  - Extraction quality counted "ratio," and "**Docs:**" as noise; edge
    punctuation is stripped before a token is judged.
  - Download of a URL for extraction read the whole body before checking
    its size; it reads one byte past `max_bytes` and stops.
  - The dashboard went blank on an address with a stray `%` in it; the part is
    kept as typed and reads as a page that is not there. The Collections table
    said "hybrid" for a collection the v1 API made as dense, since the v1 API
    gives it a text index too: the `dense` tag decides. The value above the
    tallest bar of "Chunks written a day" was cut off at the top of the chart.
    The Evaluate page showed the server's own path to its runs; it names the
    folder under the data folder instead, and the API's `where` is unchanged.
  - The chat memory example's stand-in summariser looked for a word that
    only the two turns `consolidate()` keeps contained, so it made no facts;
    `00_login.py` reads `VX_SUBSCRIPTION` for an account with more than one
    subscription, and `settings.example.env` says so.
  - `benchmarks/README.md` lists every file, `answer_cutoff_scifact_bge.json`
    and `memory_longmemeval_both.json` with their commands, says that the
    e5-small-v2 LongMemEval figure has no file and how to measure it again,
    and the four model comparisons `compare_models.py` can no longer write
    moved to `benchmarks/archive/` with a note saying what wrote them.
  - What a PDF reads as, checked against documents set the way reports,
    papers and minutes are set:
    - A line with no descender, "the self-insur-", ended its paragraph: the
      gap to the next line was measured from the bottom of its letters. A
      paragraph now ends where a line advances more than the page's leading,
      measured from the tops of the lines, which the letters do not move.
    - A word the line broke, "obliga-" / "tions", kept its hyphen, because
      both halves had been counted as words the document uses. The halves
      are left out of that count, and the word is joined.
    - A footnote mark set clear above its line, as a report sets it, came out
      as a line of its own, "1", twice; it is `[^1]` after its word, and the
      footnote's own number opens its line as `[^1]`.
    - A header of years and a word, "2025 2024 Change", was the table's
      first row, so no figure under it said its year. It heads the columns.
    - A title and the numbered heading under it, set in the same size, were
      one heading. Two heading lines are one only when they share an edge
      or a centre and the second does not open a numbered section.
    - A list item ran on from the sentence before it, and its wrapped lines,
      hanging in from the bullet, were taken for new paragraphs. An item is
      a block of its own, written with Markdown's dash, and its lines hang
      together until the next marker or a line back at the margin.
    - A two-page document kept its running head; it is dropped when it is in
      the margin of both pages, and a page's only line is never one.
  - A page read by OCR, locally or by the `ocr` callable the loader takes,
    was a line of text for every line of print, so its chunks ended
    mid-sentence and its broken words stayed broken. The lines are joined
    into the paragraphs they were printed as (`paragraphs_of`), a page of
    short lines, an invoice, left a line a line.
  - `looks_blank()` read every page as not blank under Pillow 12, whose
    deprecation of `getdata()` raised where warnings are errors.
  - Three tests imported `tomllib`, which arrived in Python 3.11, so the 3.9
    and 3.10 jobs stopped at collection; they fall back to `tomli`, now in
    the `dev` extra for those versions. The clean-install test asked
    `sys.stdlib_module_names`, which 3.9 lacks; it lists the standard
    library itself there.
  - `get()` on a point whose key an index had lost returned the vector it
    had on usearch before 2.26, which answers `get()` for a removed key; the
    index is asked whether it holds the key first.
  - `vectrixdb probe` warned "any origin is allowed" on one Starlette release
    and not another, because a server allowing any origin sends `*` on the
    old and the asking origin on the new; the two read the same now, one
    CORS answer, and only two answers is an error.
  - The Azure example tests skip without `azure-functions` rather than
    erroring in a job that does not install the extra.

- **The README's pictures showed as broken images.** Both pointed at
  `raw.githubusercontent.com` on main, where they had never been pushed, and
  a private repository's raw addresses do not load anyway. They are relative
  paths now, which show on every branch. The PyPI page gets the pictures and
  the relative links rewritten to addresses on main at build time, through
  `hatch-fancy-pypi-readme`, a build dependency only.

- **`mypy vectrixdb` failed in CI with numpy 2.4's stubs.** Two assignments
  changed dtype under the newer stubs, one in the golden writer and one in
  the collection's exact search. Typing only; nothing behaves differently.

- **The docs no longer describe what left with visibility.** The dashboard
  page gave a collection six tabs and showed its Settings tab, the sign-in
  page had an admin share a collection from there, the gateway and
  build-an-app pages listed the routes that did it, and the guests setting,
  with the settings reference built from it, said guests can search. A
  collection has five tabs, the page shows the Policy tab instead, and a
  guest sees the collections and how the setups scored, and signs in to
  search.

- **The Learn page does not offer the demo to a server with collections of
  its own.** It told a guest on a deployment to "load the demo above", which
  a guest cannot, into a server holding somebody's documents. With
  collections there and no `demo` among them, the demo and the quick start
  that leans on it are left out, and the example questions are plain text
  rather than buttons. Until the collections are read, neither the demo nor
  the buttons are drawn, so neither shows for a moment and goes again. In two
  columns the fifth tutorial takes the whole last row. An empty server, and
  one with the demo loaded, read as before.

- **A guest opening a collection is not asked to sign in.** The page asked
  for the collection's growth chart, the server refused a guest, and the
  refusal brought up the sign-in box over a page the guest may see. A guest's
  page leaves the chart out.

- **A deployment's pages no longer give this disk's size or this process's
  start as the collection's.** With the chunks in a shared store, a
  collection read "40 KB on disk, sqlite" and "Created 37m ago" after every
  restart. `GET /api/v1/collections`, `/api/v1/collections/{name}` and
  `/api/v1/info` answer `shared_store: true` with no size, and `created_at`
  from the collection's record or not at all. The tile reads "Kept in: Shared
  store", and Created is drawn only where it is known.

- **Policied counts the same on both pages.** The Collections page said
  "Policied 0" beside an Overview that named who may search each: it counted
  entitlement policies only, and never read who may search. It counts both.

- **A test of a killed writer miscounted.** It stopped listening before it
  stopped the writer, so the writer finished an add or two the test had not
  heard of. It reads what is left in the pipe.

- **A fresh checkout passes its own tests.** The collection written by
  2.1.7 that the suite opens was ignored by git, for being `.db` and
  `.usearch` files, and is kept now. A page's example that fetches a model
  is not run by a suite that runs offline, and three tests no longer need
  pypdf or the Azure packages to be installed.

- **A collection's Overview drew nothing for somebody signed in.** Its page
  called a function that was never defined and stopped before the first tile.
  A test now holds every function a page's template calls to one the page
  defines.

- **A tab the collection's policy refuses is no longer left blank.** Points
  and Quality say Restricted and why, as a search does, with the Policy tab
  one press away for an admin.

- **Counts on a deployment.** `GET /api/v1/info` and `GET
  /api/v1/collections/{name}` counted what the answering instance had written
  itself, which is nothing where the chunks live in a shared store, so the
  Overview said 0 vectors beside a list that said 2,039. Both now count what
  the list counts. Where the server keeps no document index, the tile says how
  many collections the number spans.

- **The line under the single sign-on screens sits in the middle.**

- **A server lists and searches what another process made in the backend
  they share.** Each process keeps its own list of collections, so the API
  server beside the walkthrough's ingest app, both reading one Azure AI
  Search service, listed none of the ingest app's collections and answered
  a search on one with "not found". `VectrixDB(follow_shared=True)` opens
  the collections a shared backend holds at start, in `list_collections`,
  and on a lookup of a name it does not know, and the API server is made
  that way; `open_shared()` does it on demand. It is off by default, so
  `Vectrix` still creates its collection with its own options when the
  lookup misses. A record that will not open is named in
  `failed_collections`, and a backend that cannot be read leaves what is
  open alone.

- **The second vector installs with the Azure extra.** An Azure AI Search
  store with `embeddings="both"` or `"azure"` calls its deployment through
  the `openai` package, and `vectrixdb[azure]` did not bring it, so the
  walkthrough's function app read and cut all five of its files and then
  failed every one at embedding, again each time the queue tried. The extra
  brings `openai` now. A store that names a deployment and no `embed_fn` is
  refused when it opens if the package is missing, saying
  `pip install vectrixdb[azure]`, rather than after every document has been
  cut. The walkthrough's step 06 checks, before it publishes an app given
  the second vector, that the app will install `openai`.

- **The walkthrough's query app asks people to sign in.** `VX_SIGNIN=yes`,
  the default, never reached the apps: step 06 gave them the sign-in store
  and not sign-in itself. A Function App hosts the API through
  `AsgiFunctionApp`, so the library's refusal to listen with no key and no
  sign-in, which is only in `vectrixdb serve`, never ran, and the query app
  answered anybody on every route but reading a collection with a policy:
  writing and deleting documents, deleting a collection, loading demo data,
  and starting runs that call paid models. Step 06 now gives both apps
  `VECTRIXDB_SIGNIN=email`, `VECTRIXDB_PUBLIC_URL` at the query app's
  address, and a `VECTRIXDB_SIGNIN_SECRET` made once and read back on every
  run after, printed as dots like a key. The app sends no mail, so
  `06 --add-person <email>` adds the first admin with the library's own
  `vectrixdb people add` and prints their one-time link in that terminal,
  with the secret and the Cosmos key in that one command's environment. The
  command runs the library the apps were built from, whichever Python starts
  it: a conda base with vectrixdb 2.1.0 installed said "No such command
  'people'".

- **The walkthrough's query app finds its collections.** Step 06 sets
  `AZURE_SEARCH_INDEX_PREFIX` to empty, so each index is named after its
  collection, but Azure hands an app no setting whose value is empty. The
  API read the prefix as absent, fell back to `vectrix`, listed an empty
  `vectrix-collections` and would have searched `vectrix-financial`, while
  the ingest app, which passes the empty prefix in code, wrote `financial`.
  The app puts the empty prefix back before the API starts.

- **The walkthrough's query app has guests.** The guest view was in the
  library and the walkthrough never turned it on, so a visitor saw nothing
  but the sign-in box. `VX_GUESTS=yes`, the default, gives the apps
  `VECTRIXDB_GUESTS=on` along with sign-in: somebody not signed in sees the
  overview, every collection's name and size, and the evaluations, never a
  search.

- **The walkthrough's Cosmos account has one database, `data_db`.** Step 03
  still made `parent_sections` and `chunk_records` in a second database,
  `ingestion`, after the records' database had become `data_db`, so the
  account held two where the mirror shows one. All four containers are in
  `data_db` now, sharing the free tier's 1000 RU/s. A run over an account
  made before says the old `ingestion` database is read by nothing and
  billed, and to copy its two containers across and delete it.

- **The walkthrough's embedding model keeps up with an annual report.** Step
  03 deployed it at 20,000 tokens a minute, and a 244-page report is some
  2,000 chunks and 400,000 tokens, sent 256 chunks a request: one request
  was more than a minute's allowance, so every try came back 429 and started
  again from the first chunk. It asks for 350,000 now. The model is billed
  per token, so the higher limit costs nothing by itself.

- **A collection kept in the cloud opens where nothing was written before.**
  The embedding cache opened its SQLite file without making the folder it
  goes in, and a collection whose index, chunks, parents and sources all live
  in Azure Search, Cosmos DB and Blob writes nothing else to the disk first,
  so on a fresh function app instance every file failed with "unable to open
  database file" the moment its collection opened. The cache makes its folder
  now, as the parent store always did.

- **A Markdown table whose header leaves out the label column reads right.**
  `| 2025 | 2024 |` over `| Personal banking | 14,500 | 13,828 |` read as
  `2025: Personal banking; 2024: 14,500`, the years moved onto the labels and
  the last figure left with none. A header shorter than its rows now stands
  over the figures, `Personal banking; 2025: 14,500; 2024: 13,828`. A reading
  by sight wrote a bank's revenue table this way.

- **A web page's text has no empty lines of spaces and no empty items.** A
  line of one space got past the blank-line collapse, and a list item that
  opened with a block left its marker alone on a line. A list item's later
  paragraphs stay under it, `<br>` is a line and not a paragraph, `<pre>` is
  a fenced block that keeps its indentation, and a picture marked as
  decoration with `alt=""`, a tracking pixel or inline bytes is not a figure.
- **An old page reads whole and in time.** `<p>`, `<li>`, `<dt>`, `<dd>`,
  `<td>`, `<th>` and `<tr>` left open are closed as a browser closes them.
  Left open, a page of unclosed paragraphs nested each inside the one
  before, which made reading it slow in the square of its size, and a cell
  without `</td>` lost its words to the next.
- **A web page is decoded in the charset it names.** It was always read as
  UTF-8, so a Windows-1252 or Latin-1 page lost its accents and quotes. A
  byte order mark, then `<meta charset>`, then UTF-8, then Windows-1252.

- **Step 03 no longer reads a working create as a refusal.** A command that
  prints nothing answers the same whether it worked or not, so a Cosmos
  account that was made was reported as refused and then found again, and
  an AI account never printed its `ok` line. Both now ask the resource.

- **The records' database is `data_db`.** The walkthrough's second Cosmos
  database, the one that holds `collection_records` and `signin_records`,
  is named `data_db` rather than `access`, and the mirror follows:
  `cosmosdb/data_db/collection_records/<collection>.json`. Under `blob/` the
  same name is the account folder, `blob/data_db/ingestion/raw/` and
  `blob/data_db/evals/golden_dataset/`. `_common.DATA` holds the name, the
  `ACCESS` constant is gone, and the records stay committed under the new
  path. A deployment made before this keeps its `access` database until
  step 03 runs again.

- **The walkthrough's app is deployed twice: reads and queries apart.**
  `INGEST_ROLE` makes one deployment of the folder the ingest app, the queue
  trigger and health, and another the query app, the library's API and the
  dashboard with the trigger disabled by an app setting; both read the same
  stores. Step 06 creates, configures and publishes both, and `VX_QUERY_APP`
  names the second. A burst of files no longer shares a machine with a
  search. One app doing both remains the default.

- **Reading a file survives a bad hour.** `HttpExtractor` asks the
  extraction service again after a timeout, a connection error or a 408,
  429, 502, 503 or 504, waiting longer each time with jitter and honouring
  Retry-After, `VECTRIXDB_EXTRACTOR_RETRIES` more times, three by default.
  The ingest worker recognises the same bytes under a new version by their
  hash, kept as `source_sha` beside the version, and does not read them
  again; and `chunks_per_second` gives it a pace on index writes, a token
  bucket a burst of files waits on. The walkthrough's app adds a circuit
  breaker for the extraction app: after five reads in a row that could not
  reach it, messages wait on the queue with a delay instead of burning
  their tries, one probe a window goes through, and `GET /api/v1/ingest/state`
  says paused since when and until when, which the Ingest page and the
  Overview's Needs attention show.

- **The walkthrough's dashboard shows the files that would not read.** The
  worker keeps one record a failed file beside the poison queue, the error
  in words a person acts on; the app's `GET /api/v1/ingest/failed` joins the
  queue with those records, ten a page, and `/retry` and `/drop` on a
  message put it back on the ingest queue or delete the file and everything
  of it, both logged under the person's name. The Ingest page carries the
  card, the Overview's Needs attention card the count.

- **A run records the collections it ran over.** Every saved evaluation run
  carries `collections`, read off its targets, and the list of runs says them
  without opening each report; a chunking run's list says its collections
  too. On Evaluate the golden line and the runs menu name the collection, so
  a server with many collections can tell one run from another. A run saved
  before this is read off its targets.

- **A collection's knowledge graph is kept where every server reads it.**
  `VECTRIXDB_GRAPH_STORE`, a folder, `s3://bucket/prefix` or a Blob address
  like `VECTRIXDB_EVALUATIONS`, and `<path>/graph` when unset, holds one
  `<collection>/graph.json` a collection: entities, relationships, the
  community each entity sits in, when it was read, the index build the
  chunks carried and the model that read them. Extracting writes it,
  `GET /api/v1/collections/{name}/graph` reads it first and says whether the
  index has moved on (`stale`, with how many builds and chunks came after),
  deleting the collection deletes it. The Graph tab shows the graph is older
  than the index and offers Extract again. Only a collection made for graph
  search has one, and the tab is there only when such a collection exists on
  the server; the graph library is fetched the first time the tab is opened.
  The walkthrough's own dashboard has no Graph tab at all.

- **The overview recomposed, and a guest's overview drawn from it.** The
  three charts are searches a day, with the searches a collection's policy
  refused as a red line; search time; and written a day, the chunks written
  across every collection with the part under the quality line in red, a
  click on it opening Ingest. Sign-ins against refusals moved to the Access
  page, where the people are. `GET /api/v1/access/daily` is for everyone,
  guests included, and keeps `signins`, `refused` and the new `denied` for
  whoever may read the access log; `GET /api/v1/growth` sums what was
  written across every collection with `low`, the chunks of each day under
  the line. A guest's overview is the same page without what is about
  people: three tiles, the three charts without the red line, and the
  collections full width. A setup's rank card on Evaluate leads with the
  answer's place on average, the mean reciprocal rank in its tooltip, and
  the Evaluate docs map the page's words to the names used elsewhere.
  Every list that can grow turns ten a page: the overview's collections, a
  collection's index builds and chunks by build, the chunks below the
  quality line (`offset` on `/quality`), a run's quality on Ingest, the
  ranked techniques on Chunking, and the keys on Access.

- **A collection's record is its policy, and nothing else.** The record is
  `name`, `path` and `policy`: who may search the collection, as security
  groups from the sign-in token or a list of people and domains kept with the
  record, or `null` for nobody yet. An empty policy is refused where it is
  written. That a collection exists, its name and its size, is for everyone
  who reaches the server, guests included; what is in it answers only the
  people the policy names, and anyone else who searches it is refused with a
  403 that says why, its code in `data`, and a `denied` line in the access
  log. `GET /api/v1/policies` says who may search each collection, as counts
  to everyone and as the list to people signed in. Identifiers in what
  people are shown are always masked. On the dashboard the collections list
  shows one tag a row, "2 people", "1 domain", "2 groups" or "Unavailable to
  anyone yet", with the names on hover; the Policy tab is where the list is
  edited; a search from outside the policy shows the restricted state; a
  collection's header carries one tag for its mode with the model and
  language on hover; Delete moved to the collection's header.

### Removed

- **Visibility, the masking switch and per-document policies on records.**
  `visibility` and `masking` left the collection record, with `GET
  /api/v1/visibility`, `PUT /api/v1/collections/{name}/visibility`, the
  `visibility_changed` access-log event, the Settings tab and its share and
  mask cards, the guest search with its excerpts and its thirty-a-minute
  limit, and the sign-in store's own visibility and masking rows. A record
  written before this reads as it meant: `visibility`, `masking` and
  `entitlement` are read past, and a policy holding `rules` reads as nobody
  yet. A collection's per-document `Policy` is its own metadata's alone; a
  record never carried one that the library enforced from now on. Guests
  see every collection's name and size, read how the setups scored, and
  never search.

- **The dashboard's charts, redrawn, and every list ten a page.** Each chart
  card leads with a headline and a line against the week before; the scale
  hugs the data; the latest day stands out and earlier days sit back; search
  time has an area under the median and the slowest one in twenty dotted,
  with the values written at the right; sign-ins are bars with refusals as a
  line; outcomes on Audit the same way; by collection, who reads most and
  chunks by build are rows. One pager, a find box with where the page sits
  and previous and next, ten a page, on the Collections page (rows now, not
  cards), a collection's Points, the Audit records, the Access page's people
  and log and busiest readers, the Ingest document index and Evaluate's
  ranked setups. The lists come from the server a page at a time: `GET
  /api/v1/audit`, `/api/v1/access`, `/api/v1/access/readers`, `/auth/people`,
  `/api/v1/documents` and a collection's `/points` take `q`, a page size and
  `offset` and answer with `total`; asked the old way they answer as before.
  The Azure walkthrough's custom dashboard is regenerated from the same
  pages.
- **The error catalogue.** `docs/reference/errors.md` is written by
  `scripts/make_reference.py` from the source: every refusal a route can
  send, its status, its words and the route that raises it, for the server,
  the extraction service and the walkthrough's main app; every exception the
  library defines; and what each walkthrough script says when it stops. A
  refusal added without its row fails the reference test. Building it turned
  up two things, both fixed: the walkthrough's app answered its own routes
  with a bare `{"message": ...}` where every library route answers in the one
  refusal shape, and now uses it, with a job's status in `data`; and a reader
  this build does not include was 501 on the documents route and 503 on the
  extraction service, and is 503 on both.
- **Masking with an engine that reads meaning, before a document is indexed.**
  `vectrixdb.masking` is a package now, and `mask(text, types=..., engine=...,
  language=...)` reads a whole document with the engine the deployment chose
  and masks what it found: names, addresses, national ids and bank accounts
  as well as the emails, phone numbers and cards the patterns always knew.
  Four engines answer the one call: the patterns, always there, now with US
  and Canadian social numbers, IBANs, IP addresses, keys, passwords inside
  connection strings and internal host names; Microsoft Presidio in the
  process, `pip install 'vectrixdb[masking]'` and `vectrixdb download-models
  --type masking` for a spaCy model a language; Azure AI Language, with
  `AZURE_LANGUAGE_ENDPOINT` and `AZURE_LANGUAGE_KEY`; and Amazon Comprehend
  through boto3 with `AWS_REGION`. `VECTRIXDB_MASKING_ENGINE` names one, or
  `auto` picks Azure AI Language when its endpoint is set, else Presidio when
  installed, else the patterns; Comprehend is used when named, never picked.
  Whatever ran, the patterns run after it, and an engine asked about a
  language it does not cover, `VECTRIXDB_MASKING_LANGUAGES`, `en,fr` unless
  said, answers `regex_only` rather than as if it had. Every text is folded
  first, so a card number in Cyrillic digits or with a zero-width space in it
  is still a card number. Each type carries a weight and a text a risk score.
  The extraction service masks on request: `POST /mask` for any text, and
  `?mask=1&types=…&language=…` on every `/extract` and `/transcribe` route,
  so a document comes back masked and the index never sees the identifier;
  its health says which engine loaded and which languages it covers, and
  `vectrixdb check` reads the settings. A server reading through the service
  asks for it with `VECTRIXDB_EXTRACTOR_MASK`, and `HttpExtractor` keeps
  what the service reported, counts and a risk score, in the document's
  metadata and so in the kept Markdown's front matter as `masking`. What a
  server masks on the way out to people and guests is unchanged: the same
  three shapes, in microseconds.
- **Who may retrieve from a collection: its policy, checked before anything is searched.**
  A collection's record now says who it answers, one of two ways: `{"method":
  "token", "allow": [{"id": ..., "name": ...}]}` for the people in a listed
  security group, by object id, read from the groups the identity provider
  signed into their token; or `{"method": "store", "allow": [{"email": ...}
  | {"domain": ...}]}` for a list of people kept with the collection,
  specific people by email, as long a list as you like, and a domain for
  everyone at it, matched against the address they signed in with. A
  collection with no policy answers nobody, and so does one with no record.
  With `VECTRIXDB_COLLECTION_STORE` set, the server checks the policy before
  every search and read of a collection: somebody it does not name gets the
  reply a collection that is not there gets, and is not shown it in the
  list; an admin is told why (`code`: `no_policy`, `not_in_token`,
  `not_on_list`, `not_signed_in`, `key_not_scoped`), and manages a
  collection whoever may retrieve from it. A key is nobody in particular:
  the server's own key passes, a key made for named collections passes
  those, any other key is refused.
  `PUT /api/v1/collections/{name}/policy` sets a policy, `POST
  /api/v1/access/check` says whether an address, with the groups to try,
  may retrieve and why, without searching anything, and both are written to
  the access log. `vectrixdb.collection_access` holds the policy, the
  decision and the store, for a host that checks elsewhere. Without a
  collection store nothing changes.
- **Visibility is `private` or `public`, and a guest reads excerpts.** A
  public collection is anyone's, guests included, whatever its policy
  says; the option to show guests whole chunks is gone, and
  `guest_text` with it. `PUT .../visibility` takes `visibility` (`audience`
  still reads, `everyone` meaning public), and `GET /api/v1/visibility`
  answers `visibility`, the collection's `policy` and whether the server is
  `gated`. The record's `folder` is `path`, `index` is gone, and a
  per-document `Policy` a host writes into a record is its `entitlement`;
  a record written the older way still reads as it meant. Deleting a
  collection takes its record with it, so one made again under the name
  answers nobody until it is given a policy.
- **The dashboard shows and sets who may retrieve.** With a collection
  store on, a collection's Settings tab opens with the policy as two cards,
  the sign-in token and the membership store; pick one and its list appears
  under it, security groups by id and name, or people by email and domains.
  Past five rows the list scrolls in its own box, and a pasted batch of
  addresses becomes rows, with what is already listed left out. Save writes
  it through `PUT .../policy`. Beside it, Check
  someone asks `POST /api/v1/access/check` whether an address, with the
  groups to try, would get in and why, without searching anything. The
  Overview's and a collection's Who can see it now name the policy too,
  and a collection with none is marked nobody.
- **The Azure walkthrough's app makes a collection the way its steps do.**
  The function app gained the dashboard's own routes, each an admin's:
  `POST /api/v1/collections/{name}/setup` writes the collection's record
  and its policy, which is required, before a single file is read, and
  makes its index; `POST .../files` drops one file, or typed text kept as
  Markdown, in `raw/<name>/` for Event Grid to notice; `GET
  .../setup/status` says where the collection is, one step and one line,
  from its record, its files, their Markdown, its chunks and the four jobs;
  `POST .../delete` takes everything, the record first so nothing more
  lands as its, then every file under `raw/`, `markdown/` and `chunks/`,
  then the index and its records, and keeps no copy. The walkthrough's
  dashboard has the screens for them: New collection asks for the name,
  the files or the text, and who may retrieve; a page at
  `#/collections/<name>/setup` holds one spinner whose words follow the
  steps until the collection is ready; Add files says whose policy the
  files fall under; Delete lists what goes and asks for the name. The
  seeds in `.local/cosmosdb/access/collection_records` are written in the
  record's current shape. Every file is masked as it is read: step 03 makes
  an Azure AI Language resource on its free tier when `VX_LANGUAGE=yes`, step
  05 hands the extraction app its settings and `VECTRIXDB_MASKING_ENGINE=auto`,
  step 06 sets `VECTRIXDB_EXTRACTOR_MASK=1` on the main app, and the setup
  spinner gained a Masking stage between Reading and Markdown that counts what
  each kept document says was taken out of it. Nothing is set per collection.
  The extraction app's own file was rearranged the way the main app's reads,
  settings first, then its parts under headings, the main script last. The
  walkthrough's client
  coverage entitlement is gone with it: no `VX_POLICIED`, no
  `INGEST_CLIENT`, no `client_id` on a chunk and no job run "as td". Who
  may retrieve from a collection is the whole rule, and a folder under a
  collection's is only part of a document's name. A record's `entitlement`
  stays a library feature for hosts that want document rules.
- **Each collection's rules in one record every server reads.**
  `vectrixdb.collection_records` keeps a collection's entitlement policy,
  who may see it and whether it is masked in one record, in a store every
  server shares: a path or `sqlite:///`, `postgresql://`, `cosmos://` or
  `dynamodb://`, the stores sign-in already runs on. `VECTRIXDB_COLLECTION_STORE`
  names it to a server, with its key in `VECTRIXDB_COLLECTION_STORE_KEY`, and
  `VectrixDB(collection_store=)`, `Vectrix(collection_store=)` and
  `create_app(collection_store=)` take one. `Collection.policy` reads the
  record first, so a server that opened a collection its own way enforces the
  policy another opener set, where before each kept its own copy in local
  metadata and a load-balanced API could serve bare what the app had
  policied. A record read is held thirty seconds; a store that cannot be read
  stands in with the copy from earlier, and with none raises
  `CollectionStoreUnavailable`, a 503 from the server, rather than answer "no
  policy". A record with no policy never turns off one a collection keeps in
  its own metadata, and a collection opened with a policy the store lacks
  puts it there. The sign-in store keeps visibility and masking in the same
  record once `use_collection_store()` names it, and deleting a collection,
  by any route, sets both back to private and unmasked and keeps its policy.
  `vectrixdb check` says whether the store's extra is installed.
- **The audit trail and the access log in append blobs nothing can rewrite.**
  `VECTRIXDB_AUDIT_STORE` names where decisions go: a path, an Azure Blob
  address, `s3://` under Object Lock (with `VECTRIXDB_AUDIT_RETAIN_DAYS`), or
  `postgresql://`; `VECTRIXDB_AUDIT_JSONL` still works for a file.
  `vectrixdb.append_log` writes a Blob log as one append blob a UTC day, a
  line a block, so every server behind a load balancer appends to the same
  log, and opens only a container whose immutability policy keeps what is
  written. `VECTRIXDB_ACCESS_LOG` takes a Blob address the same way. The
  Audit and Access pages read both back; a PostgreSQL table, written with
  INSERT only, is read where it is kept. `audit_sink_at()` and
  `audit_records_at()` are the two halves for a host.
- **Readable Cosmos DB items.** A sign-in or collection record's id is its
  kind and key, `collection.financial`, with only what Cosmos refuses in an
  id escaped, and its times are dates, `expires` and `updated_at`, beside the
  numbers the code compares. Items written before still read.

- **The chunking techniques compared, and the Evaluate page's Chunking tab.**
  `compare_chunking(documents, golden)` builds six techniques,
  Structure-aware, Parent-child, Recursive, Sentence, Semantic and
  Fixed-size, at three sizes each with the heading path embedded and
  without, thirty-six scratch builds, and with a model LLM-based cutting, a
  model's note on every chunk, and late chunking where the embedding model
  can, and hands every one the same characters for each golden question,
  6,000 unless `budget=` says otherwise, so a technique that cuts small does
  not get more to read. With `chat=`, or `answer=` and `judge=`, the model
  answers from those characters alone and a judge checks the answer against
  the golden one; answered right decides, and without a model the run counts
  what was found. Each technique is ranked by its best build, with equal
  counts going to fewer chunks, and each pair is put to an exact test on the
  questions only one got right, so a gap that could be luck is marked a tie.
  `save_to=` keeps the run as `chunking/runs/<id>/report.json` beside the
  retrieval runs, served at `GET /api/v1/chunking`, `/api/v1/chunking/{run}`
  and `/api/v1/chunking/{run}/golden` to the same roles as the evaluation
  runs. The dashboard's Evaluate page now has two tabs, Chunking and
  Retrieval; an old `#/evaluate/<setup>` address opens that setup under
  Retrieval. `run_chunking_build` and `chunking_report` are the two halves,
  for a host that makes one build at a time, as the Azure walkthrough's main
  function app does, a build a queue message.
- **A cut and a way of searching a host can follow the runs for, or pin.**
  `chunking_build("markdown-1000-h")` reads a build's key into what it is,
  and `chunking_options(build)` into what `add_document` and `rechunk` are
  given to cut that way, every choice said, so a document cut late before
  is not cut late again. `chunking_choice(results, current)` is which build
  a finished run picks, given the one in use, which stays when the
  questions only one of the two got right split the way luck could,
  because every switch cuts and embeds every document again.
  `handed_over(db, question, build)` is what a question is handed from a
  collection cut that way, as a run measured it: enough results to fill the
  budget, a Parent-child build's sections, the last one cut to fit.
  `search_of("hybrid_semantic.both")` is the `search()` arguments a way of
  searching names, the same in every collection. `add_document(index=False)`
  keeps a document's Markdown with its metadata and cuts nothing, so a host
  can wait for a cut before it indexes, and `rechunk()` cuts it later; it
  now takes `cut_with`, `context_with` and `late`, and records a model's
  note and late embedding as flags. The Azure walkthrough's main function
  app follows the runs with `INGEST_CHUNKING=auto` and
  `RETRIEVAL_SETUP=auto`, or pins either.
- **Runs are kept by kind: `retrieval/runs/<id>/` and `chunking/runs/<id>/`.**
  A store's evaluation runs, which were `runs/<id>/report.json`, are now
  written to `retrieval/runs/<id>/report.json`, beside the chunking runs in
  `chunking/runs/`, with the golden files still in `golden/`. A run saved in
  `runs/` before is still listed and read, by the server and by
  `ReportStore`, and no new run is written there.
- **Four more ways to cut and embed.** `chunk="fixed"` cuts every so many
  characters at a space, whatever the paragraphs. `chunk="llm"` has a model
  read the paragraphs and say where each topic starts, with
  `cut_with=llm_cutter(chat)`. `context_with=context_writer(chat)` has a
  model write a sentence or two placing each chunk in its document, which
  goes in front of the chunk for the embedder and is kept as `_vx_context`,
  never in the stored text. `late=True` embeds late: each chunk is the mean
  of its own tokens read in the whole document, in windows as long as the
  model reads, for a model that pools the mean of its tokens or has
  `embed_tokens`; the bundled bge-small pools its first token and is
  refused with a reason. All four are in `vectrixdb.chunk_models` and
  `vectrixdb.ingest.chunk`, and the documents route takes `fixed`.
- **A golden row keeps its evidence: the words that answer it.** `write_golden`
  writes the quotes each question was checked against into the row as
  `evidence`, the schema and `check_golden` accept it, `Question.evidence`
  reads it back, and a quote under three words is noted and not used. A
  chunking run then counts a question found when those words were handed
  over, a quote cut across two chunks counting when both were, rather than
  any part of the right page; `retrieval_report` and `evaluate` take
  `by="evidence"` to count only a chunk that holds them. A row without
  evidence is scored by its pages as before, and a run says how it counted:
  `by` on an evaluation run, `by_evidence` on a chunking run.
- **The collection pages count every instance's chunks, not one.** A
  collection's count, builds, growth by day, quality, points, a chunk's
  provenance and a document's chunks were read from the SQLite table beside
  the process, and on a deployment that scales out the instance drawing the
  page is rarely the one that ingested, so those pages came up empty and said
  nothing about why. `chunk_store=` on `Vectrix`, `VectrixDB` and
  `create_app`, or `VECTRIXDB_CHUNK_STORE` on the server, names a copy every
  write reaches: the chunks added, with their text, metadata and first
  written time and no vector; the ones deleted, including chunks another
  process wrote; the metadata changed. The pages read it instead. Nothing
  searches it. Where it lives is the caller's choice. Cosmos DB is built in,
  `cosmos://<account>.documents.azure.com/<db>/<container>`, one container
  partitioned by `/collection` and indexed only on what a page filters or
  sorts by, with `VECTRIXDB_CHUNK_STORE_KEY` or the managed identity; any
  object whose `collection(name)` keeps the `vectrixdb.chunk_store.CollectionChunks`
  protocol is another. A store that refuses fails the write that missed it,
  after the collection and the index have it, so writing again is the fix.
  Python's Cosmos SDK does not run GROUP BY, so the builds, days and quality
  bins are added up from a small projection of each chunk, and those pages
  cost more as a collection grows. `vectrixdb check` names the store or says
  what is wrong with its address.
- **`VECTRIXDB_KEEP_SOURCE` takes a folder, `s3://bucket/prefix` or a Blob
  address as well as `1`.** One folder a collection inside it, so the
  Documents page opens the Markdown another process kept, a function that
  ingests for instance, rather than only what this server was sent.
- **It runs behind a gateway.** Azure API Management, AWS API Gateway, an
  ingress or any reverse proxy. `VECTRIXDB_PUBLIC_URL` may now carry a path,
  and that path is what the app is served under, whether the gateway forwards
  it or strips it before the server sees it: a request that arrives without
  the prefix is given it back at the edge, because a mount is matched against
  the path with the prefix on it and the dashboard would otherwise be a 404,
  and a redirect would drop the prefix. The layers around the routes read the
  path with the prefix taken off, which matters more than it looks: the
  sign-in layer places a route in the role table, and a route nothing places
  is admin only, so behind a prefix every operator, viewer and guest would
  have been refused, the guest rules would have missed, and the masking layer
  would have let identifiers through unmasked. None of that fails loudly. The
  dashboard reads its own address to find the API rather than assuming the
  host's root, and the `/openapi.json` a deployment serves carries the prefix,
  so a gateway team importing it gets the right base URL.
  `VECTRIXDB_ROOT_PATH` sets the path on its own where there is no public URL.
- **`VECTRIXDB_TRUSTED_PROXIES` says whose `X-Forwarded-For` to believe.**
  Behind a gateway every request arrives from the gateway, and the sign-in
  lockout, the guest rate limit and the access log all key on the caller's
  address: one person's wrong codes locked out everyone behind it, and every
  guest shared one quota. The header is read only from the addresses and
  networks named here, so a caller cannot claim an address of their own, and
  with nothing named it is ignored. `vectrixdb check` has a Gateway section
  that reports the path, names the trusted networks, warns when a deployment
  has a path but no proxies, and refuses a list it cannot parse.
- **The OpenAPI document ships with the release.**
  `docs/reference/openapi.json`, written by `scripts/make_reference.py` from
  the same application the REST reference is read off, is what a gateway team
  imports to make its operations, so the endpoints they publish come from the
  code rather than from an email. It is on the docs site, and the release
  workflow attaches it to each published release, so a policy can be rebuilt
  from a version rather than from whatever was last pasted into a ticket. A test holds it to the route table the
  server serves. The dashboard's own calls are all documented routes now: it
  used the unversioned `/api/...` aliases in seven places, which the document
  leaves out, so a gateway publishing exactly what the document listed would
  have broken the page.
- **A blank page is not sent to an OCR engine, or an empty image to a
  describer.** `looks_blank` says so from the pixels, a scan's speckle
  included. It costs nothing with RapidOCR and matters when the engine is a
  vision model, because those write boilerplate for an empty page.
  `pages_blank` counts them; `RapidOcr(skip_blank=False)` turns it off.
- **Running heads and feet are taken out, and named.** The report's title at
  the top of every page and "Page 3 of 10" at the bottom landed in the middle
  of chunks and stopped a sentence that ran over a page break from being
  joined. `drop_running_lines` takes out the lines that repeat at the top or
  bottom of most pages, the two ends counted together, since a reader can put
  a left-hand page's header after its text, and the document says which in
  `running_lines`. A short line is matched with its numbers ignored, the page
  number on either side of it included; past the outermost line only what
  reads as a page number has its numbers ignored, so "See note 4." above a
  footer stays. A longer line goes when it carries its own page's printed
  number, found from the numbers most pages print at their top or bottom: a
  footer that names the part of the report it is in goes part by part, and so
  does one the reader put inside the page, one read onto the end of the text,
  and one printed hard against the year, "ANNUAL REPORT 20253" for page 3. A
  page's figure lines are never taken for one. Fewer than four pages is left
  alone. The built-in PDF reader, `RapidOcr` and `Textract` do it;
  `drop_running=False` turns it off.
- **A chunk knows whether its own page was read by OCR.** `ocr_pages` on the
  document, and `ocr` on a chunk true only when its page is one of them. It
  used to be true for every chunk of a document with one scanned appendix.
- **An HTML table in an extractor's reply becomes rows**, the rendering every
  other table gets. The models that read a page in one pass write tables as
  HTML, which was a run of tags in front of a sentence embedding.
- **`compare_extractors`**, several readers over your own pages side by side:
  quality, characters, pages, pages by OCR and seconds for each, and how alike
  each pair of texts is, the least alike first. It measures and does not
  judge. Scrambled columns pass the quality score, being made of good words,
  so only another reader shows them.
- **A document fetched from a bucket keeps its pictures.** It did not.
  `load_bytes()` read the bytes through `load()` without ever asking for
  them, so a PDF arriving from a blob or an S3 object had no figures **at
  all**, not merely figures without descriptions. A collection opened with
  `describe_figures` described nothing and one opened with `image_embedder`
  embedded nothing, in silence, while the same file read from disk was fine:
  every deployment that ingests on an event lost them. `load_bytes()` and
  `load_url()` take `images=` now, off by default because keeping them
  decodes every picture, and `Vectrix.wants_images()` is the one answer that
  `add_document()` and the ingestion worker both ask, so the two cannot
  disagree again. A Markdown file's pictures are separate files beside it, so
  bytes fetched on their own still have its figure lines and not its
  pictures; a PDF carries its own and is whole either way.
- **A walkthrough that deploys the whole thing on Azure**,
  `examples/azure`: fourteen numbered scripts, each doing one thing, that
  make a real account's resources, put your own PDFs, pictures, audio and
  video through the event path, run an evaluation, fill the dashboard and
  delete all of it again. Every `az` command is printed before it runs,
  every script takes `--dry-run` and is safe to run twice, and everything
  goes into one resource group so the delete is a single command that cannot
  miss a resource. Three of the scripts, 10 to 12, ask the same questions
  through each of the run's three picks, each with an API key of its own, so
  the Access page ends with one row a setup. Nothing in it is seeded. The
  suite runs it without an Azure account: that the settings turn into names
  Azure would accept, that a dry run runs nothing, that no key is written to
  disk, that every create names the resource group, and that the function
  code only uses names the library has.
- **Where to stop answering is measured, not borrowed.**
  `evaluation.answer_cutoff(db, questions, unanswerable=...)` searches every
  labelled question and sorts each top result into an answer or a near miss,
  the best the collection had and wrong; a question the documents do not
  answer is a near miss whatever came top. It returns the similarity that
  best separates the two, how often an answer outscores a near miss, and the
  trade at each candidate. With fewer than five of either it returns no
  cut-off and says which side is short. `vectrixdb golden cutoff` prints the
  table. Measured on SciFact with the bundled model by
  `scripts/answer_cutoff_bench.py`: 0.831, and an answer outscores a near miss
  only 74.5% of the time, because every near miss there is another abstract
  on the same subject. `search()` still has no floor: declining is the
  caller's line of code.
- **`evaluation.sweep()` compares how documents are cut.** Every combination
  of chunker, chunk size, overlap and headings-in-the-embedding is built in a
  scratch folder from the originals, asked the golden questions once for each
  way of searching given, and ranked by nDCG, MRR and time, with the chunks
  made, the build's seconds and the median search. `sweep_markdown()` is the
  table, `vectrixdb sweep` the command. Library and command line only: the
  Evaluate page compares searches of the index you have, as it did.
- **`vectrixdb golden label`** writes the labels `suggest_expected()`
  proposes into a golden file, each marked draft, and never over a file that
  is already there.
- **Two homes.** A collection kept on Azure AI Search or OpenSearch was
  already written to the service and to the index beside the process, under
  the same ids. `search(homes="store" | "local" | "both")` says which to ask.
  `"both"` fuses the two lists by rank and puts `home_ranks` in `explain`;
  when the service does not answer it returns the local list alone and says
  so in `degraded`. `"store"` raises when the service does not answer, where
  the usual routing falls back with a log line. A mistake in the call is
  raised and never taken for the service being away. Dense, keyword and
  hybrid; refused under an entitlement policy, where a decision record names
  one engine.
- **A figure found by what it looks like.** `Vectrix(image_embedder=...)`
  takes a model with a picture side and a text side, `embed_images`,
  `embed_texts` and `dimension`. Each figure's image is embedded into a
  vector index named `image`, under the figure chunk's id;
  `search(vectors="image")` asks it, the question through the text side, and
  `vectors="all"` fuses it with the words. An ordinary search never asks it.
  It follows a delete, a document written again and `rechunk()`. No picture
  model is bundled.
- **The rest of the dashboard's trends**, drawn on the canvas first and built
  as drawn. Access: who reads most, the six busiest people and keys of
  fourteen days with searches and text read apart
  (`GET /api/v1/access/readers`), and keys by last use, one not used for
  thirty days marked. A collection's Overview: chunks written a day
  (`GET /api/v1/collections/{name}/growth`). Its Builds tab: chunks by build,
  oldest first, a build whose mean quality is under the line in its own
  colour, and a quality column in the list; `/builds` gained `written_at`,
  `quality`, `low` and `quality_threshold`. Ingest: the quality of each
  document of the run against the line; the documents route's reply gained
  `quality_threshold`. Growth and builds are counted from the time, the build
  and the score every chunk already carries, so they need no log and no audit
  trail.
- **The Evaluate page has five bands: first, top 3, top 5, top 10, missed.**
  A run's `landed` has six numbers where it had five, 4th to 5th apart from
  6th to 10th. A run saved before is drawn with the bands it has.
- **A reference extraction server**, `examples/extraction_server`: a key on
  the way in, compared in constant time, and a refusal to start without one;
  `pages_reply()` for page spans from a PDF route and `minutes_reply()` for
  minute offsets from an audio route, neither importing anything, so they
  paste into a server that does not have the library. Run by the suite with
  made-up engines over a real connection.
- **A queue between the storage event and the worker on Azure**,
  `examples/deploy/azure_ingest_queue`: Event Grid writes to a Storage Queue,
  a queue-triggered function takes one message with an hour to read it, a
  failed read goes round again, and after five tries the message is in the
  poison queue, which is the list of files that would not read.
- **The server records how long a search took, and the Overview draws it.**
  It never did: the Search p50 tile is the browser's own session, so nobody
  could see search getting slower over a week. A search's line in the access
  log now carries `took_ms` and `status`, and nothing about what was asked.
  The line is written once the search has run, where every other read is
  written first, because the time is only known then; the rule that order
  kept still holds, since the reply has not left when the line is written,
  and a search that cannot be recorded is a 503 whose results never leave.
  `GET /api/v1/access/daily` gives the median and the slowest one in twenty
  a day, by nearest rank, and `null` for a day with nothing timed, which the
  chart draws as a gap and not as a nought. The card appears once a search
  has been timed. Drawn on the canvas first, then built as drawn.
- **The Overview and Audit pages have charts.** Only Evaluate and a
  collection's Quality tab drew anything; every other page was tiles and
  tables, which show now and never a trend. The Overview has searches a day
  and sign-ins against refusals for the last fourteen days, from the access
  log, for whoever may read it; the row is left out rather than drawn empty
  with sign-in off, a log on the server's output, or no collections. Audit has
  decisions a day by outcome and the same by collection, with each
  collection's denied and refused counts in words, because a thin slice of a
  long bar is easy to miss. Drawn at the width they are shown at, so the text
  is one size on a phone and a desktop, redrawn when the window changes, and
  coloured by class so both themes get them. Two things the server did not
  have: `GET /api/v1/access/daily`, counts only, no names and no collections,
  and `daily` and `by_collection` on `GET /api/v1/audit`, which count every
  record in the window and not only the newest ones listed (`days`, 1 to 90).
  Days are UTC. Search time over time is not drawn, because the server does
  not record it: the Search p50 tile is the browser's own session.
- **Every refusal is one shape.** There were two: the layer that checks keys,
  roles and scope answered `{"ok": false, "message": ..., "data": ...}`, and a
  route that was reached and then refused answered FastAPI's
  `{"detail": ...}`, a string, or a list of field errors on a 422. A client
  had to know which layer had said no to find the sentence. Every refusal now
  carries all four keys: `message` is always one sentence, and `detail` is
  what it always was, so nothing that read either shape broke and no
  deprecation was needed. On a 422 `message` says the field errors in words,
  `query_text: Field required`, and the reply no longer sends the caller's own
  input back. One module writes them, `vectrixdb.api.replies`, and a test
  fails when a refusal is written out by hand anywhere else, which is how the
  second shape came about.
- **A collection that is not the caller's reads exactly as one that is not
  there.** A guest asking for a private collection, a key asking outside its
  scope, and anybody asking for a name that does not exist now get the same
  reply to the letter. The scoped-key refusal used different words from the
  real not-found, which was enough to tell them apart and so to learn what
  else the server holds.
- **An app can act as the person using it: `VECTRIXDB_OIDC_API_AUDIENCE`.**
  An access token from the identity provider, sent as a Bearer token, is that
  person: the role their groups give them, a collection's entitlement policy
  judging the search as theirs, and their name in the access log with the
  method `token`. A key could never do the second of those. The audience must
  be this server's own and never the sign-in client's, because an identity
  token is issued to an application, and taken here it would let any app the
  person ever signed in to read their collections. Refused, each with a 401
  and a log line that never holds the token: another audience, another
  issuer, expired, `alg: none`, an unpublished key, nobody's group, and groups
  that did not fit in the token. No forgery token is asked for, since nothing
  rides on a cookie, and a change that needs a fresh check is refused,
  because an app cannot give one. Off until the audience is set.
- **A rate limit several servers share, and one for keys.** The guest limit
  was counted in each process, so three servers behind a load balancer gave
  every guest three allowances and a restart forgave everybody; a named key
  had no limit at all. Both are one count in the sign-in store now, on every
  backend. A key takes `requests_per_minute`, `VECTRIXDB_KEY_REQUESTS_PER_MINUTE`
  covers every key without a number of its own, and with neither a key is as
  unlimited as it was. A 429 carries `Retry-After` and the same number in
  `data.retry_after`. What is kept is a hash of the caller, not their address.
- **`vectrixdb keys add | list | revoke`.** Keys could be made in the
  dashboard or over the API, which left a server whose people all use single
  sign-on with no way to make its first one, and a deployment script with
  nothing to call. The key is printed once and the access log records it as
  made by the server console.
- **`vectrixdb check --url`**, a deployed server asked from outside what a
  caller would ask. Most of what goes wrong behind a gateway is not in the
  settings: a path not published, a key header stripped, the gateway's error
  page where the server's refusal should be, a document that says the wrong
  base, CORS added twice. It reads and never writes, sends a key that is wrong
  on purpose to see whose refusal comes back, and exits 1 on an error.
- **`VECTRIXDB_BUILD_THREADS=1`: two builds of the same vectors are the same
  index.** usearch inserts on every core at once, so the graph differs a
  little from build to build, and the tail of an approximate search with it.
  On one thread every build answers every query the same. It is slower, which
  is why it is a setting and not the default.
- **A key can be narrower than a role: `collections` and `expires_in_days`.**
  An app somebody else wrote should hold a key that reads the one collection
  it was made for, until a date, rather than a role across the whole server.
  Both are on the **API keys for scripts** form and on `POST /api/v1/keys`. A
  scoped key reaches its own collections and the routes where the server
  describes itself; another collection reads as one that is not there, its
  collection listing holds only its own, and a route that cuts across
  collections is refused. That last rule is written the closed way round on
  purpose: `/api/v1/documents` lists every document on the server and
  `/api/v1/visibility` names every collection, and neither carries a
  collection in its path, so a rule written the open way would have handed
  both to a key made for one collection, and so would any route added
  afterwards. An expired key reads as a key that is not ours, and the keys
  page marks it **Expired** instead of leaving somebody to guess why their app
  stopped. A key with neither is the key it has always been.
- **A key may arrive as `Authorization: Bearer`.** `api-key` is still read and
  still wins when both are sent, but a client generated from the OpenAPI
  document, an HTTP library and most gateways all reach for Bearer, so an app
  built from the document now works without a custom header.
- **[Build an app on it](docs/how-to/build-an-app.md)**, one page for whoever
  is writing an app against somebody else's server: asking for a key that is
  scoped and ends, the two headers, a search in curl, Python and JavaScript,
  generating a client from `/openapi.json`, what a scoped key may reach, the
  two shapes a refusal arrives in, and why the key does not belong in the
  browser.
- **[Put it behind a gateway](docs/how-to/behind-a-gateway.md)**, one page:
  the two settings, what to send the team that owns the gateway, the path
  families to publish as wildcards, the four routes worth naming, and the
  headers a policy must not strip (`Cookie`, `Set-Cookie`, `x-csrf-token`,
  `api-key`), with the size and timeout limits that bite on AWS.
- **Masking of email addresses, phone numbers and card numbers.** An admin
  turns it on per collection, one switch beside who can see it, or `mask` on
  the visibility route. Every reply from that collection's routes is then
  masked on its way to a person or a guest, metadata included, by one layer
  around every route so one added later is masked too; an API key is sent the
  text as stored. An address keeps its first letter and its domain, a phone
  number and a card their last four digits, a card must pass the Luhn check,
  and dates, times, decimals, versions and ids are left alone. The change is
  in the access log, a collection made again starts unmasked, and the rules
  are `vectrixdb.masking` for a program that wants them. It lowers casual
  exposure and the docs say where it stops: see "Masking identifiers" in
  `docs/how-to/sign-in.md`.
- **Reference pages read off the code.** `scripts/make_reference.py` writes
  `docs/reference/settings.md` (every setting), `cli.md` (every command and
  option) and `rest-api.md` (every documented route, the action it needs, the
  roles and key roles that hold it, and whether it asks for a fresh check). A
  test runs it in `--check` mode, so a setting, option or route added without
  its page fails.
- **Two pages for knowing it all.** `docs/how-to/tips.md`, the things that are
  easy to miss in the library and the dashboard, and
  `docs/explanation/limits.md`, every limit in one place with what to do
  about it.
- **A `test` extra** holding what the suite exercises without a network:
  `dev`, `api`, `mcp`, `signin`, `documents` and `azure`. `all` now includes
  `signin`, `mcp` and `documents` as well.
- **A collection's health says whether it has a knowledge graph**, as
  `capabilities.graph`, so the Graph tab explains itself instead of asking for
  a graph that is not there.
- **Evaluate every setup, and pick one of three.** `vectrixdb.evaluation.evaluate`
  searches every golden question every way the given collections can be
  searched: each engine, each set of dense vectors alone and fused, and each
  method the handle was opened for (keyword, dense, hybrid with and without
  the reranker, keyword and hybrid with Azure's semantic ranker where the
  index has a semantic configuration, ultimate, graph). Every search is timed as a caller sees it, after one untimed search
  that loads the models and with the search cache out of the way. The report
  ranks the setups and names three: `finds_the_most` (the most right answers
  in the top 10, then the most first, then the fastest), `best_for_balance`
  (the fastest within 4 points of it) and `best_for_time` (the fastest within
  8), inclusive and counted in whole questions, both thresholds settings. It
  also keeps the frontier nothing beats on both counts and, for each setup,
  the setups one change away. Nothing is switched. `save_to=` writes one
  file a run, `runs/<id>/report.json`, holding all of it, to a folder, S3 or
  Blob (`report_store`). See `docs/how-to/evaluate-setups.md`.
- **Golden files from anywhere, and a template to fill.** `read_golden` and
  `load_questions` read a path, `s3://bucket/key` or a Blob address, and give
  the file's sha256, which is what a run is filed under. `golden_template`
  writes a row per sampled document with its id already in `expected` and
  the start of its text in `hint`; rows nobody has filled are skipped and
  counted, so a file can be evaluated half written. A `writer=` callable
  drafts questions and marks them `draft` for a person to check.
- **A golden file is checked before a run, against one published schema.**
  `docs/reference/golden.schema.json` says what a line may hold, and
  `check_golden()` finds every way a file falls short of it at once, each
  with its line: not JSON, a field the schema does not have, a question or
  `expected` missing or empty, a value of the wrong kind, a page number that
  is not one, an id or a question used twice, no question ready. A misspelt
  field used to drop its row from the scores without a word, and a file with
  five mistakes took five runs to find them. Template rows still waiting,
  drafts, and documents the collection does not hold are notes, not errors.
  `expected` may name one page of a document, `report.pdf#page=41`. The
  Azure walkthrough's main app checks a file before its evaluation route
  opens a collection and refuses one with mistakes with the whole list.
- **A golden answer can be one page of a document.** `expected` may name
  `report.pdf#page=41`, the page's place in the file as its citation gives
  it, and a result counts when its chunk covers that page, in
  `retrieval_report`, `evaluate`, `answer_cutoff` and `missing_documents`
  alike; the results are then ranked by the pages they cover. A report of
  two hundred pages is one document, so scored by document every setup found
  it for every question and every setup tied. `golden_template(by="page")`
  writes a row a page, spread evenly through the collection, with the page,
  its printed number and its heading in `hint`, and takes a list of handles
  to sample several collections into one file. In the Azure walkthrough the
  golden dataset is one file,
  `.local/blob/evals/golden_dataset/golden.jsonl`, not one a collection, and
  `vectrixdb golden template` writes it from `.local/blob/ingestion/raw`
  before anything is deployed.
- **The walkthrough's files are a mirror of the storage account.**
  `examples/azure/data` became `examples/azure/.local`, laid out as the
  account is: `.local/blob/ingestion/raw/<collection>/<file>` is the blob
  `raw/<collection>/<file>` in the `ingestion` container, and
  `.local/blob/evals/golden_dataset/golden.jsonl` is the golden dataset where
  the evaluation reads it, so a push is a copy and nothing is worked out.
  What has not been dropped yet waits in `.local/later` instead of showing in
  a mirror that would be lying about the container.
  `04_push_local_to_blob.py` fetches whatever is missing and sends the first
  drop; `07_push_new_files_from local_to_blob.py` moves the shelf into the
  mirror and sends whatever the container has not got. Both skip a file
  already there at the same size, and both upload through one function,
  `_common.push_raw()`.
- **Golden questions drafted by a model, and checked before a person checks
  them.** `write_golden()` follows DeepEval's synthesizer on the
  collection's own chunks, the ones a search returns: passages spread across
  every section and scored by a critic, after contents pages, pictures'
  descriptions, bare numbers, scraps and garbled text are passed over
  without one; a mix from short to long, searches, facts, how and why,
  questions that need two places, and long questions that give the asker's
  situation first, the second place found by the collection's own model on
  another page; a critic for each question; and rewrites that make it
  harder. The chunks come from the ones a collection kept, which hold every
  file, before the copy beside a process, which in a function app holds one
  instance's share. Around them are checks no model makes: every quote must be
  in its passage word for word, and the page it is on is what `expected`
  names; a question that copies the passage, points at "the passage" or
  repeats one already written is sent back with the reason, while two that
  differ in a year are two questions. Every row is a draft, with the page
  and the answering words in `hint`. Passages go to the model marked as
  data with would-be control tokens made inert, a policied collection is
  read as somebody, answers are kept in a cache so a stopped run starts
  where it stopped, and a model that refuses stops the run at the first
  call rather than after every question. `ChatWriter` is any OpenAI style
  chat route, named by `VECTRIXDB_WRITER_URL`, `VECTRIXDB_WRITER_MODEL`,
  `VECTRIXDB_WRITER_KEY` and `VECTRIXDB_WRITER_KEY_HEADER`, or by
  `AZURE_OPENAI_WRITER_DEPLOYMENT`; it shares its call, the retries and the
  settings a service turns down, with `ChatDescriber`. `vectrixdb golden
  write` runs it, and the Azure walkthrough's
  main function app has it as step seven: `POST /api/v1/golden/write`
  queues a hundred questions, written by the queue trigger with the Azure
  OpenAI deployment `AZURE_OPENAI_WRITER_DEPLOYMENT` names into
  `evals/golden.jsonl`, never over one that is there, with
  `GET /api/v1/golden/write` saying how it is going.
- **`vectrixdb golden template` and `vectrixdb evaluate`.** The second opens
  each collection read only with `--mode ultimate`, prints the ranked setups
  and the picks, and saves to `<path>/evaluations` unless `--save-to` says
  otherwise.
- **The Evaluate pages.** The dashboard reads the newest run: the three
  picks, found in the top 10 against median search time with the frontier
  drawn, the top setups found at each depth, and every setup ranked seven to
  a page; each setup opens a page of its own with how far down its answers
  were, its search times, its neighbours and its top 10 run by run. Read only,
  at phone, tablet and desktop widths. The server reads runs from
  `VECTRIXDB_EVALUATIONS` (default: the data path's `evaluations` folder)
  through `GET /api/v1/evaluations` and `GET /api/v1/evaluations/{run}`,
  behind a new action, `evaluation.read`, that every signed-in role holds and
  guests do not.
- **A Function App that evaluates when the golden file changes.**
  `examples/deploy/azure_evaluate/` is reference code: Event Grid, one Durable
  Functions orchestration per golden file's hash, a minute's wait so a burst
  of saves costs one run, one activity per setup, and the report saved
  beside the golden file. `examples/deploy/aws_evaluate_handler.py` is the
  same behind an S3 notification.
- **Azure's semantic ranker, chosen search by search.** `rerank="semantic"`
  asks for the service's ranker on a keyword or hybrid search,
  `rerank="cross-encoder"` for MiniLM here instead, and `rerank=False` for
  neither, whichever way the store was opened; `semantic=True` only sets the
  default for hybrid search. So one index is compared with the ranker and
  without it: an Azure index with two sets of vectors and a semantic
  configuration gives an evaluation fourteen setups. Asking for the ranker
  on an index with no configuration, on a dense search, or off Azure AI
  Search is a `ConfigurationError` that says why.
- **Keyword search on a handle that holds nothing locally is the service's
  own.** A handle opened to search an index another process filled has an
  empty words index, and its keyword search found nothing. It goes to Azure
  AI Search or OpenSearch now, the way its dense search already did, and an
  evaluation offers keyword search on it.
- **Expected documents are looked up before an evaluation searches.**
  `missing_documents` finds each document a golden file expects on every
  target, by its id and its first chunks', here and then in the store. One
  a target does not hold is a miss on every setup, so `evaluate` warns
  with `MissingDocumentsWarning`, naming the documents and the questions no
  setup can find, and keeps them under `missing` in the report; the CLI
  says so before the searching starts, and the Function App returns them.
- **Older runs, one menu away.** The Evaluate page names its golden data and
  how many questions it holds on one line, with a menu at its end that opens
  any older run, twelve at a time. An older run on screen says so, on the
  setup pages too, with a way back to the newest. `GET /api/v1/evaluations`
  takes `offset` for the next page.
- **A note when a run is missing documents.** Above the picks, in the words
  the command line uses: which documents an index does not hold, and which
  questions no setup could find because of it. The report the server sends
  carries them as `missing_notes`.
- **The golden file a run used, for an admin to download.** Each version of a
  golden file is kept once, as `golden_dataset/<sha256>.jsonl` beside the
  runs, by `evaluate`, the command line and the Function App. One kept
  earlier in 2.2's development as `golden/<sha256>.jsonl` is still read. A button beside the
  golden data gives back the exact file a run read, even after it has
  changed, to an admin signed in as a person:
  `GET /api/v1/evaluations/{run}/golden`, a new action, `evaluation.golden`,
  recorded in the access log. The server's own admin key holds the role and
  is still refused, because a key is a script and the file holds the
  questions' text. A run saved before files were kept has none, and the
  button is not shown.
- **`vectrixdb check`, an env file and a template.** `vectrixdb check` reads
  the settings through the server's own code and says what a start would
  refuse or get wrong: a misspelt name with the one probably meant, a setting
  and its `_FILE` twin both set, a number that is not one, the storage
  backend, the keys, sign-in and its secret, mail, who will be an admin (on
  the email list, named in `VECTRIXDB_SIGNIN_USERS`, or in a group single
  sign-on maps to admin), the access log, CORS and framing, the brand, the extractor, the audit
  trail and the evaluation runs. It exits 1 on an error, so a pipeline can
  gate on it. `--template` prints every setting VectrixDB reads, 83 of them,
  grouped and commented out, secrets left empty; a test fails when the code
  reads a `VECTRIXDB_` setting the list does not have. Every command that
  opens the data or the list of people takes `--env-file`, where what the
  environment already sets wins, and finds the data at `--path`, else
  `VECTRIXDB_PATH`, else `./vectrixdb_data`. See `docs/how-to/deploy.md`.
- **The access log on the server's output.** `VECTRIXDB_ACCESS_LOG=stdout`
  writes each line to stdout, marked `"log": "vectrixdb.access"`, for App
  Service, Container Apps, ECS or Kubernetes to collect, and the Access page
  says to read it there.

- **`relevance`: how good a match is, from 0 to 1, the same on every engine.**
  `score` is whatever a mode ranks by, and in a hybrid search that is a sum of
  reciprocal ranks where the best possible result scores about 0.018, so it
  orders a list and says nothing about how good anything on it is. Every
  `Result` and every search reply now also carries `relevance`, with
  `relevance_kind` saying what the number is: `reranker` when a cross-encoder
  judged the pair, `similarity` for the cosine similarity, `relative` for a
  keyword score as a share of the best hit, `distance` for a collection that is
  not cosine, and None where no honest number exists. `Result.similarity` keeps
  the cosine on its own, so a threshold can stay on one scale whether or not a
  search reranked. `matched_by` says which search found a chunk: meaning,
  keywords or both. `score` is unchanged. See `docs/explanation/relevance.md`.
- **The same number from Azure AI Search and OpenSearch.** Azure's
  `@search.score` is `1 / (1 + cosine_distance)` and is turned back with the
  conversion Microsoft documents; the semantic ranker's 0 to 4 verdict becomes
  `reranker`. A hybrid search, and a search over two vectors, come back from
  the service as fused ranks, so the similarity is asked for with one more
  vector query (`azure_search_relevance=False` saves it). OpenSearch
  documented `1 / (1 + d)` for nmslib and faiss through 2.17 and `(2 - d) / 2`
  for every engine from 2.19, so the store does not trust a table: it checks
  both formulas against a hit whose vector it can read and uses the one that
  reproduces the true cosine (`opensearch_score_formula` overrides). A store
  that holds two vectors reports both similarities in `relevances`.
- **A collection's card says what it can do and what it is built with.**
  The health route returns `capabilities` (dense, keyword, hybrid) and
  `services`: where the vectors are stored, which models embedded them (two,
  when a store holds the collection's own vector and the service's), and which
  engines read the files, by the names people know them by: Azure AI Search,
  OpenSearch, Azure OpenAI, Amazon Bedrock, Amazon Textract, Azure Document
  Intelligence.

- **A chunk's text is sent when somebody asks for that chunk.** The
  dashboard's Points table was built by fetching every chunk on the page,
  text and all, and the quality report and a provenance lookup each carried
  an excerpt, so opening a tab meant being sent the content of everything on
  it. Listings carry where a chunk came from now
  (`GET .../points?index=true` returns id, source, page and quality, never
  text), and a "Show text" control opens one chunk under its row. With sign-in
  on, each one is a line in the access log that names the chunk, in a new
  `item` field, which also names a document that was opened or refused.
- **Opening a whole document is its own permission, `document.read`.** A
  stored document is a larger disclosure than a chunk, so an operator does
  not hold it by being an operator. An admin does, and gives it to named
  operators on the Access page, or to a group with
  `VECTRIXDB_OIDC_GRANT_MAP`. It is the only thing that can be given that
  way, a grant never turns a viewer into a reader, and changing what somebody
  holds ends their sessions. The read-only API key keeps it, as it always
  fetched documents.
- **Search excerpts, cut on the server.** `?snippet=N` on `search`,
  `text-search`, `text-hybrid-search` and `keyword-search` cuts each result's
  text and highlights to N characters and marks it `snipped`. The dashboard
  asks for 200. Without the parameter nothing changes, because a program that
  searches wants the text.
- **The quality report says why.** Each of the lowest chunks comes with
  `reasons`, the scorer's failing signals in words ("symbols and stray
  glyphs", "few ordinary words: a table, codes, or another language"), and
  `below_line`. `?text=false` leaves the excerpt out, and so does
  `?text=false` on a provenance lookup.

- **People sign in to the server and its dashboard.** Two ways, either or
  both, and neither needs a password. `VECTRIXDB_SIGNIN=oidc` is single
  sign-on through any OpenID Connect provider (Entra ID, Okta, Google
  Workspace, Auth0, Keycloak): the authorization code flow with PKCE, the
  identity token verified against the provider's published keys before a claim
  is read, and security groups mapped to roles. `VECTRIXDB_SIGNIN=email` is an
  allowlist for a team with no provider: a one-time link by email lets the
  person choose a passkey or an authenticator app, every sign-in after that is
  the passkey or the address and the 6-digit code, and ten recovery codes plus
  an admin's reset cover a lost device. The first admin is added on the server
  with `vectrixdb people add`, which prints a one-time link; `people reset`,
  `list` and `remove` do the rest. New extra: `pip install "vectrixdb[signin]"`.
  See `docs/how-to/sign-in.md`. If you ran an earlier build of this release
  with sign-in on, everybody signs in once more after upgrading, because the
  cookie keys are now derived from the secret per purpose.
- **Passkeys.** A passkey is made on the person's own computer and unlocked
  with its PIN, never leaves it, and cannot be used by a look-alike page. The
  server checks the relying party, the origin, the challenge (hashed, used
  once, five minutes long), that the device checked it was the person, and,
  where the device keeps one, a signature count that must go up, so a copied
  key is refused. ES256, EdDSA and RS256, with no new dependency.
- **How you sign in.** Everybody signed in has a page for their own ways in
  under the account menu: add or remove passkeys (never the last way in), set
  up a new authenticator while the old one keeps working until the new one is
  confirmed, make fresh recovery codes, and see every browser they are signed
  in on, with its device, place and last use, and sign any of them out.
- **Confirm it's you.** Deleting a collection, sharing one, managing people,
  managing API keys and changing your own ways in need a check from the last
  ten minutes. The dashboard asks for a passkey or a code and then makes the
  change; the API answers `403` with `step_up` and the ways that will do.
- **Passwords, only if you want them.** `VECTRIXDB_SIGNIN_PASSWORDS=on` asks
  for a password with the code, never instead of it: twelve characters or
  more, common ones refused, stored with scrypt, and a reset that still needs
  a current code.
- **A lock that grows.** Five wrong codes shut an address out for fifteen
  minutes, then an hour, four hours and a day, and the person is emailed when
  it happens. Recovery codes have their own count, and one network address
  gets fifty wrong tries across every address.
- **Single sign-on by security group and an address list.**
  `VECTRIXDB_OIDC_ALLOWED_EMAILS` takes addresses and `@domains`, and a person
  then needs both a role from their groups and a place on the list. The button
  says **Continue with SSO**; a full-screen page shows the access being
  checked, and a refusal offers **Try another account**.
  `VECTRIXDB_ADMINS_USE_SSO=on` keeps admins to single sign-on.
- **Guests and shared collections.** With `VECTRIXDB_GUESTS=on`, an admin can
  share a collection with everyone from its Settings tab. Guests see only what
  is shared, search it and read excerpts (or whole chunks, if the admin chose),
  never vectors; a private collection is a `404` to them; thirty searches a
  minute from one address, each in the access log. Delete a collection and
  make it again, and it starts private.
- **API keys for scripts.** An admin makes named keys with a role each
  (`reader`, `searcher` or `operator`), shown once, kept as a hash, revoked on
  their own and named in the access log. The two server keys can be given as
  their SHA-256 (`VECTRIXDB_API_KEY_SHA256`,
  `VECTRIXDB_READ_ONLY_API_KEY_SHA256`), and every secret setting can be read
  from a file named in its `_FILE` twin.
- **Sign-in kept where several servers can share it.** `VECTRIXDB_SIGNIN_STORE`
  moves sign-in out of the SQLite file into PostgreSQL (`postgresql://`),
  Azure Cosmos DB (`cosmos://`, with an account key or the machine's own
  identity) or Amazon DynamoDB (`dynamodb://`, with the usual AWS chain), with
  the existing `postgres`, `azure` and `aws` extras. A code, a recovery code, a
  link and a passkey challenge are each spent once however many servers are
  asked at once, and a change one server makes is never lost to another's.
  What the server needs is made on its first start where it may be, and named
  exactly where it may not. The password or key is `VECTRIXDB_SIGNIN_STORE_KEY`,
  never part of the address, which the start-up message now shows.
  `VECTRIXDB_AUTH_PATH` moves the sign-in folder away from the data. A file from
  an earlier build of this release is brought over on its first start, nobody
  signed out, and kept as it was beside it as `signin.v1.db`.
- **Passkeys only, if you want that.** `VECTRIXDB_SIGNIN_REQUIRE=passkey`: a
  new person makes a passkey and is never shown an authenticator; a code from
  somebody who has one is refused, and said so only once it proved right;
  recovery codes still work. Somebody with only an authenticator signs in with
  it once more and makes a passkey before they can reach anything else.
- **A certificate in place of a client secret.** `VECTRIXDB_OIDC_CLIENT_KEY`,
  with `VECTRIXDB_OIDC_CLIENT_CERT` for Entra ID or
  `VECTRIXDB_OIDC_CLIENT_KEY_ID` for Okta and Keycloak, signs a short assertion
  (`private_key_jwt`, RFC 7523) each time the server redeems a sign-in, so no
  secret travels. RSA of 2048 bits or more, or EC on P-256 or P-384; a
  certificate that is not the key's, or has expired, is refused, and one in its
  last thirty days is warned about at every start.
- **A company's own name, logo and colour.** `VECTRIXDB_BRAND_NAME`,
  `VECTRIXDB_BRAND_LOGO`, `VECTRIXDB_BRAND_LOGO_DARK` and
  `VECTRIXDB_BRAND_ACCENT` brand the dashboard before it is deployed. A logo is
  SVG, PNG or JPEG, checked by its content, and an SVG that could run anything
  is refused; the accent is turned into readable shades for both themes. The
  line "© 2026 VectrixDB" stays whatever the brand, with the version beside it
  for people who have signed in; with sign-in on, `/` and the OpenAPI
  description leave the version out for everybody else. See
  `docs/how-to/branding.md`.
- **The dashboard is light by default,** with its settings in an account menu
  top right, the sidebar in two groups (Explore and Manage), and times written
  short: "5m ago", "3h ago", "2d ago".
- **Roles.** `viewer` sees that collections exist and how healthy they are,
  and is never sent chunk text; `operator` reads, searches and writes;
  `admin` also reads the audit trail and the access log, manages people and
  deletes collections. One table, `vectrixdb.signin.roles`, read by the
  server and drawn by the dashboard. A route the table does not place can be
  called only by an admin, and a role it does not know holds nothing.
- **A signed-in person can search a collection that carries a policy.** Their
  principal comes from their groups, or from what an admin wrote beside their
  address, and goes to the collection with the query. The decision record the
  library writes is written by the server too, under their name, when
  `VECTRIXDB_AUDIT_JSONL` is set. An API key is still nobody, and such a
  collection stays closed to it.
- **An access log.** Every sign-in, failed sign-in, read of content, search,
  write and refusal is a line in `<database path>/auth/access.jsonl`, naming
  the person, the action and the collection and never a query or a chunk's
  text. A read that cannot be recorded is refused. The dashboard has an
  Access page for admins: people, what each role may do, and the log.

- **Policy and filter pushdown on OpenSearch.** `with_opensearch(filter_fields=)`
  maps the metadata paths you name as real index fields, so a filter or an
  entitlement policy over them runs inside OpenSearch, where until now the
  metadata sat in an object the service stores and does not index. With every
  field a policy names promoted, `pushdown_mode` is `ENGINE` and
  `require_pushdown=True` opens: scope rules travel with the query, redaction
  rules are decided locally with exact counts, and a document outside the
  scope never leaves the service. Null and absent are the same to OpenSearch,
  so every clause requires the field to exist and a null fails the rule; a
  value of the wrong type is stored as null for the same reason.
  `knn_engine="lucene"` or `"faiss"` filters during the k-NN search, where
  `"nmslib"` can only filter after it and asks for more neighbours to make up.
- **`embeddings=` on OpenSearch, with Bedrock.** `"vectrixdb"`, `"bedrock"` or
  `"both"`, the same switch as on Azure AI Search: a second k-NN field,
  `search(vectors=)`, `vector_weights` and `vector_ranks` in `explain`.
  `vectrixdb.models.bedrock.BedrockEmbedder` wraps a `bedrock-runtime` client
  the host built; OpenSearch has nothing here that embeds a question, so
  Bedrock is called at ingest and at query.
- **`reranker=` on `Vectrix`.** Anything with `rerank(query, texts, top=None)`
  takes the bundled cross-encoder's place in every mode that reranks, and
  `explain` names it. `BedrockReranker` is one, around a
  `bedrock-agent-runtime` client, sending a long list up in slices.
- **A burst of events.** `IngestWorker.handle_all()` acts on the last event
  for each object and answers the earlier ones `superseded`, so an object
  created and deleted in one batch is never read, and it saves the vector
  index once for the batch. `Vectrix.deferred_saves()` is that switch for any
  loop of writes: `add()` rewrites the index file each time, which makes one
  write durable and a thousand slow.
- **`vectrixdb.evaluation`.** Questions with known answers, from this
  library's JSONL or from a Bedrock RAG evaluation dataset, scored for
  retrieval with recall at k, MRR and nDCG, by document or by chunk, with the
  misses listed and what came back instead. `suggest_expected()` proposes
  labels by searching with the reference answer and never applies them.
  `answer_report()` scores written answers with a judge you bring, on the four
  names Bedrock's managed evaluation uses. A report over no labelled questions
  says so rather than reporting a perfect score.
- **A reference AWS stack**, `examples/deploy/`: ingest-on-event made and
  unmade in dependency order, with the bucket's notifications merged into
  what is already there, since setting them replaces all of them. Reference
  code, not run by the suite; the merge, the plan and the handler's imports
  are tested.

- **`embeddings=` on Azure AI Search: the collection's vectors, an Azure OpenAI
  deployment's, or both.** `VectrixDB.with_azure_search(embeddings="both",
  azure_embedding={...})` builds one index with two vector fields, and the
  service runs BM25 and both vector searches in one request, under one filter
  and one ranker. `"azure"` holds the deployment's vectors alone, and the
  service embeds the question itself through a vectorizer. Azure AI Search
  owns no embedding model, which the docs now say in as many words. The second
  vector goes through the embedding cache, a deployment of the wrong width is
  refused at the first write, and a write that did not come through `Vectrix`
  is embedded at insert.
- **`search(vectors=)` and `vector_weights`.** An index built with `"both"`
  answers with either vector or both, so a comparison between them is over the
  same chunks, filter and ranker. `explain=True` adds `vector_ranks`, which
  vector ranked a result where, at one extra request per vector, since the
  service fuses and does not say. A search that asks for a vector only the
  store holds is served by the store, a store failure then raises rather than
  answering from the local index, and the result cache is off on such an index
  because its key does not include which vector answered.
- **More than one dense model locally.** `dense_model` takes a list: the first
  is the collection's own and each of the others, a model name or
  `(label, embed_fn, dimension)`, gets an index of its own under
  `<name>.vectors`. A write reaches all of them and a search fuses them by
  rank or asks for one by name. The list is part of the collection. Not
  offered under an entitlement policy, where fused results would not be the
  ones the decision record names.

- **Documents over REST.** `POST /api/v1/collections/{name}/documents` takes a
  file as the request body with its name in `X-Filename`, or a multipart form
  when `python-multipart` is installed, and reads, cuts, embeds and writes it
  the way `add_document()` does, because both now call
  `vectrixdb.ingest.prepare_document()`: the same ids, citations, lineage
  stamps and figure links. The reply carries the chunk count, the extraction
  quality and every citation. `VECTRIXDB_EXTRACTOR_URL` hands file types to a
  service the operator runs and a failure there is a 502 with nothing
  written; `VECTRIXDB_KEEP_SOURCE=1` keeps the Markdown, which
  `GET .../documents/{doc_id}` serves and `DELETE` moves aside; and
  `GET /api/v1/extractors` says what the server reads. A file that says it is
  larger than `VECTRIXDB_MAX_UPLOAD_BYTES` is refused unread, a path in the
  name is reduced to its last part, a policied collection is refused like
  every data route, and the server never fetches an address a request names.
- **The dashboard reads documents through the server.** The Ingest page asks
  the server what it reads, sends dropped files and pasted text to the
  documents route, and shows each document's chunk count, reader, quality and
  citations. A point whose document was kept has Open document, which shows
  the Markdown it was indexed from with that chunk marked.

- **`keep_source`: the Markdown a document was indexed from is kept.**
  `Vectrix(keep_source=True)` writes one Markdown file per document beside the
  collection, or into a folder, a bucket (`S3Files`) or a container
  (`BlobFiles`) through `vectrixdb.documents.DocumentStore`, with front matter
  that reads back exactly: pages, headings, figures, metadata, the extractor
  and the version of the original. `rechunk()` cuts kept documents again and
  never calls an extractor, so a new chunk size over OCRed pages costs an
  embedding pass and not an OCR bill, and on an empty collection it fills the
  index from the store alone. `reextract(where=)` reads only the originals a
  function picks, an old extractor's say, and leaves a document whose text
  comes back the same alone. `document()` returns a kept document and
  `delete_document()` moves it under `_deleted` rather than removing it;
  nothing is purged unless a retention is named. The file is the original's
  whole name plus `.md`, so an original that is itself Markdown is never
  mistaken for its own extraction, and the store is a root of its own:
  `LocalWatcher` refuses a folder that overlaps it and the worker answers
  `ignored` to an event from inside it, because a store inside a watched
  folder ingests its own output for ever.
- **Front matter.** `LoadedDocument.to_markdown()` and `from_markdown()`, and
  `split_front_matter()`. A Markdown original's own front matter becomes its
  metadata and so every chunk's, which is where the fields a policy decides by
  can come from; `doc_id` there is the document's id when none is passed.
  Values are read as JSON or bare, with `[a, b]` lists and block lists;
  anything nested deeper is left out rather than guessed at.
- **The worker reads an original once.** With a document store the version of
  the original, the event's ETag or else a hash of the bytes, decides whether
  to extract, and the text decides whether to index. The same ETag is
  `unchanged` without the object being fetched, and a file saved again with
  the same words costs one extraction and no write.
- **The figure block.** An image line with a short caption and the blockquote
  directly under it as the long description become one chunk: the caption is
  what is cited and the description, values, trend and the words inside the
  image, is what is searched. A figure is embedded with the headings above it
  and the sentence that mentions it, carries a stable `figure_id` such as
  `q3.pdf#p4-fig1` and its `figure_src`, and is linked both ways with the text
  that names it, `refers_to` and `referenced_by`; `Figure 31` is not
  `Figure 3`. `db.with_figures(hits)` brings a paragraph's figures along
  through the same policy check as any read, `hits.figures(top=)` is what to
  hand a model that can see, and `db.figure_bytes(hit)` is the kept image.
- **`describe_figures=`.** A callable of the image bytes and a context, the
  caption, the page, the heading and the text either side, that returns the
  description, once, at ingestion; what it wrote is kept in the Markdown so it
  is never paid for twice. It may return a caption, a `table` whose rows are
  rendered the way a spreadsheet's are, or `decorative`. `HttpDescriber`
  points it at a route you run. A Markdown file's images are read from beside
  it, never from outside its folder or from a remote address, and a PDF's are
  taken off each page when a describer is set. Decoration is removed before
  anybody is asked to describe it: small images, long thin ones, and one that
  turns up three times or more in a document.

- **Extraction is a slot.** `vectrixdb.extract`: who turns a file into text
  is a function you register or an endpoint you run, per collection with
  `Vectrix(extractors=...)` or for the process with `extract.register()`.
  An extractor takes the bytes and the name and returns Markdown, or a
  mapping with `text`, `pages`, `headings` and `metadata` when it knows where
  the pages break, which is what makes an OCRed PDF citable by page. A suffix
  nobody registered is read by the built-in readers, so registering one
  extractor changes one format. `HttpExtractor` sends the file to a service:
  as a multipart field, or with `body="raw"` as the request body with the name
  in a header, for a route written around the raw body; a route may carry a
  query string, and anything but a success raises `ExtractionError` with the
  route and the status. Offsets that run past the text are refused, because a
  wrong offset would give every later chunk the wrong page in silence. The
  ingestion worker uses the extractors its collection was opened with and
  hands them the fetched bytes without a temporary file. A failed extraction
  raises, since on a queue the exception is what asks for the retry;
  `on_error="record"` returns a `failed` outcome instead, and either way the
  document's existing chunks are left alone. `load_bytes()` reads bytes that
  are not a file here, and `load_url()` reads a page or a file by its address.
- **Engines for what is not text yet.** `vectrixdb.extract.engines`:
  `RapidOcr` reads images and only the scanned pages of a PDF, `Whisper`
  transcribes audio, `Video` hands a video's sound to an audio extractor, and
  `AzureDocumentIntelligence`, `AzureSpeech`, `Textract` and `Transcribe` do
  the same jobs through a client or a key the host made. Speech becomes pages
  by the minute, so a transcript is cited `call.wav#page=12`. An image or a
  recording with no extractor registered goes to the local engine and names
  the extra to install, where it used to be decoded as text and indexed as
  noise.
- **Extras named for what the file becomes.** `documents` (PDF, DOCX, XLSX),
  `ocr`, `asr`, `video`, and `extract` for every local reader; a cloud SDK is a
  suffix, `ocr-azure`, `asr-azure`, `ocr-aws`, `asr-aws`. The dependency error
  for a missing PDF, DOCX or XLSX reader now names `vectrixdb[documents]`.
- **`embed_heading=True` on `add_document()`.** The model is handed
  `Terms > Late fees: Interest accrues monthly.` and the collection stores
  `Interest accrues monthly.`, so a section that never names its own subject
  is found by it while the stored text and the keyword index stay as written.
  The prefix is kept on the chunk as `_vx_embed_prefix`, and `reembed()`
  embeds the same thing again.
- **Tables are rows wherever they come from.** A Markdown pipe table, in a
  file, a string or an extractor's reply, and a deck's table are rendered the
  way a spreadsheet row is, `Region: EMEA; Revenue: 1200`, with page and
  heading offsets moved along with the rewritten text. A table inside a code
  fence is left alone. A deck's cells now stay in their columns: an empty cell
  used to shift every value after it under the wrong header.

- **XLSX, PPTX and CSV loaders.** `add_document()` and the ingestion worker
  now read spreadsheets, decks and CSV files. A sheet or a CSV becomes one
  line per row, `Region: EMEA; Revenue: 1200`, headed by the first row when
  it is text and by the sheet name, so a row reads like a sentence to the
  embedding model and its cells are keywords to the sparse index. A deck
  becomes one page per slide with the slide's title as its heading and its
  tables as rows, in presentation order, cited as `deck.pptx#page=4`. XLSX
  needs `openpyxl`; PPTX and CSV are read with the standard library.
  Charts, images and speaker notes are not read.

- **Rerank on Azure AI Search means the service's semantic ranker.** With
  `semantic=True` a hybrid search is scored inside the service, each hit
  carries the ranker's score, `explain=True` reports it as `semantic`, and
  the local cross-encoder does not run a second, weaker pass on top. A
  policied hybrid search goes through the service when the policy's fields
  are promoted: the scope rules travel with the query, the redaction rules
  are decided locally with exact counts. Before this a policy forced every
  hybrid search on Azure onto the local path, whatever the store could do.

- **Sentences survive the page break, and figures are chunks of their own.**
  The PDF loader joins a page to the next with a space when the sentence
  plainly runs on (no closing punctuation, then a lowercase letter, digit
  or opening quote), so it reaches the chunker whole, and a page holding
  only a page number is dropped and still counted. A chunk that spans
  pages carries `page_end` beside `page`. A Markdown image or an HTML
  `<img>` or `<figure>` becomes a `[Figure: caption]` line that every
  strategy keeps as one chunk with `figure` set to the caption, cited as
  `report.pdf#page=3(caption)`. The caption is what is searchable; the
  library does not describe images.

- **An ingestion worker for every event source.** `vectrixdb.worker.IngestWorker`
  takes an object event, fetches the bytes through a client the host built,
  runs `add_document()` with everything the collection was opened with (the
  policy's contract, the quality gate, the audit sink, the build stamping)
  and returns what it did. Idempotent on the document's bytes, so a queue
  that delivers twice is safe; a changed file replaces its chunks under the
  same id; a delete removes the document and mints a build. `events_from_s3`
  reads S3 notifications direct or via SQS, `events_from_event_grid` reads
  Blob Storage events in either schema, `S3Fetcher` and `BlobFetcher` read
  through boto3 and azure-storage clients, and `LocalWatcher` polls a
  directory for machines without a queue. Tested against fakes; the how-to
  carries the Lambda and Azure Function shapes.

- **Citations.** Every chunk `add_document()` writes carries `_vx_citation`,
  `report.pdf#page=3` for a paged source and `guide#Offline` for a markdown
  section, and every result has `.citation`. `Results.citations()` lists
  them best first, `Results.sources()` is the `[citation]: text` block for a
  prompt, `CITATION_INSTRUCTION` is the sentence to put beside it, and
  `validate_answer(answer, hits)` checks what came back: a bracket naming a
  source the model was given stays a citation, anything else becomes plain
  text and is listed as rejected. The dashboard shows the citation on each
  hit. The pattern is the one the Azure search sample settled on and needs
  no Azure.

- **Engine pushdown on Azure AI Search.** `with_azure_search(filter_fields=...)`
  promotes metadata paths to real filterable index fields, the backend
  compiles the library's filter grammar to OData, and a policy whose rules
  all name promoted fields runs its scope rules inside the service:
  `pushdown_mode` is `ENGINE`, `require_pushdown=True` opens, the record says
  `engine`, and a document outside the scope never leaves the store. The
  redaction rules are still decided here so the withheld-disclosable count
  stays exact. A backend declares what it can run with `filter_fields()` and
  `compile_filter()` on the storage contract; the others still say `POST`.
  On the service an absent field and a null one are the same, so a null
  promoted field fails every rule, walls included.

- **The dashboard, rebuilt.** Eight pages instead of a table and a search
  box: collection cards with state pills and a detail page with Overview,
  Points, Policy, Builds, Quality and Settings tabs; an Audit page; a
  Console with presets and the curl it sent; Learn with the tutorials and the
  guide; Settings with every model and whether it is present. One token
  sheet with a light and a dark theme, Plex Sans for words and Plex Mono for
  anything a person copies, no build step. The page is three files now
  (`index.html`, `app.css`, `app.js`) and the smoke test checks all three are
  served.
- **Inspection routes** behind those pages: `health`, `policy`, `builds`,
  `quality`, `provenance/{id}` and `rebuild` on a collection, `/api/v1/models`,
  and `/api/v1/audit`. A policied collection answers `health` and `policy`
  with configuration only and refuses the rest, since this API resolves no
  principals. The audit route needs an API key configured and presented and
  `VECTRIXDB_AUDIT_JSONL` set, and drops the principal snapshot, the result
  ids and the undisclosable count before answering.
- **`"rerank": true` on `/text-search`** re-ranks the dense candidates with
  the bundled cross-encoder.
- **Every REST write mints an index build id** and stamps `_vx_build` on the
  chunks it lands, and `text-upsert` records the embedding model on the
  collection, so lineage covers API-written chunks the way it covers
  `Vectrix.add()`.

- **Entitlement policies: a filter a collection cannot be queried without.**
  An entitlement filter the caller builds on every query is a control that
  works until somebody forgets, and `search_with_acl(acl_field=...)` was
  opt-in, so a call site that omitted it returned everything and nothing said
  so. A `Policy` is the same predicate declared once against the collection.
  It is persisted with the collection, so reopening without passing it again
  does not drop it; reopening with a different one raises `PolicyMismatch`
  rather than quietly relaxing the rules. `db.as_principal({...})` returns a
  view that reads as one principal, and `search()` and `get()` on a policied
  collection with no principal raise `PrincipalRequired` instead of answering.

  Six predicates cover every case study mapped against them, addressed by
  dotted field path against principal key, so a new vertical is a policy
  literal rather than a code change: `Overlap`, `Equals`, `AtMost`, `AtLeast`,
  `Excludes` and `Present`. They compile to the filter engine that already
  exists, which means a backend that pushes filters into SQL enforces a policy
  in the query rather than after it.

  Three properties are load-bearing, and two of them come free from work
  already done in 2.2. A document with a missing entitlement field is
  invisible rather than universal, because `FilterCondition.matches` rejects
  `MISSING` before any operator runs; there is deliberately no switch to turn
  that off, and a collection with genuinely public documents should say so in
  a field and have a rule that permits it. An empty principal value matches
  nothing on its own, because `$in []` matches no document and `$nin []`
  excludes none, which is only true since membership was fixed to mean overlap
  earlier in this release. The third is new: a principal missing a key the
  policy names raises `PrincipalIncomplete` rather than denying, because that
  is the host's entitlement resolver failing and counting it as a denial is
  how a broken resolver hides behind traffic that looks normal.

  `Decision` separates a document withheld inside a scope the principal holds
  from one they may not know exists, which `Predicate(scope=True)` declares
  because it cannot be inferred. A caller handed one "suppressed" count will
  render it, and the first time somebody outside a scope reads "7 results
  withheld" the ethical wall is gone. `Policy.partition` returns the two
  counts apart for exactly that reason.

  There is no `AccessDenied` exception, and there must never be one: a denial
  is an empty result. An exception a caller can catch and tell apart from
  "nothing matched" is itself the side channel, whatever the layer above does
  with it. Everything under `PolicyError` is a refusal to run, not a refusal
  of access. `Policy.fingerprint` is a hash of the rule set rather than a
  version number, because a version number can be forgotten when somebody
  edits a rule.

  Nothing here authenticates anybody. A principal is a plain mapping the host
  resolved; the library has no way to check it and does not try. `ROADMAP.md`
  draws that line under its first principle.

- **A slimmer wheel: e5-small-v2 is fetched once instead of shipped.** The
  wheel was 89.8 MiB against PyPI's 100 MiB limit, and 21.5 MiB of it was
  e5-small-v2, the English default before 2.2, kept only so collections
  written with it would open offline. Every remaining bundled model is
  larger than the room that left, so the next model could not have fitted
  and no trimming elsewhere would have changed that. It is out of the wheel
  and the sdist now, which brings the wheel to about 68 MiB. A collection
  written before 2.2 still opens with it: the first search fetches it once
  through `vectrixdb download-models --type dense_en` or with
  `VECTRIXDB_AUTO_DOWNLOAD=1`, and the error on a machine that allows
  neither names that and the other way out, moving the collection to the
  current default with `reembed()`. Nothing built since 2.2 is affected.

- **An audit sink under an object lock.** `PostgresAuditSink` makes the
  trail something the audited application cannot edit; `ObjectLockSink` makes
  the stronger claim a regulator asks for, that nobody can, because the store
  itself refuses. Every record is one object written under a retention lock
  in compliance mode. The sink checks at construction that the bucket has
  Object Lock enabled and refuses otherwise, because the same put lands in an
  unlocked bucket and stays exactly as long as anybody with delete permission
  wants it to. `retain_days` has no default. Every key carries the date, a
  sequence number and the hash of the body, and `read_all()` refuses an
  object that does not hash to its own name. Batching is there and means
  what it says, so it needs a `Spool`. `S3ObjectLockStore` is the shipped
  store, taking a boto3 client so credentials and endpoints stay the host's;
  anything with the same four methods works. The fake in the suite enforces
  the lock; that S3 does is a gated live test.

- **An extraction quality signal, measured before it was shipped.** A
  scanned PDF that OCRs badly produces chunks that pass every schema check
  and destroy retrieval: the fields are all there, the text is nonsense, and
  the failure surfaces later as a ranking problem nobody can explain. The
  roadmap held this back until it could be measured against a labelled set
  rather than shipped as a heuristic nobody trusts, so it was measured.
  `extraction_quality(text)` scores five signals a bad OCR pass moves and
  clean prose does not: the share of ordinary characters, of whole words, of
  pronounceable words, the hit rate against a short list of common English
  words, and mean word length. Each is reported, so a low score can be read.

  The threshold comes from `scripts/quality_eval.py`, which rebuilds a
  labelled set from real prose in these docs and the same prose put through
  `vectrixdb.quality.degrade`, a simulator of what OCR gets wrong: shape-alike
  glyphs, the rn/m confusion, dropped and inserted spaces, glyph noise. The
  line is defined operationally: 5% noise is a page a person can still read
  and counts as clean, 20% and above destroys retrieval and counts as bad,
  10% is reported but left out of the calibration. The numbers the script
  printed when the threshold was set are quoted beside
  `vectrixdb.quality.DEFAULT_THRESHOLD`, and `calibrate()` produces a
  threshold from a deployment's own labelled set, which is what to use for
  anything that is not English prose.

  `add_document` scores every chunk and stamps it as `_vx_quality`. A
  document whose mean score falls below the threshold warns by default,
  `on_low_quality="reject"` refuses it with `ExtractionQualityError` naming
  the score, and `"allow"` is silent. Warn rather than reject by default,
  because a deployment should see the scores on its own documents before it
  lets them refuse anything. The evidence pack's `lineage.json` counts the
  chunks below the line.

- **A decision can be restated.** `db.reproduce(record)` takes a decision
  record, or the dict a sink stored, and says which chunks that principal
  could reach: exact when the index build and the policy fingerprint on the
  record are the ones the collection holds now, and labelled a re-run when
  either differs, because a re-run over a different index is evidence of a
  different thing. It reports which of the chunks returned then are still
  reachable, which this principal can no longer reach, and which are gone,
  and those are three findings rather than one: a document still there but
  out of reach is a revocation, not a deletion. Pass `snapshot=` to run
  against an export of the build the record names, which is how a
  reproduction stays exact after the index has moved on. This is the
  retention decision the roadmap called the hardest item: reproducing rather
  than explaining means keeping the builds, and the export per build is the
  unit to keep.

  Two things the record needed for this. `result_ids`, which chunks were
  handed over rather than only how many; without it a record could say seven
  documents were returned and not which seven. And every sink can `find()` a
  record by id where its store can be read; the base sink says plainly that
  an insert-only store has nothing to read with.

- **Provenance on every chunk, and on the ingestion record.** Every chunk
  carries `_vx_build`, the build that stored it, and it comes back in the
  metadata a search or a `get()` returns, beside the caller's own keys;
  code that compared that dict for equality against what it wrote sees the
  extra key. It is the build that stored it, so the join runs from a chunk
  to its ingestion record whatever build is current, and the ingestion
  record carries `ids_written` so it runs the other way too. `add_document`
  stamps `_vx_doc_version`, a hash of the text as loaded, so two ingestions
  of the same file can be told from one of a changed file, and the chunking
  configuration, because the same text cut differently is a different set of
  chunks. The ingestion record carries the source, the document id and
  version, and the chunking; a plain `add()` of texts records `None` rather
  than a guess. `db.provenance(ids)` reads it all back, and a deleted chunk
  comes back `present=False` with nothing invented from a later write.

- **The evidence pack.** `db.evidence_pack(directory)` writes six files
  generated from the live objects rather than assembled by hand: what the
  model is, what it answers from, the policy as data, which controls are in
  force and what fails closed, and a summary of the audit trail. Artifacts,
  not a compliance claim, and the README in the directory says so. Two things
  are absent on purpose: the audit records themselves, which say which
  restricted documents exist and who was refused them, and the total withheld
  out of scope, which would confirm documents exist outside a scope somebody
  was refused. A sink that cannot read back contributes a summary that says
  so, and `records=` takes the trail from your store instead.

- **A timing floor, for the one channel a filter cannot close.** A principal
  who may see one document in a thousand sends the over-fetch loop round
  again and a principal who may see most of them does not, so the answer
  arrives later for the first and sooner for the second. No filter closes
  that, because the work really is different. `Vectrix(..., timing_floor=0.25)`
  holds every read until it has taken a whole number of floors: 30 ms returns
  at 250, 260 ms returns at 500. Rounding up to the next multiple rather than
  padding to one deadline is the point, because a single deadline reports the
  true duration of everything that overruns it, which is the whole
  distribution above the floor.

  `Results.time_ms` reports the padded figure, and has to: sleeping and then
  handing back the real duration in a field leaves the channel open while
  looking closed. The decision record keeps the true latency, because the
  host paying for it is entitled to know what it bought. Off by default,
  because it costs a floor on every query whether or not anything was
  withheld.

- **The write path's half of the policy: a value no rule can decide on.** The
  metadata contract caught an absent field. A field that is present and holds
  the wrong shape fails identically and was not caught: a classification rank
  stamped as the string `"high"` is not greater than any clearance and not
  less than one either, so the rule is false for every principal, the
  document is invisible to everybody for ever, and the symptom reads as a
  permissions problem. A schema validator never catches it, because `"high"`
  is a perfectly good string.

  `AtMost` and `AtLeast` need a number, and refuse a boolean, since `True`
  would otherwise compare as level one and pass. `Overlap` and `Excludes`
  need a value or a list of them. `Equals` and `Present` take anything. An
  empty list passes, on the same reasoning that lets a null through: a
  document nobody may see is a thing somebody legitimately writes, while a
  shape the comparison can only ever be false against is a mistake. Absent
  and undecidable are one bug with two spellings, so they are reported
  together, under the same `on_incomplete_document` setting.

  The honest limit is worth stating: the library can say that an ingestion
  stamped something no rule can decide on. It cannot say whether that
  ingestion was entitled to stamp `client_id: "CL-40219"`, because that is a
  question about the service doing the writing, which the host authenticates
  and the library never sees.

- **A live row level security suite, gated.** Everything about
  `SET LOCAL ROLE` that can be checked against a stand-in already was.
  Whether PostgreSQL then refuses the rows is a question only PostgreSQL
  answers, and it is the answer the feature exists for.
  `tests/unit/test_rls_live.py` asks it: a role the policy admits sees its
  own rows, a role no policy names gets the empty set rather than everything,
  the connection is handed back as the original user, the role is given back
  even when the query inside raises, and the application role cannot turn row
  level security off. It runs nightly against a PostgreSQL service container,
  which needs no cloud account and no secret, and skips everywhere else.

- **A test kit for your own policies, shipped rather than kept in ours.**
  `vectrixdb.testing` is three assertions. `assert_visibility` takes a table
  of who should see what and prints the whole table, disagreements marked,
  when it does not hold; every principal needs a row, so adding a document
  without deciding who sees it fails rather than passing by omission.
  `assert_allows` and `assert_denies` are the single-document form, and their
  failures name the rule that decided and print both sides of the comparison,
  because "assert False" says nothing you did not already know;
  `assert_denies(disclosable=...)` asserts which kind of withholding, since
  getting in scope and out of scope the wrong way round is how an ethical wall
  fails while every test still passes. `explain` prints the same reasoning on
  demand.

  `assert_indistinguishable` is the wall assertion: two searches a caller must
  not be able to tell apart, one against a collection holding the walled
  client and one against a collection where that client was never onboarded.
  Both sides are callables so that a raise counts as an observation, which is
  the case people miss. It compares the documents, their order, the count, the
  truncation and degradation signals, and the scores, the last to a tolerance
  rather than exactly. Time it leaves alone, because closing that needs a
  floor in the service layer rather than an assertion.

  Writing it turned up something the library did not know about itself:
  `mode="sparse"` carries a corpus statistic out to the caller. Inverse
  document frequency and average document length count every document in the
  index, including the ones the principal was refused, so the same visible
  document scored 0.1131 with a walled client in the index and 0.1823
  without it. Dense is unaffected, because the vector is fixed at ingestion,
  and so is hybrid, because rrf scores by rank and weighted normalises inside
  the returned set. The default tolerance of 10 percent is set from both
  sides of that: embedding is not bit stable across batches and moved a cosine
  score by as much as 2 percent in measurement, while the sparse gap is 38
  percent. Until it is closed, do not return raw sparse scores to a caller who
  must not distinguish a walled client from an absent one. It is on the
  roadmap and a test pins it.

- **A document the policy could never show anybody is refused at the write.**
  Every rule names a document field and an absent field denies, so a chunk
  written without one is invisible to every principal for ever, and the
  symptom reads as a permissions problem: somebody spends an afternoon in the
  entitlement resolver looking for a bug that is in the ingestion pipeline.
  `policy.required_document_fields` is the contract, and the check runs before
  the write, so a batch with one bad document in it does not land half of
  itself. A field deliberately set to null satisfies it, because absent and
  null are different and only absent denies.

  `on_incomplete_document` is the failure policy for that check: `reject` by
  default, `warn` to write it and say so while backfilling a collection that
  already has such chunks, `allow` for silence. Rejecting by default is the
  choice worth defending: the alternative is a write that succeeds and a read
  that fails somewhere else, later, in somebody else's code.

- **The write path is audited too.** A decision record says whether somebody
  should have seen what they saw; nothing said whether the documents were
  supposed to be in the index at all, which is the other half of the question
  and a different shape, so `IngestionRecord` is a different record rather
  than a flag on the same one. It runs as a service rather than a principal
  and says so, because both land in the same store and an investigator
  filtering on `principal_type` is asking one question or the other.

  The two join on one field: `ingestion_id` is the value a later decision
  record carries as `index_build_id`, so an answer leads to the decision that
  produced it and on to the ingestion that put the documents there. It carries
  the embedding model, without which the vectors mean nothing, and the policy
  fingerprint in force at write time, which is not necessarily the one in
  force at read time. `documents_written` and `documents_skipped` are separate
  because near-duplicate detection turning work away is a normal outcome
  rather than a failure.

  It goes through the same failure policy as a decision, so `DENY` stops a
  write it cannot record. An `add()` that writes nothing mints no build and
  produces no record: an ingestion record describes a build, and there was not
  one. `PostgresAuditSink` gains an `ingestion_events` table under the same
  grants, since a nullable half of the decisions table would be worse than
  two tables.

- **A policied read can run as a database role, so PostgreSQL enforces it.**
  Everything else in this release decides in Python: the policy compiles to a
  filter, the collection applies it, and the right documents come back. What
  that is not is enforcement, because nothing underneath ever says no. Row
  level security says no. `Policy(rules, db_role_key="db_role")` names the
  principal key holding the role, and each read on a PostgreSQL backend runs
  `SET LOCAL ROLE` inside a transaction.

  `SET LOCAL` rather than `SET` is the point: the transaction ending is what
  gives the role back, so the next request on a pooled connection cannot
  inherit it. A role that outlived the request would be worse than none. The
  Aurora backend runs in autocommit, where psycopg2 manages no transaction and
  `SET LOCAL` would silently apply to nothing, so one is opened explicitly
  rather than assumed; that was found by reading how the backend actually
  connects rather than by hoping.

  The role name is validated rather than escaped, because it goes in as an
  identifier and identifiers cannot be bind parameters: anything that is not a
  plain identifier is refused. A backend that cannot assume a role raises
  instead of continuing, since silently not assuming it leaves the caller
  believing the database is enforcing when nothing is. `RLS_SETUP` ships the
  `ENABLE`/`FORCE ROW LEVEL SECURITY` and `CREATE POLICY` as text a DBA runs,
  never something VectrixDB issues, on the same principle as the audit table's
  grants: a connection that can create a policy can drop one.

  What the suite proves is that the right statements are issued, in a
  transaction, in the right order, and that the role is handed back even when
  the read raises. Whether PostgreSQL then refuses the rows is PostgreSQL's,
  and checking that needs a live database with policies a DBA wrote. The
  `fake_postgres` stand-in records transaction control and `SET` statements
  rather than running them, which is what makes the first half checkable at
  all.

- **Translating a policy into SQL is off the plan.** Pushing the policy down
  into a WHERE fragment was the next roadmap item, and it has been dropped
  rather than done. The filter grammar is twenty-two operators
  including geo, regex and the absent-versus-null-versus-empty distinctions
  that took a round of fixes to get right this release; translating that into
  SQL means writing the policy a second time in a second language, where a
  subtle disagreement between the two is a security bug rather than a wrong
  answer. Row level security needs no translation: the rule is written once by
  whoever owns the database and VectrixDB switches role.

- **`revoke()`: change entitlements across many chunks in one call.** The
  operational half of a policy, and what the design's own notes call its
  hardest problem. Entitlements are denormalised onto the chunk, which is what
  makes a query fast and a permission change expensive: one person leaving a
  deal team means every chunk of every document for that client. That was a
  loop over `update_metadata` the caller wrote, which meant the caller also
  wrote the mistakes.

  `unset` removes a field rather than setting it to null, because those are
  different: the filter engine tells an absent field from a null one, and
  under a policy an absent field denies while a null one is a value like any
  other. `set` merges, which is how a deal team moves rather than dissolves.
  The report carries `matched` and `changed` separately, since the gap is
  usually the answer: matched many and changed none means it had already been
  applied, matched none means the filter is wrong, and one combined count
  hides both. An empty `where` is refused rather than read as "everything",
  which is the truthiness family in the one place it would be a mass edit.
  Like `export` it belongs to whoever holds the collection, not to a
  principal, and it moves the `index_build_id` because it changes what the
  index answers.

- **The wheel job runs the README's own first example.** It already installed
  the wheel alone into an empty environment on three operating systems and
  searched, which is the check that catches an undeclared dependency, and it
  had been doing so against a snippet written into the workflow: a copy of the
  README that nothing kept in step. `scripts/readme_smoke.py` extracts and
  runs the README's first fence instead, so the thing a new reader types is
  the thing CI proved works, and the job also asserts both console scripts
  land on the path. An earlier note in this changelog implied nothing
  installed the wheel in CI; that was wrong, and the roadmap entry saying so
  has been corrected rather than acted on twice.

- **A backend has to say where its filter runs, and the answer today is
  "after".** `FilterPushdown` is `ENGINE` or `POST`, declared on every storage
  backend and reported by `Collection.pushdown_mode`. Run in the engine, a
  policy's filter is enforcement; run after candidates come back it returns
  the right documents while leaving the work done proportional to how
  selective the entitlements were, which is measurable from outside.

  Nothing pushes a policy filter down. The local loop fetches from the index
  and decides in Python, and the backend path calls `vector_search` with a
  collection, a vector and a limit and no filter at all.
  `Policy(rules, require_pushdown=True)` therefore refuses everywhere, at open
  rather than at the first search, naming the path that actually decided
  rather than whichever storage object happened to be attached. That is the
  point of it: a deployment that needs engine-side enforcement should be told
  it does not have it rather than assume it from the fact that a policy is
  attached. `RetrievalRecord.pushdown_mode` was always `None` and now carries
  the answer on every row.

  The requirement is the deployment's, not the collection's, so it stays out
  of the fingerprint and out of what a collection remembers: a collection
  written where pushdown was demanded is not a different collection from one
  where it was not. `tests/unit/test_pushdown.py` keeps the declaration and
  the behaviour together, including by forcing the backend path and asserting
  that no filter reaches it, since a test that only loops over the calls a
  populated collection never makes would pass while proving nothing.

- **The id lookups are gated too, and a closed collection answers nothing.**
  `Collection.get`, `get_batch` and `iter_documents` warned rather than
  refused, because the collection's own bookkeeping calls them and refusing
  would have broken it looking after itself. Each is now a gated public
  method over an explicit `_get_raw` / `_get_batch_raw` /
  `_iter_documents_raw`, so the bookkeeping says what it is doing and
  everybody else is checked. A grep for `_raw` is the list of places that
  deliberately see everything, and every one of them is a write path or an
  internal index. Through a gated lookup a denial and a miss are the same
  answer, because telling a caller that an id exists but is not theirs
  confirms the document.

  Separately, a closed collection could still answer a search from the result
  cache. That surfaced only because turning the cache off under a policy made
  one of the documentation examples fail on a handle it had already closed:
  the answer had been coming from cache rather than raising. `close()` marks
  the collection closed and `search` and `keyword_search` refuse, since stale
  data from a handle the caller has finished with is the worse of the two
  failures.

- **Enforcement moved from `Vectrix` down to `Collection`, which is where the
  other door is.** A policy was applied by the easy API, so the REST server and
  everything else built on `VectrixDB` reached a `Collection` directly and saw
  every document the query matched, with the policy sitting unread in the
  collection's metadata. A warning made that loud; this makes it stop. A
  policied collection searched without a principal refuses wherever it is
  reached from, and `PolicyNotEnforcedWarning` now covers only the id lookups
  that are still ungated.

  The move paid for itself. The widening loop already sees every candidate
  before deciding, so the withheld counts come out of that same pass and are
  exact: `SearchResults` carries them, `candidates_exhausted` is no longer a
  hedge, and the separate over-fetching audit path in `Vectrix` is gone along
  with its window constant. `Vectrix` also stopped compiling the policy into a
  filter, because doing it in two layers would have meant a gap in either
  staying invisible while the other covered it. The counts reach a decision
  record through a dict on the throwaway copy an audited search makes, never
  on the results, since the undisclosable one confirms documents exist outside
  a scope the caller was refused.

  Two hazards the move exposed and closed. The search cache is keyed on query,
  filter and limit and **not** on the principal, so it would have served one
  principal's results to another; caching is off while a policy is in force.
  And `keyword_search` feeds the sparse half of a hybrid search, so leaving it
  ungated would have put documents the principal may not see into the fusion
  and out through the answer; it takes a principal now too.

  What cannot be decided refuses rather than answering: `hybrid_search` reads
  the text index directly, and `search_with_acl` and `enterprise_search` are a
  different control from an entitlement policy, so running only one of them
  silently is the hazard. That last pair is what `/search/acl` calls, an
  endpoint that takes `user_principals`, `acl_field` and `default_allow` from
  the request body; on a collection with a policy it now refuses. `Vectrix`
  likewise stops taking the OpenSearch and Azure hybrid shortcuts while a
  policy is in force, since those rank inside the backend where no
  per-candidate decision happens.

- **Every read path on a policied collection is now gated or refuses, and one
  of them was a bypass.** `similar(id)` pulled a document's vector straight
  out of the collection and ran an unfiltered dense search, so a principal
  holding an id could pivot off a document they may not see into its
  neighbours, which are the documents most likely to be about the same client.
  It now checks the principal before it looks the id up, treats a document the
  principal cannot see exactly like one that is not there, and filters what it
  returns. Checking existence first also meant an unknown id skipped the gate
  entirely, so whether it raised was itself an answer to "is that a document?".

  `count()` and `len()` answer for the principal through a view, because the
  total is a fact about documents they may not know exist: a walled analyst
  watching the number move learns that a client they were refused is being
  written to. That costs a pass over the collection where an unpoliced count
  is one SELECT. `export`, `graph_path` and `graph_explain` refuse a principal
  and answer the host. `recall` and `context` refuse either way, because they
  search through conversation memory's own path which the policy does not
  reach, so no principal makes them safe.

  The rule those follow is three tiers rather than one, and the tiers are not
  the same problem. Content a policy can decide refuses without a principal,
  which is the headline property. Content it cannot decide refuses whenever a
  policy exists. Administrative work belongs to the host, because
  `as_principal` returns a copy and nothing leads back to the original, so a
  principal only ever holds a view. An earlier version of this locked the host
  out of counting its own collection, which bought nothing.

- **A policied collection searched through `Collection` says so.** Policies
  are enforced by `Vectrix`, so the REST server and anything else built on
  `VectrixDB` sees every document the query matches with the policy unread in
  the collection's metadata. That gap is real and on the roadmap; until it
  closes, `PolicyNotEnforcedWarning` makes it loud, because a collection that
  looks protected and is not is worse than one that plainly is not.

- **A decision record you can reach from the answer, and that says which
  index answered.** `Results.decision_id` is the id of the record that search
  wrote, so "the assistant told me this" ties to one row instead of being
  matched by wall-clock time. It is opaque and safe to surface: it names a
  decision, never a document, and an unaudited search carries `None` rather
  than an id pointing at nothing.

  `index_build_id` is no longer always `None`. Every write mints one and
  stamps it on the collection, whether or not anything is auditing, so the
  field is populated the day auditing is switched on rather than from whenever
  somebody remembered to. It is read per search rather than cached on the
  view, because a view made before a write would otherwise keep naming the
  build that preceded it, which is the one answer the field must not give. A
  collection written before this existed has none, which reads as `None`
  rather than as a guess.

- **An append-only PostgreSQL sink for decision records.** `PostgresAuditSink`
  writes one row per decision, and the point of it is the grants rather than
  the SQL: a separate database from the index, and a role holding `INSERT` and
  neither `UPDATE` nor `DELETE`, so the thing being audited cannot rewrite its
  own history. The library never issues DDL, because a role that can create
  the table can drop it; `SCHEMA` and `GRANTS` are text a DBA runs, and the
  missing-table error prints both.

  It verifies the table at construction rather than on the first search, since
  a missing audit table found on the first policied query is found in
  production, under a failure policy, at the worst possible moment. A table
  missing a column names the column and says to add it rather than recreate
  the table, which would take the history with it. The insert is `ON CONFLICT
  (decision_id) DO NOTHING`, so draining a spool whose backlog partly landed
  is idempotent: a duplicate is not a second decision. A connection that dies
  is dropped so the next write reconnects, rather than poisoning the sink for
  the life of the process.

  It has a `connection=` seam, which the storage backends notably lack, so its
  real SQL runs in CI against the existing `fake_postgres` stand-in: the
  shipped DDL executes verbatim, JSONB columns round-trip, and a policied
  search lands a row with both withheld counts. No database, no Docker. One
  test asserts the record's fields and the table's columns are the same set,
  because a field added to one and not the other is a field the audit silently
  stops keeping.

  `fake_postgres` gained one thing to make that possible: its `CREATE TABLE`
  pattern now tolerates a trailing semicolon. Neither storage backend sends
  one, so it had never come up.

- **A decision record for every policied read, and somewhere to put it.**
  `Vectrix(..., on_retrieval=sink)` writes one `RetrievalRecord` per policied
  search: who asked, under which policy fingerprint, what came back, what was
  withheld and in which of the two senses, how stale their entitlements were,
  and how long it took. The library builds the record and never chooses a
  destination, because these are evidence and their retention usually outlives
  the data they describe.

  `AuditSink` ships with `JSONLSink` and `MemorySink`, and refuses to be built
  carelessly. `on_failure` has no default: whether a failed audit write should
  deny the answer (`DENY`) or buffer and continue (`Spool(path)`) is a decision
  about the business rather than the code. `query_key` is required because the
  record stores an HMAC of the query rather than the query, and a bare hash
  over a low-entropy query space is enumerable by whoever holds the log, which
  would make it a correlation key that reads like a redaction. A sink only
  treats the failures it declares in `TRANSIENT` as an outage, so a bug in a
  sink surfaces as a bug rather than being spooled and forgotten as an
  infrastructure blip.

  The principal snapshot is stored inline rather than as a pointer, because a
  pointer into an entitlement store with ninety-day retention is worthless in
  an audit kept seven years, and `principal_snapshot_age_ms` is the field an
  access decision is challenged on. Without a `snapshot_taken_at` from the
  host it is `None` rather than zero, since zero would read as "resolved this
  instant". `candidates_exhausted` says whether the withheld counts are exact
  or lower bounds: a count you cannot testify to should not look like one you
  can.

  Getting the withheld counts means seeing the documents the policy rejected,
  which a filter applied by the engine throws away, so a search with a sink
  attached fetches a wider window with the caller's filter alone and evaluates
  the policy in Python. That is what the local backend does internally anyway;
  on a backend that pushes filters into SQL it trades the pushdown for the
  counts. A test asserts directly that both paths return the same documents.

  `Policy.classify` is the one implementation of the in-scope and
  out-of-scope split, used by both the audit path and `Policy.partition`, with
  a replay holding it down: under a single "suppressed" count both test suites
  go red. `Policy` also gained an optional `version` label, carried on every
  record beside the fingerprint; renaming a policy deliberately does not move
  the fingerprint, because a rename is not a change.

- **The README covers conversation memory and the MCP server.** Neither
  appeared in it: `remember`, `recall`, `feedback` and `context` were absent
  from 757 lines, and so was `vectrixdb-mcp`, which a plain install puts on
  the path. A reader of the front door learned about the dashboard, the REST
  API, the CLI, the storage backends and GraphRAG, and never learned the
  library can be an agent's memory. Both sections quote measured numbers and
  point at the existing pages, and the Python example runs in CI with the
  rest of the README.

- **Lakebase and Aurora PostgreSQL can be run against the real service.**
  They had no live entry in the contract suite at all, so there was no way to
  reach them with credentials in hand. All six cloud backends now answer to
  `VECTRIXDB_LIVE_BACKENDS`.
- `rank-bm25` is a dev dependency, so the test that verifies this BM25
  implementation against the reference actually runs. It skipped everywhere,
  CI included, while the roadmap claimed the check was being made.

- **Every documentation page with a Python example now runs in CI.** The
  runner covered eight pages of eighteen and skipped any fence containing one
  of a long list of substrings, which is how the tutorial's fifth step and
  the README's model-download example shipped broken past a green build. All
  eighteen pages run, the skip list is down to what genuinely cannot execute
  here, and a test fails if a new page with examples is not added to it.
- `SyncResult.filtered` says whether an incremental sync could apply its
  cutoff, or copied everything because the source carries no timestamps.
- **Nine filter operators gained tests.** `docs/reference/filters.md` claims
  every operator on it is exercised against a real collection; ten were not,
  and every filter bug this release fixed was among them. A test enforces the
  claim now rather than the page merely asserting it.


- `VectrixDB.failed_collections` names any registered collection that would
  not open, and why. Empty in the ordinary case.
- `InvalidCollectionName`, `CollectionLoadWarning` and
  `SparseModelUnavailableWarning` are exported.
- **Thirteen regression tests, each with a replay.** `tests/unit/
  test_audit_fix_first.py` covers every fix above, and each has a plugin
  under `tests/replays` that restores the old behaviour; all thirteen make
  their tests fail against it, which is what makes them load-bearing.

- `Vectrix.get_one(id)` returns one document or `None`. `get()` takes one id
  or many and always answers with a list, so a miss is `[]` and a hit has to
  be unwrapped; that is the right shape for a batch and the wrong one for a
  single lookup.

- **Every storage backend now passes the contract suite on every push.** The
  four that had never been executed by any test, Cosmos DB, Delta Lake,
  Lakebase and Aurora PostgreSQL, run against in-memory stand-ins for their
  drivers: `tests/unit/fake_cosmos.py`, `fake_databricks.py` and
  `fake_postgres.py`, which serves both PostgreSQL backends. No cloud
  account is needed. All four failed on first contact, and the nine bugs
  that surfaced are listed under Fixed. Live runs against the real services
  remain gated behind `VECTRIXDB_LIVE_BACKENDS`.

- **A collection can be written past RAM.** `Vectrix(..., shard_size=N)` and
  `create_collection(..., shard_size=N)` keep one mutable head shard of N
  vectors and seal the rest to disk as memory-mapped views, so only the head
  is ever held in memory. The head is the same file an unsharded collection
  writes and sealed shards sit beside it, so an existing collection reopens
  sharded without conversion and a collection that never fills a shard is
  unchanged on disk. Search asks every shard and merges by distance; a
  re-added id is shadowed by the copy in the head, since a sealed shard
  cannot be edited; `rebuild_index()` doubles as compaction, dropping
  deletions and shadowed copies and packing the shards again. Default is
  `None`, which is the single in-memory index every collection has today.
  Measured with `scripts/shard_bench.py`: at matched recall ten shards cost
  about 1.4 times one index at the collection's default search width, and
  the numbers are on the docs site under Explanation. The design's original claim that fan-out would be nearly free
  was wrong and the page says so.

- **Graph quality block.**
  - *Incremental community detection*: an add re-detects communities only in
    the connected components it touched and keeps every other community
    with its summary; under an LLM extractor that is one summary per changed
    community instead of one per community. New ids carry a generation
    number so they never collide. `GraphRAGConfig(incremental_communities=
    False)` restores full re-detection. Stats report recomputed and kept.
  - *Entity types with schemas*: `EntitySchema(name, threshold,
    allow_subset, merge_across_types)` per type, with defaults that let
    people merge on initials and subsets while concepts, locations, events
    and products merge only on near-exact names, and that keep "Apple" the
    company and "Apple" the fruit apart. Without schemas the graph behaves
    as before.
  - *Relationship supersession*: `valid_from`, `valid_to` and
    `superseded_by` on every relationship, `add_relationship(supersedes=)`
    and `supersede_relationship()`. A superseded edge leaves traversal,
    search and community detection but stays for the record; the SQLite
    graph store gains the columns on open, so old databases keep working.
  - *`graph_path()` and `graph_explain()`* on `Vectrix`: the shortest chain
    of relationships between two entities, and everything the graph knows
    about one entity or a pair, including superseded facts, shared
    neighbours and communities with summaries. Names resolve through the
    graph's own matching.
  - *Dashboard*: the graph tab colours nodes by community, draws superseded
    relationships dashed, and the API returns community and validity fields.

- **Examples gallery and governance.** `examples/` holds three scripts under
  a screen each, RAG in 20 lines, chat memory and GraphRAG over a book, all
  executed by the test suite offline, with matching notebooks generated by
  `scripts/make_notebooks.py` and checked for drift. Governance files:
  issue and pull request templates, `CODEOWNERS`, `CODE_OF_CONDUCT.md`,
  `GOVERNANCE.md` with the release cadence and support policy, and
  `ROADMAP.md` with the open items and what is deliberately not planned.

- **Developer experience block.**
  - *CLI*: `vectrixdb ingest` (files or directories, every `add_document`
    option), `vectrixdb query` (table or `--json`, `--parents`, `--explain`)
    and `vectrixdb stats`, beside the existing `serve`.
  - *LangChain adapter*: `vectrixdb.integrations.langchain.VectrixVectorStore`,
    a `VectorStore` over a collection using VectrixDB's bundled model or any
    LangChain `Embeddings`; `search_kwargs` pass every `search()` keyword
    through. Needs `langchain-core`.
  - *LlamaIndex adapter*: `vectrixdb.integrations.llamaindex.VectrixLlamaStore`,
    a `BasePydanticVectorStore` that takes LlamaIndex's vectors and creates
    the collection at their dimension. Needs `llama-index-core`.
  - *OpenAI-compatible embeddings*: `OpenAIEmbedder(model, base_url=...)`
    for api.openai.com, Ollama, vLLM, LM Studio and Text Embeddings
    Inference through the `openai` package, and
    `Vectrix(dense_model="openai:<model>")` as the one-keyword form.
  - *Plugins*: entry-point groups `vectrixdb.embedders`, `.rerankers`,
    `.extractors` and `.storage`; `dense_model="plugin:<name>"` and
    `StorageConfig(backend="plugin:<name>")` load them, `vectrixdb.plugins`
    lists and registers them.
  - *Timing hooks*: `Vectrix(on_search=fn, on_add=fn)` receive `{op, ms,
    count, mode, collection}` after every call; a failing hook is logged,
    never raised.
  - *Release automation*: `scripts/release.py --check` fails CI when the
    unreleased changelog section is empty, and `scripts/release.py X.Y.Z`
    stamps the version and date and prints the git steps without running
    them. A CI job requires a changelog line on any pull request that
    touches the package.
  - *Wheel verification*: CI builds the wheel, asserts it is pure Python
    (`py3-none-any`), and installs it alone on Linux, macOS and Windows to
    run a search. A conda-forge recipe is in `conda/recipe/`.
  - *Dependency audit*: `pip-audit` over the package and every runtime
    extra, nightly, so an upstream vulnerability is seen the next morning
    without blocking unrelated pull requests.

- **Memory block: memory is finite and facts conflict.**
  - *Time to live*: `ConversationMemory(ttl_days={"turn": 30})` and
    `remember(ttl_days=...)` stamp an expiry; expired memories are invisible
    to recall, context and turns at once, and `expire()` deletes them.
  - *`forget()`*: delete by ids, or by kind, session and age (`older_than`
    days or a datetime), or only the memories a correction or contradiction
    has already superseded. Returns the count.
  - *`consolidate(session, summarize)`*: hands a session's old turns (all
    but the last `keep_recent`, optionally older than a date) to your
    callable in batches, stores what comes back as facts dated at the newest
    turn they summarise and linked to the turn ids, then drops the turns.
    `delete=False` is a dry run, `pinned=True` pins the facts. Any LLM
    call fits; nothing is bundled.
  - *Contradiction on write*: `ConversationMemory(judge=fn)` runs
    `fn(new_text, [nearest stored facts])` on every new fact and marks the
    facts it says are contradicted as superseded by the new one, the same
    link `feedback("corrected")` makes. `check=` overrides per call.
  - *Memory benchmarks*: `scripts/memory_bench.py locomo | longmemeval |
    longmemeval_s` downloads the dataset, stores each conversation as
    dated turns, runs every question through `context()` under a token
    budget and reports evidence recall against plain search. Numbers are on
    the conversation-memory page.

- **Azure AI Search storage backend.** `VectrixDB.with_azure_search(endpoint,
  key=None, index_prefix="vectrix", semantic=False)` or
  `StorageConfig(backend=StorageBackend.AZURE_SEARCH, ...)`. One Azure index
  per collection with a vector field on the service's HNSW, a BM25 text
  field and a JSON metadata field; dense mode is a vector query, hybrid is
  the service's own text-plus-vector fusion, and `semantic=True` adds Azure's
  semantic ranker. Keyless auth through `DefaultAzureCredential` when no key
  is given. Ids of any shape round-trip through a URL-safe key encoding. It
  passes the storage contract suite over an in-memory stand-in for the SDK
  clients (`tests/unit/fake_azure_search.py`) and, with
  `VECTRIXDB_LIVE_BACKENDS=azure_search`, against a real service. Extra:
  `pip install vectrixdb[azure]`.

- **Ingestion block: where RAG quality is decided.** New module
  `vectrixdb.ingest`, wired into `Vectrix`:
  - *Chunking strategies*: `chunk(text, "recursive" | "sentence" | "semantic"
    | "markdown", size, overlap)` returns chunks that know their offsets,
    page and heading. Sentence never cuts mid-sentence; semantic embeds each
    sentence and splits at the sharpest topic changes; markdown makes one
    chunk per heading section. Tested as properties: substring at offsets,
    in order, never over size.
  - *Document loaders*: `load_document(path)` reads PDF (pypdf or PyMuPDF),
    DOCX (python-docx), HTML (standard library), Markdown and text into a
    `LoadedDocument` with page and heading positions, so every chunk carries
    `page` and `heading` metadata. `with_page_markers()` feeds the existing
    `build_tree_from_pdf`.
  - *`Vectrix.add_document(source, chunk=, chunk_size=, overlap=,
    parent_size=, dedupe=, metadata=)`* loads, chunks and adds in one call;
    `delete_document(doc_id)` removes what it added.
  - *Parent-child retrieval*: with `parent_size`, chunks are grouped into
    sections stored beside the collection, and `search(parents=True)`
    returns the section a matching chunk belongs to, once per section, at
    its best chunk's score, with the matching chunk under
    `metadata["_vx_child"]`.
  - *Near-duplicate detection*: `add(dedupe=0.9)` skips texts whose MinHash
    similarity over word 3-shingles to a stored text, or to an earlier text
    in the same call, is at or above the value. Signatures persist in one
    JSON file beside the collection; `last_add_report` lists what was skipped
    and what it duplicated.
  - *Embedding model versioning*: the model a collection was built with is
    recorded in the collection; opening it with another model raises
    `ModelMismatchWarning`, and `reembed()` re-embeds every text with the
    current model, rebuilds the index and updates the record. A dimension
    change is refused with the instruction to create a new collection.
  - *Embedding cache*: vectors keyed on content hash and model in
    `_embed_cache.db` beside the collection, on by default for collections
    on disk, so re-adding unchanged text never reaches the model.
    `Vectrix(embedding_cache=False)` turns it off.
- `Collection.get_meta()`, `set_meta()` and `iter_documents()`: the
  collection's own key/value table, unused since it was created, now holds
  the embedding model; iteration yields every stored (id, text, metadata).

- **Trust block: measured, reproducible, published.**
  - *Comparison benchmark* (`scripts/benchmark_compare.py`): VectrixDB,
    Chroma, LanceDB and Qdrant on the same vectors from the bundled model,
    same machine, one thread. Ingest time, recall@10 against exhaustive
    search, p50 and p95 latency and QPS, as a Markdown table, a JSON file
    and a chart. Engines that are not installed are skipped and named.
  - *BEIR evaluation* (`scripts/beir_eval.py`): any BEIR dataset by name,
    downloaded on first use and cached, indexed with the bundled model,
    scored with nDCG@10 and recall@100 computed the way trec_eval computes
    them. No dependency on the beir package or torch. Modes are dense, hybrid
    and hybrid with the cross-encoder on the top candidates.
  - *Docs site*: a "Why VectrixDB" page that says who it is for and who it is
    not for, a benchmarks page whose tables are pasted from the two scripts
    and nothing else, and a GitHub Pages workflow that builds the site with
    MkDocs on every push to main and fails a pull request that breaks a
    page. Pages needs to be set to "GitHub Actions" once in the repository
    settings.
- **`search(rerank=False)`** turns the cross-encoder off in hybrid, ultimate
  and graph modes, which reranked three times the limit unconditionally. The
  reranker costs about 170 ms per long document on a CPU, so a hybrid search
  with `limit=100` over 1,500-character documents took 51 seconds; with the
  switch it takes under three. The default is unchanged.
- **`DenseEmbedder.embed(pad_to_longest=True)`** exposes the bucketed padding
  path on the embedder for callers who want throughput over bit-identical
  vectors, such as the benchmark corpus.

- **Testing block.** The suite grew from 683 to over 750 tests and every
  addition is designed to fail against the code it guards:
  - *Storage contract suite* (`tests/unit/test_storage_contract.py`): one
    parametrised set of behaviours every backend must satisfy, run live
    against memory and SQLite, instantiation-only for the cloud backends
    unless their credentials are in the environment.
  - *Property tests* with Hypothesis for the filter grammar, quantisation,
    entity resolution and geo matching; *fuzzing* of the filter parser and
    the REST surface (a bad filter may only raise the documented errors, a bad
    request may never be a 500).
  - *Crash durability*: a subprocess is killed mid-add and the collection must
    reopen with every committed row and a loadable index.
  - *Concurrency stress*: threads and spawned processes reading while another
    writes, per backend.
  - *Data compatibility*: a checked-in collection written by 2.1.7
    (`tests/fixtures/legacy_2_1_7`) opens with 2.2, compacts its JSON vectors
    and, for the common unsaved case, recovers search with `rebuild_index()`.
  - *Retrieval baselines*: dense and hybrid MRR and recall@1 on the fixture
    question set are recorded in `tests/fixtures/recall_baseline.json`; a drop
    beyond 0.02 fails the change. This caught the padding regression below on
    its first day.
  - *Golden graph*: the entity and edge set the extractor produces for a fixed
    corpus, so a change to extraction or resolution is a visible diff.
  - *Docs as tests*: every Python fence in the README and the docs runs in
    CI, skipping only the ones that need a download or a cloud service.
  - *Cross-platform*: spaces, unicode and 260-plus-character paths, plus
    Python 3.14, the free-threaded build and linux/arm64 in the CI matrix.
  - *Ratchets*: `scripts/coverage_ratchet.py` fails a drop below
    `.coverage-baseline`, beside the existing mypy ratchet.
  - *Nightly*: the suite three times for flake detection, the `perf` marker,
    and mutation testing with mutmut on the core modules weekly.
  - *Replays* (`tests/replays/`): each regression test ships with a plugin
    that restores the old behaviour, and `scripts/replay.py` proves the test
    goes red under it. `CONTRIBUTING.md` makes this the rule.
- **`tokenizer.encode_batch(pad_to_longest=True)`** pads a batch to its
  longest member in buckets of 32 instead of to `max_length`. It is opt-in
  because the bundled INT8 dense model is padding-sensitive: on the fixture
  set, dense MRR is 0.955 padded to 512, 0.945 at 128 and 0.929 at 32, and
  vectors users have already stored were computed at 512. A fp32 or
  statically quantised re-export would make it the default and cut a batch
  of 200 short texts from 34 s to 0.65 s.

- **`search(explain=True)`** puts the parts of each score on
  `Result.explain`: dense similarity, BM25, both reciprocal ranks, the fused
  score, ColBERT, the reranker's score and any graph boost. What you cannot
  see you cannot tune.
- **Fusion control.** `search(fusion="rrf" | "weighted", alpha=...)` chooses
  rank fusion or min-max weighted scores and sets the dense share; hybrid,
  ultimate and graph modes all honour it.
- **Query expansion and HyDE.** `search(expand=fn)` searches every phrasing
  `fn` returns and fuses the runs by rank; `search(hyde=fn)` embeds the
  hypothetical answer `fn` writes instead of the question. Both take any LLM
  call and bundle nothing.
- **Field boosts for keyword search.** `Vectrix(text_boosts={"title": 2.0})`
  weights BM25 hits per metadata field, stored with the collection and applied
  on every open.
- **Snapshots.** `db.export("notes.zip")` packs index, stores and graph with a
  manifest; `Vectrix.import_snapshot(zip, path=..., name=...)` restores it with
  the same mode and models, and refuses to overwrite.
- **`rebuild_index()`** rebuilds the ANN index from the live vectors, dropping
  tombstones, without blocking readers: the build happens outside the lock and
  only the swap is locked.
- **Read-only, memory-mapped opens.** `Vectrix(..., readonly=True)` maps the
  index instead of loading it, so opening is instant and a collection larger
  than RAM can be searched; writes raise.
- **`AsyncVectrix`** (`vectrixdb.aio`) runs every blocking call off the event
  loop, sharing one thread-safe `Vectrix` underneath. `async with` opens and
  closes it.
- **`add_many()`** streams any iterable, a generator included, in batches.
- **The filter grammar is documented** in `docs/reference/filters.md`, and
  every operator on that page is exercised on a real collection by a test.
- **`scripts/compare_modes.py`** measures dense, hybrid with and without
  its reranker, and ultimate on a small hand-written question set and writes
  the numbers into the search modes page, so the claim that a mode earns its
  cost is measured.
- **`import vectrixdb` costs 4 ms, down from 585 ms warm and 1.2 s cold.**
  Every public name now resolves on first access (PEP 562), so importing the
  package no longer pulls in numpy, usearch, asyncio, every storage backend and
  the whole GraphRAG tree before a single call. `from vectrixdb import Vectrix`
  is about 400 ms on this laptop, which is the honest "time to first call", and
  it has grown with the package: it read 163 ms when this was written. `core.database` also
  stopped importing GraphRAG eagerly. `scripts/import_time.py` measures both
  and CI fails the first if it exceeds 300 ms. Type checkers still see the
  real symbols through a `TYPE_CHECKING` block.
- **Nothing downloads unless asked.** `VECTRIXDB_OFFLINE=1` refuses every
  download, explicit ones included. Implicit ones, a model fetched because
  first use found it missing, a spaCy model pulled while an extractor was
  being built, are off unless `VECTRIXDB_AUTO_DOWNLOAD=1`; a first use that
  would need one raises and names the exact command instead. The explicit
  paths (`vectrixdb download-models`, `download_models()`) need no flag. See
  `docs/how-to/offline.md`.
- **Downloads are checksummed.** `vectrixdb/models/checksums.json` records
  SHA-256 per file for every model set; a mismatch after download raises
  `ModelDownloadError` naming the file, and a model with no entry is accepted
  with a warning that it was not verified. `scripts/model_checksums.py` writes
  and verifies the manifest. `scripts/check_wheel_size.py` keeps the wheel,
  which bundles the English models on purpose, under PyPI's 100 MB limit, and
  CI runs it.
- **`Vectrix` is a real context manager.** `close()` saves, releases the
  SQLite connections and the graph pipeline, is safe to call twice, and any
  use afterwards raises `ConfigurationError` rather than failing somewhere
  inside the collection. Nothing relies on `__del__`.
- **numpy in, numpy out.** `db.embed(texts)` returns the `(n, dimension)`
  float32 matrix the collection stores; `db.add(texts, vectors=...)` accepts
  precomputed vectors. `Result` and `Results` gain `to_dict()` / `from_dict()`
  and pickle cleanly; `Results.to_pandas()` and `to_polars()` are one call,
  raising `DependencyError` naming the package when it is missing.
- **Notebook reprs and progress.** `Vectrix`, `Results` and `Result` render as
  HTML in notebooks, with text escaped; `add(progress=True)` shows a tqdm bar
  when tqdm is installed, and on by itself from 200 texts.
- **An explicit public surface.** Every public module declares `__all__`, and
  `tests/unit/public_api.txt` records what `vectrixdb` exports; a change in
  either direction fails a test with the diff, so the API grows or shrinks
  deliberately.
- **Concurrency is documented**, in `docs/explanation/concurrency.md`, and
  exercised: eight threads adding and searching on one `Vectrix` at once.
- **Memory per vector is measured**, in `docs/explanation/memory.md`,
  regenerated by `scripts/measure_memory.py` so the figures are never typed
  by hand: 1,684 bytes per vector in the ANN index, 384 / 48 / 8 bytes under
  scalar, binary and product quantisation.
- **Python 3.14** in the CI matrix, plus the free-threaded build as an
  advisory job until numpy, onnxruntime and usearch all ship wheels for it.
- **`ProductQuantizer.fit` runs in seconds instead of minutes.** Fitting 500
  vectors took 96 s: k-means++ seeding recomputed distances to every centroid
  at every step (O(n k² d)), assignment allocated an (n, k, d) tensor, and the
  centroid update looped over samples in Python. Seeding keeps a running
  minimum, assignment is one matrix product, and the update is a scatter-add.
- **Token budgets on search.** `search(token_budget=N)` stops returning
  results once their text would cost more than `N` tokens. The top result
  always ships, and `Results.truncated`, `cut_count` and `token_estimate` say
  what was cut and what the list costs, so a shortened list can never pass for
  a complete one. Without a budget the estimate is still reported. Tokens are
  estimated at four characters each unless `Vectrix(token_counter=...)` is
  given, so nothing downloads a tokenizer.
- **`score_gap`** drops results below a fraction of the top score, so a
  specific question returns one strong answer instead of a padded list. The
  useful value depends on the score scale: cosine from the bundled models sits
  in a narrow band, so 0.9 is typical, while BM25 or graph scores want 0.2.
- **Conversation memory** (`vectrixdb.memory`, reached as `db.memory`):
  `remember()` stores turns and facts with session, role and turn numbering
  that survives a restart; `recall()` blends relevance with a recency
  half-life and past feedback, cut to a budget; `feedback()` grades a memory
  `useful`, `dead_end` or `corrected`, and a correction supersedes the old
  memory without deleting it; `context()` assembles pinned facts, the latest
  turns and relevant memories under one budget as ready-to-inject text.

  Relevance is normalised across the candidates before blending. On absolute
  cosine a fresh but irrelevant turn beat a relevant old one, because the
  bundled models score an unrelated sentence around 0.76 and the right one
  around 0.85; a multiplicative recency factor swamped that gap. Every
  recalled result explains itself in `metadata["_vx_scoring"]`.

  The recency, feedback and budget ideas follow Graphify (Apache 2.0), whose
  `reflect.py` and query renderer solve the same problems for code.
- **An MCP server** (`vectrixdb-mcp`, `vectrixdb mcp`, or `build_server(db)`),
  behind the new `mcp` extra, exposing `search`, `recall`, `remember`,
  `feedback` and `context` as tools. Every tool takes a token budget and every
  answer starts with a line saying whether it was cut.

- **`tier="graph"` now builds a knowledge graph.** It documented itself as
  "Ultimate + Knowledge Graph", configured three models, and then ran ultimate
  search: `easy.py` contained no reference to `GraphRAGPipeline` at all, so
  graph mode silently returned dense similarity scores over a graph that was
  never built. `Vectrix.add()` now extracts into a pipeline, `search(mode="graph")`
  uses it to reorder results, and `db.graph` exposes `get_entity`,
  `get_neighbors` and `get_subgraph`. Extraction defaults to the NLP extractor
  rather than REBEL, so graph mode works offline like the rest of the package;
  pass `graphrag_config=` to choose otherwise.
- **Entity resolution that understands names** (`graphrag/graph/resolution.py`).
  Matching compared names with `difflib.SequenceMatcher` against a 0.85
  threshold, which is the wrong question for names: "Curie" scores 0.625 against
  "Marie Curie" and "M. Curie" 0.737, so one person became three nodes and every
  community and centrality score was computed over a graph that believed in
  three people. Resolution now weighs token structure, with token-subset and
  initial-form rules, Jaro-Winkler implemented in-package so behaviour does not
  depend on whether rapidfuzz is installed, and legal-suffix stripping so "Acme"
  and "Acme Corporation" are one company.

  The guards matter more than the rules, because a false merge fuses two real
  entities and nothing downstream can tell. Numeric tokens must agree, so
  "Phase 1" and "Phase 2" never merge; low-information labels are refused; a
  single shared token must be the head noun, so "Paris" does not fold into
  "Paris Agreement"; and subset matching is directional, so a bare "Curie" can
  fold into "Marie Curie" while "Pierre Curie" arriving later stays separate.
- **`config.entity_similarity_threshold` is finally read.** It was declared with
  a default of 0.85 and never used anywhere; the graph used its own constant.
- **Confidence labels on relationships** (`EXTRACTED` / `INFERRED` /
  `AMBIGUOUS`). The regex fallback links two names because they appeared near
  each other; the LLM and REBEL paths link them because the text said so. Both
  produced an identical `RELATED_TO` edge, so retrieval weighted a guess exactly
  like a citation. Co-occurrence edges are now `AMBIGUOUS`, model-read relations
  `EXTRACTED`, and merging two sightings keeps the better-supported label.

  Entity resolution and confidence labelling follow Graphify (Apache 2.0), which
  solves both problems for code symbols. The thresholds here are retuned for
  prose.


- **Typing marker (`py.typed`).** The package ships ~830 annotated signatures
  that downstream type checkers were required to ignore, because PEP 561 needs
  this marker. Type information is now visible to users. A CI job asserts the
  marker is present in the built wheel.
- **A public exception hierarchy** rooted at `VectrixError`, so callers can
  insulate themselves from the library with a single `except`. See
  `vectrixdb.exceptions`.
- **CI that runs before release.** Lint, type check and the test suite across
  Python 3.9 to 3.13 on Linux, plus 3.13 on macOS and Windows. The publish
  workflow now requires it to pass, and verifies the release tag matches the
  built version.
- Tests for the parts that carried the most risk and the least coverage: HNSW
  recall against brute-force ground truth, quantization accuracy bounds via
  Hypothesis, storage failure semantics, SQLite durability across reopen,
  concurrent writers, and community-hierarchy persistence across a restart.

### Security

- **The audit before release.** Each with a test:
  - `VECTRIXDB_OPEN_READS=0` makes every read ask for the full or the
    read-only key, the live feed at `/ws` included, on a server with a key
    and no sign-in. Reads stay open unless it is set, as they always were;
    and the read-only key can now run a search, the one POST that is a read.
  - A collection-scoped key's path was percent-decoded twice, so a key for
    `handbook%41` reached `handbookA`. The path is decoded once.
  - A zip entry named `../x` or `/x` in a downloaded model, a cached model
    archive or an imported snapshot was written where it pointed. Every
    destination is checked before anything is extracted, and a snapshot's
    manifest name is held to the rules a collection name is.
  - The sparse index was a pickle, so a snapshot from elsewhere could run
    code on load. It is an `.npz` read with `allow_pickle=False`; an existing
    pickle is read once and replaced on the next save.
  - PostgreSQL and Aurora statements quoted the schema and collection name
    by hand or not at all, so a name with a quote or a semicolon in it ended
    the statement. Every identifier goes through one quoting function.
  - The MCP `forget` tool with no arguments deleted every turn of every
    session; it now needs `all_sessions=true` for that, and `forget(ids=...)`
    deletes only what the memory tools wrote.
  - The Azure dashboard backend puts `UPSTREAM_KEY` on every call it forwards,
    so a page on another site the operator had open could write with it, and
    could open its WebSocket, which CORS does not cover. A write or a socket
    whose `Origin` is another site is refused. The Azure ingest function app's
    own routes, a collection's delete among them, were open to anyone with
    the address: the server's key is asked for, in `api-key` or as a Bearer
    token, as the library asks for it.

- **The dashboard runs no inline script, and its policy refuses all of it.**
  Its 212 inline handlers became `data-on-<event>` attributes, each a JSON
  list of calls that one listener per kind of event reads, and the listener
  calls only functions named in a fixed list, so markup that reached the page
  some other way cannot call `eval` or `fetch` with it. The theme and single
  sign-on start-up scripts moved to files of their own, and the brand arrives
  as a JSON data block with `<` escaped, so no name can end it. `script-src`
  is the server and the chart library's host, without `'unsafe-inline'`.
  Tests fail on any handler attribute, any inline script, a control that
  calls a function not on the list, a name on the list that is not a
  function, and a local variable that hides the helper that writes a
  control, which is how the Evaluate page stopped drawing while this was
  built.
- **The chart library is pinned by its hash.** The one script from a CDN,
  cytoscape from cdnjs, carries its SHA-512 in `integrity`, checked against
  the CDN's own listing and the file itself, so a copy changed there is
  refused.
- **Any-origin CORS stops the start while sign-in is on.**
  `VECTRIXDB_CORS_ORIGINS=*` would let any site call the API with a
  signed-in person's cookie; the server refuses to start and says to name
  the origins.
- **Every reply carries security headers.** The dashboard has a
  Content-Security-Policy that allows its own files, its fonts and its chart
  library and nothing else, and may not be framed unless
  `VECTRIXDB_FRAME_ANCESTORS` names a portal; every other reply may do nothing
  at all. HSTS over https, `nosniff`, `Referrer-Policy: no-referrer`, a
  Permissions-Policy, `Cross-Origin-Opener-Policy`, `Cache-Control: no-store`
  on the API and sign-in replies, and no `Server` header.
- **A collection or document name could run script in the dashboard.** Six
  buttons put a name inside a quoted script string in an inline handler, and
  three links did the same with it address-encoded. Escaping kept the name
  inside its attribute but not inside the string, because the browser turns
  `&#39;` back into `'` before a handler runs: a collection named
  `x');alert(1);('`, which is a legal name, ran script in the session of
  whoever clicked Rebuild, Delete or its card, and a file uploaded under such a
  name did the same from Delete. Every value now goes into a handler as a JSON
  literal, a test fails if one does not, and a collection with that name was
  made and clicked on a running server to check.
- **Session cookies only this host can set.** Over https they are
  `__Host-vx_sid` and `__Host-vx_csrf`, so a neighbouring subdomain can neither
  plant nor replace them, and `SameSite=Strict` unless single sign-on needs
  `Lax`; `VECTRIXDB_COOKIE_SAMESITE` decides otherwise. Everybody signs in
  once more after upgrading from an earlier build of this release.
- **A collection name was used as a filesystem path with nothing checked.**
  The name becomes a directory under the database path, so a name containing
  `..` or a separator wrote the collection's files outside the directory the
  caller designated, and `POST /api/v1/collections` accepted one over the
  wire and answered 200. Names are validated now: separators, parent
  references, drive and stream separators, reserved Windows device names,
  empty and whitespace-only names are refused with `InvalidCollectionName`,
  which is also a `ValueError` so existing handlers keep working. The REST
  route answers 400 for a malformed name and still answers 409 for one that
  genuinely exists.
- **An empty API key was replaced by the environment's real one.**
  `OpenAIEmbedder(api_key="", base_url=...)` read the empty string as "not
  supplied" and fell back to `OPENAI_API_KEY`, handing a caller's production
  credential to whatever endpoint they had named, typically a local Ollama or
  vLLM server. An explicitly passed key is now used as given. The same shape
  was fixed in the GraphRAG extractor's Azure client, where `azure_endpoint`
  is likewise the caller's, and in its OpenAI client and the summarizer for
  consistency.
- **A user in no groups saw every document.** `Collection.enterprise_search`
  gated ACL filtering on `if user_principals:`, so an empty principal list
  was read as "no access control wanted" rather than "this user matches
  nothing". Its sibling `search_with_acl` got the same input right. Only
  `None` skips ACL now, which is what the docstring always said.

- **Cosmos DB queries now use bind parameters.** `get_document`, `get_node`,
  `get_document_nodes` and `get_child_nodes` interpolated caller-supplied ids
  directly into query text with f-strings, so a crafted document id could alter
  the query. They now pass values as parameters.
- **Databricks SQL values are bound, not escaped.** The Delta Lake backend
  interpolated every value into statement text, some through `_sql_literal`
  and some (documents, nodes, collection registry, inserts) with no escaping
  at all. `databricks-sql-connector` 3.x, which the package already requires,
  binds `:name` parameters natively, and every value now travels that way.
  Identifiers cannot be bound, so table names are backtick-quoted with
  embedded backticks doubled and control characters refused. Embeddings are
  the one thing rendered inline, as an `ARRAY` of floats, since arrays cannot
  be bound and a float cannot carry an injection. Verified against a
  recording cursor: the statements this backend hands the connector carry no
  caller-supplied value in their text. A live warehouse run is still owed.

### Changed

- **The Azure walkthrough pushes each collection's record, and records every decision.**
  `04_push_local_to_blob_cosmosdb.py` and
  `07_push_new_files_from local_to_blob_cosmosdb.py`, renamed from
  `04_push_local_to_blob.py` and `07_push_new_files_from local_to_blob.py`,
  send each collection's record from
  `.local/cosmosdb/access/collection_records/<collection>.json` to Cosmos DB
  before its files: the policy follows its file, and who may see a
  collection stays as the dashboard left it unless `--again`. Both read as
  the main function app does, SETTINGS, then STEP ONE to THREE, then the
  MAIN SCRIPT. 03 adds the `access` database, `collection_records` and
  `signin_records`, and both databases share their throughput, 600 and 400
  RU/s, so four containers fit the free tier. 01 adds a third Blob
  container, `audit`, under an unlocked retention policy of `VX_AUDIT_DAYS`
  days that allows appends, 06 points the app's decisions and access log at
  it with a query key it keeps the same, and 99 takes the policy off before
  deleting the group. The main function app reads each collection's policy
  from its record, stamps the client a policy reads, and records its own
  searches where the hosted API does.

- **Graph routes refuse in words a person can act on.** "Collection does not
  have GraphRAG enabled. Add 'Graph' tag to enable." is now that the
  collection was not made for graph search and how a collection gets a graph:
  `mode="graph"`, `--mode graph`, or the Graph tag over the API. The Graph tab
  says so without asking, and hides its empty chart, Extract and Reload.
- **CI tests what it claims to.** Lint covers the whole repository, scripts
  and examples included. Every Python version installs `signin`, so the
  server's own sign-in is tested on each. The coverage and nightly jobs
  install the `test` extra, and the coverage baseline is measured with exactly
  that in a fresh environment, so the job and a developer's run read the same
  number: 85.1% in both, where an environment without those extras reads 2.7
  points lower because a test whose extra is missing skips. The ratchet also
  fails when the test run fails, rather than comparing a broken run's
  percentage.
- **The benchmarks were run again**, on an otherwise idle machine, and every
  table is the new output; the comparison with Chroma, LanceDB and Qdrant
  keeps its order and its gaps.
- **A missing OpenAI key is a `ConfigurationError`,** said before the
  `openai` package is imported, rather than a `DependencyError`.
- **The sidebar folds from its own header.** The control sits beside the
  VectrixDB name. Folded, the rail keeps the logo and that control, one above
  the other, and the page icons below. The top bar's menu button is for a
  narrow screen only, where the sidebar opens as a drawer.

- **Search results lead with relevance.** A percentage and a word (strong,
  fair or weak match), with the raw score under it and a tooltip on each
  explaining what it is. A result says which search found it. The "quality"
  pill is "text quality", and says it is about how cleanly the text was
  extracted and not about the match.
- **A search mode the chosen collection cannot do is greyed out and says why.**
  Keyword and Hybrid need a text index and Rerank needs a reranker model on the
  machine. If the selected mode cannot run, the page moves to Dense.
- **A card no longer says "hybrid" beside a tag that says "dense".** The tag
  was the tier the collection was once opened with. Cards show what is true of
  the collection now, and tier words are left out of the tag row.

- **The Quality tab's "Worst chunks" is "Below the quality line".** It listed
  the five lowest whatever they scored, so a clean collection showed 0.86 and
  0.96 under the word "worst". It lists only what is under the threshold, with
  the reason beside each, and says so when nothing is.

- **`vectrixdb serve` listens on `127.0.0.1` by default, and refuses any
  other address with no API key and no sign-in.** The default was `0.0.0.0`,
  which with no key is every chunk of every collection readable by whoever
  finds the port. `VECTRIXDB_ALLOW_OPEN=1` is for a server behind a gateway
  that does the asking.
- **The start-up banner says the API key is set and does not show it.** It
  printed the first eight characters.
- **The count of things that need attention is red, at zero too.** It was
  orange, and green at zero.
- **Settings is a menu behind a gear in the top bar, not a page.** It holds
  the theme, the server's address, backend, version and access, the API key
  and a link to the API reference. It does not repeat what the Overview says.
  The separate Keys and theme buttons went into it. The Overview's five tiles
  sit five across, and below that the one that asks for attention takes a row
  of its own.
- **A collection's overview has four tiles: chunks, index health, size on disk
  and last write.** Language and model were tiles too, repeating the tags in
  the header and leaving a fifth tile alone on a second row. The date under
  the last write is short enough to stay on one line.

- **The dashboard's look.** A warm ebony and gold palette in the dark theme
  and paper and ink in the light one, where gold darkens to read as text and
  stays gold as a fill. One icon family throughout, Lucide's, inlined so the
  page still works with no network. The button at the top left folds the
  sidebar to a rail of icons on a desktop and remembers it, and opens the
  drawer on a phone as before.
- **A build id is shown as an id.** Eight characters without the `build_`
  prefix, the whole of it on hover, copied on a click. It is on the Builds
  tab, in a chunk's provenance, on an audit record and once among a
  collection's details, and no longer on collection cards, the collection
  header, a headline tile, the points table or search results. The points
  table shows where a chunk came from instead.
- **Settings no longer lists the models on the machine.** It read as a wall
  of commands. A collection whose model is missing is still flagged on the
  Overview and on its own page, and `vectrixdb download-models` fetches it.

- **The dashboard works at three widths.** Desktop from 1100 pixels, tablet
  from 700, phone below. On a phone a table row is a stack of values, each
  beside its column heading, two-column pages become one, metric tiles go two
  across, and card headers and buttons wrap. Buttons, chips, tabs and
  fields are at least 40 pixels tall below 900, keyboard focus is visible,
  and reduced motion is respected. A cell whose text is cut off carries the
  whole of it as a tooltip.

- **Audit record schema version 2.** Decisions gain `result_ids`;
  ingestions gain `source`, `document_id`, `document_version`, `chunking` and
  `ids_written`. A PostgreSQL table created by an earlier VectrixDB refuses at
  construction and prints the `ALTER TABLE` statements to add the columns
  rather than recreating the table, which would take the history with it;
  `PostgresAuditSink.SCHEMA_UPGRADE` and `INGESTION_SCHEMA_UPGRADE` hold
  them.

- **The library no longer writes to stdout.** Nineteen prints outside the CLI
  reported state, warnings and swallowed errors into whatever process had
  imported the package, with no way to turn them off. They are log records
  now, at the level each deserves. The eighty-nine in the downloader stay:
  that path runs from `vectrixdb download-models`, which a person invokes and
  watches.

- **Coverage is 85.1%, from 50.4%,** and the baseline is locked there. Every
  module the roadmap item named is at 100%: the CLI, the model downloader,
  the REST API, fusion, advanced search, the cache, the type helpers, the
  sparse index and the three search helpers, scaling, the batch helpers and
  the benchmark runners. The graph extractors went from 15% to 100%. The
  legacy streaming and embeddings modules are deprecated for 2.3 rather than
  tested. Writing those tests found twenty-five bugs, all listed under Fixed,
  and the suite went from about 970 tests to 1,992. It stands at 2,068
  after the sharding and storage work that followed, which carried
  coverage to 80.7%, and more than 3,900 after the sign-in, evaluation and
  deploy work, and 85.1% with the gateway work.
- **mypy is clean and CI gates on it.** The whole package type-checks with
  no errors, so the workflow now fails on any new one instead of on a rising
  count against a baseline.

- **The README led nowhere.** Three of the twenty-one documentation pages
  were reachable from it; the other eighteen existed only in `mkdocs.yml`, so
  a reader who did not build the site locally could not find the filters
  reference, the backend guide or the benchmarks. It has a Documentation
  section now, grouped as learning, doing, looking up and understanding, and a
  Project section carrying the changelog, contributing guide, roadmap,
  governance, code of conduct and security policy, none of which anything
  linked either. `tests/unit/test_docs_links.py` keeps both honest: every
  relative link in the repository has to resolve, and a new page under `docs/`
  has to appear in the table of contents. The bottom of the README still said
  models were "auto-downloaded", which is the claim the model section was
  already corrected to drop.
- **PyPI's Documentation link pointed back at the README.** `[project.urls]`
  sends it to the docs site now, and adds the changelog and the issue tracker,
  which had no route from the package page at all.
- **uv is documented, because it already worked.** The project is a standard
  PEP 517 and PEP 621 build with hatchling behind it and nothing pip-specific,
  so `uv build`, `uv venv`, `uv pip install`, `uv add` and `uv run` all work
  on it. Verified end to end: `uv build` produces an 89.7 MiB wheel and a
  93.7 MiB sdist, and that wheel installs into an empty uv environment, puts
  both console scripts on the path and searches. There is no `uv.lock`,
  because a library states ranges and lets the application pin.

- **The bundled English dense model is now bge-small-en-v1.5 (INT8).** By
  exact search it scores 0.713 nDCG@10 on SciFact where the previous default
  scored 0.645, at the same 384 dimensions and the same 33 MB. The previous
  default was e5-small-v2, mislabelled `vectrixdb/all-MiniLM-L6-v2` in the
  code; it is still `dense_model="e5-small"`, fetched once on first use since
  it left the wheel (see "A slimmer wheel" under Added). A collection with vectors and
  no recorded model predates this change and is opened with e5-small-v2
  automatically, so its searches keep working; `reembed()` moves it to the
  new default. Padding to the longest text in each batch is now the default
  for every dense model, which makes ingest about ten times faster; on
  SciFact it costs nothing. `pad_to_longest=False` reproduces 2.1 vectors.
- `DenseEmbedder` reads the pooling each model was trained with from its
  config (mean for e5, [CLS] for bge) and accepts `pooling=` for a custom
  model directory.

- **Graph search says when it fell back.** `search(mode="graph")` answers
  from vectors alone when the graph cannot be asked, which is right, but it
  used to be indistinguishable from a hit. `Results.degraded` now carries the
  reason. The new `GraphUnavailable(SearchError)` separates the ordinary case
  (nothing built, no searcher for the search type; logged at debug) from a
  real failure inside graph search (warned once per reason).
- **Community detection and summarisation run only when the graph changed.**
  They are the expensive tail of every `add_documents()`: under an LLM
  extractor, summarising is one model call per community, and it was paid
  again on every message even when nothing new was extracted. If the node and
  edge sets are unchanged the previous hierarchy is kept, and
  `GraphRAGStats.hierarchy_reused` says so.
- **The regex extractor sees lowercase prose.** Without spaCy it matched
  capitalised phrases and nothing else, so chat logs, tickets and notes
  produced no entities and graph mode silently had no graph. It now also
  takes acronyms, quoted terms and lowercase phrases that recur within the
  text, and it says once, at warning level, that it is the fallback and how
  to install a model: `pip install "vectrixdb[nlp]"` (new extra) and
  `python -m spacy download en_core_web_sm`. A `print()` used to go to stdout
  on every extractor construction instead.
- **Type checking is a ratchet with teeth.** `.mypy-baseline` records the
  package-wide error count and `scripts/mypy_ratchet.py` fails CI when it
  rises; lower it as you fix things. The count went from 517 to 467 in this
  release, and the strictly-typed set grew from two modules to six.
- **The core install is five dependencies instead of thirteen.** `aiosqlite`,
  `httpx`, `orjson`, `websockets` and `tokenizers` were declared but never
  imported anywhere in the package, and have been removed. FastAPI, uvicorn and
  pydantic moved to the `api` extra, so `pip install vectrixdb` no longer
  installs a web server. Importing `vectrixdb.api` without the extra now raises
  `DependencyError` naming the extra to install.
- New extras for the backends that previously had no declared dependencies:
  `azure`, `databricks`, `postgres`. The `all` extra no longer drags in `dev`.
- PyPI publishing uses Trusted Publishing (OIDC) instead of a stored API token.

- **The document index was completely broken on SQLite.** `_get_connection`
  created a generic `documents` table (`id`, `data`, ...) on every database
  including the internal `_documents` one, so `ensure_document_tables`'
  `CREATE TABLE IF NOT EXISTS` was a no-op and its index then failed with
  `no such column: doc_type`. Internal databases no longer receive the generic
  schema, and an empty legacy placeholder table is replaced on upgrade. A
  populated one is left alone and reported rather than dropped.

  This is the clearest example of what the bare `except:` clauses were costing:
  `list_documents` swallowed the error and returned `[]`, so the dashboard
  displayed "No documents indexed" indefinitely instead of surfacing a failure.

- **The one-line quick start failed on a clean install.** `easy.py` passed the
  registry label `vectrixdb/all-MiniLM-L6-v2` straight to `DenseEmbedder`, which
  expects one of its own keys (`dense_en`, `e5-small`, ...), so every embedded
  model lookup raised `Unknown model: vectrixdb/all-MiniLM-L6-v2` even with the
  bundled ONNX files present. Labels now resolve to bundled keys.

### Deprecated

- `PayloadIndexManager`, `NumericRangeIndex`, `StringIndex`, `TagIndex` and
  `GeoIndex` warn on first use and are removed in 2.3. They are exported and
  nothing calls them: every search path filters with a linear scan through
  `Filter.matches`. A differential test over four thousand generated filters
  found the two disagree on five operator families, and nothing on the
  roadmap asks for a pre-filtered index. Use the `filter` argument of
  `search()`.

- `StorageBackend.POSTGRESQL` has no backend of its own and raised
  "Unknown storage backend" when chosen, while the README listed it as
  supported. It warns and resolves to `AURORA_POSTGRESQL`, which speaks
  pgvector over any PostgreSQL, and is removed in 2.3.
- `EmbeddingManager`, `EmbeddingConfig`, `EmbeddedDenseProvider`,
  `EmbeddedSparseProvider`, `EmbeddedRerankerProvider`,
  `get_embedded_provider`, `StreamingBatchProcessor` and `StreamingReader`
  warn on first use and name their replacement. Nothing inside the package
  calls them; they are removed in 2.3.

### Removed

- **`setup.py` and `requirements.txt`,** leftovers from 1.0. The first set up
  a React front end with npm that has not existed since the dashboard became
  plain JavaScript inside the package, and the build ignores it; the second
  listed test requirements nothing used.
- **`filter_sql`, ahead of its schedule.** `LakebaseStorage.vector_search`,
  `hybrid_search` and `ultimate_search` and `AuroraPostgreSQLStorage.hybrid_search`
  took a raw WHERE fragment and interpolated it into the statement. Nothing in
  the library ever built one, which is why it was never exploited, and it sat
  exactly where somebody would reach to push an entitlement filter down:
  building that fragment out of a principal's values is the injection path.

  It was deprecated in this release with the removal set for 2.4, which the
  deprecation policy above would ordinarily require. Removed now instead, as a
  deliberate exception: holding an injection surface open for two releases to
  honour a notice period is the wrong trade when nothing in the library calls
  it and `filter=` has always been the replacement. Passing one is now a
  `TypeError` rather than a warning, which is the loud failure a caller wants,
  and a test asserts nothing in the package takes such a parameter again.

  Aurora's hybrid search used the fragment to select the ids both halves were
  restricted to. Those results are filtered by the collection now, as they are
  for every other backend.

### Fixed

- **A scanned page in two columns was read across the columns.** `RapidOcr`
  sorted what the engine found by the top of each box, then its left edge,
  and that was the whole of the reading order. Two columns came out `L1 R1
  L2 R2`, interleaved line by line, and the words of one line came out of
  order whenever a scan set their tops a pixel apart: "is Payment due". Every
  chunk from such a page was text nobody wrote, chunked and embedded as if
  somebody had, and nothing failed. `vectrixdb.extract.layout.reading_order`
  groups boxes into lines with a tolerance, finds the gutter between columns,
  reads down one column and then the next, keeps a heading that crosses the
  columns where it sits, and reads a table across, since its columns are
  short cells and not prose. Textract returns lines in no reading order, so
  its pages go through the same function. Replay:
  `old_ocr_read_across_columns.py`. Measured on the real engine by
  `scripts/ocr_order_check.py`, word for word against what a person reads:
  two columns 0.543 before and 0.942 now, three columns 0.415 and 0.950, a
  crooked, speckled scan 0.531 and 1.000, and a table 0.600 and 1.000, which
  the old sort was breaking too. What is left is the engine's own reading.
  It drops the space between sentences, `fees.Interest`, and
  `mend_sentence_breaks` puts that back for a plain word, its stop and a
  capitalised word, leaving `e.g.This`, `U.S.Army` and `www.Example.com`
  alone.
- **Text that loops scored a perfect 1.0 on extraction quality.** "the" a
  hundred times is how OCR fails on noise, and the same sentence over and
  over is how a vision model fails on a page it cannot read; both are made of
  good words, so all five signals were at their best. A sixth, `varied`,
  scales the score: the longest run of one token, and past thirty tokens how
  many four-token windows differ. Ordinary prose has it at exactly 1, so the
  threshold and what it was measured with have not moved, and a sentence said
  three times, a refrain or a repeated disclaimer, is still let through.

- **No collection card alone on a row.** Four collections in a grid that fits
  three left one card by itself. The page counts the cards and picks the most
  columns that fit without that, so four are two rows of two.
- **The keys table says what a key may do in full.** "Read, search and write"
  was cut to "Read, search an…".
- **The hand-written notebooks are out of `tests/`.** Ten notebooks from
  February sat among the tests, where nothing ran them. They are in
  `examples/notebooks/by-hand/` with a note saying what they are.

- **A collection written through the REST API warned, when the library
  opened it, that it was built with another model.** The server recorded
  the embedder's key, `bge_small_en`, where the library records the model's
  name, `vectrixdb/bge-small-en-v1.5`, and the library read the difference
  as a mismatch: "Search scores will be wrong until you call reembed()",
  about the same model. The server records the name now, the library treats
  a key and its name as one model, so collections an older server wrote
  open quietly too, and the dashboard shows the name. The text routes'
  documentation said queries were embedded by the multilingual model; they
  are embedded by the English default, as the documents are.
- **The CI coverage job would have failed on its first run.** The test
  extra installs azure-cosmos, and with it present the Cosmos DB fake left
  the SDK in place but still raised its own stand-in error, which the
  backend does not catch, so two document tests failed wherever the extra
  was installed. It raises the SDK's own error when the SDK is there.
- **Cosmos DB asked to create the document containers on every write.**
  `save_document` and `save_node` each tried to create both containers
  again, two requests answered 409 for every document and graph node
  written. They are made once.
- **The dashboard said "1 chunks".** Every count shown with a noun, on the
  collection cards, the points tab, the graph, the ingest log and the
  evaluation pages, goes through one helper and reads "1 chunk".
- **A hybrid search spent most of its time on highlights nothing read.** The
  library's hybrid and ultimate searches fetch ten times the limit from the
  keyword index, and each of those candidates came back with highlights, its
  whole text tokenized again, which the library then dropped. On SciFact a
  hybrid query at `limit=100` took 1.2 s, 1.1 s of it on highlights, and now
  takes 0.25 s; the benchmark's own profile is what found it. The server's
  routes, which return highlights, still make them for the results they send.
- **`scripts/compare_modes.py --doc` deleted part of the page it updates.**
  It cut the page at its own heading and wrote nothing after it, so a rerun
  removed the Languages section and everything below. It replaces its own
  section only. Its table now has a row for hybrid with `rerank=False`,
  because hybrid and ultimate run the cross-encoder by default and without
  that row its cost read as the cost of BM25, and the paragraph under the
  table is written from the numbers, so a ratio cannot outlive the run that
  measured it.
- **The README's comparison table undersold two of the engines it compares.**
  Checked against the installed versions: LanceDB searches multivector
  columns and Qdrant's local mode searches multivectors with MaxSim, where
  the table said no and server only; LanceDB's full-text index is its own
  now, not tantivy; and Chroma's BM25 hybrid search exists but refuses to
  run outside Chroma Cloud, which is what the cell says now.
- **`scripts/shard_bench.py` measured a search width collections do not
  use.** It searched at `ef_search=50` while a collection searches at 100,
  and the page called its numbers the default. It reads the collection's
  default now, and the page's numbers are measured there.
- **`scripts/beir_eval.py` put a search nobody runs into the reranked row's
  time.** The row's recall column reads an unreranked search at
  `limit=100`, and that search was timed together with the reranked one at
  `limit=10` that a user runs. Only the second is timed now.
- **`scripts/quantize_models.py` with no arguments rewrote every model in
  place,** the bundled INT8 ones again included, from a hard-coded list that
  named models the package no longer uses. With no arguments it lists the
  models on the machine, their sizes and which are INT8; `--model` is needed
  to change anything, an INT8 model is skipped, the original is kept as
  `.onnx.backup`, and `--restore` puts it back.
- **`scripts/replay.py` counted a replay that never ran as one that passed.**
  A wrong path, a plugin that did not import or a selection that matched
  nothing also makes pytest exit non-zero, and was read as the tests failing
  against the old behaviour. Only a test failure counts now; anything else
  exits 2 with pytest's error.
- **Three scripts ignored `--help` and did their whole job.**
  `make_notebooks.py` rewrote the notebooks, `readme_smoke.py` ran the
  README's example and `mypy_ratchet.py` ran mypy. Every script under
  `scripts/` answers `--help` with what it does now.
- **Benchmark results say what produced them.** `beir_eval.py`,
  `memory_bench.py`, `shard_bench.py`, `compare_models.py` and
  `benchmark_compare.py` write the date, the machine, the versions that
  matter (VectrixDB, usearch, onnxruntime, the other engines) and, where one
  is involved, the model into their JSON, because a table once quoted
  e5-small-v2 numbers as bge-small-en-v1.5's and the comparison's versions
  were typed by hand. `benchmarks/README.md` says which script and command
  wrote each file, and which page quotes it.
- **OpenSearch scores came back as one minus the engine's score,** and a
  `score_threshold` then kept the worst matches and dropped the best. The
  store hands the collection `1 - score`, as the Azure store always did, so a
  served result's score is OpenSearch's own and runs downhill. With two
  vectors fused, the scores had run uphill too.
- **The MCP server did not start on a fresh install.** `pip install
  vectrixdb[mcp]` now gets mcp 2, which renamed `FastMCP` to `MCPServer` and
  raises on the old name. The server uses whichever the installed version has.
- **A Markdown document of headings alone was indexed as nothing.** Every
  section was dropped for having no body under its heading, so an outline
  vanished. Its headings are its text now. Found by the property test.
- **`requests` was imported and never declared.** The Ollama fallbacks in the
  LLM extractor and the community summarizer used it when the `ollama`
  package was missing, and a machine without it failed there. They call
  Ollama's HTTP API with the standard library.
- **The OpenAI embedder's errors were garbled.** It passed whole sentences
  where the package name belongs, so a missing package read "pip install
  OpenAIEmbedder needs the openai package".
- **Tests skipped far more than they needed to without an optional package.**
  One Azure check skipped every OpenSearch, REST and dashboard test beside it,
  and one MCP check skipped the tools that need no SDK. Each skips only its
  own tests now.
- **Seven API test files could not be collected in CI**, because FastAPI's
  test client needs httpx and only the mcp extra had been bringing it. It is
  in `dev`.
- **The golden graph fixture depended on what else was installed.** Community
  detection finds a different number of communities with leidenalg, networkx
  or neither; the fixture now pins the method that needs nothing.
- **One test left the API modules re-imported for every test after it,** so
  a later test held one copy of the server while lazy imports got another,
  and failed or passed by the order the suite ran in. It puts them back.
- **`vectrixdb check` said there was no admin when single sign-on maps one,**
  and advised a `people add` that only works for the email list. It reads the
  group map, and warns when no group is mapped to admin.
- **Rerank scored every result the same.** A collection the library fills
  keeps a chunk's text in a column of its own, and the rerank search read it
  from the metadata, where it is not, so the cross-encoder scored every
  candidate on an empty string and they came back with one score, in the
  order the first search gave. It reads the text now.
- **Search results could come back without their text.** Keyword, hybrid,
  sparse, rerank and policy searches on a collection the library filled
  returned results with no text, and the dashboard showed the chunk's
  metadata in its place. Every search fills the text from where the
  collection keeps it, in one query, and a chunk with no text says so.
- **The Overview counted no documents.** Its count read only the server's
  own document index, so collections full of documents the library wrote
  showed none. It counts distinct documents across every collection now,
  kept until a collection changes.
- **A viewer was offered what a viewer cannot do.** The Overview showed them
  Run a search, and the Quality tab opened for them by its address. Both
  follow the role now, and a tab the role does not show opens the
  collection's overview. The Mode tile said the wrong mode when Rerank was
  chosen.
- **A run made with `vectrixdb evaluate` could be saved where the server
  never looks.** The server reads runs from `VECTRIXDB_EVALUATIONS` when it
  is set, and the command saved beside its `--path` regardless. It saves to
  the same place as the server reads now, and the people commands find the
  same list of people as the server, with `VECTRIXDB_PATH` and `--env-file`.
- **The start-up banner said there was no admin when the settings named
  one.** An admin in `VECTRIXDB_SIGNIN_USERS` is added as the server starts,
  so the line telling you to add the first one was wrong advice. It shows
  only when nobody will be an admin.
- **The sign-in card named ways in the server does not have.** A server with
  single sign-on alone said an authenticator app could sign you in, and one
  with passwords and no single sign-on mentioned a company account. The
  note names only the ways this server has.
- **A sign-in refused at the company's sign-in page said only that it did
  not finish,** under a heading that said the same. Cancelled or refused
  there, busy, and failed there are each said in words now. The reason
  arrives in the address, which anybody can write, so a code that is not on
  the list is never repeated on the page.
- **A guest's shared collections table split on a phone.** The ways a
  collection can be searched wrapped under the row's label. Each cell holds
  one value now.
- **Opening an Azure index could remove its semantic configuration.** Opening
  brings the index's definition up to date, and an update replaces the whole
  definition, so a handle opened with `semantic=False` took away the
  configuration a production search depended on, and one opened with
  `semantic=True` replaced a configuration made in the portal. An index keeps
  the configuration it has now, whichever way it is opened, and
  `semantic=True` adds one only to an index that has none.
- **A hybrid search on Azure AI Search or OpenSearch, from a handle opened to
  search an index another process filled, asked the service for one
  candidate.** The candidate count was capped by the number of chunks this
  process had written, which for such a handle is none, so every hybrid
  search returned one result. The service's own count is what it holds; the
  cap is ten times the limit now.
- **`vectrixdb serve --path` kept sign-in state in the wrong folder.** The
  server's application was built when its module was imported, which `serve`
  does before it sets the database path, so the sign-in file went to
  `./vectrixdb_data/auth` under whatever folder the server was started from,
  possibly not on the persistent volume, and `--no-dashboard` was ignored. The
  application is built the first time it is asked for now, and importing
  `vectrixdb.api` builds nothing.
- **A browser that had the dashboard before a brand was set kept the old
  page.** The file server answered the browser's "is my copy current?" against
  the plain file before the brand was applied. The branded page answers it.
- **The sign-in guide's advice on rotating the secret could lock everybody
  out.** The old secret can go only after one start with both, which seals the
  authenticator secrets again with the new one.
- **The dashboard at tablet and phone widths.** A row of tiles now has as many
  columns as tiles, so none is left alone on a line, and on a phone the tiles
  fold into one card of rows. The collection filters are one line that
  scrolls, the Access page's people take two lines between phone and desktop,
  the access log is two short lines an event on a phone, and the sign-in box
  no longer runs past the edge of a phone.
- **A folded sidebar showed a squeezed copy of its name.** The rule that hid
  the name matched the header's last element, and the drawer's close button
  had become the last element, so the name stayed, 86 pixels wide in a
  68 pixel rail. The rules name the header's parts now.

- **The first chunk ever added could not be found by keywords in a hybrid
  search.** Index keys start at 0 and the hybrid path tested `if idx:`, so the
  chunk with key 0 was treated as missing whenever only the keyword half found
  it, and its vector was never returned with `include_vectors`.

- **API keys were compared with `==`.** They are compared in constant time.
- **With sign-in on, other sites cannot read the server as whoever is signed
  in.** The server allowed any origin with credentials, which was harmless
  while the only credential was a header a page had to be given, and would
  not have been with a cookie. Cross-origin requests now need
  `VECTRIXDB_CORS_ORIGINS`.

- **A selective filter could miss documents that match.** Asked for every
  node, an approximate index returns most of them and not all, about 960 to
  995 of 1,000 on near-duplicate vectors, so a filter or a policy matching
  ten documents in a thousand could come back with nine, or none, and a short
  result looks like an ordinary outcome. When the search window is already
  the whole index and the result is still short, the points the index did
  not return are scored exactly. The same search also counted candidates
  again each time its window widened, so the withheld counts on a decision
  record could be too high; the counters start again with each window.
- **A reopened collection said it had never been written to, and that it was
  made when the server started.** Both times were kept in memory only, so a
  collection could be created after its last write. The last write is read
  back from the rows, and the creation time is kept with the collection; for
  one made before this, its oldest row stands in.
- **An open sidebar drawer could not be closed.** Below 900 pixels the
  sidebar opens over the page, and it covered the button that opened it. It
  closes from a button of its own, from a click on the page behind it, and
  from Escape.
- **The menu button did nothing on a desktop.** It was hidden by one rule and
  shown again by a later one. It folds the sidebar now.

- **On a phone the dashboard's text overlapped.** Twenty-two table layouts
  were inline percentage grids, which no stylesheet can override, the one
  breakpoint only hid the sidebar, and buttons never wrapped: at 375 pixels
  the Overview did not stack, column headings shrank to a letter, a state
  pill sat on top of a command and Settings scrolled sideways. The grids are
  named classes now. A browser sweep of every page at 1440, 768 and 375
  pixels finds nothing off screen, overlapping or clipped, and a structural
  test keeps inline grids from coming back.
- **An upgraded server could show the old dashboard.** The dashboard's files
  went out with no `Cache-Control`, so a browser guessed how long they were
  good for and kept showing the previous page against the new server. They
  are served `no-cache` now, which means the browser asks each time and the
  usual answer is a 304 with no body. A copy cached before this needs one
  hard refresh.

- **A search repeated after a write returned the answer from before it.**
  `VectorCache.invalidate_collection()` was an empty function, so the search
  cache was never invalidated: add a document, ask the same question again,
  and the new document was not there, for an hour with the memory cache and a
  day with Redis. Invalidation now forgets a collection's cached searches by
  prefix, with a scan and never `KEYS` on Redis, and a cache that cannot look
  keys up by prefix forgets everything rather than too little. Deletes and
  metadata updates invalidate too; only adds tried to before.
- **A delete never reached the storage backend.** `Collection.delete()`
  removed the point locally and left its row in the store, so on Azure AI
  Search, OpenSearch and the rest a deleted document stayed in the index for
  ever, and once the local index was empty the store served it back. The
  store is asked first now, and if it refuses nothing has been removed and the
  caller hears about it.
- **A revocation did not revoke anything a store served.** A metadata update
  never reached the store either, and under a policy the decision is made on
  the metadata the store hands back. `revoke()` and `update_metadata()` write
  to the store before the local copy now, promoted filter fields included, and
  a test proves a revoked document stops being returned by the service.
- **A cleared or recreated collection answered with documents that were
  gone.** `delete_collection()` closed the collection, dropped its catalogue
  row and its folder, and never told the store, whose rows outlived it. An
  empty index defers to the store, so `clear()` on a reopened collection left
  `count()` at zero and `search()` returning the old ids with no text. The
  store's rows and the collection's cached searches go with it now.

- **Clicking a point in the dashboard did nothing.** The row's handler was
  written `onclick="showPoint(${JSON.stringify(id)})"`, which puts a double
  quote inside a double-quoted attribute, so the attribute ended at
  `showPoint(`. The delete button on a point and the policy's copy button had
  the same fault. The JSON is escaped now, and a test refuses the pattern.

- **`LocalFetcher` could not read a real file URI on Windows.**
  `file:///C:/docs/a.pdf` was cut to `/C:/docs/a.pdf`, which is not a path
  there, and a space in a name stayed percent-encoded on every platform. It
  reads a real file URI now, and still takes `file://` followed by a plain
  path.

- **The Azure backend dropped its filter on a semantic query.** Adding the
  semantic query type replaced the request options instead of extending
  them, so with `semantic=True` a text or hybrid query ranked over every
  document and the filter, a promoted policy's scope rules included, never
  reached the service. Found by the first test that turned both on
  together; a redaction rule decided locally still hid the results, which
  is why nothing leaked and why it stayed hidden.

- **A collection on a storage backend reported `ENGINE` at open and ran
  `POST` after the first write.** The store served a search only while the
  local index held nothing, and every `add()` fills the local index too, so
  `require_pushdown=True` passed at open and then nothing ran in the engine.
  Found while building the Azure pushdown, which is the first backend that
  could have said `ENGINE`. The store now serves every search for a policy it
  can enforce, whatever the local index holds, `pushdown_mode` and the search
  use the same condition, and a backend failure under such a policy raises
  rather than answering from the local copy.

- **The REST server never saved the ANN index after a write.** `Vectrix.add`
  saves; the server's five write routes called `Collection.add` and
  `Collection.delete` and stopped there, so the index lived in memory until a
  clean shutdown. A server killed rather than stopped, which is how a
  container dies, reopened with every id in SQLite and no vectors: dense
  search found nothing and a point lookup crashed on the vector it did not
  have. Every write route saves now, a point without a vector is still a
  point, and a replay holds it.
- **The dashboard's Rerank and Late interaction modes were dense search.**
  Both sent flags the server did not read. Rerank is a real flag now; Late
  interaction is off the page until the server has a text route for it.
- **The dashboard said "Read Only" on a server with no key**, and its demo
  loader demanded a key the server did not have. Both read `/auth/status`
  now, and the badge says *Open, no key set*.

- **The sparse index only knew ASCII letters.** Its word pattern was
  `[a-z0-9]+`, so "Prüfung" indexed as "fung", "prêt" as "pr" and "t", Thai
  was dropped entirely, and the English stemmer ran on German and French
  because they happen to be ASCII. Every accented language had a sparse
  index full of fragments, and hybrid search on it was dense search with
  noise added. Words in any script are indexed as words now, Thai, Lao,
  Khmer and Myanmar join CJK as bigram scripts, `casefold` replaces `lower`
  so ß and the Turkish i are right, and a mixed run like "東京office" splits
  at the script boundary. `text_language=` on `Vectrix` says what the text
  is: `"en"` keeps stemming and stopwords, anything else turns both off, and
  it is persisted with the collection so a German index is never reopened
  with English stemming. The CJK bigrams themselves were already there and
  are unchanged. Found on the way: `clear()` recreated the collection
  without its `text_boosts`, so a cleared collection came back with every
  field weighted equally; it carries the boosts and the language now.

- **A delete kept the build id.** Only `add()` and `revoke()` minted an
  `index_build_id`. A delete, a clear, an index rebuild and a re-embed all
  changed what a query could answer and left the id where it was, and
  reproduction rests on one invariant: same build id, same index. Every
  mutation mints one now. The id is minted before a write and committed after
  it, so the chunks a write stores can carry it and a write that fails leaves
  the previous build in force.

- **The decision record's `duration_ms` included the timing floor's sleep.**
  It was measured around a call that padded, so the record reported the
  padded figure as the cost and `padded_to_ms` was never set. The record now
  keeps the true latency in `duration_ms` and the figure the caller saw in
  `padded_to_ms`; the gap between them is the channel that was closed, and
  the host paying for it is entitled to see what it bought.

- **The built-in REST API refused a policied collection in some places and
  served it in others.** Gating `count`, `list_ids`, `scroll` and
  `sparse_search` closed the content reads and left the collection's own size
  readable, which is the same inference by a slower route: `size_bytes` grows
  when documents are written, so a walled reader watching it move learns that
  a client they were refused is being written to. Every route now takes its
  collection from one helper that refuses a policied one with a 403, in place
  of the same four lines written out twenty-one times, and the listing names
  such a collection without its count or size. A collection with no policy is
  untouched, and a name nothing answers to is still a 404: the wall is over
  documents, not over which collections a deployment has.

  The refusal says that this API resolves no principals and the collection
  should be served from a tier that does. It deliberately does not repeat the
  library's own advice about `as_principal`, which is addressed to whoever
  wrote the service rather than to whoever is holding an HTTP client.

- **BM25 counted documents the principal was refused.** The subtlest of these,
  because nothing about it looks like a leak: the right documents came back,
  and only the right documents. What carried was the arithmetic. BM25 weights
  a term by how many documents contain it, counted over the whole index, so a
  visible document scored 0.1131 with a walled client in the index and 0.1823
  without it. Worse, with a dozen withheld documents about covenants the term
  goes cheap and a visible covenant document falls below a visible escrow
  one, so the *order* differed too: hiding the score would not have closed
  it, and `rrf` fused that ranking into hybrid, so hybrid carried it as well.

  On a policied collection the document frequency, the document count and the
  average length are now computed over the documents the principal may see,
  which means the policy decides before the scoring rather than over the
  results. That costs a pass over the collection's metadata per keyword
  query, and there is no cheaper correct version: scoring against a corpus
  and then hiding part of it leaves the corpus in the arithmetic. An ordinary
  filtered search is unchanged, because narrowing statistics to a `filter=`
  is a ranking decision rather than a security one.

- **Four read paths on `Collection` were never gated.** `search`,
  `keyword_search`, `get`, `get_batch` and `iter_documents` had all been
  gated and the work was marked done. `count`, `list_ids`, `scroll` and
  `sparse_search` had not been, and the REST server calls three of them: a
  policied collection served over HTTP answered a collection page, an id
  listing and a sparse query for anybody who could reach it. Found by reading
  every public method on the class rather than by a failing test, which is
  the uncomfortable part.

  Each now refuses without a `principal=` and answers for that principal with
  one. `list_ids` and `scroll` page over visible documents rather than over
  rows, so a page is not short whenever a withheld document falls inside it,
  because those gaps would map the wall. The library's own bookkeeping uses
  `_count_raw` and `_scroll_raw`, named so that a grep for `_raw` still lists
  every place that deliberately sees everything.

- **A wholesale denial returned instantly with `time_ms=0.0`.** When a
  principal matches nothing at all, the search is skipped, which is right.
  Reporting zero was not: every other empty answer carried a real duration
  beside it, so the zero was a tell a caller could read straight off the
  result. It reports elapsed time like any other answer now, padded when a
  timing floor is set. `similar()` had the same shape for a different reason,
  reporting a constant zero on every path.

- **`mode="sparse"` refused a principal it had been given.** The dense and
  hybrid paths passed the principal down to `keyword_search`; the sparse one
  did not, and that method refuses a policied collection handed no principal.
  Nothing leaked, because the failure was closed, but sparse search was
  unusable under a policy while the other two modes worked. No test caught it
  because every policy test took the default mode; they run over all three
  now.

- **A plain `pip install vectrixdb` could not create a collection.** `psutil`
  was imported at module scope in `core/scaling.py` and was not one of the
  five declared dependencies, and `core/__init__` reaches that module, so the
  first `Vectrix(...)` in a clean environment raised `ModuleNotFoundError`.
  Every development machine already had psutil, and `import vectrixdb` on its
  own succeeds because the package binds its names lazily, so the failure
  waited for first use and no test saw it. psutil reads whole-system memory
  and CPU for telemetry and the memory-pressure monitor, none of which is on
  the path that answers a query, so it is optional rather than a sixth
  dependency: without it the metrics report zero, `check_memory()` returns no
  pressure rather than guessing at one, and `get_available_memory_mb()`
  returns infinity, which reads as "no limit known" instead of "nothing free".
  `system_memory_available()` says which of the two you have. Found by
  installing the built wheel into an empty environment, which the suite now
  does in `tests/unit/test_clean_install.py`: it walks every module-level
  import in the package and fails on any undeclared one that is not wrapped
  in a try/except, so nothing is exempt by name. psutil joins the `dev`
  extra at the same time, not as a dependency but so that CI runs the branch
  that does take a reading; it was installed on every developer machine by
  accident, through a Jupyter dependency, which is the whole reason an
  unguarded import of it survived a full green suite.
- **`vectrixdb-mcp` answered a missing extra with a stack trace.** A plain
  install puts the command on the path but not the `mcp` package it needs, so
  running it was a new user's first encounter with the library, and it ended
  in an eight-frame traceback. The exception's message already names what to
  install; it is printed on its own now, and the command exits 1.
- **The last of the optional-argument family, and the smaller wrongs around
  it.** A cache TTL of zero cached for the default hour and served the value
  back; a chunk overlap of zero became fifty and duplicated 950 characters of
  every thousand; an empty source list queried every registered source, which
  made the guard below it unreachable; a numpy query vector raised because an
  array has no truth value; a community whose membership emptied kept its old
  members; and `load_vectors_mmap` documented a dtype it never checked.
- **`scroll()` applied its filter after cutting the page.** A page of rows was
  read and then thinned, so `scroll(limit=100, filter=...)` returned however
  many of those hundred matched: 15 of 46 on a 320-document collection. It
  reads forward until the page is full now, and the offset it hands back is
  where it actually stopped.
- **Seven filter shapes raised out of the middle of a search.** An operator
  typo, a nested dict used as a value, a malformed date range, and a metadata
  field that is numeric in some documents and a string in others all threw.
  The rule now: a caller's mistake is caught when the filter is built, naming
  the operator and the field; a value that cannot be compared matches nothing,
  because a search must not die on one odd row. `{"dims": {"w": 20}}` is an
  equality test against that dict rather than an operator called `w`.
- **A bounding box could not cross the antimeridian.** `min_lon` above
  `max_lon` means a box that wraps through 180, and comparing the bounds
  directly made it match nothing, so the strip either side of the date line
  was unreachable.
- **`ColBERTEncoder` returned random vectors.** It seeded a generator from the
  hash of each token, so it looked deterministic and meant nothing. Nothing in
  the package used it and the real late-interaction path loads the bundled
  ONNX model, but it sat in the module's `__all__` with tests pinning its
  behaviour. It raises now and names the replacement.
- **The binary quantiser answered metrics it cannot measure.** A 1-bit code
  keeps the sign of each dimension and nothing else, so Hamming distance is
  all it can give; `metric="euclidean"` returned it anyway, at recall@10 of
  0.090 against exact, which is noise in the shape of an answer. It raises
  now, as `ScalarQuantizer` already did. Its asymmetric distances also
  normalised by the dimension where the norm is its square root, which
  crushed every distance to within a few thousandths of 0.5 and made any
  threshold filter return nothing.
- **`compute_distances_fast` was 10 to 40 times slower than the path it
  replaced**, because it rebuilt a dimension-by-256 lookup table with a Python
  double loop on every call. One outer product now.
- **The product quantiser's euclidean returned squared L2** while the scalar
  one returned the distance itself, so the same metric name meant different
  things behind one base class.
- **A non-finite vector encoded to an ordinary-looking code.** NaN clipped to
  the training minimum in every dimension and was impossible to spot later.
  Both quantisers refuse it.
- **The one live-backend job could never run.** Its condition read the
  `secrets` context from `jobs.<id>.if`, where GitHub does not provide it, so
  the job skipped even with the secret set. The guard is on the steps now,
  with one that says why it skipped.

- **A selective filter returned nothing at all.** The filter is applied to
  whatever the index has already chosen, and the candidate window was a flat
  `limit * 10`. At a one percent pass rate nothing in that window matched, so
  `search(limit=5, filter=...)` answered with an empty list while ten
  documents matched, and an empty result is the answer that looks ordinary.
  The window now widens until the limit is filled or the index is exhausted.
  An unfiltered search is untouched; only a selective filter pays.
- **Reading from a store that never held a document raised.** The document
  tables are created lazily, so `list_documents()`, `get_document()` and
  `delete_document()` on a fresh SQLite store raised
  `sqlite3.OperationalError: no such table: documents`, a raw driver error
  out of a call whose honest answer is that there are none. They create the
  tables first now, and the storage backends guide's own example runs.
- **A missing model raised a bare `FileNotFoundError`.** The README and the
  error-handling guide both promise that one `except VectrixError` covers the
  library, and six raise sites in the model loader did not honour it.
  `ModelNotFoundError` now also inherits `FileNotFoundError`, so existing
  handlers keep working, and the message points at the latest release rather
  than v1.9.0, two minor versions behind the package.
- **The scalar quantiser truncated where it should round.** `astype(uint8)`
  floors, so every reconstructed value was biased low by half a step, mean
  absolute error was double what rounding gives, and the training maximum
  encoded to 254 with code 255 unreachable. Measured after: signed bias falls
  from -0.50 steps to -0.002, mean absolute error from 0.50 to 0.25, and the
  relative L2 error halves.
- **The product quantiser's cosine ranked by projection, not angle.** It
  divided by the query norm alone and called that an approximation, so two
  vectors pointing the same way scored differently if one was longer, and
  distances fell outside the valid range, negatives included. The
  reconstruction's own norm is part of the denominator now, looked up from a
  cached table of centroid norms. Recall against exact cosine improves on
  both unit-normalised and varied-magnitude data.
- **The product quantiser crashed on any duplicate-heavy corpus.** Once
  k-means++ had picked every distinct point the weights summed to zero and
  numpy refused with "probabilities do not sum to 1". Not a small-corpus
  problem: fifty thousand vectors drawn from a forty-entry palette hit it
  too. Remaining centroids are filled uniformly.
- **`is_models_installed()` said no on a correct install.**
  `model_dir_name("reranker")` resolved to a directory that was never
  bundled; the reranker ships as `reranker_en`. The exported check returned
  False on a stock wheel, and the twelve tests gated on it skipped
  everywhere, CI included. The bundled English model now answers "is a
  reranker here", and never "is the multilingual one here": when it answered
  both, neither multilingual model could be fetched on a fresh install, by
  the first-use download or by `vectrixdb download-models --type dense`,
  the command the error message names, which said "already installed". The
  download, `models-info` and the models route ask with `exact=True`, and
  the route calls a model "wheel" only when the wheel carries it.
  `download-models --help` named two models the package does not use.
- **`vectrixdb serve --no-dashboard` served the dashboard.** `create_app`
  honours the flag, but `run_server` starts the module-level singleton by
  import string and could not pass it. The switch travels through
  `VECTRIXDB_DASHBOARD`, the way the database path and the API keys already
  do, and the banner no longer advertises what it just turned off.
- **`index_text` accepted a chunk size and discarded it.** Both chunk
  arguments were documented, forwarded by `index_file`, and read by neither.
  Structure still chooses the boundaries; a node longer than `chunk_size` is
  now split into siblings under the same parent.
- **`VectrixSync.incremental()` was `full()` under another name.** It
  returned `self.full()` and never read `since`, while the module docstring
  advertised "Subsequent: only changes" and `start_scheduler` called it every
  tick, so a scheduled sync re-copied the whole dataset forever. It compares
  rows on `updated_at`, keeps a watermark, and reports `SyncResult.filtered`
  as False when the source carries no timestamp to filter on, which is the
  case for a local SQLite collection.

- **Deleting memories by an empty id list deleted everything.** The MCP
  `forget` tool guarded with `if ids:`, so an agent whose filter matched
  nothing sent `ids=[]`, fell through to the unscoped branch and removed
  every turn in every session, then reported success. `ConversationMemory.
  forget` underneath was written correctly against `None`; the wrapper one
  layer up undid it. This is the tenth instance of the optional-argument
  defect family, and the sweep that found it also found the ACL and sync
  ones below: the earlier nine fixes were point repairs that never followed
  the call graph outward.
- **`$in` and `$nin` were wrong for every list-valued field.** The test was
  `field_value in self.value`, which puts the whole field inside the operand
  and is false for any list, so `$in` matched nothing and `$nin`, its
  negation, excluded nothing. An exclusion filter that silently excludes
  nothing is the direction that leaks the records you meant to hide.
  Membership against a list field is an overlap test now, and a scalar field
  behaves exactly as the filter reference describes.
- **Absent, null and empty were the same thing to three operators.**
  `_get_nested_value` returned `None` both for a path that did not resolve
  and for a field explicitly set to null, so `$exists` said a null field was
  not there, `$is_null` said an absent field was null, and `$is_empty` said
  both were empty. A missing path returns a sentinel now, and each operator
  picks out exactly the case the documentation gives it. A present null also
  takes part in equality and membership, where it is a value like any other,
  rather than failing both `$eq` and `$ne` at once.
- **A failed search on Lakebase returned an empty result set.**
  `vector_search` printed the exception and returned `[]`, and
  `hybrid_search` and `ultimate_search` quietly downgraded to a weaker
  search, so a connection failure, a permission error and a genuinely empty
  collection were the same answer. All three raise `SearchError` now, chained
  from the original.
- **A collection that would not load was dropped in silence.** The loader
  printed a warning and carried on, so the collection was absent from the
  database object and listing it showed nothing; a user could reasonably
  conclude the data was gone and re-ingest over it. The rest of the database
  still opens, but the failure is recorded in `VectrixDB.failed_collections`,
  raised as a `CollectionLoadWarning`, and asking for that collection by name
  raises and says why instead of reporting it missing.
- **The binary quantiser could fit to NaN and report success.** With an empty
  calibration set, or `threshold_samples=0`, or non-finite training data,
  `np.median` returned NaN, `_is_fitted` was set anyway, and since every
  comparison against NaN is false every vector encoded to an identical code:
  the index silently became one bucket. All three now raise
  `QuantizationError`.
- **`Vectrix.search(rerank="exact")` did nothing and `rerank="mmr"` returned
  nothing.** Both rerank by vector, and the candidates reaching them carry no
  vector because the dense path does not ask for one. With no way to fetch
  them, `exact` returned the input order untouched and `mmr` dropped every
  candidate. The Easy API now passes a lookup for just those candidates, and
  MMR falls back to score order rather than an empty result if it ever has no
  vectors at all. The collection-level rerank paths already requested vectors
  and were never affected.
- **Aurora PostgreSQL's hybrid search accepted a filter and ignored it.** The
  signature matched its Lakebase sibling so callers could swap backends, and
  the body never read `filter_sql`, so a filtered hybrid search returned
  unfiltered results. Introduced in this release while adding the method that
  was missing, and overtaken by the removal of `filter_sql` above: the
  parameter that was ignored no longer exists.
- **A sparse model that cannot run was swapped for BM25 in silence.** The
  sparse half of a hybrid search reaches a learned model only through a
  storage backend that implements its own hybrid search; on a local
  collection it is the bundled BM25 index whatever `sparse_model` says. Asking
  for SPLADE returned BM25 results with identical scores and no warning.
  Naming a sparse model that cannot be the one that runs now raises
  `SparseModelUnavailableWarning` at construction.
- **`VectrixSync.full(collections=[])` copied every collection.** The guard
  was `if collections and name not in collections`, which never skips for an
  empty list.

- **Cosmos DB stored vectors and could not read them back, so its vector
  search returned nothing.** `get()` and `scan()` dropped every key starting
  with an underscore to strip the fields Cosmos adds, which also dropped the
  caller's `_embedding`, the name Collection keeps the vector under. Every
  query on every Cosmos collection came back empty. Only the seven fields
  Cosmos actually adds are stripped now. The same idiom appeared in five
  more readers and is gone from all of them.
- **Lakebase lost the vector on a round trip and could not update at all.**
  `get()` selected only `metadata` and `text_content`, never the vector
  column. `update()` set a column named `embedding`, which no table has: the
  column it creates is `dense_embedding`, so every update raised.
- **Aurora PostgreSQL discarded most of a document and had no hybrid
  search.** `insert()` stored only the dict nested under a `metadata` key
  and dropped every other field, so an ordinary flat document, which is what
  Collection writes, lost its entire payload; `update()` skipped any field
  that was not a column of its own, so updating one wrote nothing and still
  reported success; and `get()` and `scan()` returned raw table rows rather
  than the caller's document. `hybrid_search` was absent entirely, so
  calling it raised AttributeError. It has one now, fused the way its
  sibling backends fuse.
- **Delta Lake reported success whatever happened.** `update()` and
  `delete()` both returned `True` unconditionally, so `delete_batch()`
  counted deletions of ids that were never there. `get_document()` and four
  sibling readers handed back the metadata column as the JSON text it is
  stored as, where every other backend returns a dict.

- **A search limit of zero returned one result.** `search(limit=0)` and
  `search(limit=-5)` both came back with one hit: the number went to the
  index unchecked and negative slicing did the rest. The REST layer has
  always refused it with `gt=0`; the Python layer refuses it now too.
- **The dashboard's file upload never worked.** The page posted markdown
  chunks to `/api/v2/collections/{name}/add`, a route the server has never
  served, so every drag and drop ended in the error snackbar. It posts to
  `text-upsert` now, and the smoke test drives the page's own payload shape
  through the real route so the two cannot drift apart silently again.
- **A heavily deleted collection got slower with no sign of why.** Deleted
  vectors stay in the HNSW graph and are filtered after the search, so at a
  fifth deleted a query went from 1.0 ms to 18.0 ms on 20,000 vectors.
  `Collection.tombstone_ratio` reports the share, and `search()` sets
  `Results.degraded` past a fifth, naming `rebuild_index()` as the remedy.

- **One unbroken token could stop an ingest.** The WordPiece scan tried the
  longest match first and shortened by one on every miss, slicing a fresh
  substring each time, so its cost grew with the square of the word it was
  given. Measured: 4,000 characters took 7 seconds, 8,000 took 40, and
  50,000 never finished. Ordinary prose never reaches that, but real corpora
  are full of tokens that are not words: base64 blobs, minified JavaScript,
  long URLs, hashes, DNA. Word length is capped at 100 characters, which is
  where the reference WordPiece implementation caps it, so nothing that is
  actually a word tokenizes differently. The same 50,000 character token now
  embeds in 10 milliseconds.

- **An explicit empty filter returned everything instead of nothing.**
  `SparseSearch.search`, `BM25Scorer.score` and `ColBERTSearch.search` each
  tested `filter_ids` for truthiness, so a caller who passed an empty set,
  meaning allow nothing, got an unfiltered search. They test against `None`
  now, and `CONTRIBUTING.md` carries the rule, because this was the same
  line of code written nine times across the package: an optional argument
  read for truthiness treats a caller's explicit empty set, zero timeout,
  zero iteration count, zero start time or empty collection as "not given".
  The others fixed here are `ConnectionPool.acquire(timeout=0)`, which
  blocked for the configured default instead of failing fast,
  `BenchmarkRunner.run_benchmark(n_iterations=0)`, `MetricsCollector` and
  `ThroughputTracker` reporting elapsed time from a start of exactly zero,
  and a measured `recall_at_k` of 0.0 reported as no measurement at all.
- **Cosine search could miss its own best hit.** `SparseIndex.search_cosine`
  took the top `2 * limit` documents by dot product and then reranked those
  by cosine, so a short document with a small dot product and the highest
  cosine never reached the rerank. It ranks every document the query touches
  now, which is the same accumulation pass and a bigger heap.
- **Graph extraction dropped or dangled relationships when entities merged.**
  `NLPExtractor.extract()` deduplicates entities across text units by name
  and then looked up relationship endpoints by exact id, so a relationship
  whose entity lost the id race was silently discarded; `_extract_with_pipe`
  kept the relationship and left it pointing at an id absent from the
  returned entities. Both remap through the surviving entity now.
- **Merging two extraction results reset a fact to inferred.**
  `ExtractionResult.merge_with()` rebuilt a new relationship without its
  `confidence`, `valid_from`, `valid_to` or `superseded_by`, so an extracted
  fact became an inferred one and a superseded fact came back to life the
  first time two results were merged.
- **`LLMExtractor.extract_batch()` ignored `batch_size`.** Every text unit
  was submitted to the thread pool at once whatever the caller asked for,
  which against a rate-limited model is the whole corpus in flight.
- **A file the memory-mapped batcher wrote could not be read back by the
  batcher.** `create_vector_mmap()` wrote a headerless raw array under a
  `.npy` name while `load_vectors_mmap()` called `np.load`, which needs the
  header. Both use `numpy.lib.format.open_memmap` now. Separately
  `merge_chunks(delete_chunks=True)`, the default, always raised
  `PermissionError` on Windows because it held the first chunk mapped while
  deleting it.
- **Three crashes and a silent miscount in the search helpers.**
  `MaxSimScorer.score` raised from numpy on a document with no tokens,
  `MultiQuerySearch.search` raised `IndexError` partway through a fan-out
  when `weights` was shorter than `queries` (it validates up front now),
  `DenseSearch` with a negative `k` returned almost every vector through
  `[:-1]` slicing, and `SparseSearch.remove` left an empty posting list
  behind that `get_stats()` counted as a live term.

- **The chunker could exceed the size it was given, and repeat an offset.**
  `chunk_text()` tracked a running total that counted one separator per
  split, including the last, and never measured the chunk it was about to
  emit, so a carried overlap could push a chunk past `chunk_size`. Separately
  the recursive strategy mapped each piece back to an offset with a search
  that could start at or before the previous match, so repetitive text gave
  two chunks the same start and the same text. Found by the ingest property
  test; both counterexamples are pinned on it now.
- **`VectrixSync.full()` copied nothing and reported success.** Three bugs
  on one path, each hiding the next. It read `collection._storage`, which is
  the database's attribute and not the collection's, so every run put an
  AttributeError in the per-collection error list. Behind that, the copy was
  guarded by `if source_coll and target_coll`: a collection is sized and the
  target had just been created, so the empty target was falsy and the copy
  was skipped with no error at all. Behind that, the copy called `iterate`,
  which only three of the seven backends implement; it pages with `scan`
  when `iterate` is missing. The class docstring now also states the scope
  that was always implied: this moves rows between storage backends, so a
  local target's own index and count are not rebuilt by it.
- **`VectrixDB.with_auto_scaling()` raised TypeError on every call.** It
  passed `max_memory_percent` to `ScalingConfig`, which has no such field.
  The value goes to `memory_high_watermark`, which is what both docstrings
  describe. Found by mypy.
- **Graph extraction over more than one chunk crashed.** The REBEL
  extractor and the hybrid extractor read `text_unit.content` in their
  multi-unit loops; `TextUnit` carries its text on `.text`, so both raised
  AttributeError on the first chunk. REBEL is the default extractor. Found
  by mypy.
- **The global graph searcher stashed a score on somebody else's dataclass.**
  `_get_candidate_communities` set a `_level_weight` attribute on `Community`
  instances and read it back later. The weights live in a dict on the
  searcher now, with the same values.
- **Enterprise search returned documents nobody was cleared to see.**
  `enterprise_search()` passed `default_allow=True` to the ACL filter with no
  way to change it, so a document carrying no `_acl` was returned to every
  caller who asked with principals. Its sibling `search_with_acl()` defaults
  to refusing those documents, and the REST request model already had the
  field. `enterprise_search()` now takes `default_allow` (default `False`,
  matching the sibling) and `POST /api/v1/collections/{name}/search/enterprise`
  passes the caller's value. Callers who relied on unlabelled documents
  appearing must now ask for `default_allow: true` explicitly.
- **A collection reported HNSW parameters it was not built with.**
  `Collection.__init__` builds the index from `m` and `ef_construction` but
  stored a default `IndexConfig()`, and `info()` reports that config, so a
  collection created with `m=8` reported 16. The REST API's `hnsw_m` and
  `hnsw_ef_construction` fields looked like they did nothing.
- **An installed ColBERT model was reported as missing.**
  `is_models_installed()` derived the directory from the model type, but the
  multilingual late-interaction model installs into `bge-m3` and the English
  one into `colbert`, so `vectrixdb models-info` said Not installed however
  many times you downloaded them. One `MODEL_DIRS` map now answers for both
  the check and the downloader.
- **`vectrixdb download-models --type colbert` raised KeyError.**
  An advertised choice whose handler began with `MODEL_CONFIG["colbert"]`, a
  key that has never existed. It resolves to the English late-interaction
  config, which installs into the same directory.
- **A failed model download left its zip behind.** The temporary file is
  created with `delete=False` and was only removed on the success path, so
  every interrupted download or unreadable zip leaked a file into the system
  temp directory. It is removed in a `finally` now.
- **`TextAnalyzer(stopwords=set())` stripped English stopwords anyway.** The
  constructor read `stopwords or ENGLISH_STOPWORDS`, so an explicit empty set
  meant the opposite of what it says.
- **A new `Point` carried a naive timestamp.** `created_at` defaulted to the
  deprecated `datetime.utcnow`, while `Point.from_dict()` produced an aware
  one, so two points of the same collection could not be compared. Both use
  the package's `utcnow()` helper now.
- **The missing-dependency hint dropped the extra it names.** The CLI printed
  `pip install vectrixdb[setup-models]` through Rich, which read
  `[setup-models]` as a markup tag and printed `pip install vectrixdb`.


- **The OpenSearch backend had never been driven end to end, and six things
  were wrong with it.** The collection registry was written without an id and
  read back by id, so `get_collection_config()` always returned None;
  `insert()` stored `data["metadata"]` while callers put their fields at the
  top level, so every field was dropped; inserting an existing id created a
  second document instead of replacing it; reads returned the raw OpenSearch
  document with the caller's fields nested under `metadata` and searches
  returned OpenSearch's own `_id`; `scan()` had a different signature from
  the base class and ignored `limit`, `offset` and `filter_func`; and
  `delete_collection()` left the registry row behind. All found by running
  the storage contract suite against an in-memory stand-in for the
  opensearch-py client (`tests/unit/fake_opensearch.py`), which now runs on
  every push. A live run against a real domain is a nightly job that only
  runs when the repository has an endpoint secret.
- **`create_app(db_path=...)` ignored its argument.** The lifespan handler
  read `VECTRIXDB_PATH` from the environment only, so an embedded app, and
  every test, opened `./vectrixdb_data` in the working directory. The path
  given to `create_app()` now wins over the environment.
- **A sparse, reranker or late-interaction model fetched from a GitHub
  release raised `TypeError`.** The three constructors take `model_dir`; the
  download branch passed `model_path=`. Found by mypy.
- **`POST /api/v1/documents` raised on every call.** It passed `text=` to
  `DocumentIndex.index_text()`, whose parameters are the document id and its
  content. Found by mypy.
- **The benchmarks page quoted e5-small-v2 numbers as bge-small-en-v1.5.**
  The BEIR harness cached its index by dataset and mode only, so after the
  default model changed it reopened the e5 index and the legacy path kept e5
  for it. The cache is keyed on the model now and the page carries the
  measured bge rows: 0.707 dense and 0.736 hybrid nDCG@10 on SciFact, from
  0.650 and 0.723.

- **A deleted vector could hide the documents that remained.** usearch keeps
  deleted keys in the graph as tombstones, so the index holds more keys than
  the collection has live documents, but `search()` asked it for exactly the
  live count. When the nearest neighbours were all deleted the search came
  back empty while the survivors sat in the index one position further down:
  with two documents and one deleted, a query returned nothing at all. It now
  asks for the tombstones on top of what was wanted. Found by the model
  switch, which changed which neighbour ranked first; the old default hid it
  by luck. Regression test in `TestSearchAfterDelete`, replay in
  `tests/replays/old_tombstone_clamp.py`.
- **Graph mode returned results in an order its scores did not explain.** The
  path where the graph contributed a boost sorted by score; the paths where
  it did not, an unavailable graph or a query no entity matched, returned the
  reranker's order with the fused scores beside it, so a caller sorting or
  thresholding by `score` got a different list from the one it was handed.
  Every return path now sorts by the score it reports.
- The REST API embedded queries with a model named in a literal beside the
  library's own default. The two drifted the moment the default changed,
  which is the bug `DEFAULT_QUERY_MODEL` was introduced to prevent; it is now
  derived from `Vectrix` rather than restated.

- **Vector search spent most of its time outside the index.** Profiled at
  2,000 vectors: usearch answered in about a millisecond, then one SQLite
  SELECT per result took six and hashing the query vector through JSON for
  the result cache took two. Payloads are now fetched in one statement per
  query and the cache key is the rounded float32 bytes. A query dropped from
  9.9 ms to 2.5 ms on the same collection.

- **`AuroraPostgreSQLStorage` could not be instantiated.** It never
  implemented `get_collection_config`, `scan` or `flush`, all abstract on
  `BaseStorage`, so constructing it raised `TypeError` before `connect()` was
  reached. The storage contract suite now instantiates every backend and
  caught it. `insert` also accepts `_embedding` as the vector key, as the
  other backends do.
- **Readers in other processes failed while a writer was busy.** Both SQLite
  connections opened with the default five-second busy timeout and re-ran
  `PRAGMA journal_mode=WAL` on every open, which takes an exclusive lock, so a
  process opening a collection mid-write got `database is locked`. When that
  hit the collection registry the loader skipped the row, the open fell
  through to create, and the insert failed on the unique name. Connections
  now wait up to thirty seconds, switch the journal only when it is not WAL
  already, and a duplicate registration is a `ValueError` naming the
  collection.
- **A process that only searched rewrote the index file on close.** `save()`
  ran unconditionally, so a reader closing at the wrong moment raced the
  writer's own rename and one of them got `PermissionError` on Windows. The
  collection now tracks whether the index changed and saves only then; the
  rename over the old file is retried briefly on Windows, where a reader
  loading the index holds it open for a few milliseconds.
- **usearch could not open paths with non-ASCII characters, or long paths on
  Windows.** The native library takes narrow strings and has no long-path
  support, so a collection under `über/日本語` or deeper than 260 characters
  failed inside usearch while the SQLite files beside it were fine. Long
  paths get the extended-length prefix, and when the native call still
  refuses, the index is read and written through Python as bytes.
- **Malformed filters escaped the parser as `AttributeError`.** A list where a
  dict was expected under `$and`, `$or`, `$not`, `must`, `should` or
  `must_not`, or a string as a Qdrant condition, is now refused with
  `TypeError`. Fuzzing found both; the documented errors for a bad filter are
  `ValueError`, `TypeError` and `KeyError`, and nothing else gets out.
- **Docs examples that could not run.** The README's document-index example
  passed a path where `DocumentIndex` takes a storage, used `...` for
  vectors, and called a `LateInteractionEmbedder.embed` that does not exist;
  the filters, export and conversation-memory pages referenced names their
  fences never defined. Every fence is now executed in CI, so this class of
  drift fails the build.

- **Search did not work. Four separate defects, now all fixed.** Together these
  made the product's central feature unusable, and none were visible from the
  old suite because nothing measured a real query.
  1. *The ANN index was never persisted.* `Collection.save()` existed but was
     only reachable through `VectrixDB.close()`, which the one-line `Vectrix`
     API never exposes. A fresh process loaded an empty index, so every search
     returned nothing while `count()` still reported the rows. `Vectrix.add()`
     now saves the index.
  2. *`SearchResult.score` was a distance on the default backend.* usearch
     returns a distance and hnswlib a similarity, and only the hnswlib branch
     converted. Callers ranking by score got the order inverted and
     `score_threshold` discarded the closest matches. Both backends now go
     through one conversion.
  3. *`SearchResult` had no `text` field at all,* so every API response carried
     an empty snippet. It now has one, populated from the stored `text_content`.
  4. *The API embedded queries with a different model than the library writes
     with.* `get_text_embedder()` called `DenseEmbedder()` with no argument,
     which defaults to the multilingual model, while `Vectrix` writes with the
     English `dense_en`. The same sentence embedded by both scored **0.0148**
     against itself, so dashboard rankings were noise. Query embedding now
     defaults to the write-side model, and a test asserts the two agree.

  End to end, "how do I stop the server" against the sample corpus now returns
  "Ctrl+C stops the server" at 0.86, with the text, surviving a restart.

- **Community hierarchies were never saved, so graph search quietly stopped
  using the graph after any restart.** `GraphStorage.save_hierarchy` returned
  early and then did nothing, above a comment claiming the rows were "already
  saved during detection via `save_community`". Nothing in the package ever
  called `save_community`, so the `communities` and `community_members` tables
  were written by no code path at all, and `load_hierarchy` matched it by
  returning `None` unconditionally.

  That reached search. The pipeline gated its global and hybrid searchers behind
  a truthy hierarchy, and `GraphSearchType.HYBRID` is the default, so a reopened
  graph raised `Hybrid searcher not initialized` on the first query, which
  `easy.py` caught at debug level before returning vector-only results. Nothing
  failed and nothing was logged: `mode="graph"` answered every query after a
  restart without consulting the graph. Both methods are now implemented, the
  searchers are built against whatever hierarchy exists, and that fallback now
  logs. It warns once per distinct reason and drops to debug for repeats: the
  package installs no logging handler, so `logging.lastResort` puts warnings on
  stderr for anyone who never configured logging, and a graph that extracts no
  entities stays that way, so warning per query would bury the occurrences that
  mean something.

  Saving clears both tables and rewrites them rather than relying on
  `INSERT OR REPLACE`, because community ids are positional
  (`community_L{level}_{index}`) and are reused: a rebuild producing fewer
  communities would otherwise leave phantoms behind, holding members that may no
  longer be in the graph. It clears them outright instead of excluding the
  survivors by id, which would bind one SQL parameter per community and exceed
  `SQLITE_LIMIT_VARIABLE_NUMBER`, only 999 before SQLite 3.32, on exactly the
  graphs large enough to care. `children_ids` is derived from the stored parent links,
  since it is not a column.

- **The reranker stage never ran for bundled models.** The bundled
  `RerankerEmbedder` has a `rerank` method as well as `score`, so the branch
  meant for HuggingFace models caught it and called it with `limit=`, which it
  does not accept. The `TypeError` was swallowed and hybrid, ultimate and
  graph searches quietly returned the fused order in every mode. On top of
  that, the default reranker for those modes was `"L6"`, which is a download,
  not the bundled L12. The bundled path is now taken first, the default is
  L12, and a reranker failure is logged once per reason instead of never.
  `search(rerank="cross-encoder")` in dense mode uses the same bundled model
  instead of reaching for sentence-transformers, which is not a dependency.
- **The bundled cross-encoder and ColBERT ran once per document, padded to
  512 tokens.** A pair cost about 150 ms, thirty candidates five seconds, and
  ColBERT twenty; this is why hybrid and ultimate cost 30 to 110 times a dense
  query. Both now batch, one session call per 32-token length bucket. Not per
  batch: the INT8 exports are padding-sensitive (the same pair scores 0.765
  padded to 512 and 0.801 unpadded), so padding a batch to its longest member
  would make a document's score depend on its neighbours. Bucketing by the
  document's own length keeps scores reproducible; thirty pairs now take about
  80 ms.
- **BM25 uses BM25's IDF.** The text index computed `log((N + 1) / (df + 0.5))`,
  which never reaches zero for a term in every document and overweights rare
  terms in small collections. It now uses the Lucene / rank_bm25 form,
  `log(1 + (N - df + 0.5) / (df + 0.5))`, and a test compares scores against a
  reference implementation written out in full (and against rank_bm25 when
  installed).
- **The sparse index saw no CJK text.** Tokenisation was `[a-z0-9]+`, so a
  Chinese, Japanese or Korean collection had an empty keyword index and hybrid
  search silently became dense search. CJK runs are now indexed as character
  bigrams, the standard model-free segmentation.
- **Vectors in the local document store are float32 blobs.** Every local
  collection kept a JSON copy of each vector, measured at 8.8 KB per 384-d
  vector against the 1.5 KB the index already holds. Dense vectors and ColBERT
  token matrices now live in blob columns; rows from earlier versions still
  read, and `SQLiteStorage.compact_vectors()` rewrites them. The fallback
  vector search over that store is one NumPy pass instead of a Python loop.
- **`NativeHNSWIndex` is usable now.** Build cost grew close to cubically
  because every inner loop called a scalar distance function once per pair,
  and the pruning step re-ran the neighbour-selection heuristic, with
  candidate extension, once per new connection: two hops of the graph a few
  dozen times per insert. Each loop now makes one NumPy call over an index
  array, selection uses one pairwise matrix, and extension is off by default
  as in the reference implementation. On the reference machine 320 vectors
  build in 1.6s instead of 160s, and 3,000 in 30s. Recall against brute force
  is pinned by a test.
- **`create_app()` can be called more than once.** Route decorators were
  applied to the module-level singleton, so a second application came back
  with middleware and the dashboard mount but no endpoints. Routes register on
  an `APIRouter` that `create_app()` includes.
- **The LLM and hybrid extractors could not be constructed.** The pipeline
  passed `entity_types=` and `relationship_types=` to two constructors that
  do not accept them, so `ExtractorType.LLM` and `HYBRID` raised `TypeError`
  before any document was seen. Both read those settings from the config
  they are given. mypy found it.
- **`DeltaLakeStorage` could not be instantiated.** It never implemented the
  abstract `scan` and `flush`, so construction raised `TypeError`. Its
  `get()`, `iterate()` and `update()` also named a column `embedding` that the
  schema this class creates calls `dense_embedding`, and never returned the
  sparse or late-interaction columns, so hybrid and ultimate search over this
  backend could not have worked. All three now read the columns the schema
  has. Verified against a recording cursor, not a live warehouse.
- **`get()` and `similar()` raised on every call.** Both passed a list under
  `ids=` to `Collection.get`, which takes one `id`, so `db.get("x")` was a
  `TypeError` and `similar()` could never find its starting document.
- **A graph with no entities raised on every query.** `_rebuild_searchers`
  returned early on an empty graph, but `add_documents()` marked the pipeline
  built regardless, so a corpus that extracted nothing was "built" with no
  searchers and every search raised `Hybrid searcher not initialized`. The
  one-line API caught it and fell back to vector results, so the raise and
  catch happened on every single query and nothing was visible. This is easy
  to reach: spaCy is not a dependency, so the default extractor falls back to a
  regex that only matches capitalised phrases, and any lowercase corpus
  extracts no entities at all. An empty graph now behaves like an empty
  collection and returns an empty result, whether nothing was added yet or the
  documents produced no entities. Each searcher already handled an empty graph
  correctly; only the pipeline refused to build them.

- **A graph with no communities was indistinguishable from no graph.** A corpus
  below `min_community_size` yields entities and no communities, which is an
  ordinary state rather than a broken one, and it is what the first run holds in
  memory. Loading `None` for it made a reopened graph behave unlike the run that
  built it, and the pipeline gates its searchers on that value. Loading now
  returns an empty hierarchy when entities exist, and `None` only when the store
  is genuinely empty.

- **Storage backends no longer swallow failures.** Thirty-six bare `except:`
  clauses returned `None`, `[]`, `0` or `False` when a backend read failed,
  making an unreachable database indistinguishable from an empty collection.
  Reads now raise `StorageOperationError`, while genuine absence still returns
  the empty value. `LakebaseStorage.scan` was the worst case: it caught every
  error and returned, silently truncating iteration mid-stream.
- **Adding the same text twice crashed.** Document ids are derived from
  content, so a repeated sentence produced a repeated key, and usearch rejects
  duplicates with a bare `RuntimeError: Duplicate keys not allowed in high-level
  wrappers`. Indexing any corpus containing a repeat failed outright. A repeat
  add is now an upsert, which is what content-addressed ids imply.
- **`geo_radius` silently returned too few documents.** The candidate pre-filter
  derived a geohash prefix length from `log10(radius)`, which bears no relation
  to a geohash cell's size: a 600 km radius narrowed to a 156 km cell, so a
  point 504 km from the centre was never even considered. The obvious repair was
  unavailable because `get_geohash_neighbors` returns the parent prefix rather
  than the eight surrounding cells. The query now scans and filters on exact
  haversine distance; a correct linear pass beats a fast wrong answer for
  something whose job is deciding which documents exist.
- **`StringIndex` `contains` behaved as `equals`.** The query was padded with
  the same boundary markers as stored values, so its trigrams were anchored at
  both ends: searching `"gre"` could never match `"green"`. It only appeared to
  work when the query happened to be the whole value. Substring search now
  matches anywhere, and queries shorter than a trigram fall back to a scan
  instead of silently returning nothing.
- **The SQLite backend is now actually thread-safe.** Connections were created
  with `check_same_thread=False` and shared between threads, but the lock was
  only held while looking a connection up, not while using it. Concurrent writers
  raised `InterfaceError: bad parameter or other API misuse`. Each thread now
  gets its own connection, which is what SQLite's WAL mode is designed for.
- **`SyncStatus.lag_seconds` was wrong by the machine's UTC offset.** It
  subtracted a UTC timestamp from `datetime.now()`, which returns local time.
- **The Delta Lake backend wrote local-time timestamps** into the same columns
  every other backend filled with UTC.
- **`__version__` said `2.1.0` while the package was `2.1.7`.** It is now read
  from installed package metadata, so it cannot drift from `pyproject.toml`.
- Replaced 35 calls to the deprecated `datetime.utcnow()`. Timestamps are now
  timezone-aware; `vectrixdb._time.parse_iso` reads the naive timestamps written
  by earlier versions and treats them as UTC, so existing data still loads.

### Known limitations

- `NativeHNSWIndex` is pure Python and NumPy, for reading the graph algorithm
  rather than for use: fit for about a hundred vectors. `Collection` indexes
  with usearch. See `docs/explanation/hnsw.md`.
- **The Delta Lake backend has not been run against a live warehouse in this
  release.** Its statements are verified by a recording cursor.
- **The bundled model is the weaker one for conversation memory.**
  bge-small-en-v1.5 was chosen on SciFact retrieval, where it beats
  e5-small-v2 by six points of nDCG@10. On LongMemEval, with the same code
  over the same 150 questions, it scores 0.721 evidence recall against
  e5-small-v2's 0.797, all of the difference in questions whose evidence is
  spread across sessions. LoCoMo, whose answers sit in one or two turns of a
  single conversation, is half a point apart either way. The conversation memory and limits pages say so,
  and `dense_model="e5-small"` opens a collection with the older model. A
  collection that carries both, `dense_model=["bge-small", "e5-small"]`,
  scores 0.814, most of the gain in those same questions (0.677 against
  0.494 and 0.642), at twice the embedding and the disk. The bge-small
  figures are `benchmarks/memory_longmemeval.json` and the two-model ones
  `benchmarks/memory_longmemeval_both.json`; the e5-small-v2 run (0.797, and
  0.642 multi-session) was measured before `memory_bench.py` wrote its model
  into the file and that file was not kept, so
  `python scripts/memory_bench.py longmemeval --limit 150 --dense-model e5-small --json ...`
  is how to measure it again.
  `scripts/memory_bench.py` takes `--dense-model`, once or twice, refuses to
  quote a number for two models from a collection that does not carry two,
  and writes the models into the results it saves.
- **Two builds of the same vectors are not the same index, and an INT8
  vector depends a little on its batch.** usearch builds on every core, so on
  SciFact five builds of the same vectors scored 0.704 to 0.708 nDCG@10, where
  a one-thread build matches exact search every time; and the bundled INT8
  models scale activations per batch, so a text embedded beside different
  texts comes out at cosine 0.997 to itself embedded alone.
  Both were measured while refreshing the benchmarks and are on the limits
  page with what to do about them.
- Every limit that still holds is on one page, `docs/explanation/limits.md`,
  kept current: this list is only what is new or particular to the release.

## [2.1.7] and earlier

Released before this changelog was kept. See the
[release history](https://github.com/knowusuboaky/VectrixDB/releases).
