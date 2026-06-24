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
from src.auth.tokens import decode_gate, make_access_token
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


# ---------- gate-кука: нерротируемый путь для панелей ----------

def test_login_issues_gate_token(tmp_path):
    auth = _auth(tmp_path)
    auth.ensure_admin("root", "pw")
    res = auth.login_password("root", "pw")
    claims = decode_gate(res["gate_token"], auth.cfg.secret, auth.cfg.alg)
    assert claims and claims["role"] == "admin" and claims["login"] == "root"
    # access-токен не принимается как gate (другой type)
    acc, _ = make_access_token(auth.cfg.secret, auth.cfg.alg, {"id": "x", "role": "admin"}, 900)
    assert decode_gate(acc, auth.cfg.secret, auth.cfg.alg) is None


def test_forward_auth_via_gate_cookie_only(tmp_path):
    auth = _auth(tmp_path)
    auth.ensure_admin("root", "pw")
    res = auth.login_password("root", "pw")
    c = _client(auth)
    c.cookies.set("codelens_gate", res["gate_token"])        # только gate, без refresh-куки
    r = c.get("/auth/forward-auth")
    assert r.status_code == 200 and r.headers["X-Auth-Role"] == "Admin"


def test_forward_auth_gate_survives_refresh_rotation(tmp_path):
    auth = _auth(tmp_path)
    auth.ensure_admin("root", "pw")
    res = auth.login_password("root", "pw")
    auth.refresh(res["refresh_token"])                       # refresh отозван ротацией
    c = _client(auth)
    c.cookies.set("codelens_gate", res["gate_token"])
    c.cookies.set("codelens_rt", res["refresh_token"])       # эта кука уже невалидна
    assert c.get("/auth/forward-auth").status_code == 200    # gate спасает - панели не падают


def test_forward_auth_user_gate_401(tmp_path):
    auth = _auth(tmp_path)
    auth.register("u", "pw")
    res = auth.login_password("u", "pw")                     # role=user
    c = _client(auth)
    c.cookies.set("codelens_gate", res["gate_token"])
    assert c.get("/auth/forward-auth").status_code == 401
