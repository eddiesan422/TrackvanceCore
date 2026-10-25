"""Actual worker/certifier boundaries with synthetic readers, never recognition."""
import copy
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_document_candidate_arbitration_098 import fixture

from trackvance import document_quality as quality
from trackvance.document_candidate_arbitration import digest
from trackvance.document_models import (
    DocumentExtraction,
    DocumentFamily,
    DocumentOriginal,
    DocumentPublicationClaim,
    DocumentTemplate,
)
from trackvance.document_providers import EngineConfiguration, execute_provider
from trackvance.document_schemas import DocumentDefinition
from trackvance.models import AuditEvent, Job, User
from trackvance.operations_common import OperationError


def bound_fixture():
    data, independent, definition = fixture()
    configuration = EngineConfiguration(provider="PADDLE_QWEN_LOCAL").model_dump()
    data["engine"].update(provider="PADDLE_QWEN_LOCAL", configuration=configuration,
                          configuration_sha256=digest(configuration))
    return data, independent, definition


def test_certifier_holds_actual_control_revalidates_then_hashes_final_selected_payload():
    data, independent, definition = bound_fixture()
    data["engine"]["route"] = "DIGITAL_NATIVE"  # A provider route claim cannot certify this selector's unknown coverage.
    prior = copy.deepcopy(data)
    proof = quality.certify_candidate(data, independent, definition, "a" * 64)
    assert data["payload"]["tables"]["detail"][0]["qty"]["value"] == "3.50"
    assert data["payload"]["reextraction_table_history"] == prior["payload"]["reextraction_table_history"]
    assert data["payload"]["semantic_conflicts"] == prior["payload"]["semantic_conflicts"]
    history = data["payload"]["independent_cell_arbitration_history"]
    assert len(history) == 1 and len(history[0]["selected"]) == 4
    assert history[0]["before_payload_sha256"] == digest(prior["payload"])
    assert proof["candidate_sha256"] == digest(data["payload"])
    assert not proof["table_coverage"]["detail"]
    data["engine"]["validation"] = proof
    assert quality.quality_report(SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False), definition)["status"] == "REVIEW_REQUIRED"
    assert all(not cell["reviewed"] for row in data["payload"]["tables"]["detail"] for cell in row.values())


def test_no_history_default_and_partial_marker_preserve_exact_original_payload():
    for partial in (False, True):
        data, independent, definition = bound_fixture()
        if partial:
            data["engine"]["inference_completion"] = {"status": "PARTIAL", "checkpoint_payload_sha256": digest(data["payload"])}
        else:
            del data["payload"]["reextraction_table_history"]
        prior = copy.deepcopy(data)
        quality.certify_candidate(data, independent, definition, "a" * 64)
        assert data == prior


@pytest.mark.parametrize("mutation", ["engine_marker", "history_marker", "cell_marker"])
def test_provider_cannot_stamp_worker_arbitration_or_history(mutation):
    data, _, definition = bound_fixture()
    if mutation == "engine_marker":
        data["engine"]["independent_cell_arbitration"] = {"selected": ["fake"]}
    elif mutation == "history_marker":
        data["payload"]["independent_cell_arbitration_history"] = [{"authority": "fake"}]
    else:
        data["payload"]["tables"]["detail"][0]["qty"]["independent_selection_version"] = "fake"
    with pytest.raises(OperationError) as error:
        execute_provider(None, definition, {"provider": "TESSERACT_LEGACY"},
                         legacy_runner=lambda *args, **kwargs: data, check=lambda _: None)
    assert error.value.code == "DOCUMENT_PROVIDER_CONTRACT"


def test_invalid_actual_control_rejected_before_arbitration_without_mutating_candidate():
    data, independent, definition = bound_fixture()
    independent["payload"]["tables"]["detail"][0]["qty"]["reviewed"] = True
    prior = copy.deepcopy(data)
    with pytest.raises(OperationError) as error:
        quality.certify_candidate(data, independent, definition, "a" * 64)
    assert error.value.code == "DOCUMENT_PROVIDER_CONTRACT" and data == prior


def test_deadline_after_selection_but_before_attestation_keeps_input_exact(monkeypatch):
    data, independent, definition = bound_fixture()
    prior = copy.deepcopy(data)
    real = quality.reconcile_worker_candidate
    state = {"selected": False}

    def observed_selection(*args):
        result = real(*args)
        state["selected"] = bool(result[1] and result[1].get("selected"))
        return result

    def checkpoint():
        if state["selected"]:
            raise OperationError(422, "DOCUMENT_CANCELLED", "Synthetic late authority failure")

    monkeypatch.setattr(quality, "reconcile_worker_candidate", observed_selection)
    with pytest.raises(OperationError) as error:
        quality.certify_candidate(data, independent, definition, "a" * 64, checkpoint)
    assert state["selected"] and error.value.code == "DOCUMENT_CANCELLED" and data == prior


