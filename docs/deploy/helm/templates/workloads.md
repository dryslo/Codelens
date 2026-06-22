# helm/templates - stateless-манифесты (workloads)

Поузловой разбор шаблонов stateless-сервисов чарта CodeLens: `frontend`, `backend`,
`embedder`, `reranker`, `llm`, `worker`. Все они - Deployment'ы без своего тома данных
(стейт в Qdrant/Postgres/Redis, разбор соседей в [./data.md](./data.md)). Профили запуска
и устройство `values.yaml` - в [../chart.md](../chart.md), общая платформа (Ingress,
ServiceMonitor, секреты) - в [./platform.md](./platform.md).

Пять из шести манифестов - однострочники: вся логика вынесена в общий хелпер
`codelens.workload`, файл-обёртка лишь передаёт в него имя компонента и его блок values.
Исключение - `worker.yaml`, расписанный целиком (у воркера нет публичного Service, и форма
отличается). Поэтому ниже сначала разбирается хелпер, затем - что меняет каждый блок values,
затем особняком worker.

## Сводка

| Манифест | Источник | Рендерит | Ключевые тумблеры values |
| --- | --- | --- | --- |
| [frontend.yaml](../../../../deploy/helm/codelens/templates/frontend.yaml) | хелпер `codelens.workload` | Deployment + Service + HPA | `frontend.enabled`, `frontend.hpa.enabled` |
| [backend.yaml](../../../../deploy/helm/codelens/templates/backend.yaml) | хелпер `codelens.workload` | Deployment + Service (`/metrics`) + HPA | `backend.enabled`, `backend.metrics`, `backend.hpa.enabled` |
| [embedder.yaml](../../../../deploy/helm/codelens/templates/embedder.yaml) | хелпер `codelens.workload` | Deployment + Service (`/metrics`) + HPA | `embedder.enabled`, `embedder.nodeSelector` |
| [reranker.yaml](../../../../deploy/helm/codelens/templates/reranker.yaml) | хелпер `codelens.workload` | Deployment + Service (`/metrics`) [+ HPA] | `reranker.enabled` (вместе с `config.reranker.enabled`) |
| [llm.yaml](../../../../deploy/helm/codelens/templates/llm.yaml) | хелпер `codelens.workload` | Deployment + Service (`/metrics`) + HPA | `llm.enabled`, ключи провайдеров из секрета |
| [worker.yaml](../../../../deploy/helm/codelens/templates/worker.yaml) | inline-шаблон | Deployment + Service (только `/metrics`) | `worker.enabled` (нужен при `config.jobs.kind=redis`) |

Все сервис-доки приложений: [../../../services/backend-app.md](../../../services/backend-app.md),
[../../../services/inference-app.md](../../../services/inference-app.md),
[../../../services/llm-app.md](../../../services/llm-app.md).

## Общий хелпер `codelens.workload`

Файл [frontend.yaml](../../../../deploy/helm/codelens/templates/frontend.yaml) целиком:

```yaml
{{- include "codelens.workload" (dict "root" . "name" "frontend" "spec" .Values.frontend) }}
```

Хелпер получает словарь из трёх ключей: `root` - корневой контекст (`.`, нужен ради
`.Values`, `.Release`, `.Chart`), `name` - имя компонента (идёт в `app.kubernetes.io/component`
и в имя ресурсов), `spec` - соответствующий блок values. `backend.yaml`, `embedder.yaml`,
`reranker.yaml`, `llm.yaml` отличаются только парой `name`/`.Values.<...>` - один и тот же
шаблон с разными данными, поэтому формы Deployment/Service/HPA у них идентичны.

Тело хелпера (`_helpers.tpl`) распаковывает аргументы и считает полное имя:

```yaml
{{- define "codelens.workload" -}}
{{- $ := .root -}}
{{- $spec := .spec -}}
{{- $name := .name -}}
{{- $full := printf "%s-%s" (include "codelens.fullname" $) $name -}}
{{- if $spec.enabled }}
```

- `$full` = `<release>-codelens-<name>` (например `codelens-backend`) - это и DNS-имя Service,
  на которое ссылаются URL'ы в ConfigMap (`http://<full>-backend:8080` и т.п.).
- Весь рендер обёрнут в `if $spec.enabled` - выключенный компонент (`reranker.enabled: false`
  по умолчанию) не даёт ни одного объекта.

