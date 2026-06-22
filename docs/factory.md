# factory.py - composition root

Единственное место, где компоненты создаются и связываются по конфигу. Весь остальной код получает готовые объекты, не зная про конкретные реализации.

## Контейнер `Components`
```python
@dataclass
class Components:
    cfg: dict
    backend: BackendClient | None = None
    embedder: Embedder | None = None
    ...
```
- Dataclass вместо словаря: поля типизированы, mypy ловит опечатки и доступ к несуществующему атрибуту. Все поля кроме `cfg` опциональны - профиль `frontend` заполняет только `backend` и `cfg`, полный пайплайн (`all`/`backend`) - остальные.

## Раскрытие `${VAR:-default}`
```python
def _expand(node):
    if isinstance(node, dict):
        return {k: _expand(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand(v) for v in node]
    if isinstance(node, str):
        m = re.fullmatch(r"\$\{(\w+)(?::-(.*))?\}", node)
        if m:
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else "")
        return node
    return node
```
- Рекурсивно обходит распарсенный YAML (словарь/список/строка) и в строках раскрывает `${VAR:-default}`.
- Группа 1 - имя переменной (`\w+`), необязательная группа 2 после `:-` - дефолт. `os.environ.get(name, default)` - значение из окружения или дефолт. Без синтаксиса `${...}` строка остаётся как есть. YAML сам не подставляет env, поэтому один `config.yaml` параметризуется переменными (профиль, адреса, ключи) без правки файла.

```python
def load_config(path=None):
    path = path or os.environ.get("CODELENS_CONFIG", "config/config.yaml")
    with open(path, encoding="utf-8") as f:
        return _expand(yaml.safe_load(f))
```
- Путь конфига - из аргумента, иначе из `CODELENS_CONFIG`, иначе дефолт. `yaml.safe_load` → dict, затем `_expand` раскрывает переменные.

## Сборка LLM
```python
def build_llms(llm_cfg: dict) -> dict:
    if llm_cfg.get("kind") == "remote":
        from src.llm.remote import build_remote_llms
        return build_remote_llms(llm_cfg["llm_url"])
    out = {}
    for name, spec in (llm_cfg.get("providers") or {}).items():
        try:
            kind = spec["kind"]
            if kind == "ollama":
                ...
            elif kind == "openai_compatible":
                ...
        except Exception:
            pass
    return out
```
- `kind=remote` - провайдеры живут в llm-gateway, тут только HTTP-клиенты под тем же контрактом `{name: LLMProvider}`.
- Локально: обход `llm.providers`, по `kind` создаётся нужный класс, остальные ключи спека передаются как kwargs (исключив `kind`).
- `try/except: pass` - degradable: недоступный провайдер (нет пакета/ключа) пропускается, остальные работают. Возвращается `{имя: провайдер}`.

## Сборка адаптеров (выбор реализации)
```python
def _build_embedder(cfg):
    e = cfg["embedder"]
    if e.get("kind", "local") == "remote":
        from src.embeddings.remote import RemoteEmbedder
        return RemoteEmbedder(cfg.get("embedder_url") or cfg["inference_url"])
    from src.embeddings.local import LocalEmbedder
    return LocalEmbedder(e["model"], batch_size=int(e.get("batch_size", 32)))
```
- По `embedder.kind` - local (в процессе) или remote (inference-сервис). Ленивые импорты: загружается только нужная ветка. `batch_size` - размер батча модели для throughput на индексации/eval.

```python
def _build_reranker(cfg):
    r = cfg.get("reranker", {})
    if str(r.get("enabled", "false")).lower() != "true":
        return None
    ...
```
- Если `reranker.enabled` не «true» - `None` (реранк отключён). `str(...).lower()` - значение могло прийти строкой из env. Иначе local/remote по аналогии.

```python
def _build_store(cfg):
    v = cfg["vector"]
    if v["kind"] == "qdrant":
        from src.stores.qdrant import QdrantStore
        return QdrantStore(url=v["url"], dim=int(cfg["embedder"]["dim"]),
                           shards=int(v.get("shards", 2)), replicas=int(v.get("replicas", 2)))
    from src.stores.chroma import ChromaStore
    return ChromaStore(path=v.get("path", ".chroma"))
```
- `vector.kind` → Qdrant (large) или Chroma (small). `int(...)` - приведение, т.к. из env числа приходят строками. Обе реализуют один `VectorStore`.

