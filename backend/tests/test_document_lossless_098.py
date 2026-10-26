"""Lossless source structure, replay and explicit evidence-policy boundaries."""
import base64
import copy
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trackvance.document_engine import extract
from trackvance.document_inference_completion import AttemptBudget
from trackvance.document_lossless import adapt_saved_recognition, run_lossless
from trackvance.document_normalization import canonical_hash, normalize_observation
from trackvance.document_quality import cell_is_valid, quality_report, validate_candidate_contract
from trackvance.document_representation import parse_table_html
from trackvance.document_schemas import DocumentDefinition
from trackvance.document_validation_policy import certify_lossless_candidate
from trackvance.operations_common import OperationError


def definition(*, critical=False, normalization=None, table=False, policy=True):
    value = {"fields": [{"name": "reference", "label": "Reference", "critical": critical}],
        "publication": {"review_policy": "AUTO_VERIFIED"}}
    if normalization:
        value["fields"][0]["normalization"] = normalization
    if table:
        value["tables"] = [{"name": "detail", "row_pattern": r"(?P<code>\S+) (?P<description>.+)",
            "fields": [{"name": "code", "label": "Code", "critical": True},
                       {"name": "description", "label": "Description"}]}]
    if policy:
        value["validation_policy"] = {"allow_noncritical_source_bound": True}
    return DocumentDefinition.model_validate(value).model_dump(mode="json")


def artifact(kind, value):
    raw = value.encode() if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode()
    return {"kind": kind, "media_type": "application/json", "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw), "content_b64": base64.b64encode(raw).decode()}


def events(selection=None, *, raw=None, count=1, emitted=1, released=True, stage="QWEN_VISUAL"):
    result = []
    for page in range(1, emitted + 1):
        payload = {"artifacts": [artifact("PADDLE_PAGE_JSON", raw)] if raw is not None else
            [artifact("QWEN_SELECTION_JSON", selection)],
            "page_geometry": {"width": 1000, "height": 1000, "unit": "PIXEL"}}
        event = {"version": 1, "kind": "PAGE", "sequence": len(result), "request_id": "request",
            "attempt_id": "attempt", "source_sha256": "a" * 64, "configuration_sha256": "b" * 64, "definition_sha256":"c" * 64,
            "stage": stage, "page_one_based": page, "expected_pages": count, "payload": payload}
        event["event_sha256"] = canonical_hash(event)
        result.append(event)
    terminal = {"version": 1, "kind": "TERMINAL", "sequence": len(result), "request_id": "request",
        "attempt_id": "attempt", "source_sha256": "a" * 64, "configuration_sha256": "b" * 64, "definition_sha256":"c" * 64,
        "stage": stage, "status": "COMPLETE", "expected_pages": count, "emitted_pages": list(range(1, emitted + 1)),
        "pending_pages": [], "resources_released": released, "payload": {}}
    terminal["event_sha256"] = canonical_hash(terminal)
    return [*result, terminal]


def selection(value="R-001", rows=None):
    return {"fields": {"reference": {"observed": value, "ambiguous": False, "alternatives": []}},
        "tables": {"detail": rows} if rows is not None else {}}


def replay(items, config):
    if not any(item.get("stage") == "NATIVE_SOURCE_PROBE" for item in items):
        first = items[0]
        probe = {**{key:first[key] for key in ("version","request_id","attempt_id","source_sha256","configuration_sha256","definition_sha256","expected_pages")},
            "kind":"PHASE", "sequence":0, "stage":"NATIVE_SOURCE_PROBE", "page_one_based":None,
            "payload":{"source_page_count_verified":True, "artifacts":[artifact("NATIVE_SOURCE_PROBE_JSON",
                {"engine":{"pages":first["expected_pages"]}, "payload":{"native_incomplete_pages":[]}})]}}
        probe["event_sha256"] = canonical_hash(probe)
        items.insert(0, probe)
    return adapt_saved_recognition(items, config, {"extraction_strategy": "QWEN_VISUAL"}, source_sha256="a" * 64)


def attest(data, config, items, independent=None):
    context = {"source_sha256": "a" * 64, "expected_pages": items[0]["expected_pages"],
        "persisted_event_hashes": [item["event_sha256"] for item in items],
        "artifacts": [{"artifact_id":"durable", "kind":artifact["kind"], "sha256":artifact["sha256"],
            "page":item.get("page_one_based"), "stage":item["stage"]} for item in items
            for artifact in item["payload"].get("artifacts", [])],
        "resources_released": True}
    data["engine"]["validation"] = certify_lossless_candidate(data, config, "a" * 64,
        source_context=context, independent=independent)
    return SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False)


