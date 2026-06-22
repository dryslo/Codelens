"""LLM-gateway (профиль large): шлюз к провайдерам.

Ключи и реестр только здесь. Промпт-логика hyde/multiquery - в BaseLLM провайдеров.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.util import metrics, sse

_LLMS: dict = {}


def _load() -> None:
    """Строит реестр локальных LLM-провайдеров из конфига (без kind, чтобы не зациклить)."""
    global _LLMS
    from src.factory import build_llms, load_config
    llm_cfg = dict(load_config().get("llm", {}))
    llm_cfg.pop("kind", None)  # gateway строит только локальные провайдеры (иначе self-loop)
    _LLMS = build_llms(llm_cfg)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Строит провайдеров на старте приложения."""
    _load()
    yield


app = FastAPI(title="codelens-llm", lifespan=lifespan)
metrics.mount(app, "llm")            # /metrics + латентность эндпоинтов (no-op без prometheus)


class ChatReq(BaseModel):
    """Запрос чата: провайдер и история сообщений."""

    provider: str
    messages: list[dict]


class HydeReq(BaseModel):
    """Запрос HyDE: провайдер и исходный запрос."""

    provider: str
    query: str


class MultiQueryReq(BaseModel):
    """Запрос MultiQuery: провайдер, запрос и число переформулировок."""

    provider: str
    query: str
    n: int = 3


def _get(provider: str) -> object:
    """Возвращает провайдера по имени; 404 при неизвестном."""
    llm = _LLMS.get(provider)
    if llm is None:
        raise HTTPException(404, f"unknown provider: {provider}")
    return llm


@app.get("/llms")
def llms() -> dict:
    """Возвращает список доступных провайдеров."""
    return {"names": list(_LLMS)}


@app.post("/chat")
def chat(r: ChatReq) -> dict:
    """Возвращает ответ провайдера на историю сообщений."""
    return {"content": _get(r.provider).chat(r.messages)}


@app.post("/chat/stream")
def chat_stream(r: ChatReq) -> StreamingResponse:
    """Стримит ответ провайдера дельтами в формате SSE."""
    llm = _get(r.provider)
    return StreamingResponse(sse.stream(llm.chat_stream(r.messages)),
                             media_type="text/event-stream")


@app.post("/hyde")
def hyde(r: HydeReq) -> dict:
    """Возвращает гипотетический документ HyDE по запросу."""
    return {"text": _get(r.provider).hyde(r.query)}


@app.post("/multiquery")
def multiquery(r: MultiQueryReq) -> dict:
    """Возвращает N переформулировок запроса."""
    return {"variants": _get(r.provider).multiquery(r.query, r.n)}


@app.get("/healthz")
def healthz() -> dict:
    """Возвращает статус шлюза и список провайдеров."""
    return {"ok": True, "names": list(_LLMS)}
