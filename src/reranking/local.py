"""Локальный реранкер на кросс-энкодере."""
from src.domain.interfaces import Reranker
from src.reranking import top_k_by_score
from src.util.model_cache import cached_cross_encoder


class LocalReranker(Reranker):
    """Кросс-энкодер. Под e5-large хорошо подходит BAAI/bge-reranker-v2-m3 (мультиязычный)."""

    def __init__(self, model: str = "BAAI/bge-reranker-v2-m3") -> None:
        self.model = cached_cross_encoder(model)

    def rerank(self, query: str, cands: list[dict], k: int = 5) -> list[dict]:
        """Переранжировать кандидатов кросс-энкодером и вернуть top-k."""
        if not cands:
            return []
        scores = self.model.predict([(query, c["code"]) for c in cands])
        return top_k_by_score(cands, scores, k)
