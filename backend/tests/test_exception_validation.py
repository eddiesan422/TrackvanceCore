from datetime import timedelta

import pytest
from sqlalchemy import select

from trackvance.db import utcnow
from trackvance.models import (
    AuditEvent,
    Configuration,
    Dataset,
    DatasetVersion,
    ExceptionCase,
    Finding,
    Run,
    User,
)
from trackvance.services import (
    assess_exception_validation,
    create_exception,
    exception_dto,
    record_exception_validation,
)


@pytest.mark.parametrize("role", ["Data Analyst", "Operations", "Data Owner", "Administrator"])
@pytest.mark.parametrize("state", ["RESOLVED", "DISCARDED", "ACCEPTED", "NOT_APPLICABLE"])
def test_exception_closure_requires_close_permission(authenticated, database, role, state):
    with database() as db:
        case, *_ = make_case(db, "sentinel", candidate_decision="HEALTHY")
        db.get(User, "test-user").role = role
        db.commit()
        case_id = case.id
    url = f"/api/v1/exceptions/{case_id}"
    managed = authenticated.patch(url, json={"version": 1, "comment": "En gestión"})
    assert managed.status_code == 200
    response = authenticated.patch(url, json={
        "version": 2, "state": state, "root_cause": "Fuente corregida",
        "resolution": "Validación saludable", "administrative_reason": "Excepción aceptada por negocio",
    })
    allowed = role in {"Data Owner", "Administrator"}
    assert response.status_code == (200 if allowed else 403), response.text
    if not allowed:
        assert response.json()["error"]["code"] == "FORBIDDEN"
        current = authenticated.get(url).json()
        assert current["state"] == "PENDING_VALIDATION"
        assert current["version"] == 2


def make_case(
    db,
    module: str,
    *,
    candidate_decision: str,
    repeated_finding: bool = False,
    candidate_config: Configuration | None = None,
):
    now = utcnow()
    dataset = Dataset(name=f"Dataset {module} {now.timestamp()}")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(
        dataset_id=dataset.id,
        version=1,
        filename="data.parquet",
        source_type="UPLOAD",
        sha256="a" * 64,
        schema_hash="b" * 64,
        size_bytes=1,
        row_count=1,
        column_count=1,
        original_path="",
        canonical_path="unused.parquet",
        schema_json=[],
        profile={},
        ingestion_metadata={},
    )
    config = Configuration(
        name=f"Control {module}",
        module=module,
        dataset_id=dataset.id,
        config={},
    )
    db.add_all([version, config])
    db.flush()
    origin = Run(
        module=module,
        name=config.name,
        status="SUCCESS",
        decision={"intake": "REJECTED", "recon": "WITH_FINDINGS", "sentinel": "ALERT"}[module],
        config_id=config.id,
        dataset_version_id=version.id,
        initiated_by="Test User",
        created_at=now - timedelta(minutes=2),
        finished_at=now - timedelta(minutes=2),
    )
    db.add(origin)
    db.flush()
    code = {"intake": "REQUIRED:customer_id", "recon": "VALUE_MISMATCH", "sentinel": "NULL_RATE"}[module]
    finding = Finding(
        run_id=origin.id,
        title=f"Hallazgo {module}",
        code=code,
        severity="HIGH",
        fingerprint=f"fingerprint-{module}",
        details={"code": code},
    )
    db.add(finding)
    db.flush()
    case = create_exception(db, finding, "Test User")
    case.state = "PENDING_VALIDATION"
    validation_config = candidate_config or config
    candidate = Run(
        module=module,
        name=validation_config.name,
        status="SUCCESS",
        decision=candidate_decision,
        config_id=validation_config.id,
        dataset_version_id=version.id,
        initiated_by="Test User",
        created_at=now - timedelta(minutes=1),
        finished_at=now - timedelta(minutes=1),
        metrics={"rules": [{"code": "REQUIRED", "column": "customer_id", "evaluated_count": 1,
                            "failed_count": 1 if repeated_finding else 0}]} if module == "intake" else {},
    )
    db.add(candidate)
    db.flush()
    if repeated_finding:
        db.add(Finding(
            run_id=candidate.id,
            title=f"Hallazgo repetido {module}",
            code=code,
            severity="HIGH",
            fingerprint=finding.fingerprint,
            details={"code": code},
        ))
        db.flush()
    return case, config, candidate, finding, version


