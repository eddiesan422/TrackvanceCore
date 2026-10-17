"""The disposable fixture resets migration state together with current models.

These two tests intentionally run in order: a real historical migration is
followed by normal API startup, matching the previously failing suite sequence.
"""

from alembic import command
from sqlalchemy import MetaData, Table, inspect, text

from trackvance.db import Base, engine
from trackvance.migrate import alembic_config


def test_historical_migration_leaves_its_actual_revision(database):
    # Build through actual migrations, including their named constraints, instead
    # of treating a create_all fixture as a historical migration artifact.
    Base.metadata.drop_all(engine)
    Table("alembic_version", MetaData()).drop(engine, checkfirst=True)
    with engine.connect() as connection:
        config = alembic_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.commit()
        command.downgrade(config, "0022_intake_automation_deletion")
        connection.commit()
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0022_intake_automation_deletion"
        assert len(set(inspect(connection).get_table_names()) - {"alembic_version"}) == 65


def test_current_client_after_historical_migration_starts_with_current_schema(client):
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0025_comparison_095"
        assert set(inspect(connection).get_table_names()) - {"alembic_version"} == set(Base.metadata.tables)
        assert len(Base.metadata.tables) == 77
    assert client.get("/api/v1/health").status_code == 200
