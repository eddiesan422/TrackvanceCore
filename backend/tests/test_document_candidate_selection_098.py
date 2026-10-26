"""Useful alternatives retain source identity, conflicts and independent controls."""
import copy
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_document_lossless_098 import attest, definition, events, replay, selection

from trackvance.document_candidate_selection import combine_candidates, secondary_decision
from trackvance.document_engine import extract
from trackvance.document_inference_completion import AttemptBudget
from trackvance.document_lossless import (
    _cell,
    _retain_alternatives,
    _secondary_decision_event,
    run_lossless,
)
from trackvance.document_normalization import (
    DocumentNormalization,
    canonical_hash,
    normalize_observation,
)
from trackvance.document_quality import quality_report, validate_candidate_contract
from trackvance.document_schemas import DocumentDefinition
from trackvance.document_validation_policy import align_rows


def other_events(value, stage="PADDLE_STRUCTURED"):
    return events(raw={"parsing_res_list": [{"block_label": "text", "block_content": "Reference: " + value}]}, stage=stage)


def test_corroboration_provenance_has_canonical_source_order(monkeypatch):
    from trackvance import document_validation_policy as policy

    fields = [{"name": name, "logical_type": "STRING", "required": True}
        for name in ("code", "description")]

    def rows(prefix):
        return [{field["name"]: _cell(value, field, 1, prefix + field["name"] + value)
            for field, value in zip(fields, values, strict=True)}
            for values in [("001-A", "first"), ("002-B", "second")]]

    monkeypatch.setattr(policy, "align_rows", lambda *_: (
        {1: 1, 0: 0}, {1: "FULL_ROW_OCCURRENCE", 0: "FULL_ROW_OCCURRENCE"}))
    payload = {"fields": {}, "tables": {"lines": rows("primary-")}}
    definition = {"fields": [], "tables": [{"name": "lines", "fields": fields}]}
    control = {"fields": {}, "tables": {"lines": rows("control-")},
        "_mechanism": "PADDLE", "_stage": "PADDLE_STRUCTURED", "_completed_pages": [1]}
    _, structure, provenance = policy._corroborated(payload, definition, [control], primary_stage="QWEN_VISUAL")
    assert structure == {"lines": True}
    assert [(item["source_occurrence"], item["control_occurrence"])
        for item in provenance["lines"]] == [(0, 0), (1, 1)]


@pytest.mark.parametrize("missing", [None, "", " \t "])
def test_absent_primary_is_completed_from_bound_alternative_without_self_corroboration(missing):
    config = definition(critical=True)
    items = [*events(selection(missing)), *other_events("0009")]
    data = replay(items, config)
    cell = data["payload"]["fields"]["reference"]
    assert cell["observed"] == cell["value"] == "0009"
    assert cell["candidate_selection"]["selected_mechanism"] == "PADDLE"
    assert data["payload"]["candidate_selection"]["decisions"]["headers.0.reference"]["case"] == "A"
    assert data["payload"]["candidate_history"][0]["fields"]["reference"]["observed"] == missing
    assert data["payload"]["candidate_history"][1]["fields"]["reference"]["observed"] == "0009"
    extraction = attest(data, config, items)
    assert quality_report(extraction, config)["status"] == "REVIEW_REQUIRED"
    assert extraction.engine["validation"]["verified_cells"] == {}
    validate_candidate_contract(data, config)


def test_first_retained_stage_is_primary_instead_of_qwen_name_priority():
    config = definition(critical=True)
    items = [*other_events("0009"), *events(selection("0009"))]
    data = replay(items, config)
    assert data["engine"]["route"] == "PADDLE_STRUCTURED"
    assert data["payload"]["fields"]["reference"]["observed"] == "0009"
    assert quality_report(attest(data, config, items), config)["status"] == "AUTO_VERIFIED"


def test_declared_currency_equivalence_preserves_both_observations():
    config = definition(critical=True, normalization={"currency_equivalences": {"COP": ["Peso colombiano"]}})
    items = [*events(selection("COP")), *other_events("Peso colombiano")]
    data = replay(items, config)
    cell = data["payload"]["fields"]["reference"]
    assert cell["observed"] == cell["value"] == "COP"
    assert cell["candidate_alternatives"][-1]["candidate"]["observed"] == "Peso colombiano"
    assert cell["candidate_selection"]["reason"] == "DECLARED_NORMALIZATION_EQUIVALENCE"
    assert quality_report(attest(data, config, items), config)["status"] == "AUTO_VERIFIED"


