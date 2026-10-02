"""A chat model behind an OpenAI style chat completions route, asked for JSON.

The one call behind :class:`~vectrixdb.extract.describers.ChatDescriber`,
which describes a picture, and :class:`~vectrixdb.evaluation.ChatWriter`,
which writes golden questions: Azure OpenAI, a model on Azure AI Foundry,
OpenAI, or a server on your own machine such as vLLM or Ollama.

What the two share is what makes a small or a strict service usable. The
answer is asked for as JSON at temperature 0. A service that turns a setting
down by name, a model that takes no ``temperature`` or wants
``max_completion_tokens``, is asked again without it, and the change is kept
for the calls after it. A service that is throttled or down is waited for,
as long as it says and never past ``max_wait``, ``tries`` times. A refusal
is no answer, never an error, and :attr:`ChatRoute.failure` says what the
service last said when it gave none.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse


# ============================================================================
# SETTINGS: the logger, and the transport type
# ============================================================================
#
# A transport is what the tests hand in place of a real HTTP client.

logger = logging.getLogger(__name__)

Transport = Callable[[str, str, Dict[str, str], bytes, float], Tuple[int, Mapping[str, str], bytes]]


# ============================================================================
# THE ANSWER AND THE TOKEN
# ============================================================================
#
# INPUT   a model's answer; the host's identity
# OUTPUT  the JSON object in the answer, bare, fenced or with words around it,
#         or None when there is none; a Bearer token for Azure AI services, or
#         None without azure-identity
#
# Models wrap JSON in fences and prose; the object is found wherever it sits.


def json_in(text: str) -> Optional[Dict[str, Any]]:
    """The JSON object in a model's answer, bare, fenced or with words around it; None when there is none."""
    text = text.strip()
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def managed_identity() -> Optional[Callable[[], str]]:
    """A Bearer token for Azure AI services from the host's identity, or None without ``azure-identity``."""
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError:
        return None
    credential = DefaultAzureCredential()

    def token() -> str:
        return credential.get_token("https://cognitiveservices.azure.com/.default").token

    return token


# ============================================================================
# THE ROUTE: one chat completions call
# ============================================================================
#
# INPUT   an OpenAI style chat completions route, its key or a managed
#         identity, and the messages
# OUTPUT  the model's text, asked for JSON
#
# The one call behind ChatDescriber, which describes a picture, and
# ChatWriter, which writes golden questions, so both talk to a model the same
# way.


