# Ingest кодовой базы

Разбор `../../src/ingest/acquire.py` и `../../src/ingest/runner.py` - получение корпуса
(ZIP-загрузка из админки или клон GitHub) и запуск индексации над ним.

Ingest всегда идёт фоновой задачей через очередь ([../jobs/jobs.md](../jobs/jobs.md)): админ-роутер
кладёт сериализуемый дескриптор задачи, а исполняет его `run_ingest` - в потоке того же процесса
(`InProcessQueue`) либо в отдельном worker-поде (`RedisQueue`/RQ). Тело задачи одно для обеих
очередей.

## Конвейер задачи

```
admin POST /ingest/zip|github  →  jobs.submit(task)  →  run_ingest(task, report, comp)
                                                            ├─ acquire: from_zip | from_github → folder
                                                            ├─ comp.index_path(folder, source, ...)
                                                            └─ bump_epoch(cache); rmtree(folder)
```

`task` - словарь-дескриптор: `{"kind": "zip", "source": ..., "data": <bytes>}` либо
`{"kind": "github", "source": ..., "url": ..., "ref": ...}`. Его собирает
`LocalBackend.ingest_zip`/`ingest_github` и передаёт в `jobs.submit` (см.
`../../src/clients/backend.py`). Сериализуемость нужна, потому что RQ не умеет замыкания и
гоняет дескриптор через Redis.

## Получение корпуса (`acquire.py`)

Модуль возвращает путь к временной папке с распакованным кодом. Код репозитория не исполняется -
дальше его только парсит AST в [pipeline.index_path](../indexing/pipeline.md). Чистит папку
вызывающая сторона (`runner`).

```python
MAX_FILES = 20000
MAX_TOTAL_BYTES = 500 * 1024 * 1024          # распакованный размер ZIP
GITHUB_TIMEOUT = 60
_GH_RE = re.compile(r"^https?://github\.com/([^/]+)/([^/#?]+?)(?:\.git)?/?$")
```
- Лимиты против zip-bomb (число файлов и суммарный распакованный размер).
- `_GH_RE` - anti-SSRF: принимается только `github.com/<owner>/<repo>`, прочие хосты отклоняются
  до сетевого вызова.

```python
def _new_tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="codelens-ingest-"))
```
- Каждый ingest получает свежий каталог `codelens-ingest-*` в системном temp.

### ZIP-загрузка - `from_zip`

```python
def from_zip(data, *, max_files=MAX_FILES, max_total=MAX_TOTAL_BYTES) -> Path:
    root = _new_tmp()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = [i for i in zf.infolist() if not i.is_dir()]
        if len(members) > max_files: raise ValueError(...)        # лимит числа файлов
        total = 0
        for info in members:
            total += info.file_size
            if total > max_total: raise ValueError("zip-bomb: ...")  # лимит распакованного размера
            _check_no_slip(root, info.filename)                      # zip-slip
            zf.extract(info, root)
    return root
```
- Принимает уже прочитанные байты архива (их читает админ-роутер чанками с лимитом, см. ниже).
- Три защиты: переполнение по числу файлов (`max_files`), zip-bomb по суммарному
  `file_size` из заголовков (счёт до распаковки), zip-slip.

```python
def _check_no_slip(root, name) -> None:
    rr = root.resolve()
    target = (root / name).resolve()
    if rr != target and rr not in target.parents:
        raise ValueError(f"zip-slip: путь вне каталога архива: {name!r}")
```
- Запись по относительному пути (`../evil.py`) или абсолютному не должна вылезти за `root`:
  резолвится итоговый путь и проверяется, что `root` - его предок.

### Клон GitHub - `from_github`

```python
def from_github(url, ref=None, *, timeout=GITHUB_TIMEOUT) -> Path:
    m = _GH_RE.match((url or "").strip())
    if not m: raise ValueError("ожидается публичный URL вида https://github.com/<owner>/<repo>")
    owner, repo = m.group(1), m.group(2)
    import requests
    refs = [ref] if ref else ["main", "master"]   # без ref - дефолтные ветки
    for r in refs:
        tar_url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/{r}"
        resp = requests.get(tar_url, timeout=timeout)
        if resp.status_code == 200:
            return _extract_tar(resp.content)
        last = resp.status_code
    raise ValueError(f"не удалось скачать {owner}/{repo} (ref=..., http={last})")
```
- Не клон git, а скачивание `tar.gz` снапшота через `codeload.github.com`. Без `git`-зависимости.
- Без явного `ref` пробуются `main`, затем `master`. С `ref` - только он.
- Хост жёстко зафиксирован regex-ом (`github.com`), URL `codeload.*` строится из распарсенных
  `owner`/`repo` - произвольный хост подставить нельзя.

```python
def _extract_tar(data) -> Path:
    root = _new_tmp()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        tf.extractall(root, filter="data")        # 3.12+: защита от traversal/спецфайлов
    subs = [p for p in root.iterdir() if p.is_dir()]
    return subs[0] if len(subs) == 1 else root
```
- `filter="data"` (Python 3.12+) отсекает path-traversal и спецфайлы (симлинки, устройства) на
  стороне tarfile.
