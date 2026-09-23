from datetime import timedelta

from trackvance.dashboard import run_health_score, run_operational_status
from trackvance.db import utcnow
from trackvance.models import Configuration, Dataset, DatasetVersion, ExceptionCase, Finding, Run


def dataset_with_run(db, *, identifier, name, criticality, module, decision, metrics, created_at):
    dataset = Dataset(id=identifier, name=name, criticality=criticality, domain="Finanzas")
    version = DatasetVersion(
        id=f"{identifier}-v1",
        dataset_id=dataset.id,
        version=1,
        filename=f"{identifier}.csv",
        source_type="UPLOAD",
        sha256="a" * 64,
        schema_hash="b" * 64,
        size_bytes=100,
        row_count=100,
        column_count=2,
        original_path=f"/{identifier}.csv",
        canonical_path=f"/{identifier}.parquet",
        schema_json=[],
        profile={},
        created_at=created_at,
    )
    config = Configuration(
        id=f"{identifier}-{module}",
        name=f"Control {name}",
        module=module,
        dataset_id=dataset.id,
        config={},
    )
    run = Run(
        id=f"run-{identifier}-{int(created_at.timestamp())}",
        module=module,
        name=config.name,
        status="SUCCESS",
        decision=decision,
        config_id=config.id,
        dataset_version_id=version.id,
        initiated_by="Test User",
        metrics=metrics,
        created_at=created_at,
        started_at=created_at,
        finished_at=created_at + timedelta(seconds=65),
    )
    db.add(dataset)
    db.flush()
    db.add_all([version, config])
    db.flush()
    db.add(run)
    db.flush()
    return dataset, run


def test_dashboard_prioritizes_real_issues_and_exposes_operational_metrics(authenticated, database):
    now = utcnow()
    with database() as db:
        critical, intake = dataset_with_run(
            db,
            identifier="critical-orders",
            name="Pedidos críticos",
            criticality="CRITICAL",
            module="intake",
            decision="REJECTED",
            metrics={"total_rows": 100, "valid_rows": 80, "acceptance_rate": 80},
            created_at=now - timedelta(days=1),
        )
        _, sentinel = dataset_with_run(
            db,
            identifier="healthy-daily",
            name="Seguimiento saludable",
            criticality="LOW",
            module="sentinel",
            decision="HEALTHY",
            metrics={"row_count": 100, "total_checks": 10, "failed_checks": 0, "health_score": 100},
            created_at=now - timedelta(days=2),
        )
        prior_config = Configuration(
            id="prior-intake",
            name="Control previo",
            module="intake",
            dataset_id=critical.id,
            config={},
        )
        prior = Run(
            id="prior-run",
            module="intake",
            name="Control previo",
            status="SUCCESS",
            decision="APPROVED",
            config_id=prior_config.id,
            dataset_version_id="critical-orders-v1",
            initiated_by="Test User",
            metrics={"total_rows": 100, "valid_rows": 100, "acceptance_rate": 100},
            created_at=now - timedelta(days=10),
            started_at=now - timedelta(days=10),
            finished_at=now - timedelta(days=10) + timedelta(seconds=30),
        )
        finding = Finding(
            id="finding-intake",
            run_id=intake.id,
            title="Valores obligatorios ausentes",
            code="REQUIRED",
            severity="HIGH",
            fingerprint="dashboard-required",
            details={"message": "20 filas necesitan corrección."},
        )
        case = ExceptionCase(
            id="exception-intake",
            display_id="EX-DASH-001",
            finding_id=finding.id,
            run_id=intake.id,
            configuration_id=intake.config_id,
            title="Corregir pedidos rechazados",
            module="intake",
            severity="HIGH",
            state="OPEN",
            updated_at=now - timedelta(hours=4),
        )
        db.add(prior_config)
        db.flush()
        db.add(prior)
        db.flush()
        db.add(finding)
        db.flush()
        db.add(case)
        db.commit()
        critical_id, intake_id, sentinel_id = critical.id, intake.id, sentinel.id

    response = authenticated.get("/api/v1/dashboard?period=7d")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["stats"] == {
        "datasets": 2,
        "total_rows": 200,
        "runs": 2,
        "open_exceptions": 1,
        "health_score": 81.8,
        "controls_failed": 1,
        "affected_datasets": 1,
    }
    assert data["variations"]["health_score"] == {"previous": 100.0, "delta": -18.2}
    assert data["variations"]["controls_failed"] == {"previous": 0.0, "delta": 1.0}
    assert data["attention_total"] == 1
    attention = data["attention"][0]
    assert {key: attention[key] for key in (
        "id", "dataset_id", "dataset_name", "module", "criticality", "action_label", "href"
    )} == {
        "id": "exception-intake",
        "dataset_id": critical_id,
        "dataset_name": "Pedidos críticos",
        "module": "intake",
        "criticality": "CRITICAL",
        "action_label": "Gestionar excepción",
        "href": "/exceptions?id=exception-intake",
    }
    assert data["datasets_attention"][0]["dataset_id"] == critical_id
    assert data["datasets_attention"][0]["last_run"]["id"] == intake_id
    assert {point["date"] for point in data["health_history"]} == {
        (now - timedelta(days=1)).date().isoformat(),
        (now - timedelta(days=2)).date().isoformat(),
    }
    recent = data["recent_runs"][0]
    assert recent["id"] == intake_id
    assert recent["processed_records"] == 100
    assert recent["finding_count"] == 1
    assert recent["duration_seconds"] == 65
    assert recent["dataset_criticality"] == "CRITICAL"
    assert next(item for item in data["module_status"] if item["module"] == "sentinel")["health_score"] == 100
    assert sentinel_id in {run["id"] for run in data["recent_runs"]}


