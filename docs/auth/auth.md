# Авторизация (src/auth) - JWT + refresh, пароли argon2, OIDC-ready

Вся логика - в `src/auth/`; таблицы - в `persistence/orm.py` (общий `Base`/Alembic). Админ-роуты -
в `src/admin/` (разбор - [admin/router.md](../admin/router.md)). По умолчанию `auth.enabled=false` →
открытый dev-режим (anon=admin, без логина).

## Токены

| Токен | Где живёт | Срок | Назначение |
|---|---|---|---|
| access | JWT, серверная сессия по `jti` в кэше (`access:{jti}`) | короткий (`access_ttl`) | доступ к роутам; allow-list в кэше → отзыв сразу |
| refresh | в БД (`refresh_tokens`), хранится только sha256-хэш | длинный (`refresh_ttl`) | обновление access (`/refresh` с ротацией) и восстановление сессии (`/session` без ротации); гейт панелей в forward-auth |

Проверка access: JWT декодируется (подпись+exp), затем сверяется `access:{jti}` в кэше - если записи
нет (logout/expiry), доступ отклоняется.

Refresh дополнительно кладётся в httpOnly+SameSite cookie (`cookie_name`, path `cookie_path`, по
умолчанию `/`) - для браузера за single-origin reverse-proxy. `path=/` нужен, чтобы кука уходила и на
`/grafana`/`/adminer` и пр.: гейтинг внешних панелей через `/auth/forward-auth` читает именно её
(сузить путь - сломать гейт). Тело ответа с `refresh_token` сохранено для Streamlit-посредника
(`HttpBackend` ставит браузерную куку сам). `cookie_secure=true` нужен в prod (HTTPS), иначе браузер
не сохранит Secure-cookie.

## Таблицы (persistence/orm.py)

- **users** - `id`, `login`, `role` (`admin`|`user`), `created_at`.
- **credentials** - `user_id` (PK→users), `password_hash` (argon2). Отдельно от users: у OIDC-юзера
  пароля может не быть.
- **identities** - `id`, `user_id`→users, `provider`, `subject`, UNIQUE(provider, subject) -
  привязка к внешнему OIDC-провайдеру (`sub` из id_token).
- **refresh_tokens** - `id` (jti), `user_id`, `token_hash`, `expires_at`, `revoked`, `created_at`.

Схема создаётся baseline-миграцией (`migrations/versions/*_baseline.py`).

## Модули src/auth/

| Файл | Что |
|---|---|
| `config.py` | `AuthConfig` (enabled, secret, alg, access_ttl, refresh_ttl, oidc, cookie, rate_limit) из `config.yaml` |
| `passwords.py` | argon2id `hash_password`/`verify_password` |
| `tokens.py` | access-JWT (`make_access_token`/`decode_access`), refresh (`make_refresh_token`/`hash_refresh`) |
| `repositories.py` | `SqlUsers`/`SqlCredentials`/`SqlIdentities`/`SqlRefreshTokens` поверх ORM |
| `service.py` | `AuthService`: register, login_password, login_oidc, refresh (ротация), restore (без ротации), logout, resolve_access, ensure_admin, list_users/set_role |
| `oidc.py` | `verify_id_token` (JWKS провайдера) → `login_with_id_token` → `AuthService.login_oidc` |
| `deps.py` | `get_current_user`, `require_user`/`require_admin` - общие зависимости на группы роутеров |
| `ratelimit.py` | `allow` - fixed-window ограничитель частоты для login/register (счётчик в кэше) |
| `router.py` | публичный `/auth/*`: register, login, refresh, session, oidc, oidc/{provider}/callback, forward-auth, me, logout |

## Группировка роутов (backend_app.py)

- **public** - `/auth/{register,login,refresh,session,oidc,oidc/{provider}/callback}`, `/healthz`, `/flag-policy`.
- **protected** - `APIRouter(dependencies=[Depends(require_user)])`: `/search`, `/chat`, `/chats`,
  `/llms`, `/answer` (+ `/auth/me`, `/auth/logout`). Чаты скоупятся по `current_user.user_id`.
- **admin** (`src/admin/router.py`) - `APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])`:
  `/stats`, `/index`, `/remove`, `/ingest/*`, `/users`, `/users/{id}/role`.

