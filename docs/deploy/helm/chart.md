# Helm-чарт CodeLens - устройство

Разбор того, как собран чарт `deploy/helm/codelens` и зачем именно так: метаданные, общие
шаблоны `_helpers.tpl`, `NOTES.txt`, структура `values.yaml` и набор overlay-ов под разные
окружения. Сами манифесты из `templates/` тут не разбираются - они вынесены в отдельные доки:
[workloads](./templates/workloads.md) (stateless-сервисы, worker, hooks), [data](./templates/data.md)
(qdrant, postgres/CNPG, redis) и [platform](./templates/platform.md) (ingress, secret, configmap,
servicemonitor, dashboard). Общий обзор раскладки деплоя - в [../README.md](../README.md).

Один чарт обслуживает только профиль **large** (k8s). Профиль small запускается без кластера
(`make run` - один Streamlit-процесс на chroma+sqlite; `make up`/`make down` - docker compose), и
к чарту отношения не имеет.

## Chart.yaml

[`Chart.yaml`](../../../deploy/helm/codelens/Chart.yaml):

```yaml
apiVersion: v2
name: codelens
description: CodeLens - RAG-поиск по коду. Один чарт на small/large; различие задаётся через values.
type: application
version: 0.1.0
appVersion: "0.1.0"
keywords: [rag, code-search, retrieval]
```

| Поле | Значение | Назначение |
|---|---|---|
| `apiVersion` | `v2` | формат Helm 3 (зависимости в самом `Chart.yaml`, тут их нет). |
| `name` | `codelens` | имя чарта · подставляется в `codelens.name`/`codelens.chart` и в лейблы. |
| `type` | `application` | разворачиваемое приложение, не library-чарт. |
| `version` | `0.1.0` | версия упаковки чарта (SemVer). Двигается при изменении шаблонов/values. |
| `appVersion` | `"0.1.0"` | версия приложения. Идёт в лейбл `app.kubernetes.io/version`. Тег образа версией **не** задаётся - он отдельно в `image.tag` и правится CI. |

`version` и `appVersion` разнесены намеренно: пересборка манифестов чарта (правка шаблона) повышает
`version`, а выкатка нового кода идёт через `image.tag` (git-SHA, его правит CI), не трогая
метаданные чарта.

## _helpers.tpl - именованные шаблоны

[`templates/_helpers.tpl`](../../../deploy/helm/codelens/templates/_helpers.tpl) собирает имена,
лейблы и переиспользуемую логику. Префикс `_` - файл не рендерится в манифест, только `define`-блоки.

| Шаблон | Что отдаёт | Зачем |
|---|---|---|
| `codelens.name` | имя чарта (или `nameOverride`), обрезано до 63 символов | базовое имя для лейблов и `fullname`. |
| `codelens.fullname` | префикс имён ресурсов | базовое имя всех объектов чарта (см. ниже про guard). |
| `codelens.chart` | `codelens-0.1.0` | значение лейбла `helm.sh/chart`. |
| `codelens.labels` | общий набор лейблов | навешивается на каждый ресурс (chart, version, managed-by + селекторы). |
| `codelens.selectorLabels` | `app.kubernetes.io/name` + `/instance` | стабильное ядро селектора Deployment/Service (без version - иначе апгрейд сломает selector). |
| `codelens.secretName` | `<fullname>-secrets` | единое имя Secret для `envFrom.secretRef` всех подов. |
| `codelens.image` | `registry/<name>:tag` | полное имя образа компонента из `image.*`. |
| `codelens.workload` | Deployment + Service [+ HPA] | generic-шаблон stateless-сервиса (разбирается в [workloads](./templates/workloads.md)). |

### codelens.fullname и guard против двойного префикса

```gotemplate
{{- define "codelens.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := include "codelens.name" . -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}
```

Логика по приоритету:

1. Если задан `fullnameOverride` - берётся он как есть. Так делают overlay-и staging/prod
   (`fullnameOverride: codelens`), чтобы имена ресурсов (`codelens-backend`, `codelens-pg-rw`)
   были стабильны и не зависели от имени Argo Application.
