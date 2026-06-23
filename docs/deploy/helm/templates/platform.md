# helm/templates - платформенные манифесты (config, secrets, ingress, jobs, observability)

Построчный разбор платформенной обвязки чарта CodeLens (профиль large, k8s): откуда поды берут
конфиг и секреты, как трафик попадает в сервисы, какие хуки выполняют миграции и индексацию, чем
включается наблюдаемость. Stateless-сервисы (frontend/backend/embedder/llm/worker) разобраны в
[./workloads.md](./workloads.md), stateful-сторы (Qdrant/Postgres/Redis) - в [./data.md](./data.md),
устройство `values.yaml` и профили запуска - в [../chart.md](../chart.md).

Все манифесты берут префикс имени из `{{ $full := include "codelens.fullname" . }}`
([_helpers.tpl](../../../../deploy/helm/codelens/templates/_helpers.tpl)), поэтому ресурсы релиза
получают единое имя: `<release>-config`, `<release>-secrets`, `<release>-migrate` и т.д.

## Сводка

| Манифест | Условие рендера | Назначение |
|---|---|---|
| [configmap.yaml](../../../../deploy/helm/codelens/templates/configmap.yaml) | всегда | `config.yaml` из `values.config` → ConfigMap; монтируется во все поды |
| [secret.yaml](../../../../deploy/helm/codelens/templates/secret.yaml) | `secrets.create=true` | Opaque-Secret с DSN/JWT/ключами провайдеров (локалка; в GitOps - sealed-secrets) |
| [ingress.yaml](../../../../deploy/helm/codelens/templates/ingress.yaml) | `ingress.enabled` | маршрутизация host → `/api`, `/auth` на backend, `/` на frontend |
| [pgadmin.yaml](../../../../deploy/helm/codelens/templates/pgadmin.yaml) | `dbadmin.enabled` | админ-панель Postgres: PVC + Deployment + Service (доступ гейтит dbadmin-ingress) |
| [dbadmin-ingress.yaml](../../../../deploy/helm/codelens/templates/dbadmin-ingress.yaml) | `dbadmin.enabled` + `ingress.enabled` | два Ingress (pgAdmin + дашборд Qdrant) за forward-auth `role=admin` |
| [migrate-job.yaml](../../../../deploy/helm/codelens/templates/migrate-job.yaml) | всегда | pre-install/upgrade hook: `alembic upgrade head` |
| [index-job.yaml](../../../../deploy/helm/codelens/templates/index-job.yaml) | `indexJob.enabled` | post-install/upgrade hook: индексация корпуса |
| [servicemonitor.yaml](../../../../deploy/helm/codelens/templates/servicemonitor.yaml) | `monitoring.enabled` | скрейп `/metrics` Service'ов с лейблом `codelens.io/scrape=true` |
| [grafana-dashboard.yaml](../../../../deploy/helm/codelens/templates/grafana-dashboard.yaml) | `monitoring.enabled` + `monitoring.dashboards.enabled` | ConfigMap дашборда с лейблом `grafana_dashboard` |
| [NOTES.txt](../../../../deploy/helm/codelens/templates/NOTES.txt) | всегда (post-render) | подсказки доступа/предусловий после `helm install` |

## configmap.yaml - единый config.yaml для всех подов

ConfigMap рендерит весь [`config.yaml`](../../../../config/config.yaml) одним ключом из блока
`values.config`. Это тот же формат, что читает приложение в любом профиле (small/dev - из файла,
large - из этого ConfigMap), но значения подставлены статически из values, а не через `${VAR}`:

```yaml
data:
  # Весь config.yaml одним файлом. Секреты - ссылки ${VAR}; значения даёт Secret.
  config.yaml: |
    profile: {{ .Values.profile }}
    role: ${ROLE:-backend}
    backend_url: http://{{ $full }}-backend:{{ .Values.backend.port }}
    embedder:
      kind: {{ .Values.config.embedder.kind }}
      ...
    database_dsn: ${DATABASE_DSN}
    auth:
      jwt_secret: ${JWT_SECRET}
```

Разделение ответственности:

- Несекретные параметры (модели, флаги retrieval, TTL кэша, провайдеры LLM) приходят из
  `values.config` и зашиты в текст ConfigMap. Адреса сервисов собираются из `$full` и портов:
  `http://<release>-backend:8080`, `http://<release>-qdrant:6333`, `redis://<release>-redis:6379` -
  имена совпадают с Service'ами из [./workloads.md](./workloads.md) и [./data.md](./data.md).
