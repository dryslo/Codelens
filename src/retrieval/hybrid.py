"""Гибридный ретривер: dense + опциональные bm25/multiquery/hyde/rerank/mmr."""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from src.domain.interfaces import Retriever
from src.persistence.cache import cache_get_or_set, current_epoch, digest
from src.retrieval import filters as filt
from src.retrieval.bm25 import BM25Index
from src.retrieval.flags import FlagsPolicy, SearchFlags
from src.util import metrics
from src.util.concurrency import run_parallel

if TYPE_CHECKING:
    from collections.abc import Sequence

    from src.domain.interfaces import Embedder, Reranker, SessionStore, VectorStore
    from src.retrieval.hyde import HyDEExpander
    from src.retrieval.multiquery import MultiQueryExpander


def _sigmoid(x: float) -> float:
    """Численно устойчивая логистическая функция (без переполнения exp на больших |x|)."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def rrf(rank_lists: Sequence[Sequence[str]], k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion - слияние любого числа списков id. Возвращает {id: score}."""
    scores: dict[str, float] = {}
    for ranks in rank_lists:
        for pos, cid in enumerate(ranks):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + pos + 1)
    return scores


class HybridRetriever(Retriever):
    """Пайплайн: dense + (опц.) bm25/multiquery/hyde/rerank/mmr.

    Каналы переключаются через SearchFlags на каждый запрос. FlagsPolicy (config.yaml)
    фиксирует каналы в off/on или оставляет решение за UI. Без LLM-провайдера
    hyde/multiquery игнорируются.
    """

    def __init__(self, store: VectorStore, embedder: Embedder,
                 reranker: Reranker | None = None,
                 hyde: HyDEExpander | None = None,
                 multiquery: MultiQueryExpander | None = None,
                 policy: FlagsPolicy | None = None,
                 cache: SessionStore | None = None, cache_ttl: int = 3600) -> None:
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        self.hyde = hyde
        self.multiquery = multiquery
        self.bm25 = BM25Index(store)
        self.policy = policy or FlagsPolicy()
        self.cache = cache
        self.cache_ttl = cache_ttl

    def search(self, query: str, k: int = 5, flags: Any = None, mode: str | None = None,
               where: dict | None = None, query_emb: Any = None) -> list[dict]:
        """Выполнить поиск с резолвом флагов по политике и cache-aside на границе."""
        if flags is None:
            flags = SearchFlags.from_mode(mode)
        else:
            flags = SearchFlags.from_any(flags)
        # Политика - последнее слово: off->False, fast->True, thinking->True при mode="thinking".
        flags = self.policy.apply(flags, mode=mode)
        where = filt.normalize(where)

        # Cache-aside на границе оркестратора: на попадании чистое ядро не запускается.
        # Ключ включает index-epoch - admin index/remove сдвигают его и осиротляют старые
        # записи поиска (см. cache.bump_epoch). where входит в ключ - разный фильтр, разный кэш.
        if self.cache is not None and getattr(self.cache, "enabled", False):
            key = (f"search:{current_epoch(self.cache)}:"
                   f"{digest({'q': query, 'f': flags.to_dict(), 'k': k, 'w': where})}")
            return cache_get_or_set(self.cache, key,
                                    lambda: self._search(query, k, flags, where=where,
                                                         query_emb=query_emb),
                                    self.cache_ttl)
        return self._search(query, k, flags, where=where, query_emb=query_emb)

    def _search(self, query: str, k: int, flags: SearchFlags,
                where: dict | None = None, query_emb: Any = None) -> list[dict]:
        """Чистое ядро поиска (без кэша): query + резолвнутые flags -> результаты.

        query_emb (опц.) - заранее посчитанный вектор запроса (батч-eval). Используется только
        когда запрос не меняется расширением (нет hyde/multiquery), иначе кодируется как обычно.
        """
        # 1) Варианты запроса для dense-каналов.
        #    hyde и multiquery - независимые LLM-вызовы, выполняются параллельно.
        queries = [query]
        exp = []
        if flags.hyde and self.hyde is not None:
            exp.append(("hyde", lambda: metrics.timed("hyde", lambda: self.hyde.expand(query))))
        if flags.multiquery and self.multiquery is not None:
            exp.append(("mq", lambda: metrics.timed(
                "multiquery", lambda: self.multiquery.expand_list(query, n=flags.multiquery_n))))
        if exp:
            res = dict(zip([n for n, _ in exp], run_parallel([t for _, t in exp])))
            if "hyde" in res:
                queries[0] = res["hyde"]
            for v in res.get("mq", []):
                if v and v != query:
                    queries.append(v)

        # 2) Закодировать все варианты разом, прогнать dense по каждому.
        if query_emb is not None and len(queries) == 1 and queries[0] == query:
            q_embs = [query_emb]                  # батч-eval: вектор уже посчитан
        else:
            with metrics.stage("embed"):
                q_embs = self.embedder.encode(queries, is_query=True)
        # dense-поиск по каждому варианту независим (e=e фиксирует вектор в замыкании).
        with metrics.stage("store"):
            dense_lists = run_parallel(
                [(lambda e=e: self.store.query(e, k=flags.k_cand, where=where)) for e in q_embs])
        rank_lists: list[list[str]] = []
        by_id: dict[str, dict] = {}
        for dense in dense_lists:
            rank_lists.append([c["chunk_id"] for c in dense])
            for c in dense:
                by_id.setdefault(c["chunk_id"], c)

        # 3) BM25-канал по оригинальному запросу (bm25 по всему корпусу, затем постфильтр по where).
        if flags.bm25:
            with metrics.stage("bm25"):
                lex = self.bm25.search(query, k=flags.k_cand)
            if where:
                lex = [c for c in lex if filt.match(c.get("meta"), where)]
            rank_lists.append([c["chunk_id"] for c in lex])
            for c in lex:
                by_id.setdefault(c["chunk_id"], c)

        # 4) RRF-фьюжн всех каналов.
        if len(rank_lists) > 1:
            rrf_scores = rrf(rank_lists)
            fused_ids = sorted(rrf_scores, key=rrf_scores.get, reverse=True)
        else:
            rrf_scores = None
            fused_ids = rank_lists[0] if rank_lists else []
        cands = [by_id[i] for i in fused_ids if i in by_id][: flags.k_cand]

        # 4.5) Базовый score для UI: нормированный RRF при фьюжне, иначе cosine similarity.
        if rrf_scores:
            max_s = max(rrf_scores.values()) or 1.0
            for c in cands:
                c["score"] = rrf_scores.get(c["chunk_id"], 0.0) / max_s
        else:
            for c in cands:
                if "distance" in c:
                    c["score"] = max(0.0, min(1.0, 1.0 - float(c["distance"])))
                else:
                    c.setdefault("score", 0.0)

        # 5) Реранк (по оригинальному запросу).
        if flags.rerank and self.reranker is not None:
            # перед MMR держим запас для диверсификации
            with metrics.stage("rerank"):
                cands = self.reranker.rerank(query, cands,
                                             k=max(k * 4, k) if flags.mmr else k)
            # cross-encoder возвращает логит - в [0,1] через сигмоиду.
            for c in cands:
                c["score"] = _sigmoid(float(c["score"]))

        # 6) MMR-диверсификация финальной выдачи.
        if flags.mmr:
            with metrics.stage("mmr"):
                pool = cands[: max(k * 4, k)]
                emb_map = self.store.get_embeddings([c["chunk_id"] for c in pool])
                pairs = [(c, emb_map.get(c["chunk_id"])) for c in pool]
                pairs = [(c, v) for c, v in pairs if v is not None]
                if pairs:
                    from src.retrieval.mmr import mmr as mmr_fn
                    vecs = [v for _, v in pairs]
                    idxs = mmr_fn(q_embs[0], vecs, k=k, lambda_=flags.mmr_lambda)
                    cands = [pairs[i][0] for i in idxs]
                else:
                    cands = pool[:k]
        else:
            cands = cands[:k]

        return cands
