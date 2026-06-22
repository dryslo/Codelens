# План улучшений CodeLens

Перечень доработок за пределами текущего scope. Каждая держится принципа проекта - «один код,
размещение и реализация задаются конфигом за интерфейсами» (порты в [src/domain/interfaces.py](src/domain/interfaces.py),
сборка в [src/factory.py](src/factory.py)), поэтому добавляется как новый адаптер/компонент, а не как
переписывание ядра. Порядок и зависимости - в конце.

---

## 1. Переход на React

**Зачем.** Streamlit удобен для прототипа, но навязывает full-page rerun, слабый контроль состояния и
DOM (отсюда обходные решения в [app.py](app.py): `segmented_control` вместо `st.tabs`, ручная
синхронизация `session_state`). Для «продуктового» лица нужны нормальная компонентная модель,
маршрутизация SPA, оптимистичный UI и аккуратный стриминг чата. Backend для этого уже готов:
[services/backend_app.py](services/backend_app.py) отдаёт чистый REST (`/search`, `/chat`,
`/chat/stream` по SSE, `/chats`, `/auth/*`, `/admin/*`), а frontend уже отвязан за `ROLE=frontend` +
`BACKEND_URL` ([src/clients/backend.py](src/clients/backend.py) `HttpBackend`).

**Подход.** React-клиент (Vite + TypeScript) поверх того же REST-контракта; стриминг чата - на
существующем SSE (`/chat/stream`); авторизация - access-токен в памяти + httpOnly refresh-cookie,
которую сервер уже выставляет ([src/auth/router.py](src/auth/router.py)). Single-origin
уже настроен в [deploy/nginx/nginx.conf](deploy/nginx/nginx.conf) (`/api`, `/auth`). Замена касается
только `frontend/` и [deploy/Dockerfile.frontend](deploy/Dockerfile.frontend) (статическая сборка под
nginx); сервисы и контракты не трогаются. Streamlit можно держать параллельно на время миграции.

## 2. Почтовый сервис (и восстановление пароля)

**Зачем.** Сейчас восстановления пароля нет: эндпоинты auth - register/login/refresh/oidc/me/logout
([src/auth/router.py](src/auth/router.py)), у `users` нет даже email ([src/persistence/orm.py](src/persistence/orm.py)).
Забывший пароль пользователь заблокирован. Почта нужна и для верификации адреса, и для уведомлений
(например, завершение фоновой индексации).

**Подход.** Порт `Mailer` (как embedder/llm - пара local/remote): SMTP-адаптер или транзакционный
провайдер (SES/Resend/Mailgun), ключ - через env/sealed-secrets. Флоу сброса по образцу refresh-токенов
(в БД хранится только хэш, короткий TTL, одноразовость): `POST /auth/password/forgot {email}` →
одноразовый токен → письмо со ссылкой → `POST /auth/password/reset {token, new_password}` → проверка,
обновление `credentials`, отзыв активных сессий. Нужно: поле `email` у `users`, rate-limit (переиспользуется
[src/auth/ratelimit.py](src/auth/ratelimit.py)), блок `email` в [config/config.yaml](config/config.yaml).
Для dev - контейнер Mailpit/MailHog отдельным профилем в [deploy/docker-compose.yml](deploy/docker-compose.yml).

## 3. Логирование через Loki и трассировка

**Зачем.** Наблюдаемость сейчас - только метрики Prometheus + Grafana ([src/util/metrics.py](src/util/metrics.py)).
Централизованных логов нет (отладка по `kubectl logs` отдельно на каждом поде frontend/backend/worker/
embedder/llm), распределённой трассировки нет. Медленный `/chat` идёт через backend → embedder →
вектор-стор → llm, и без трассировки не видно, где именно теряется время (стадии ретривера сейчас
меряются только агрегированной гистограммой).

**Подход.** Логи: структурный JSON-логгер в `src/util` с корреляцией по `request_id`/`trace_id`; в
кластере - Loki + сборщик (Grafana Alloy/Promtail) на stdout подов, Loki-datasource добавляется в уже
провиженную Grafana ([deploy/grafana/provisioning/](deploy/grafana/provisioning/)). Трассировка:
OpenTelemetry SDK - автоинструментация FastAPI и `requests` (HTTP-переходы backend↔embedder/llm) плюс
ручные спаны вокруг стадий `HybridRetriever._search` (там уже есть тайминги стадий - переводятся в
спаны), экспорт OTLP в Tempo/Jaeger, datasource в Grafana, связка traces↔logs↔metrics. Для compose -
профиль с Loki/Tempo рядом с профилем `panels`.

## 4. Keycloak как IdP

**Зачем.** Авторизация самописная (JWT + refresh + argon2), но с OIDC-заготовкой:
[src/auth/oidc.py](src/auth/oidc.py) проверяет внешний `id_token` по JWKS, есть таблица `identities`
и конфиг `auth.oidc.<provider>`. Реального IdP при этом нет. Keycloak даёт централизованную identity:
SSO, соц-логины, MFA, самообслуживание аккаунта, админ-консоль, стандартные OIDC/SAML - и снимает с
приложения ответственность за хранение/восстановление учёток.

**Подход.** Развернуть Keycloak (Helm/контейнер) + Postgres (можно тот же CNPG-оператор), завести realm
и client. Прописать `auth.oidc.keycloak {jwks_url, issuer, audience}` в [config/config.yaml](config/config.yaml).
Реализовать authorization-code flow: редирект на Keycloak → callback с кодом → обмен на токены → уже
существующий `verify_id_token` → `AuthService.login_oidc` апсертит пользователя через `identities` →
выдаётся сессия приложения. Роли Keycloak маппятся в `admin`/`user`. Секреты - через sealed-secrets,
деплой - ещё один компонент large-профиля ([deploy/helm/codelens/](deploy/helm/codelens/)).

---

## Порядок и зависимости

| Шаг | Риск | Зависит от |
|---|---|---|
| 3. Loki + трассировка | низкий | независимо; расширяет текущую Prometheus/Grafana-обвязку |
| 1. React | средний | REST/SSE-контракт уже готов; чисто клиентская работа |
| 4. Keycloak | средний | OIDC-плумбинг уже есть; нужен деплой IdP |
| 2. Почта + сброс пароля | низкий | пересекается с п.4 (см. ниже) |

Пункты 2 и 4 пересекаются по identity: если принимается Keycloak, восстановление пароля и верификация
email становятся его зоной ответственности, а не приложения. Поэтому решение развилочное - либо
встроенный сброс по почте (п.2), либо делегирование Keycloak (п.4). Рекомендуемый порядок: сначала
наблюдаемость (п.3, быстрый эффект и низкий риск), затем React (п.1, лицо продукта), затем выбор по
identity - Keycloak (п.4) с делегированием почты ему, иначе самостоятельный почтовый сброс (п.2).
