# persistence/orm.py - SQLAlchemy-модели

Декларативные ORM-модели. Почему ORM, а не сырой SQL: один код работает на SQLite (dev/small) и Postgres (large) - отличается только строка подключения; типобезопасно; миграции через Alembic читают эти же модели.

```python
from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass
```
- `DeclarativeBase` (стиль SQLAlchemy 2.0) - общий базовый класс; `Base.metadata` хранит описание всех таблиц (его читает Alembic для автогенерации миграций).
- `Mapped[...]` + `mapped_column(...)` - типизированный способ объявления колонок 2.0 (статическая проверка типов + явные ограничения).

```python
class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    login: Mapped[str] = mapped_column(String, unique=True)
```
- Минимальный `User` (под будущую авторизацию). `id` - первичный ключ, `login` - уникальный.

```python
class Chat(Base):
    __tablename__ = "chats"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    messages: Mapped[list["Message"]] = relationship(back_populates="chat")
```
- `user_id` с `index=True` - по нему фильтруем чаты пользователя (индекс ускоряет `list_chats`).
- `created_at` с `server_default=func.now()` - время ставит БД при вставке (надёжнее, чем питон-время; единообразно при нескольких бэкендах).
- `messages = relationship(back_populates="chat")` - ORM-связь «один-ко-многим»: `chat.messages` даёт список сообщений; `back_populates` связывает с обратной ссылкой `Message.chat`.

```python
class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id"), index=True)
    role: Mapped[str] = mapped_column(String)            # user | assistant
    content: Mapped[str] = mapped_column(Text)
    retrieved_ids: Mapped[str | None] = mapped_column(Text)   # JSON-список цитат
    model: Mapped[str | None] = mapped_column(String)
    mode: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    chat: Mapped["Chat"] = relationship(back_populates="messages")
```
- `chat_id` - внешний ключ на `chats.id`, с индексом (грузим сообщения чата по нему).
- `content` - `Text` (длинный текст), а не `String` (ограниченная длина).
- `retrieved_ids` - JSON-строка со списком `chunk_id`, на которые опирался ассистент (цитаты). Почему строкой, а не отдельной таблицей: читается одним полем; нормализация избыточна для чат-истории.
- `model`/`mode` - какой LLM и в каком режиме отвечал (полезно для анализа). Nullable (`| None`).

```python
class IndexedFile(Base):
    __tablename__ = "indexed_files"
    source: Mapped[str] = mapped_column(String, primary_key=True)
    file: Mapped[str] = mapped_column(String, primary_key=True)
    hash: Mapped[str] = mapped_column(String)
```
- Реестр для инкрементальной индексации. Составной первичный ключ `(source, file)` - один файл уникален в пределах источника. `hash` - SHA1 содержимого; пайплайн сравнивает с ним, чтобы пропустить неизменённые файлы.

Подводный камень: при изменении моделей нужна миграция (`make migration m="..."`), иначе схема БД и код разойдутся. В dev `factory` вызывает `init_db` (create_all) для удобства, но в проде источник правды - Alembic.
