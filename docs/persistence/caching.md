# Кэширование и сессии

Разбор `src/persistence/cache.py` и того, как кэш встроен в пайплайн.

## Идея

Один компонент кэша на процесс реализует порт `SessionStore` (`get`/`set(ttl)`) и используется
тремя независимыми путями:

| Что в кэше | Где живёт кэш | Ключ | Зачем |
|---|---|---|---|
| **результат поиска** | `HybridRetriever` (оркестратор) | `search:{epoch}:sha1({query,flags,k})` | на повтор/демо/eval не гонять embed→store→llm→rerank |
| **hyde / multiquery** | `HyDEExpander` / `MultiQueryExpander` | `hyde:sha1(q)` · `mq:{n}:sha1(q)` | дорогой LLM-вызов расширения не повторять |
| **index-реестр** (`source`+`file`→`hash`) | `CachingRegistry` (вокруг `SqlRegistry`) | `reg:{source}:{file}` | инкрементальная индексация не бьёт БД на каждый файл |
| **ответ LLM** (`answer`) | `LocalBackend` | `answer:sha1({query,chunk_ids,model})` | повтор того же вопроса по тем же фрагментам - мгновенно |
| **condense** (фоллоу-ап→запрос) | `LocalBackend` | `condense:sha1({convo,follow-up})` | не дёргать LLM повторно на тот же контекст чата |
| **состояние чата** (`get_messages`) | `LocalBackend` | `chat:{chat_id}:messages` | чтение истории из кэша; tombstone на `append` |

Разделение слоёв: поисковый кэш инкапсулирован в ретривере-оркестраторе (вокруг чистого
ядра `_search`), а кэш реестра и ответов - на стороне backend. Ядро поиска остаётся чистой
функцией `query → results`; кэш - обёртка вокруг него (cache-aside).

## Реализации `SessionStore` (`cache.py`)

```
build_cache(redis_url):
  redis_url пуст        → NullCache       # кэш выключен (дефолт dev)
  redis_url задан       → RedisSessionStore (+ping)
  redis недоступен      → InProcessCache   # fallback: TTL-словарь в памяти процесса
```

- **`NullCache`** - `enabled=False`, всегда промах, запись игнорируется. Поведение системы при
  пустом `redis_url` ровно такое же, как при отсутствии кэша.
- **`InProcessCache`** - `enabled=True`, TTL-словарь в памяти; для dev (`role=all`) и тестов,
  без внешнего Redis.
- **`RedisSessionStore`** - `enabled=True`, прод/общий кэш (профиль large). Значения - JSON-строки
  (`_dumps` умеет numpy через `.tolist()`), `ttl=0` → без срока жизни.

Везде, где кэш опционален, проверяется `getattr(cache, "enabled", False)` - `NullCache`
прозрачно отключает всю логику.

## Cache-aside в ретривере (`retrieval/hybrid.py`)

```python
def search(self, query, k=5, flags=None, mode=None):
    flags = ...политика применена...
    if self.cache and self.cache.enabled:
        key = self._cache_key(query, flags, k, current_epoch(self.cache))
        hit = self.cache.get(key)
        if hit is not None:
            return hit                      # ← ядро НЕ запускается
        result = self._search(query, k, flags)
        self.cache.set(key, result, ttl=self.cache_ttl)
        return result
    return self._search(query, k, flags)    # чистое ядро без кэша
```

- `_search(query, k, flags)` - прежний пайплайн (dense → bm25 → RRF → rerank → mmr), без
  знания о кэше.
- Ключ включает `index-epoch` - при переиндексации старые ключи осиротевают (см. ниже).
- На попадание ни эмбеддер, ни стор, ни LLM/реранкер не вызываются.

## Кэш index-реестра (`persistence/registry_repo.py`)

`CachingRegistry` оборачивает любой `IndexRegistry`:
- `get_hash` - сначала кэш, потом база; найденный непустой хэш кладётся в кэш.
- `set_hash` - write-through (база + кэш синхронно).
- `remove` - tombstone в кэше (следующий `get_hash` уйдёт в базу), поэтому stale-хэшей нет.

Это ускоряет «skip неизменного файла» в [pipeline.index_path](../indexing/pipeline.md): на каждый
файл - лукап в кэше, а не запрос в Postgres/SQLite.

## Инвалидация: `index-epoch`

Счётчик `index-epoch` хранится в самом кэше (`current_epoch`/`bump_epoch`) и входит в ключ поиска.
`LocalBackend.index()` и `LocalBackend.remove()` после изменения индекса вызывают
`_invalidate_search()` → `bump_epoch(cache)`. Все прежние `search:{epoch}:…` ключи перестают
совпадать и осиротевают (без скана/удаления по маске). Так backend, владея и кэшем, и
admin-эндпоинтами, держит инвалидацию когерентной.

## Где собирается (`factory.py`)

```python
cache = build_cache(cfg.get("redis_url"))
cache_ttl = int((cfg.get("cache") or {}).get("ttl", 3600))

registry = SqlRegistry(sf)
if getattr(cache, "enabled", False):
    registry = CachingRegistry(registry, cache, ttl=cache_ttl)

comp = {... "retriever": HybridRetriever(..., cache=cache, cache_ttl=cache_ttl),
        "registry": registry, "cache": cache, ...}
```

Один `cache` на процесс: инжектится в ретривер (поисковый кэш) и его экспандеры (hyde/multiquery),
в `CachingRegistry` (index-реестр) и доступен backend (ответы, condense, состояние чата).
`RedisSessionStore` - полноценный `SessionStore`, поэтому обслуживает и пользовательские сессии
(access-токены авторизации).

## Батч-эмбеддинг (throughput bulk-путей)

Не про кэш, но рядом - оптимизация пропускной способности (не latency живого запроса):
- индексация (`pipeline.index_path`) копит чанки через файлы и кодирует/пишет батчами
  (`batch=256`), а не по одному файлу за вызов;
- eval (`evaluate.run_eval`) эмбеддит все вопросы одним `encode` и ищет по готовым
  векторам - `HybridRetriever.search(query_emb=…)` берёт готовый вектор, пока запрос не меняется
  hyde/multiquery (иначе кодирует сам - безопасно при любых флагах);
- размер батча модели - `LocalEmbedder.batch_size` (config `embedder.batch_size`).

## Конфиг (`config/config.yaml`)

```yaml
redis_url: ${REDIS_URL:-}      # пусто → NullCache (кэш выкл); иначе Redis (dev-fallback in-process)
cache:
  ttl: ${CACHE_TTL:-3600}      # TTL записей кэша, сек
```

## Профили

- **small/dev** - `redis_url` пуст → `NullCache`, поведение без изменений. Чтобы увидеть ускорение
  повторного прогона `evaluate.py`, задать `REDIS_URL` (или поднять локальный Redis).
- **large** - общий Redis (обычный, не шардированный): кэш + сессии. Поисковый кэш - за
  ретривером, реестр/ответы/сессии - на стороне backend; модель-сервисы (embedder/reranker/llm)
  остаются stateless и Redis не касаются.

## Тесты

[tests/test_cache.py](../../tests/test_cache.py): hit/miss, истечение TTL, `NullCache`,
`build_cache`, epoch-инвалидация, `CachingRegistry` (write-through + tombstone), ретривер
(skip ядра на попадании, ре-запуск после `bump_epoch`, работа без кэша).
