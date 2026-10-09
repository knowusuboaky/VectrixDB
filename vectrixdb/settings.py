"""Every setting the server reads, in one place: what it is, whether it is a secret, and a file to fill in.

The server is configured from the environment, one variable a setting. This
module names them all. ``vectrixdb check`` reads an environment against it,
so a name nothing reads is caught as the typo it almost always is, and
``vectrixdb check --template`` prints :func:`template`, a file to fill in and
keep beside the deployment with every secret left empty.

An env file is ``NAME=value`` lines: blank lines and ``#`` comments are
skipped, ``export`` in front of a name is allowed, and a value in matching
quotes has them taken off. :func:`read_env_file` reads one, and
``vectrixdb serve --env-file`` and ``vectrixdb check --env-file`` start from
it. What the real environment already sets wins over the file, the way
Docker's and dotenv's env files work, so a platform's own settings are never
overridden by a file somebody copied.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, MutableMapping, Optional, Tuple, Union

from .exceptions import ConfigurationError

__all__ = ["SETTINGS", "Setting", "apply_env_file", "known", "read_env_file", "template", "unknown"]


# ============================================================================
# ONE SETTING, AND THE TABLE
# ============================================================================
#
# INPUT   a name, a group, and what it does
# OUTPUT  one environment variable as the pages list it; the table of every
#         setting the server reads
#
# One place, so the reference page, the template and vectrixdb check cannot
# disagree.


@dataclass(frozen=True)
class Setting:
    """One environment variable: its name, the group it is listed in, and what it does."""

    name: str
    group: str
    means: str
    example: str = ""
    secret: bool = False
    #: Also read from the file ``<name>_FILE`` names, for secrets mounted as files.
    file_twin: bool = False


def _s(group: str, rows: Iterable[Tuple]) -> List[Setting]:
    out = []
    for row in rows:
        name, means, *rest = row
        example = rest[0] if rest else ""
        flags = set(rest[1:]) if len(rest) > 1 else set()
        # "bare": the name as another service's own tooling writes it, with no
        # VECTRIXDB_ prefix, because the code reads it that way.
        full = name if "bare" in flags else "VECTRIXDB_" + name
        out.append(Setting(full, group, means, example, "secret" in flags, "file" in flags))
    return out


SETTINGS: Tuple[Setting, ...] = tuple(
    _s(
        "The server",
        [
            (
                "PATH",
                "Where the collections are kept: what every command uses when --path is left out.",
                "./vectrixdb_data",
            ),
            (
                "PUBLIC_URL",
                "The address people type. Sign-in redirects to it and emails link to it.",
                "https://vectors.company.com",
            ),
            (
                "ROOT_PATH",
                "The path a gateway serves this app under. Left out, the path of PUBLIC_URL.",
                "/vectrixdb",
            ),
            (
                "PREFIX",
                "The path every route lives under, inside the gateway's paths: acme serves /acme/api/v1/... Answered with or without it.",
                "acme",
            ),
            (
                "GATEWAY_PATHS",
                "Each route's own gateway path, as the gateway team hands them over: route=path, comma separated. A route or a family of them; each answers with its path or without, and a name no route falls under stops the start.",
                "api/v1=/files/search,auth=/files/auth",
            ),
            ("KEY_HEADER", "The header an app's key arrives in.", "api-key"),
            (
                "TOKEN_HEADER",
                "The header a person's access token arrives in. Named otherwise, Authorization is left to the gateway.",
                "Authorization",
            ),
            (
                "TRUSTED_PROXIES",
                "Addresses and networks whose X-Forwarded-For says who is calling. None unless set, so the header is ignored.",
                "10.0.0.0/8",
            ),
            ("DASHBOARD", "0 turns the dashboard off. On unless set.", "1"),
            (
                "CORS_ORIGINS",
                "Other sites that may call the API from a browser, comma separated. With sign-in on, name them; * is refused.",
                "",
            ),
            (
                "FRAME_ANCESTORS",
                "https origins that may show the dashboard in a frame, separated by spaces. None unless set.",
                "",
            ),
            (
                "ALLOW_OPEN",
                "1 lets serve listen beyond this machine with no key and no sign-in, for a server behind a gateway that asks.",
                "",
            ),
            (
                "LISTEN_PORT",
                "The port serve listens on when --port is left out. The container image sets it to 7337. Not VECTRIXDB_PORT, which Kubernetes sets in every pod for a Service named vectrixdb.",
                "7337",
            ),
            ("MAX_UPLOAD_BYTES", "The largest document the server reads, in bytes.", "104857600"),
            ("OFFLINE", "1 refuses every download: the bundled models only.", ""),
            ("MODELS_DIR", "Where models are kept, when not beside the package.", ""),
            ("AUTO_DOWNLOAD", "1 fetches a missing model on first use.", ""),
            (
                "MODELS_URL",
                "Where model downloads come from instead of GitHub's releases: a company's mirror of them, such as an Artifactory generic remote repository. The same paths below it: <tag>/<model>.zip.",
                "https://acme.jfrog.io/artifactory/vectrixdb-models/knowusuboaky/VectrixDB/releases/download",
            ),
            (
                "WARM",
                "1 loads the embedding model at start, and /ready says so once it has. On under "
                "vectrixdb serve and in the image; 0 loads it on the first search.",
                "1",
            ),
            (
                "THREADS",
                "Threads one model session uses. Left out, the CPUs this process may use, the "
                "container's quota when there is one, at most 4.",
                "2",
            ),
            (
                "INFERENCE_CONCURRENCY",
                "Model calls the server runs at once; the rest wait their turn, a batch at a time.",
                "2",
            ),
            (
                "INFERENCE_WAIT_SECONDS",
                "How long a request waits for its turn before it is answered 503 with Retry-After.",
                "30",
            ),
        ],
    )
    + _s(
        "Keys for scripts",
        [
            (
                "API_KEY",
                "The full key: every action, as an admin, for scripts. Sign-in is for people.",
                "",
                "secret",
                "file",
            ),
            (
                "API_KEY_SHA256",
                "The full key given as its SHA-256, so the key itself is never on the server.",
                "",
            ),
            ("READ_ONLY_API_KEY", "A key that reads and never writes.", "", "secret", "file"),
            ("READ_ONLY_API_KEY_SHA256", "The read-only key as its SHA-256.", ""),
            (
                "OPEN_READS",
                "With a key and no sign-in: 0 makes every read ask for the full or the read-only key, the dashboard's live feed too. On unless set, so a read needs no key.",
                "1",
            ),
        ],
    )
    + _s(
        "Assistants over MCP",
        [
            (
                "MCP",
                "1 answers MCP at /mcp: an assistant searches as the person or key it acts for, through the same checks as the REST API. Off unless set.",
                "",
            ),
            (
                "MCP_WRITES",
                "1 offers the tools that write over MCP (add_document, create_collection, delete_document, add_source, refresh_source), to a caller whose role may. Off unless set, so an assistant only reads.",
                "",
            ),
            (
                "MCP_SCOPES",
                "The scopes an MCP client asks the identity provider for, separated by spaces. Left out, <OIDC_API_AUDIENCE>/.default when the audience is an api:// one.",
                "api://vectrixdb/search",
            ),
        ],
    )
    + _s(
        "Sign-in",
        [
            (
                "SIGNIN",
                "oidc, email, or oidc,email. Off unless set. With oidc nobody keeps a passkey: those are for email alone. oidc with no provider named yet signs the People list in with a code by email until one is.",
                "email",
            ),
            (
                "SIGNIN_SECRET",
                "32 characters or more. While rotating, two, comma separated, newest first.",
                "",
                "secret",
                "file",
            ),
            (
                "SIGNIN_USERS",
                "People to add to the People list if missing, address:role, comma separated. Changes nobody already there. With single sign-on on, the list says who may sign in and with what role.",
                "ada@company.com:admin",
            ),
            (
                "SIGNIN_PASSWORDS",
                "on: the email way in asks for a password and a code together, never a password alone.",
                "",
            ),
            (
                "SIGNIN_REQUIRE",
                "passkey: people on the email list sign in with a passkey only. For email alone, not beside oidc.",
                "",
            ),
            ("SESSION_HOURS", "How long a sign-in lasts.", "8"),
            ("SESSION_IDLE_MINUTES", "Signed out after this long unused.", "120"),
            ("COOKIE_SAMESITE", "strict or lax. Strict, or lax while single sign-on is on.", ""),
            (
                "ADMINS_USE_SSO",
                "on: an admin signs in with single sign-on only. The server console stays the way back in.",
                "",
            ),
            (
                "GUESTS",
                "on: people who have not signed in may look around: the collections and how the setups scored. Searching needs a sign-in.",
                "",
            ),
            (
                "KEY_REQUESTS_PER_MINUTE",
                "Requests a minute for a named API key that has no number of its own. Empty: no limit.",
                "",
            ),
            (
                "AUTH_PATH",
                "The folder for the sign-in file and the access log. Default <path>/auth.",
                "",
            ),
            (
                "SIGNIN_STORE",
                "Where sign-in state is kept: a SQLite file by default, or postgresql://, cosmos:// or dynamodb://.",
                "",
                "file",
            ),
            (
                "SIGNIN_STORE_KEY",
                "That database's password or account key. Never in the address.",
                "",
                "secret",
                "file",
            ),
            (
                "ACCESS_LOG",
                "Where sign-ins and reads are recorded: a file; stdout for a platform that collects the server's output; or https://<account>.blob.core.windows.net/<container>/<prefix>, append blobs every server shares, in a container under an immutability policy. Default <path>/auth/access.jsonl.",
                "",
            ),
        ],
    )
    + _s(
        "Single sign-on",
        [
            (
                "OIDC_ISSUER",
                "The identity provider's issuer.",
                "https://login.microsoftonline.com/<tenant>/v2.0",
            ),
            ("OIDC_CLIENT_ID", "The application's client id at the provider.", ""),
            (
                "OIDC_CLIENT_SECRET",
                "The client secret. Left out for a public client; PKCE is always used.",
                "",
                "secret",
                "file",
            ),
            (
                "OIDC_CLIENT_KEY",
                "A private key (PEM) the server signs in to the provider with, in place of a client secret.",
                "",
                "secret",
                "file",
            ),
            (
                "OIDC_CLIENT_CERT",
                "Its certificate (PEM): Entra ID finds the key by it.",
                "",
                "file",
            ),
            ("OIDC_CLIENT_KEY_ID", "The key's id, for Okta and Keycloak.", ""),
            ("OIDC_SCOPES", "The scopes asked for.", "openid profile email"),
            ("OIDC_GROUPS_CLAIM", "The claim that holds a person's groups.", "groups"),
            ("OIDC_ROLE_MAP", "JSON, group to role.", '{"<group id>": "admin"}'),
            ("OIDC_DEFAULT_ROLE", "The role of somebody in no mapped group. Unset: refused.", ""),
            ("OIDC_PRINCIPAL_CLAIMS", "JSON, policy attribute to claim.", ""),
            ("OIDC_GRANT_MAP", "JSON, group to what its members are given by name.", ""),
            ("OIDC_GROUPS_URL", "Where to fetch groups that did not fit in the token.", ""),
            ("OIDC_LABEL", "The words on the sign-in button.", "Continue with SSO"),
            (
                "OIDC_ALLOWED_EMAILS",
                "Addresses that may sign in beside the People list, which single sign-on also needs: an address or @domain, comma separated. * alone: the groups decide on their own.",
                "",
            ),
            (
                "OIDC_API_AUDIENCE",
                "The audience of an access token taken as a Bearer token, so an app acts as the person using it: api://vectrixdb. Empty: no token is taken.",
                "",
            ),
            (
                "OIDC_TOKEN_ROLE",
                "The role an app's access token is given, whoever it is for: reader, searcher, viewer or operator. Unset: their groups'. A token never needs the People list.",
                "searcher",
            ),
            (
                "SSO_RECHECK_DAYS",
                "With oidc,email: a passkey or a code works only for somebody who signed in with single sign-on within this many days.",
                "30",
            ),
        ],
    )
    + _s(
        "Developer Access",
        [
            (
                "DEVELOPER_ACCESS",
                "on: a username and a password for trying the roles on this machine. Refused unless PUBLIC_URL is this machine's; answers only this machine.",
                "",
            ),
            (
                "DEVELOPER_USERS",
                "Its accounts, name:role, comma separated.",
                "admin.user:admin,viewer.user:viewer",
            ),
            ("DEVELOPER_PASSWORD", "Their password. There is no default.", "", "secret", "file"),
        ],
    )
    + _s(
        "Emergency sign-in",
        [
            (
                "BREAK_GLASS",
                "on: emergency sign-in at /dashboard/#/break-glass, for while the usual sign-in is down, with any VECTRIXDB_SIGNIN. Nothing links to it. Off, its address answers 404.",
                "",
            ),
            (
                "BREAK_GLASS_UNTIL",
                "When it turns itself off, in UTC. Needed while it is on; its sessions end then too.",
                "2026-09-28T02:00Z",
            ),
            (
                "BREAK_GLASS_ADMIN",
                "The one username it takes. It signs in as an admin, recorded as break_glass_used.",
                "emergency.admin",
            ),
            (
                "BREAK_GLASS_PASSWORD_HASH",
                "The hash of its password, from vectrixdb break-glass hash. The password itself stays in a key vault, and works for one emergency.",
                "",
                "secret",
                "file",
            ),
        ],
    )
    + _s(
        "Email",
        [
            (
                "SMTP_URL",
                "The mail server sign-in links are sent through: smtp://user:password@host:587 or smtps://host:465.",
                "",
                "secret",
                "file",
            ),
            ("MAIL_FROM", "The address sign-in emails come from.", ""),
        ],
    )
    + _s(
        "Brand",
        [
            (
                "BRAND_NAME",
                "Your name, shown where VectrixDB's would be. 40 characters or fewer.",
                "",
            ),
            (
                "BRAND_LOGO",
                "A square logo: SVG, PNG or JPEG, as a file or a data: address. An SVG that could run anything is refused.",
                "",
            ),
            ("BRAND_LOGO_DARK", "The same logo for the dark theme. Optional.", ""),
            (
                "BRAND_ACCENT",
                "One colour, #rrggbb. VectrixDB works out the tint, the text shade and the ink on it.",
                "",
            ),
            ("BRAND_WORDMARK", "on: the name is set as a wordmark, larger and in the accent.", ""),
            (
                "BRAND_COPYRIGHT",
                "Whose the line under the sidebar names: Northwind reads © 2026 Northwind.",
                "Northwind",
            ),
            (
                "BRAND_PALETTE",
                "The grounds, inks and lines for each theme, as JSON or a JSON file. Every ink must read 4.5 to 1 on its grounds.",
                '{"light": {"page": "#f4f6f4"}}',
            ),
        ],
    )
    + _s(
        "Documents",
        [
            ("EXTRACTOR_URL", "A service that reads file types the server does not.", ""),
            (
                "EXTRACTOR_ROUTES",
                "JSON, file suffix to route on that service.",
                '{".pdf": "/extract/pdf"}',
            ),
            ("EXTRACTOR_BODY", "raw or multipart.", "raw"),
            ("EXTRACTOR_KEY", "The key the service asks for.", "", "secret", "file"),
            ("EXTRACTOR_KEY_HEADER", "The header that key goes in.", "x-api-key"),
            ("EXTRACTOR_TIMEOUT", "Seconds to wait for the service.", "300"),
            (
                "EXTRACTOR_RETRIES",
                "How many more times to ask the service after a timeout, a connection error or a 408, 429, 502, 503 or 504, waiting longer each time with jitter; Retry-After is honoured.",
                "3",
            ),
            (
                "EXTRACTOR_MASK",
                "Ask the service to mask identifiers as it reads, so the index never holds them: 1 for the identifiers, all, or the types comma separated. Empty: not asked. What was masked is kept with the document, as counts.",
                "",
            ),
            (
                "KEEP_SOURCE",
                "1 keeps each document's Markdown beside its collection, so it can be opened and cut again. A folder, s3://bucket/prefix or a Blob address serves what another process keeps there, one folder a collection.",
                "",
            ),
        ],
    )
    + _s(
        "Extraction service",
        [
            (
                "EXTRACT_LISTEN_PORT",
                "The port extract-serve listens on when --port is left out. The container image sets it to 7338.",
                "7338",
            ),
            (
                "EXTRACT_PREFIX",
                "The path every route lives under, the deployment's choice: /api gives /api/extract/pdf.",
                "",
            ),
            (
                "EXTRACT_GATEWAY_PATHS",
                "Comma separated route=path: the path a gateway publishes each endpoint under, as its team hands them over.",
                "",
            ),
            (
                "EXTRACT_URL_HOSTS",
                "Comma separated: the only hosts its address routes may fetch from; *.example.com for subdomains.",
                "",
            ),
            (
                "EXTRACT_JOBS",
                "memory, or azure for a job record in a container and the work on a queue.",
                "memory",
            ),
            ("EXTRACT_OUTPUT", "Where memory jobs write what they make.", "./output"),
            (
                "EXTRACT_STORAGE",
                "Azure jobs: a storage connection string, or an account URL for the managed identity.",
                "",
                "secret",
            ),
            (
                "EXTRACT_CONTAINER",
                "Azure jobs: the container records and results go in.",
                "extraction",
            ),
            ("EXTRACT_QUEUE", "Azure jobs: the queue the work waits on.", "extract-jobs"),
            (
                "EXTRACT_PDF",
                "How a PDF is read: text, its own text layer with OCR for scanned pages only; vision, the same with the pages the rules are unsure of (charts, big figures set apart, parts drawn out of order) read by a chat model that can see them and held to each page's own words; or layout, Document Intelligence's layout model, for tables as rows, headings, the numbers printed on the pages and charts cut out to describe. vision is paid for the pages it reads, layout for every page.",
                "text",
            ),
            (
                "VISION_PAGES",
                "With EXTRACT_PDF=vision: hard, the pages the rules are unsure of; or all, every page with words on it.",
                "hard",
            ),
            (
                "SPEECH_LOCALES",
                "Comma separated: the languages a recording may be in, when a call names none. Azure Speech hears which one it is.",
                "en-US,fr-CA",
            ),
            (
                "SPEECH_SPEAKERS",
                "Up to how many voices Azure Speech tells apart, each phrase starting Speaker 1:, Speaker 2:. 0 asks for none.",
                "4",
            ),
            (
                "VIDEO_FRAMES",
                "The most frames of a video read for what is on screen, taken where the picture changes and spread over its length, each read by the picture reader. 0 reads the sound alone.",
                "12",
            ),
            (
                "DESCRIBER_URL",
                "A chat model that can see, asked first to describe pictures in detail: its chat completions address. AZURE_OPENAI_VISION_DEPLOYMENT with AZURE_OPENAI_ENDPOINT does the same for Azure OpenAI.",
                "",
            ),
            ("DESCRIBER_MODEL", "The model to ask, for a service that serves more than one.", ""),
            ("DESCRIBER_KEY", "The key that service asks for.", "", "secret"),
            (
                "DESCRIBER_KEY_HEADER",
                "The header the key goes in; Authorization sends it as a Bearer token.",
                "Authorization",
            ),
        ],
    )
    + _s(
        "Masking",
        [
            (
                "MASKING_ENGINE",
                "How identifiers in a document are found before it is indexed, and by /mask on the extraction service: auto, regex, presidio, language or comprehend. auto picks Azure AI Language when AZURE_LANGUAGE_ENDPOINT is set, with AZURE_LANGUAGE_KEY or the managed identity, else Presidio when it is installed, else the patterns. comprehend is Amazon's, used when named, with AWS_REGION. The patterns run after every engine.",
                "auto",
            ),
            (
                "MASKING_LANGUAGES",
                "The languages the documents are in, comma separated: what Presidio loads a model for, and what an engine is asked to cover.",
                "en,fr",
            ),
            # Azure AI Language, by the names its own tooling writes, with no prefix.
            (
                "AZURE_LANGUAGE_ENDPOINT",
                "The Azure AI Language resource that finds names, addresses and national ids: https://<name>.cognitiveservices.azure.com. Set, auto picks it.",
                "",
                "bare",
            ),
            (
                "AZURE_LANGUAGE_KEY",
                "Its key. Left empty, the managed identity is used.",
                "",
                "secret",
                "file",
                "bare",
            ),
        ],
    )
    + _s(
        "Sources",
        [
            (
                "SOURCES_HOSTS",
                "Comma separated: the only hosts the feeds and pages a collection keeps up with may be fetched from, redirects included; *.example.com for subdomains. Unset, any public host.",
                "",
            ),
            (
                "SOURCES_INTERNAL_HOSTS",
                "Comma separated: intranet hosts a source may fetch although they resolve to private addresses. Link-local addresses, where cloud metadata services answer, stay refused for them too.",
                "",
            ),
        ],
    )
    + _s(
        "Audit and evaluation",
        [
            (
                "AUDIT_STORE",
                "Where search decisions under a policy are recorded: a file; https://<account>.blob.core.windows.net/<container>/<prefix>, append blobs in a container under an immutability policy; s3://<bucket>/<prefix>, under Object Lock; or postgresql://, a table written with INSERT only.",
                "",
                "file",
            ),
            (
                "AUDIT_RETAIN_DAYS",
                "How many days S3 must refuse to change each record. Only S3 is told this per record, and it has no default.",
                "",
            ),
            (
                "AUDIT_JSONL",
                "The older name for AUDIT_STORE when it is a file, one line a decision.",
                "",
            ),
            (
                "AUDIT_QUERY_KEY",
                "The key queries are hashed with in that file. Derived from the sign-in secret when unset.",
                "",
                "secret",
            ),
            (
                "EVALUATIONS",
                "Where evaluation runs are read from: a folder, s3://bucket/prefix or a Blob address. Default <path>/evaluations.",
                "",
            ),
            (
                "GRAPH_STORE",
                "Where each collection's knowledge graph is kept for every server: a folder, s3://bucket/prefix or a Blob address, one <collection>/graph.json each. Default <path>/graph.",
                "",
            ),
            (
                "WRITER_URL",
                "A chat model that drafts golden questions: its chat completions address. AZURE_OPENAI_WRITER_DEPLOYMENT with AZURE_OPENAI_ENDPOINT does the same for Azure OpenAI.",
                "",
            ),
            ("WRITER_MODEL", "The model to ask, for a service that serves more than one.", ""),
            ("WRITER_KEY", "The key that service asks for.", "", "secret"),
            (
                "WRITER_KEY_HEADER",
                "The header the key goes in; Authorization sends it as a Bearer token.",
                "Authorization",
            ),
            (
                "TRACING",
                "1 sends a span for every search, ingestion and evaluation run to OpenTelemetry; 0 keeps it off even with an endpoint set. Off unless set or OTEL_EXPORTER_OTLP_ENDPOINT is. A span carries counts and timings, never query or document text. Needs vectrixdb[tracing].",
                "",
            ),
            (
                "OTEL_EXPORTER_OTLP_ENDPOINT",
                "Where spans go, an OTLP/HTTP collector: Jaeger, Grafana Tempo, Honeycomb, Datadog, Azure Monitor's collector. Setting it turns tracing on.",
                "http://localhost:4318",
                "bare",
            ),
            (
                "OTEL_SERVICE_NAME",
                "The service name spans carry.",
                "vectrixdb",
                "bare",
            ),
        ],
    )
    + _s(
        "Storage",
        [
            (
                "STORAGE_BACKEND",
                "sqlite, memory, azure_search, cosmosdb, lakebase or delta_lake.",
                "sqlite",
            ),
            ("COSMOS_ENDPOINT", "The Cosmos DB account address.", ""),
            ("COSMOS_KEY", "Its key.", "", "secret"),
            ("COSMOS_DATABASE", "The database.", "vectrixdb"),
            ("LAKEBASE_HOST", "The Lakebase host.", ""),
            ("LAKEBASE_PORT", "Its port.", "5432"),
            ("LAKEBASE_DATABASE", "The database.", "vectrixdb"),
            ("LAKEBASE_USER", "The user.", ""),
            ("LAKEBASE_PASSWORD", "The password.", "", "secret"),
            ("LAKEBASE_TOKEN", "A token, in place of a password.", "", "secret"),
            ("LAKEBASE_SSL", "true or false.", "true"),
            ("DELTA_WORKSPACE_URL", "The Databricks workspace address.", ""),
            ("DELTA_TOKEN", "Its token.", "", "secret"),
            ("DELTA_CATALOG", "The catalog.", "main"),
            ("DELTA_SCHEMA", "The schema.", "vectrixdb"),
            ("DELTA_WAREHOUSE_ID", "The SQL warehouse.", ""),
            ("DELTA_HTTP_PATH", "The warehouse's HTTP path, in place of its id.", ""),
            # Azure AI Search. Each of these also answers to its name without the
            # VECTRIXDB_ prefix, AZURE_SEARCH_ENDPOINT and so on, because that is
            # what Azure's own tooling writes and what a host that configured the
            # service for ingestion already has. Configuring it twice, differently
            # spelled, is how an API ends up serving an empty database.
            (
                "AZURE_SEARCH_ENDPOINT",
                "The search service, like https://<service>.search.windows.net.",
                "",
            ),
            (
                "AZURE_SEARCH_KEY",
                "An admin key. Left empty, the managed identity is used.",
                "",
                "secret",
            ),
            ("AZURE_SEARCH_INDEX_PREFIX", "Index names are <prefix>-<collection>.", "vectrix"),
            (
                "AZURE_SEARCH_SEMANTIC",
                "true adds Azure's semantic ranker, on a tier that has it.",
                "false",
            ),
            (
                "AZURE_SEARCH_EMBEDDINGS",
                "vectrixdb, azure, or both vectors in one index.",
                "vectrixdb",
            ),
            (
                "AZURE_SEARCH_FILTER_FIELDS",
                "JSON: metadata paths to promote to filterable fields.",
                "",
            ),
            ("AZURE_SEARCH_VECTOR_WEIGHTS", "JSON: how much each vector counts in the fusion.", ""),
            ("AZURE_OPENAI_ENDPOINT", "The Azure OpenAI account behind the second vector.", ""),
            (
                "AZURE_OPENAI_EMBED_DEPLOYMENT",
                "Its embedding deployment. Naming it asks for both vectors.",
                "",
            ),
            ("AZURE_OPENAI_KEY", "Its key, when it is not keyless.", "", "secret"),
            # Not a backend: one small store beside whichever backend is chosen.
            (
                "PARENT_STORE",
                "Where parent sections live: a path, or cosmos://<account>.documents.azure.com/<db>/<container>.",
                "",
            ),
            # Nor this: a copy of every chunk, for the pages, never searched.
            (
                "CHUNK_STORE",
                "Where the collection pages read every chunk from when more than one process writes: cosmos://<account>.documents.azure.com/<db>/<container>. Left out, the table beside this process.",
                "",
            ),
            (
                "CHUNK_STORE_KEY",
                "That Cosmos account's key. Left empty, the managed identity is used.",
                "",
                "secret",
                "file",
            ),
            # Nor this: each collection's policy, visibility and masking, one record each, read by every server.
            (
                "COLLECTION_STORE",
                "Where every collection's record is kept for every server: who may retrieve from it, who may see it, and masking. A path, or postgresql://, cosmos:// or dynamodb://. Left out, nothing is gated and collections are served as they always were.",
                "",
                "file",
            ),
            (
                "COLLECTION_STORE_KEY",
                "That database's password or account key. Never in the address. Left empty, a cloud store uses the managed identity.",
                "",
                "secret",
                "file",
            ),
        ],
    )
    + _s(
        "Cache and memory",
        [
            ("CACHE_BACKEND", "memory, redis, hybrid or none.", "memory"),
            ("REDIS_HOST", "The Redis host.", "localhost"),
            ("REDIS_PORT", "Its port.", "6379"),
            ("REDIS_PASSWORD", "Its password.", "", "secret"),
            ("REDIS_SSL", "true or false.", "false"),
            (
                "SCALING_STRATEGY",
                "none, or how indexes are moved out of memory under pressure.",
                "none",
            ),
            ("MAX_MEMORY_PERCENT", "The share of memory the server tries to stay under.", "85"),
            (
                "BUILD_THREADS",
                "Threads that insert into the vector index. 1 makes every build of the same vectors the same graph, and is slower; empty is every core.",
                "",
            ),
        ],
    )
    + _s(
        "The command line, against a server",
        [
            (
                "URL",
                "The server list, create, delete, ingest, query, stats, sources and whoami go to when --server is left out. Left out too, they use the data on this machine.",
                "https://vectors.company.com",
            ),
            (
                "KEY",
                "The key those commands call the server with, for scripts and CI. Wins over what vectrixdb login kept; --key-file wins over it.",
                "",
                "secret",
            ),
            (
                "TOKEN",
                "A person's or an app's access token from the identity provider, sent as a bearer token, when there is no key.",
                "",
                "secret",
            ),
            (
                "CA_FILE",
                "The company's certificate authority, as a PEM file or a folder of them, or system for the operating system's own store: for a server or a TLS-inspecting proxy the default bundle does not trust.",
                "system",
            ),
            (
                "ALLOW_HTTP",
                "1 lets a key or token go to another machine over plain http://. Off: only https://, or http:// to this machine.",
                "",
            ),
            (
                "CLIENT_ID",
                "The company's client id for the command line at its identity provider: a public client with the device code flow allowed. vectrixdb login uses it.",
                "",
            ),
            (
                "CREDENTIALS",
                "Where vectrixdb login keeps a sign-in: keyring for the system keychain, file for a file only this user may read. Left out, the keychain when the keyring package is installed.",
                "",
            ),
            (
                "DEFAULTS_FILE",
                "A company's defaults file for the client and the command line (TOML, or JSON by its suffix). Left out, /etc/vectrixdb/defaults.toml, %ProgramData%\\vectrixdb\\defaults.toml or /Library/Application Support/vectrixdb/defaults.toml when there is one.",
                "",
            ),
            (
                "DEFAULTS",
                "With more than one wrapper package installed, the one whose defaults apply, by its entry point name.",
                "",
            ),
            (
                "CONFIG_DIR",
                "The folder that file is in. Left out, the platform's own: %APPDATA%\\vectrixdb, ~/Library/Application Support/vectrixdb, or ~/.config/vectrixdb.",
                "",
            ),
        ],
    )
)


# ============================================================================
# KNOWN, UNKNOWN, AND THE ENV FILE
# ============================================================================
#
# INPUT   names; a path; the environment
# OUTPUT  every name the server reads, _FILE twins included; the VECTRIXDB_
#         names nothing reads, each with the setting it most likely meant;
#         NAME=value lines as a mapping, a bad line a ConfigurationError
#         naming it; the file read into the environment, what it already sets
#         winning; a template with every setting, grouped, commented and empty
#         where it is a secret
#
# The environment wins over the file, so a deployment's own settings are never
# overwritten by a file that shipped with the code.

_NAMES = frozenset(s.name for s in SETTINGS) | frozenset(
    s.name + "_FILE" for s in SETTINGS if s.file_twin
)
_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

#: What Kubernetes puts in every pod for each Service in its namespace, unless
#: the pod says ``enableServiceLinks: false``: a Service named vectrixdb gives
#: VECTRIXDB_SERVICE_HOST, VECTRIXDB_PORT=tcp://..., VECTRIXDB_PORT_7337_TCP
#: and more. They are not settings, so they are not reported as misspelt ones.
SERVICE_LINK = re.compile(
    r"^[A-Z0-9_]+_(SERVICE_HOST|SERVICE_PORT(_[A-Z0-9_]+)?"
    r"|PORT(_[0-9]+_(TCP|UDP|SCTP)(_(ADDR|PORT|PROTO))?)?)$"
)


def known() -> frozenset:
    """Every name the server reads, the ``_FILE`` twins included."""
    return _NAMES


def unknown(names: Iterable[str]) -> List[Tuple[str, Optional[str]]]:
    """The ``VECTRIXDB_`` names among these that nothing reads, each with the setting it most likely meant."""
    out = []
    for name in sorted(set(names)):
        if name.startswith("VECTRIXDB_") and name not in _NAMES and not SERVICE_LINK.match(name):
            near = difflib.get_close_matches(name, sorted(_NAMES), n=1, cutoff=0.8)
            out.append((name, near[0] if near else None))
    return out


def read_env_file(path: Union[str, Path]) -> Dict[str, str]:
    """``NAME=value`` lines from a file, as a mapping. A line that is not one is a ConfigurationError naming it."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigurationError(f"The env file {source} cannot be read: {exc}") from exc
    out: Dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            raise ConfigurationError(f"{source}, line {number}: not NAME=value")
        name, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[name] = value
    return out


