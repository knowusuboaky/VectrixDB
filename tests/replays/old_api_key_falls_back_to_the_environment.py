"""Restore an explicit empty api_key falling back to the environment.

``api_key or os.environ.get("OPENAI_API_KEY")`` meant api_key="" against a
caller-supplied base_url handed that endpoint the real key.
"""

from vectrixdb.models import openai_compat

_fixed = openai_compat.OpenAIEmbedder.__init__


def _old_init(self, *args, **kwargs):
    if kwargs.get("api_key") == "":
        kwargs["api_key"] = None
    return _fixed(self, *args, **kwargs)


def pytest_configure(config):
    openai_compat.OpenAIEmbedder.__init__ = _old_init
