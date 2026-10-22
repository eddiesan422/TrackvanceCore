"""Native Linux EXTRACT replay for exact source-header confirmation.

Every successful replay and child failure uses the installed run_builder and
the real confined executable. Only its private temporary directory and the
test-local deadline are configured; no transport result is substituted.
"""
import copy
import os
import sys
import time

import pytest

from trackvance.document_header_review import append_confirmations, source_digest
from trackvance.document_schemas import DocumentDefinition
from trackvance.operations_common import OperationError


def word_line(text, x, y, width=.10):
    region = [x, y, x + width, y + .007]
    return {"text": text, "region": region, "page": 1, "method": "DIGITAL", "confidence": 100,
            "words": [{"text": text, "region": list(region), "confidence": 100}]}


def declared_source(*, partial=True, row_start=True):
    fields = [{"name": name, "label": label, "logical_type": kind, "required": True,
               "decimal_separator": ".", "thousands_separator": None, "currency_symbols": []}
              for name, label, kind in [("item", "Product", "STRING"), ("units", "Units", "DECIMAL"),
                                       ("rate", "Rate", "DECIMAL"), ("extended", "Extended", "DECIMAL")]]
    visual = {"page": 1, "region": [.05, .19, .96, .24],
              "header_labels": ["Product", "Units", "Rate", "Extended"],
              "column_bounds": [.04, .35, .55, .75, .96], "flowing": True, "continue_pages": True,
              "multiline_column": 0, "row_tolerance": .018, "tolerance": .06,
              "stop_label": "End", "stop_label_aliases": []}
    if row_start:
        visual["row_start"] = {"column": 0, "separator": "-", "segments": [
            {"alphabet": "ASCII_ALPHA", "minimum": 2, "maximum": 2},
            {"alphabet": "ASCII_DIGIT", "minimum": 3, "maximum": 3},
            {"alphabet": "ASCII_DIGIT", "minimum": 2, "maximum": 2}], "maximum_start_inset": .035}
    if partial:
        visual["header_observation_policy"] = "SOURCE_REVIEW_REQUIRED"
    configured = DocumentDefinition.model_validate({"fields": [{"name": "document_number",
        "label": "Document", "pattern": "([A-Z0-9]+)", "logical_type": "STRING", "required": True}], "tables": [
        {"name": "positions", "fields": fields, "required": True, "visual": visual}]}).model_dump(mode="json")
    raw = [word_line("Document: DOC001", .05, .10, .40)]
    raw.extend(word_line(text, x, .20) for text, x in [
        ("Product", .05), ("U??" if partial else "Units", .42), ("Rate", .62), ("Extended", .82)])
    for index, y in enumerate((.30, .55), start=1):
        raw.extend([word_line(f"ZX-123-{index:02d}", .05, y, .20),
                    word_line(f"Service{index}", .05, y + .015, .15),
                    word_line("2", .42, y + .03), word_line("3.00", .62, y + .03),
                    word_line("6.00", .82, y + .03)])
    return configured, [*raw, word_line("End", .05, .90)]


