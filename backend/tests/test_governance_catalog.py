"""Governance uses real Intake evidence and guards native derived-content routes."""

import copy
from pathlib import Path

import polars as pl
import pytest
from sqlalchemy import func, select

from trackvance.governance import (
    add_security_dependency,
    authorize_dataset_content,
    dataset_eligibility,
    governance_snapshot,
    strict_approval,
    verify_strict_approval_artifacts,
)
from trackvance.governance_models import DatasetBlock, StrictApproval
from trackvance.models import (
    Artifact,
    ArtifactLink,
    Configuration,
    Dataset,
    DatasetVersion,
    Job,
    Role,
    RolePermission,
    Run,
    User,
)
from trackvance.operations_common import OperationError
from trackvance.processing import intake
from trackvance.services import create_version, enqueue, execute_run


@pytest.fixture
def approved(database, tmp_path):
    path = tmp_path / "source.csv"
    path.write_text("key,country,value\n001,CO,yes\n002,US,yes\n003,CO,yes\n", encoding="utf-8")
    with database() as db:
        dataset = Dataset(name="Governed source")
        db.add(dataset)
        db.flush()
        source = create_version(db, dataset, path, path.name)
        config = Configuration(name="Strict contract", module="intake", dataset_id=dataset.id,
            config={"required_columns": ["key", "value"], "max_error_rate": 0})
        db.add(config)
        db.flush()
        run = enqueue(db, config, source, None, "Test User")
        db.commit()
        execute_run(db, run)
        db.commit()
        assert run.status == "SUCCESS" and run.decision == "APPROVED"
        return {"dataset": dataset.id, "input": source.id, "run": run.id, "output": run.output_version_id, "config": config.id}


def classify(client, dataset_id):
    macro = client.post("/api/v1/governance/macrodomains", json={"name": "Finanzas"}).json()
    domain = client.post("/api/v1/governance/domains", json={"name": "Ventas", "macro_domain_id": macro["id"]}).json()
    response = client.patch(f"/api/v1/datasets/{dataset_id}/governance", json={"expected_version": 1,
        "macro_domain_id": macro["id"], "domain_id": domain["id"]})
    assert response.status_code == 200, response.text
    return macro, domain


def test_committed_strict_evidence_and_late_classification(approved, database, authenticated):
    with database() as db:
        run = db.get(Run, approved["run"])
        user = db.get(User, "test-user")
        output = db.get(DatasetVersion, approved["output"])
        assert strict_approval(db, run)["approved"]
        assert verify_strict_approval_artifacts(db, run)["verified_bytes"]
        assert db.scalar(select(func.count()).select_from(StrictApproval)) == 1
        before = {"run": copy.deepcopy(run.metrics), "decision": run.decision, "hash": output.sha256}
        assert not dataset_eligibility(db, user, output)["eligible"]
    macro, domain = classify(authenticated, approved["dataset"])
    with database() as db:
        run = db.get(Run, approved["run"])
        output = db.get(DatasetVersion, approved["output"])
        evaluation = dataset_eligibility(db, db.get(User, "test-user"), output)
        assert evaluation["eligible"] and evaluation["governance"]["inherited"]
        assert evaluation["governance"]["domain_id"] == domain["id"]
        assert run.metrics == before["run"] and run.decision == before["decision"] and output.sha256 == before["hash"]
        assert run.execution_plan["governance_snapshot"]["domain_id"] is None
    panel = authenticated.get(f"/api/v1/catalog/datasets/{approved['dataset']}?section=history").json()
    assert panel["total"] == 1
    assert panel["items"][0]["snapshot"]["macro_domain_id"] == macro["id"]