def test_dashboard_filters_apply_consistently_and_validate_dataset_scope(authenticated, database):
    now = utcnow()
    with database() as db:
        critical, _ = dataset_with_run(
            db,
            identifier="critical-filter",
            name="Crítico",
            criticality="CRITICAL",
            module="intake",
            decision="REJECTED",
            metrics={"total_rows": 10, "valid_rows": 5, "acceptance_rate": 50},
            created_at=now - timedelta(hours=1),
        )
        healthy, _ = dataset_with_run(
            db,
            identifier="healthy-filter",
            name="Saludable",
            criticality="LOW",
            module="sentinel",
            decision="HEALTHY",
            metrics={"row_count": 10, "total_checks": 4, "failed_checks": 0, "health_score": 100},
            created_at=now - timedelta(hours=2),
        )
        db.commit()
        critical_id, healthy_id = critical.id, healthy.id

    by_dataset = authenticated.get(f"/api/v1/dashboard?period=30d&dataset_id={critical_id}").json()
    assert by_dataset["stats"]["runs"] == 1
    assert by_dataset["stats"]["controls_failed"] == 1
    assert {item["dataset_id"] for item in by_dataset["attention"]} == {critical_id}

    healthy_only = authenticated.get("/api/v1/dashboard?period=30d&module=sentinel&status=HEALTHY&criticality=LOW").json()
    assert healthy_only["stats"]["runs"] == 1
    assert healthy_only["stats"]["health_score"] == 100
    assert healthy_only["stats"]["controls_failed"] == 0
    assert healthy_only["attention"] == []
    assert healthy_only["recent_runs"][0]["dataset_id"] == healthy_id
    assert next(item for item in healthy_only["module_status"] if item["module"] == "intake")["runs"] == 0

    delivery_only = authenticated.get("/api/v1/dashboard?period=30d&module=DELIVERY")
    assert delivery_only.status_code == 200
    assert "DELIVERY" in delivery_only.json()["filter_options"]["modules"]

    assert authenticated.get("/api/v1/dashboard?period=1d").status_code == 422
    assert authenticated.get("/api/v1/dashboard?status=UNKNOWN").status_code == 422
    missing = authenticated.get("/api/v1/dashboard?dataset_id=missing")
    assert missing.status_code == 404


def test_operational_status_preserves_technical_and_business_outcomes():
    run = Run(status="RUNNING", decision=None, metrics={}, module="intake")
    assert run_operational_status(run) == "IN_PROGRESS"
    assert run_health_score(run) is None
    run.status, run.error = "FAILED", "Engine error"
    assert run_operational_status(run) == "TECHNICAL_FAILURE"
    assert run_health_score(run) == 0
    run.status, run.decision = "SUCCESS", "APPROVED_WITH_WARNINGS"
    run.metrics = {"total_rows": 20, "valid_rows": 18}
    assert run_operational_status(run) == "ATTENTION"
    assert run_health_score(run) == 90
