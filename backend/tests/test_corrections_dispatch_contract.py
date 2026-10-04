"""The C05 certificate validates real per-occurrence effective configurations.

These are disposable SQLite metadata/dispatch tests, not PostgreSQL load timings.
"""

# Pytest injects imported fixtures by their unaliased names.
# ruff: noqa: F811

import importlib
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import select
from test_automation_events import (  # noqa: F401 -- reuse the isolated API/data fixture
    automation_case,
    make_automation,
    settings,
)
from test_delivery_service_api import delivery_case, delivery_runtime  # noqa: F401

from trackvance.automation import dispatch_due, revision_for, save_automation
from trackvance.automation_models import DeliveryOccurrence
from trackvance.db import utcnow
from trackvance.manifests import configuration_hash
from trackvance.models import Artifact, Configuration, DatasetVersion, Run, User


@pytest.fixture
def dispatch_certificate(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/tests"))
    return importlib.import_module("corrections_dispatch")


def real_occurrence(db, case, certificate):
    now = utcnow()
    automation = make_automation(db, case, now=now)
    published = db.get(Configuration, case["config_id"])
    source = db.get(DatasetVersion, case["source_id"])
    canonical = db.get(Artifact, source.canonical_artifact_id)
    save_automation(db, db.get(User, "test-user"), published.id, "Updated automation name",
        settings(now), automation=automation, expected_version=1)
    db.commit()
    identity = {"dataset_version_id": source.id, "dataset_id": source.dataset_id,
        "source_sha256": source.sha256, "schema_hash": source.schema_hash,
        "canonical_artifact_id": canonical.id, "canonical_sha256": canonical.sha256,
        "row_count": source.row_count, "column_count": source.column_count,
        "canonical_size_bytes": canonical.size_bytes, "canonical_media_type": canonical.media_type,
        "source_run_id": source.source_run_id}
    report = {"automation_configurations": {automation.id: published.id},
        "configuration_inputs": {published.id: {"source": identity,
            "config_hash": configuration_hash(published.config),
            "destination_version_id": case["destination_version_id"]}}}
    with certificate.forbid_population_io() as calls:
        assert dispatch_due(db, now) == 1
        db.commit()
    assert not any(calls.values())
    occurrence = db.scalar(select(DeliveryOccurrence).where(DeliveryOccurrence.automation_id == automation.id))
    return occurrence, report, automation, published


def test_certificate_accepts_distinct_real_effective_id_and_parent_through_revision(
    database, automation_case, dispatch_certificate,
):
    with database() as db:
        occurrence, report, automation, published = real_occurrence(db, automation_case, dispatch_certificate)
        run, job, source, parent_id = dispatch_certificate.verify_occurrence_contract(
            db, occurrence, report, automation.organization_id)
        effective = db.get(Configuration, run.config_id)
        assert parent_id == published.id and effective.id != published.id
        assert effective.status == "AUTOMATION_EFFECTIVE"
        assert effective.previous_version_id is None  # No fabricated direct parent FK.
        assert effective.version == revision_for(db, automation).version == 2
        assert published.version == 1
        assert occurrence.automation_version_id == revision_for(db, automation).id
        assert source["dataset_version_id"] == occurrence.dataset_version_id == run.dataset_version_id
        assert job.run_id == run.id and job.lane == "DELIVERY"


@pytest.mark.parametrize("drift", [
    "published_role", "effective_role", "effective_hash", "source_identity",
    "destination_revision", "parent_revision", "actor", "responsible",
])
def test_certificate_rejects_effective_parent_source_and_authority_drift(
    database, automation_case, dispatch_certificate, drift,
):
    with database() as db:
        occurrence, report, automation, published = real_occurrence(db, automation_case, dispatch_certificate)
        run = db.get(Run, occurrence.run_id)
        effective = db.get(Configuration, run.config_id)
        revision = revision_for(db, automation)
        plan = deepcopy(run.execution_plan)
        if drift == "published_role":
            published.status = "VALIDATION_PRIVATE"
        elif drift == "effective_role":
            effective.status = "PUBLISHED"
        elif drift == "effective_hash":
            effective.config = {**effective.config, "write_strategy": "OVERWRITE"}
        elif drift == "source_identity":
            plan["source_identity"]["row_count"] += 1
        elif drift == "destination_revision":
            plan["destination_version_id"] = "unexpected-revision"
        elif drift == "parent_revision":
            revision.configuration_id = effective.id
        elif drift == "actor":
            run.initiated_by_type = "USER"
        else:
            plan["automation"]["responsible_user_id"] = "unexpected-user"
        run.execution_plan = plan
        db.flush()
        with pytest.raises(AssertionError, match="configuración efectiva"):
            dispatch_certificate.verify_occurrence_contract(db, occurrence, report, automation.organization_id)
