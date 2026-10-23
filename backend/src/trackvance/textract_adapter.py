"""AnalyzeExpense sync/async adapter for local fixtures; AWS execution disabled.

Shapes follow the official AnalyzeExpense/StartExpenseAnalysis/GetExpenseAnalysis
APIs. No SDK is imported and no credentials are read. Simulation demonstrates
the port and error handling, never AWS homologation or measured OCR quality.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field

from .s3_artifactstore import error_code, require_simulation


class TextractError(ValueError):
    pass


def _region(geometry: dict) -> list[float] | None:
    if not isinstance(geometry, dict):
        raise TextractError("TEXTRACT_GEOMETRY_INVALID")
    box = geometry.get("BoundingBox")
    if not box:
        return None
    if not isinstance(box, dict):
        raise TextractError("TEXTRACT_GEOMETRY_INVALID")
    values = [box.get(k) for k in ("Left", "Top", "Width", "Height")]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise TextractError("TEXTRACT_GEOMETRY_INVALID")
    x, y, w, h = [float(value) for value in values if isinstance(value, (int, float))]
    if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1
            and x + w <= 1.000001 and y + h <= 1.000001):
        raise TextractError("TEXTRACT_GEOMETRY_INVALID")
    return [x, y, min(1.0, x + w), min(1.0, y + h)]


def _evidence(detection: dict, page: int, *, origin: str, identity: str) -> dict:
    if not isinstance(detection, dict):
        raise TextractError("TEXTRACT_EVIDENCE_INVALID")
    text = detection.get("Text", "")
    if not isinstance(text, str) or len(text) > 100_000 or type(page) is not int or not 1 <= page <= 3000:
        raise TextractError("TEXTRACT_EVIDENCE_INVALID")
    return {"observed": text, "page": page, "region": _region(detection.get("Geometry", {})),
            "origin": origin, "source_id": identity, "reviewed": False}


def normalize_expenses(responses: list[dict]) -> dict:
    """Retain page/block/expense/group identities, missing geometry and warnings.

    ExpenseIndex and LineItemGroupIndex may span API pagination. Block identity
    deduplicates repeated API blocks; repeated business rows are never silently
    deduplicated. Conflicting block IDs and pagination model drift are rejected.
    """
    if not 1 <= len(responses) <= 1000:
        raise TextractError("TEXTRACT_PAGE_LIMIT")
    documents: dict[int, dict] = {}
    blocks: dict[str, dict] = {}
    warnings, versions, page_counts = [], set(), set()
    for response in responses:
        version = response.get("AnalyzeExpenseModelVersion")
        if version:
            versions.add(str(version))
        count = response.get("DocumentMetadata", {}).get("Pages")
        if count is not None:
            if type(count) is not int or not 1 <= count <= 3000:
                raise TextractError("TEXTRACT_PAGE_COUNT_INVALID")
            page_counts.add(count)
        warnings.extend(response.get("Warnings", []))
        for expense in response.get("ExpenseDocuments", []):
            index = expense.get("ExpenseIndex")
            if type(index) is not int or index < 1:
                raise TextractError("TEXTRACT_EXPENSE_ID_INVALID")
            normalized = documents.setdefault(index, {"expense_index": index, "fields": [], "tables": {}})
            for ordinal, value in enumerate(expense.get("SummaryFields", [])):
                normalized["fields"].append({"type": value.get("Type", {}).get("Text", "OTHER"),
                    "label": value.get("LabelDetection", {}).get("Text"),
                    **_evidence(value.get("ValueDetection", {}), value.get("PageNumber", 1),
                        origin="AWS_TEXTRACT_SUMMARY", identity=f"expense:{index}:summary:{len(normalized['fields'])}:{ordinal}")})
            for group in expense.get("LineItemGroups", []):
                group_index = group.get("LineItemGroupIndex")
                if type(group_index) is not int or group_index < 1:
                    raise TextractError("TEXTRACT_GROUP_ID_INVALID")
                rows = normalized["tables"].setdefault(str(group_index), [])
                for item in group.get("LineItems", []):
                    row_index = len(rows)
                    rows.append([{"type": value.get("Type", {}).get("Text", "OTHER"),
                        **_evidence(value.get("ValueDetection", {}), value.get("PageNumber", 1),
                            origin="AWS_TEXTRACT_LINE_ITEM",
                            identity=f"expense:{index}:group:{group_index}:row:{row_index}:cell:{column}")}
                        for column, value in enumerate(item.get("LineItemExpenseFields", []))])
            for block in expense.get("Blocks", []):
                identity = block.get("Id")
                if not isinstance(identity, str) or not identity:
                    raise TextractError("TEXTRACT_BLOCK_ID_INVALID")
                if identity in blocks and blocks[identity] != block:
                    raise TextractError("TEXTRACT_BLOCK_ID_CONFLICT")
                blocks[identity] = block
    if len(versions) > 1 or len(page_counts) > 1:
        raise TextractError("TEXTRACT_PAGINATION_METADATA_DRIFT")
    lines = []
    for identity, block in blocks.items():
        if block.get("BlockType") != "LINE":
            continue
        evidence = _evidence(block, block.get("Page", 1), origin="AWS_TEXTRACT_BLOCK", identity=identity)
        words = []
        for relationship in block.get("Relationships", []):
            if relationship.get("Type") == "CHILD":
                for child in relationship.get("Ids", []):
                    word = blocks.get(child)
                    if not word or word.get("BlockType") != "WORD":
                        raise TextractError("TEXTRACT_WORD_RELATIONSHIP_MISSING")
                    item = _evidence(word, word.get("Page", evidence["page"]), origin="AWS_TEXTRACT_WORD", identity=child)
                    if item["page"] != evidence["page"]:
                        raise TextractError("TEXTRACT_WORD_PAGE_MISMATCH")
                    words.append({"text": item["observed"], "region": item["region"], "source_id": child, "confidence": None})
        lines.append({"text": evidence["observed"], "page": evidence["page"], "region": evidence["region"],
                      "source_id": identity, "method": "AWS_TEXTRACT", "words": words, "confidence": None})
    lines.sort(key=lambda line: (line["page"], (line["region"] or [0, 0])[1], (line["region"] or [0])[0]))
    if len(blocks) > 100_000 or len(json.dumps(responses)) > 64 * 1024**2:
        raise TextractError("TEXTRACT_RESPONSE_LIMIT")
    return {"provider": "AWS_TEXTRACT", "lines": lines,
            "expense_documents": [documents[k] for k in sorted(documents)],
            "pages": next(iter(page_counts), max((v["page"] for v in lines), default=0)),
            "model_version": next(iter(versions), None), "warnings": warnings, "simulation": True,
            "status": "PARTIAL_SUCCESS" if any(r.get("JobStatus") == "PARTIAL_SUCCESS" for r in responses) else "SUCCEEDED"}


@dataclass
class ExpenseJob:
    job_id: str
    request_token: str
    status: str = "IN_PROGRESS"
    next_token: str | None = None
    responses: list[dict] = field(default_factory=list)
    seen_tokens: set[str] = field(default_factory=set)
    result: dict | None = None


class TextractAnalyzeExpenseAdapter:
    def __init__(self, client, *, attempts: int = 3, timeout_seconds: float = 60,
                 check: Callable[[], None] = lambda: None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        require_simulation(client)
        if not 1 <= attempts <= 5 or not 0 < timeout_seconds <= 300:
            raise ValueError("TEXTRACT_LIMITS_INVALID")
        self.client, self.attempts, self.timeout = client, attempts, timeout_seconds
        self.check, self.sleep, self.clock = check, sleep, clock

    def candidate(self, normalized: dict, definition: dict, *, source_sha256: str,
                  configuration: dict | None = None) -> dict:
        """Map fixture observations into the same candidate boundary as local OCR.

        The caller retains the normalized response as evidence. No AWS type is
        guessed to be a family field: the existing declared family rules perform
        that mapping. Missing geometry is rejected rather than synthesized.
        A simulated provider can never approve or publish an operational result.
        """
        from .document_engine import extract
        from .document_providers import EngineConfiguration, hash_value
        from .document_quality import validate_candidate_contract

        require_simulation(self.client)
        if (normalized.get("provider") != "AWS_TEXTRACT" or normalized.get("simulation") is not True
                or normalized.get("status") not in {"SUCCEEDED", "PARTIAL_SUCCESS"}
                or not re.fullmatch(r"[a-f0-9]{64}", source_sha256)
                or type(normalized.get("pages")) is not int or not 1 <= normalized["pages"] <= 50):
            raise TextractError("TEXTRACT_CANDIDATE_SCOPE_INVALID")
        effective = EngineConfiguration.model_validate(configuration or {"provider": "AWS_TEXTRACT"}).model_dump()
        if effective["provider"] != "AWS_TEXTRACT":
            raise TextractError("TEXTRACT_CANDIDATE_PROVIDER_INVALID")
        lines = deepcopy(normalized["lines"])
        if (not lines or any(not isinstance(line.get("region"), list) or len(line["region"]) != 4
                or any(word.get("region") is None for word in line.get("words", [])) for line in lines)):
            raise TextractError("TEXTRACT_CANDIDATE_GEOMETRY_MISSING")
        payload = extract(lines, definition)
        payload["pages"] = list(range(1, normalized["pages"] + 1))
        payload["semantic_conflicts"] = ["SIMULATED_PROVIDER"]
        result = {"payload": payload, "engine": {"key": "AWS_TEXTRACT_SIMULATION_098", "provider": "AWS_TEXTRACT",
            "source_sha256": source_sha256, "configuration": effective,
            "configuration_sha256": hash_value(effective), "model_version": normalized.get("model_version"),
            "status": normalized["status"], "simulation": True, "automatic_approval_eligible": False,
            "normalization_sha256": hash_value(normalized),
            "expense_indices": [item["expense_index"] for item in normalized["expense_documents"]],
            "independent_row_coverage": False, "warning_count": len(normalized.get("warnings", []))}}
        validate_candidate_contract(result, definition)
        return result

    def _call(self, operation: str, **request) -> dict:
        require_simulation(self.client)
        began = self.clock()
        for attempt in range(self.attempts):
            self.check()
            if self.clock() - began >= self.timeout:
                raise TextractError("TEXTRACT_TIMEOUT")
            try:
                response = getattr(self.client, operation)(**request)
                self.check()
                if self.clock() - began >= self.timeout:
                    raise TextractError("TEXTRACT_TIMEOUT")
                if not isinstance(response, dict) or len(json.dumps(response)) > 64 * 1024**2:
                    raise TextractError("TEXTRACT_RESPONSE_LIMIT")
                return response
            except Exception as error:
                if error_code(error) not in {"ThrottlingException", "ProvisionedThroughputExceededException", "InternalServerError"}:
                    raise
                if attempt == self.attempts - 1:
                    raise TextractError("TEXTRACT_RETRIES_EXHAUSTED") from None
                self.sleep(min(0.25 * 2**attempt, max(0, self.timeout - (self.clock() - began))))
        raise TextractError("TEXTRACT_RETRIES_EXHAUSTED")

    def analyze(self, document: bytes) -> dict:
        if not isinstance(document, bytes) or not 1 <= len(document) <= 10 * 1024**2:
            raise TextractError("TEXTRACT_SYNC_INPUT_LIMIT")
        return normalize_expenses([self._call("analyze_expense", Document={"Bytes": document})])

    def start(self, *, bucket: str, key: str, organization_id: str, source_sha256: str,
              configuration_sha256: str, version_id: str | None = None) -> ExpenseJob:
        if (not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", organization_id)
                or not key.startswith(f"trackvance/{organization_id}/")
                or any(p in {"", ".", ".."} for p in key.split("/")) or "\\" in key
                or not all(re.fullmatch(r"[a-f0-9]{64}", h) for h in [source_sha256, configuration_sha256])):
            raise TextractError("TEXTRACT_SOURCE_INVALID")
        source = {"Bucket": bucket, "Name": key}
        if version_id:
            source["Version"] = version_id
        token = hashlib.sha256(json.dumps([organization_id, source_sha256, configuration_sha256, source],
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        response = self._call("start_expense_analysis", DocumentLocation={"S3Object": source}, ClientRequestToken=token)
        job_id = response.get("JobId")
        if not isinstance(job_id, str) or not job_id:
            raise TextractError("TEXTRACT_JOB_ID_MISSING")
        return ExpenseJob(job_id, token)

    def poll(self, job: ExpenseJob) -> ExpenseJob:
        """One bounded poll/page. Caller durably stores cursor and checks cancel.

        Textract has no expense cancellation API: local cancellation stops polling
        and blocks publication; this adapter never pretends to cancel a remote job.
        """
        self.check()
        if job.status in {"SUCCEEDED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED"}:
            return job
        request = {"JobId": job.job_id, "MaxResults": 1000}
        if job.next_token:
            request["NextToken"] = job.next_token
        response = self._call("get_expense_analysis", **request)
        status = response.get("JobStatus")
        if status not in {"IN_PROGRESS", "SUCCEEDED", "FAILED", "PARTIAL_SUCCESS"}:
            raise TextractError("TEXTRACT_STATUS_INVALID")
        if status == "IN_PROGRESS":
            if job.responses:
                raise TextractError("TEXTRACT_PAGINATION_STATUS_DRIFT")
            return job
        if status == "FAILED":
            job.status = "FAILED"
            return job
        token = response.get("NextToken")
        if token is not None and (not isinstance(token, str) or not token or token in job.seen_tokens):
            raise TextractError("TEXTRACT_PAGINATION_CYCLE")
        if len(job.responses) >= 1000:
            raise TextractError("TEXTRACT_PAGE_LIMIT")
        job.responses.append(response)
        job.next_token = token
        if token:
            job.seen_tokens.add(token)
        else:
            job.result = normalize_expenses(job.responses)
            job.status = "PARTIAL_SUCCESS" if any(r["JobStatus"] == "PARTIAL_SUCCESS" for r in job.responses) else "SUCCEEDED"
        return job

    def cancel(self, job: ExpenseJob) -> ExpenseJob:
        if job.status == "IN_PROGRESS":
            job.status = "CANCELLED"
        return job