def test_contradictory_identifiers_remain_ambiguous_without_digit_repair():
    config = definition(critical=True)
    items = [*events(selection("0010")), *other_events("0011")]
    data = replay(items, config)
    cell = data["payload"]["fields"]["reference"]
    assert cell["observed"] == "0010" and cell["value"] is None and cell["validity"] == "AMBIGUOUS"
    assert data["payload"]["candidate_selection"]["decisions"]["headers.0.reference"]["case"] == "C"
    assert quality_report(attest(data, config, items), config)["status"] == "REVIEW_REQUIRED"


def test_only_review_of_the_actual_conflicting_field_resolves_its_retained_conflict():
    config = definition(critical=True)
    config["fields"].append({"name": "memo", "label": "Memo", "logical_type": "STRING", "required": True})
    primary = selection("0010")
    primary["fields"]["memo"] = {"observed": "unchanged", "ambiguous": False, "alternatives": []}
    secondary = events(raw={"parsing_res_list": [
        {"block_label": "text", "block_content": "Reference: 0011"},
        {"block_label": "text", "block_content": "Memo: unchanged"}]}, stage="PADDLE_STRUCTURED")
    items = [*events(primary), *secondary]
    extraction = attest(replay(items, config), config, items)
    assert quality_report(extraction, config)["cells"] == {"total": 2, "verified": 1, "pending": 1}
    unchanged_history = copy.deepcopy(extraction.payload["candidate_history"])
    extraction.reviewed = True
    extraction.payload["fields"]["memo"]["reviewed"] = True
    assert "SEMANTIC_CONFLICT:FIELD:reference" in quality_report(extraction, config)["issues"]
    extraction.payload["fields"]["reference"].update(value="0011", validity="VALID", reviewed=True)
    assert quality_report(extraction, config)["status"] == "HUMAN_VERIFIED"
    assert extraction.payload["candidate_history"] == unchanged_history


def test_two_same_mechanism_stages_never_supply_independent_authority():
    config = definition(critical=True)
    items = [*events(selection("0010")), *events(selection("0010"), stage="QWEN_REPEAT")]
    data = replay(items, config)
    extraction = attest(data, config, items)
    assert extraction.engine["validation"]["independent_extraction"] is False
    assert quality_report(extraction, config)["status"] == "REVIEW_REQUIRED"
    assert secondary_decision(data, config)["required"] is True


def test_failed_alternative_is_retained_without_erasing_valid_primary():
    config = definition(critical=True)
    second = other_events("other")
    second[-1].update(status="FAILED", completed_pages=[], pending_pages=[1])
    second[-1]["event_sha256"] = canonical_hash({key: value for key, value in second[-1].items() if key != "event_sha256"})
    items = [*events(selection("0010")), *second]
    data = replay(items, config)
    assert data["payload"]["fields"]["reference"]["value"] == "0010"
    assert data["payload"]["candidate_history"][1]["fields"]["reference"]["observed"] == "other"
    extraction = attest(data, config, items)
    assert extraction.engine["validation"]["independent_extraction"] is False


def test_invalid_observed_primary_is_not_replaced_by_better_looking_value():
    config = definition()
    config["fields"][0]["logical_type"] = "DECIMAL"
    data = replay([*events(selection("unreadable")), *other_events("10.00")], config)
    cell = data["payload"]["fields"]["reference"]
    assert cell["observed"] == "unreadable" and cell["validity"] == "INVALID"
    assert data["payload"]["candidate_selection"]["decisions"]["headers.0.reference"]["case"] == "D"


def candidate(stage, payload, *, complete=True):
    return {"stage": stage, "payload": copy.deepcopy(payload), "payload_sha256": canonical_hash(payload),
        "execution": {"recognition_complete": complete, "resources_released": True, "completed_pages": [1]}}


def test_unbound_alternative_cannot_fill_absence_or_bypass_reviewed_value():
    config = definition()
    base = replay(events(selection(None)), config)["payload"]
    other = replay(other_events("0009"), config)["payload"]
    other["fields"]["reference"]["evidence_refs"] = []
    _retain_alternatives(base, [candidate("QWEN_VISUAL", base), candidate("PADDLE_STRUCTURED", other)], config, "QWEN_VISUAL")
    assert base["fields"]["reference"]["observed"] is None
    assert base["candidate_selection"]["decisions"]["headers.0.reference"]["case"] == "E"
    protected = replay(events(selection("reviewed")), config)["payload"]
    protected["fields"]["reference"].update(reviewed=True, corrected=True)
    unchanged = copy.deepcopy(protected["fields"]["reference"])
    combine_candidates(protected, [candidate("QWEN_VISUAL", protected), candidate("PADDLE_STRUCTURED", other)], config, "QWEN_VISUAL")
    assert protected["fields"]["reference"] == unchanged