- Секреты остаются ссылками `${VAR}` (`${DATABASE_DSN}`, `${JWT_SECRET}`, `api_key_env` провайдеров).
  Их подставляет приложение из переменных окружения, которые приходят из Secret. В ConfigMap
  чувствительных значений нет - его можно держать в git и в `helm template` без утечки.

Поды читают конфиг через volume-mount из хелпера `codelens.workload`: ConfigMap монтируется по
`subPath: config.yaml` в `/app/config/config.yaml`, путь передаётся через `CODELENS_CONFIG`. Тот же
монтаж повторён в index-job (миграции конфиг не читают - им хватает `DATABASE_DSN` из Secret).

## secret.yaml - секреты только для локалки

Манифест целиком под `{{- if .Values.secrets.create -}}`. Это намеренно: чарт создаёт Secret лишь
в локальном/dev-сценарии, где значения задаются прямо в values. В GitOps-окружениях секреты
поставляются отдельно (sealed-secrets - зашифрованный `SealedSecret` в git, который контроллер
расшифровывает в обычный `Secret` того же имени), поэтому там ставится `secrets.create=false`, и
чарт чужой Secret не перетирает. Имя Secret в обоих случаях одно -
`codelens.secretName` (`<release>-secrets`), на него ссылается `envFrom.secretRef` во всех подах.

```yaml
stringData:
  # Имена должны совпадать с ${VAR} в config.yaml и api_key_env провайдеров.
  DATABASE_DSN: {{ $dsn | quote }}
  JWT_SECRET: {{ .Values.secrets.jwtSecret | quote }}
  GROQ_API_KEY: {{ .Values.secrets.groqApiKey | quote }}
  GEMINI_API_KEY: {{ .Values.secrets.geminiApiKey | quote }}
  ADMIN_LOGIN: {{ .Values.secrets.adminLogin | quote }}
  ADMIN_PASSWORD: {{ .Values.secrets.adminPassword | quote }}
  HF_TOKEN: {{ .Values.secrets.hfToken | quote }}
```

Ключи:

- `DATABASE_DSN` - DSN Postgres. Если `secrets.databaseDsn` пуст и `postgres.enabled`, DSN
  собирается из `postgres.*` на сервис primary: `postgresql+psycopg://<user>:<pass>@<release>-pg-rw:5432/<db>`.
- `JWT_SECRET` - ключ подписи access/refresh-токенов (HS256); дев-дефолт `>=32` байт, в prod заменяется.
- `GROQ_API_KEY` / `GEMINI_API_KEY` - ключи LLM-провайдеров; имена совпадают с `api_key_env` в блоке
  `config.llm.providers`.
- `ADMIN_LOGIN` / `ADMIN_PASSWORD` - первый администратор (создаётся при первом старте, см. блок
  `auth` в [`config.yaml`](../../../../config/config.yaml)).
- `HF_TOKEN` - токен Hugging Face для загрузки моделей embedder/reranker.

