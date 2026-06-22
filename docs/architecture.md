# Архитектура

Верхнеуровневый обзор системы. Код-уровень и потоки данных - в [00-overview-and-dataflow.md](00-overview-and-dataflow.md).

## Принцип

Один код и одни образы на оба профиля. Размещение компонентов задаётся конфигом, а не другим кодом.
Всё взаимодействие идёт через порты из `src/domain/interfaces.py`:

| Порт | Локальная реализация (small) | Удалённая (large) |
|---|---|---|
| `Embedder` | `embeddings/local.py` (sentence-transformers) | `embeddings/remote.py` (inference-сервис) |
| `Reranker` | `reranking/local.py` | `reranking/remote.py` |
| `LLMProvider` | `llm/openai_compatible.py`, `llm/ollama.py` | `llm/remote.py` (llm-gateway) |
| `VectorStore` | `stores/chroma.py` | `stores/qdrant.py` |
| `SessionStore` | `persistence/cache.py` (InProcess) | `persistence/cache.py` (Redis) |
| `JobQueue` | `jobs/inprocess.py` | `jobs/redis_queue.py` (RQ + worker) |

Смена local <-> remote - это выбор реализации в `factory.py` по `config.yaml`. Оркестратор
(`retrieval/hybrid.py`) и backend (`clients/backend.py`) работают через порты и о размещении не знают.

## Профили

- **small / all**: один процесс. Ретривер и модель-сервисы свёрнуты в backend, стор - Chroma,
  реляционка - SQLite, кэш и очередь - in-process. Запуск: `make run` или `docker compose`.
- **large**: каждый компонент - отдельный под, масштабируется репликами. Стор - Qdrant (кластер),
  реляционка - Postgres (CNPG), кэш и очередь - Redis. Запуск: Helm-чарт в k8s/k3s.

Различие профилей - только values Helm-чарта и мощности, не код.

## Компоненты large

```
браузер -> frontend (Streamlit) -> backend (FastAPI, оркестратор + чат)
                                      |
        search -> HybridRetriever ----+---- chat -> llm-gateway (ключи провайдеров только тут)
                    |                 |
   embedder-сервис  | reranker-сервис | qdrant (эмбеддинги)   postgres (истории чатов + index-реестр)
   (INFERENCE_ROLE) |                 | redis (кэш + сессии + очередь RQ)
                                        worker (фоновый ingest из очереди)
```

- **frontend** - тонкий Streamlit-клиент, ходит в backend по REST.
- **backend** - сборщик (`factory.build()`), эндпоинты поиска/чата/ответа/админки, оркестрация.
- **inference** - один образ, роль задаёт `INFERENCE_ROLE` (`embed`/`rerank`/`all`).
- **llm-gateway** - общий шлюз к провайдерам LLM, изолирует ключи.
- **worker** - исполняет ingest-задачи из RQ (тот же пайплайн индексации, без моделей).
- **qdrant / postgres / redis** - стораджи: векторы, реляционные данные, кэш и очередь.

## Сквозные подсистемы

- **Индексация и поиск** (потоки данных): [00-overview-and-dataflow.md](00-overview-and-dataflow.md).
- **Кэширование и сессии** (cache-aside, epoch-инвалидация): [caching.md](persistence/caching.md).
- **Авторизация** (JWT + refresh, argon2id, OIDC, роли, админка): [auth.md](auth/auth.md).
- **Стриминг ответа LLM** (SSE через все слои): [services/backend-app.md](services/backend-app.md).
- **Наблюдаемость** (Prometheus + Grafana): [observability.md](util/observability.md).
- **Качество поиска** (метрики по каналам ретривера): [retrieval-eval.md](retrieval-eval.md).

## Деплой

- **Образы**: GHCR, тег - git-SHA (CI собирает и пушит, см. `.github/workflows/ci.yml`).
- **GitOps**: Argo CD синхронизирует кластер с git (`deploy/gitops/`); staging едет за веткой dev,
  prod - за main. Секреты - через sealed-secrets (в git только зашифрованный SealedSecret).
- **Топология** (k3s на VPS): размещение по узлам через nodeSelector/taint (embedder на heavy-узел,
  llm на EU-узел, qdrant/postgres на data-узлы), см. `deploy/k3s-setup.md`.
