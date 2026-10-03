"""Azure AI Translator: translate text, say what language it is in, list the languages.

Three calls, one a method, and the shapes they answer in are the library's
rather than the service's, so a caller does not have to know that Azure nests
a translation inside a list inside a list::

    translator = AzureTranslator.from_environment()
    translator.translate("Bonjour tout le monde", to="en")
    # [{"translations": {"en": "Hello everyone"},
    #   "detected": {"language": "fr", "score": 1.0}}]
    translator.detect("Guten Tag")
    # [{"language": "de", "score": 1.0, "translatable": True}]
    translator.languages()["fr"]
    # {"name": "French", "native": "Français", "direction": "ltr"}

Plain REST over the standard library, so it needs no package installed and
works anywhere the library does. ``languages`` needs no key at all; the
other two need one, and a regional or multi-service resource needs its
region as well, which is the usual reason a first call is refused.

The service takes at most a thousand texts and fifty thousand characters a
request. More than that is split into as many requests as it takes, in
order, so a caller hands over a list and gets back a list the same length.
One text longer than fifty thousand characters is refused by name rather
than cut somewhere a sentence did not end.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

from .exceptions import TranslationError

__all__ = ["AzureTranslator"]


# ============================================================================
# SETTINGS: the texts type
# ============================================================================
#
# One text or several, taken the same way.

Texts = Union[str, Sequence[str]]


# ============================================================================
# THE TRANSLATOR
# ============================================================================
#
# INPUT   texts and a language, through Azure AI Translator version 3
# OUTPUT  the translation, what language a text is in, and the languages it
#         knows, in the library's shapes rather than the service's
#
# Three calls, one a method.


class AzureTranslator:
    """Azure AI Translator, version 3.

    ``key`` is the resource's key; ``region`` is its region, which Azure
    requires for a regional or multi-service resource and ignores for a
    global one. ``transport`` replaces the network, for a test: it is
    called as ``transport(method, url, headers, body, timeout)`` and returns
    ``(status, headers, body)``, the same convention as the library's other
    endpoints.
    """

    ENDPOINT = "https://api.cognitive.microsofttranslator.com"
    API = "3.0"
    #: The service's own limits, a request.
    MOST_TEXTS = 1000
    MOST_CHARACTERS = 50_000
    #: A throttled request is tried once more after this long at most, and
    #: then refused, so a caller is never left waiting without being told.
    MOST_WAIT = 10.0

    def __init__(
        self,
        key: str = "",
        *,
        region: str = "",
        endpoint: str = ENDPOINT,
        timeout: float = 30.0,
        transport: Any = None,
    ) -> None:
        self._key = str(key or "")
        self.region = str(region or "")
        self.endpoint = str(endpoint or self.ENDPOINT).rstrip("/")
        self.timeout = float(timeout)
        self._transport = transport

    def __repr__(self) -> str:  # the key is never in it
        return f"{type(self).__name__}({self.endpoint!r}, region={self.region!r})"

    @classmethod
    def from_environment(
        cls, env: Optional[Mapping[str, str]] = None, **kwargs: Any
    ) -> Optional["AzureTranslator"]:
        """One built from ``AZURE_TRANSLATOR_KEY``, ``_REGION`` and ``_ENDPOINT``, or None without a key."""
        found = os.environ if env is None else env
        key = str(found.get("AZURE_TRANSLATOR_KEY") or "").strip()
        if not key:
            return None
        return cls(
            key,
            region=str(found.get("AZURE_TRANSLATOR_REGION") or "").strip(),
            endpoint=str(found.get("AZURE_TRANSLATOR_ENDPOINT") or "").strip() or cls.ENDPOINT,
            **kwargs,
        )

    # -- the three calls

    def translate(
        self, texts: Texts, to: Union[str, Sequence[str]], *, source: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Each text in each language asked for, in the order given.

        ``to`` is one language code or several. ``source`` is the language
        the texts are in; left out, the service works it out and says what
        it decided in ``detected``, which is worth reading, because a short
        text is easy to mistake.
        """
        targets = [to] if isinstance(to, str) else list(to)
        if not targets or not all(isinstance(t, str) and t.strip() for t in targets):
            raise ValueError("to is a language code, like 'fr', or a list of them")
        items = self._texts(texts, len(targets))
        query: List[Any] = [("to", t.strip()) for t in targets]
        if source:
            query.append(("from", source.strip()))
        out: List[Dict[str, Any]] = []
        for batch in self._batches(items, len(targets)):
            for answer in self._post("translate", query, batch):
                found: Dict[str, Any] = {
                    "translations": {
                        str(t.get("to")): str(t.get("text") or "")
                        for t in answer.get("translations") or []
                    }
                }
                detected = answer.get("detectedLanguage")
                if detected:
                    found["detected"] = {
                        "language": detected.get("language"),
                        "score": detected.get("score"),
                    }
                out.append(found)
        return out

    def detect(self, texts: Texts) -> List[Dict[str, Any]]:
        """The language each text is in, and how sure the service is."""
        out: List[Dict[str, Any]] = []
        for batch in self._batches(self._texts(texts)):
            for answer in self._post("detect", [], batch):
                found = {
                    "language": answer.get("language"),
                    "score": answer.get("score"),
                    "translatable": bool(answer.get("isTranslationSupported")),
                }
                alternatives = [
                    {"language": a.get("language"), "score": a.get("score")}
                    for a in answer.get("alternatives") or []
                ]
                if alternatives:
                    found["alternatives"] = alternatives
                out.append(found)
        return out

    def languages(self, scope: str = "translation") -> Dict[str, Dict[str, Any]]:
        """Every language the service can translate to or from, by its code.

        Needs no key. ``scope`` is ``translation``, ``transliteration`` or
        ``dictionary``; the default is the one a caller of ``translate``
        wants.
        """
        status, _headers, body = self._send(
            "GET",
            f"{self.endpoint}/languages?api-version={self.API}&scope={scope}",
            {"Accept": "application/json"},
            b"",
        )
        payload = self._read(status, body, "languages")
        found = payload.get(scope) or {}
        return {
            str(code): {
                "name": info.get("name"),
                "native": info.get("nativeName"),
                "direction": info.get("dir", "ltr"),
            }
            for code, info in sorted(found.items())
        }

    # -- the requests

    @classmethod
    def _most_characters(cls, targets: int = 1) -> int:
        """The characters one request may carry. The service counts the
        limit once per target language, so three targets leave a third."""
        return cls.MOST_CHARACTERS // max(1, int(targets))

    @classmethod
    def _texts(cls, texts: Texts, targets: int = 1) -> List[str]:
        items = [texts] if isinstance(texts, str) else list(texts)
        if not items:
            raise ValueError("there is nothing to translate: texts is empty")
        most = cls._most_characters(targets)
        for text in items:
            if not isinstance(text, str):
                raise TypeError(f"each text is a string, got {type(text).__name__}")
            if len(text) > most:
                into = f" into {targets} languages" if targets > 1 else ""
                raise ValueError(
                    f"one text is {len(text):,} characters and the service takes "
                    f"{most:,} at most{into}; split it where a sentence ends"
                )
        return items

    @classmethod
    def _batches(cls, items: Iterable[str], targets: int = 1) -> Iterable[List[str]]:
        """The texts in as few requests as the service's limits allow, in order."""
        batch: List[str] = []
        characters = 0
        most = cls._most_characters(targets)
        for text in items:
            if batch and (len(batch) >= cls.MOST_TEXTS or characters + len(text) > most):
                yield batch
                batch, characters = [], 0
            batch.append(text)
            characters += len(text)
        if batch:
            yield batch

    def _post(self, path: str, query: List[Any], batch: List[str]) -> List[Dict[str, Any]]:
        if not self._key:
            raise TranslationError(
                f"{path} needs a key: set AZURE_TRANSLATOR_KEY, or pass key= to AzureTranslator",
                route=path,
            )
        from urllib.parse import urlencode

        url = f"{self.endpoint}/{path}?" + urlencode([("api-version", self.API)] + query)
        headers = {
            "Ocp-Apim-Subscription-Key": self._key,
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json",
        }
        if self.region:
            headers["Ocp-Apim-Subscription-Region"] = self.region
        body = json.dumps([{"Text": text} for text in batch], ensure_ascii=False).encode("utf-8")
        status, reply_headers, reply = self._send("POST", url, headers, body)
        if int(status) == 429:
            # Throttled. Once, after what the service asked for, and no longer
            # than MOST_WAIT: a caller waiting a minute unannounced is worse
            # than one told to slow down.
            time.sleep(min(self._retry_after(reply_headers), self.MOST_WAIT))
            status, reply_headers, reply = self._send("POST", url, headers, body)
        answers = self._read(status, reply, path)
        if not isinstance(answers, list) or len(answers) != len(batch):
            raise TranslationError(
                f"{path} answered {len(answers) if isinstance(answers, list) else 'something'} for {len(batch)} texts",
                route=path,
            )
        return answers

    @staticmethod
    def _retry_after(headers: Mapping[str, str]) -> float:
        for key, value in (headers or {}).items():
            if str(key).lower() == "retry-after":
                try:
                    return max(0.0, float(value))
                except ValueError:
                    return 1.0
        return 1.0

    def _send(self, method: str, url: str, headers: Dict[str, str], body: bytes) -> Any:
        from .extract import _urllib_transport

        try:
            return (self._transport or _urllib_transport)(method, url, headers, body, self.timeout)
        except Exception as exc:
            raise TranslationError(f"the translator could not be reached: {exc}") from exc

    @staticmethod
    def _read(status: Any, body: bytes, route: str) -> Any:
        """The answer, or the service's own reason for refusing, in words."""
        try:
            payload = json.loads(body or b"null")
        except ValueError:
            payload = None
        if 200 <= int(status) < 300:
            if payload is None:
                raise TranslationError(
                    f"{route} answered {status} with something that is not JSON",
                    route=route,
                    status=int(status),
                )
            return payload
        reason = ""
        if isinstance(payload, dict):
            error = payload.get("error") or {}
            reason = str(error.get("message") or "") if isinstance(error, dict) else str(error)
        hint = {
            401: " The key is wrong, or it belongs to a regional resource and no region was given.",
            403: " The resource has no quota left, or this operation is not in its tier.",
        }.get(int(status), "")
        raise TranslationError(
            f"{route} was refused, {status}{': ' + reason if reason else ''}.{hint}",
            route=route,
            status=int(status),
        )
