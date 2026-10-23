"""Provider isolation, immutable snapshots and independent approval controls."""
import copy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from trackvance.document_engine import cross_table_checks, extract
from trackvance.document_models import (
    DocumentExtraction,
    DocumentFamily,
    DocumentOriginal,
    DocumentTemplate,
)
from trackvance.document_providers import (
    EngineConfiguration,
    EngineSettingsBody,
    execute_provider,
    freeze_configuration,
    save_settings,
    settings_dto,
)
from trackvance.document_quality import (
    certify_candidate,
    quality_report,
    validate_candidate_contract,
)
from trackvance.document_schemas import DocumentDefinition
from trackvance.document_service import digest, issues
from trackvance.models import AuditEvent, DatasetVersion, Job, Role, RolePermission, User
from trackvance.operations_common import OperationError


def definition():
    return DocumentDefinition.model_validate({"fields": [{"name": "invoice", "label": "Invoice",
        "pattern": r"([A-Z0-9-]+)", "business_key": True, "critical": True}],
        "publication": {"review_policy": "AUTO_VERIFIED"}}).model_dump(mode="json")


def candidate(config=None):
    config = config or definition()
    lines = [{"text": "Invoice: A-001", "page": 1, "region": [.1, .1, .8, .2],
        "method": "DIGITAL", "confidence": None}]
    return {"payload": extract(lines, config), "engine": {"key": "TEST_INDEPENDENT_LEGACY"}}


def extracted(data):
    return SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False)


def test_auto_verified_requires_independent_worker_proof_not_confidence():
    config = definition()
    data = candidate(config)
    data["payload"]["fields"]["invoice"]["confidence"] = 100
    assert issues(extracted(data), config) == ["REVIEW_REQUIRED:headers.0.invoice"]
    control = candidate(config)
    data["engine"]["validation"] = certify_candidate(data, control, config, "a" * 64)
    assert quality_report(extracted(data), config)["status"] == "AUTO_VERIFIED"
    assert not issues(extracted(data), config)
    assert data["payload"]["fields"]["invoice"]["reviewed"] is False
    changed = copy.deepcopy(data)
    changed["payload"]["fields"]["invoice"]["value"] = "FABRICATED"
    assert issues(extracted(changed), config)
    changed = copy.deepcopy(data)
    changed["engine"]["validation"]["source_sha256"] = "b" * 64
    assert issues(extracted(changed), config)  # The attestation digest binds source provenance.


@pytest.mark.parametrize("mutation", ["human_review", "unknown_field", "geometry", "nan", "extra_table", "self_resolution"])
def test_provider_candidates_cannot_claim_review_or_escape_contract(mutation):
    config, data = definition(), candidate()
    cell = data["payload"]["fields"]["invoice"]
    if mutation == "human_review":
        cell["reviewed"] = True
    elif mutation == "unknown_field":
        data["payload"]["fields"]["unknown"] = copy.deepcopy(cell)
    elif mutation == "geometry":
        cell["origin"]["region"] = [0, 0, 2, 1]
    elif mutation == "nan":
        cell["confidence"] = float("nan")
    elif mutation == "extra_table":
        data["payload"]["tables"]["invented"] = []
    else:
        data["payload"]["header_resolutions"] = []
    with pytest.raises(OperationError, match="contrato") as error:
        validate_candidate_contract(data, config)
    assert error.value.code == "DOCUMENT_PROVIDER_CONTRACT"


def test_provider_dispatch_never_falls_back_to_external_or_accepts_model_attestation(tmp_path):
    source, config = tmp_path / "source.pdf", definition()
    source.write_bytes(b"%PDF-synthetic")
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        data = candidate(config)
        data["engine"]["validation"] = {"status": "AUTO_VERIFIED"}
        return data

    result = execute_provider(source, config, EngineConfiguration().model_dump(), legacy_runner=runner, check=lambda _: None)
    assert "validation" not in result["engine"] and result["engine"]["provider"] == "TESSERACT_LEGACY"
    external = EngineConfiguration(provider="AWS_TEXTRACT").model_dump()
    with pytest.raises(OperationError) as error:
        execute_provider(source, config, external, legacy_runner=runner, check=lambda _: None)
    assert error.value.code == "DOCUMENT_EXTERNAL_PROVIDER_DISABLED" and len(calls) == 1


