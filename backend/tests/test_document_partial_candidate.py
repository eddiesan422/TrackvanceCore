"""Private integration candidate: synthetic records only, no OCR/model/network."""
import copy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from trackvance import document_local_inference as local
from trackvance.document_engine import extract
from trackvance.document_inference_completion import (
    AttemptBudget,
    DocumentDeadlineExpired,
    canonical_hash,
    make_marker,
    marker_hash,
)
from trackvance.document_models import (
    DocumentExtraction,
    DocumentFamily,
    DocumentOriginal,
    DocumentTemplate,
)
from trackvance.document_providers import EngineConfiguration, execute_provider
from trackvance.document_quality import certify_candidate, quality_report
from trackvance.document_schemas import DocumentDefinition
from trackvance.document_service import digest, issues, preserve_extraction
from trackvance.models import AuditEvent, DatasetVersion, Job, User
from trackvance.operations_common import OperationError


def definition(policy="AUTO_VERIFIED", optional=False):
    fields = [{"name": "invoice", "label": "Invoice", "pattern": r"([A-Z0-9-]+)",
        "business_key": True, "critical": True}]
    if optional:
        fields.append({"name": "optional", "pattern": "Optional: (.*)", "required": False})
    return DocumentDefinition.model_validate({"fields": fields,
        "publication": {"review_policy": policy}}).model_dump(mode="json")


def candidate(spec=None):
    spec = spec or definition()
    lines = [{"text": "Invoice: SYNTHETIC-1", "page": 1, "region": [.1, .1, .8, .2],
        "method": "DIGITAL", "confidence": None}]
    return {"payload": extract(lines, spec), "engine": {"key": "SYNTHETIC_CONTROL", "pages": 2}}


def partial_marker(data, spec, configuration, source_sha="a" *64):
    return make_marker(payload=data["payload"], stage="REFINE", source_sha256=source_sha,
        definition=spec, configuration=configuration, pages=[1, 2], request_id="b" *32,
        completed_stages=[{"stage": "PADDLE_INITIAL", "engine": {"phases": {"paddle_seconds": .1}}}])


def local_fixture(tmp_path, monkeypatch, *, failure="REFINE", error=None, useful=True):
    spec = definition()
    spec["fields"].append({**spec["fields"][0], "name": "missing", "business_key": False})
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"synthetic source-bound transport fixture")
    calls = []

    def runner(*args, **kwargs):
        data = candidate(spec)
        if not useful:
            data["payload"]["fields"]["invoice"].update(value=None, observed=None, validity="MISSING")
        data["payload"]["fields"]["missing"].update(value=None, observed=None, validity="MISSING")
        return data

    def invoke(path, body, **kwargs):
        calls.append(path)
        if path == "/extract":
            if failure == "INITIAL":
                raise error or local.RemoteInferenceTimeout()
            return {"source_sha256": body["source_sha256"], "lines": candidate(spec)["payload"]["lines"],
                "engine": {"key": "REAL_TEST_RAW", "runtime_provenance": {"gateway": {"revision": "synthetic"}}}}
        if path == "/interpret" and failure != "INTERPRET":
            return {"source_sha256": body["source_sha256"], "observations_sha256": body["observations_sha256"],
                "semantics": {"fields": {name: {"observed": None, "evidence_id": None}
                    for name in ["invoice", "missing"]}, "tables": {}}, "engine": {}}
        raise error or local.OptionalInferenceTimeout(body["request_id"])

    monkeypatch.setattr(local, "_invoke", invoke)
    monkeypatch.setattr(local, "focused_regions", lambda *args: [{"page": 1, "region": [.1, .1, .8, .3]}])
    return source, spec, runner, calls


