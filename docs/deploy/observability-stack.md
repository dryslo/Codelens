# Стек наблюдаемости в compose (профиль panels)

Compose-вариант наблюдаемости: Prometheus собирает метрики приложения, Grafana показывает дашборд.
Разбор [`../../deploy/prometheus/prometheus.yml`](../../deploy/prometheus/prometheus.yml) и
[`../../deploy/grafana/provisioning/`](../../deploy/grafana/provisioning/).

Это локальный/демо-аналог кластерной наблюдаемости. В k8s то же делают `ServiceMonitor` +
grafana-сайдкар kube-prometheus-stack - разбор и список метрик в
[`../util/observability.md`](../util/observability.md). Сами метрики (`codelens_http_*`,
`codelens_retrieval_*`, `codelens_cache_*`, `codelens_ingest_*`) и точки инструментирования общие
для обоих вариантов; здесь - только обвязка сбора и отображения.

Поднимается в профиле `panels` вместе с nginx и Grafana (разбор сервисного блока -
[`./docker-compose.md`](./docker-compose.md#nginx-grafana-prometheus-профиль-panels)).

## prometheus.yml - сбор метрик

```yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: backend
    static_configs:
      - targets: ["backend:8080"]
  - job_name: worker
    static_configs:
      - targets: ["worker:9100"]
```

`scrape_interval: 15s` - частота опроса по умолчанию. Таргеты заданы статикой по docker-DNS-именам
сервисов compose (в k8s их находит `ServiceMonitor` по лейблам - этого слоя service discovery тут нет).

- **backend** (`backend:8080/metrics`) - HTTP-метрики FastAPI-сервиса (`codelens_http_*`), стадии
  ретривера (`codelens_retrieval_*`), кэш (`codelens_cache_*`). `/metrics` навешивает
  `metrics.mount`.
- **worker** (`worker:9100/metrics`) - у воркера нет HTTP-сервиса, поэтому метрики ингеста
  (`codelens_ingest_*`) отдаёт отдельный порт `9100`, поднятый `metrics.start_metrics_server`.

`job_name` тут роли почти не играет: метки `service`/`route`/`stage` приходят из самих метрик
приложения, а не из job. embedder/reranker/llm-поды в этом демо-наборе отдельными таргетами не
прописаны - их латентность видна со стороны backend как стадии ретривера (`embed`/`store`/`hyde`/
`multiquery`). Полная таблица метрик - в [`../util/observability.md`](../util/observability.md#собираемые-метрики).

## grafana/provisioning - datasource и дашборд

Каталог `provisioning/` монтируется в Grafana read-only и автонастраивает её при старте, без ручного
клика по UI. Две части: источник данных и провайдер дашбордов.

### datasources/datasource.yml

```yaml
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
```

Единственный источник - Prometheus из этого же compose (`access: proxy` - Grafana ходит к нему
сама, по серверной сети, а не из браузера). `isDefault: true` важен: панели `codelens.json` ссылаются
на дефолтный datasource, не зашивая его имя, - так один и тот же JSON-дашборд работает и тут, и в
кластере.

### dashboards/provider.yml

```yaml
apiVersion: 1
providers:
  - name: codelens
    type: file
    disableDeletion: true
    options:
      path: /var/lib/grafana/dashboards
```

Провайдер `file` подхватывает все дашборды из каталога `/var/lib/grafana/dashboards`. Туда compose
монтирует `./helm/codelens/dashboards` - тот же `codelens.json`, что кладёт ConfigMap в helm. Один
исходник дашборда на оба варианта развёртывания: правка `codelens.json` отражается и в compose, и в
кластере. `disableDeletion: true` запрещает удалить дашборд из UI - он управляется файлом.

Дашборд «CodeLens - обзор»: HTTP RPS/p95/доля 5xx, cache hit ratio, p95 стадий ретривера, глубина
очереди ингеста, p95 и rate ingest-job. Содержание панелей и примеры PromQL - в
[`../util/observability.md`](../util/observability.md#пример-promql).

## Доступ к панелям

Grafana в этом профиле открыта не напрямую, а через nginx по `/grafana` и только для роли `admin` -
гейтинг через forward-auth. Разбор reverse-proxy и проброса идентичности (`auth.proxy`) - в
[`./nginx.md`](./nginx.md).

## compose против k8s

| | compose (`panels`) | k8s (`monitoring.enabled`) |
|---|---|---|
| Кто скрейпит | статичный `prometheus.yml` (targets по DNS) | `ServiceMonitor` (отбор по лейблам) |
| Стек метрик | один `prom/prometheus` | kube-prometheus-stack (через Argo) |
| Источник Grafana | provisioning `datasource.yml` | сайдкар kube-prometheus-stack |
| Дашборд | смонтированный `codelens.json` | ConfigMap `grafana_dashboard: "1"` |
| Доступ к Grafana | nginx + forward-auth | ingress/доступ кластера |

Кластерный вариант подробно - в [`../util/observability.md`](../util/observability.md#кластер-helm--argo-cd).

## См. также

- [`../util/observability.md`](../util/observability.md) - метрики, k8s-наблюдаемость, PromQL.
- [`./nginx.md`](./nginx.md) - reverse-proxy и гейтинг доступа к Grafana.
- [`./docker-compose.md`](./docker-compose.md#nginx-grafana-prometheus-профиль-panels) - сервисный
  блок профиля `panels`.
- [`./README.md`](./README.md) - обзор deploy-обвязки.