def apply_env_file(
    path: Union[str, Path], environ: Optional[MutableMapping[str, str]] = None
) -> Dict[str, str]:
    """Read an env file into the environment. What the environment already sets wins; returns what was taken from the file."""
    target = os.environ if environ is None else environ
    taken = {}
    for name, value in read_env_file(path).items():
        if name not in target:
            target[name] = value
            taken[name] = value
    return taken


def template() -> str:
    """An env file with every setting, grouped, commented and empty where it is a secret."""
    lines = [
        "# VectrixDB server settings. Fill in what you need; a setting left empty or commented out keeps its default.",
        "# Check it before a start:  vectrixdb check --env-file vectrixdb.env",
        "# Start with it:            vectrixdb serve --env-file vectrixdb.env --host 0.0.0.0",
        "# Secrets are left empty here. One that says so may instead be given as NAME_FILE, naming a file that holds it.",
    ]
    group = None
    for setting in SETTINGS:
        if setting.group != group:
            group = setting.group
            lines += ["", f"# ---- {group}"]
        note = [setting.means]
        if setting.secret:
            note.append("A secret.")
        if setting.file_twin:
            note.append(f"Or {setting.name}_FILE, naming a file that holds it.")
        lines.append("# " + " ".join(note))
        lines.append(f"# {setting.name}={'' if setting.secret else setting.example}")
    return "\n".join(lines) + "\n"
