# deploy/docker-compose.yml - стек профиля large

Разбор [../../deploy/docker-compose.yml](../../deploy/docker-compose.yml) - профиль small/large в одном
docker-compose: по одному экземпляру каждого сервиса в общей сети, без k8s. Образы собираются из
[Dockerfile.*](./dockerfiles.md); как разворачивается стек в целом и какие ещё способы запуска
есть - в [./README.md](./README.md).

Ingest идёт не в backend, а через очередь RQ (`JOBS_KIND=redis`): backend кладёт задачу в Redis,
исполняет отдельный `worker`. Эмбеддер, реранкер и LLM вынесены в отдельные поды (`*_KIND=remote`),
стор - Qdrant, реляционка - Postgres, кэш/очередь - Redis.

## Сервисы

| Сервис | Образ | Порт (host:cont) | Профиль | Тома | Запускается |
|---|---|---|---|---|---|
| frontend | `Dockerfile.frontend` | 8501:8501 | (всегда) | - | streamlit `app.py` |
| backend | `Dockerfile.backend` | 8080:8080 | (всегда) | - | uvicorn `services.backend_app` |
| worker | `Dockerfile.worker` | 9100:9100 | (всегда) | - | `python -m services.worker_app` |
| embedder | `Dockerfile.inference` | 8000:8000 | (всегда) | `model_cache` | uvicorn `services.inference_app` (`INFERENCE_ROLE=embed`) |
| reranker | `Dockerfile.inference` | 8002:8000 | `reranker` | `model_cache` | uvicorn `services.inference_app` (`INFERENCE_ROLE=rerank`) |
| llm | `Dockerfile.llm` | 8001:8001 | (всегда) | - | uvicorn `services.llm_app` |
| qdrant | `qdrant/qdrant:v1.12.4` | - | (всегда) | `qdrant_data` | векторный стор |
| postgres | `postgres:16` | - | (всегда) | `pg_data` | реляционка (refresh-токены, реестр, чаты) |
| redis | `redis:7` | - | (всегда) | - | кэш + очередь RQ |
| nginx | `nginx:1.27-alpine` | 80:80, 8081:8081 | `panels` | `nginx.conf` (ro) | single-origin reverse-proxy + forward-auth (80 - приложение+панели, 8081 - дашборд Qdrant) |
| pgadmin | `dpage/pgadmin4:8.14` | - | `panels` | `pgadmin_data` | админка Postgres за forward-auth (`/pgadmin`) |
| grafana | `grafana/grafana:11.2.0` | - | `panels` | provisioning + dashboards (ro) | панели за forward-auth (`/grafana`) |
| prometheus | `prom/prometheus:v2.54.1` | - | `panels` | `prometheus.yml` (ro) | сбор метрик |

Сервисы без профиля поднимаются на `docker compose up`. Сервисы с профилем (`reranker`, `panels`)
стартуют только при явном `--profile`.

## Общий env: anchor `x-app-env`

```yaml
x-app-env: &app-env
  ROLE: backend
  VECTOR_KIND: qdrant
  QDRANT_URL: http://qdrant:6333
  DATABASE_DSN: postgresql+psycopg://codelens:codelens@postgres/codelens
  EMBEDDER_KIND: remote
  EMBEDDER_URL: http://embedder:8000
  RERANKER_URL: http://reranker:8000   # при RERANKER_ENABLED=true + RERANKER_KIND=remote
  LLM_KIND: remote
  LLM_URL: http://llm:8001
  REDIS_URL: redis://redis:6379
  JOBS_KIND: redis
```

- YAML-anchor `&app-env`: общий блок окружения для `backend` и `worker`. Это один composition root
  (`factory.py`), но два разных процесса с разными командами - оркестратор FastAPI и RQ-воркер. Оба
  читают один и тот же конфиг и должны видеть одинаковое размещение компонентов, поэтому env общий.
