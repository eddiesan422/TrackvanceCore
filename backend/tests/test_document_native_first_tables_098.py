"""Measured native table readiness is candidate selection, never approval."""
import base64
import copy
import hashlib
import json

import pytest
import test_document_semantic_corroboration_098 as source_fixtures

from trackvance.document_automatic_verification import source_context
from trackvance.document_engine import extract
from trackvance.document_lossless import (
    _native_events,
    _native_table_projection,
    adapt_saved_recognition,
    map_representation,
)
from trackvance.document_normalization import canonical_hash
from trackvance.document_semantic_corroboration import _authenticate, semantic_source_context
from trackvance.document_semantic_evidence import annotate_semantic_payload
from trackvance.document_semantic_general import (
    compile_profile,
    configuration_for,
    native_semantic_sufficient,
)


@pytest.fixture(autouse=True)
def opt_in(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", "true")


def fixture(*, rows=None, version="SEMANTIC_AUTO_V2", route="NATIVE_FIRST"):
    profile = {"id": "native-columns-fixture", "revision": 1, "name": "Measured columns",
        "document_type": "CUSTOM", "description": "Source inventory readiness fixture without model approval",
        "fields": [{"name": "invoice_number", "label": "Invoice number"}],
        "tables": [{"name": "items", "description": "Every measured physical source row", "fields": [{"name": "description", "label": "Description"},
            {"name": "amount", "label": "Amount", "logical_type": "DECIMAL"}]}]}
    definition = compile_profile(profile, route=route, automatic_verification=version)
    configuration = configuration_for(definition, {})
    lines = [source_fixtures._line("Invoice number: INV-7", .05)]
    for i, values in enumerate([["Description", "Amount"], *(rows or [["Charge", "10"], ["Other", "20"]])]):
        left = source_fixtures._line(values[0], .2 + i * .04)
        right = source_fixtures._line(values[1], .2 + i * .04, left=.65)
        words = left["words"] + right["words"]
        lines.append({**left, "text": " ".join(w["text"] for w in words), "words": words,
            "region": [words[0]["region"][0], left["region"][1], words[-1]["region"][2], left["region"][3]]})
    native = {"payload": extract(lines, definition), "engine": {"pages": 1, "key": "POPPLER_TESSERACT_CPU_V1"}}
    native["payload"].update(page_geometry={"1": {"width": 600, "height": 800, "unit": "PDF_POINT"}}, native_incomplete_pages=[])
    return definition, configuration, native


def probe(native, definition, configuration):
    raw = json.dumps(native, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    event = {"version": 1, "kind": "PHASE", "sequence": 1, "request_id": "fixture-probe", "attempt_id": "native-fixture",
        "source_sha256": "a" * 64, "configuration_sha256": canonical_hash(configuration), "definition_sha256": canonical_hash(definition),
        "stage": "NATIVE_SOURCE_PROBE", "page_one_based": None, "expected_pages": native["engine"]["pages"],
        "payload": {"source_page_count_verified": True, "artifacts": [{"kind": "NATIVE_SOURCE_PROBE_JSON",
            "media_type": "application/json", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "content_b64": base64.b64encode(raw).decode()}]}}
    event["event_sha256"] = canonical_hash(event)
    return event


def preview(native, definition, event):
    result = copy.deepcopy(native)
    _native_table_projection(result["payload"], definition, native=native, source_events=[event])
    return result


@pytest.mark.parametrize("rows", [None, [["A repeated", "10"], ["A repeated", "10"]], [["Different service", "-15.25"]]])
def test_complete_measured_native_table_is_ready_with_occurrences_and_actual_geometry(rows):
    definition, configuration, native = fixture(rows=rows)
    original = copy.deepcopy(native)
    event = probe(native, definition, configuration)
    result = preview(native, definition, event)
    assert native_semantic_sufficient(result, definition, source_probe=event)
    assert native == original and native["payload"]["tables"]["items"] == []
    assert all(not c["reviewed"] and c["geometry_kind"] == "MEASURED_SOURCE"
        for row in result["payload"]["tables"]["items"] for c in row.values())
    assert len(result["payload"]["row_provenance"]["items"]) == len(rows or [0, 1])
    assert "validation" not in result["engine"]


@pytest.mark.parametrize("change", ["omit", "extra", "duplicate", "reorder", "contradiction", "page", "observed"])
def test_candidate_must_match_every_occurrence_column_order_page_and_observation(change):
    definition, configuration, native = fixture()
    event = probe(native, definition, configuration)
    result = preview(native, definition, event)
    rows = result["payload"]["tables"]["items"]
    if change == "omit":
        rows.pop()
    elif change == "extra":
        rows.append(copy.deepcopy(rows[0]))
    elif change == "duplicate":
        rows[1] = copy.deepcopy(rows[0])
    elif change == "reorder":
        rows.reverse()
    elif change == "contradiction":
        rows[0]["amount"]["value"] = "999"
    elif change == "page":
        rows[0]["amount"]["origin"]["page"] = 2
    else:
        rows[0]["amount"]["observed"] = "999"
    assert not native_semantic_sufficient(result, definition, source_probe=event)


@pytest.mark.parametrize("change", ["no_words", "gap", "scanned", "ambiguous_column", "partial_row", "closing_body", "section"])
def test_incomplete_or_ambiguous_measured_source_still_requires_fallback(change):
    definition, configuration, native = fixture()
    if change == "no_words":
        native["payload"]["lines"][2].pop("words")
    elif change == "gap":
        native["payload"]["native_incomplete_pages"] = [1]
    elif change == "scanned":
        native["payload"]["lines"][2]["method"] = "OCR"
    elif change == "ambiguous_column":
        native["payload"]["lines"][1]["text"] = "Description Description"
    elif change == "partial_row":
        native["payload"]["lines"].append(source_fixtures._line("Unclassified continuation", .5))
    elif change == "closing_body":
        native["payload"]["lines"].append(source_fixtures._line("Closing balance: 30", .5))
    else:
        native["payload"]["lines"].append(source_fixtures._line("Separate schedule", .5))
    event = probe(native, definition, configuration)
    result = preview(native, definition, event)
    assert not native_semantic_sufficient(result, definition, source_probe=event)


@pytest.mark.parametrize("version,route", [("OFF", "NATIVE_FIRST"), ("SEMANTIC_AUTO_V1", "NATIVE_FIRST"), ("SEMANTIC_AUTO_V2", "QWEN_VISUAL")])
def test_existing_contracts_and_visual_route_do_not_gain_native_table_fast_path(version, route):
    definition, configuration, native = fixture(version=version, route=route)
    event = probe(native, definition, configuration)
    result = preview(native, definition, event)
    assert result == native
    assert not native_semantic_sufficient(result, definition, source_probe=event)


def test_probe_and_native_page_originals_are_bound_and_certifier_regenerates_candidate():
    definition, configuration, native = fixture()
    event = probe(native, definition, configuration)
    events = [event, *_native_events(native, "a" * 64, canonical_hash(configuration),
        {"attempt_id": "native-fixture"}, definition_sha256=canonical_hash(definition))]
    source_fixtures._reseal(events)
    data = adapt_saved_recognition(events, definition, configuration, source_sha256="a" * 64,
        attempt_context={"expected_pages": 1})
    context = semantic_source_context(events, source_context(events, "a" * 64))
    _ir, controls, errors = _authenticate(data, definition, "a" * 64, context, lambda: None)
    assert not errors
    regenerated = copy.deepcopy(controls[0]["payload"])
    annotate_semantic_payload(regenerated, definition)
    assert regenerated["tables"] == data["payload"]["tables"]
    assert regenerated["fields"] == data["payload"]["fields"]
    assert len(data["payload"]["tables"]["items"]) == 2
    original = copy.deepcopy(event)
    event["payload"]["artifacts"][0]["sha256"] = "b" * 64
    assert not native_semantic_sufficient(preview(native, definition, event), definition, source_probe=event)
    changed = copy.deepcopy(native)
    changed["payload"]["lines"][2]["text"] += " altered"
    assert not native_semantic_sufficient(preview(changed, definition, original), definition, source_probe=original)
    ir = data["payload"]["document_representation"]
    assert map_representation(ir, definition, stage="DIGITAL_NATIVE")["tables"]["items"] == []


def test_native_api_worker_reader_double_uses_registered_source_authority_without_models(authenticated, monkeypatch):
    from test_document_semantic_general_lifecycle_098 import upload_fixture

    import trackvance.document_recognition_transport as transport
    import trackvance.document_worker as worker

    definition, _configuration, native = fixture()
    family = authenticated.post("/api/v1/documents/families", json={"name": "Native measured table CPU fixture"}).json()["id"]
    activated = authenticated.post(f"/api/v1/documents/families/{family}/semantic-profile", json={
        "profile": definition["semantic_general"]["contract"], "route": "NATIVE_FIRST",
        "automatic_verification": "SEMANTIC_AUTO_V2", "reason": "Explicit measured native reader double; no model quality or automatic approval claim"})
    assert activated.status_code == 200, activated.text
    calls = []
    def no_model(*args, **kwargs):
        calls.append(kwargs.get("strategy"))
        raise AssertionError("A complete measured native candidate must not call a model")
    monkeypatch.setattr(worker, "run_engine", lambda *args, **kwargs: copy.deepcopy(native))
    monkeypatch.setattr(transport, "recognize_stream", no_model)
    original = upload_fixture(authenticated, family, label="measured-native-tables")
    assert worker.process_once("native-measured-table-unit-fixture")
    response = authenticated.get("/api/v1/documents/originals/" + original["id"])
    assert response.status_code == 200, response.text
    detail = response.json()
    assert calls == []
    assert detail["status"] == "PUBLISHED", detail
    assert detail["extraction"]["engine"]["route"] == "DIGITAL_NATIVE"
    assert len(detail["extraction"]["payload"]["tables"]["items"]) == 2
    assert detail["semantic_review"]["automatic_approval"]
    assert len(detail["publications"]) == 1 and len(detail["publications"][0]["versions"]) == 2
    assert all(not c["reviewed"] for row in detail["extraction"]["payload"]["tables"]["items"] for c in row.values())
