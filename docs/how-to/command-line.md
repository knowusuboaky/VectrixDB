# Use the command line against a server

The `vectrixdb` commands that work on collections run on the data on this
machine, or on a server. Give them `--server`, or set `VECTRIXDB_URL`, and
they call that server's REST API as you: your role, your collections and
each collection's policy decide what comes back, as they do for the dashboard
and for an assistant.

```bash
pip install "vectrixdb[client]"
vectrixdb login --server https://vectors.company.com
vectrixdb query "how long do refunds take" --name handbook --server https://vectors.company.com
```

Every command that goes to a server prints `server <address>` on stderr
first, so a command never reaches a server without saying so.

## The commands that take a server

| Command | On a server |
| --- | --- |
| `list` | The collections you can reach. `--json` for a script |
| `create NAME DIMENSION` | Makes a collection; needs a role that may |
| `delete NAME` | Asks first, naming the server; `--force` to skip |
| `ingest FILES...` | Sends each file; the server reads, cuts and embeds it. A new collection is made |
| `query TEXT` | `--mode` is `hybrid`, `dense`, `keyword` or `rerank`; `--json` for a script |
| `stats` | The collection's size, dimension, metric and filterable fields |
| `sources add`, `sources list`, `sources refresh` | The feeds and pages a collection on the server keeps up with |
| `whoami` | Who the server says you are, your role, what it allows, the collections you reach |

`--parents` and `--explain` on `query`, and the options of `sources add`
that only a local collection keeps, are refused with `--server` rather than
quietly left out. `serve`, `check`, `doctor`, `keys` and `people` act on this
machine's server and its settings, and take no `--server`.

## Who you are to the server

The command line looks for a caller in this order, and uses the first it finds:

1. `--key-file`: a file holding a key, its first line. `-` reads it from stdin.
2. `VECTRIXDB_KEY`: a key in the environment, for a script or a CI job.
3. `VECTRIXDB_TOKEN`: an access token from your identity provider, sent as
   a bearer token.
4. What `vectrixdb login` kept for that server.

A command that calls a server never takes a key as an argument. An argument
is written to your shell's history and shown to anyone on the machine who
lists processes, and a key there would outlive the session it was meant for.

## Sign in with the company

```console
$ vectrixdb login --server https://vectors.company.com --client-id 0a1b2c3d-...
Open https://microsoft.com/devicelogin and enter the code WDJB-MJHT
Server: https://vectors.company.com
Signed in as: ama@company.com
By: oidc
Role: viewer
Collections: handbook, policies
Kept in the system keychain.
```

