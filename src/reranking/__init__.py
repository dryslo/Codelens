"""Реранкеры и общий хелпер отбора top-k по скорам."""
from collections.abc import Sequence


def top_k_by_score(cands: list[dict], scores: Sequence[float], k: int) -> list[dict]:
    """Отсортировать кандидатов по scores (убыв.), вернуть копии top-k с проставленным score."""
    ranked = sorted(zip(cands, scores), key=lambda x: x[1], reverse=True)
    out = []
    for c, s in ranked[:k]:
        c = dict(c)
        c["score"] = float(s)
        out.append(c)
    return out
