"""A new coordinator attempt references original events without rewriting them."""
import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_document_recognition_store_098 import event

from trackvance.document_recognition_store import (
    canonical,
    latest_recognition_events,
    persist_recognition_event,
)
from trackvance.models import User
from trackvance.operations_common import OperationError


@pytest.mark.parametrize("wrong_configuration", [False, True])
def test_recovery_journal_links_same_subject_and_binding_only(database, wrong_configuration):
    import hashlib
    with database() as db:
        user = db.get(User, "test-user")
        original = SimpleNamespace(id=uuid4().hex, organization_id=user.organization_id, sha256="a" * 64)
        first = event()
        persist_recognition_event(db, original, first, attempt_id="job-1", configuration_sha256="b" * 64,
            job_id="job", worker_attempt_number=1)
        db.commit()
        link = copy.deepcopy(first)
        link.update(kind="PHASE", sequence=0, request_id="reuse-request", attempt_id="job-2",
            stage="RECOVERY_REUSE", page_one_based=None,
            configuration_sha256=("c" if wrong_configuration else "b") * 64,
            payload={"reused_event_hashes": [first["event_sha256"]], "reused_attempt_ids": ["job-1"]})
        link["event_sha256"] = hashlib.sha256(canonical({k:v for k,v in link.items() if k != "event_sha256"})).hexdigest()
        persist_recognition_event(db, original, link, attempt_id="job-2", configuration_sha256=link["configuration_sha256"],
            job_id="job", worker_attempt_number=2)
        db.commit()
        if wrong_configuration:
            with pytest.raises(OperationError):
                latest_recognition_events(db, original, hydrate=True)
        else:
            loaded = latest_recognition_events(db, original, hydrate=True)
            assert len(loaded) == 2
            assert {key:value for key,value in loaded[0].items() if key != "durable_refs"} == first
            assert loaded[0]["event_sha256"] != loaded[1]["event_sha256"]
            assert loaded[0]["attempt_id"] == "job-1" and loaded[1]["attempt_id"] == "job-2"
