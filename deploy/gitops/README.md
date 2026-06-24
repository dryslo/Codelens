# GitOps (Argo CD) - деплой CodeLens large

Git - единственный источник правды. Ручной push в кластер не используется: Argo CD работает внутри
кластера, каждые ~3 минуты сравнивает git с реальным состоянием и приводит кластер к git.

```
push dev ─► CI: тесты ─► сборка образов ─► push GHCR ─► bump image.tag в values-staging.yaml (git commit)
                                                                          │
                                                          Argo CD (pull) ◄┘ синхронизирует staging
```

| Окружение | Ветка | Overlay | Namespace | Application |
|-----------|-------|---------|-----------|-------------|
| staging   | `dev`  | `values-staging.yaml` | `codelens-staging` | `application-staging.yaml` |
| prod      | `main` | `values-prod.yaml`    | `codelens-prod`    | `application-prod.yaml` |

CI бампает тег только на `dev` (staging): `main` защищён (push лишь через PR), туда CI коммитить не может.

**Промоушн в прод** = PR `dev -> main`, и в этом PR `values-prod.yaml` `image.tag` ставится равным
текущему тегу из `values-staging.yaml` - тот же образ, что собран и проверен на staging (не пересобираем):

```bash
cd deploy/helm/codelens
yq -i ".image.tag = \"$(yq .image.tag values-staging.yaml)\"" values-prod.yaml
git add values-prod.yaml && git commit -m "promote: image.tag -> staging"
# затем PR dev -> main; после merge Argo раскатывает prod
```

## Предпосылки
- Установлен **Argo CD** (`kubectl create ns argocd && kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml`).
- Установлен оператор **CloudNativePG** (для `postgres.enabled`) и ingress-controller.
- `repoURL` в `project.yaml` и `application-*.yaml` указывает на репозиторий (`github.com/dryslo/Codelens`).
- `image.registry` в `values.yaml` указывает на GHCR (`ghcr.io/dryslo/codelens`), куда CI пушит образы.

## Bootstrap (разово, вручную)
Сами Argo-объекты применяются один раз, дальше всё через git:
```bash
kubectl apply -f deploy/gitops/project.yaml
kubectl apply -f deploy/gitops/application-staging.yaml
kubectl apply -f deploy/gitops/application-prod.yaml
# секреты (sealed-secrets) - отдельные приложения, синкают папки sealed/<env>:
kubectl apply -f deploy/gitops/application-secrets-staging.yaml
kubectl apply -f deploy/gitops/application-secrets-prod.yaml
```
После этого Argo сам подтянет чарт и развернёт namespace. UI: `argocd app get codelens-staging`.
Контроллер sealed-secrets ставится до этого (один раз), см. `sealed/README.md`.

## Секреты вне git - sealed-secrets
Плейнтекст-секреты в git недопустимы. Оба окружения (staging и prod) берут секрет от sealed-secrets:
в git лежит зашифрованный `SealedSecret`, контроллер в кластере расшифровывает его в обычный
`Secret/codelens-secrets`, который поды читают через `envFrom.secretRef` (ключи: `DATABASE_DSN`,
`JWT_SECRET`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `ADMIN_LOGIN`, `ADMIN_PASSWORD`, `HF_TOKEN`).
Чарт сам Secret не создаёт (`secrets.create: false` в обоих overlay). Adminer своей учётки в
секрете не имеет (stateless, вход - форма подключения к БД).

Полная инструкция (установка контроллера, генерация, ротация) - [sealed/README.md](sealed/README.md).
Кратко:
```bash
# реальные значения - в gitignored deploy/gitops/sealed/secrets.prod.env, затем:
deploy/gitops/sealed/seal.sh prod          # → deploy/gitops/sealed/prod/codelens-secrets.yaml (зашифр.)
git add deploy/gitops/sealed/prod && git commit -m "prod secrets (sealed)" && git push
```
Альтернативы того же подхода: **SOPS** (age/KMS) или **external-secrets** (Vault/AWS SM).

Для локального smoke на kind можно обойтись без sealed-secrets: базовый `values.yaml` оставляет
`secrets.create: true` (дев-`JWT_SECRET`, пустые ключи) - чарт создаёт Secret сам. В GitOps-окружениях
overlay перекрывают это на `false`.

## Откат
`git revert <коммит с bump image.tag>` -> push -> Argo откатывает кластер. Либо в UI/CLI:
`argocd app rollback codelens-prod <revision>`.

## Ручной break-glass
Когда нет CI/Argo: `helm upgrade --install` с нужным `image.tag` напрямую в namespace. Вносит дрейф -
Argo при `selfHeal` возвращает состояние к git, поэтому после инцидента изменение фиксируется в git.