### Deployment

```yaml
spec:
  replicas: {{ $spec.replicas }}
  selector:
    matchLabels:
      {{- include "codelens.selectorLabels" $ | nindent 6 }}
      app.kubernetes.io/component: {{ $name }}
  template:
    metadata:
      labels:
        {{- include "codelens.selectorLabels" $ | nindent 8 }}
        app.kubernetes.io/component: {{ $name }}
```

- `replicas` берётся из блока сервиса (frontend/backend/embedder/llm - 2, reranker - 0).
- Селектор = общие `selectorLabels` (`app.kubernetes.io/name` + `instance`) плюс
  `component: <name>`. Лейбл компонента разводит поды одного релиза: без него селекторы
  backend и frontend совпали бы.

Размещение - опциональные `nodeSelector`/`tolerations`/`affinity`, каждый рендерится только
если задан в values:

```yaml
      {{- with $spec.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $spec.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $spec.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
```

В дефолтных values все три пустые (`{}`/`[]`), поэтому в smoke-кластере поды садятся куда
угодно. На большом кластере их задают точечно: `embedder.nodeSelector: { codelens.io/pool:
heavy }` уводит эмбеддер на ноды с памятью/GPU (профиль ресурсов у моделей другой, см.
[../../../services/inference-app.md](../../../services/inference-app.md)); для llm-пула
размещение в EU (`codelens.io/region: eu` + соответствующий toleration) держит шлюз и трафик
к провайдерам в нужной юрисдикции. Сам хелпер региона/пула не знает - это чисто данные values.

### Контейнер: образ, env, проба

```yaml
      containers:
        - name: {{ $name }}
          image: {{ include "codelens.image" (dict "root" $ "name" $spec.image) }}
          imagePullPolicy: {{ $.Values.image.pullPolicy }}
          ports:
            - containerPort: {{ $spec.port }}
          envFrom:
            - secretRef:
                name: {{ include "codelens.secretName" $ }}
          env:
            - name: CODELENS_CONFIG
              value: /app/config/config.yaml
            {{- range $spec.env }}
            - name: {{ .name }}
              value: {{ .value | quote }}
            {{- end }}
```

- `codelens.image` собирает `registry/<image>:tag` из глобального `image.*`; `$spec.image` -
  имя образа компонента (`frontend`, `backend`, `inference`, `llm`). Embedder и reranker
  делят один образ `inference` - роль внутри задаётся через env (ниже).
- `envFrom: secretRef` подмешивает в контейнер все ключи из общего Secret чарта целиком
  (`DATABASE_DSN`, `JWT_SECRET`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `ADMIN_*`, `HF_TOKEN`).
  Так каждый под получает значения для подстановки `${VAR}` из `config.yaml`, не перечисляя
  ключи поимённо. Разбор секрета - в [./platform.md](./platform.md).
- `CODELENS_CONFIG` указывает на смонтированный `config.yaml` (один файл из ConfigMap, ниже).
- `env` из values добавляет специфичные переменные - это главный различитель компонентов:
  - `frontend`: `ROLE=frontend`;
  - `backend`: `ROLE=backend` (config.yaml читает `role: ${ROLE:-backend}`, по роли процесс
    выбирает точку входа);
  - `embedder`: `INFERENCE_ROLE=embed` + `EMBEDDER_MODEL=...` - под образа `inference`
    поднимает только эмбеддер;
  - `reranker`: `INFERENCE_ROLE=rerank` + `RERANKER_MODEL=...` - тот же образ, но только
    кросс-энкодер (логика разделения - в [../../../services/inference-app.md](../../../services/inference-app.md));
  - `llm`: `env: []` - шлюзу спецпеременные не нужны, провайдерские ключи он берёт из секрета
    по `api_key_env` (`GROQ_API_KEY` и т.п.).

URL'ы между сервисами задаются не здесь, а в ConfigMap, и собираются из тех же `$full`-имён:

```yaml
    embedder_url:  http://{{ $full }}-embedder:{{ .Values.embedder.port }}
    reranker_url:  http://{{ $full }}-reranker:{{ .Values.reranker.port }}
    llm:
      kind: {{ .Values.config.llm.kind }}        # remote -> ходить в шлюз
      llm_url: http://{{ $full }}-llm:{{ .Values.llm.port }}
```