2. Иначе, если имя релиза уже содержит имя чарта (`contains $name .Release.Name`) - берётся только
   `.Release.Name`. Это guard против двойного префикса: при `helm install codelens ...` без
   него вышло бы `codelens-codelens-backend`. Проверка `contains` гасит повтор → имена остаются
   `codelens-backend`.
3. Иначе - `<release>-<name>` (релиз `prod` + чарт `codelens` → `prod-codelens-backend`).

`trunc 63 | trimSuffix "-"` держит имена в пределах лимита k8s на длину DNS-метки и убирает
хвостовой дефис после обрезки.

### Лейблы и селекторы

`codelens.labels` навешивается на каждый ресурс; `codelens.selectorLabels` - на `selector` и
`template.metadata.labels` подов. Селектор намеренно уже полного набора лейблов: в него входят
только `name` и `instance` (плюс `app.kubernetes.io/component` добавляется в самом workload).
`helm.sh/chart` и `app.kubernetes.io/version` в селектор не попадают - иначе после bump версии
selector Deployment перестал бы матчить старые поды (это immutable-поле, апгрейд бы упал).

## NOTES.txt

[`templates/NOTES.txt`](../../../deploy/helm/codelens/templates/NOTES.txt) печатается после
`helm install`/`upgrade` (рендерится теми же values). Назначение - сводка по факту установки и
напоминание о предусловиях, а не статичный текст. Что выводит:

- профиль, имя релиза и namespace;
- список компонентов с фактическим числом реплик (`frontend x2`, `backend x2`, ...), причём для
  опциональных - состояние: `reranker выключен`, `qdrant внешний`, `postgres CNPG x3`, `redis внешний`;
- предусловия large-профиля: оператор CloudNativePG (под `postgres.enabled`), ingress-controller
  и запись host в DNS/hosts (под `ingress.enabled`);
- напоминание про миграции (pre-install/upgrade hook `alembic upgrade head` на `<fullname>-pg-rw`)
  и про индексацию (post-install hook, если `indexJob.enabled`, иначе - подсказка запустить вручную);
- способ доступа: URL ingress либо команда `kubectl port-forward`, в зависимости от `ingress.enabled`;
- готовая команда smoke-теста на kind/minikube (ужать масштаб через `--set`).

## values.yaml - структура

[`values.yaml`](../../../deploy/helm/codelens/values.yaml) - **база large-дефолтов**: значения
рассчитаны на полноценный HA-кластер (HPA включены, qdrant x3, postgres x3). Модель наложения:
база задаёт «полный large», а overlay сверху **ужимает или специализирует** под конкретное окружение
(локалка, staging, prod, k3s). Поэтому в overlay-ах лежит только дельта, а не полная копия values.

```yaml
profile: large   # чарт только под large; small идёт мимо k8s (make run / docker compose)
```

### image - общий блок образов

```yaml
image:
  registry: ghcr.io/dryslo/codelens
  tag: latest
  pullPolicy: IfNotPresent
```

Один реестр и один `tag` на все компоненты; конкретный образ собирается в `codelens.image` как
`registry/<name>:tag`, где `<name>` - поле `image` per-service блока (`backend`, `frontend`,
`inference`, `llm`, `worker`). `tag` в overlay-ах правит CI на git-SHA.

### config - проброс в ConfigMap

Блок `config` целиком рендерится в `config.yaml` внутри ConfigMap и монтируется в поды по
`/app/config/config.yaml` (env `CODELENS_CONFIG`). Секреты внутрь не пишутся - там только ссылки
`${VAR}`, а значения приходят из Secret через `envFrom`. Ключевые подблоки:

| Подблок | Смысл |
|---|---|
| `vector` | стор `qdrant`, фактор шардирования/репликации **коллекции** (`shards`/`replicas`) - не путать с числом подов `qdrant.replicas`. |
| `embedder` | `kind: remote` (ходит в inference-под), модель `multilingual-e5-large`, `dim: 1024`. |
| `reranker` | `enabled: false` - cross-encoder выключен по умолчанию (включается вместе с reranker-подом). |
| `retrieval.flags` | режимы техник ретривера (`bm25`/`multiquery`/`hyde`/`rerank`/`mmr`): `off`/`ui`/`thinking`/`fast`. |
| `llm` | `kind: remote` (backend/ретривер ходят в llm-gateway), провайдеры и `fast`-модель. |
| `jobs` | `kind: redis` - ingest исполняет worker-под (RQ); в small/dev было бы `inprocess`. |
| `auth` | включён, TTL access/refresh-токенов. |

