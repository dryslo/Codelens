# Документация CodeLens - построчный разбор реализации

По каждому файлу проекта описано назначение строк и принятые решения.
Структура папок зеркалит `src/`: на каждый пакет исходников - своя папка в `docs/`.
Сквозные и верхнеуровневые темы (архитектура, потоки данных, деплой, точки входа, сервисы) лежат
в корне `docs/` и в `docs/services`/`docs/entrypoints`.

## Сквозное

| Документ | О чём |
|---|---|
| [architecture.md](architecture.md) | обзор архитектуры: порты, профили, топология деплоя |
| [00-overview-and-dataflow.md](00-overview-and-dataflow.md) | сквозные сценарии (индексация и поиск/чат) |
| [deploy/README.md](deploy/README.md) | развёртывание: small, docker-compose, Helm/k8s, GitOps, k3s |
| [retrieval-eval.md](retrieval-eval.md) | замер каналов ретривера, гейт Precision@5 (`evaluate.py`) |

## Карта файлов (зеркалит `src/`)

| Документ | Исходник |
|---|---|
| [domain/models.md](domain/models.md) | `src/domain/models.py` |
| [domain/interfaces.md](domain/interfaces.md) | `src/domain/interfaces.py` |
| [util/model-cache.md](util/model-cache.md) | `src/util/model_cache.py` |
| [util/observability.md](util/observability.md) | `src/util/metrics.py` + ServiceMonitor/дашборд |
| [indexing/parsers.md](indexing/parsers.md) | `src/indexing/parsers/python_ast.py`, `base.py`, `treesitter.py` |
| [indexing/pipeline.md](indexing/pipeline.md) | `src/indexing/enrich.py`, `pipeline.py` |
| [embeddings/embeddings.md](embeddings/embeddings.md) | `src/embeddings/local.py`, `remote.py` |
| [reranking/reranking.md](reranking/reranking.md) | `src/reranking/local.py`, `remote.py` |
| [stores/chroma.md](stores/chroma.md) | `src/stores/chroma.py` |
| [stores/qdrant.md](stores/qdrant.md) | `src/stores/qdrant.py` |
| [retrieval/hybrid.md](retrieval/hybrid.md) | `src/retrieval/hybrid.py`, `bm25.py`, `flags.py`, `filters.py` |
| [retrieval/expanders.md](retrieval/expanders.md) | `src/retrieval/hyde.py`, `multiquery.py`, `mmr.py` |
| [persistence/orm.md](persistence/orm.md) | `src/persistence/orm.py` |
| [persistence/db.md](persistence/db.md) | `src/persistence/db.py` |
| [persistence/schemas.md](persistence/schemas.md) | `src/persistence/schemas.py` |
| [persistence/repositories.md](persistence/repositories.md) | `history_repo.py`, `registry_repo.py` |
| [persistence/caching.md](persistence/caching.md) | `src/persistence/cache.py` + cache-aside в `hybrid.py`/`backend.py` |
| [ingest/ingest.md](ingest/ingest.md) | `src/ingest/acquire.py`, `runner.py` |
| [jobs/jobs.md](jobs/jobs.md) | `src/jobs/inprocess.py`, `redis_queue.py` |
| [llm/providers.md](llm/providers.md) | `src/llm/base.py`, `ollama.py`, `openai_compatible.py` |
| [llm/remote.md](llm/remote.md) | `src/llm/remote.py` + ветка `kind=remote` в factory |
| [auth/auth.md](auth/auth.md) | `src/auth/*` (JWT+refresh, argon2, OIDC) |
| [admin/router.md](admin/router.md) | `src/admin/router.py` |
| [clients/backend-client.md](clients/backend-client.md) | `src/clients/backend.py` |
| [factory.md](factory.md) | `src/factory.py` |
| [services/backend-app.md](services/backend-app.md) | `services/backend_app.py` |
| [services/inference-app.md](services/inference-app.md) | `services/inference_app.py` |
| [services/llm-app.md](services/llm-app.md) | `services/llm_app.py` |
| [entrypoints/index.md](entrypoints/index.md) | `index.py` |
| [entrypoints/evaluate.md](entrypoints/evaluate.md) | `evaluate.py` |
| [entrypoints/app-streamlit.md](entrypoints/app-streamlit.md) | `app.py` |

