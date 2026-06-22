# Helm-чарт CodeLens - stateful/data-манифесты

Построчный разбор трёх data-манифестов чарта (профиль large, k8s):

- [`qdrant.yaml`](../../../../deploy/helm/codelens/templates/qdrant.yaml) - векторный стор, StatefulSet + headless Service, кластер с шардами и репликами;
- [`postgres.yaml`](../../../../deploy/helm/codelens/templates/postgres.yaml) - реляционная БД через оператор CloudNativePG (CNPG `kind: Cluster`);
- [`redis.yaml`](../../../../deploy/helm/codelens/templates/redis.yaml) - кэш, сессии и очередь RQ, одиночный под.

Stateless-сервисы (frontend/backend/embedder/llm/worker) собираются шаблоном `codelens.workload` и разобраны в [workloads.md](./workloads.md); ConfigMap/Secret/Ingress/ServiceMonitor - в [platform.md](./platform.md). Разбор values и общая структура чарта - в [chart.md](../chart.md). Bootstrap кластера и HA-топология - в рантбуках [k3s-setup.md](../../../../deploy/k3s-setup.md) (VPS, prod) и [minikube.md](../../../../deploy/minikube.md) (локальная валидация).

## Сводка

| Стор | Тип | Реплики (дефолт) | Репликация данных | Сервисы | HA |
|------|-----|------------------|-------------------|---------|-----|
| Qdrant | StatefulSet + 2 Service (headless + ClusterIP) | `qdrant.replicas: 3` | да, на уровне приложения (Raft, шарды/RF коллекции) | `-qdrant-headless` (DNS подов, p2p), `-qdrant` (http 6333) | есть при `replicas>1` |
| Postgres | CNPG `kind: Cluster` (CRD) | `postgres.instances: 3` | да, стриминг-репликация (primary → standby) | `-pg-rw` (primary), `-pg-ro` (реплики) - создаёт оператор | есть при `instances>1` |
| Redis | StatefulSet, `replicas: 1` | нет (намеренно) | нет | `-redis` (6379) | нет (потеря пода = сброс кэша/очереди) |

Все три манифеста обёрнуты в `{{- if .Values.<store>.enabled -}}` и берут префикс имени из `{{ $full := include "codelens.fullname" . }}` ([_helpers.tpl](../../../../deploy/helm/codelens/templates/_helpers.tpl)), поэтому ресурсы получают единое имя релиза: `<release>-qdrant`, `<release>-pg`, `<release>-redis`.

---

## qdrant.yaml - StatefulSet + headless Service

Один манифест-файл рендерит три объекта: headless Service, обычный ClusterIP Service и StatefulSet. Qdrant - не stateless: у него стабильная идентичность подов (нужна для Raft) и собственный том на каждый под, поэтому это StatefulSet, а не Deployment из `codelens.workload`.

### Headless Service - DNS для bootstrap

```yaml
kind: Service
metadata:
  name: {{ $full }}-qdrant-headless
spec:
  clusterIP: None
  publishNotReadyAddresses: true
  selector:
    {{- include "codelens.selectorLabels" . | nindent 4 }}
    app.kubernetes.io/component: qdrant
  ports:
    - { name: http, port: 6333 }
    - { name: p2p, port: 6335 }
```

- `clusterIP: None` делает Service headless: вместо одного виртуального IP DNS отдаёт A-записи на каждый под. Это даёт стабильные имена `<full>-qdrant-0.<full>-qdrant-headless`, `...-1`, `...-2` - по ним поды находят друг друга для Raft-консенсуса. Имя headless-сервиса завязано на `serviceName` StatefulSet (ниже).
- `publishNotReadyAddresses: true` - ключевой пункт. По умолчанию headless Service публикует DNS только готовых (Ready) подов. При сборке кластера это даёт дедлок: лидер (pod-0) должен достучаться до новой реплики по p2p-порту 6335, чтобы та синхронизировалась через Raft; но реплика не станет Ready, пока не синхронизируется (`/readyz`), а пока она не Ready - её DNS-имя не резолвится, и лидер до неё не дойдёт. Флаг публикует имена ещё не готовых подов, разрывая цикл «не Ready → нет DNS → нет синхронизации → не Ready».
- Порты: `http` (6333) - REST/healthz, `p2p` (6335) - межузловой Raft-трафик кластера.