def test_html_grid_retains_spans_linebreaks_pipes_empty_and_missing():
    html = '<table><tr><th rowspan="2">Code</th><th colspan="2">Data</th></tr><tr><td>A|B<br>C</td><td></td></tr><tr><td>009</td></tr></table>'
    table = parse_table_html(html)[0]
    assert table["original_html"] == html
    assert table["rows"][1]["slots"][0]["state"] == "SPAN"
    assert table["cells"][2]["text"] == "A|B\nC"
    assert table["cells"][3]["state"] == "EMPTY"
    assert table["rows"][2]["slots"][1]["state"] == "MISSING"
    assert all(cell["geometry"] is None for cell in table["cells"])


def test_old_definition_serialization_has_no_opt_in_defaults():
    old = definition(policy=False)
    assert "validation_policy" not in old
    assert "normalization" not in old["fields"][0]
    assert DocumentDefinition.model_validate(old).model_dump(mode="json") == old


def test_sign_normalization_is_declared_and_preserves_observed():
    field = {"logical_type": "DECIMAL", "decimal_separator": ",", "thousands_separator": ".", "required": True}
    assert normalize_observation("-1.234,50", field)[:2] == ("-1234.50", "VALID")
    field["normalization"] = {"numeric_semantics": "DEDUCTION_MAGNITUDE", "decimal_scale": 2}
    value, validity, trace = normalize_observation("-1.234,50", field)
    assert (value, validity) == ("1234.50", "VALID")
    assert trace["observed"] == "-1.234,50" and trace["rule"] == "DEDUCTION_MAGNITUDE"
    assert normalize_observation("1,239", field)[1] == "INVALID"


def test_currency_equivalences_resolve_presentation_without_guessing_symbol():
    config = definition(normalization={"currency_equivalences": {"COP": ["Peso colombiano", "COP - Peso colombiano"]}})
    first = events(selection("COP"), count=2)[0]
    second = events(selection("COP - Peso colombiano"), count=2)[0]
    second.update(page_one_based=2, sequence=1)
    second["event_sha256"] = canonical_hash({k:v for k,v in second.items() if k != "event_sha256"})
    data = replay([first, second, events(selection(), count=2, emitted=2)[-1]], config)
    cell = data["payload"]["fields"]["reference"]
    assert cell["value"] == "COP" and cell["validity"] == "VALID"
    assert cell_is_valid(cell, config["fields"][0])
    assert normalize_observation("$", config["fields"][0])[1] == "AMBIGUOUS"
    bad = {"currency_equivalences": {"COP": ["$"], "USD": ["$"]}}
    with pytest.raises(ValidationError):
        definition(normalization=bad)
    with pytest.raises(ValidationError):
        definition(normalization={"currency_equivalences": {"USD": ["$"]}})


def test_replay_keeps_ambiguous_response_and_leading_zero_identifier():
    config = definition()
    value = selection("0007")
    items = events(value)
    data = replay(items, config)
    assert data["payload"]["fields"]["reference"]["value"] == "0007"
    value["fields"]["reference"].update(ambiguous=True, alternatives=["0007", "0077"])
    other = replay(events(value), config)
    assert other["payload"]["fields"]["reference"]["observed"] == "0007"
    assert other["payload"]["fields"]["reference"]["value"] is None
    assert items == copy.deepcopy(items)
    validate_candidate_contract(data, config)


