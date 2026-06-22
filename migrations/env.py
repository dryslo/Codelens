import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from src.persistence.orm import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

# DSN из переменной окружения (тот же, что у приложения)
config.set_main_option("sqlalchemy.url",
                       os.environ.get("DATABASE_DSN", "sqlite:///codelens.db"))
target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=config.get_main_option("sqlalchemy.url"),
                      target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
