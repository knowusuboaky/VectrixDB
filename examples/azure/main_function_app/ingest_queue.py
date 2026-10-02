"""What a queue message holds, and what to do when reading a file fails. No Azure import.

Kept apart from ``function_app.py`` so it can be tested: that file cannot be
imported without the Functions runtime, and this one is where a mistake would
lose a file in silence.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, List

from vectrixdb.worker import IngestEvent, events_from_event_grid

__all__ = ["events_in", "outcome_line", "should_go_round_again"]


# ============================================================================
# EVENTS: what came in, whether to go round again, the outcome line
# ============================================================================
#
# INPUT   one queue message; a failure and how many times it has been tried;
#         what happened to which file
# OUTPUT  the blob events in the message; whether it goes back on the queue;
#         one line for the log, with why when it failed
#
# This is where a mistake would lose a file in silence, so it is kept apart
# from the Functions runtime and tested.


def events_in(body: Any) -> List[IngestEvent]:
    """The blob events in one queue message.

    Event Grid writes one event a message, as JSON. The Functions host hands
    it over decoded when ``messageEncoding`` matches what Event Grid wrote,
    and as base64 when it does not, which is a setting people get wrong, so
    both are read. A message that is not a blob event, the subscription's
    validation handshake say, gives no events and is not an error: raising
    would send it round five times and then to the poison queue.
    """
    if isinstance(body, (bytes, bytearray)):
        body = bytes(body).decode("utf-8", errors="replace")
    if isinstance(body, str):
        text = body.strip()
        if not text.startswith(("{", "[")):
            try:
                text = base64.b64decode(text, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                return []
        try:
            body = json.loads(text)
        except ValueError:
            return []
    if not isinstance(body, (dict, list)):
        return []
    return events_from_event_grid(body)


def should_go_round_again(outcome: Any) -> bool:
    """Whether the message goes back on the queue.

    A failed read does: the extraction server was busy, the blob was not
    there yet. Everything else is final. After ``maxDequeueCount`` tries the
    host moves the message to ``<queue>-poison``, where a person finds the
    file that would not read, which is the point of having the queue.
    """
    return str(getattr(outcome, "action", "")) == "failed"


def outcome_line(outcome: Any) -> str:
    """One line for the log: what happened to which file, and why when it failed."""
    parts = [
        str(getattr(outcome, "action", "?")),
        str(getattr(outcome, "uri", "") or getattr(outcome, "doc_id", "")),
    ]
    chunks = getattr(outcome, "chunks", None)
    if chunks:
        parts.append(f"{chunks} chunks")
    error = getattr(outcome, "error", None)
    if error:
        parts.append(str(error))
    return " | ".join(p for p in parts if p)
