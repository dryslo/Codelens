"""Подключение к БД: фабрика сессий и инициализация схемы."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.persistence.orm import Base


def make_session_factory(dsn: str) -> sessionmaker:
    """Создать фабрику сессий SQLAlchemy по DSN."""
    engine = create_engine(dsn, future=True)
    return sessionmaker(engine, expire_on_commit=False)


def init_db(dsn: str) -> None:
    """Dev-удобство: создать таблицы без Alembic (в проде - миграции)."""
    engine = create_engine(dsn, future=True)
    Base.metadata.create_all(engine)
