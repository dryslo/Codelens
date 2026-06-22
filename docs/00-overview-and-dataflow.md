# 00. Обзор и потоки данных

## Слои (снизу вверх)
1. **domain** - `models.py` (структура `Chunk`) и `interfaces.py` (абстрактные порты). Ни от чего не зависит, от него зависит всё.
2. **adapters** - реализации портов: парсеры (`indexing/parsers`), эмбеддер (`embeddings`), реранкер (`reranking`), вектор-стор (`stores`), реляционка (`persistence`), LLM (`llm`).
3. **orchestration** - `retrieval/hybrid.py` (`HybridRetriever` - оркестратор поиска: dense/bm25/hyde/rerank/mmr + cache-aside поверх чистого ядра), `clients/backend.py` (`LocalBackend` - чат, ответы, админка; кэш реестра/ответов и инвалидация поиска).
4. **composition root** - `factory.py` собирает всё по конфигу.
5. **entrypoints** - `index.py` (CLI), `app.py` (UI), `evaluate.py` (метрика), `services/*` (REST).

Правило зависимостей: внешние слои зависят от внутренних, не наоборот. Любая реализация подключается через интерфейс из `domain/interfaces.py`.

## Поток 1: индексация (`index.py` → стор)
```
index.py
  └─ factory.build()                      # собирает Components по config.yaml
       └─ comp.backend.index(folder, source)
            └─ pipeline.index_path(...)
                 ├─ registry.get_hash(source, file)  # CachingRegistry: кэш source+file→hash
                 │                                     # совпал → skip (БД не трогаем)
                 ├─ get_parser(".py").parse(file)   # ast → list[Chunk]
                 ├─ enrich(chunk)                   # Chunk → NL-текст
                 ├─ embedder.encode(texts, is_query=False)   # NL → векторы (passage)
                 └─ store.add(ids, embs, metas, codes)       # запись в Chroma/Qdrant
       └─ backend._invalidate_search()        # bump index-epoch → кэш поиска осиротел
```
Ключевое: при индексации текст идёт как passage, `chunk_id` формируется в формате scorer, в стор кладётся `source::chunk_id`, а в метаданные - чистый `chunk_id`. Реестр (`source`+`file`→`hash`) кэшируется (`CachingRegistry`), чтобы инкрементальный прогон не бил БД на каждый файл - см. [caching.md](persistence/caching.md).

## Поток 2: поиск (`app.py`/`backend_app` → стор → ответ)
```
backend.search(query, k=5, mode)
  └─ HybridRetriever.search(query, k, mode)
       ├─ cache.get(search:{epoch}:sha1(query,flags,k))   # cache-aside: hit → return, ядро не зовём
       └─ _search(...)  # промах → чистое ядро:
            ├─ (hyde/multiquery) expander.expand(query)   # доп. варианты запроса через LLM
            ├─ embedder.encode([q...], is_query=True)      # query-векторы
            ├─ store.query(emb, k=n_cand)                  # кандидаты из вектор-стора
            ├─ (+bm25) RRF-фьюжн каналов
            ├─ reranker.rerank(query, cands, k)            # (если включён) точная пересортировка
            └─ cache.set(key, result, ttl)                 # положить в кэш
```
Результат - список dict с `chunk_id` (формат scorer), `code`, `meta`, `score`. UI рисует карточки, `evaluate.py` берёт `chunk_id` для метрики, чат подмешивает фрагменты в промпт LLM. Кэш-обёртка живёт внутри оркестратора, ядро `_search` остаётся чистой функцией; ключ включает `index-epoch`, который сдвигается при переиндексации - см. [caching.md](persistence/caching.md).

## Поток 3: чат (history-aware RAG)
```
backend.chat(chat_id, user_msg, mode, model)
  ├─ history = history.get_messages(chat_id)        # из Postgres/SQLite
  ├─ standalone = _condense(history, user_msg)      # фоллоу-ап → самостоятельный запрос (LLM)
  ├─ chunks = retriever.search(standalone, mode)    # поток 2
  ├─ answer = llms[model].chat(system + окно истории + контекст + вопрос)
  └─ history.append(user) ; history.append(assistant, retrieved_ids)
```

## Два профиля - один код
`factory.build()` по `role`:
- `all` - всё в одном процессе (`LocalBackend`), для двух команд без серверов.
- `backend` - то же, но как REST-сервис (`backend_app.py` оборачивает `LocalBackend`).
- `frontend` - только `HttpBackend` (ходит по HTTP в backend).

Профиль `small`/`large` меняет лишь реализации за интерфейсами (Chroma↔Qdrant, SQLite↔Postgres) и масштаб, но не логику.