Зависимость задаётся на группу (`dependencies=[...]`), а не на каждый эндпоинт.

`login`/`register` под ограничителем частоты по IP (`ratelimit.allow`, fixed-window: `rate_limit_attempts`
за `rate_limit_window` секунд); превышение - 429.

## forward-auth (доступ к внешним панелям)

`/auth/forward-auth` - `auth_request` для reverse-proxy: 200, если по refresh-куке пользователь имеет
роль `admin`, иначе 401. Источник доступа - роль аккаунта в БД (без IdP). При 200 отдаются
`X-Auth-User`/`X-Auth-Role`: nginx копирует их в проксируемый запрос, а Grafana через `auth.proxy`
опознаёт пользователя по ним. Refresh резолвится read-only (`resolve_refresh`, без ротации); кука
стабильна, т.к. восстановление сессии идёт через `restore` без ротации.

## OIDC (Google + точка расширения)

Ядро - `/auth/oidc/{provider}`: принимает `id_token` → `oidc.verify_id_token` проверяет его по JWKS
провайдера (`config.yaml → auth.oidc.<provider>.{jwks_url, issuer, audience}`) → `sub` →
`AuthService.login_oidc(provider, subject, claims)` находит/создаёт пользователя через identities
и выдаёт access+refresh. Новые провайдеры добавляются конфигом, без правок кода.

Провайдеры подписывают `id_token` асимметрично (Google - RS256), поэтому `verify_id_token` требует
`cryptography`: backend ставится с экстрой `pyjwt[crypto]` (`pyproject.toml`). Без неё PyJWT умеет
только HS*, и верификация падает с `RS256 requires 'cryptography' to be installed`.

**Google** доведён до рабочего входа. Streamlit рендерит компоненты в sandboxed iframe без
`allow-top-navigation`, поэтому GIS-виджет верхнее окно увести не может - используется обычная
ссылка верхнего уровня на OAuth-эндпоинт (`response_type=id_token`, `response_mode=form_post`):

1. На экране входа - `st.link_button` (рендерится, если задан `auth.oidc.google.clientId`), ведёт на
   `accounts.google.com/o/oauth2/v2/auth?...&redirect_uri=<login_uri>&nonce=...`.
2. Клик уводит вкладку на выбор аккаунта; Google form_post'ит `id_token` на `login_uri` =
   `https://<host>/auth/oidc/google/callback`.
3. `/auth/oidc/{provider}/callback` принимает поле `id_token` (или `credential` от GIS) - CSRF
   (`g_csrf_token`) проверяется, только если пришёл; верифицирует токен тем же `login_with_id_token`,
   ставит refresh-куку и редиректит на `/`.
4. Фронт подхватывает сессию по куке (как при F5).

Конфиг рендерится из `config.oidc.google.clientId` (публичен): `audience`/`jwks_url`/`issuer` читает
бэкенд, `clientId`/`login_uri` - фронт. `clientId` пуст → кнопка скрыта. В `ingress` нужен
`authPath: true`, чтобы `/auth/*` (с callback) проксировался на backend. В Google-клиенте `<host>` +
`<host>/auth/oidc/google/callback` должны быть в Authorized origins/redirect URIs. GitHub - не OIDC
(нет id_token/JWKS), потребовал бы отдельного OAuth2 code-flow.

## Где собирается

`factory.build()` создаёт `AuthService` (репозитории + кэш + `AuthConfig`) в `Components.auth`,
бутстрапит первого админа из `ADMIN_LOGIN`/`ADMIN_PASSWORD`, и при `auth.enabled` без `redis_url`
поднимает in-process кэш под access-сессии. `backend_app` кладёт сервис в `app.state.auth`.
Frontend ([app.py](../../app.py)) в role=all зовёт `Components.auth` напрямую, во frontend-профиле -
`HttpBackend` с Bearer-токеном.

## Тесты

[tests/test_auth.py](../../tests/test_auth.py): argon2, access/refresh-токены (подпись, exp, хэш),
register/login, ротация refresh, logout (отзыв access+refresh), OIDC-upsert, `resolve_access`
(disabled→anon / guard), bootstrap админа и роли.
