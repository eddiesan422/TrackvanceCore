"""Immutable prior evidence and late resource ownership guard the continuation."""
import importlib.util
import json
import time
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("reports_deep_continuation_085", Path(__file__).with_name("reports_deep_continuation_085.py"))
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
SHA = "a" * 40
PROJECT = "trackvance-v080-test-catalog-reports-0123456789ab"
LATE = "trackvance-v080-test-restore-abcdef012345"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def parent_receipts(tmp_path):
    directory = tmp_path / ".codex-local/v080" / PROJECT
    save(directory / "isolation.json", {"project": PROJECT, "main_project": "trackvance-certification"})
    save(directory / "result.json", {"source_sha": SHA, "source_tree_dirty": False, "status": "FAIL"})
    for rows in runner.ROWS:
        save(directory / f"reports-{rows}.json", {"status": "FAIL" if rows == 1000000 else "PASS", "rows_per_source": rows})
    return directory


def test_previous_fail_is_preserved_and_tier_bytes_cannot_be_promoted_or_changed(tmp_path):
    directory = parent_receipts(tmp_path)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    proof = runner.original_tiers(directory, SHA)
    assert proof["aggregate_original_status"] == "FAIL" and proof["new_sha_approval_inherited"] is False
    assert [item["rows_per_source"] for item in proof["tiers"]] == [120, 400000, 1000000]
    assert [item["original_status"] for item in proof["tiers"]] == ["PASS", "PASS", "FAIL"]
    runner.verify_originals(proof)
    assert before == {path.name: path.read_bytes() for path in directory.iterdir()}
    save(directory / "reports-400000.json", {"status": "PASS", "rows_per_source": 400000, "changed": True})
    with pytest.raises(ValueError, match="ORIGINAL_EVIDENCE_CHANGED"):
        runner.verify_originals(proof)


@pytest.mark.parametrize("change", ["sha", "dirty", "pending", "tier-fail", "wrong-rows", "habitual"])
def test_invalid_prior_provenance_cannot_start_the_continuation(tmp_path, change):
    directory = parent_receipts(tmp_path)
    if change in {"sha", "dirty", "pending"}:
        value = runner.read(directory / "result.json")
        value[{"sha": "source_sha", "dirty": "source_tree_dirty", "pending": "status"}[change]] = {
            "sha": "b" * 40, "dirty": True, "pending": "RUNNING"}[change]
        save(directory / "result.json", value)
    elif change == "habitual":
        save(directory / "isolation.json", {"project": "trackvance-certification", "main_project": "trackvance-certification"})
    else:
        rows = 400000 if change == "tier-fail" else 1000000
        save(directory / f"reports-{rows}.json", {"status": "FAIL" if change == "tier-fail" else "PASS",
                                                 "rows_per_source": 999999 if change == "wrong-rows" else rows})
    with pytest.raises(ValueError):
        runner.original_tiers(directory, SHA)


@pytest.mark.parametrize("memory,cpus,other_memory,other_quota,passes", [
    (12, 4, 0, 0, True), (11, 4, 0, 0, False), (12, 3, 0, 0, False),
    (24, 16, 10, 400000, True), (18, 16, 10, 400000, False),
    (24, 4, 10, 400000, False), (24, 16, 10, 0, False),
    (13, 16, 1, 100000, True), (12, 16, 1, 100000, False)])
def test_capacity_includes_other_uuid_workloads_and_fails_before_creation(monkeypatch, memory, cpus, other_memory, other_quota, passes):
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if args[1] == "info":
            return json.dumps({"MemTotal": memory * runner.GIB, "NCPU": cpus})
        if args[1] == "ps":
            return "existing" if other_memory else ""
        assert args == ["docker", "inspect", "existing"]
        return json.dumps([{"HostConfig": {"Memory": other_memory * runner.GIB, "CpuQuota": other_quota, "CpuPeriod": 100000}}])

    monkeypatch.setattr(runner.guard, "command", command)
    monkeypatch.setattr(runner.shutil, "disk_usage", lambda path: type("Disk", (), {"free": 30 * runner.GIB})())
    if passes:
        receipt = runner.capacity()
        assert receipt["runner_memory_budget_bytes"] == 10 * runner.GIB
        assert receipt["active_declared_memory_bytes"] == other_memory * runner.GIB
        assert receipt["infrastructure_reservation_bytes"] == 2 * runner.GIB
    else:
        with pytest.raises(ValueError, match="PENDING_CAPACITY"):
            runner.capacity()
    assert all(args[1] in {"info", "ps", "inspect"} for args in calls)