### Per-service блоки (stateless)

`frontend`, `backend`, `embedder`, `reranker`, `llm` - однотипные блоки под generic-шаблон
`codelens.workload` (Deployment + Service [+ HPA]). Поля каждого:

| Поле | Назначение |
|---|---|
| `enabled` | рендерить ли компонент вообще. |
| `replicas` | число реплик (база - 2; `reranker` - 0, выключен). |
| `image` | имя образа в реестре (`embedder` и `reranker` делят один образ `inference`, роль задаётся env `INFERENCE_ROLE`). |
| `port` / `healthPath` | порт контейнера и путь readiness-пробы. |
| `metrics` | `true` → добавляет лейбл `codelens.io/scrape: "true"` на Service (для ServiceMonitor). frontend его не имеет. |
| `env` | доп. переменные (`ROLE`, `INFERENCE_ROLE`, `*_MODEL`). |
| `resources` | requests/limits (заданы у backend/embedder; у лёгких - `{}`). |
| `nodeSelector` / `tolerations` / `affinity` | раскладка подов по узлам (в базе пусты, заполняются в k3s-overlay). |
| `hpa` | `{ enabled, minReplicas, maxReplicas, cpu }` - автоскейл по CPU. |

```yaml
embedder:
  image: inference        # тот же образ, что reranker; роль задаётся INFERENCE_ROLE
  env:
    - { name: INFERENCE_ROLE, value: embed }
  hpa: { enabled: true, minReplicas: 2, maxReplicas: 6, cpu: 70 }
```

`worker` стоит особняком: это Deployment **без** Service и порта (тянет очередь из Redis), нужен при
`config.jobs.kind=redis`. Параллелизм - числом реплик. У него отдельный `metricsPort: 9100` и
отдельный Service для скрейпа `/metrics`.

### Stateful: qdrant, postgres, redis

```yaml
qdrant:
  replicas: 3            # >1 включает QDRANT__CLUSTER__ENABLED + podAntiAffinity + bootstrap кластера
  antiAffinity: hard     # hard: 1 под/узел (prod-HA); soft: предпочтительно (мало узлов)
  storage: 50Gi
  storageClass: ""       # пусто -> дефолтный (в k3s local-path - годится для self-replicating БД)

postgres:
  enabled: true          # CNPG Cluster (нужен оператор CloudNativePG)
  instances: 3           # HA: primary + 2 реплики (CNPG разносит по узлам сам)

redis:
  enabled: true
  storage: 5Gi
```

- `qdrant.replicas` - число **подов** (узлов кластера Qdrant), `antiAffinity` управляет разносом по
  узлам, `storageClass` пуст → дефолтный (local-path для самореплицирующейся БД, не сетевой Longhorn).
- `postgres` рендерит CNPG `Cluster` (требует установленного оператора); `instances` - primary плюс
  реплики.
- `enabled: false` у любого из трёх означает «БД внешняя» - чарт её не поднимает, поды ходят наружу.

### ingress

```yaml
ingress:
  enabled: true
  className: nginx
  host: codelens.local
  apiPath: true          # /api/* -> backend
  authPath: false        # /auth/* -> backend (single-origin httpOnly refresh-cookie)
  annotations: {}
```

`apiPath`/`authPath` управляют дополнительными путями маршрутизации на backend; `authPath`
включает single-origin для httpOnly refresh-cookie. Разбор самого манифеста - в
[platform](./templates/platform.md).

### secrets

