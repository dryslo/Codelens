# Очередь фоновых задач

Разбор `../../src/jobs/inprocess.py` и `../../src/jobs/redis_queue.py` - порт `JobQueue` и две его
реализации. Единственный тип задач - ingest кодовой базы (см. [../ingest/ingest.md](../ingest/ingest.md));
тело задачи (`acquire → index_path → bump_epoch`) одно для обеих очередей, различается только
исполнение и хранение статуса.

## Порт `JobQueue` (`../../src/domain/interfaces.py`)

```python
class JobQueue(ABC):
    @abstractmethod
    def submit(self, task: dict) -> str: ...     # поставить задачу, вернуть job_id
    @abstractmethod
    def get(self, job_id: str) -> dict | None: ...  # состояние задачи или None
    @abstractmethod
    def list(self) -> list[dict]: ...               # список задач
```
- `task` - сериализуемый дескриптор `{kind, source, data|url+ref}`. Сериализуемость обязательна:
  RQ не умеет замыкания, поэтому через очередь едут данные, а не функция; исполняет их общий
  `src.ingest.runner.run_ingest`.
- Формат статуса (запись задачи) одинаков у обеих реализаций:
  `{id, status, progress, stats, error, kind, source, ...}`, где
  `status ∈ {queued, running, done, failed}`.

## Выбор реализации (`../../src/jobs/__init__.py`)

```python
def build_queue(jobs_cfg, redis_url=None) -> JobQueue:
    kind = (jobs_cfg or {}).get("kind", "inprocess")
    if kind == "redis":
        if not redis_url: raise ValueError("jobs.kind=redis требует redis_url")
        from src.jobs.redis_queue import RedisQueue
        return RedisQueue(redis_url)
    return InProcessQueue()
```
- `jobs.kind` (env `JOBS_KIND`, дефолт `inprocess`) разводит на реализацию. `redis` без
  `redis_url` - ошибка сборки.
- Конфиг (`config/config.yaml`): `jobs.kind: ${JOBS_KIND:-inprocess}` (`inprocess` small/dev,
  `redis` large/RQ).

Сборка в `factory.py`:

```python
jobs = build_queue(cfg.get("jobs"), cfg.get("redis_url"))
...
if hasattr(jobs, "bind"):
    jobs.bind(comp)        # InProcessQueue исполняет ingest в этом же процессе - нужен comp
```
- Только `InProcessQueue` имеет `bind`: ей нужен живой `comp` (исполняет в текущем процессе).
  `RedisQueue` строит свой `comp` лениво уже в worker-процессе.

## `InProcessQueue` - small/dev

Фон в потоке того же backend-процесса. Статус хранится в памяти процесса.

```python
def __init__(self, max_keep=100):
    self._jobs: dict[str, dict] = {}
    self._order: list[str] = []
    self._lock = threading.Lock()
    self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")
    self._max_keep = max_keep
    self._comp = None
```
- `max_workers=1` - один worker-поток сериализует ingest-ы, чтобы не писать в индекс-стор
  конкурентно.
- `max_keep=100` - кольцо последних задач в памяти; старые записи вытесняются.

```python
def submit(self, task) -> str:
    job_id = uuid.uuid4().hex[:12]
    rec = {"id": job_id, "status": "queued", "progress": {}, "stats": None, "error": None,
           "kind": ..., "source": ..., "created_at": ..., "finished_at": None}
    with self._lock:
        self._jobs[job_id] = rec; self._order.append(job_id)
        while len(self._order) > self._max_keep:
            self._jobs.pop(self._order.pop(0), None)     # вытеснение старых
    self._pool.submit(self._run, job_id, task)
    return job_id
```
- Запись задачи создаётся сразу в статусе `queued`, исполнение уходит в пул.

```python
def _run(self, job_id, task):
    from src.ingest.runner import run_ingest
    self._update(job_id, status="running")
    try:
        stats = run_ingest(task, lambda p: self._update(job_id, progress=p), self._comp)
        self._update(job_id, status="done", stats=stats, finished_at=...)
    except Exception as e:                               # любая ошибка ingest → failed
        self._update(job_id, status="failed", error=str(e), finished_at=...)
```
- Колбэк прогресса пишет `progress` прямо в запись задачи; любое исключение даёт `failed` с
  текстом ошибки.
- `get`/`list` отдают копии записей под `_lock` (`list` - новые первыми).

## `RedisQueue` - large (RQ)

Backend только кладёт задачу в Redis; исполняет отдельный worker-под (`services/worker_app.py`).
Статус, прогресс и результат хранят нативные RQ-registries и `job.meta` - своего стора нет.

```python
_QUEUE_NAME = "codelens-ingest"
_worker_comp = None   # composition root в процессе воркера (строится один раз)

def _comp() -> Components:
    global _worker_comp
    if _worker_comp is None:
        from src.factory import build
        _worker_comp = build()
    return _worker_comp
```
- `comp` воркера собирается лениво один раз на процесс (не на каждую задачу).

```python
def run_ingest_job(task) -> dict:                # тело RQ-задачи, исполняется в воркере
    from rq import get_current_job
    from src.ingest.runner import run_ingest
    job = get_current_job()
    def report(progress):
        if job is not None:
            job.meta["progress"] = progress; job.save_meta()
    return run_ingest(task, report, _comp())
```
- Enqueue по строковому пути функции `run_ingest_job` (importable), а не по объекту - RQ не умеет
  замыкания.
- Прогресс пишется в `job.meta` (видим через `get`/`list`), результат `run_ingest` - в
  `job.result`.