def test_paddle_table_mapping_never_repairs_shifted_columns():
    config = definition(table=True)
    html = '<table><tr><th>Code</th><th>Description</th></tr><tr><td></td><td>001</td></tr><tr><td>001</td><td>Same product</td></tr><tr><td>001</td><td>Same product</td></tr></table>'
    raw = {"page_index": 0, "parsing_res_list": [{"block_label": "table", "block_content": html, "block_bbox": [0,0,1000,1000]}]}
    data = replay(events(raw=raw, stage="PADDLE_STRUCTURED"), config)
    rows = data["payload"]["tables"]["detail"]
    assert len(rows) == 3 and rows[0]["code"]["observed"] == "" and rows[0]["description"]["observed"] == "001"
    assert len({row["occurrence_id"] for row in data["payload"]["row_provenance"]["detail"]}) == 3
    assert all(cell["geometry_kind"] == "QUERY_REGION" for row in rows for cell in row.values())


def test_source_binding_policy_can_approve_declared_noncritical_scanned_field():
    config, items = definition(), events(selection())
    data = replay(items, config)
    extraction = attest(data, config, items)
    assert quality_report(extraction, config)["status"] == "AUTO_VERIFIED"
    assert not extraction.reviewed and not extraction.payload["fields"]["reference"]["reviewed"]
    extraction.payload["fields"]["reference"]["value"] = "OTHER"
    assert quality_report(extraction, config)["status"] == "REVIEW_REQUIRED"


def test_critical_code_not_verified_by_valid_json_or_same_model_repeat():
    config, items = definition(critical=True), events(selection())
    data = replay(items, config)
    data["payload"]["candidate_history"] = [{"stage": "QWEN_REPEAT", "fields": copy.deepcopy(data["payload"]["fields"]), "tables": {}}]
    extraction = attest(data, config, items)
    assert quality_report(extraction, config)["status"] == "REVIEW_REQUIRED"
    assert "QUALITY_INDEPENDENT_SOURCE_REQUIRED:headers.0.reference" in extraction.engine["validation"]["issues"]


def test_independent_row_matching_retains_repeated_occurrences():
    config = definition(table=True)
    row = {name:{"observed":value,"ambiguous":False,"alternatives":[]} for name,value in {"code":"0001","description":"Repeated"}.items()}
    items = events(selection(rows=[row, row]))
    data = replay(items, config)
    control = {"payload": copy.deepcopy(data["payload"])}
    extraction = attest(data, config, items, control)
    assert quality_report(extraction, config)["status"] == "AUTO_VERIFIED"
    control["payload"]["tables"]["detail"].pop()
    extraction = attest(data, config, items, control)
    assert "QUALITY_ROW_COVERAGE:detail" in extraction.engine["validation"]["issues"]


def test_missing_pages_and_unverified_close_block_approval():
    config = definition()
    for items in [events(selection(), count=2), events(selection(), released=False)]:
        data = replay(items, config)
        extraction = attest(data, config, items)
        assert quality_report(extraction, config)["status"] == "REVIEW_REQUIRED"


