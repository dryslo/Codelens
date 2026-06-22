"""Метрики Prometheus для сервисов CodeLens.

Мягкая деградация: без пакета `prometheus_client` все хелперы становятся no-op, а `/metrics`
не монтируется. Модуль безопасно импортировать из любого процесса без try/except на каждой точке.

Собираем:
- HTTP: счётчик запросов и латентность по сервису/методу/маршруту/статусу.
- Ретривер: латентность каждой стадии (embed/store/bm25/hyde/multiquery/rerank/mmr).
- Кэш поиска: hit/miss.
- Ingest: длительность job по финальному статусу + глубина очереди по состояниям.

Реестр процессный. Поды запускаем 1 uvicorn-воркером на процесс; при нескольких воркерах
нужен prometheus multiprocess-режим (здесь не используется - масштаб идёт репликами пода).
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI, Request, Response

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
    _ENABLED = True
except Exception:  # noqa: BLE001 - prometheus_client не установлен, весь модуль no-op
    _ENABLED = False

# Бакеты онлайн-латентностей (HTTP, стадии ретривера): 5мс-10с.
_FAST = (.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10)
# Бакеты ingest-job: 0.5с-10мин.
_SLOW = (.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)

if _ENABLED:
    HTTP_REQS = Counter(
        "codelens_http_requests_total", "HTTP-запросы по сервису/методу/маршруту/статусу",
        ["service", "method", "route", "status"])
    HTTP_LAT = Histogram(
        "codelens_http_request_duration_seconds", "Латентность HTTP-запроса",
        ["service", "method", "route"], buckets=_FAST)
    RETRIEVAL_STAGE = Histogram(
        "codelens_retrieval_stage_duration_seconds", "Латентность стадии ретривера",
        ["stage"], buckets=_FAST)
    CACHE_OPS = Counter(
        "codelens_cache_total", "Обращения к кэшу поиска (cache-aside)", ["result"])  # hit|miss
    INGEST_JOB = Histogram(
        "codelens_ingest_job_duration_seconds", "Длительность ingest-job по финальному статусу",
        ["status"], buckets=_SLOW)
    QUEUE_DEPTH = Gauge(
        "codelens_ingest_queue_jobs", "Глубина очереди ingest по состояниям", ["state"])


# ---------- ретривер: стадии ----------

@contextmanager
def stage(name: str) -> Iterator[None]:
    """Замерить длительность стадии ретривера (embed/store/bm25/hyde/...)."""
    if not _ENABLED:
        yield
        return
    t = time.perf_counter()
    try:
        yield
    finally:
        RETRIEVAL_STAGE.labels(stage=name).observe(time.perf_counter() - t)


def timed(name: str, fn: Callable[[], object]) -> object:
    """stage(name) как обёртка вокруг вызова - для списков задач run_parallel."""
    with stage(name):
        return fn()


# ---------- кэш ----------

def cache_result(hit: bool) -> None:
    """Отметить попадание/промах кэша поиска."""
    if _ENABLED:
        CACHE_OPS.labels(result="hit" if hit else "miss").inc()


# ---------- ingest ----------

@contextmanager
def ingest_timer() -> Iterator[None]:
    """Замерить ingest-job, разметка по финальному статусу (done или failed при исключении)."""
    if not _ENABLED:
        yield
        return
    t = time.perf_counter()
    status = "done"
    try:
        yield
    except Exception:
        status = "failed"
        raise
    finally:
        INGEST_JOB.labels(status=status).observe(time.perf_counter() - t)


def set_queue_depth(state: str, n: int) -> None:
    """Выставить gauge глубины очереди для состояния (queued/started/failed)."""
    if _ENABLED:
        QUEUE_DEPTH.labels(state=state).set(n)


# ---------- экспозиция ----------

def mount(app: FastAPI, service: str) -> None:
    """Подключить /metrics и HTTP-middleware к FastAPI-приложению (no-op без prometheus_client)."""
    if not _ENABLED:
        return
    from fastapi import Response

    @app.middleware("http")
    async def _measure(request: Request, call_next: Callable) -> Response:
        if request.url.path == "/metrics":
            return await call_next(request)
        t = time.perf_counter()
        status = 500                          # если call_next бросит - останется 5xx
        try:
            resp = await call_next(request)
            status = resp.status_code
            return resp
        finally:
            # шаблон маршрута ("/chats/{chat_id}"), а не сырой путь - иначе взрыв кардинальности по id.
            route = request.scope.get("route")
            tmpl = getattr(route, "path", None) or request.url.path
            HTTP_LAT.labels(service, request.method, tmpl).observe(time.perf_counter() - t)
            HTTP_REQS.labels(service, request.method, tmpl, str(status)).inc()

    @app.get("/metrics")
    def metrics():  # noqa: ANN202 - FastAPI-роут; аннотация Response под future-annotations ломает openapi
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def start_metrics_server(port: int = 9100) -> bool:
    """Поднять отдельный HTTP-эндпоинт /metrics (для процессов без FastAPI - worker).

    Возвращает True, если сервер поднят; no-op (False) без prometheus_client.
    """
    if not _ENABLED:
        return False
    from prometheus_client import start_http_server
    start_http_server(port)
    return True
