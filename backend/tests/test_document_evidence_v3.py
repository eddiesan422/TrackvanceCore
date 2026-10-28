"""Source-identity and complete table evidence, separate from model quality."""
import copy

import pytest
from test_document_lossless_098 import attest, definition, events, replay, selection

from trackvance.document_evidence_v3 import corroboration_v3
from trackvance.document_lossless import _cell
from trackvance.document_normalization import canonical_hash
from trackvance.document_quality import quality_report
from trackvance.document_validation_policy import LEGACY_VALIDATOR_VERSION, VALIDATOR_VERSION


def paired(*, rows=2, extra_table="", critical=True):
    config = definition(table=True, critical=critical)
    row = {name: {"observed": value, "ambiguous": False, "alternatives": []}
        for name, value in (("code", "0001"), ("description", "Repeated source product"))}
    first = events(selection(rows=[row] * rows))
    html = ("<table><tr><th>Code</th><th>Description</th></tr>"
        + "<tr><td>0001</td><td>Repeated source product</td></tr>" * rows + "</table>")
    blocks = [{"block_label": "text", "block_content": "Reference: R-001"},
        {"block_label": "table", "block_content": html}]
    if extra_table:
        blocks.append({"block_label": "table", "block_content": extra_table})
    second = events(raw={"parsing_res_list": blocks}, stage="PADDLE_STRUCTURED")
    items = [*first, *second]
    return config, items, replay(items, config)


def test_two_bound_raw_mechanisms_keep_repeated_occurrences_and_cover_every_source_body_row():
    config, items, data = paired()
    extraction = attest(data, config, items)
    assert quality_report(extraction, config)["status"] == "AUTO_VERIFIED"
    proof = extraction.engine["validation"]
    assert proof["validator_version"] == VALIDATOR_VERSION
    assert {source["mechanism"] for source in proof["recognition_sources"]} == {"QWEN", "PADDLE"}
    inventory = proof["source_table_inventory"]["detail"]["PADDLE_STRUCTURED"]
    assert inventory["covered"] and inventory["source_tables"][0]["source_body_rows"] == 2
    assert len(extraction.payload["tables"]["detail"]) == 2


def test_renamed_qwen_stage_is_still_the_same_raw_mechanism():
    config = definition(critical=True)
    items = [*events(selection()), *events(selection(), stage="PADDLE_RENAMED")]
    data = replay(items, config)
    extraction = attest(data, config, items)
    assert not extraction.engine["validation"]["independent_extraction"]
    assert quality_report(extraction, config)["status"] == "REVIEW_REQUIRED"


@pytest.mark.parametrize("damage", ["model", "source", "config", "event", "artifact", "binding_family"])
def test_unbound_source_cannot_become_an_independent_control(damage):
    config, items, data = paired(rows=1)
    if damage == "model":
        data["engine"]["recognition_sources"]["PADDLE_STRUCTURED"]["model"] = "invented"
    elif damage == "source":
        data["engine"]["recognition_sources"]["PADDLE_STRUCTURED"]["source_sha256"] = "f" * 64
    elif damage == "config":
        data["engine"]["recognition_sources"]["PADDLE_STRUCTURED"]["configuration_sha256"] = "f" * 64
    elif damage == "event":
        data["engine"]["recognition_sources"]["PADDLE_STRUCTURED"]["event_hashes"] = []
    elif damage == "binding_family":
        data["engine"]["recognition_sources"]["PADDLE_STRUCTURED"]["mechanism_family"] = "QWEN"
    else:
        # Persisted registry absence cannot be repaired by a provider/hash flag.
        for item in items:
            if item["stage"] == "PADDLE_STRUCTURED":
                item["payload"]["artifacts"] = []
    extraction = attest(data, config, items)
    assert quality_report(extraction, config)["status"] == "REVIEW_REQUIRED"
    assert "headers.0.reference" not in extraction.engine["validation"]["verified_cells"]
    assert "detail.0.code" not in extraction.engine["validation"]["verified_cells"]


def test_copied_candidate_history_cannot_replace_the_retained_raw_control():
    config, items, data = paired(rows=1)
    data["payload"]["candidate_history"][1]["fields"]["reference"]["value"] = "forged"
    data["payload"]["candidate_history"][1]["fields"]["reference"]["observed"] = "forged"
    summary = corroboration_v3(data, config)
    assert "headers.0.reference" in summary["paths"]
    assert summary["structure"]["detail"] is True
    assert summary["integrity_issues"] == ["QUALITY_RETAINED_CANDIDATE_INTEGRITY:PADDLE_STRUCTURED"]
    assert quality_report(attest(data, config, items), config)["status"] == "REVIEW_REQUIRED"


