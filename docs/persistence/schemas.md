# persistence/schemas.py - Pydantic-схемы API

Контракты запросов REST-бэка. FastAPI по ним валидирует тело запроса и генерит OpenAPI-документацию.

```python
class SearchReq(BaseModel):
    query: str
    k: int = 5
    mode: str = "fast"
```
- Тело `POST /search`. Поля с дефолтами (`k`, `mode`) необязательны в запросе; `query` обязателен (нет дефолта → FastAPI вернёт 422 без него). Типы (`str`/`int`) автоматически проверяются.

```python
class ChatReq(BaseModel):
    chat_id: str
    user_msg: str
    mode: str = "fast"
    model: str | None = None
```
- Тело `POST /chat`. `model: str | None = None` - модель LLM можно не указывать (бэк возьмёт первую доступную).

```python
class CreateChatReq(BaseModel):
    user_id: str = "anon"
    title: str = "Новый чат"
```
- `POST /chats`. Дефолты позволяют создать чат вообще без тела.

```python
class IndexReq(BaseModel):
    folder: str
    source: str
    incremental: bool = True

class RemoveReq(BaseModel):
    source: str

class AnswerReq(BaseModel):
    query: str
    chunks: list[dict]
    model: str
```
- Админ-операции и генерация ответа. `AnswerReq.chunks: list[dict]` - фронт присылает уже найденные фрагменты, бэк только генерит по ним ответ (не ищет заново).

Почему Pydantic, а не ручной разбор JSON: валидация типов и обязательности без отдельного кода, автодокументация Swagger, единый источник формы запроса для клиента и сервера. Это «P» из связки SQLAlchemy (ORM для БД) + Pydantic (схемы API).
