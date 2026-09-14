"""Every test uses a disposable database and storage, never the local demo runtime."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

_sandbox = TemporaryDirectory(prefix="trackvance-tests-")
TEST_ROOT = Path(_sandbox.name).resolve()
# Configure before any application import creates the engine or storage directory.
os.environ["DATABASE_URL"] = f"sqlite:///{(TEST_ROOT / 'test.db').as_posix()}"
os.environ["TRACKVANCE_STORAGE_DIR"] = str(TEST_ROOT / "storage")
os.environ["TRACKVANCE_STORAGE_ROOT"] = str(TEST_ROOT / "storage")
os.environ["DEMO_SEED_ENABLED"] = "false"


@pytest.fixture
def database():
    from trackvance.db import Base, SessionLocal, engine
    from trackvance.models import User

    assert Path(engine.url.database).resolve().is_relative_to(TEST_ROOT)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(User(id="test-user", name="Test User", email="tester@example.test",
                    password_hash=PasswordHasher(time_cost=1, memory_cost=8192).hash("test-password")))
        db.commit()
    yield SessionLocal
    engine.dispose()


@pytest.fixture
def client(database):
    from trackvance.api import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def authenticated(client):
    response = client.post("/api/v1/auth/login", json={
        "email": "tester@example.test", "password": "test-password"})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client


@pytest.fixture
def queued_intake(database, tmp_path):
    from trackvance.models import Configuration, Dataset
    from trackvance.services import create_version, enqueue

    source = tmp_path / "orders.csv"
    source.write_text("order_id,amount\nA,12.25\nB,-1\nC,invalid\n", encoding="utf-8")
    with database() as db:
        dataset = Dataset(name="Test orders")
        db.add(dataset)
        db.flush()
        version = create_version(db, dataset, source, source.name)
        config = Configuration(name="Positive amounts", module="intake", dataset_id=dataset.id,
                               config={"required_columns": ["order_id"],
                                       "positive_columns": ["amount"], "max_error_rate": 0})
        db.add(config)
        db.flush()
        run = enqueue(db, config, version, None, "Test User")
        db.commit()
        return {"run_id": run.id, "config_id": config.id,
                "version_id": version.id, "original_path": version.original_path}


def pytest_sessionfinish(session, exitstatus):
    from trackvance.db import engine

    engine.dispose()
    _sandbox.cleanup()
