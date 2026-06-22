"""Общее тело ingest-задачи: acquire -> index_path -> инвалидация поиска.

Один код для обеих очередей (InProcess и RQ). `comp` - composition root (store/embedder/
registry/cache/index_path). `report` - колбэк прогресса (см. index_path).
"""
from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from src.persistence.cache import bump_epoch
from src.util import metrics

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.factory import Components


def run_ingest(task: dict, report: Callable[[dict], None], comp: Components) -> dict:
    """Выполнить ingest-задачу с замером длительности по финальному статусу."""
    with metrics.ingest_timer():     # длительность job по финальному статусу (done|failed)
        return _run_ingest(task, report, comp)


def _run_ingest(task: dict, report: Callable[[dict], None], comp: Components) -> dict:
    kind = task["kind"]
    source = task["source"]
    if kind == "zip":
        from src.ingest.acquire import from_zip
        folder = from_zip(task["data"])
    elif kind == "github":
        from src.ingest.acquire import from_github
        folder = from_github(task["url"], task.get("ref"))
    else:
        raise ValueError(f"неизвестный тип ingest: {kind!r}")
    try:
        res = comp.index_path(str(folder), source, comp.store, comp.embedder,
                              comp.registry, True, progress=report)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    cache = comp.cache
    if cache is not None and getattr(cache, "enabled", False):
        bump_epoch(cache)        # индекс изменился, осиротить кэш поиска
    return res
