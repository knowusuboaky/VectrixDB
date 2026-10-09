"""scripts/release.py: the four clients go out at the version PyPI gets."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _script():
    spec = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = _script()


@pytest.mark.parametrize(
    "pypi, clients",
    [
        ("2.2.0", "2.2.0"),
        ("2.3.0rc1", "2.3.0-rc.1"),
        ("2.3.0b2", "2.3.0-beta.2"),
        ("2.3.0a1", "2.3.0-alpha.1"),
    ],
)
def test_a_pre_release_is_written_as_npm_crates_and_go_take_it(pypi, clients):
    assert release.client_version(pypi) == clients


def _declared() -> str:
    """The version pyproject.toml declares."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)


def test_every_client_says_the_version_pyproject_does():
    declared = _declared()
    wanted = release.client_version(declared)
    assert release.client_versions() == dict.fromkeys(release.CLIENTS, wanted)
    assert release.check_clients(declared) == 0


def test_the_check_names_a_client_that_says_another_version(capsys):
    assert release.check_clients("9.9.9") == 1
    printed = capsys.readouterr().out
    assert "sdk/rust/Cargo.toml says" in printed
    assert "python scripts/release.py 9.9.9" in printed


def test_stamping_changes_the_version_and_nothing_else():
    stamped = release.stamp_clients("2.9.0rc1")
    assert {p.relative_to(ROOT).as_posix() for p in stamped} == {
        "sdk/typescript/package.json",
        "sdk/typescript/package-lock.json",
        "sdk/typescript/src/index.ts",
        "sdk/rust/Cargo.toml",
        "sdk/rust/Cargo.lock",
        "sdk/go/client.go",
    }
    for path, text in stamped.items():
        before = path.read_text(encoding="utf-8").splitlines()
        after = text.splitlines()
        changed = [(a, b) for a, b in zip(before, after) if a != b]
        assert len(before) == len(after)
        assert changed, path
        for old, new in changed:
            assert "2.9.0-rc.1" in new
            assert old.replace(release.client_version(_declared()), "2.9.0-rc.1") == new
