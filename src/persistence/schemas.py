"""Pydantic-схемы запросов HTTP-API."""
from pydantic import BaseModel


class SearchReq(BaseModel):
    """Запрос поиска."""

    query: str
    k: int = 5
    mode: str = "fast"
    flags: dict | None = None
    filters: dict | None = None   # {lang: [...], source: [...]}


class ChatReq(BaseModel):
    """Запрос сообщения в чат."""

    chat_id: str
    user_msg: str
    mode: str = "fast"
    model: str | None = None


class CreateChatReq(BaseModel):
    """Запрос создания чата."""

    user_id: str = "anon"
    title: str = "Новый чат"


class IndexReq(BaseModel):
    """Запрос индексации папки."""

    folder: str
    source: str
    incremental: bool = True


class RemoveReq(BaseModel):
    """Запрос удаления источника."""

    source: str


class IngestGithubReq(BaseModel):
    """Запрос ingest из GitHub-репозитория."""

    url: str
    source: str
    ref: str | None = None


class AnswerReq(BaseModel):
    """Запрос генерации ответа по чанкам."""

    query: str
    chunks: list[dict]
    model: str
