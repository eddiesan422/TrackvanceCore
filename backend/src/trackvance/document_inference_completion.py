"""Worker-owned deadline and incomplete-stage facts; model metadata is not authority."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time


class DocumentDeadlineExpired(Exception):
    """The frozen total/mandatory-stage deadline expired. Never recover as partial."""


class DocumentCheckpointFailure(Exception):
    """A worker authority callback failed; never classify it as a control timeout."""
    def __init__(self, original):
        self.original = original


class AttemptBudget:
    def __init__(self, total_seconds, *, started=None, clock=time.monotonic):
        if type(total_seconds) not in (int, float) or not math.isfinite(total_seconds) or not 30 <= total_seconds <= 1800:
            raise ValueError("INVALID_DOCUMENT_DEADLINE")
        self.clock = clock
        self.started = clock() if started is None else started
        self.deadline = self.started + total_seconds
        self.total_seconds = total_seconds
        self.validation_seconds = min(120.0, max(10.0, total_seconds * .2))
        self.commit_seconds = min(10.0, max(5.0, total_seconds * .05))
        self.validation_deadline = self.deadline - self.commit_seconds
        self.extraction_deadline = self.validation_deadline - self.validation_seconds

    def require(self, deadline=None):
        remaining = min(self.deadline, self.deadline if deadline is None else deadline) - self.clock()
        if remaining <= 0:
            raise DocumentDeadlineExpired()
        return remaining

    def control_deadline(self):
        self.require(self.validation_deadline)
        return self.validation_deadline  # run_engine independently retains its native120s cap.

    def report(self):
        return {"policy_version": "IDP_TOTAL_ATTEMPT_V1", "total_seconds": self.total_seconds,
            "validation_reserve_seconds": self.validation_seconds, "commit_reserve_seconds": self.commit_seconds,
            "optional_allocation_seconds": self.extraction_deadline - self.started,
            "observed_attempt_seconds": round(self.clock() - self.started, 4)}


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class LocalCandidateResult(dict):
    """In-process side channel; a JSON provider response cannot instantiate it."""
    completion_marker: dict | None = None


def trusted_partial_result(data, marker):
    result = LocalCandidateResult(data)
    result.completion_marker = copy.deepcopy(marker)
    return result


def marker_hash(marker):
    return canonical_hash(marker)


def source_pages(engine):
    count = engine.get("pages")
    if type(count) is not int or not 1 <= count <= 50:
        raise ValueError("INCOMPLETE_SOURCE_PAGES_UNVERIFIED")
    return list(range(1, count + 1))


def usable_candidate(payload):
    rows = [payload.get("fields", {}),
        *(row for rows in payload.get("tables", {}).values() for row in rows)]
    return any(isinstance(cell, dict) and cell.get("validity") == "VALID"
        and isinstance(cell.get("observed"), str) and bool(cell["observed"].strip())
        and isinstance(cell.get("value"), str) for row in rows for cell in row.values())


def make_marker(*, payload, stage, source_sha256, definition, configuration, pages, completed_stages, request_id):
    if stage not in {"INTERPRET", "REFINE"} or not usable_candidate(payload):
        raise ValueError("INCOMPLETE_CANDIDATE_UNAVAILABLE")
    if (not pages or any(type(page) is not int for page in pages)
            or pages != list(range(1, len(pages) + 1)) or len(pages) > 50):
        raise ValueError("INCOMPLETE_SOURCE_PAGES_UNVERIFIED")
    return {"version": 1, "status": "PARTIAL", "failed_stage": stage,
        "error_code": "DOCUMENT_TIMEOUT", "automatic_approval_blocked": True,
        "source_sha256": source_sha256, "definition_sha256": canonical_hash(definition),
        "configuration_sha256": canonical_hash(configuration), "checkpoint_payload_sha256": canonical_hash(payload),
        "source_pages": pages, "completed_stages": copy.deepcopy(completed_stages),
        "request_id": request_id}


def all_cells_reviewed(payload):
    rows = [payload.get("fields", {}), *(row for rows in payload.get("tables", {}).values() for row in rows)]
    cells = [cell for row in rows for cell in row.values()]
    return bool(cells) and all(cell.get("reviewed") is True and cell.get("validity") == "VALID" for cell in cells)


def unresolved_completion(extraction):
    marker = (getattr(extraction, "engine", None) or {}).get("inference_completion")
    if not marker:
        return False
    resolution = extraction.payload.get("inference_completion_resolution")
    if not getattr(extraction, "reviewed", False) or not isinstance(resolution, dict) or not all_cells_reviewed(extraction.payload):
        return True
    return not (resolution.get("version") == 1 and resolution.get("marker_sha256") == marker_hash(marker)
        and resolution.get("source_sha256") == marker.get("source_sha256")
        and resolution.get("definition_sha256") == marker.get("definition_sha256")
        and resolution.get("pages") == marker.get("source_pages")
        and resolution.get("all_pages_confirmed") is True and resolution.get("all_rows_confirmed") is True
        and isinstance(resolution.get("actor_id"), str) and bool(resolution["actor_id"])
        and isinstance(resolution.get("reason"), str) and bool(resolution["reason"].strip())
        and isinstance(resolution.get("created_at"), str) and bool(resolution["created_at"]))


def completion_issues(extraction, definition):
    if not unresolved_completion(extraction):
        return []
    marker = extraction.engine["inference_completion"]
    result = ["INFERENCE_INCOMPLETE:" + str(marker.get("failed_stage", "UNKNOWN"))]
    scopes = {"headers": definition["fields"], **{table["name"]: table["fields"] for table in definition["tables"]}}
    for name, fields in scopes.items():
        rows = [extraction.payload["fields"]] if name == "headers" else extraction.payload["tables"].get(name, [])
        for index, row in enumerate(rows):
            for field in fields:
                if row.get(field["name"], {}).get("reviewed") is not True:
                    result.append(f"REVIEW_REQUIRED:{name}.{index}.{field['name']}")
    return result


def confirmation_binding(original, extraction):
    marker = extraction.engine["inference_completion"]
    return {"source_sha256": original.sha256, "expected_extraction_id": extraction.id,
        "expected_payload_hash": extraction.payload_hash, "expected_marker_hash": marker_hash(marker),
        "pages": marker["source_pages"]}
