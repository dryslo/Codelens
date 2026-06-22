# GitOps (Argo CD) - объекты деплоя

Разбор манифестов `deploy/gitops/`. Операционная сторона (bootstrap, откат, break-glass) -
в рантбуке [../../../deploy/gitops/README.md](../../../deploy/gitops/README.md); здесь - что
описывает каждый объект Argo CD и зачем он так устроен. Обзор деплоя целиком - [../README.md](../README.md).

Argo CD работает внутри кластера и каждые ~3 минуты сравнивает git с реальным состоянием, приводя
кластер к git. Сами объекты Argo (`AppProject`, `Application`) живут в namespace `argocd` и
применяются один раз при bootstrap; дальше всё едет через git.

| Application | Что синкает | Namespace | syncPolicy |
|---|---|---|---|
| `codelens-staging` | Helm-чарт `deploy/helm/codelens` @ `dev` | `codelens-staging` | automated prune + selfHeal |
| `codelens-prod` | Helm-чарт `deploy/helm/codelens` @ `main` | `codelens-prod` | automated prune + selfHeal |
| `codelens-secrets-staging` | каталог `sealed/staging` @ `dev` | `codelens-staging` | automated prune + selfHeal |
| `codelens-secrets-prod` | каталог `sealed/prod` @ `main` | `codelens-prod` | automated prune + selfHeal |
| `kube-prometheus-stack` | чарт `kube-prometheus-stack` 65.5.0 | `monitoring` | automated prune + selfHeal |

## `project.yaml` - AppProject (песочница)

[../../../deploy/gitops/project.yaml](../../../deploy/gitops/project.yaml) описывает `AppProject`
`codelens` - границы, в которых вообще разрешено деплоить приложениям этого проекта. Это слой
изоляции: даже если в `Application` указать чужой репозиторий или посторонний namespace, Argo
откажет синхронизировать.

- `sourceRepos` - белый список `repoURL`. Разрешены только два: репозиторий проекта
  (`github.com/dryslo/Codelens`) и Helm-репозиторий prometheus-community (для
  `kube-prometheus-stack`). Любой другой источник запрещён.
- `destinations` - белый список пар (namespace · server). Три namespace на одном кластере
  (`https://kubernetes.default.svc` - тот же кластер, где работает Argo): `codelens-staging`,
  `codelens-prod`, `monitoring`. Деплой в `kube-system`, `argocd` и прочие отсекается.
- `clusterResourceWhitelist` / `namespaceResourceWhitelist` - оба `{ group: "*", kind: "*" }`,
  то есть любые виды ресурсов разрешены. Послабление осознанное: чарт ставит cluster-scoped
  объекты (`Cluster` CloudNativePG, CRD), поэтому сужать по видам нельзя без поломки рендера.
  Защита держится на ограничении репозиториев и namespace, а не видов ресурсов.

Каждый `Application` ссылается на этот проект полем `spec.project: codelens` и наследует его рамки.

## `application-staging.yaml` / `application-prod.yaml` - приложение CodeLens

Два почти идентичных `Application`, различающихся только окружением. Оба держат namespace в точности
равным тому, что рендерит чарт `deploy/helm/codelens` на своей ветке.

`source`:
- `repoURL` - репозиторий проекта (тот же, что в белом списке проекта).
- `targetRevision` - ветка окружения: staging едет за `dev`, prod - за `main`. Промоушн в прод =
  merge `dev → main`.
- `path: deploy/helm/codelens` - путь к чарту в репозитории.
- `helm.valueFiles` - порядок важен, последующий файл перекрывает предыдущий:
  `values.yaml` (база, large-дефолты) → `values-<env>.yaml` (overlay окружения). Overlay задаёт
  масштаб реплик/ресурсов под окружение и - главное - `image.tag`, который бампит CI.

`destination` - тот же кластер (`https://kubernetes.default.svc`), namespace `codelens-staging`
или `codelens-prod`.

`syncPolicy`:
- `automated.prune: true` - ресурс, убранный из git, Argo удаляет из кластера.
- `automated.selfHeal: true` - ручная правка кластера откатывается к git (борьба с дрейфом).
- `syncOptions`: `CreateNamespace=true` (namespace создаёт сам Argo) и
  `ApplyOutOfSyncOnly=true` (применяются только изменившиеся объекты).

