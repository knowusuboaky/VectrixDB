"""Paths that break libraries: spaces, unicode, and Windows' 260-character limit."""

import os
import sys

import pytest

from vectrixdb import Vectrix


def test_spaces_and_unicode_in_the_path(tmp_path):
    path = tmp_path / "my notes" / "über" / "日本語"
    db = Vectrix("p", path=str(path))
    db.add(["alpha beta"])
    db.close()
    assert Vectrix("p", path=str(path)).search("alpha", limit=1).top.text == "alpha beta"


def test_a_path_longer_than_260_characters(tmp_path):
    deep = tmp_path
    while len(str(deep)) < 300:
        deep = deep / ("segment_" + "x" * 40)
    try:
        deep.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if sys.platform == "win32":
            pytest.skip(f"long paths are disabled on this Windows host: {exc}")
        raise
    db = Vectrix("p", path=str(deep))
    db.add(["gamma delta"])
    db.close()
    assert Vectrix("p", path=str(deep)).count() == 1
    assert os.path.exists(deep)
