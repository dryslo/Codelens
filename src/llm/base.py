"""Базовый LLM-провайдер: hyde/multiquery поверх chat()."""
from src.domain.interfaces import LLMProvider


class BaseLLM(LLMProvider):
    """Общая логика: hyde/multiquery строятся поверх chat(). Наследники реализуют chat()."""

    def chat(self, messages: list[dict]) -> str:
        """Отправить сообщения модели и вернуть текст ответа."""
        raise NotImplementedError

    def hyde(self, query: str) -> str:
        """Сгенерировать гипотетический фрагмент кода (HyDE) по запросу."""
        return self.chat([{"role": "user", "content":
            "Напиши короткий гипотетический фрагмент Python-кода (без пояснений), "
            f"который отвечал бы на вопрос:\n{query}"}])

    def multiquery(self, query: str, n: int = 3) -> list[str]:
        """Сгенерировать n переформулировок запроса плюс исходный."""
        out = self.chat([{"role": "user", "content":
            f"Дай {n} разных переформулировок запроса (RU и EN), по одной на строку, без нумерации:\n{query}"}])
        variants = [s.strip() for s in out.splitlines() if s.strip()]
        return [query] + variants[:n]
