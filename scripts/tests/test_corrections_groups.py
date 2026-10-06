"""Independent XLSX groups retain full populations and immutable partial evidence."""
from __future__ import annotations

import hashlib
import json
import subprocess
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import corrections_cycle as cycle
import corrections_runtime as runtime
import pytest
from ci.common import EvidenceError, load_manifest
from ci.evidence import wrap_group

SHA = "a" * 40
CI = {"run_id": "123", "run_attempt": "4", "job_id": "suite-corrections-acquisition"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def evidence(tmp_path, group="corrections-acquisition"):
    return runtime.ScenarioEvidence(tmp_path, group, SHA, ci=CI)


def test_source_commit_uses_pr_head_override_and_rejects_other_checkout():
    command = lambda _args: SHA + "\n"
    assert runtime.source_identity(command, {"CI_SOURCE_SHA": SHA, "GITHUB_SHA": "b" * 40}) == SHA
    assert runtime.source_identity(command, {"GITHUB_SHA": SHA}) == SHA
    with pytest.raises(ValueError, match="requested source"):
        runtime.source_identity(command, {"CI_SOURCE_SHA": "b" * 40, "GITHUB_SHA": SHA})


@pytest.mark.parametrize("interruption", ["TIMEOUT", "ALARM"])
def test_controlled_native_child_gets_cleanup_grace_and_own_group_kill_preserving_cause(tmp_path, monkeypatch, interruption):
    events = []
    original = subprocess.TimeoutExpired("native", 1800) if interruption == "TIMEOUT" else runtime.ScenarioDeadline("XLSX_PHASE_DEADLINE")
    class Child:
        pid = 45678
        def wait(self, *, timeout):
            events.append(("wait", timeout))
            if timeout == 1800:
                raise original
            if timeout == 30:
                raise subprocess.TimeoutExpired("cleanup", 30)
            return -9
    def spawn(_arguments, **kwargs):
        assert kwargs["start_new_session"] is True
        assert kwargs["cwd"] == Path(runtime.__file__).resolve().parents[2]
        return Child()
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="posix",
        killpg=lambda pid, sig: events.append(("killpg", pid, sig))))
    monkeypatch.setattr(runtime.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(runtime.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    with pytest.raises(type(original)) as caught:
        runtime.private_command(["native-child"], tmp_path, "native", seconds=1800)
    assert caught.value is original
    assert events == [("wait", 1800), ("killpg", Child.pid, runtime.signal.SIGINT),
        ("wait", 30), ("killpg", Child.pid, runtime.signal.SIGKILL), ("wait", 10)]


def test_partial_evidence_is_written_before_work_and_attaches_full_result(tmp_path):
    ledger = evidence(tmp_path)
    result = {"rows": 100000, "canonical_rows_sha256": "b" * 64, "physical_numbering": {"first": 5}}
    def callback():
        progress = list(ledger.directory.glob("progress-*.json"))
        assert len(progress) == 1
        assert read(progress[0])["status"] == "RUNNING"
        assert read(progress[0])["kind"] == "XLSX_PROGRESS"
        assert read(tmp_path / "corrections-evidence.json")["status"] == "RUNNING"
        return result
    assert ledger.scenario("acquisition-100000-inline", 100000, "inline", callback) == result
    group = read(tmp_path / "corrections-evidence.json")
    receipt = read(tmp_path / group["scenario_results"][0]["path"])
    assert receipt["source_sha"] == SHA and receipt["ci"] == CI
    assert receipt["result"] == result and receipt["kind"] == "XLSX_SCENARIO"
    assert receipt["rows"] == 100000 and receipt["status"] == "PASS"
    attachment = ledger.directory / receipt["evidence"][0]["path"]
    assert read(attachment) == result
    assert hashlib.sha256(attachment.read_bytes()).hexdigest() == receipt["evidence"][0]["sha256"]
    with pytest.raises(ValueError, match="Every mandatory"):
        ledger.finish()
    assert read(tmp_path / "corrections-evidence.json")["status"] == "FAIL"


def test_failure_preserves_prior_pass_and_never_publishes_exception_payload_or_resumes(tmp_path):
    ledger = evidence(tmp_path)
    ledger.scenario("acquisition-100000-inline", 100000, "inline", lambda: {"verified": True})
    secret = "synthetic-cookie-and-query-body"
    def failure():
        raise AssertionError(secret)
    with pytest.raises(AssertionError):
        ledger.scenario("acquisition-100000-shared", 100000, "shared", failure)
    document = read(tmp_path / "corrections-evidence.json")
    assert document["status"] == "FAIL" and len(document["scenario_results"]) == 2
    statuses = [read(tmp_path / row["path"])["status"] for row in document["scenario_results"]]
    assert statuses == ["PASS", "FAIL"]
    assert all(secret not in path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))
    with pytest.raises(ValueError, match="cannot resume"):
        ledger.scenario("acquisition-100001-inline", 100001, "inline", dict)


def test_prerequisites_never_count_as_required_and_duplicate_execution_is_rejected(tmp_path):
    ledger = evidence(tmp_path, "corrections-dispatch")
    ledger.scenario("prerequisite-acquisition-1000000-inline", 1000000, "inline", dict, prerequisite=True)
    group = read(tmp_path / "corrections-evidence.json")
    assert group["scenario_results"] == [] and len(group["prerequisite_results"]) == 1
    assert read(tmp_path / group["prerequisite_results"][0]["path"])["kind"] == "XLSX_PREREQUISITE"
    with pytest.raises(ValueError, match="duplicate"):
        ledger.scenario("prerequisite-acquisition-1000000-inline", 1000000, "inline", dict, prerequisite=True)
    with pytest.raises(ValueError, match="Unexpected"):
        ledger.scenario("chain-999999-inline", 999999, "inline", dict)


@pytest.mark.parametrize("group", list(runtime.GROUP_ALIASES))
def test_ci_cli_short_groups_reuse_images_without_flags_or_smaller_population(group):
    args = cycle.parse_arguments(["--reuse-images", "--group", group])
    assert args.group == "corrections-" + group and args.reuse_images
    assert args.rows == [100000, 100001, 400000, 1000000]
    assert not any((args.with_browser, args.with_dispatch, args.with_recovery, args.with_native_recovery))
    with pytest.raises(SystemExit):
        cycle.parse_arguments(["--group", group, "--rows", "100000"])


def test_image_reuse_pins_exact_checkout_ids_and_changes_only_image_fields(tmp_path, monkeypatch):
    import ci_images
    refs = {"backend": "sha256:" + "b" * 64, "web": "sha256:" + "c" * 64}
    monkeypatch.setattr(ci_images, "verified_images", lambda: refs)
    monkeypatch.setattr(cycle, "private_command", lambda *_a, **_k: pytest.fail("Reusable images must never build"))
    def command(args):
        assert args[:3] == ["docker", "image", "inspect"]
        return json.dumps([{"Id": args[3], "Config": {"Labels": {"org.opencontainers.image.revision": SHA}}}])
    monkeypatch.setattr(cycle.certification, "command", command)
    original = {"services": {service: {"image": "old", "environment": {"UNCHANGED": "literal$VALUE"},
        "volumes": ["private:/owned"], "mem_limit": "original"} for service in cycle.certification.SERVICES}}
    runtime.atomic_json(tmp_path / "compose.json", original)
    context = {"project": "own-uuid", "image": "old", "port": 32076}
    images = cycle.prepare_images(tmp_path, context, Namespace(reuse_images=True, backend_image=None), SHA)
    result = read(tmp_path / "compose.json")
    for service, value in original["services"].items():
        expected = value.copy()
        if service != "postgres":
            expected["image"] = refs["web" if service == "web" else "backend"]
        assert result["services"][service] == expected
    assert context["image"] == images["backend"]["id"] == refs["backend"]
    monkeypatch.setattr(ci_images, "verified_images", lambda: None)
    with pytest.raises(ValueError, match="verified current-SHA"):
        cycle.prepare_images(tmp_path, context, Namespace(reuse_images=True, backend_image=None), SHA)


@pytest.mark.parametrize("identity,revision", [("sha256:" + "z" * 64, SHA), ("sha256:" + "b" * 64, "d" * 40)])
def test_image_reuse_rejects_invalid_digest_or_other_sha(identity, revision):
    command = lambda _args: json.dumps([{"Id": identity, "Config": {"Labels": {"org.opencontainers.image.revision": revision}}}])
    with pytest.raises(ValueError, match="exact checkout"):
        runtime.immutable_image_pair("backend", "web", SHA, command)


def test_nested_phase_deadline_cannot_extend_parent_and_restores_remaining_time(monkeypatch):
    clock, timer, handler, calls = [0.0], [0.0, 0.0], [None], []
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime.signal, "SIGALRM", 14, raising=False)
    monkeypatch.setattr(runtime.signal, "ITIMER_REAL", 0, raising=False)
    def set_timer(_which, seconds, interval=0):
        timer[:] = [seconds, interval]
        calls.append(seconds)
    def set_handler(_which, callback):
        previous, handler[0] = handler[0], callback
        return previous
    monkeypatch.setattr(runtime.signal, "getitimer", lambda _which: tuple(timer), raising=False)
    monkeypatch.setattr(runtime.signal, "setitimer", set_timer, raising=False)
    monkeypatch.setattr(runtime.signal, "signal", set_handler)
    with runtime.deadline(100):
        clock[0] = 20
        timer[0] = 80
        with runtime.deadline(200):
            assert calls[-1] == 80
            clock[0] = 30
        assert calls[-1] == 70
    assert timer[0] == 0


