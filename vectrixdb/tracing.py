"""Spans for searches, ingestion and evaluation runs, off until you turn them on.

    pip install "vectrixdb[tracing]"
    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   # or VECTRIXDB_TRACING=1

or, in code, after setting up OpenTelemetry the way the rest of the app does::

    import vectrixdb.tracing
    vectrixdb.tracing.enable()

Every ``Vectrix.search``, ``add_document``, ``rechunk``, ``write_golden`` and
``evaluate`` then becomes a span named ``vectrixdb.<operation>``, with the
collection, the mode, the counts, the top relevance and how long it took, in
whatever tracing tool the app already sends to: Jaeger, Grafana Tempo,
Honeycomb, Datadog, Azure Monitor, any OTLP collector.

**What a span never carries.** No query text, no document text, no file
names, no metadata values and no keys. A span says *that* a search ran, how
it ran and how well it scored, the way the audit records carry a query's
fingerprint and never its words. An exception is recorded by its type.

**What it costs when it is off.** One boolean check per call. OpenTelemetry
is not imported, and no span is made, until tracing is on. It turns on when
``enable()`` is called, or at import when ``VECTRIXDB_TRACING`` is ``1`` or
``OTEL_EXPORTER_OTLP_ENDPOINT`` is set. ``VECTRIXDB_TRACING=0`` keeps it off
even with an endpoint set. Spans go to the app's OpenTelemetry provider,
whenever the app sets it up. Where nothing sets one up, an OTLP exporter is
set up from the standard ``OTEL_*`` variables: at import with
``VECTRIXDB_TRACING=1``, and by ``vectrixdb serve`` and ``vectrixdb mcp``,
whose process is VectrixDB's own. An app that has its own provider keeps it.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import os
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional, TypeVar

__all__ = [
    "enable",
    "disable",
    "enabled",
    "describe",
    "export_to_otlp",
    "span",
    "traced",
    "inject",
    "CallerTrace",
    "fastapi_options",
    "SAFE_ATTRIBUTES",
]

F = TypeVar("F", bound=Callable[..., Any])

_state: Dict[str, Any] = {"on": False, "tracer": None, "to": None}

#: The only attribute names a span may carry. Anything else handed to
#: :func:`span` is dropped, so a later edit cannot leak text by accident.
SAFE_ATTRIBUTES = frozenset(
    {
        "vectrixdb.collection",
        "vectrixdb.mode",
        "vectrixdb.limit",
        "vectrixdb.rerank",
        "vectrixdb.filtered",
        "vectrixdb.results",
        "vectrixdb.top_relevance",
        "vectrixdb.top_relevance_kind",
        "vectrixdb.degraded",
        "vectrixdb.truncated",
        "vectrixdb.chunks",
        "vectrixdb.chunking",
        "vectrixdb.documents",
        "vectrixdb.kind",
        "vectrixdb.questions",
        "vectrixdb.collections",
        "vectrixdb.setups",
        "vectrixdb.preview",
        "vectrixdb.duration_ms",
        # An MCP tool call: which tool, by its fixed name, and whether it was refused.
        "vectrixdb.tool",
        "vectrixdb.refused",
    }
)

# A search that calls itself again (a fallback, a second home) is one span.
_inside: contextvars.ContextVar[frozenset] = contextvars.ContextVar(
    "vectrixdb_tracing_inside", default=frozenset()
)


# ============================================================================
# SWITCHING IT ON AND OFF
# ============================================================================


def enable(tracer_provider: Any = None) -> bool:
    """Turn spans on. Returns False, and stays off, when OpenTelemetry is not installed.

    ``tracer_provider`` is the provider to use; left out, OpenTelemetry's
    global one, which is whatever the app set up.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        _state.update(on=False, tracer=None)
        return False
    provider = tracer_provider or trace.get_tracer_provider()
    try:
        from importlib.metadata import version as _version

        version: Optional[str] = _version("vectrixdb")
    except Exception:
        version = None
    _state.update(on=True, tracer=provider.get_tracer("vectrixdb", version), to=None)
    return True


def disable() -> None:
    """Turn spans off. Calls cost one boolean check again."""
    _state.update(on=False, tracer=None, to=None)


def enabled() -> bool:
    return bool(_state["on"])


def describe() -> Dict[str, Any]:
    """Whether spans are on, and the host they go to when VectrixDB set up the exporter.

    What the dashboard's settings show. ``to`` is the endpoint's host alone,
    never its path, query or credentials, and None when the app set up
    OpenTelemetry itself and VectrixDB does not know where it sends.
    """
    return {"on": enabled(), "to": _state["to"] if enabled() else None}


