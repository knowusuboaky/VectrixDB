"""Masking of email addresses, phone numbers and card numbers in what a collection sends people.

An admin turns it on per collection, beside who can see it. From then on a
reply from any of that collection's routes is masked on its way out when it
goes to a person or a guest, and left as stored when it goes to an API key,
because a script feeding a pipeline needs the text as it is. One layer, around
every route, so a route added later is masked without anybody remembering to.

Identifiers are left alone, so the page can still open what it was shown:
an ``id``, anything ending ``_id`` or ``_ids``, a collection's name and a
citation. Everything else that is text is masked, metadata values included.
See :mod:`vectrixdb.masking` for what is found and what is not.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .rootpath import route_path

from ..masking import mask_text, mask_value

__all__ = ["MaskingMiddleware", "masked_reply"]


# ============================================================================
# SETTINGS: the collection a path names, and what is kept
# ============================================================================
#
# How a route's path names its collection, and the record fields the
# middleware reads.

#: A route that belongs to one collection, under every API version and the dashboard's alias.
_COLLECTION = re.compile(r"^/api(?:/v\d+)?/collections/([^/]+)")

#: Keys whose values a caller sends back, or that name rather than say.
_KEPT = frozenset({"id", "ids", "collection", "name", "citation", "citations", "route", "build", "index_build_id"})


def _kept(key: str) -> bool:
    return key in _KEPT or key.endswith("_id") or key.endswith("_ids")


# ============================================================================
# MASKING ON THE WAY OUT
# ============================================================================
#
# INPUT   a reply's JSON from one of a collection's routes
# OUTPUT  the same JSON with its text masked and its identifiers as they were;
#         the middleware that does it for every reply to a person or a guest
#
# Always on, for every collection: emails, phone numbers and card numbers go
# before a person reads them. A key's caller gets the text as it is, since a
# script is not a person reading.


def masked_reply(data: Any) -> Any:
    """A reply's JSON with its text masked and its identifiers as they were."""
    return mask_value(data, keep=_kept)


class MaskingMiddleware(BaseHTTPMiddleware):
    """Masks a collection's replies to people and guests, always."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        runtime = getattr(request.app.state, "signin", None)
        # The path as the app knows it: behind a gateway's prefix the pattern
        # would not match, and identifiers would go out unmasked.
        found = _COLLECTION.match(route_path(request))
        if runtime is None or found is None or not 200 <= response.status_code < 300:
            return response
        caller = getattr(request.state, "caller", None)
        if caller is None or caller.method == "key":
            return response
        kind = response.headers.get("content-type", "")
        if not (kind.startswith("application/json") or kind.startswith("text/")):
            return response
        body = b"".join([part async for part in response.body_iterator])
        if kind.startswith("application/json"):
            try:
                shown = json.dumps(masked_reply(json.loads(body)), ensure_ascii=False).encode("utf-8")
            except ValueError:
                shown = body
        else:
            shown = mask_text(body.decode("utf-8", errors="replace")).encode("utf-8")
        headers = {name: value for name, value in response.headers.items() if name.lower() != "content-length"}
        return Response(content=shown, status_code=response.status_code, headers=headers)
