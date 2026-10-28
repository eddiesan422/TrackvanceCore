"""Synthetic source-event logic: no quality claim about OCR/model accuracy."""
import base64
import copy
import json

import pytest
from test_document_candidate_selection_098 import other_events
from test_document_lossless_098 import attest, definition, events, replay, selection

from trackvance.document_observation_uncertainty import OBSERVATION_POLICY_VERSION
from trackvance.document_quality import quality_report, validate_candidate_contract


def multi_page(*observations):
    count = len(observations)
    pages = [events(observation, count=count, emitted=count)[index]
        for index, observation in enumerate(observations)]
    return [*pages, events(observations[0], count=count, emitted=count)[-1]]


def uncertain(*alternatives, ambiguous=True):
    item = selection(None)
    item["fields"]["reference"].update(ambiguous=ambiguous, alternatives=list(alternatives))
    return item


@pytest.mark.parametrize("missing", [None, "", " \t "])
def test_normal_absence_on_another_page_does_not_create_a_conflict(missing):
    config = definition()
    items = multi_page(selection("R-001"), selection(missing))
    data = replay(items, config)
    cell = data["payload"]["fields"]["reference"]
    assert (cell["observed"], cell["value"], cell["validity"]) == ("R-001", "R-001", "VALID")
    assert quality_report(attest(data, config, items), config)["status"] == "AUTO_VERIFIED"


@pytest.mark.parametrize("suggestions,flag", [([], True), (["R-001", "R-002"], True), (["R-002"], False), ([""], True)])
def test_null_uncertainty_reaches_the_approval_gate_and_preserves_original(suggestions, flag):
    config = definition()
    items = multi_page(selection("R-001"), uncertain(*suggestions, ambiguous=flag))
    before = copy.deepcopy(items)
    replayed = copy.deepcopy(items)
    data = replay(replayed, config)
    cell = data["payload"]["fields"]["reference"]
    null = next(value for value in cell["candidate_alternatives"] if value["observed"] is None)
    assert null["ambiguous"] is flag and null["alternatives"] == suggestions and null["page"] == 2
    assert cell["value"] is None and cell["validity"] == "AMBIGUOUS"
    assert cell["observation_policy"]["version"] == OBSERVATION_POLICY_VERSION
    assert cell["observation_policy"]["unresolved"] is True
    assert items == before
    frozen = data["payload"]["document_representation"]["pages"][1]["selection"]["fields"]["reference"]
    source = json.loads(base64.b64decode(before[1]["payload"]["artifacts"][0]["content_b64"]))
    assert frozen == source["fields"]["reference"]
    report = quality_report(attest(data, config, replayed), config)
    assert report["status"] == "REVIEW_REQUIRED" and report["checks"]["durable_source_bound"] is True
    assert "QUALITY_OBSERVATION_UNCERTAINTY:headers.0.reference" in data["engine"]["validation"]["issues"]
    validate_candidate_contract(data, config)


@pytest.mark.parametrize("optional", [False, True])
def test_all_null_observations_remain_missing_or_legitimately_optional(optional):
    config = definition()
    config["fields"][0]["required"] = not optional
    items = multi_page(selection(None), selection(None))
    data = replay(items, config)
    cell = data["payload"]["fields"]["reference"]
    assert cell["value"] is None and cell["validity"] == ("VALID" if optional else "MISSING")
    report = quality_report(attest(data, config, items), config)
    assert report["status"] == ("AUTO_VERIFIED" if optional else "REVIEW_REQUIRED")


@pytest.mark.parametrize("required", [False, True])
def test_null_ambiguity_is_not_a_normal_absence(required):
    config = definition()
    config["fields"][0]["required"] = required
    items = multi_page(selection(None), uncertain("unreadable"))
    data = replay(items, config)
    assert data["payload"]["fields"]["reference"]["observed"] is None
    assert quality_report(attest(data, config, items), config)["status"] == "REVIEW_REQUIRED"


def currency_config():
    return definition(normalization={"currency_equivalences": {"EUR": ["euro", "euros"], "CHF": ["franc suisse"]}})


def test_declared_equivalent_null_hypotheses_preserve_original_uncertainty_without_granting_independence():
    config = currency_config()
    items = multi_page(selection("EUR"), uncertain("euro", "euros"))
    data = replay(items, config)
    cell = data["payload"]["fields"]["reference"]
    assert cell["value"] == "EUR" and cell["validity"] == "VALID"
    assert cell["candidate_alternatives"][1]["ambiguous"] is True
    assert cell["observation_policy"]["unresolved"] is False
    assert cell["observation_policy"]["grants_independent_corroboration"] is False
    assert quality_report(attest(data, config, items), config)["status"] == "AUTO_VERIFIED"
    config["fields"][0]["critical"] = True
    assert quality_report(attest(replay(items, config), config, items), config)["status"] == "REVIEW_REQUIRED"


@pytest.mark.parametrize("suggestions", [["CHF"], ["euros", "CHF"], ["$"], []])
def test_distinct_or_illegible_null_currency_hypotheses_remain_pending(suggestions):
    config = currency_config()
    items = multi_page(selection("EUR"), uncertain(*suggestions))
    data = replay(items, config)
    assert data["payload"]["fields"]["reference"]["validity"] == "AMBIGUOUS"
    assert quality_report(attest(data, config, items), config)["status"] == "REVIEW_REQUIRED"


