# Развёртывание CodeLens large на k3s (самоуправляемые VPS, HA-данные)

Цель: рабочая репликация и шардирование (Qdrant-кластер и CNPG-Postgres) плюс выделенные узлы под
embedder и llm.

## Топология (6 узлов: prod + staging на одном кластере)

| Узел | k3s-роль | Назначение | vCPU/RAM/диск | Где | Метки / taint'ы |
|------|----------|-----------|---------------|-----|-----------------|
| `node-s1` | **server** (`--cluster-init`) | prod data-тир + control-plane | 4 / 6 ГБ / 80 ГБ | РФ | `codelens.io/pool=data` |
| `node-s2` | **server** | prod data-тир + control-plane | 4 / 6 ГБ / 80 ГБ | РФ | `codelens.io/pool=data` |
| `node-s3` | **server** | prod data-тир + control-plane | 4 / 6 ГБ / 80 ГБ | РФ | `codelens.io/pool=data` |
| `node-heavy` | agent | prod embedder (e5-large на CPU) | 4 / 8 ГБ / 90 ГБ | РФ | `codelens.io/pool=heavy`, taint `dedicated=embedder:PreferNoSchedule` |
| `node-dev` | agent | **весь staging-стек** (своё окружение) | 4 / 8 ГБ / 90 ГБ | РФ | `codelens.io/env=staging`, taint `dedicated=staging:NoSchedule` |
| `node-eu` | agent | llm-gateway (внешние API; prod + staging) | 4 / 6 ГБ / 80 ГБ | **KZ** | `geo=eu`, taint `dedicated=llm:NoSchedule` |

Числа RAM/диска отражают демо-парк (под нагрузку не масштабируется): `values-k3s.yaml` урезает
память stateless и потолки HPA под 6 ГБ data-узлы и ставит лимит embedder 5Gi под 8 ГБ heavy-узел;
диски qdrant/postgres - 25Gi/10Gi под 80 ГБ. Для боевой нагрузки узлы берутся крупнее, а трим
убирается.

**prod и staging - одно окружение каждый, но один кластер.** prod (namespace `codelens-prod`,
ветка main) размазан по 3 data + heavy + EU. staging (namespace `codelens-staging`, ветка dev)
целиком сидит на `node-dev`: свой embedder, qdrant-1, postgres-1, redis, worker, stateless. Общий
у них только EU-узел: оба llm-пода (по сервису на namespace) едут на `geo=eu`, т.к. только оттуда
доступны Groq/Gemini. taint `dedicated=staging:NoSchedule` держит prod-поды прочь от `node-dev`,
а `nodeSelector pool=data` у prod-stateless - прочь от staging-узла.

**Почему 3 server-узла, а не 1 control-plane + 3 data.** Один control-plane - единая точка отказа
управления кластером. Поскольку data-узлов всё равно три, они же выступают k3s-серверами с embedded
etcd (нечётное число -> кворум): HA получают и control-plane, и данные одним парком, без отдельного
SPOF-узла. На них же работают qdrant/postgres/redis и лёгкий stateless (frontend/backend/worker).
*(Вариант «1 cp + 3 агента-данных» возможен, но cp останется SPOF; data-узлов тогда 3
агента с той же меткой `pool=data`.)*

**Почему local-path, а не Longhorn.** Qdrant-кластер (шард 2 / реплика 2) и CNPG (primary + 2 standby)
реплицируют сами на уровне приложения. Каждому поду нужен только свой локальный диск
(`storageClass: ""` -> k3s `local-path`). Longhorn здесь - лишняя вторая репликация поверх первой;
он нужен только одиночным stateful без собственной репликации.

**Почему `qdrant.replicas` = числу data-узлов.** podAntiAffinity жёсткая (копии шардов обязаны быть на
разных узлах). При числе реплик больше, чем узлов с меткой `pool=data`, лишний под зависнет в Pending.
В `values-k3s.yaml` стоит `qdrant.replicas: 3` под три data-узла.

`llm` отделён в EU, т.к. ходит во внешние Groq/Gemini и не хранит ни кода, ни чатов - за границу
уходит только текст запроса. Все данные (индекс, БД, пользователи) остаются в РФ.

---

## 1. Сеть кластера и firewall

Сеть строится до k3s: 5 РФ-узлов - в приватной сети провайдера, `node-eu` (KZ) - через границу по
AmneziaWG (обфускация против DPI). Полный разбор и генератор конфигов - [`amnezia/README.md`](amnezia/README.md).
Подними его сначала; дальше k3s едет поверх.

