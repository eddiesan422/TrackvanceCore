"""Native trial proof checks with isolated synthetic evidence; no Docker calls."""
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
import selective_cleanup_recovery as trial


def transition():
    before = {"tables": {name: {} for name in trial.docker_state.CURRENT_STATE_TABLES}}
    before["tables"]["datasets"] = {"remove": "a" * 64, "retain": "b" * 64}
    before["tables"]["users"] = {"admin": "c" * 64}
    before["tables"]["audit_events"] = {"historical": "d" * 64}
    plan = {"plan_sha256": "e" * 64, "scope": {"delete": {name: [] for name in before["tables"]},
        "removed_counts": {name: int(name == "datasets") for name in before["tables"]}}}
    plan["scope"]["delete"]["datasets"] = ["remove"]
    after = deepcopy(before)
    after.update(database="POSTGRESQL", sql_inspection="READ_ONLY_VALIDATED", validated_relationships=3,
        audit_markers=[{"id": "new", "event_type": "OPERATIONAL_TEST_DATA_REMOVED", "plan_sha256": plan["plan_sha256"],
            "removed_counts": plan["scope"]["removed_counts"], "external_destinations_touched": False}])
    del after["tables"]["datasets"]["remove"]
    after["tables"]["audit_events"]["new"] = "f" * 64
    return before, after, plan


def test_sql_transition_keeps_exact_protected_rows_and_adds_bound_audit():
    before, after, plan = transition()
    trial.assert_transition(before, after, plan)


@pytest.mark.parametrize("mutation", ["changed_user", "missing_audit", "changed_history", "wrong_marker", "sqlite", "removed_extra"])
def test_sql_transition_rejects_partial_proofs_and_any_unplanned_change(mutation):
    before, after, plan = transition()
    if mutation == "changed_user":
        after["tables"]["users"]["admin"] = "modified"
    elif mutation == "missing_audit":
        del after["tables"]["audit_events"]["new"]
    elif mutation == "changed_history":
        after["tables"]["audit_events"]["historical"] = "modified"
    elif mutation == "wrong_marker":
        after["audit_markers"][0]["plan_sha256"] = "wrong"
    elif mutation == "sqlite":
        after["database"] = "SQLITE"
    else:
        del after["tables"]["datasets"]["retain"]
    with pytest.raises(ValueError):
        trial.assert_transition(before, after, plan)


def test_live_habitual_scope_is_rejected_before_any_command():
    def forbidden(*_args):
        pytest.fail("No Docker or database call may inspect or modify a habitual project in this trial.")
    with pytest.raises(ValueError, match="owned restored"):
        trial.trial(SimpleNamespace(preflight=forbidden), None, {"project": "trackvance-certification"}, {}, None)


def test_selected_file_proof_requires_exact_hashes_and_both_absence_sides():
    proof = {"status": "PASS", "originals_intact": 0, "originals_absent": 3, "originals_mismatched": 0,
        "quarantine_intact": 3, "quarantine_absent": 0, "quarantine_mismatched": 0}
    trial.assert_files(proof, 3, moved=True)
    proof["quarantine_intact"] = 2
    proof["quarantine_mismatched"] = 1
    with pytest.raises(ValueError, match="file hashes"):
        trial.assert_files(proof, 3, moved=True)


