"""Ограничитель частоты (fixed-window) для чувствительных эндпоинтов.

Счётчик в общем кэше (Redis в large - кросс-реплично), при выключенном кэше в памяти процесса.
Окно фиксированное: ключ включает номер окна, запись в кэше живёт TTL=window.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.domain.interfaces import SessionStore

_local: dict[str, tuple[int, int]] = {}   # key -> (bucket, count); fallback без кэша


def allow(cache: SessionStore | None, key: str, limit: int, window: int) -> bool:
    """Учесть обращение и вернуть True, если лимит за окно не превышен (limit<=0 - без лимита)."""
    if limit <= 0:
        return True
    bucket = int(time.time()) // window
    if cache is not None and getattr(cache, "enabled", False):
        ck = f"rl:{key}:{bucket}"
        count = int(cache.get(ck) or 0) + 1
        cache.set(ck, count, ttl=window)
    else:
        prev_bucket, prev_count = _local.get(key, (bucket, 0))
        count = prev_count + 1 if prev_bucket == bucket else 1
        _local[key] = (bucket, count)
    return count <= limit
