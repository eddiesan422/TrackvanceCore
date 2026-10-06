"""Standalone summary metadata must come from complete verified runtimes."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest
import spark_docker_cycle as cycle
from spark_docker_guard import assert_population

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci.common import EvidenceError, load_manifest
from scripts.ci.validators import validate_content


def population():
    """Artificial aggregate tests the contract; it certifies no real rows."""
    rows = 1000000
    total, xor = "a" * 64, "b" * 64
    fingerprint = {"method": "CANONICAL_JSON_ROW_SHA256_COUNT_SUM_XOR_V1", "rows": rows,
        "sum_sha256": total, "xor_sha256": xor,
        "sha256": hashlib.sha256(rows.to_bytes(8, "big") + bytes.fromhex(total) + bytes.fromhex(xor)).hexdigest()}
    outcomes = {module: {"status": "PASS", "runtime": {"deployment_mode": "STANDALONE_CLIENT",
        "executor_memory_status_entries": 3}, "results": {"logical_fingerprint": fingerprint}}
        for module in ("intake", "recon", "sentinel")}
    outcomes["intake"]["accepted"] = {"logical_fingerprint": fingerprint}
    return {"status": "PASS", "input_population_rows": rows, "complete_value_verification": "PASS",
        "outcomes": outcomes, "expected_logical_fingerprints": {module: fingerprint
            for module in ("intake", "intake_accepted", "recon", "sentinel")}}


def test_verified_standalone_population_supplies_summary_field_required_by_full_gate(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(population()), encoding="utf-8")
    measured = assert_population(path, 1000000, "STANDALONE_CLIENT")
    summary = {"status": "PASS", "parity_only": False, "cleanup_finished": True,
        "protected_main_inventory_unchanged": True}
    specification = next(item for group in load_manifest()["groups"] for item in group["scenarios"]
        if item["id"] == "spark-standalone-million-three-modules")
    envelope = {"documents": {"result": summary, "million": measured}}
    with pytest.raises(EvidenceError, match="SPARK_RUNTIME_OR_CLEANUP_INCOMPLETE"):
        validate_content(specification, envelope)
    summary["deployment_mode"] = cycle.observed_population_mode(measured)
    validate_content(specification, envelope)


@pytest.mark.parametrize("damage", ["missing_module", "extra_module", "missing_runtime", "missing_mode",
    "mixed_mode", "ambiguous_mode", "non_runtime", "non_outcome", "non_population"])
def test_missing_mixed_or_ambiguous_module_runtime_cannot_publish_observed_mode(damage):
    measured = copy.deepcopy(population())
    if damage == "missing_module": measured["outcomes"].pop("sentinel")
    elif damage == "extra_module": measured["outcomes"]["unknown"] = measured["outcomes"]["intake"]
    elif damage == "missing_runtime": measured["outcomes"]["recon"].pop("runtime")
    elif damage == "missing_mode": measured["outcomes"]["recon"]["runtime"].pop("deployment_mode")
    elif damage == "mixed_mode": measured["outcomes"]["recon"]["runtime"]["deployment_mode"] = "LOCAL"
    elif damage == "ambiguous_mode": measured["outcomes"]["recon"]["runtime"]["deployment_mode"] = ["STANDALONE_CLIENT", "LOCAL"]
    elif damage == "non_runtime": measured["outcomes"]["recon"]["runtime"] = "STANDALONE_CLIENT"
    elif damage == "non_outcome": measured["outcomes"]["recon"] = "STANDALONE_CLIENT"
    else: measured = None
    with pytest.raises(RuntimeError, match="SPARK_DEPLOYMENT_MODE_NOT_OBSERVED"):
        cycle.observed_population_mode(measured)


@pytest.mark.parametrize("damage", ["truncated", "unverified_values", "changed_fingerprint", "single_executor"])
def test_summary_mode_cannot_replace_existing_full_population_and_executor_oracles(tmp_path, damage):
    measured = population()
    if damage == "truncated": measured["input_population_rows"] = 999999
    elif damage == "unverified_values": measured["complete_value_verification"] = "SAMPLE"
    elif damage == "changed_fingerprint": measured["outcomes"]["recon"]["results"]["logical_fingerprint"] = {}
    else: measured["outcomes"]["recon"]["runtime"]["executor_memory_status_entries"] = 2
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(measured), encoding="utf-8")
    with pytest.raises(RuntimeError):
        assert_population(path, 1000000, "STANDALONE_CLIENT")