def test_real_stopped_restore_gate_precedes_every_plan_or_apply(monkeypatch, tmp_path):
    project = "trackvance-v080-test-restore085-012345abcdef"
    restore_project = "trackvance-v080-test-cleanuprestore085-123456abcdef"
    calls = []
    state = {"containers": [{"service": "api", "id": "owned-api", "image_id": "owned-image"},
        {"service": "postgres", "id": "owned-pg"}]}
    monkeypatch.setattr(trial.docker_state, "inventory", lambda _project: state)
    monkeypatch.setattr(trial.cleanup_operational, "require_quiescence", lambda _project: calls.append("quiescent") or state)
    monkeypatch.setattr(trial.cleanup_operational, "execute", lambda *_args, **_kwargs:
                        pytest.fail("An unverified restore must prevent both plan and apply helpers."))

    def backup(identity, path):
        assert identity == project
        path.mkdir()
        (path / "state.json").write_text(json.dumps({"tables": {}}))
        (path / "backup-manifest.json").write_text("{}")
        calls.append("consistent_backup")

    monkeypatch.setattr(trial.docker_state, "backup", backup)
    monkeypatch.setattr(trial.docker_state, "verify_backup", lambda _path: calls.append("verify_backup"))
    original_adapter = lambda identity, *_args: calls.append("stop_source")
    restore_adapter = lambda *_args: None
    monkeypatch.setattr(trial.docker_state, "compose", original_adapter)

    def restore(_backup, target, *, start, web_port):
        assert target == restore_project and start is False and trial.docker_state.compose is restore_adapter
        calls.append("real_restore")
        return {"status": "RUNNING_VERIFIED", "target_project": target}

    monkeypatch.setattr(trial.docker_state, "restore", restore)
    harness = SimpleNamespace(preflight=lambda *_args: None, run=lambda *_args, **_kwargs: json.dumps(
        {"project": project, "fixture": "SYNTHETIC_LOCAL_METADATA_NO_ENGINE_OR_REMOTE_IO"}),
        target_context=lambda *_args: (tmp_path, {"project": restore_project, "port": 32080}, {}),
        compose_adapter=lambda *_args: restore_adapter, cleanup=lambda *_args: calls.append("cleanup_own_restore"),
        assert_main=lambda *_args: calls.append("usual_unchanged"))
    with pytest.raises(ValueError, match="separate real stopped restore"):
        trial.trial(harness, tmp_path, {"project": project}, {}, tmp_path)
    assert calls == ["consistent_backup", "verify_backup", "stop_source", "quiescent", "real_restore",
        "cleanup_own_restore", "usual_unchanged"]
    assert trial.docker_state.compose is original_adapter


def test_bind_access_is_confined_by_private_outer_host_directory(tmp_path):
    evidence = tmp_path / "private-evidence"
    backup = evidence / "backup"
    parent = evidence / "quarantine"
    backup.mkdir(parents=True)
    parent.mkdir()
    (backup / "state.json").write_text("{}")
    trial.prepare_private_mounts(evidence, backup, parent)
    assert (backup / "state.json").read_text() == "{}"
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="fresh children"):
        trial.prepare_private_mounts(evidence, outside, parent)


@pytest.mark.parametrize("mutation", [None, "quarantine_bytes", "incomplete_recovery", "live_bytes", "metadata"])
def test_abrupt_verification_recovers_before_full_metadata_without_losing_guards(tmp_path, mutation):
    """Exercise the harness ordering with real files; native SQL remains an E2E gate."""
    storage, quarantine = tmp_path / "storage", tmp_path / "quarantine"
    storage.mkdir()
    quarantine.mkdir()
    payload = b"isolated abrupt-checkpoint fixture\n"
    selected = {"path": "fixture.csv", "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
    saved, live = quarantine / selected["path"], storage / selected["path"]
    saved.write_bytes(payload)
    before = {"tables": {name: {"synthetic-id": "immutable-row-hash"} for name in trial.docker_state.CURRENT_STATE_TABLES}}
    calls = []
    if mutation == "quarantine_bytes":
        saved.write_bytes(b"corrupt")

    def inspect_files():
        result = {"status": "PASS"}
        for label, path in (("originals", live), ("quarantine", saved)):
            intact = path.is_file() and path.read_bytes() == payload
            result.update({label + "_intact": int(intact), label + "_absent": int(not path.exists()),
                           label + "_mismatched": int(path.exists() and not intact)})
        return result

    def call(action, *, folder, **values):
        assert folder == "abrupt"
        calls.append(action)
        if action == "inspect_files":
            assert values == {"files": [selected]}
            return inspect_files()
        if action == "recover":
            saved.replace(live)
            if mutation == "live_bytes":
                live.write_bytes(b"corrupt")
            return {"status": "ROLLED_BACK", "files_restored": 0 if mutation == "incomplete_recovery" else 1}
        assert action == "metadata" and not values
        # Full metadata verification legitimately rejects missing live artifacts.
        if not live.is_file():
            raise ValueError("CLEANUP_FILE_UNVERIFIED")
        after = deepcopy(before)
        if mutation == "metadata":
            after["tables"]["users"]["synthetic-id"] = "changed-row-hash"
        return after

    if mutation is None:
        trial.verify_abrupt_recovery(call, [selected], before)
        assert calls == ["inspect_files", "recover", "inspect_files", "metadata"]
        assert live.read_bytes() == payload and not saved.exists()
    else:
        with pytest.raises(ValueError):
            trial.verify_abrupt_recovery(call, [selected], before)
        assert ("recover" in calls) is (mutation != "quarantine_bytes")
        assert ("metadata" in calls) is (mutation == "metadata")