### Обычный Service - точка входа клиентов

```yaml
kind: Service
metadata:
  name: {{ $full }}-qdrant
spec:
  selector: { ... component: qdrant }
  ports:
    - { name: http, port: 6333, targetPort: 6333 }
```

- ClusterIP-сервис с балансировкой на http-порт. На него ходит `QdrantStore` (см. [docs/stores/qdrant.md](../../../stores/qdrant.md)) по url; запрос попадает на любой под кластера, дальше Qdrant сам маршрутизирует по шардам. p2p-порт здесь не нужен - это клиентский вход, а не межузловой.

### StatefulSet - идентичность, кластер, тома

```yaml
kind: StatefulSet
spec:
  serviceName: {{ $full }}-qdrant-headless
  replicas: {{ .Values.qdrant.replicas }}
```

- `serviceName` указывает на headless Service - именно он формирует стабильный DNS подов. `replicas` берётся из values (`qdrant.replicas: 3` под три data-узла k3s).

```yaml
      {{- if gt (int .Values.qdrant.replicas) 1 }}
      affinity:
        podAntiAffinity:
          {{- if eq .Values.qdrant.antiAffinity "soft" }}
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                topologyKey: kubernetes.io/hostname
                ...
          {{- else }}
          requiredDuringSchedulingIgnoredDuringExecution:
            - topologyKey: kubernetes.io/hostname
              ...
          {{- end }}
      {{- end }}
```

- podAntiAffinity по `kubernetes.io/hostname` разносит реплики по разным узлам. Без этого копии одного шарда могут оказаться на одном узле - и при падении узла теряются обе, HA нет.
- `antiAffinity: hard` (дефолт, prod) → `requiredDuringScheduling...`: жёстко максимум 1 под Qdrant на узел. Если узлов с нужной меткой меньше, чем реплик, лишние поды зависают в Pending (см. рантбук k3s - `qdrant.replicas` = числу data-узлов).
- `antiAffinity: soft` (локалка, мало узлов) → `preferredDuringScheduling...` с `weight: 100`: планировщик предпочтёт разные узлы, но соберёт кластер и при нехватке. Блок целиком выводится только при `replicas>1` - одиночному поду anti-affinity бессмысленна.

```yaml
          {{- if gt (int .Values.qdrant.replicas) 1 }}
          env:
            - { name: QDRANT__CLUSTER__ENABLED, value: "true" }
          command: ["/bin/sh", "-c"]
          args:
            - |
              ORD="${HOSTNAME##*-}"
              HS="{{ $full }}-qdrant-headless"
              SELF="http://${HOSTNAME}.${HS}:6335"
              if [ "$ORD" = "0" ]; then
                exec /qdrant/qdrant --uri "$SELF"
              else
                exec /qdrant/qdrant --bootstrap "http://{{ $full }}-qdrant-0.${HS}:6335" --uri "$SELF"
              fi
          {{- end }}
```

- `replicas>1` включает распределённый режим: переменная `QDRANT__CLUSTER__ENABLED=true` плюс кастомный запуск. При `replicas=1` блока нет - под стартует обычным образом, без кластера (так делается smoke-тест на одном поде).
- `ORD="${HOSTNAME##*-}"` вырезает ordinal из hostname StatefulSet: под `...-qdrant-2` → `ORD=2`. Идентичность подов StatefulSet стабильна, поэтому ordinal - надёжный признак позиции пода.
- pod-0 (`ORD=0`) запускается с `--uri` на себя и поднимает Raft-консенсус первым - это seed кластера.
- pod-N (`ORD>0`) запускается с `--bootstrap` на адрес pod-0 по p2p (6335) и присоединяется к уже поднятому консенсусу. `--uri` - собственный адрес, по которому его будут видеть пиры.
- Именно на этой связке работает `publishNotReadyAddresses`: bootstrap идёт по DNS-именам подов, в т.ч. ещё не Ready. Сборку кластера имеет смысл проверять на живом k8s (см. [minikube.md](../../../../deploy/minikube.md): `…/cluster` должен отдать 3 пира).