@pytest.mark.parametrize("stage", ["REFINE", "INTERPRET"])
def test_optional_typed_timeout_retains_validated_source_candidate(tmp_path, monkeypatch, stage):
    source, spec, runner, calls = local_fixture(tmp_path, monkeypatch, failure=stage)
    configuration = EngineConfiguration(provider="PADDLE_QWEN_LOCAL", prompt_version=local.LEGACY_PROMPT_VERSION).model_dump()
    result = execute_provider(source, spec, configuration, legacy_runner=runner, check=lambda _: None)
    marker = result["engine"]["inference_completion"]
    assert marker["failed_stage"] == stage and marker["source_pages"] == [1, 2]
    assert marker["checkpoint_payload_sha256"] == canonical_hash(result["payload"])
    assert result["payload"]["fields"]["invoice"]["value"] == "SYNTHETIC-1"
    assert result["payload"]["fields"]["missing"]["value"] is None
    assert result["payload"]["inference_observation_history"][-1]["status"] == "INCOMPLETE"
    assert result["engine"]["runtime_provenance"]["gateway"]["revision"] == "synthetic"
    assert not any(cell["reviewed"] for cell in result["payload"]["fields"].values())
    assert calls.count("/extract") == 1 and calls.count("/refine") <= 1


@pytest.mark.parametrize("stage,error", [
    ("INITIAL", local.RemoteInferenceTimeout()),
    ("INTERPRET", OperationError(422, "DOCUMENT_TIMEOUT", "callback/sandbox timeout")),
    ("INTERPRET", OperationError(409, "DOCUMENT_CANCELLED", "cancelled")),
    ("REFINE", OperationError(409, "DOCUMENT_LEASE_LOST", "lease lost")),
    ("INTERPRET", OperationError(422, "DOCUMENT_LLM_EVIDENCE_INVALID", "invalid selection")),
    ("REFINE", OperationError(422, "DOCUMENT_LOCAL_INFERENCE_CONTRACT", "wrong source")),
    ("INTERPRET", OperationError(422, "DOCUMENT_LOCAL_AUDIT_LIMIT", "audit overflow")),
    ("INTERPRET", OperationError(422, "DOCUMENT_LLM_CONTEXT_LIMIT", "context insufficient")),
])
def test_nonoptional_or_wrong_origin_errors_remain_fatal(tmp_path, monkeypatch, stage, error):
    source, spec, runner, _ = local_fixture(tmp_path, monkeypatch, failure=stage, error=error)
    with pytest.raises(OperationError) as caught:
        local.run_local(source, spec, {"prompt_version": local.LEGACY_PROMPT_VERSION}, legacy_runner=runner, check=lambda _: None)
    assert caught.value is error


def test_blank_initial_candidate_has_no_recovery(tmp_path, monkeypatch):
    source, spec, runner, _ = local_fixture(tmp_path, monkeypatch, failure="INTERPRET", useful=False)
    with pytest.raises(local.OptionalInferenceTimeout):
        local.run_local(source, spec, {}, legacy_runner=runner, check=lambda _: None)


@pytest.mark.parametrize("pages", [None, 0, True])
def test_unverified_native_page_count_cannot_support_partial_recovery(tmp_path, monkeypatch, pages):
    source, spec, real_runner, _ = local_fixture(tmp_path, monkeypatch, failure="INTERPRET")

    def runner(*args, **kwargs):
        result = real_runner(*args, **kwargs)
        result["engine"]["pages"] = pages
        return result

    with pytest.raises(local.OptionalInferenceTimeout):
        local.run_local(source, spec, {}, legacy_runner=runner, check=lambda _: None)


def test_cancellation_racing_timeout_wins_before_partial_return(tmp_path, monkeypatch):
    source, spec, runner, _ = local_fixture(tmp_path, monkeypatch, failure="INTERPRET")
    def check(_):
        raise OperationError(409, "DOCUMENT_CANCELLED", "cancelled at recovery fence")
    with pytest.raises(OperationError) as caught:
        local.run_local(source, spec, {}, legacy_runner=runner, check=check)
    assert caught.value.code == "DOCUMENT_CANCELLED"


def test_partial_uses_deep_copy_of_accepted_candidate_not_mutated_failed_stage(tmp_path, monkeypatch):
    source, spec, real_runner, _ = local_fixture(tmp_path, monkeypatch, failure="INTERPRET")
    last = {}
    def runner(*args, **kwargs):
        result = real_runner(*args, **kwargs)
        last["result"] = result
        return result
    invoke = local._invoke
    def mutate(path, body, **kwargs):
        if path == "/interpret":
            last["result"]["payload"]["fields"]["invoice"]["value"] = "MUTATED-UNACCEPTED"
        return invoke(path, body, **kwargs)
    monkeypatch.setattr(local, "_invoke", mutate)
    result = local.run_local(source, spec, {}, legacy_runner=runner, check=lambda _: None)
    assert result["payload"]["fields"]["invoice"]["value"] == "SYNTHETIC-1"


