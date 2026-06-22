"""Векторное хранилище на Chroma (профиль small, embedded)."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from src.domain.interfaces import VectorStore


def _chroma_where(where: dict | None) -> dict | None:
    """Транслировать {field: [values]} в where-клаузу Chroma ($in, при двух полях $and)."""
    if not where:
        return None
    clauses = [{field: {"$in": vals}} for field, vals in where.items() if vals]
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


class ChromaStore(VectorStore):
    """Профиль small: embedded, без сервера."""

    def __init__(self, path: str = ".chroma", name: str = "code") -> None:
        import chromadb
        self.col = chromadb.PersistentClient(path=path).get_or_create_collection(
            name, metadata={"hnsw:space": "cosine"})

    def add(self, ids: list[str], embeddings: Any, metadatas: list[dict],
            documents: list[str]) -> None:
        """Загрузить чанки с эмбеддингами и метаданными в коллекцию."""
        self.col.add(ids=ids, embeddings=[e.tolist() for e in embeddings],
                     metadatas=metadatas, documents=documents)

    def query(self, embedding: Any, k: int = 20, where: dict | None = None) -> list[dict]:
        """Вернуть k ближайших чанков к вектору запроса (опц. фильтр по lang/source)."""
        r = self.col.query(query_embeddings=[embedding.tolist()], n_results=k,
                           where=_chroma_where(where))
        if not r["ids"] or not r["ids"][0]:
            return []
        out = []
        for i in range(len(r["ids"][0])):
            meta = r["metadatas"][0][i]
            out.append({"chunk_id": meta.get("chunk_id", r["ids"][0][i]),
                        "code": r["documents"][0][i], "meta": meta,
                        "distance": r["distances"][0][i]})
        return out

    def iter_all(self) -> Iterator[dict]:
        """Итерировать все чанки коллекции."""
        r = self.col.get(include=["documents", "metadatas"]) or {}
        ids = r.get("ids") or []
        docs = r.get("documents") or []
        metas = r.get("metadatas") or []
        for i in range(len(ids)):
            meta = metas[i] if i < len(metas) else {}
            yield {"chunk_id": meta.get("chunk_id", ids[i]),
                   "code": docs[i] if i < len(docs) else "", "meta": meta}

    def get_embeddings(self, ids: list[str]) -> dict:
        """Вернуть отображение chunk_id -> вектор для заданных id."""
        if not ids:
            return {}
        import numpy as np
        # В col id != chunk_id (есть префикс source::), поэтому ищем по chunk_id через where.
        if len(ids) == 1:
            r = self.col.get(where={"chunk_id": ids[0]},
                             include=["embeddings", "metadatas"])
        else:
            r = self.col.get(where={"chunk_id": {"$in": list(ids)}},
                             include=["embeddings", "metadatas"])
        out = {}
        embs = r.get("embeddings")
        metas = r.get("metadatas") or []
        if embs is None:
            return out
        for i in range(len(embs)):
            cid = (metas[i] or {}).get("chunk_id") if i < len(metas) else None
            if cid is not None:
                out[cid] = np.asarray(embs[i])
        return out

    def delete_where(self, **conds: Any) -> None:
        """Удалить документы по равенству полей метаданных (no-match - no-op у chroma)."""
        flt = [{k: v} for k, v in conds.items()]
        self.col.delete(where=flt[0] if len(flt) == 1 else {"$and": flt})

    def count(self) -> int:
        """Вернуть число документов в коллекции."""
        return self.col.count()

    def list_sources(self) -> list[str]:
        """Вернуть отсортированный список источников."""
        metas = self.col.get(include=["metadatas"]).get("metadatas") or []
        return sorted({m.get("source", "") for m in metas if m and m.get("source")})

    def list_langs(self) -> list[str]:
        """Вернуть отсортированный список языков в индексе."""
        metas = self.col.get(include=["metadatas"]).get("metadatas") or []
        return sorted({m.get("lang", "") for m in metas if m and m.get("lang")})
