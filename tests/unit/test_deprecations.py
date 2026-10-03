"""Names on their way out warn before they go.

The governance rule in CONTRIBUTING is one minor release of
DeprecationWarning before a public name is removed. These tests hold the
warning in place for the 2.2 names and, by reading the same table the
package uses, cannot drift from it.

The lazy loader caches a resolved name into the package's globals, so the
warning fires once per process. To see it again, drop that one cached
attribute. Deleting ``vectrixdb`` from ``sys.modules`` would do it too, but
the re-imported package object then has no attribute for any submodule
imported earlier, and anything that later resolves a dotted path such as
``vectrixdb.mcp_server.build_server`` fails in a way that is very hard to
trace back to here.
"""

from __future__ import annotations

import importlib
import warnings

import pytest

vectrixdb = importlib.import_module("vectrixdb")
DEPRECATED = sorted(vectrixdb._DEPRECATED)


def _rearm(name: str):
    """Forget the cached attribute so the lazy loader runs again."""
    vectrixdb.__dict__.pop(name, None)
    return vectrixdb


@pytest.mark.parametrize("name", DEPRECATED)
def test_deprecated_name_warns_and_still_imports(name):
    module = _rearm(name)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        value = getattr(module, name)

    assert value is not None, (
        f"{name} no longer resolves; remove it from _DEPRECATED and public_api.txt together"
    )
    messages = [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]
    assert any(name in m and "2.3" in m and "use " in m for m in messages), messages


def test_live_names_do_not_warn():
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert _rearm("Vectrix").Vectrix is not None
        assert _rearm("VectrixDB").VectrixDB is not None


def test_every_deprecated_name_is_in_the_public_api_snapshot():
    """The snapshot is the list the governance test guards. A name cannot be
    deprecated here and quietly missing there."""
    from pathlib import Path

    snapshot = Path(__file__).with_name("public_api.txt").read_text(encoding="utf-8").split()
    missing = [n for n in DEPRECATED if n not in snapshot]
    assert not missing, missing
