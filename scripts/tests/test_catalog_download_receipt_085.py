"""Certification cannot accept a truncated transfer or omit a negative case."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2]))
from scripts.ci import validators


def receipt():
    oracle = {"rows": 120, "ordered_logical_sha256": "a" * 64, "logical_json_bytes": 12345}
    csv = {"status": "PASS", "format": "CSV", "independent_csv": dict(oracle),
           "file_sha256": "b" * 64, "terminal_status": "SUCCESS", "generation_status": "COMPLETE",
           "transmission_status": "COMPLETE", "bytes_received": 56789,
           "server_metrics": {"rows": 120, "serialized_bytes": 56789, "serialization_complete": True}}
    xlsx = {**csv, "format": "XLSX", "independent_xml": dict(oracle), "independent_openpyxl": dict(oracle),
            "physical_rows": 121, "ooxml": {"status": "PASS", "crc": "ALL_ENTRIES_PASS"}}
    xlsx.pop("independent_csv")
    return {"status": "PASS", "source_sha": "c" * 40, "main_unchanged": True,
            "requested_result_rows": [120], "product_row_limit_under_test": 120,
            "customers": 12, "transactions_input": 121, "client_result_storage": "PRIVATE_HOST_ONLY",
            "output_data_published": False, "client_resources": {"status": "PASS", "samples": 4,
            "sampled_peak_rss_bytes": 12345, "budget_bytes": 23456},
            "effective_limits": {"profiles": {"DOWNLOAD": {"max_rows": 120}, "XLSX": {"max_rows": 120}, "PREVIEW": {"max_rows": 10}}},
            "sources": [{"alias": alias, "status": "PASS", "approval_id": "approval", "output_version_id": "version",
                         "rows": rows, "input_fixture_sha256": "d" * 64, "input_fixture_bytes": 123}
                        for alias, rows in (("t", 121), ("c", 12))],
            "downloads": [csv, xlsx], "oracles": {"120": oracle}, "final_limit": dict(csv),
            "excess": [{"status": "PASS", "format": kind, "http_status": 422, "result_bytes_received": 0,
                        "error_code": "REPORT_RESULT_LIMIT", "terminal_status": "FAILED", "generation_status": "FAILED",
                        "transmission_status": "NOT_STARTED", "output_version_id": None, "execution_id": "execution"}
                       for kind in ("CSV", "XLSX")], "browser_streaming_requested": False}


@pytest.mark.parametrize("damage", ["no-receipt", "truncated", "missing-xlsx", "missing-excess", "excess-transmitted",
                                  "reader-mismatch", "not-settled", "wrong-cap", "different-source", "browser-skipped"])
def test_host_receipt_rejects_incomplete_evidence(damage):
    value = receipt()
    validators.catalog_download_receipt(value, [120], "c" * 40)
    if damage == "no-receipt":
        value = {"status": "PASS"}
    elif damage == "truncated":
        value["downloads"][0]["independent_csv"]["rows"] = 119
    elif damage == "missing-xlsx":
        value["downloads"].pop()
    elif damage == "missing-excess":
        value["excess"].pop()
    elif damage == "excess-transmitted":
        value["excess"][0]["result_bytes_received"] = 1
    elif damage == "reader-mismatch":
        value["downloads"][1]["independent_openpyxl"]["ordered_logical_sha256"] = "e" * 64
    elif damage == "not-settled":
        value["downloads"][0]["transmission_status"] = "INTERRUPTED"
    elif damage == "wrong-cap":
        value["effective_limits"]["profiles"]["XLSX"]["max_rows"] = 50
    elif damage == "different-source":
        value["source_sha"] = "f" * 40
    else:
        value["browser_streaming_requested"] = True
        value["browser_streaming"] = {"status": "PASS"}
    with pytest.raises(validators.EvidenceError, match="CATALOG_HOST_"):
        validators.catalog_download_receipt(value, [120], "c" * 40)
