"""Доменные интерфейсы (порты): парсеры, поиск, история, кэш, очередь, LLM."""
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

from src.domain.models import Chunk


class Parser(ABC):
    """Парсер исходников: режет файл на чанки (function | class | method)."""

    extensions: set[str]
    lang: str

    @abstractmethod
    def parse(self, path: str, source: str, source_name: str) -> list[Chunk]:
        """Разобрать `source` в список чанков."""
        ...


class Embedder(ABC):
    """Кодировщик текста в эмбеддинги."""

    @abstractmethod
    def encode(self, texts: list[str], is_query: bool = False) -> Any:
        """Закодировать тексты в векторы."""
        ...


class Reranker(ABC):
    """Переранжировщик кандидатов под запрос."""

    @abstractmethod
    def rerank(self, query: str, cands: list[dict], k: int) -> list[dict]:
        """Переранжировать кандидатов и вернуть top-k."""
        ...


class VectorStore(ABC):
    """Хранилище векторов с поиском по близости и фильтрами."""

    @abstractmethod
    def add(self, ids: Any, embeddings: Any, metadatas: Any, documents: Any) -> None:
        """Добавить записи в хранилище."""
        ...
    @abstractmethod
    def query(self, embedding: Any, k: int, where: dict | None = None) -> list[dict]:
        """Найти top-k ближайших к `embedding`; where - опц. фильтр {lang:[...], source:[...]}."""
        ...
    @abstractmethod
    def iter_all(self) -> Any:
        """Проитерировать все записи хранилища."""
        ...
    @abstractmethod
    def get_embeddings(self, ids: list[str]) -> dict:
        """Вернуть эмбеддинги по идентификаторам."""
        ...
    @abstractmethod
    def delete_where(self, **conditions: Any) -> None:
        """Удалить записи по условиям."""
        ...
    @abstractmethod
    def count(self) -> int:
        """Вернуть число записей."""
        ...
    @abstractmethod
    def list_sources(self) -> list[str]:
        """Вернуть список источников."""
        ...
    @abstractmethod
    def list_langs(self) -> list[str]:
        """Вернуть список языков в индексе."""
        ...


class Retriever(ABC):
    """Поисковик по индексу."""

    @abstractmethod
    def search(
        self, query: str, k: int, flags: Any = None, mode: str | None = None,
        where: dict | None = None,
    ) -> list[dict]:
        """Найти top-k чанков под запрос; where - опц. фильтр по lang/source."""
        ...


class History(ABC):
    """История чатов и сообщений."""

    @abstractmethod
    def create_chat(self, user_id: str, title: str) -> str:
        """Создать чат и вернуть его идентификатор."""
        ...
    @abstractmethod
    def list_chats(self, user_id: str) -> list[dict]:
        """Вернуть чаты пользователя."""
        ...
    @abstractmethod
    def get_messages(self, chat_id: str) -> list[dict]:
        """Вернуть сообщения чата."""
        ...
    @abstractmethod
    def append(
        self,
        chat_id: str,
        role: str,
        content: str,
        citations: list[dict] | None = None,
        model: str | None = None,
        mode: str | None = None,
    ) -> None:
        """Добавить сообщение в чат; citations - [{chunk_id, score}] цитат ответа."""
        ...
    @abstractmethod
    def rename(self, chat_id: str, title: str) -> None:
        """Переименовать чат."""
        ...
    @abstractmethod
    def delete_chat(self, chat_id: str) -> None:
        """Удалить чат и его сообщения."""
        ...


class IndexRegistry(ABC):
    """Реестр проиндексированных файлов (source+file → hash)."""

    @abstractmethod
    def get_hash(self, source: str, file: str) -> str | None:
        """Вернуть хэш файла или None."""
        ...
    @abstractmethod
    def set_hash(self, source: str, file: str, h: str) -> None:
        """Записать хэш файла."""
        ...
    @abstractmethod
    def files(self, source: str) -> list[str]:
        """Вернуть файлы источника."""
        ...
    @abstractmethod
    def remove(self, source: str, file: str | None = None) -> None:
        """Удалить источник целиком или конкретный файл."""
        ...


