"""Индексирование кодовой базы: парсинг файлов, обогащение, батч-эмбеддинг в стор."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from src.indexing.enrich import enrich
from src.indexing.parsers.base import get_parser

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.domain.interfaces import Embedder, IndexRegistry, VectorStore
    from src.domain.models import Chunk


def _meta(c: Chunk) -> dict:
    """Метаданные чанка для записи в стор (chunk_id в формате scorer)."""
    return {
        "chunk_id": c.chunk_id,            # формат scorer
        "source": c.source, "lang": c.lang, "file": c.file, "type": c.type,
        "name": c.name, "parent": c.parent or "", "start_line": c.start_line,
        "end_line": c.end_line, "docstring": c.docstring or "",
        "calls": ",".join(c.calls),
    }


def _store_id(c: Chunk) -> str:
    """Уникальный ключ чанка в сторе при нескольких источниках (chunk_id остаётся в meta)."""
    return f"{c.source}::{c.chunk_id}"


def index_path(folder: str, source: str, store: VectorStore, embedder: Embedder,
               registry: IndexRegistry, incremental: bool = True, batch: int = 64,
               progress: Callable[[dict], None] | None = None) -> dict:
    """Проиндексировать папку: парсинг, эмбеддинг батчами, инкрементальная синхронизация."""
    root = Path(folder)
    files = [p for p in root.rglob("*") if p.is_file() and get_parser(p.suffix)]
    current = {p.relative_to(root).as_posix() for p in files}
    added = updated = skipped = 0
    total_files = len(files)
    files_done = embedded = 0

    # Копим чанки всего корпуса перед эмбеддингом: общее число чанков становится известно
    # до тяжёлой части, и прогресс идёт по добавленным чанкам, а не по файлам.
    texts: list[str] = []
    ids: list[str] = []
    metas: list[dict] = []
    codes: list[str] = []

    def _report() -> None:
        # chunks_total = len(texts): в фазе парсинга растёт, в фазе эмбеддинга фиксирован.
        # chunks_indexed = embedded: двигается только в фазе эмбеддинга (тяжёлая часть).
        if progress is not None:
            progress({"files_done": files_done, "files_total": total_files,
                      "chunks_total": len(texts), "chunks_indexed": embedded,
                      "added": added, "updated": updated, "skipped": skipped})

    # Шаг 1: парсинг изменённых файлов (лёгкая), сбор чанков в буферы.
    for i, p in enumerate(files, 1):
        files_done = i
        rel = p.relative_to(root).as_posix()
        text = p.read_text(encoding="utf-8", errors="ignore")
        h = hashlib.sha1(text.encode()).hexdigest()
        prev = registry.get_hash(source, rel)
        if incremental and prev == h:
            skipped += 1
            _report()
            continue
        store.delete_where(source=source, file=rel)
        chunks = get_parser(p.suffix).parse(rel, text, source)
        for c in chunks:
            texts.append(enrich(c))
            ids.append(_store_id(c))
            metas.append(_meta(c))
            codes.append(c.code)
        updated += 1 if prev else 0
        added += 0 if prev else 1
        registry.set_hash(source, rel, h)
        _report()

    # Шаг 2: эмбеддинг батчами, прогресс по чанкам - _report после каждого батча, чтобы
    # бар двигался (батч умеренный: компромисс throughput encode против частоты обновлений).
    for i in range(0, len(texts), batch):
        sl = slice(i, i + batch)
        embs = embedder.encode(texts[sl], is_query=False)
        store.add(ids[sl], embs, metas[sl], codes[sl])
        embedded += len(ids[sl])
        _report()

    for rel in set(registry.files(source)) - current:
        store.delete_where(source=source, file=rel)
        registry.remove(source, rel)
    return {"added": added, "updated": updated, "skipped": skipped, "total": store.count()}


def remove_source(source: str, store: VectorStore, registry: IndexRegistry) -> dict:
    """Удалить все чанки источника из стора и реестра."""
    store.delete_where(source=source)
    registry.remove(source)
    return {"removed": source, "total": store.count()}
