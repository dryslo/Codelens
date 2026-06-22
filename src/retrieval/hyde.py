"""HyDE-расширение запроса: гипотетический фрагмент кода от LLM."""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.persistence.cache import cache_get_or_set, digest

if TYPE_CHECKING:
    from src.domain.interfaces import LLMProvider, SessionStore


class HyDEExpander:
    """LLM генерит гипотетический фрагмент кода, эмбеддится query + гипотеза.

    Выход LLM кэшируется по запросу: дорогой вызов, промпт детерминирован.
    """

    def __init__(self, llm: LLMProvider, cache: SessionStore | None = None,
                 cache_ttl: int = 3600) -> None:
        self.llm = llm
        self.cache = cache
        self.cache_ttl = cache_ttl

    def expand(self, query: str) -> str:
        """Вернуть запрос с подмешанным гипотетическим фрагментом (или исходный при сбое LLM)."""
        try:
            hypo = cache_get_or_set(self.cache, f"hyde:{digest(query)}",
                                    lambda: self.llm.hyde(query), self.cache_ttl)
            return f"{query}\n{hypo}"
        except Exception:
            return query  # LLM недоступна -> обычный запрос