def test_native_route_replays_and_never_calls_model(tmp_path):
    config = definition()
    lines = [{"text":"Reference: R-001","page":1,"region":[.1,.1,.9,.2],"method":"DIGITAL","confidence":None}]
    native = {"payload":extract(lines, config),"engine":{"pages":1,"key":"NATIVE"}}
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-synthetic")
    calls, persisted = [], []
    def recognize(*args, **kwargs):
        calls.append(args)
        raise AssertionError("NATIVE_MUST_NOT_CALL_MODEL")
    data = run_lossless(source, config, {"extraction_strategy":"NATIVE_FIRST"},
        legacy_runner=lambda *args,**kwargs:copy.deepcopy(native), check=lambda _:None,
        budget=None, recognize=recognize, on_event=lambda event:persisted.append(copy.deepcopy(event)))
    assert not calls and data["engine"]["route"] == "DIGITAL_NATIVE"
    assert next(event for event in persisted if event["kind"] == "PAGE")["payload"]["artifacts"][1]["kind"] == "NATIVE_DOCUMENT_JSON"
    result = adapt_saved_recognition(persisted, config, {"extraction_strategy":"NATIVE_FIRST"},
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    assert result["payload"]["fields"] == data["payload"]["fields"]


def test_selective_route_cannot_overlap_when_resources_not_released(tmp_path):
    config = definition(critical=True)
    native = {"payload":extract([], config),"engine":{"pages":1}}
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-synthetic")
    calls = []
    def recognize(source, definition, configuration, **kwargs):
        calls.append(kwargs["strategy"])
        items = events(selection(), released=False)
        for item in items:
            item["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
            item["configuration_sha256"] = canonical_hash(configuration)
            item["definition_sha256"] = canonical_hash(definition)
            item["event_sha256"] = canonical_hash({key:value for key,value in item.items() if key != "event_sha256"})
            kwargs["on_event"](item)
        return {"pages":items[:-1],"terminal":items[-1],"engine":{}}
    data = run_lossless(source, config, {"extraction_strategy":"SELECTIVE"},
        legacy_runner=lambda *args,**kwargs:copy.deepcopy(native),check=lambda _:None,
        budget=AttemptBudget(300),recognize=recognize)
    assert calls == ["QWEN_VISUAL"] and data["engine"]["secondary_route_blocked"]


@pytest.mark.parametrize("native_text", [True, False])
def test_reference_discovery_with_empty_definition_uses_native_or_structured_only(tmp_path, native_text):
    config = {"fields":[], "tables":[], "arithmetic":[], "cross_totals":[]}
    lines = [{"text":"Heading: native", "page":1, "region":[0,0,1,1], "method":"DIGITAL", "confidence":None}] if native_text else []
    native = {"payload":extract(lines, config), "engine":{"pages":1}}
    source = tmp_path/"reference.pdf"
    source.write_bytes(b"%PDF-reference")
    calls = []
    durable = {"manifest_artifact_id":"durable"}
    def recognize(source, definition, configuration, **kwargs):
        calls.append(kwargs["strategy"])
        items = events(raw={"parsing_res_list":[{"block_label":"text", "block_content":"Heading: scanned"}]}, stage="PADDLE_STRUCTURED")
        for item in items:
            item["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
            item["configuration_sha256"] = canonical_hash(configuration)
            item["definition_sha256"] = canonical_hash(definition)
            item["event_sha256"] = canonical_hash({key:value for key,value in item.items() if key != "event_sha256"})
            assert kwargs["on_event"](item) == durable
        return {"pages":items[:-1], "terminal":items[-1], "engine":{}}
    data = run_lossless(source, config, {"extraction_strategy":"SELECTIVE"}, legacy_runner=lambda *args,**kwargs:copy.deepcopy(native),
        check=lambda _:None, budget=None, on_event=lambda _:durable, attempt_context={"purpose":"REFERENCE_DISCOVERY"}, recognize=recognize)
    route = "DIGITAL_NATIVE" if native_text else "PADDLE_STRUCTURED"
    assert calls == ([] if native_text else ["PADDLE_STRUCTURED"])
    assert data["engine"]["purpose"] == "REFERENCE_DISCOVERY" and data["engine"]["effective_route"] == route
    assert data["payload"]["lines"][0]["text"] == ("Heading: native" if native_text else "Heading: scanned")


def test_empty_and_absent_source_cells_remain_distinct_and_required_missing():
    config = definition()
    empty = replay(events(selection("")), config)["payload"]["fields"]["reference"]
    absent = replay(events(selection(None)), config)["payload"]["fields"]["reference"]
    assert empty["observed"] == "" and absent["observed"] is None
    assert empty["value"] is absent["value"] is None
    assert empty["validity"] == absent["validity"] == "MISSING"


def test_replay_rejects_mutated_event_or_artifact_before_adaptation():
    config, items = definition(), events(selection())
    items[0]["payload"]["artifacts"][0]["sha256"] = "0" * 64
    with pytest.raises(OperationError) as first:
        replay(items, config)
    assert first.value.code == "DOCUMENT_RECOGNITION_INTEGRITY"
    page = next(item for item in items if item["kind"] == "PAGE")
    page["event_sha256"] = canonical_hash({key:value for key,value in page.items() if key != "event_sha256"})
    with pytest.raises(OperationError) as second:
        replay(items, config)
    assert second.value.code == "DOCUMENT_RECOGNITION_INTEGRITY"


def test_declared_continuation_preserves_rows_and_rejects_unbound_table():
    config = definition(table=True)
    config["tables"][0]["visual"] = {"page":1,"region":[0,0,1,1],"header_labels":["Code","Description"],
        "column_bounds":[0,.4,1],"flowing":True,"continue_pages":True,
        "row_start":{"column":0,"separator":"-","segments":[{"alphabet":"ASCII_ALPHA","minimum":2,"maximum":2},
            {"alphabet":"ASCII_DIGIT","minimum":3,"maximum":3}]}}
    first = {"parsing_res_list":[{"block_label":"table","block_content":'<table><tr><th>Code</th><th>Description</th></tr><tr><td>AB-001</td><td>A<br>B</td></tr></table>'}]}
    second = {"parsing_res_list":[{"block_label":"table","block_content":'<table><tr><td>AB-001</td><td>Repeated code</td></tr></table>'}]}
    left = events(raw=first, stage="PADDLE_STRUCTURED", count=2)[0]
    right = events(raw=second, stage="PADDLE_STRUCTURED", count=2)[0]
    right.update(page_one_based=2, sequence=1)
    right["event_sha256"] = canonical_hash({key:value for key,value in right.items() if key != "event_sha256"})
    terminal = events(raw=first, stage="PADDLE_STRUCTURED", count=2, emitted=2)[-1]
    data = replay([left,right,terminal], config)
    assert len(data["payload"]["tables"]["detail"]) == 2
    assert data["payload"]["tables"]["detail"][0]["description"]["observed"] == "A\nB"
    assert data["payload"]["table_continuations"][0]["rule"] == "DECLARED_CONTINUATION_AND_ROW_GRAMMAR"
    config["tables"][0]["visual"]["continue_pages"] = False
    blocked = replay([left,right,terminal], config)
    assert len(blocked["payload"]["tables"]["detail"]) == 1
    assert blocked["payload"]["table_unparsed"]["detail"][0]["reason"] == "UNASSOCIATED_SOURCE_TABLE"


def test_new_automatic_policy_requires_explicit_risk_contract():
    config, items = definition(policy=False), events(selection())
    data = replay(items, config)
    extraction = attest(data, config, items)
    assert quality_report(extraction, config)["status"] == "REVIEW_REQUIRED"
    assert "QUALITY_POLICY_REQUIRED" in quality_report(extraction, config)["issues"]


def test_critical_conflict_review_does_not_require_confirming_unaffected_cells():
    config = definition(table=True)
    config["tables"][0]["fields"].append({"name":"unit","label":"Unit","logical_type":"STRING","required":True})
    config["tables"][0]["row_pattern"] = r"(?P<code>\S+) (?P<description>.+) (?P<unit>\S+)"
    config = DocumentDefinition.model_validate(config).model_dump(mode="json")
    row = {key:{"observed":value,"ambiguous":False,"alternatives":[]} for key,value in
        {"code":"0099","description":"Unique description","unit":"EA"}.items()}
    first = events(selection(rows=[row]))
    raw = {"parsing_res_list":[{"block_label":"text","block_content":"Reference: R-001"},
        {"block_label":"table","block_content":'<table><tr><th>Code</th><th>Description</th><th>Unit</th></tr><tr><td>0009</td><td>Unique description</td><td>EA</td></tr></table>'}]}
    second = events(raw=raw, stage="PADDLE_STRUCTURED")
    items = [*first,*second]
    data = replay(items, config)
    assert data["payload"]["semantic_conflicts"] == ["CELL:detail.0.code"]
    extraction = attest(data, config, items)
    report = quality_report(extraction, config)
    assert report["cells"] == {"total":4,"verified":3,"pending":1}
    assert extraction.engine["validation"]["table_coverage"]["detail"] is True
    assert extraction.engine["validation"]["row_alignment"]["detail"][0]["basis"] == "UNIQUE_MULTI_COLUMN_SOURCE_PAGE"
    corrected = copy.deepcopy(extraction)
    corrected.reviewed = True
    corrected.payload["tables"]["detail"][0]["code"].update(value="0009", validity="VALID", reviewed=True)
    assert quality_report(corrected, config)["status"] == "HUMAN_VERIFIED"
    assert corrected.payload["tables"]["detail"][0]["description"]["reviewed"] is False


def structured_reference(*, heading=True):
    blocks = [{"block_label":"text", "block_content":"Invoice: 000001"}] if heading else []
    blocks.append({"block_label":"table", "block_bbox":[10,20,900,800], "block_content":
        '<table><tr><th>Code</th><th>Description</th></tr><tr><td>0009</td><td>A|B<br>C</td></tr><tr><td>0009</td><td>Repeated code</td></tr></table>'})
    data = replay(events(raw={"parsing_res_list":blocks}, stage="PADDLE_STRUCTURED"), {"fields":[],"tables":[]})
    return SimpleNamespace(id="structured-reference", payload=data["payload"])


def test_builder_discovery_proposes_and_extracts_original_grid_without_cell_boxes():
    from trackvance.document_builder_executor import _valid_request, _valid_result
    from trackvance.document_builder_workspace import builder_reference
    from trackvance.document_discovery import discover, extract_reference

    reference = structured_reference()
    request = {"operation":"DISCOVER", "references":[builder_reference(reference)]}
    assert _valid_request(request)
    result = discover([reference])
    assert _valid_result(result, request)
    table = result["definition"]["tables"][0]
    assert table["structured_header_labels"] == ["Code", "Description"]
    assert table["visual"] is None and table["row_pattern"] == ""
    assert result["table_alternatives"][0]["geometry_kind"] == "UNKNOWN"
    mapped = extract_reference(reference, result["definition"])
    assert [row["code"]["observed"] for row in mapped["tables"]["detalle"]] == ["0009", "0009"]
    assert mapped["tables"]["detalle"][0]["description"]["observed"] == "A|B\nC"
    assert all(cell["geometry"] is None for page in mapped["document_representation"]["pages"] for source in page["tables"] for cell in source["cells"])
    assert _valid_result({reference.id:mapped}, {**request,"operation":"EXTRACT","definition":result["definition"]})
    tampered = copy.deepcopy(result)
    tampered["results"][reference.id]["document_representation"]["pages"][0]["tables"][0]["cells"][0]["text"] = "changed"
    assert not _valid_result(tampered, request)


def test_table_only_discovery_preserves_proposal_without_inventing_header_field():
    from trackvance.document_builder_executor import _valid_result
    from trackvance.document_builder_workspace import builder_reference
    from trackvance.document_discovery import discover

    reference = structured_reference(heading=False)
    result = discover([reference])
    assert result["definition"]["fields"] == [] and len(result["definition"]["tables"]) == 1
    assert result["results"] == {} and len(result["table_alternatives"][0]["source_body_row_ids"]) == 2
    assert _valid_result(result, {"operation":"DISCOVER", "references":[builder_reference(reference)]})
    with pytest.raises(ValidationError):
        DocumentDefinition.model_validate(result["definition"])


def test_other_stage_complete_cannot_certify_failed_primary_stage():
    first = events(selection())
    first[-1].update(status="FAILED", completed_pages=[], pending_pages=[1])
    first[-1]["event_sha256"] = canonical_hash({key:value for key,value in first[-1].items() if key != "event_sha256"})
    second = events(raw={"parsing_res_list":[{"block_label":"text","block_content":"Reference: R-001"}]}, stage="PADDLE_STRUCTURED")
    data = replay([*first,*second], definition())
    assert data["engine"]["execution"]["recognition_complete"] is False
    assert data["engine"]["execution"]["terminal_status"] == "FAILED"
    assert data["engine"]["execution"]["pending_pages"] == [1]


def test_replay_preserves_native_source_page_count_and_blocks_lost_probe():
    from trackvance.document_recognition_service import recognition_source_context

    config, items = definition(), events(selection(), count=2, emitted=1)
    replay(items, config)
    probe = items[0]
    for item in items[1:]:
        item["expected_pages"] = 1
        item["event_sha256"] = canonical_hash({key:value for key,value in item.items() if key != "event_sha256"})
    with pytest.raises(OperationError) as invalid:
        adapt_saved_recognition(items, config, {}, source_sha256="a"*64)
    assert invalid.value.code == "DOCUMENT_RECOGNITION_INTEGRITY"
    lost = items[1:]
    data = adapt_saved_recognition(lost, config, {}, source_sha256="a"*64)
    assert data["payload"]["fields"]["reference"]["observed"] == "R-001"
    assert data["engine"]["execution"]["recognition_complete"] is False
    for item in items:
        item["durable_refs"] = {"event_sha256":item["event_sha256"], "artifacts":[]}
    assert recognition_source_context(items, "a"*64)["expected_pages"] == probe["expected_pages"] == 2
    assert recognition_source_context(lost, "a"*64)["expected_pages"] == 0


def test_failed_secondary_stage_is_retained_without_independent_authority():
    config = definition(critical=True)
    first = events(selection())
    second = events(raw={"parsing_res_list":[{"block_label":"text","block_content":"Reference: R-001"}]}, stage="PADDLE_STRUCTURED")
    second[-1].update(status="FAILED", completed_pages=[], pending_pages=[1])
    second[-1]["event_sha256"] = canonical_hash({key:value for key,value in second[-1].items() if key != "event_sha256"})
    items = [*first,*second]
    data = replay(items, config)
    assert len(data["payload"]["candidate_history"]) == 2
    report = quality_report(attest(data, config, items), config)
    assert report["status"] == "REVIEW_REQUIRED" and report["cells"]["verified"] == 0


def test_invalid_decimal_alternative_remains_reviewable_without_coercion():
    config = definition()
    config["fields"][0]["logical_type"] = "DECIMAL"
    first = events(selection("not an amount"))
    second = events(raw={"parsing_res_list":[{"block_label":"text","block_content":"Reference: 10.00"}]}, stage="PADDLE_STRUCTURED")
    data = replay([*first,*second], config)
    cell = data["payload"]["fields"]["reference"]
    assert cell["observed"] == cell["value"] == "not an amount" and cell["validity"] == "INVALID"
    assert cell["candidate_alternatives"][-1]["candidate"]["value"] == "10.00"


@pytest.mark.skipif(sys.platform != "linux", reason="Installed builder confinement requires Linux")
def test_real_builder_child_discovers_and_extracts_saved_ir(tmp_path, monkeypatch):
    from trackvance import document_builder_executor as executor
    from trackvance.document_builder_workspace import builder_reference

    monkeypatch.setattr(executor.storage_provider, "temporary_path", lambda suffix:tmp_path/("owned"+suffix))
    references = [builder_reference(structured_reference())]
    result = executor.run_builder("DISCOVER", references)
    mapped = executor.run_builder("EXTRACT", references, result["definition"])
    assert mapped["structured-reference"]["tables"]["detalle"][0]["code"]["observed"] == "0009"
