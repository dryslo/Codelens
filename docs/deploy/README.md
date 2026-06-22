# Развёртывание CodeLens

Обзор деплоя и индекс по папке `deploy/`. Структура этого раздела зеркалит `deploy/`: на каждый
манифест/файл - свой разбор «что/зачем/как устроено». Архитектурный контекст (порты, профили,
топология) - в [../architecture.md](../architecture.md); переменные окружения и где их задавать -
раздел «Настройки» в [../README.md](../README.md).

## Карта файлов (зеркалит `deploy/`)

| Документ | Исходники |
|---|---|
| [dockerfiles.md](dockerfiles.md) | `deploy/Dockerfile.{backend,frontend,inference,llm,worker}` |
| [docker-compose.md](docker-compose.md) | `deploy/docker-compose.yml` |
| [nginx.md](nginx.md) | `deploy/nginx/nginx.conf` |
| [observability-stack.md](observability-stack.md) | `deploy/prometheus/prometheus.yml`, `deploy/grafana/provisioning/*` |
| [helm/chart.md](helm/chart.md) | `Chart.yaml`, `_helpers.tpl`, `values.yaml` + overlay'и |
| [helm/templates/workloads.md](helm/templates/workloads.md) | `templates/{frontend,backend,worker,embedder,reranker,llm}.yaml` |
| [helm/templates/data.md](helm/templates/data.md) | `templates/{qdrant,postgres,redis}.yaml` |
| [helm/templates/platform.md](helm/templates/platform.md) | `templates/{configmap,secret,ingress,migrate-job,index-job,servicemonitor,grafana-dashboard}.yaml` |
| [gitops/gitops.md](gitops/gitops.md) | `deploy/gitops/{project,application-*}.yaml` |
| [gitops/sealed-secrets.md](gitops/sealed-secrets.md) | `deploy/gitops/sealed/*` |

Операционные рантбуки лежат рядом с кодом (это пошаговые инструкции, не разбор реализации):
[../../deploy/minikube.md](../../deploy/minikube.md) - локальная валидация чарта,
[../../deploy/k3s-setup.md](../../deploy/k3s-setup.md) - прод на VPS,
[../../deploy/gitops/README.md](../../deploy/gitops/README.md) - bootstrap Argo CD.

## Один код, размещение задаётся конфигом

Один кодовый репозиторий и один набор образов обслуживают оба профиля. Где живёт компонент - в том же
процессе или в отдельном поде - определяет [../../config/config.yaml](../../config/config.yaml) через
переменные окружения, а не другой код. Взаимодействие идёт через порты из `src/domain/interfaces.py`,
выбор реализации делает `factory.py` по конфигу (таблица портов - в [../architecture.md](../architecture.md)).

- `PROFILE` (`small` | `large`) - целевой масштаб. На код влияет не сам флаг, а связанные `kind`-переключатели.
- `ROLE` (`all` | `frontend` | `backend`) - что делает процесс: `all` - один процесс (dev), `frontend` -
  Streamlit-клиент, `backend` - оркестратор FastAPI (тот же образ под worker).

| Переключатель | small (`local`/in-process) | large (`remote`/внешний) | env |
|---|---|---|---|
| Эмбеддер | `local` - sentence-transformers в процессе | `remote` - inference-сервис по HTTP | `EMBEDDER_KIND`, `EMBEDDER_URL` |
| Реранкер | `local` (по умолчанию выключен) | `remote` - inference-сервис | `RERANKER_KIND`, `RERANKER_URL`, `RERANKER_ENABLED` |
| LLM | `local` - провайдеры в процессе | `remote` - llm-gateway по HTTP | `LLM_KIND`, `LLM_URL` |
| Векторный стор | `chroma` (файл `.chroma`) | `qdrant` (кластер) | `VECTOR_KIND`, `QDRANT_URL` |
| Реляционка | SQLite (`codelens.db`) | Postgres (CNPG) | `DATABASE_DSN` |
| Кэш + сессии | in-process (или `NullCache`, если `REDIS_URL` пуст) | Redis | `REDIS_URL` |
| Очередь ingest | `inprocess` | `redis` (RQ + worker-под) | `JOBS_KIND` |

## Способы запуска

- **small (без Docker)** - один процесс, Chroma + SQLite, модели и LLM в процессе:
  `make install` → `make index` → `make run` (UI на `http://localhost:8501`). Индексация и поиск
  работают без ключей; LLM-функции (чат, HyDE, multi-query) требуют `GROQ_API_KEY`.
- **large через docker-compose** - тот же набор сервисов, что в k8s, в одной сети. Запуск:
  `make up` (без панелей) либо `make up-panels` (+ nginx/grafana/prometheus на `http://localhost`).
  Разбор графа сервисов, профилей `reranker`/`panels`, healthcheck и проводки env - [docker-compose.md](docker-compose.md);
  образы - [dockerfiles.md](dockerfiles.md); reverse-proxy и панели - [nginx.md](nginx.md),
  [observability-stack.md](observability-stack.md).
- **large на k8s (Helm)** - чарт `deploy/helm/codelens`. Устройство чарта, `values.yaml` и overlay'и -
  [helm/chart.md](helm/chart.md); манифесты - [workloads](helm/templates/workloads.md) /
  [data](helm/templates/data.md) / [platform](helm/templates/platform.md). Статическая проверка -
  `make mk-validate`; локальный прогон на настоящем k8s - [../../deploy/minikube.md](../../deploy/minikube.md).
- **GitOps (Argo CD)** - git как единственный источник правды: push → CI → bump `image.tag` в overlay →
  Argo приводит кластер к git. Argo-объекты и sealed-secrets - [gitops/gitops.md](gitops/gitops.md),
  [gitops/sealed-secrets.md](gitops/sealed-secrets.md); bootstrap - [../../deploy/gitops/README.md](../../deploy/gitops/README.md).
- **прод на k3s (VPS, HA)** - самоуправляемые узлы, Qdrant-кластер и CNPG-Postgres реплицируют на
  уровне приложения, выделенные узлы под embedder и llm. Топология и установка - [../../deploy/k3s-setup.md](../../deploy/k3s-setup.md).

## Make-цели для деплоя

| Цель | Что делает |
|---|---|
| `make up` / `make up-panels` | docker compose up (large; `-panels` добавляет nginx/grafana/prometheus) |
| `make build S="..."` | собрать образы (все или указанные) |
| `make down` | остановить compose |
| `make inference` | локально поднять inference-сервис (uvicorn, порт 8000) |
| `make migrate DSN=...` | применить alembic-миграции |
| `make mk-start` | minikube: узлы + metrics-server + оператор CNPG |
| `make mk-images` | собрать образы и загрузить в minikube как `codelens/<svc>:local` |
| `make mk-validate` | статика чарта: helm lint + template + kubeconform |
| `make mk-up` / `make mk-infra` | helm upgrade --install (overlay `values-local`; `mk-infra` - только инфра без app-образов) |
| `make mk-status` | проверки: узлы, поды, кластер Qdrant, статус CNPG |
| `make mk-down` | снести релиз (PVC остаются) |

## Где что настраивается

- Переменные окружения и профили - [../../config/config.yaml](../../config/config.yaml) (все `${VAR}` с дефолтами).
- Раздел «Настройки» и «Быстрый старт» - [../README.md](../README.md), [../../README.md](../../README.md).
- Тумблеры чарта (`values.yaml` + overlay'и) - [helm/chart.md](helm/chart.md).
- Секреты large - sealed-secrets, [gitops/sealed-secrets.md](gitops/sealed-secrets.md).