- `codeload` кладёт всё в единственную подпапку `<repo>-<ref>/` - она и возвращается как корень
  корпуса (а не временный `root`).

## Запуск индексации (`runner.py`)

Общее тело ingest-задачи. Один код для обеих очередей: получает `task`, колбэк прогресса `report`
и composition root `comp` (`store`/`embedder`/`registry`/`cache`/`index_path`).

```python
def run_ingest(task, report, comp) -> dict:
    with metrics.ingest_timer():     # длительность job по финальному статусу (done|failed)
        return _run_ingest(task, report, comp)
```
- Внешняя обёртка только меряет длительность (см. «Метрики»).

```python
def _run_ingest(task, report, comp) -> dict:
    kind = task["kind"]; source = task["source"]
    if kind == "zip":
        from src.ingest.acquire import from_zip
        folder = from_zip(task["data"])
    elif kind == "github":
        from src.ingest.acquire import from_github
        folder = from_github(task["url"], task.get("ref"))
    else:
        raise ValueError(f"неизвестный тип ingest: {kind!r}")
    try:
        res = comp.index_path(str(folder), source, comp.store, comp.embedder,
                              comp.registry, True, progress=report)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    cache = comp.cache
    if cache is not None and getattr(cache, "enabled", False):
        bump_epoch(cache)        # индекс изменился, осиротить кэш поиска
    return res
```
- `kind` разводит на `acquire`-функцию; неизвестный тип - ошибка (задача завершится `failed`).
- `comp.index_path(folder, source, store, embedder, registry, True, progress=report)` -
  единая точка индексации (`incremental=True`); разбор - в [indexing/pipeline.md](../indexing/pipeline.md).
  `progress=report` пробрасывает прогресс наружу (для `InProcessQueue` - в запись задачи, для
  `RedisQueue` - в `job.meta`).
- `folder` удаляется в `finally` всегда - и на успехе, и на исключении. Acquire создаёт временную
  папку, runner её убирает.
- После изменения индекса сдвигается `index-epoch` (`bump_epoch`) - кэш поиска осиротевает (тот же
  механизм инвалидации, что у `LocalBackend.index`, см. [persistence/caching.md](../persistence/caching.md)).
  Под `NullCache` (`enabled=False`) шаг пропускается.
- Возвращает результат `index_path` (`{"added": ..., ...}`) - он становится `stats` задачи.

## Связь с админкой

Постановка - в `../../src/admin/router.py`, вся группа `/admin/*` за `require_admin`:

```python
@router.post("/ingest/zip")
async def ingest_zip(request, source=Form(...), file=UploadFile(...)) -> dict:
    limit = int((cfg.get("ingest") or {}).get("max_upload_mb", 100)) * 1024 * 1024
    return _backend(request).ingest_zip(await _read_capped(file, limit), source)
```
- `_read_capped` читает загрузку чанками по 1 MiB и прерывает с HTTP 413 при превышении
  `ingest.max_upload_mb` (`MAX_UPLOAD_MB`, дефолт 100). Это первый рубеж до `from_zip` (у того свой
  лимит на распакованный размер).
- `POST /ingest/github` принимает `{url, ref, source}` и сразу ставит задачу.
- `GET /ingest/jobs` и `GET /ingest/jobs/{job_id}` отдают список и статус задач из очереди.

`LocalBackend` лишь формирует `task` и зовёт `jobs.submit`; исполнение и хранение статуса - забота
очереди ([../jobs/jobs.md](../jobs/jobs.md)).

## Метрики ingest (`../../src/util/metrics.py`)

```python
INGEST_JOB = Histogram("codelens_ingest_job_duration_seconds",
                       "Длительность ingest-job по финальному статусу", ["status"], buckets=_SLOW)
```
- `ingest_timer()` оборачивает `_run_ingest` и пишет длительность с меткой `status=done` либо
  `status=failed` (исключение → `failed`, затем re-raise). Бакеты `_SLOW` - 0.5с-10мин.
- Глубину очереди (`codelens_ingest_queue_jobs`, метки `queued`/`started`/`failed`) выставляет
  только worker через `set_queue_depth` (см. [../jobs/jobs.md](../jobs/jobs.md)); у `InProcessQueue`
  отдельного gauge нет.
- В worker-поде нет HTTP-сервиса, поэтому `/metrics` поднимается отдельным портом
  (`METRICS_PORT`, дефолт 9100).

## Тесты

`../../tests/test_ingest.py`: `from_zip` (успех, отказ на zip-slip, лимит числа файлов),
`from_github` (отказ на не-`github.com`, успех на замоканном `requests.get`), e2e через
`InProcessQueue` + `LocalBackend.ingest_zip` (реальные `run_ingest`/`index_path`: статус `done`,
`stats.added`, прогресс по чанкам), `ingest_github` на чужом хосте → `failed`.
