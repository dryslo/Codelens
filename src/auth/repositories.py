"""Репозитории авторизации поверх ORM (таблицы - в persistence/orm.py)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Callable

from src.persistence.orm import Credential, Identity, RefreshToken, User


class SqlUsers:
    """Репозиторий пользователей."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self.Session = session_factory

    @staticmethod
    def _d(u: User) -> dict:
        return {"id": u.id, "login": u.login, "role": u.role}

    def create(self, login: str, role: str = "user") -> str:
        """Создать пользователя и вернуть его идентификатор."""
        uid = str(uuid.uuid4())
        with self.Session() as s:
            s.add(User(id=uid, login=login, role=role))
            s.commit()
        return uid

    def get(self, user_id: str) -> dict | None:
        """Вернуть пользователя по идентификатору или None."""
        with self.Session() as s:
            u = s.get(User, user_id)
            return self._d(u) if u else None

    def get_by_login(self, login: str) -> dict | None:
        """Вернуть пользователя по логину или None."""
        with self.Session() as s:
            u = s.query(User).filter_by(login=login).first()
            return self._d(u) if u else None

    def list(self) -> list[dict]:
        """Вернуть всех пользователей в порядке создания."""
        with self.Session() as s:
            return [self._d(u) for u in s.query(User).order_by(User.created_at).all()]

    def set_role(self, user_id: str, role: str) -> None:
        """Назначить роль пользователю."""
        with self.Session() as s:
            u = s.get(User, user_id)
            if u:
                u.role = role
                s.commit()

    def count(self) -> int:
        """Вернуть число пользователей."""
        with self.Session() as s:
            return s.query(User).count()


class SqlCredentials:
    """Репозиторий парольных учётных данных."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self.Session = session_factory

    def set(self, user_id: str, password_hash: str) -> None:
        """Сохранить или обновить хэш пароля пользователя."""
        with self.Session() as s:
            c = s.get(Credential, user_id)
            if c:
                c.password_hash = password_hash
            else:
                s.add(Credential(user_id=user_id, password_hash=password_hash))
            s.commit()

    def get_hash(self, user_id: str) -> str | None:
        """Вернуть хэш пароля пользователя или None."""
        with self.Session() as s:
            c = s.get(Credential, user_id)
            return c.password_hash if c else None


class SqlIdentities:
    """Репозиторий внешних идентичностей (OIDC)."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self.Session = session_factory

    def get_user_id(self, provider: str, subject: str) -> str | None:
        """Вернуть id пользователя по паре (provider, subject) или None."""
        with self.Session() as s:
            row = s.query(Identity).filter_by(provider=provider, subject=subject).first()
            return row.user_id if row else None

    def link(self, user_id: str, provider: str, subject: str) -> str:
        """Привязать внешнюю идентичность к пользователю."""
        iid = str(uuid.uuid4())
        with self.Session() as s:
            s.add(Identity(id=iid, user_id=user_id, provider=provider, subject=subject))
            s.commit()
        return iid


class SqlRefreshTokens:
    """Репозиторий refresh-токенов (хранятся хэши)."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self.Session = session_factory

    def create(self, jti: str, user_id: str, token_hash: str, expires_at: datetime) -> None:
        """Создать запись refresh-токена."""
        with self.Session() as s:
            s.add(RefreshToken(id=jti, user_id=user_id, token_hash=token_hash,
                               expires_at=expires_at, revoked=False))
            s.commit()

    def get_active_by_hash(self, token_hash: str, now: datetime) -> dict | None:
        """Вернуть активный (не отозванный, не истёкший) токен по хэшу или None."""
        with self.Session() as s:
            row = (s.query(RefreshToken)
                   .filter_by(token_hash=token_hash, revoked=False).first())
            if not row or row.expires_at <= now:
                return None
            return {"id": row.id, "user_id": row.user_id}

    def revoke(self, jti: str) -> None:
        """Отозвать refresh-токен по идентификатору."""
        with self.Session() as s:
            row = s.get(RefreshToken, jti)
            if row:
                row.revoked = True
                s.commit()

    def revoke_user(self, user_id: str) -> None:
        """Отозвать все активные refresh-токены пользователя."""
        with self.Session() as s:
            s.query(RefreshToken).filter_by(user_id=user_id, revoked=False).update(
                {"revoked": True})
            s.commit()
