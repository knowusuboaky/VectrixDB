# How VectrixDB is run

## Who decides

One maintainer, `@knowusuboaky`, decides what merges and what ships. Contributors propose through issues and pull requests; `CODEOWNERS` routes review. A second maintainer is added when someone has landed several substantial changes and reviewed others' work well; at that point this file gains a decision rule for disagreements. Until then the rule is that the maintainer explains a no in writing.

## What the project is

A single-node, embeddable Python vector database with its models in the package. Scope is written down in the "who it is not for" section of the docs and in `ROADMAP.md`. Requests that turn it into a server product, a cluster, or a hosted service are declined on scope rather than on merit, and the answer points at the products that do those things.

## Release cadence

- **Patch releases** (`2.2.x`) ship when a bug fix lands that a user is waiting on. There is no schedule; the cost of a patch release is one command and a GitHub release, so there is no reason to batch them.
- **Minor releases** (`2.x.0`) ship roughly every eight weeks, or when a roadmap block is complete, whichever comes first. Every minor release gets a changelog entry under each of Added, Changed, Fixed and Security that applies.
- **Major releases** are for removing deprecated public API. Anything reachable from `import vectrixdb` is public. Removal needs a `DeprecationWarning` naming the replacement for at least one full minor release first.

The mechanics are in `scripts/release.py`: it checks the changelog, stamps the version and date, and prints the git commands. The publish workflow runs the full suite and the clients' own tests, uploads to PyPI on the GitHub release, and then publishes the clients at the same version: npm, crates.io, and the tag `sdk/go/vX.Y.Z` for Go. Nothing publishes from a laptop.

## What is checked before a merge

CI is the reviewer that does not get tired. A pull request needs green: lint, types under the ratchet, the suite on three operating systems and every supported Python, coverage not below its baseline, import time and wheel size under their gates, a changelog line, and the docs building strictly. A regression test comes with a replay that proves it fails on the old code. Retrieval quality changes come with a number from the fixture set or the benchmark scripts.

## Supported versions

The current minor release and the one before it receive patch releases. Python versions are supported from the oldest the packaging targets (see `pyproject.toml`) through the newest stable, with the free-threaded build tested as experimental.

## Security

`SECURITY.md` says where to report a vulnerability privately and what to expect. Cosmos DB and Databricks query construction were rewritten to bind parameters in 2.2; the storage contract suite's hostile-id case exists so that class of bug cannot come back quietly.
