"""Restore the WordPiece scan with no cap on word length.

``SimpleTokenizer._wordpiece_tokenize`` tried the longest match first and
shortened by one on every miss, slicing a fresh substring each time, so its
cost grew with the square of the word it was given. A 4,000 character token
took 7 seconds, 8,000 took 40, and 50,000 never finished. One base64 blob or
one line of minified JavaScript in a corpus was enough to stop an ingest.
"""

from vectrixdb.models.embedded import SimpleTokenizer

_CAP = SimpleTokenizer.MAX_CHARS_PER_WORD


def pytest_configure(config):
    # The guard is a single comparison against this number. Raising it past
    # any word length restores the unbounded scan exactly.
    SimpleTokenizer.MAX_CHARS_PER_WORD = 10**9
