"""Удалённый LLM-провайдер: HTTP-клиент к llm-gateway."""
from collections.abc import Iterator

from src.domain.interfaces import LLMProvider


class RemoteLLM(LLMProvider):
    """Клиент к llm-gateway (профиль large).

    Промпт-логика hyde/multiquery - в поде (BaseLLM), поэтому все три метода - простые
    HTTP-вызовы, не поверх chat().
    """

    def __init__(self, url: str, provider: str) -> None:
        """Запомнить базовый URL gateway и имя провайдера."""
        self.url = url.rstrip("/")
        self.provider = provider

    def _post(self, path: str, payload: dict) -> dict:
        """Выполнить POST к gateway с подмешанным provider, вернуть JSON-ответ."""
        import requests
        r = requests.post(f"{self.url}{path}", json={"provider": self.provider, **payload}, timeout=120)
        r.raise_for_status()
        return r.json()

    def chat(self, messages: list[dict]) -> str:
        """Выполнить нестриминговый chat-запрос через gateway."""
        return self._post("/chat", {"messages": messages})["content"]

    def chat_stream(self, messages: list[dict]) -> Iterator[str]:
        """Стримить chat-ответ по дельтам токенов через gateway."""
        import requests

        from src.util.sse import parse_lines
        with requests.post(f"{self.url}/chat/stream",
                           json={"provider": self.provider, "messages": messages},
                           stream=True, timeout=180) as r:
            r.raise_for_status()
            yield from parse_lines(r.iter_lines(decode_unicode=True))

    def hyde(self, query: str) -> str:
        """Запросить HyDE-фрагмент у gateway."""
        return self._post("/hyde", {"query": query})["text"]

    def multiquery(self, query: str, n: int = 3) -> list[str]:
        """Запросить переформулировки запроса у gateway."""
        return self._post("/multiquery", {"query": query, "n": n})["variants"]


def build_remote_llms(llm_url: str) -> dict:
    """Запрашивает у gateway список провайдеров -> {name: RemoteLLM}. Тот же контракт, что build_llms."""
    import requests
    url = llm_url.rstrip("/")
    names = requests.get(f"{url}/llms", timeout=30).json()["names"]
    return {name: RemoteLLM(url, name) for name in names}
