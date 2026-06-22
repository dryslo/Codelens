"""Кэш и сессии - реализации `SessionStore`.

- `RedisSessionStore` - прод/общий кэш (профиль large), поверх `redis_url`.
- `InProcessCache`  - dev/тесты: TTL-словарь в памяти процесса, без внешних зависимостей.
- `NullCache`       - no-op: всегда промах (когда `redis_url` пуст и кэш не нужен).

`build_cache(redis_url)` выбирает реализацию. Значения сериализуются в JSON (numpy → list
через default), поэтому кэшировать можно списки dict'ов (результаты поиска), строки, числа.

`index-epoch` (общий счётчик) хранится в самом кэше: admin index/remove двигают его
(`bump_epoch`), а ключи поиска включают epoch - старые записи осиротевают без скана/удаления.
"""
import hashlib
import json
import time
from collections.abc import Callable
from typing import Any

from src.domain.interfaces import SessionStore
from src.util import metrics

EPOCH_KEY = "codelens:index-epoch"


def digest(obj: Any) -> str:
    """sha1 от строки или JSON-представления (стабильный) - для ключей кэша."""
    raw = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()


def cache_get_or_set(
    cache: SessionStore | None, key: str, producer: Callable[[], Any], ttl: int,
) -> Any:
    """Cache-aside: вернуть `cache[key]`, иначе вычислить `producer()`, записать и вернуть.

    `cache=None`/`NullCache` (или выключенный) → просто `producer()` без кэша.
    """
    if cache is None or not getattr(cache, "enabled", False):
        return producer()
    hit = cache.get(key)
    if hit is not None:
        metrics.cache_result(True)
        return hit
    metrics.cache_result(False)
    out = producer()
    cache.set(key, out, ttl=ttl)
    return out


def _dumps(value: Any) -> str:
    def _default(o: Any) -> Any:
        if hasattr(o, "tolist"):      # numpy
            return o.tolist()
        raise TypeError(f"not JSON-serializable: {type(o)}")
    return json.dumps(value, ensure_ascii=False, default=_default)


class NullCache(SessionStore):
    """Кэш выключен: всегда промах, запись игнорируется."""

    enabled = False

    def get(self, key: str) -> Any:
        """Всегда промах."""
        return None

    def set(self, key: str, value: Any, ttl: int = 3600) -> None:
        """No-op: запись игнорируется."""
        pass


class InProcessCache(SessionStore):
    """TTL-словарь в памяти процесса - для dev (role=all) и тестов."""

    enabled = True

    def __init__(self) -> None:
        self._d: dict[str, tuple[float | None, str]] = {}

    def get(self, key: str) -> Any:
        """Вернуть значение по ключу с проверкой TTL или None."""
        item = self._d.get(key)
        if item is None:
            return None
        exp, raw = item
        if exp is not None and exp < time.monotonic():
            self._d.pop(key, None)
            return None
        return json.loads(raw)

    def set(self, key: str, value: Any, ttl: int = 3600) -> None:
        """Записать значение с временем жизни."""
        exp = time.monotonic() + ttl if ttl else None
        self._d[key] = (exp, _dumps(value))


class RedisSessionStore(SessionStore):
    """Redis-бэкенд. Значения - JSON-строки; ttl=0 → без срока жизни."""

    enabled = True

    def __init__(self, url: str) -> None:
        import redis
        self._r = redis.Redis.from_url(url, decode_responses=True)

    def get(self, key: str) -> Any:
        """Вернуть значение по ключу или None."""
        raw = self._r.get(key)
        return json.loads(raw) if raw is not None else None

    def set(self, key: str, value: Any, ttl: int = 3600) -> None:
        """Записать значение; ttl=0 → без срока жизни."""
        self._r.set(key, _dumps(value), ex=ttl or None)


def build_cache(redis_url: str | None) -> SessionStore:
    """Пустой url → `NullCache`. Иначе Redis; если клиент/сервер недоступен - `InProcessCache`."""
    if not redis_url:
        return NullCache()
    try:
        cache = RedisSessionStore(redis_url)
        cache._r.ping()
        return cache
    except Exception:
        return InProcessCache()


def current_epoch(cache: SessionStore) -> int:
    """Вернуть текущий index-epoch (0, если не задан)."""
    return int(cache.get(EPOCH_KEY) or 0)


def bump_epoch(cache: SessionStore) -> int:
    """Сдвинуть index-epoch → осиротить все ключи поиска. Вызывается на admin index/remove."""
    new = current_epoch(cache) + 1
    cache.set(EPOCH_KEY, new, ttl=0)
    return new
