# Наблюдаемость (метрики Prometheus + Grafana)

Разбор `src/util/metrics.py`, точек инструментирования и k8s-обвязки.

## Устройство

Один модуль метрик на процесс (`src/util/metrics.py`) с мягкой деградацией: если пакет
`prometheus_client` не установлен (профиль small/dev без extra `scale`/`inference`/`llm`), все
хелперы превращаются в no-op, а `/metrics` не монтируется. Модуль импортируется
откуда угодно без оговорок - backend, inference, llm-gateway, worker, in-process ingest.

`prometheus_client` входит в extra `scale`, `inference`, `llm` → в large-образах он есть, метрики
включаются автоматически. В dev (`make run`) его нет → накладных расходов ноль.

## Собираемые метрики

| Метрика | Тип | Лейблы | Точка инструментирования |
|---|---|---|---|
| `codelens_http_requests_total` | counter | `service`, `method`, `route`, `status` | HTTP-middleware (`metrics.mount`) во всех FastAPI-сервисах |
| `codelens_http_request_duration_seconds` | histogram | `service`, `method`, `route` | то же middleware |
| `codelens_retrieval_stage_duration_seconds` | histogram | `stage` | `HybridRetriever._search` - `embed`/`store`/`bm25`/`hyde`/`multiquery`/`rerank`/`mmr` |
| `codelens_cache_total` | counter | `result` (`hit`/`miss`) | `cache_get_or_set` в `persistence/cache.py` |
| `codelens_ingest_job_duration_seconds` | histogram | `status` (`done`/`failed`) | `ingest/runner.run_ingest` |
| `codelens_ingest_queue_jobs` | gauge | `state` (`queued`/`started`/`failed`) | фоновый поток воркера (`worker_app`) |

Ошибки видны без отдельной метрики - как доля `codelens_http_requests_total{status=~"5.."}`.
Латентность embedder/llm-подов покрыта их же HTTP-метриками (`/embed`, `/rerank`, `/chat`), а с точки
зрения backend - стадиями ретривера `embed`/`store`/`hyde`/`multiquery`.

## Экспозиция `/metrics`

- backend / inference / llm - FastAPI. `metrics.mount(app, "<service>")` навешивает HTTP-middleware
  (латентность+счётчик по шаблону маршрута, не сырому пути - иначе взрыв кардинальности по id)
  и отдаёт `/metrics` на основном порту сервиса.
- worker - HTTP-сервиса нет, поэтому `metrics.start_metrics_server(METRICS_PORT)` поднимает
  отдельный порт (дефолт `9100`), а фоновый поток раз в 5с кладёт глубину очереди RQ в gauge
  (Prometheus читает её при скрейпе).

Реестр процессный; поды запускаются с 1 uvicorn-воркером (масштаб - репликами пода), поэтому
multiprocess-режим prometheus не нужен.

## Кластер (Helm + Argo CD)

Всё под флагом `monitoring.enabled` (по умолчанию `false` - включать на кластере с установленным
Prometheus Operator):

- `templates/servicemonitor.yaml` - `ServiceMonitor`, отбирающий Service'ы с лейблом
  `codelens.io/scrape: "true"` (его получают только сервисы с `metrics: true` в values:
  backend, embedder, reranker, llm + отдельный Service воркера). Скрейп - порт `http`, путь `/metrics`.
  Frontend (Streamlit, без `/metrics`) лейбла не имеет и не скрейпится.
- `templates/grafana-dashboard.yaml` - ConfigMap с дашбордом (`dashboards/codelens.json`),
  помеченный `grafana_dashboard: "1"`; grafana-сайдкар kube-prometheus-stack импортирует его сам.
- `deploy/gitops/application-monitoring.yaml` - Argo-приложение, ставящее
  `kube-prometheus-stack` (Prometheus + Grafana + Alertmanager + node-exporter + kube-state-metrics)
  в namespace `monitoring`. Стек настроен скрейпить все ServiceMonitor'ы (для демо), grafana-сайдкар
  ищет дашборды во всех namespace.

Селектор оператора: kube-prometheus-stack по умолчанию берёт ServiceMonitor'ы только со своим
release-лейблом. В `application-monitoring.yaml` это ослаблено (`serviceMonitorSelectorNilUsesHelmValues:
false` → берёт все). На проде можно ужесточить обратно и проставить
`monitoring.serviceMonitor.labels: { release: kube-prometheus-stack }`.

## Локальная проверка (kind)

```bash
# 1. Стек мониторинга (один раз на кластер)
kubectl apply -f deploy/gitops/application-monitoring.yaml      # через Argo
# или напрямую helm-ом:
# helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack -n monitoring --create-namespace

# 2. CodeLens с включёнными метриками
helm upgrade --install codelens deploy/helm/codelens -n codelens-staging \
  --set monitoring.enabled=true

# 3. Проверить экспозицию
kubectl -n codelens-staging port-forward svc/codelens-backend 8080:8080
curl -s localhost:8080/metrics | grep codelens_

# 4. Цели Prometheus и дашборд Grafana
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090
#   → Status → Targets: codelens-* должны быть UP
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80
#   → Dashboards → «CodeLens - обзор»
```

Дашборд «CodeLens - обзор» (`dashboards/codelens.json`): HTTP RPS/p95/доля 5xx, cache hit ratio,
p95 стадий ретривера, глубина очереди ingest, p95 и rate ingest-job.

## Пример PromQL

```promql
# p95 латентности поиска по маршруту backend
histogram_quantile(0.95, sum by (le, route) (rate(codelens_http_request_duration_seconds_bucket{service="backend"}[5m])))

# cache hit ratio за 15 минут
sum(rate(codelens_cache_total{result="hit"}[15m])) / sum(rate(codelens_cache_total[15m]))

# самая медленная стадия ретривера (p95)
topk(1, histogram_quantile(0.95, sum by (le, stage) (rate(codelens_retrieval_stage_duration_seconds_bucket[5m]))))

# очередь ingest растёт → не хватает воркеров
sum(codelens_ingest_queue_jobs{state="queued"})
```
