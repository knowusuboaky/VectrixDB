# Wrap it for your company

A platform team can hand its people one package: VectrixDB already set up
for the company's server, its sign-in and its network, under the company's
own name. People install `acme-vectors`, type `acme-vectors login`, and
search. Nobody copies a server address, a client id, a certificate or a
gateway key from a wiki page.

The wrapper is thin on purpose. It depends on one tested version of
VectrixDB and adds only what is the company's: where the server is and how
to reach it. Every call, every refusal and every command is VectrixDB's own,
so the wrapper needs no work when VectrixDB adds a call, only a version bump.

To publish the wrapper on the company's own registry and make it the one
people install, see [Distribute it through JFrog Artifactory](artifactory.md).

## What a wrapper sets

| Default | What it does | Example |
| --- | --- | --- |
| `server` | Where a call or a command goes when no address is given | `https://vectors.acme.com` |
| `client_id` | The command line's app registration, for `login` | `0a1b2c3d-...` |
| `scope` | The scopes `login` asks for, when not the server's own | `api://acme-vectors/search offline_access` |
| `ca_file` | The company's certificate authority, or `system` | `system` |
| `key_header` | The header a key goes in, when a gateway renames it | `x-acme-key` |
| `headers` | Headers every request to the server carries; `${NAME}` reads a variable | `{"Ocp-Apim-Subscription-Key": "${ACME_APIM_KEY}"}` |
| `user_agent` | The wrapper's name and version, in front of VectrixDB's | `acme-vectors/1.0.0` |
| `command` | The wrapper's command, so every hint names it | `acme-vectors` |
| `credentials` | Where `login` keeps a sign-in: `keyring` or `file` | `keyring` |

Two rules hold whatever the defaults say:

- **The company's options go to the company's server only.** The headers,
  the certificate authority and the key header are sent with calls to
  `server` (the same scheme, host and port) and to no other address. A call
  to a partner's server gets none of them, so a gateway's subscription key
  never leaves for a host it was not meant for.
- **No secret is written in the defaults.** A header's value names an
  environment variable, `${ACME_APIM_KEY}`, read when the call is made. A
  variable that is not set is named in the error, never its value.

What wins, last to first: the wrapper's defaults, then the machine's
defaults file, then the person's environment (`VECTRIXDB_URL`,
`VECTRIXDB_CA_FILE` and the rest), then what a call or a command is given.
So a person can point at staging with `VECTRIXDB_URL` without uninstalling
anything.

!!! note "Defaults make it convenient. They enforce nothing."
    A person who installs plain `vectrixdb` and types the address reaches the
    same server. Who may do what is decided there: by
    [sign-in](sign-in.md), [roles and scoped keys](keys-and-roles.md),
    [collection policies](collection-policies.md), and the
    [gateway](behind-a-gateway.md) in front of it. Make the wrapper the easy
    way and the server the strict one.

## Python: the command line and the client

A complete wrapper is four files.

```text
acme-vectors/
  pyproject.toml
  src/acme_vectors/__init__.py
  src/acme_vectors/defaults.py
  tests/test_defaults.py
```

### pyproject.toml

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "acme-vectors"
version = "1.0.0"
description = "Acme's VectrixDB: the company's server, sign-in and network, set up."
requires-python = ">=3.9"
# One version of VectrixDB, the one the platform team tested.
dependencies = ["vectrixdb[client]==2.2.0"]

[project.optional-dependencies]
# Scripts and services sign in as the machine they run on.
azure = ["azure-identity>=1.15"]

[project.scripts]
# The whole vectrixdb command line, under the company's name.
acme-vectors = "vectrixdb.cli:app"

[project.entry-points."vectrixdb.defaults"]
acme = "acme_vectors.defaults:DEFAULTS"

[tool.setuptools.packages.find]
where = ["src"]
```

Two lines do the work. `[project.scripts]` installs the `acme-vectors`
command, which is VectrixDB's own command line: every command, option and
`--help` is there. The `vectrixdb.defaults` entry point is how VectrixDB
finds the company's defaults; installing the package is all it takes.

### defaults.py

```python
"""Where Acme's VectrixDB is, and how to reach it. No secret lives here."""

DEFAULTS = {
    "server": "https://vectors.acme.example",
    # The command line's app registration: a public client with the device code flow on.
    "client_id": "00000000-0000-0000-0000-000000000000",
    "scope": "api://acme-vectors/search offline_access",
    # Laptops trust Acme's certificate authority through the operating system.
    "ca_file": "system",
    # The API gateway's subscription key, read from the person's environment.
    "headers": {"Ocp-Apim-Subscription-Key": "${ACME_APIM_KEY}"},
    "user_agent": "acme-vectors/1.0.0",
    "command": "acme-vectors",
}
```

A field VectrixDB does not know is refused by name the first time it is
read, so a typo is found by the first person who runs a command, not by
the hundredth who wonders why nothing works.

`DEFAULTS` may also be a function that returns the mapping, for defaults
worked out when they are read: a server per region, say.

### \_\_init\_\_.py

```python
"""Acme's VectrixDB, from Python.

    import acme_vectors

    db = acme_vectors.connect(collection="handbook")
    for r in db.search("how long do refunds take", limit=5):
        print(r.readable_citation, r.text)
"""

