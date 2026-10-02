"""e5-small-v2 left the wheel, and collections built with it still open.

Two claims, held apart. The packaging one: the wheel and the sdist exclude
the model, which is what buys the room. The behaviour one: a machine without
the model gets an error that names both ways out, fetching it once or
re-embedding to the current default, and a machine that allows first-use
downloads fetches it. Neither test touches the network.
"""

from __future__ import annotations

import shutil
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 and 3.10
    import tomli as tomllib  # type: ignore[no-redef]

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUNDLED = ROOT / "vectrixdb" / "models" / "data"


class TestPackaging:
    def test_the_wheel_and_the_sdist_leave_it_out(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        targets = pyproject["tool"]["hatch"]["build"]["targets"]
        for name in ("wheel", "sdist"):
            assert "vectrixdb/models/data/dense_en" in targets[name]["exclude"], name

    def test_the_default_model_is_still_in(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        excluded = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["exclude"]
        assert "vectrixdb/models/data/bge_small_en" not in excluded

    def test_the_list_of_bundled_models_matches_the_build(self):
        """The models route says which models came in the wheel; it reads this
        list, and the build decides. They must agree."""
        from vectrixdb.models.embedded import WHEEL_MODEL_DIRS

        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        excluded = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["exclude"]
        dropped = {path.rsplit("/", 1)[-1] for path in excluded}
        assert not dropped & set(WHEEL_MODEL_DIRS), dropped & set(WHEEL_MODEL_DIRS)
        for folder in WHEEL_MODEL_DIRS:
            assert (BUNDLED / folder).is_dir(), f"{folder} is listed as bundled and is not here"

    def test_the_registry_says_it_is_fetched_not_bundled(self):
        from vectrixdb.models.embedded import MODEL_CONFIG

        assert "not in the wheel" in MODEL_CONFIG["dense_en"]["description"]
        assert MODEL_CONFIG["dense_en"]["github_release"] == "dense-en"


@pytest.fixture
def models_without_e5(tmp_path, monkeypatch):
    """A models directory that has everything the wheel ships and not e5:
    what a fresh install looks like."""
    target = tmp_path / "models"
    target.mkdir()
    for name in ("bge_small_en", "sparse"):
        shutil.copytree(BUNDLED / name, target / name)
    monkeypatch.setenv("VECTRIXDB_MODELS_DIR", str(target))
    monkeypatch.delenv("VECTRIXDB_AUTO_DOWNLOAD", raising=False)
    return target


class TestFirstUse:
    def test_without_the_model_the_error_names_both_ways_out(self, models_without_e5, monkeypatch):
        from vectrixdb.exceptions import ModelDownloadError
        from vectrixdb.models.embedded import DenseEmbedder

        monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
        with pytest.raises(ModelDownloadError) as info:
            DenseEmbedder(model="e5-small").embed(["covenant"])

        message = str(info.value)
        assert "download-models --type dense_en" in message
        assert 'dense_model="bge-small").reembed()' in message
        assert "before VectrixDB 2.2" in message

    def test_allowed_first_use_fetches_it_once(self, models_without_e5, monkeypatch):
        """The download itself is stood in for by copying the real files, so
        the test proves the wiring and not GitHub."""
        from vectrixdb.models import downloader
        from vectrixdb.models.embedded import DenseEmbedder

        fetched = []

        def fake_download(self, model_type):
            fetched.append(model_type)
            shutil.copytree(BUNDLED / model_type, models_without_e5 / model_type)
            return models_without_e5 / model_type

        monkeypatch.setattr(downloader.ModelDownloader, "download", fake_download)
        monkeypatch.setenv("VECTRIXDB_AUTO_DOWNLOAD", "1")

        vectors = DenseEmbedder(model="e5-small").embed(["covenant"])
        assert fetched == ["dense_en"]
        assert len(vectors[0]) == 384

    def test_the_default_model_needs_no_network(self, models_without_e5, monkeypatch):
        from vectrixdb.models.embedded import DenseEmbedder

        monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
        assert len(DenseEmbedder(model="bge-small").embed(["covenant"])[0]) == 384

    def test_the_cli_offers_it(self):
        import argparse

        from vectrixdb.models import downloader

        parser_source = Path(downloader.__file__).read_text(encoding="utf-8")
        assert '"dense_en",' in parser_source[parser_source.index("def download_models_cli") :]
        assert hasattr(downloader.ModelDownloader, "_download_dense_en")
        assert argparse  # imported to mirror the CLI's own dependency
