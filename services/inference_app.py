"""Inference-сервис (профиль large): только модели. Кэш в cache/models/."""
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.util import metrics

_EMB = None
_RER = None
_PREFIXES = None  # (query-префикс, doc-префикс) или None


def _load() -> None:
    """INFERENCE_ROLE: embed | rerank | all (дефолт all - обе модели)."""
    global _EMB, _RER, _PREFIXES
    from src.embeddings.local import prefixes_for
    from src.util.model_cache import cached_cross_encoder, cached_sentence_transformer
    role = os.environ.get("INFERENCE_ROLE", "all")
    if role in ("embed", "all"):
        name = os.environ.get("EMBEDDER_MODEL", "intfloat/multilingual-e5-large")
        _EMB = cached_sentence_transformer(name)
        _PREFIXES = prefixes_for(name)
    if role in ("rerank", "all"):
        rr = os.environ.get("RERANKER_MODEL")
        _RER = cached_cross_encoder(rr) if rr else None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Загружает модели на старте приложения."""
    _load()
    yield


app = FastAPI(title="codelens-inference", lifespan=lifespan)
metrics.mount(app, "inference")      # /metrics + латентность эндпоинтов (no-op без prometheus)


class EmbedReq(BaseModel):
    """Запрос эмбеддинга: тексты и признак запроса (для query-префикса)."""

    texts: list[str]
    is_query: bool = False


class RerankReq(BaseModel):
    """Запрос реранка: запрос и тексты кандидатов."""

    query: str
    texts: list[str]


@app.post("/embed")
def embed(r: EmbedReq) -> dict:
    """Возвращает нормированные векторы для текстов (с учётом префиксов модели)."""
    if _EMB is None:
        raise HTTPException(503, "embedder not loaded on this pod (INFERENCE_ROLE)")
    texts = r.texts
    if _PREFIXES:
        prefix = _PREFIXES[0] if r.is_query else _PREFIXES[1]
        texts = [prefix + t for t in texts]
    return {"vectors": _EMB.encode(texts, normalize_embeddings=True).tolist()}


@app.post("/rerank")
def rerank(r: RerankReq) -> dict:
    """Возвращает скоры кросс-энкодера; нейтральные нули при выключенном реранкере."""
    if _RER is None:
        # reranker выключен - нейтральные скоры, чтобы пайплайн не падал
        return {"scores": [0.0] * len(r.texts)}
    return {"scores": [float(s) for s in _RER.predict([(r.query, t) for t in r.texts])]}


@app.get("/healthz")
def healthz() -> dict:
    """Возвращает статус пода и какие модели загружены."""
    return {"ok": True, "role": os.environ.get("INFERENCE_ROLE", "all"),
            "embed": _EMB is not None, "rerank": _RER is not None}
