import json

import pytest
from sqlalchemy import select

from trackvance import connections_api
from trackvance.dataset_sources import SourceError
from trackvance.models import AuditEvent


@pytest.mark.parametrize("fails", [False, True])
def test_draft_connection_test_audits_outcome_without_credentials(authenticated, database, monkeypatch, fails):
    class Source:
        def test(self):
            if fails:
                raise SourceError("SOURCE_AUTH_FAILED", "No se pudo autenticar.")

    monkeypatch.setattr(connections_api.source_registry, "create", lambda _settings: Source())
    response = authenticated.post("/api/v1/connections/test", json={
        "name": "Draft", "source_type": "POSTGRESQL", "host": "database.local", "port": 5432,
        "database": "warehouse", "username": "readonly", "password": "never-audit-this-password",
    })
    assert response.status_code == (422 if fails else 200), response.text
    with database() as db:
        event = db.scalar(select(AuditEvent).where(AuditEvent.event_type == "CONNECTION_TESTED"))
        assert event is not None
        assert event.actor_id == "test-user"
        assert event.actor_type == "USER"
        assert event.request_id
        assert event.subject_type == "connection_test"
        assert event.metadata_json["status"] == ("FAILED" if fails else "SUCCESS")
        assert event.metadata_json["draft"] is True
        assert "password" not in json.dumps(event.metadata_json)
        assert "never-audit-this-password" not in response.text
