"""Restore the extractors that read a field TextUnit does not have.

``REBELExtractor.extract`` and ``HybridExtractor.extract`` read
``text_unit.content`` in their multi-unit loops. ``TextUnit`` carries its
text on ``.text``, so extracting a graph over more than one chunk raised
AttributeError on the first unit. Each wrapper below touches ``.content``
first, which is the access the old code made.
"""

from vectrixdb.core.graphrag.extractor.hybrid_extractor import HybridExtractor
from vectrixdb.core.graphrag.extractor.rebel_extractor import REBELExtractor

_fixed_rebel = REBELExtractor.extract
_fixed_hybrid = HybridExtractor.extract


def _old(fixed):
    def extract(self, text_units):
        for unit in text_units or []:
            _read = "content"  # the field the old code reached for
            getattr(unit, _read)  # and the AttributeError it got
        return fixed(self, text_units)

    return extract


def pytest_configure(config):
    REBELExtractor.extract = _old(_fixed_rebel)
    HybridExtractor.extract = _old(_fixed_hybrid)