@pytest.mark.parametrize("kind,expected", [
    ("success_only", "DECISION_NOT_APPROVED"), ("warning", "QUALITY_FINDINGS"),
    ("partial", "ROW_COUNTS_INCONSISTENT"), ("rounded", "QUALITY_FINDINGS"),
    ("empty", "EMPTY_INPUT"), ("missing_counts", "ACCOUNTING_INSUFFICIENT"),
    ("no_rules", "NO_VALIDATION_RULES"), ("zero_evaluation", "NO_EFFECTIVE_VALIDATION"),
    ("missing_rule", "RULE_EVIDENCE_MISMATCH"), ("missing_link", "LINEAGE_EVIDENCE_MISSING"),
    ("wrong_parent", "EVIDENCE_IDENTITY_MISMATCH"), ("no_output", "EVIDENCE_IDENTITY_MISSING"),
    ("cancelled", "EXECUTION_NOT_COMPLETE"),
])
def test_strict_negative_evidence(approved, database, kind, expected):
    with database() as db:
        run = db.get(Run, approved["run"])
        metrics = copy.deepcopy(run.metrics)
        if kind == "success_only":
            run.decision = None
        elif kind == "warning":
            metrics["warning_rows"] = 1
        elif kind in {"partial", "rounded"}:
            metrics.update(total_rows=10000, processed_rows=10000, valid_rows=9900, output_rows=9900,
                error_rows=100, discarded_rows=100, acceptance_rate=100)
        elif kind == "empty":
            metrics.update({key: 0 for key in ("total_rows", "processed_rows", "valid_rows", "output_rows", "validation_coverage_rows")})
        elif kind == "missing_counts":
            metrics.pop("validation_coverage_rows")
        elif kind == "no_rules":
            db.get(Configuration, approved["config"]).config = {"transforms": []}
        elif kind == "zero_evaluation":
            metrics["validation_coverage_rows"] = 0
            for rule in metrics["rules"]:
                rule.update(evaluated_count=0, skipped_count=metrics["total_rows"])
        elif kind == "missing_rule":
            metrics["rules"] = metrics["rules"][:1]
        elif kind == "missing_link":
            link = db.scalar(select(ArtifactLink).where(ArtifactLink.source_id == run.id, ArtifactLink.relation == "RUN_INPUT"))
            db.delete(link)
        elif kind == "wrong_parent":
            db.get(DatasetVersion, approved["output"]).parent_version_id = None
        elif kind == "no_output":
            run.output_version_id = None
        else:
            run.cancel_requested = True
        run.metrics = metrics
        db.flush()
        result = strict_approval(db, run)
        assert not result["approved"]
        assert expected in {reason["code"] for reason in result["reasons"]}


def test_conditional_validation_does_not_sum_overlap_or_require_every_condition():
    frame = pl.DataFrame({"country": ["CO", "US", "CO"], "value": ["yes", "yes", "yes"]})
    rules = [{"type": "required", "column": "value", "rule_id": "a", "when": {"column": "country", "operator": "eq", "value": "CO"}},
             {"type": "required", "column": "value", "rule_id": "b", "when": {"column": "country", "operator": "eq", "value": "CO"}}]
    _, metrics, _ = intake(frame, {"rules": rules})
    assert sum(rule["evaluated_count"] for rule in metrics["rules"]) == 4
    assert metrics["validation_coverage_rows"] == 2
    assert all(rule["skipped_count"] == 1 for rule in metrics["rules"])


@pytest.mark.parametrize("artifact_kind", ["evidence", "canonical", "original"])
def test_integrity_worker_detects_corrupt_full_artifacts(approved, database, artifact_kind):
    with database() as db:
        run = db.get(Run, approved["run"])
        version = db.get(DatasetVersion, approved["input"] if artifact_kind == "original" else approved["output"])
        artifact = db.scalar(select(Artifact).where(Artifact.path == run.evidence_path)) if artifact_kind == "evidence" else db.get(Artifact,
            version.original_artifact_id if artifact_kind == "original" else version.canonical_artifact_id)
        path = Path(artifact.path)
        path.write_bytes(path.read_bytes() + b"corrupt")
        assert strict_approval(db, run)["approved"]  # Metadata tree never hashes files.
        with pytest.raises(OperationError) as error:
            verify_strict_approval_artifacts(db, run)
        assert error.value.code == "APPROVAL_INTEGRITY_FAILED"


