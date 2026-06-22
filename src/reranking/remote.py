"""Удалённый реранкер через HTTP-сервис."""
from src.domain.interfaces import Reranker
from src.reranking import top_k_by_score


class RemoteReranker(Reranker):
    """Реранкер, делегирующий скоринг удалённому /rerank-эндпоинту."""

    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")

    def rerank(self, query: str, cands: list[dict], k: int = 5) -> list[dict]:
        """Переранжировать кандидатов через удалённый сервис и вернуть top-k."""
        if not cands:
            return []
        import requests
        texts = [c["code"] for c in cands]
        scores = requests.post(f"{self.url}/rerank",
                               json={"query": query, "texts": texts}, timeout=60).json()["scores"]
        return top_k_by_score(cands, scores, k)
