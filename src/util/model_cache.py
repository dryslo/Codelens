"""Локальный кэш моделей: загрузка один раз, дальше с диска.

Проектный кэш в cache/models/ вместо ~/.cache/huggingface - переносимый и не зависит
от $HOME. Путь переопределяется переменной MODEL_CACHE.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder, SentenceTransformer

CACHE_DIR = Path(os.environ.get("MODEL_CACHE", "cache/models"))


def _safe(name: str) -> str:
    """Заменить '/' в имени модели на '-' для безопасного пути."""
    return name.replace("/", "-")


def cached_sentence_transformer(name: str) -> SentenceTransformer:
    """Загрузить SentenceTransformer из локального кэша или скачать и сохранить."""
    from sentence_transformers import SentenceTransformer
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    local = CACHE_DIR / _safe(name)
    if local.exists():
        return SentenceTransformer(str(local))
    model = SentenceTransformer(name)
    model.save(str(local))
    return model


def cached_cross_encoder(name: str) -> CrossEncoder:
    """Загрузить CrossEncoder из локального кэша или скачать и сохранить."""
    from sentence_transformers import CrossEncoder
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    local = CACHE_DIR / _safe(name)
    if local.exists():
        return CrossEncoder(str(local))
    model = CrossEncoder(name)
    model.save(str(local))
    return model
