# Distribute it through JFrog Artifactory

Many companies let nothing onto a laptop, a build agent or a cluster that
did not come through their own registry. This page sets up JFrog Artifactory
so that VectrixDB, its clients, its models, its container images and the
company's own [wrapper](wrap-for-your-company.md) all come from there, and
so that people install the wrapper the company tested, not whatever the
public registries hold that day.

Everything here uses Artifactory's own features: remote repositories that
proxy and cache a public registry, local repositories that hold what the
company builds, and virtual repositories that put the two behind one
address. In the examples, Artifactory is `https://acme.jfrog.io` and the
wrapper is `acme-vectors`; put your own in their place.

## What goes where

| What | Public source | Remote repository | Local repository | Virtual repository people use |
| --- | --- | --- | --- | --- |
| Python: `vectrixdb`, its dependencies, the wrapper | pypi.org | `pypi-remote` | `pypi-local` | `pypi` |
| TypeScript: `vectrixdb`, `@acme/vectors` | registry.npmjs.org | `npm-remote` | `npm-local` | `npm` |
| Go: `github.com/knowusuboaky/VectrixDB/sdk/go/v2`, the wrapper | proxy.golang.org | `go-remote` | `go-local` | `go` |
| Rust: `vectrixdb`, `acme-vectors` | crates.io | `cargo-remote` | `cargo-local` | `cargo` |
| Container images | ghcr.io | `docker-remote` | `docker-local` | `docker` |
| Models that are not in the wheel | GitHub releases, Hugging Face | `huggingface-remote` | `vectrixdb-models` (generic) | |