from typing import Any, Optional

import vectrixdb
from vectrixdb.cli_remote import caller
from vectrixdb.exceptions import (  # noqa: F401 - one import for a program's error handling
    ServerBusy,
    ServerNotFound,
    ServerPermissionDenied,
    ServerRefused,
    ServerSignInRequired,
)

from .defaults import DEFAULTS

__version__ = "1.0.0"


def connect(collection: Optional[str] = None, **options: Any) -> Any:
    """Acme's server, as the person signed in with acme-vectors login, or as VECTRIXDB_KEY or VECTRIXDB_TOKEN.

    The server, its headers and its certificate authority come from the
    defaults; anything given here wins.
    """
    if "key" not in options and "token" not in options:
        options.update(caller(DEFAULTS["server"]))
    return vectrixdb.connect(collection=collection, **options)


def connect_as_this_machine(collection: Optional[str] = None, **options: Any) -> Any:
    """For a service or a scheduled job: a token for the machine's managed identity, renewed as it runs out."""
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()
    scope = "api://acme-vectors/.default"
    return vectrixdb.connect(
        collection=collection, token=lambda: credential.get_token(scope).token, **options
    )
```

`vectrixdb.connect()` with no address goes to the company's server and
brings its headers and certificate authority; the wrapper only says who is
calling. `caller` is the command line's own answer to that question: the
`--key-file`, `VECTRIXDB_KEY` or `VECTRIXDB_TOKEN` if set, else the sign-in
`acme-vectors login` kept, renewed as it runs out. So a notebook uses the
same sign-in as the terminal next to it.

A service has no person to sign in. `connect_as_this_machine` asks the
machine's managed identity for a token instead, through `azure-identity`;
on AWS or anywhere else, any function that returns a fresh token does.

### Test it as it is installed

```python
"""The wrapper is installed the way people get it, and VectrixDB finds its defaults."""

from vectrixdb import company


def test_vectrixdb_finds_the_defaults():
    found = company.load()
    assert found.server == "https://vectors.acme.example"
    assert found.program == "acme-vectors"
    assert found.came_from["server"] == "acme (package)"


def test_the_gateway_key_goes_to_acme_only(monkeypatch):
    monkeypatch.setenv("ACME_APIM_KEY", "test-subscription")
    found = company.load()
    assert found.for_server("https://vectors.acme.example")["headers"] == {
        "Ocp-Apim-Subscription-Key": "test-subscription"
    }
    assert found.for_server("https://elsewhere.example") == {}
```

```bash
pip install -e ".[azure]"
pytest
acme-vectors --help
```

Entry points exist only for an installed package, so the test runs after
`pip install`, not from a bare checkout.

### What people see

```console
$ pip install acme-vectors
$ acme-vectors login
Open https://microsoft.com/devicelogin and enter the code WDJB-MJHT
Server: https://vectors.acme.example
Signed in as: ama@acme.example
Role: viewer
Kept in the system keychain.

$ acme-vectors query "how long do refunds take" --name handbook
server https://vectors.acme.example
```

Every hint names the wrapper's command: a sign-in that ran out says
`acme-vectors login`, not `vectrixdb login`. Everything in
[Use the command line against a server](command-line.md) holds, under the
wrapper's name.

### More than one wrapper on a machine

A person who works for two companies, or a team with a staging wrapper, can
have two installed. VectrixDB then refuses to guess and says which are
there; `VECTRIXDB_DEFAULTS=acme` picks one by its entry point name.

## Without a package: a file on every machine

When the machines are managed, an administrator can skip the package and put
the same defaults in a file that every machine reads:

| System | File |
| --- | --- |
| Linux | `/etc/vectrixdb/defaults.toml` |
| macOS | `/Library/Application Support/vectrixdb/defaults.toml` |
| Windows | `%ProgramData%\vectrixdb\defaults.toml` |

```toml
[vectrixdb]
server = "https://vectors.acme.example"
client_id = "00000000-0000-0000-0000-000000000000"
ca_file = "system"
command = "vectrixdb"