def test_domain_optional_compatible_duplicates_version_conflict(approved, authenticated, database):
    macro, domain = classify(authenticated, approved["dataset"])
    duplicate = authenticated.post("/api/v1/governance/macrodomains", json={"name": "  FINANZAS  "})
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "DOMAIN_DUPLICATE"
    other = authenticated.post("/api/v1/governance/macrodomains", json={"name": "Operaciones"}).json()
    incompatible = authenticated.post("/api/v1/datasets", json={"name": "Bad classification", "macro_domain_id": other["id"], "domain_id": domain["id"]})
    assert incompatible.status_code == 422
    optional = authenticated.post("/api/v1/datasets", json={"name": "Incomplete", "macro_domain_id": macro["id"]})
    assert optional.status_code == 201 and not optional.json()["governance"]["classification_complete"]
    conflict = authenticated.patch(f"/api/v1/datasets/{approved['dataset']}/governance", json={"expected_version": 1, "description": "stale"})
    assert conflict.status_code == 409
    renamed = authenticated.patch(f"/api/v1/governance/domains/{domain['id']}", json={"expected_version": 1, "name": "Ventas nuevas"})
    assert renamed.status_code == 200 and renamed.json()["id"] == domain["id"]
    with database() as db:
        assert db.get(Dataset, approved["dataset"]).domain == "Operaciones"
        assert governance_snapshot(db, db.get(Dataset, approved["dataset"]))["domain"]["name"] == "Ventas nuevas"


def test_legacy_inheritance_requires_verified_relationships_and_not_names(approved, database, authenticated):
    _, domain = classify(authenticated, approved["dataset"])
    with database() as db:
        output = db.get(DatasetVersion, approved["output"])
        asset = db.get(Dataset, output.dataset_id)
        asset.intake_input_dataset_id = None
        asset.intake_contract_id = None
        asset.name = "Completely unrelated display name"
        db.commit()
        assert governance_snapshot(db, asset)["domain_id"] == domain["id"]
        output.source_run_id = None
        db.commit()
        assert governance_snapshot(db, asset)["domain_id"] is None


def test_catalog_paginated_counts_and_filters_are_metadata_only(approved, authenticated, database, monkeypatch):
    _, domain = classify(authenticated, approved["dataset"])
    with database() as db:
        for number in range(101):
            db.add(Dataset(name=f"Unclassified {number:03}"))
        db.add(Dataset(organization_id="foreign", name="Private foreign dataset"))
        db.commit()
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Catalog must not materialize canonical/evidence files")
    from trackvance.artifactstore import storage_provider
    monkeypatch.setattr(storage_provider, "dataset_paths", forbidden)
    monkeypatch.setattr(storage_provider, "materialize", forbidden)
    response = authenticated.get("/api/v1/catalog/resources", params={"pending": "true", "offset": 100, "limit": 1})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 101 and len(response.json()["items"]) == 1
    tree = authenticated.get("/api/v1/catalog/tree").json()
    assert next(item for item in tree["items"] if item["id"] == "pending")["count"] == 101
    intake_resources = authenticated.get("/api/v1/catalog/resources", params={"resource_type": "INTAKE", "domain_id": domain["id"]}).json()
    assert intake_resources["total"] == 1
    assert "Private foreign" not in response.text


def test_report_catalog_paginates_latest_revision_and_all_source_access_in_sql(approved, authenticated, database):
    from trackvance.report_models import ReportDefinition, ReportRevision

    _, domain = classify(authenticated, approved["dataset"])
    with database() as db:
        pending, foreign = Dataset(name="Pending report source"), Dataset(organization_id="foreign", name="Private report source")
        db.add_all([pending, foreign])
        db.flush()
        definitions = []
        for number in range(101):
            definition = ReportDefinition(name=f"REPORT {number:03}", owner_user_id="test-user", active=number != 3)
            db.add(definition)
            db.flush()
            definitions.append(definition)
            db.add(ReportRevision(definition_id=definition.id, version=1,
                draft={"sources": [{"input_dataset_id": approved["dataset"]}]}, query_hash="a" * 64, created_by_user_id="test-user"))
        for index, sources in ((0, [foreign.id]), (1, [pending.id]), (2, [approved["dataset"], foreign.id])):
            db.add(ReportRevision(definition_id=definitions[index].id, version=2,
                draft={"sources": [{"input_dataset_id": identity} for identity in sources]}, query_hash="b" * 64, created_by_user_id="test-user"))
        db.commit()
        final_id = definitions[-1].id
    response = authenticated.get("/api/v1/catalog/resources", params={"resource_type": "REPORT", "domain_id": domain["id"], "offset": 97, "limit": 1})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 98 and response.json()["items"][0]["id"] == final_id
    assert authenticated.get("/api/v1/catalog/resources", params={"resource_type": "REPORT", "pending": True}).json()["total"] == 1
    assert authenticated.get("/api/v1/catalog/resources", params={"resource_type": "REPORT", "domain_id": domain["id"], "status": "INACTIVE"}).json()["total"] == 1


