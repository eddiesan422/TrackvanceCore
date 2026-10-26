"""Durable originals, source fencing and replay integrity without model calls."""
import base64
import copy
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from trackvance.artifactstore import ArtifactIntegrityError, storage_provider
from trackvance.document_models import (
    DocumentOriginal,  # noqa: F401 -- Register document metadata for the disposable DB.
)
from trackvance.document_recognition_store import (
    canonical,
    latest_recognition_events,
    persist_recognition_event,
    recognition_artifact,
    recognition_metadata,
)
from trackvance.models import Artifact, User
from trackvance.operations_common import OperationError

DOCUMENT_ID = "fixture"


@pytest.fixture(autouse=True)
def distinct_immutable_namespace(monkeypatch):
    # The DB fixture resets rows while artifact bytes intentionally survive;
    # different test documents must never reuse an immutable artifact identity.
    monkeypatch.setattr(__import__(__name__), "DOCUMENT_ID", uuid4().hex)


def event(kind="PAGE", sequence=1):
    raw = b'{"page_index":0,"literal":"A|B\\nC","empty":""}'
    value = {"version": 1, "kind": kind, "sequence": sequence, "request_id": "request-1",
        "attempt_id": "job-1", "source_sha256": "a" * 64, "configuration_sha256": "b" * 64,
        "stage": "PADDLE_STRUCTURED", "page_one_based": 1 if kind == "PAGE" else None,
        "expected_pages": 2, "payload": {"artifacts": [{"kind": "PADDLE_PAGE_JSON",
            "media_type": "application/json", "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw), "content_b64": base64.b64encode(raw).decode()}] if kind == "PAGE" else []}}
    if kind == "TERMINAL":
        value.update(status="PARTIAL", resources_released=True)
    value["event_sha256"] = hashlib.sha256(canonical(value)).hexdigest()
    return value


def subject(db):
    user = db.get(User, "test-user")
    return SimpleNamespace(id=DOCUMENT_ID, organization_id=user.organization_id,
        sha256="a" * 64, status="CANCELLED", extraction_id=None)


def persist(db, original, value):
    return persist_recognition_event(db, original, value, attempt_id="job-1",
        configuration_sha256="b" * 64, job_id="job", worker_attempt_number=1)


def test_emitted_original_survives_process_boundary_and_roundtrips_exactly(database):
    value = event()
    with database() as db:
        original = subject(db)
        refs = persist(db, original, value)
        db.commit()
    with database() as db:
        original = subject(db)
        restored = latest_recognition_events(db, original, hydrate=True)
        assert len(restored) == 1
        assert {k: v for k, v in restored[0].items() if k != "durable_refs"} == value
        raw = recognition_artifact(db, original, refs["artifacts"][0]["artifact_id"])
        assert storage_provider.materialize(raw).read_bytes() == base64.b64decode(value["payload"]["artifacts"][0]["content_b64"])
        execution, metadata = recognition_metadata(db, original)
        assert execution["expected_pages"] == [1, 2] and execution["emitted_pages"] == [1]
        assert execution["pending_pages"] == [2]
        assert not execution["recognition_complete"] and not execution["resources_released"]
        assert not execution["publication_complete"] and metadata[0]["artifact_id"] == raw.id


def test_duplicate_event_is_idempotent_and_partial_terminal_is_not_complete(database):
    with database() as db:
        original = subject(db)
        first = persist(db, original, event())
        db.commit()
        second = persist(db, original, event())
        db.commit()
        assert first == second
        assert db.scalar(select(func.count()).select_from(Artifact)) == 2
        persist(db, original, event("TERMINAL", 2))
        db.commit()
        execution, _ = recognition_metadata(db, original)
        assert execution["resources_released"] is True
        assert execution["recognition_complete"] is False


def test_empty_terminal_payload_is_not_changed_while_journaling(database):
    value = event("TERMINAL", 1)
    value["payload"] = {}
    value.pop("event_sha256")
    value["event_sha256"] = hashlib.sha256(canonical(value)).hexdigest()
    with database() as db:
        original = subject(db)
        persist(db, original, value)
        db.commit()
        restored = latest_recognition_events(db, original, hydrate=True)[0]
        assert restored["payload"] == {}
        assert {key: item for key, item in restored.items() if key != "durable_refs"} == value


@pytest.mark.parametrize("mutation", ["source", "attempt", "configuration", "bytes", "hash", "self_hash", "authority", "media"])
def test_unbound_or_forged_original_never_registers_an_artifact(database, mutation):
    value = event()
    if mutation in {"source", "configuration"}:
        value[mutation + "_sha256"] = "c" * 64
    elif mutation == "attempt":
        value["attempt_id"] = "other-attempt"
    elif mutation == "self_hash":
        value["event_sha256"] = "c" * 64
    elif mutation == "authority":
        value["durable_refs"] = {"artifact_id": "forged"}
    else:
        item = value["payload"]["artifacts"][0]
        if mutation == "bytes":
            item["bytes"] += 1
        elif mutation == "hash":
            item["sha256"] = "c" * 64
        else:
            item["media_type"] = "text/html\r\nX-Forged: yes"
    with database() as db:
        with pytest.raises(OperationError):
            persist(db, subject(db), value)
        assert db.scalar(select(func.count()).select_from(Artifact)) == 0


def test_raw_tampering_and_foreign_original_are_not_replayable(database):
    with database() as db:
        original = subject(db)
        refs = persist(db, original, event())
        db.commit()
        foreign = copy.copy(original)
        foreign.id = "another-original"
        with pytest.raises(OperationError):
            recognition_artifact(db, foreign, refs["artifacts"][0]["artifact_id"])
        artifact = db.get(Artifact, refs["artifacts"][0]["artifact_id"])
        path = storage_provider.materialize(artifact)
        path.chmod(0o600)
        path.write_bytes(b"changed")
        with pytest.raises(ArtifactIntegrityError):
            latest_recognition_events(db, original, hydrate=True)