[vectrixdb.headers]
Ocp-Apim-Subscription-Key = "${ACME_APIM_KEY}"
```

Ship it with Intune, Jamf, Group Policy, Ansible or whatever already ships
settings. `VECTRIXDB_DEFAULTS_FILE` names another file, for a container or a
CI runner; a `.json` file is read as JSON. On Python 3.9 and 3.10, TOML needs
`pip install tomli`, or write the file as JSON.

The machine's file wins over a wrapper package where both set a field, so
an administrator can point one region's machines at that region's server
without a second wrapper. Headers from both are kept, the file's winning
for a name both set.

## TypeScript

A package of a few lines, published to the company's npm registry:

```json
{
  "name": "@acme/vectors",
  "version": "1.0.0",
  "type": "module",
  "main": "dist/index.js",
  "types": "dist/index.d.ts",
  "dependencies": { "vectrixdb": "2.2.0" }
}
```

```ts
import { connect as vectrix, type ConnectOptions } from "vectrixdb";

export * from "vectrixdb";

export const SERVER = "https://vectors.acme.example";

/** Acme's server, with the gateway's key and the wrapper's name on every request. */
export function connect(options: ConnectOptions = {}) {
  const key = process.env.ACME_APIM_KEY;
  if (!key) throw new Error("ACME_APIM_KEY is not set: ask the platform team for a subscription key");
  return vectrix(SERVER, {
    ...options,
    headers: { "Ocp-Apim-Subscription-Key": key, ...options.headers },
    userAgent: "acme-vectors-js/1.0.0",
  });
}
```

A program then writes `connect({ token }).collection("handbook")`. In a
browser, a token from the company's sign-in library, never a key, and no
subscription key either: put the browser's calls through a back end that
holds it.

Node trusts the company's certificate authority through
`NODE_EXTRA_CA_CERTS`, or the operating system's store with
`--use-system-ca` on Node 23.8 and later.

## Go

```go
// Package vectors is Acme's VectrixDB.
package vectors

import (
	"errors"
	"os"

	vectrixdb "github.com/knowusuboaky/VectrixDB/sdk/go/v2"
)

// Server is Acme's VectrixDB.
const Server = "https://vectors.acme.example"

// Connect is Acme's server, with the gateway's key and the wrapper's name on
// every request. Options given here come after, so they win.
func Connect(options ...vectrixdb.Option) (*vectrixdb.Client, error) {
	key := os.Getenv("ACME_APIM_KEY")
	if key == "" {
		return nil, errors.New("ACME_APIM_KEY is not set: ask the platform team for a subscription key")
	}
	acme := []vectrixdb.Option{
		vectrixdb.WithHeader("Ocp-Apim-Subscription-Key", key),
		vectrixdb.WithUserAgent("acme-vectors-go/1.0.0"),
	}
	return vectrixdb.Connect(Server, append(acme, options...)...)
}
```

```text
module git.acme.example/platform/vectors

go 1.22

require github.com/knowusuboaky/VectrixDB/sdk/go/v2 v2.2.0
```

Go trusts the operating system's store on Windows and macOS, and
`SSL_CERT_FILE` on Linux.

## Rust

```rust
//! Acme's VectrixDB.

pub use vectrixdb::*;

/// Acme's server.
pub const SERVER: &str = "https://vectors.acme.example";

/// A builder for Acme's server, with the gateway's key and the wrapper's
/// name already on it. Add the caller and build.
pub fn client() -> Result<ClientBuilder, Error> {
    let key = std::env::var("ACME_APIM_KEY").map_err(|_| {
        Error::Invalid("ACME_APIM_KEY is not set: ask the platform team for a subscription key".into())
    })?;
    Ok(Client::connect(SERVER)
        .header("Ocp-Apim-Subscription-Key", key)
        .user_agent("acme-vectors-rs/1.0.0"))
}
```

```text
[package]
name = "acme-vectors"
version = "1.0.0"
edition = "2021"
publish = ["acme"]

[dependencies]
vectrixdb = "=2.2.0"
```

`publish = ["acme"]` keeps the crate off crates.io: it can only go to the
company's registry. For the company's certificate authority, build a
`reqwest::Client` with `add_root_certificate` and pass it with `.http(...)`.

## Keep it thin

- **Pin one version of VectrixDB**, the one you tested, and bump it on
  purpose. Renovate or Dependabot can open the bump against your registry.
- **Add nothing VectrixDB already has.** Re-export its errors and calls; a
  call of your own drifts from the server the first time it changes.
- **Put no secret in the package.** Not a key, not a subscription key, not a
  client secret: everyone who can install the package can read it. The
  command line's app registration is a public client for this reason.
- **Version the wrapper on its own**, and say in its changelog which
  VectrixDB version each release carries.
- **Test it installed.** Entry points and console scripts only exist once
  the package is installed; the tests above run after `pip install`.

Reference: [vectrixdb.company](../reference/company.md).
