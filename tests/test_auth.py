from src.auth.config import AuthConfig
from src.auth.passwords import hash_password, verify_password
from src.auth.repositories import (
    SqlCredentials,
    SqlIdentities,
    SqlRefreshTokens,
    SqlUsers,
)
from src.auth.service import AuthService
from src.auth.tokens import decode_access, hash_refresh, make_access_token, make_refresh_token
from src.persistence.cache import InProcessCache
from src.persistence.db import init_db, make_session_factory


# --- пароли (argon2) ---

def test_passwords_roundtrip():
    h = hash_password("s3cret")
    assert h != "s3cret" and h.startswith("$argon2")
    assert verify_password("s3cret", h)
    assert not verify_password("wrong", h)
    assert not verify_password("s3cret", "garbage")


# --- токены ---

_SEC = "sec-key-0123456789-abcdefghijklmn"   # не короче 32 байт, иначе PyJWT предупреждает


def test_access_token_roundtrip():
    tok, jti = make_access_token(_SEC, "HS256", {"id": "u1", "role": "user", "login": "a"}, 900)
    claims = decode_access(tok, _SEC, "HS256")
    assert claims and claims["sub"] == "u1" and claims["jti"] == jti and claims["role"] == "user"
    assert decode_access(tok, "wrong", "HS256") is None      # чужая подпись
    assert decode_access(tok + "x", _SEC, "HS256") is None   # повреждён


def test_expired_access_rejected():
    tok, _ = make_access_token(_SEC, "HS256", {"id": "u", "role": "user"}, -10)
    assert decode_access(tok, _SEC, "HS256") is None


def test_refresh_hash_deterministic():
    rt = make_refresh_token()
    assert hash_refresh(rt) == hash_refresh(rt) and len(hash_refresh(rt)) == 64


# --- AuthService ---

def _auth(tmp_path, enabled=True, access_ttl=900):
    dsn = f"sqlite:///{tmp_path}/auth.db"
    init_db(dsn)
    sf = make_session_factory(dsn)
    cfg = AuthConfig(enabled=enabled, secret="test-secret-key-0123456789-abcdef", alg="HS256",
                     access_ttl=access_ttl, refresh_ttl=3600)
    return AuthService(SqlUsers(sf), SqlCredentials(sf), SqlIdentities(sf),
                       SqlRefreshTokens(sf), InProcessCache(), cfg)


def test_register_login_flow(tmp_path):
    a = _auth(tmp_path)
    assert a.register("alice", "pw").get("ok")
    assert "error" in a.register("alice", "pw")              # дубликат логина
    res = a.login_password("alice", "pw")
    assert res["access_token"] and res["refresh_token"] and res["user"]["role"] == "user"
    assert a.resolve_access(res["access_token"])["user_id"] == res["user"]["user_id"]
    assert "error" in a.login_password("alice", "bad")


def test_refresh_rotation(tmp_path):
    a = _auth(tmp_path)
    a.register("bob", "pw")
    rt = a.login_password("bob", "pw")["refresh_token"]
    new = a.refresh(rt)
    assert new.get("access_token") and new["refresh_token"] != rt
    assert "error" in a.refresh(rt)                          # старый refresh отозван (ротация)


def test_logout_revokes_access_and_refresh(tmp_path):
    a = _auth(tmp_path)
    a.register("c", "pw")
    res = a.login_password("c", "pw")
    access, rt = res["access_token"], res["refresh_token"]
    assert a.resolve_access(access)
    a.logout(access)
    assert a.resolve_access(access) is None                  # access-сессия снята из кэша
    assert "error" in a.refresh(rt)                          # refresh отозван


def test_oidc_upsert_same_user(tmp_path):
    a = _auth(tmp_path)
    r1 = a.login_oidc("google", "sub-123", {"email": "x@y.z"})
    r2 = a.login_oidc("google", "sub-123", {})
    assert r1["user"]["user_id"] == r2["user"]["user_id"]
    assert a.users.count() == 1


def test_resolve_access_disabled_is_anon_admin(tmp_path):
    a = _auth(tmp_path, enabled=False)
    u = a.resolve_access(None)
    assert u["role"] == "admin" and u["user_id"] == "anon"


def test_resolve_access_requires_valid_token(tmp_path):
    a = _auth(tmp_path)
    assert a.resolve_access(None) is None
    assert a.resolve_access("bad.token") is None


def test_admin_bootstrap_and_roles(tmp_path):
    a = _auth(tmp_path)
    a.ensure_admin("root", "pw")
    a.ensure_admin("root", "pw")                             # идемпотентно
    assert a.login_password("root", "pw")["user"]["role"] == "admin"
    assert any(u["login"] == "root" and u["role"] == "admin" for u in a.list_users())
