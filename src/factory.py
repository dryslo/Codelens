"""Composition root: единый код, профиль/размещение задаётся конфигом."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.auth.service import AuthService
    from src.domain.interfaces import (
        BackendClient,
        Embedder,
        History,
        IndexRegistry,
        JobQueue,
        LLMProvider,
        Reranker,
        Retriever,
        SessionStore,
        VectorStore,
    )
    from src.retrieval.flags import FlagsPolicy


@dataclass
class Components:
    """Собранные компоненты приложения (composition root).

    Поля кроме cfg опциональны: профиль frontend заполняет только backend и cfg,
    полный пайплайн (role all/backend) - все.
    """

    cfg: dict
    backend: BackendClient | None = None
    embedder: Embedder | None = None
    reranker: Reranker | None = None
    store: VectorStore | None = None
    retriever: Retriever | None = None
    history: History | None = None
    registry: IndexRegistry | None = None
    cache: SessionStore | None = None
    auth: AuthService | None = None
    llms: dict[str, LLMProvider] = field(default_factory=dict)
    fast: str | None = None
    flag_policy: FlagsPolicy | None = None
    jobs: JobQueue | None = None
    index_path: Callable[..., dict] | None = None
    remove_source: Callable[..., dict] | None = None


def _expand(node: object) -> object:
    """Поддержка ${VAR:-default} в config.yaml."""
    if isinstance(node, dict):
        return {k: _expand(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand(v) for v in node]
    if isinstance(node, str):
        m = re.fullmatch(r"\$\{(\w+)(?::-(.*))?\}", node)
        if m:
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else "")
        return node
    return node


def load_config(path: str | None = None) -> dict:
    """Загрузить config.yaml с раскрытием ${VAR:-default}."""
    path = path or os.environ.get("CODELENS_CONFIG", "config/config.yaml")
    with open(path, encoding="utf-8") as f:
        return _expand(yaml.safe_load(f))


def build_llms(llm_cfg: dict) -> dict:
    """Построить словарь {name: LLMProvider} по конфигу llm (local или remote)."""
    # remote: провайдеры в llm-gateway, тут только HTTP-клиенты (контракт {name: LLMProvider}).
    if llm_cfg.get("kind") == "remote":
        from src.llm.remote import build_remote_llms
        return build_remote_llms(llm_cfg["llm_url"])
    out: dict[str, LLMProvider] = {}
    for name, spec in (llm_cfg.get("providers") or {}).items():
        try:
            kind = spec["kind"]
            if kind == "ollama":
                from src.llm.ollama import OllamaLLM
                out[name] = OllamaLLM(**{k: v for k, v in spec.items() if k != "kind"})
            elif kind == "openai_compatible":
                from src.llm.openai_compatible import OpenAICompatibleLLM
                out[name] = OpenAICompatibleLLM(**{k: v for k, v in spec.items() if k != "kind"})
        except Exception:
            pass  # недоступный провайдер не попадает в список
    return out


def _build_embedder(cfg: dict) -> Embedder:
    """Построить эмбеддер (local или remote) по конфигу."""
    e = cfg["embedder"]
    if e.get("kind", "local") == "remote":
        from src.embeddings.remote import RemoteEmbedder
        return RemoteEmbedder(cfg.get("embedder_url") or cfg["inference_url"])
    from src.embeddings.local import LocalEmbedder
    return LocalEmbedder(e["model"], batch_size=int(e.get("batch_size", 32)))


def _build_reranker(cfg: dict) -> Reranker | None:
    """Построить реранкер (local или remote) или None, если выключен."""
    r = cfg.get("reranker", {})
    if str(r.get("enabled", "false")).lower() != "true":
        return None
    if r.get("kind", "local") == "remote":
        from src.reranking.remote import RemoteReranker
        return RemoteReranker(cfg.get("reranker_url") or cfg["inference_url"])
    from src.reranking.local import LocalReranker
    return LocalReranker(r["model"])


def _build_store(cfg: dict) -> VectorStore:
    """Построить векторное хранилище (qdrant или chroma) по конфигу."""
    v = cfg["vector"]
    if v["kind"] == "qdrant":
        from src.stores.qdrant import QdrantStore
        return QdrantStore(url=v["url"], dim=int(cfg["embedder"]["dim"]),
                           shards=int(v.get("shards", 2)), replicas=int(v.get("replicas", 2)))
    from src.stores.chroma import ChromaStore
    return ChromaStore(path=v.get("path", ".chroma"))


def build() -> Components:
    """Собрать компоненты приложения по конфигу (composition root)."""
    cfg = load_config()
    role = cfg.get("role", "all")

    if role == "frontend":
        from src.clients.backend import HttpBackend
        return Components(cfg=cfg, backend=HttpBackend(cfg["backend_url"]))

    # role in {all, backend}: полный пайплайн
    from src.indexing.pipeline import index_path, remove_source
    from src.persistence.cache import build_cache
    from src.persistence.db import init_db, make_session_factory
    from src.persistence.history_repo import SqlHistory
    from src.persistence.registry_repo import CachingRegistry, SqlRegistry
    from src.retrieval.flags import FlagsPolicy
    from src.retrieval.hybrid import HybridRetriever

    embedder = _build_embedder(cfg)
    reranker = _build_reranker(cfg)
    store = _build_store(cfg)

    init_db(cfg["database_dsn"])  # dev-удобство; в проде - Alembic
    sf = make_session_factory(cfg["database_dsn"])

    # Один кэш на процесс (поиск/ответы/index-реестр/access-сессии). Пустой redis_url - NullCache.
    cache = build_cache(cfg.get("redis_url"))
    cache_ttl = int((cfg.get("cache") or {}).get("ttl", 3600))

    from src.auth.config import AuthConfig
    auth_cfg = AuthConfig.from_cfg(cfg)
    if auth_cfg.enabled and not getattr(cache, "enabled", False):
        from src.persistence.cache import InProcessCache
        cache = InProcessCache()   # access-сессиям нужен рабочий стор даже без redis (dev/small)

    registry: IndexRegistry = SqlRegistry(sf)
    if getattr(cache, "enabled", False):
        registry = CachingRegistry(registry, cache, ttl=cache_ttl)

    from src.auth.repositories import (
        SqlCredentials,
        SqlIdentities,
        SqlRefreshTokens,
        SqlUsers,
    )
    from src.auth.service import AuthService
    auth = AuthService(SqlUsers(sf), SqlCredentials(sf), SqlIdentities(sf),
                       SqlRefreshTokens(sf), cache, auth_cfg)
    auth.ensure_admin(os.environ.get("ADMIN_LOGIN"), os.environ.get("ADMIN_PASSWORD"))

    llms = build_llms(cfg.get("llm", {}))
    hyde = mq = None
    fast = cfg.get("llm", {}).get("fast")
    if fast and fast in llms:
        from src.retrieval.hyde import HyDEExpander
        from src.retrieval.multiquery import MultiQueryExpander
        hyde = HyDEExpander(llms[fast], cache=cache, cache_ttl=cache_ttl)
        mq = MultiQueryExpander(llms[fast], cache=cache, cache_ttl=cache_ttl)

    policy = FlagsPolicy.from_config((cfg.get("retrieval") or {}).get("flags"))

    from src.jobs import build_queue
    jobs = build_queue(cfg.get("jobs"), cfg.get("redis_url"))

    comp = Components(
        cfg=cfg, embedder=embedder, reranker=reranker, store=store,
        retriever=HybridRetriever(store, embedder, reranker,
                                  hyde=hyde, multiquery=mq, policy=policy,
                                  cache=cache, cache_ttl=cache_ttl),
        history=SqlHistory(sf), registry=registry, cache=cache, auth=auth,
        llms=llms, fast=fast, flag_policy=policy, jobs=jobs,
        index_path=index_path, remove_source=remove_source,
    )
    if hasattr(jobs, "bind"):
        jobs.bind(comp)        # InProcessQueue исполняет ingest в этом же процессе - нужен comp

    from src.clients.backend import LocalBackend
    comp.backend = LocalBackend(comp)
    return comp
