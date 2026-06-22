"""acquire (zip-slip/bomb, github), InProcessQueue, LocalBackend.ingest_zip e2e."""
import io
import tarfile
import time
import zipfile

import pytest
import requests

from src.clients.backend import LocalBackend
from src.factory import Components
from src.indexing import pipeline
from src.ingest.acquire import from_github, from_zip
from src.jobs import InProcessQueue


# ---------- helpers ----------

def _zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _targz_bytes(top: str, files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _wait(q, jid, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = q.get(jid)
        if j and j["status"] in ("done", "failed"):
            return j
        time.sleep(0.02)
    raise AssertionError(f"job не завершился: {q.get(jid)}")


# ---------- acquire: ZIP ----------

def test_from_zip_ok():
    folder = from_zip(_zip_bytes({"pkg/m.py": "def a():\n    return 1\n"}))
    assert (folder / "pkg" / "m.py").read_text().startswith("def a")


def test_from_zip_rejects_slip():
    with pytest.raises(ValueError, match="zip-slip"):
        from_zip(_zip_bytes({"../evil.py": "x"}))


def test_from_zip_rejects_too_many_files():
    data = _zip_bytes({f"f{i}.py": "x" for i in range(5)})
    with pytest.raises(ValueError, match="слишком много"):
        from_zip(data, max_files=2)


# ---------- acquire: GitHub ----------

def test_from_github_bad_host():
    with pytest.raises(ValueError, match="github.com"):
        from_github("https://gitlab.com/owner/repo")


def test_from_github_ok(monkeypatch):
    tar = _targz_bytes("repo-main", {"m.py": "def a():\n    return 1\n"})

    class _R:
        status_code = 200
        content = tar

    monkeypatch.setattr(requests, "get", lambda url, timeout: _R())
    folder = from_github("https://github.com/owner/repo")
    assert (folder / "m.py").exists()


# ---------- InProcessQueue + LocalBackend (e2e через реальный index_path/run_ingest) ----------

class _Embedder:
    def encode(self, texts, is_query=False):
        return [[0.0] for _ in texts]


class _Store:
    def __init__(self):
        self.added = 0

    def delete_where(self, **_):
        pass

    def add(self, ids, embs, metas, codes):
        self.added += len(ids)

    def count(self):
        return self.added


class _Registry:
    def __init__(self):
        self.h = {}

    def get_hash(self, s, f):
        return self.h.get((s, f))

    def set_hash(self, s, f, x):
        self.h[(s, f)] = x

    def files(self, s):
        return [f for (ss, f) in self.h if ss == s]

    def remove(self, s, f=None):
        for k in [k for k in self.h if k[0] == s and (f is None or k[1] == f)]:
            self.h.pop(k)


def _bound_backend():
    q = InProcessQueue()
    comp = Components(index_path=pipeline.index_path, store=_Store(), embedder=_Embedder(),
                      registry=_Registry(), jobs=q, cache=None, cfg={})
    q.bind(comp)                       # InProcessQueue исполняет run_ingest в этом процессе
    return LocalBackend(comp), comp


def test_localbackend_ingest_zip_e2e():
    be, comp = _bound_backend()
    res = be.ingest_zip(_zip_bytes({"m.py": "def a():\n    return 1\n"}), "up")
    job = _wait(comp.jobs, res["job_id"])
    assert job["status"] == "done"
    assert comp.store.added >= 1
    assert job["stats"]["added"] == 1
    assert job["progress"]["chunks_indexed"] >= 1     # прогресс по чанкам, не только файлам
    assert any(j["id"] == res["job_id"] for j in be.ingest_jobs())


def test_localbackend_ingest_github_bad_host_fails():
    be, comp = _bound_backend()
    res = be.ingest_github("https://gitlab.com/owner/repo", None, "s")
    job = _wait(comp.jobs, res["job_id"])
    assert job["status"] == "failed" and "github.com" in job["error"]


# ---------- RedisQueue: маппинг статуса (без живого Redis/RQ) ----------

def test_job_view_status_mapping():
    from src.jobs.redis_queue import job_view

    class _Job:
        id = "j1"
        meta = {"progress": {"files_done": 2}, "kind": "zip", "source": "s"}
        result = {"added": 3}
        exc_info = None

        def get_status(self, refresh=False):
            return "finished"

    v = job_view(_Job())
    assert v["status"] == "done" and v["stats"] == {"added": 3}
    assert v["progress"]["files_done"] == 2 and v["source"] == "s"

    class _Failed(_Job):
        exc_info = "Traceback ...\nValueError: boom"

        def get_status(self, refresh=False):
            return "failed"

    vf = job_view(_Failed())
    assert vf["status"] == "failed" and "boom" in vf["error"] and vf["stats"] is None
