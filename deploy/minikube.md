# Локальная валидация деплоя на minikube

Цель - прогнать Helm-чарт на настоящем k8s до выхода на VPS: рендер корректен,
**кластер Qdrant собирается** (главный непроверенный пункт), **CNPG-HA работает**, проходит
сквозной ingest -> поиск. Всё в локальном Docker, без публичной сети/DNS/гео.

## Предусловия (инструменты)

| Инструмент | Зачем | Установка |
|---|---|---|
| docker + minikube | кластер | уже есть |
| kubectl, helm | управление/рендер | уже есть |
| **kubeconform** | валидация манифестов (mk-validate, CI) | бинарь с github.com/yannh/kubeconform/releases |
| **CNPG-оператор** | `postgres` рендерит `kind: Cluster` (CRD) | ставит `make mk-start` |
| metrics-server | HPA (в overlay выключен, addon не обязателен) | `minikube addons enable metrics-server` |

Не нужно для локальной валидации (относится к GitOps): kind (заменяется `minikube --nodes`), Argo CD, kubeseal,
Prometheus Operator (`monitoring.enabled=false`).

Установка kubeconform (Linux):
```
curl -sL https://github.com/yannh/kubeconform/releases/latest/download/kubeconform-linux-amd64.tar.gz \
  | tar xz kubeconform && sudo mv kubeconform /usr/local/bin/
```

## Overlay

[values-local.yaml](helm/codelens/values-local.yaml) ужимает large-дефолты под ноутбук: по одной
реплике, HPA off, без nodeSelector'ов (иначе Pending), ingress off (port-forward), monitoring off.
**`qdrant.replicas=3` оставлено намеренно** - это и проверяет bootstrap кластера.

## Шаги

```
# 1) Статика - быстро, без кластера (ловит большинство ошибок чарта)
make mk-validate

# 2) Кластер + оператор CNPG (драйвер docker под Docker Desktop; версию CNPG: make mk-start CNPG=<url>)
make mk-start

# 3) Образы в minikube (codelens/<svc>:local)
make mk-images

# 4) Установка чарта (на машине ~8 ГБ - без тяжёлого embedder, см. раздел «Ресурсы»)
make mk-up MK_SET="--set embedder.enabled=false"

# 5) Дождаться готовности (embedder тянет e5-large - первые минуты не Ready, это норма)
kubectl get pods -w
```

## Проверки

```
make mk-status
```

- **Qdrant-кластер** (ранее НЕ прогонялся): `…/cluster` отдаёт 3 пира; шарды/RF разъехались.
  Устойчивость: `kubectl delete pod codelens-qdrant-1` -> поднялся, данные на месте.
- **CNPG-HA**: поднять с `--set postgres.instances=3` (`make mk-up`), затем `kubectl cnpg status
  codelens-pg` - primary + 2 standby, стриминг; снос primary -> failover, новый primary избран.
- **E2E ingest**: port-forward фронта, проход ingest -> поиск:
  ```
  kubectl port-forward svc/codelens-frontend 8501:8501
  # открыть http://localhost:8501, войти admin/admin, в админке загрузить ZIP,
  # дождаться job (worker), затем поиск находит код; повторный ingest = инкрементальный skip
  ```

## Уборка

```
make mk-down                 # снести релиз (PVC остаются)
kubectl delete pvc --all     # при необходимости - тома Qdrant/PG/Redis
minikube delete              # снести кластер целиком
```

## Ресурсы (важно)

minikube с docker-драйвером выделяет память **на каждый узел**: `MK_NODES * MK_MEM` не должно
превышать ОЗУ машины. На 8 ГБ ноутбуке `--nodes 3 --memory 8192` просит 24 ГБ и падает
(`RSRC_OVER_ALLOC_MEM`). Дефолт таргета подобран под слабую машину: `MK_NODES=3 MK_MEM=2048` (~6 ГБ).
Переопределяется: `make mk-start MK_MEM=3072 MK_NODES=1`.

Весь стек **с embedder** (e5-large, ~2-4 ГБ) в ~6 ГБ не влезает. Поэтому два прохода:

- **Проход 1 - инфраструктура (главное).** Кластер Qdrant и CNPG-HA дают публичные образы
  (`qdrant/qdrant`, оператор CNPG, `redis`) - **образы `codelens/*` тут не нужны**, поэтому
  `make mk-images` можно пропустить (и не упереться в память при сборке). `mk-infra` ставит только
  stateful-часть (app-компоненты off, `--no-hooks` - без migrate-job, которому нужен backend-образ):
  ```
  make mk-start                                   # 3 узла * 2 ГБ (MK_NODES*MK_MEM <= ОЗУ)
  make mk-infra                                   # qdrant(3) + CNPG + redis, без образов
  make mk-status
  ```
  Для CNPG-HA поднять реплики Postgres: `make mk-infra MK_SET="--set postgres.instances=3"`.

- **Проход 2 - полный E2E с поиском.** Нужен embedder -> заметно больше ОЗУ. На машине с ~8 ГБ
  нереалистично; для E2E поиска/чата опираемся на compose (`make up-panels`), он это уже покрывает.