def test_independent_disagreement_and_fabricated_observation_require_review():
    config, data, control = definition(), candidate(), candidate()
    control["payload"]["fields"]["invoice"]["value"] = "A-002"
    data["engine"]["validation"] = certify_candidate(data, control, config, "a" * 64)
    assert "REVIEW_REQUIRED:headers.0.invoice" in issues(extracted(data), config)
    data = candidate()
    cell = data["payload"]["fields"]["invoice"]
    cell["observed"] = cell["value"] = "NOT-ON-PAGE"
    control = copy.deepcopy(data)
    data["engine"]["validation"] = certify_candidate(data, control, config, "a" * 64)
    assert issues(extracted(data), config)


def test_exact_cross_totals_and_historical_definition_serialization():
    historical = DocumentDefinition.model_validate({"fields": [{"name": "invoice"}]}).model_dump(mode="json")
    assert "cross_totals" not in historical
    config = DocumentDefinition.model_validate({"fields": [{"name": "total", "logical_type": "DECIMAL"}],
        "tables": [{"name": "details", "row_pattern": r"(?P<amount>\d+\.\d+)",
            "fields": [{"name": "amount", "logical_type": "DECIMAL"}]}],
        "cross_totals": [{"name": "details_total", "table": "details", "column": "amount", "header": "total"}]}).model_dump(mode="json")

    def cell(value):
        return {"value": value, "validity": "VALID"}

    payload = {"fields": {"total": cell("0.30")}, "tables": {"details": [
        {"amount": cell("0.10")}, {"amount": cell("0.20")}]}}
    assert cross_table_checks(payload, config)[0]["status"] == "VALID"
    assert cross_table_checks(payload, config)[0]["expected"] == "0.30"
    payload["fields"]["total"]["value"] = "0.32"
    assert cross_table_checks(payload, config)[0]["status"] == "DISCREPANCY"
    payload["tables"]["details"] = []
    assert cross_table_checks(payload, config)[0]["status"] == "NOT_EVALUATED"
    bad = copy.deepcopy(config)
    bad["cross_totals"][0]["column"] = "missing"
    with pytest.raises(ValidationError):
        DocumentDefinition.model_validate(bad)


def test_decimal_without_declared_arithmetic_cannot_autoapprove():
    config = DocumentDefinition.model_validate({"fields": [{"name": "total", "label": "Invoice",
        "pattern": r"([0-9.]+)", "logical_type": "DECIMAL"}], "publication": {"review_policy": "AUTO_VERIFIED"}}).model_dump(mode="json")
    lines = [{"text": "Invoice: 1.20", "page": 1, "region": [0, 0, 1, 1], "method": "DIGITAL", "confidence": 100}]
    data = {"payload": extract(lines, config), "engine": {"key": "CONTROL"}}
    data["engine"]["validation"] = certify_candidate(data, copy.deepcopy(data), config, "a" * 64)
    assert issues(extracted(data), config) == ["REVIEW_REQUIRED:headers.0.total"]
    config["arithmetic"] = [{"name": "tautology", "table": None, "operation": "SUM", "terms": ["total"],
        "subtract": [], "result": "total", "scale": 2, "tolerance": "0.01"}]
    data = {"payload": extract(lines, config), "engine": {"key": "CONTROL"}}
    data["engine"]["validation"] = certify_candidate(data, copy.deepcopy(data), config, "a" * 64)
    assert issues(extracted(data), config) == ["REVIEW_REQUIRED:headers.0.total"]


