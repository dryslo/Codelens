# services/llm_app.py - LLM-gateway (профиль large)

FastAPI-шлюз к LLM-провайдерам. Выделен в отдельный сервис, чтобы ключи провайдеров и реестр моделей жили только здесь, а backend ходил к нему по HTTP (клиент - `RemoteLLM`, см. [llm/remote.md](../llm/remote.md)).

Главное про границу ответственности: ключи провайдеров (`GROQ_API_KEY` и подобные) монтируются только в под этого шлюза. Backend и ретривер их не видят - они знают лишь адрес gateway. Секрет облачной LLM не растекается по всем подам, а изолирован в одном сервисе.

```python
_LLMS: dict = {}

def _load() -> None:
    global _LLMS
    from src.factory import build_llms, load_config
    llm_cfg = dict(load_config().get("llm", {}))
    llm_cfg.pop("kind", None)  # gateway строит только локальные провайдеры (иначе self-loop)
    _LLMS = build_llms(llm_cfg)
```
- Глобальный реестр провайдеров, заполняется на старте. `build_llms` - тот же composition-root, что и у backend, но из конфига выбрасывается `kind`: иначе при `kind=remote` шлюз построил бы `RemoteLLM` к самому себе и зациклился. Шлюз - это всегда локальная ветка `build_llms` (провайдеры в процессе с реальными ключами).

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _load()
    yield

app = FastAPI(title="codelens-llm", lifespan=lifespan)
metrics.mount(app, "llm")
```
- Провайдеры собираются один раз при старте (через `lifespan`), а не на каждый запрос. `metrics.mount` навешивает `/metrics` и латентность эндпоинтов (no-op без `prometheus_client`).

```python
class ChatReq(BaseModel):
    provider: str
    messages: list[dict]

class HydeReq(BaseModel):
    provider: str
    query: str

class MultiQueryReq(BaseModel):
    provider: str
    query: str
    n: int = 3
```
- Контракты запросов. В каждом есть `provider` - имя профиля из реестра; шлюз по нему выбирает нужную модель. Формы согласованы с тем, что шлёт `RemoteLLM` (`{"provider", ...}`).

```python
def _get(provider: str) -> object:
    llm = _LLMS.get(provider)
    if llm is None:
        raise HTTPException(404, f"unknown provider: {provider}")
    return llm
```
- Резолв провайдера по имени; 404 при неизвестном. Единая точка, через которую проходят все POST-эндпоинты.

```python
@app.get("/llms")
def llms() -> dict:
    return {"names": list(_LLMS)}
```
- Список доступных провайдеров. Его читает `build_remote_llms` на стороне backend, чтобы собрать реестр `{name: RemoteLLM}` - шлюз тут единственный источник истины о наличии моделей.

```python
@app.post("/chat")
def chat(r: ChatReq) -> dict:
    return {"content": _get(r.provider).chat(r.messages)}
```
- Нестриминговый чат: вызывается `chat` локального провайдера, ответ - `{"content": ...}` (клиент читает `content`).

```python
@app.post("/chat/stream")
def chat_stream(r: ChatReq) -> StreamingResponse:
    llm = _get(r.provider)
    return StreamingResponse(sse.stream(llm.chat_stream(r.messages)),
                             media_type="text/event-stream")
```
- Стриминг: дельты провайдера оборачиваются в SSE через `sse.stream` - каждый токен `sse.pack`, в конце `sse.done()` (`[DONE]`), в том числе при ошибке источника (иначе клиент висит до таймаута). На другом конце `RemoteLLM.chat_stream` разбирает поток через `parse_lines`. См. [sse.py](../../src/util/sse.py).

```python
@app.post("/hyde")
def hyde(r: HydeReq) -> dict:
    return {"text": _get(r.provider).hyde(r.query)}

@app.post("/multiquery")
def multiquery(r: MultiQueryReq) -> dict:
    return {"variants": _get(r.provider).multiquery(r.query, r.n)}
```
- HyDE и MultiQuery исполняются здесь же: `hyde`/`multiquery` берутся из `BaseLLM` провайдера, то есть промпты собираются на стороне шлюза, а не клиента. Ответы - `{"text"}` и `{"variants"}` соответственно.

```python
@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "names": list(_LLMS)}
```
- Проба готовности для k8s; заодно отдаёт список реально загруженных провайдеров.

Замечание по профилям: в small/dev этот сервис не нужен - провайдеры живут в процессе backend (`kind: local`). В large он выделен из-за изоляции секретов и независимого масштабирования: ключи в одном поде, а вызовы к нему - по HTTP через `RemoteLLM`.

См. также: [llm/remote.md](../llm/remote.md) - HTTP-клиент к этому шлюзу и ветка `kind=remote`; [llm/providers.md](../llm/providers.md) - локальные провайдеры и `BaseLLM`, чьи `hyde`/`multiquery` исполняет шлюз.
