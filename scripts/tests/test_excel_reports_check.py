"""Watchdog contracts without opening Excel during unit tests."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import excel_reports_check as runner


def test_boundary_expectations_cover_all_null_forms_without_materializing_population(tmp_path):
    path = tmp_path / "client.xlsx"
    path.write_bytes(b"fixture-for-hash")
    result = runner.expectation(path, 1000000, 100000, "a" * 40)
    assert result["rows"] == 1000000
    assert [sample["worksheet_row"] for sample in result["samples"]] == [2, 3, 4, 5, 6, 1000001]
    assert [sample["values"][8] for sample in result["samples"][:5]] == [None, "", r"\N", "'literal", " "]
    assert result["samples"][-1]["values"][0] == "000001000000"
    assert result["samples"][-1]["values"][1] == "00099999"
    assert result["samples"][1]["values"][9] == "=SUM(1,2)"
    assert all(len(sample["values"]) == 11 for sample in result["samples"])


@pytest.mark.parametrize("sample,ready,code", [
    ({"rss_bytes": 10, "peak_rss_bytes": 101, "logical_processor_count": 1}, True, "EXCEL_MEMORY_BUDGET"),
    ({"rss_bytes": 10, "peak_rss_bytes": 10, "logical_processor_count": 2}, True, "EXCEL_CPU_BUDGET"),
])
def test_watchdog_rejects_peak_memory_and_cpu_budget(sample, ready, code):
    with pytest.raises(runner.NativeFailure, match=code):
        runner.enforce_budget(sample, 100, affinity_ready=ready)


def test_watchdog_allows_only_narrow_affinity_setup_grace():
    runner.enforce_budget({"rss_bytes": 10, "peak_rss_bytes": 10,
                          "logical_processor_count": 2}, 100, affinity_ready=False)


def test_foreign_excel_pid_rejected_before_opening_process_handle():
    with pytest.raises(runner.NativeFailure, match="EXCEL_OWNERSHIP_INVALID"):
        runner.OwnedExcel({"status": "OWNED", "pid": 123, "preexisting_excel_pids": [123]})


def test_external_file_rejected(tmp_path):
    path = tmp_path / "foreign.xlsx"
    path.write_bytes(b"fixture")
    with pytest.raises(runner.NativeFailure, match="EXCEL_PRIVATE_CLIENT_FILE_REQUIRED"):
        runner.private_file(path)


def test_native_open_policy_ownership_precedes_any_option_change():
    script = Path(runner.__file__).with_suffix(".ps1").read_text(encoding="utf-8")
    ownership = script.index("Write-PrivateJson (Join-Path $directory 'ownership.json')")
    assert ownership < script.index("$excelProcess.ProcessorAffinity =")
    for option in ("$application.Visible = $false", "$application.DisplayAlerts = $false", "$application.AutomationSecurity = 3"):
        assert ownership < script.index(option)
    assert "[object[]]$openArguments = @($sourceFile, 0, $true, $missing, '', '', $true, $missing, $missing, $false, $false, $missing, $false, $false, 0)" in script
    assert "InvokeMember('Open', [Reflection.BindingFlags]::InvokeMethod, $null, $workbooks, $openArguments)" in script
    assert "if ($owned) {" in script and "$application.Quit()" in script


@pytest.mark.parametrize("rows,customers,sha", [(1000001, 1, "a" * 40), (10, 0, "a" * 40), (10, 2, "pending")])
def test_invalid_fixture_shape_or_provenance_rejected(tmp_path, rows, customers, sha):
    with pytest.raises(runner.NativeFailure):
        runner.expectation(tmp_path / "ignored.xlsx", rows, customers, sha)
