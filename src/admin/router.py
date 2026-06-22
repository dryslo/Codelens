"""Админ-роутер: общая зависимость require_admin на всю группу (`/admin/*`)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from src.auth.deps import get_auth, require_admin
from src.auth.schemas import SetRoleReq
from src.auth.service import AuthService  # рантайм-импорт: тип в сигнатурах роутов резолвит FastAPI
from src.persistence.schemas import IndexReq, IngestGithubReq, RemoveReq

if TYPE_CHECKING:
    from src.domain.interfaces import BackendClient

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _backend(request: Request) -> BackendClient:
    """Backend-клиент из состояния приложения."""
    return request.app.state.backend


# Кодовая база.
@router.get("/stats")
def stats(request: Request) -> dict:
    """Статистика индекса."""
    return _backend(request).stats()


@router.post("/index")
def index(r: IndexReq, request: Request) -> dict:
    """Проиндексировать папку источника."""
    return _backend(request).index(r.folder, r.source, r.incremental)


@router.post("/remove")
def remove(r: RemoveReq, request: Request) -> dict:
    """Удалить источник из индекса."""
    return _backend(request).remove(r.source)


async def _read_capped(file: UploadFile, limit: int) -> bytes:
    """Прочитать загрузку чанками, прервав с 413 при превышении лимита байт."""
    buf = bytearray()
    while True:
        chunk = await file.read(1 << 20)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            raise HTTPException(status_code=413, detail="файл превышает лимит загрузки")
    return bytes(buf)


# Ingest из админки (фоном): ZIP-загрузка / GitHub-ссылка.
@router.post("/ingest/zip")
async def ingest_zip(request: Request, source: str = Form(...),
                     file: UploadFile = File(...)) -> dict:
    """Поставить ingest загруженного ZIP-архива в фон (с лимитом размера на уровне FastAPI)."""
    cfg = getattr(request.app.state, "cfg", {}) or {}
    limit = int((cfg.get("ingest") or {}).get("max_upload_mb", 100)) * 1024 * 1024
    return _backend(request).ingest_zip(await _read_capped(file, limit), source)


@router.post("/ingest/github")
def ingest_github(r: IngestGithubReq, request: Request) -> dict:
    """Поставить ingest GitHub-репозитория в фон."""
    return _backend(request).ingest_github(r.url, r.ref, r.source)


@router.get("/ingest/jobs")
def ingest_jobs(request: Request) -> dict:
    """Список ingest-задач."""
    return {"jobs": _backend(request).ingest_jobs()}


@router.get("/ingest/jobs/{job_id}")
def ingest_job(job_id: str, request: Request) -> dict:
    """Статус ingest-задачи по id."""
    return _backend(request).ingest_job(job_id) or {"error": "job not found"}


# Пользователи.
@router.get("/users")
def list_users(auth: AuthService = Depends(get_auth)) -> dict:
    """Список пользователей."""
    return {"users": auth.list_users()}


@router.post("/users/{user_id}/role")
def set_role(user_id: str, r: SetRoleReq, auth: AuthService = Depends(get_auth)) -> dict:
    """Сменить роль пользователя."""
    return auth.set_role(user_id, r.role)