@pytest.mark.parametrize("group", [group for group in runtime.GROUP_SCENARIOS if group != "corrections-functional"])
def test_independent_groups_execute_every_mandatory_case_without_legacy_opt_in_flags(tmp_path, monkeypatch, group):
    calls = []
    monkeypatch.setattr(cycle.certification, "assert_main_unchanged", lambda _context: None)
    monkeypatch.setattr(cycle.certification, "command", lambda _args: json.dumps({"MemTotal": 8 * cycle.volume.GIB}))
    monkeypatch.setattr(cycle.shutil, "disk_usage", lambda _root: SimpleNamespace(free=30 * cycle.volume.GIB))
    class Api:
        def __init__(self, *_args):
            self.csrf = None
        def post(self, *_args, **_kwargs):
            return {"csrf_token": "not-published"}
        def get(self, path):
            if path.endswith("system/engines"):
                return {"limits": {"acquisition": {"xlsx_max_rows": 1000000}}}
            return {"items": [{"id": "notice", "read_at": None}]}
    monkeypatch.setattr(cycle.volume, "VolumeApi", Api)
    monkeypatch.setattr(cycle.volume, "destination_fixture", lambda *_a: ({"id": "destination"}, {}, "schema", "reader"))
    def acquire(_api, _directory, _context, rows, variant):
        calls.append(("acquire", rows, variant))
        return {"fixture": {"rows": rows, "strings": variant}, "acquisition": {"output_version_id": variant}, "column_types": {}}
    def chain(*args):
        calls.append(("chain", args[3]["fixture"]["rows"], "inline"))
        return {"delivery": {"configuration_id": "configuration"}, "unread": {"notification_id": "notice"}}
    def browser(_a, _d, _c, rows, *_args):
        calls.append(("browser", rows, "shared"))
        return {"rows": rows}
    monkeypatch.setattr(cycle, "acquire_group_fixture", acquire)
    monkeypatch.setattr(cycle, "chain_group_fixture", chain)
    monkeypatch.setattr(cycle, "browser_group_fixture", browser)
    monkeypatch.setattr(cycle, "generate", lambda _d, rows, **kwargs: {"rows": rows, "strings": kwargs["strings"]})
    def mutation(name, field, payload):
        def callback(*args):
            calls.append((name, 1000000, "shared"))
            args[-1][field] = payload.copy()
        return callback
    monkeypatch.setattr(cycle, "certify_row_limit_failure", mutation("limit", "row_limit_failure", {"status": "PASS", "rows_in_fixture": 1000001}))
    monkeypatch.setattr(cycle, "certify_dispatch", mutation("dispatch", "dispatch", {"status": "PASS"}))
    monkeypatch.setattr(cycle, "certify_unread_restart", mutation("restart", "unread_restart", {"status": "PASS"}))
    monkeypatch.setattr(cycle, "cancel_during_reading", mutation("cancel", "cancel", {"status": "CANCELLED", "versions": 0}))
    monkeypatch.setattr(cycle, "recover_crashed_acquisition", mutation("crash", "recovery", {"attempts": 2, "versions": 1}))
    def private_command(args, *_a, **_k):
        calls.append(("native", 1000000, "both"))
        native = Path(args[args.index("--evidence-dir") + 1])
        native.mkdir()
        runtime.atomic_json(native / "result.json", {"status": "PASS"})
    monkeypatch.setattr(cycle, "private_command", private_command)
    ledger = runtime.ScenarioEvidence(tmp_path, group, SHA, ci=CI)
    cycle.certify_group(tmp_path, {"port": 32076}, Namespace(group=group), {}, ledger)
    report = read(tmp_path / "corrections-evidence.json")
    assert report["status"] == "PASS" and report["main_inventory"] == "UNCHANGED"
    assert report["xlsx_test_limit_overrides"] is False
    assert {row["scenario_id"] for row in report["scenario_results"]} == set(runtime.GROUP_SCENARIOS[group])
    assert sum(len(ids) for group, ids in runtime.GROUP_SCENARIOS.items() if group != "corrections-functional") == 17
    manifest = next(item for item in load_manifest()["groups"] if item["id"] == group)
    specifications = {item["id"]: item for item in manifest["scenarios"]}
    assert set(specifications) == set(runtime.GROUP_SCENARIOS[group])
    for entry in report["scenario_results"]:
        receipt = read(tmp_path / entry["path"])
        expected = specifications[entry["scenario_id"]]
        assert receipt["rows"] == expected["rows"] and receipt["variant"] == expected.get("variant")
    if group == "corrections-acquisition":
        assert {(rows, variant) for name, rows, variant in calls if name == "acquire"} == {
            (rows, variant) for rows in (100000, 100001, 400000, 1000000) for variant in ("inline", "shared")}
        assert calls[-1][0] == "limit"
        assert cycle.services_for(group) == ("postgres", "api", "web", "acquisition-worker", "events-notifications")
    elif group == "corrections-browser":
        assert calls == [("browser", 400000, "shared"), ("browser", 1000000, "shared")]
    elif group == "corrections-dispatch":
        assert [name for name, *_ in calls] == ["acquire", "chain", "acquire", "dispatch", "restart"]
    else:
        assert [name for name, *_ in calls] == ["acquire", "chain", "cancel", "crash", "limit", "restart", "native"]


