"""Удалённый эмбеддер: HTTP-клиент к inference-сервису."""
import numpy as np

from src.domain.interfaces import Embedder


class RemoteEmbedder(Embedder):
    """Клиент к inference-сервису (профиль large). Префиксы e5 применяет сервис."""

    def __init__(self, url: str) -> None:
        """Запомнить базовый URL inference-сервиса."""
        self.url = url.rstrip("/")

    def encode(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        """Закодировать тексты через inference-сервис, вернуть матрицу векторов.

        Несколько попыток с backoff: эмбеддер мог перезапускаться/догружать модель (connection
        refused / 5xx). Долгую первую загрузку покрывает healthcheck+depends_on в compose.
        """
        import time

        import requests
        payload = {"texts": list(texts), "is_query": is_query}
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = requests.post(f"{self.url}/embed", json=payload, timeout=60)
                r.raise_for_status()
                return np.array(r.json()["vectors"])
            except requests.RequestException as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise last  # type: ignore[misc]
