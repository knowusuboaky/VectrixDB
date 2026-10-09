# Ship your own client or command on it

A company that runs VectrixDB for its people often wants them to type
`acme vectors query ...` or `acme_vectors.connect()`, with the address, the
gateway and the sign-in already filled in. Each of the four clients and the
`vectrixdb` command is made to be wrapped like that: a wrapper presets what
is the same for everyone at the company and passes the rest through, so it
stays a few dozen lines and every safety rule underneath still holds.

## What the licence asks

VectrixDB is [Apache-2.0](https://github.com/knowusuboaky/VectrixDB/blob/main/LICENSE).
A wrapper may be private or public, free or sold, and may use any licence of
its own. What it owes:

- **A wrapper that depends on the package** (`vectrixdb>=2.2,<3` in its own
  requirements) ships nothing of ours, and owes nothing beyond what the
  package manager already fetches with it.
- **A wrapper that bundles it** (a container image, a single-file binary, a
  vendored copy) ships `LICENSE` and `NOTICE` with it, and says what it
  changed in any file it changed.
- **The name is not part of the licence.** "Acme Vectors, built on
  VectrixDB" says where it comes from; calling the wrapper VectrixDB, or
  using its logo, needs asking first.

## A client of your own

Preset the address, the gateway's settings and a header naming the wrapper,
so the server's access log can tell its callers apart. Leave the key or token
to the caller: a wrapper that holds one gives it to everyone who installs it.

=== "Python"

    ```python
    """acme_vectors: Acme's way in to its VectrixDB server."""

    import os

    import vectrixdb
    from vectrixdb.client import VectrixClient


    def connect(key=None, token=None, **options) -> VectrixClient:
        """Acme's server, through its gateway. Anything given here wins."""
        settings = {
            "key_header": "Ocp-Apim-Subscription-Key",
            "prefix": "/acme",
            "headers": {"x-acme-client": "acme-vectors/1.0"},
            **options,
        }
        url = os.environ.get("ACME_VECTORS_URL", "https://vectors.acme.com")
        return vectrixdb.connect(url, key=key, token=token, **settings)
    ```

=== "TypeScript"

    ```ts
    import { VectrixClient, type VectrixClientOptions } from "vectrixdb";

    export * from "vectrixdb";

    /** Acme's server, through its gateway. Anything given here wins. */
    export function connect(options: Partial<VectrixClientOptions> = {}): VectrixClient {
      return new VectrixClient({
        url: process.env.ACME_VECTORS_URL ?? "https://vectors.acme.com",
        keyHeader: "Ocp-Apim-Subscription-Key",
        prefix: "/acme",
        headers: { "x-acme-client": "acme-vectors/1.0" },
        ...options,
      });
    }
    ```

=== "Go"

    ```go
    // Package acmevectors is Acme's way in to its VectrixDB server.
    package acmevectors

    import vectrixdb "github.com/knowusuboaky/VectrixDB/sdk/go/v2"

    // Connect is the server, through Acme's gateway. Options given here win.
    func Connect(key string, more ...vectrixdb.Option) (*vectrixdb.Client, error) {
        opts := append([]vectrixdb.Option{
            vectrixdb.WithKey(key),
            vectrixdb.WithKeyHeader("Ocp-Apim-Subscription-Key"),
            vectrixdb.WithPrefix("/acme"),
            vectrixdb.WithHeader("x-acme-client", "acme-vectors/1.0"),
        }, more...)
        db := vectrixdb.New("https://vectors.acme.com", opts...)
        return db, db.Err()
    }
    ```

=== "Rust"

    ```rust
    //! Acme's way in to its VectrixDB server.
    pub use vectrixdb::*;

    /// The server, through Acme's gateway, with the key given.
    pub fn connect(key: &str) -> vectrixdb::Result<vectrixdb::Client> {
        vectrixdb::Client::new("https://vectors.acme.com")
            .key(key)
            .key_header("Ocp-Apim-Subscription-Key")
            .prefix("/acme")
            .header("x-acme-client", "acme-vectors/1.0")
            .build()
    }
    ```

Everything else is the client underneath, unchanged: the calls, the errors,
the retries, and the refusals (no key over plain HTTP, no redirects,
certificates always checked). A wrapper cannot switch those off, which is the
point: what [Use it from Python, TypeScript, Go or Rust](clients.md) promises
holds for every caller, whoever's name is on the package.

## A command of your own

The `vectrixdb` command is a [Typer](https://typer.tiangolo.com/) app, so a
company's own command mounts it whole, under a name of its choosing, next to
commands of its own. Presets go in the environment before it runs, with
`setdefault` so a person's own settings still win:

```python
"""acme: Acme's command line, with VectrixDB's commands under `acme vectors`."""

import os

import typer
from vectrixdb.cli import app as vectrixdb_app

PRESETS = {
    "VECTRIXDB_URL": "https://vectors.acme.com",
    "VECTRIXDB_LOGIN_CLIENT_ID": "0f3c9a2e-5d1b-4c7e-9a8f-1b2c3d4e5f60",
    "VECTRIXDB_LOGIN_SCOPES": "api://acme-vectors/search",
    "VECTRIXDB_KEY_HEADER": "Ocp-Apim-Subscription-Key",
    "VECTRIXDB_PREFIX": "/acme",
}

app = typer.Typer(help="Acme's tools")
app.add_typer(vectrixdb_app, name="vectors", help="Search Acme's documents")


def main() -> None:
    for name, value in PRESETS.items():
        os.environ.setdefault(name, value)
    app()


if __name__ == "__main__":
    main()
```

```toml
# pyproject.toml
[project]
name = "acme-cli"
dependencies = ["vectrixdb[client]>=2.2,<3", "typer"]

[project.scripts]
acme = "acme_cli:main"
```

```bash
pip install acme-cli
acme vectors login --device
acme vectors query "how long do refunds take?" --name handbook
```

With the scopes pinned, `login` asks nobody to confirm and refuses a server
that asks for anything else ([Sign in with your company account](command-line-server.md#sign-in-with-your-company-account)).
The messages still name `vectrixdb` in their hints, `vectrixdb login --url ...`,
since they come from the command underneath.

A company that would rather not ship code at all can ship the presets alone:
an env file every command reads with `--env-file`, or the same lines in the
machine image its people use.

## Publish it inside the company

A wrapper is an ordinary package, so it goes wherever the company keeps
packages: JFrog Artifactory, Sonatype Nexus, Azure Artifacts, AWS CodeArtifact
or GitHub Packages. The lines below are Artifactory's; the others take the
same steps with addresses of their own. In each, a **virtual** repository serves both the company's own
packages and a cached copy of the public ones, so `vectrixdb` comes through
the same address as `acme-cli`.

| Language | Install from it | Publish to it |
|---|---|---|
| Python | `pip install --index-url https://acme.jfrog.io/artifactory/api/pypi/pypi-virtual/simple acme-cli` | `twine upload --repository-url https://acme.jfrog.io/artifactory/api/pypi/pypi-local dist/*` |
| TypeScript | `registry=https://acme.jfrog.io/artifactory/api/npm/npm-virtual/` in `.npmrc` | `npm publish` with the same `.npmrc` |
| Go | `GOPROXY=https://acme.jfrog.io/artifactory/api/go/go-virtual` and `GOPRIVATE=acme.com/*` | a tag in the company's Git, served through a Go remote pointed at it |
| Rust | `.cargo/config.toml` below | `cargo publish --registry acme` |

```toml
# .cargo/config.toml
[registries.acme]
index = "sparse+https://acme.jfrog.io/artifactory/api/cargo/cargo-virtual/index/"

[source.crates-io]
replace-with = "acme"

[source.acme]
registry = "sparse+https://acme.jfrog.io/artifactory/api/cargo/cargo-virtual/index/"
```

`GOPRIVATE` keeps the company's own module names away from the public Go
checksum database; `github.com/knowusuboaky/VectrixDB/sdk/go/v2` is public,
so its checksums are still checked there.

[Run it inside your company's registry](inside-your-registry.md) sets up
every repository this needs, step by step, along with the images and the
models, and makes the wrapper the only way in.

## What a person at the company does

The registry hands out the software and nothing else: once installed, the
command and the clients talk to the company's VectrixDB server directly,
through its gateway, and the registry is not in the way of a single search.

Each machine is pointed at the registry once, by whoever looks after
machines: a `pip.conf`, an `.npmrc`, `GOPROXY`, a `.cargo/config.toml`, put
there by device management, the JFrog CLI or a CI template. After that,
nobody types the registry's address or its credentials again, and nothing
needs the public internet.

| Who | Installs | Then |
|---|---|---|
| Anyone with a terminal | `pip install acme-cli` | `acme vectors login --device`, signed in with their work account; `acme vectors query "refunds" --name handbook` |
| A Python developer | `pip install acme-vectors` | `acme_vectors.connect(token=...)`, everything else filled in |
| A TypeScript developer | `npm install @acme/vectors` | `connect({ token })` |
| A Go developer | `go get acme.com/vectors` | `acmevectors.Connect(key)` |
| A Rust developer | `cargo add acme-vectors` | `acme_vectors::connect(&key)?` |
| A CI job | the same, from the same registry | a key from the CI's secret store, as `VECTRIXDB_KEY` |

Who may search what is still the server's to say, for each person or key,
whatever package they came through
([Use the command line on a server](command-line-server.md#who-you-are)).

## Keep it working

- **Pin the major version**, `>=2.2,<3` or its equivalent, and let the
  minor versions in: they bring fixes, and a call that changes waits for 3.0.
- **Test it against a real server.** `python sdk/conformance/serve.py`
  starts one on a free port and prints its address and key as one line of
  JSON, and `sdk/conformance/handbook.md` is the document to add. The four
  clients' own tests walk that server the same way.
- **Another language.** Generate types from
  [`openapi.json`](../reference/openapi.json) and follow
  [`sdk/CONTRACT.md`](https://github.com/knowusuboaky/VectrixDB/blob/main/sdk/CONTRACT.md)
  for the envelopes, errors, retries and safety rules, then walk the same
  conformance steps against the same server.