FIELDS = [{"name": name, "logical_type": "STRING", "required": True} for name in ("lot", "unit", "quantity", "description")]
FIELDS[-1]["critical"] = True


def source_row(values, *, page=1):
    return {field["name"]: _cell(value, field, page, "source:" + field["name"])
        for field, value in zip(FIELDS, values, strict=True)}


def test_row_identity_uses_unique_reciprocal_strongest_columns_not_weak_shared_pairs():
    left = [source_row(["lot-a", "EA", "4", "first"]), source_row(["lot-b", "EA", "4", "second"])]
    right = [source_row(["lot-b", "EA", "4", "second changed"]), source_row(["lot-a", "EA", "4", "first changed"])]
    matches, _ = align_rows(left, right, FIELDS)
    assert matches == {0: 1, 1: 0}


def test_tied_multi_column_identity_and_single_identifier_are_not_ordinal_matches():
    left = [source_row(["lot-a", "EA", "4", "first"]), source_row(["lot-b", "EA", "4", "second"])]
    right = [source_row(["lot-c", "EA", "4", "other"]), source_row(["lot-d", "EA", "4", "another"])]
    assert align_rows(left, right, FIELDS)[0] == {}
    only = [{"name": "id", "logical_type": "STRING"}]
    one = [{"id": _cell("same", only[0], 1, "source")}]
    assert align_rows(one, one, only)[0] == {}


def test_consuming_full_row_cannot_manufacture_residual_identity_from_weak_shared_values():
    left = [source_row(["lot-a", "EA", "4", "first"]), source_row(["lot-b", "EA", "4", None])]
    right = [source_row(["lot-a", "EA", "4", "first"]), source_row(["lot-c", "EA", "4", "third"])]
    matches, _ = align_rows(left, right, FIELDS)
    assert matches == {0: 0}


def test_declared_business_identity_contradiction_blocks_otherwise_unique_shared_anchors():
    fields = copy.deepcopy(FIELDS)
    fields[0]["business_key"] = True
    left = [source_row(["lot-a", "EA", "4", "same"])]
    right = [source_row(["lot-b", "EA", "4", "same"])]
    assert align_rows(left, right, fields)[0] == {}


def test_complete_duplicate_occurrences_are_retained_but_mismatched_multiplicity_is_not():
    row = source_row(["same-lot", "EA", "4", "same"])
    assert align_rows([row, row], [row, row], FIELDS)[0] == {0: 0, 1: 1}
    assert align_rows([row, row], [row, row, row], FIELDS)[0] == {}
    assert align_rows([row], [source_row(["same-lot", "EA", "4", "same"], page=2)], FIELDS)[0] == {}
    absent = source_row([None] * 4)
    assert align_rows([absent], [absent], FIELDS)[0] == {}


def test_table_missing_values_are_filled_only_after_complete_occurrence_alignment():
    config = definition()
    config["tables"] = [{"name": "detail", "fields": FIELDS}]
    base = replay(events(selection(None)), definition())["payload"]
    base["tables"], base["table_unparsed"] = {"detail": [source_row(["lot-a", "EA", None, "description"])]}, {"detail": []}
    base["row_provenance"] = {"detail": [{"occurrence_id": "occurrence-a", "source_page": 1}]}
    other = copy.deepcopy(base)
    other["tables"]["detail"] = [source_row(["lot-a", "EA", "4", "description"])]
    other["row_provenance"]["detail"][0]["occurrence_id"] = "secondary-occurrence-a"
    original_hypothesis = copy.deepcopy(base["tables"])
    combine_candidates(base, [candidate("QWEN_VISUAL", base), candidate("PADDLE_STRUCTURED", other)], config, "QWEN_VISUAL")
    cell = base["tables"]["detail"][0]["quantity"]
    assert cell["value"] == "4" and cell["candidate_selection"]["selected_mechanism"] == "PADDLE"
    assert base["candidate_history"][0]["tables"] == original_hypothesis
    assert base["candidate_history"][1]["row_provenance"]["detail"][0]["occurrence_id"] == "secondary-occurrence-a"
    assert base["row_provenance"]["detail"][0]["occurrence_id"] == "occurrence-a"
    assert cell["candidate_selection"]["identity_basis"][0]["source_occurrence_id"] == "occurrence-a"
    assert cell["candidate_selection"]["identity_basis"][0]["alternative_occurrence_id"] == "secondary-occurrence-a"


