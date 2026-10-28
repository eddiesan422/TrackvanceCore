"""Synthetic coordinator/registry tests, not measurements of model quality."""
import copy
import hashlib

import pytest
from pydantic import ValidationError
from test_document_lossless_098 import definition, events, selection

from trackvance.document_automatic_verification import POLICY_VERSION, source_context
from trackvance.document_engine import extract
from trackvance.document_inference_completion import AttemptBudget, DocumentCheckpointFailure
from trackvance.document_lossless import run_lossless
from trackvance.document_normalization import canonical_hash
from trackvance.document_providers import EngineConfiguration
from trackvance.document_quality import quality_report
from trackvance.document_validation_policy import certify_lossless_candidate
from trackvance.operations_common import OperationError


def configuration(strategy="QWEN_VISUAL", *, use_llm=True):
    return EngineConfiguration(provider="PADDLE_QWEN_LOCAL", extraction_strategy=strategy,
        automatic_verification="BOUNDED_LOCAL_V1", use_llm=use_llm).model_dump()


def harness(tmp_path, configuration, *, released=True):
    schema = definition(critical=True)
    source = tmp_path / "original.pdf"
    source.write_bytes(b"%PDF-synthetic-coordinator-fixture")
    calls, persisted = [], []

    def persist(event):
        refs = {"manifest_artifact_id": "manifest-" + str(len(persisted)), "event_sha256": event["event_sha256"],
            "artifacts": [{"artifact_id": "raw-" + str(len(persisted)) + "-" + str(i),
                "kind": item["kind"], "sha256": item["sha256"], "page": event.get("page_one_based"),
                "stage": event["stage"]} for i, item in enumerate(event["payload"].get("artifacts", []))]}
        persisted.append({**copy.deepcopy(event), "durable_refs": refs})
        return refs

    def recognize(_source, definition, frozen, **kwargs):
        strategy = kwargs["strategy"]
        calls.append(strategy)
        items = events(selection(), released=released) if strategy == "QWEN_VISUAL" else events(
            raw={"parsing_res_list": [{"block_label": "text", "block_content": "Reference: R-001"}]},
            stage="PADDLE_STRUCTURED", released=released)
        for item in items:
            item.update(request_id="request-" + str(len(calls)), attempt_id=kwargs["attempt_context"]["attempt_id"],
                source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                configuration_sha256=canonical_hash(frozen), definition_sha256=canonical_hash(definition))
            item["event_sha256"] = canonical_hash({k:v for k,v in item.items() if k != "event_sha256"})
            kwargs["on_event"](item)
        return {"pages": items[:-1], "terminal": items[-1]}

    def native(*_args, **_kwargs):
        return {"payload": extract([], schema), "engine": {"pages": 1}}

    def run(**kwargs):
        return run_lossless(source, schema, configuration, legacy_runner=kwargs.pop("legacy_runner", native),
            check=kwargs.pop("check", lambda _: None), budget=kwargs.pop("budget", AttemptBudget(300)),
            recognize=kwargs.pop("recognize", recognize), on_event=kwargs.pop("on_event", persist),
            attempt_context={"attempt_id": kwargs.pop("attempt", "new-attempt")}, **kwargs)
    run.recognize = recognize
    run.persist = persist
    return source, schema, calls, persisted, run


@pytest.mark.parametrize("primary,expected", [("QWEN_VISUAL", ["QWEN_VISUAL", "PADDLE_STRUCTURED"]),
    ("PADDLE_STRUCTURED", ["PADDLE_STRUCTURED", "QWEN_VISUAL"])])
def test_normal_flow_completes_independent_controls_once_then_validator_decides(tmp_path, primary, expected):
    frozen = configuration(primary)
    source, schema, calls, persisted, run = harness(tmp_path, frozen)
    data = run()
    assert calls == expected
    proof = data["engine"]["verification"]
    assert proof["policy_version"] == POLICY_VERSION and proof["status"] == "CONTROLS_COMPLETED"
    assert proof["resolved_paths"] == ["headers.0.reference"] and proof["remaining_reasons"] == []
    assert proof["automatic_approval_granted"] is False and proof["human_reviewed"] is False
    assert proof["scope"]["kind"] == "WHOLE_DOCUMENT" and proof["rereads_allowed"] == 0
    data["engine"]["validation"] = certify_lossless_candidate(data, schema, hashlib.sha256(source.read_bytes()).hexdigest(),
        source_context=source_context(persisted, hashlib.sha256(source.read_bytes()).hexdigest()))
    from types import SimpleNamespace
    assert quality_report(SimpleNamespace(**data, reviewed=False), schema)["status"] == "AUTO_VERIFIED"


def test_paddle_primary_respects_semantic_control_disabled_in_frozen_configuration(tmp_path):
    frozen = configuration("PADDLE_STRUCTURED", use_llm=False)
    _source, _schema, calls, _persisted, run = harness(tmp_path, frozen)
    data = run()
    assert calls == ["PADDLE_STRUCTURED"]
    assert data["engine"]["verification"]["blocked_reason"] == "QWEN_DISABLED_IN_FROZEN_CONFIGURATION"
    assert data["engine"]["verification"]["remaining_reasons"]
    assert frozen["use_llm"] is False


