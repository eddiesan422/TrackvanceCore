"""Effective bounds of native draft testing and discovery, with real child work."""
import copy
import json
import os
import sys
import time

import pytest
from sqlalchemy import event, func, select

from trackvance.document_models import DocumentBuilderTest, DocumentReference
from trackvance.document_schemas import DocumentDefinition
from trackvance.operations_common import OperationError


def sample(text="Invoice: 000001"):
    return {"text": text, "page": 1, "region": [.1, .1, .8, .13], "confidence": None, "method": "DIGITAL",
            "words": [{"text": word, "region": [.1 + .3 * index, .1, .3 + .3 * index, .13], "confidence": None}
                      for index, word in enumerate(text.split())]}


def test_real_builder_child_preserves_values_and_runs_discovery(tmp_path, monkeypatch):
    from trackvance import document_builder_executor as executor
    monkeypatch.setattr(executor.storage_provider, "temporary_path", lambda suffix: tmp_path / ("owned" + suffix))
    definition = DocumentDefinition.model_validate({"fields": [{"name": "invoice", "label": "Invoice", "pattern": "([0-9]+)"}]}).model_dump(mode="json")
    refs = [{"id": "reference-a", "lines": [sample()]}]
    before = copy.deepcopy(refs)
    result = executor.run_builder("EXTRACT", refs, definition)
    assert result["reference-a"]["fields"]["invoice"]["value"] == "000001"
    assert result["reference-a"]["fields"]["invoice"]["origin"]["method"] == "DIGITAL"
    discovered = executor.run_builder("DISCOVER", refs)
    assert discovered["definition"]["fields"]
    assert set(discovered["results"]) == {"reference-a"}
    assert refs == before
    assert list(tmp_path.iterdir()) == []


def test_real_rule_deadline_terminates_child_and_preserves_inputs(tmp_path, monkeypatch):
    from trackvance import document_builder_executor as executor
    monkeypatch.setattr(executor, "TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(executor.storage_provider, "temporary_path", lambda suffix: tmp_path / ("owned" + suffix))
    refs = [{"id": "bounded", "lines": [sample("a" * 32 + "!")]}]
    definition = DocumentDefinition.model_validate({"fields": [{"name": "capture", "pattern": r"((?:a+)+)$"}]}).model_dump(mode="json")
    before = copy.deepcopy(refs)
    processes = []
    original = executor.subprocess.Popen

    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(executor.subprocess, "Popen", record)
    started = time.monotonic()
    with pytest.raises(OperationError) as caught:
        executor.run_builder("EXTRACT", refs, definition)
    assert caught.value.code == "DOCUMENT_BUILDER_TIMEOUT"
    assert time.monotonic() - started < 4
    assert len(processes) == 1 and processes[0].poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(processes[0].pid, 0)
    assert refs == before
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("references", [[], [{"id": "a", "lines": "invalid"}],
    [{"id": "a", "lines": [{**sample(), "page": 0}]}],
    [{"id": "a", "lines": [{**sample(), "region": [0, 0, float("nan"), 1]}]}],
    [{"id": "a", "lines": [sample()]}, {"id": "a", "lines": [sample()]}]])
def test_builder_rejects_invalid_input_before_process(references, monkeypatch):
    from trackvance import document_builder_executor as executor

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid input must not launch a process")

    monkeypatch.setattr(executor.subprocess, "Popen", forbidden)
    with pytest.raises(OperationError) as caught:
        executor.run_builder("DISCOVER", references)
    assert caught.value.code == "DOCUMENT_BUILDER_INPUT"


def test_builder_rejects_oversized_serialized_input_without_process(monkeypatch):
    from trackvance import document_builder_executor as executor
    monkeypatch.setattr(executor, "MAX_BYTES", 100)
    with pytest.raises(OperationError) as caught:
        executor.run_builder("DISCOVER", [{"id": "a", "lines": [sample("x" * 1000)]}])
    assert caught.value.code == "DOCUMENT_BUILDER_BYTES_LIMIT"


@pytest.mark.parametrize(("content", "expected_code"), [
    (json.dumps({"reference-a": {"fields": [], "tables": {}, "table_unparsed": {}}}), "DOCUMENT_BUILDER_EXECUTION_FAILURE"),
    ("x" * 100001, "DOCUMENT_BUILDER_BYTES_LIMIT"),
], ids=["invalid-shape", "oversize"])
def test_builder_rejects_incomplete_or_oversized_child_results(tmp_path, monkeypatch, content, expected_code):
    from trackvance import document_builder_executor as executor
    monkeypatch.setattr(executor, "MAX_BYTES", 100000)
    monkeypatch.setattr(executor.storage_provider, "temporary_path", lambda suffix: tmp_path / ("owned" + suffix))
    original = executor.subprocess.Popen

    def substitute(command, **kwargs):
        script = "from pathlib import Path;import sys;Path(sys.argv[1],'result.json').write_text(sys.argv[2])"
        return original([sys.executable, "-c", script, command[-1], content], **kwargs)

    monkeypatch.setattr(executor.subprocess, "Popen", substitute)
    configured = DocumentDefinition.model_validate({"fields": [{"name": "invoice", "label": "Invoice"}]}).model_dump(mode="json")
    with pytest.raises(OperationError) as caught:
        executor.run_builder("EXTRACT", [{"id": "reference-a", "lines": [sample()]}], configured)
    assert caught.value.code == expected_code
    assert list(tmp_path.iterdir()) == []


def test_builder_failure_does_not_persist_partial_test(authenticated, database, tmp_path, monkeypatch):
    from test_document_builder_097 import create_workspace

    from trackvance import document_builder_workspace as workspace
    family, _ids, draft = create_workspace(authenticated, tmp_path)

    def bounded_failure(*args, **kwargs):
        raise OperationError(422, "DOCUMENT_BUILDER_TIMEOUT", "La prueba excedió su límite de tiempo; el borrador se conserva.")

    monkeypatch.setattr(workspace, "run_builder", bounded_failure)
    response = authenticated.post(f"/api/v1/documents/builder/drafts/{draft['id']}/test", json={"expected_version": draft["version"]})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DOCUMENT_BUILDER_TIMEOUT"
    recovered = authenticated.get(f"/api/v1/documents/builder/drafts/{draft['id']}").json()
    assert recovered["version"] == draft["version"] and recovered["sha256"] == draft["sha256"]
    assert authenticated.get(f"/api/v1/documents/families/{family}").json()["templates"] == []
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DocumentBuilderTest)) == 0


