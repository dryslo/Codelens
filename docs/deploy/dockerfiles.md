# deploy/Dockerfile.* - образы сервисов

Разбор пяти Dockerfile из [../../deploy/](../../deploy/): по образу на каждую роль профиля large.
Из них собираются сервисы в [./docker-compose.yml](./docker-compose.md); обзор деплоя - в
[./README.md](./README.md). Группы зависимостей (`backend`/`frontend`/`inference`/`llm`/`worker` и
вложенные `scale`/`parsers`/`local`) объявлены в [../../pyproject.toml](../../pyproject.toml).

## Таблица образов

| Образ | extras (`pip install`) | Что запускает | Особенности |
|---|---|---|---|
| [Dockerfile.backend](../../deploy/Dockerfile.backend) | `.[backend,scale]` | `uvicorn services.backend_app:app` (:8080) | `ROLE=backend` зашит; без torch - эмбеддер/реранкер удалённые |
| [Dockerfile.frontend](../../deploy/Dockerfile.frontend) | `.[frontend]` | `streamlit run app.py` (:8501) | `ROLE=frontend` зашит; без torch/БД |
| [Dockerfile.inference](../../deploy/Dockerfile.inference) | `.[inference]` | `uvicorn services.inference_app:app` (:8000) | модели НЕ в образе - качаются на старте в `MODEL_CACHE`; нужен том |
| [Dockerfile.llm](../../deploy/Dockerfile.llm) | `.[llm]` | `uvicorn services.llm_app:app` (:8001) | тонкий шлюз, без ML-весов |
| [Dockerfile.worker](../../deploy/Dockerfile.worker) | `.[worker]` | `python -m services.worker_app` | `ROLE=backend`; тот же пайплайн без моделей |

## Общий паттерн

