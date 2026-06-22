# src/llm/remote.py - удалённый LLM-провайдер (профиль large)

HTTP-клиент к [llm-gateway](../services/llm-app.md). Тот же порт `LLMProvider`, что и у локальных провайдеров ([providers.md](providers.md)), но `chat`/`hyde`/`multiquery` - это запросы к шлюзу, а не вызовы модели в процессе.

Симметрия с эмбеддингами и реранкингом: у `embeddings` и `reranking` есть пара `local.py`/`remote.py`, и переключение `local↔remote` - это правка конфига (`kind`), а не кода. У LLM роль `local.py` играет ветка локальных провайдеров в `factory.build_llms` (Ollama/OpenAI-совместимые в процессе), роль `remote.py` - этот модуль. Контракт у обоих один: `{name: LLMProvider}`, поэтому остальной код (`HybridRetriever`, чат, HyDE/MultiQuery) не различает, где живёт модель.

## `RemoteLLM(LLMProvider)`

```python
class RemoteLLM(LLMProvider):
    def __init__(self, url: str, provider: str) -> None:
        self.url = url.rstrip("/")
        self.provider = provider
```
- `url` - базовый адрес gateway, `provider` - имя профиля у шлюза (например `Groq Llama 3.3 70B`). Один `RemoteLLM` инкапсулирует один провайдер: имя подмешивается в каждый запрос, чтобы шлюз выбрал нужную модель из своего реестра.
- Промпт-логика `hyde`/`multiquery` живёт в `BaseLLM` на стороне пода ([base.py](providers.md)), поэтому здесь все три метода - простые HTTP-вызовы, а не надстройка поверх `chat`. Клиент не дублирует промпты: он просто проксирует вызовы шлюзу, где промпты и собираются.

```python
def _post(self, path: str, payload: dict) -> dict:
    import requests
    r = requests.post(f"{self.url}{path}", json={"provider": self.provider, **payload}, timeout=120)
    r.raise_for_status()
    return r.json()
```
- Общий нестриминговый POST. Тело - всегда `{"provider": <имя>, ...payload}`: имя провайдера добавляется ко всякому запросу. `timeout=120` - облачная LLM может думать долго. `raise_for_status()` превращает HTTP-ошибку в исключение, чтобы degradable-обёртки (HyDE/answer) его поймали. Ленивый `import requests` - модуль не тянет зависимость, пока remote-режим не используется.

```python
def chat(self, messages: list[dict]) -> str:
    return self._post("/chat", {"messages": messages})["content"]
```
- Нестриминговый чат: `POST /chat` с историей сообщений (OpenAI-формат `[{"role","content"}, ...]`), из ответа берётся ключ `content`.

```python
def chat_stream(self, messages: list[dict]) -> Iterator[str]:
    import requests
    from src.util.sse import parse_lines
    with requests.post(f"{self.url}/chat/stream",
                       json={"provider": self.provider, "messages": messages},
                       stream=True, timeout=180) as r:
        r.raise_for_status()
        yield from parse_lines(r.iter_lines(decode_unicode=True))
```
- Стриминг: `POST /chat/stream` со `stream=True`, ответ читается построчно. `parse_lines` ([sse.py](../../src/util/sse.py)) разбирает SSE-поток - достаёт дельты токенов из `data:`-строк до сентинела `[DONE]`. `decode_unicode=True` отдаёт строки, а не байты. `with` закрывает соединение по выходу из генератора (в том числе при обрыве клиента). `timeout=180` больше, чем у `_post`, - поток живёт дольше разового ответа.
- Симметрия с сервером: gateway пакует дельты через `sse.stream` (`pack` + завершающий `done()`), клиент распаковывает их `parse_lines`. Один и тот же формат на обоих концах.

```python
def hyde(self, query: str) -> str:
    return self._post("/hyde", {"query": query})["text"]

def multiquery(self, query: str, n: int = 3) -> list[str]:
    return self._post("/multiquery", {"query": query, "n": n})["variants"]
```
- HyDE и MultiQuery - тоже HTTP к шлюзу, а не локальная сборка промпта. `hyde` отдаёт `text`, `multiquery` - список `variants`. Промпты формирует `BaseLLM` уже на стороне gateway, поэтому remote-клиент не знает их содержимого - он лишь дёргает эндпоинт.

## `build_remote_llms(llm_url)`

```python
def build_remote_llms(llm_url: str) -> dict:
    import requests
    url = llm_url.rstrip("/")
    names = requests.get(f"{url}/llms", timeout=30).json()["names"]
    return {name: RemoteLLM(url, name) for name in names}
```
- `GET /llms` у шлюза → список имён провайдеров → реестр `{name: RemoteLLM}`. Шлюз - единственный источник истины о наличии провайдеров: backend не знает конфиг моделей, он спрашивает шлюз и оборачивает каждое имя в `RemoteLLM`.
- Возвращаемая структура - тот же контракт `{name: LLMProvider}`, что и у локальной ветки `build_llms`. Для остального кода (выбор `fast`-провайдера, список моделей в UI, `HybridRetriever`) реестр одинаков независимо от профиля.

## Ветка `kind=remote` в `factory.build_llms`

```python
def build_llms(llm_cfg: dict) -> dict:
    if llm_cfg.get("kind") == "remote":
        from src.llm.remote import build_remote_llms
        return build_remote_llms(llm_cfg["llm_url"])
    out: dict[str, LLMProvider] = {}
    for name, spec in (llm_cfg.get("providers") or {}).items():
        ...  # local: Ollama / OpenAI-совместимые провайдеры в процессе
    return out
```
- `kind: remote` → строятся HTTP-клиенты к gateway (`build_remote_llms`), модели и ключи остаются в поде шлюза. Иначе (`local`, дефолт) провайдеры собираются в процессе из `llm.providers` по `kind` (`ollama`/`openai_compatible`).
- Переключение профиля - только конфиг: `kind: ${LLM_KIND:-local}` и `llm_url: ${LLM_URL:-http://llm:8001}`. В small/dev провайдеры живут в процессе (с ключами в окружении того же процесса), в large - вынесены в gateway, а backend ходит туда по HTTP. Код вызова (`build_llms`, `HybridRetriever`, чат) при этом не меняется.

См. также: [services/llm-app.md](../services/llm-app.md) - сам шлюз и его эндпоинты; [providers.md](providers.md) - локальные провайдеры и `BaseLLM`, где живут промпты HyDE/MultiQuery.
