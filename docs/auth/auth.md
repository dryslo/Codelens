# Авторизация (src/auth) - JWT + refresh, пароли argon2, OIDC-ready

Вся логика - в `src/auth/`; таблицы - в `persistence/orm.py` (общий `Base`/Alembic). Админ-роуты -
в `src/admin/` (разбор - [admin/router.md](../admin/router.md)). По умолчанию `auth.enabled=false` →
открытый dev-режим (anon=admin, без логина).

## Токены

| Токен | Где живёт | Срок | Назначение |
|---|---|---|---|
| access | JWT, серверная сессия по `jti` в кэше (`access:{jti}`) | короткий (`access_ttl`) | доступ к роутам; allow-list в кэше → отзыв сразу |
| refresh | в БД (`refresh_tokens`), хранится только sha256-хэш | длинный (`refresh_ttl`) | обновление access; ротация при каждом `/refresh` |

Проверка access: JWT декодируется (подпись+exp), затем сверяется `access:{jti}` в кэше - если записи
нет (logout/expiry), доступ отклоняется.

Refresh дополнительно кладётся в httpOnly+SameSite cookie (`cookie_name`, path `cookie_path`, по
умолчанию `/`) - для браузера за single-origin reverse-proxy. `path=/` нужен, чтобы кука уходила и на
`/grafana`/`/pgadmin` и пр.: гейтинг внешних панелей через `/auth/forward-auth` читает именно её
(сузить путь - сломать гейт). Тело ответа с `refresh_token` сохранено для совместимости (Streamlit,
`HttpBackend`). `cookie_secure=true` нужен в prod (HTTPS), иначе браузер не сохранит Secure-cookie.

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
| `service.py` | `AuthService`: register, login_password, login_oidc, refresh (ротация), logout, resolve_access, ensure_admin, list_users/set_role |
| `oidc.py` | `verify_id_token` (JWKS провайдера) → `login_with_id_token` → `AuthService.login_oidc` |
| `deps.py` | `get_current_user`, `require_user`/`require_admin` - общие зависимости на группы роутеров |
| `ratelimit.py` | `allow` - fixed-window ограничитель частоты для login/register (счётчик в кэше) |
| `router.py` | публичный `/auth/*`: register, login, refresh, oidc, forward-auth, me, logout |

## Группировка роутов (backend_app.py)

- **public** - `/auth/{register,login,refresh,oidc}`, `/healthz`, `/flag-policy`.
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
опознаёт пользователя по ним. Refresh резолвится read-only (`resolve_refresh`, без ротации).

## OIDC (заточка)

`/auth/oidc/{provider}` принимает `id_token` → `oidc.verify_id_token` проверяет его по JWKS
провайдера (`config.yaml → auth.oidc.<provider>.{jwks_url, issuer, audience}`) → `sub` →
`AuthService.login_oidc(provider, subject, claims)` находит/создаёт пользователя через identities
и выдаёт access+refresh. Новые провайдеры добавляются конфигом, без правок кода.

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
