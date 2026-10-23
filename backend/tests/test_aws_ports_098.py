"""Local doubles only. These assertions do not homologate real AWS services."""
import copy
import io

import polars as pl
import pytest
from sqlalchemy import func, select

from trackvance.artifactstore import ArtifactIntegrityError, StorageProvider, storage_provider
from trackvance.models import Artifact, ArtifactLink
from trackvance.s3_artifactstore import S3ArtifactStore
from trackvance.textract_adapter import (
    TextractAnalyzeExpenseAdapter,
    TextractError,
    normalize_expenses,
)


class SimulatedError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class LocalS3:
    trackvance_local_simulation = True

    def __init__(self):
        self.objects = {}

    def put_object(self, **request):
        identity = request["Bucket"], request["Key"]
        assert request["IfNoneMatch"] == "*"
        if identity in self.objects:
            raise SimulatedError("PreconditionFailed")
        data = request["Body"].read()
        assert len(data) == request["ContentLength"]
        self.objects[identity] = {"bytes": data, "ContentLength": len(data),
            "Metadata": request["Metadata"], "ChecksumSHA256": request["ChecksumSHA256"]}
        return {}

    def get_object(self, *, Bucket, Key, **kwargs):
        value = self.head_object(Bucket=Bucket, Key=Key)
        return {**value, "Body": io.BytesIO(value["bytes"])}

    def head_object(self, *, Bucket, Key, **kwargs):
        if (Bucket, Key) not in self.objects:
            raise SimulatedError("NoSuchKey")
        return self.objects[Bucket, Key]


def test_aws_ports_reject_unmarked_clients_before_any_call(tmp_path):
    with pytest.raises(ValueError, match="AWS_REAL_EXECUTION_DISABLED"):
        S3ArtifactStore(tmp_path, client=object(), bucket="local-test", organization_id="default")
    with pytest.raises(ValueError, match="AWS_REAL_EXECUTION_DISABLED"):
        TextractAnalyzeExpenseAdapter(object())
    assert type(storage_provider).__name__ == "FileArtifactStore"


def test_s3_immutability_rollback_replay_integrity_and_organization(database, tmp_path):
    client = LocalS3()
    store = S3ArtifactStore(tmp_path / "cache", client=client, bucket="local-test", organization_id="default")
    assert isinstance(store, StorageProvider)
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"local synthetic bytes")
    with database() as db:
        first = store.put_file(db, source, "ORIGINAL", "default", artifact_id="stable-artifact")
        locator, digest = first.path, first.sha256
        db.rollback()
        second = store.put_file(db, source, "ORIGINAL", "default", artifact_id="stable-artifact")
        db.commit()
        assert (second.path, second.sha256) == (locator, digest)
        assert len(client.objects) == 1
        assert store.open_read(second).read() == source.read_bytes()
        assert store.exists(second)
        with pytest.raises(ArtifactIntegrityError, match="ORGANIZATION"):
            store.put_file(db, source, "ORIGINAL", "other", artifact_id="stable-artifact")
        for reference in [locator.replace("/default/", "/other/"), locator + "?secret=x",
                          locator.replace("artifacts/", "../"), locator.replace("local-test", "other-bucket")]:
            with pytest.raises(ArtifactIntegrityError):
                store.materialize_reference(reference, expected_sha256=digest, expected_size=source.stat().st_size)
        source.write_bytes(b"different document")
        with pytest.raises(ArtifactIntegrityError, match="IMMUTABLE"):
            store.put_file(db, source, "ORIGINAL", "default", artifact_id="stable-artifact")
        assert store.open_read(second).read() == b"local synthetic bytes"
        store.materialize(second).write_bytes(b"tampered")
        with pytest.raises(ArtifactIntegrityError, match="HASH_MISMATCH"):
            store.materialize(second)


def test_s3_download_catches_corrupt_remote_body_and_limits(database, tmp_path):
    client = LocalS3()
    store = S3ArtifactStore(tmp_path / "cache", client=client, bucket="local-test", organization_id="default",
                            max_object_bytes=32, max_cache_bytes=100)
    source = tmp_path / "input.txt"
    source.write_bytes(b"hello")
    with database() as db:
        artifact = store.put_file(db, source, "ORIGINAL", "default")
        store.materialize(artifact).unlink()
        next(iter(client.objects.values()))["bytes"] = b"other"
        with pytest.raises(ArtifactIntegrityError, match="HASH_MISMATCH"):
            store.materialize(artifact)
        assert not list((store.root / "tmp").iterdir())
        source.write_bytes(b"x" * 33)
        with pytest.raises(ArtifactIntegrityError, match="CAPACITY"):
            store.put_file(db, source, "ORIGINAL", "default")