## `build()` - главная сборка
```python
def build() -> Components:
    cfg = load_config()
    role = cfg.get("role", "all")
    if role == "frontend":
        from src.clients.backend import HttpBackend
        return Components(cfg=cfg, backend=HttpBackend(cfg["backend_url"]))
```
- Ранний выход для фронта: в `role=frontend` пайплайн не нужен - возвращается только `HttpBackend` (ходит в backend по сети). Так фронт-контейнер не загружает модели/БД.

```python
    embedder = _build_embedder(cfg)
    reranker = _build_reranker(cfg)
    store = _build_store(cfg)
    init_db(cfg["database_dsn"])           # dev-удобство; в проде - Alembic
    sf = make_session_factory(cfg["database_dsn"])

    cache = build_cache(cfg.get("redis_url"))           # NullCache при пустом redis_url
    cache_ttl = int((cfg.get("cache") or {}).get("ttl", 3600))
```
- Для `all`/`backend`: импорты внутри функции (быстрый старт фронта без них). Собираются эмбеддер, реранкер, стор. `init_db` создаёт таблицы (dev). `sf` - фабрика сессий. Кэш собирается один на процесс.

```python
    auth_cfg = AuthConfig.from_cfg(cfg)
    if auth_cfg.enabled and not getattr(cache, "enabled", False):
        cache = InProcessCache()   # access-сессиям нужен рабочий стор даже без redis (dev/small)

    registry = SqlRegistry(sf)
    if getattr(cache, "enabled", False):
        registry = CachingRegistry(registry, cache, ttl=cache_ttl)   # кэш source+file→hash
```
- При включённом auth access-сессиям нужен рабочий стор: без redis вместо NullCache берётся `InProcessCache`. Реестр оборачивается `CachingRegistry`, только если кэш реально включён (см. [caching.md](persistence/caching.md)).

```python
    auth = AuthService(SqlUsers(sf), SqlCredentials(sf), SqlIdentities(sf),
                       SqlRefreshTokens(sf), cache, auth_cfg)
    auth.ensure_admin(os.environ.get("ADMIN_LOGIN"), os.environ.get("ADMIN_PASSWORD"))

    llms = build_llms(cfg.get("llm", {}))
    fast = cfg.get("llm", {}).get("fast")
    if fast and fast in llms:
        hyde = HyDEExpander(llms[fast], cache=cache, cache_ttl=cache_ttl)
        mq = MultiQueryExpander(llms[fast], cache=cache, cache_ttl=cache_ttl)

    policy = FlagsPolicy.from_config((cfg.get("retrieval") or {}).get("flags"))
    jobs = build_queue(cfg.get("jobs"), cfg.get("redis_url"))
```
- `AuthService` собирается из SQL-репозиториев и кэша; `ensure_admin` создаёт админа из env при первом запуске.
- Сборка LLM. Если задан и доступен `llm.fast` - создаются расширители HyDE и MultiQuery на его основе (думающий режим), оба с кэшем выхода LLM. `policy` - деплойная политика каналов поиска из `config.yaml`. `jobs` - очередь ingest (InProcess или Redis/RQ по конфигу).

```python
    comp = Components(
        cfg=cfg, embedder=embedder, reranker=reranker, store=store,
        retriever=HybridRetriever(store, embedder, reranker, hyde=hyde, multiquery=mq,
                                  policy=policy, cache=cache, cache_ttl=cache_ttl),
        history=SqlHistory(sf), registry=registry, cache=cache, auth=auth,
        llms=llms, fast=fast, flag_policy=policy, jobs=jobs,
        index_path=index_path, remove_source=remove_source,
    )
    if hasattr(jobs, "bind"):
        jobs.bind(comp)        # InProcessQueue исполняет ingest в этом же процессе - нужен comp

    comp.backend = LocalBackend(comp)
    return comp
```
- `HybridRetriever` получает стор/эмбеддер/реранкер/expander'ы/политику и кэш (поисковый cache-aside внутри оркестратора). Репозитории - фабрику сессий; `cache` доступен backend для ответов/condense/состояния чата.
- `InProcessQueue.bind(comp)` нужен, потому что в small-профиле ingest исполняется в этом же процессе и опирается на собранные компоненты.
- `LocalBackend(comp)` создаётся после заполнения `comp` (хранит ссылку и читает поля при вызовах - поэтому порядок корректен).

Ленивые импорты - чтобы профиль/роль загружали только свои зависимости.
