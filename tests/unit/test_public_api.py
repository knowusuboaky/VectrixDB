"""The public surface is explicit, and it changes on purpose.

Every public module declares __all__ and every name in it exists. The names
`vectrixdb` itself exports are recorded in public_api.txt; a difference in
either direction fails, with the diff, so growing or shrinking the API is a
deliberate edit to that file rather than a side effect.
"""

import importlib
import importlib.util
import pkgutil
from pathlib import Path

import pytest

import vectrixdb

SNAPSHOT = Path(__file__).with_name("public_api.txt")


def _public_modules():
    for info in pkgutil.walk_packages(vectrixdb.__path__, prefix="vectrixdb."):
        parts = info.name.split(".")
        if any(p.startswith("_") for p in parts[1:]) or "dashboard" in parts:
            continue
        yield info.name


#: The adapters subclass another framework's classes, so they cannot be imported
#: without it, and they say so. Every other module must import on the core install.
NEEDS_A_FRAMEWORK = {
    "vectrixdb.integrations.langchain": "langchain_core",
    "vectrixdb.integrations.llamaindex": "llama_index",
}


@pytest.mark.parametrize("module_name", sorted(_public_modules()))
def test_module_declares_its_surface(module_name):
    framework = NEEDS_A_FRAMEWORK.get(module_name)
    if framework and importlib.util.find_spec(framework) is None:
        pytest.skip(f"{module_name} needs {framework}")
    module = importlib.import_module(module_name)
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
