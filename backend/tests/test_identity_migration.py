import os
import subprocess
import sys
from pathlib import Path


def test_identity_upgrade_preserves_populated_legacy_users_and_sessions(tmp_path):
    environment = {**os.environ, "DATABASE_URL": f"sqlite:///{(tmp_path / 'identity.db').as_posix()}",
                   "TRACKVANCE_STORAGE_DIR": str(tmp_path / "storage"), "DEMO_SEED_ENABLED": "false"}
    script = r'''
from datetime import UTC, datetime, timedelta
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, select, inspect
from trackvance.db import engine

config = Config("alembic.ini")
command.upgrade(config, "0009_delivery_reviews")
metadata = MetaData()
metadata.reflect(bind=engine)
now = datetime.now(UTC)
with engine.begin() as connection:
    for uid, email, role in [("admin", "admin@example.test", "Administrator"), ("legacy-a", "bob@alpha.test", "Data Owner"), ("legacy-b", "bob@beta.test", "Operations")]:
        connection.execute(metadata.tables["users"].insert().values(id=uid, organization_id="legacy", created_at=now,
            name="Legacy unsplit full name", email=email, role=role, password_hash="unchanged-argon2-hash", active=True,
            version=7, updated_at=now, password_changed_at=now))
    connection.execute(metadata.tables["sessions"].insert().values(id="legacy-session", organization_id="legacy",
        created_at=now, user_id="admin", token_hash="a" * 64, csrf_token="historical-csrf", expires_at=now+timedelta(hours=1)))
from trackvance.migrate import migrate
migrate()  # Exercise the application's foreign-key-enforcing supplied SQLite connection.
upgraded = MetaData()
upgraded.reflect(bind=engine)
with engine.connect() as connection:
    users = connection.execute(select(upgraded.tables["users"]).order_by(upgraded.tables["users"].c.email)).mappings().all()
    assert [row["username"] for row in users] == ["admin", "bob", "bob-2"]
    for user in users:
        assert user["name"] == "Legacy unsplit full name"
        assert user["first_name"] is None and user["last_name"] is None and user["last_login_at"] is None
        assert user["password_hash"] == "unchanged-argon2-hash" and user["version"] == 7
        assert not user["must_change_password"] and not user["deleted"]
        role = connection.execute(select(upgraded.tables["roles"]).where(upgraded.tables["roles"].c.id == user["role_id"])).mappings().one()
        if user["id"] == "legacy-a":
            assert user["role"] == "Data Owner" and role["name"] == "Data Owner / Lead"
    session = connection.execute(select(upgraded.tables["sessions"])).mappings().one()
    assert session["authentication_method"] == "LOCAL" and session["token_hash"] == "a" * 64
command.downgrade(config, "0009_delivery_reviews")
assert "roles" not in inspect(engine).get_table_names()
command.upgrade(config, "head")
engine.dispose()
'''
    result = subprocess.run([sys.executable, "-c", script], env=environment,
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
