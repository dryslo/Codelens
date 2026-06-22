# services/inference_app.py - сервис моделей (профиль large)

Отдельный сервис только под модели (embed/rerank). Масштабируется независимо на тяжёлых нодах.

```python
app = FastAPI(title="codelens-inference", lifespan=lifespan)
metrics.mount(app, "inference")
_EMB = None
_RER = None
_PREFIXES = None  # (query-префикс, doc-префикс) или None
```
- Глобальные слоты под загруженные модели и префиксы эмбеддера. Заполняются на старте. `metrics.mount` навешивает `/metrics` и латентность эндпоинтов (no-op без `prometheus_client`).

```python
class EmbedReq(BaseModel):
    texts: list[str]
    is_query: bool = False

class RerankReq(BaseModel):
    query: str
    texts: list[str]
```
- Контракты запросов. `is_query` нужен, чтобы сервис сам поставил query-префикс (клиент `RemoteEmbedder` его только передаёт).

```python
def _load() -> None:
    global _EMB, _RER, _PREFIXES
    from src.embeddings.local import prefixes_for
    from src.util.model_cache import cached_cross_encoder, cached_sentence_transformer
    role = os.environ.get("INFERENCE_ROLE", "all")
    if role in ("embed", "all"):
        name = os.environ.get("EMBEDDER_MODEL", "intfloat/multilingual-e5-large")
        _EMB = cached_sentence_transformer(name)
        _PREFIXES = prefixes_for(name)
    if role in ("rerank", "all"):
        rr = os.environ.get("RERANKER_MODEL")
        _RER = cached_cross_encoder(rr) if rr else None
```
- Вызывается из `lifespan` на старте - модели грузятся один раз (через кэш), а не на каждый запрос.
- `INFERENCE_ROLE` (`embed`/`rerank`/`all`, дефолт `all`) делит сервис: один под держит только эмбеддер, другой - только реранкер, и масштабируются они врозь.
- Имя эмбеддера из env (`EMBEDDER_MODEL`), реранкер - только если задан `RERANKER_MODEL` (иначе `None`). `prefixes_for(name)` возвращает пару префиксов под семейство модели (e5 и т.п.) или `None`.

```python
@app.post("/embed")
def embed(r: EmbedReq) -> dict:
    if _EMB is None:
        raise HTTPException(503, "embedder not loaded on this pod (INFERENCE_ROLE)")
    texts = r.texts
    if _PREFIXES:
        prefix = _PREFIXES[0] if r.is_query else _PREFIXES[1]
        texts = [prefix + t for t in texts]
    return {"vectors": _EMB.encode(texts, normalize_embeddings=True).tolist()}
```
- 503, если эмбеддер не загружен на этом поде (роль `rerank`).
- Префикс ставится по `_PREFIXES` (та же логика, что в `LocalEmbedder`, но на стороне сервиса - единственное место, знающее имя модели в remote-режиме).
- `normalize_embeddings=True` согласовано с косинусной метрикой стора. `.tolist()` - numpy → JSON-список.

```python
@app.post("/rerank")
def rerank(r: RerankReq) -> dict:
    if _RER is None:
        return {"scores": [0.0] * len(r.texts)}
    return {"scores": [float(s) for s in _RER.predict([(r.query, t) for t in r.texts])]}
```
- Реранкер выключен - возвращаются нейтральные нули, пайплайн не падает. Иначе скоры считаются по парам `(query, text)`; `float(...)` - для JSON. Сортировку делает клиент (`RemoteReranker`).

```python
@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "role": os.environ.get("INFERENCE_ROLE", "all"),
            "embed": _EMB is not None, "rerank": _RER is not None}
```
- Проба здоровья для k8s; заодно отдаёт роль пода и какие модели реально загружены.

Модели вынесены в отдельный сервис из-за другого профиля ресурсов (память/GPU); их масштабируют отдельно от лёгкого оркестратора. В small этот сервис не нужен - там `LocalEmbedder`/`LocalReranker` в процессе.
