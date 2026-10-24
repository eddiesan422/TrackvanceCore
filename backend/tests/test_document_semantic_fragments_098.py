"""Source selection cannot invent strings, offsets, money or approval evidence."""
import copy
import math

import pytest

from trackvance import document_local_inference as local
from trackvance.document_providers import EngineConfiguration
from trackvance.document_quality import cell_is_valid, certify_candidate, quality_report
from trackvance.document_semantic_fragments import (
    ESCAPED_LINE_BREAK,
    MAX_FRAGMENTS,
    build_catalog,
    canonical_observed,
    escaped_break_normalization,
)
from trackvance.operations_common import OperationError


def line(text):
    return {"text": text, "page": 1, "region": [.1, .1, .9, .3], "method": "OCR", "confidence": None}


def cell(value):
    return {"value": value, "observed": value, "validity": "VALID" if value is not None else "MISSING",
        "origin": {key: line("")[key] for key in ("page", "region", "method")} if value is not None else None,
        "confidence": None, "reviewed": False}


def fixture():
    definition = {"fields": [{"name": "invoice", "logical_type": "STRING", "required": True}],
        "tables": [{"name": "items", "fields": [{"name": "concept", "logical_type": "STRING", "required": True}],
            "visual": {"multiline_column": 0, "flowing": True, "row_start": {"column": 0, "kind": "ASCII_VISIBLE"}}}]}
    observations = [line("Invoice: 0001"), line("AA" + ESCAPED_LINE_BREAK + "Última milla")]
    payload = {"fields": {"invoice": cell(None)}, "tables": {"items": []}, "table_unparsed": {"items": []},
        "lines": observations, "pages": [1], "arithmetic": []}
    return definition, payload


def selection(catalog):
    def identity(text):
        return next(f["fragment_id"] for f in catalog["fragments"] if f["text"] == text)
    return {"fields": {"invoice": {"fragment_id": identity("0001")}},
        "tables": {"items": [{"concept": {"fragment_id": identity("AA" + ESCAPED_LINE_BREAK + "Última milla")}}]}}


def test_catalog_is_deterministic_exact_offsets_and_geometry_bound():
    observations = [line("  A | A\tÚltima 😀: 12,50  ")]
    before = copy.deepcopy(observations)
    catalog = build_catalog(observations)
    assert catalog == build_catalog(observations) and observations == before
    for fragment in catalog["fragments"]:
        assert fragment["text"] == observations[fragment["evidence_id"]]["text"][fragment["start"]:fragment["end"]]
    repeated = [f for f in catalog["fragments"] if f["text"] == "A"]
    assert len(repeated) == 2 and repeated[0]["fragment_id"] != repeated[1]["fragment_id"]
    observations[0]["region"][0] += .01
    assert build_catalog(observations)["sha256"] != catalog["sha256"]


@pytest.mark.parametrize("observations", [None, {}, [None], [{"text": False}], [line("x"), {**line("y"), "confidence": math.nan}]])
def test_catalog_rejects_malformed_or_nonfinite_source(observations):
    with pytest.raises(ValueError, match="LLM_FRAGMENT_INTEGRITY"):
        build_catalog(observations)


def test_catalog_limits_fail_without_truncating_or_mutating_source():
    observations = [line(" ".join(["A"] * (MAX_FRAGMENTS + 1)))]
    before = copy.deepcopy(observations)
    with pytest.raises(ValueError, match="LLM_FRAGMENT_LIMIT"):
        build_catalog(observations)
    assert observations == before
    with pytest.raises(ValueError, match="LLM_FRAGMENT_LIMIT"):
        build_catalog([line("x" * 65536), line("y" * 65536)])


