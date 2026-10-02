"""Pictures described in words, by whatever can see them, best first.

A picture in a document is found by what it shows and cited by what it is,
so the words written under it are what make it findable. How many words,
and how good, depends on who writes them:

* :class:`ChatDescriber` asks a chat model that can see: GPT-4o or GPT-4.1
  on Azure OpenAI, a model on Azure AI Foundry or OpenAI, or one served on
  your own machine. It writes a detailed description: what kind of picture
  it is, who and what is in it and how they look, the setting, every word
  printed in it, and for a chart every value it shows, as rows.
* :class:`~vectrixdb.extract.engines.AzureImageAnalysis`, Azure AI Vision,
  gives a caption, short phrases for the parts it sees, and the words it
  reads; in a region that cannot caption, the words alone.
* :class:`WordsOnly` turns an OCR reader into a describer: the words, and
  nothing about what the picture shows.

:class:`Fallback` asks them in that order, one picture at a time. A model
that is throttled for one picture, or will not describe it, hands that
picture to the next, and the document goes on. Which of them described
each figure is recorded, so the plainer descriptions can be found and done
again once the better describer is back.

The picture itself is never kept or sent on. It is in memory while it is
described, and only the words travel.
"""

from __future__ import annotations

import base64
import io
import logging
import re
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .._chat import ChatRoute, Transport
from .._chat import json_in as _json_in
from .._chat import managed_identity as _managed_identity  # noqa: F401 - the name it had here
from ..exceptions import DependencyError


# ============================================================================
# SETTINGS: the logger, the instruction, the same-language rule, and the formats
# ============================================================================
#
# What a model is asked when shown a picture, the rule that it answers in the
# document's language, and the picture formats a chat model and an OCR engine
# take.

logger = logging.getLogger(__name__)

__all__ = ["ChatDescriber", "Fallback", "WordsOnly", "describer_from_environment"]

#: What a chat model is asked for. The checklist is the difference between
#: a caption and a description somebody can search: a chart's every value,
#: every word printed in the picture exactly as written, and what the people
#: in it visibly wear and do, never who they are.
INSTRUCTION = """You describe one picture taken from a document, so that people can find it by searching and cite it.
Look at the picture. Use the text given with it only to say how the picture relates to the document, and do not repeat that text.

Answer with one JSON object and nothing else, with these keys:
"kind": one of photograph, chart, diagram, table, map, screenshot, illustration, logo, signature, other.
"caption": what the picture shows, in one line of at most 15 words, with no full stop.
"description": a detailed description in plain sentences, the most important first:
 - what kind of picture it is, and how it is laid out;
 - every person: what they visibly wear and hold, their expression and pose, and what they are doing;
 - the objects, the setting and the colours that matter;
 - for a chart: its type, title, axes and units, legend, each series, and what the values show: the highest, the lowest and the trend;
 - for a diagram: its parts and how they connect, in order;
 - for a table: what it compares;
 - how the picture relates to the text around it.
"words": every word and number printed in the picture, exactly as written, in reading order.
"table": for a table, every value it shows; for a chart, every value printed on it as a label, never a value read off a bar or a line against its axis, and never a number from the axis itself; one row each, the first row naming the columns; otherwise null.
"decorative": true only for a picture with nothing anyone would search for: a background, a texture, a border, a spacer or a lone logo.

Never say who a person is from how they look, and never guess anyone's age, ethnicity, religion or health.
If something is unclear or too small to read, say so rather than guess.
Write in {language}."""

#: When nobody says which language to write in.
SAME_LANGUAGE = "the language of the text given with the picture, or English when there is none"

#: What a chat model reads, and what an OCR reader reads.
_CHAT_FORMATS = ("image/png", "image/jpeg", "image/gif", "image/webp")
_OCR_FORMATS = ("image/png", "image/jpeg")


