"""метрики Prometheus - мягкая деградация (no-op без пакета) и эмиссия при наличии.

Контрактные тесты идут всегда: хелперы не падают и прозрачно возвращают значения даже без
prometheus_client (профиль small/dev). Тесты эмиссии под skipif - требуют prometheus_client.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.util import metrics

enabled_only = pytest.mark.skipif(not metrics._ENABLED, reason="нужен prometheus_client")


# ---------- контракт: ничего не падает в любом режиме ----------

def test_stage_is_passthrough_context():
    with metrics.stage("embed"):
        pass                                  # не бросает ни в enabled, ни в no-op


def test_timed_returns_inner_value():
    assert metrics.timed("bm25", lambda: 42) == 42


def test_cache_and_queue_helpers_never_raise():
    metrics.cache_result(True)
    metrics.cache_result(False)
    metrics.set_queue_depth("queued", 3)


def test_ingest_timer_reraises():
    with pytest.raises(ValueError):
        with metrics.ingest_timer():
            raise ValueError("boom")


def test_mount_does_not_break_app():
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    metrics.mount(app, "test")
    client = TestClient(app)
    assert client.get("/ping").json() == {"ok": True}


# ---------- эмиссия (только при установленном prometheus_client) ----------

@enabled_only
def test_stage_increments_histogram():
    from prometheus_client import REGISTRY
    name = "codelens_retrieval_stage_duration_seconds_count"
    before = REGISTRY.get_sample_value(name, {"stage": "store"}) or 0.0
    with metrics.stage("store"):
        pass
    assert (REGISTRY.get_sample_value(name, {"stage": "store"}) or 0.0) == before + 1


@enabled_only
def test_cache_result_counts_hit_and_miss():
    from prometheus_client import REGISTRY
    name = "codelens_cache_total"
    h0 = REGISTRY.get_sample_value(name, {"result": "hit"}) or 0.0
    m0 = REGISTRY.get_sample_value(name, {"result": "miss"}) or 0.0
    metrics.cache_result(True)
    metrics.cache_result(False)
    assert (REGISTRY.get_sample_value(name, {"result": "hit"}) or 0.0) == h0 + 1
    assert (REGISTRY.get_sample_value(name, {"result": "miss"}) or 0.0) == m0 + 1


@enabled_only
def test_ingest_timer_labels_status():
    from prometheus_client import REGISTRY
    name = "codelens_ingest_job_duration_seconds_count"
    ok0 = REGISTRY.get_sample_value(name, {"status": "done"}) or 0.0
    fail0 = REGISTRY.get_sample_value(name, {"status": "failed"}) or 0.0
    with metrics.ingest_timer():
        pass
    with pytest.raises(RuntimeError):
        with metrics.ingest_timer():
            raise RuntimeError("x")
    assert (REGISTRY.get_sample_value(name, {"status": "done"}) or 0.0) == ok0 + 1
    assert (REGISTRY.get_sample_value(name, {"status": "failed"}) or 0.0) == fail0 + 1


@enabled_only
def test_metrics_endpoint_and_http_counter_use_route_template():
    from prometheus_client import REGISTRY
    app = FastAPI()

    @app.get("/item/{item_id}")
    def item(item_id: str):
        return {"id": item_id}

    metrics.mount(app, "svc")
    client = TestClient(app)
    client.get("/item/abc")
    client.get("/item/xyz")
    # маршрут размечен шаблоном, а не сырым путём: один ряд на оба запроса (низкая кардинальность)
    count = REGISTRY.get_sample_value("codelens_http_requests_total",
                                      {"service": "svc", "method": "GET",
                                       "route": "/item/{item_id}", "status": "200"})
    assert count == 2
    body = client.get("/metrics")
    assert body.status_code == 200
    assert "codelens_http_requests_total" in body.text
