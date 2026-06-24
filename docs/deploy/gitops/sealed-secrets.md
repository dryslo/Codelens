# Секреты через sealed-secrets

Подход к хранению секретов в GitOps-окружениях CodeLens (staging, prod). Разбор того, как и зачем
устроено; пошаговые операции (установка контроллера, генерация, ротация) -
[../../../deploy/gitops/sealed/README.md](../../../deploy/gitops/sealed/README.md). Манифесты Argo,
синкающие зашифрованные секреты, разобраны в [gitops.md](gitops.md); шаблон самого `Secret` в чарте -
[../helm/templates/platform.md](../helm/templates/platform.md). Обзор деплоя - [../README.md](../README.md).

## Зачем

Git - единственный источник правды для GitOps, но класть в него плейнтекст-секреты недопустимо:
история git расходится по форкам, зеркалам и бэкапам, и однажды утёкший секрет нельзя «забыть».
sealed-secrets (Bitnami) снимает противоречие: в git коммитится зашифрованный объект, расшифровать
который может только контроллер в кластере. Приватный ключ живёт исключительно в кластере, git
остаётся безопасным.

## Как устроено

```
seal.sh  ──шифрует публичным ключом──►  SealedSecret (в git)
                                              │  Argo CD применяет (application-secrets-<env>.yaml)
                                              ▼
                            контроллер sealed-secrets расшифровывает приватным ключом
                                              ▼
                            Secret/codelens-secrets  ──envFrom.secretRef──►  поды приложения
```

- В git лежит `SealedSecret` - зашифрованный публичным ключом контроллера. Безопасен для коммита.
- Отдельный Argo `Application` (`codelens-secrets-<env>`, см. [gitops.md](gitops.md)) синкает каталог
  `sealed/<env>` в namespace `codelens-<env>`.
- Контроллер sealed-secrets в кластере расшифровывает `SealedSecret` приватным ключом в обычный
  `Secret/codelens-secrets`.
- Поды приложения читают этот Secret через `envFrom.secretRef`. Ключи: `DATABASE_DSN`, `JWT_SECRET`,
  `GROQ_API_KEY`, `GEMINI_API_KEY`, `ADMIN_LOGIN`, `ADMIN_PASSWORD`, `HF_TOKEN`. Имена ключей обязаны
  совпадать с `${VAR}` в `config.yaml` (см. шаблон Secret чарта). Панель Adminer своей учётки в
  секрете не держит (stateless).

Скоуп шифрования - strict: `SealedSecret` расшифруется только в том же namespace и под тем же именем
(`codelens-secrets` в `codelens-<env>`). При переименовании или переносе в другой namespace требуется
перешифровка.

## Почему чарт сам Secret не создаёт

В обоих GitOps-окружениях overlay (`values-staging.yaml`, `values-prod.yaml`) задают
`secrets.create: false` - чарт не рендерит `templates/secret.yaml`, иначе он бы перезаписывал Secret,
созданный контроллером, и плейнтекст-значения снова попали бы в git/values. Источник `Secret`
в staging/prod ровно один - контроллер sealed-secrets.

Для локального smoke на kind можно обойтись без sealed-secrets: базовый `values.yaml` оставляет
`secrets.create: true` (дев-`JWT_SECRET`, пустые ключи), чарт создаёт Secret сам. GitOps-overlay
перекрывают это на `false`.

## `seal.sh` - генератор

[../../../deploy/gitops/sealed/seal.sh](../../../deploy/gitops/sealed/seal.sh) превращает реальные
значения в зашифрованный манифест, готовый к коммиту. Вызывается с аргументом окружения:
`seal.sh staging` или `seal.sh prod`.

1. Реальные значения берутся из окружения; их держат в gitignored-файле
   `deploy/gitops/sealed/secrets.<env>.env`, который скрипт подхватывает автоматически. Обязательны
   `JWT_SECRET` и `ADMIN_PASSWORD`; `DATABASE_DSN` либо задаётся целиком, либо собирается из
   `PG_PASSWORD` на сервис `codelens-pg-rw` (как делал чарт при `secrets.create=true`).
2. Локально собирается обычный `Secret` через `kubectl create secret --dry-run=client -o yaml` -
   в кластер при этом ничего не уходит, на stdout только YAML.
3. Этот YAML передаётся в `kubeseal`, который шифрует его публичным ключом контроллера в
   `SealedSecret`. Контроллер по умолчанию ищется как service `sealed-secrets-controller` в
   `kube-system` (переопределяется `CONTROLLER_NS` / `CONTROLLER_NAME`).
4. Результат пишется в `sealed/<env>/codelens-secrets.yaml` - ровно ту папку, что синкает
   `application-secrets-<env>.yaml`. Дальше `git add` / `commit` / `push`, и Argo применяет,
   контроллер расшифровывает.

## Ротация

Сменить значение - поправить `secrets.<env>.env` → снова `seal.sh <env>` → commit/push. Argo и
контроллер обновят `Secret`, но переменные окружения из Secret поды читают на старте, поэтому нужен
рестарт: `kubectl rollout restart deploy -n codelens-<env>`. Резервная копия приватного ключа
контроллера обязательна и хранится вне git: без неё ранее зашифрованные секреты не восстановить.

## Альтернативы

Тот же принцип «секрет в git зашифрован, расшифровка только в кластере» реализуют:
- **SOPS** (age / KMS) - шифрование пофайлово, расшифровка через KMS или age-ключ.
- **external-secrets** - секрет хранится во внешнем хранилище (Vault, AWS Secrets Manager), в
  кластере только ссылка-`ExternalSecret`, оператор подтягивает значение.

Для CodeLens выбран sealed-secrets как самый лёгкий вариант без внешних зависимостей: один
контроллер в кластере и CLI `kubeseal`.

Полный рантбук (установка контроллера, первое разворачивание, бэкап ключа) -
[../../../deploy/gitops/sealed/README.md](../../../deploy/gitops/sealed/README.md).