```yaml
          readinessProbe:
            httpGet: { path: /readyz, port: 6333 }
            initialDelaySeconds: 10
          volumeMounts:
            - { name: storage, mountPath: /qdrant/storage }
  volumeClaimTemplates:
    - metadata: { name: storage }
      spec:
        accessModes: ["ReadWriteOnce"]
        {{- if .Values.qdrant.storageClass }}
        storageClassName: {{ .Values.qdrant.storageClass | quote }}
        {{- end }}
        resources:
          requests:
            storage: {{ .Values.qdrant.storage }}
```

- `readinessProbe` на `/readyz` - тот самый сигнал, который при дефолтном headless-сервисе блокировал бы bootstrap (см. выше).
- `volumeClaimTemplates` - в StatefulSet это шаблон PVC на каждый под: у каждой реплики свой том `storage` (50Gi по дефолту), переживающий перезапуск пода. `accessModes: ReadWriteOnce` - том привязан к одному узлу, что согласовано с anti-affinity «1 под/узел».
- `storageClass: ""` (дефолт) → класс не задаётся, берётся дефолтный кластера. В k3s это `local-path` (локальный диск узла) - этого достаточно: Qdrant реплицирует данные сам на уровне приложения, сетевое хранилище (Longhorn) было бы лишней второй репликацией (см. рантбук [k3s-setup.md](../../../../deploy/k3s-setup.md)).

### Шардирование коллекции отдельно от числа подов

Число подов (`qdrant.replicas`) и фактор шардирования коллекции - разные вещи:

- `qdrant.replicas` (этот манифест) - сколько узлов в кластере Qdrant.
- `config.vector.shards` и `config.vector.replicas` (блок `config` в [values.yaml](../../../../deploy/helm/codelens/values.yaml), дефолт `shards: 2`, `replicas: 2`) - как именно коллекция бьётся на шарды и сколько копий каждого шарда хранится. Эти значения уходят в ConfigMap и применяются кодом при создании коллекции (`shard_number`/`replication_factor`, см. [docs/stores/qdrant.md](../../../stores/qdrant.md)).

То есть кластер из 3 подов может держать коллекцию с 2 шардами и фактором репликации 2: данные распределяются и дублируются между узлами, а не «один под = один шард». RF=2 означает, что каждый шард есть на двух узлах - падение одного узла не теряет данные.

---

## postgres.yaml - CNPG `kind: Cluster`

Postgres не разворачивается как «голый» StatefulSet. Манифест декларирует ресурс оператора CloudNativePG (CNPG) - сам StatefulSet, тома, репликацию, сервисы и failover делает оператор. Предусловие: оператор cnpg установлен в кластере (ставится `make mk-start` локально; на VPS - отдельным шагом, см. рантбуки).

### Secret - учётка владельца БД

```yaml
kind: Secret
metadata:
  name: {{ $full }}-pg-app
type: kubernetes.io/basic-auth
stringData:
  username: {{ .Values.postgres.user | quote }}
  password: {{ .Values.postgres.password | quote }}
```

- Secret типа `basic-auth` с логином/паролем владельца БД. На него ссылается `bootstrap.initdb.secret` ниже; те же значения попадают в `DATABASE_DSN` приложения. В prod пароль (`postgres.password`, дев-дефолт `codelens`) заменяется через secrets/external-secrets.

### Cluster - декларация HA-инстанса

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: {{ $full }}-pg
spec:
  instances: {{ .Values.postgres.instances }}
```

- `apiVersion: postgresql.cnpg.io/v1` - это CRD оператора, без установленного CNPG объект не примется. На minikube для статической валидации (`kubeconform`) схему CRD надо подсунуть отдельно - см. [minikube.md](../../../../deploy/minikube.md).
- `instances: 3` (дефолт) = primary + 2 standby. Оператор сам поднимает один primary и N-1 реплик со стриминг-репликацией (WAL течёт с primary на standby), сам выбирает узлы и сам проводит failover при потере primary.

```yaml
  {{- if or .Values.postgres.nodeSelector .Values.postgres.tolerations }}
  affinity:
    {{- with .Values.postgres.nodeSelector }}
    nodeSelector:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- with .Values.postgres.tolerations }}
    tolerations:
      {{- toYaml . | nindent 6 }}
    {{- end }}
  {{- end }}