def registries(directory, projects):
    save(directory / "owned-projects.json", projects)
    save(directory / "owned-builders.json", [])


def test_final_cleanup_reads_latest_restore_project_and_only_new_exact_ids(tmp_path, monkeypatch):
    registries(tmp_path, [PROJECT])
    before = {"containers": {"preexisting"}, "volumes": set(), "networks": set()}
    containers = {"preexisting": PROJECT, "last-restore": LATE, "foreign-new": "other-app"}
    removed = []
    # The last restore registers after the earlier cleanup checkpoint.
    registries(tmp_path, [PROJECT, LATE])
    monkeypatch.setattr(runner.owned_cleanup, "snapshot", lambda: {**before, "containers": set(containers)})

    def docker(*args):
        if args[0] == "inspect":
            identifier = args[1]
            return json.dumps([{"Id": identifier, "Config": {"Labels": {"com.docker.compose.project": containers[identifier]}},
                                "State": {"Running": False}}])
        assert args[0] == "rm" and args[1] == "last-restore"
        removed.append(args[1])
        del containers[args[1]]
        return ""

    monkeypatch.setattr(runner.owned_cleanup, "docker", docker)
    monkeypatch.setattr(runner.guard, "command", lambda args, **kwargs: "")
    monkeypatch.setattr(runner, "cleanup_registered_images", lambda *args, **kwargs: {"status": "PASS"})
    monkeypatch.setattr(runner.run_local, "cleanup_builder_images", lambda directory: {"status": "PASS"})
    result = runner.cleanup(tmp_path, "local-" + "c" * 32, before, set())
    assert result["status"] == "PASS" and removed == ["last-restore"]
    assert set(containers) == {"preexisting", "foreign-new"}
    assert result["project_registry"]["projects"] == sorted([PROJECT, LATE])


def test_cleanup_failure_still_attempts_registered_images_and_preserves_preexisting_builder(tmp_path, monkeypatch):
    registries(tmp_path, [PROJECT])
    builder = "tv-local-build-0123456789ab"
    save(tmp_path / "owned-builders.json", [builder])
    calls = []
    monkeypatch.setattr(runner.owned_cleanup, "cleanup", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("failure")))
    monkeypatch.setattr(runner.guard, "command", lambda args, **kwargs: calls.append(args) or "")
    monkeypatch.setattr(runner, "cleanup_registered_images", lambda *args, **kwargs: calls.append("images") or {"status": "PASS"})
    monkeypatch.setattr(runner.run_local, "cleanup_builder_images", lambda directory: calls.append("builder-images") or {"status": "PASS"})
    result = runner.cleanup(tmp_path, "local-" + "c" * 32, {}, {builder})
    assert result["status"] == "FAIL" and result["builders"]["status"] == "FAIL"
    assert calls == ["images", "builder-images"]


def test_uncertain_owned_image_is_preserved_and_cleanup_cannot_claim_pass(tmp_path, monkeypatch):
    registries(tmp_path, [PROJECT])
    monkeypatch.setattr(runner.owned_cleanup, "cleanup", lambda *args, **kwargs: {"status": "PASS"})
    monkeypatch.setattr(runner.guard, "command", lambda args, **kwargs: "")
    retained = {"image_id": "sha256:" + "d" * 64, "reason": "CONTAINER_CONSUMER"}
    monkeypatch.setattr(runner, "cleanup_registered_images", lambda *args, **kwargs: {"status": "PASS", "skipped": [retained]})
    monkeypatch.setattr(runner.run_local, "cleanup_builder_images", lambda directory: {"status": "PASS"})
    result = runner.cleanup(tmp_path, "local-" + "c" * 32, {}, set())
    assert result["status"] == "FAIL" and result["images"]["status"] == "FAIL"
    assert result["images"]["skipped"] == [retained]


