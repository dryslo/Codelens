"""параллельный фан-аут (run_parallel) и его применение в ретривере (hyde + multiquery)."""
import threading

import pytest

from src.retrieval.hybrid import HybridRetriever
from src.util.concurrency import run_parallel


# ---------- run_parallel ----------

def test_run_parallel_preserves_order():
    assert run_parallel([lambda: 1, lambda: 2, lambda: 3]) == [1, 2, 3]


def test_run_parallel_single_task_no_pool():
    assert run_parallel([lambda: 42]) == [42]
    assert run_parallel([]) == []


def test_run_parallel_actually_concurrent():
    # Barrier(2) разблокируется только если обе задачи дошли до wait() одновременно.
    # При последовательном выполнении первая зависла бы: timeout, BrokenBarrierError.
    barrier = threading.Barrier(2, timeout=2)
    out = run_parallel([lambda i=i: (barrier.wait(), i)[1] for i in (0, 1)])
    assert out == [0, 1]


def test_run_parallel_propagates_exception():
    def boom():
        raise ValueError("x")
    with pytest.raises(ValueError):
        run_parallel([lambda: 1, boom])


# ---------- ретривер: hyde и multiquery бегут параллельно ----------

class _Store:
    def query(self, emb, k=50, where=None):
        return [{"chunk_id": "a", "distance": 0.1}, {"chunk_id": "b", "distance": 0.3}]


class _Embedder:
    def encode(self, queries, is_query=False):
        return [[0.0] for _ in queries]            # по вектору на вариант, значения не важны


class _Barriered:
    """hyde/multiquery с общим барьером: поиск пройдёт только при параллельном запуске."""
    def __init__(self, barrier):
        self.barrier = barrier
        self.calls = 0

    def expand(self, query):                       # hyde
        self.calls += 1
        self.barrier.wait()
        return f"{query}\nHYPO"

    def expand_list(self, query, n=3):             # multiquery
        self.calls += 1
        self.barrier.wait()
        return ["вариант"]


def test_retriever_runs_hyde_and_multiquery_concurrently():
    barrier = threading.Barrier(2, timeout=2)
    hyde, mq = _Barriered(barrier), _Barriered(barrier)
    r = HybridRetriever(_Store(), _Embedder(), hyde=hyde, multiquery=mq, cache=None)
    out = r.search("q", k=2, flags={"hyde": True, "multiquery": True})
    assert {c["chunk_id"] for c in out} <= {"a", "b"} and out          # фьюжн отдал результаты
    assert hyde.calls == 1 and mq.calls == 1                            # оба канала вызваны, барьер пройден
