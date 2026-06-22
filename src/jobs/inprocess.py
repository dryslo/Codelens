"""InProcessQueue: фон в потоке того же процесса (профиль small/dev).

Статус - в памяти процесса (для single-replica backend достаточно; кросс-репличный статус -
в RedisQueue/RQ). Один worker-поток сериализует ingest-ы, чтобы не писать в индекс-стор
конкурентно. comp привязывается через bind() после сборки composition root.
"""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from src.domain.interfaces import JobQueue

if TYPE_CHECKING:
    from src.factory import Components


class InProcessQueue(JobQueue):
    """Очередь ingest-задач в потоке того же процесса (профиль small/dev)."""

    def __init__(self, max_keep: int = 100) -> None:
        self._jobs: dict[str, dict] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")
        self._max_keep = max_keep
        self._comp: Components | None = None

    def bind(self, comp: Components) -> None:
        """Привязать composition root (после сборки)."""
        self._comp = comp

    def submit(self, task: dict) -> str:
        """Поставить ingest-задачу в очередь, вернуть job_id."""
        job_id = uuid.uuid4().hex[:12]
        rec = {"id": job_id, "status": "queued", "progress": {}, "stats": None, "error": None,
               "kind": task.get("kind"), "source": task.get("source"),
               "created_at": time.time(), "finished_at": None}
        with self._lock:
            self._jobs[job_id] = rec
            self._order.append(job_id)
            while len(self._order) > self._max_keep:
                self._jobs.pop(self._order.pop(0), None)
        self._pool.submit(self._run, job_id, task)
        return job_id

    def _run(self, job_id: str, task: dict) -> None:
        from src.ingest.runner import run_ingest
        self._update(job_id, status="running")
        try:
            stats = run_ingest(task, lambda p: self._update(job_id, progress=p), self._comp)
            self._update(job_id, status="done", stats=stats, finished_at=time.time())
        except Exception as e:  # noqa: BLE001 - любая ошибка ingest даёт статус failed
            self._update(job_id, status="failed", error=str(e), finished_at=time.time())

    def _update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is not None:
                rec.update(fields)

    def get(self, job_id: str) -> dict | None:
        """Статус задачи по job_id (копия записи) или None."""
        with self._lock:
            rec = self._jobs.get(job_id)
            return dict(rec) if rec else None

    def list(self) -> list[dict]:
        """Статусы всех известных задач (новые первыми)."""
        with self._lock:
            return [dict(self._jobs[j]) for j in reversed(self._order) if j in self._jobs]