def test_existing_http_prepare_builder_is_supported_without_adopting_preexisting_names(tmp_path, monkeypatch):
    images = {"backend": "sha256:" + "a" * 64, "web": "sha256:" + "b" * 64}
    monkeypatch.setattr(runner.observation.debug_images, "verified_images", lambda values: images)
    project = "trackvance-v080-test-reports-http-0123456789ab"
    proof = runner.observation.debug_images.prepare(tmp_path, {"project": project, "image": images["backend"]},
        environment={"TRACKVANCE_SOURCE_SHA": SHA, "TRACKVANCE_LOCAL_EXECUTION_ID": "local-" + "c" * 32})
    assert runner.BUILDER.fullmatch(proof["builder"])
    assert proof["project"] == project and proof["base_images"] == images
    assert not runner.BUILDER.fullmatch("default") and not runner.BUILDER.fullmatch("trackvance-certification")


def test_capacity_pending_receipt_is_explicit_and_no_ownership_mutation_is_started(tmp_path, monkeypatch, capsys):
    directory = parent_receipts(tmp_path / "parent")
    code_root = tmp_path / "code"
    manifest = tmp_path / "image-proof.json"
    save(manifest, {"status": "PASS"})
    monkeypatch.setattr(runner, "ROOT", code_root)
    monkeypatch.setattr(runner.downloads, "verify_code", lambda sha: {"backend": "sha256:" + "a" * 64})
    monkeypatch.setattr(runner, "source_provenance", lambda *args: {"approval_inheritance": "NOT_GRANTED"})
    monkeypatch.setattr(runner, "capacity", lambda: (_ for _ in ()).throw(ValueError("PENDING_CAPACITY_CONTINUATION_4_CPU_10_GIB")))
    monkeypatch.setattr(runner.owned_cleanup, "snapshot", lambda: pytest.fail("no owned resource boundary is started"))
    monkeypatch.setattr(runner.run_local, "protected_inventory", lambda: pytest.fail("no workload creation follows pending capacity"))
    # main writes child environment only in this process; isolate it from later tests.
    monkeypatch.setattr(runner.os, "environ", dict(runner.os.environ))
    assert runner.main(["--original-context", str(directory), "--original-source-sha", SHA,
        "--source-sha", "b" * 40, "--image-manifest", str(manifest), "--main-project", "trackvance-certification"]) == 1
    output = json.loads(capsys.readouterr().out)
    receipt = runner.read(Path(output["evidence"]))
    assert receipt["status"] == "PENDING_CAPACITY" and receipt["error_code"] == "PENDING_CAPACITY"
    assert [item["status"] for item in receipt["stages"]] == ["PENDING"] * 4
    assert receipt["full_catalog_approved"] is False and receipt["original_evidence_unchanged"] is True
    assert not (Path(output["evidence"]).parent / "owned-images.json").exists()


def test_mass_failure_preserves_failed_receipt_and_pending_later_scopes(tmp_path, monkeypatch):
    mass = tmp_path / "mass"
    save(mass / "reports-download-085.json", {"status": "FAIL", "active_phase": "RESOLVE_120"})
    monkeypatch.setattr(runner.downloads, "prepare", lambda *args: (mass, {"project": PROJECT}))
    monkeypatch.setattr(runner.guard, "compose_args", lambda *args: ["docker", "compose"])
    save(mass / "compose.json", {})
    monkeypatch.setattr(runner.downloads, "effective_cgroups", lambda *args: {"status": "PASS"})
    monkeypatch.setattr(runner.validators, "catalog_population", lambda *args: None)
    calls = []

    def execute(*args, **kwargs):
        calls.append(args[2])
        if args[2] == "api_1m_startup":
            return {}
        if args[2] == "api_1m_retry":
            assert "report_tier(directory,context,1000000,{})" in args[0][2]
            save(mass / "reports-1000000.json", {"status": "PASS"})
            return {}
        raise RuntimeError("child failed")

    monkeypatch.setattr(runner, "execute", execute)
    summary = {"source_sha": SHA, "original_evidence": {"project": LATE},
               "stages": [{"scope": name, "status": "PENDING"} for name in runner.STAGES]}
    with pytest.raises(RuntimeError, match="child failed"):
        runner.run_scopes(tmp_path, summary, {}, time.monotonic() + 6300, 32088, "trackvance-certification")
    assert calls == ["api_1m_startup", "api_1m_retry", "mass_download_opfs"]
    assert [item["status"] for item in summary["stages"]] == ["PASS", "FAIL", "PENDING", "PENDING"]
    assert summary["stages"][1]["receipt"]["sha256"] == runner.downloads.file_hash(mass / "reports-download-085.json")


