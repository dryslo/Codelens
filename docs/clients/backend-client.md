# clients/backend.py - `LocalBackend` и `HttpBackend`

Реализации `BackendClient`. Вся бизнес-логика бэка - в `LocalBackend`; `HttpBackend` - сетевой прокси к тем же операциям. Фронт зависит только от интерфейса, поэтому переключение «в процессе ↔ по HTTP» не меняет UI.

### Системный промпт `_SYS`

```python
_SYS = (
    "Ты отвечаешь на вопросы по коду ТОЛЬКО на основе предоставленных фрагментов.\n"
    "Никогда не выдумывай файлы, функции или сигнатуры, которых нет в контексте.\n"
    "Если ответа в контексте нет - честно скажи об этом в первой строке.\n"
    "\n"
    "Формат ответа - валидный GitHub-Flavored Markdown:\n"
    "1. `## Разбор` - основной разбор; каждое утверждение поддерживай ссылками "
    "вида `[1]`, `[2]` на нужный фрагмент.\n"
    "2. Любой код - внутри fenced-блоков с указанием языка (```python, ```ts и т.п.).\n"
    "3. В конце `## Источники` - нумерованный список `[N] file::name (строки A–B)`.\n"
    "Не оборачивай весь ответ в один code-блок, не используй HTML."
)
```

Промпт разбит на три блока, выровненные через `\n`-разделители (для строгих токенизаторов - чтобы границы между правилами были чёткими):

1. Анти-галлюцинационный блок (первые три строки) - три явных запрета: «отвечай только по фрагментам», «не выдумывай артефакты, которых нет», «честно признайся, если контекста не хватает». Каждый запрет сформулирован как императив (а не «избегай…») - категоричным инструкциям модели следуют лучше.
2. Формат-блок - нумерованный список из трёх пунктов, задающих скелет ответа:
   - `## Разбор` - уровень H2; со ссылками `[N]` в квадратных скобках на номера фрагментов из `_ctx` (см. ниже). Эти `[N]` далее преобразуются в кликабельные якоря на карточки без дополнения промпта.
   - Fenced code-блоки с языком - без `language` Streamlit отрисует серый монохром; с `python`/`ts` подсветка работает по умолчанию.
   - `## Источники` - формат `[N] file::name (строки A–B)`, ровно повторяющий заголовок фрагмента в `_ctx` (см. ниже). Даёт LLM безопасный способ сослаться на источник без выдумывания.
3. Запреты на форматирование (последняя строка) - реальные баги с практики: модели склонны (а) обернуть весь markdown в один большой ```\` блок (тогда Streamlit рисует серый прямоугольник без подсветки, а заголовки превращаются в `## текст`), и (б) подмешивать HTML (`<br>`, `<details>`), который частично игнорируется рендером.

### Сборка контекста `_ctx`

```python
def _ctx(chunks):
    parts = []
    for i, c in enumerate(chunks):
        m = c["meta"]
        head = (f"[{i + 1}] {m.get('file')}::{m.get('name')} "
                f"(строки {m.get('start_line')}–{m.get('end_line')})")
        parts.append(f"{head}\n{c['code']}")
    return "\n\n".join(parts)
```

- Каждый фрагмент превращается в блок `[N] file::name (строки A–B)\n<код>`. Номер `N` - 1-based индекс, соответствует ссылкам `[1]`, `[2]` в выходе LLM.
- Включение `start_line–end_line` в заголовок принципиально: без них LLM не собирает раздел `## Источники` с правильными строками - либо опускает их, либо выдумывает. С ними строка из `## Источники` буквально копируется из `_ctx` (по сути - extractive cite).
- Блоки разделяются `\n\n` (двумя переводами строки) - это естественный markdown-разделитель параграфов; LLM воспринимает каждый блок как самостоятельный отрывок.

## `LocalBackend`
```python
def __init__(self, comp):
    self.c = comp
```
- Хранит `comp` - `Components` (dataclass собранных в `factory` зависимостей: retriever, store, history, registry, llms, fast, index_path, remove_source, embedder, cache, jobs). Все методы дёргают их через атрибуты (`self.c.retriever`, …).

```python
def search(self, query, k=5, mode="fast", flags=None, filters=None):
    return self.c.retriever.search(query, k, flags=flags, mode=mode, where=filters)
def list_llms(self):
    return list(self.c.llms.keys())
def answer(self, query, chunks, model):
    key = f"answer:{digest(...)}"
    def produce():
        msgs = [{"role": "system", "content": _SYS},
                {"role": "user", "content": f"{query}\n\nКонтекст:\n{_ctx(chunks)}"}]
        return self.c.llms[model].chat(msgs)
    return cache_get_or_set(self._cache(), key, produce, self._cache_ttl())
```
- `search` - делегирует ретриверу (опц. фильтры по lang/source через `where`); поисковый кэш - внутри оркестратора.
- `list_llms` - имена доступных провайдеров (для селектора в UI).
- `answer` - разовая генерация по уже найденным фрагментам (для тумблера «Ответ LLM» на вкладке Поиск): system + вопрос с контекстом → `chat`. Результат кэшируется по ключу из запроса, chunk_id и модели.

