# stores/qdrant.py - `QdrantStore` (профиль large)

Реализация `VectorStore` на Qdrant: сервер/кластер с шардами и репликами.

```python
def _id(cid):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, cid))
```
- Qdrant требует, чтобы id точки был UUID или int. Ключ здесь - строка `source::chunk_id`. `uuid5` детерминированно превращает строку в UUID (одинаковый вход → одинаковый UUID), поэтому повторная индексация того же чанка перезапишет ту же точку (upsert), а не создаст дубль.

```python
def __init__(self, url, name="code", dim=1024, shards=2, replicas=2):
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams
    self.q = QdrantClient(url=url)
    self.name = name
    if not self.q.collection_exists(name):
        self.q.create_collection(
            name,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            shard_number=shards, replication_factor=replicas,
        )
```
- Клиент к серверу по `url`.
- `dim` задаётся явно (в отличие от Chroma) - поэтому `embedder.dim` в конфиге обязан совпадать с моделью (e5-large = 1024).
- `Distance.COSINE` - согласовано с нормированными векторами.
- `shard_number`/`replication_factor` - это и есть «кластеризация»: данные бьются на шарды и реплицируются. В `small` это не используется (там Chroma); значения берутся из конфига `vector.shards/replicas`.
- `if not collection_exists` - идемпотентное создание.

```python
def add(self, ids, embeddings, metadatas, documents):
    from qdrant_client.models import PointStruct
    pts = [PointStruct(id=_id(cid), vector=e.tolist(),
                       payload={**m, "code": d})
           for cid, e, m, d in zip(ids, embeddings, metadatas, documents)]
    self.q.upsert(self.name, pts)
```
- Точки собираются так: `id` = UUID от `source::chunk_id`, `vector` = список float, `payload` = метаданные + код.
- `{**m, "code": d}` - раскрытие метаданных (там уже есть `chunk_id` в формате scorer) и добавление кода. Код не перетирает `chunk_id` (в отличие от ранней версии, где id передавался отдельно) - `chunk_id` сохраняется из `m`.
- `upsert` - вставка-или-обновление по id (повторная индексация безопасна).

```python
def query(self, embedding, k=20):
    res = self.q.query_points(self.name, query=embedding.tolist(), limit=k,
                              with_payload=True).points
    return [{"chunk_id": p.payload.get("chunk_id"), "code": p.payload.get("code"),
             "meta": p.payload, "distance": 1 - p.score} for p in res]
```
- `query_points(..., with_payload=True)` - поиск ближайших с возвратом payload.
- `p.score` у Qdrant для COSINE - это похожесть (больше = ближе), а унифицированный формат отдаёт `distance` (меньше = ближе), поэтому `distance = 1 - score`. Это лишь для единообразия dict; ретривер с реранкером на distance не опирается.
- `chunk_id`/`code` берутся из payload - формат scorer сохранён.

```python
def delete_where(self, **conds):
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    self.q.delete(self.name, points_selector=Filter(
        must=[FieldCondition(key=k, match=MatchValue(value=v)) for k, v in conds.items()]))
```
- Qdrant удаляет по фильтру payload: `must` = все условия должны совпасть (`source` И `file`). На каждый kwarg строится `FieldCondition`. Тот же вызов `delete_where(source=.., file=..)`, что у Chroma - единый контракт.

```python
def count(self): return self.q.count(self.name).count
def list_sources(self): return []
```
- `count` - число точек.
- `list_sources` пуст намеренно: на больших базах перебор payload дорог; источники в large берутся из реляционного реестра (`IndexRegistry`), а не из стора.

Где выбирается: `factory._build_store` по `vector.kind` (`chroma`/`qdrant`).
