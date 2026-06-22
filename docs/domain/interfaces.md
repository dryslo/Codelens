# domain/interfaces.py - порты (абстрактные интерфейсы)

Основа чистой архитектуры: здесь только контракты, без реализаций. Любой адаптер (парсер, стор, LLM…) наследует свой ABC. Благодаря этому `factory` подставляет нужную реализацию, а верхние слои зависят только от интерфейса.

```python
from abc import ABC, abstractmethod
from src.domain.models import Chunk
```
- `ABC` + `@abstractmethod` - попытка создать класс без реализации абстрактного метода падает в рантайме: гарантия, что адаптер реализовал контракт.
- Импорт `Chunk` только для аннотаций типов.

### `Parser`
```python
class Parser(ABC):
    extensions: set[str]   # какие расширения обрабатывает: {".py"}
    lang: str              # метка языка для Chunk.lang
    @abstractmethod
    def parse(self, path, source, source_name) -> list[Chunk]: ...
```
- `extensions`/`lang` - атрибуты класса (реестр в `parsers/base.py` ищет парсер по расширению). `parse` принимает относительный путь, текст файла, имя источника → список чанков. Добавление языка = новый класс с этими тремя членами.

### `Embedder`
```python
def encode(self, texts: list[str], is_query: bool = False): ...
```
- `is_query` критичен для e5-моделей: запрос и документ префиксуются по-разному (`query:` / `passage:`). Дефолт `False` (passage) - индексация вызывается без флага, поиск - с `is_query=True`.

### `Reranker`
```python
def rerank(self, query, cands, k) -> list[dict]: ...
```
- Принимает кандидатов от вектор-поиска, возвращает топ-`k` пересортированных. Отдельный порт - реранкер опционален и подменяем (локальный/remote).

### `VectorStore`
```python
def add(...); def query(embedding, k); def delete_where(**conditions)
def count(); def list_sources()
```
- Минимальный набор операций: добавить, искать по вектору, удалить по условию (для переиндексации источника/файла), посчитать, перечислить источники. `delete_where(**conditions)` - kwargs, чтобы вызывать `delete_where(source=..., file=...)` единообразно для Chroma и Qdrant.

### `Retriever`
```python
def search(self, query, k, flags=None, mode=None, where=None) -> list[dict]: ...
```
- `mode` - `fast`/`thinking` (пресет флагов), `flags` переопределяет его поканально, `where` - фильтр по `lang`/`source`. Единая точка входа поиска для backend.

### `History` / `IndexRegistry`
- `History` - чаты и сообщения (CRUD под conversational RAG).
- `IndexRegistry` - хэши проиндексированных файлов (для инкрементальности): `get_hash/set_hash/files/remove`. Оба реализуются на SQLAlchemy (один код на SQLite/Postgres).

### `LLMProvider`
```python
def chat(messages); def hyde(query); def multiquery(query, n)
```
- `chat` - базовая операция; `hyde`/`multiquery` - для думающего режима. В `BaseLLM` последние два построены поверх `chat`, так что новому провайдеру достаточно реализовать только `chat`.
- `chat_stream` - стриминг ответа по токенам; не абстрактный, дефолт отдаёт весь ответ одним чанком, провайдер переопределяет при поддержке стрима.

### `SessionStore`
- `get/set(ttl)` - абстракция кэша/сессий (Redis в large, in-process в small). Используется для cache-aside поиска/ответов, реестра индекса и access-сессий auth.

### `BackendClient`
- Полный контракт бэка (search/chat/list_chats/create_chat/get_messages/list_llms/answer/stats/index/remove). Две реализации: `LocalBackend` (в процессе) и `HttpBackend` (по HTTP). Фронт зависит только от этого интерфейса → переключение «в процессе ↔ по сети» не меняет UI.

Большое число мелких интерфейсов объясняется тем, что каждый - отдельная ось расширения/замены (стор, эмбеддер, LLM, фронт↔бэк). Это и есть «единая архитектура, меняются детали за портами».