def test_selected_source_retains_escape_and_records_declared_text_composition():
    definition, payload = fixture()
    catalog = build_catalog(payload["lines"])
    result = local.apply_fragment_semantics(payload, selection(catalog), definition, payload["lines"],
        catalog, local.semantic_targets(payload, definition))
    concept = result["tables"]["items"][0]["concept"]
    assert concept["observed"] == "AA" + ESCAPED_LINE_BREAK + "Última milla"
    assert concept["value"] == "AA Última milla"
    assert canonical_observed(concept, allow_multiline=True) == concept["value"]
    assert concept["source_fragment"]["catalog_sha256"] == catalog["sha256"]
    assert cell_is_valid(concept, definition["tables"][0]["fields"][0], allow_multiline=True)
    assert not cell_is_valid(concept, definition["tables"][0]["fields"][0])
    assert payload["tables"]["items"] == []


def test_composition_still_needs_independent_original_read_and_row_coverage():
    from types import SimpleNamespace

    definition, payload = fixture()
    catalog = build_catalog(payload["lines"])
    candidate = local.apply_fragment_semantics(payload, selection(catalog), definition, payload["lines"],
        catalog, local.semantic_targets(payload, definition))
    independent = {"payload": {**copy.deepcopy(payload), "fields": {"invoice": cell("0001")},
        "tables": {"items": [{"concept": cell("AA Última milla")}]},
        "lines": [line("Invoice: 0001"), line("AA Última milla")]}, "engine": {"key": "INDEPENDENT_CPU"}}
    data = {"payload": candidate, "engine": {"route": "PADDLE_QWEN", "independent_row_coverage": False}}
    proof = certify_candidate(data, independent, definition, "a" * 64)
    assert "items.0.concept" in proof["verified_cells"]
    data["engine"]["validation"] = proof
    report = quality_report(SimpleNamespace(payload=candidate, engine=data["engine"], reviewed=False), definition)
    assert report["status"] == "REVIEW_REQUIRED" and report["checks"]["row_coverage"] is False
    # A printed literal backslash+n remains different in the independent reader.
    independent["payload"]["tables"]["items"][0]["concept"] = cell(candidate["tables"]["items"][0]["concept"]["observed"])
    independent["payload"]["lines"] = copy.deepcopy(payload["lines"])
    proof = certify_candidate(data, independent, definition, "a" * 64)
    assert "items.0.concept" not in proof["verified_cells"]


def test_text_composition_cannot_change_monetary_or_undeclared_cells():
    raw = "12" + ESCAPED_LINE_BREAK + "34"
    item = {**cell("1234"), "observed": raw, "source_text_normalization": escaped_break_normalization(raw)}
    assert not cell_is_valid(item, {"logical_type": "DECIMAL"}, allow_multiline=True)
    item = {**cell("AA B"), "observed": "AA" + ESCAPED_LINE_BREAK + "B",
        "source_text_normalization": {"kind": "invented", "observed_sha256": "0" * 64}}
    assert not cell_is_valid(item, {"logical_type": "STRING"}, allow_multiline=True)


@pytest.mark.parametrize("identity", [True, -1, 1000000, "1", {"observed": "invented"}])
def test_selection_rejects_noninteger_out_of_range_or_generated_text(identity):
    definition, payload = fixture()
    catalog = build_catalog(payload["lines"])
    chosen = selection(catalog)
    chosen["fields"]["invoice"]["fragment_id"] = identity
    with pytest.raises(OperationError) as caught:
        local.apply_fragment_semantics(payload, chosen, definition, payload["lines"], catalog,
            local.semantic_targets(payload, definition))
    assert caught.value.code == "DOCUMENT_LLM_EVIDENCE_INVALID"


def test_forged_catalog_and_target_mask_cannot_override_source_or_valid_cells():
    definition, payload = fixture()
    catalog = build_catalog(payload["lines"])
    chosen = selection(catalog)
    forged = copy.deepcopy(catalog)
    forged["fragments"][0]["text"] = "forged"
    with pytest.raises(OperationError):
        local.apply_fragment_semantics(payload, chosen, definition, payload["lines"], forged,
            local.semantic_targets(payload, definition))
    payload["fields"]["invoice"] = cell("0001")
    targets = local.semantic_targets(payload, definition)
    assert targets["fields"] == []
    with pytest.raises(OperationError):
        local.apply_fragment_semantics(payload, chosen, definition, payload["lines"], catalog, targets)
    chosen["fields"]["invoice"]["fragment_id"] = None
    result = local.apply_fragment_semantics(payload, chosen, definition, payload["lines"], catalog, targets)
    assert result["fields"]["invoice"] == payload["fields"]["invoice"]


