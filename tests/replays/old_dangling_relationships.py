"""Restore the entity deduplication that broke its own relationships.

``NLPExtractor.extract`` deduplicates entities across text units by name and
then looked up relationship endpoints by exact id, so a relationship whose
entity lost the id race was discarded. ``_extract_with_pipe`` kept the
relationship and left it pointing at an entity id that is not in the result.
Both are restored here by putting the endpoints back to ids that no longer
survive deduplication.
"""

from vectrixdb.core.graphrag.extractor.nlp_extractor import NLPExtractor

_fixed_extract = NLPExtractor.extract
_fixed_pipe = NLPExtractor._extract_with_pipe


def _dangle(fixed):
    def call(self, *args, **kwargs):
        result = fixed(self, *args, **kwargs)
        # The old code left endpoints pointing at merged-away ids. Point
        # every relationship at an id nothing holds, which is the same
        # observable state.
        for rel in result.relationships:
            rel.source_id = rel.source_id + "-merged-away"
        return result

    return call


def pytest_configure(config):
    NLPExtractor.extract = _dangle(_fixed_extract)
    NLPExtractor._extract_with_pipe = _dangle(_fixed_pipe)
