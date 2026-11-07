"""Early reader failure keeps its unknown total and cannot complete a source."""
import copy

import pytest
from test_document_lossless_098 import artifact, attest, definition, events, selection

from trackvance.document_lossless import adapt_saved_recognition
from trackvance.document_normalization import canonical_hash
from trackvance.document_quality import quality_report
from trackvance.document_representation import representation_from_events


def rehash(event):
    event["event_sha256"] = canonical_hash({key: value for key, value in event.items()
        if key not in {"event_sha256", "durable_refs"}})
    return event


def source_events(*, emitted=2):
    primary = events(selection("PUBLIC-REFERENCE"), count=2, emitted=emitted)
    if emitted < 2:
        primary[-1].update(status="TIMEOUT", pending_pages=[2])
        rehash(primary[-1])
    probe = {**{key: primary[0][key] for key in ("version", "attempt_id", "source_sha256", "configuration_sha256", "definition_sha256")},
        "request_id": "native-source", "kind": "PHASE", "sequence": 0, "stage": "NATIVE_SOURCE_PROBE",
        "page_one_based": None, "expected_pages": 2, "payload": {"source_page_count_verified": True,
            "artifacts": [artifact("NATIVE_SOURCE_PROBE_JSON", {"engine": {"pages": 2}, "payload": {"native_incomplete_pages": []}})]}}
    secondary = {**{key: primary[0][key] for key in ("version", "attempt_id", "source_sha256", "configuration_sha256", "definition_sha256")},
        "request_id": "secondary-reader", "kind": "TERMINAL", "sequence": 1, "stage": "PADDLE_STRUCTURED",
        "page_one_based": None, "expected_pages": None, "status": "TIMEOUT", "emitted_pages": [],
        "completed_pages": [], "pending_pages": [], "resources_released": True,
        "payload": {"error_code": "INFERENCE_TIMEOUT"}}
    return [rehash(probe), *primary, rehash(secondary)]


def representation(items):
    return representation_from_events(items, source_sha256="a" * 64, configuration_sha256="b" * 64)


@pytest.mark.parametrize("status", ["FAILED", "TIMEOUT", "CANCELLED"])
def test_released_early_failure_preserves_unknown_and_independent_source_total(status):
    items = source_events()
    items[-1]["status"] = status
    rehash(items[-1])
    original = copy.deepcopy(items)
    result = representation(items)
    assert result["expected_pages"] == 2 and result["source_page_count_verified"]
    assert result["terminals"][-1]["expected_pages"] is None
    assert result["terminals"][-1]["completed_pages"] == [] and result["terminals"][-1]["status"] == status
    assert len(result["pages"]) == 2 and all(page["stage"] == "QWEN_VISUAL" for page in result["pages"])
    assert items == original


@pytest.mark.parametrize("status", ["COMPLETE", "INCOMPLETE", "UNRECOGNIZED"])
def test_unknown_page_count_does_not_allow_complete_or_unsupported_terminal(status):
    items = source_events()
    items[-1]["status"] = status
    rehash(items[-1])
    with pytest.raises(ValueError, match="RECOGNITION_PAGE_COUNT_BINDING"):
        representation(items)


@pytest.mark.parametrize("attribute", ["emitted_pages", "completed_pages", "pending_pages"])
def test_unknown_failure_with_nonempty_page_claim_remains_integrity_error(attribute):
    items = source_events()
    items[-1][attribute] = [1]
    rehash(items[-1])
    with pytest.raises(ValueError, match="RECOGNITION_PAGE_COUNT_BINDING"):
        representation(items)


@pytest.mark.parametrize("attribute", ["expected_pages", "emitted_pages", "completed_pages", "pending_pages"])
def test_unknown_failure_requires_explicit_null_and_empty_arrays(attribute):
    items = source_events()
    items[-1].pop(attribute)
    rehash(items[-1])
    with pytest.raises(ValueError, match="RECOGNITION_PAGE_COUNT_BINDING"):
        representation(items)


@pytest.mark.parametrize("released", [False, None])
def test_unknown_failure_without_release_stays_fail_closed(released):
    items = source_events()
    items[-1]["resources_released"] = released
    rehash(items[-1])
    with pytest.raises(ValueError, match="RECOGNITION_PAGE_COUNT_BINDING"):
        representation(items)


def test_known_contradictory_terminal_total_remains_integrity_error():
    items = source_events()
    items[-1]["expected_pages"] = 3
    rehash(items[-1])
    with pytest.raises(ValueError, match="RECOGNITION_PAGE_COUNT_BINDING"):
        representation(items)


@pytest.mark.parametrize("page_before_terminal", [True, False])
def test_unknown_failure_cannot_hide_an_actual_page_from_the_same_request(page_before_terminal):
    items = source_events()
    page = copy.deepcopy(items[1])
    page.update(request_id="secondary-reader", stage="PADDLE_STRUCTURED")
    page["payload"] = {"artifacts": [artifact("PADDLE_PAGE_JSON", {"page_index": 0, "parsing_res_list": []})]}
    rehash(page)
    items.insert(-1 if page_before_terminal else len(items), page)
    with pytest.raises(ValueError, match="RECOGNITION_PAGE_COUNT_BINDING"):
        representation(items)


def test_preserved_primary_candidate_is_not_an_independently_verified_secondary():
    items, config = source_events(), definition(critical=True)
    result = adapt_saved_recognition(items, config, {"extraction_strategy": "QWEN_VISUAL"}, source_sha256="a" * 64)
    assert result["payload"]["fields"]["reference"]["value"] == "PUBLIC-REFERENCE"
    assert result["engine"]["execution"]["recognition_complete"] is True
    assert result["engine"]["candidate_stages"] == ["QWEN_VISUAL"]
    report = quality_report(attest(result, config, items), config)
    assert report["status"] == "REVIEW_REQUIRED"


def test_partial_primary_cannot_be_completed_by_zero_page_secondary_failure():
    items, config = source_events(emitted=1), definition(critical=True)
    result = adapt_saved_recognition(items, config, {"extraction_strategy": "QWEN_VISUAL"}, source_sha256="a" * 64)
    execution = result["engine"]["execution"]
    assert execution["expected_pages"] == 2 and execution["completed_pages"] == [1]
    assert execution["pending_pages"] == [2] and execution["recognition_complete"] is False
    assert quality_report(attest(result, config, items), config)["status"] == "REVIEW_REQUIRED"
