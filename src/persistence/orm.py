"""ORM-модели SQLAlchemy: пользователи, аутентификация, чаты, индекс."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Базовый декларативный класс ORM."""

    pass


class User(Base):
    """Пользователь системы."""

    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    login: Mapped[str] = mapped_column(String, unique=True)
    role: Mapped[str] = mapped_column(String, default="user")          # admin | user
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Credential(Base):
    """Локальный пароль (argon2). Отдельно от users - у OIDC-пользователя его может не быть."""

    __tablename__ = "credentials"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String)


class Identity(Base):
    """Привязка к внешнему OIDC-провайдеру (google, keycloak, ...)."""

    __tablename__ = "identities"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String)                       # "sub" из OIDC
    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_identity_provider_subject"),)


class RefreshToken(Base):
    """Refresh-токены живут в БД (в отличие от access - тот в кэше). Храним только хэш."""

    __tablename__ = "refresh_tokens"
    id: Mapped[str] = mapped_column(String, primary_key=True)          # jti
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Chat(Base):
    """Чат пользователя."""

    __tablename__ = "chats"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    messages: Mapped[list["Message"]] = relationship(back_populates="chat")


class Message(Base):
    """Сообщение в чате."""

    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id"), index=True)
    role: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    retrieved_ids: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String)
    mode: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    chat: Mapped["Chat"] = relationship(back_populates="messages")


class IndexedFile(Base):
    """Проиндексированный файл источника с хэшем содержимого."""

    __tablename__ = "indexed_files"
    source: Mapped[str] = mapped_column(String, primary_key=True)
    file: Mapped[str] = mapped_column(String, primary_key=True)
    hash: Mapped[str] = mapped_column(String)
