"""Параллельный фан-аут независимых вызовов (потоки).

Потоки, а не asyncio: окружающий стек синхронный, FastAPI sync-эндпоинты Starlette и так
исполняет в threadpool. ThreadPoolExecutor параллелит и I/O-bound HTTP (qdrant/llm-gateway),
и нативный compute, отпускающий GIL (torch/requests), без переписывания адаптеров в async.
"""
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

_MAX_WORKERS = 8


def run_parallel(tasks: Sequence[Callable[[], object]]) -> list:
    """Выполнить вызываемые параллельно, результаты в исходном порядке.

    На 0-1 задаче - без накладных расходов на пул.
    """
    tasks = list(tasks)
    if len(tasks) <= 1:
        return [t() for t in tasks]
    with ThreadPoolExecutor(max_workers=min(len(tasks), _MAX_WORKERS)) as ex:
        futures = [ex.submit(t) for t in tasks]
        return [f.result() for f in futures]
