# Развёртывание CodeLens large на k3s (самоуправляемые VPS, HA-данные)

Цель: рабочая репликация и шардирование (Qdrant-кластер и CNPG-Postgres) плюс выделенные узлы под
embedder и llm.

## Топология (рекомендуемая, 5 узлов)

| Узел | k3s-роль | Назначение | vCPU/RAM/диск | Где | Метки / taint'ы |
|------|----------|-----------|---------------|-----|-----------------|
| `node-s1` | **server** (`--cluster-init`) | data-тир + control-plane | 4 / 16 ГБ / 150+ ГБ SSD | РФ | `codelens.io/pool=data` |
| `node-s2` | **server** | data-тир + control-plane | 4 / 16 ГБ / 150+ ГБ SSD | РФ | `codelens.io/pool=data` |
| `node-s3` | **server** | data-тир + control-plane | 4 / 16 ГБ / 150+ ГБ SSD | РФ | `codelens.io/pool=data` |
| `node-heavy` | agent | embedder (e5-large на CPU) | 8 / 16 ГБ / 40 ГБ | РФ | `codelens.io/pool=heavy`, taint `dedicated=embedder:PreferNoSchedule` |
| `node-eu` | agent | llm-gateway (внешние API) | 2 / 4 ГБ / 40 ГБ | **EU** | `geo=eu`, taint `dedicated=llm:NoSchedule` |

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

## 1. Firewall - открыть между узлами кластера
- `6443/tcp` - Kubernetes API
- `2379-2380/tcp` - embedded etcd (между server-узлами)
- `51820/udp` - wireguard (flannel-backend, см. п.2); либо `8472/udp` для обычного VXLAN
- `10250/tcp` - kubelet

Наружу публично - только ingress (`80/443`) на узле, куда смотрит DNS.

## 2. Установить k3s (HA, шифрованная сеть РФ-EU)
Узлы общаются по публичному интернету -> flannel поверх wireguard. Traefik сохраняется (overlay настроен
на `className: traefik`; для nginx добавляется `--disable traefik` и ставится ingress-nginx).

**server #1 (node-s1):**
```bash
curl -sfL https://get.k3s.io | sh -s - server --cluster-init \
  --flannel-backend=wireguard-native \
  --node-external-ip=<PUB_IP_s1> --tls-san=<PUB_IP_s1>
sudo cat /var/lib/rancher/k3s/server/node-token        # токен для остальных
```
**server #2 и #3 (node-s2, node-s3):**
```bash
curl -sfL https://get.k3s.io | sh -s - server \
  --server https://<PUB_IP_s1>:6443 --token <ТОКЕН> \
  --flannel-backend=wireguard-native \
  --node-external-ip=<PUB_IP_этого_узла> --tls-san=<PUB_IP_этого_узла>
```
**агенты (node-heavy, node-eu):**
```bash
curl -sfL https://get.k3s.io | K3S_URL=https://<PUB_IP_s1>:6443 K3S_TOKEN=<ТОКЕН> \
  sh -s - agent --node-external-ip=<PUB_IP_этого_узла>
```
Проверка: `kubectl get nodes -o wide` - 3 server + 2 agent в `Ready`.

> kubeconfig: `/etc/rancher/k3s/k3s.yaml` (`127.0.0.1` заменяется на `<PUB_IP_s1>` для внешнего kubectl/Argo).

## 3. Разметить узлы (метки + taint'ы)
```bash
for n in node-s1 node-s2 node-s3; do kubectl label node $n codelens.io/pool=data; done

kubectl label node node-heavy codelens.io/pool=heavy
kubectl taint node node-heavy dedicated=embedder:PreferNoSchedule   # мягко: чужие избегают, но могут заехать

kubectl label node node-eu geo=eu
kubectl taint node node-eu dedicated=llm:NoSchedule                 # жёстко: РФ-поды сюда нельзя
```
Это соответствует `nodeSelector`/`tolerations` в `values-k3s.yaml`.

## 4. Предусловия-операторы (один раз)
```bash
# CloudNativePG (postgres)
kubectl apply --server-side -f \
  https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.24/releases/cnpg-1.24.0.yaml
# sealed-secrets controller (см. deploy/gitops/sealed/README.md)
helm repo add sealed-secrets https://bitnami-labs.github.io/sealed-secrets
helm install sealed-secrets sealed-secrets/sealed-secrets \
  -n kube-system --set fullnameOverride=sealed-secrets-controller
# Argo CD
kubectl create ns argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
```

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