В prod-манифесте отмечено, что для ручного подтверждения каждого релиза блок `automated` убирается
целиком: тогда Argo показывает `OutOfSync`, а синк выполняется кнопкой в UI или
`argocd app sync codelens-prod`.

`finalizers: [resources-finalizer.argocd.argoproj.io]` - при удалении самого `Application` каскадно
удаляются (prune) все его ресурсы из кластера, а не остаются сиротами.

### Как bump image.tag триггерит sync

CI собирает образы, пушит в GHCR и коммитом меняет `image.tag` в `values-<env>.yaml` нужной ветки.
Этот коммит - изменение в git на пути, который синкает `Application`. На следующей сверке Argo видит
расхождение (новый тег в рендере чарта vs текущие поды), помечает приложение `OutOfSync` и при
`automated` синхронизирует: обновляет Deployment'ы на новый образ. Откат симметричен -
`git revert` коммита с bump возвращает прежний тег, Argo раскатывает обратно.

## `application-secrets-staging.yaml` / `application-secrets-prod.yaml` - секреты

Секреты вынесены в отдельные `Application`, чтобы их жизненный цикл не смешивался с чартом
приложения. Argo применяет не только Helm-чарт, но и каталог обычных манифестов: здесь `source`
указывает не на чарт, а на папку с зашифрованными `SealedSecret`.

- `path: deploy/gitops/sealed/<env>` - каталог зашифрованных манифестов окружения,
  `directory.recurse: false` (только верхний уровень).
- `targetRevision` - та же ветка окружения (`dev` / `main`), `destination.namespace` -
  `codelens-<env>`.
- `syncPolicy` - тот же `automated { prune, selfHeal }` плюс `CreateNamespace=true`.

Контроллер sealed-secrets в кластере расшифровывает применённый `SealedSecret` в обычный
`Secret/codelens-secrets`, который поды приложения читают через `envFrom.secretRef`. Подробнее -
[sealed-secrets.md](sealed-secrets.md); сам шаблон Secret разбирается в
[../helm/templates/platform.md](../helm/templates/platform.md).

## `application-monitoring.yaml` - стек наблюдаемости

[../../../deploy/gitops/application-monitoring.yaml](../../../deploy/gitops/application-monitoring.yaml)
ставит `kube-prometheus-stack` (Prometheus + Grafana + Alertmanager + node-exporter +
kube-state-metrics) один раз на кластер в namespace `monitoring`. В отличие от приложений CodeLens,
`source` тянет внешний чарт из Helm-репозитория, а не путь из git:

- `repoURL: https://prometheus-community.github.io/helm-charts`, `chart: kube-prometheus-stack`,
  `targetRevision: 65.5.0` - здесь `targetRevision` это версия чарта, не образа.
- `helm.values` (inline) - ослабление селекторов оператора:
  `serviceMonitorSelectorNilUsesHelmValues: false` и `podMonitorSelectorNilUsesHelmValues: false`.
  По умолчанию kube-prometheus-stack скрейпит только `ServiceMonitor`/`PodMonitor` со своим
  release-лейблом; здесь он берёт все, чтобы `ServiceMonitor` чарта подхватывался без проставления
  лейблов (удобно для демо/staging). На проде селектор можно ужесточить обратно и проставлять
  `monitoring.serviceMonitor.labels` в чарте. Там же `retention: 7d` и grafana-сайдкар, ищущий
  ConfigMap-дашборды во всех namespace (`searchNamespace: ALL`, label `grafana_dashboard`).
- `syncOptions` дополнительно содержит `ServerSideApply=true` - крупные CRD kube-prometheus-stack
  применяются server-side, чтобы не упереться в лимит размера аннотации client-side apply.

Имя `Application` совпадает с helm release name (`kube-prometheus-stack`) - на него можно завязать
`serviceMonitorSelector`, если позже понадобится сузить отбор.

Какие метрики отдаёт CodeLens и как они попадают в этот стек - в [../../util/observability.md](../../util/observability.md).
