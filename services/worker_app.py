"""RQ-воркер ingest (профиль large).

Тянет задачи из очереди codelens-ingest и исполняет
run_ingest_job (acquire -> index_path -> bump_epoch). Масштаб - числом реплик пода.

Запуск: python -m services.worker_app   (или rq worker codelens-ingest -u $REDIS_URL).
Прогрев comp на старте, чтобы первый job не платил за инициализацию.

У воркера нет HTTP-сервиса, поэтому /metrics поднимается отдельным портом
(METRICS_PORT, дефолт 9100). Длительность job меряется в run_ingest; глубину очереди
обновляет фоновый поток.
"""
import os
import threading
import time

from src.util import metrics

_DEPTH_PERIOD = 5    # сек между обновлениями глубины очереди


def _poll_queue_depth(queue: object) -> None:
    """Периодически кладёт длину очереди и реестров в gauge для скрейпа Prometheus."""
    while True:
        try:
            metrics.set_queue_depth("queued", queue.count)
            metrics.set_queue_depth("started", queue.started_job_registry.count)
            metrics.set_queue_depth("failed", queue.failed_job_registry.count)
        except Exception:  # noqa: BLE001 - телеметрия не должна ронять воркер
            pass
        time.sleep(_DEPTH_PERIOD)


def main() -> None:
    """Поднимает RQ-воркер очереди ingest: прогрев comp, /metrics, опрос глубины."""
    from rq import Queue, Worker

    from src.factory import load_config
    from src.jobs.redis_queue import _QUEUE_NAME, _comp, redis_conn

    cfg = load_config()
    redis_url = cfg.get("redis_url") or os.environ.get("REDIS_URL")
    if not redis_url:
        raise SystemExit("worker: нужен redis_url (REDIS_URL) для очереди ingest")
    _comp()                                    # прогрев composition root
    # redis_conn: агрессивный TCP keepalive, иначе idle BLPOP-соединение в WSL/Docker режется
    # за секунды и воркер выходит с "Redis connection timeout".
    conn = redis_conn(redis_url)
    queue = Queue(_QUEUE_NAME, connection=conn)

    if metrics.start_metrics_server(int(os.environ.get("METRICS_PORT", "9100"))):
        threading.Thread(target=_poll_queue_depth, args=(queue,), daemon=True).start()

    Worker([queue], connection=conn).work(with_scheduler=False)


if __name__ == "__main__":
    main()
