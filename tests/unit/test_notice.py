"""The NOTICE file: VectrixDB's own attribution, and every model the library carries or downloads, with its licence.

Apache 2.0 has a NOTICE file travel with the work wherever it goes, and the
models keep their authors' licences, so a model added to the library without
a line here would be passed on without its terms. The ids are read from the
model registries themselves, so this fails the day one is added and not
listed.
"""

from __future__ import annotations

import re
from pathlib import Path

import vectrixdb

REPO = Path(vectrixdb.__file__).resolve().parents[1]
NOTICE = REPO / "NOTICE"
MODELS = REPO / "vectrixdb" / "models"


def model_ids() -> set:
    ids = set()
    for source in MODELS.glob("*.py"):
        ids.update(re.findall(r'"huggingface_id":\s*"([^"]+)"', source.read_text(encoding="utf-8")))
    return ids


class TestTheNotice:
    def test_it_says_whose_work_this_is_and_under_what_licence(self):
        text = NOTICE.read_text(encoding="utf-8")
        assert text.startswith("VectrixDB\nCopyright 2026 Kwadwo Daddy Nyame Owusu - Boakye\n")
        assert "Apache License, Version 2.0" in text and (REPO / "LICENSE").exists()

    def test_every_model_the_library_knows_is_listed_with_a_licence(self):
        text = NOTICE.read_text(encoding="utf-8")
        ids = model_ids()
        assert len(ids) >= 10, "the registries were not found where this looks"
        for model in sorted(ids):
            line = next((row for row in text.splitlines() if model in row.split()), None)
            assert line is not None, f"{model} is not in NOTICE"
            assert re.search(r"\b(MIT|Apache-2\.0|CC-BY-NC-SA-4\.0)\b", line), (
                f"{model} has no licence beside it"
            )

    def test_the_non_commercial_model_is_said_plainly(self):
        text = NOTICE.read_text(encoding="utf-8")
        assert "Babelscape/mrebel-base" in text and "non-commercial use only" in text

    def test_the_model_folders_it_carries_are_listed(self):
        """The package carries these; data/sparse is the library's own word weights, nobody else's work."""
        text = NOTICE.read_text(encoding="utf-8")
        for folder in ("bge_small_en", "colbert", "reranker_en", "dense_en"):
            assert f"vectrixdb/models/data/{folder}" in text, folder