def test_selective_distinguishes_risk_incident_and_already_satisfied_controls():
    standard = definition()
    assert secondary_decision(replay(events(selection()), standard), standard)["required"] is False
    critical = definition(critical=True)
    first = replay(events(selection()), critical)
    risk = secondary_decision(first, critical)
    assert {item["kind"] for item in risk["reasons"]} == {"RISK_POLICY"}
    assert risk["scope"] == {"kind": "WHOLE_DOCUMENT", "pages": [1], "focal_reread": False}
    missing = secondary_decision(replay(events(selection(None)), critical), critical)
    assert {item["kind"] for item in missing["reasons"]} == {"INCIDENT"}
    both = replay([*events(selection()), *other_events("R-001")], critical)
    assert secondary_decision(both, critical)["required"] is False


def test_selective_records_durable_reason_before_second_call_and_observed_cost(tmp_path):
    config = definition(critical=True)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-synthetic")
    native = {"payload": extract([], config), "engine": {"pages": 1}}
    persisted, calls = [], []

    def recognize(source, definition, configuration, **kwargs):
        calls.append(kwargs["strategy"])
        if len(calls) == 2:
            decision = next(item for item in persisted if item["stage"] == "SELECTIVE_DECISION")
            assert decision["kind"] == "PHASE" and decision["payload"]["decision"]["reasons"][0]["kind"] == "RISK_POLICY"
        items = events(selection(), stage=kwargs["strategy"]) if len(calls) == 1 else other_events("R-001")
        for item in items:
            item.update(source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                configuration_sha256=canonical_hash(configuration), definition_sha256=canonical_hash(definition),
                attempt_id=kwargs["attempt_context"]["attempt_id"])
            item["event_sha256"] = canonical_hash({key: value for key, value in item.items() if key != "event_sha256"})
            kwargs["on_event"](item)
        return {"pages": items[:-1], "terminal": items[-1], "engine": {}}

    data = run_lossless(source, config, {"extraction_strategy": "SELECTIVE"},
        legacy_runner=lambda *args, **kwargs: copy.deepcopy(native), check=lambda _: None,
        budget=AttemptBudget(300), recognize=recognize, on_event=lambda event: persisted.append(copy.deepcopy(event)))
    assert calls == ["QWEN_VISUAL", "PADDLE_STRUCTURED"]
    assert len(data["engine"]["recorded_selective_decisions"]) == 1
    assert [item["http_requests_observed"] for item in data["engine"]["recognition_calls"]] == [1, 1]
    assert all(item["elapsed_seconds"] >= 0 for item in data["engine"]["recognition_calls"])
    assert data["engine"]["post_secondary_decision"]["required"] is False
    assert len([item for item in persisted if item["kind"] == "TERMINAL"]) == 2


def test_declared_multiline_equivalence_preserves_original_and_is_not_implicit_escape_decoding():
    field = {"logical_type": "STRING", "required": True, "normalization": {"text_linebreaks": "DECLARED_MULTILINE_V1"}}
    for observed in ["First\nSecond", "First\r\nSecond", r"First\nSecond"]:
        value, validity, trace = normalize_observation(observed, field, evidence_refs=["source-cell"])
        assert (value, validity) == ("First Second", "VALID")
        assert trace["observed"] == observed and trace["evidence_refs"] == ["source-cell"]
        assert trace["rule"] == "DECLARED_MULTILINE_V1"
    assert normalize_observation(r"First\tSecond", field)[0] == r"First\tSecond"
    assert normalize_observation(r"First\\nSecond", field)[0] == r"First\\nSecond"
    assert normalize_observation(r"C:\new\folder", {"logical_type": "STRING"})[0] == r"C:\new\folder"
    assert "text_linebreaks" not in DocumentNormalization.model_validate({"decimal_scale": 2}).model_dump()


@pytest.mark.parametrize("ordinary", ["First  Second", "First\tSecond"])
def test_multiline_contract_does_not_fold_horizontal_spacing_without_a_linebreak(ordinary):
    field = {"logical_type": "STRING", "required": True, "normalization": {"text_linebreaks": "DECLARED_MULTILINE_V1"}}
    value, status, trace = normalize_observation(ordinary, field)
    assert status == "VALID" and value == ordinary and value != "First Second"
    assert trace["observed"] == ordinary
    assert normalize_observation(ordinary + " \t\n\tIndented", field)[0] == ordinary + " Indented"


