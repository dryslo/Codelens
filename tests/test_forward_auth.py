"""forward-auth - resolve_refresh (read-only) и эндпоинт /auth/forward-auth."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth.config import AuthConfig
from src.auth.repositories import (
    SqlCredentials,
    SqlIdentities,
    SqlRefreshTokens,
    SqlUsers,
)
from src.auth.router import router as auth_router
from src.auth.service import AuthService
from src.persistence.cache import InProcessCache
from src.persistence.db import init_db, make_session_factory


def _auth(tmp_path):
    dsn = f"sqlite:///{tmp_path}/fa.db"
    init_db(dsn)
    sf = make_session_factory(dsn)
    cfg = AuthConfig(enabled=True, secret="test-secret-key-0123456789-abcdef", alg="HS256")
    return AuthService(SqlUsers(sf), SqlCredentials(sf), SqlIdentities(sf),
                       SqlRefreshTokens(sf), InProcessCache(), cfg)


def _client(auth):
    app = FastAPI()
    app.state.auth = auth
    app.include_router(auth_router)
    return TestClient(app)


# ---------- resolve_refresh: read-only, без ротации ----------

def test_resolve_refresh_returns_user_no_rotation(tmp_path):
    auth = _auth(tmp_path)
    auth.ensure_admin("root", "pw")
    rt = auth.login_password("root", "pw")["refresh_token"]
    u1 = auth.resolve_refresh(rt)
    u2 = auth.resolve_refresh(rt)                 # тот же токен снова валиден (нет ротации)
    assert u1 and u1["role"] == "admin"
    assert u2 == u1


def test_resolve_refresh_invalid_and_empty(tmp_path):
    auth = _auth(tmp_path)
    assert auth.resolve_refresh("nope") is None
    assert auth.resolve_refresh(None) is None


def test_resolve_refresh_revoked_is_none(tmp_path):
    auth = _auth(tmp_path)
    auth.ensure_admin("root", "pw")
    rt = auth.login_password("root", "pw")["refresh_token"]
    auth.refresh(rt)                              # ротация: старый refresh отозван
    assert auth.resolve_refresh(rt) is None


# ---------- эндпоинт /auth/forward-auth ----------

def test_forward_auth_admin_200(tmp_path):
    auth = _auth(tmp_path)
    auth.ensure_admin("root", "pw")
    c = _client(auth)
    c.post("/auth/login", json={"login": "root", "password": "pw"})   # refresh-кука осела в клиенте
    r = c.get("/auth/forward-auth")
    assert r.status_code == 200
    assert r.headers["X-Auth-User"] == "root"          # для Grafana auth.proxy
    assert r.headers["X-Auth-Role"] == "Admin"


def test_forward_auth_user_401(tmp_path):
    auth = _auth(tmp_path)
    c = _client(auth)
    c.post("/auth/register", json={"login": "u", "password": "pw"})   # role=user
    c.post("/auth/login", json={"login": "u", "password": "pw"})
    assert c.get("/auth/forward-auth").status_code == 401


def test_forward_auth_no_cookie_401(tmp_path):
    assert _client(_auth(tmp_path)).get("/auth/forward-auth").status_code == 401
