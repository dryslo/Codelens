"""MultiQuery-расширение: N LLM-переформулировок запроса для отдельных выдач."""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.persistence.cache import cache_get_or_set, digest

if TYPE_CHECKING:
    from src.domain.interfaces import LLMProvider, SessionStore


class MultiQueryExpander:
    """LLM выдаёт N переформулировок; каждая ищется отдельно, выдачи сливаются через RRF.

    expand_list(q) - [q, var1, var2, ...] для отдельных dense-выдач.
    expand(q)      - конкатенация в одну выдачу.

    Выход LLM кэшируется по (n, query): дорогой вызов, промпт детерминирован.
    """

    def __init__(self, llm: LLMProvider, n: int = 3, cache: SessionStore | None = None,
                 cache_ttl: int = 3600) -> None:
        self.llm = llm
        self.n = n
        self.cache = cache
        self.cache_ttl = cache_ttl

    def expand_list(self, query: str, n: int | None = None) -> list[str]:
        """Дедуплицированный список [query, var1, ...] (или [query] при сбое LLM)."""
        n = n or self.n
        try:
            variants = cache_get_or_set(self.cache, f"mq:{n}:{digest(query)}",
                                        lambda: self.llm.multiquery(query, n), self.cache_ttl)
        except Exception:
            return [query]
        # provider может уже включить query первым - дедуплицируем
        seen, out = set(), []
        for s in [query, *variants]:
            s = (s or "").strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out[: n + 1]

    def expand(self, query: str) -> str:
        """Конкатенация переформулировок в одну строку (для одиночной выдачи)."""
        return "\n".join(self.expand_list(query))