def test_changed_source_during_optional_timeout_cannot_recover(tmp_path, monkeypatch):
    source, spec, runner, _ = local_fixture(tmp_path, monkeypatch, failure="INTERPRET")
    invoke = local._invoke
    def mutate(path, body, **kwargs):
        if path == "/interpret":
            source.write_bytes(b"different synthetic original")
        return invoke(path, body, **kwargs)
    monkeypatch.setattr(local, "_invoke", mutate)
    with pytest.raises(OperationError) as caught:
        local.run_local(source, spec, {}, legacy_runner=runner, check=lambda _: None)
    assert caught.value.code == "DOCUMENT_LOCAL_INFERENCE_CONTRACT"


def test_actual_total_deadline_expiring_with_optional_timeout_is_fatal(tmp_path, monkeypatch):
    source, spec, runner, _ = local_fixture(tmp_path, monkeypatch, failure="INTERPRET")
    clock = [0.0]
    budget = AttemptBudget(30, clock=lambda: clock[0])
    invoke = local._invoke
    def expire(path, body, **kwargs):
        if path == "/interpret":
            clock[0] = 31
        return invoke(path, body, **kwargs)
    monkeypatch.setattr(local, "_invoke", expire)
    with pytest.raises(DocumentDeadlineExpired):
        local.run_local(source, spec, {}, legacy_runner=runner, check=lambda _: None, budget=budget)


def test_ordinary_provider_engine_cannot_forge_partial_or_manual_resolution(tmp_path):
    spec = definition()
    data = candidate(spec)
    data["engine"]["inference_completion"] = {"status": "HUMAN_RESOLVED", "automatic_approval_blocked": False}
    with pytest.raises(OperationError) as caught:
        execute_provider(tmp_path / "unused", spec, EngineConfiguration().model_dump(),
            legacy_runner=lambda *args, **kwargs: data, check=lambda _: None)
    assert caught.value.code == "DOCUMENT_PROVIDER_CONTRACT"
    data["engine"].pop("inference_completion")
    data["payload"]["inference_completion_resolution"] = {"actor_id": "forged"}
    with pytest.raises(OperationError) as caught:
        execute_provider(tmp_path / "unused", spec, EngineConfiguration().model_dump(),
            legacy_runner=lambda *args, **kwargs: data, check=lambda _: None)
    assert caught.value.code == "DOCUMENT_PROVIDER_CONTRACT"


@pytest.mark.parametrize("policy", ["AUTO_VERIFIED", "ALL", "CRITICAL_OR_UNCERTAIN"])
def test_partial_blocks_all_policies_even_with_full_independent_proof(policy):
    spec, configuration = definition(policy), EngineConfiguration(provider="PADDLE_QWEN_LOCAL").model_dump()
    data = candidate(spec)
    data["engine"]["validation"] = certify_candidate(data, candidate(spec), spec, "a" *64)
    data["engine"]["inference_completion"] = partial_marker(data, spec, configuration)
    extraction = SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False)
    assert quality_report(extraction, spec)["status"] == "REVIEW_REQUIRED"
    assert "INFERENCE_INCOMPLETE:REFINE" in issues(extraction, spec)
    assert "REVIEW_REQUIRED:headers.0.invoice" in issues(extraction, spec)