```

- В отличие от Qdrant, podAntiAffinity тут не пишется руками: CNPG по умолчанию (`enablePodAntiAffinity=true`) сам разносит инстансы по разным узлам. Чарт задаёт лишь `nodeSelector` (прибить к data-узлам, метка `codelens.io/pool: data`) и `tolerations` (пройти taint'ы) - и только если они заданы в values.

```yaml
  storage:
    size: {{ .Values.postgres.storage }}
    {{- if .Values.postgres.storageClass }}
    storageClass: {{ .Values.postgres.storageClass | quote }}
    {{- end }}
  bootstrap:
    initdb:
      database: {{ .Values.postgres.database }}
      owner: {{ .Values.postgres.user }}
      secret:
        name: {{ $full }}-pg-app
```

- `storage` - размер тома на каждый инстанс (20Gi дефолт); `storageClass: ""` → дефолтный (local-path в k3s, как у Qdrant: CNPG реплицирует сам, сетевой том избыточен).
- `bootstrap.initdb` - первичная инициализация: создать БД `codelens`, владелец `codelens`, пароль из Secret `-pg-app`. Выполняется один раз при создании кластера.

### Сервисы оператора (-rw / -ro)

Сервисы создаёт сам CNPG, в манифесте их нет:

- `<full>-pg-rw` - всегда указывает на текущий primary. Сюда ходят запись, DDL и миграции.
- `<full>-pg-ro` - балансирует по standby-репликам (read-only).

Приложение по умолчанию собирает DSN на `-pg-rw` (см. `secrets.databaseDsn: ""` в values → собирается из `postgres.*` на сервис `-pg-rw`). При failover оператор переключает `-pg-rw` на нового primary - строка подключения не меняется.

---

## redis.yaml - одиночный (кэш + сессии + очередь RQ)

Redis обслуживает три роли сразу: кэш ответов, пользовательские сессии и очередь задач ingest (RQ, `config.jobs.kind: redis`). Развёрнут одним подом и намеренно не реплицируется.

```yaml
kind: StatefulSet
metadata:
  name: {{ $full }}-redis
spec:
  serviceName: {{ $full }}-redis
  replicas: 1
```

- `replicas: 1` зашит в шаблоне (не из values) - один под без HA. StatefulSet, а не Deployment, выбран ради стабильного имени и привязанного PVC (`volumeClaimTemplates`), чтобы данные пережили перезапуск пода.

```yaml
      containers:
        - name: redis
          image: {{ .Values.redis.image }}
          args: ["--appendonly", "yes"]
          ports:
            - { name: redis, containerPort: 6379 }
          readinessProbe:
            tcpSocket: { port: 6379 }
            initialDelaySeconds: 5
          volumeMounts:
            - { name: data, mountPath: /data }
```

- `--appendonly yes` включает AOF-персистентность: команды пишутся в журнал на томе `/data`, перезапуск пода не теряет данные. PVC `data` (5Gi, `volumeClaimTemplates` ниже) хранит этот журнал.
- `readinessProbe` - простой tcp-чек порта 6379 (Redis не отдаёт http-healthz).

```yaml
kind: Service
metadata:
  name: {{ $full }}-redis
spec:
  selector: { ... component: redis }
  ports:
    - { port: 6379, targetPort: 6379 }
```

- Обычный ClusterIP-сервис на 6379; backend и worker подключаются по имени `<full>-redis`.

### Почему без репликации

Решение осознанное под профиль large на самоуправляемом кластере:

- потеря пода Redis = сброс кэша (пересчитается) и потеря незавершённых ingest-задач в очереди RQ (индексацию можно перезапустить);
- ни кода, ни постоянных данных в Redis нет - всё это в Qdrant и Postgres, которые реплицируются;
- кластер Redis (Sentinel/Cluster) добавил бы заметную сложность ради защиты эфемерных данных, что для этого профиля не оправдано.

Если в дальнейшем сессии или очередь станут критичными, Redis выносится в HA-режим отдельно - текущий манифест этого сознательно не делает.
