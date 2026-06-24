"""JWT access-токены и opaque refresh-токены.

access  - короткоживущий JWT (claims sub/role/login/jti/exp), верифицируется подписью;
          серверная сессия по jti хранится в кэше (allow-list → мгновенный отзыв).
refresh - случайная строка, отдаётся клиенту; в БД хранится только sha256-хэш.
"""
import hashlib
import secrets
import time
import uuid

import jwt


def new_jti() -> str:
    """Сгенерировать уникальный идентификатор токена (jti)."""
    return uuid.uuid4().hex


def make_access_token(secret: str, alg: str, user: dict, ttl: int) -> tuple[str, str]:
    """Вернуть (jwt, jti). user: {id, role, login}."""
    jti = new_jti()
    now = int(time.time())
    claims = {
        "sub": user["id"], "role": user.get("role", "user"), "login": user.get("login"),
        "type": "access", "jti": jti, "iat": now, "exp": now + ttl,
    }
    return jwt.encode(claims, secret, algorithm=alg), jti


def decode_access(token: str, secret: str, alg: str) -> dict | None:
    """Верификация подписи и срока. None при любой ошибке/неверном типе."""
    try:
        claims = jwt.decode(token, secret, algorithms=[alg])
    except jwt.PyJWTError:
        return None
    return claims if claims.get("type") == "access" else None


def make_gate_token(secret: str, alg: str, user: dict, ttl: int) -> str:
    """Подписанный gate-токен для forward-auth панелей: claims role/login, без БД и без ротации.

    Отдельно от refresh: refresh ротируется (на гонках Streamlit-rerun браузерная кука протухает),
    а gate проверяется только подписью+сроком, поэтому слегка устаревшая копия в браузере всё равно
    валидна - панели не ловят 401. Снимается при logout (кука удаляется фронтом).
    """
    now = int(time.time())
    claims = {
        "sub": user["id"], "role": user.get("role", "user"), "login": user.get("login"),
        "type": "gate", "iat": now, "exp": now + ttl,
    }
    return jwt.encode(claims, secret, algorithm=alg)


def decode_gate(token: str, secret: str, alg: str) -> dict | None:
    """Верификация подписи и срока gate-токена. None при любой ошибке/неверном типе."""
    try:
        claims = jwt.decode(token, secret, algorithms=[alg])
    except jwt.PyJWTError:
        return None
    return claims if claims.get("type") == "gate" else None


def make_refresh_token() -> str:
    """Сгенерировать случайный opaque refresh-токен."""
    return secrets.token_urlsafe(48)


def hash_refresh(token: str) -> str:
    """Вернуть sha256-хэш refresh-токена для хранения в БД."""
    return hashlib.sha256(token.encode()).hexdigest()