Учётки pgAdmin в секрете нет: панель в desktop-режиме без логина, email/пароль задаются прямо в
[pgadmin.yaml](#pgadminyaml---админ-панель-postgres-за-forward-auth) плейн-env (для входа не нужны).

Имена ключей - контракт: они должны совпадать с `${VAR}` в ConfigMap и с `api_key_env` провайдеров,
иначе подстановка в подах даст пустое значение.

## ingress.yaml - host и маршруты на backend/frontend

Под `{{- if .Values.ingress.enabled -}}`. Берёт `host`, `className` и произвольные `annotations`
из values; правило одного хоста раскладывает префиксы на два бэкенд-Service'а:

```yaml
paths:
  {{- if .Values.ingress.apiPath }}
  - path: /api
    pathType: Prefix
    backend: { service: { name: {{ $full }}-backend, ... } }
  {{- end }}
  {{- if .Values.ingress.authPath }}
  # single-origin: браузер бьёт /auth/* напрямую в backend (httpOnly refresh-cookie)
  - path: /auth
    pathType: Prefix
    backend: { service: { name: {{ $full }}-backend, ... } }
  {{- end }}
  - path: /
    pathType: Prefix
    backend: { service: { name: {{ $full }}-frontend, ... } }
```

- `apiPath` (`/api/*` → backend) - REST-API, к которому ходит фронтенд-клиент (`HttpBackend`).
- `authPath` (`/auth/*` → backend) выключен по умолчанию. Он нужен, когда браузер обращается к
  эндпоинтам авторизации напрямую, а не через прокси фронтенда. Smart-причина -
  **single-origin**: refresh-токен живёт в `httpOnly`+`SameSite`-cookie (см. блок `auth` в
  [`config.yaml`](../../../../config/config.yaml)). Чтобы браузер сохранял и отправлял такую cookie без
  возни с CORS и `SameSite=None`, `/auth/*` и UI должны висеть на одном origin (один host через один
  Ingress). Тогда cookie ставится на общий домен и автоматически прикладывается к refresh-запросам.
- `/` (catch-all) → frontend (Streamlit). Порядок важен: более специфичные префиксы `/api`, `/auth`
  объявлены раньше `/`.

Аналог для docker-compose (тот же раскрой на reverse-proxy) - в [../../nginx.md](../../nginx.md).

## pgadmin.yaml - админ-панель Postgres за forward-auth

Под `{{- if .Values.dbadmin.enabled -}}` (по умолчанию выключено). Один манифест разворачивает
pgAdmin 4 тремя объектами: PVC + Deployment + Service. Внешнего доступа сам по себе не даёт - его
гейтит [dbadmin-ingress.yaml](#dbadmin-ingressyaml---гейт-ingress-админ-панелей); Service висит
только внутри кластера.

```yaml
spec:
  securityContext:
    fsGroup: 5050             # uid/gid pgadmin - чтобы PVC был доступен на запись
  containers:
    - name: pgadmin
      image: {{ .Values.dbadmin.image }}
      env:
        - { name: PGADMIN_CONFIG_SERVER_MODE, value: "False" }            # desktop-режим: без логина pgAdmin
        - { name: PGADMIN_CONFIG_MASTER_PASSWORD_REQUIRED, value: "False" }
        - { name: PGADMIN_CONFIG_PROXY_X_HOST_COUNT, value: "1" }
        - { name: PGADMIN_CONFIG_PROXY_X_PREFIX_COUNT, value: "1" }
        - { name: PGADMIN_DEFAULT_EMAIL, value: "admin@codelens.com" }      # должен быть валидным, иначе старт падает
        - { name: PGADMIN_DEFAULT_PASSWORD, value: "desktop-mode-no-login" }  # заглушка: в desktop-режиме не для входа
      readinessProbe:
        tcpSocket: { port: 80 }
```

- **PVC** (`dbadmin.storage`/`storageClass`) держит учётку pgAdmin и сохранённые подключения -
  состояние, переживающее рестарт пода.
- **fsGroup: 5050** - uid/gid пользователя контейнера pgAdmin; без него смонтированный PVC окажется
  недоступен на запись, и панель не стартует.
- **Без логина pgAdmin** (`SERVER_MODE=False`, desktop-режим): второй формы входа нет, гейт один -
  `role=admin` через ingress forward-auth. `PGADMIN_DEFAULT_EMAIL`/`PGADMIN_DEFAULT_PASSWORD` заданы
  плейн-env (в секрете их нет): email обязан быть валидным, иначе entrypoint падает, пароль - заглушка.
- **Префикс задаёт ingress, не env**: pgAdmin узнаёт о субпути `/pgadmin` из заголовка `X-Script-Name`,
  который проставляет ingress, а не из `SCRIPT_NAME`-env. Иначе при `rewrite-target` префикс
  навесился бы дважды. `PROXY_X_*=1` велит доверять `X-Forwarded-*`/`X-Script-Name` ровно от одного
  прокси (ingress-controller).
- **readiness - tcpSocket:80**, а не `httpGet`: HTTP-путь зависит от `X-Script-Name`, которого у
  пробы нет, поэтому проверяется только TCP-готовность порта.

## dbadmin-ingress.yaml - гейт-Ingress админ-панелей

Под `{{- if and .Values.dbadmin.enabled .Values.ingress.enabled -}}`. Ставит панель на тот же host,
что приложение, за гейтом forward-auth. Тот же host обязателен: forward-auth опирается на
refresh-cookie `path=/` приложения, а она host-only и до субдомена/другого хоста не доходит. Шаблон
ветвится по `ingress.className`: **traefik** (overlay k3s, основной путь) и **nginx**.

**Traefik** - цепочка из трёх `Middleware` на путь `/pgadmin`:

```yaml
forwardAuth:                       # гейт role=admin
  address: http://<full>-backend:<port>/auth/forward-auth
  authResponseHeaders: [X-Auth-User, X-Auth-Role]
---
headers: { customRequestHeaders: { X-Script-Name: /pgadmin } }   # pgAdmin строит ссылки с префиксом
---
stripPrefix: { prefixes: [/pgadmin] }                            # снять /pgadmin перед проксированием
```

Ingress навешивает их аннотацией в порядке `forwardAuth → headers → stripPrefix`:
`traefik.ingress.kubernetes.io/router.middlewares: "<ns>-<full>-forward-auth@kubernetescrd,<ns>-<full>-pgadmin-headers@kubernetescrd,<ns>-<full>-pgadmin-strip@kubernetescrd"`.
`forwardAuth` пускает только при `role=admin` в БД (как доступ к Grafana), иначе `401`; `stripPrefix`
заменяет `rewrite-target` из nginx, а `X-Script-Name` возвращает pgAdmin его субпуть.

**nginx** (ветка `else`) - тот же гейт через external-auth: `auth-url` → `/auth/forward-auth`,
`auth-signin` редиректит не-admin на `https://<host>/`, `rewrite-target: /$2` срезает префикс,
`configuration-snippet` ставит `X-Script-Name /pgadmin`.

- **Qdrant-дашборд** (под `dbadmin.qdrant`, по умолчанию выключен): путь `/qdrant` → Service
  `<full>-qdrant:6333` за тем же гейтом. **Caveat**: UI Qdrant ходит в API по корне-относительным
  путям (`/collections`, `/cluster`), поэтому под субпутём часть запросов уходит мимо, а на субдомен
  не дойдёт кука. Для живого кластера - port-forward к `:6333/dashboard` либо отдельный host с
  собственной авторизацией (basicAuth).

## migrate-job.yaml - миграции схемы Argo Sync-хуком

Job прогоняет `alembic upgrade head` на primary (`-pg-rw`). Оформлен как **Argo Sync-хук**, а не
Helm `pre-install`: pre-install шёл бы до создания CNPG `Cluster` в основном синке, падал на
отсутствии БД и блокировал весь синк (дедлок). Sync-хук же исполняется внутри синка с учётом
sync-wave, а готовность БД обеспечивает initContainer:

```yaml
annotations:
  argocd.argoproj.io/hook: Sync
  argocd.argoproj.io/hook-delete-policy: BeforeHookCreation
  argocd.argoproj.io/sync-wave: "-1"          # после Cluster (wave -2), до app-подов (0)
spec:
  backoffLimit: 6
  template:
    spec:
      restartPolicy: Never
      initContainers:
        - name: wait-postgres                  # крутится, пока -pg-rw не примет соединение
          command: ["sh", "-c", "until python -c '...create_connection((\"<full>-pg-rw\",5432))'; do sleep 3; done"]
      containers:
        - name: migrate
          image: {{ include "codelens.image" (dict "root" . "name" "backend") }}
          command: ["alembic", "upgrade", "head"]
          envFrom:
            - secretRef: { name: {{ include "codelens.secretName" . }} }
```

- **sync-wave**: CNPG `Cluster` и его секрет помечены `sync-wave: "-2"` ([data.md](./data.md)),
  миграция - `"-1"`, остальные ресурсы - дефолтный `0`. Argo прогоняет волны по порядку: БД → миграция
  → поды сервисов.
- **initContainer `wait-postgres`** ждёт, пока сервис `-pg-rw` начнёт принимать соединения, - не
  завязываясь на то, умеет ли Argo health-check CNPG. Поэтому миграция не падает, даже если волна
  стартовала раньше готовности primary.
- **hook-delete-policy: BeforeHookCreation** - старый Job удаляется перед пересозданием (Job immutable).
  Образ - тот же `backend` (в нём alembic и модели), DSN из Secret через `envFrom`. `backoffLimit: 6` -
  запас попыток на время подъёма CNPG.

## index-job.yaml - опциональная индексация корпуса

Под `{{- if .Values.indexJob.enabled -}}` (по умолчанию выключен), оформлен как хук **после**
install/upgrade. Прогоняет `python index.py <folder> <source>`: считает эмбеддинги, кладёт векторы в
Qdrant, реестр - в Postgres.

```yaml
annotations:
  "helm.sh/hook": post-install,post-upgrade
  "helm.sh/hook-weight": "10"
  "helm.sh/hook-delete-policy": before-hook-creation
spec:
  backoffLimit: 10        # запас на время подъёма embedder/qdrant/postgres
  template:
    spec:
      restartPolicy: OnFailure
      containers:
        - name: index
          command: ["python", "index.py", {{ .Values.indexJob.folder | quote }}, {{ .Values.indexJob.source | quote }}]
```

Почему опционально и почему post-хук:

- Индексация имеет смысл не на каждом релизе - корпус заливают разово или из админки (ingest
  ZIP/GitHub), поэтому дефолт `enabled=false`. Альтернатива - `python index.py` вручную в поде backend
  (см. [NOTES.txt](../../../../deploy/helm/codelens/templates/NOTES.txt)).
- Хук **post**-install: индексатору нужны уже поднятые embedder, Qdrant и Postgres, поэтому он идёт
  после сервисов. Запас `backoffLimit: 10` и `restartPolicy: OnFailure` дают время на их readiness -
  первые попытки могут падать, пока зависимости стартуют.
- В отличие от миграций, index-job монтирует ConfigMap (нужны адреса embedder/Qdrant и параметры
  retrieval) и читает Secret.

## servicemonitor.yaml - скрейп метрик Prometheus Operator

Под `{{- if .Values.monitoring.enabled }}` (по умолчанию `false`). Это декларация для Prometheus
Operator (kube-prometheus-stack): «скрейпь `/metrics` на порту `http` у Service'ов с лейблом
`codelens.io/scrape=true` в этом namespace».

```yaml
spec:
  namespaceSelector: { matchNames: [ {{ .Release.Namespace }} ] }
  selector:
    matchLabels:
      {{- include "codelens.selectorLabels" . | nindent 6 }}
      codelens.io/scrape: "true"
  endpoints:
    - port: http
      path: /metrics
      interval: {{ .Values.monitoring.serviceMonitor.interval }}
```

- Лейбл `codelens.io/scrape: "true"` ставится хелпером `codelens.workload` только на Service'ы с
  `metrics: true` в values (backend, embedder, reranker, llm + отдельный Service воркера). Frontend
  (Streamlit, без `/metrics`) лейбла не имеет и не скрейпится.
- Порт `http` - именованный порт из того же хелпера; ServiceMonitor ссылается на имя, а не на номер.
- Сам ServiceMonitor оператор подхватит только если его лейблы попадают под `serviceMonitorSelector`
  стека - их задают в `monitoring.serviceMonitor.labels` (обычно `{ release: <имя-стека> }`).

Подробнее об инструментировании, метриках и PromQL - в
[../../../util/observability.md](../../../util/observability.md).

## grafana-dashboard.yaml - дашборд через сайдкар

Под `{{- if and .Values.monitoring.enabled .Values.monitoring.dashboards.enabled }}`. Это ConfigMap с
JSON-дашбордом, помеченный лейблом `grafana_dashboard`:

```yaml
metadata:
  labels:
    {{ .Values.monitoring.dashboards.label }}: "1"
data:
  codelens.json: |-
    {{- .Files.Get "dashboards/codelens.json" | nindent 4 }}
```

Grafana-сайдкар kube-prometheus-stack сам сканирует ConfigMap'ы с этим лейблом
(`sidecar.dashboards.label`, дефолт `grafana_dashboard`) и импортирует дашборды - отдельная провизия
через provisioning-файлы или API не нужна. JSON подтягивается из `dashboards/codelens.json` чарта
через `.Files.Get`. Состав дашборда «CodeLens - обзор» описан в
[../../../util/observability.md](../../../util/observability.md).

## NOTES.txt - подсказки после установки

Не Kubernetes-ресурс: рендерится Helm-ом и печатается в stdout после `helm install/upgrade`. Сводит
по релизу число реплик каждого компонента, предусловия large-профиля (операторы CNPG и
ingress-controller, запись `host` в DNS/hosts), напоминает про pre-install миграции и статус
индексации (хук или ручной запуск). В блоке «Доступ» даёт URL Ingress (если включён) либо команду
`port-forward` на frontend, и приводит smoke-команду для kind/minikube с урезанным масштабом
(`--set qdrant.replicas=1 --set postgres.instances=1 --set *.hpa.enabled=false`).
