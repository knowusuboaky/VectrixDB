# Run it inside your company's registry

Many companies let nothing onto a machine that did not come through their own
registry: JFrog Artifactory most often, or Sonatype Nexus, Azure Artifacts,
AWS CodeArtifact. Everything VectrixDB needs can come that way: the Python,
npm, Go and Rust packages, the container images, the models, and the
company's own wrapper of the command line and the clients. This page sets it
up in Artifactory, from an empty instance to a person running
`acme vectors query`, and then makes the company's wrapper the way in.

Throughout, the company is Acme, its Artifactory is `acme.jfrog.io`, and its
VectrixDB server is `https://vectors.acme.com`. Replace them with yours.

## What comes through the registry

| What | Where it comes from in public | Artifactory repository | Who needs it |
|---|---|---|---|
| `vectrixdb` and its Python dependencies | pypi.org | PyPI remote | the server, the command line, Python developers |
| `vectrixdb` for TypeScript | registry.npmjs.org | npm remote | TypeScript developers |
| `github.com/knowusuboaky/VectrixDB/sdk/go/v2` | proxy.golang.org | Go remote | Go developers |
| `vectrixdb` for Rust | crates.io | Cargo remote | Rust developers |
| The server and extraction images | ghcr.io | Docker remote | whoever runs the server |
| The models `download-models` fetches | GitHub releases | Generic remote | a server that needs the multilingual models, or an older collection's |
| The speech model the extraction image carries | huggingface.co | Hugging Face remote | building the extraction image in-house |
| The company's wrapper | built in-house | PyPI, npm, Go, Cargo local | everyone at the company |

The English models the server uses by default ship inside the Python package
and the image, so a company that searches in English needs no model
repository at all.

## 1. Create the repositories

In Artifactory, **Administration**, **Repositories**, create one *remote*
repository for each public source, one *local* repository for each kind of
package the company makes itself, and one *virtual* repository per language
that puts the two together. People and machines only ever use the virtual
one, so `vectrixdb` and `acme-cli` come from the same address.

| Repository | Type | Package type | URL to proxy |
|---|---|---|---|
| `pypi-remote` | remote | PyPI | `https://files.pythonhosted.org`, registry URL `https://pypi.org` |
| `pypi-local` | local | PyPI | |
| `pypi` | virtual | PyPI | `pypi-local`, then `pypi-remote` |
| `npm-remote` | remote | npm | `https://registry.npmjs.org` |
| `npm-local` | local | npm | |
| `npm` | virtual | npm | `npm-local`, then `npm-remote` |
| `go-remote` | remote | Go | `https://proxy.golang.org` |
| `go-acme` | remote | Go, VCS | the company's Git, for its own modules |
| `go` | virtual | Go | `go-acme`, then `go-remote` |
| `cargo-remote` | remote | Cargo | `https://index.crates.io`, with sparse index on |
| `cargo-local` | local | Cargo | |
| `cargo` | virtual | Cargo | `cargo-local`, then `cargo-remote` |
| `ghcr-remote` | remote | Docker | `https://ghcr.io` |
| `dockerhub-remote` | remote | Docker | `https://registry-1.docker.io`, for the Python base image when building in-house |
| `docker-local` | local | Docker | |
| `vectrixdb-models` | remote | Generic | `https://github.com/knowusuboaky/VectrixDB/releases/download` |
| `hf-remote` | remote | Hugging Face | `https://huggingface.co` |

Put the local repository first in each virtual one, so a package the company
publishes can never be shadowed by a public one of the same name. Where the
company's packages have a scope or prefix of their own (`@acme/` on npm,
`acme-` on PyPI), add an exclude pattern for it on the remote repository
too: then nobody outside can publish a public `acme-cli` that a mistyped
install would pick up.

`vectrixdb-models` needs no credentials, because GitHub's releases are
public. Turn on **Store artifacts locally**, so each model is fetched from
GitHub once and served from Acme after that.

### Scanning and curation

Attach the remote repositories to a JFrog Xray watch, so every package and
image is scanned as it is first fetched, and, where JFrog Curation is
licensed, a policy that blocks what the security team has not allowed. Our
releases help:

- every container image is signed, with its SBOM and build provenance
  attached ([Verify an image before it runs](containers.md#verify-an-image-before-it-runs));
- every model zip is checked against checksums recorded in the package, so
  a mirror, or anything between it and GitHub, cannot change what is
  installed.

## 2. Give people and machines their tokens

Each person gets an Artifactory **identity token**, or signs in with
`jf login`, which keeps one. A CI job gets a short-lived token through
Artifactory's OpenID Connect integration with the CI platform (GitHub
Actions, GitLab, Azure DevOps, Jenkins), so no long-lived token sits in its
settings.

Read-only is all anyone but the release job needs. The token goes in
`~/.netrc`, the environment, or the operating system's keychain, as below;
never in a file that is committed.

## 3. Point each machine at it

Whoever manages machines puts these in place once, through device management,
the base image every developer starts from, or `jf pip-config`,
`jf npm-config` and friends. After that, plain `pip install`,
`npm install`, `go get` and `cargo add` go to Artifactory without anybody
typing its address.

=== "Python"

    `pip.conf` (`~/.config/pip/pip.conf`; `%APPDATA%\pip\pip.ini` on Windows),
    with no credentials in it:

    ```ini
    [global]
    index-url = https://acme.jfrog.io/artifactory/api/pypi/pypi/simple
    ```

    and the token in `~/.netrc` (`%USERPROFILE%\_netrc` on Windows), readable
    by its owner alone, which pip, Go and most other tools read:

    ```text
    machine acme.jfrog.io login ada@acme.com password <identity token>
    ```

    `uv` and Poetry take the same address as an index.

=== "TypeScript"

    `.npmrc` in the home folder:

    ```ini
    registry=https://acme.jfrog.io/artifactory/api/npm/npm/
    //acme.jfrog.io/artifactory/api/npm/npm/:_authToken=${JFROG_TOKEN}
    ```

=== "Go"

    ```bash
    go env -w GOPROXY="https://acme.jfrog.io/artifactory/api/go/go"
    go env -w GOPRIVATE="acme.com/*"
    ```

    Go signs in to the proxy with the same `~/.netrc` line as pip.

    `GOPRIVATE` keeps the company's own module names out of the public
    checksum database. `github.com/knowusuboaky/VectrixDB/sdk/go/v2` is public,
    so its checksums are still checked; where `sum.golang.org` is out of
    reach, Go asks for it through `GOPROXY` first.

=== "Rust"

    `~/.cargo/config.toml`:

    ```toml
    [registries.acme]
    index = "sparse+https://acme.jfrog.io/artifactory/api/cargo/cargo/index/"
    credential-provider = "cargo:token"

    [source.crates-io]
    replace-with = "acme"

    [source.acme]
    registry = "sparse+https://acme.jfrog.io/artifactory/api/cargo/cargo/index/"
    ```

    and once: `cargo login --registry acme` with the token.

=== "Docker"

    ```bash
    docker login acme.jfrog.io
    docker pull acme.jfrog.io/ghcr-remote/knowusuboaky/vectrixdb:2.2.0
    ```

Check it from any machine:

```bash
pip download --no-deps "vectrixdb==2.2.0" -d /tmp/check   # Python, through Artifactory
npm view vectrixdb version                                # npm
go list -m github.com/knowusuboaky/VectrixDB/sdk/go/v2@latest
cargo search vectrixdb --registry acme
```

## 4. Run the server from it

### With the image

Pull the image through `ghcr-remote`, check its signature, and push it to
`docker-local` under the name the cluster uses, so what runs is the image
that was checked:

```bash
image=acme.jfrog.io/ghcr-remote/knowusuboaky/vectrixdb:2.2.0
docker pull "$image"
digest="$(docker inspect --format '{{index .RepoDigests 0}}' "$image" | cut -d@ -f2)"
# The digest is the same through the remote; the signature is checked at its source.
cosign verify "ghcr.io/knowusuboaky/vectrixdb@$digest" \
  --certificate-identity-regexp '^https://github\.com/knowusuboaky/VectrixDB/\.github/workflows/containers\.yml@refs/(heads/main|tags/v.+)$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
docker tag "$image" acme.jfrog.io/docker-local/vectrixdb:2.2.0
docker push acme.jfrog.io/docker-local/vectrixdb:2.2.0
```

`cosign verify` asks GitHub's registry and Sigstore's public transparency
log, so run it where those are reachable, such as the job that promotes
images into `docker-local`, and pin the digest it verified in the cluster
([Verify an image before it runs](containers.md#verify-an-image-before-it-runs)).

### Built in-house

A company that builds every image itself builds ours from the source, with
its own base image and every package and model through Artifactory. Two
build secrets carry the addresses and credentials. They are mounted for the
steps that need them and never written into a layer:

```ini
# pip.conf, for the build
[global]
index-url = https://build%40acme.com:<token>@acme.jfrog.io/artifactory/api/pypi/pypi/simple
```

```bash
# mirrors.env, for the build
VECTRIXDB_MODELS_URL=https://acme.jfrog.io/artifactory/vectrixdb-models
VECTRIXDB_MODELS_TOKEN=<token>
HF_ENDPOINT=https://acme.jfrog.io/artifactory/api/huggingfaceml/hf-remote
HF_TOKEN=<token>
```

```bash
docker build -f docker/Dockerfile \
  --build-arg PYTHON_IMAGE=acme.jfrog.io/dockerhub-remote/library/python:3.12-slim \
  --build-arg DEBIAN_UPDATES=0 \
  --build-arg MODELS=all \
  --secret id=pip,src=pip.conf \
  --secret id=mirrors,src=mirrors.env \
  -t acme.jfrog.io/docker-local/vectrixdb:2.2.0 .
docker build -f docker/Dockerfile --target extract \
  --build-arg PYTHON_IMAGE=acme.jfrog.io/dockerhub-remote/library/python:3.12-slim \
  --build-arg DEBIAN_UPDATES=0 \
  --secret id=pip,src=pip.conf \
  --secret id=mirrors,src=mirrors.env \
  -t acme.jfrog.io/docker-local/vectrixdb-extract:2.2.0 .
```

`DEBIAN_UPDATES=0` is for a build that cannot reach Debian's archive; the
company's own base image is then where its security fixes come from. Behind
a proxy that inspects TLS, add `--secret id=ca,src=acme-ca.pem` as well.

### With pip

```bash
pip install "vectrixdb[api,signin,mcp]==2.2.0"      # through pip.conf
export VECTRIXDB_MODELS_URL=https://acme.jfrog.io/artifactory/vectrixdb-models
export VECTRIXDB_MODELS_TOKEN="$JFROG_TOKEN"
vectrixdb download-models --type dense              # only for the multilingual models
vectrixdb serve
```

`VECTRIXDB_MODELS_URL` replaces GitHub for every model download and takes the
same paths, `<release tag>/<model>.zip`. The token goes to that address
alone, never on to a redirect, and each model is checked against the
package's checksums before it is used. Set `VECTRIXDB_OFFLINE=1` on a server
that should never download anything at all.

## 5. Ship the company's own tool

Build the wrapper as [Ship your own client or command on it](wrap-it.md)
shows, with the company's address, gateway settings and sign-in presets in
it, and publish it to the local repositories:

```bash
# Python: the command and the client
python -m build
twine upload --repository-url https://acme.jfrog.io/artifactory/api/pypi/pypi-local dist/*

# TypeScript
npm publish --registry https://acme.jfrog.io/artifactory/api/npm/npm-local/

# Rust
cargo publish --registry acme

# Go: a tag in the company's Git, which go-acme serves
git tag v1.0.0 && git push origin v1.0.0
```

A person at Acme then needs nothing but the package name:

```bash
pip install acme-cli
acme vectors login --device        # their work account, once
acme vectors query "how long do refunds take?" --name handbook
```

## Require the company's own tool

Making the wrapper available is the registry's job. Making it *the* way in is
the server's, because the registry cannot tell a person installing
`acme-cli` from one installing the `vectrixdb` it depends on.

1. **Register the wrapper as an app of its own** with the identity provider:
   a public client (no secret), `http://127.0.0.1` as a redirect, the device
   code flow on, and permission for the server's API scope. Its client id is
   the `VECTRIXDB_LOGIN_CLIENT_ID` the wrapper presets.
2. **Have the server take tokens from that app alone**, and from the MCP
   clients the company allows:

    ```bash
    export VECTRIXDB_OIDC_API_AUDIENCE="api://vectrixdb"
    export VECTRIXDB_OIDC_API_CLIENTS="<acme-cli client id> <MCP client id>"
    ```

    A token another app got for the same API is refused, and the access log
    records it as `app_not_allowed`, with who it was.
3. **Pin the scopes in the wrapper** with `VECTRIXDB_LOGIN_SCOPES`, so it
   asks for this server's scope and nothing else, and refuses a server that
   asks for more.
4. **Give keys to services, not people.** An admin makes a key for each
   service or CI job, scoped to its collections and with an end date
   (`vectrixdb keys add ... --days 90`). People sign in.

What this does not stop: a client id is not a secret, since a public client
cannot keep one, so somebody determined can copy the wrapper's into another
tool. Step 2 keeps everyone on the wrapper by default and leaves a trace of
anyone who goes around it; making it impossible is the job of the company's
device management (which software may run) and its identity provider's
conditional access (which devices may sign in), as it is for any company
tool.

## Upgrading

A new VectrixDB release reaches Acme when somebody first asks for it: the
remote repositories fetch it, Xray scans it, and Curation lets it through or
not. The wrapper pins the major version (`vectrixdb[client]>=2.2,<3`), so a
2.x release reaches people with the wrapper's next build, and a 3.0 waits for
the wrapper to move. Upgrade the server first: a client older than the
server keeps working, since a 2.x server only adds.

## Checklist

| Step | Done when |
|---|---|
| Repositories | `pip download vectrixdb`, `npm view vectrixdb`, `go list -m ...`, `cargo search vectrixdb` all answer through Artifactory |
| Images | the image is in `docker-local`, its signature verified and its digest pinned |
| Models | `vectrixdb download-models --type dense` with `VECTRIXDB_MODELS_URL` set fetches through `vectrixdb-models` |
| Wrapper | `pip install acme-cli` on a fresh machine, then `acme vectors login --device` and a query, work |
| Required | a token from another app gets 401, and the access log says `app_not_allowed` |
