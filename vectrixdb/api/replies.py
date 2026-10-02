"""One shape for every refusal the server sends.

There used to be two. The layer at the door, which decides about keys, roles
and scope, answered ``{"ok": false, "message": ..., "data": ...}``. A route
that was reached and then refused answered in FastAPI's own shape,
``{"detail": ...}``, where ``detail`` is a string, or a list of field errors
on a 422. A client had to know which layer had refused it to know where the
sentence was.

Every refusal now carries all four keys::

    {"ok": false, "message": "Collection 'nope' not found", "data": null,
     "detail": "Collection 'nope' not found"}

``message`` is always one sentence meant for a person. ``detail`` is what
FastAPI would have sent, so a client written against either shape keeps
working: nothing was taken away, which is why this needed no deprecation.
On a 422 ``detail`` is still the list of field errors and ``message`` says
the same thing in words.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from fastapi.responses import JSONResponse

__all__ = ["collection_not_found", "message_of", "refusal", "refusal_content"]


# ============================================================================
# ONE SHAPE FOR EVERY REFUSAL
# ============================================================================
#
# INPUT   a status, a message, and what the route gave as detail
# OUTPUT  {ok: false, message, data, detail}: the message one sentence a
#         person can act on, the detail what the route gave, and the data what
#         a client needs to go on
#
# There used to be two shapes, the door's and the routes'. A collection that
# is not there, or is private to others, has one answer, so a name cannot be
# probed.


def message_of(detail: Any) -> str:
    """The sentence a ``detail`` amounts to.

    A string is its own sentence. A validation error is a list of mappings
    with ``loc`` and ``msg``, and reads as ``query_text: Field required``,
    without the ``body`` that says only where FastAPI looked.
    """
    if isinstance(detail, str):
        return detail
    if isinstance(detail, (list, tuple)):
        said = []
        for entry in detail:
            if isinstance(entry, Mapping):
                where = ".".join(
                    str(part)
                    for part in entry.get("loc", ())
                    if part not in ("body", "query", "path", "header")
                )
                what = str(entry.get("msg") or "is not valid")
                said.append(f"{where}: {what}" if where else what)
            else:
                said.append(str(entry))
        return "; ".join(said) or "The request is not valid"
    if isinstance(detail, Mapping) and isinstance(detail.get("message"), str):
        return str(detail["message"])
    return str(detail) if detail is not None else "The request was refused"


def refusal_content(message: str, *, detail: Any = None, data: Any = None) -> dict:
    """The body of a refusal. ``detail`` defaults to the message itself."""
    return {
        "ok": False,
        "message": message,
        "data": data,
        "detail": message if detail is None else detail,
    }


def refusal(
    status: int,
    message: str,
    *,
    detail: Any = None,
    data: Any = None,
    headers: Optional[Mapping[str, str]] = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=refusal_content(message, detail=detail, data=data),
        headers=dict(headers) if headers else None,
    )


def collection_not_found(name: str) -> JSONResponse:
    """The one answer for a collection that is not there.

    It is also the answer for one that is there and is not the caller's to
    know about: a guest asking for a private collection, or a key asking for
    one outside its scope. Those must be this reply to the letter, because a
    difference between them is how a caller learns what else the server holds.
    """
    return refusal(404, f"Collection '{name}' not found")