def test_complete_tables_stay_untouched_and_explicit_conflicts_remain_targets():
    definition, payload = fixture()
    payload["tables"]["items"] = [{"concept": cell("AA") }]
    targets = local.semantic_targets(payload, definition)
    assert targets["tables"] == []
    catalog = build_catalog(payload["lines"])
    chosen = selection(catalog)
    chosen["tables"]["items"] = []
    result = local.apply_fragment_semantics(payload, chosen, definition, payload["lines"], catalog, targets)
    assert result["tables"] == payload["tables"] and not result["semantic_conflicts"]
    payload["semantic_conflicts"] = ["TABLE:items", "REEXTRACTION_FIELD:invoice"]
    assert local.semantic_targets(payload, definition) == {"fields": ["invoice"], "tables": ["items"]}


def test_new_default_is_v2_and_explicit_v1_snapshot_stays_v1():
    assert EngineConfiguration().prompt_version == "trackvance-idp-098-v2"
    assert EngineConfiguration(prompt_version="trackvance-idp-098-v1").model_dump()["prompt_version"] == "trackvance-idp-098-v1"


@pytest.mark.parametrize("corrupt_catalog", [False, True])
def test_coordinator_v2_binds_catalog_and_target_schema_before_merging(tmp_path, monkeypatch, corrupt_catalog):
    definition, payload = fixture()
    source = tmp_path / "document.pdf"
    source.write_bytes(b"synthetic model contract, no inference in this test")
    endpoints = []

    def runner(*args, **kwargs):
        return {"payload": copy.deepcopy(payload), "engine": {"key": "CONFINED_CONTRACT_DOUBLE"}}

    def invoke(endpoint, body, **kwargs):
        endpoints.append(endpoint)
        assert body["configuration"]["prompt_version"] == "trackvance-idp-098-v2"
        if endpoint == "/extract":
            return {"source_sha256": body["source_sha256"], "lines": payload["lines"],
                "engine": {"independent_row_coverage": False, "phases": {"paddle_seconds": .1}}}
        assert endpoint == "/interpret"
        catalog = build_catalog(body["observations"])
        assert body["semantic_targets"] == local.semantic_targets(payload, definition)
        assert body["semantic_schema"] == local.fragment_schema(definition, catalog, body["semantic_targets"])
        return {"source_sha256": body["source_sha256"], "observations_sha256": body["observations_sha256"],
            "fragment_catalog_sha256": "0" * 64 if corrupt_catalog else catalog["sha256"],
            "fragment_catalog_version": catalog["version"], "semantics": selection(catalog),
            "engine": {"phases": {"llm_seconds": .1}}}

    monkeypatch.setattr(local, "_invoke", invoke)
    configuration = {"prompt_version": "trackvance-idp-098-v2", "max_retries": 0}
    if corrupt_catalog:
        with pytest.raises(OperationError) as caught:
            local.run_local(source, definition, configuration, legacy_runner=runner, check=lambda _: None)
        assert caught.value.code == "DOCUMENT_LLM_EVIDENCE_INVALID"
        assert caught.value.details["local_inference"]["failed_stage"] == "INTERPRET"
    else:
        result = local.run_local(source, definition, configuration, legacy_runner=runner, check=lambda _: None)
        assert result["payload"]["tables"]["items"][0]["concept"]["value"] == "AA Última milla"
        assert result["engine"]["route"] == "PADDLE_QWEN"
        assert result["engine"]["independent_row_coverage"] is False
    assert endpoints == ["/extract", "/interpret"]
