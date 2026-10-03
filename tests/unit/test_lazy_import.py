"""`import vectrixdb` must cost what you use, not everything the package can do.

It used to pull in numpy, usearch, asyncio, every storage backend and GraphRAG
before a single call: 585 ms warm, 1.2 s cold. Public names now resolve on
first access. These pin the shape of that, not a millisecond figure, because
timing tests on shared CI machines are noise.
"""

import json
import subprocess
import sys

import pytest

HEAVY = [
    "vectrixdb.easy",
    "vectrixdb.core.database",
    "vectrixdb.core.collection",
    "vectrixdb.core.graphrag",
    "vectrixdb.core.storage",
    "vectrixdb.api",
    "numpy",
    "onnxruntime",
    "usearch",
    "fastapi",
]


def _in_fresh_interpreter(code: str) -> dict:
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out.strip().splitlines()[-1])


class TestNothingHeavyOnImport:
    def test_import_loads_no_heavy_module(self):
        loaded = _in_fresh_interpreter(
            "import sys, json; import vectrixdb; "
            "print(json.dumps(sorted(m for m in sys.modules if m.startswith('vectrixdb') "
            "or m in %r)))" % HEAVY
        )
        heavy = [m for m in loaded if m in HEAVY]
        assert not heavy, f"import vectrixdb pulled in {heavy}"

    def test_accessing_vectrix_loads_it(self):
        loaded = _in_fresh_interpreter(
            "import sys, json; import vectrixdb; vectrixdb.Vectrix; "
            "print(json.dumps('vectrixdb.easy' in sys.modules))"
        )
        assert loaded is True

    @pytest.mark.perf
    def test_import_is_fast_enough_to_notice_a_regression(self):
        """Generous on purpose: a return to eager imports is 5x this."""
        ms = float(
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import time; t=time.perf_counter(); import vectrixdb; "
                    "print((time.perf_counter()-t)*1000)",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            .stdout.strip()
            .splitlines()[-1]
        )
        assert ms < 400, f"import vectrixdb took {ms:.0f} ms"


class TestTheSurfaceIsIntact:
    def test_every_public_name_resolves(self):
        import vectrixdb

        missing = []
        for name in vectrixdb.__all__:
            try:
                getattr(vectrixdb, name)
            except AttributeError:
                missing.append(name)
        assert not missing

    def test_dir_lists_lazy_names_before_they_are_touched(self):
        listing = _in_fresh_interpreter("import json, vectrixdb; print(json.dumps(dir(vectrixdb)))")
        assert "Vectrix" in listing and "VectrixDB" in listing

    def test_version_is_a_string(self):
        import vectrixdb

        assert isinstance(vectrixdb.__version__, str) and vectrixdb.__version__

    def test_alias_is_the_same_object(self):
        import vectrixdb

        assert vectrixdb.V is vectrixdb.Vectrix

    def test_star_import_works(self):
        namespace = {}
        exec("from vectrixdb import *", namespace)
        assert "Vectrix" in namespace and "SearchError" in namespace

    def test_unknown_attribute_is_still_an_error(self):
        import vectrixdb

        with pytest.raises(AttributeError):
            vectrixdb.definitely_not_a_thing  # noqa: B018

    def test_graphrag_flag(self):
        import vectrixdb

        assert vectrixdb.GRAPHRAG_AVAILABLE is True
        assert vectrixdb.GraphRAGConfig is not None
