"""Alembic configuration shared with the application, without running its seed."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from trackvance import models  # noqa: F401: registers every table with Base
from trackvance.config import DATABASE_URL
from trackvance.db import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

# ConfigParser requires percent escaping, including URL-encoded passwords.
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied = config.attributes.get("connection")
    if supplied is not None:
        context.configure(connection=supplied, target_metadata=target_metadata,
                          compare_type=True, render_as_batch=supplied.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