def test_secondary_intent_roundtrips_through_real_storage_before_transport_callback(database, tmp_path):
    from trackvance.document_recognition_store import (
        latest_recognition_events,
        persist_recognition_event,
    )
    from trackvance.models import User

    config = definition(critical=True)
    configuration = {"extraction_strategy": "SELECTIVE"}
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-synthetic-selection")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    original_id = uuid4().hex
    context = {"attempt_id": "selection-attempt"}
    with database() as db:
        user = db.get(User, "test-user")
        original = SimpleNamespace(id=original_id, organization_id=user.organization_id,
            sha256=source_sha, status="RUNNING", extraction_id=None)

        def persist(item):
            refs = persist_recognition_event(db, original, item, attempt_id=context["attempt_id"],
                configuration_sha256=canonical_hash(configuration), job_id="owned-job", worker_attempt_number=1)
            db.commit()
            return refs

        calls = []

        def recognize(source, definition, configuration, **kwargs):
            calls.append(kwargs["strategy"])
            if len(calls) == 2:
                restored = latest_recognition_events(db, original, hydrate=True)
                intent = next(item for item in restored if item["stage"] == "SELECTIVE_DECISION")
                assert intent["durable_refs"]["artifacts"][0]["kind"] == "SELECTIVE_DECISION_JSON"
                assert intent["payload"]["decision"]["scope"]["focal_reread"] is False
            items = events(selection(), stage=kwargs["strategy"]) if len(calls) == 1 else other_events("R-001")
            for item in items:
                item.update(source_sha256=source_sha, request_id=kwargs["strategy"],
                    configuration_sha256=canonical_hash(configuration), definition_sha256=canonical_hash(definition),
                    attempt_id=context["attempt_id"])
                item["event_sha256"] = canonical_hash({key: value for key, value in item.items() if key != "event_sha256"})
                kwargs["on_event"](item)
            return {"pages": items[:-1], "terminal": items[-1]}

        data = run_lossless(source, config, configuration, legacy_runner=lambda *args, **kwargs:
            {"payload": extract([], config), "engine": {"pages": 1}}, check=lambda _: None,
            budget=AttemptBudget(300), recognize=recognize, on_event=persist, attempt_context=context)
        restored = latest_recognition_events(db, original, hydrate=True)
        assert len([item for item in restored if item["kind"] == "TERMINAL"]) == 2
        assert len(data["engine"]["recorded_selective_decisions"]) == 1


@pytest.mark.parametrize("binding", ["attempt", "source", "configuration"])
def test_secondary_decision_is_rejected_by_existing_source_attempt_configuration_gates(database, binding):
    from sqlalchemy import func, select

    from trackvance.document_recognition_store import persist_recognition_event
    from trackvance.models import Artifact, User
    from trackvance.operations_common import OperationError

    config = definition(critical=True)
    item = _secondary_decision_event({"required": True, "reasons": []}, "a" * 64, "b" * 64,
        config, {"attempt_id": "attempt", "expected_pages": 1})
    item[{"attempt": "attempt_id", "source": "source_sha256", "configuration": "configuration_sha256"}[binding]] = "c" * 64
    item["event_sha256"] = canonical_hash({key: value for key, value in item.items() if key != "event_sha256"})
    with database() as db:
        user = db.get(User, "test-user")
        original = SimpleNamespace(id=uuid4().hex, organization_id=user.organization_id, sha256="a" * 64)
        with pytest.raises(OperationError):
            persist_recognition_event(db, original, item, attempt_id="attempt", configuration_sha256="b" * 64,
                job_id="job", worker_attempt_number=1)
        assert db.scalar(select(func.count()).select_from(Artifact)) == 0


@pytest.mark.parametrize("extra", [{"logical_type": "DECIMAL"}, {"business_key": True},
    {"normalization": {"text_linebreaks": "DECLARED_MULTILINE_V1", "currency_equivalences": {"COP": ["Peso"]}}}])
def test_multiline_contract_is_not_accepted_for_numeric_keys_or_currency(extra):
    config = {"fields": [{"name": "document_text", "normalization": {"text_linebreaks": "DECLARED_MULTILINE_V1"}, **extra}]}
    with pytest.raises(ValidationError):
        DocumentDefinition.model_validate(config)


def test_multiline_equivalent_mechanisms_can_corroborate_only_after_explicit_declaration():
    config = definition(critical=True)
    first = events(selection("First\nSecond"))
    other = other_events(r"First\nSecond")
    undecided = replay([*first, *other], config)
    assert undecided["payload"]["fields"]["reference"]["validity"] == "AMBIGUOUS"
    config["fields"][0]["normalization"] = {"text_linebreaks": "DECLARED_MULTILINE_V1"}
    items = [*first, *other]
    bound = replay(items, config)
    assert bound["payload"]["fields"]["reference"]["value"] == "First Second"
    assert quality_report(attest(bound, config, items), config)["status"] == "AUTO_VERIFIED"
