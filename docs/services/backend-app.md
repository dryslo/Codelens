# services/backend_app.py - REST-оркестратор (профиль large)

FastAPI-обёртка над `LocalBackend`. Бизнес-логику не дублирует - переиспользует.

```python
app = FastAPI(title="codelens-backend")
metrics.mount(app, "backend")
COMP = build()
BACKEND = COMP.backend
app.state.auth = COMP.auth
app.state.backend = BACKEND
app.state.cfg = COMP.cfg
```
- `build()` собирает пайплайн один раз при старте сервиса (модели/стор/БД грузятся на старте, а не на каждый запрос) и возвращает dataclass `Components`. `COMP.backend` - это `LocalBackend`; эндпоинты вызывают его методы.
- `metrics.mount` навешивает HTTP-middleware и `/metrics` (no-op без `prometheus_client`).
- `auth`/`backend`/`cfg` кладутся в `app.state` - оттуда их берут зависимости (`require_user`) и админ-роутер.

## Группы роутеров

Три группы с разным уровнем доступа:
- public (`auth_router`, `/flag-policy`, `/healthz`) - без авторизации.
- protected - зависимость `require_user` на всю группу (`APIRouter(dependencies=[Depends(require_user)])`).
- admin (`admin_router`) - зависимость `require_admin`, префикс `/admin`.

```python
protected = APIRouter(dependencies=[Depends(require_user)])

@protected.post("/search")
def search(r: SearchReq) -> dict:
    return {"results": BACKEND.search(r.query, r.k, r.mode, flags=r.flags, filters=r.filters)}
```
- FastAPI по аннотации `r: SearchReq` парсит и валидирует JSON-тело, возвращаемый dict сериализуется в JSON. `HttpBackend.search` читает ключ `results`.

```python
@protected.post("/chat")
def chat(r: ChatReq, user: dict = Depends(require_user)) -> dict:
    return BACKEND.chat(r.chat_id, r.user_msg, r.mode, r.model)

@protected.get("/chats")
def list_chats(user: dict = Depends(require_user)) -> dict:
    return {"chats": BACKEND.list_chats(user["user_id"])}

@protected.post("/chats")
def create_chat(r: CreateChatReq, user: dict = Depends(require_user)) -> dict:
    return {"chat_id": BACKEND.create_chat(user["user_id"], r.title)}
```
- Чат-эндпоинты. `user` приходит из `require_user` - `user_id` берётся из токена, а не из тела/query. `{chat_id}` - path-параметр. Формы ответов согласованы с `HttpBackend`.

```python
@protected.post("/chat/stream")
def chat_stream(r: ChatReq, user: dict = Depends(require_user)) -> StreamingResponse:
    src = BACKEND.chat_stream(r.chat_id, r.user_msg, r.mode, r.model)
    return StreamingResponse(sse.stream(src), media_type="text/event-stream")
```
- Стриминговые эндпоинты (`/chat/stream`, `/answer/stream`) отдают дельты в формате SSE через `sse.stream`: каждый токен `sse.pack`, в конце `sse.done()` (`[DONE]`) - в том числе при ошибке источника, иначе клиент висит до таймаута.

```python
@protected.get("/llms")
def llms() -> dict:
    return {"models": BACKEND.list_llms()}

@protected.post("/answer")
def answer(r: AnswerReq) -> dict:
    return {"answer": BACKEND.answer(r.query, r.chunks, r.model)}
```
- `/llms` - список моделей. `/answer` - разовая генерация по переданным фрагментам.

```python
@app.get("/flag-policy")
def flag_policy() -> dict:
    return BACKEND.flag_policy()

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
```
- Публичные. `/flag-policy` - политика флагов retrieval, `/healthz` - проба готовности для k8s.

```python
app.include_router(auth_router)
app.include_router(protected)
app.include_router(admin_router)
```
- Админ-эндпоинты (`/admin/stats`, `/admin/index`, `/admin/remove`, ingest ZIP/GitHub, управление ролями) вынесены в `src/admin/router.py` с общей зависимостью `require_admin`. Backend-клиент там берётся из `request.app.state.backend`.

Замечание по async: эндпоинты синхронные (`def`, не `async def`) - FastAPI выполняет их в threadpool, поэтому блокирующие вызовы (модели/сеть) не блокируют event loop.
