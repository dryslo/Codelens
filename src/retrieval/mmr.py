"""Maximal Marginal Relevance - диверсификация топа.

MMR_i = λ · sim(q, d_i) - (1-λ) · max_j∈S sim(d_i, d_j)
λ=1 - чистая релевантность, λ=0 - чистая новизна. Дефолт 0.7 - лёгкая дедупликация.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Sequence


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    """Косинусная близость двух векторов (0.0 при нулевой норме)."""
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def mmr(query_vec: Sequence[float], cand_vecs: Sequence[Sequence[float]],
        k: int = 5, lambda_: float = 0.7) -> list[int]:
    """Возвращает индексы кандидатов в порядке MMR-отбора (длина ≤ k)."""
    if not cand_vecs:
        return []
    q = np.asarray(query_vec)
    vecs = [np.asarray(v) for v in cand_vecs]
    sim_q = np.array([_cos(q, v) for v in vecs])

    selected: list[int] = []
    remaining = list(range(len(vecs)))
    while remaining and len(selected) < k:
        if not selected:
            i = int(remaining[int(np.argmax(sim_q[remaining]))])
        else:
            best_score, best_i = -1e18, remaining[0]
            for idx in remaining:
                max_sim = max(_cos(vecs[idx], vecs[j]) for j in selected)
                score = lambda_ * sim_q[idx] - (1 - lambda_) * max_sim
                if score > best_score:
                    best_score, best_i = score, idx
            i = best_i
        selected.append(i)
        remaining.remove(i)
    return selected