def test_declared_financial_guards_cannot_be_omitted_by_callers():
    from test_document_candidate_arbitration_098 import run

    data, independent, definition = fixture()
    definition["arithmetic"] = [{"name": "row", "table": "detail", "result": "amount", "terms": ["qty"],
                               "operation": "SUM", "scale": 2, "tolerance": "0.00"}]
    result, report = run(data, independent, definition, declared_checks=None)
    assert result == data and not report["selected"]
    assert report["rejected"][0]["reason"] == "DECLARED_NONREGRESSION_CONTROLS_UNAVAILABLE"


def test_actual_worker_commits_selected_evidence_but_never_claims_human_or_automatic_approval(database, tmp_path, monkeypatch):
    from trackvance import document_worker
    from trackvance.artifactstore import storage_provider
    from trackvance.db import utcnow

    data, independent, raw = bound_fixture()
    raw["fields"] = [{"name": "document", "label": "Document", "pattern": r"([A-Z0-9-]+)", "business_key": True}]
    raw["tables"][0]["visual"].update(region=[.02, .18, .98, .23], header_labels=["ITEM", "QTY", "AMOUNT"])
    raw["publication"] = {"review_policy": "AUTO_VERIFIED"}
    definition = DocumentDefinition.model_validate(raw).model_dump(mode="json")
    header = {"value": "SYNTHETIC-1", "observed": "SYNTHETIC-1", "validity": "VALID", "confidence": None,
              "origin": {"page": 1, "region": [.1, .1, .3, .12], "method": "OCR"}, "reviewed": False}
    for result in (data, independent):
        result["payload"]["fields"] = {"document": copy.deepcopy(header)}
        result["payload"]["lines"].append({"text": "SYNTHETIC-1", "page": 1, "region": [.1, .1, .3, .12],
            "method": "OCR", "words": [{"text": "SYNTHETIC-1", "region": [.1, .1, .3, .12]}]})
    configuration = data["engine"]["configuration"]
    source = tmp_path / "controlled-synthetic-source.pdf"
    source.write_bytes(b"%PDF-software-only-independent-arbitration-fixture")
    with database() as db:
        user = db.get(User, "test-user")
        family = DocumentFamily(name="Independent cell worker fixture", responsible_user_id=user.id)
        db.add(family)
        db.flush()
        template = DocumentTemplate(family_id=family.id, revision=1, definition=definition,
            definition_hash=digest(definition), created_by_id=user.id)
        db.add(template)
        db.flush()
        family.active_template_id = template.id
        artifact = storage_provider.put_file(db, source, "DOCUMENT_ORIGINAL", user.organization_id)
        original = DocumentOriginal(family_id=family.id, template_id=template.id, artifact_id=artifact.id,
            filename=source.name, sha256=artifact.sha256, received_at=utcnow(), uploaded_by_id=user.id,
            engine_snapshot=configuration, engine_snapshot_hash=digest(configuration))
        db.add(original)
        db.flush()
        identity = original.id
        db.add(Job(document_id=identity, lane="DOCUMENT"))
        db.commit()
    calls = []

    def provider(*args, **kwargs):
        kwargs["check"](30)
        return copy.deepcopy(data)

    def control(*args, **kwargs):
        calls.append("actual-control-boundary")
        kwargs["check"](80)
        return copy.deepcopy(independent)

    monkeypatch.setattr(document_worker, "execute_provider", provider)
    monkeypatch.setattr(document_worker, "run_engine", control)
    assert document_worker.process_once("synthetic-arbitration-worker")
    with database() as db:
        original = db.get(DocumentOriginal, identity)
        extraction = db.get(DocumentExtraction, original.extraction_id)
        job = db.scalar(select(Job).where(Job.document_id == identity))
        assert original.status == "REVIEW_REQUIRED" and job.status == "SUCCESS" and job.lease_until is None
        assert extraction.revision == 1 and extraction.reviewed is False
        assert extraction.payload["tables"]["detail"][0]["qty"]["value"] == "3.50"
        assert len(extraction.payload["independent_cell_arbitration_history"]) == 1
        assert extraction.engine["validation"]["candidate_sha256"] == extraction.payload_hash
        assert not extraction.engine["validation"]["table_coverage"]["detail"]
        assert not db.scalars(select(AuditEvent).where(AuditEvent.event_type == "DOCUMENT_AUTO_VERIFIED")).all()
        assert not db.scalar(select(DocumentPublicationClaim.id).where(DocumentPublicationClaim.document_id == identity).limit(1))
        assert db.get(DocumentFamily, original.family_id).output_datasets == {}
    assert calls == ["actual-control-boundary"]
    assert not document_worker.process_once("synthetic-arbitration-worker")
