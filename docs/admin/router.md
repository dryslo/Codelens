# Админ-роутер (src/admin/router.py) - управление индексом и пользователями

Группа `/admin/*` за общей зависимостью `require_admin`: статистика и правка индекса, постановка
ingest-задач (ZIP/GitHub) и управление ролями. Бизнес-логику роутер не держит - проксирует в
backend-клиент и `AuthService`. Подключается в [services/backend_app.py](../../services/backend_app.py)
(разбор - [../services/backend-app.md](../services/backend-app.md)).

## Префикс и общая зависимость

```python
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])
```
- `prefix="/admin"` - все пути ниже идут под `/admin/*`.
- `dependencies=[Depends(require_admin)]` навешивается на группу, а не на каждый эндпоинт.
  `require_admin` резолвит пользователя по access-токену и требует `role=admin`, иначе 403 (нет
  токена - 401). Разбор зависимости - [../auth/auth.md](../auth/auth.md), исходник -
  [../../src/auth/deps.py](../../src/auth/deps.py).

## Откуда берётся backend

```python
def _backend(request: Request) -> BackendClient:
    """Backend-клиент из состояния приложения."""
    return request.app.state.backend
```
- `backend_app` при старте кладёт собранный `Components.backend` (это `LocalBackend`) в
  `app.state.backend`. Роутер достаёт его из `request.app.state` - своего экземпляра не создаёт и
  пайплайн на запрос не пересобирает.
- `AuthService` берётся аналогично, но через зависимость `get_auth` (`request.app.state.auth`):
  эндпоинты пользователей зовут его напрямую, минуя backend-клиент.
- `BackendClient` импортируется под `TYPE_CHECKING` - только для аннотации, в рантайме не нужен.

## Эндпоинты

| Метод + путь | Тело / параметры | Делегирует | Возвращает |
|---|---|---|---|
| `GET /admin/stats` | - | `backend.stats()` | `{chunks, sources, langs}` |
| `POST /admin/index` | `IndexReq {folder, source, incremental=true}` | `backend.index(...)` | результат индексации |
| `POST /admin/remove` | `RemoveReq {source}` | `backend.remove(source)` | результат удаления |
| `POST /admin/ingest/zip` | multipart: `source` (Form), `file` (UploadFile) | `backend.ingest_zip(bytes, source)` | `{job_id}` |
| `POST /admin/ingest/github` | `IngestGithubReq {url, source, ref?}` | `backend.ingest_github(url, ref, source)` | `{job_id}` |
| `GET /admin/ingest/jobs` | - | `backend.ingest_jobs()` | `{jobs: [...]}` |
| `GET /admin/ingest/jobs/{job_id}` | path `job_id` | `backend.ingest_job(job_id)` | запись задачи либо `{error}` |
| `GET /admin/users` | - | `auth.list_users()` | `{users: [...]}` |
| `POST /admin/users/{user_id}/role` | path `user_id`, `SetRoleReq {role}` | `auth.set_role(user_id, role)` | результат смены роли |

Схемы тел - в [../../src/persistence/schemas.py](../../src/persistence/schemas.py) (`IndexReq`,
`RemoveReq`, `IngestGithubReq`) и [../../src/auth/schemas.py](../../src/auth/schemas.py)
(`SetRoleReq`). FastAPI по аннотации парсит и валидирует JSON-тело, возвращаемый dict сериализуется
в JSON.

## Кодовая база: stats / index / remove

```python
@router.get("/stats")
def stats(request: Request) -> dict:
    return _backend(request).stats()

@router.post("/index")
def index(r: IndexReq, request: Request) -> dict:
    return _backend(request).index(r.folder, r.source, r.incremental)

@router.post("/remove")
def remove(r: RemoveReq, request: Request) -> dict:
    return _backend(request).remove(r.source)
```
- `stats` отдаёт число чанков, список источников и языков (последнее - для UI-фильтров).
- `index` индексирует папку источника на стороне backend (`incremental` по умолчанию `true`),
  `remove` выкидывает источник из индекса. Оба на стороне `LocalBackend` осиротевают кэш поиска
  через сдвиг `index-epoch` (см. [../persistence/caching.md](../persistence/caching.md)).
- `index` ожидает путь, видимый backend-процессу. Загрузка произвольного архива/репозитория идёт
  не сюда, а через `/ingest/*` (фоном, с acquire во временную папку).

## Ingest из админки: ZIP / GitHub