@pytest.mark.parametrize(
    ("module", "decision", "repeated", "expected", "criterion"),
    [
        ("intake", "APPROVED", False, True, "INTAKE_RULE_NO_LONGER_FAILS"),
        ("intake", "REJECTED", True, False, "INTAKE_RULE_NO_LONGER_FAILS"),
        ("recon", "WITH_FINDINGS", False, True, "RECON_FINDING_NO_LONGER_PRESENT"),
        ("recon", "WITH_FINDINGS", True, False, "RECON_FINDING_NO_LONGER_PRESENT"),
        ("recon", "CONFORME", True, True, "RECON_FINDING_NO_LONGER_PRESENT"),
        ("sentinel", "HEALTHY", False, True, "SENTINEL_MONITOR_HEALTHY"),
        ("sentinel", "ALERT", False, False, "SENTINEL_MONITOR_HEALTHY"),
    ],
)
def test_module_specific_technical_validation(
    database,
    module,
    decision,
    repeated,
    expected,
    criterion,
):
    with database() as db:
        case, _config, candidate, _finding, _version = make_case(
            db,
            module,
            candidate_decision=decision,
            repeated_finding=repeated,
        )
        assessment = assess_exception_validation(db, case)
        assert assessment["eligible"] is True
        assert assessment["validated"] is expected
        assert assessment["can_resolve"] is expected
        assert assessment["candidate_run_id"] == candidate.id
        assert assessment["evidence"]["criterion"] == criterion
        assert assessment["validation_run_id"] == (candidate.id if expected else None)


def test_exact_configuration_is_required_for_validation(database):
    with database() as db:
        case, config, candidate, _finding, _version = make_case(
            db,
            "intake",
            candidate_decision="APPROVED",
        )
        newer_config = Configuration(
            name=config.name,
            module="intake",
            dataset_id=config.dataset_id,
            config={},
            previous_version_id=config.id,
            version=2,
        )
        db.add(newer_config)
        db.flush()
        candidate.config_id = newer_config.id
        db.flush()
        assessment = assess_exception_validation(db, case)
        assert assessment["status"] == "NO_LATER_RUN"
        assert assessment["can_resolve"] is False


def test_newest_successful_run_invalidates_stale_positive_evidence(database):
    with database() as db:
        case, _config, passing, origin_finding, version = make_case(
            db,
            "intake",
            candidate_decision="APPROVED",
        )
        first = assess_exception_validation(db, case)
        record_exception_validation(db, case, first, "Test User")
        assert case.validation_run_id == passing.id

        regression = Run(
            module="intake",
            name="Control intake",
            status="SUCCESS",
            decision="REJECTED",
            config_id=case.configuration_id,
            dataset_version_id=version.id,
            initiated_by="Test User",
            created_at=passing.created_at + timedelta(seconds=1),
            finished_at=passing.finished_at + timedelta(seconds=1),
        )
        db.add(regression)
        db.flush()
        db.add(Finding(
            run_id=regression.id,
            title="La regla volvió a fallar",
            code=origin_finding.code,
            severity="HIGH",
            fingerprint=origin_finding.fingerprint,
            details={},
        ))
        db.flush()

        current = assess_exception_validation(db, case)
        assert current["status"] == "FAILED"
        assert current["candidate_run_id"] == regression.id
        assert current["can_resolve"] is False
        assert case.validation_run_id == passing.id  # retained until an explicit/worker check