При `config.embedder.kind=remote` / `config.llm.kind=remote` backend и ретривер не грузят
модели в процесс, а ходят по этим внутренним адресам в embedder/reranker/llm-поды. Имена
адресов совпадают с именами Service, которые рендерит этот же хелпер.

Проба готовности - только если у компонента задан `healthPath`:

```yaml
          {{- if $spec.healthPath }}
          readinessProbe:
            httpGet:
              path: {{ $spec.healthPath }}
              port: {{ $spec.port }}
            initialDelaySeconds: 10
            periodSeconds: 10
          {{- end }}
```

- Пробы readiness, а не liveness: цель - не слать трафик на ещё не прогревшийся под (модели
  и пайплайн грузятся на старте), а не перезапускать живой. Перезапуск тяжёлого пода по
  ложному liveness обошёлся бы дороже, чем временное снятие из Endpoints.
- `initialDelaySeconds: 10` даёт фору на старт; `periodSeconds: 10` - частота опроса.
- Пути по компонентам: frontend - `/_stcore/health` (служебный health Streamlit), backend -
  `/healthz`, embedder/reranker/llm - `/healthz` своих сервисов. У всех проба бьёт в основной
  `port`.

### Ресурсы и монтирование config

```yaml
          {{- with $spec.resources }}
          resources:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          volumeMounts:
            - name: config
              mountPath: /app/config/config.yaml
              subPath: config.yaml
      volumes:
        - name: config
          configMap:
            name: {{ include "codelens.fullname" $ }}-config
```

- `resources` рендерится как есть из values. Профили различаются: backend - `requests
  cpu 500m/512Mi`, `limits 2/2Gi`; embedder заметно тяжелее - `requests 1/2Gi`, `limits
  4/6Gi` (модель в памяти); у frontend/reranker/llm в дефолте `{}` (без лимитов в smoke).
- ConfigMap `<full>-config` монтируется через `subPath` одним файлом в `config.yaml` - тот
  самый путь из `CODELENS_CONFIG`. Один и тот же ConfigMap у всех подов, расхождение задаётся
  только через `ROLE`/`INFERENCE_ROLE`.

### Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ $full }}
  labels:
    ...
    {{- if $spec.metrics }}
    codelens.io/scrape: "true"          # selector для ServiceMonitor: только сервисы с /metrics
    {{- end }}
spec:
  selector:
    {{- include "codelens.selectorLabels" $ | nindent 4 }}
    app.kubernetes.io/component: {{ $name }}
  ports:
    - name: http                        # именованный порт - на него ссылается ServiceMonitor
      port: {{ $spec.port }}
      targetPort: {{ $spec.port }}
```

- ClusterIP по умолчанию: имя `$full` - это и есть DNS, по которому ходят URL'ы из ConfigMap
  и Ingress (внешний доступ к frontend/backend - через Ingress, см. [./platform.md](./platform.md)).
- Лейбл `codelens.io/scrape: "true"` ставится только при `$spec.metrics: true` (backend,
  embedder, reranker, llm). По нему ServiceMonitor отбирает, что скрейпить; у frontend
  `metrics` не задан, лейбла нет. Порт назван `http` - именно на имя ссылается ServiceMonitor.

### HPA

```yaml
{{- if and $spec.hpa $spec.hpa.enabled }}
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
spec:
  scaleTargetRef:
    kind: Deployment
    name: {{ $full }}
  minReplicas: {{ $spec.hpa.minReplicas }}
  maxReplicas: {{ $spec.hpa.maxReplicas }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ $spec.hpa.cpu }}
{{- end }}
```

- Рендерится только при `$spec.hpa.enabled`. Метрика одна - средняя утилизация CPU (требует
  metrics-server в кластере); на smoke-кластере HPA выключают `--set backend.hpa.enabled=false`.
- Диапазоны по компонентам: frontend `2..4`, backend `2..8`, embedder `2..6`, llm `2..6`,
  все с порогом `cpu: 70`. У reranker `hpa.enabled: false` (под включается отдельно, см. ниже).

## reranker - под под тумблером

`reranker.yaml` идёт через тот же хелпер, но в дефолте отключён:

```yaml
reranker:
  enabled: false          # включается вместе с config.reranker.enabled
  replicas: 0
  image: inference
  env:
    - { name: INFERENCE_ROLE, value: rerank }
    - { name: RERANKER_MODEL, value: BAAI/bge-reranker-v2-m3 }
  hpa: { enabled: false }
