# stores/chroma.py - `ChromaStore` (профиль small)

Реализация `VectorStore` на ChromaDB: embedded, без сервера - файл на диске. Подходит для двух команд и демо.

```python
def __init__(self, path=".chroma", name="code"):
    import chromadb
    self.col = chromadb.PersistentClient(path=path).get_or_create_collection(
        name, metadata={"hnsw:space": "cosine"})
```
- `PersistentClient(path=...)` - Chroma пишет на диск в `.chroma/` (переживает перезапуск; без сервера).
- `get_or_create_collection(name, ...)` - коллекция «code» создаётся при первом запуске, дальше переиспользуется (идемпотентно).
- `metadata={"hnsw:space": "cosine"}` - индекс HNSW считает косинусное расстояние. Согласовано с нормированными эмбеддингами (`normalize_embeddings=True`).
- Ленивый `import chromadb` - чтобы не тянуть зависимость, когда выбран Qdrant.

```python
def add(self, ids, embeddings, metadatas, documents):
    self.col.add(ids=ids, embeddings=[e.tolist() for e in embeddings],
                 metadatas=metadatas, documents=documents)
```
- `ids` - физические ключи (`source::chunk_id`).
- `embeddings` приходит numpy-массивом; `[e.tolist() for e in embeddings]` превращает каждую строку-вектор в обычный список (Chroma принимает списки).
- `documents` - исходный код (Chroma хранит и его), `metadatas` - плоский dict с `chunk_id` и пр.

```python
def query(self, embedding, k=20):
    r = self.col.query(query_embeddings=[embedding.tolist()], n_results=k)
    if not r["ids"] or not r["ids"][0]:
        return []
```
- `query_embeddings=[вектор]` - Chroma поддерживает батч запросов, здесь он один → оборачивается в список.
- Ответ Chroma - словарь списков, где первый индекс = номер запроса (здесь 0). Защита: если результатов нет (`r["ids"][0]` пуст) → пустой список, чтобы не упасть ниже.

```python
    out = []
    for i in range(len(r["ids"][0])):
        meta = r["metadatas"][0][i]
        out.append({"chunk_id": meta.get("chunk_id", r["ids"][0][i]),
                    "code": r["documents"][0][i], "meta": meta,
                    "distance": r["distances"][0][i]})
    return out
```
- Обход результатов нулевого (единственного) запроса.
- `chunk_id` берётся из метаданных (формат scorer), а не из `r["ids"]` (там физический `source::chunk_id`). `meta.get(..., fallback)` - на случай отсутствия.
- Возвращается унифицированный dict `{chunk_id, code, meta, distance}` - такой же, как у Qdrant, чтобы ретривер не различал сторы.

```python
def delete_where(self, **conds):
    flt = [{k: v} for k, v in conds.items()]
    try:
        self.col.delete(where=flt[0] if len(flt) == 1 else {"$and": flt})
    except Exception:
        pass
```
- Chroma-фильтр: для одного условия `{ "source": "x" }`, для нескольких - `{"$and": [..]}` (синтаксис Chroma). `delete_where(source=.., file=..)` → удаляет чанки файла.
- `try/except` - удаление по несуществующему условию не должно ронять индексацию (например первый проход, когда удалять нечего).

```python
def count(self): return self.col.count()
def list_sources(self):
    metas = self.col.get(include=["metadatas"]).get("metadatas") or []
    return sorted({m.get("source", "") for m in metas if m})
```
- `count` - число чанков (для админки/метрик).
- `list_sources` - сбор уникальных `source` из всех метаданных. Дорого на большой базе (читает все метаданные) - для small приемлемо; в Qdrant этот метод намеренно пустой (источники ведутся в реляционке).

Ограничение: размерность коллекции Chroma выводится из первых добавленных векторов. При смене эмбеддера на другую размерность нужно удалить `.chroma/` и переиндексировать.
