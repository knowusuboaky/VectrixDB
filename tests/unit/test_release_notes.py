"""scripts/release_notes.py: the release's notes are the changelog's, and an undated section cannot go out."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _script():
    spec = importlib.util.spec_from_file_location(
        "release_notes", ROOT / "scripts" / "release_notes.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHANGELOG = """# Changelog

## Deprecation policy

Words.

## [2.3.0] - Unreleased

### Added

- Something new.

## [2.2.0] - 2026-10-03

### Fixed

- **Ties fall the same way.** Under a policy.

## [2.1.7] and earlier

Old.
"""


def test_the_notes_are_the_section_and_nothing_after_it():
    notes = _script().section(CHANGELOG, "2.2.0")
    assert notes == ("2026-10-03", "### Fixed\n\n- **Ties fall the same way.** Under a policy.")


def test_a_dated_section_may_be_released():
    assert _script().problems(CHANGELOG, "2.2.0") == []


def test_an_unreleased_section_may_not():
    said = _script().problems(CHANGELOG, "2.3.0")
    assert len(said) == 1 and "'Unreleased'" in said[0] and "YYYY-MM-DD" in said[0]


def test_a_version_with_no_section_may_not():
    said = _script().problems(CHANGELOG, "9.9.9")
    assert said and "no section for 9.9.9" in said[0]


def test_the_command_line_drops_a_leading_v_and_refuses_with_exit_1(tmp_path, capsys):
    log = tmp_path / "CHANGELOG.md"
    log.write_text(CHANGELOG, encoding="utf-8")
    script = _script()
    assert script.main(["v2.2.0", "--check", "--changelog", str(log)]) == 0
    assert "Ties fall the same way" in capsys.readouterr().out
    assert script.main(["2.3.0", "--check", "--changelog", str(log)]) == 1
    assert "Unreleased" in capsys.readouterr().err


def test_the_declared_version_is_read_from_pyproject(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\nversion = "2.2.0"\n', encoding="utf-8")
    assert _script().declared_version(pyproject) == "2.2.0"


def test_the_release_workflow_reads_its_notes_from_here():
    flow = (ROOT / ".github" / "workflows" / "python-publish.yml").read_text(encoding="utf-8")
    assert "scripts/release_notes.py" in flow and "--check" in flow
    assert "workflow_dispatch" in flow and "environment: pypi" in flow


def test_a_section_too_long_for_a_release_becomes_its_lead_ins_and_a_link():
    script = _script()
    long_entry = "- **Ties fall the same way.** " + "detail " * 50
    text = CHANGELOG.replace("- **Ties fall the same way.** Under a policy.", long_entry)
    brief = script.notes(text, "2.2.0", limit=200)
    assert brief.startswith("### Fixed\n\n- Ties fall the same way.")
    assert "detail" not in brief
    assert "CHANGELOG.md#220---2026-10-03" in brief and len(brief) <= 200


def test_the_real_changelog_fits_on_a_release():
    script = _script()
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    version = script.declared_version()
    body = script.notes(text, version)
    assert body is not None and len(body) <= script.LIMIT