# ============================================================================
# THE PICTURE, AS A READER TAKES IT
# ============================================================================
#
# INPUT   a picture's bytes
# OUTPUT  its type from its first bytes, for the four a model reads; the
#         picture in a form the reader takes, resized, or None when it cannot
#         be made one
#
# A picture that is not one of the four is not sent.


def _sniff(data: bytes) -> Optional[str]:
    """The picture's type from its first bytes, for the four a model reads, or None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _picture(data: bytes, max_side: int, formats: Sequence[str] = _CHAT_FORMATS) -> Optional[Tuple[bytes, str]]:
    """The picture in a form the reader takes, and its type; None when it cannot be made one.

    Sent as it came when it already is one, and turned into PNG or JPEG only
    when it is not, JPEG 2000 or a printer's CMYK say, or when a side is
    longer than ``max_side``. Pillow does the turning; without it, a picture
    already in a readable form is sent and any other is not.
    """
    mime = _sniff(data)
    readable = mime if mime in formats else None
    try:
        from PIL import Image
    except ImportError:
        return (data, readable) if readable else None
    try:
        with Image.open(io.BytesIO(data)) as picture:
            picture.load()
            if readable and max(picture.size) <= max_side and picture.mode in ("RGB", "L", "RGBA", "LA", "P"):
                return data, readable
            picture.thumbnail((max_side, max_side))
            out = io.BytesIO()
            if picture.mode in ("RGBA", "LA", "P"):
                picture.save(out, "PNG")
                return out.getvalue(), "image/png"
            picture.convert("RGB").save(out, "JPEG", quality=90)
            return out.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - what Pillow cannot open is sent as it came, when it can be
        return (data, readable) if readable else None


# ============================================================================
# THE WORDS: capped, listed, in rows, and where the picture sits
# ============================================================================
#
# INPUT   a model's answer; a picture's context
# OUTPUT  text cut after a sentence rather than inside one; the printed words
#         each once, in order; a chart's or a table's values as rows; where
#         the picture sits, for the model: the file, the page, the section,
#         the caption and the text either side
#
# The context is what lets a model say what a chart is a chart of.


def _capped(text: str, most: int) -> str:
    """At most ``most`` characters, cut after a sentence rather than inside one."""
    text = text.strip()
    if len(text) <= most:
        return text
    cut = text[:most]
    end = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("; "))
    return (cut[: end + 1] if end > most // 2 else cut.rsplit(" ", 1)[0]).rstrip()


def _words(value: Any) -> List[str]:
    """The printed words a model listed, in its order, each once."""
    items = value if isinstance(value, list) else [value] if isinstance(value, str) else []
    seen: List[str] = []
    for item in items:
        word = " ".join(str(item or "").split())
        if word and word not in seen:
            seen.append(word)
    return seen


#: A value a model says it guessed: "about 7,100", "approximately 450". A
#: chart's rows hold what it prints, so a guess is no value for one of them.
GUESS_WORDS = re.compile(
    r"\b(?:about|approximately|approx\.?|around|roughly|nearly|almost|circa|estimated|est\.)\s*[$€£¥]?\s*\(?\d", re.IGNORECASE
)


def _rows(value: Any, most: int, chart: bool = False) -> Optional[List[List[str]]]:
    """A chart's or a table's values as rows, the first naming the columns; None unless there is a row under it.

    A chart's value the model says it guessed, "about 7,100", is left out:
    what a bar reaches against its scale is a guess, and a row holding one
    reads like a figure the document printed. Then a chart's row with
    nothing past its label is left out too: a year the chart prints no
    value for, ``Year: 2017``, says nothing. A table's is kept, since a
    label alone there can head the rows under it, "Current assets".
    """
    if not isinstance(value, list):
        return None
    rows = [
        [" ".join(str(cell).split()) if cell is not None else "" for cell in row]
        for row in value
        if isinstance(row, list) and any(cell is not None and str(cell).strip() for cell in row)
    ]
    if chart and rows:
        rows = rows[:1] + [[row[0]] + ["" if GUESS_WORDS.search(cell) else cell for cell in row[1:]] for row in rows[1:]]
        rows = rows[:1] + [row for row in rows[1:] if any(row[1:])]
    return rows[: most + 1] if len(rows) >= 2 else None


def _context_text(context: Mapping[str, Any]) -> str:
    """Where the picture sits, for the model: the file, the page, the section, the caption and the text either side."""
    lines: List[str] = []
    if context.get("name"):
        lines.append(f"File: {context['name']}")
    page, label = context.get("page"), context.get("page_label")
    if page is not None:
        lines.append(f"Page: {page}" + (f", printed as {label}" if label else ""))
    if context.get("heading"):
        lines.append(f"Section: {context['heading']}")
    lines.append(f"Caption in the document: {context.get('caption') or 'none'}")
    for key, title in (("before", "Text just before the picture"), ("after", "Text just after the picture")):
        text = str(context.get(key) or "").strip()
        if text:
            lines.append(f'{title}:\n"""\n{text}\n"""')
    return "\n".join(lines)


def _label_of(describer: Any) -> str:
    return str(getattr(describer, "label", "") or type(describer).__name__)


# ============================================================================
# THE DESCRIBERS: a chat model, the words alone, and the fallback
# ============================================================================
#
# INPUT   a picture and its context
# OUTPUT  a picture described in detail by a chat model that can see; the
#         words printed in it as its description, the last resort; describers
#         asked in turn, the first with an answer describing it; every
#         describer the settings name, best first, or None when nothing can
#         see
#
# A picture in a document is found by what it shows and cited by what it is,
# so the words written under it are what the index holds.


class ChatDescriber(ChatRoute):
    """A picture described in detail by a chat model that can see.

    Any service with an OpenAI style chat completions route takes it: Azure
    OpenAI, a model on Azure AI Foundry, OpenAI, or a local server such as
    vLLM or Ollama. :meth:`azure_openai` builds one for an Azure OpenAI
    deployment; otherwise ``url`` is the whole address of the route, and
    ``model`` names the model for a service that serves more than one.
    ``key`` goes in ``key_header``, as a Bearer token when that is
    ``Authorization``; ``token``, a callable, gives a fresh Bearer token per
    call instead, for a managed identity.

    The picture goes in the request as it came out of the document, turned
    into PNG or JPEG only when it is in a form the model cannot read, and
    made smaller only when a side is longer than ``max_side``. With it go
    the page and the number printed on it, the heading, the document's own
    caption and the text either side, so the model can say how the picture
    fits the page.

    What comes back is a caption, a description of at most ``max_chars``
    ending in the words printed in the picture, and for a chart or a table
    its values as rows, at most ``max_rows``, which ``describe_figures``
    writes the way a spreadsheet's rows are written. A caption is only given
    for a picture the document has not captioned itself: a document's own
    "Figure 3" is what a reader cites.

    Like :class:`~vectrixdb.extract.engines.AzureImageAnalysis`, it never
    fails a document. A refusal, a service still throttled or down after
    ``tries``, or an answer with nothing in it gives no description, and
    :class:`Fallback` hands the picture to the next describer. A request the
    service turns down for one of its settings, a model that takes no
    ``temperature`` or wants ``max_completion_tokens``, is sent again
    without it, and the change is kept for the pictures after it.
    """

    label = "chat-model"
    _AZURE_DEPLOYMENT = "AZURE_OPENAI_VISION_DEPLOYMENT"
    _ROUTE = ("VECTRIXDB_DESCRIBER_URL", "VECTRIXDB_DESCRIBER_KEY", "VECTRIXDB_DESCRIBER_MODEL", "VECTRIXDB_DESCRIBER_KEY_HEADER")
    _FAILED = "did not describe a picture"
    _WITHOUT = "pictures are not described by it"

    def __init__(
        self,
        url: str,
        *,
        key: Optional[str] = None,
        token: Optional[Callable[[], str]] = None,
        model: Optional[str] = None,
        key_header: str = "Authorization",
        label: Optional[str] = None,
        language: Optional[str] = None,
        max_chars: int = 1500,
        max_rows: int = 40,
        max_tokens: int = 1500,
        max_side: int = 2048,
        detail: str = "high",
        timeout: float = 90.0,
        tries: int = 3,
        max_wait: float = 20.0,
        transport: Optional[Transport] = None,
    ) -> None:
        super().__init__(
            url,
            key=key,
            token=token,
            model=model,
            key_header=key_header,
            label=label,
            max_tokens=max_tokens,
            timeout=timeout,
            tries=tries,
            max_wait=max_wait,
            transport=transport,
        )
        self.language = language
        self.max_chars = int(max_chars)
        self.max_rows = int(max_rows)
        self.max_side = int(max_side)
        self.detail = detail
        self.asked = 0

    @classmethod
    def azure_openai(cls, endpoint: str, deployment: str, **kwargs: Any) -> "ChatDescriber":
        """One for an Azure OpenAI deployment of a model that can see, GPT-4o or GPT-4.1 say."""
        return super().azure_openai(endpoint, deployment, **kwargs)

    @classmethod
    def from_environment(cls, env: Optional[Mapping[str, str]] = None, **kwargs: Any) -> Optional["ChatDescriber"]:
        """One from the settings, or None when they name none.

        ``AZURE_OPENAI_VISION_DEPLOYMENT`` with ``AZURE_OPENAI_ENDPOINT`` is an
        Azure OpenAI deployment, called with ``AZURE_OPENAI_KEY``, or with the
        managed identity when there is no key; ``AZURE_OPENAI_API_VERSION``
        changes the version. Otherwise ``VECTRIXDB_DESCRIBER_URL`` is any chat
        completions route, with ``VECTRIXDB_DESCRIBER_KEY``,
        ``VECTRIXDB_DESCRIBER_MODEL`` and ``VECTRIXDB_DESCRIBER_KEY_HEADER``.
        """
        return super().from_environment(env, **kwargs)

    def __call__(self, image: bytes, context: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        ready = _picture(image, self.max_side)
        if ready is None:
            logger.info("%s was not sent %s: not a picture it can read", self.label, context.get("src") or "a picture")
            return None
        data, mime = ready
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": INSTRUCTION.format(language=self.language or SAME_LANGUAGE)},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _context_text(context)},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", "detail": self.detail},
                    },
                ],
            },
        ]
        answer = self._ask(messages)
        if answer is None:
            return None
        self.asked += 1
        return self._described(answer, context)

    def _described(self, answer: str, context: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """The answer as ``describe_figures`` takes one."""
        parsed = _json_in(answer)
        if parsed is None:
            # A model that answered in prose still said what it saw.
            prose = " ".join(answer.split())
            return {"description": _capped(prose, self.max_chars), "by": self.label} if prose else None
        if parsed.get("decorative") is True:
            return {"decorative": True, "by": self.label}
        found: Dict[str, Any] = {"by": self.label}
        words = _words(parsed.get("words"))
        printed = _capped(f"Words in the picture: {'; '.join(words)}.", self.max_chars // 3) if words else ""
        room = self.max_chars - (len(printed) + 1 if printed else 0)
        description = _capped(" ".join(str(parsed.get("description") or "").split()), room)
        text = "\n".join(part for part in (description, printed) if part)
        if text:
            found["description"] = text
        caption = " ".join(str(parsed.get("caption") or "").split()).rstrip(".")
        if caption and not str(context.get("caption") or "").strip():
            found["caption"] = caption[:160]
        rows = _rows(parsed.get("table"), self.max_rows, chart=str(parsed.get("kind") or "").strip().lower() == "chart")
        if rows:
            found["table"] = rows
        return found if len(found) > 1 else None


class WordsOnly:
    """The words printed in a picture, as its description: the last resort.

    Wraps a reader of pictures, ``AzureDocumentIntelligence`` or
    ``RapidOcr``, for when nothing that can see answers. It says nothing
    about what the picture shows, and a picture with no words in it gets no
    description rather than an empty one. A reader whose package is not
    installed here is not asked again.
    """

    def __init__(self, reader: Callable[..., Any], *, label: Optional[str] = None) -> None:
        self.reader = reader
        self.label = label or f"ocr:{_label_of(reader)}"
        self.available = True

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.label!r})"

    def __call__(self, image: bytes, context: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.available:
            return None
        ready = _picture(image, 4096, _OCR_FORMATS)
        if ready is None:
            return None
        data, mime = ready
        stem = PurePosixPath(str(context.get("src") or "picture")).stem or "picture"
        name = f"{stem}.{'png' if mime == 'image/png' else 'jpg'}"
        try:
            read = self.reader(data, name)
        except DependencyError as exc:
            self.available = False
            logger.warning("%s cannot read pictures here, and is not asked again: %s", self.label, exc)
            return None
        from . import coerce

        lines = [" ".join(line.split()) for line in coerce(read, name).text.splitlines() if line.strip()]
        if not lines:
            return None
        return {"description": "The words in it read: " + "; ".join(lines) + ".", "by": self.label}


class Fallback:
    """Describers asked in turn for each picture; the first with an answer describes it.

    In the order a deployment trusts them: a chat model that writes a
    detailed description, then Azure Vision, then the words alone. Each
    picture goes down the list on its own, so a model that is throttled for
    one picture costs that picture a plainer description, not the document
    its reading. A describer that raises is passed over like one with
    nothing to say, and the answer names the one that gave it, which
    ``describe_figures`` records beside the figure.
    """

    label = "fallback"

    def __init__(self, *describers: Any) -> None:
        self.describers = [d for d in describers if d is not None]

    def __repr__(self) -> str:
        return f"{type(self).__name__}({', '.join(self.labels)})"

    @property
    def labels(self) -> List[str]:
        """Who is asked, in order."""
        return [_label_of(d) for d in self.describers]

    @property
    def seeing(self) -> Optional["Fallback"]:
        """The ones that see, without the words-only last resort: for a picture whose words are read anyway."""
        kept = [d for d in self.describers if not isinstance(d, WordsOnly)]
        return Fallback(*kept) if kept else None

    def __call__(self, image: bytes, context: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        for describer in self.describers:
            try:
                answer = describer(image, context)
            except Exception as exc:  # noqa: BLE001 - the next one is asked instead
                logger.warning("%s could not describe %s: %s", _label_of(describer), context.get("src") or "a picture", exc)
                continue
            if isinstance(answer, str):
                answer = {"description": answer} if answer.strip() else None
            if not isinstance(answer, Mapping):
                continue
            found = dict(answer)
            found.setdefault("by", _label_of(describer))
            return found
        return None


def describer_from_environment(
    env: Optional[Mapping[str, str]] = None, *, reader: Optional[Callable[..., Any]] = None
) -> Optional[Fallback]:
    """Every describer the settings name, best first, as one :class:`Fallback`; None when nothing can see.

    A chat model when ``AZURE_OPENAI_VISION_DEPLOYMENT`` or
    ``VECTRIXDB_DESCRIBER_URL`` names one, then Azure Vision when
    ``AZURE_VISION_ENDPOINT`` and ``AZURE_VISION_KEY`` are there, then
    ``reader``, an OCR reader, for the words alone. The words-only last
    resort joins only behind something that can see: with nothing that can,
    the answer is None, and a document's pictures are not opened at all,
    which costs nothing.
    """
    from .engines import AzureImageAnalysis

    seeing = [d for d in (ChatDescriber.from_environment(env), AzureImageAnalysis.from_environment(env)) if d is not None]
    if not seeing:
        return None
    return Fallback(*seeing, WordsOnly(reader) if reader is not None else None)