```python
class RedisQueue(JobQueue):
    def __init__(self, redis_url, queue_name=_QUEUE_NAME, job_timeout=3600, result_ttl=86400):
        self._conn = redis_conn(redis_url)
        self._q = Queue(queue_name, connection=self._conn, default_timeout=job_timeout)
        self._result_ttl = result_ttl

    def submit(self, task) -> str:
        job = self._q.enqueue(run_ingest_job, task, result_ttl=self._result_ttl,
                              meta={"kind": ..., "source": ...})
        return job.id
```
- `job_timeout=3600` (1ч на задачу), `result_ttl=86400` (результат живёт сутки в Redis).
- ZIP-байты едут в payload задачи через Redis; для очень больших архивов предпочтителен GitHub
  (URL вместо блоба).

```python
_STATUS = {"queued": "queued", "deferred": "queued", "scheduled": "queued",
           "started": "running", "finished": "done", "failed": "failed"}

def job_view(job) -> dict:                        # RQ Job → внутренний формат статуса
    status = _STATUS.get(job.get_status(refresh=False), "queued")
    err = job.exc_info.strip().splitlines()[-1] if status == "failed" and job.exc_info else None
    return {"id": job.id, "status": status,
            "progress": (job.meta or {}).get("progress", {}),
            "stats": job.result if status == "done" else None, "error": err, ...}
```
- `job_view` маппит RQ-статусы в общий формат (`get` и `list` строят ответ через него). Вынесен
  отдельно - тестируется без живого Redis.
- `list` собирает id из очереди и трёх registries (started/finished/failed) и дедуплицирует:
  один job_id может быть и в очереди, и в registry на переходе статуса.

```python
def redis_conn(url) -> Redis:
    # агрессивный TCP keepalive: первый пинг через 3с простоя, дальше каждые 2с
    return Redis.from_url(url, socket_keepalive=True, socket_keepalive_options=..., health_check_interval=30)
```
- Воркер блокируется на пустой очереди (BLPOP); в WSL2/Docker idle-соединение режется за секунды,
  системный keepalive стартует только через ~2ч. Поэтому опции задаются вручную, иначе воркер
  выходит с `Redis connection timeout`.

## Как worker тянет задачу (`services/worker_app.py`)

```python
def main():
    _comp()                                    # прогрев composition root на старте
    conn = redis_conn(redis_url)
    queue = Queue(_QUEUE_NAME, connection=conn)
    if metrics.start_metrics_server(int(os.environ.get("METRICS_PORT", "9100"))):
        threading.Thread(target=_poll_queue_depth, args=(queue,), daemon=True).start()
    Worker([queue], connection=conn).work(with_scheduler=False)
```
- `comp` греется на старте, чтобы первая задача не платила за инициализацию.
- У воркера нет HTTP-сервиса - `/metrics` поднимается отдельным портом (`METRICS_PORT`, дефолт
  9100). Фоновый поток `_poll_queue_depth` каждые 5с кладёт глубину очереди (`queued`/`started`/
  `failed`) в gauge `codelens_ingest_queue_jobs`. Длительность задачи меряет `run_ingest`
  (`ingest_timer`).
- Запуск: `python -m services.worker_app` (образ `deploy/Dockerfile.worker` - тот же пайплайн, что
  backend, но без моделей, эмбеддер удалённый). В docker-compose `worker` стоит с
  `restart: unless-stopped` - воркер может выйти на простое, перезапуск очередь не теряет.

## Сравнение реализаций

| | `InProcessQueue` (small/dev) | `RedisQueue` / RQ (large) |
|---|---|---|
| `jobs.kind` | `inprocess` (дефолт) | `redis` |
| Где исполняется | поток backend-процесса | отдельный worker-под |
| Параллелизм | 1 поток (сериализация записи в стор) | масштаб числом реплик воркера |
| Хранение статуса | словарь в памяти процесса | RQ-registries + `job.meta` в Redis |
| Прогресс | поле `progress` записи | `job.meta["progress"]` |
| Результат (`stats`) | поле `stats` записи | `job.result` (TTL `result_ttl=86400`) |
| История задач | последние `max_keep=100` в памяти | registries Redis (queued/started/finished/failed) |
| `comp` | внешний, через `bind()` | свой, лениво `build()` в воркере |
| Таймаут задачи | нет | `job_timeout=3600` |
| Метрика глубины очереди | нет | gauge `codelens_ingest_queue_jobs` (поллинг воркера) |
| Внешние зависимости | нет | Redis + worker-под |
| Кросс-репличный статус | нет (только single-replica) | да (общий Redis) |

## Что теряется при падении

- `InProcessQueue`: статус живёт только в памяти backend-процесса. Рестарт backend теряет весь
  список задач и прерывает выполняющийся ingest (он не перезапускается). Подходит для
  single-replica dev.
- `RedisQueue`: задача, статус, прогресс и результат - в Redis, переживают рестарт backend и
  воркера; `restart: unless-stopped` поднимает воркер обратно, очередь не теряется. Падение
  выполняющейся задачи RQ помечает `failed` (с `exc_info`). Падение самого Redis теряет очередь и
  историю; результаты в любом случае живут до `result_ttl`.
- Кэш поиска при ingest осиротевает через `bump_epoch` (сдвиг `index-epoch`), а не чистится по
  маске - стейл-записей не остаётся независимо от реализации очереди (см.
  [../persistence/caching.md](../persistence/caching.md)).

## Тесты

`../../tests/test_ingest.py`: e2e через `InProcessQueue` + `LocalBackend.ingest_zip` (реальные
`run_ingest`/`index_path`, статус `done`, прогресс по чанкам, появление в `ingest_jobs()`); ingest
на чужом хосте → `failed`; `job_view` маппинг RQ-статусов `finished`/`failed` в общий формат без
живого Redis.
