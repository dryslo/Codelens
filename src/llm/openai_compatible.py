"""LLM-провайдер для OpenAI-совместимых API (Groq/Gemini/Mistral/OpenRouter/OpenAI)."""
from __future__ import annotations

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

from src.llm.base import BaseLLM

if TYPE_CHECKING:
    from openai import OpenAI


class OpenAICompatibleLLM(BaseLLM):
    """Groq / Gemini (OpenAI-режим) / Mistral / OpenRouter / OpenAI - один класс."""

    def __init__(self, model: str, base_url: str = "https://api.openai.com/v1",
                 api_key_env: str = "OPENAI_API_KEY", **_: object) -> None:
        """Запомнить имя модели, базовый URL и имя переменной с API-ключом."""
        self.model = model
        self.base_url = base_url
        self.api_key_env = api_key_env

    def _client(self) -> OpenAI:
        """Создать клиента OpenAI с base_url и ключом из окружения."""
        from openai import OpenAI
        return OpenAI(base_url=self.base_url, api_key=os.environ.get(self.api_key_env, ""))

    def chat(self, messages: list[dict]) -> str:
        """Выполнить нестриминговый chat-запрос и вернуть текст ответа."""
        resp = self._client().chat.completions.create(model=self.model, messages=messages)
        return resp.choices[0].message.content or ""

    def chat_stream(self, messages: list[dict]) -> Iterator[str]:
        """Стримить chat-ответ по дельтам токенов."""
        stream = self._client().chat.completions.create(
            model=self.model, messages=messages, stream=True)
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