def test_secondary_selected_value_never_corroborates_itself():
    config = definition(critical=True)
    first = events(selection(None))
    second = events(raw={"parsing_res_list": [{"block_label": "text", "block_content": "Reference: R-001"}]}, stage="PADDLE_STRUCTURED")
    items = [*first, *second]
    data = replay(items, config)
    assert data["payload"]["fields"]["reference"]["value"] == "R-001"
    extraction = attest(data, config, items)
    assert extraction.engine["validation"]["verified_cells"] == {}
    assert quality_report(extraction, config)["status"] == "REVIEW_REQUIRED"


def test_extra_unassociated_table_cannot_be_hidden_by_equal_mapped_row_counts():
    extra = "<table><tr><th>Code</th><th>Description</th></tr><tr><td>0002</td><td>Additional source occurrence</td></tr></table>"
    config, items, data = paired(rows=1, extra_table=extra)
    extraction = attest(data, config, items)
    assert quality_report(extraction, config)["status"] == "REVIEW_REQUIRED"
    assert not extraction.engine["validation"]["table_coverage"]["detail"]


def test_unknown_table_scope_blocks_coverage_instead_of_being_silently_ignored():
    config, items, data = paired(rows=1, extra_table="<table><tr><td>unlabelled</td><td>unknown</td><td>content</td></tr></table>")
    extraction = attest(data, config, items)
    inventory = extraction.engine["validation"]["source_table_inventory"]["detail"]["PADDLE_STRUCTURED"]
    assert any(reason.startswith("SOURCE_TABLE_SCOPE_UNRESOLVED:") for reason in inventory["reasons"])
    assert not extraction.engine["validation"]["table_coverage"]["detail"]


def test_valid_cell_with_unresolved_null_signal_still_blocks_attestation():
    config, items, data = paired(rows=1)
    cell = data["payload"]["fields"]["reference"]
    cell["observation_policy"] = {"version": "TRACKVANCE_OBSERVATION_UNCERTAINTY_098_V3", "unresolved": True}
    extraction = attest(data, config, items)
    assert "QUALITY_OBSERVATION_UNCERTAINTY:headers.0.reference" in extraction.engine["validation"]["issues"]
    assert quality_report(extraction, config)["status"] == "REVIEW_REQUIRED"


def test_old_v2_signed_contract_remains_readable_without_reclassifying_its_payload():
    config, items, data = paired(rows=1)
    extraction = attest(data, config, items)
    proof = extraction.engine["validation"]
    proof["validator_version"] = LEGACY_VALIDATOR_VERSION
    proof["attestation_sha256"] = canonical_hash({key: value for key, value in proof.items() if key != "attestation_sha256"})
    before = copy.deepcopy(extraction.payload)
    assert quality_report(extraction, config)["status"] == "AUTO_VERIFIED"
    assert quality_report(extraction, config)["validator_version"] == LEGACY_VALIDATOR_VERSION
    assert extraction.payload == before
    proof["validator_version"] = "UNRECOGNIZED_EVIDENCE_VERSION"
    proof["attestation_sha256"] = canonical_hash({key: value for key, value in proof.items() if key != "attestation_sha256"})
    assert quality_report(extraction, config)["status"] == "REVIEW_REQUIRED"


@pytest.mark.parametrize("optional_absence", [False, True])
def test_standard_policy_cannot_verify_a_value_or_absence_not_in_its_raw_source(optional_absence):
    config = definition()
    config["fields"][0]["required"] = not optional_absence
    items = events(selection())
    data = replay(items, config)
    original = data["payload"]["fields"]["reference"]
    data["payload"]["fields"]["reference"] = _cell(None if optional_absence else "FORGED",
        config["fields"][0], (original.get("origin") or {}).get("page"), original["evidence_refs"][0])
    extraction = attest(data, config, items)
    assert "QUALITY_SOURCE_VALUE_BINDING:headers.0.reference" in extraction.engine["validation"]["issues"]
    assert quality_report(extraction, config)["status"] == "REVIEW_REQUIRED"
