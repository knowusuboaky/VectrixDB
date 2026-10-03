"""Nothing downloads at import or first use unless asked.

VECTRIXDB_OFFLINE refuses every download. Without VECTRIXDB_AUTO_DOWNLOAD, a
first use that would need a model raises and names the explicit command,
instead of quietly reaching for the network.
"""

import logging
import sys
import types

import pytest

from vectrixdb import _net
from vectrixdb.exceptions import ModelDownloadError


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("VECTRIXDB_OFFLINE", raising=False)
    monkeypatch.delenv("VECTRIXDB_AUTO_DOWNLOAD", raising=False)


class TestSwitches:
    def test_defaults(self):
        assert not _net.offline()
        assert not _net.auto_download_allowed()

    @pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
    def test_offline_values(self, monkeypatch, value):
        monkeypatch.setenv("VECTRIXDB_OFFLINE", value)
        assert _net.offline()

    def test_offline_wins_over_auto_download(self, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
        monkeypatch.setenv("VECTRIXDB_AUTO_DOWNLOAD", "1")
        assert not _net.auto_download_allowed()

    def test_assert_network_names_the_switch(self, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
        with pytest.raises(ModelDownloadError, match="VECTRIXDB_OFFLINE"):
            _net.assert_network("download the dense model")

    def test_refuse_implicit_names_the_command(self):
        err = _net.refuse_implicit(
            "The multilingual model", "vectrixdb download-models --type dense"
        )
        assert "vectrixdb download-models --type dense" in str(err)
        assert "VECTRIXDB_AUTO_DOWNLOAD" in str(err)


class TestDownloaderRespectsOffline:
    def test_explicit_download_is_refused_before_any_network(self, monkeypatch):
        from vectrixdb.models import downloader

        monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")

        def no_network(*args, **kwargs):
            raise AssertionError("the network was touched")

        monkeypatch.setattr(downloader, "urlopen", no_network)
        with pytest.raises(ModelDownloadError, match="VECTRIXDB_OFFLINE"):
            downloader.ModelDownloader(progress=False).download("dense")


class TestExtractorDoesNotFetchModels:
    """spaCy installed, model missing: the old code downloaded it silently."""

    @pytest.fixture
    def fake_spacy(self, monkeypatch):
        calls = []
        module = types.ModuleType("spacy")
        cli = types.ModuleType("spacy.cli")

        def load(name):
            if calls:
                return object()  # after a "download", loading succeeds
            raise OSError("model not found")

        def download(name):
            calls.append(name)

        module.load = load
        cli.download = download
        module.cli = cli
        monkeypatch.setitem(sys.modules, "spacy", module)
        monkeypatch.setitem(sys.modules, "spacy.cli", cli)
        return calls

    def test_missing_model_is_not_downloaded_by_default(self, fake_spacy, caplog):
        from vectrixdb.core.graphrag.extractor import nlp_extractor

        caplog.set_level(logging.WARNING, logger="vectrixdb")
        extractor = nlp_extractor.NLPExtractor(model="en_core_web_sm")
        assert fake_spacy == [], "the model was downloaded without being asked"
        assert extractor._nlp is None, "it falls back to the regex extractor"
        assert any("spacy download" in r.getMessage() for r in caplog.records)

    def test_missing_model_is_downloaded_when_allowed(self, fake_spacy, monkeypatch):
        from vectrixdb.core.graphrag.extractor import nlp_extractor

        monkeypatch.setenv("VECTRIXDB_AUTO_DOWNLOAD", "1")
        extractor = nlp_extractor.NLPExtractor(model="en_core_web_sm")
        assert fake_spacy == ["en_core_web_sm"]
        assert extractor._nlp is not None