def test_column_glossary_is_bound_to_exact_version(approved, authenticated, database, tmp_path):
    term = authenticated.post("/api/v1/governance/glossary", json={"name": "Llave", "definition": "Identificador funcional"}).json()
    body = {"version_id": approved["input"], "column_name": "key", "description": "Llave registrada", "term_ids": [term["id"]], "expected_version": 0}
    response = authenticated.patch(f"/api/v1/catalog/datasets/{approved['dataset']}/columns", json=body)
    assert response.status_code == 200
    assert authenticated.patch(f"/api/v1/catalog/datasets/{approved['dataset']}/columns", json=body).status_code == 409
    with database() as db:
        path = tmp_path / "next.csv"
        path.write_text("key,value\nNew,yes\n", encoding="utf-8")
        version = create_version(db, db.get(Dataset, approved["dataset"]), path, path.name)
        db.commit()
        new_id = version.id
    panel = authenticated.get(f"/api/v1/catalog/datasets/{approved['dataset']}?section=columns&version_id={new_id}").json()
    key = next(item for item in panel["items"] if item["name"] == "key")
    assert key["description"] == "" and key["terms"] == [] and key["documentation_version"] == 0


def test_inactive_term_can_be_retained_without_a_new_association(approved, authenticated):
    term = authenticated.post("/api/v1/governance/glossary", json={"name": "Historical term", "definition": "Preserved definition"}).json()
    path = f"/api/v1/catalog/datasets/{approved['dataset']}/columns"
    body = {"version_id": approved["input"], "column_name": "key", "description": "First", "term_ids": [term["id"]], "expected_version": 0}
    assert authenticated.patch(path, json=body).status_code == 200
    assert authenticated.patch(f"/api/v1/governance/glossary/{term['id']}", json={"expected_version": 1, "active": False}).status_code == 200
    retained = authenticated.patch(path, json={**body, "description": "Updated description", "expected_version": 1})
    assert retained.status_code == 200 and not retained.json()["terms"][0]["active"]
    new_association = authenticated.patch(path, json={**body, "column_name": "value"})
    assert new_association.status_code == 422 and new_association.json()["error"]["code"] == "TERM_INACTIVE"


def test_inactive_governance_can_be_retained_but_not_newly_assigned(approved, authenticated, database):
    macro, domain = classify(authenticated, approved["dataset"])
    with database() as db:
        password_hash = db.get(User, "test-user").password_hash
        owner = User(name="Historical owner", email="historical-owner@test.local", password_hash=password_hash)
        other = User(name="Other inactive owner", email="inactive-other@test.local", active=False, password_hash=password_hash)
        foreign = User(name="Foreign owner", email="foreign-owner@test.local", organization_id="foreign-org", password_hash=password_hash)
        db.add_all([owner, other, foreign])
        db.commit()
        owner_id, other_id, foreign_id = owner.id, other.id, foreign.id
    path = f"/api/v1/datasets/{approved['dataset']}/governance"
    assert authenticated.patch(path, json={"expected_version": 2, "business_owner_id": owner_id}).status_code == 200
    with database() as db:
        db.get(User, owner_id).active = False
        db.commit()
    assert authenticated.patch(f"/api/v1/governance/macrodomains/{macro['id']}", json={"expected_version": 1, "active": False}).status_code == 200
    assert authenticated.patch(f"/api/v1/governance/domains/{domain['id']}", json={"expected_version": 1, "active": False}).status_code == 200
    retained = {"expected_version": 3, "macro_domain_id": macro["id"], "domain_id": domain["id"],
                "business_owner_id": owner_id, "description": "Updated while retaining history"}
    response = authenticated.patch(path, json=retained)
    assert response.status_code == 200, response.text
    assert authenticated.patch(path, json=retained).status_code == 409
    for identity in (other_id, foreign_id):
        refused = authenticated.patch(path, json={"expected_version": 4, "business_owner_id": identity})
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "RESPONSIBLE_INVALID"
    unclassified = authenticated.post("/api/v1/datasets", json={"name": "New classification attempt"}).json()
    refused = authenticated.patch(f"/api/v1/datasets/{unclassified['id']}/governance", json={
        "expected_version": 1, "macro_domain_id": macro["id"], "domain_id": domain["id"]})
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "MACRODOMAIN_INVALID"
    with database() as db:
        persisted = db.get(Dataset, approved["dataset"])
        assert persisted.business_owner_id == owner_id and persisted.governance_version == 4
        eligibility = dataset_eligibility(db, db.get(User, "test-user"), db.get(DatasetVersion, approved["output"]))
        assert eligibility["strict_approval"]["approved"] and not eligibility["eligible"]
        assert any(reason["code"] == "CLASSIFICATION_INCOMPLETE" for reason in eligibility["reasons"])


