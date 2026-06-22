"""httpOnly refresh-cookie, rate-limit login/register, лимит загрузки ZIP."""
import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.admin.router import _read_capped
from src.auth import ratelimit
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

COOKIE = "codelens_rt"


def _app(tmp_path, attempts=10):
    dsn = f"sqlite:///{tmp_path}/sec.db"
    init_db(dsn)
    sf = make_session_factory(dsn)
    cfg = AuthConfig(enabled=True, secret="test-secret-key-0123456789-abcdef", alg="HS256",
                     access_ttl=900, refresh_ttl=3600, rate_limit_attempts=attempts,
                     rate_limit_window=60)
    auth = AuthService(SqlUsers(sf), SqlCredentials(sf), SqlIdentities(sf),
                       SqlRefreshTokens(sf), InProcessCache(), cfg)
    app = FastAPI()
    app.state.auth = auth
    app.include_router(auth_router)
    return TestClient(app)


# ---------- httpOnly refresh-cookie ----------

def test_login_sets_httponly_cookie(tmp_path):
    c = _app(tmp_path)
    c.post("/auth/register", json={"login": "a", "password": "pw"})
    r = c.post("/auth/login", json={"login": "a", "password": "pw"})
    assert r.status_code == 200 and r.json()["access_token"]
    set_cookie = r.headers.get("set-cookie", "").lower()
    assert COOKIE in set_cookie and "httponly" in set_cookie
    assert c.cookies.get(COOKIE)


def test_refresh_from_cookie_without_body(tmp_path):
    c = _app(tmp_path)
    c.post("/auth/register", json={"login": "b", "password": "pw"})
    c.post("/auth/login", json={"login": "b", "password": "pw"})   # cookie осел в клиенте
    r = c.post("/auth/refresh")                                    # без тела - токен из cookie
    assert r.status_code == 200 and r.json()["access_token"]


def test_refresh_from_body_still_works(tmp_path):
    c = _app(tmp_path)
    c.post("/auth/register", json={"login": "d", "password": "pw"})
    rt = c.post("/auth/login", json={"login": "d", "password": "pw"}).json()["refresh_token"]
    c.cookies.clear()
    r = c.post("/auth/refresh", json={"refresh_token": rt})
    assert r.status_code == 200 and r.json()["access_token"]


def test_refresh_without_token_is_401(tmp_path):
    c = _app(tmp_path)
    c.cookies.clear()
    assert c.post("/auth/refresh").status_code == 401


def test_logout_clears_cookie(tmp_path):
    c = _app(tmp_path)
    c.post("/auth/register", json={"login": "e", "password": "pw"})
    tok = c.post("/auth/login", json={"login": "e", "password": "pw"}).json()["access_token"]
    r = c.post("/auth/logout", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "max-age=0" in r.headers.get("set-cookie", "").lower()


# ---------- rate-limit ----------

def test_login_rate_limited(tmp_path):
    c = _app(tmp_path, attempts=2)
    codes = [c.post("/auth/login", json={"login": "x", "password": "bad"}).status_code
             for _ in range(3)]
    assert codes[:2] == [401, 401] and codes[2] == 429


def test_register_rate_limited(tmp_path):
    c = _app(tmp_path, attempts=1)
    c.post("/auth/register", json={"login": "u1", "password": "pw"})
    assert c.post("/auth/register", json={"login": "u2", "password": "pw"}).status_code == 429


# ---------- ratelimit.allow (юнит) ----------

def test_allow_in_process_fallback():
    ratelimit._local.clear()
    assert ratelimit.allow(None, "k", 2, 60) is True
    assert ratelimit.allow(None, "k", 2, 60) is True
    assert ratelimit.allow(None, "k", 2, 60) is False


def test_allow_zero_limit_disabled():
    assert ratelimit.allow(None, "k2", 0, 60) is True


def test_allow_uses_cache():
    cache = InProcessCache()
    assert ratelimit.allow(cache, "c", 1, 60) is True
    assert ratelimit.allow(cache, "c", 1, 60) is False


# ---------- лимит загрузки (_read_capped) ----------

class _FakeUpload:
    def __init__(self, data, chunk=512):
        self._buf, self._i, self._chunk = data, 0, chunk

    async def read(self, n):
        part = self._buf[self._i:self._i + n]
        self._i += n
        return part


def test_read_capped_ok():
    up = _FakeUpload(b"x" * 500)
    assert asyncio.run(_read_capped(up, 1024)) == b"x" * 500


def test_read_capped_rejects_oversize():
    up = _FakeUpload(b"x" * 4096)
    with pytest.raises(HTTPException) as e:
        asyncio.run(_read_capped(up, 1024))
    assert e.value.status_code == 413