## Настройки - где что и как задаётся

Единый источник конфигурации - [`config/config.yaml`](../config/config.yaml). Значения берутся из
переменных окружения с дефолтами (`${VAR:-default}`), поэтому одна и та же сборка работает в любом
профиле: меняются только переменные. Профиль (`PROFILE`) и роль процесса (`ROLE`) задают остальное.

**Где переменные задаются - по версии:**

| Версия | Где править настройки | Где секреты |
|---|---|---|
| **small / dev** (`make run`) | `config/config.yaml` (дефолты) либо переменные окружения процесса | `.env` в корне / переменные окружения (`GROQ_API_KEY` и т.п.) |
| **large / docker-compose** | блок `environment` сервисов в [`deploy/docker-compose.yml`](../deploy/docker-compose.yml) | `.env` в корне (`env_file`), переопределяет дефолты config.yaml |
| **large / k8s (Helm)** | overlay `deploy/helm/codelens/values-*.yaml` (поле `config:` + тумблеры компонентов) | Secret `codelens-secrets` через sealed-secrets (`secrets.create: false`), см. [deploy/gitops/README.md](../deploy/gitops/README.md) |

**Группы настроек** (полный список - в [`config/config.yaml`](../config/config.yaml), подробный разбор
проводки - в [factory.md](factory.md)):

| Группа | Ключевые переменные | small (дефолт) | large |
|---|---|---|---|
| Профиль/роль | `PROFILE`, `ROLE`, `BACKEND_URL` | `small`, `all` | `large`, `frontend`/`backend` по подам |
| Эмбеддер | `EMBEDDER_KIND`, `EMBEDDER_MODEL`, `EMBEDDER_DIM`, `EMBEDDER_BATCH` | `local`, e5-large, 1024 | `remote` (под embedder) |
| Реранкер | `RERANKER_ENABLED`, `RERANKER_KIND`, `RERANKER_MODEL` | `false` (без cross-encoder) | опц. `remote`, профиль `reranker` |
| Каналы поиска | `FLAG_BM25`, `FLAG_MULTIQUERY`, `FLAG_HYDE`, `FLAG_RERANK`, `FLAG_MMR` | `ui`/`off` (нейтральны на корпусе) | можно `bm25=fast`, `hyde/mq=thinking` |
| Адреса моделей | `INFERENCE_URL`, `EMBEDDER_URL`, `RERANKER_URL`, `LLM_URL` | один под (`all`) | отдельные поды по URL |
| Векторный стор | `VECTOR_KIND`, `CHROMA_PATH`, `QDRANT_URL` | `chroma` (`.chroma`) | `qdrant` (кластер) |
| БД / кэш / очередь | `DATABASE_DSN`, `REDIS_URL`, `CACHE_TTL`, `JOBS_KIND` | SQLite, NullCache, `inprocess` | Postgres, Redis, `redis` (RQ) |
| LLM | `LLM_KIND`, `LLM_URL`, `LLM_FAST`, ключи провайдеров | `local` (в процессе) | `remote` (llm-gateway) |
| Авторизация | `AUTH_ENABLED`, `JWT_SECRET`, `ACCESS_TTL`, `REFRESH_TTL`, `AUTH_COOKIE_SECURE`, `ADMIN_LOGIN`/`ADMIN_PASSWORD` | `enabled`, дев-секрет, cookie insecure | прод-секрет, `cookie_secure=true` (HTTPS) |
| Ingest | `MAX_UPLOAD_MB` | 100 | как нужно |

Подробности по слоям: профили и проводка - [architecture.md](architecture.md) и [factory.md](factory.md);
кэш/Redis - [persistence/caching.md](persistence/caching.md); авторизация - [auth/auth.md](auth/auth.md);
размещение по подам и overlay'и Helm - [deploy/README.md](deploy/README.md).

## Порядок чтения
Отправная точка - [00-overview-and-dataflow.md](00-overview-and-dataflow.md): там показано, как файлы
соединяются в два пути - индексация и поиск/чат. Далее - по слоям снизу вверх: domain → indexing /
embeddings / stores → retrieval → persistence → ingest / jobs → llm → auth / admin → clients →
factory → services → entrypoints. Развёртывание - [deploy/README.md](deploy/README.md).
