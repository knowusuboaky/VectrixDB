# Deploy the server

Everything a server starts with is an environment variable. This page keeps
them in one file, checks the file before a start, and hands the same file to
every command, so the server and the commands run beside it always agree on
where the data is and who may sign in.

## One file of settings

```bash
vectrixdb check --template > vectrixdb.env
```

The template has every setting VectrixDB reads, grouped: the server, keys for
scripts, sign-in, single sign-on, email, the brand, documents, audit and
evaluation, storage, and the cache. Each has a line saying what it does, and
each starts commented out, so the file changes nothing until you uncomment a
line. Secrets are left empty. Put them in the platform's secret store and name
the file it mounts with the setting's `_FILE` twin, or have the platform set
them in the environment.

A line is `NAME=value`. `#` starts a comment, a leading `export` is allowed,
and a value may be in single or double quotes. What the environment already
sets wins over the file, so a secret the platform injects is never replaced by
a stale line in it. A line that is not `NAME=value` stops the command with
the file's name and the line's number, and a name VectrixDB does not read is
a warning from the check below. Every setting, with what it does, is also in
the [settings reference](../reference/settings.md).

## Check it before a start

```bash
vectrixdb check --env-file vectrixdb.env
```

```text
Settings for ./vectrixdb_data, from vectrixdb.env and the environment

  ok     Storage      vectrixdb_data does not exist yet, and will be made at the first start
  ok     Sign-in      email, for https://vectors.example.com
  ok     Sign-in      Sign-in state is kept in SQLite file vectrixdb_data/auth/signin.db
  ok     Access log   Written to the server's output, for the platform to collect
  ok     Brand        Acme
  warn   Settings     VECTRIXDB_CORS_ORIGIN is not a setting VectrixDB reads. Did you mean VECTRIXDB_CORS_ORIGINS?
  warn   Email        No mail server, so sign-in links are not sent. Make each one on the server: vectrixdb people reset <address>
  warn   Sign-in      No admin yet, and the sign-in file is made at the first start. Then add the first: vectrixdb people add you@company.com --role admin

No errors, 3 warnings. Ready to start.
```

It reads the settings through the server's own code, so an error is what a
start would refuse, or what would break once it runs: a sign-in secret that is
too short, a secret given both inline and as a file, `*` for the allowed
origins while sign-in is on, a number that is not one, a storage backend
missing its address, an access log that cannot be written. A warning is what
would start and then surprise you, like a misspelt name, which does nothing at
all. Errors come last, and the command exits `1` while there is one, so it can
gate a deployment pipeline.

## Every command reads the same file

```bash
vectrixdb serve --env-file vectrixdb.env --host 0.0.0.0
vectrixdb people add ada@example.com --role admin --env-file vectrixdb.env
vectrixdb evaluate policies --golden golden.jsonl --env-file vectrixdb.env
```

Every command that opens the data or the list of people takes `--env-file`,
and every one finds the data the same way: `--path` when given, else
`VECTRIXDB_PATH`, else `./vectrixdb_data`. So the admin added on the server's
machine is added to the list that server reads, and an evaluation run is saved
where its Evaluate page looks, which is `VECTRIXDB_EVALUATIONS` when that is
set.

## Logs the platform keeps

App Service, Container Apps, ECS and Kubernetes keep what a server writes to
its output, and a file on a container's disk goes with the container. Send the
access log there:

```bash
VECTRIXDB_ACCESS_LOG=stdout
```

Each line is marked `"log": "vectrixdb.access"`, so a query in the platform's
log viewer finds them among everything else. The server keeps no copy, and
its Access page says where to read it instead. See
[Sign people in](sign-in.md#the-access-log) for what a line holds.

## What the browser is held to

The dashboard runs no inline script, and its security policy refuses all of
it. The one script from another host is the chart library, from
`cdnjs.cloudflare.com`, pinned by its hash, so a copy changed on the CDN is not
run. Two things follow for a deployment:

- **A proxy must not add script to the page.** Some corporate proxies and
  monitoring agents inject an inline script into every page they pass on. The
  policy refuses it and the dashboard works on without it.
- **Where the CDN is blocked, only the graph goes.** The Graph tab says the
  library did not load. Every other page is served by the server itself.

Behind Azure API Management, AWS API Gateway or any reverse proxy, two more
settings matter: the path the app is served under, and whose
`X-Forwarded-For` says who is calling. See
[Put it behind a gateway](behind-a-gateway.md).

Calls from other sites need their origins named in `VECTRIXDB_CORS_ORIGINS`,
and with sign-in on, `*` is refused at the start. To show the dashboard inside a
portal, name the portal in `VECTRIXDB_FRAME_ANCESTORS`. The rest of what every
reply carries is in [Sign people in](sign-in.md#what-every-reply-carries).
