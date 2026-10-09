"""Opt-in tracing: off costs nothing, on makes one safe span per operation."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from vectrixdb import tracing  # noqa: E402
from vectrixdb.easy import Vectrix  # noqa: E402

SECRET = "zebra quartz invoice 4417"


def embed(texts):
    out = np.zeros((len(texts), 8), dtype=np.float32)
    for i, text in enumerate(texts):
        for word in text.lower().split():
            out[i, sum(map(ord, word)) % 8] += 1.0
        out[i] /= np.linalg.norm(out[i]) or 1.0
    return out


def open_db(tmp_path, name, **options):
    return Vectrix(name, path=str(tmp_path / "db"), embed_fn=embed, dimension=8, **options)


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    assert tracing.enable(provider)
    try:
        yield exporter
    finally:
        tracing.disable()


def _texts() -> list:
    return [
        f"The {SECRET} was paid in March.",
        "Cats sleep for most of the day.",
        "The quarterly report covers revenue and costs.",
    ]


def _every_value(finished) -> str:
    out = []
    for s in finished:
        out.append(s.name)
        out.extend(str(v) for v in s.attributes.values())
        out.extend(str(e.attributes) for e in s.events)
    return " ".join(out)


def test_off_by_default_makes_no_spans(tmp_path):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.disable()
    db = open_db(tmp_path, "off")
    db.add(_texts())
    db.search("revenue")
    assert not tracing.enabled()
    assert exporter.get_finished_spans() == ()
    assert tracing.describe() == {"on": False, "to": None}


def test_one_span_per_search_with_safe_attributes(tmp_path, spans):
    db = open_db(tmp_path, "traced", mode="dense")
    db.add(_texts())
    spans.clear()
    found = db.search(SECRET, limit=2, filter={"kind": "none"})
    finished = spans.get_finished_spans()
    # The collection's own search runs inside it and does not make a second span.
    assert [s.name for s in finished] == ["vectrixdb.search"]
    attrs = dict(finished[0].attributes)
    assert attrs["vectrixdb.collection"] == "traced"
    assert attrs["vectrixdb.limit"] == 2
    assert attrs["vectrixdb.filtered"] is True
    assert attrs["vectrixdb.results"] == len(found.items)
    assert attrs["vectrixdb.duration_ms"] >= 0
    assert set(attrs) <= tracing.SAFE_ATTRIBUTES
    assert SECRET not in _every_value(finished)
    assert "zebra" not in _every_value(finished)


def test_add_document_and_rechunk_spans(tmp_path, spans):
    db = open_db(tmp_path, "docs", keep_source=True)
    text = "# Notes\n\n" + " ".join(
        f"The {SECRET} was paid on day {i} of the month." for i in range(40)
    )
    added = db.add_document(text, doc_id="a", chunk_size=200)
    written = db.rechunk("a", chunk_size=400)
    by_name = {}
    for s in spans.get_finished_spans():
        by_name.setdefault(s.name, []).append(dict(s.attributes))
    adds = by_name["vectrixdb.add_document"]
    assert adds[0]["vectrixdb.chunks"] == added
    assert adds[0]["vectrixdb.chunking"] == "recursive"
    (rechunked,) = by_name["vectrixdb.rechunk"]
    assert rechunked["vectrixdb.chunks"] == written
    assert rechunked["vectrixdb.preview"] is False
    assert SECRET not in _every_value(spans.get_finished_spans())


def test_rechunk_preview_is_a_span_marked_preview(tmp_path, spans):
    db = open_db(tmp_path, "docs", keep_source=True)
    db.add_document(
        " ".join(f"Sentence {i} about invoices." for i in range(60)),
        doc_id="a",
        on_low_quality="allow",
    )
    spans.clear()
    planned = db.rechunk_preview(chunk_size=200)
    (span,) = spans.get_finished_spans()
    assert span.name == "vectrixdb.rechunk"
    assert span.attributes["vectrixdb.preview"] is True
    assert span.attributes["vectrixdb.documents"] == 1
    assert span.attributes["vectrixdb.chunks"] == planned.chunks_after


def test_an_exception_is_recorded_by_its_type_only(spans):
    @tracing.traced("search")
    def broken(query):
        raise ValueError(f"no index holds {query}")

    with pytest.raises(ValueError):
        broken(SECRET)
    (span,) = spans.get_finished_spans()
    assert span.attributes["exception.type"] == "ValueError"
    assert span.status.status_code.name == "ERROR"
    assert SECRET not in _every_value([span])
    assert SECRET not in (span.status.description or "")


def test_unknown_and_non_primitive_attributes_are_dropped(spans):
    with tracing.span("search", collection="c", query=SECRET, metadata={"a": 1}, limit=[1]):
        pass
    (span,) = spans.get_finished_spans()
    assert set(span.attributes) == {"vectrixdb.collection", "vectrixdb.duration_ms"}


def test_attribute_failures_never_break_the_call(spans):
    @tracing.traced("search", before=lambda a: 1 / 0, after=lambda r: r.missing)
    def fine(query):
        return 7

    assert fine("x") == 7
    assert [s.name for s in spans.get_finished_spans()] == ["vectrixdb.search"]


def test_environment_switch(monkeypatch):
    exporters = []
    monkeypatch.setattr(tracing, "export_to_otlp", lambda: exporters.append(1) or True)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.example:4318/x?token=1")
    monkeypatch.setenv("VECTRIXDB_TRACING", "0")
    tracing.disable()
    tracing._from_environment()
    assert not tracing.enabled()
    try:
        monkeypatch.delenv("VECTRIXDB_TRACING")
        tracing._from_environment()
        assert tracing.enabled()
        # The endpoint is usually the app's own setting: no provider is set up
        # for it, so the app's own, set up before or after, is never refused.
        assert exporters == []
        assert tracing.describe() == {"on": True, "to": None}
        tracing.disable()
        monkeypatch.setenv("VECTRIXDB_TRACING", "1")
        tracing._from_environment()
        assert tracing.enabled()
        assert exporters == [1]
    finally:
        tracing.disable()


def test_the_endpoint_is_shown_by_its_host_alone(monkeypatch):
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "https://user:pw@otlp.example.com:4318/v1?k=s"
    )
    assert tracing._endpoint_host() == "otlp.example.com"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://traces.example/v1/traces")
    assert tracing._endpoint_host() == "traces.example"


def test_an_app_that_set_up_opentelemetry_keeps_its_provider(monkeypatch):
    from opentelemetry import trace

    own = TracerProvider()
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: own)
    monkeypatch.setattr(trace, "set_tracer_provider", lambda p: pytest.fail("replaced"))
    assert tracing.export_to_otlp() is False


def test_the_server_says_whether_tracing_is_on(tmp_path, spans):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from vectrixdb.api.server import create_app

    with TestClient(create_app(db_path=str(tmp_path / "db"))) as c:
        assert c.get("/api/v1/info").json()["tracing"] == {"on": True, "to": None}
        tracing.disable()
        assert c.get("/api/v1/info").json()["tracing"] == {"on": False, "to": None}


# ---------------------------------------------------------------- across services

CALLER_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
CALLER_SPAN = "00f067aa0ba902b7"


@pytest.fixture
def app_spans(monkeypatch):
    """Spans from the provider the whole process sees, as an app's own set-up makes.

    FastAPI 0.142 and later make request spans of their own on that provider,
    with the request's path and query on them; a server VectrixDB builds must not.
    """
    pytest.importorskip("fastapi")
    from opentelemetry import trace

    for name in ("VECTRIXDB_API_KEY", "VECTRIXDB_SIGNIN", "VECTRIXDB_EXTRACT_PREFIX"):
        monkeypatch.delenv(name, raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    assert tracing.enable(provider)
    try:
        yield exporter
    finally:
        tracing.disable()


def test_the_server_makes_no_request_spans_and_joins_the_callers_trace(tmp_path, app_spans):
    from fastapi.testclient import TestClient

    from vectrixdb.api.server import create_app

    with TestClient(create_app(db_path=str(tmp_path / "db"))) as c:
        assert c.post("/api/v1/collections", json={"name": "c", "dimension": 3}).status_code == 200
        point = {"id": "invoice-4417.pdf", "vector": [1.0, 0.0, 0.0], "metadata": {"n": SECRET}}
        assert c.post("/api/v1/collections/c/points", json={"points": [point]}).status_code == 200
        app_spans.clear()
        found = c.post(
            "/api/v1/collections/c/search",
            params={"note": SECRET},
            json={"query": [1.0, 0.0, 0.0]},
            headers={"traceparent": f"00-{CALLER_TRACE}-{CALLER_SPAN}-01"},
        )
        assert found.status_code == 200
        assert c.get("/api/v1/collections/c/points/invoice-4417.pdf").status_code == 200
    finished = app_spans.get_finished_spans()
    # A request span would carry the path, with the document's id in it, and the query.
    assert [s.name for s in finished] == ["vectrixdb.search"]
    assert format(finished[0].context.trace_id, "032x") == CALLER_TRACE
    assert format(finished[0].parent.span_id, "016x") == CALLER_SPAN
    every = _every_value(finished)
    assert "zebra" not in every and "invoice" not in every


def test_a_file_sent_to_the_extraction_service_is_read_in_the_senders_trace(app_spans):
    import contextvars

    from fastapi.testclient import TestClient

    from vectrixdb.api.extraction import ExtractionService, create_extraction_app
    from vectrixdb.extract import HttpExtractor

    service = TestClient(create_extraction_app(ExtractionService(), allow_open=True))
    sent = []

    def transport(method, url, headers, body, timeout):
        sent.append({k.lower() for k in headers})
        # A context of its own, as another process has: the header alone carries the trace.
        reply = contextvars.Context().run(
            service.request, method, url, headers=headers, content=body
        )
        return reply.status_code, dict(reply.headers), reply.content

    reader = HttpExtractor(
        "http://testserver", routes={".txt": "/extract/txt"}, body="raw", transport=transport
    )
    with tracing.span("add_document"):
        doc = reader(f"The {SECRET} was paid.".encode(), "invoice-4417.txt")
    assert SECRET in doc.text
    extract, add = app_spans.get_finished_spans()
    assert (extract.name, add.name) == ("vectrixdb.extract", "vectrixdb.add_document")
    assert extract.context.trace_id == add.context.trace_id
    assert extract.parent.span_id == add.context.span_id
    assert extract.attributes["vectrixdb.kind"] == "text"
    every = _every_value([extract, add])
    assert "zebra" not in every and "invoice" not in every

    tracing.disable()
    reader(b"plain words", "a.txt")
    assert "traceparent" in sent[0] and "traceparent" not in sent[-1]