def test_s3_multipart_dataset_reuses_native_schema_and_lineage(database, tmp_path):
    store = S3ArtifactStore(tmp_path / "cache", client=LocalS3(), bucket="local-test", organization_id="default")
    parts = [tmp_path / "one.parquet", tmp_path / "two.parquet"]
    for index, path in enumerate(parts):
        pl.DataFrame({"id": [index], "amount": ["12.25"]}).write_parquet(path)
    with database() as db:
        artifact = store.put_dataset(db, parts, "IDP_DETAIL", "default", artifact_id="detail")
        db.commit()
        assert len(store.dataset_paths(artifact)) == 2
        assert pl.concat([pl.read_parquet(p) for p in store.dataset_paths(artifact)]).height == 2
        assert db.scalar(select(func.count()).select_from(Artifact)) == 3
        assert db.scalar(select(func.count()).select_from(ArtifactLink)) == 2
        repeated = store.put_dataset(db, parts, "IDP_DETAIL", "default", artifact_id="detail")
        assert repeated.id == artifact.id
        assert db.scalar(select(func.count()).select_from(Artifact)) == 3


def expense_response(index=1, status="SUCCEEDED"):
    detection = {"Text": "12,25", "Geometry": {"BoundingBox": {"Left": .1, "Top": .2, "Width": .2, "Height": .05}}}
    return {"JobStatus": status, "DocumentMetadata": {"Pages": 1}, "AnalyzeExpenseModelVersion": "fixture-v1",
        "ExpenseDocuments": [{"ExpenseIndex": index,
            "SummaryFields": [{"Type": {"Text": "TOTAL"}, "PageNumber": 1, "ValueDetection": detection}],
            "LineItemGroups": [{"LineItemGroupIndex": 1, "LineItems": [
                {"LineItemExpenseFields": [{"Type": {"Text": "PRICE"}, "PageNumber": 1, "ValueDetection": detection}]}]}],
            "Blocks": [{"Id": f"line-{index}", "BlockType": "LINE", "Page": 1, **detection}]}]}


class LocalTextract:
    trackvance_local_simulation = True

    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def analyze_expense(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)

    def start_expense_analysis(self, **kwargs):
        self.calls.append(kwargs)
        return {"JobId": "local-job"}

    def get_expense_analysis(self, **kwargs):
        self.calls.append(kwargs)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_textract_normalizes_sync_and_async_pagination_without_losing_rows():
    sync = TextractAnalyzeExpenseAdapter(LocalTextract([expense_response()])).analyze(b"synthetic")
    assert sync["expense_documents"][0]["fields"][0]["observed"] == "12,25"
    assert sync["expense_documents"][0]["fields"][0]["reviewed"] is False
    from trackvance.document_engine import extract
    from trackvance.document_schemas import DocumentDefinition
    definition = DocumentDefinition.model_validate({"fields": [{"name": "amount", "pattern": r"([0-9,]+)",
        "logical_type": "DECIMAL", "decimal_separator": ","}]}).model_dump(mode="json")
    result = extract(sync["lines"], definition)
    assert result["fields"]["amount"]["value"] == "12.25"
    assert result["fields"]["amount"]["confidence"] is None
    assert result["fields"]["amount"]["reviewed"] is False
    first, second = expense_response(), expense_response(2)
    first["NextToken"] = "page2"
    client = LocalTextract([{"JobStatus": "IN_PROGRESS"}, first, second])
    adapter = TextractAnalyzeExpenseAdapter(client)
    args = {"bucket": "local-test", "key": "trackvance/default/source.pdf", "organization_id": "default",
            "source_sha256": "a" * 64, "configuration_sha256": "b" * 64}
    job = adapter.start(**args)
    assert adapter.start(**args).request_token == job.request_token
    assert adapter.poll(job).status == "IN_PROGRESS"
    assert adapter.poll(job).next_token == "page2"
    assert adapter.poll(job).status == "SUCCEEDED"
    assert client.calls[-1]["NextToken"] == "page2"
    assert len(job.result["expense_documents"]) == 2
    assert len(job.result["lines"]) == 2


def test_textract_retry_cancel_partial_failure_and_pagination_cycle():
    client = LocalTextract([SimulatedError("ThrottlingException"), expense_response(status="PARTIAL_SUCCESS")])
    waits = []
    adapter = TextractAnalyzeExpenseAdapter(client, sleep=waits.append)
    job = adapter.start(bucket="local-test", key="trackvance/default/source.pdf", organization_id="default",
        source_sha256="a" * 64, configuration_sha256="b" * 64)
    assert adapter.poll(job).status == "PARTIAL_SUCCESS"
    assert waits == [.25]
    cancelled = adapter.start(bucket="local-test", key="trackvance/default/source.pdf", organization_id="default",
        source_sha256="c" * 64, configuration_sha256="b" * 64)
    count = len(client.calls)
    assert adapter.poll(adapter.cancel(cancelled)).status == "CANCELLED"
    assert len(client.calls) == count
    looping = expense_response()
    looping["NextToken"] = "cycle"
    client.responses = [looping, copy.deepcopy(looping)]
    job = adapter.start(bucket="local-test", key="trackvance/default/source.pdf", organization_id="default",
        source_sha256="d" * 64, configuration_sha256="b" * 64)
    adapter.poll(job)
    with pytest.raises(TextractError, match="PAGINATION_CYCLE"):
        adapter.poll(job)
    failed = adapter.start(bucket="local-test", key="trackvance/default/source.pdf", organization_id="default",
        source_sha256="e" * 64, configuration_sha256="b" * 64)
    client.responses = [{"JobStatus": "FAILED"}]
    assert adapter.poll(failed).status == "FAILED"