`login` asks the server which identity provider signs its people in (the
server's `/.well-known/oauth-protected-resource` document), asks that
provider for a code, and waits while you enter the code in a browser. The
browser can be on another machine, so this works on a server reached over
SSH, in a container, or anywhere without a browser of its own. The sign-in is
the one the company already has: its passwords, its multi-factor rules, its
conditional access.

The provider gives an access token and a refresh token. Each command sends
the access token. When it is a minute from running out, the next command
renews it with the refresh token and keeps the new one, so a sign-in lasts as
long as the company's policy allows. When it can no longer be renewed, the
command says so and names `vectrixdb login`.

### What the identity provider needs

One application registration for the command line, made once by whoever runs
the identity provider:

- **A public client**: the command line keeps no client secret, because a
  secret shipped to every laptop is not a secret.
- **The device code flow allowed.** In Microsoft Entra ID, *Allow public
  client flows* on the registration. In Okta, the *Device Authorization*
  grant on the app. Any OpenID Connect provider whose discovery document has
  a `device_authorization_endpoint` works.
- **Permission to ask for the server's scope**, the one the server names in
  `VECTRIXDB_MCP_SCOPES` (the same scope an assistant over MCP asks for), and
  `offline_access` for the refresh token.

Its client id goes to everyone as `--client-id`, or once in
`VECTRIXDB_CLIENT_ID`. It is not a secret.

The server needs company sign-in turned on with an API audience, as in
[Company sign-in for MCP](mcp-sign-in.md). A server that takes keys only
says so when you try, and names the way to sign in with a key.

## Sign in with a key

```bash
vectrixdb login --server https://vectors.company.com --key-file ~/.config/vectrixdb-key
```

The key is tried first (`whoami` must answer) and kept only when the server
takes it. A key from a secret manager goes through stdin, so it never lands
in a file:

```bash
vault kv get -field=key secret/vectrixdb | vectrixdb login --server https://vectors.company.com --key-file -
```

On Linux and macOS, a key file that other users may read is refused, with
the `chmod 600` that fixes it.

## Where a sign-in is kept

| Where | When |
| --- | --- |
| The system keychain: Windows Credential Manager, macOS Keychain, the Secret Service on Linux | The `keyring` package is installed (`pip install keyring`) |
| A file only you may read: `credentials.json` in `%APPDATA%\vectrixdb`, `~/Library/Application Support/vectrixdb` or `~/.config/vectrixdb` | Otherwise |

`VECTRIXDB_CREDENTIALS=keyring` or `file` chooses, and refuses to fall back
when the keychain it names cannot be reached. With the keychain, the file
holds only which servers you signed in to, never a key or a token. Without
it, the file is created readable by you alone, in a folder only you may open.

`vectrixdb whoami` shows who you are to a server. `vectrixdb logout` forgets
the sign-in for `--server`, or for the last server you signed in to.

## On a company network

The command line works where the company's other tools work:

- **Proxies.** `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY` are honoured, as
  every command line tool honours them.
- **The company's certificate authority.** A server with a certificate from
  the company's own authority, or a proxy that inspects TLS, needs that
  authority trusted. `VECTRIXDB_CA_FILE` names a PEM file or a folder of
  them. `VECTRIXDB_CA_FILE=system` trusts what the operating system trusts,
  which is where a managed laptop's company certificates already are.
  `SSL_CERT_FILE` is honoured too.
- **No plain http.** A key or a token goes over `https://`, or over
  `http://` to this machine only. To another machine over `http://`, the
  command stops before sending anything. On a network you trust, such as a
  private cluster network, `VECTRIXDB_ALLOW_HTTP=1` lifts that.
- **No redirects.** A server that answers with a redirect is reported, not
  followed, so a key is never sent to a second host.

=== "Linux and macOS"

    ```bash
    export VECTRIXDB_URL=https://vectors.company.com
    export VECTRIXDB_CA_FILE=system
    vectrixdb whoami
    ```

=== "Windows PowerShell"

    ```powershell
    $env:VECTRIXDB_URL = "https://vectors.company.com"
    $env:VECTRIXDB_CA_FILE = "system"
    vectrixdb whoami
    ```

## In a script or a CI job

Give the job a key of its own, scoped to the collections it needs, from the
CI's secret store into `VECTRIXDB_KEY`. Make one with
[`vectrixdb keys add`](keys-and-roles.md).

```yaml
- name: Index the handbook
  env:
    VECTRIXDB_URL: https://vectors.company.com
    VECTRIXDB_KEY: ${{ secrets.VECTRIXDB_HANDBOOK_KEY }}
  run: vectrixdb ingest docs/handbook --name handbook
```

| Exit code | Means |
| --- | --- |
| 0 | Done |
| 1 | The server refused, could not be reached, or a file failed to ingest. The line on stderr says which |
| 2 | The command could not start: no address, a key file refused, plain http to another machine, a certificate file that is not there |

A refusal is one line with the server's own words and its HTTP status, never
a stack trace. `--json` on `list`, `query` and `whoami` prints JSON alone on
stdout. The `server` line and any errors go to stderr.

## Every setting

| Setting | What it does |
| --- | --- |
| `VECTRIXDB_URL` | The server, when `--server` is left out |
| `VECTRIXDB_KEY` | A key, for scripts |
| `VECTRIXDB_TOKEN` | An access token, when there is no key |
| `VECTRIXDB_CLIENT_ID` | The company's client id for the command line |
| `VECTRIXDB_CA_FILE` | The company's certificate authority, or `system` |
| `VECTRIXDB_ALLOW_HTTP` | `1` lets a key cross a network over plain http |
| `VECTRIXDB_CREDENTIALS` | `keyring` or `file` |
| `VECTRIXDB_CONFIG_DIR` | The folder the credentials file is in |

The same rules hold in Python: `vectrixdb.connect` refuses plain http to
another machine, takes `verify="system"` or a certificate file, and never
follows a redirect. See [Call a server from your code](clients.md).
