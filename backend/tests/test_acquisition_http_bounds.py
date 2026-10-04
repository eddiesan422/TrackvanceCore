"""Reception and asynchronous inspection diagnose the same effective bounds."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import anyio
import polars as pl
import pytest
from sqlalchemy import func, select
from starlette.requests import Request

from trackvance import acquisition_api
from trackvance.acquisition import AcquisitionOperationError
from trackvance.acquisition_config import AcquisitionLimits
from trackvance.acquisition_errors import AcquisitionReadError
from trackvance.acquisition_models import AcquisitionRun, AcquisitionUpload
from trackvance.batch_readers import FileBatchReader, inspect_file
from trackvance.dataset_readers import DatasetCellLimit
from trackvance.processing import ProcessingError


@pytest.mark.parametrize("filename", ["source.xlsx", "source.json"])
def test_announced_format_bound_rejects_before_allocating_or_writing_staging(authenticated, monkeypatch, filename):
    limits = replace(AcquisitionLimits(), max_upload_bytes=4096, xlsx_max_upload_bytes=1024, bounded_format_bytes=1024)
    monkeypatch.setattr(AcquisitionLimits, "configured", classmethod(lambda cls: limits))
    allocations = []
    def forbidden_allocation(suffix):
        allocations.append(suffix)
        raise AssertionError("An oversized announced upload must not allocate staging")
    monkeypatch.setattr(acquisition_api.storage_provider, "temporary_path", forbidden_allocation)
    content = b"[" + b"x" * 1535 if filename.endswith(".json") else b"x" * 1536
    response = authenticated.post("/api/v1/datasets/uploads/stage", params={"filename": filename}, content=content)
    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "UPLOAD_TOO_LARGE"
    assert response.json()["error"]["details"] == {"limit": "compressed_bytes", "maximum": 1024, "observed": 1536}
    assert allocations == []


@pytest.mark.parametrize("filename", ["source.xlsx", "source.json"])
def test_chunked_format_bound_stops_before_oversized_write_and_removes_partial(tmp_path, monkeypatch, filename):
    limits = replace(AcquisitionLimits(), max_upload_bytes=4096, xlsx_max_upload_bytes=1024, bounded_format_bytes=1024)
    monkeypatch.setattr(AcquisitionLimits, "configured", classmethod(lambda cls: limits))
    target = tmp_path / "chunked.tmp"
    monkeypatch.setattr(acquisition_api.storage_provider, "temporary_path", lambda suffix: target)
    real_open, written, received = Path.open, [], []
    class TrackedFile:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def write(self, chunk):
            written.append(len(chunk))
            return self.stream.write(chunk)
        def __getattr__(self, name):
            return getattr(self.stream, name)
    def tracked_open(path, *args, **kwargs):
        stream = real_open(path, *args, **kwargs)
        return TrackedFile(stream) if path == target else stream
    monkeypatch.setattr(Path, "open", tracked_open)
    async def receive():
        received.append(1)
        body = b"[" + b"x" * 511 if filename.endswith(".json") and len(received) == 1 else b"x" * 512
        return {"type": "http.request", "body": body, "more_body": True}
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive)
    async def invoke():
        return await acquisition_api.stage_file(request, filename, db=SimpleNamespace(), user=SimpleNamespace())
    with pytest.raises(AcquisitionOperationError) as failure:
        anyio.run(invoke)
    assert failure.value.code == "UPLOAD_TOO_LARGE"
    assert failure.value.details == {"limit": "compressed_bytes", "maximum": 1024, "observed": 1536}
    assert written == [512, 512] and len(received) == 3
    assert not target.exists()


@pytest.mark.parametrize("filename,content", [
    ("source.csv", b"id,value\n" + b"001,abcdefghijklmnopqrstuvwxyz\n" * 60),
    ("source.ndjson", b'{"id":"001","value":"abcdefghijklmnopqrstuvwxyz"}\n' * 40),
    ("source.json", b'{"id":"001","value":"abcdefghijklmnopqrstuvwxyz"}\n' * 40),
])
def test_streaming_text_formats_keep_the_general_reception_bound(authenticated, monkeypatch, filename, content):
    limits = replace(AcquisitionLimits(), max_upload_bytes=4096, xlsx_max_upload_bytes=1024, bounded_format_bytes=1024)
    monkeypatch.setattr(AcquisitionLimits, "configured", classmethod(lambda cls: limits))
    response = authenticated.post("/api/v1/datasets/uploads/stage", params={"filename": filename}, content=content)
    assert 1024 < len(content) < 4096
    assert response.status_code == 201, response.text
    assert response.json()["upload"]["size_bytes"] == len(content)


def test_parquet_reception_keeps_general_bound(authenticated, tmp_path, monkeypatch):
    path = tmp_path / "source.parquet"
    pl.DataFrame({"id": [str(index) for index in range(500)], "value": [f"{index:05d}-varied" for index in range(500)]}).write_parquet(path, compression="uncompressed")
    content = path.read_bytes()
    limits = replace(AcquisitionLimits(), max_upload_bytes=16384, xlsx_max_upload_bytes=1024, bounded_format_bytes=1024)
    monkeypatch.setattr(AcquisitionLimits, "configured", classmethod(lambda cls: limits))
    assert 1024 < len(content) < 16384
    response = authenticated.post("/api/v1/datasets/uploads/stage", params={"filename": path.name}, content=content)
    assert response.status_code == 201, response.text
    assert response.json()["upload"]["size_bytes"] == len(content)


@pytest.mark.parametrize("content", [b"PK" + b"x" * 1534, ("[\"" + "é" * 2500).encode()])
def test_content_signature_applies_format_bound_before_allocating_even_with_csv_name(authenticated, monkeypatch, content):
    limits = replace(AcquisitionLimits(), max_upload_bytes=8192, xlsx_max_upload_bytes=1024, bounded_format_bytes=1024)
    monkeypatch.setattr(AcquisitionLimits, "configured", classmethod(lambda cls: limits))
    def forbidden_allocation(suffix):
        raise AssertionError("Known content signature must be bounded before allocation")
    monkeypatch.setattr(acquisition_api.storage_provider, "temporary_path", forbidden_allocation)
    response = authenticated.post("/api/v1/datasets/uploads/stage", params={"filename": "source.csv"}, content=content)
    assert response.status_code == 413
    assert response.json()["error"]["details"]["maximum"] == 1024


@pytest.mark.parametrize("failure,expected", [
    (ProcessingError("Este prototipo admite hasta 100,000 filas por archivo."), "ACQUISITION_ROW_LIMIT"),
    (DatasetCellLimit("business-value must never be echoed"), "ACQUISITION_CELL_LIMIT"),
    (ProcessingError("Este prototipo admite hasta 100 columnas."), "ACQUISITION_COLUMN_LIMIT"),
    (ProcessingError("ACQUISITION_RECORD_LIMIT: private-value /secret/path"), "ACQUISITION_RECORD_LIMIT"),
    (ProcessingError("ACQUISITION_BATCH_LIMIT: private-value /secret/path"), "ACQUISITION_BATCH_LIMIT"),
    (ProcessingError("private-value /secret/path token=confidential"), "ACQUISITION_INVALID_DATA"),
])
@pytest.mark.parametrize("route", ["stage", "inspect", "register"])
def test_async_http_inspection_uses_closed_safe_diagnostics(authenticated, database, monkeypatch, failure, expected, route):
    content = b"id,value\n001,exact\n"
    staged = authenticated.post("/api/v1/datasets/uploads/stage", params={"filename": "source.csv"}, content=content).json()
    dataset = authenticated.post("/api/v1/datasets", json={"name": "Safe HTTP diagnosis"}).json()
    def broken_reader(self):
        raise failure
        yield  # Generator lifecycle must remain closeable.
    monkeypatch.setattr(FileBatchReader, "__iter__", broken_reader)
    if route == "stage":
        response = authenticated.post("/api/v1/datasets/uploads/stage", params={"filename": "failure.csv"}, content=content)
    elif route == "inspect":
        response = authenticated.get(f'/api/v1/datasets/uploads/{staged["upload"]["id"]}/inspect')
    else:
        response = authenticated.post(f'/api/v1/datasets/{dataset["id"]}/acquisitions', json={"upload_id": staged["upload"]["id"]})
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == expected and error["request_id"]
    assert all(secret not in response.text for secret in ["private-value", "/secret/path", "confidential", "business-value"])
    with database() as db:
        assert db.scalar(select(func.count()).select_from(AcquisitionRun)) == 0
        assert db.scalar(select(func.count()).select_from(AcquisitionUpload)) == 1


@pytest.mark.parametrize("budget,code,name", [
    ({"max_rows": 1}, "ACQUISITION_ROW_LIMIT", "data_rows"),
    ({"max_observed_bytes": 1}, "ACQUISITION_EXPANDED_SIZE_LIMIT", "expanded_bytes"),
])
def test_parquet_complete_footer_reports_specific_limit_before_population_read(tmp_path, budget, code, name):
    path = tmp_path / "footer.parquet"
    pl.DataFrame({"id": ["first", "second"]}).write_parquet(path)
    limits = replace(AcquisitionLimits(), **budget)
    with pytest.raises(AcquisitionReadError) as failure:
        inspect_file(path, path.name, limits=limits)
    assert failure.value.code == code
    assert failure.value.details["limit"] == name
    assert failure.value.details["maximum"] == 1
    assert failure.value.details["observed"] >= 2
