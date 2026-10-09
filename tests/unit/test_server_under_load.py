"""The server under load: the model off the event loop, in line, warmed, and built once."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")

from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb.api import inference  # noqa: E402

KEY = {"api-key": "the-key"}


class SlowEmbedder:
    """Stands in for the model: takes ``seconds`` a call, and counts the calls and their sizes."""

    _vectrix_model = None

    def __init__(self, seconds: float = 0.0, gate: threading.Event = None, fail: bool = False):
        self.seconds, self.gate, self.fail = seconds, gate, fail
        self.calls = []

    def embed(self, texts):
        texts = [texts] if isinstance(texts, str) else list(texts)
        self.calls.append(len(texts))
        if self.fail:
            raise RuntimeError("the model file is missing")
        if self.gate is not None:
            self.gate.wait(5)
        time.sleep(self.seconds)
        return np.ones((len(texts), 384), dtype=np.float32) / np.sqrt(384)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in (
        "VECTRIXDB_WARM",
        "VECTRIXDB_INFERENCE_CONCURRENCY",
        "VECTRIXDB_INFERENCE_WAIT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VECTRIXDB_API_KEY", "the-key")
    inference._reset_for_tests()
    yield
    inference._reset_for_tests()


def use(monkeypatch, embedder):
    import vectrixdb.api.server as server

    monkeypatch.setattr(server, "get_text_embedder", lambda model=None: embedder)
    return embedder


@pytest.fixture
def app(tmp_path):
    from vectrixdb.api.server import create_app

    return create_app(db_path=str(tmp_path / "data"))


def make_collection(client):
    made = client.post(
        "/api/v2/collections",
        headers=KEY,
        json={"name": "handbook", "dimension": 384, "metric": "cosine", "enable_text_index": True},
    )
    assert made.status_code in (200, 201), made.text


class TestOffTheEventLoop:
    def test_a_slow_upsert_does_not_hold_health(self, app, monkeypatch):
        use(monkeypatch, SlowEmbedder(seconds=1.5))
        with TestClient(app) as client:
            make_collection(client)
            done = {}

            def upsert():
                started = time.perf_counter()
                reply = client.post(
                    "/api/v1/collections/handbook/text-upsert",
                    headers=KEY,
                    json={"points": [{"id": "a", "text": "Refunds take ten days."}]},
                )
                done["upsert"] = (reply.status_code, time.perf_counter() - started)

            worker = threading.Thread(target=upsert)
            worker.start()
            time.sleep(0.3)
            started = time.perf_counter()
            health = client.get("/health")
            health_took = time.perf_counter() - started
            worker.join(10)
            assert health.status_code == 200
            assert health_took < 0.6, f"/health waited {health_took:.2f}s behind the model"
            assert done["upsert"][0] == 200 and done["upsert"][1] >= 1.4

    def test_a_slow_search_does_not_hold_another_route(self, app, monkeypatch):
        use(monkeypatch, SlowEmbedder(seconds=1.2))
        with TestClient(app) as client:
            make_collection(client)
            worker = threading.Thread(
                target=client.post,
                args=("/api/v1/collections/handbook/text-search",),
                kwargs={"headers": KEY, "json": {"query_text": "refunds"}},
            )
            worker.start()
            time.sleep(0.3)
            started = time.perf_counter()
            assert client.get("/api/v1/collections", headers=KEY).status_code == 200
            assert time.perf_counter() - started < 0.6
            worker.join(10)

    def test_a_slow_rerank_does_not_hold_health(self, app, monkeypatch):
        import vectrixdb.core.collection as core

        use(monkeypatch, SlowEmbedder())
        real = core.Collection.search_with_rerank

        def slow(self, *a, **k):
            time.sleep(1.2)
            return real(self, *a, **k)

        monkeypatch.setattr(core.Collection, "search_with_rerank", slow)
        with TestClient(app) as client:
            make_collection(client)
            client.post(
                "/api/v1/collections/handbook/text-upsert",
                headers=KEY,
                json={"points": [{"id": "a", "text": "Refunds take ten days."}]},
            )
            worker = threading.Thread(
                target=client.post,
                args=("/api/v1/collections/handbook/text-search",),
                kwargs={"headers": KEY, "json": {"query_text": "refunds", "rerank": True}},
            )
            worker.start()
            time.sleep(0.3)
            started = time.perf_counter()
            assert client.get("/health").status_code == 200
            assert time.perf_counter() - started < 0.6, "/health waited behind the reranker"
            worker.join(10)


class TestInLine:
    def test_a_long_upload_is_embedded_a_batch_at_a_time(self, monkeypatch):
        fake = use(monkeypatch, SlowEmbedder())
        vectors = inference.embed_blocking([f"chunk {i}" for i in range(70)])
        assert fake.calls == [32, 32, 6]
        assert vectors.shape == (70, 384)

    def test_a_full_line_is_a_503_with_retry_after(self, app, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_INFERENCE_CONCURRENCY", "1")
        monkeypatch.setenv("VECTRIXDB_INFERENCE_WAIT_SECONDS", "0.3")
        gate = threading.Event()
        use(monkeypatch, SlowEmbedder(gate=gate))
        with TestClient(app) as client:
            make_collection(client)
            holder = threading.Thread(
                target=client.post,
                args=("/api/v1/collections/handbook/text-search",),
                kwargs={"headers": KEY, "json": {"query_text": "first"}},
            )
            holder.start()
            time.sleep(0.2)
            refused = client.post(
                "/api/v1/collections/handbook/text-search",
                headers=KEY,
                json={"query_text": "second"},
            )
            gate.set()
            holder.join(10)
        assert refused.status_code == 503
        assert refused.headers["retry-after"] == "5"
        assert "waited" in refused.text

    def test_the_wait_is_a_setting(self, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_INFERENCE_WAIT_SECONDS", "nonsense")
        assert inference.wait_seconds() == 30.0
        monkeypatch.setenv("VECTRIXDB_INFERENCE_CONCURRENCY", "0")
        assert inference.concurrency() == 2


class TestWarmAndReady:
    def test_ready_waits_for_the_models(self, app, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_WARM", "1")
        gate = threading.Event()
        use(monkeypatch, SlowEmbedder(gate=gate))
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200, "alive while it loads"
            loading = client.get("/ready")
            assert loading.status_code == 503 and loading.json()["models"] == "loading"
            gate.set()
            for _ in range(50):
                answer = client.get("/ready")
                if answer.status_code == 200:
                    break
                time.sleep(0.05)
            assert answer.status_code == 200 and answer.json()["models"] == "loaded"

    def test_a_model_that_fails_to_load_says_why(self, app, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_WARM", "1")
        use(monkeypatch, SlowEmbedder(fail=True))
        with TestClient(app) as client:
            for _ in range(50):
                answer = client.get("/ready")
                if answer.json().get("models") != "loading":
                    break
                time.sleep(0.05)
        assert answer.status_code == 503
        assert "the model file is missing" in answer.json()["error"]

    def test_without_warming_it_is_ready_and_loads_on_first_use(self, app, monkeypatch):
        fake = use(monkeypatch, SlowEmbedder())
        with TestClient(app) as client:
            answer = client.get("/ready")
        assert answer.status_code == 200 and answer.json()["models"] == "on first use"
        assert fake.calls == []

    def test_ready_needs_no_key(self, app, monkeypatch):
        use(monkeypatch, SlowEmbedder())
        with TestClient(app) as client:
            assert client.get("/ready").status_code == 200

    def test_serve_warms_unless_told_not_to(self, monkeypatch):
        import vectrixdb.api.server as server

        import os

        ran = {}
        # run_server writes these; monkeypatch records each, so it is put back after the test.
        for name in ("VECTRIXDB_PATH", "VECTRIXDB_DASHBOARD", "VECTRIXDB_WARM"):
            monkeypatch.setenv(name, "recorded")
            monkeypatch.delenv(name)
        monkeypatch.setattr(server, "refuse_open_server", lambda *a, **k: None)
        monkeypatch.setitem(
            __import__("sys").modules,
            "uvicorn",
            type("U", (), {"run": staticmethod(lambda *a, **k: ran.update(k))}),
        )
        server.run_server(db_path="./unused")
        assert os.environ["VECTRIXDB_WARM"] == "1"


class TestOneSession:
    def test_racing_first_requests_build_one_model(self, monkeypatch):
        import vectrixdb.api.server as server
        import vectrixdb.models as models

        built = []

        class Counting(SlowEmbedder):
            def __init__(self, model=None):
                time.sleep(0.2)
                built.append(model)
                super().__init__()

        monkeypatch.setattr(models, "DenseEmbedder", Counting)
        monkeypatch.setattr(server, "_text_embedder", None)
        got = []
        threads = [
            threading.Thread(target=lambda: got.append(server.get_text_embedder()))
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert len(built) == 1
        assert len({id(e) for e in got}) == 1
        monkeypatch.setattr(server, "_text_embedder", None)


class TestThreads:
    @pytest.fixture(autouse=True)
    def fresh(self, monkeypatch):
        from vectrixdb.models import _threads

        monkeypatch.delenv("VECTRIXDB_THREADS", raising=False)
        _threads.session_threads.cache_clear()
        yield _threads
        _threads.session_threads.cache_clear()

    def test_the_setting_wins(self, fresh, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_THREADS", "3")
        assert fresh.session_threads() == 3

    def test_read_once(self, fresh, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_THREADS", "3")
        assert fresh.session_threads() == 3
        monkeypatch.setenv("VECTRIXDB_THREADS", "1")
        assert fresh.session_threads() == 3

    def test_never_more_than_four_unless_asked(self, fresh, monkeypatch):
        monkeypatch.setattr(fresh, "available_cpus", lambda: 32)
        assert fresh.session_threads() == 4

    def test_a_bad_value_falls_back(self, fresh, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_THREADS", "lots")
        monkeypatch.setattr(fresh, "available_cpus", lambda: 2)
        assert fresh.session_threads() == 2

    @pytest.mark.parametrize(
        "cpu_max, expected", [("150000 100000", 2), ("100000 100000", 1), ("max 100000", None)]
    )
    def test_a_container_quota_v2(self, fresh, tmp_path, cpu_max, expected):
        (tmp_path / "cpu.max").write_text(cpu_max)
        found = fresh.available_cpus(tmp_path)
        assert found >= 1
        if expected is not None:
            assert found <= expected, "held to the quota, rounded up"

    def test_a_container_quota_v1(self, fresh, tmp_path):
        (tmp_path / "cpu").mkdir()
        (tmp_path / "cpu" / "cpu.cfs_quota_us").write_text("50000")
        (tmp_path / "cpu" / "cpu.cfs_period_us").write_text("100000")
        assert fresh.available_cpus(tmp_path) == 1

    def test_every_session_asks_the_one_setting(self):
        from pathlib import Path

        import vectrixdb.models.embedded as embedded

        source = Path(embedded.__file__).read_text(encoding="utf-8")
        assert "intra_op_num_threads = 4" not in source
        assert source.count("intra_op_num_threads = session_threads()") == 5