Firewall:
- между публичными IP (KZ ↔ каждый РФ-узел): `51820/udp` (AmneziaWG);
- в приватной сети (РФ↔РФ): `6443/tcp`, `2379-2380/tcp` (server-узлы), `8472/udp` (vxlan), `10250/tcp`;
- наружу публично: `80/443` только на data-узлах, куда смотрит DNS.

Служебные порты k3s наружу не открываются: РФ↔РФ они в приватной сети, KZ↔РФ - внутри awg-туннеля.

## 2. Установить k3s (HA поверх гибридной сети)

Кластерный трафик идёт по приватным IP (РФ, подсеть `10.16.0.0/24`, интерфейс `ens9`) и tunnel-IP
`10.10.0.6` (KZ): задаём через `--node-ip`/`--flannel-iface`. Публичный IP остаётся только для
ingress (`--node-external-ip`).

| Узел | приватный/tunnel IP (`--node-ip`) | публичный IP (`--node-external-ip`) |
|------|-----------------------------------|-------------------------------------|
| node-s1 | 10.16.0.2 | 159.194.229.34 |
| node-s2 | 10.16.0.3 | 159.194.235.78 |
| node-s3 | 10.16.0.4 | 31.207.76.197 |
| node-heavy | 10.16.0.5 | 85.198.66.196 |
| node-dev | 10.16.0.1 | 85.198.68.29 |
| node-eu | 10.10.0.6 (awg0) | 178.236.17.61 |

**server #1 (node-s1):**
```bash
curl -sfL https://get.k3s.io | sh -s - server --cluster-init --node-name node-s1 \
  --node-ip 10.16.0.2 --flannel-iface ens9 \
  --node-external-ip 159.194.229.34 --tls-san 159.194.229.34 --tls-san 10.16.0.2
sudo cat /var/lib/rancher/k3s/server/node-token        # токен для остальных
```
**server #2 (node-s2) и #3 (node-s3):** то же, добавив `--server https://10.16.0.2:6443 --token <ТОКЕН>`
и свои `--node-name`/`--node-ip`/`--node-external-ip`/`--tls-san` (например node-s2: `--node-ip 10.16.0.3`,
`--node-external-ip 159.194.235.78`, `--tls-san 159.194.235.78 --tls-san 10.16.0.3`).

**агенты РФ (node-heavy, node-dev):**
```bash
# node-heavy:
curl -sfL https://get.k3s.io | K3S_URL=https://10.16.0.2:6443 K3S_TOKEN=<ТОКЕН> \
  sh -s - agent --node-name node-heavy --node-ip 10.16.0.5 --flannel-iface ens9 \
  --node-external-ip 85.198.66.196
# node-dev: --node-name node-dev --node-ip 10.16.0.1 --node-external-ip 85.198.68.29
```
**агент KZ (node-eu) - по приватному IP s1 через awg-туннель, node-ip = tunnel-IP:**
```bash
curl -sfL https://get.k3s.io | K3S_URL=https://10.16.0.2:6443 K3S_TOKEN=<ТОКЕН> \
  sh -s - agent --node-name node-eu --node-ip 10.10.0.6 --flannel-iface awg0 \
  --node-external-ip 178.236.17.61
```
Проверка: `kubectl get nodes -o wide` - 3 server + 3 agent в `Ready`; INTERNAL-IP у РФ - `10.16.0.x`,
у `node-eu` - `10.10.0.6`.

> kubeconfig: `/etc/rancher/k3s/k3s.yaml` (`127.0.0.1` заменяется на публичный IP `node-s1` для внешнего kubectl/Argo).

## 3. Разметить узлы (метки + taint'ы)
```bash
for n in node-s1 node-s2 node-s3; do kubectl label node $n codelens.io/pool=data; done

kubectl label node node-heavy codelens.io/pool=heavy
kubectl taint node node-heavy dedicated=embedder:PreferNoSchedule   # мягко: чужие избегают, но могут заехать

kubectl label node node-dev codelens.io/env=staging
kubectl taint node node-dev dedicated=staging:NoSchedule           # жёстко: prod-поды на staging-узел нельзя

kubectl label node node-eu geo=eu
kubectl taint node node-eu dedicated=llm:NoSchedule                 # жёстко: РФ-поды сюда нельзя
```
Это соответствует `nodeSelector`/`tolerations` в `values-k3s.yaml`.