```

- При `reranker.enabled: false` хелпер не рендерит ничего (Deployment/Service/HPA отсутствуют).
- Включать нужно согласованно: `reranker.enabled: true` (поднять под) вместе с
  `config.reranker.enabled: true` и флагом `config.retrieval.flags.rerank` (включить шаг в
  пайплайне). Иначе под либо есть, но не используется, либо пайплайн ждёт сервис, которого нет.
- Образ тот же `inference`, что у эмбеддера; различие - `INFERENCE_ROLE=rerank`: один под
  держит только кросс-энкодер, масштабируется отдельно от эмбеддера.

## llm - шлюз провайдеров

`llm.yaml` - стандартный workload (`image: llm`, `port: 8001`, HPA `2..6`), специфика - в env
и секрете. `env: []`: переменные роли не нужны, ключи провайдеров приходят через `envFrom`
из секрета (`GROQ_API_KEY`, `GEMINI_API_KEY`), а какой ключ под каким именем берётся - задаёт
`api_key_env` провайдера в ConfigMap:

```yaml
    llm:
      providers:
        "Groq Llama 3.3 70B":
          api_key_env: GROQ_API_KEY
```

backend/ретривер ходят в шлюз по `llm_url` (`http://<full>-llm:8001`) при `config.llm.kind=remote`.
Размещение шлюза - кандидат на EU-пул через `llm.nodeSelector`/`tolerations`: трафик к внешним
LLM-провайдерам выходит из нод нужного региона. Контракт сервиса - в
[../../../services/llm-app.md](../../../services/llm-app.md).

## worker - Deployment без публичного Service

`worker.yaml` расписан inline (не через хелпер), потому что форма принципиально другая: воркер
не обслуживает HTTP-трафик, а тянет задачи ingest из очереди Redis (RQ, `index_path`). Поэтому
у него нет ни публичного Service, ни readiness-пробы.

```yaml
{{- if .Values.worker.enabled -}}
# Воркер ingest (RQ): тянет задачи из Redis, исполняет index_path. Без Service/порта.
kind: Deployment
spec:
  replicas: {{ .Values.worker.replicas }}
```

- Нужен только при `config.jobs.kind=redis` (профиль large); в small/dev ingest идёт inprocess
  и воркер не разворачивают. Параллелизм - числом реплик (опц. KEDA по длине очереди).

Единственный порт - не для трафика, а для скрейпа метрик:

```yaml
          ports:
            - name: http                       # /metrics (у воркера нет HTTP-сервиса - отдельный порт)
              containerPort: {{ .Values.worker.metricsPort }}
          envFrom:
            - secretRef:
                name: {{ include "codelens.secretName" . }}
          env:
            - name: CODELENS_CONFIG
              value: /app/config/config.yaml
            - name: METRICS_PORT
              value: {{ .Values.worker.metricsPort | quote }}
```

- `envFrom: secretRef` тот же, что у остальных подов (воркеру нужны `DATABASE_DSN`, `HF_TOKEN`
  и т.п. для индексации). Своего `ROLE` нет - точка входа воркера фиксирована.
- `METRICS_PORT` (по умолчанию `9100`) поднимает отдельный HTTP-эндпоинт `/metrics` сбоку от
  основного цикла обработки очереди.

Второй объект - ClusterIP Service исключительно под скрейп:

```yaml
# Service только для скрейпа /metrics воркера (ClusterIP, без нагрузочного трафика).
kind: Service
metadata:
  labels:
    ...
    codelens.io/scrape: "true"
spec:
  ports:
    - name: http
      port: {{ .Values.worker.metricsPort }}
      targetPort: {{ .Values.worker.metricsPort }}
```

- Лейбл `codelens.io/scrape: "true"` и порт `http` - те же конвенции, что у обычных сервисов,
  чтобы ServiceMonitor подхватил воркер так же, как backend/embedder. Балансировки запросов
  этот Service не выполняет - в него никто не ходит, кроме Prometheus.

Монтирование config (`<full>-config` → `/app/config/config.yaml` через `subPath`) и
`nodeSelector` - как у остальных. HPA у воркера нет (масштабирование по длине очереди - дело
KEDA, не CPU-HPA).
