# deploy/nginx/nginx.conf - single-origin reverse-proxy (профиль panels)

Разбор [`../../deploy/nginx/nginx.conf`](../../deploy/nginx/nginx.conf): обвязка профиля `panels`,
которая ставит приложение и панели наблюдаемости под один домен и гейтит доступ к Grafana по
роли `admin` через forward-auth.

Профиль `panels` поднимается отдельно (`docker compose --profile panels up`), разбор сервисного
блока - в [`./docker-compose.md`](./docker-compose.md#nginx-grafana-prometheus-профиль-panels).

## Зачем один origin

В обычном large-стеке frontend, backend и Grafana - разные сервисы на разных портах. Браузер за
single-origin reverse-proxy видит только один домен (`http://localhost`), а nginx разводит запросы
по upstream'ам по префиксу пути.

Это требование refresh-куки, а не косметика. Refresh-токен лежит в httpOnly+SameSite cookie с
`path=/auth` (разбор - [`../auth/auth.md`](../auth/auth.md#токены)). Чтобы при заходе на `/grafana`
эта кука ушла на тот же origin и forward-auth смог её прочитать, приложение и Grafana обязаны
жить под одним доменом. Развести их на поддомены/порты - значит потерять куку на cross-origin
запросе и сломать гейтинг.

```nginx
upstream frontend { server frontend:8501; }
upstream backend  { server backend:8080; }
upstream grafana  { server grafana:3000; }

server {
  listen 80;
```

Три upstream'а по docker-DNS-именам сервисов compose, один `server` на порту 80.

## Location-блоки

Маршрутизация - по префиксу пути, от частного к общему: `/auth_check` (внутренний),
`/grafana/`, `/api/`, `/` (catch-all).

### Приложение (`/`)

```nginx
location / {
  proxy_pass http://frontend;
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection $connection_upgrade;
  proxy_set_header Host $host;
  proxy_read_timeout 86400;
}
```

Catch-all на Streamlit. Streamlit держит websocket, поэтому нужны `Upgrade`/`Connection`
(значение `$connection_upgrade` собирается из `map $http_upgrade` в начале `http`-блока) и
длинный `proxy_read_timeout` - иначе долгая сессия рвётся по таймауту.

### API backend (`/api/`)

```nginx
location /api/ {
  proxy_pass http://backend/;
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto $scheme;
  proxy_read_timeout 300;
}
```

Trailing slash в `proxy_pass http://backend/` снимает префикс `/api`: `/api/search` →
`backend/search`. Таймаут 300с - запас под медленные ретрив/LLM-запросы.

### Grafana за forward-auth (`/grafana/`)

```nginx
location = /grafana { return 302 /grafana/; }
location /grafana/ {
  auth_request /auth_check;
  error_page 401 403 = @login;
  auth_request_set $auth_user $upstream_http_x_auth_user;
  auth_request_set $auth_role $upstream_http_x_auth_role;
  proxy_set_header X-Auth-User $auth_user;
  proxy_set_header X-Auth-Role $auth_role;
  proxy_pass http://grafana;
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto $scheme;
}
```

Префикс `/grafana` тут сохраняется (без trailing slash в `proxy_pass`): Grafana поднята с
`GF_SERVER_SERVE_FROM_SUB_PATH=true` и сама ждёт этот префикс. Редирект `/grafana` → `/grafana/`
нужен, чтобы относительные ссылки внутри Grafana резолвились корректно.

Каждый запрос к панелям сперва проходит `auth_request` (см. ниже). Подробности гейтинга и проброса
идентичности - в следующих разделах.

## auth_request → /auth/forward-auth

```nginx
location = /auth_check {
  internal;
  proxy_pass http://backend/auth/forward-auth;
  proxy_pass_request_body off;
  proxy_set_header Content-Length "";
  proxy_set_header Cookie $http_cookie;
}
location @login { return 302 /; }
```

Директива `auth_request /auth_check` заставляет nginx перед каждым запросом к `/grafana/` сделать
внутренний подзапрос на `/auth_check`, а тот проксируется на эндпоинт backend
[`/auth/forward-auth`](../auth/auth.md#forward-auth-доступ-к-внешним-панелям).

- `internal` - блок недоступен снаружи, только как auth-подзапрос.
- `proxy_pass_request_body off` + `Content-Length ""` - тело проверки не нужно, гоняется только
  заголовок.
- `proxy_set_header Cookie $http_cookie` - исходная refresh-кука пробрасывается в forward-auth;
  по ней эндпоинт и решает доступ.

Логика на стороне backend (`src/auth/router.py`): refresh-кука резолвится read-only
(`resolve_refresh`, без ротации); если по ней пользователь имеет роль `admin` - ответ `200`, иначе
`401`. Источник доступа к панелям - роль аккаунта в БД, без IdP.

`error_page 401 403 = @login` ловит отказ forward-auth и редиректит на `/` - страницу логина
приложения. Так не-admin (или анонимный) видит логин вместо голого `403`.

## Проброс идентичности в Grafana

При `200` forward-auth возвращает заголовки `X-Auth-User` (логин) и `X-Auth-Role` (`Admin`). Их
надо перенести из ответа auth-подзапроса в основной проксируемый запрос к Grafana:

```nginx
auth_request_set $auth_user $upstream_http_x_auth_user;
auth_request_set $auth_role $upstream_http_x_auth_role;
proxy_set_header X-Auth-User $auth_user;
proxy_set_header X-Auth-Role $auth_role;
```

`auth_request_set` кладёт заголовок ответа forward-auth (`$upstream_http_x_auth_user` - это
`X-Auth-User` от backend) в переменную nginx, а `proxy_set_header` ставит её на запрос к Grafana.

Grafana поднята с `auth.proxy` (`GF_AUTH_PROXY_ENABLED=true`, заголовок имени - `X-Auth-User`,
роль - через `GF_AUTH_PROXY_HEADERS=Role:X-Auth-Role`). Так панель опознаёт именно вошедшего
пользователя, а не безличного anonymous-admin. Доступ при этом всё равно гейтит nginx: `auth.proxy`
доверяет единственному источнику запросов (`GF_AUTH_PROXY_WHITELIST=""`), форма логина Grafana
отключена.

Итого цепочка: браузер с refresh-кукой → `/grafana/` → `auth_request` → forward-auth (роль admin?)
→ `200` + `X-Auth-User`/`X-Auth-Role` → nginx переносит их на запрос к Grafana → `auth.proxy`
опознаёт пользователя.

## См. также

- [`./docker-compose.md`](./docker-compose.md#nginx-grafana-prometheus-профиль-panels) - сервисный
  блок профиля `panels` (env Grafana, тома).
- [`../auth/auth.md`](../auth/auth.md#forward-auth-доступ-к-внешним-панелям) - forward-auth и
  refresh-кука.
- [`./observability-stack.md`](./observability-stack.md) - что показывает Grafana за этим прокси.
- [`./README.md`](./README.md) - обзор deploy-обвязки.