- Именно этот блок проводит профиль large через `kind`-переключатели конфига (см. таблицу в
  [./README.md](./README.md)): `EMBEDDER_KIND=remote` уводит эмбеддер в отдельный под (`EMBEDDER_URL`),
  `LLM_KIND=remote` - в llm-gateway (`LLM_URL`), `VECTOR_KIND=qdrant` подключает Qdrant вместо Chroma,
  `DATABASE_DSN` переводит реляционку на Postgres, `REDIS_URL` включает Redis-кэш вместо `NullCache`,
  `JOBS_KIND=redis` - очередь RQ вместо in-process.
- `RERANKER_URL` задан всегда, но задействуется лишь при `RERANKER_ENABLED=true` и
  `RERANKER_KIND=remote` (по умолчанию реранкер выключен - см. профиль `reranker` ниже).
- Дефолты `kind` в [../../config/config.yaml](../../config/config.yaml) рассчитаны на small (`chroma`,
  `local`, `inprocess`); этот anchor явно переопределяет их под large.

## frontend

```yaml
frontend:
  build: { context: .., dockerfile: deploy/Dockerfile.frontend }
  ports: ["8501:8501"]
  environment:
    ROLE: frontend
    BACKEND_URL: http://backend:8080
  depends_on: [backend]
```

- Тонкий Streamlit-клиент. Своего ML/БД не держит, ходит в backend по `BACKEND_URL`. Единственная
  точка входа для пользователя на `:8501` (или `http://localhost` через nginx в профиле `panels`).
- `depends_on: [backend]` - условие по умолчанию `service_started` (контейнер запущен, без проверки
  готовности).

## backend

```yaml
backend:
  build: { context: .., dockerfile: deploy/Dockerfile.backend }
  ports: ["8080:8080"]
  env_file: [../.env]        # секреты; environment ниже их переопределяет
  environment: *app-env
  depends_on:
    embedder: { condition: service_healthy }
    llm: { condition: service_started }
    qdrant: { condition: service_started }
    postgres: { condition: service_started }
    redis: { condition: service_started }
```

- Оркестратор FastAPI: ретривер (bm25/mmr in-process), auth, remote-клиенты к embedder/llm/qdrant.
- `env_file: ../.env` подаёт секреты (`JWT_SECRET`, `ADMIN_LOGIN`/`ADMIN_PASSWORD`, и т.п.). Блок
  `environment: *app-env` идёт после `env_file` и переопределяет совпадающие ключи - то есть `.env`
  отвечает за секреты, anchor - за размещение компонентов.
- `depends_on` с условиями: `embedder` ожидается по `service_healthy`, остальные зависимости - по
  `service_started`. Разница принципиальна:
  - `embedder` отдаёт `healthy` только после загрузки модели (см. healthcheck ниже). До этого его порт
    закрыт, и любой `search`/`ingest`, которому нужен эмбеддинг, упал бы. Поэтому backend ждёт именно
    готовности.
  - `qdrant`/`postgres`/`redis` - внешние сервисы со своей внутренней готовностью; их клиенты
    переподключаются, так что достаточно факта запуска контейнера (`service_started`). `llm` тоже
    `service_started` - он не на критическом пути поиска (LLM-функции опциональны, фолбэк на пустой
    ответ), а его собственная готовность зависит от провайдеров.

## worker

```yaml
worker:
  build: { context: .., dockerfile: deploy/Dockerfile.worker }
  restart: unless-stopped
  env_file: [../.env]
  environment:
    <<: *app-env
    METRICS_PORT: "9100"     # /metrics воркера (глубина очереди + длительность job)
  ports: ["9100:9100"]
  depends_on:
    embedder: { condition: service_healthy }
    llm: { condition: service_started }
    qdrant: { condition: service_started }
    postgres: { condition: service_started }
    redis: { condition: service_started }
```