def test_unreleased_primary_never_admits_secondary(tmp_path):
    _source, _schema, calls, _persisted, run = harness(tmp_path, configuration(), released=False)
    result = run()
    assert calls == ["QWEN_VISUAL"]
    assert result["engine"]["verification"]["blocked_reason"] == "PRIOR_RESOURCES_NOT_RELEASED"


def test_remaining_attempt_budget_blocks_control_without_resetting_deadline(tmp_path):
    _source, _schema, calls, _persisted, run = harness(tmp_path, configuration())
    result = run(budget=AttemptBudget(30, started=0, clock=lambda: 0))
    assert calls == ["QWEN_VISUAL"]
    assert result["engine"]["verification"]["blocked_reason"] == "INSUFFICIENT_FROZEN_ATTEMPT_BUDGET"


def test_exhausted_control_allocation_keeps_reserved_validation_time_and_never_starts_another_model(tmp_path):
    _source, _schema, calls, _persisted, run = harness(tmp_path, configuration())
    clock = [0.0]
    budget = AttemptBudget(300, started=0, clock=lambda: clock[0])
    def recognized(*args, **kwargs):
        response = run.recognize(*args, **kwargs)
        clock[0] = budget.extraction_deadline + 1
        return response
    result = run(recognize=recognized, budget=budget)
    assert calls == ["QWEN_VISUAL"] and budget.require() > 0
    assert result["engine"]["verification"]["blocked_reason"] == "INSUFFICIENT_FROZEN_ATTEMPT_BUDGET"


def test_recovery_reuses_both_mechanisms_without_native_probe_or_model_repetition(tmp_path):
    _source, _schema, calls, persisted, run = harness(tmp_path, configuration())
    run()
    saved = copy.deepcopy(persisted)
    calls.clear()
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Recovery must reuse the immutable primary and secondary")
    result = run(saved_events=saved, legacy_runner=forbidden, recognize=forbidden, attempt="recovery-attempt")
    assert not calls and result["engine"]["verification"]["primary_reused"] is True
    assert result["engine"]["verification"]["status"] == "CONTROLS_COMPLETED"
    recovery = next(item for item in reversed(persisted) if item["stage"] == "RECOVERY_REUSE")
    assert recovery["payload"]["reused_event_hashes"] == [e["event_sha256"] for e in saved]
    assert result["engine"]["recognition_calls"] == []


def test_cancellation_during_control_is_not_converted_into_an_approval(tmp_path):
    _source, _schema, calls, persisted, run = harness(tmp_path, configuration())
    def cancel_control(*args, **kwargs):
        if kwargs["strategy"] == "PADDLE_STRUCTURED":
            raise DocumentCheckpointFailure(OperationError(409, "DOCUMENT_CANCELLED", "Cancelled fixture"))
        return run.recognize(*args, **kwargs)
    with pytest.raises(DocumentCheckpointFailure):
        run(recognize=cancel_control)
    assert calls == ["QWEN_VISUAL"] and any(item["kind"] == "PAGE" for item in persisted)
    assert not any(item.get("payload", {}).get("verification", {}).get("status") == "CONTROLS_COMPLETED" for item in persisted)


def test_late_secondary_page_cannot_bypass_the_owning_attempt_sink(tmp_path):
    _source, _schema, calls, persisted, run = harness(tmp_path, configuration())
    def fenced(event):
        if event["stage"] == "PADDLE_STRUCTURED":
            raise DocumentCheckpointFailure(OperationError(409, "DOCUMENT_LEASE_LOST", "Late fixture result"))
        return run.persist(event)
    with pytest.raises(DocumentCheckpointFailure):
        run(on_event=fenced)
    assert calls == ["QWEN_VISUAL", "PADDLE_STRUCTURED"]
    assert not any(item["stage"] == "PADDLE_STRUCTURED" for item in persisted)


def test_default_configuration_preserves_historical_serialization_and_requires_explicit_opt_in():
    assert "automatic_verification" not in EngineConfiguration().model_dump()
    assert "automatic_verification" not in EngineConfiguration(extraction_strategy="SELECTIVE").model_dump()
    assert configuration()["automatic_verification"] == "BOUNDED_LOCAL_V1"


@pytest.mark.parametrize("provider,strategy", [("TESSERACT_LEGACY", "QWEN_VISUAL"),
    ("PADDLE_QWEN_LOCAL", "LEGACY"), ("AWS_TEXTRACT", "PADDLE_STRUCTURED")])
def test_automatic_policy_cannot_bypass_the_lossless_local_pipeline(provider, strategy):
    with pytest.raises(ValidationError, match="AUTOMATIC_VERIFICATION_REQUIRES_LOCAL_LOSSLESS_ROUTE"):
        EngineConfiguration(provider=provider, extraction_strategy=strategy, automatic_verification="BOUNDED_LOCAL_V1")
    assert "automatic_verification" not in EngineConfiguration(provider=provider, extraction_strategy=strategy).model_dump()