def scoped_config():
    config = definition()
    config["fields"][0].update(source_association="DECLARED_LABEL_CONTEXT_V1",
        visual={"page": 1, "label": "Reference", "section_label": "Invoice", "direction": "RIGHT", "region": [0, 0, 1, 1]},
        visual_alternatives=[{"page": 2, "label": "Reference", "section_label": "Invoice", "direction": "RIGHT", "region": [0, 0, 1, 1]}])
    return config


def context(item, section="Invoice"):
    item["fields"]["reference"]["source_context"] = {"anchor_label": "Reference", "section_label": section}
    return item


def test_null_uncertainty_demonstrably_in_another_section_is_retained_and_excluded():
    config = scoped_config()
    items = multi_page(context(selection("R-001")), context(uncertain("OTHER"), "Delivery"))
    data = replay(items, config)
    cell = data["payload"]["fields"]["reference"]
    assert cell["validity"] == "VALID" and cell["value"] == "R-001"
    rejected = next(item for item in cell["candidate_alternatives"] if item.get("eligible_for_field") is False)
    assert rejected["observed"] is None and rejected["ambiguous"] is True
    assert rejected["association"]["reason"] == "OBSERVED_SECTION_NOT_DECLARED_FOR_FIELD"
    assert quality_report(attest(data, config, items), config)["status"] == "AUTO_VERIFIED"


def test_null_ambiguity_with_missing_context_cannot_be_erased_as_outside_the_field():
    config = scoped_config()
    items = multi_page(context(selection("R-001")), uncertain("R-001"))
    data = replay(items, config)
    cell = data["payload"]["fields"]["reference"]
    assert cell["validity"] == "AMBIGUOUS"
    assert cell["candidate_alternatives"][1]["context_unresolved"] == "LITERAL_SOURCE_CONTEXT_MISSING_OR_INVALID"
    assert quality_report(attest(data, config, items), config)["status"] == "REVIEW_REQUIRED"


def test_declared_page_excludes_a_null_ambiguity_from_another_page_without_erasing_it():
    config = scoped_config()
    config["fields"][0]["visual_alternatives"] = []
    items = multi_page(context(selection("R-001")), uncertain("OTHER"))
    data = replay(items, config)
    cell = data["payload"]["fields"]["reference"]
    assert cell["value"] == "R-001" and cell["validity"] == "VALID"
    rejected = next(item for item in cell["candidate_alternatives"] if item.get("eligible_for_field") is False)
    assert rejected["observed"] is None and rejected["ambiguous"] is True
    assert rejected["association"]["reason"] == "PAGE_OUTSIDE_DECLARED_SOURCE_CONTEXT"
    assert quality_report(attest(data, config, items), config)["status"] == "AUTO_VERIFIED"


def test_equivalent_hypotheses_without_any_observed_value_do_not_invent_a_value():
    config = currency_config()
    items = multi_page(uncertain("euro", "euros"), selection(None))
    data = replay(items, config)
    cell = data["payload"]["fields"]["reference"]
    assert cell["observed"] is None and cell["value"] is None and cell["validity"] == "AMBIGUOUS"
    assert quality_report(attest(data, config, items), config)["status"] == "REVIEW_REQUIRED"


def test_declared_equivalence_does_not_resolve_a_null_signal_with_unproven_field_context():
    config = scoped_config()
    config["fields"][0]["normalization"] = currency_config()["fields"][0]["normalization"]
    items = multi_page(context(selection("EUR")), uncertain("euro", "euros"))
    data = replay(items, config)
    assert data["payload"]["fields"]["reference"]["validity"] == "AMBIGUOUS"
    assert quality_report(attest(data, config, items), config)["status"] == "REVIEW_REQUIRED"


def test_secondary_cannot_silently_replace_a_null_ambiguity_without_hypotheses():
    config = definition(critical=True)
    items = [*events(uncertain()), *other_events("R-001")]
    data = replay(items, config)
    cell = data["payload"]["fields"]["reference"]
    assert cell["observed"] is None and cell["value"] is None and cell["validity"] == "AMBIGUOUS"
    assert data["payload"]["candidate_selection"]["version"].endswith("_V3")
    assert quality_report(attest(data, config, items), config)["status"] == "REVIEW_REQUIRED"
    validate_candidate_contract(data, config)


def test_secondary_resolving_declared_null_hypotheses_cannot_corroborate_itself():
    config = currency_config()
    config["fields"][0]["critical"] = True
    items = [*events(uncertain("euro", "euros")), *other_events("EUR")]
    data = replay(items, config)
    cell = data["payload"]["fields"]["reference"]
    assert cell["value"] == "EUR" and cell["validity"] == "VALID"
    assert cell["candidate_selection"]["selected_mechanism"] == "PADDLE"
    assert cell["observation_policy"]["source_ambiguous"] is True
    assert cell["observation_policy"]["unresolved"] is False
    assert cell["observation_policy"]["grants_independent_corroboration"] is False
    extraction = attest(data, config, items)
    assert quality_report(extraction, config)["status"] == "REVIEW_REQUIRED"
    assert "headers.0.reference" not in extraction.engine["validation"]["verified_cells"]


def test_complete_null_ambiguous_secondary_is_not_discarded_behind_a_valid_primary():
    config = definition()
    secondary = events(uncertain("OTHER"), stage="QWEN_REPEAT")
    items = [*events(selection("R-001")), *secondary]
    data = replay(items, config)
    assert data["payload"]["fields"]["reference"]["validity"] == "AMBIGUOUS"
    assert any(item.get("candidate", {}).get("observed") is None
        for item in data["payload"]["fields"]["reference"]["candidate_alternatives"])
    assert quality_report(attest(data, config, items), config)["status"] == "REVIEW_REQUIRED"
