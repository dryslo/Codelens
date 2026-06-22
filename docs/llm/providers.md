# llm - base.py, ollama.py, openai_compatible.py

Провайдеры LLM (порт `LLMProvider`). Базовый класс реализует `hyde`/`multiquery` поверх `chat`, наследники реализуют только `chat`.

## base.py - `BaseLLM`
```python
class BaseLLM(LLMProvider):
    def chat(self, messages):
        raise NotImplementedError
```
- `chat` - единственная операция, которую обязан реализовать наследник. Принимает список сообщений `[{"role","content"}, ...]` (OpenAI-формат), возвращает текст ответа.

```python
    def hyde(self, query):
        return self.chat([{"role": "user", "content":
            "Напиши короткий гипотетический фрагмент Python-кода (без пояснений), "
            f"который отвечал бы на вопрос:\n{query}"}])
```
- HyDE-промпт: запрашивается короткий код без пояснений (для эмбеддинга нужен текст, похожий на код, а не объяснение). Реализован один раз здесь → работает для всех провайдеров.

```python
    def multiquery(self, query, n=3):
        out = self.chat([{"role": "user", "content":
            f"Дай {n} разных переформулировок запроса (RU и EN), по одной на строку, без нумерации:\n{query}"}])
        variants = [s.strip() for s in out.splitlines() if s.strip()]
        return [query] + variants[:n]
```
- Запрашивается `n` переформулировок по строке. Ответ разбирается по строкам, пустые отбрасываются. Возвращается `[исходный_запрос] + варианты` - исходник всегда включён, чтобы не потерять оригинальную формулировку.

Почему hyde/multiquery в базовом классе: это специальные промпты поверх `chat`; иначе их пришлось бы дублировать в каждом провайдере. Новый провайдер реализует `chat` - и сразу умеет HyDE/multi-query.

## ollama.py - `OllamaLLM`
```python
def __init__(self, model, url="http://localhost:11434", **_):
    self.model = model
    self.url = url.rstrip("/")
```
- `**_` - проглатывает лишние ключи из конфига (например `kind`), чтобы конструктор не падал на незнакомых параметрах. Это позволяет фабрике звать `OllamaLLM(**spec)` без фильтрации (хотя она и фильтрует `kind`).

```python
def chat(self, messages):
    import requests
    r = requests.post(f"{self.url}/api/chat",
                      json={"model": self.model, "messages": messages, "stream": False},
                      timeout=120)
    r.raise_for_status()
    return r.json()["message"]["content"]
```
- POST на локальный Ollama `/api/chat`. `stream: False` - ожидается полный ответ, без разбора потока. `timeout=120` - LLM может думать долго. `raise_for_status()` - превращает HTTP-ошибку в исключение (чтобы degradable-обёртки в HyDE/answer его поймали). Ответ Ollama: `{"message": {"content": ...}}`.

## openai_compatible.py - `OpenAICompatibleLLM`
```python
def __init__(self, model, base_url="https://api.openai.com/v1",
             api_key_env="OPENAI_API_KEY", **_):
    self.model = model; self.base_url = base_url; self.api_key_env = api_key_env
```
- Один класс на все OpenAI-совместимые API: Groq, Gemini (OpenAI-режим), Mistral, OpenRouter, сам OpenAI. Различие - `base_url` и имя переменной с ключом (`api_key_env`). Сам ключ не хранится в конфиге, а читается из окружения по имени - секреты не попадают в репозиторий.

```python
def _client(self):
    from openai import OpenAI
    return OpenAI(base_url=self.base_url, api_key=os.environ.get(self.api_key_env, ""))

def chat(self, messages):
    resp = self._client().chat.completions.create(model=self.model, messages=messages)
    return resp.choices[0].message.content

def chat_stream(self, messages):
    stream = self._client().chat.completions.create(
        model=self.model, messages=messages, stream=True)
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta
```
- Клиент создаётся с нужным `base_url` и ключом из `os.environ`. `chat.completions.create` - стандартный вызов; берётся `choices[0].message.content`. Ленивый `import openai` - чтобы базовый профиль не требовал пакет, если облачные LLM не используются.
- `chat_stream` - тот же вызов со `stream=True`: ответ отдаётся по дельтам токенов (для потокового вывода в UI). Пустые дельты пропускаются.

Где собираются: `factory.build_llms(cfg)` читает `llm.providers`, по `kind` создаёт нужный класс, складывает в `{имя: провайдер}`. Недоступный или некорректный провайдер ловится `try/except` и не попадает в список (UI его не покажет - degradable).