@pytest.mark.parametrize("conflict_kind", ["FIELD", "REEXTRACTION_FIELD"])
def test_semantic_disagreement_remains_an_exception_until_human_confirmation(conflict_kind):
    config, data = definition(), candidate()
    data["payload"]["semantic_conflicts"] = [conflict_kind + ":invoice"]
    data["engine"]["validation"] = certify_candidate(data, candidate(), config, "a" * 64)
    item = extracted(data)
    assert "SEMANTIC_CONFLICT:" + conflict_kind + ":invoice" in issues(item, config)
    item.reviewed = True
    item.payload["fields"]["invoice"]["reviewed"] = True
    assert not issues(item, config)


def test_regex_tables_and_missing_rows_cannot_use_confidence_as_coverage():
    config = DocumentDefinition.model_validate({"fields": [{"name": "invoice", "label": "Invoice", "pattern": r"([A-Z0-9-]+)"}],
        "tables": [{"name": "details", "row_pattern": r"ITEM (?P<sku>\S+)", "fields": [{"name": "sku"}]}],
        "publication": {"review_policy": "AUTO_VERIFIED"}}).model_dump(mode="json")
    lines = [*candidate()["payload"]["lines"],
        {"text": "ITEM X1", "page": 1, "region": [.1, .3, .8, .4], "method": "DIGITAL", "confidence": 100},
        {"text": "ITEM X2", "page": 1, "region": [.1, .5, .8, .6], "method": "DIGITAL", "confidence": 100}]
    control = {"payload": extract(lines, config), "engine": {"key": "CONTROL"}}
    data = copy.deepcopy(control)
    data["payload"]["tables"]["details"].pop()
    data["engine"]["validation"] = certify_candidate(data, control, config, "a" * 64)
    assert "QUALITY_ROW_COVERAGE:details" in issues(extracted(data), config)
    assert quality_report(extracted(data), config)["status"] == "REVIEW_REQUIRED"


def test_settings_are_admin_only_organization_scoped_optimistic_and_audited(authenticated, database):
    response = authenticated.get("/api/v1/documents/engines")
    assert response.status_code == 200 and response.json()["version"] == 0
    assert response.json()["can_configure"]
    assert next(item for item in response.json()["providers"] if item["id"] == "AWS_TEXTRACT")["execution_enabled"] is False
    body = {"expected_version": 0, "configuration": {"provider": "TESSERACT_LEGACY", "timeout_seconds": 180}, "reason": "Synthetic administration test"}
    response = authenticated.put("/api/v1/documents/engines", json=body)
    assert response.status_code == 200 and response.json()["version"] == 1
    assert authenticated.put("/api/v1/documents/engines", json=body).status_code == 409
    body["expected_version"] = 1
    body["configuration"]["provider"] = "AWS_TEXTRACT"
    assert authenticated.put("/api/v1/documents/engines", json=body).status_code == 422
    with database() as db:
        user = db.get(User, "test-user")
        changes = db.scalars(select(AuditEvent).where(AuditEvent.event_type == "DOCUMENT_ENGINE_CONFIGURED")).all()
        assert len(changes) == 1 and changes[0].organization_id == user.organization_id
        assert changes[0].message.endswith("Synthetic administration test")
        assert changes[0].metadata_json["provider"] == "TESSERACT_LEGACY"
        assert changes[0].metadata_json["timeout_seconds"] == 180 and changes[0].metadata_json["config_hash"]
        role = Role(name="Document configurator", normalized_name="document configurator", organization_id=user.organization_id, active=True)
        db.add(role)
        db.flush()
        db.add_all([RolePermission(role_id=role.id, permission_code=code) for code in ("documents:read", "documents:configure")])
        user.role_id = role.id
        db.commit()
    body["configuration"]["provider"] = "TESSERACT_LEGACY"
    assert authenticated.put("/api/v1/documents/engines", json=body).status_code == 403