- RQ-воркер ingest: тянет задачи из Redis и исполняет `index_path` - тот же пайплайн индексации, что
  и в backend, но в отдельном процессе. Так загрузка большого ZIP/GitHub-репозитория не блокирует
  оркестратор.
- `<<: *app-env` - merge-key: подмешивает общий anchor и добавляет `METRICS_PORT: 9100`. Воркер
  поднимает свой `/metrics` (глубина очереди + длительность job) на `:9100`.
- `restart: unless-stopped` - RQ-воркер может завершиться на простое; перезапуск возвращает его к
  очереди, задачи в Redis при этом не теряются.
- Те же условия `depends_on`, что у backend, и по той же причине: ingest эмбеддит чанки, поэтому ждёт
  `embedder: service_healthy`.

## embedder и reranker

```yaml
embedder:
  build: { context: .., dockerfile: deploy/Dockerfile.inference }
  environment:
    INFERENCE_ROLE: embed
    EMBEDDER_MODEL: intfloat/multilingual-e5-large
  volumes: ["model_cache:/app/cache/models"]
  ports: ["8000:8000"]
  healthcheck:
    test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')\""]
    interval: 10s
    timeout: 5s
    retries: 12
    start_period: 600s
```

- Эмбеддер и реранкер - два пода из одного образа `Dockerfile.inference`; роль задаёт `INFERENCE_ROLE`
  (`embed` / `rerank`). Под `embed` загружает только эмбеддер, под `rerank` - только реранкер, и
  масштабируются они независимо (разбор сервиса - [../services/inference-app.md](../services/inference-app.md)).
- Модель не вшита в образ, скачивается на старте в `model_cache:/app/cache/models` (`MODEL_CACHE`).
  Том `model_cache` сохраняет веса между пересозданиями пода - иначе e5-large (~2 ГБ) качался бы заново
  каждый раз.
- Порт `:8000` открывается только после загрузки модели в `lifespan`. Healthcheck дёргает `/healthz`
  и переводит контейнер в `healthy`, что и позволяет `depends_on: service_healthy` у backend/worker
  дождаться готовности.
- `start_period: 600s` - окно, в течение которого падающие пробы не считаются провалом. Большое
  значение покрывает первую загрузку e5-large (скачивание + инициализация). `retries: 12` × `interval:
  10s` - запас после прогрева.

```yaml
reranker:
  build: { context: .., dockerfile: deploy/Dockerfile.inference }
  profiles: ["reranker"]
  environment:
    INFERENCE_ROLE: rerank
    RERANKER_MODEL: BAAI/bge-reranker-v2-m3
  volumes: ["model_cache:/app/cache/models"]
  ports: ["8002:8000"]
```

- Тот же образ, роль `rerank`, модель `bge-reranker-v2-m3`. Наружу проброшен на `:8002` (внутри тот же
  `:8000`), том `model_cache` общий.
- `profiles: ["reranker"]` - под не стартует на обычном `docker compose up`. Текущая retrieval-политика
  работает без cross-encoder (`FLAG_RERANK=off` в конфиге), поэтому реранкер по умолчанию не нужен и не
  тратит ресурсы. Чтобы включить: `docker compose --profile reranker up` плюс `RERANKER_ENABLED=true`
  и `RERANKER_KIND=remote` у backend (тогда `RERANKER_URL` из anchor вступит в силу).

## llm

```yaml
llm:
  build: { context: .., dockerfile: deploy/Dockerfile.llm }
  ports: ["8001:8001"]
  env_file: [../.env]
```

- Тонкий шлюз к провайдерам LLM (`services.llm_app`), без ML-весов. backend/worker ходят к нему по
  `LLM_URL=http://llm:8001` (`LLM_KIND=remote`). Разбор - [../services/llm-app.md](../services/llm-app.md).
- Ключи провайдеров (`GROQ_API_KEY`, `GEMINI_API_KEY`) из `.env` живут только в этом сервисе и не
  попадают в backend - секрет провайдера изолирован в gateway.

