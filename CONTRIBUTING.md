# Contributing to VectrixDB

Thanks for considering a contribution.

## Setup

```bash
git clone https://github.com/knowusuboaky/VectrixDB.git
cd VectrixDB
pip install -e ".[test]"
pre-commit install
```

`test` is everything the suite exercises without a network: `dev`, `api`,
`mcp`, `signin`, `documents` and `azure`, cloud services through fakes. With
less installed, the tests that need the rest skip rather than fail, which is
how CI's matrix runs on `.[dev,api,signin]`.

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv venv
uv pip install -e ".[test]"
uv run pytest
```

Nothing here is pinned to pip. The project is PEP 517 and PEP 621 with
hatchling as its backend, and `uv build` and `python -m build` are both front
ends onto that same backend, so either produces the wheel and sdist that ship.
There is no `uv.lock` in the repository, because a library states ranges and
lets the application that installs it do the pinning.

The bundled ONNX models are stored in Git LFS. If search returns errors about
missing model files, run `git lfs pull`.

## Definition of done

A change is ready when all four of these are true. CI enforces the first three.

1. **`ruff check .` passes**, scripts included, and `ruff format` has been run.
2. **`mypy vectrixdb` passes.** New modules should be fully annotated. The config
   ratchets toward strict: modules listed under `[[tool.mypy.overrides]]` in
   `pyproject.toml` are held to `disallow_untyped_defs`, and that list should
   only grow.
3. **`pytest` passes, and there is a test that fails without your change.** A
   test that passes before and after does not demonstrate anything.
4. **`CHANGELOG.md` has a line under `## [Unreleased]`** in the right section.

A new setting, command, option or route also needs
`python scripts/make_reference.py`, which rewrites the reference pages from
the code; a test fails until it has been run. The coverage baseline is measured
in a fresh environment with `.[test]` and nothing else, because that is what
CI's coverage job installs: `python scripts/coverage_ratchet.py --update` there,
not on a machine with every extra on it.

## Writing tests

Put them in `tests/unit/`, named after what they cover.

Assert behaviour, not existence. `assert SearchResult is not None` passes whether
or not the class works; it costs a line and buys nothing. Prefer:

- **Ground truth.** For anything approximate, compare against an exact
  implementation. `tests/unit/test_hnsw_recall.py` measures recall against
  brute-force search.
- **Properties over examples.** For numerical code, use Hypothesis to generate
  inputs and assert an invariant holds. `tests/unit/test_quantization_accuracy.py`
  bounds reconstruction error rather than checking one hand-picked vector.
- **Failure paths.** A storage backend that cannot reach its database must raise,
  not return an empty list. `tests/unit/test_storage_failures.py` pins that.

- **Proof the test bites.** A regression test that passes against the fix is
  half a test; the other half is showing it fails against the bug. Put a
  small pytest plugin under `tests/replays/` that monkeypatches the old
  behaviour back, and run `python scripts/replay.py tests/replays/<name>.py
  <the test>`: it exits 0 only when the test goes red. Link the replay from
  the table in `tests/replays/README.md`. The storage contract suite, the
  retrieval baselines and the golden graph are the standing versions of this
  rule: they fail on a drop, not on a missing attribute.

Mark anything slower than a second or so with `@pytest.mark.slow`, anything
needing a live service with `@pytest.mark.backend`, and anything that only
measures speed with `@pytest.mark.perf` (run nightly, not on every push). Run
the fast set with:

```bash
pytest -m "not slow and not backend"
```

## Error handling

Never write a bare `except:`. It catches `KeyboardInterrupt` and `SystemExit`
along with everything else, and it hides real failures.

Raise something from `vectrixdb.exceptions`, and chain the cause so the traceback
survives:

```python
try:
    container.read_item(item=doc_id, partition_key=key)
except Exception as exc:
    if _is_not_found(exc):
        return None          # a genuine absence is a normal result
    raise StorageOperationError("get", "CosmosDB", str(exc)) from exc
```

The distinction that matters: **absence is a result, failure is an exception.**
A caller must never be unable to tell an empty collection from a database it
could not reach.

## Queries

Use bind parameters. Where a driver's parameter style makes that impossible,
escape through `_sql_literal` and say why in a comment. Never interpolate a
caller-supplied value into query text with an f-string.

## Optional arguments

Test an optional argument with `is None`, never for truthiness. Twenty-five
bugs in 2.2 came from the same line of code written twenty-five times:

```python
self.stopwords = stopwords or ENGLISH_STOPWORDS   # an empty set means none
timeout = timeout or self.config.timeout          # 0 means do not block
if filter_ids:                                    # an empty set allows nothing
if source_coll and target_coll:                   # an empty collection is falsy
```

Every one of those reads a caller's explicit choice as an absent one, and
every one was found by a test that passed the empty or zero value on
purpose. When you add an optional parameter, add that test.

**Fixing one is not fixing it. Sweep.** The first nine were repaired where
they were reported and nowhere else, and a later mechanical sweep of all 89
modules found sixteen more, two of them inside the very functions those
fixes had touched, a few lines above the repaired line. One of the sixteen
deleted every memory in every session; another turned off access control for
a user in no groups; another sent a real API key to a caller-supplied
endpoint.

So when you fix one instance, walk the call graph outward and check every
wrapper above it and every sibling beside it. A one-off `ast` script over
`vectrixdb/` that lists parameters defaulting to `None` and then finds those
names in a truthiness position will do it in a minute, and it is worth
re-running whenever one of these turns up again.

## Dependencies

The core install should stay small: someone who wants vector search should not
have to install a web server. Anything used by one subsystem belongs in an
extra, imported lazily, with a `DependencyError` naming the extra when it is
missing.

## Commits and pull requests

Explain why in the commit body, not just what; the diff already shows what.
Keep one logical change per pull request.

## Governance and releases

`GOVERNANCE.md` says who decides, what is in scope, and the release cadence: patch releases when a fix is waiting, minor releases about every eight weeks or when a roadmap block completes, majors only to remove deprecated API. `ROADMAP.md` is the open list. A release is two steps: `python scripts/release.py X.Y.Z` stamps the changelog and the version, and after that commit is on main, **Actions > Release > Run workflow** checks the changelog is dated, runs the suite, builds and checks the package, tags `vX.Y.Z`, writes the GitHub release from the changelog, and waits for one approval on the `pypi` environment before uploading. `CODE_OF_CONDUCT.md` applies everywhere the project happens.

## Reporting bugs

Include the VectrixDB version (`python -c "import vectrixdb; print(vectrixdb.__version__)"`),
your Python version and OS, the storage backend, and the smallest snippet that
reproduces the problem.