```yaml
secrets:
  create: true           # false -> Secret кладёт sealed-secrets/external-secrets, не чарт
  jwtSecret: dev-insecure-change-me-min-32-bytes!!
  databaseDsn: ""        # пусто -> собирается из postgres.* на сервис -pg-rw
  groqApiKey: ""
  adminLogin: admin
  adminPassword: ""
  pgadminEmail: admin@codelens.com   # учётка pgAdmin; нужна при dbadmin.enabled
  pgadminPassword: ""                  # задать при dbadmin.enabled (в prod - sealed-secrets)
```

`create: true` (база/local) - чарт сам создаёт Secret `<fullname>-secrets` из этих полей. В
staging/prod - `create: false`: Secret кладёт в namespace контроллер sealed-secrets (или
external-secrets), а чарт его только читает через `envFrom.secretRef` с тем же именем. Так
секреты не попадают в git/values.

### monitoring

```yaml
monitoring:
  enabled: false         # true -> ServiceMonitor + ConfigMap дашборда Grafana
  serviceMonitor:
    interval: 30s
    labels: {}           # serviceMonitorSelector оператора (напр. release: kube-prometheus-stack)
  dashboards:
    enabled: true
    label: grafana_dashboard
```

По умолчанию выключено - включается только на кластере с Prometheus Operator. Подробности
скрейпа и дашборда - в [наблюдаемости](../../util/observability.md).

### dbadmin

```yaml
dbadmin:
  enabled: false         # тумблер админ-панелей БД (pgAdmin + дашборд Qdrant за forward-auth)
  image: dpage/pgadmin4:8.14
  storage: 1Gi           # PVC pgAdmin (учётка + сохранённые подключения)
  storageClass: ""
  resources: {}
  qdrant: true           # добавить путь /qdrant к дашборду Qdrant за тем же гейтом (см. caveat)
```

Один тумблер на обе админ-панели. `enabled: true` рендерит pgAdmin (PVC+Deployment+Service) и
dbadmin-ingress, который гейтит доступ через `/auth/forward-auth` (`role=admin` в БД, как у
Grafana). Учётку самого pgAdmin даёт Secret - поля `secrets.pgadminEmail`/`secrets.pgadminPassword`
(→ ключи `PGADMIN_DEFAULT_EMAIL`/`PGADMIN_DEFAULT_PASSWORD`); пароль нужно задать при включении (в
prod - через sealed-secrets, не в values).

Требование: панели висят на **том же host**, что приложение. forward-auth опирается на
refresh-cookie `path=/` приложения, а такая cookie прикладывается только в пределах своего домена -
на отдельном host гейт не увидит сессию. `qdrant: true` добавляет путь `/qdrant` за тем же гейтом
(с оговоркой про корне-относительные пути UI Qdrant, см. [platform](./templates/platform.md)).

Реализация auth-аннотаций - ingress-nginx (external auth). Для traefik (overlay k3s,
`ingress.className: traefik`) аналога аннотаций нет: тот же гейт собирается через `Middleware` типа
`forwardAuth`. Подробности обоих манифестов - в [platform](./templates/platform.md).

### indexJob

```yaml
indexJob:
  enabled: false         # post-install/upgrade hook индексации корпуса
  folder: data/codebase
  source: codebase
```

Опциональный Job, запускающий индексацию корпуса как post-install hook. По умолчанию выключен -
индексация запускается вручную или включением флага.

### reranker.enabled

Реранкер выключен в двух местах согласованно: `reranker.enabled: false` (нет пода, `replicas: 0`)
и `config.reranker.enabled: false` (пайплайн не зовёт cross-encoder). Включать оба сразу - под и
флаг конфигурации.

## Overlay-и

Поверх базового `values.yaml` накладываются файлы дельт. Helm и Argo CD применяют `valueFiles`
**по порядку** - каждый следующий перетирает предыдущий по совпадающим ключам. Слияние глубокое:
переопределяется только указанный ключ, остальное наследуется из базы.