@pytest.fixture
def partial_document(database, tmp_path):
    from trackvance.artifactstore import storage_provider
    from trackvance.db import utcnow
    spec = definition(optional=True)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"synthetic complete-source human-review fixture")
    configuration = EngineConfiguration(provider="PADDLE_QWEN_LOCAL").model_dump()
    with database() as db:
        user = db.get(User, "test-user")
        family = DocumentFamily(name="Private partial review", responsible_user_id=user.id)
        db.add(family)
        db.flush()
        template = DocumentTemplate(family_id=family.id, revision=1, definition=spec,
            definition_hash=digest(spec), created_by_id=user.id)
        db.add(template)
        db.flush()
        family.active_template_id = template.id
        artifact = storage_provider.put_file(db, source, "DOCUMENT_ORIGINAL", user.organization_id)
        original = DocumentOriginal(family_id=family.id, template_id=template.id, artifact_id=artifact.id,
            filename="source.pdf", sha256=artifact.sha256, received_at=utcnow(), uploaded_by_id=user.id,
            status="REVIEW_REQUIRED", engine_snapshot=configuration)
        db.add(original)
        db.flush()
        data = candidate(spec)
        data["engine"]["validation"] = certify_candidate(data, candidate(spec), spec, original.sha256)
        data["engine"]["inference_completion"] = partial_marker(data, spec, configuration, original.sha256)
        data["engine"]["inference_completion_sha256"] = marker_hash(data["engine"]["inference_completion"])
        extraction = DocumentExtraction(document_id=original.id, template_id=template.id, revision=1,
            payload=data["payload"], payload_hash=digest(data["payload"]), engine=data["engine"], reviewed=False, actor_id=user.id)
        db.add(extraction)
        db.flush()
        preserve_extraction(db, extraction)
        original.extraction_id = extraction.id
        result = {"id": original.id, "automatic_id": extraction.id, "automatic_payload": copy.deepcopy(extraction.payload),
            "marker": copy.deepcopy(data["engine"]["inference_completion"])}
        db.commit()
    return result


def confirm_cells(client, identity):
    original = client.get("/api/v1/documents/originals/" + identity).json()
    extraction = original["extraction"]
    paths = ["headers.0." + name for name in extraction["payload"]["fields"]]
    response = client.post("/api/v1/documents/originals/" + identity + "/review", json={
        "expected_extraction_id": extraction["id"], "confirmed": paths, "changes": {}, "reason": "Checked every synthetic source cell"})
    assert response.status_code == 200, response.text
    return response.json()


def acknowledge_body(original):
    return {"expected_extraction_id": original["extraction"]["id"], "changes": {}, "confirmed": [],
        "reason": "Checked all original pages and all rows, including omissions and absent optional values",
        "incomplete_source_confirmation": {**original["incomplete_inference"]["confirmation_binding"],
            "all_pages_confirmed": True, "all_rows_confirmed": True}}


def test_full_source_acknowledgment_is_manual_revision_and_preserves_automatic_marker(authenticated, database, partial_document):
    client, identity = authenticated, partial_document["id"]
    before = client.get("/api/v1/documents/originals/" + identity).json()
    assert before["incomplete_inference"]["status"] == "PARTIAL" and not before["incomplete_inference"]["can_confirm"]
    premature = client.post("/api/v1/documents/originals/" + identity + "/review", json=acknowledge_body(before))
    assert premature.status_code == 422
    cells_reviewed = confirm_cells(client, identity)
    assert cells_reviewed["incomplete_inference"]["can_confirm"]
    assert cells_reviewed["quality"]["status"] == "REVIEW_REQUIRED" and not cells_reviewed["publications"]
    response = client.post("/api/v1/documents/originals/" + identity + "/review", json=acknowledge_body(cells_reviewed))
    assert response.status_code == 200, response.text
    after = response.json()
    assert after["quality"]["status"] == "HUMAN_VERIFIED"
    assert after["incomplete_inference"]["status"] == "HUMAN_RESOLVED"
    assert "confirmation_binding" not in after["incomplete_inference"]
    assert after["extraction"]["revision"] == 3 and len(after["revisions"]) == 3
    listing = client.get("/api/v1/documents/originals").json()
    listed = next(item for item in listing["items"] if item["id"] == identity)
    assert listed["incomplete_inference"]["status"] == "HUMAN_RESOLVED"
    assert listed["incomplete_inference"]["requires_full_source_review"] is False
    assert listed["incomplete_inference"]["can_confirm"] is False
    assert after["extraction"]["engine"]["inference_completion"] == partial_document["marker"]
    resolution = after["extraction"]["payload"]["inference_completion_resolution"]
    assert resolution["actor_id"] == "test-user" and resolution["pages"] == [1, 2]
    assert resolution["marker_sha256"] == marker_hash(partial_document["marker"])
    with database() as db:
        automatic = db.get(DocumentExtraction, partial_document["automatic_id"])
        assert automatic.payload == partial_document["automatic_payload"] and not automatic.reviewed
        assert automatic.engine["inference_completion"] == partial_document["marker"]
        assert not db.scalars(select(AuditEvent).where(AuditEvent.event_type == "DOCUMENT_AUTO_VERIFIED")).all()
        assert len(db.scalars(select(AuditEvent).where(AuditEvent.event_type == "DOCUMENT_INCOMPLETE_SOURCE_CONFIRMED")).all()) == 1


