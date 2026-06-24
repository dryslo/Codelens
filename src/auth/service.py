"""AuthService - оркестрация авторизации.

Логин (пароль argon2 из credentials, либо OIDC по identities) выдаёт пару токенов:
  access  - JWT, серверная сессия по jti в кэше (allow-list, мгновенный отзыв);
  refresh - opaque-строка, в БД только её sha256-хэш; ротация при каждом /refresh.
"""
from datetime import datetime, timedelta, timezone
from typing import Any

from src.auth.config import AuthConfig
from src.auth.passwords import hash_password, verify_password
from src.auth.tokens import (
    decode_access,
    hash_refresh,
    make_access_token,
    make_refresh_token,
    new_jti,
)

# Псевдо-пользователь при выключенной авторизации (dev): полный доступ без логина.
ANON = {"user_id": "anon", "login": "anon", "role": "admin"}


def _public(user: dict) -> dict:
    """Вернуть публичное представление пользователя."""
    return {"user_id": user["id"], "login": user.get("login"), "role": user.get("role", "user")}


def _utcnow() -> datetime:
    """Вернуть текущее время как naive UTC (как в БД)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)   # naive UTC (как в БД)


class AuthService:
    """Оркестрация регистрации, логина, выдачи и отзыва токенов."""

    def __init__(self, users: Any, creds: Any, identities: Any, refresh_tokens: Any,
                 cache: Any, cfg: AuthConfig) -> None:
        self.users = users
        self.creds = creds
        self.identities = identities
        self.refresh_tokens = refresh_tokens
        self.cache = cache
        self.cfg = cfg

    # --- выдача токенов ---
    def _issue_access(self, user: dict, refresh_token: str) -> dict:
        """Выдать новый access для существующего refresh-токена (без создания нового refresh)."""
        pub = _public(user)
        access, jti = make_access_token(self.cfg.secret, self.cfg.alg, user, self.cfg.access_ttl)
        if self.cache is not None:
            self.cache.set(f"access:{jti}", pub, ttl=self.cfg.access_ttl)
        return {"access_token": access, "refresh_token": refresh_token,
                "token_type": "bearer", "user": pub}

    def _issue(self, user: dict) -> dict:
        rt = make_refresh_token()
        self.refresh_tokens.create(new_jti(), user["id"], hash_refresh(rt),
                                   _utcnow() + timedelta(seconds=self.cfg.refresh_ttl))
        return self._issue_access(user, rt)

    # --- регистрация / логин паролем ---
    def register(self, login: str, password: str) -> dict:
        """Зарегистрировать пользователя по логину и паролю."""
        if not login or not password:
            return {"error": "empty login/password"}
        if self.users.get_by_login(login):
            return {"error": "login already exists"}
        uid = self.users.create(login, role="user")
        self.creds.set(uid, hash_password(password))
        return {"ok": True}

    def login_password(self, login: str, password: str) -> dict:
        """Проверить пароль и выдать пару токенов."""
        u = self.users.get_by_login(login)
        if not u:
            return {"error": "invalid credentials"}
        h = self.creds.get_hash(u["id"])
        if not h or not verify_password(password, h):
            return {"error": "invalid credentials"}
        return self._issue(u)

    # --- логин через OIDC (identities) ---
    def login_oidc(self, provider: str, subject: str, claims: dict | None = None) -> dict:
        """Найти или создать пользователя по OIDC-идентичности и выдать токены."""
        claims = claims or {}
        uid = self.identities.get_user_id(provider, subject)
        if not uid:
            login = claims.get("email") or f"{provider}:{subject}"
            if self.users.get_by_login(login):
                login = f"{provider}:{subject}"
            uid = self.users.create(login, role="user")
            self.identities.link(uid, provider, subject)
        return self._issue(self.users.get(uid))

    # --- refresh / logout ---
    def refresh(self, refresh_token: str) -> dict:
        """Ротировать refresh-токен и выдать новую пару токенов."""
        row = self.refresh_tokens.get_active_by_hash(hash_refresh(refresh_token), _utcnow())
        if not row:
            return {"error": "invalid refresh token"}
        self.refresh_tokens.revoke(row["id"])       # ротация: старый refresh недействителен
        user = self.users.get(row["user_id"])
        if not user:
            return {"error": "invalid refresh token"}
        return self._issue(user)

    def restore(self, refresh_token: str) -> dict:
        """Восстановить сессию по refresh без ротации: тот же refresh-токен, новый access."""
        row = self.refresh_tokens.get_active_by_hash(hash_refresh(refresh_token), _utcnow())
        if not row:
            return {"error": "invalid refresh token"}
        user = self.users.get(row["user_id"])
        if not user:
            return {"error": "invalid refresh token"}
        return self._issue_access(user, refresh_token)

    def logout(self, access_token: str | None) -> dict:
        """Снять access-сессию и отозвать все refresh-токены пользователя."""
        claims = decode_access(access_token, self.cfg.secret, self.cfg.alg) if access_token else None
        if claims:
            if self.cache is not None:
                self.cache.set(f"access:{claims['jti']}", None, ttl=1)   # снять access-сессию
            self.refresh_tokens.revoke_user(claims["sub"])                # и все refresh юзера
        return {"ok": True}

    # --- проверка access (для зависимостей) ---
    def resolve_access(self, token: str | None) -> dict | None:
        """Вернуть пользователя по access-токену из allow-list или None."""
        if not self.cfg.enabled:
            return dict(ANON)
        claims = decode_access(token, self.cfg.secret, self.cfg.alg) if token else None
        if not claims or self.cache is None:
            return None
        return self.cache.get(f"access:{claims['jti']}")   # allow-list: None если отозван/истёк

    def resolve_refresh(self, refresh_token: str | None) -> dict | None:
        """Read-only резолв refresh-токена в пользователя (без ротации). Для forward-auth.

        Reverse-proxy на навигацию к панели шлёт refresh-куку; роль берём из БД, токен не трогаем.
        """
        if not self.cfg.enabled:
            return dict(ANON)
        if not refresh_token:
            return None
        row = self.refresh_tokens.get_active_by_hash(hash_refresh(refresh_token), _utcnow())
        if not row:
            return None
        user = self.users.get(row["user_id"])
        return _public(user) if user else None

    # --- админ: управление пользователями ---
    def list_users(self) -> list[dict]:
        """Вернуть список всех пользователей."""
        return self.users.list()

    def set_role(self, user_id: str, role: str) -> dict:
        """Назначить роль пользователю."""
        self.users.set_role(user_id, role)
        return {"ok": True}

    def ensure_admin(self, login: str, password: str) -> None:
        """Bootstrap первого админа (идемпотентно)."""
        if not login or not password or self.users.get_by_login(login):
            return
        uid = self.users.create(login, role="admin")
        self.creds.set(uid, hash_password(password))
