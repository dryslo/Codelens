"""Локальный эмбеддер на sentence-transformers с префиксами query/passage."""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.domain.interfaces import Embedder
from src.util.model_cache import cached_sentence_transformer

if TYPE_CHECKING:
    import numpy as np

# Семейства, требующие префиксов query/passage.
# Ключ ищется подстрокой в имени модели (lower-case), значение - (query-префикс, doc-префикс).
PREFIXES = {
    "e5":    ("query: ",        "passage: "),
    "frida": ("search_query: ", "search_document: "),
}


def prefixes_for(model: str) -> tuple[str, str] | None:
    """(query-префикс, doc-префикс) для модели или None, если префиксы не нужны."""
    m = model.lower()
    return next((p for k, p in PREFIXES.items() if k in m), None)


class LocalEmbedder(Embedder):
    """e5 и FRIDA требуют префиксов query/passage; для прочих моделей не добавляются."""

    def __init__(self, model: str = "intfloat/multilingual-e5-large", batch_size: int = 32) -> None:
        """Загрузить модель (с кэшем) и определить префиксы по её имени."""
        self.model = cached_sentence_transformer(model)
        self._prefixes = prefixes_for(model)
        self.batch_size = batch_size

    def _prep(self, texts: list[str], is_query: bool) -> list[str]:
        """Применить query/passage-префикс к текстам, если он задан для модели."""
        if not self._prefixes:
            return list(texts)
        prefix = self._prefixes[0] if is_query else self._prefixes[1]
        return [prefix + t for t in texts]

    def encode(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        """Закодировать тексты с нормализацией, вернуть матрицу векторов."""
        # show_progress_bar=False: прогресс ведётся по чанкам в pipeline.index_path,
        # а tqdm-бар "Batches" шумит в stderr (особенно в фоновом ingest).
        return self.model.encode(self._prep(texts, is_query),
                                 normalize_embeddings=True, show_progress_bar=False,
                                 batch_size=self.batch_size)