@pytest.mark.parametrize("ready", [True, False])
def test_reference_byte_budget_is_checked_before_loading_payloads(authenticated, database, tmp_path, monkeypatch, ready):
    from test_document_builder_097 import create_workspace

    from trackvance import document_builder_workspace as workspace
    from trackvance.models import User
    family, ids, _ = create_workspace(authenticated, tmp_path)
    monkeypatch.setattr(workspace, "BUILDER_MAX_BYTES", 100)
    loaded = []
    with database() as db:
        user = db.get(User, "test-user")

        @event.listens_for(db, "loaded_as_persistent")
        def record(_session, item):
            if isinstance(item, DocumentReference):
                loaded.append(item.id)

        with pytest.raises(OperationError) as caught:
            workspace.references_for(db, family, ids, user, ready=ready)
        assert caught.value.code == "DOCUMENT_BUILDER_BYTES_LIMIT"
        assert loaded == []


def test_document_limits_report_native_builder_budgets(authenticated):
    result = authenticated.get("/api/v1/documents/limits").json()
    assert result["timeout_seconds"] == 120
    assert result["builder"] == {"timeout_seconds": 30, "input_bytes": 64 * 1024**2,
        "output_bytes": 64 * 1024**2, "references": 20, "process_memory_bytes": 768 * 1024**2,
        "arithmetic_precision_digits": 4096}


def protocol_example():
    configured = DocumentDefinition.model_validate({"fields": [{"name": "invoice", "required": False},
        {"name": "amount", "logical_type": "DECIMAL"}], "tables": [{"name": "detalle",
            "row_pattern": r"(?P<code>\S+) (?P<value>\S+)", "fields": [{"name": "code"}, {"name": "value"}]}],
        "arithmetic": [{"name": "total", "terms": ["amount"], "result": "amount", "scale": 2}]}).model_dump(mode="json")
    request = {"operation": "EXTRACT", "references": [{"id": "a", "lines": [sample()]}], "definition": configured}
    cell = {"value": "1", "observed": "1", "validity": "VALID", "origin": {"page": 1, "region": [.1, .1, .3, .13], "method": "DIGITAL"}, "confidence": None, "reviewed": False}
    payload = {"fields": {"invoice": copy.deepcopy(cell), "amount": copy.deepcopy(cell)},
        "tables": {"detalle": [{"code": copy.deepcopy(cell), "value": copy.deepcopy(cell)}]}, "table_unparsed": {"detalle": []},
        "lines": copy.deepcopy(request["references"][0]["lines"]), "pages": [1], "business_period": None,
        "arithmetic": [{"name": "total", "table": None, "row": 0, "status": "VALID", "scale": 2,
            "rounding": "ROUND_HALF_UP", "expected": "1.00", "observed": "1"}]}
    return request, {"a": payload}


@pytest.mark.parametrize("mutation", ["missing-header", "extra-header", "missing-table", "missing-unparsed",
    "missing-column", "bare-cell", "missing-arithmetic", "wrong-arithmetic", "missing-lines", "wrong-pages"])
@pytest.mark.parametrize("operation", ["EXTRACT", "DISCOVER"])
def test_builder_protocol_rejects_partial_declared_structures(mutation, operation):
    from trackvance.document_builder_executor import _valid_result
    request, result = protocol_example()
    payload = result["a"]
    if mutation == "missing-header":
        payload["fields"].pop("invoice")
    elif mutation == "extra-header":
        payload["fields"]["rogue"] = copy.deepcopy(payload["fields"]["invoice"])
    elif mutation == "missing-table":
        payload["tables"].pop("detalle")
    elif mutation == "missing-unparsed":
        payload["table_unparsed"].pop("detalle")
    elif mutation == "missing-column":
        payload["tables"]["detalle"][0].pop("value")
    elif mutation == "bare-cell":
        payload["fields"]["amount"] = {"validity": "VALID"}
    elif mutation == "missing-arithmetic":
        payload.pop("arithmetic")
    elif mutation == "wrong-arithmetic":
        payload["arithmetic"][0]["name"] = "wrong"
    elif mutation == "missing-lines":
        payload.pop("lines")
    else:
        payload["pages"] = [2]
    if operation == "DISCOVER":
        request["operation"] = operation
        result = {"definition": request["definition"], "results": result}
    assert _valid_result(result, request) is False


def test_builder_protocol_keeps_optional_missing_cells_and_named_empty_tables():
    from trackvance.document_builder_executor import _valid_result
    request, result = protocol_example()
    assert _valid_result(result, request) is True
    result["a"]["fields"]["invoice"].update(value=None, observed=None, origin=None, validity="VALID")
    result["a"]["tables"]["detalle"] = []
    assert _valid_result(result, request) is True
