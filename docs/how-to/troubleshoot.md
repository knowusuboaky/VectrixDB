# Check and doctor

Two commands find what is wrong with an install before a person does.

| Command | What it does | Changes anything |
| --- | --- | --- |
| `vectrixdb check` | Reads the settings through the server's own code and says what a start would refuse, and what would start and then surprise you | No |
| `vectrixdb check --url` | Asks a running server, from outside, the questions a caller would, through whatever gateway stands in front of it | No: every request is a GET |
| `vectrixdb doctor` | Runs the check, then tries each part for real: the data folder, the models, the readers and every service the settings name | No |

[Deploy the server](deploy.md) shows each in its place in a deployment. This page
is what each one looks at, and what to do about what it finds.

## vectrixdb check

```bash
vectrixdb check --env-file vectrixdb.env
```

| Option | What it does |
| --- | --- |
| `--env-file` | A settings file to read first. What the environment already sets wins over it |
| `--path`, `-d` | The data folder. Default `VECTRIXDB_PATH`, else `./vectrixdb_data` |
| `--template` | Prints a settings file with every setting in it, commented out, to fill in, and stops |
| `--url` | Checks a running server from outside instead; see [below](#check-a-deployed-server) |
| `--prefix`, `--gateway-paths` | With `--url`: the server's `VECTRIXDB_PREFIX` and `VECTRIXDB_GATEWAY_PATHS`, when this machine does not have them |

Each finding is `ok`, `warn` or `error`, with the part of the server it is about.
An error is what a start would refuse, or what would fail the first request
that needs it. A warning is a setting that works and is probably not what was
meant. Errors are listed last, and the command exits `1` while there is one,
so it can stop a pipeline.

It writes nothing. A folder is asked whether it can be written, never written
to. The sign-in store is opened only when it already exists, to see whether an
admin has been added. The machine is asked what it has: each reader's package
is imported, ffmpeg is run with `-version`, and the speech model is looked for
in its cache, never fetched.

| Area | What it reads |
| --- | --- |
| Settings | Every `VECTRIXDB_` name, against the ones VectrixDB reads; a secret given inline and as a file; numbers; the variables Kubernetes sets for a Service named `vectrixdb` |
| Storage, Cache | The storage backend and what it needs, whether the data folder can be written, the chunk store, the collection records and the cache |
| Access, Sign-in, Email | Keys, sign-in, single sign-on, who may sign in, whether there is an admin, the mail server, the access log |
| Browsers, Gateway | Allowed origins, the path the app is served under, gateway paths, trusted proxies |
| Brand | The name, logo and colours |
| Extraction | Who reads each kind of file this server is sent, and an extraction service's settings |
| Masking | The masking engine and what it needs |
| Audit, Evaluation | Where search decisions and evaluation runs go |

## vectrixdb doctor

```bash
vectrixdb doctor --env-file vectrixdb.env
```

| Option | What it does |
| --- | --- |
| `--env-file`, `--path` | As for `check` |
| `--offline` | Asks no service over the network. `VECTRIXDB_OFFLINE=1` does the same |
| `--quick` | Leaves the models unloaded, for a fast run |
| `--json` | Prints the result as JSON |

The check's warnings and errors come first, then each part tried:

| Area | What it tries |
| --- | --- |
| Install | The VectrixDB and Python versions. Writes a file to the data folder and removes it, and warns under 1 GB free |
| Models | Loads the English embedding model and embeds a word, loads the reranker and scores a pair, and times each. Says which other models are installed: the sparse model, the multilingual late-interaction model, the graph extraction model |
| Readers | Reads a small Markdown, HTML and text document |
| Server | Whether the `api` extra is installed; the sign-in packages when `VECTRIXDB_SIGNIN` is set; `mcp` 2 or later when `VECTRIXDB_MCP` is on; the OpenTelemetry SDK when tracing asks for it |
| Documents | The extraction service's `/health`, with its key |
| Sign-in | The identity provider's description, that it calls itself the issuer you set, its signing keys, and that a client id is set |
| Email | A connection to the mail server, with nothing sent |
| Storage | The storage backend's service, and the sign-in store, audit store, evaluation runs and graph store when they are on a service |
| Services | The chat models that draft golden questions and describe pictures, Azure OpenAI, and Azure AI Language for masking |
| Tracing | A connection to the trace collector |

A diagnosis is `ok`, `--` for something this install does not have, with how to
add it, `warn` or `error`. Each warning and error has a line under it saying
what to do. A service is asked a GET, or for a connection with nothing sent. A
key is never printed, and an address shows only its host, since a database
address can hold a password. It exits `1` while there is an error.

`--json` prints the counts, `healthy`, and every diagnosis:

```text
{
  "counts": {"ok": 6, "skip": 3, "warn": 6, "error": 0},
  "healthy": true,
  "diagnoses": [
    {"level": "ok", "area": "Install", "text": "VectrixDB 2.2.0 on Python 3.12.7", "fix": ""},
    ...
  ]
}
```

## Check a deployed server

```bash
vectrixdb check --url https://apim.company.com/vectrixdb
```

Most of what goes wrong behind a gateway is not in the settings at all. So this
asks the public address what a caller would, and says which answer is not the
server's:

| It asks | It finds |
| --- | --- |
| `GET /health` | Whether the address reaches the server at all |
| Each gateway path | Whether each path the gateway publishes reaches the server, and answers in its words |
| `GET /openapi.json` | Whether the document is served, and says the API is under the path callers use |
| A wrong key, in the key header and as `Authorization: Bearer` | Whether the header reaches the server, and its refusal comes back whole |
| `GET /dashboard/`, then `/dashboard` | Whether the dashboard is served, and a redirect keeps the gateway's path |
| `GET /health` from another origin | Whether the gateway and the server both add CORS headers |

The key it sends is wrong on purpose, to see whose refusal comes back. A
gateway that publishes each part of the server under a path of its own is
asked the same way, each route where it is published. See
[Put it behind a gateway](behind-a-gateway.md).

## Common findings

| Finding | Command | What to do |
| --- | --- | --- |
| `X is not a setting VectrixDB reads. Did you mean Y?` | check | Fix the name. A misspelt setting does nothing at all |
| `X and X_FILE are both set` | check | Keep one, so there is no doubt which is meant |
| `X_FILE names ..., which is not a file here` | check | Mount the secret where the setting says, or fix the path |
| `X is tcp://..., which Kubernetes sets for a Service named vectrixdb-...` | check | `enableServiceLinks: false` on the pod, or rename the Service |
| `cosmosdb needs VECTRIXDB_COSMOS_ENDPOINT, and VECTRIXDB_COSMOS_KEY` | check | Set what the storage backend needs; each backend names its own |
| `sign-in is on, so VECTRIXDB_SIGNIN_SECRET is needed` | check | A secret of 32 characters or more, the same on every restart |
| `Single sign-on is on and nobody is named who may sign in` | check | `VECTRIXDB_SIGNIN_USERS=you@company.com:admin`, `vectrixdb people add`, or `VECTRIXDB_OIDC_ALLOWED_EMAILS` |
| `No admin yet` | check | `vectrixdb people add you@company.com --role admin` on the server |
| `No mail server, so sign-in links are not sent` | check | `VECTRIXDB_SMTP_URL`, or make each link with `vectrixdb people reset <address>` |
| `VECTRIXDB_CORS_ORIGINS is * while sign-in is on` | check | Name the origins: `https://host`, no path |
| `... cannot be written, and a read that cannot be recorded is refused` | check | `VECTRIXDB_ACCESS_LOG=stdout` on a read-only disk |
| `No VECTRIXDB_TRUSTED_PROXIES` | check | Name the proxy's addresses, such as `10.0.0.0/8`, or one person's failed sign-ins lock everybody out |
| `Nothing reads pictures here` and the like | check | Install the extra it names, or send that kind of file to an extraction service; see [Install what you need](install.md) |
| `... is installed and does not import` | check | The package is there and broken here, usually a missing system library; the reason is in the finding |
| `The default embedding model does not load` | doctor | `pip install --force-reinstall vectrixdb`, or `vectrixdb download-models --type dense` |
| `The reranker does not load` | doctor | `vectrixdb download-models --type reranker`. Searches without reranking still work |
| `... cannot be written` under Install | doctor | Give the user the server runs as write access, or point `VECTRIXDB_PATH` somewhere it has |
| `VECTRIXDB_SIGNIN is ..., and the sign-in packages are not installed` | doctor | `pip install "vectrixdb[signin]"` |
| `VECTRIXDB_MCP is on and the mcp package, version 2 or later, is not installed` | doctor | `pip install -U "vectrixdb[mcp]"` |
| `... cannot be reached: the name does not resolve` | doctor | Check the address, and that this machine can reach it: a firewall, a private endpoint, a proxy |
| `... answers 500: it is there and failing` | doctor | Read that service's own logs |
| `The provider calls itself ..., not VECTRIXDB_OIDC_ISSUER` | doctor | Set the issuer to what the provider says. Entra's `common` and `organizations` are shared addresses: use your tenant's |
| `VECTRIXDB_OIDC_CLIENT_ID is not set` | doctor | The application's id at the provider |
| `GET /health answered 404, and not as the server does` | check --url | The gateway does not publish the path, or does not send it on to the server |
| `the header is being stripped on the way in` | check --url | Have the gateway pass the key header, or `Authorization`, through |
| `refused with 401, but not in the server's words` | check --url | The gateway answered, or rewrites error bodies; let the server's replies through |
| `/openapi.json does not say the API is under ...` | check --url | `VECTRIXDB_PUBLIC_URL` set to the address callers use, or `VECTRIXDB_PREFIX` the same as the gateway's |
| `redirects to ..., which has lost ...` | check --url | `VECTRIXDB_PUBLIC_URL` set to the address callers use |
| `two Access-Control-Allow-Origin values came back` | check --url | Keep CORS in the gateway and leave `VECTRIXDB_CORS_ORIGINS` unset |

For a server that restarts, slows down or answers `503` under load, see
[Size the server for load](sizing.md).

## From Python

Both run from code, for a test or a health page of your own:

```python
from vectrixdb.check import run as check
from vectrixdb.doctor import run as diagnose, summary

for finding in check("./vectrixdb_data"):
    if finding.level == "error":
        print(finding.area, finding.text)

found = diagnose("./vectrixdb_data", offline=True, quick=True)
report = summary(found)
print(report["healthy"], report["counts"])
for d in found:
    if d.level in ("warn", "error"):
        print(d.level, d.area, d.text, d.fix)
```

Each reads the settings from the environment. `vectrixdb.probe.probe(url)`
does what `check --url` does and returns the same findings.
