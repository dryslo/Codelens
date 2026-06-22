"""Фильтр результатов поиска по полям метаданных (lang/source).

Фильтр - словарь {field: [values]}. Сторы транслируют его в нативную форму (Chroma where /
Qdrant Filter), bm25-канал отсеивает результаты постфактум через match().
"""
from __future__ import annotations

_FIELDS = ("lang", "source")


def normalize(filters: dict | None) -> dict | None:
    """Оставить только непустые поля lang/source; вернуть None, если ограничений нет."""
    if not filters:
        return None
    out = {f: list(filters[f]) for f in _FIELDS if filters.get(f)}
    return out or None


def match(meta: dict | None, where: dict | None) -> bool:
    """Проверить, удовлетворяет ли meta фильтру where (значение поля входит в список допустимых)."""
    if not where:
        return True
    return all((meta or {}).get(field) in vals for field, vals in where.items())