def test_native_content_paths_revalidate_transitive_restrictions(approved, authenticated, database, tmp_path):
    with database() as db:
        derived = Dataset(name="Report derived")
        db.add(derived)
        db.flush()
        source = db.get(DatasetVersion, approved["output"])
        add_security_dependency(db, derived.id, source.dataset_id)
        path = tmp_path / "derived.csv"
        path.write_text("key,value\n001,yes\n", encoding="utf-8")
        version = create_version(db, derived, path, path.name, source_type="REPORTS")
        config = Configuration(name="Derived contract", module="intake", dataset_id=derived.id, config={"required_columns": ["key"]})
        db.add(config)
        db.flush()
        run = enqueue(db, config, version, None, "Test User")
        db.commit()
        execute_run(db, run)
        db.commit()
        descendant = db.get(DatasetVersion, run.output_version_id)
        db.add(DatasetBlock(dataset_id=approved["dataset"], scope="CONTENT", reason="Revocation", created_by_id="test-user"))
        db.commit()
        protected = [version, descendant]
        routes = [f"/api/v1/dataset-versions/{item.id}/profile" for item in protected]
        routes += [f"/api/v1/datasets/{item.dataset_id}" for item in protected]
        routes += [f"/api/v1/artifacts/{item.canonical_artifact_id}/download" for item in protected]
        routes += [f"/api/v1/runs/{run.id}/export.csv", f"/api/v1/runs/{run.id}/export.xlsx", f"/api/v1/runs/{run.id}/results"]
        config_id, input_id = config.id, version.id
    for route in routes:
        response = authenticated.get(route)
        assert response.status_code == 403, (route, response.text)
    for item in protected:
        catalog = authenticated.get(f"/api/v1/catalog/datasets/{item.dataset_id}?section=versions")
        assert catalog.status_code == 200 and all("profile" not in entry for entry in catalog.json()["items"])
    response = authenticated.post("/api/v1/intake/runs", json={"contract_id": config_id, "dataset_version_id": input_id})
    assert response.status_code == 403


def test_report_block_scope_and_release_are_explicit(approved, authenticated, database):
    classify(authenticated, approved["dataset"])
    response = authenticated.post(f"/api/v1/catalog/datasets/{approved['dataset']}/blocks", json={"scope": "REPORT", "reason": "Uso suspendido"})
    assert response.status_code == 201
    block = response.json()
    with database() as db:
        version = db.get(DatasetVersion, approved["output"])
        user = db.get(User, "test-user")
        authorize_dataset_content(db, user, version)
        assert not dataset_eligibility(db, user, version)["eligible"]
        assert strict_approval(db, db.get(Run, approved["run"]))["approved"]
    released = authenticated.patch(f"/api/v1/catalog/blocks/{block['id']}", json={"expected_version": 1, "reason": "Restricción resuelta", "active": False})
    assert released.status_code == 200 and not released.json()["active"]
    with database() as db:
        assert dataset_eligibility(db, db.get(User, "test-user"), db.get(DatasetVersion, approved["output"]))["eligible"]


def test_custom_roles_receive_no_new_permissions_but_can_select_domains(authenticated, database):
    with database() as db:
        role = Role(name="Existing custom", normalized_name="existing custom")
        db.add(role)
        db.flush()
        for permission in ("datasets:read", "datasets:write"):
            db.add(RolePermission(role_id=role.id, permission_code=permission))
        db.get(User, "test-user").role_id = role.id
        db.commit()
    assert authenticated.get("/api/v1/governance/macrodomains").status_code == 200
    assert authenticated.post("/api/v1/governance/macrodomains", json={"name": "Denied"}).status_code == 403
    assert authenticated.get("/api/v1/catalog/tree").status_code == 403
    assert authenticated.post("/api/v1/reports/datasets", json={}).status_code == 403