class ChatRoute:
    """One chat completions route, called with messages and answering with text.

    :meth:`azure_openai` builds one for an Azure OpenAI deployment;
    otherwise ``url`` is the whole address of the route, and ``model`` names
    the model for a service that serves more than one. ``key`` goes in
    ``key_header``, as a Bearer token when that is ``Authorization``;
    ``token``, a callable, gives a fresh Bearer token per call instead, for a
    managed identity.
    """

    label = "chat-model"
    #: The Azure OpenAI version that takes JSON answers, generally available.
    AZURE_API_VERSION = "2024-10-21"
    #: The settings a service may turn down, dropped one at a time when it names them.
    _OPTIONAL = ("temperature", "response_format")
    #: The setting that names an Azure OpenAI deployment for this use, and the
    #: four that name any other route: its address, key, model and key's header.
    _AZURE_DEPLOYMENT = ""
    _ROUTE: Tuple[str, str, str, str] = ("", "", "", "")
    #: What a call that got no answer is logged as, after the label.
    _FAILED = "did not answer"
    #: What is lost when the deployment is named with nothing to sign in with.
    _WITHOUT = "it is not asked"

    def __init__(
        self,
        url: str,
        *,
        key: Optional[str] = None,
        token: Optional[Callable[[], str]] = None,
        model: Optional[str] = None,
        key_header: str = "Authorization",
        label: Optional[str] = None,
        max_tokens: int = 1500,
        timeout: float = 90.0,
        tries: int = 3,
        max_wait: float = 20.0,
        transport: Optional[Transport] = None,
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"url is the chat completions address, got {url!r}")
        self.url = url
        self._key = key
        self._token = token
        self.model = model or None
        self.key_header = key_header
        self.label = label or model or type(self).label
        self.max_tokens = int(max_tokens)
        self.timeout = float(timeout)
        self.tries = max(int(tries), 1)
        self.max_wait = float(max_wait)
        if transport is None:
            from .extract import _urllib_transport

            transport = _urllib_transport
        self._transport = transport
        self._settings: Dict[str, Any] = {
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        self._limit = "max_tokens"
        #: The status and the start of what the service said, the last time a call got no answer.
        self.failure: Optional[Tuple[int, str]] = None

    def __repr__(self) -> str:  # the key is not in it, and never should be
        return f"{type(self).__name__}({self.url.split('?')[0]!r}, label={self.label!r})"

    @classmethod
    def azure_openai(
        cls,
        endpoint: str,
        deployment: str,
        *,
        key: Optional[str] = None,
        token: Optional[Callable[[], str]] = None,
        api_version: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        """One for an Azure OpenAI deployment."""
        if not (endpoint and deployment):
            raise ValueError(
                "an Azure OpenAI deployment is called with the resource's endpoint and the deployment's name"
            )
        url = (
            f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions"
            f"?api-version={api_version or cls.AZURE_API_VERSION}"
        )
        kwargs.setdefault("label", deployment)
        return cls(
            url, key=key, token=token, key_header="api-key" if key else "Authorization", **kwargs
        )

    @classmethod
    def from_environment(cls, env: Optional[Mapping[str, str]] = None, **kwargs: Any) -> Any:
        """One from the settings, or None when they name none.

        The class's Azure OpenAI deployment setting with ``AZURE_OPENAI_ENDPOINT``
        is an Azure OpenAI deployment, called with ``AZURE_OPENAI_KEY``, or with
        the managed identity when there is no key; ``AZURE_OPENAI_API_VERSION``
        changes the version. Otherwise the first of the class's route settings
        is any chat completions route, with the key, the model and the key's
        header the other three name.
        """
        found = os.environ if env is None else env

        def get(name: str) -> str:
            return str(found.get(name) or "").strip()

        deployment, endpoint = get(cls._AZURE_DEPLOYMENT), get("AZURE_OPENAI_ENDPOINT")
        if deployment and endpoint:
            key = get("AZURE_OPENAI_KEY") or None
            token = None if key else managed_identity()
            if key or token:
                return cls.azure_openai(
                    endpoint,
                    deployment,
                    key=key,
                    token=token,
                    api_version=get("AZURE_OPENAI_API_VERSION") or None,
                    **kwargs,
                )
            logger.warning(
                "%s is set with no AZURE_OPENAI_KEY and no managed identity to use, so %s",
                cls._AZURE_DEPLOYMENT,
                cls._WITHOUT,
            )
        at, key, model, header = cls._ROUTE
        url = get(at) if at else ""
        if url:
            return cls(
                url,
                key=get(key) or None,
                model=get(model) or None,
                key_header=get(header) or "Authorization",
                **kwargs,
            )
        return None

    # -- the call

    def _auth(self) -> Dict[str, str]:
        if self._token is not None:
            return {"Authorization": f"Bearer {self._token()}"}
        if not self._key:
            return {}
        if self.key_header.lower() == "authorization":
            return {"Authorization": f"Bearer {self._key}"}
        return {self.key_header: self._key}

    def _wait(self, headers: Mapping[str, str], attempt: int) -> float:
        """How long to wait before asking again: what the service said, else a doubling pause, never past ``max_wait``."""
        said = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
            try:
                return min(self.max_wait, max(float(said[name]) * scale, 0.0))
            except (KeyError, ValueError):
                continue
        return min(self.max_wait, 2.0 ** (attempt - 1))

    def _adjust(self, refusal: str) -> bool:
        """Drop, or rename, the one setting the service said it does not take. True when there was one.

        Calls made side by side all go out with the setting as it was, and
        the first refusal back puts it right for every call after it. A
        refusal about a setting already put right counts too, so the call it
        came back on is asked again as the settings now stand, not given up.
        """
        said = refusal.lower()
        if "max_tokens" in said and "max_completion_tokens" in said:
            self._limit = "max_completion_tokens"
            return True
        for name in self._OPTIONAL:
            if name in said:
                self._settings.pop(name, None)
                return True
        return False

    def _ask(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        """The model's answer, or None after a refusal or ``tries`` without one."""
        attempt = 0
        adjusted = 0
        while True:
            body: Dict[str, Any] = {
                "messages": messages,
                **self._settings,
                self._limit: self.max_tokens,
            }
            if self.model:
                body["model"] = self.model
            try:
                # A managed identity's token is fetched here, and a host with
                # none to give fails like a service that did not answer.
                headers = {"Content-Type": "application/json", **self._auth()}
                status, reply_headers, reply = self._transport(
                    "POST", self.url, headers, json.dumps(body).encode("utf-8"), self.timeout
                )
            except Exception as exc:  # noqa: BLE001 - no answer is one worth waiting for
                status, reply_headers, reply = 0, {}, str(exc).encode("utf-8")
            status = int(status)
            text = bytes(reply or b"").decode("utf-8", errors="replace")
            if 200 <= status < 300:
                answer = self._content(text)
                self.failure = None if answer is not None else (status, text[:300])
                return answer
            if status == 400 and adjusted < len(self._OPTIONAL) + 1 and self._adjust(text):
                adjusted += 1
                continue
            attempt += 1
            if (status == 0 or status == 429 or status >= 500) and attempt < self.tries:
                time.sleep(self._wait(reply_headers, attempt))
                continue
            logger.warning(
                "%s %s: %s %s", self.label, self._FAILED, status or "no answer", text[:300]
            )
            self.failure = (status, text[:300])
            return None

    def _content(self, text: str) -> Optional[str]:
        try:
            message = json.loads(text)["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            logger.warning(
                "%s answered with something that is not a chat completion: %s",
                self.label,
                text[:300],
            )
            return None
        if not isinstance(message, Mapping) or message.get("refusal"):
            return None
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "") for part in content if isinstance(part, Mapping)
            )
        return str(content or "").strip() or None
