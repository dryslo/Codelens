# deploy/nginx/nginx.conf - single-origin reverse-proxy (профиль panels)

Разбор [`../../deploy/nginx/nginx.conf`](../../deploy/nginx/nginx.conf): обвязка профиля `panels`,
которая ставит приложение и админ-панели под один домен и гейтит доступ к каждой по роли `admin`
через forward-auth. Под гейтом три панели: Grafana (`/grafana`), Adminer (`/adminer`) и дашборд
Qdrant (отдельный origin `http://localhost:8081`).

Профиль `panels` поднимается отдельно (`docker compose --profile panels up`), разбор сервисного
блока - в [`./docker-compose.md`](./docker-compose.md#nginx-adminer-grafana-prometheus-профиль-panels).

## Зачем один origin

В обычном large-стеке frontend, backend и панели - разные сервисы на разных портах. Браузер за
single-origin reverse-proxy видит только один домен (`http://localhost`), а nginx разводит запросы
по upstream'ам по префиксу пути.

Это требование refresh-куки, а не косметика. Refresh-токен лежит в httpOnly+SameSite cookie с
`path=/` (разбор - [`../auth/auth.md`](../auth/auth.md#токены)). `path=/` обязателен: при `path=/auth`
кука не ушла бы на `/grafana` или `/adminer`, и forward-auth увидел бы пусто. Плюс приложение и
панели должны жить под одним доменом, иначе кука теряется на cross-origin запросе. Развести их на
поддомены - значит сломать гейтинг.

```nginx
upstream frontend { server frontend:8501; }
upstream backend  { server backend:8080; }
upstream grafana  { server grafana:3000; }
upstream adminer  { server adminer:8080; }
upstream qdrant   { server qdrant:6333; }

server {
  listen 80;
```

Пять upstream'ов по docker-DNS-именам сервисов compose. Основной `server` слушает порт 80
(приложение + Grafana + Adminer); дашборд Qdrant вынесен в отдельный `server` на `8081` (см. ниже).
Порт-разводка тут не нарушает single-origin: cookie не привязана к порту, поэтому refresh-кука
уходит и на `:8081` того же хоста.

## Location-блоки

Маршрутизация - по префиксу пути, от частного к общему: `/auth_check` (внутренний),
`/grafana/`, `/adminer/`, `/api/`, `/` (catch-all).

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

### Adminer за forward-auth (`/adminer/`)

```nginx
location = /adminer { return 302 /adminer/; }
location /adminer/ {
  auth_request /auth_check;
  error_page 401 403 = @login;
  proxy_set_header Host $host;
  proxy_pass http://adminer/;
}
```

Тот же гейт `auth_request`/`@login`, что и у Grafana: до Adminer доходит только запрос с
admin-сессией. Подгонка под субпуть простая: Adminer строит все ссылки
относительными, поэтому `X-Script-Name` не нужен - префикс снимает trailing slash в
`proxy_pass http://adminer/` (`/adminer/?server=...` → `adminer/?server=...`), а относительные
ссылки в выдаче остаются под `/adminer`.

Своего логина у панели нет: вход в Adminer - это форма подключения к БД (System/Server/User/
Password), а единственный гейт доступа - forward-auth по `role=admin`. Пройдя его, пользователь
сразу видит форму подключения.

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

## Дашборд Qdrant на отдельном origin (`:8081`)

Grafana и Adminer живут под субпутём (`/grafana`, `/adminer`), а UI Qdrant - нет. Его дашборд ходит
в API по корне-относительным путям (`/collections`, `/cluster`), которые префикс не учитывают:
под субпуть `/qdrant/` такие запросы ушли бы мимо. Поэтому дашборд вынесен на отдельный порт/origin,
где живёт в корне.

```nginx
server {
  listen 8081;

  location = /auth_check {
    internal;
    proxy_pass http://backend/auth/forward-auth;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header Cookie $http_cookie;
  }
  location @login { return 302 http://localhost/; }

  location / {
    auth_request /auth_check;
    error_page 401 403 = @login;
    proxy_pass http://qdrant;
    proxy_set_header Host $host;
  }
}
```

- Гейт тот же - forward-auth по той же refresh-куке. Это здесь критично: у Qdrant нет собственной
  авторизации, его API/дашборд открыты для всех, кто достучался до порта. Единственная защита -
  forward-auth перед ним, поэтому `:6333` Qdrant наружу не публикуется, а дашборд доступен только
  через этот `server` за `auth_request`.
- Кука доезжает сюда, хотя порт другой: cookie не привязана к порту (только к домену), а домен тот
  же (`localhost`). Поэтому refresh-кука, выставленная приложением на `:80`, уходит и на `:8081`.
- `@login` редиректит на `http://localhost/` явно с портом `80` - чтобы отказ привёл на логин
  приложения, а не на корень текущего `:8081`.
- UI открывается по `http://localhost:8081/dashboard`.

## См. также

- [`./docker-compose.md`](./docker-compose.md#nginx-adminer-grafana-prometheus-профиль-panels) -
  сервисный блок профиля `panels` (Grafana, Adminer).
- [`../auth/auth.md`](../auth/auth.md#forward-auth-доступ-к-внешним-панелям) - forward-auth и
  refresh-кука.
- [`./observability-stack.md`](./observability-stack.md) - что показывает Grafana за этим прокси.
- [`./README.md`](./README.md) - обзор deploy-обвязки.