def _endpoint_host() -> Optional[str]:
    from urllib.parse import urlsplit

    url = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    if not url:
        return "localhost"  # where the OTLP exporter sends when nothing says otherwise
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def export_to_otlp() -> bool:
    """Send spans to the OTLP endpoint the standard ``OTEL_*`` variables name.

    Only when the app has not set up OpenTelemetry itself: an app with its
    own provider keeps it, and this returns False, as it does when the SDK
    or the exporter is not installed (the ``[tracing]`` extra has both).
    ``vectrixdb serve`` and ``vectrixdb mcp`` call it when tracing is on,
    since there the process is VectrixDB's own.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return False
    if type(trace.get_tracer_provider()).__name__ not in (
        "ProxyTracerProvider",
        "NoOpTracerProvider",
    ):
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        return False
    service = os.environ.get("OTEL_SERVICE_NAME", "vectrixdb")
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    if enabled():
        _state["to"] = _endpoint_host()
    return True


def _from_environment() -> None:
    switch = os.environ.get("VECTRIXDB_TRACING", "").strip().lower()
    if switch in ("0", "false", "no", "off"):
        return
    asked = switch in ("1", "true", "yes", "on")
    if not asked and not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    # The global provider, which until the app sets one is OpenTelemetry's
    # proxy: spans go to whatever provider the app sets up later, so an app
    # that configures OpenTelemetry after importing this still gets them.
    # Only an explicit VECTRIXDB_TRACING=1 sets up an exporter here, because
    # OTEL_EXPORTER_OTLP_ENDPOINT is usually the app's own setting, and a
    # provider set here would refuse the one the app sets after it.
    if enable() and asked:
        export_to_otlp()


# ============================================================================
# MAKING SPANS
# ============================================================================


class _Span:
    """What a traced block can add to its span, with the names held to SAFE_ATTRIBUTES."""

    __slots__ = ("_otel",)

    def __init__(self, otel: Any) -> None:
        self._otel = otel

    def set(self, **attributes: Any) -> None:
        if self._otel is None:
            return
        for key, value in attributes.items():
            name = f"vectrixdb.{key}"
            if name not in SAFE_ATTRIBUTES or value is None:
                continue
            if not isinstance(value, (bool, int, float, str)):
                continue
            self._otel.set_attribute(name, value)


_OFF = _Span(None)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[_Span]:
    """A span named ``vectrixdb.<name>`` around the block, or nothing when tracing is off."""
    if not _state["on"]:
        yield _OFF
        return
    tracer = _state["tracer"]
    started = time.perf_counter()
    # OpenTelemetry's own handling would record the exception's message, and
    # with it whatever query or path the message quotes; this records the type.
    with tracer.start_as_current_span(
        f"vectrixdb.{name}", record_exception=False, set_status_on_exception=False
    ) as otel:
        handle = _Span(otel)
        handle.set(**attributes)
        try:
            yield handle
        except BaseException as e:
            from opentelemetry.trace import Status, StatusCode

            otel.set_attribute("exception.type", type(e).__name__)
            otel.set_status(Status(StatusCode.ERROR, type(e).__name__))
            raise
        finally:
            handle.set(duration_ms=round((time.perf_counter() - started) * 1000.0, 3))


def traced(
    name: str,
    before: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    after: Optional[Callable[[Any], Dict[str, Any]]] = None,
) -> Callable[[F], F]:
    """Wrap a function in a span. ``before(arguments)`` and ``after(result)`` give its attributes.

    ``arguments`` maps every parameter name to the value the call gave it, or
    its default, however it was passed. Off, the wrapper is one boolean check
    and the call. A function that calls itself again inside the same
    operation makes one span, not two.
    """

    def wrap(fn: F) -> F:
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def call(*args: Any, **kwargs: Any) -> Any:
            if not _state["on"] or name in _inside.get():
                return fn(*args, **kwargs)
            token = _inside.set(_inside.get() | {name})
            try:
                with span(name) as s:
                    if before is not None:
                        try:
                            bound = signature.bind(*args, **kwargs)
                            bound.apply_defaults()
                            s.set(**before(bound.arguments))
                        except Exception:
                            pass
                    result = fn(*args, **kwargs)
                    if after is not None:
                        try:
                            s.set(**after(result))
                        except Exception:
                            pass
                    return result
            finally:
                _inside.reset(token)

        return call  # type: ignore[return-value]

    return wrap


# ============================================================================
# ACROSS SERVICES
# ============================================================================


def inject(headers: Dict[str, str]) -> None:
    """Put the current trace in an outgoing request's headers, when tracing is on.

    The ``traceparent`` header, and whatever else the app's propagators
    carry, so the service at the other end, VectrixDB's extraction service
    among them, puts its spans in the same trace.
    """
    if not _state["on"]:
        return
    from opentelemetry import propagate

    propagate.inject(headers)


class CallerTrace:
    """ASGI middleware: a request's spans join the trace its ``traceparent`` header names.

    What a request span would do for the spans under it, without the span,
    which would carry the request's path and query. Off, it is one boolean
    check per request. A span already current, from an app that serves this
    one inside its own, stays the parent.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if not _state["on"] or scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        from opentelemetry import context, propagate, trace

        if trace.get_current_span().get_span_context().is_valid:
            await self.app(scope, receive, send)
            return
        carrier = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in scope.get("headers") or ()
        }
        token = context.attach(propagate.extract(carrier))
        try:
            await self.app(scope, receive, send)
        finally:
            context.detach(token)


def fastapi_options() -> Dict[str, Any]:
    """What each FastAPI app VectrixDB builds is given, so FastAPI traces nothing itself.

    FastAPI 0.142 and later trace every request once a tracer provider is
    set up, with the request's path and query on the span, and send metrics
    and logs, exception messages among them, wherever
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` points, whatever ``VECTRIXDB_TRACING``
    says. A path names a document and a query can carry metadata, which no
    span here carries. :class:`CallerTrace` keeps a request's spans in its
    caller's trace.
    """
    try:
        from fastapi import FastAPI

        native = "telemetry" in inspect.signature(FastAPI.__init__).parameters
    except (ImportError, TypeError, ValueError):
        native = False
    if not native:
        return {}
    return {
        "telemetry": {
            "tracing": False,
            "metrics": False,
            "logs": False,
            "operation_spans": False,
            "auto_configure": False,
        }
    }


_from_environment()