def test_plan_does_not_inspect_docker_or_claim_full_catalog(tmp_path, monkeypatch, capsys):
    directory = parent_receipts(tmp_path)
    monkeypatch.setattr(runner.guard, "command", lambda *args, **kwargs: pytest.fail("plan must not call Docker/git"))
    assert runner.main(["--original-context", str(directory), "--original-source-sha", SHA,
        "--source-sha", "b" * 40, "--image-manifest", str(tmp_path / "future-images.json"),
        "--main-project", "trackvance-certification", "--plan"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["full_catalog_approved"] is False and value["automatic_approval_inheritance"] is False
    assert [item["status"] for item in value["stages"]] == ["PENDING"] * 4
    assert value["candidate_images_built"] is False


def test_serial_scopes_release_population_before_trace_and_reuse_all_recovery_gates(tmp_path, monkeypatch):
    mass, http = tmp_path / "mass", tmp_path / "http"
    save(mass / "compose.json", {})
    monkeypatch.setattr(runner.downloads, "prepare", lambda *args: (mass, {"project": PROJECT}))
    monkeypatch.setattr(runner.guard, "compose_args", lambda *args: ["docker", "compose"])
    monkeypatch.setattr(runner.downloads, "effective_cgroups", lambda *args: {"status": "PASS"})
    events = []
    monkeypatch.setattr(runner.validators, "catalog_population", lambda *args: events.append("population-gate"))
    monkeypatch.setattr(runner.validators, "catalog_download_receipt", lambda *args: events.append("download-gate"))
    monkeypatch.setattr(runner.validators, "ephemeral_http_observation", lambda *args: events.append("http-gate"))
    monkeypatch.setattr(runner, "stop_mass", lambda *args: events.append("stop-population") or {"status": "PASS"})

    def prepare_http(*args):
        assert events[-1] == "stop-population"
        return http, {}

    def gate(spec, value):
        assert spec["validator"] == "catalog-recovery" and value["source_sha"] == SHA
        assert value["host_download_085"]["requested_result_rows"] == runner.ROWS
        events.append("recovery-gate-" + spec["mode"])

    def execute(args, directory, stage, *extra, **kwargs):
        events.append(stage)
        if stage == "api_1m_retry":
            save(mass / "reports-1000000.json", {"status": "PASS"})
        elif stage == "mass_download_opfs":
            save(mass / "reports-download-085.json", {"status": "PASS", "main_unchanged": True, "requested_result_rows": runner.ROWS})
        elif stage == "http_17_cases":
            save(http / "reports-ephemeral-http.json", {"status": "PASS"})
        elif stage == "recovery_both":
            save(mass / "catalog-recovery-0123456789ab/result.json", {"status": "PASS", "mode": "both",
                 **{mode: {"status": "PASS"} for mode in ("native", "legacy", "legacy080")}})

    monkeypatch.setattr(runner.observation, "prepare", prepare_http)
    monkeypatch.setattr(runner.validators, "validate_content", gate)
    monkeypatch.setattr(runner, "execute", execute)
    summary = {"source_sha": SHA, "original_evidence": {"project": LATE},
               "stages": [{"scope": name, "status": "PENDING"} for name in runner.STAGES]}
    runner.run_scopes(tmp_path, summary, {}, time.monotonic() + 10800, 32088, "trackvance-certification")
    assert events == ["api_1m_startup", "api_1m_retry", "population-gate", "mass_download_opfs", "download-gate",
                      "stop-population", "http_17_cases", "http-gate", "recovery_both",
                      "recovery-gate-native", "recovery-gate-legacy", "recovery-gate-legacy080"]
    assert [item["status"] for item in summary["stages"]] == ["PASS"] * 4
