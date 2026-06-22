"""Очередь фоновых задач (порт JobQueue). Размещение - конфигом jobs.kind."""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.jobs.inprocess import InProcessQueue

if TYPE_CHECKING:
    from src.domain.interfaces import JobQueue


def build_queue(jobs_cfg: dict | None, redis_url: str | None = None) -> JobQueue:
    """Собрать очередь по конфигу (inprocess по умолчанию, redis при jobs.kind=redis)."""
    kind = (jobs_cfg or {}).get("kind", "inprocess")
    if kind == "redis":
        if not redis_url:
            raise ValueError("jobs.kind=redis требует redis_url")
        from src.jobs.redis_queue import RedisQueue
        return RedisQueue(redis_url)
    return InProcessQueue()


__all__ = ["InProcessQueue", "build_queue"]
