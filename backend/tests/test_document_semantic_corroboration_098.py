"""Deterministic authority tests; these are not measurements of OCR quality."""
import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from trackvance.document_automatic_verification import source_context
from trackvance.document_lossless import adapt_saved_recognition
from trackvance.document_normalization import canonical_hash
from trackvance.document_semantic_corroboration import (
    certify_semantic_candidate,
    semantic_source_context,
    semantic_verification_decision,
    trusted_semantic_proof,
)
from trackvance.document_semantic_general import compile_profile


@pytest.fixture(autouse=True)
def enable_synthetic_auto_contract(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", "true")


def _artifact(kind, value):
    raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
    return {"kind": kind, "media_type": "image/png" if kind == "PAGE_IMAGE_PNG" else "application/json",
        "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "content_b64": base64.b64encode(raw).decode()}


def _producer():
    path = Path(__file__).resolve().parents[2] / "deploy" / "idp" / "recognition.py"
    spec = importlib.util.spec_from_file_location("semantic_corroboration_request_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    prompt = (path.parent / "prompt-visual-v1.txt").read_text(encoding="utf-8")
    prompt += "\n" + module.VISUAL_SOURCE_INSTRUCTIONS
    prompt += "\n" + module.SEMANTIC_GENERAL_LITERAL_INSTRUCTIONS + "\n" + module.SEMANTIC_GENERAL_GUARD_INSTRUCTIONS
    return module, prompt


def _word(text, left, top):
    width = max(.006, len(text) * .004)
    return {"text": text, "region": [left, top, left + width, top + .012]}


def _line(text, top, page=1, left=.08):
    words = []
    for value in text.split():
        words.append(_word(value, left, top))
        left = words[-1]["region"][2] + .004
    return {"text": text, "page": page, "method": "DIGITAL", "confidence": None,
        "region": [words[0]["region"][0], top, words[-1]["region"][2], top + .012], "words": words}


def _physical_line(values, top, page=1):
    positions = [.08, .52, .68, .84]
    words = []
    for value, left in zip(values, positions, strict=True):
        for token in value.split():
            words.append(_word(token, left, top))
            left = words[-1]["region"][2] + .004
    return {"text": " ".join(value["text"] for value in words), "page": page,
        "method": "DIGITAL", "confidence": None, "words": words,
        "region": [words[0]["region"][0], top, words[-1]["region"][2], top + .012]}


def corroboration_fixture(*, rows=None, count=1, native=True, paddle=False, narrative=False, date_literal=None, invoice=False):
    """Source bytes, proposals and a coordinator registry receipt are separate."""
    rows = rows or [["Bolts", "2", "5.00", "10.00"]]
    profile = {"id": "corroboration-fixture", "revision": 1, "name": "Corroboration fixture",
        "document_type": "NARRATIVE" if narrative else "INVOICE" if invoice else "CUSTOM", "description": "Synthetic source-authority contract; business controls are tested separately",
        "fields": [{"name": "invoice_number", "label": "Invoice number", "critical": True},
            {"name": "issuer", "label": "Issuer", "critical": True, "section": "ISSUER"},
            {"name": "total", "label": "Total due", "logical_type": "DECIMAL", "critical": True, "section": "TOTALS"}],
        "tables": [{"name": "items", "kind": "COLLECTION" if narrative else "TABLE", "description": "Every printed occurrence",
            "fields": [{"name": "description", "label": "Description"},
                {"name": "quantity", "label": "Quantity", "logical_type": "DECIMAL"},
                {"name": "unit_price", "label": "Unit price", "logical_type": "DECIMAL"},
                {"name": "amount", "label": "Amount", "logical_type": "DECIMAL", "critical": True},
                {"name": "item_code", "label": "Item code", "required": False},
                {"name": "source_quote", "label": "Source quote", "required": False,
                    "normalization": {"text_linebreaks": "DECLARED_MULTILINE_V1"}}]}]}
    if date_literal is not None:
        profile["fields"].append({"name": "issue_date", "label": "Issue date", "logical_type": "DATE",
            "normalization": {"date_formats": ["%d/%m/%Y", "%m/%d/%Y"]}})
    for field in [*profile["fields"], *(field for table in profile["tables"] for field in table["fields"])]:
        if field.get("logical_type") == "DECIMAL":
            field["normalization"] = {"decimal_formats": "UNAMBIGUOUS_LOCALE_V1"}
    definition = compile_profile(profile, automatic_verification="SEMANTIC_AUTO_V1")
    configuration = {"provider": "PADDLE_QWEN_LOCAL", "extraction_strategy": "QWEN_VISUAL",
        "automatic_verification": "BOUNDED_LOCAL_V1", "llm_model": "qwen3.5:9b-q4_K_M", "paddle_model": "PaddleOCR-VL-1.6"}
    source_sha = "a" * 64
    events = []

    def event(kind, stage, page, payload, **extra):
        value = {"version": 1, "kind": kind, "sequence": len(events) + 1, "request_id": "request-" + ("paddle" if stage == "PADDLE_STRUCTURED" else "qwen"),
            "attempt_id": "fixture-attempt", "source_sha256": source_sha, "configuration_sha256": canonical_hash(configuration),
            "definition_sha256": canonical_hash(definition), "stage": stage, "page_one_based": page,
            "expected_pages": count, "payload": payload, **extra}
        value["event_sha256"] = canonical_hash(value)
        events.append(value)

    lines = []
    for page in range(1, count + 1):
        lines.extend([_line("Invoice number: INV-A", .05, page), _line("Supplier: Alpha", .09, page),
            _physical_line(["Description", "Quantity", "Unit price", "Amount"], .2, page)])
        if date_literal is not None:
            lines.append(_line("Issue date: " + date_literal, .13, page))
        for index, row in enumerate(rows):
            lines.append(_physical_line(row, .25 + index * .04, page))
        lines.append(_line("Total due: 10.00", .8, page))
    original = {"payload": {"lines": lines if native else [], "pages": list(range(1, count + 1)) if native else [],
        "native_incomplete_pages": [] if native else list(range(1, count + 1)),
        "page_geometry": {str(page): {"width": 600, "height": 800, "unit": "PDF_POINT"} for page in range(1, count + 1)}},
        "engine": {"key": "POPPLER_TESSERACT_CPU_V1", "pages": count}}
    event("PHASE", "NATIVE_SOURCE_PROBE", None, {"source_page_count_verified": True,
        "artifacts": [_artifact("NATIVE_SOURCE_PROBE_JSON", original)]})
    producer, prompt = _producer()
    for page in range(1, count + 1):
        image = _artifact("PAGE_IMAGE_PNG", b"synthetic-image-evidence-not-an-inference")
        page_meta = {"page": page, "page_geometry": {"width": 1000, "height": 1000, "unit": "PIXEL"}}
        schema = producer.visual_contract(definition)
        body = {"model": configuration["llm_model"], "think": False, "keep_alive": 0,
            "format": schema, "truncate": False, "shift": False,
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps({
                "page": page, "document_pages": count, "definition": producer.semantic_definition(definition),
                "json_schema": schema, "targets": {"fields": [field["name"] for field in definition["fields"]], "tables": ["items"]}}, ensure_ascii=False),
                "images": [image["content_b64"]]}], "options": {"temperature": 0}}
        metadata = producer.visual_request_metadata(body, source_sha256=source_sha, definition_sha256=canonical_hash(definition),
            configuration_sha256=canonical_hash(configuration), image=image, page=page_meta)
        event("PHASE", "QWEN_GENERATION", page, {"artifacts": [_artifact("QWEN_REQUEST_JSON", metadata)]})
        def cell(value):
            return {"observed": value, "ambiguous": False, "alternatives": []}
        selection = {"fields": {"invoice_number": cell("INV-A"), "issuer": cell("Alpha"), "total": cell("10.00")},
            "tables": {"items": [{**{name: cell(value) for name, value in zip(("description", "quantity", "unit_price", "amount"), row, strict=True)},
                "item_code": cell(None), "source_quote": cell(" ".join(row))} for row in rows]}}
        if date_literal is not None:
            selection["fields"]["issue_date"] = cell(date_literal)
        event("PAGE", "QWEN_VISUAL", page, {"artifacts": [image, _artifact("QWEN_SELECTION_JSON", selection)],
            "page_geometry": page_meta["page_geometry"], "recognition_status": "COMPLETE"})
    event("TERMINAL", "QWEN_VISUAL", None, {}, status="COMPLETE", resources_released=True,
        emitted_pages=list(range(1, count + 1)), completed_pages=list(range(1, count + 1)), pending_pages=[])
    if paddle:
        for page in range(1, count + 1):
            html = "<table><tr><th>Description</th><th>Quantity</th><th>Unit price</th><th>Amount</th></tr>"
            html += "".join("<tr>" + "".join("<td>" + value + "</td>" for value in row) + "</tr>" for row in rows) + "</table>"
            raw = {"parsing_res_list": [{"block_label": "text", "block_content": "Invoice number: INV-A", "block_bbox": [80, 50, 400, 70]},
                {"block_label": "text", "block_content": "Supplier: Alpha", "block_bbox": [80, 90, 400, 110]},
                {"block_label": "table", "block_content": html, "block_bbox": [80, 200, 950, 500]},
                {"block_label": "text", "block_content": "Total due: 10.00", "block_bbox": [80, 800, 400, 820]}]}
            event("PAGE", "PADDLE_STRUCTURED", page, {"artifacts": [_artifact("PADDLE_PAGE_JSON", raw)],
                "page_geometry": {"width": 1000, "height": 1000, "unit": "PIXEL"}})
        event("TERMINAL", "PADDLE_STRUCTURED", None, {}, status="COMPLETE", resources_released=True,
            emitted_pages=list(range(1, count + 1)), completed_pages=list(range(1, count + 1)), pending_pages=[])
    return definition, configuration, events


def _reseal(events):
    for index, event in enumerate(events):
        event.pop("durable_refs", None)
        event["event_sha256"] = canonical_hash({key: value for key, value in event.items() if key != "event_sha256"})
        event["durable_refs"] = {"manifest_artifact_id": "manifest-" + str(index), "event_sha256": event["event_sha256"],
            "artifacts": [{"artifact_id": f"artifact-{index}-{ordinal}", "kind": artifact["kind"], "sha256": artifact["sha256"],
                "bytes": artifact["bytes"], "page": event.get("page_one_based"), "stage": event["stage"]}
                for ordinal, artifact in enumerate(event["payload"].get("artifacts", []))]}
    return events


def _replace_artifact(event, kind, change):
    for index, record in enumerate(event["payload"]["artifacts"]):
        if record["kind"] == kind:
            value = json.loads(base64.b64decode(record["content_b64"]))
            change(value)
            event["payload"]["artifacts"][index] = _artifact(kind, value)
            return
    raise AssertionError(kind)


def _certify(definition, configuration, events):
    _reseal(events)
    data = adapt_saved_recognition(events, definition, configuration, source_sha256="a" * 64)
    context = semantic_source_context(events, source_context(events, "a" * 64))
    proof = certify_semantic_candidate(data, definition, "a" * 64, source_context=context)
    return data, context, proof


def test_digital_complete_inventory_and_association():
    data, _, proof = _certify(*corroboration_fixture())
    assert proof["global_issues"] == [] and proof["issues"] == []
    assert proof["table_coverage"] == {"items": True}
    assert len(proof["verified_cells"]) == 9
    assert proof["coverage_details"]["items"]["source_inventory"][0]["headers"][0]["columns"] == {0: "description", 1: "quantity", 2: "unit_price", 3: "amount"}
    assert data["payload"]["tables"]["items"][0]["item_code"]["value"] is None


def test_invoice_without_declared_business_scope_cannot_gain_auto():
    _, _, proof = _certify(*corroboration_fixture(invoice=True))
    assert proof["global_issues"] == []
    assert "QUALITY_BUSINESS_SCOPE_REQUIRED" in proof["issues"]
    assert proof["business_validation"]["required"] and not proof["business_validation"]["applicable"]
    assert not proof["arithmetic"]
    assert len(proof["verified_cells"]) == 9 and proof["table_coverage"]["items"]


def test_scanned_original_paddle_logical_columns_are_independent():
    _, _, proof = _certify(*corroboration_fixture(native=False, paddle=True))
    assert proof["issues"] == [] and proof["table_coverage"]["items"]
    refs = proof["corroboration"]["items.0.amount"]["source_refs"]
    assert refs[0]["mechanism"] == "PADDLE" and refs[0]["geometry_kind"] == "UNKNOWN"


def test_wrong_header_role_and_source_section_block():
    definition, configuration, events = corroboration_fixture()
    def wrong(original):
        original["payload"]["lines"][1] = _line("Buyer", .08)
        original["payload"]["lines"].insert(2, _line("Supplier: Alpha", .11))
    _replace_artifact(events[0], "NATIVE_SOURCE_PROBE_JSON", wrong)
    _, _, proof = _certify(definition, configuration, events)
    assert proof["corroboration"]["headers.0.issuer"]["status"] == "REVIEW_REQUIRED"
    assert "ASSOCIATION_NOT_PROVEN:headers.0.issuer" in proof["issues"]


def test_same_amount_under_another_label_is_not_total_evidence():
    definition, configuration, events = corroboration_fixture()
    def changed(original):
        original["payload"]["lines"][-1] = _line("Previous balance: 10.00", .8)
    _replace_artifact(events[0], "NATIVE_SOURCE_PROBE_JSON", changed)
    _, _, proof = _certify(definition, configuration, events)
    assert "ASSOCIATION_NOT_PROVEN:headers.0.total" in proof["issues"]


def test_prose_prefix_without_caption_delimiter_is_not_a_field_relation():
    definition, configuration, events = corroboration_fixture()
    _replace_artifact(events[0], "NATIVE_SOURCE_PROBE_JSON", lambda value: value["payload"]["lines"].__setitem__(1, _line("Supplier Alpha", .09)))
    _, _, proof = _certify(definition, configuration, events)
    assert "ASSOCIATION_NOT_PROVEN:headers.0.issuer" in proof["issues"]


def test_cross_mechanism_header_contradiction_is_not_erased_by_matching_native():
    definition, configuration, events = corroboration_fixture(paddle=True)
    page = next(event for event in events if event["stage"] == "PADDLE_STRUCTURED" and event["kind"] == "PAGE")
    def changed(original):
        original["parsing_res_list"][1]["block_content"] = "Supplier: Beta"
    _replace_artifact(page, "PADDLE_PAGE_JSON", changed)
    _, _, proof = _certify(definition, configuration, events)
    assert "SOURCE_CONTRADICTION:headers.0.issuer" in proof["issues"]
    assert "headers.0.issuer" not in proof["verified_cells"]


def test_cross_mechanism_table_contradiction_blocks_coverage():
    definition, configuration, events = corroboration_fixture(paddle=True)
    page = next(event for event in events if event["stage"] == "PADDLE_STRUCTURED" and event["kind"] == "PAGE")
    def changed(original):
        original["parsing_res_list"][2]["block_content"] = original["parsing_res_list"][2]["block_content"].replace("<td>10.00</td>", "<td>11.00</td>")
    _replace_artifact(page, "PADDLE_PAGE_JSON", changed)
    _, _, proof = _certify(definition, configuration, events)
    assert not proof["table_coverage"]["items"]
    assert "UNMATCHED_SOURCE_ROW:items" in proof["issues"]


@pytest.mark.parametrize("mutation", ["event", "blob", "registry", "ir"])
def test_forged_event_blob_registry_or_ir_block(mutation):
    data, context, _ = _certify(*corroboration_fixture())
    if mutation == "event":
        context["recognition_events"][0]["definition_sha256"] = "f" * 64
    elif mutation == "blob":
        context["recognition_events"][0]["payload"]["artifacts"][0]["content_b64"] = base64.b64encode(b"forged").decode()
    elif mutation == "registry":
        context["artifacts"][0]["artifact_id"] = "forged-id"
    else:
        data["payload"]["document_representation"]["pages"][0]["selection"]["fields"]["issuer"]["observed"] = "Forged"
    proof = certify_semantic_candidate(data, corroboration_fixture()[0], "a" * 64, source_context=context)
    assert proof["global_issues"] and not proof["worker_registry_verified"]
    assert not proof["verified_cells"] and not proof["table_coverage"]["items"]


def test_dependent_qwen_request_never_corroborates():
    definition, configuration, events = corroboration_fixture()
    def dependent(value):
        value["request_without_image_bytes"]["messages"][1]["content"] += " Paddle supplied Alpha and 10.00"
    _replace_artifact(events[1], "QWEN_REQUEST_JSON", dependent)
    _, _, proof = _certify(definition, configuration, events)
    assert "INDEPENDENT_REQUEST_UNVERIFIED:headers.0.issuer" in proof["issues"]


@pytest.mark.parametrize("mode", ["missing", "extra", "column"])
def test_missing_extra_rows_and_columns_block(mode):
    definition, configuration, events = corroboration_fixture()
    def changed(original):
        lines = original["payload"]["lines"]
        if mode == "missing":
            lines.insert(-1, _physical_line(["Nuts", "1", "2.00", "2.00"], .32))
        elif mode == "extra":
            lines.pop(3)
        else:
            lines[2] = _physical_line(["Unknown", "Quantity", "Unit price", "Amount"], .2)
    _replace_artifact(events[0], "NATIVE_SOURCE_PROBE_JSON", changed)
    _, _, proof = _certify(definition, configuration, events)
    assert not proof["table_coverage"]["items"]


def test_unassociated_text_only_table_before_recognized_table_blocks():
    definition, configuration, events = corroboration_fixture()
    def changed(original):
        a = _physical_line(["Kind", "Meaning", "Category", "Area"], .13)
        b = _physical_line(["Notes", "Text", "Open", "North"], .16)
        original["payload"]["lines"][2:2] = [a, b]
    _replace_artifact(events[0], "NATIVE_SOURCE_PROBE_JSON", changed)
    _, _, proof = _certify(definition, configuration, events)
    assert not proof["table_coverage"]["items"]
    assert "UNASSOCIATED_SOURCE_TABLE:items" in proof["issues"]


def test_unclassified_single_column_collection_cannot_be_ignored():
    definition, configuration, events = corroboration_fixture()
    def changed(original):
        original["payload"]["lines"][2:2] = [_line("Additional obligations", .13), _line("Deliver report", .16)]
    _replace_artifact(events[0], "NATIVE_SOURCE_PROBE_JSON", changed)
    _, _, proof = _certify(definition, configuration, events)
    assert not proof["table_coverage"]["items"]
    assert "UNCLASSIFIED_SOURCE_SCOPE:items" in proof["issues"]


def test_ambiguous_source_values_and_dates_block():
    _, _, proof = _certify(*corroboration_fixture(date_literal="01/02/2026", rows=[["Bolts", "2", "5.00", "1,234"]]))
    assert "VALUE_UNRESOLVED:headers.0.issue_date" in proof["issues"]
    assert "VALUE_UNRESOLVED:items.0.amount" in proof["issues"]
    assert not proof["table_coverage"]["items"]


def test_legitimate_duplicate_occurrences_preserved():
    data, _, proof = _certify(*corroboration_fixture(rows=[["Bolts", "2", "5.00", "10.00"]] * 2))
    assert len(data["payload"]["tables"]["items"]) == 2
    assert proof["table_coverage"]["items"]
    aligned = proof["coverage_details"]["items"]["occurrence_alignment"]
    assert len({row["source_occurrence"] for row in aligned}) == 2


def test_repeated_headers_multipage_preserve_occurrences():
    _, _, proof = _certify(*corroboration_fixture(count=2))
    assert proof["table_coverage"]["items"]
    assert [row["source_page"] for row in proof["coverage_details"]["items"]["occurrence_alignment"]] == [1, 2]


def test_headerless_continuation_remains_review():
    definition, configuration, events = corroboration_fixture(count=2)
    def changed(original):
        original["payload"]["lines"] = [line for line in original["payload"]["lines"] if not (line["page"] == 2 and line["text"].startswith("Description"))]
    _replace_artifact(events[0], "NATIVE_SOURCE_PROBE_JSON", changed)
    _, _, proof = _certify(definition, configuration, events)
    assert not proof["table_coverage"]["items"]


@pytest.mark.parametrize("incomplete", ["mixed", "missing_page", "missing_geometry"])
def test_native_mixed_page_or_incomplete_scope_blocks(incomplete):
    definition, configuration, events = corroboration_fixture()
    def changed(original):
        if incomplete == "mixed":
            original["payload"]["native_incomplete_pages"] = [1]
        elif incomplete == "missing_page":
            original["payload"]["pages"] = []
        else:
            original["payload"]["page_geometry"] = {}
    _replace_artifact(events[0], "NATIVE_SOURCE_PROBE_JSON", changed)
    _, _, proof = _certify(definition, configuration, events)
    assert not proof["native_scope_complete"] and not proof["table_coverage"]["items"]


def test_narrative_literal_quote_has_no_semantic_authority():
    _, _, proof = _certify(*corroboration_fixture(narrative=True))
    assert not proof["table_coverage"]["items"]
    assert "NARRATIVE_SEMANTICS_UNVERIFIED:items" in proof["issues"]


def test_proof_regenerates_and_manual_edit_does_not_gain_auto():
    definition, configuration, events = corroboration_fixture()
    data, _, proof = _certify(definition, configuration, events)
    data["engine"]["validation"] = proof
    extraction = SimpleNamespace(**data, reviewed=False)
    assert trusted_semantic_proof(extraction, definition) == proof
    extraction.payload["fields"]["issuer"].update(value="Human correction", observed="Human correction", reviewed=True)
    assert trusted_semantic_proof(extraction, definition) == proof
    proof["verified_cells"]["headers.0.issuer"] = "forged"
    proof["attestation_sha256"] = canonical_hash({key: value for key, value in proof.items() if key != "attestation_sha256"})
    assert trusted_semantic_proof(extraction, definition) is None


def test_native_scope_prevents_wasteful_paddle_on_semantic_exception():
    definition, configuration, events = corroboration_fixture()
    _replace_artifact(events[0], "NATIVE_SOURCE_PROBE_JSON", lambda value: value["payload"]["lines"].__setitem__(1, _line("Buyer: Alpha", .09)))
    data, context, proof = _certify(definition, configuration, events)
    decision = semantic_verification_decision(data, definition, context)
    assert proof["native_scope_complete"] and decision["required"] and not decision["actionable"]


def test_proof_is_stable_across_process_hash_seeds():
    root = Path(__file__).resolve().parents[1]
    script = ("from test_document_semantic_corroboration_098 import corroboration_fixture, _certify; "
        "_,_,p=_certify(*corroboration_fixture(rows=[['Bolts','2','5.00','10.00'],['Nuts','1','2.00','2.00']],count=2)); "
        "print(p['attestation_sha256'])")
    digests = []
    for seed in ("11", "97"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "TRACKVANCE_SEMANTIC_GENERAL_ENABLED": "true",
            "TRACKVANCE_SEMANTIC_AUTO_ENABLED": "true", "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root / "tests")])}
        result = subprocess.run([sys.executable, "-c", script], cwd=root, env=env, capture_output=True,
            text=True, check=True, timeout=30)
        digests.append(result.stdout.strip())
    assert digests[0] == digests[1]
