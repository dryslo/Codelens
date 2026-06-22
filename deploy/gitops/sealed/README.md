# Секреты CodeLens через sealed-secrets

Секреты в git нельзя класть открытым текстом. sealed-secrets (Bitnami) решает это так:
в git кладётся зашифрованный объект `SealedSecret`, а контроллер в кластере расшифровывает
его в обычный `Secret`. Приватный ключ только в кластере - git остаётся безопасным.

```
seal.sh  ──шифрует публичным ключом──►  SealedSecret (в git)
                                              │  Argo CD применяет (application-secrets-<env>.yaml)
                                              ▼
                            контроллер sealed-secrets расшифровывает приватным ключом
                                              ▼
                            Secret/codelens-secrets  ──envFrom──►  поды приложения
```

## Раскладка
```
deploy/gitops/sealed/
  seal.sh                     # генератор (шифрует и кладёт результат в нужную подпапку)
  staging/codelens-secrets.yaml   # ЗАШИФРОВАННЫЙ SealedSecret - коммитится (создаётся seal.sh)
  prod/codelens-secrets.yaml      #   то же для prod
  secrets.<env>.env           # plaintext-вход с реальными значениями - В GIT НЕ ИДЁТ (.gitignore)
```
Папки `staging/` и `prod/` синкаются отдельными Argo-приложениями
[application-secrets-staging.yaml](../application-secrets-staging.yaml) /
[application-secrets-prod.yaml](../application-secrets-prod.yaml). Чарт приложения сам Secret не
создаёт (`secrets.create: false` в `values-staging.yaml`/`values-prod.yaml`).

## Шаг 0 - разово: контроллер + CLI
Контроллер ставится один раз (инфраструктура, ставится вне Argo):
```bash
helm repo add sealed-secrets https://bitnami-labs.github.io/sealed-secrets
helm install sealed-secrets sealed-secrets/sealed-secrets \
  -n kube-system --set fullnameOverride=sealed-secrets-controller
```
CLI `kubeseal` - из релизов https://github.com/bitnami-labs/sealed-secrets/releases (или `brew install kubeseal`).

## Шаг 1 - задать реальные значения (локально, не в git)
Создаётся `deploy/gitops/sealed/secrets.prod.env` (он в `.gitignore`):
```sh
JWT_SECRET=<openssl rand -base64 32>
ADMIN_PASSWORD=<сильный пароль>
GROQ_API_KEY=gsk_...
# опц.: GEMINI_API_KEY=...  HF_TOKEN=...  ADMIN_LOGIN=admin
# DATABASE_DSN целиком ЛИБО PG_PASSWORD (DSN тогда соберётся на codelens-pg-rw):
PG_PASSWORD=<пароль postgres>
```

## Шаг 2 - зашифровать и закоммитить
```bash
deploy/gitops/sealed/seal.sh prod
git add deploy/gitops/sealed/prod/codelens-secrets.yaml
git commit -m "prod secrets (sealed)" && git push
```
Argo подхватывает коммит -> применяет SealedSecret -> контроллер создаёт `Secret/codelens-secrets` ->
поды стартуют с ключами. Для staging - то же с аргументом `staging`.

## Ротация / смена ключа
Значение в `secrets.<env>.env` меняется -> снова `seal.sh <env>` -> commit/push. Argo и контроллер
обновляют `Secret`; поды перезапускаются (`kubectl rollout restart deploy -n codelens-<env>`), т.к.
переменные окружения из Secret читаются на старте.

## Замечания
- Скоуп шифрования - strict: `SealedSecret` расшифруется только в том же namespace и под тем же
  именем (`codelens-secrets` в `codelens-<env>`). При переименовании/переносе требуется перешифровка.
- Резервная копия приватного ключа контроллера обязательна: без него зашифрованные секреты не вернуть
  (`kubectl get secret -n kube-system -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml > backup.yaml`,
  хранится вне git).
- Порядок при первом разворачивании: контроллер (шаг 0) -> bootstrap Argo-приложений
  (`application-secrets-<env>.yaml` и основное `application-<env>.yaml`). Если поды стартуют раньше
  секрета - поднимутся в CrashLoop и сами восстановятся, когда Secret появится.