Все пять образов от `python:3.12-slim` и `WORKDIR /app`. Зависимости и код ставятся двумя слоями,
чтобы правки кода не пересобирали тяжёлый слой зависимостей:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
# 1) Зависимости отдельным кэшируемым слоем (пакет ещё пустой - ставятся только deps)
COPY pyproject.toml ./
RUN pip install --no-cache-dir ".[<extras>]"
# 2) Код и установка самого пакета без повторного резолва зависимостей
COPY . .
RUN pip install --no-cache-dir --no-deps .
```

- Шаг 1 копирует только `pyproject.toml` и ставит зависимости выбранных extras. Слой кэшируется и
  переиспользуется, пока `pyproject.toml` не изменился - правка исходников его не инвалидирует (а в
  `backend`/`worker` extras тянут torch транзитивно через парсеры/scale, так что слой дорогой).
- Шаг 2 копирует код и ставит сам пакет `codelens` с `--no-deps`: зависимости уже стоят с шага 1,
  повторный резолв не нужен, ставится только сам пакет (точки входа, метаданные).
- Различаются образы по трём вещам: какие extras на шаге 1, что зашито в `ROLE`/`MODEL_CACHE`, и
  какая команда `CMD`. Какие extras что тянут - в [../../pyproject.toml](../../pyproject.toml):
  - `backend` = FastAPI + ретривер (bm25/mmr) + auth + remote-клиенты + парсеры; БЕЗ
    sentence-transformers/torch и chromadb (эмбеддер/реранкер удалённые, стор - Qdrant);
  - `scale` = драйверы large (qdrant-client, psycopg, redis, prometheus-client);
  - `frontend` = Streamlit + requests, без torch/FastAPI/БД;
  - `inference` = sentence-transformers (то есть torch) + FastAPI;
  - `llm` = FastAPI + openai-SDK + requests, без ML;
  - `worker` = `codelens[backend,scale]` - тот же набор, что у backend.

## Dockerfile.backend

```dockerfile
RUN pip install --no-cache-dir ".[backend,scale]"
...
ENV ROLE=backend
EXPOSE 8080
CMD ["uvicorn", "services.backend_app:app", "--host", "0.0.0.0", "--port", "8080"]
```

- Оркестратор. `.[backend,scale]` = ядро оркестратора + драйверы large (Qdrant/Postgres/Redis). torch
  в образ НЕ тянется: эмбеддер и реранкер вынесены в отдельный inference-под, backend ходит к ним по
  HTTP - образ лёгкий относительно inference.
- `ROLE=backend` зашит в образ (не передаётся снаружи) - этот образ всегда оркестратор. В compose
  `environment` дополняет его `kind`-переключателями large.
- Копирует весь контекст (`COPY . .`), потому что backend использует и `services`, и `src`, и
  парсеры; пакет ставится `--no-deps`. Разбор сервиса - [../services/backend-app.md](../services/backend-app.md).

## Dockerfile.frontend

```dockerfile
RUN pip install --no-cache-dir ".[frontend]"
...
ENV ROLE=frontend
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
```

- Самый тонкий образ. `.[frontend]` = Streamlit + cookies-controller + requests, без torch, FastAPI и
  драйверов БД - клиент ходит в backend по HTTP.
- `ROLE=frontend` зашит. Запускает `app.py` как Streamlit-приложение.

## Dockerfile.inference

```dockerfile
RUN pip install --no-cache-dir ".[inference]"
COPY services ./services
COPY src ./src
ENV MODEL_CACHE=/app/cache/models
EXPOSE 8000
CMD ["uvicorn", "services.inference_app:app", "--host", "0.0.0.0", "--port", "8000"]
```

- Образ моделей (embedder/reranker). `.[inference]` = sentence-transformers (с torch) + FastAPI. Это
  единственный тяжёлый образ по ML-весам кода (сами модели - отдельно).
- Отличие 1: модели НЕ зашиты в образ. `ENV MODEL_CACHE=/app/cache/models` указывает, куда сервис
  скачивает веса на старте (в `lifespan`). Образ остаётся одинаковым для любой модели - имя задаёт env
  (`EMBEDDER_MODEL`/`RERANKER_MODEL`), а роль - `INFERENCE_ROLE` (`embed`/`rerank`/`all`).
- Отличие 2: под `MODEL_CACHE` нужен примонтированный том (в compose - `model_cache`), иначе веса
  (e5-large ~2 ГБ) качаются заново при каждом пересоздании пода. Один образ обслуживает и embedder, и
  reranker - различает их `INFERENCE_ROLE`. Разбор сервиса - [../services/inference-app.md](../services/inference-app.md).
- Копирует только `services` и `src` (без data/config/tests) - сервису не нужен остальной контекст,
  и пакет здесь НЕ ставится отдельным `--no-deps` шагом (запуск идёт по `services.inference_app`).
- `ROLE` не зашит - этот образ не frontend/backend, его роль задаёт `INFERENCE_ROLE`.

## Dockerfile.llm

```dockerfile
RUN pip install --no-cache-dir ".[llm]"
COPY services ./services
COPY src ./src
COPY config ./config
EXPOSE 8001
CMD ["uvicorn", "services.llm_app:app", "--host", "0.0.0.0", "--port", "8001"]
```

- Тонкий шлюз к провайдерам LLM. `.[llm]` = FastAPI + openai-SDK + requests + prometheus-client, без
  ML-весов и без sentence-transformers - вся «тяжесть» здесь сетевая (вызовы провайдеров), не
  вычислительная.
- Помимо `services`/`src` копирует `config` - gateway читает список провайдеров и `fast`-провайдера
  из [../../config/config.yaml](../../config/config.yaml). Ключи провайдеров приходят из `.env` в
  compose и живут только в этом сервисе. Разбор - [../services/llm-app.md](../services/llm-app.md).

## Dockerfile.worker

```dockerfile
RUN pip install --no-cache-dir ".[worker]"
COPY . .
RUN pip install --no-cache-dir --no-deps .
ENV ROLE=backend
CMD ["python", "-m", "services.worker_app"]
```

- RQ-воркер ingest. `.[worker]` = `codelens[backend,scale]` - ровно тот же набор зависимостей, что у
  backend (тот же пайплайн индексации, те же парсеры и драйверы large), БЕЗ моделей: эмбеддинг чанков
  идёт через удалённый embedder-под.
- `ROLE=backend` зашит - воркер использует тот же composition root, что и оркестратор, поэтому видит
  ту же роль; различается лишь точка входа.
- Отличие от backend - только `CMD`: вместо uvicorn запускает `python -m services.worker_app`, который
  тянет задачи RQ из Redis и исполняет `index_path`. Порт наружу не открывает (метрики на
  `METRICS_PORT` задаёт compose). Так загрузка большого ZIP/GitHub не блокирует backend.

## См. также

- [./docker-compose.md](./docker-compose.md) - как эти образы собираются в сервисы и связываются env/томами.
- [./README.md](./README.md) - обзор деплоя: профили small/large и способы запуска.
- [../services/backend-app.md](../services/backend-app.md) · [../services/inference-app.md](../services/inference-app.md) · [../services/llm-app.md](../services/llm-app.md) - сервисы за образами.
