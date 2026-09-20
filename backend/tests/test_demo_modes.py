import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from trackvance import api, seed
from trackvance.config import DEMO_USER_ID
from trackvance.models import (
    Configuration,
    Dataset,
    DatasetVersion,
    ExceptionCase,
    Finding,
    Job,
    Run,
    User,
)

SYNTHETIC_MODELS = (
    Dataset,
    DatasetVersion,
    Configuration,
    Run,
    Job,
    Finding,
    ExceptionCase,
)


@pytest.mark.parametrize(
    ("access", "seed_enabled", "expected"),
    [
        (None, None, [True, True]),
        (None, "false", [False, False]),
        ("true", "false", [True, False]),
        ("false", "true", [False, True]),
    ],
)
def test_demo_flag_configuration_and_legacy_fallback(access, seed_enabled, expected):
    environment = os.environ.copy()
    for name, value in {
        "DEMO_ACCESS_ENABLED": access,
        "DEMO_SEED_ENABLED": seed_enabled,
    }.items():
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = value
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from trackvance.config import DEMO_ACCESS_ENABLED, "
                "DEMO_SEED_ENABLED; "
                "print(json.dumps([DEMO_ACCESS_ENABLED, DEMO_SEED_ENABLED]))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert json.loads(result.stdout) == expected


def row_count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_demo_user_provisioning_is_idempotent_and_does_not_seed_data(database):
    seed.ensure_demo_user()
    seed.ensure_demo_user()

    with database() as db:
        assert db.get(User, DEMO_USER_ID) is not None
        assert row_count(db, User) == 2  # Local test account plus the demo identity.
        assert all(row_count(db, model) == 0 for model in SYNTHETIC_MODELS)


def test_disabled_demo_seed_is_a_noop(database):
    seed.seed_demo()

    with database() as db:
        assert db.get(User, DEMO_USER_ID) is None
        assert all(row_count(db, model) == 0 for model in SYNTHETIC_MODELS)


def test_demo_access_without_seed_logs_in_to_an_empty_workspace(database, monkeypatch):
    monkeypatch.setattr(api, "DEMO_ACCESS_ENABLED", True)
    monkeypatch.setattr(api, "DEMO_SEED_ENABLED", False)

    with TestClient(api.app) as client:
        health = client.get("/api/v1/health").json()
        assert health["demo_enabled"] is True
        assert health["demo_access_enabled"] is True
        assert health["demo_seed_enabled"] is False

        login = client.post("/api/v1/auth/demo")
        assert login.status_code == 200
        assert login.json()["user"]["id"] == DEMO_USER_ID
        assert login.json()["demo_mode"] is True

        for path in (
            "/api/v1/datasets",
            "/api/v1/intake/contracts",
            "/api/v1/recon/controls",
            "/api/v1/monitors",
            "/api/v1/runs",
            "/api/v1/findings",
            "/api/v1/exceptions",
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert response.json() == {"items": [], "total": 0}

    with database() as db:
        assert db.get(User, DEMO_USER_ID) is not None
        assert all(row_count(db, model) == 0 for model in SYNTHETIC_MODELS)


def test_demo_access_disabled_rejects_login_even_when_demo_content_exists(
    client, database,
):
    seed.ensure_demo_user()
    with database() as db:
        db.add(Dataset(id="demo-content", name="Persisted demo content"))
        db.commit()

    response = client.post("/api/v1/auth/demo")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DEMO_DISABLED"
    health = client.get("/api/v1/health").json()
    assert health["demo_enabled"] is False
    assert health["demo_access_enabled"] is False
    with database() as db:
        assert db.get(Dataset, "demo-content") is not None


def test_demo_seed_builds_synthetic_content_independently_from_access(database, monkeypatch):
    monkeypatch.setattr(seed, "DEMO_SEED_ENABLED", True)
    monkeypatch.setattr(api, "DEMO_SEED_ENABLED", True)

    seed.seed_demo()

    with TestClient(api.app) as client:
        assert client.post("/api/v1/auth/demo").status_code == 404
        health = client.get("/api/v1/health").json()
        assert health["demo_access_enabled"] is False
        assert health["demo_seed_enabled"] is True

    with database() as db:
        assert db.get(User, DEMO_USER_ID) is None
        for model in (Dataset, DatasetVersion, Configuration, Run, Job, Finding, ExceptionCase):
            assert row_count(db, model) > 0


def test_demo_access_and_seed_together_preserve_the_existing_flow(database, monkeypatch):
    monkeypatch.setattr(api, "DEMO_ACCESS_ENABLED", True)
    monkeypatch.setattr(api, "DEMO_SEED_ENABLED", True)
    monkeypatch.setattr(seed, "DEMO_SEED_ENABLED", True)

    with TestClient(api.app) as client:
        login = client.post("/api/v1/auth/demo")
        assert login.status_code == 200
        assert login.json()["user"]["id"] == DEMO_USER_ID

    with database() as db:
        assert db.get(User, DEMO_USER_ID) is not None
        assert all(row_count(db, model) > 0 for model in SYNTHETIC_MODELS)
