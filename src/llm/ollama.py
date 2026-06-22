"""LLM-провайдер Ollama (локальный сервер)."""
from src.llm.base import BaseLLM


class OllamaLLM(BaseLLM):
    """Клиент к локальному серверу Ollama через HTTP API."""

    def __init__(self, model: str, url: str = "http://localhost:11434", **_: object) -> None:
        """Запомнить имя модели и базовый URL сервера Ollama."""
        self.model = model
        self.url = url.rstrip("/")

    def chat(self, messages: list[dict]) -> str:
        """Выполнить нестриминговый chat-запрос и вернуть текст ответа."""
        import requests
        r = requests.post(f"{self.url}/api/chat",
                          json={"model": self.model, "messages": messages, "stream": False},
                          timeout=120)
        r.raise_for_status()
        return r.json()["message"]["content"]
