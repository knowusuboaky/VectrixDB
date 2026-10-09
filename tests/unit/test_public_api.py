"""The public surface is explicit, and it changes on purpose.

Every public module declares __all__ and every name in it exists. The names
`vectrixdb` itself exports are recorded in public_api.txt; a difference in
either direction fails, with the diff, so growing or shrinking the API is a
deliberate edit to that file rather than a side effect.
"""

import importlib
import importlib.util
import pkgutil
import sys
from pathlib import Path

import pytest

import vectrixdb
from vectrixdb.exceptions import DependencyError

SNAPSHOT = Path(__file__).with_name("public_api.txt")


def _a_package_needs_an_extra(name):
    # walk_packages imports each package to look inside it, and vectrixdb.api
    # refuses to import without the api extra. Without this the whole file
    # stopped at collection on an install without it, the nightly perf job's,
    # and took the snapshot test with it. walk_packages calls this from inside
    # its except block, so a bare raise passes on anything else. The package
    # itself is still listed, and the test below says which extra it wants.
    if not isinstance(sys.exc_info()[1], DependencyError):
        raise


def _public_modules():
    for info in pkgutil.walk_packages(
        vectrixdb.__path__, prefix="vectrixdb.", onerror=_a_package_needs_an_extra
    ):
        parts = info.name.split(".")
        if any(p.startswith("_") for p in parts[1:]) or "dashboard" in parts:
            continue
        yield info.name


#: The adapters subclass another framework's classes, so they cannot be imported
#: without it, and they say so. Every other module must import on the core
#: install, or refuse with a DependencyError naming the extra that is missing.
NEEDS_A_FRAMEWORK = {
    "vectrixdb.integrations.langchain": "langchain_core",
    "vectrixdb.integrations.llamaindex": "llama_index",
}


@pytest.mark.parametrize("module_name", sorted(_public_modules()))
def test_module_declares_its_surface(module_name):
    framework = NEEDS_A_FRAMEWORK.get(module_name)
    if framework and importlib.util.find_spec(framework) is None:
        pytest.skip(f"{module_name} needs {framework}")
    try:
        module = importlib.import_module(module_name)
    except DependencyError as exc:
        # Only when what it names is really absent: a module that asks for a
        # package which is installed is a fault, not an install shape.
        if importlib.util.find_spec(exc.package) is not None:
            raise
        pytest.skip(f"{module_name} needs the {exc.extra or exc.package} extra")
    assert hasattr(module, "__all__"), f"{module_name} has no __all__"
    missing = [n for n in module.__all__ if not hasattr(module, n)]
    assert not missing, f"{module_name}.__all__ names things that do not exist: {missing}"


def test_top_level_api_matches_the_snapshot():
    current = set(vectrixdb.__all__)
    recorded = set(SNAPSHOT.read_text(encoding="utf-8").split())
    added = sorted(current - recorded)
    removed = sorted(recorded - current)
    assert not added and not removed, (
        "The public API changed. If that is intended, edit tests/unit/public_api.txt.\n"
        f"  added:   {added}\n  removed: {removed}"
    )


def test_snapshot_has_no_duplicates():
    names = SNAPSHOT.read_text(encoding="utf-8").split()
    assert len(names) == len(set(names))