def test_full_integrity_rejects_changed_values_even_when_row_count_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(cycle.volume, "materialized_hash", lambda *_args: {"rows": 1000000, "canonical_rows_sha256": "a" * 64})
    monkeypatch.setattr(cycle.certification, "compose", lambda *_args: pytest.fail("Mismatch must fail before secondary verification"))
    with pytest.raises(AssertionError, match="oráculo completo"):
        cycle.record_integrity(tmp_path, {}, "version", {"rows": 1000000, "canonical_rows_sha256": "b" * 64})


def test_group_receipts_integrate_with_real_wrapper_and_reject_changed_result_attachment(tmp_path):
    """These synthetic aggregates test schemas/hashes; they certify no population."""
    ledger = evidence(tmp_path)
    digest = "b" * 64
    def integrity(rows):
        return {"rows": rows, "canonical_rows_sha256": digest, "physical_numbering": {
            "rows": rows, "first": 5, "last": rows + 4, "numbering_mismatches": 0}}
    for rows in (100000, 100001, 400000, 1000000):
        for variant in ("inline", "shared"):
            fixture = {"rows": rows, "canonical_rows_sha256": digest, "expected_cardinality": {"record_id": rows},
                "expected_observed_nulls": 12}
            result = {"status": "PASS", "rows": rows, "strings": variant, "fixture": fixture,
                "integrity": integrity(rows), "acquisition": {"output_version_id": "synthetic-test-version"},
                "profile": {"row_count": rows, "columns": [{"name": "record_id", "distinct_count": rows},
                    {"name": "observed", "null_count": 12}, {"name": "late_type", "logical_type": "STRING"}]}}
            ledger.scenario(f"acquisition-{rows}-{variant}", rows, variant, lambda result=result: result)
    limit = {"status": "PASS", "rows_in_fixture": 1000001, "configured_maximum": 1000000,
        "attempt_status": "FAILED", "no_partial_version": True, "diagnostic": {"code": "ACQUISITION_ROW_LIMIT"},
        "previous_version_ids": ["synthetic-test-version"], "new_diagnostic_kept_for_native_backup": True,
        "retained_full_integrity": integrity(1000000)}
    ledger.scenario("row-limit-preserves-version-1000001", 1000001, "inline", lambda: limit)
    ledger.finish(main_inventory="UNCHANGED", xlsx_test_limit_overrides=False)
    summary = tmp_path / "corrections-evidence.json"
    document = read(summary)
    args = {"source_sha": SHA, "ci": CI, "started_at": document["started_at"],
        "completed_at": document["completed_at"], "duration_seconds": document["duration_seconds"]}
    snapshots = wrap_group("corrections-acquisition", {"result": summary}, tmp_path / "normalized", **args)
    assert len(snapshots) == 9
    assert {read(path)["scenario_id"] for path in snapshots} == set(runtime.GROUP_SCENARIOS["corrections-acquisition"])
    receipt_path = tmp_path / document["scenario_results"][0]["path"]
    receipt = read(receipt_path)
    (receipt_path.parent / receipt["evidence"][0]["path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(EvidenceError, match="XLSX_ATTACHMENT_HASH_MISMATCH"):
        wrap_group("corrections-acquisition", {"result": summary}, tmp_path / "tampered", **args)


def test_native_failure_is_copied_as_closed_diagnostic_before_original_exception_escapes(tmp_path, monkeypatch):
    original = RuntimeError("synthetic-private-cookie-query-and-password")
    def child(arguments, directory, _name, **kwargs):
        assert directory == tmp_path and kwargs == {"seconds": 1800}
        native = Path(arguments[arguments.index("--evidence-dir") + 1])
        native.mkdir()
        runtime.atomic_json(native / "result.json", {"status": "FAIL", "failed_stage": "fresh_restore",
            "error_type": "ValueError", "main_inventory": "UNCHANGED", "password": "synthetic-private-secret",
            "source_project": "synthetic-private-project", "message": str(original)})
        raise original
    monkeypatch.setattr(cycle, "private_command", child)
    with pytest.raises(RuntimeError) as caught:
        cycle.native_recovery_with_diagnostic(tmp_path)
    assert caught.value is original
    result = read(tmp_path / "native-recovery-diagnostic.json")
    assert result == {"schema_version": 1, "kind": "RECOVERY_PARTIAL_SUMMARY", "profile": "native",
        "summary_present": True, "result": {"status": "FAIL", "failed_stage": "fresh_restore",
            "error_type": "ValueError", "main_inventory": "UNCHANGED"}}
    assert "synthetic-private" not in (tmp_path / "native-recovery-diagnostic.json").read_text()


def test_native_diagnostic_write_failure_cannot_mask_original_child_error(tmp_path, monkeypatch):
    original = TimeoutError("XLSX_PHASE_DEADLINE")
    monkeypatch.setattr(cycle, "private_command", lambda *_a, **_k: (_ for _ in ()).throw(original))
    monkeypatch.setattr(cycle, "atomic_json", lambda *_a, **_k: (_ for _ in ()).throw(OSError("diagnostic unavailable")))
    with pytest.raises(TimeoutError) as caught:
        cycle.native_recovery_with_diagnostic(tmp_path)
    assert caught.value is original


def test_native_exit_success_without_child_result_cannot_be_certified(tmp_path, monkeypatch):
    monkeypatch.setattr(cycle, "private_command", lambda *_a, **_k: None)
    with pytest.raises(ValueError, match="RESULT_UNAVAILABLE"):
        cycle.native_recovery_with_diagnostic(tmp_path)
    assert read(tmp_path / "native-recovery-diagnostic.json")["summary_present"] is False
