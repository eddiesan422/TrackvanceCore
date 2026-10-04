"""Validate isolated scope and interval-censored profiling measurements."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

spec = importlib.util.spec_from_file_location("acquisition_timing_cycle", Path(__file__).with_name("acquisition_timing_cycle.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.fixture
def timing_inputs(tmp_path):
    fixture_root = tmp_path / "volume-fixtures"
    fixture_root.mkdir()
    path = fixture_root / "volume-100mib-1000000-varied.csv"
    path.write_text("id\n1\n")
    fixture = {"target_mib": 100, "rows": 1_000_000, "mode": "varied", "canonical_rows_sha256": "synthetic",
        "formats": {"CSV": {"path": str(path), "actual_bytes": path.stat().st_size}}}
    (fixture_root / "volume-100mib-1000000-varied.json").write_text(json.dumps(fixture))
    context = {"project": "trackvance-v070-test-core-0123456789ab"}
    baseline = {"project": context["project"], "rows": 1_000_000, "tiers": [{"target_mib": 100, "status": "PASS",
        "formats": {"CSV": {"version_id": str(uuid4()), "canonical_rows_sha256": "synthetic"}}}]}
    return tmp_path, context, baseline


@pytest.mark.parametrize("change", ["project", "population", "not_pass", "invalid_version"])
def test_baseline_scope_rejected_before_external_actions(timing_inputs, change):
    directory, context, baseline = timing_inputs
    if change == "project":
        baseline["project"] = "trackvance-certification"
    elif change == "population":
        baseline["rows"] = 10
    elif change == "not_pass":
        baseline["tiers"][0]["status"] = "RUNNING"
    else:
        baseline["tiers"][0]["formats"]["CSV"]["version_id"] = "not-a-version"
    with pytest.raises(ValueError):
        runner.planned_inputs(directory, context, baseline, [100])


@pytest.mark.parametrize("sizes", [[100, 100], [2048]])
def test_only_declared_nonduplicate_mandatory_tiers(timing_inputs, sizes):
    with pytest.raises(ValueError, match="tiers"):
        runner.planned_inputs(*timing_inputs, sizes)


def test_fixture_path_cannot_escape_its_private_directory(timing_inputs):
    directory, context, baseline = timing_inputs
    manifest = directory / "volume-fixtures/volume-100mib-1000000-varied.json"
    fixture = json.loads(manifest.read_text())
    fixture["formats"]["CSV"]["path"] = str(directory.parent / "volume-100mib-1000000-varied.csv")
    manifest.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="carpeta privada"):
        runner.planned_inputs(directory, context, baseline, [100])


def test_prepare_only_performs_no_docker_api_or_database_actions(timing_inputs, monkeypatch):
    directory, context, baseline = timing_inputs
    baseline_path = directory / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))
    monkeypatch.setattr(runner.certification, "load_context", lambda *_: (directory, context))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Preparation may not reach Docker/API/database")

    monkeypatch.setattr(runner.certification, "assert_main_unchanged", forbidden)
    monkeypatch.setattr(runner.certification, "compose", forbidden)
    monkeypatch.setattr(runner.volume, "VolumeApi", forbidden)
    report = runner.run(SimpleNamespace(context=directory, baseline=baseline_path, sizes=[100], prepare_only=True, with_chain=True))
    assert report["status"] == "PREPARED_ONLY" and report["planned_tiers"][0]["target_mib"] == 100
    assert report["tiers"] == []


def observation(start, end, stage):
    return {"request_started_seconds": start, "response_finished_seconds": end, "stage": stage}


def test_profile_transition_bounds_include_poll_gap_and_http_latency():
    rows = [observation(3, 3.1, "QUEUED"), observation(3.5, 3.6, "READING"),
        observation(4, 4.1, "MATERIALIZING"), observation(4.5, 4.6, "PROFILING"),
        observation(14.5, 14.6, "PROFILING"), observation(15, 15.1, "PUBLISHING"),
        observation(16, 16.1, "COMPLETED")]
    stages = runner.stage_windows(rows)
    assert [item["stage"] for item in stages] == ["QUEUED", "READING_AND_MATERIALIZING", "PROFILING", "PUBLISHING"]
    profile = stages[2]
    assert profile["entry_transition_bounds_seconds"] == [4, 4.6]
    assert profile["exit_transition_bounds_seconds"] == [14.5, 15.1]
    assert profile["duration_lower_bound_seconds"] == 9.9
    assert profile["duration_upper_bound_seconds"] == 11.1
    assert profile["duration_is_exact"] is False
    assert profile["stable_observed_window_seconds"] == [4.6, 14.5]


def test_unobserved_publishing_is_never_fabricated_as_zero_duration():
    stages = runner.stage_windows([observation(1, 1.1, "PROFILING"), observation(10, 10.1, "COMPLETED")])
    assert [item["stage"] for item in stages] == ["PROFILING"]


def test_phase_resources_require_entire_probe_inside_stable_observed_window():
    def values(start, end, usage):
        return {"metric_read_started_at": start, "metric_read_finished_at": end,
            "container_id": "same", "cpu": {"usage_usec": usage}, "memory.current": 50}
    samples = [{"at": 1, "disk_free_bytes": 1000, "services": {"worker": values(3, 3.2, 1_000_000)}},
        {"at": 4, "disk_free_bytes": 900, "services": {"worker": values(4, 4.2, 2_000_000)}},
        {"at": 5, "disk_free_bytes": 800, "services": {"worker": values(5, 5.2, 3_000_000)}}]
    report = runner.resources_in_stable_window(samples, 4, 5.1)
    assert report["samples"] == 1
    assert report["services"]["worker"]["cpu_seconds"] is None
    assert report["services"]["worker"]["cpu_incomplete_reason"] == "INSUFFICIENT_SAMPLES_OR_COUNTER_RESET"


def test_counter_reset_does_not_become_negative_or_invented_phase_cpu():
    report = runner.resource_summary([{"at": 1, "disk_free_bytes": 100, "services": {"worker": {
        "container_id": "old", "cpu": {"usage_usec": 10_000_000}}}}, {"at": 2, "disk_free_bytes": 100,
        "services": {"worker": {"container_id": "new", "cpu": {"usage_usec": 1_000_000}}}}])
    assert report["services"]["worker"]["cpu_seconds"] is None
    assert report["lifetime_memory_peak_used"] is False


def test_failed_acquisition_preserves_observations_in_attempt_journal():
    class Api:
        def get(self, _path):
            return {"stage": "FAILED", "status": "FAILED", "processed_rows": 12,
                "processed_bytes": 24, "error_code": "SOURCE_SIZE_LIMIT"}

    journal = {}
    run = {"id": str(uuid4()), "stage": "QUEUED", "status": "QUEUED",
        "processed_rows": 0, "processed_bytes": 0}
    with pytest.raises(RuntimeError, match="SOURCE_SIZE_LIMIT"):
        runner.observe_acquisition(Api(), run, runner.time.monotonic(), SimpleNamespace(check=lambda: None),
            {"request_started_seconds": 0, "response_finished_seconds": 0.1}, journal=journal)
    assert [row["stage"] for row in journal["observations"]] == ["QUEUED", "FAILED"]
    assert journal["observations"][-1]["processed_rows"] == 12
