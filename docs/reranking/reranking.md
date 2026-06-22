# reranking - local.py и remote.py

Реализации порта `Reranker`. Cross-encoder пересортировывает кандидатов от вектор-поиска точнее, чем bi-encoder. Опционален (включается конфигом).

## local.py - `LocalReranker`
```python
class LocalReranker(Reranker):
    def __init__(self, model="BAAI/bge-reranker-v2-m3"):
        self.model = cached_cross_encoder(model)
```
- bge-reranker-v2-m3 - мультиязычный cross-encoder, хорошо ложится на e5-large (см. docs моделей). Через кэш.

```python
    def rerank(self, query, cands, k=5):
        if not cands:
            return []
        scores = self.model.predict([(query, c["code"]) for c in cands])
```
- Защита от пустого входа.
- `CrossEncoder.predict` принимает пары `(запрос, текст_кандидата)` и выдаёт оценку релевантности на каждую пару. Пары, а не отдельные эмбеддинги: cross-encoder видит запрос и кандидата вместе (полное внимание между ними) → точнее bi-encoder, но дороже (поэтому только на топ-N кандидатов, не на всю базу). В пару кладётся `c["code"]` (исходный код фрагмента).

```python
        ranked = sorted(zip(cands, scores), key=lambda x: x[1], reverse=True)
        out = []
        for c, s in ranked[:k]:
            c = dict(c)
            c["score"] = float(s)
            out.append(c)
        return out
```
- `zip(cands, scores)` спаривает кандидата с его оценкой; сортировка по оценке по убыванию; берётся топ-`k`.
- `c = dict(c)` - копия, чтобы не мутировать исходный dict; записывается нормализованный `score` (float - score от модели может быть numpy-типом, приводится для JSON/UI).

## remote.py - `RemoteReranker`
```python
    def rerank(self, query, cands, k=5):
        import requests
        texts = [c["code"] for c in cands]
        scores = requests.post(f"{self.url}/rerank",
                               json={"query": query, "texts": texts}, timeout=60).json()["scores"]
        ranked = sorted(zip(cands, scores), key=lambda x: x[1], reverse=True)
        ...
```
- То же, но скоринг считает inference-сервис: отправляется запрос и список текстов, в ответ приходит список оценок, дальше сортировка идентична локальной. Логика пересортировки на клиенте - единообразно с local.

Где включается: `factory._build_reranker` возвращает `None`, если `reranker.enabled != true`. Тогда `HybridRetriever` пропускает реранк (degradable). Включается при замере прироста Precision - модель тяжёлая.