| Overlay | Назначение | Порядок наложения |
|---|---|---|
| [`values-local.yaml`](../../../deploy/helm/codelens/values-local.yaml) | локальная валидация на minikube | `values.yaml`, `values-local.yaml` |
| [`values-staging.yaml`](../../../deploy/helm/codelens/values-staging.yaml) | staging-кластер (ветка dev) | `values.yaml`, `values-staging.yaml` |
| [`values-prod.yaml`](../../../deploy/helm/codelens/values-prod.yaml) | prod-кластер (ветка main) | `values.yaml`, `values-prod.yaml` |
| [`values-k3s.yaml`](../../../deploy/helm/codelens/values-k3s.yaml) | инфра-специфика k3s на self-managed VPS | `values.yaml`, `values-prod.yaml`, `values-k3s.yaml` |

Local применяется напрямую `helm install ... -f values-local.yaml`; staging/prod/k3s - через Argo CD
(`helm.valueFiles` в Application). k3s накладывается **последним** поверх prod-overlay: prod даёт
прикладные настройки (масштаб, флаги, секреты-извне), k3s - только инфраструктурную раскладку
(ingress-класс, метки/taint'ы узлов, привязку тяжёлых подов).

### Ключевые отличия

| Параметр | base (large) | local | staging | prod | k3s (поверх prod) |
|---|---|---|---|---|---|
| `image.registry`/`tag` | ghcr / `latest` | `codelens`/`local` | `latest` (CI→SHA) | `latest` (CI→SHA) | - |
| `fullnameOverride` | - | - | `codelens` | `codelens` | - |
| HPA | включены | выключены | выключены | включены | embedder/llm выключены |
| `frontend/backend/embedder/llm.replicas` | 2 | 1 | 1 | 2 | embedder 1, llm 1 |
| `qdrant.replicas` | 3 | 3 | 1 | 3 | 3 (= числу data-узлов) |
| `qdrant.antiAffinity` | hard | soft | hard | hard | hard |
| `postgres.instances` | 3 | 1 | 1 | 3 | 3 |
| `config.vector.shards/replicas` | 2/2 | 2/2 | 1/1 | 2/2 | 2/2 |
| `ingress` | nginx, `codelens.local` | выключен | `staging.codelens.local` | `codelens.example.com`+TLS | className `traefik` |
| `monitoring` | выключен | выключен | (база) | (база) | - |
| `secrets.create` | `true` | `true` | `false` | `false` | - |
| `nodeSelector`/`tolerations` | пусты | пусты | пусты | пусты | embedder→heavy, llm→geo=eu, data→pool=data |

**local** (minikube): масштаб ужат под ноутбук, HPA выключены (нет metrics-server), `nodeSelector`
не задаются (на minikube нет меток pool/geo - иначе поды зависнут в Pending), ingress выключен
(доступ через port-forward), мониторинг выключен. `qdrant.replicas=3` оставлено намеренно - именно
этот overlay прогоняет bootstrap кластера Qdrant; `antiAffinity: soft`, чтобы при <3 узлах кластер
собрался, а не залип в Pending.

**staging**: `fullnameOverride: codelens` для стабильных имён независимо от имени Application; одна
нода Qdrant (`replicas: 1`) → фактор коллекции тоже 1 (`config.vector.shards/replicas: 1`), иначе не
разместятся; масштаб урезан; `secrets.create: false` - Secret кладёт sealed-secrets из
`deploy/gitops/sealed/staging/`.

**prod**: полный large-масштаб берётся из базы (HPA включены, qdrant x3, postgres x3); overlay меняет
только host (`codelens.example.com`), добавляет TLS-аннотацию cert-manager и ставит
`secrets.create: false` (Secret - через sealed-secrets/external-secrets).

**k3s**: инфраструктурный слой поверх prod. `ingress.className: traefik` (k3s по умолчанию ставит
Traefik); раскладка тяжёлых подов по выделенным узлам через `nodeSelector`+`tolerations` -
embedder на `pool: heavy` (одна реплика: два пода = двойная загрузка модели → OOM), llm на `geo: eu`
(доступ к внешним API), qdrant/postgres на `pool: data` (каждому поду свой local-path-диск, т.к. БД
реплицируют на уровне приложения - сетевой том дал бы двойную репликацию). `qdrant.replicas` держат
равным числу data-узлов (жёсткая podAntiAffinity - лишний под зависнет в Pending).