def test_noop_review_does_not_resolve_or_publish(authenticated, database, partial_document):
    identity = partial_document["id"]
    original = authenticated.get("/api/v1/documents/originals/" + identity).json()
    response = authenticated.post("/api/v1/documents/originals/" + identity + "/review", json={
        "expected_extraction_id": original["extraction"]["id"], "reason": "No actual cell confirmation"})
    assert response.status_code == 200
    assert response.json()["quality"]["status"] == "REVIEW_REQUIRED"
    assert not response.json()["publications"] and not response.json()["incomplete_inference"]["can_confirm"]
    with database() as db:
        assert not db.scalars(select(DatasetVersion)).all()


def test_list_projects_only_lightweight_pending_completion(authenticated, partial_document):
    result = authenticated.get("/api/v1/documents/originals").json()
    item = next(item for item in result["items"] if item["id"] == partial_document["id"])
    assert item["incomplete_inference"] == {"status": "PARTIAL", "failed_stage": "REFINE",
        "error_code": "DOCUMENT_TIMEOUT", "automatic_approval_blocked": True,
        "requires_full_source_review": True, "can_confirm": False}
    assert "confirmation_binding" not in item["incomplete_inference"]
    assert "payload" not in item["extraction"] and "engine" not in item["extraction"]


def test_changing_a_resolved_revision_resets_source_ack_and_preserves_old_resolution(authenticated, database, partial_document):
    identity = partial_document["id"]
    reviewed = confirm_cells(authenticated, identity)
    resolved = authenticated.post("/api/v1/documents/originals/" + identity + "/review", json=acknowledge_body(reviewed)).json()
    assert resolved["incomplete_inference"]["status"] == "HUMAN_RESOLVED"
    resolved_id = resolved["extraction"]["id"]
    response = authenticated.post("/api/v1/documents/originals/" + identity + "/review", json={
        "expected_extraction_id": resolved_id, "changes": {"headers.0.invoice": "SYNTHETIC-CHANGED"},
        "confirmed": ["headers.0.invoice"], "reason": "Synthetic source correction requires renewed full-source review"})
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed["incomplete_inference"]["status"] == "PARTIAL"
    assert changed["quality"]["status"] == "REVIEW_REQUIRED"
    assert "inference_completion_resolution" not in changed["extraction"]["payload"]
    assert changed["incomplete_inference"]["confirmation_binding"]["expected_extraction_id"] != resolved_id
    with database() as db:
        assert db.get(DocumentExtraction, resolved_id).payload["inference_completion_resolution"]["actor_id"] == "test-user"


def test_full_source_ack_requires_server_download_permission(authenticated, partial_document, monkeypatch):
    from trackvance import document_service
    identity = partial_document["id"]
    original = confirm_cells(authenticated, identity)
    real_permission = document_service.permission
    def permission(db, user, action):
        if action == "download":
            raise OperationError(403, "DOCUMENT_FORBIDDEN", "Synthetic no source access")
        return real_permission(db, user, action)
    monkeypatch.setattr(document_service, "permission", permission)
    response = authenticated.post("/api/v1/documents/originals/" + identity + "/review", json=acknowledge_body(original))
    assert response.status_code == 403


def test_blank_full_source_reason_cannot_acknowledge(authenticated, partial_document):
    identity = partial_document["id"]
    body = acknowledge_body(confirm_cells(authenticated, identity))
    body["reason"] = "   "
    response = authenticated.post("/api/v1/documents/originals/" + identity + "/review", json=body)
    assert response.status_code == 422


