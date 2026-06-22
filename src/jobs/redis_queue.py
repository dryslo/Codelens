"""RedisQueue: очередь на RQ (профиль large).

Backend только кладёт задачу; исполняет
отдельный worker-под (services/worker_app.py). Статус/прогресс - нативные RQ-registries +
job.meta, поэтому свой стор не нужен.

RQ не умеет замыкания → enqueue по строковому пути `run_ingest_job` (importable), task -
сериализуемый дескриптор. Для ZIP байты едут в payload job-а (Redis); для очень больших
архивов предпочтителен GitHub (URL вместо блоба) либо объект-стор (future).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.domain.interfaces import JobQueue

if TYPE_CHECKING:
    from src.factory import Components

_QUEUE_NAME = "codelens-ingest"
_worker_comp = None   # composition root в процессе воркера (строится один раз)


def _comp() -> Components:
    """Composition root воркера (ленивая сборка один раз на процесс)."""
    global _worker_comp
    if _worker_comp is None:
        from src.factory import build
        _worker_comp = build()
    return _worker_comp


def run_ingest_job(task: dict) -> dict:
    """Тело RQ-задачи (выполняется в воркере). Прогресс - в job.meta."""
    from rq import get_current_job

    from src.ingest.runner import run_ingest
    job = get_current_job()

    def report(progress: dict) -> None:
        if job is not None:
            job.meta["progress"] = progress
            job.save_meta()

    return run_ingest(task, report, _comp())


_STATUS = {"queued": "queued", "deferred": "queued", "scheduled": "queued",
           "started": "running", "finished": "done", "failed": "failed"}


def job_view(job: Any) -> dict:
    """RQ Job → внутренний формат статуса (вынесено для тестируемости без живого Redis)."""
    status = _STATUS.get(job.get_status(refresh=False), "queued")
    err = None
    if status == "failed" and job.exc_info:
        err = job.exc_info.strip().splitlines()[-1]
    return {"id": job.id, "status": status,
            "progress": (job.meta or {}).get("progress", {}),
            "stats": job.result if status == "done" else None, "error": err,
            "kind": (job.meta or {}).get("kind"), "source": (job.meta or {}).get("source")}


def redis_conn(url: str) -> Any:
    """Redis-соединение с агрессивным TCP keepalive.

    RQ-воркер блокируется на пустой очереди (BLPOP); в WSL2/Docker idle-соединение режется за
    секунды, и воркер выходит с 'Redis connection timeout'. Системный keepalive стартует только
    через ~2 часа, поэтому задаём опции: первый пинг через 3с простоя, дальше каждые 2с.
    """
    import socket

    from redis import Redis
    opts = {}
    for name, val in (("TCP_KEEPIDLE", 3), ("TCP_KEEPINTVL", 2), ("TCP_KEEPCNT", 3)):
        if hasattr(socket, name):
            opts[getattr(socket, name)] = val
    return Redis.from_url(url, socket_keepalive=True,
                          socket_keepalive_options=opts or None, health_check_interval=30)


class RedisQueue(JobQueue):
    """Очередь ingest-задач на RQ/Redis (профиль large)."""

    def __init__(self, redis_url: str, queue_name: str = _QUEUE_NAME,
                 job_timeout: int = 3600, result_ttl: int = 86400) -> None:
        from rq import Queue
        self._conn = redis_conn(redis_url)
        self._q = Queue(queue_name, connection=self._conn, default_timeout=job_timeout)
        self._result_ttl = result_ttl

    def submit(self, task: dict) -> str:
        """Поставить ingest-задачу в очередь RQ, вернуть job_id."""
        job = self._q.enqueue(run_ingest_job, task, result_ttl=self._result_ttl,
                              meta={"kind": task.get("kind"), "source": task.get("source")})
        return job.id

    def get(self, job_id: str) -> dict | None:
        """Статус задачи по job_id или None, если не найдена."""
        from rq.job import Job
        try:
            return job_view(Job.fetch(job_id, connection=self._conn))
        except Exception:  # noqa: BLE001 - нет такого job_id
            return None

    def list(self) -> list[dict]:
        """Статусы задач из очереди и RQ-registries (queued/started/finished/failed)."""
        from rq.job import Job
        ids = list(self._q.job_ids)
        for reg in (self._q.started_job_registry, self._q.finished_job_registry,
                    self._q.failed_job_registry):
            ids += list(reg.get_job_ids())
        # один job_id может попасть в очередь и в registry одновременно (переход статуса),
        # поэтому дедуп с сохранением порядка - иначе одна задача рисуется дважды
        ids = list(dict.fromkeys(ids))
        out = []
        for jid in ids:
            try:
                out.append(job_view(Job.fetch(jid, connection=self._conn)))
            except Exception:  # noqa: BLE001
                pass
        return out