### Чат (history-aware)
```python
def _condense(self, history, user_msg):
    fast = self.c.fast
    if not history or not fast or fast not in self.c.llms:
        return user_msg
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in history[-6:])
    prompt = (f"Сформулируй ОДИН самостоятельный поисковый запрос.\n{convo}\n"
              f"Фоллоу-ап: {user_msg}\nЗапрос:")
    return cache_get_or_set(self._cache(), f"condense:{digest(...)}",
        lambda: self.c.llms[fast].chat([{"role": "user", "content": prompt}]),
        self._cache_ttl())
```
- Сжатие фоллоу-апа в самостоятельный запрос. Если истории нет или «быстрый» провайдер не настроен - вопрос возвращается как есть (переписывать нечем).
- Иначе последние 6 реплик (`history[-6:]`) берутся как контекст, и `fast`-модель формулирует один автономный запрос. Назначение: «а как там ошибки?» без контекста даёт мусорный поиск; переписанный запрос («как обрабатываются ошибки авторизации в gymhero») ищется корректно.

```python
def chat(self, chat_id, user_msg, mode="fast", model=None):
    h = self.c.history; llms = self.c.llms
    model = model or (next(iter(llms)) if llms else None)
    history = self.get_messages(chat_id)
    first_turn = not history
    standalone = self._condense(history, user_msg)
    chunks = self.c.retriever.search(standalone, k=5, mode=mode)
```
- Выбор модели: явная или первая доступная, иначе `None`.
- Загрузка истории (через кэш состояния чата) → сжатие фоллоу-апа → поиск по самостоятельному запросу (обычный пайплайн, fast/thinking).

```python
    if model:
        msgs = ([{"role": "system", "content": _SYS}]
                + [{"role": m["role"], "content": m["content"]} for m in history[-6:]]
                + [{"role": "user", "content": f"{user_msg}\n\nКонтекст:\n{_ctx(chunks)}"}])
        answer = self.c.llms[model].chat(msgs)
    else:
        answer = "(LLM не настроена - показаны только найденные фрагменты.)"
```
- Промпт = system + окно истории (последние 6 реплик, только текст - без старого кода) + текущий вопрос с контекстом текущего хода. Окно без старого кода - чтобы контекст модели не раздувался; код подмешивается только за текущий ход. Без LLM возвращается заглушка (поиск всё равно отработал, цитаты есть).

```python
    h.append(chat_id, "user", user_msg)
    h.append(chat_id, "assistant", answer,
             citations=_citations(chunks), model=model, mode=mode)
    self._invalidate_chat(chat_id)
    if first_turn:
        h.rename(chat_id, self._gen_title(user_msg, model))
    return {"answer": answer, "citations": chunks}
```
- Сохраняются обе реплики; у ответа - `citations` (`_citations`: дедуп `[{chunk_id, score}]`) для перерисовки источников позже. После append сбрасывается кэш сообщений чата; на первом ходе чат получает имя от выбранной модели по первому сообщению. Возвращаются ответ и фрагменты.
- `chat_stream` - то же, но ответ по токенам; история и название пишутся после исчерпания стрима.

```python
def stats(self): ...
def index(self, folder, source, incremental=True):
    res = self.c.index_path(folder, source, self.c.store, self.c.embedder,
                            self.c.registry, incremental)
    self._invalidate_search()
    return res
def remove(self, source):
    res = self.c.remove_source(source, self.c.store, self.c.registry)
    self._invalidate_search()
    return res
```
- Админка: статистика, индексация (прокидывает компоненты в `pipeline.index_path`), удаление источника. После изменения индекса сдвигается index-epoch - кэш поиска осиротевает.
- ingest из админки (`ingest_zip`/`ingest_github`) ставится в `JobQueue` и идёт фоном; методы возвращают `job_id`, статус читается через `ingest_jobs`/`ingest_job`.

## `HttpBackend`
```python
def __init__(self, url):
    self.url = url.rstrip("/")
    self.token = None          # access-токен (Bearer), выставляется после login
def _headers(self):
    return {"Authorization": f"Bearer {self.token}"} if self.token else {}
def _post(self, path, payload):
    import requests
    return requests.post(f"{self.url}{path}", json=payload,
                         headers=self._headers(), timeout=180).json()
def _get(self, path):
    import requests
    return requests.get(f"{self.url}{path}", headers=self._headers(), timeout=30).json()
```
- Все запросы несут Bearer-токен (`_headers`), если он выставлен после `login`/`refresh`. `timeout=180` на POST - индексация/чат могут идти долго; `30` на GET - быстрые операции.

Далее каждый метод - зеркало `LocalBackend`, но по HTTP к `backend_app`:
```python
def search(self, query, k=5, mode="fast", flags=None, filters=None):
    return self._post("/search", {"query": query, "k": k, "mode": mode})["results"]
def chat(self, chat_id, user_msg, mode="fast", model=None):
    return self._post("/chat", {...})
def list_chats(self, user_id):
    return self._get("/chats")["chats"]   # пользователь берётся из access-токена
...
```
- Сигнатуры идентичны `LocalBackend` (тот же `BackendClient`), поэтому фронт пишется один раз. Различие только в том, исполняется ли логика в процессе или за сетью - это выбор `factory` по `role`. Чаты скоупятся по пользователю из access-токена, поэтому `user_id` в URL не нужен.
- `register`/`login`/`refresh`/`logout` - прокси на `/auth/*`; `login`/`refresh` запоминают access-токен в `self.token`, `logout` сбрасывает его.

`backend_app` (REST-сервис) внутри использует `LocalBackend`, а `HttpBackend` к нему обращается. Бизнес-логика написана один раз (`LocalBackend`), а `HttpBackend`/`backend_app` - транспортная обёртка.
