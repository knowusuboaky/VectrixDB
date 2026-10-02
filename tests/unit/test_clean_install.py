"""A plain install must be able to do the thing the README opens with.

`psutil` was imported at module scope in `core/scaling.py` and was not one of
the declared dependencies, so `pip install vectrixdb` followed by
`Vectrix(...)` raised ModuleNotFoundError. Nobody saw it because every
development machine has psutil, and `import vectrixdb` on its own succeeds:
the package binds its names lazily, so the failure waits for first use.

These tests fail if an undeclared import creeps back in.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: The one subpackage allowed to import an extra's packages at module scope.
#: It earns that because nothing reaches it except through its `__init__`,
#: which catches the ImportError and raises DependencyError naming the extra;
#: `test_the_exempt_subpackage_really_is_guarded` checks that is still true.
#:
#: Everywhere else the rule is core dependencies or a local try/except. An
#: extra is not installed by `pip install vectrixdb`, so treating every extra
#: as fair game would have let psutil back in the moment it was added to one.
GUARDED_SUBPACKAGES = {"vectrixdb/api"}

#: Import names that differ from the distribution name that ships them.
IMPORT_NAMES = {
    "fastapi": "fastapi",
    "starlette": "fastapi",
    "pydantic": "fastapi",
    "sklearn": "scikit-learn",
    "umap": "umap-learn",
    "spacy": "spacy",
    "mcp": "mcp",
}


def _dependencies() -> tuple[set[str], set[str]]:
    """The core requirements, and everything any extra adds."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def names(specs):
        out = set()
        for spec in specs:
            name = spec.split("[")[0].split(">")[0].split("<")[0].split("=")[0]
            name = name.split(";")[0].strip()
            out |= {name, name.replace("-", "_")}
        return out

    core = names(data["project"]["dependencies"])
    extra = set()
    for group in data["project"].get("optional-dependencies", {}).values():
        extra |= names(group)
    for import_name, dist in IMPORT_NAMES.items():
        if dist in extra or dist.replace("-", "_") in extra:
            extra.add(import_name)
    return core, extra


def _guarded_in(tree: ast.Module) -> set[str]:
    """Names imported inside a try/except somewhere in this module."""
    guarded: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for child in ast.walk(node):
                if isinstance(child, ast.Import):
                    guarded.update(a.name.split(".")[0] for a in child.names)
                elif isinstance(child, ast.ImportFrom) and child.module:
                    guarded.add(child.module.split(".")[0])
    return guarded


def test_no_module_level_import_of_an_undeclared_package():
    """Every module-level import in the package is the standard library, the
    package itself, a core dependency, or wrapped in a try/except that handles
    its absence. Nothing is exempt by name."""
    core, extra = _dependencies()
    offenders = []

    for path in sorted((ROOT / "vectrixdb").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        exempt = any(relative.startswith(f"{pkg}/") for pkg in GUARDED_SUBPACKAGES)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        guarded = _guarded_in(tree)

        for node in tree.body:  # module level only
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in sys.stdlib_module_names or name.startswith("vectrixdb"):
                    continue
                if name in core or name in guarded:
                    continue
                if exempt and name in extra:
                    continue
                offenders.append(f"{relative}: {name}")

    assert not offenders, (
        "imported at module scope but not a core dependency, so a plain "
        "`pip install vectrixdb` breaks on first use. Declare it, or wrap the "
        "import in a try/except that works without it:\n  " + "\n  ".join(sorted(set(offenders)))
    )


def test_the_exempt_subpackage_really_is_guarded():
    """`vectrixdb/api` may import the api extra at module scope only because
    its `__init__` turns the ImportError into a DependencyError naming the
    extra. If that stops being true the exemption is unearned."""
    for package in GUARDED_SUBPACKAGES:
        source = (ROOT / package / "__init__.py").read_text(encoding="utf-8")
        assert "except ImportError" in source, f"{package}: nothing catches the import"
        assert "DependencyError" in source, f"{package}: the failure is not named"


@pytest.fixture
def reimportable():
    """Put `sys.modules` back afterwards.

    Reimporting the package under a blocked psutil leaves a psutil-less
    `vectrixdb.core.scaling` in `sys.modules` for every test that runs after
    it in the session, which silently turned the with-psutil branch into dead
    code. The originals go back on the way out.
    """
    saved = {name: module for name, module in sys.modules.items() if name.startswith("vectrixdb")}
    yield
    for name in [n for n in sys.modules if n.startswith("vectrixdb")]:
        del sys.modules[name]
    sys.modules.update(saved)


def test_a_collection_works_without_psutil(tmp_path, monkeypatch, reimportable):
    """psutil reads whole-system memory and CPU, which is telemetry, never
    part of answering a query."""
    real_import = builtins.__import__

    def without_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("psutil is not installed")
        return real_import(name, *args, **kwargs)

    for module in [m for m in list(sys.modules) if m.startswith("vectrixdb")]:
        del sys.modules[module]
    monkeypatch.delitem(sys.modules, "psutil", raising=False)
    monkeypatch.setattr(builtins, "__import__", without_psutil)

    scaling = importlib.import_module("vectrixdb.core.scaling")
    assert scaling.psutil is None
    assert scaling.system_memory_available() is False

    monkeypatch.setattr(builtins, "__import__", real_import)

    vectrix = importlib.import_module("vectrixdb").Vectrix
    db = vectrix("clean", path=str(tmp_path))
    db.add(["cats nap on warm sills", "dogs fetch balls in the park"])
    assert [r.text for r in db.search("cats", limit=1)] == ["cats nap on warm sills"]
    assert db.count() == 2
    db.close()


def test_the_memory_monitor_degrades_rather_than_guessing(monkeypatch):
    """Without a reading there is no pressure: firing the callbacks on a
    guess would evict caches for no reason."""
    from vectrixdb.core import scaling

    monkeypatch.setattr(scaling, "psutil", None)
    monitor = scaling.MemoryManager(scaling.ScalingConfig())

    fired = []
    monitor._pressure_callbacks.append(lambda: fired.append(1))

    percent, pressure = monitor.check_memory()
    assert (percent, pressure) == (0.0, False)
    assert fired == []
    assert monitor.get_available_memory_mb() == float("inf")


def test_the_memory_monitor_reads_psutil_when_it_is_there():
    """The other branch. psutil is in the dev extra so CI runs this one too;
    without it the guarded path was the only path anything exercised, which is
    how the bug survived a full suite in the first place."""
    from vectrixdb.core import scaling

    if scaling.psutil is None:
        pytest.skip("psutil is not installed in this environment")

    monitor = scaling.MemoryManager(scaling.ScalingConfig())
    percent, _ = monitor.check_memory()
    assert 0.0 < percent <= 100.0
    assert monitor.get_available_memory_mb() > 0
    assert scaling.system_memory_available() is True
