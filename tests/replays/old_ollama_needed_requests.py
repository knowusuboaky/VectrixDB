"""Restore the Ollama fallback that imported requests, which the package never declared.

Without the ``ollama`` package the LLM extractor fell back to ``requests``,
so on a machine without that too, which a clean install is, building the
extractor raised ImportError from a dependency nobody had been told about.
"""

from vectrixdb.core.graphrag.extractor.llm_extractor import LLMExtractor


def _old_init_ollama(self):
    try:
        import ollama

        self._client = ollama
        self._endpoint = self.endpoint or "http://localhost:11434"
        self._call_fn = self._call_ollama
    except ImportError:
        import requests

        self._client = requests
        self._endpoint = self.endpoint or "http://localhost:11434"
        self._call_fn = self._call_ollama_http


def pytest_configure(config):
    LLMExtractor._init_ollama = _old_init_ollama