## qdrant, postgres, redis

```yaml
qdrant:
  image: qdrant/qdrant:v1.12.4
  volumes: ["qdrant_data:/qdrant/storage"]
postgres:
  image: postgres:16
  environment:
    POSTGRES_USER: codelens
    POSTGRES_PASSWORD: codelens
    POSTGRES_DB: codelens
  volumes: ["pg_data:/var/lib/postgresql/data"]
redis:
  image: redis:7
```

- Готовые образы, без сборки. `qdrant` - векторный стор (`qdrant_data`), `postgres` - реляционка
  refresh-токенов, index-реестра и чатов (`pg_data`), `redis` - кэш и очередь RQ (без тома: кэш
  восстанавливается, а очередь - рабочий поток).
- Учётные данные Postgres (`codelens`/`codelens`/`codelens`) совпадают с `DATABASE_DSN` в anchor.

## nginx, pgadmin, grafana, prometheus (профиль panels)

```yaml
nginx:
  image: nginx:1.27-alpine
  profiles: ["panels"]
  volumes: ["./nginx/nginx.conf:/etc/nginx/nginx.conf:ro"]
  ports: ["80:80", "8081:8081"]      # 80 - приложение+панели; 8081 - дашборд Qdrant (свой origin)
  depends_on: [frontend, backend, grafana, pgadmin, qdrant]
```

- Профиль `panels` (`docker compose --profile panels up`) добавляет за reverse-proxy три
  административные панели. Всё сводится в один origin `http://localhost`: приложение на `/`, Grafana
  на `/grafana`, pgAdmin на `/pgadmin`. Дашборд Qdrant вынесен на отдельный порт `http://localhost:8081`
  (его UI ходит в API по корне-относительным путям, под субпуть не годится - разбор в
  [./nginx.md](./nginx.md)). Доступ к каждой панели гейтит nginx через forward-auth - подзапрос на
  `/auth/forward-auth`, который проверяет refresh-куку и пропускает только `role=admin`.
- nginx публикует два порта: `80` (приложение + Grafana + pgAdmin) и `8081` (отдельный origin
  дашборда Qdrant). `depends_on` ждёт запуска `pgadmin` и `qdrant` - оба за гейтом nginx.

```yaml
pgadmin:
  image: dpage/pgadmin4:8.14
  profiles: ["panels"]
  environment:
    PGADMIN_DEFAULT_EMAIL: ${PGADMIN_EMAIL:-admin@codelens.com}     # нужен entrypoint'у, для входа не используется
    PGADMIN_DEFAULT_PASSWORD: ${PGADMIN_PASSWORD:-admin}
    PGADMIN_CONFIG_SERVER_MODE: "False"               # desktop-режим: без логина pgAdmin
    PGADMIN_CONFIG_MASTER_PASSWORD_REQUIRED: "False"  # не спрашивать мастер-пароль
    PGADMIN_CONFIG_PROXY_X_HOST_COUNT: "1"            # доверяем заголовкам единственного прокси - nginx
    PGADMIN_CONFIG_PROXY_X_PREFIX_COUNT: "1"
  volumes: ["pgadmin_data:/var/lib/pgadmin"]
  depends_on: [postgres]
```

- Веб-админка Postgres за nginx `/pgadmin` (forward-auth, `role=admin`). Прямого порта наружу нет -
  единственный вход через прокси.
