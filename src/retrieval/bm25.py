"""BM25 поверх корпуса векторного стора (bm25s, sparse).

Токенизация код-ориентированная: snake_case и camelCase бьются на части, оригинальные
идентификаторы тоже остаются (для точного матча).

Индекс строится лениво один раз на процесс. Тексты чанков держатся в памяти, чтобы после
фьюжна с dense-каналом отдать готовый dict без повторного запроса в стор.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.domain.interfaces import VectorStore

_WORD_RE = re.compile(r"[A-Za-zА-Яа-я_][\w]*", re.UNICODE)
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def tokenize(text: str) -> list[str]:
    """Код-ориентированная токенизация: snake_case/camelCase + оригинальные идентификаторы."""
    out: list[str] = []
    for tok in _WORD_RE.findall(text or ""):
        low = tok.lower()
        out.append(low)
        for part in tok.split("_"):
            if not part:
                continue
            lp = part.lower()
            if lp != low:
                out.append(lp)
            for sub in _CAMEL_RE.findall(part):
                ls = sub.lower()
                if ls != lp:
                    out.append(ls)
    return out


class BM25Index:
    """Ленивый BM25-индекс поверх корпуса векторного стора."""

    def __init__(self, store: VectorStore) -> None:
        self.store = store
        self._bm25 = None
        self._chunks: list[dict] = []

    def _ensure(self) -> None:
        if self._bm25 is not None:
            return
        import bm25s

        chunks = [c for c in self.store.iter_all() if c.get("code")]
        self._chunks = chunks
        if not chunks:
            self._bm25 = bm25s.BM25()
            self._bm25.index([[""]])
            return
        corpus_tokens = [tokenize(c["code"]) for c in chunks]
        self._bm25 = bm25s.BM25()
        self._bm25.index(corpus_tokens)

    def search(self, query: str, k: int = 50) -> list[dict]:
        """Топ-k чанков по BM25 для запроса (chunk-dict со score)."""
        self._ensure()
        if not self._chunks:
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        docs, scores = self._bm25.retrieve([q_tokens], k=min(k, len(self._chunks)))
        out = []
        for idx, score in zip(docs[0], scores[0]):
            if score <= 0:
                break
            c = dict(self._chunks[int(idx)])
            c["score"] = float(score)
            out.append(c)
        return out