## 4. Предусловия-операторы (один раз)
```bash
# CloudNativePG (postgres)
kubectl apply --server-side -f \
  https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.24/releases/cnpg-1.24.0.yaml
# sealed-secrets controller (см. deploy/gitops/sealed/README.md) - манифестом из релизов,
# создаёт Deployment sealed-secrets-controller в kube-system (имя по умолчанию для kubeseal)
kubectl apply -f https://github.com/bitnami-labs/sealed-secrets/releases/latest/download/controller.yaml
# cert-manager + ClusterIssuer (TLS на codelens.fun; issuer letsencrypt-prod = аннотация в values)
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml
kubectl -n cert-manager rollout status deploy/cert-manager-webhook   # дождаться webhook перед issuer
kubectl apply -f deploy/gitops/cluster-issuer.yaml
# Argo CD (server-side: CRD applicationsets крупный, client-side apply упрётся в лимит аннотации 256КБ)
kubectl create ns argocd
kubectl apply --server-side --force-conflicts -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
```

DNS-записи `codelens.fun` и `staging.codelens.fun` (по 3 A-записи на публичные IP data-узлов
s1/s2/s3, TTL 60s - без single-IP SPOF) должны резолвиться до применения issuer, иначе ACME
HTTP-01 не подтвердит домен (cert-manager будет ретраить).

### Argo CD UI за forward-auth
Свой логин Argo выключается, доступ гейтит тот же `role=admin`, что Grafana/Adminer (UI под
`/argocd` на host приложения). Применять после установки Argo и подъёма staging:
```bash
kubectl -n argocd patch cm argocd-cmd-params-cm --type merge \
  -p '{"data":{"server.insecure":"true","server.rootpath":"/argocd","server.disable.auth":"true"}}'
kubectl -n argocd rollout restart deploy/argocd-server
kubectl apply -f deploy/gitops/argocd-ingress.yaml
```
UI: `https://staging.codelens.fun/argocd` под admin-сессией. (Иначе - port-forward `svc/argocd-server`
и пароль из `secret/argocd-initial-admin-secret`.)

## 5. Секреты (sealed-secrets)
```bash
# deploy/gitops/sealed/secrets.prod.env с реальными значениями (в .gitignore), затем:
deploy/gitops/sealed/seal.sh prod
git add deploy/gitops/sealed/prod && git commit -m "prod secrets (sealed)" && git push
```

## 6. Подключить k3s-overlay и развернуть через Argo
В [gitops/application-prod.yaml](gitops/application-prod.yaml) overlay добавляется последним:
```yaml
    helm:
      valueFiles:
        - values.yaml
        - values-prod.yaml
        - values-k3s.yaml      # ingress traefik, раскладка по узлам, qdrant/pg HA на data-узлах
```
Bootstrap (разово; дальше всё через git):
```bash
kubectl apply -f deploy/gitops/project.yaml
kubectl apply -f deploy/gitops/application-prod.yaml
kubectl apply -f deploy/gitops/application-secrets-prod.yaml
```

## 7. Проверка раскладки и репликации
```bash
kubectl get pods -A -o wide                       # embedder→node-heavy, llm→node-eu, qdrant/pg по 3 data-узлам
kubectl get pods -n codelens-prod -l app.kubernetes.io/component=qdrant -o wide   # 3 пода на 3 РАЗНЫХ узлах
kubectl exec -n codelens-prod codelens-qdrant-0 -- curl -s localhost:6333/cluster | head   # статус кластера
kubectl cnpg status codelens-pg -n codelens-prod  # primary + 2 standby, стриминг-репликация
```

## Что проверить на kind/minikube перед prod
Bootstrap Qdrant-кластера (pod-0 поднимает Raft, pod-N присоединяется через `--bootstrap`) задан в
`templates/qdrant.yaml` по hostname-ordinal. Это стандартный паттерн, не проверенный на живом
кластере - запустить локально с урезанным масштабом и убедиться, что `.../cluster` показывает 3 пира
и шарды разъехались:
```bash
helm template ... | kubectl apply -f -     # или через Argo на kind
```
Redis одиночный (кэш + очередь RQ) - намеренно не реплицируется; потеря пода = сброс кэша и
незавершённых ingest-задач, для этого профиля это приемлемо.