def test_security_dependency_cycles_duplicates_and_cross_org_fail_closed(database):
    with database() as db:
        a, b, c = Dataset(name="A"), Dataset(name="B"), Dataset(organization_id="other", name="C")
        db.add_all([a, b, c])
        db.flush()
        add_security_dependency(db, b.id, a.id)
        add_security_dependency(db, b.id, a.id)
        from trackvance.governance_models import DatasetSecurityDependency
        assert db.scalar(select(func.count()).select_from(DatasetSecurityDependency)) == 1
        with pytest.raises(OperationError, match="ciclo"):
            add_security_dependency(db, a.id, b.id)
        with pytest.raises(OperationError):
            add_security_dependency(db, a.id, c.id)


@pytest.mark.parametrize("change", ["content_block", "source_inactive", "permission_revoked"])
def test_intake_rechecks_authority_after_compute_before_publication(approved, database, monkeypatch, change):
    from trackvance import services
    from trackvance.worker import process_once

    with database() as db:
        db.scalar(select(Job).where(Job.run_id == approved["run"])).status = "SUCCESS"
        source = db.get(DatasetVersion, approved["output"] if change == "source_inactive" else approved["input"])
        config = db.get(Configuration, approved["config"])
        if change == "source_inactive":
            config = Configuration(name="Revalidate derived source", module="intake", dataset_id=source.dataset_id,
                config={"required_columns": ["key", "value"], "max_error_rate": 0})
            db.add(config)
            db.flush()
        run = enqueue(db, config, source, None, "Test User")
        assert run.initiated_by_type == "USER"
        db.commit()
        run_id = run.id
        before = db.scalar(select(func.count()).select_from(DatasetVersion))

    original = services.intake

    def revoke_during_compute(*args, **kwargs):
        result = original(*args, **kwargs)
        with database() as control:
            if change == "content_block":
                control.add(DatasetBlock(dataset_id=approved["dataset"], scope="CONTENT", reason="Revoked during compute", created_by_id="test-user"))
            elif change == "source_inactive":
                # The source of an existing derivative becomes unavailable.
                control.get(Dataset, approved["dataset"]).status = "ARCHIVED"
            else:
                role = Role(name="Read only after enqueue", normalized_name="read only after enqueue")
                control.add(role)
                control.flush()
                control.add(RolePermission(role_id=role.id, permission_code="datasets:read"))
                control.get(User, "test-user").role_id = role.id
            control.commit()
        return result

    monkeypatch.setattr(services, "intake", revoke_during_compute)
    assert process_once("governance-worker", lane="DEFAULT")
    with database() as db:
        run = db.get(Run, run_id)
        assert run.status == "FAILED" and run.output_version_id is None and run.result_path is None
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == before
        assert not db.scalar(select(StrictApproval.id).where(StrictApproval.run_id == run_id))


def test_new_derivation_reestablishes_a_released_security_source(database):
    from trackvance.governance_models import DatasetSecurityDependency
    from trackvance.models import AuditEvent

    with database() as db:
        child, source = Dataset(name="Materialized"), Dataset(name="Independent source")
        db.add_all([child, source])
        db.flush()
        add_security_dependency(db, child.id, source.id)
        dependency = db.scalar(select(DatasetSecurityDependency))
        dependency.active, dependency.version = False, 2
        db.flush()
        add_security_dependency(db, child.id, source.id)
        assert dependency.active and dependency.version == 3
        assert db.scalar(select(AuditEvent.id).where(AuditEvent.event_type == "SECURITY_DEPENDENCY_REESTABLISHED"))


def test_explicit_null_metadata_is_rejected_but_classification_can_be_cleared(approved, authenticated):
    classify(authenticated, approved["dataset"])
    path = f"/api/v1/datasets/{approved['dataset']}/governance"
    for field in ("description", "criticality", "information_classification"):
        response = authenticated.patch(path, json={"expected_version": 2, field: None})
        assert response.status_code == 422, response.text
    cleared = authenticated.patch(path, json={"expected_version": 2, "macro_domain_id": None, "domain_id": None})
    assert cleared.status_code == 200 and not cleared.json()["governance"]["classification_complete"]
