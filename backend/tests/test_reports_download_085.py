"""R085-01 small injected budgets and authoritative stream settlement."""
import io
import zipfile

import pytest
from openpyxl import load_workbook

from trackvance.db import utcnow
from trackvance.operations_common import OperationError
from trackvance.report_config import ReportLimits
from trackvance.report_exports import xlsx_stream
from trackvance.report_models import ReportContext, ReportExecution
from trackvance.report_service import terminal


def test_download_product_caps_cannot_be_raised_by_environment(monkeypatch):
    for profile in ("DOWNLOAD", "XLSX"):
        assert ReportLimits.configured(profile).max_rows == 1000000
        assert ReportLimits.configured(profile).max_bytes == 1024**3
        monkeypatch.setenv(f"REPORT_{profile}_MAX_ROWS", "1000001")
        assert ReportLimits.configured(profile).max_rows == 1000000
        monkeypatch.setenv(f"REPORT_{profile}_MAX_ROWS", "3")
        assert ReportLimits.configured(profile).max_rows == 3
    assert ReportLimits.configured("PREVIEW").max_rows == 10
    assert ReportLimits.configured("DATASET").max_rows == 5000000
    assert ReportLimits.configured("DATASET").dto()["max_cell_bytes"] == 65536
    assert ReportLimits.configured("DOWNLOAD").dto()["max_cell_bytes"] == 65536


def test_xlsx_data_row_cap_excludes_header_and_uses_excel_compatible_streamed_zip32(monkeypatch):
    monkeypatch.setenv("REPORT_XLSX_MAX_ROWS", "3")
    content = b"".join(xlsx_stream([{"name": "id"}], iter([[["001"], ["002"], ["003"]]])))
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.testzip() is None
        worksheet = archive.getinfo("xl/worksheets/sheet1.xml")
        assert worksheet.extract_version == 20 and worksheet.flag_bits & 8
    assert list(load_workbook(io.BytesIO(content), read_only=True).active.values) == [("id",), ("001",), ("002",), ("003",)]
    with pytest.raises(OperationError) as error:
        b"".join(xlsx_stream([{"name": "id"}], iter([[["001"], ["002"], ["003"], ["004"]]])))
    assert error.value.code == "REPORT_XLSX_ROWS"


def test_xlsx_effective_expanded_budget_cannot_require_unreadable_streamed_zip64(monkeypatch):
    monkeypatch.setenv("REPORT_XLSX_EXPANDED_MAX_BYTES", str(4 * 1024**3))
    limits = ReportLimits.configured("XLSX")
    assert limits.expanded_max_bytes == zipfile.ZIP64_LIMIT == 2 * 1024**3 - 1
    assert limits.dto()["xlsx_zip_policy"] == "ZIP32_STREAM_BOUNDED_BELOW_ZIP64"


@pytest.mark.parametrize("value", ["invalid\ufffe", "invalid\uffff", "invalid\ud800", "😀" * 16384], ids=["fffe", "ffff", "surrogate", "utf16-length"])
def test_xlsx_unrepresentable_xml_and_utf16_cell_lengths_are_failures(value):
    with pytest.raises(OperationError):
        b"".join(xlsx_stream([{"name": "text"}], iter([[[value]]])))


def test_expanded_xml_budget_is_independent_of_zip_size(monkeypatch):
    monkeypatch.setenv("REPORT_XLSX_EXPANDED_MAX_BYTES", "1024")
    with pytest.raises(OperationError) as error:
        b"".join(xlsx_stream([{"name": "text"}], iter([[["a" * 2000]]])))
    assert error.value.code == "REPORT_XLSX_EXPANDED_BYTES"


def execution(database, **state):
    with database() as db:
        context = ReportContext(id="download-context", user_id="test-user", expires_at=utcnow(), snapshot={}, integrity_hash="0" * 64)
        db.add(context)
        db.flush()
        item = ReportExecution(id="download-execution", context_id=context.id, user_id="test-user", profile="DOWNLOAD", **state)
        db.add(item)
        db.commit()
    return item.id


@pytest.mark.parametrize("later", ["SUCCESS", "INTERRUPTED"])
def test_transport_finally_preserves_original_generation_failure(database, later):
    identity = execution(database, status="FAILED", generation_status="FAILED", error_code="REPORT_XLSX_CELL_LIMIT", error_message="Original diagnostic")
    terminal(identity, status=later, generation="COMPLETE", transmission="COMPLETE" if later == "SUCCESS" else "INTERRUPTED")
    with database() as db:
        item = db.get(ReportExecution, identity)
        assert item.status == "FAILED" and item.generation_status == "FAILED"
        assert (item.error_code, item.error_message) == ("REPORT_XLSX_CELL_LIMIT", "Original diagnostic")


@pytest.mark.parametrize("generation", ["RUNNING", "COMPLETE"])
def test_success_requires_serialization_generation_complete(database, generation):
    identity = execution(database, status="RUNNING", generation_status=generation)
    terminal(identity, status="SUCCESS", generation="COMPLETE", transmission="COMPLETE")
    with database() as db:
        item = db.get(ReportExecution, identity)
        assert item.status == "FAILED" and item.error_code == "REPORT_STREAM_UNVERIFIED"
