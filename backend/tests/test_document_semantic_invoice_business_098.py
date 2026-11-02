"""Declared invoice coherence with measured synthetic source and native APIs.

All source numbers are explicitly printed in the test double. These tests do
not measure OCR quality, read evaluation references or calculate missing cells.
"""
import copy
import hashlib

import pytest
from pydantic import ValidationError
from test_document_semantic_auto_lifecycle_098 import independent_qwen_events
from test_document_semantic_general_098 import cell
from test_document_semantic_general_lifecycle_098 import all_paths, upload_fixture

from trackvance.document_engine import extract
from trackvance.document_normalization import canonical_hash
from trackvance.document_schemas import DocumentDefinition
from trackvance.document_semantic_business import business_report, proposal
from trackvance.document_semantic_general import catalog, compile_profile, profile_versions
from trackvance.document_worker import process_once


def invoice_profile():
    return {"id": "declared-invoice-unit", "name": "Declared invoice coherence unit", "revision": 1,
        "document_type": "INVOICE", "description": "Complete invoice without unrepresented adjustments; all declared roles printed.",
        "fields": [{"name": "invoice_number", "label": "Invoice number", "critical": True, "business_key": True},
            *[{"name": name, "label": label, "logical_type": "DECIMAL", "critical": True}
                for name, label in [("subtotal", "Subtotal"), ("tax", "Tax"), ("total", "Total")]]],
        "tables": [{"name": "items", "description": "Every printed line occurrence with quantity, unit price and line amount.",
            "fields": [{"name": "description", "label": "Description"},
                *[{"name": name, "label": label, "logical_type": "DECIMAL"} for name, label in
                    [("quantity", "Quantity"), ("unit_price", "Unit price"), ("amount", "Amount")]]]}]}


def controls():
    return proposal(invoice_profile())["controls"]