def test_family_override_freezes_at_start_and_retries_keep_original_snapshot(database):
    with database() as db:
        user = db.get(User, "test-user")
        family = DocumentFamily(name="Snapshot family", responsible_user_id=user.id)
        db.add(family)
        db.flush()
        save_settings(db, user, EngineSettingsBody(expected_version=0, configuration=EngineConfiguration(timeout_seconds=180), reason="First org config"))
        subject = SimpleNamespace(organization_id=user.organization_id, family_id=family.id, engine_snapshot=None, engine_snapshot_hash=None)
        frozen = freeze_configuration(db, subject)
        assert frozen["timeout_seconds"] == 180 and frozen["configuration_version"] == 1
        save_settings(db, user, EngineSettingsBody(expected_version=1, configuration=EngineConfiguration(timeout_seconds=240), reason="New work config"))
        assert freeze_configuration(db, subject) == frozen
        inherited = settings_dto(db, user, family.id)
        assert inherited["version"] == 0 and inherited["inherited_version"] == 2
        save_settings(db, user, EngineSettingsBody(expected_version=0, configuration=EngineConfiguration(timeout_seconds=360), reason="Family override"), family.id)
        other = SimpleNamespace(organization_id=user.organization_id, family_id=family.id, engine_snapshot=None, engine_snapshot_hash=None)
        assert freeze_configuration(db, other)["timeout_seconds"] == 360
        subject.engine_snapshot["timeout_seconds"] = 900
        with pytest.raises(OperationError) as error:
            freeze_configuration(db, subject)
        assert error.value.code == "DOCUMENT_ENGINE_SNAPSHOT_INTEGRITY"
        with pytest.raises(OperationError) as error:
            settings_dto(db, SimpleNamespace(organization_id="another-organization"), family.id)
        assert error.value.status == 404


@pytest.mark.parametrize("control_failure", [False, True])
def test_auto_verified_worker_preserves_candidate_when_control_fails_and_publishes_once_when_valid(database, tmp_path, monkeypatch, control_failure):
    from trackvance import document_worker
    from trackvance.artifactstore import storage_provider
    from trackvance.db import utcnow

    config = definition()
    source = tmp_path / "test.pdf"
    source.write_bytes(b"%PDF-synthetic-controlled-provider-double")
    with database() as db:
        user = db.get(User, "test-user")
        family = DocumentFamily(name="Automatic native publication", responsible_user_id=user.id)
        db.add(family)
        db.flush()
        template = DocumentTemplate(family_id=family.id, revision=1, definition=config,
            definition_hash=digest(config), created_by_id=user.id)
        db.add(template)
        db.flush()
        family.active_template_id = template.id
        artifact = storage_provider.put_file(db, source, "DOCUMENT_ORIGINAL", user.organization_id)
        original = DocumentOriginal(family_id=family.id, template_id=template.id, artifact_id=artifact.id,
            filename="test.pdf", sha256=artifact.sha256, received_at=utcnow(), uploaded_by_id=user.id)
        db.add(original)
        db.flush()
        identity = original.id
        db.add(Job(document_id=identity, lane="DOCUMENT"))
        db.commit()
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        kwargs["check"](80)
        if control_failure and len(calls) == 2:
            raise OperationError(422, "DOCUMENT_TIMEOUT", "Synthetic independent control timeout")
        return candidate(config)

    monkeypatch.setattr(document_worker, "run_engine", runner)
    assert document_worker.process_once("idp-worker-test")
    with database() as db:
        original = db.get(DocumentOriginal, identity)
        extraction = db.get(DocumentExtraction, original.extraction_id)
        assert original.status == ("REVIEW_REQUIRED" if control_failure else "PUBLISHED")
        assert original.engine_snapshot["provider"] == "TESSERACT_LEGACY"
        assert not extraction.reviewed and all(not cell["reviewed"] for cell in extraction.payload["fields"].values())
        versions = db.scalars(select(DatasetVersion)).all()
        if control_failure:
            assert extraction.engine["validation_failure"]["automatic_approval_blocked"]
            assert not versions
        else:
            assert extraction.engine["validation"]["independent_extraction"]
            assert len(versions) == 1 and versions[0].row_count == 1
            assert versions[0].ingestion_metadata["document_publication"]["family_id"] == original.family_id
    assert len(calls) == 2
    assert not document_worker.process_once("idp-worker-test")