@pytest.mark.parametrize("mutation,expected_status", [
    ("source", 409), ("payload", 409), ("marker", 409), ("cross_document", 409), ("missing_page", 409),
    ("stale", 409), ("false_page_ack", 422), ("forged_actor", 422), ("boolean_page", 422), ("mix_changes", 422),
])
def test_source_ack_rejects_stale_forged_or_incomplete_bindings(authenticated, partial_document, mutation, expected_status):
    identity = partial_document["id"]
    original = confirm_cells(authenticated, identity)
    body = acknowledge_body(original)
    ack = body["incomplete_source_confirmation"]
    if mutation in {"source", "payload", "marker"}:
        ack[{"source": "source_sha256", "payload": "expected_payload_hash", "marker": "expected_marker_hash"}[mutation]] = "f" *64
    elif mutation == "cross_document":
        ack["expected_extraction_id"] = "another-document-extraction"
    elif mutation == "missing_page":
        ack["pages"] = [1]
    elif mutation == "stale":
        confirm_cells(authenticated, identity)
    elif mutation == "false_page_ack":
        ack["all_pages_confirmed"] = False
    elif mutation == "forged_actor":
        ack["actor_id"] = "another-user"
    elif mutation == "boolean_page":
        ack["pages"] = [True, 2]
    else:
        body["changes"] = {"headers.0.invoice": "CHANGED"}
    response = authenticated.post("/api/v1/documents/originals/" + identity + "/review", json=body)
    assert response.status_code == expected_status, response.text
    after = authenticated.get("/api/v1/documents/originals/" + identity).json()
    assert after["quality"]["status"] == "REVIEW_REQUIRED" and not after["publications"]


def table_payload(rows):
    def cell(value):
        return {"value": value, "observed": value, "validity": "VALID", "reviewed": False}
    return {"fields": {}, "tables": {"detail": [{"sku": cell(key), "amount": cell(value)} for key, value in rows]}, "semantic_conflicts": []}


@pytest.mark.parametrize("proposed", [
    [("A", "1")], [("A", "1"), ("B", "2"), ("C", "3")], [("B", "2"), ("A", "1")],
    [("A", "9"), ("B", "2")],
])
def test_retry_table_preserves_all_prior_rows_and_keeps_proposal_separate(proposed):
    before, after = table_payload([("A", "1"), ("B", "2")]), table_payload(proposed)
    spec = {"tables": [{"name": "detail", "fields": [{"name": "sku", "business_key": True}, {"name": "amount"}]}]}
    result = local.retain_disagreements(copy.deepcopy(before), after, spec)
    assert [row["sku"]["value"] for row in result["tables"]["detail"]] == ["A", "B"]
    assert [row["amount"]["value"] for row in result["tables"]["detail"]] == ["1", "2"]
    assert result["reextraction_table_candidates"]["detail"]["after"] == table_payload(proposed)["tables"]["detail"]
    assert "REEXTRACTION_TABLE:detail" in result["semantic_conflicts"]
    assert any(cell["validity"] == "AMBIGUOUS" for row in result["tables"]["detail"] for cell in row.values())


def test_retry_field_dictionary_order_does_not_invent_conflict():
    before = table_payload([("A", "1"), ("B", "2")])
    after = copy.deepcopy(before)
    after["tables"]["detail"] = [{"amount": row["amount"], "sku": row["sku"]} for row in after["tables"]["detail"]]
    result = local.retain_disagreements(before, after)
    assert not result["semantic_conflicts"] and "reextraction_table_candidates" not in result


def test_late_commit_keeps_durable_success_and_reports_overrun():
    from trackvance.document_worker import _commit_before_deadline
    clock = [100.0]
    budget = AttemptBudget(30, clock=lambda: clock[0])
    class DB:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
        committed = False
        def commit(self):
            self.committed = True
            clock[0] = 131.0
    db = DB()
    assert _commit_before_deadline(db, budget) is True
    assert db.committed  # No post-commit timeout exception can mark a durable result FAILED.


