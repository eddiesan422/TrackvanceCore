from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import iso, utcnow
from .models import AuditEvent, Dataset, DatasetVersion, ExceptionCase, Finding, Run
from .services import audit_dto, run_dto

ACTIVE_EXCEPTION_STATES = frozenset(
    {"OPEN", "INVESTIGATING", "PENDING_VALIDATION", "WAITING_EXTERNAL"}
)
HEALTHY_DECISIONS = frozenset({"APPROVED", "CONFORME", "HEALTHY"})
IN_PROGRESS_STATUSES = frozenset({"QUEUED", "RUNNING"})
SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
PERIOD_DAYS: dict[str, int | None] = {"7d": 7, "30d": 30, "90d": 90, "all": None}


@dataclass(frozen=True)
class DashboardFilters:
    period: str = "30d"
    dataset_id: str | None = None
    module: str | None = None
    status: str | None = None
    criticality: str | None = None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _metric_number(metrics: dict[str, Any], key: str) -> float | None:
    value = metrics.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def run_operational_status(run: Run) -> str:
    if run.status in IN_PROGRESS_STATUSES:
        return "IN_PROGRESS"
    if run.status != "SUCCESS":
        return "TECHNICAL_FAILURE"
    if not run.decision or run.decision in HEALTHY_DECISIONS:
        return "HEALTHY"
    return "ATTENTION"


def run_health_score(run: Run) -> float | None:
    operational_status = run_operational_status(run)
    if operational_status == "IN_PROGRESS":
        return None
    if operational_status == "TECHNICAL_FAILURE":
        return 0.0
    metrics = run.metrics or {}
    if run.module == "intake":
        score = _metric_number(metrics, "acceptance_rate")
        total = _metric_number(metrics, "total_rows")
        valid = _metric_number(metrics, "valid_rows")
        if score is None and total:
            score = 100 * (valid or 0) / total
    elif run.module == "recon":
        score = _metric_number(metrics, "match_rate")
        total = _metric_number(metrics, "total_rows")
        matched = _metric_number(metrics, "matched")
        if score is None and total:
            score = 100 * (matched or 0) / total
    else:
        score = _metric_number(metrics, "health_score")
        total = _metric_number(metrics, "total_checks")
        failed = _metric_number(metrics, "failed_checks")
        if score is None and total:
            score = 100 * (total - (failed or 0)) / total
    return round(max(0.0, min(100.0, 100.0 if score is None else score)), 2)


def processed_records(run: Run) -> int | None:
    metrics = run.metrics or {}
    if run.module == "recon":
        source = _metric_number(metrics, "source_rows")
        target = _metric_number(metrics, "target_rows")
        if source is not None or target is not None:
            return int((source or 0) + (target or 0))
    for key in ("total_rows", "row_count", "source_rows"):
        value = _metric_number(metrics, key)
        if value is not None:
            return int(value)
    return None


def duration_seconds(run: Run) -> float | None:
    started, finished = _utc(run.started_at), _utc(run.finished_at)
    if not started or not finished:
        return None
    return round(max(0.0, (finished - started).total_seconds()), 2)


def _health_weight(run: Run) -> float:
    metrics = run.metrics or {}
    key = "total_checks" if run.module == "sentinel" else "total_rows"
    return max(1.0, _metric_number(metrics, key) or 1.0)


def aggregate_health(runs: list[Run]) -> float | None:
    scored = [(run_health_score(run), _health_weight(run)) for run in runs]
    available = [(score, weight) for score, weight in scored if score is not None]
    if not available:
        return None
    return round(sum(float(score) * weight for score, weight in available) / sum(weight for _, weight in available), 1)


def _variation(current: float | None, previous: float | None, available: bool) -> dict[str, float] | None:
    if not available or current is None or previous is None:
        return None
    return {"previous": round(float(previous), 1), "delta": round(float(current) - float(previous), 1)}


