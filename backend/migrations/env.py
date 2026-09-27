"""Alembic configuration shared with the application, without running its seed."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from trackvance import models  # noqa: F401 - registers every table with Base
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


def migrate_connection(connection) -> None:
    if connection.dialect.name != "sqlite":
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    # Alembic's SQLite batch rebuild temporarily drops referenced tables. Disable
    # enforcement only on this migration connection, and verify the complete graph
    # before an explicit atomic DDL transaction commits. Application connections
    # keep their normal foreign_keys=ON policy.
    enforcement = int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar())
    connection.commit()
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    connection.commit()
    try:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        context.configure(connection=connection, target_metadata=target_metadata,
                          compare_type=True, render_as_batch=True, transactional_ddl=True)
        with context.begin_transaction():
            context.run_migrations()
        if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise RuntimeError("La migración dejaría referencias SQLite inválidas.")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.exec_driver_sql(f"PRAGMA foreign_keys={enforcement}")
        connection.commit()


def run_migrations_online() -> None:
    supplied = config.attributes.get("connection")
    if supplied is not None:
        migrate_connection(supplied)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        migrate_connection(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