def test_expired_budget_prevents_commit():
    from trackvance.document_worker import _commit_before_deadline
    budget = AttemptBudget(30, started=0, clock=lambda: 31)
    db = SimpleNamespace(commit=lambda: pytest.fail("expired transaction committed"))
    with pytest.raises(DocumentDeadlineExpired):
        _commit_before_deadline(db, budget)


def queue_partial_attempt(database, partial_document):
    from trackvance.db import utcnow
    with database() as db:
        prior = db.get(DocumentOriginal, partial_document["id"])
        configuration = EngineConfiguration(provider="PADDLE_QWEN_LOCAL").model_dump()
        snapshot = {**configuration, "configuration_version": 1, "scope": "FAMILY", "family_id": prior.family_id}
        original = DocumentOriginal(family_id=prior.family_id, template_id=prior.template_id,
            artifact_id=prior.artifact_id, filename="queued-synthetic.pdf", sha256=prior.sha256,
            received_at=utcnow(), uploaded_by_id=prior.uploaded_by_id,
            engine_snapshot=snapshot, engine_snapshot_hash=digest(snapshot))
        db.add(original)
        db.flush()
        db.add(Job(document_id=original.id, lane="DOCUMENT"))
        db.commit()
        return original.id


@pytest.mark.parametrize("control_error", [None, "ORDINARY_422", "GLOBAL_DEADLINE", "SOURCE_INTEGRITY", "CALLBACK_422"])
def test_worker_validates_retained_partial_and_never_publishes(database, partial_document, monkeypatch, control_error):
    from trackvance import document_worker
    identity = queue_partial_attempt(database, partial_document)
    controls = []
    callback = {"armed": False}
    real_authority = document_worker._authority
    def authority(*args, **kwargs):
        if callback["armed"]:
            raise OperationError(422, "DOCUMENT_TIMEOUT", "Synthetic mandatory callback timeout")
        return real_authority(*args, **kwargs)
    def provider(source, spec, configuration, **kwargs):
        assert kwargs["budget"].extraction_deadline < kwargs["budget"].validation_deadline < kwargs["budget"].deadline
        data = candidate(spec)
        data["engine"]["inference_completion"] = partial_marker(data, spec, configuration, partial_document["marker"]["source_sha256"])
        return data
    def control(source, spec, **kwargs):
        controls.append(kwargs)
        callback["armed"] = control_error == "CALLBACK_422"
        kwargs["check"](80)
        if control_error == "GLOBAL_DEADLINE":
            raise DocumentDeadlineExpired()
        if control_error == "SOURCE_INTEGRITY":
            raise OperationError(422, "DOCUMENT_ARTIFACT_INTEGRITY", "Synthetic source mismatch")
        if control_error == "ORDINARY_422":
            raise OperationError(422, "DOCUMENT_ENGINE_FAILURE", "Synthetic unavailable control")
        return candidate(spec)
    monkeypatch.setattr(document_worker, "execute_provider", provider)
    monkeypatch.setattr(document_worker, "run_engine", control)
    monkeypatch.setattr(document_worker, "_authority", authority)
    assert document_worker.process_once("private-partial-worker")
    with database() as db:
        original = db.get(DocumentOriginal, identity)
        job = db.scalar(select(Job).where(Job.document_id == identity))
        assert len(controls) == 1 and controls[0]["deadline"] is not None
        assert not db.scalars(select(DatasetVersion)).all()
        if control_error in {"GLOBAL_DEADLINE", "SOURCE_INTEGRITY", "CALLBACK_422"}:
            assert original.status == job.status == "FAILED" and original.extraction_id is None
            assert original.error_code == ("DOCUMENT_ARTIFACT_INTEGRITY" if control_error == "SOURCE_INTEGRITY" else "DOCUMENT_TIMEOUT")
        else:
            assert original.status == "REVIEW_REQUIRED" and job.status == "SUCCESS"
            extraction = db.get(DocumentExtraction, original.extraction_id)
            assert not extraction.reviewed and extraction.engine["inference_completion"]["failed_stage"] == "REFINE"
            assert ("validation_failure" in extraction.engine) == (control_error == "ORDINARY_422")
            assert ("validation" in extraction.engine) == (control_error is None)


