"""Векторное хранилище на Qdrant (профиль large)."""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

from src.domain.interfaces import VectorStore


def _id(cid: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, cid))


class QdrantStore(VectorStore):
    """Профиль large: сервер/кластер (shards + replicas)."""

    def __init__(self, url: str, name: str = "code", dim: int = 1024,
                 shards: int = 2, replicas: int = 2) -> None:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams
        self.q = QdrantClient(url=url)
        self.name = name
        if not self.q.collection_exists(name):
            self.q.create_collection(
                name,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
                shard_number=shards, replication_factor=replicas,
            )

    def add(self, ids: list[str], embeddings: Any, metadatas: list[dict],
            documents: list[str]) -> None:
        """Загрузить чанки с эмбеддингами и метаданными в коллекцию."""
        from qdrant_client.models import PointStruct
        pts = [PointStruct(id=_id(cid), vector=e.tolist(),
                           payload={**m, "code": d})   # m содержит chunk_id
               for cid, e, m, d in zip(ids, embeddings, metadatas, documents)]
        self.q.upsert(self.name, pts)

    def query(self, embedding: Any, k: int = 20, where: dict | None = None) -> list[dict]:
        """Вернуть k ближайших чанков к вектору запроса (опц. фильтр по lang/source)."""
        res = self.q.query_points(self.name, query=embedding.tolist(), limit=k,
                                  query_filter=self._filter(where), with_payload=True).points
        return [{"chunk_id": p.payload.get("chunk_id"), "code": p.payload.get("code"),
                 "meta": p.payload, "distance": 1 - p.score} for p in res]

    @staticmethod
    def _filter(where: dict | None) -> Any:
        """Собрать Qdrant Filter из {field: [values]} (MatchAny по каждому полю)."""
        if not where:
            return None
        from qdrant_client.models import FieldCondition, Filter, MatchAny
        must = [FieldCondition(key=field, match=MatchAny(any=vals))
                for field, vals in where.items() if vals]
        return Filter(must=must) if must else None

    def iter_all(self) -> Iterator[dict]:
        """Итерировать все чанки коллекции (без векторов)."""
        offset = None
        while True:
            points, offset = self.q.scroll(self.name, limit=256, offset=offset,
                                           with_payload=True, with_vectors=False)
            for p in points:
                payload = p.payload or {}
                yield {"chunk_id": payload.get("chunk_id"),
                       "code": payload.get("code", ""), "meta": payload}
            if offset is None:
                break

    def get_embeddings(self, ids: list[str]) -> dict:
        """Вернуть отображение chunk_id -> вектор для заданных id."""
        if not ids:
            return {}
        import numpy as np
        pts = self.q.retrieve(self.name, [_id(c) for c in ids],
                              with_vectors=True, with_payload=True)
        out = {}
        for p in pts:
            cid = (p.payload or {}).get("chunk_id")
            if cid is not None and p.vector is not None:
                out[cid] = np.asarray(p.vector)
        return out

    def delete_where(self, **conds: Any) -> None:
        """Удалить точки по равенству полей payload."""
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        self.q.delete(self.name, points_selector=Filter(
            must=[FieldCondition(key=k, match=MatchValue(value=v)) for k, v in conds.items()]))

    def count(self) -> int:
        """Вернуть число точек в коллекции."""
        return self.q.count(self.name).count

    def _distinct(self, field: str) -> list[str]:
        """Уникальные непустые значения поля payload (scroll только по этому полю)."""
        out: set[str] = set()
        offset = None
        while True:
            points, offset = self.q.scroll(self.name, limit=256, offset=offset,
                                           with_payload=[field], with_vectors=False)
            for p in points:
                v = (p.payload or {}).get(field)
                if v:
                    out.add(v)
            if offset is None:
                break
        return sorted(out)

    def list_sources(self) -> list[str]:
        """Вернуть список источников из payload коллекции."""
        return self._distinct("source")

    def list_langs(self) -> list[str]:
        """Вернуть список языков из payload коллекции."""
        return self._distinct("lang")
