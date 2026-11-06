"""The routing gate must not remove documentary blockers from the proof."""

import pytest
import test_document_semantic_corroboration_098 as fixtures
from test_document_semantic_corroboration_v2_098 import assess, mutate, v2_fixture

from trackvance.document_semantic_corroboration import semantic_verification_decision
from trackvance.document_semantic_evidence import semantic_review_dto


@pytest.fixture(autouse=True)
def experimental_opt_in(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", "true")


def structural_case(monkeypatch, *, counter="Page 1 of 1", paddle=False, narrative=False, contradiction=False):
    definition, configuration, events = v2_fixture(monkeypatch, paddle=paddle, narrative=narrative)

    def source(body):
        lines = body["payload"]["lines"]
        lines[:] = [fixtures._physical_line(["Unassigned", "Quantity", "Unit price", "Amount"], .2)
            if line["text"] == "Description Quantity Unit price Amount" else line for line in lines]
        lines.append(fixtures._line(counter, .95))
        if contradiction:
            lines.append(fixtures._line("Total due: 20.00", .18))

    mutate(events, "NATIVE_SOURCE_PROBE_JSON", source)
    return definition, configuration, events


def test_unscoped_counter_does_not_veto_independent_structure_control(monkeypatch):
    from types import SimpleNamespace

    definition, configuration, events = structural_case(monkeypatch)
    data, context, proof = assess(definition, configuration, events)
    assert proof["global_issues"] == ["PRINTED_SCOPE_UNPROVEN"]
    assert proof["printed_pagination"]["status"] == "UNVERIFIED"
    assert proof["source_checks"]["integrity"]["status"] == "VERIFIED"
    assert proof["source_checks"]["execution_pages"]["status"] == "COMPLETE"
    assert not proof["table_coverage"]["items"]
    decision = semantic_verification_decision(data, definition, context)
    assert any(reason.get("table") == "items" for reason in decision["pending_controls"])
    assert decision["actionable"]
    extraction = SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False)
    review = semantic_review_dto(extraction, definition)
    assert not review["automatic_approval"] and review["source_checks"]["automatic_eligibility"]["status"] == "BLOCKED"
    assert review["printed_pagination"]["status"] == "UNVERIFIED"


@pytest.mark.parametrize("counter,expected_issue", [
    ("Document page 1 of 2", "DOCUMENT_SOURCE_INCOMPLETE"),
    ("Document page 3 of 2", "PRINTED_PAGINATION_CONFLICT"),
    ("Page 2 of 3", "PRINTED_SCOPE_UNPROVEN"),
])
def test_structural_pending_cannot_override_documentary_deficit(monkeypatch, counter, expected_issue):
    definition, configuration, events = structural_case(monkeypatch, counter=counter)
    data, context, proof = assess(definition, configuration, events)
    assert expected_issue in proof["global_issues"]
    assert not semantic_verification_decision(data, definition, context)["actionable"]


@pytest.mark.parametrize("case", ["previous_paddle", "narrative", "source_conflict", "integrity", "unreleased"])
def test_escalation_does_not_override_authority_or_exclusivity(monkeypatch, case):
    definition, configuration, events = structural_case(monkeypatch, paddle=case == "previous_paddle",
        narrative=case == "narrative", contradiction=case == "source_conflict")
    if case == "unreleased":
        events[-1]["resources_released"] = False
    data, context, proof = assess(definition, configuration, events)
    if case == "integrity":
        context["recognition_events"][0]["source_sha256"] = "f" * 64
    elif case == "source_conflict":
        assert "DOCUMENTARY_SOURCE_CONTRADICTION:headers.0.total" in proof["global_issues"]
    assert not semantic_verification_decision(data, definition, context)["actionable"]