def test_textract_rejects_identity_metadata_geometry_and_missing_word_evidence():
    response = expense_response()
    malformed = copy.deepcopy(response)
    malformed["AnalyzeExpenseModelVersion"] = "different"
    with pytest.raises(TextractError, match="METADATA_DRIFT"):
        normalize_expenses([response, malformed])
    malformed = copy.deepcopy(response)
    malformed["ExpenseDocuments"][0]["Blocks"][0]["Text"] = "different"
    with pytest.raises(TextractError, match="BLOCK_ID_CONFLICT"):
        normalize_expenses([response, malformed])
    malformed = copy.deepcopy(response)
    malformed["ExpenseDocuments"][0]["Blocks"][0]["Geometry"]["BoundingBox"]["Width"] = 2
    with pytest.raises(TextractError, match="GEOMETRY_INVALID"):
        normalize_expenses([malformed])
    malformed = copy.deepcopy(response)
    malformed["ExpenseDocuments"][0]["Blocks"][0]["Relationships"] = [{"Type": "CHILD", "Ids": ["missing"]}]
    with pytest.raises(TextractError, match="RELATIONSHIP_MISSING"):
        normalize_expenses([malformed])
    malformed["ExpenseDocuments"][0]["Blocks"].append({"Id": "missing", "BlockType": "WORD", "Page": 2,
        "Text": "12,25", "Geometry": response["ExpenseDocuments"][0]["Blocks"][0]["Geometry"]})
    with pytest.raises(TextractError, match="WORD_PAGE_MISMATCH"):
        normalize_expenses([malformed])


@pytest.mark.parametrize("status", ["SUCCEEDED", "PARTIAL_SUCCESS"])
def test_textract_simulation_uses_unified_candidate_contract_and_cannot_autoapprove(status):
    from trackvance.document_providers import hash_value
    from trackvance.document_quality import (
        certify_candidate,
        quality_report,
        validate_candidate_contract,
    )
    from trackvance.document_schemas import DocumentDefinition

    definition = DocumentDefinition.model_validate({"fields": [{"name": "amount", "pattern": r"([0-9,]+)",
        "logical_type": "STRING"}]}).model_dump(mode="json")
    client = LocalTextract([expense_response(status=status)])
    adapter = TextractAnalyzeExpenseAdapter(client)
    normalized = adapter.analyze(b"synthetic")
    configuration = {"provider": "AWS_TEXTRACT", "timeout_seconds": 60}
    result = adapter.candidate(normalized, definition, source_sha256="a" * 64, configuration=configuration)
    validate_candidate_contract(result, definition)
    assert set(result) == {"payload", "engine"} and len(client.calls) == 1
    assert result["payload"]["fields"]["amount"]["value"] == "12,25"
    assert result["payload"]["fields"]["amount"]["reviewed"] is False
    assert result["engine"]["status"] == status and result["engine"]["simulation"] is True
    assert result["engine"]["configuration"]["timeout_seconds"] == 60
    assert result["engine"]["configuration_sha256"] == hash_value(result["engine"]["configuration"])
    assert result["engine"]["normalization_sha256"] == hash_value(normalized)
    assert result["engine"]["expense_indices"] == [1]
    result["engine"]["validation"] = certify_candidate(result, copy.deepcopy(result), definition, "a" * 64)
    from types import SimpleNamespace
    quality = quality_report(SimpleNamespace(payload=result["payload"], engine=result["engine"], reviewed=False), definition)
    assert quality["status"] == "REVIEW_REQUIRED"
    assert "SEMANTIC_CONFLICT:SIMULATED_PROVIDER" in quality["issues"]
    assert result["engine"]["automatic_approval_eligible"] is False


def test_textract_candidate_rejects_missing_evidence_and_provider_substitution():
    from trackvance.document_schemas import DocumentDefinition

    definition = DocumentDefinition.model_validate({"fields": [{"name": "amount", "pattern": r"([0-9,]+)"}]}).model_dump(mode="json")
    adapter = TextractAnalyzeExpenseAdapter(LocalTextract([expense_response()]))
    normalized = adapter.analyze(b"synthetic")
    with pytest.raises(TextractError, match="CANDIDATE_PROVIDER_INVALID"):
        adapter.candidate(normalized, definition, source_sha256="a" * 64, configuration={"provider": "PADDLE_QWEN_LOCAL"})
    absent = copy.deepcopy(normalized)
    absent["lines"][0]["region"] = None
    with pytest.raises(TextractError, match="GEOMETRY_MISSING"):
        adapter.candidate(absent, definition, source_sha256="a" * 64)
    absent = copy.deepcopy(normalized)
    absent["simulation"] = False
    with pytest.raises(TextractError, match="SCOPE_INVALID"):
        adapter.candidate(absent, definition, source_sha256="a" * 64)
