"""Masking of identifiers in text that people are shown, and in documents before they are indexed.

Two places, one package. On the way out of a server, :func:`mask_text` and
:func:`mask_value` hide email addresses, phone numbers and card numbers in a
reply to a person or a guest, by their shape, in this process, in microseconds:
a script with an API key is sent the text as stored, because a pipeline needs
it. On the way in, :func:`mask` reads a whole document with the engine the
deployment chose, so what is indexed never held the identifier at all.

    >>> from vectrixdb.masking import mask_text, mask
    >>> mask_text("Call Ada on +1 416 555 0199 or ada@example.com, card 4111 1111 1111 1111.")
    'Call Ada on +• ••• ••• 0199 or a•••@example.com, card •••• •••• •••• 1111.'
    >>> mask("SIN 046-454-286, key AKIAIOSFODNN7EXAMPLE").text
    'SIN [SIN], key [API_KEY]'

Four engines answer :func:`mask`, one call in front of them: the patterns,
always there; Presidio, in this process, ``pip install 'vectrixdb[masking]'``;
Azure AI Language; Amazon Comprehend. ``VECTRIXDB_MASKING_ENGINE`` names one,
or ``auto`` picks Azure AI Language when ``AZURE_LANGUAGE_ENDPOINT`` is set,
else Presidio when it is installed, else the patterns. Comprehend is used when
named, never picked, so a region set for Bedrock never starts billing another
service. Whatever ran, the patterns run after it. ``VECTRIXDB_MASKING_LANGUAGES``
says which languages the documents are in, ``en,fr`` unless you say, which is
what Presidio loads a model for and what an engine is asked to cover.

Masking lowers casual exposure. It is not a guarantee, and the documentation
says so wherever it is offered: a thing no engine knows is shown as stored, and
a search still finds a chunk by a number the index holds as written, which is
why masking at ingestion is the stronger of the two.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from ..exceptions import ConfigurationError, DependencyError
from .engines import ComprehendEngine, LanguageEngine, PresidioEngine, download_models
from .normalize import normalize
from .patterns import (
    DEFAULT_TYPES,
    IN_PLACE,
    MASK,
    TYPES,
    WEIGHTS,
    Found,
    RegexEngine,
    apply,
    score_of,
)

__all__ = [
    "DEFAULT_TYPES",
    "ENGINES",
    "MASK",
    "TYPES",
    "WEIGHTS",
    "ComprehendEngine",
    "Found",
    "LanguageEngine",
    "Masked",
    "PresidioEngine",
    "RegexEngine",
    "describe_engine",
    "download_models",
    "engine_from_env",
    "languages_from_env",
    "mask",
    "mask_text",
    "mask_value",
    "normalize",
    "types_of",
]


# ============================================================================
# SETTINGS: the engines, and the two settings
# ============================================================================
#
# The engine names a deployment may choose, and the variables that name the
# engine and the languages.

#: The engines a deployment may name.
ENGINES: Tuple[str, ...] = ("auto", "regex", "presidio", "language", "comprehend")
#: The setting that names one, and the one that names the documents' languages.
ENGINE_ENV = "VECTRIXDB_MASKING_ENGINE"
LANGUAGES_ENV = "VECTRIXDB_MASKING_LANGUAGES"


# ============================================================================
# ON THE WAY OUT: by shape, where the text stands
# ============================================================================
#
# INPUT   a text, or any value with strings inside
# OUTPUT  emails, phone numbers and card numbers masked where they stand, so a
#         sentence still reads; every string inside a value masked, a result,
#         its metadata, a list of highlights
#
# The read-time middleware calls these on every reply of a masked collection.

_regex = RegexEngine()


def mask_text(text: str) -> str:
    """``text`` with its email addresses, phone numbers and card numbers masked where they stand, by their shape."""
    if not text:
        return text
    return apply(text, _regex.find(text, ("credit_card", "phone", "email")))


def mask_value(
    value: Any, keep: Callable[[str], bool] = lambda key: False, _key: Optional[str] = None
) -> Any:
    """Every string inside ``value`` masked: a result, its metadata, a list of highlights.

    ``keep`` names the keys whose values are left as they are, whole subtrees
    included: an id a caller sends back to open a chunk has to stay the id.
    """
    if _key is not None and keep(_key):
        return value
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {key: mask_value(item, keep, key) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        items = [mask_value(item, keep, None) for item in value]
        return items if isinstance(value, list) else tuple(items)
    return value


# ============================================================================
# BEFORE INDEXING: an engine, then the patterns
# ============================================================================
#
# INPUT   a text, the types to mask, an engine and a language
# OUTPUT  what a text's masking made of it: the text, what was found, how
#         risky it was and who looked; the types to mask, from what a record
#         or a request says; the text read by the engine and then by the
#         patterns, overlaps settled in favour of the earliest and longest
#
# Whatever engine ran, the patterns run after it, so a shape the model missed
# is still caught by its form.


@dataclass
class Masked:
    """What :func:`mask` made of a text: the text, what was found, how risky it was, and who looked."""

    text: str
    found: List[Found] = field(default_factory=list)
    score: float = 0.0
    engine: str = "regex"
    language: Optional[str] = None
    #: True when the named engine does not cover the language, so only the patterns ran.
    regex_only: bool = False

    def to_dict(self) -> Dict[str, Any]:
        counted: Dict[str, int] = {}
        for item in self.found:
            counted[item.type] = counted.get(item.type, 0) + 1
        return {
            "text": self.text,
            "found": [item.to_dict() for item in self.found],
            "counts": counted,
            "score": self.score,
            "engine": self.engine,
            "language": self.language,
            "regex_only": self.regex_only,
        }


def types_of(given: Union[None, bool, str, Iterable[str]]) -> Tuple[str, ...]:
    """The types to mask, from what a record or a request says: nothing, ``all``, a list, or a comma-separated string."""
    if given is None or given is True:
        return DEFAULT_TYPES
    if given is False:
        return ()
    if isinstance(given, str):
        if given.strip().lower() == "all":
            return TYPES
        given = given.split(",")
    out = []
    for name in given:
        cleaned = str(name).strip().lower()
        if not cleaned:
            continue
        if cleaned == "all":
            return TYPES
        if cleaned not in TYPES and cleaned not in ("national_id", "organization"):
            raise ConfigurationError(
                f"{cleaned!r} is not a type that can be masked: {', '.join(TYPES)}"
            )
        if cleaned not in out:
            out.append(cleaned)
    return tuple(out)


def mask(
    text: str,
    *,
    types: Union[None, bool, str, Iterable[str]] = None,
    engine: Any = None,
    language: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Masked:
    """``text`` read by the engine and then by the patterns, with what was found masked.

    ``types`` is what to hide: nothing said means the identifiers, not names;
    ``"all"`` means everything an engine knows. ``engine`` is one of the
    engine objects, or None for the deployment's, from the environment.
    ``language`` is the text's, ``en`` or ``fr``, which Presidio and Comprehend
    need to know and Azure AI Language will detect if not told.
    """
    wanted = types_of(types)
    folded = normalize(text or "")
    if not folded or not wanted:
        return Masked(folded, [], 0.0, "regex", language)
    chosen = engine if engine is not None else engine_from_env(env)
    found: List[Found] = []
    regex_only = False
    if chosen is not None and chosen.name != "regex":
        if chosen.covers(language):
            found.extend(chosen.find(folded, wanted, language))
        else:
            regex_only = True
    found.extend(_regex.find(folded, wanted, language))
    return Masked(
        apply(folded, found),
        _settle(found),
        score_of(found),
        chosen.name if chosen is not None else "regex",
        language,
        regex_only,
    )


def _settle(found: Sequence[Found]) -> List[Found]:
    """What was found, without overlaps: the earliest and longest of two that cover the same characters."""
    kept: List[Found] = []
    for item in sorted(found, key=lambda f: (f.start, -f.end)):
        if kept and item.start < kept[-1].end:
            continue
        kept.append(item)
    return kept


# ============================================================================
# FROM THE ENVIRONMENT
# ============================================================================
#
# INPUT   VECTRIXDB_MASKING_ENGINE, VECTRIXDB_MASKING_LANGUAGES, and the keys
# OUTPUT  the languages, en,fr unless said; the engine the deployment chose,
#         built from its settings, auto picking Azure AI Language when it is
#         set and Presidio when it is installed; the engine as a health reply
#         says it
#
# Comprehend only when named. A bad setting is a ConfigurationError at start,
# not at the first file.


def _secret(env: Mapping[str, str], name: str) -> Optional[str]:
    value = str(env.get(name) or "").strip()
    if value:
        return value
    path = str(env.get(f"{name}_FILE") or "").strip()
    if path:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip() or None
    return None


def languages_from_env(env: Optional[Mapping[str, str]] = None) -> Tuple[str, ...]:
    """The languages the documents are in, ``VECTRIXDB_MASKING_LANGUAGES``, ``en,fr`` unless said."""
    source = os.environ if env is None else env
    named = [
        part.strip().lower().split("-")[0]
        for part in str(source.get(LANGUAGES_ENV) or "en,fr").split(",")
    ]
    out = []
    for lang in named:
        if lang and lang not in out:
            out.append(lang)
    return tuple(out) or ("en",)


def _presidio_installed() -> bool:
    try:
        import presidio_analyzer  # noqa: F401
    except ImportError:
        return False
    return True


def engine_from_env(env: Optional[Mapping[str, str]] = None) -> Any:
    """The engine the deployment chose, ``VECTRIXDB_MASKING_ENGINE``, built from its settings.

    ``auto``, the default, picks Azure AI Language when its endpoint is set,
    else Presidio when it is installed, else the patterns. A named engine
    whose settings or package are missing is a ConfigurationError or a
    DependencyError here, at start, rather than a surprise at the first file.
    """
    source = os.environ if env is None else env
    name = str(source.get(ENGINE_ENV) or "auto").strip().lower()
    if name not in ENGINES:
        raise ConfigurationError(
            f"{ENGINE_ENV} is {name!r}. It can be {', '.join(ENGINES[:-1])} or {ENGINES[-1]}"
        )
    endpoint = str(source.get("AZURE_LANGUAGE_ENDPOINT") or "").strip()
    languages = languages_from_env(source)
    if name == "auto":
        if endpoint:
            name = "language"
        elif _presidio_installed():
            name = "presidio"
        else:
            name = "regex"
    if name == "regex":
        return _regex
    if name == "language":
        if not endpoint:
            raise ConfigurationError(
                f"{ENGINE_ENV}=language needs AZURE_LANGUAGE_ENDPOINT, the Language resource's address"
            )
        return LanguageEngine(endpoint, _secret(source, "AZURE_LANGUAGE_KEY"), languages=languages)
    if name == "presidio":
        if not _presidio_installed():
            raise DependencyError(
                "VECTRIXDB_MASKING_ENGINE=presidio needs Presidio: pip install 'vectrixdb[masking]', then vectrixdb download-models --type masking"
            )
        return PresidioEngine(languages=languages)
    return ComprehendEngine(
        region=str(source.get("AWS_REGION") or source.get("AWS_DEFAULT_REGION") or "").strip()
        or None
    )


def describe_engine(engine: Any, languages: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """The engine as a health reply says it: its name, which of the languages it covers, and that the patterns run last."""
    langs = tuple(languages or languages_from_env())
    name = getattr(engine, "name", "regex")
    return {
        "engine": name,
        "languages": {
            lang: bool(engine.covers(lang)) if engine is not None else True for lang in langs
        },
        "patterns_last": True,
        "types": list(TYPES),
        "default_types": list(DEFAULT_TYPES),
    }