The virtual repository lists its local repository first, so the company's
own package wins over a public one of the same name. That ordering is also
what stops a public package from posing as an internal one: see
[Keep internal names internal](#keep-internal-names-internal).

## 1. Make the repositories

In the Artifactory UI, **Administration → Repositories → Create a
Repository**, once per row above: a **Remote** of the right package type
pointing at the public source, a **Local** of the same type, and a
**Virtual** that includes both, local first.

The JFrog CLI does the same from a file, which can be kept in the platform
team's repository and reviewed like any other change:

```bash
jf rt repo-create pypi-remote.json
```

```json
{
  "key": "pypi-remote",
  "rclass": "remote",
  "packageType": "pypi",
  "url": "https://files.pythonhosted.org",
  "pyPIRegistryUrl": "https://pypi.org",
  "includesPattern": "**/*"
}
```

```json
{
  "key": "pypi",
  "rclass": "virtual",
  "packageType": "pypi",
  "repositories": ["pypi-local", "pypi-remote"],
  "defaultDeploymentRepo": "pypi-local"
}
```

The other types follow the same shape, with `packageType` `npm`, `go`,
`cargo` or `docker` and the public source's address as `url`. For Cargo,
turn on the sparse index (*Enable sparse index support* in the UI), which
current Cargo uses. For Go, the remote's upstream is the public Go module
proxy, `https://proxy.golang.org`.

Ask Artifactory's administrator for an **Xray** policy on the remote
repositories too, if Xray is licensed: every package and image is then
scanned as it is first cached, and a build can be failed on a known
vulnerability before anyone installs it.

## 2. Bring VectrixDB in

Nothing to upload for the public parts: the first install through a remote
repository fetches the package and caches it. Pin what you tested:

| Part | Name | Pinned as |
| --- | --- | --- |
| Python library, client, command line | `vectrixdb` | `vectrixdb[client]==2.2.0` |
| TypeScript client | `vectrixdb` | `"vectrixdb": "2.2.0"` |
| Go client | `github.com/knowusuboaky/VectrixDB/sdk/go/v2` | `v2.2.0` |
| Rust client | `vectrixdb` | `vectrixdb = "=2.2.0"` |
| Server image | `ghcr.io/knowusuboaky/vectrixdb` | `2.2.0`, by digest in production |
| Extraction service image | `ghcr.io/knowusuboaky/vectrixdb-extract` | `2.2.0`, by digest |

### The models

The English embedding model and reranker are inside the Python wheel and
inside the server image, so a search needs nothing else. The models fetched
on demand (multilingual, the larger English ones, the graph extractor) come from
two places, and both can go through Artifactory.

**From VectrixDB's GitHub releases.** These files never change once
released, so the simplest mirror is a **generic local** repository that
holds them. Each model's address on GitHub is printed by:

```bash
python -c "from vectrixdb.models.downloader import RELEASE_ASSETS, release_asset_url as u; [print(u(m)) for m in RELEASE_ASSETS]"
```

Download the ones you need once, on a machine that may, into folders laid
out as GitHub has them, `<tag>/<model>.zip`, and upload that layout from the
folder that holds the tags:

```bash
jf rt upload "*/*.zip" "vectrixdb-models/knowusuboaky/VectrixDB/releases/download/" --flat=false
```

Then point VectrixDB at the mirror. The paths below it are the same as on
GitHub, `<tag>/<model>.zip`:

```bash
export VECTRIXDB_MODELS_URL=https://acme.jfrog.io/artifactory/vectrixdb-models/knowusuboaky/VectrixDB/releases/download
vectrixdb download-models --type dense
```

A **generic remote** repository pointing at `https://github.com` works too,
caching each file as it is first asked for, if the network lets Artifactory
follow GitHub's redirect to its download host.

**From Hugging Face.** Artifactory has a Hugging Face repository type.
Make a remote one pointing at `https://huggingface.co`, and set:

```bash
export HF_ENDPOINT=https://acme.jfrog.io/artifactory/api/huggingfaceml/huggingface-remote
export HF_TOKEN="$ARTIFACTORY_TOKEN"
```

### The images

```bash
docker pull acme.jfrog.io/docker/knowusuboaky/vectrixdb:2.2.0
```

Verify each image's signature before it is promoted for use, as
[Run it in containers](containers.md#verify-an-image-before-it-runs)
shows, then run it by digest so what runs is what was verified. In
Kubernetes, point the image at the company registry with
`kustomize edit set image`, and give the cluster an image pull secret for
Artifactory.

## 3. Publish the company's wrapper

Build the wrapper as [Wrap it for your company](wrap-for-your-company.md)
shows, test it installed, then publish it to the local repository. Use a
token made for the build, never a person's password.

=== "Python"

    ```bash
    python -m build
    twine upload --repository-url https://acme.jfrog.io/artifactory/api/pypi/pypi-local dist/*
    ```

    `twine` reads `TWINE_USERNAME` and `TWINE_PASSWORD`; the password is the
    access token.

=== "TypeScript"

    ```bash
    npm publish --registry https://acme.jfrog.io/artifactory/api/npm/npm-local/
    ```

    In `package.json`, `"publishConfig": {"registry": "https://acme.jfrog.io/artifactory/api/npm/npm-local/"}`
    makes a plain `npm publish` go there and nowhere else.

=== "Go"

    ```bash
    git tag v1.0.0 && git push origin v1.0.0
    jf go-publish v1.0.0
    ```

    `jf go-publish` packs the module at that version and deploys it to the
    Go repository `jf go-config` named.

=== "Rust"

    ```bash
    cargo publish --registry acme
    ```

    With `publish = ["acme"]` in the crate's `Cargo.toml`, it cannot go to
    crates.io by mistake.

Run the publish from CI, after the wrapper's tests pass, with the token from
the CI's secret store. Where the CI and Artifactory both support it, an OIDC
federation between them removes the stored token altogether: JFrog's
`setup-jfrog-cli` action for GitHub Actions does this.

## 4. Set up the machines people use

Each tool is pointed at the virtual repository once. The token goes in a
file only that person can read, or in an environment variable from a secret
store: never in a command typed at the prompt, and never in a file
committed to a repository.

The JFrog CLI writes most of this itself (`jf pip-config`, `jf npm-config`,
`jf go-config`), and many companies ship the result through device
management. By hand:

=== "Python"

    `pip.conf` (`%APPDATA%\pip\pip.ini` on Windows, `~/.config/pip/pip.conf`
    elsewhere):

    ```ini
    [global]
    index-url = https://acme.jfrog.io/artifactory/api/pypi/pypi/simple
    ```

    and the credentials in `~/.netrc` (`_netrc` on Windows), which pip reads:

    ```text
    machine acme.jfrog.io
    login ama@acme.example
    password <access token>
    ```

    ```bash
    chmod 600 ~/.netrc
    pip install acme-vectors
    ```

    In CI, `PIP_INDEX_URL` instead, from the secret store.

=== "TypeScript"

    `.npmrc` in the user's home:

    ```ini
    registry=https://acme.jfrog.io/artifactory/api/npm/npm/
    //acme.jfrog.io/artifactory/api/npm/npm/:_authToken=${ARTIFACTORY_TOKEN}
    ```

    npm reads `${ARTIFACTORY_TOKEN}` from the environment, so the file holds
    no token and can be shipped to every machine.

=== "Go"

    ```bash
    go env -w GOPROXY=https://acme.jfrog.io/artifactory/api/go/go,direct
    go env -w GONOSUMDB=git.acme.example
    ```

    The credentials go in `~/.netrc`, as for Python. `GONOSUMDB` keeps the
    company's own module paths out of the public checksum database, which
    would otherwise be asked about a module it has never seen. Drop
    `,direct` to forbid fetching around Artifactory.

=== "Rust"

    `~/.cargo/config.toml`:

    ```toml
    [registries.acme]
    index = "sparse+https://acme.jfrog.io/artifactory/api/cargo/cargo/index/"

    [registry]
    default = "acme"
    global-credential-providers = ["cargo:token"]

    [source.crates-io]
    replace-with = "acme"
    ```

    and the token in the environment: `CARGO_REGISTRIES_ACME_TOKEN="Bearer <access token>"`.
    `replace-with` sends every crates.io dependency through Artifactory.

Then the company's own settings, which the wrapper already carries:

```bash
acme-vectors login
acme-vectors whoami
```

A person signs in once, with the company's identity provider, and every
command and every `acme_vectors.connect()` after that uses it. See
[Use the command line against a server](command-line.md).

## 5. Make the wrapper the way in

Artifactory can't stop someone typing a server's address into plain
`vectrixdb`, and it shouldn't try: the wrapper depends on `vectrixdb`, so
the plain package is always installable. What it can do is decide which
versions of anything exist inside the company, and the server decides who
gets in.

**Allow what was tested, and nothing else.** On each remote repository,
**Include patterns** make it an allow list: only those packages can be
fetched from the public registry at all. On `pypi-remote`, for instance,
allow `vectrixdb/**` and the dependencies its lock file names. With
**JFrog Curation**, where licensed, the same can be a policy: block a
package version that is newer than N days, that has a known vulnerability,
or that nobody approved.

**Pin the wrapper's own dependency.** `vectrixdb[client]==2.2.0` in the
wrapper means an install of the wrapper brings that version and no other,
whatever the remote has cached since.

**Send every install through Artifactory.** Block direct access to pypi.org,
registry.npmjs.org, proxy.golang.org, crates.io and ghcr.io at the network
or the proxy. Machines then have one way to install anything.

**Enforce on the server.** The rules that matter live where they cannot be
installed around:

- [Company sign-in](sign-in.md): a person reaches the server only as
  themselves, with the company's multi-factor and conditional access
  rules.
- [Roles and scoped keys](keys-and-roles.md): a script's key reaches only
  the collections it was made for, and can expire.
- [Collection policies](collection-policies.md): who may search a
  collection at all.
- [The gateway](behind-a-gateway.md): a subscription key per application,
  so the gateway's logs show which application, and which wrapper version
  from its `User-Agent`, made each call.

## Keep internal names internal

A public package with the same name as an internal one is a known attack:
the installer may pick the public one. Three things stop it:

1. **The local repository comes first** in each virtual repository, so the
   internal package wins.
2. **Exclude patterns on the remote repositories** for the company's own
   names: on `pypi-remote`, exclude `acme-*/**`; on `npm-remote`, exclude
   `@acme/**`. A public `acme-vectors` can then never be fetched, whatever
   its version number.
3. **A scope or prefix nobody else can register**: an npm scope the company
   owns, a Go module path on the company's own domain, a crate published
   only to `acme`.

## In an air-gapped network

With no route to the internet even for Artifactory, carry everything in
once, on a machine that may reach both:

```bash
pip download "vectrixdb[client]==2.2.0" -d wheels/
jf rt upload "wheels/*" pypi-local/

docker pull ghcr.io/knowusuboaky/vectrixdb:2.2.0
docker tag ghcr.io/knowusuboaky/vectrixdb:2.2.0 acme.jfrog.io/docker-local/knowusuboaky/vectrixdb:2.2.0
docker push acme.jfrog.io/docker-local/knowusuboaky/vectrixdb:2.2.0
```

`pip download` runs on the same operating system and Python version as the
machines that will install, since wheels are built for one. Upload the
model zips to the generic repository as in [The models](#the-models). The
server image needs nothing more: its models are inside it, and
[Run without a network](offline.md) covers the rest.

## Check it end to end

On a clean machine set up as in step 4:

```bash
pip install acme-vectors
acme-vectors --help
acme-vectors login
acme-vectors whoami
acme-vectors query "how long do refunds take" --name handbook
```

`acme-vectors whoami` names the company's server, the person signed in and
their role. If the install fails, the virtual repository or its credentials
are wrong. If the sign-in fails, the client id or the server's sign-in is.
If the call fails with a certificate error, the machine does not trust the
company's authority: set `ca_file` to `system` in the wrapper, or
`VECTRIXDB_CA_FILE`.