def test_durable_worker_result_is_not_changed_to_failed_by_late_commit(database, partial_document, monkeypatch):
    from trackvance import document_worker
    identity = queue_partial_attempt(database, partial_document)
    def provider(source, spec, configuration, **kwargs):
        data = candidate(spec)
        data["engine"]["inference_completion"] = partial_marker(data, spec, configuration, partial_document["marker"]["source_sha256"])
        return data
    def commit(db, budget):
        budget.require()
        db.commit()
        budget.deadline = budget.clock() -1
        return True
    monkeypatch.setattr(document_worker, "execute_provider", provider)
    monkeypatch.setattr(document_worker, "run_engine", lambda source, spec, **kwargs: candidate(spec))
    monkeypatch.setattr(document_worker, "_commit_before_deadline", commit)
    monkeypatch.setattr(document_worker, "dispatch_individual", lambda *args: pytest.fail("overrun tried new publication"))
    assert document_worker.process_once("private-partial-late-commit")
    with database() as db:
        original = db.get(DocumentOriginal, identity)
        job = db.scalar(select(Job).where(Job.document_id == identity))
        assert original.status == "REVIEW_REQUIRED" and original.extraction_id and original.error_code is None
        assert job.status == "SUCCESS"


def test_worker_global_deadline_is_checked_before_progress_debounce(database, partial_document, monkeypatch):
    from trackvance import document_worker
    identity = queue_partial_attempt(database, partial_document)
    def provider(source, spec, configuration, **kwargs):
        kwargs["check"](10)
        kwargs["budget"].deadline = kwargs["budget"].clock() -1
        kwargs["check"](10)  # Same value within debounce interval must still fail.
        pytest.fail("expired progress callback returned")
    monkeypatch.setattr(document_worker, "execute_provider", provider)
    monkeypatch.setattr(document_worker, "run_engine", lambda *args, **kwargs: pytest.fail("expired candidate reached validation"))
    assert document_worker.process_once("private-expired-debounce")
    with database() as db:
        original = db.get(DocumentOriginal, identity)
        job = db.scalar(select(Job).where(Job.document_id == identity))
        assert original.status == job.status == "FAILED" and original.error_code == "DOCUMENT_TIMEOUT"
        assert original.extraction_id is None


@pytest.mark.parametrize("helper", ["document_inference_completion.py", "document_candidate_arbitration.py",
    "document_representation.py", "document_normalization.py", "document_lossless.py", "document_validation_policy.py",
    "document_recognition_transport.py", "document_recognition_store.py", "document_recognition_service.py"])
@pytest.mark.parametrize("mutation", ["missing_helper", "changed_helper_hash"])
def test_new_completion_authority_is_in_processor_identity_and_stales_prior_builder_test(authenticated, database, monkeypatch, mutation, helper):
    import hashlib
    from pathlib import Path

    from trackvance import document_builder_workspace as workspace
    from trackvance.document_models import DocumentBuilderTest, DocumentDraft
    current = workspace.processor_identity()
    assert current[helper] == hashlib.sha256(
        Path(workspace.__file__).with_name(helper).read_bytes()).hexdigest()
    previous = dict(current)
    if mutation == "missing_helper":
        del previous[helper]
    else:
        previous[helper] = "f" *64
    spec = definition()
    snapshot = {"processor": previous, "definition": spec, "references": []}
    with database() as db:
        family = DocumentFamily(name="Stale independent authority", responsible_user_id="test-user")
        db.add(family)
        db.flush()
        draft = DocumentDraft(family_id=family.id, definition=spec, definition_hash=digest(spec), actor_id="test-user")
        db.add(draft)
        db.flush()
        item = DocumentBuilderTest(draft_id=draft.id, draft_version=1, snapshot=snapshot,
            snapshot_hash=digest(snapshot), automatic_results={}, automatic_results_hash=digest({}), actor_id="test-user")
        db.add(item)
        db.flush()
        identity = item.id
        db.commit()
    monkeypatch.setattr(workspace, "snapshot_for", lambda *args: {"processor": workspace.processor_identity(), "definition": spec, "references": []})
    response = authenticated.post(f"/api/v1/documents/builder/tests/{identity}/activate")
    assert response.status_code == 409 and response.json()["error"]["code"] == "DOCUMENT_TEST_STALE"