- `SERVER_MODE=False` (desktop-режим) - **без собственного логина pgAdmin**: доступ уже гейтит
  forward-auth по `role=admin`, второй вход избыточен. `MASTER_PASSWORD_REQUIRED=False` убирает запрос
  мастер-пароля за сохранённые подключения. `PGADMIN_DEFAULT_*` в этом режиме для входа не нужны
  (требуются лишь entrypoint'у), переопределяются `PGADMIN_EMAIL`/`PGADMIN_PASSWORD` в `.env`.
- `PGADMIN_CONFIG_PROXY_X_HOST_COUNT`/`X_PREFIX_COUNT=1` - pgAdmin доверяет `X-Forwarded-*` и
  `X-Script-Name` от единственного прокси (nginx), чтобы строить ссылки с префиксом `/pgadmin`
  (разбор location - [./nginx.md](./nginx.md)).
- Том `pgadmin_data` (`/var/lib/pgadmin`) хранит стейт панели: сохранённые соединения и настройки
  переживают пересоздание контейнера.

```yaml
grafana:
  image: grafana/grafana:11.2.0
  profiles: ["panels"]
  environment:
    GF_SERVER_ROOT_URL: http://localhost/grafana/
    GF_SERVER_SERVE_FROM_SUB_PATH: "true"
    GF_AUTH_ANONYMOUS_ENABLED: "false"
    GF_AUTH_PROXY_ENABLED: "true"
    GF_AUTH_PROXY_HEADER_NAME: X-Auth-User
    GF_AUTH_PROXY_HEADER_PROPERTY: username
    GF_AUTH_PROXY_AUTO_SIGN_UP: "true"
    GF_AUTH_PROXY_HEADERS: "Role:X-Auth-Role"
    GF_AUTH_PROXY_WHITELIST: ""
    GF_AUTH_DISABLE_LOGIN_FORM: "true"
  volumes:
    - ./grafana/provisioning:/etc/grafana/provisioning:ro
    - ./helm/codelens/dashboards:/var/lib/grafana/dashboards:ro
  depends_on: [prometheus]
```

- `GF_SERVER_ROOT_URL` + `SERVE_FROM_SUB_PATH` подгоняют Grafana под путь `/grafana` за nginx.
- `auth.proxy` доверяет заголовкам от nginx: имя пользователя - из `X-Auth-User`, роль - из
  `X-Auth-Role` (оба ставит forward-auth). Так панель знает конкретного вошедшего, а не безличного
  anonymous-admin; сам доступ всё равно гейтит nginx. Анонимный вход и форма логина выключены.
- `GF_AUTH_PROXY_WHITELIST: ""` - источник запросов единственный (nginx), поэтому whitelist не нужен.
- Дашборды монтируются из `helm/codelens/dashboards` - тот же `codelens.json`, что и в Helm-чарте.

```yaml
prometheus:
  image: prom/prometheus:v2.54.1
  profiles: ["panels"]
  volumes: ["./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro"]
```

- Сбор метрик с `/metrics` сервисов (backend, worker:9100, inference, llm). Источник дашбордов Grafana.

## Тома

```yaml
volumes:
  qdrant_data: {}
  pg_data: {}
  pgadmin_data: {}   # стейт pgAdmin (сохранённые соединения/настройки)
  model_cache: {}
```

- `qdrant_data` - хранилище векторов Qdrant (`/qdrant/storage`).
- `pg_data` - данные Postgres (`/var/lib/postgresql/data`).
- `pgadmin_data` - стейт pgAdmin (`/var/lib/pgadmin`): сохранённые соединения и настройки панели.
- `model_cache` - кэш скачанных весов embedder/reranker (`/app/cache/models`). Вместо предзагрузки
  моделей в образ они качаются на старте и переживают пересоздание пода в этом томе. Общий между
  `embedder` и `reranker`.

## См. также

- [./dockerfiles.md](./dockerfiles.md) - разбор образов `Dockerfile.*`, собираемых этим compose.
- [./README.md](./README.md) - обзор деплоя: оба профиля и четыре способа запуска.
- [../services/inference-app.md](../services/inference-app.md) · [../services/llm-app.md](../services/llm-app.md) - сервисы за `embedder`/`reranker` и `llm`.
- [../architecture.md](../architecture.md) - порты и `kind`-переключатели размещения.
