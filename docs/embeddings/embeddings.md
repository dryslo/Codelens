# embeddings - local.py и remote.py

Реализации порта `Embedder`. Превращают тексты в векторы. Две реализации: в процессе (`small`) и по HTTP к inference-сервису (`large`).

## local.py - `LocalEmbedder`
```python
PREFIXES = {
    "e5":    ("query: ",        "passage: "),
    "frida": ("search_query: ", "search_document: "),
}

class LocalEmbedder(Embedder):
    def __init__(self, model="intfloat/multilingual-e5-large", batch_size=32):
        self.model = cached_sentence_transformer(model)
        self._prefixes = prefixes_for(model)
        self.batch_size = batch_size
```
- Модель грузится через кэш (`cached_sentence_transformer`) - один раз скачивается, потом с диска.
- `self._prefixes` - пара префиксов query/passage для модели или `None`. e5 и FRIDA обучены с инструкциями-префиксами; для прочих моделей префиксы не нужны и даже вредны. Семейство определяется подстрокой в имени модели (`prefixes_for`), чтобы класс работал и с другими моделями без правок.

```python
    def _prep(self, texts, is_query):
        if not self._prefixes:
            return list(texts)
        prefix = self._prefixes[0] if is_query else self._prefixes[1]
        return [prefix + t for t in texts]
```
- Для e5/FRIDA: запросы получают query-префикс, документы - doc-префикс. Без правильных префиксов качество заметно падает (модель ожидает их по протоколу обучения). `is_query=True` приходит из поиска, `False` - из индексации.
- Для прочих моделей: тексты возвращаются как есть (`list(...)` - на случай генератора).

```python
    def encode(self, texts, is_query=False):
        return self.model.encode(self._prep(texts, is_query),
                                 normalize_embeddings=True, show_progress_bar=False,
                                 batch_size=self.batch_size)
```
- `normalize_embeddings=True` - L2-нормировка: тогда косинусная близость = скалярное произведение, и метрика стора (cosine) корректна. Стор тоже настроен на cosine - согласовано.
- `show_progress_bar=False` - без прогресс-бара: прогресс ведётся по чанкам в пайплайне, а бар "Batches" шумит в stderr (особенно при фоновом ingest).
- Возвращается numpy-массив `(N, dim)`; пайплайн/ретривер берут строки (`embs[0]` и подобное).

## remote.py - `RemoteEmbedder`
```python
class RemoteEmbedder(Embedder):
    def __init__(self, url):
        self.url = url.rstrip("/")
    def encode(self, texts, is_query=False):
        import time, requests
        payload = {"texts": list(texts), "is_query": is_query}
        last = None
        for attempt in range(3):
            try:
                r = requests.post(f"{self.url}/embed", json=payload, timeout=60)
                r.raise_for_status()
                return np.array(r.json()["vectors"])
            except requests.RequestException as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise last
```
- Тот же интерфейс `encode(texts, is_query)`, но вместо локальной модели - POST на `/embed` inference-сервиса. Префиксы e5 применяет сервис (он знает имя модели) - поэтому сюда передаётся только `is_query`, а не готовый префикс.
- Несколько попыток с backoff: эмбеддер мог перезапускаться или догружать модель (connection refused / 5xx). Долгую первую загрузку покрывает healthcheck+depends_on в compose.
- `rstrip("/")` - нормализация URL, чтобы не получить двойной слэш.
- `np.array(...)` - приведение JSON-списка обратно к numpy, чтобы вызывающий код не различал local/remote.
- Ленивый `import requests` - чтобы local-профиль не тянул лишнего на импорте.

Где выбирается local vs remote: в `factory._build_embedder` по `embedder.kind` (`local`/`remote`). Это размещение, не архитектура - интерфейс один.