def test_validation_endpoint_records_failed_and_successful_attempts(authenticated, database):
    with database() as db:
        case, _config, failing, _finding, version = make_case(
            db,
            "recon",
            candidate_decision="WITH_FINDINGS",
            repeated_finding=True,
        )
        db.commit()
        case_id = case.id
        failing_id = failing.id

    url = f"/api/v1/exceptions/{case_id}"
    failed = authenticated.post(f"{url}/validate", json={"version": 1})
    assert failed.status_code == 200, failed.text
    failed_case = failed.json()
    assert failed_case["state"] == "PENDING_VALIDATION"
    assert failed_case["version"] == 2
    assert failed_case["technical_validation"]["status"] == "FAILED"
    assert failed_case["validation_run_id"] is None
    assert failed_case["validation_evidence"]["validation_run_id"] == failing_id
    blocked = authenticated.patch(url, json={
        "version": 2,
        "state": "RESOLVED",
        "root_cause": "Desalineación de importes",
        "resolution": "Se intentó corregir la fuente",
    })
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "TECHNICAL_VALIDATION_REQUIRED"

    with database() as db:
        case = db.get(ExceptionCase, case_id)
        latest = db.get(Run, failing_id)
        passing = Run(
            module="recon",
            name=latest.name,
            status="SUCCESS",
            decision="CONFORME",
            config_id=case.configuration_id,
            dataset_version_id=version.id,
            initiated_by="Test User",
            created_at=latest.created_at + timedelta(seconds=1),
            finished_at=latest.finished_at + timedelta(seconds=1),
        )
        db.add(passing)
        db.commit()
        passing_id = passing.id

    validated = authenticated.post(f"{url}/validate", json={"version": 2})
    assert validated.status_code == 200, validated.text
    validated_case = validated.json()
    assert validated_case["version"] == 3
    assert validated_case["technical_validation"]["status"] == "VALIDATED"
    assert validated_case["validation_run_id"] == passing_id
    repeated_check = authenticated.post(f"{url}/validate", json={"version": 3})
    assert repeated_check.status_code == 200
    assert repeated_check.json()["version"] == 3
    assert (
        repeated_check.json()["technical_validation"]["evidence"]["checked_at"]
        == validated_case["technical_validation"]["evidence"]["checked_at"]
    )
    assert (
        repeated_check.json()["technical_validation"]["validated_at"]
        == validated_case["technical_validation"]["validated_at"]
    )
    resolved = authenticated.patch(url, json={
        "version": 3,
        "state": "RESOLVED",
        "root_cause": "Desalineación de importes",
        "resolution": "La fuente fue corregida y conciliada nuevamente",
    })
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["state"] == "RESOLVED"
    assert resolved.json()["origin_run_id"] != resolved.json()["validation_run_id"]
    with database() as db:
        events = db.scalars(select(AuditEvent).where(
            AuditEvent.subject_id == case_id,
            AuditEvent.event_type == "EXCEPTION_VALIDATION_CHECKED",
        )).all()
        assert [event.metadata_json["status"] for event in events] == ["FAILED", "VALIDATED"]


@pytest.mark.parametrize("state", ["DISCARDED", "ACCEPTED", "NOT_APPLICABLE"])
def test_administrative_closure_requires_distinct_reason(authenticated, database, state):
    with database() as db:
        case, _config, _candidate, _finding, _version = make_case(
            db,
            "sentinel",
            candidate_decision="ALERT",
        )
        case.state = "OPEN"
        db.commit()
        case_id = case.id
    url = f"/api/v1/exceptions/{case_id}"
    rejected = authenticated.patch(url, json={"version": 1, "state": state})
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "ADMINISTRATIVE_REASON_REQUIRED"
    closed = authenticated.patch(url, json={
        "version": 1,
        "state": state,
        "administrative_reason": "Decisión aprobada por el responsable del dato",
    })
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert body["state"] == state
    assert body["administrative_reason"]
    assert body["technical_validation"]["status"] == "NOT_REQUESTED"
    assert body["validation_run_id"] is None


def test_legacy_resolved_case_is_visible_as_unverified_history(database):
    with database() as db:
        case, _config, _candidate, _finding, _version = make_case(
            db,
            "sentinel",
            candidate_decision="HEALTHY",
        )
        case.state = "RESOLVED"
        case.validation_evidence = {}
        case.validation_run_id = None
        dto = exception_dto(db, case)
        assert dto["state"] == "RESOLVED"
        assert dto["technical_validation"]["status"] == "LEGACY_UNVERIFIED"
        assert dto["technical_validation"]["validated"] is False
        assert dto["validation_run_id"] is None