class LLMProvider(ABC):
    """Провайдер LLM: чат, HyDE, multi-query."""

    @abstractmethod
    def chat(self, messages: list[dict]) -> str:
        """Вернуть ответ модели на диалог."""
        ...
    @abstractmethod
    def hyde(self, query: str) -> str:
        """Сгенерировать гипотетический документ под запрос."""
        ...
    @abstractmethod
    def multiquery(self, query: str, n: int) -> list[str]:
        """Сгенерировать n переформулировок запроса."""
        ...

    def chat_stream(self, messages: list[dict]) -> Iterator[str]:
        """Стриминг ответа по токенам. По умолчанию - один чанк (весь ответ)."""
        yield self.chat(messages)


class SessionStore(ABC):
    """Кэш/сессии ключ-значение с TTL."""

    @abstractmethod
    def get(self, key: str) -> Any:
        """Вернуть значение по ключу или None."""
        ...
    @abstractmethod
    def set(self, key: str, value: Any, ttl: int = 3600) -> None:
        """Записать значение с временем жизни."""
        ...


class JobQueue(ABC):
    """Фоновые задачи (ingest кодовой базы).

    Размещение задаётся конфигом:
    InProcessQueue (small/dev) | RedisQueue/RQ (large). task - сериализуемый дескриптор
    ({kind, source, data|url+ref}), исполняется общим src.ingest.runner.run_ingest
    (RQ не умеет замыкания, поэтому передаются данные, а не функция).
    """

    @abstractmethod
    def submit(self, task: dict) -> str:
        """Поставить задачу и вернуть её идентификатор."""
        ...
    @abstractmethod
    def get(self, job_id: str) -> dict | None:
        """Вернуть состояние задачи или None."""
        ...
    @abstractmethod
    def list(self) -> list[dict]:
        """Вернуть список задач."""
        ...


class BackendClient(ABC):
    """Клиент backend-сервиса (фасад для фронтенда/CLI)."""

    @abstractmethod
    def search(
        self, query: str, k: int = 5, mode: str = "fast", flags: Any = None,
        filters: dict | None = None,
    ) -> list[dict]:
        """Найти top-k чанков под запрос; filters - опц. {lang:[...], source:[...]}."""
        ...
    @abstractmethod
    def chat(
        self, chat_id: str, user_msg: str, mode: str = "fast", model: str | None = None,
    ) -> dict:
        """Отправить сообщение в чат и вернуть ответ."""
        ...
    @abstractmethod
    def chat_stream(
        self, chat_id: str, user_msg: str, mode: str = "fast", model: str | None = None,
    ) -> Iterator[str]:
        """Стриминг ответа чата по токенам."""
        ...
    @abstractmethod
    def list_chats(self, user_id: str) -> list[dict]:
        """Вернуть чаты пользователя."""
        ...
    @abstractmethod
    def create_chat(self, user_id: str, title: str) -> str:
        """Создать чат и вернуть его идентификатор."""
        ...
    @abstractmethod
    def get_messages(self, chat_id: str) -> list[dict]:
        """Вернуть сообщения чата."""
        ...
    @abstractmethod
    def delete_chat(self, chat_id: str) -> dict:
        """Удалить чат."""
        ...
    @abstractmethod
    def list_llms(self) -> list[str]:
        """Вернуть доступные модели."""
        ...
    @abstractmethod
    def answer(self, query: str, chunks: list[dict], model: str) -> str:
        """Сгенерировать ответ по запросу и чанкам."""
        ...
    @abstractmethod
    def answer_stream(self, query: str, chunks: list[dict], model: str) -> Iterator[str]:
        """Стриминг ответа по запросу и чанкам."""
        ...
    @abstractmethod
    def stats(self) -> dict:
        """Вернуть статистику индекса."""
        ...
    @abstractmethod
    def index(self, folder: str, source: str, incremental: bool = True) -> dict:
        """Запустить индексацию папки."""
        ...
    @abstractmethod
    def remove(self, source: str) -> dict:
        """Удалить источник из индекса."""
        ...
    @abstractmethod
    def flag_policy(self) -> dict:
        """Вернуть политику флагов поиска."""
        ...
    @abstractmethod
    def ingest_zip(self, data: bytes, source: str) -> dict:
        """Поставить задачу ingest из zip-архива."""
        ...
    @abstractmethod
    def ingest_github(self, url: str, ref: str | None, source: str) -> dict:
        """Поставить задачу ingest из GitHub-репозитория."""
        ...
    @abstractmethod
    def ingest_jobs(self) -> list[dict]:
        """Вернуть список задач ingest."""
        ...
    @abstractmethod
    def ingest_job(self, job_id: str) -> dict | None:
        """Вернуть состояние задачи ingest или None."""
        ...