Постановка задачи, а не синхронная индексация: оба `/ingest/*` лишь кладут сериализуемый дескриптор
в очередь и сразу отдают `job_id`. Исполняет задачу `run_ingest` (`acquire → index_path →
bump_epoch`) - в потоке того же процесса (`InProcessQueue`) либо в worker-поде (`RedisQueue`/RQ).
Конвейер - [../ingest/ingest.md](../ingest/ingest.md), очередь и статусы -
[../jobs/jobs.md](../jobs/jobs.md).

```python
async def _read_capped(file: UploadFile, limit: int) -> bytes:
    buf = bytearray()
    while True:
        chunk = await file.read(1 << 20)        # чанки по 1 MiB
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            raise HTTPException(status_code=413, detail="файл превышает лимит загрузки")
    return bytes(buf)


@router.post("/ingest/zip")
async def ingest_zip(request: Request, source: str = Form(...),
                     file: UploadFile = File(...)) -> dict:
    cfg = getattr(request.app.state, "cfg", {}) or {}
    limit = int((cfg.get("ingest") or {}).get("max_upload_mb", 100)) * 1024 * 1024
    return _backend(request).ingest_zip(await _read_capped(file, limit), source)
```
- Принимает multipart: `source` (Form) и `file` (UploadFile). Тело читается чанками по 1 MiB.
- Лимит размера - `ingest.max_upload_mb` из `app.state.cfg` (env `MAX_UPLOAD_MB`, дефолт 100;
  `config/config.yaml`). Превышение прерывает чтение с HTTP 413, не дочитывая загрузку в память.
  Это первый рубеж до `from_zip` - у того отдельные лимиты на число файлов и распакованный размер
  (anti zip-bomb/slip, см. [../ingest/ingest.md](../ingest/ingest.md)).
- `backend.ingest_zip(data, source)` собирает `task={"kind": "zip", "source", "data": bytes}` и
  зовёт `jobs.submit`, возвращая `{job_id}`.

```python
@router.post("/ingest/github")
def ingest_github(r: IngestGithubReq, request: Request) -> dict:
    return _backend(request).ingest_github(r.url, r.ref, r.source)
```
- Принимает `{url, source, ref?}`. Без блоба в payload - через очередь едет только URL, что
  предпочтительно для крупных корпусов. `backend.ingest_github` ставит
  `task={"kind": "github", "source", "url", "ref"}`. Без `ref` acquire пробует `main`, затем
  `master`. Хост ограничен `github.com` на стороне acquire (anti-SSRF).

```python
@router.get("/ingest/jobs")
def ingest_jobs(request: Request) -> dict:
    return {"jobs": _backend(request).ingest_jobs()}

@router.get("/ingest/jobs/{job_id}")
def ingest_job(job_id: str, request: Request) -> dict:
    return _backend(request).ingest_job(job_id) or {"error": "job not found"}
```
- `/ingest/jobs` - список задач очереди (новые первыми), `/ingest/jobs/{job_id}` - статус одной.
  Формат записи единый у обеих очередей: `{id, status, progress, stats, error, kind, source, ...}`,
  `status ∈ {queued, running, done, failed}`. Неизвестный `job_id` отдаёт `{"error": "job not
  found"}` (не 404) - удобно для опроса из UI.

## Пользователи

```python
@router.get("/users")
def list_users(auth: AuthService = Depends(get_auth)) -> dict:
    return {"users": auth.list_users()}

@router.post("/users/{user_id}/role")
def set_role(user_id: str, r: SetRoleReq, auth: AuthService = Depends(get_auth)) -> dict:
    return auth.set_role(user_id, r.role)
```
- В отличие от индекса/ingest, эти два зовут `AuthService` напрямую через `get_auth`
  (`app.state.auth`), без backend-клиента.
- `/users` - список аккаунтов. `/users/{user_id}/role` принимает `{role}` (`admin`|`user`) и меняет
  роль. Доступ к группе уже отфильтрован `require_admin`, так что менять роли может только админ.
  Логика `AuthService.list_users`/`set_role` - [../auth/auth.md](../auth/auth.md).

## async-замечание

`ingest_zip` объявлен `async def` (читает загрузку через `await file.read`), остальные эндпоинты -
синхронные `def` и выполняются FastAPI в threadpool, поэтому блокирующие вызовы backend не
блокируют event loop.

## Тесты

Прямого `test_admin.py` нет; ingest-путь (`backend.ingest_zip` → `InProcessQueue` → `run_ingest`,
статус `done`, прогресс, появление в `ingest_jobs()`) покрыт в
[../../tests/test_ingest.py](../../tests/test_ingest.py); роли, `list_users`/`set_role` и
`require_admin` - в [../../tests/test_auth.py](../../tests/test_auth.py).