def build_dashboard(db: Session, organization_id: str, filters: DashboardFilters) -> dict[str, Any]:
    now = utcnow()
    days = PERIOD_DAYS[filters.period]
    current_start = now - timedelta(days=days) if days is not None else None
    previous_start = current_start - timedelta(days=days) if current_start and days is not None else None

    datasets = list(db.scalars(select(Dataset).where(Dataset.organization_id == organization_id)).all())
    versions = list(db.scalars(select(DatasetVersion).where(DatasetVersion.organization_id == organization_id)).all())
    runs = list(db.scalars(select(Run).where(Run.organization_id == organization_id).order_by(Run.created_at.desc())).all())
    findings = list(db.scalars(select(Finding).where(Finding.organization_id == organization_id)).all())
    cases = list(db.scalars(select(ExceptionCase).where(ExceptionCase.organization_id == organization_id)).all())
    audit_events = list(db.scalars(select(AuditEvent).where(AuditEvent.organization_id == organization_id).order_by(AuditEvent.created_at.desc()).limit(8)).all())

    dataset_by_id = {dataset.id: dataset for dataset in datasets}
    version_by_id = {version.id: version for version in versions}
    run_by_id = {run.id: run for run in runs}

    def associated_dataset_ids(run: Run) -> tuple[str, ...]:
        identifiers: list[str] = []
        for version_id in (run.dataset_version_id, run.target_version_id):
            version = version_by_id.get(version_id or "")
            if version and version.dataset_id not in identifiers:
                identifiers.append(version.dataset_id)
        return tuple(identifiers)

    associations = {run.id: associated_dataset_ids(run) for run in runs}

    def primary_dataset(run: Run) -> Dataset | None:
        identifiers = associations.get(run.id, ())
        return dataset_by_id.get(identifiers[0]) if identifiers else None

    def filtered_dataset_ids(run: Run) -> tuple[str, ...]:
        return tuple(
            identifier for identifier in associations.get(run.id, ())
            if (not filters.dataset_id or identifier == filters.dataset_id)
            and (not filters.criticality or (
                dataset_by_id.get(identifier) is not None
                and dataset_by_id[identifier].criticality == filters.criticality
            ))
        )

    def contextual_dataset(run: Run) -> Dataset | None:
        identifiers = filtered_dataset_ids(run) or associations.get(run.id, ())
        return dataset_by_id.get(identifiers[0]) if identifiers else None

    def matches_dimensions(run: Run, *, include_status: bool = True) -> bool:
        if filters.module and run.module != filters.module:
            return False
        identifiers = associations.get(run.id, ())
        if filters.dataset_id and filters.dataset_id not in identifiers:
            return False
        if filters.criticality and not any(dataset_by_id.get(identifier) and dataset_by_id[identifier].criticality == filters.criticality for identifier in identifiers):
            return False
        return not (include_status and filters.status and run_operational_status(run) != filters.status)

    def in_period(run: Run, start: datetime | None, end: datetime | None = None) -> bool:
        created = _utc(run.created_at)
        if created is None:
            return False
        return (start is None or created >= start) and (end is None or created < end)

    current_runs = [run for run in runs if matches_dimensions(run) and in_period(run, current_start)]
    previous_runs = [run for run in runs if previous_start and matches_dimensions(run) and in_period(run, previous_start, current_start)]
    current_ids, previous_ids = {run.id for run in current_runs}, {run.id for run in previous_runs}
    active_cases = [case for case in cases if case.state in ACTIVE_EXCEPTION_STATES and case.run_id in current_ids]
    previous_active_cases = [case for case in cases if case.state in ACTIVE_EXCEPTION_STATES and case.run_id in previous_ids]
    current_findings = [finding for finding in findings if finding.run_id in current_ids]
    findings_by_run: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        findings_by_run[finding.run_id].append(finding)

    def affected_ids(relevant_runs: list[Run], relevant_cases: list[ExceptionCase]) -> set[str]:
        affected: set[str] = set()
        for run in relevant_runs:
            if run_operational_status(run) in {"ATTENTION", "TECHNICAL_FAILURE"}:
                affected.update(filtered_dataset_ids(run))
        for case in relevant_cases:
            case_run = run_by_id.get(case.run_id)
            if case_run:
                affected.update(filtered_dataset_ids(case_run))
        return affected

    current_affected = affected_ids(current_runs, active_cases)
    previous_affected = affected_ids(previous_runs, previous_active_cases)
    current_health, previous_health = aggregate_health(current_runs), aggregate_health(previous_runs)
    current_failed = sum(run_operational_status(run) in {"ATTENTION", "TECHNICAL_FAILURE"} for run in current_runs)
    previous_failed = sum(run_operational_status(run) in {"ATTENTION", "TECHNICAL_FAILURE"} for run in previous_runs)
    comparison_available = bool(previous_runs or previous_active_cases)

    latest_version_by_dataset: dict[str, DatasetVersion] = {}
    for version in versions:
        current = latest_version_by_dataset.get(version.dataset_id)
        if current is None or version.version > current.version:
            latest_version_by_dataset[version.dataset_id] = version
    dataset_scope = [dataset for dataset in datasets if (not filters.dataset_id or dataset.id == filters.dataset_id) and (not filters.criticality or dataset.criticality == filters.criticality)]

    def enriched_run(run: Run) -> dict[str, Any]:
        result = run_dto(db, run)
        source = primary_dataset(run)
        target_ids = associations.get(run.id, ())[1:]
        target = dataset_by_id.get(target_ids[0]) if target_ids else None
        result.update({
            "dataset_id": source.id if source else None,
            "dataset_name": source.name if source else result.get("dataset_name", ""),
            "dataset_criticality": source.criticality if source else None,
            "target_dataset_id": target.id if target else None,
            "target_dataset_name": target.name if target else None,
            "processed_records": processed_records(run),
            "finding_count": len(findings_by_run.get(run.id, [])),
            "duration_seconds": duration_seconds(run),
            "operational_status": run_operational_status(run),
            "health_score": run_health_score(run),
        })
        return result

    cases_by_finding = {case.finding_id: case for case in cases if case.finding_id}
    attention: list[tuple[int, float, dict[str, Any]]] = []

    def add_attention(item: dict[str, Any], dataset: Dataset | None, moment: datetime | None) -> None:
        dataset_criticality = dataset.criticality if dataset else "HIGH"
        issue_severity = str(item.get("severity") or dataset_criticality)
        priority = max(SEVERITY_RANK.get(dataset_criticality, 0), SEVERITY_RANK.get(issue_severity, 0))
        item.update({
            "dataset_id": dataset.id if dataset else None,
            "dataset_name": dataset.name if dataset else "Dataset no disponible",
            "criticality": dataset_criticality,
            "date": iso(moment),
        })
        normalized_moment = _utc(moment)
        timestamp = normalized_moment.timestamp() if normalized_moment else 0.0
        attention.append((priority, timestamp, item))

    for case in active_cases:
        run = run_by_id.get(case.run_id)
        if run:
            add_attention({
                "id": case.id,
                "subject_type": "EXCEPTION",
                "module": case.module,
                "problem": case.title,
                "detail": f"{case.display_id} · {case.state}",
                "severity": case.severity,
                "action_label": "Gestionar excepción",
                "href": f"/exceptions?id={case.id}",
            }, contextual_dataset(run), case.updated_at)

    for finding in current_findings:
        if finding.id in cases_by_finding:
            continue
        run = run_by_id.get(finding.run_id)
        if not run:
            continue
        detail = finding.details.get("message") if isinstance(finding.details, dict) else None
        add_attention({
            "id": finding.id,
            "subject_type": "FINDING",
            "module": run.module,
            "problem": finding.title,
            "detail": detail or finding.code,
            "severity": finding.severity,
            "action_label": "Revisar hallazgo",
            "href": f"/runs/{run.id}",
        }, contextual_dataset(run), finding.created_at)

    for run in current_runs:
        operational = run_operational_status(run)
        has_unmanaged_detail = findings_by_run.get(run.id) or any(case.run_id == run.id for case in active_cases)
        if operational == "TECHNICAL_FAILURE" or (operational == "ATTENTION" and not has_unmanaged_detail):
            dataset = contextual_dataset(run)
            add_attention({
                "id": run.id,
                "subject_type": "RUN",
                "module": run.module,
                "problem": "La ejecución no pudo completarse" if operational == "TECHNICAL_FAILURE" else "Control con resultado que requiere revisión",
                "detail": run.error or run.name,
                "severity": dataset.criticality if dataset else "HIGH",
                "action_label": "Revisar ejecución",
                "href": f"/runs/{run.id}",
            }, dataset, run.finished_at or run.created_at)

    attention.sort(key=lambda entry: (-entry[0], -entry[1]))
    attention_items = [item for _, _, item in attention]

    health_by_day: dict[str, dict[str, list[Run]]] = defaultdict(lambda: defaultdict(list))
    for run in current_runs:
        day = (iso(run.created_at) or "")[:10]
        if day:
            health_by_day[day][run.module].append(run)
    health_history = []
    for day in sorted(health_by_day):
        groups = health_by_day[day]
        health_history.append({
            "date": day,
            "overall": aggregate_health([run for module_runs in groups.values() for run in module_runs]),
            "intake": aggregate_health(groups.get("intake", [])),
            "recon": aggregate_health(groups.get("recon", [])),
            "sentinel": aggregate_health(groups.get("sentinel", [])),
        })

    module_status = []
    for module in ("intake", "recon", "sentinel"):
        relevant = [run for run in current_runs if run.module == module]
        technical = sum(run_operational_status(run) == "TECHNICAL_FAILURE" for run in relevant)
        alerts = sum(run_operational_status(run) == "ATTENTION" for run in relevant)
        latest = max(relevant, key=lambda run: _utc(run.created_at) or datetime.min.replace(tzinfo=UTC), default=None)
        activity_count = len({run.config_id for run in relevant}) if module == "sentinel" else len(relevant)
        module_status.append({
            "module": module,
            "status": "ATTENTION" if technical or alerts else "HEALTHY" if relevant else "NO_DATA",
            "runs": len(relevant),
            "activity_count": activity_count,
            "activity_label": "monitores" if module == "sentinel" else "conciliaciones" if module == "recon" else "ejecuciones",
            "failed_runs": technical,
            "alerts": alerts,
            "issues": technical + alerts,
            "findings": sum(len(findings_by_run.get(run.id, [])) for run in relevant),
            "processed_records": sum(processed_records(run) or 0 for run in relevant),
            "health_score": aggregate_health(relevant),
            "latest_run_at": iso(latest.created_at) if latest else None,
        })

    latest_current_by_dataset: dict[str, Run] = {}
    for run in current_runs:
        for dataset_id in filtered_dataset_ids(run):
            previous = latest_current_by_dataset.get(dataset_id)
            if previous is None or (_utc(run.created_at) or datetime.min.replace(tzinfo=UTC)) > (_utc(previous.created_at) or datetime.min.replace(tzinfo=UTC)):
                latest_current_by_dataset[dataset_id] = run
    candidate_dataset_ids = {
        dataset_id for dataset_id, run in latest_current_by_dataset.items()
        if run_operational_status(run) in {"ATTENTION", "TECHNICAL_FAILURE"}
    }
    for case in active_cases:
        run = run_by_id.get(case.run_id)
        if run:
            candidate_dataset_ids.update(filtered_dataset_ids(run))

    datasets_attention: list[dict[str, Any]] = []
    for dataset_id in candidate_dataset_ids:
        dataset = dataset_by_id.get(dataset_id)
        latest = latest_current_by_dataset.get(dataset_id)
        if not dataset or not latest:
            continue
        relevant_current = [run for run in current_runs if dataset_id in associations.get(run.id, ())]
        relevant_all = [run for run in runs if dataset_id in associations.get(run.id, ()) and matches_dimensions(run, include_status=False)]
        scored: list[tuple[Run, float]] = []
        for run in relevant_all:
            score = run_health_score(run)
            if score is not None:
                scored.append((run, score))
        scored.sort(key=lambda entry: _utc(entry[0].created_at) or datetime.min.replace(tzinfo=UTC), reverse=True)
        trend = round(float(scored[0][1]) - float(scored[1][1]), 1) if len(scored) > 1 else None
        dataset_findings = sum(len(findings_by_run.get(run.id, [])) for run in relevant_current)
        dataset_cases = sum(dataset_id in associations.get(case.run_id, ()) for case in active_cases)
        technical = sum(run_operational_status(run) == "TECHNICAL_FAILURE" for run in relevant_current)
        datasets_attention.append({
            "dataset_id": dataset.id,
            "dataset_name": dataset.name,
            "domain": dataset.domain,
            "criticality": dataset.criticality,
            "health_score": run_health_score(latest),
            "findings": dataset_findings + technical,
            "open_exceptions": dataset_cases,
            "last_run": {
                "id": latest.id,
                "module": latest.module,
                "status": latest.status,
                "decision": latest.decision,
                "created_at": iso(latest.created_at),
            },
            "trend_delta": trend,
            "href": f"/datasets/{dataset.id}",
            "action_href": f"/runs/{latest.id}",
        })
    datasets_attention.sort(key=lambda item: (
        -SEVERITY_RANK.get(str(item["criticality"]), 0),
        float(item["health_score"]) if item["health_score"] is not None else 101.0,
        -int(item["findings"]),
    ))

    volumes: dict[str, int] = defaultdict(int)
    for version in versions:
        created = _utc(version.created_at)
        if version.source_type != "INTAKE_OUTPUT" and (current_start is None or (created and created >= current_start)):
            volumes[(iso(version.created_at) or "")[:10]] += version.row_count

    return {
        "applied_filters": {
            "period": filters.period,
            "dataset_id": filters.dataset_id,
            "module": filters.module,
            "status": filters.status,
            "criticality": filters.criticality,
        },
        "filter_options": {
            "datasets": [{"id": dataset.id, "name": dataset.name, "domain": dataset.domain, "criticality": dataset.criticality} for dataset in sorted(datasets, key=lambda item: item.name.casefold())],
            "periods": ["7d", "30d", "90d", "all"],
            "modules": ["intake", "recon", "sentinel"],
            "statuses": ["ATTENTION", "HEALTHY", "IN_PROGRESS", "TECHNICAL_FAILURE"],
            "criticalities": ["CRITICAL", "HIGH", "MEDIUM", "LOW"],
        },
        "period": {
            "start": iso(current_start),
            "end": iso(now),
            "previous_start": iso(previous_start),
        },
        "stats": {
            "datasets": len(dataset_scope),
            "total_rows": sum(latest_version_by_dataset[dataset.id].row_count for dataset in dataset_scope if dataset.id in latest_version_by_dataset),
            "runs": len(current_runs),
            "open_exceptions": len(active_cases),
            "health_score": current_health,
            "controls_failed": current_failed,
            "affected_datasets": len(current_affected),
        },
        "variations": {
            "health_score": _variation(current_health, previous_health, comparison_available),
            "controls_failed": _variation(current_failed, previous_failed, comparison_available),
            "open_exceptions": _variation(len(active_cases), len(previous_active_cases), comparison_available),
            "affected_datasets": _variation(len(current_affected), len(previous_affected), comparison_available),
        },
        "attention": attention_items[:10],
        "attention_total": len(attention_items),
        "health_history": health_history,
        "datasets_attention": datasets_attention[:8],
        "recent_runs": [enriched_run(run) for run in current_runs[:8]],
        "activity": [audit_dto(event) for event in audit_events],
        "module_status": module_status,
        "volume_history": [{"date": day, "rows": count} for day, count in sorted(volumes.items())[-14:]],
        "organization_name": "Trackvance Demo",
        "prototype": True,
    }
