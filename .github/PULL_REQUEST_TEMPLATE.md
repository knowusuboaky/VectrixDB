<!-- Thanks. CONTRIBUTING.md has the definition of done; the checklist below is the short form. -->

## What this changes

<!-- One paragraph: the problem, the fix, what a user will notice. -->

## How it was verified

<!-- The test that fails without this change and passes with it. For a bug fix, the replay under tests/replays that proves the test bites. -->

## Checklist

- [ ] A test covers the change, and it fails on the old code
- [ ] `CHANGELOG.md` has a line under the unreleased section
- [ ] `ruff check .` passes and `mypy vectrixdb` is clean
- [ ] Docs updated if behaviour or a signature changed, and `python scripts/make_reference.py` run for a new setting, command or route
- [ ] No new runtime dependency, or the reason for it is in the description
