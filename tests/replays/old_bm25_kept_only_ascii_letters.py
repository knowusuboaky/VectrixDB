"""Restore the word pattern that only knew ASCII letters and digits.

It kept "fung" out of "Prüfung", indexed "prêt" as "pr" and "t", dropped
Thai entirely, and ran the English stemmer on German because German happens
to be ASCII. Every language with an accent had a sparse index full of
fragments, and hybrid search on it was dense search with noise added.

    python scripts/replay.py tests/replays/old_bm25_kept_only_ascii_letters.py \
        tests/unit/test_bm25.py
"""

import re

from vectrixdb.core.collection import TextIndex

_OLD = re.compile(r"[a-z0-9]+|[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+")


def _old_split(cls, text):
    out = []
    for match in _OLD.finditer(text.lower()):
        token = match.group(0)
        if token[0].isascii() or len(token) == 1:
            out.append(token)
        else:
            out.extend(token[i : i + 2] for i in range(len(token) - 1))
    return out


def pytest_configure(config):
    TextIndex._split = classmethod(_old_split)
