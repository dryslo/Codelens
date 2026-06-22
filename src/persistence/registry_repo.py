"""Реализации `IndexRegistry`: кэширующая обёртка и SQL-бэкенд."""
from typing import Any

from src.domain.interfaces import IndexRegistry
from src.persistence.orm import IndexedFile


class CachingRegistry(IndexRegistry):
    """Кэш `source`+`file` → `hash` поверх любого `IndexRegistry`.

    Инкрементальная индексация спрашивает хэш на каждый файл ([pipeline.index_path]);
    кэш гасит обращения к БД на неизменных файлах. Запись/удаление синхронно обновляют
    кэш, поэтому stale-хэшей не возникает.
    """

    def __init__(self, base: IndexRegistry, cache: Any, ttl: int = 3600) -> None:
        self.base = base
        self.cache = cache
        self.ttl = ttl

    @staticmethod
    def _key(source: str, file: str) -> str:
        return f"reg:{source}:{file}"

    def get_hash(self, source: str, file: str) -> str | None:
        """Вернуть хэш файла из кэша или базы."""
        cached = self.cache.get(self._key(source, file))
        if cached is not None:
            return cached
        h = self.base.get_hash(source, file)
        if h is not None:
            self.cache.set(self._key(source, file), h, ttl=self.ttl)
        return h

    def set_hash(self, source: str, file: str, h: str) -> None:
        """Записать хэш в базу и кэш."""
        self.base.set_hash(source, file, h)
        self.cache.set(self._key(source, file), h, ttl=self.ttl)

    def files(self, source: str) -> list[str]:
        """Вернуть файлы источника."""
        return self.base.files(source)

    def remove(self, source: str, file: str | None = None) -> None:
        """Удалить источник/файл и проставить tombstone в кэше."""
        targets = [file] if file else self.base.files(source)
        self.base.remove(source, file)
        for f in targets:                       # tombstone: следующий get_hash уйдёт в базу
            self.cache.set(self._key(source, f), None, ttl=self.ttl)


class SqlRegistry(IndexRegistry):
    """SQL-реестр проиндексированных файлов поверх таблицы `indexed_files`."""

    def __init__(self, session_factory: Any) -> None:
        self.Session = session_factory

    def get_hash(self, source: str, file: str) -> str | None:
        """Вернуть хэш файла из БД или None."""
        with self.Session() as s:
            row = s.get(IndexedFile, (source, file))
            return row.hash if row else None

    def set_hash(self, source: str, file: str, h: str) -> None:
        """Записать или обновить хэш файла."""
        with self.Session() as s:
            row = s.get(IndexedFile, (source, file))
            if row:
                row.hash = h
            else:
                s.add(IndexedFile(source=source, file=file, hash=h))
            s.commit()

    def files(self, source: str) -> list[str]:
        """Вернуть файлы источника."""
        with self.Session() as s:
            return [r.file for r in s.query(IndexedFile).filter_by(source=source).all()]

    def remove(self, source: str, file: str | None = None) -> None:
        """Удалить источник целиком или конкретный файл."""
        with self.Session() as s:
            q = s.query(IndexedFile).filter_by(source=source)
            if file:
                q = q.filter_by(file=file)
            q.delete()
            s.commit()