@pytest.fixture
def real_builder(tmp_path, monkeypatch):
    from trackvance import document_builder_executor as executor

    assert sys.platform == "linux", "The mandatory real confinement regression must run in Linux"
    monkeypatch.setattr(executor.storage_provider, "temporary_path", lambda suffix: tmp_path / ("owned" + suffix))
    processes = []
    actual_popen = executor.subprocess.Popen

    def record_real_process(command, **kwargs):
        assert command[:3] == [sys.executable, "-I", "-B"]
        assert kwargs["start_new_session"] is True
        process = actual_popen(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(executor.subprocess, "Popen", record_real_process)
    yield executor, processes
    assert processes and all(process.poll() is not None for process in processes)
    for process in processes:
        with pytest.raises(ProcessLookupError):
            os.kill(process.pid, 0)
    assert list(tmp_path.iterdir()) == []


def produce(executor, configured, raw):
    before = copy.deepcopy((configured, raw))
    payload = executor.run_builder("EXTRACT", [{"id": "native-source", "lines": raw}], configured)["native-source"]
    assert (configured, raw) == before and payload["lines"] == raw
    assert "header_resolutions" not in payload
    return payload


def test_native_body_replay_accepts_complete_original_and_keeps_header_visible(real_builder):
    executor, processes = real_builder
    configured, raw = declared_source()
    automatic = produce(executor, configured, raw)
    before = copy.deepcopy(automatic)
    assert automatic["fields"]["document_number"]["value"] == "DOC001"
    assert len(automatic["tables"]["positions"]) == 2
    assert [row["item"]["value"] for row in automatic["tables"]["positions"]] == [
        "ZX-123-01 Service1", "ZX-123-02 Service2"]
    assert automatic["table_unparsed"]["positions"][0]["text"] == "Product U?? Rate Extended"
    assert executor.verify_automatic_payload(automatic, configured) is True
    assert len(processes) == 2 and automatic == before


@pytest.mark.parametrize("omitted", ["row", "cell", "unparsed"], ids=["row", "cell", "unparsed"])
def test_native_body_replay_rejects_omission_without_changing_source(real_builder, omitted):
    executor, processes = real_builder
    configured, raw = declared_source()
    automatic = produce(executor, configured, raw)
    changed = copy.deepcopy(automatic)
    if omitted == "row":
        changed["tables"]["positions"].pop()
    elif omitted == "cell":
        changed["tables"]["positions"][0].pop("rate")
    else:
        changed["table_unparsed"]["positions"].clear()
    before = copy.deepcopy(changed)
    assert executor.verify_automatic_payload(changed, configured) is False
    assert len(processes) == 2 and changed == before and automatic["lines"] == raw
    assert "header_resolutions" not in changed


def test_native_source_confirmation_runs_real_body_check_before_immutable_append(real_builder):
    executor, processes = real_builder
    configured, raw = declared_source()
    automatic = produce(executor, configured, raw)
    payload = copy.deepcopy(automatic)
    for cell in payload["fields"].values():
        cell["reviewed"] = True
    for row in payload["tables"]["positions"]:
        for cell in row.values():
            cell["reviewed"] = True
    observed = payload["table_unparsed"]["positions"][0]["header_observation"]
    request = {"observation_id": observed["observation_id"], "expected_observation_hash": source_digest(observed),
               "expected_payload_hash": source_digest(payload), "decision": "SOURCE_HEADER_ONLY_NO_PRODUCTS_CONFIRMED",
               "source_note": "The original page contains only these header labels in this band."}
    binding = {"kind": "DOCUMENT", "document_id": "own-native-document", "source_sha256": "a" * 64,
               "definition_hash": source_digest(configured), "automatic_payload_hash": source_digest(automatic)}
    before = copy.deepcopy((payload, automatic))
    result = append_confirmations(payload, automatic, configured, [request], binding,
                                  "native-reviewer", "2026-01-01T00:00:00Z", "Checked the original")
    assert len(processes) == 2 and (payload, automatic) == before
    assert result["header_resolutions"][0]["event"] == "CONFIRMED"
    assert result["table_unparsed"] == automatic["table_unparsed"]
    assert result["tables"] == payload["tables"] and result["lines"] == raw


def test_native_body_replay_timeout_returns_no_approval_and_reaps_real_child(real_builder, monkeypatch):
    executor, processes = real_builder
    configured, raw = declared_source()
    configured["fields"] = [{"name": "probe", "label": "Marker", "pattern": "(.+)", "required": False}]
    configured = DocumentDefinition.model_validate(configured).model_dump(mode="json")
    raw.append(word_line("Marker: " + "a" * 32 + "!", .05, .10, .85))
    automatic = produce(executor, configured, raw)
    failing = copy.deepcopy(configured)
    failing["fields"][0]["pattern"] = r"((?:a+)+)$"
    monkeypatch.setattr(executor, "TIMEOUT_SECONDS", 1)
    before = copy.deepcopy((automatic, failing))
    started = time.monotonic()
    with pytest.raises(OperationError) as caught:
        executor.verify_automatic_payload(automatic, failing)
    assert caught.value.code == "DOCUMENT_BUILDER_TIMEOUT"
    assert time.monotonic() - started < 4 and len(processes) == 2
    assert (automatic, failing) == before and "header_resolutions" not in automatic


def test_native_body_replay_child_error_returns_no_approval_or_partial_resolution(real_builder):
    executor, processes = real_builder
    configured, raw = declared_source()
    automatic = produce(executor, configured, raw)
    failing = copy.deepcopy(configured)
    failing["tables"][0]["fields"][0]["logical_type"] = "INVALID_DECLARED_TYPE"
    before = copy.deepcopy((automatic, failing))
    with pytest.raises(OperationError) as caught:
        executor.verify_automatic_payload(automatic, failing)
    assert caught.value.code == "DOCUMENT_BUILDER_EXECUTION_FAILURE"
    assert len(processes) == 2 and (automatic, failing) == before
    assert "header_resolutions" not in automatic


def test_native_legacy_default_replay_adds_no_opt_in_or_resolution_metadata(real_builder):
    executor, processes = real_builder
    configured, raw = declared_source(partial=False, row_start=False)
    visual = configured["tables"][0]["visual"]
    assert not {"row_start", "header_label_aliases", "header_observation_policy", "continuation_context"} & visual.keys()
    automatic = produce(executor, configured, raw)
    before = copy.deepcopy((automatic, configured))
    assert executor.verify_automatic_payload(automatic, configured) is True
    assert len(processes) == 2 and (automatic, configured) == before
    assert "header_resolutions" not in automatic
