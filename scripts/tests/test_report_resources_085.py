"""The private mass profile preserves finite guards and API child headroom."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_resources_085 as profile


def topology():
    return {"services": {name: {"environment": {}, "labels": {}, "pids_limit": 512}
                         for name in profile.PROFILE}}


def test_private_profile_aggregate_and_concurrency_before_service_start(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACKVANCE_LOCAL_EXECUTION_ID", raising=False)
    (tmp_path / "compose.json").write_text(json.dumps(topology()))
    context = {"project": "trackvance-v080-test-download-123456789abc", "main_project": "trackvance-certification"}
    receipt = profile.configure(tmp_path, context, 1000000)
    assert receipt["memory_bytes"] == 8128 * 1024**2 < 8 * 1024**3
    assert receipt["cpus"] == 2 and receipt["concurrency"] == 1
    actual = json.loads((tmp_path / "compose.json").read_text())
    assert actual["services"]["api"]["mem_limit"] == 2048 * 1024**2
    assert actual["services"]["report-worker"]["environment"]["REPORT_CONCURRENCY"] == "1"
    assert (tmp_path / "report-resource-profile-085.json").is_file()


def test_local_cap_too_small_fails_before_replacing_owned_configuration(tmp_path, monkeypatch):
    original = json.dumps(topology())
    (tmp_path / "compose.json").write_text(original)
    monkeypatch.setenv("TRACKVANCE_LOCAL_EXECUTION_ID", "local-" + "a" * 32)
    monkeypatch.setenv("TRACKVANCE_LOCAL_MAX_MEMORY_BYTES", str(7 * 1024**3))
    monkeypatch.setenv("TRACKVANCE_LOCAL_MAX_CPUS", "2")
    monkeypatch.delenv("TRACKVANCE_LOCAL_PROJECT_REGISTRY", raising=False)
    monkeypatch.delenv("TRACKVANCE_LOCAL_GROUP", raising=False)
    with pytest.raises(ValueError, match="PENDING_CAPACITY"):
        profile.configure(tmp_path, {"project": "trackvance-v080-test-dl-123456789abc", "main_project": "usual"}, 120)
    assert (tmp_path / "compose.json").read_text() == original


@pytest.mark.parametrize("mutation", ["api_memory", "unbounded_cpu", "extra_service", "concurrency"])
def test_borrowed_context_cannot_claim_incomplete_or_weakened_profile(tmp_path, monkeypatch, mutation):
    monkeypatch.delenv("TRACKVANCE_LOCAL_EXECUTION_ID", raising=False)
    (tmp_path / "compose.json").write_text(json.dumps(topology()))
    profile.configure(tmp_path, {"project": "trackvance-v080-test-dl-123456789abc", "main_project": "usual"}, 120)
    data = copy.deepcopy(json.loads((tmp_path / "compose.json").read_text()))
    if mutation == "api_memory":
        data["services"]["api"]["mem_limit"] = 1024 * 1024**2
    elif mutation == "unbounded_cpu":
        data["services"]["api"]["cpus"] = float("inf")
    elif mutation == "extra_service":
        data["services"]["foreign"] = {"cpus": 1, "mem_limit": "1g"}
    else:
        data["services"]["api"]["environment"]["REPORT_CONCURRENCY"] = "2"
    with pytest.raises(ValueError):
        profile.validate(data)
