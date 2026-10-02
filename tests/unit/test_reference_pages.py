"""The reference pages are read off the code, and a page that has fallen behind it fails.

docs/reference/settings.md, cli.md and rest-api.md are written by
scripts/make_reference.py: every setting, every command and option, every
documented route with who may call it. A setting, an option or a route added
without running the script would otherwise go unmentioned in the docs.
"""

import importlib.util
import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_reference.py"


def test_the_reference_pages_match_the_code():
    pytest.importorskip(
        "fastapi", reason="the REST page is read off the server, which needs the API extra"
    )
    spec = importlib.util.spec_from_file_location("make_reference", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--check"]) == 0, (
        "run python scripts/make_reference.py and keep what it writes"
    )


def test_the_error_catalogue_reads_every_refusal_off_the_code():
    """A refusal the scan cannot read would be a refusal the page does not show, so the scan is held to what the code has."""
    pytest.importorskip(
        "fastapi",
        reason="the page's server section is read off the server, which needs the API extra",
    )
    spec = importlib.util.spec_from_file_location("make_reference", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = module.ROOT
    for relative, _heading in module._REFUSAL_FILES:
        source = (root / relative).read_text(encoding="utf-8")
        one_liners = len(
            re.findall(
                r"(?:refusal|_Refused|_refuse|again)\(\s*\d{3}|HTTPException\(status_code=\d{3}",
                source,
            )
        )
        assert len(module.refusals_of(root / relative)) >= one_liners, (
            f"{relative}: a refusal the page cannot read"
        )
    documents = module.refusals_of(root / "vectrixdb" / "api" / "documents.py")
    assert any(
        status == "415" and message.startswith("A multipart form needs python-multipart")
        for status, message, _ in documents
    ), "a refusal spread over lines is read whole"
    signin = {
        message: status
        for status, message, _ in module.refusals_of(root / "vectrixdb" / "api" / "signin.py")
    }
    assert signin["Sign in to continue"] == "401" and "the error's own words" in signin, (
        "a str(exc) reads as what it is"
    )
    assert any(
        message.startswith("That code was not accepted")
        and ", or That password and code" in message
        for message in signin
    ), "a conditional constant shows both sides"
    page = module.errors_page()
    statuses = set(re.findall(r"^\| (\d{3})(?: or \d{3})? \| ", page, re.M)) | set(
        re.findall(r"^\| \d{3} or (\d{3}) \| ", page, re.M)
    )
    explained = set(re.findall(r"^\| (\d{3}) \| [A-Z]", page, re.M))
    assert statuses <= explained, (
        f"a status without its row in the intro: {sorted(statuses - explained)}"
    )
    assert (
        "| 401 | Sign in to continue | " in page
        and "| 404 | Collection '{name}' not found |" in page
    )
    assert (
        "| 503 | nothing reads sound: set AZURE_SPEECH_ENDPOINT and AZURE_SPEECH_KEY |" in page
    ), "the extraction service's refusals are there"
    assert "| `CollectionStoreUnavailable` | `PolicyError` |" in page, (
        "every exception the library defines is listed"
    )
    assert "status_code=501" not in (root / "vectrixdb" / "api" / "documents.py").read_text(
        encoding="utf-8"
    ), "a missing reader is 503 everywhere, as the extraction service says it"