def enabled(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", "true")


def definition(monkeypatch):
    enabled(monkeypatch)
    return compile_profile(invoice_profile(), automatic_verification="SEMANTIC_AUTO_V1", business_controls=controls())


def test_catalog_proposes_explicit_scope_bound_to_profile_without_changing_off_contract(monkeypatch):
    enabled(monkeypatch)
    profile = next(profile for profile in profile_versions() if profile["id"] == "invoice-v1" and profile["revision"] == 3)
    before = compile_profile(profile)
    listed = next(item for item in catalog()["profiles"] if item["id"] == profile["id"] and item["revision"] == 3)
    offered = listed["automatic_business_controls"]
    assert offered["profile_sha256"] == listed["sha256"] == canonical_hash(profile)
    assert offered["controls_sha256"] == canonical_hash(offered["controls"])
    assert offered["explicit_scope_confirmation_required"] is True
    automatic = compile_profile(profile, automatic_verification="SEMANTIC_AUTO_V1", business_controls=offered["controls"])
    assert len(automatic["arithmetic"]) == 2 and len(automatic["cross_totals"]) == 1
    assert "business_controls" not in before["semantic_general"]
    assert compile_profile(profile) == before
    assert automatic["semantic_general"]["profile_sha256"] == before["semantic_general"]["profile_sha256"]


@pytest.mark.parametrize("mutation", ["hash", "rule", "role", "scope", "unpaired", "omitted_product"])
def test_declaration_cannot_omit_rules_forge_roles_or_assume_adjustments(monkeypatch, mutation):
    value = definition(monkeypatch)
    if mutation == "hash":
        value["semantic_general"]["business_controls_sha256"] = "f" * 64
    elif mutation == "rule":
        value["arithmetic"].pop()
    elif mutation == "omitted_product":
        value["semantic_general"]["business_controls"].update(quantity=None, unit_price=None)
        value["semantic_general"]["business_controls_sha256"] = canonical_hash(value["semantic_general"]["business_controls"])
    else:
        key = {"role": "subtotal", "scope": "scope", "unpaired": "quantity"}[mutation]
        value["semantic_general"]["business_controls"][key] = {"role": "invoice_number", "scope": "ASSUME_NO_ADJUSTMENTS", "unpaired": None}[mutation]
        value["semantic_general"]["business_controls_sha256"] = canonical_hash(value["semantic_general"]["business_controls"])
    with pytest.raises(ValidationError):
        DocumentDefinition.model_validate(value)


def normalized(value):
    return {"observed": value, "value": value, "validity": "VALID", "reviewed": False}


def test_partial_inventory_does_not_claim_a_source_sum_contradiction_and_missing_tax_is_not_zero(monkeypatch):
    schema = definition(monkeypatch)
    payload = {"fields": {name: normalized(value) for name, value in
        [("invoice_number", "INV-1"), ("subtotal", "10.00"), ("tax", "0.00"), ("total", "10.00")]},
        "tables": {"items": [{name: normalized(value) for name, value in
            [("description", "Widget"), ("quantity", "1.00"), ("unit_price", "10.00"), ("amount", "10.00")]}] * 2}}
    paths = {"headers.0." + name for name in payload["fields"]} | {
        f"items.{index}.{name}" for index, row in enumerate(payload["tables"]["items"]) for name in row}
    partial = business_report(payload, schema, verified_paths=paths, table_coverage={"items": False})
    row = next(check for check in partial["checks"] if check["name"] == "semantic_invoice_detail_subtotal_v1")
    assert row["computed_status"] == "DISCREPANCY" and row["status"] == "UNVERIFIED"
    complete = business_report(payload, schema, verified_paths=paths, table_coverage={"items": True})
    assert any("QUALITY_BUSINESS_DISCREPANCY:semantic_invoice_detail_subtotal_v1" in issue for issue in complete["issues"])
    payload["fields"]["tax"] = normalized(None)
    frozen = copy.deepcopy(payload)
    missing = business_report(payload, schema, verified_paths=paths - {"headers.0.tax"}, table_coverage={"items": True})
    header = next(check for check in missing["checks"] if check["name"] == "semantic_invoice_subtotal_tax_total_v1")
    assert header["status"] == "UNVERIFIED" and missing["issues"]
    assert payload == frozen and payload["fields"]["tax"]["value"] is None


def install_invoice_reader(monkeypatch, *, total):
    import trackvance.document_recognition_transport as transport
    import trackvance.document_worker as worker

    def line(parts, top):
        words = [{"text": text, "region": [left, top, left + min(.18, len(text) * .01), top + .02]}
            for text, left in parts]
        return {"page": 1, "text": " ".join(word["text"] for word in words), "words": words,
            "region": [min(word["region"][0] for word in words), top, max(word["region"][2] for word in words), top + .02],
            "method": "DIGITAL", "confidence": 100}

    lines = [line([("Invoice", .1), ("number:", .18), ("INV-1", .27)], .1),
        line([("Description", .1), ("Quantity", .42), ("Unit", .62), ("price", .67), ("Amount", .84)], .2),
        line([("WidgetA", .1), ("1.00", .42), ("10.00", .62), ("10.00", .84)], .25),
        line([("WidgetB", .1), ("1.00", .42), ("10.00", .62), ("10.00", .84)], .3),
        line([("Subtotal:", .1), ("20.00", .25)], .4), line([("Tax:", .1), ("0.00", .25)], .45),
        line([("Total:", .1), (total, .25)], .5)]
    geometry = {"1": {"width": 600, "height": 800, "unit": "PDF_POINT"}}
    calls = []

    def native(_source, schema, **_kwargs):
        payload = extract(lines, schema, geometry)
        payload.update(pages=[1], native_incomplete_pages=[])
        return {"payload": payload, "engine": {"pages": 1, "key": "SYNTHETIC_DECLARED_INVOICE"}}

    def recognize(source, schema, configuration, **kwargs):
        assert kwargs["strategy"] == "QWEN_VISUAL"
        calls.append("QWEN_VISUAL")
        observations = {"fields": {name: cell(value) for name, value in
            [("invoice_number", "INV-1"), ("subtotal", "20.00"), ("tax", "0.00"), ("total", total)]},
            "tables": {"items": [{name: cell(value) for name, value in
                [("description", description), ("quantity", "1.00"), ("unit_price", "10.00"), ("amount", "10.00")]}
                for description in ("WidgetA", "WidgetB")]}}
        events = independent_qwen_events(schema, configuration, hashlib.sha256(source.read_bytes()).hexdigest(), observations=observations)
        for index, event in enumerate(events, 1):
            event.pop("event_sha256", None)
            event.update(sequence=index, attempt_id=kwargs["attempt_context"]["attempt_id"])
            event["event_sha256"] = canonical_hash(event)
            kwargs["on_event"](event)
        return {"pages": [event for event in events if event["kind"] == "PAGE"], "terminal": events[-1]}

    monkeypatch.setattr(worker, "run_engine", native)
    monkeypatch.setattr(transport, "recognize_stream", recognize)
    return calls


def activate_invoice(client, monkeypatch, *, declared=True):
    enabled(monkeypatch)
    family = client.post("/api/v1/documents/families", json={"name": "Invoice business lifecycle unit"}).json()["id"]
    body = {"profile": invoice_profile(), "automatic_verification": "SEMANTIC_AUTO_V1",
        "reason": "Explicit complete invoice scope and printed roles in a synthetic source fixture"}
    if declared:
        body["business_controls"] = controls()
    response = client.post(f"/api/v1/documents/families/{family}/semantic-profile", json=body)
    assert response.status_code == 200, response.text
    return family


@pytest.mark.parametrize("total,expected", [("20.00", "PUBLISHED"), ("10.00", "REVIEW_REQUIRED")])
def test_real_invoice_lifecycle_requires_source_coverage_and_declared_coherence(authenticated, monkeypatch, total, expected):
    family = activate_invoice(authenticated, monkeypatch)
    calls = install_invoice_reader(monkeypatch, total=total)
    original = upload_fixture(authenticated, family, label="invoice-business-" + total)
    assert process_once("invoice-business-synthetic")
    detail = authenticated.get("/api/v1/documents/originals/" + original["id"]).json()
    assert calls == ["QWEN_VISUAL"]
    assert detail["semantic_review"]["tables"][0]["coverage_state"] == "AUTOMATICALLY_VERIFIED"
    assert detail["status"] == expected, detail["issues"]
    assert detail["extraction"]["payload"]["fields"]["total"]["value"] == total
    if expected == "PUBLISHED":
        assert detail["quality"]["status"] == "AUTO_VERIFIED"
        assert detail["quality"]["business_validation"]["issues"] == []
        assert len(detail["publications"]) == 1 and not detail["extraction"]["reviewed"]
    else:
        assert any("QUALITY_BUSINESS_DISCREPANCY" in issue for issue in detail["issues"])
        assert detail["publications"] == [] and not detail["semantic_review"]["automatic_approval"]
        reviewed = authenticated.post(f"/api/v1/documents/originals/{original['id']}/review", json={
            "expected_extraction_id": detail["extraction"]["id"], "confirmed": all_paths(detail["extraction"]),
            "semantic_table_confirmations": ["items"], "reason": "Even full human confirmation cannot waive a declared arithmetic contradiction"})
        assert reviewed.status_code == 200, reviewed.text
        blocked = reviewed.json()
        assert blocked["status"] == "REVIEW_REQUIRED" and blocked["publications"] == []
        assert any("QUALITY_BUSINESS_DISCREPANCY" in issue for issue in blocked["issues"])


def test_missing_invoice_declaration_needs_complete_authorized_review_not_one_flag(authenticated, monkeypatch):
    family = activate_invoice(authenticated, monkeypatch, declared=False)
    install_invoice_reader(monkeypatch, total="20.00")
    original = upload_fixture(authenticated, family, label="invoice-no-declaration")
    assert process_once("invoice-missing-business-synthetic")
    detail = authenticated.get("/api/v1/documents/originals/" + original["id"]).json()
    assert "QUALITY_BUSINESS_SCOPE_REQUIRED" in detail["issues"] and detail["publications"] == []
    partial = authenticated.post(f"/api/v1/documents/originals/{original['id']}/review", json={
        "expected_extraction_id": detail["extraction"]["id"], "confirmed": ["headers.0.invoice_number"],
        "reason": "One actual human confirmation cannot waive the missing complete invoice declaration"})
    assert partial.status_code == 200, partial.text
    current = partial.json()
    assert current["status"] == "REVIEW_REQUIRED" and "QUALITY_BUSINESS_SCOPE_REQUIRED" in current["issues"]
    complete = authenticated.post(f"/api/v1/documents/originals/{original['id']}/review", json={
        "expected_extraction_id": current["extraction"]["id"], "confirmed": all_paths(current["extraction"]),
        "semantic_table_confirmations": ["items"], "reason": "Authorized full original and all printed row occurrences checked by a human fixture reviewer"})
    assert complete.status_code == 200, complete.text
    final = complete.json()
    assert final["status"] == "PUBLISHED" and final["quality"]["status"] == "HUMAN_VERIFIED"
    assert final["quality"]["business_validation"]["status"] == "AUTHORIZED_FULL_DOCUMENT_REVIEW"
    assert final["semantic_review"]["automatic_approval"] is False
