"""Native selective-cleanup trial, exclusively on a guarded restored PostgreSQL copy."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cleanup_operational
import docker_state

PROTECTED = frozenset({"users", "roles", "role_permissions", "sessions", "external_identities", "oidc_login_attempts",
    "external_connections", "external_connection_versions", "delivery_destinations", "delivery_destination_versions",
    "delivery_target_guards", "delivery_target_policies", "delivery_target_decisions", "macro_domains", "data_domains",
    "glossary_terms", "governance_people", "dataset_blocks", "dataset_security_dependencies"})
CHECKS = ("dry_plan_read_only", "fault_rollback", "abrupt_fault_recovery", "metadata_transaction",
    "sql_relationships", "protected_hashes", "protected_secret_bytes", "unknown_barrier_preserved", "files_quarantined", "reset_audit")


def require_copy(project):
    if not re.fullmatch(r"trackvance-v080-test-restore085-[a-f0-9]{12}", project):
        raise ValueError("Selective cleanup certification is restricted to the owned restored 0.8.5 copy.")


def assert_plan(plan, before, fixture):
    scope = plan["scope"]
    if scope["state_tables"] != before["tables"] or set(scope["delete"]) != docker_state.CURRENT_STATE_TABLES:
        raise ValueError("The dry plan does not match the complete native backup population.")
    if set(scope["delete"]["datasets"]) != set(fixture["removable_dataset_ids"]) or not scope["files"]:
        raise ValueError("The plan did not select exactly the removable datasets and their files.")
    if any(scope["delete"][name] for name in PROTECTED | {"audit_events"}):
        raise ValueError("The plan selected a protected identity, security reference or historical audit.")
    for name, key in (("datasets", "protected_dataset_id"), ("runs", "unknown_run_id"),
                      ("delivery_attempts", "unknown_attempt_id"), ("delivery_target_guards", "target_guard_id")):
        if fixture[key] not in before["tables"][name] or fixture[key] in scope["delete"][name]:
            raise ValueError("The UNKNOWN branch or external-target barrier was not protected.")
    if any(not scope["delete"][name] for name in ("configurations", "monitor_schedules", "monitor_schedule_versions",
            "report_definitions", "report_revisions", "report_contexts", "report_executions", "strict_approvals",
            "governance_history", "outbox_events", "event_consumptions", "internal_notifications")):
        raise ValueError("The trial did not populate and select every required dependent operational family.")
    if scope["external_destinations_touched"] is not False:
        raise ValueError("An external operation is forbidden in the cleanup trial.")


def assert_transition(before, after, plan):
    if set(after["tables"]) != docker_state.CURRENT_STATE_TABLES or after.get("database") != "POSTGRESQL":
        raise ValueError("Native SQL verification requires the complete PostgreSQL schema.")
    if after.get("sql_inspection") != "READ_ONLY_VALIDATED" or after.get("validated_relationships", 0) <= 0:
        raise ValueError("Every SQL and frozen/polymorphic relationship must be inspected after removal.")
    for name, rows in before["tables"].items():
        expected = {key: value for key, value in rows.items() if key not in plan["scope"]["delete"][name]}
        actual = after["tables"][name]
        if name == "audit_events":
            if any(actual.get(key) != value for key, value in expected.items()) or len(actual) != len(expected) + 1:
                raise ValueError("Historical audits changed or the new removal audit is missing.")
        elif actual != expected:
            raise ValueError("The SQL transition changed records outside the sealed exact IDs.")
    markers = after.get("audit_markers", [])
    if len(markers) != 1 or markers[0].get("id") in before["tables"]["audit_events"] or (
            markers[0].get("event_type") != "OPERATIONAL_TEST_DATA_REMOVED"
            or markers[0].get("plan_sha256") != plan["plan_sha256"]
            or markers[0].get("removed_counts") != plan["scope"]["removed_counts"]
            or markers[0].get("external_destinations_touched") is not False):
        raise ValueError("The exact reset audit does not bind the applied plan and removed counts.")


def assert_files(result, count, *, moved):
    expected = {"status": "PASS", "originals_intact": 0 if moved else count, "originals_absent": count if moved else 0,
        "originals_mismatched": 0, "quarantine_intact": count if moved else 0, "quarantine_absent": 0 if moved else count,
        "quarantine_mismatched": 0}
    if result != expected:
        raise ValueError("Selected live/quarantine file hashes or absence do not match the plan.")


def archived_file_hashes(path):
    """Hash private encrypted/key archive contents without exporting their bytes."""
    docker_state.inspect_archive(path)
    result = {}
    with tarfile.open(path, "r|gz") as archive:
        for member in archive:
            if member.isfile():
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("Missing private archived file.")
                result[member.name] = {"bytes": member.size, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
    return result


def prepare_private_mounts(evidence, backup, quarantine_parent):
    """Permit the non-root image at bind roots while a private outer directory protects host access."""
    evidence = evidence.resolve(strict=True)
    if (not evidence.is_dir() or evidence.is_symlink() or backup.resolve(strict=True).parent != evidence
            or quarantine_parent.resolve(strict=True).parent != evidence):
        raise ValueError("Helper mounts must be fresh children of the explicit private evidence directory.")
    evidence.chmod(0o700)
    # Bind mounts bypass the 0700 outer host directory. Backup stays read-only in
    # Docker; these inner modes let the image's own UID read exact private bytes.
    for path in [backup, *backup.rglob("*")]:
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError("A private backup mount contains an unsafe host entry.")
        path.chmod(0o755 if path.is_dir() else 0o644)
    quarantine_parent.chmod(0o777)


def trial(harness, directory, context, environment, evidence):
    """Backup, prove a second restore, then fault/recover/apply only the first owned copy."""
    require_copy(context["project"])
    harness.preflight(directory, context, environment)
    api = docker_state._container_for(docker_state.inventory(context["project"]), "api")
    fixture = json.loads(harness.run(["docker", "exec", "-i", str(api["id"]), "python", "-", context["project"]], environment,
        input_text=Path(__file__).with_name("selective_cleanup_fixture.py").read_text(encoding="utf-8")))
    if fixture.get("project") != context["project"] or fixture.get("fixture") != "SYNTHETIC_LOCAL_METADATA_NO_ENGINE_OR_REMOTE_IO":
        raise ValueError("The synthetic fixture did not prove its owned project and local-only origin.")
    backup = evidence / "selective-cleanup-backup"
    docker_state.backup(context["project"], backup)
    docker_state.verify_backup(backup)
    before = json.loads((backup / "state.json").read_text(encoding="utf-8"))
    manifest_sha = docker_state.digest(backup / "backup-manifest.json")
    state_sha = docker_state.digest(backup / "state.json")
    # Release API/web before verifying the second fresh restore. The first copy's
    # PostgreSQL stays alive and only its exact native metadata/files are targeted.
    services = {item["service"] for item in docker_state.inventory(context["project"])["containers"]} - {"postgres"}
    docker_state.compose(context["project"], "stop", "--timeout", "30", *sorted(services))
    cleanup_operational.require_quiescence(context["project"])
    restore_dir, restored, restore_env = harness.target_context(context, "cleanuprestore085", environment)
    own_compose, claimed = docker_state.compose, False
    try:
        docker_state.compose = harness.compose_adapter(restore_dir, restored, restore_env)
        claimed = True
        restore_receipt = docker_state.restore(backup, restored["project"], start=False, web_port=restored["port"])
        if (restore_receipt.get("schema_version") != 1 or restore_receipt.get("status") != "STOPPED_VERIFIED"
                or restore_receipt.get("target_project") != restored["project"] or restored["project"] == context["project"]
                or restore_receipt.get("source_manifest_sha256") != manifest_sha
                or restore_receipt.get("verified_state_sha256") != state_sha
                or any(item["running"] for item in docker_state.inventory(restored["project"])["containers"])):
            raise ValueError("A separate real stopped restore must match both exact backup hashes before apply.")
    finally:
        docker_state.compose = own_compose
        if claimed:
            harness.cleanup(restore_dir, restored, evidence)
        harness.assert_main(context)
    (evidence / "selective-cleanup-verified-restore.json").write_text(json.dumps(restore_receipt, indent=2), encoding="utf-8")
    image = str(api["image_id"])
    revision = cleanup_operational.inspect_one("image", image)["Config"]["Labels"]["org.opencontainers.image.revision"]
    quarantine_parent = evidence / "selective-quarantine"
    quarantine_parent.mkdir()
    prepare_private_mounts(evidence, backup, quarantine_parent)
    options = {"image": image, "source_commit": revision, "backup": backup, "quarantine_parent": quarantine_parent}
    common = {"project": context["project"]}

    def call(action, *, folder="fault", **values):
        return cleanup_operational.execute({**common, "action": action, "quarantine": "/cleanup-quarantine/" + folder, **values}, **options)

    plan = call("plan", organization_id=fixture["organization_id"], dataset_ids=fixture["dataset_ids"])
    assert_plan(plan, before, fixture)
    (evidence / "selective-cleanup-plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    count = len(plan["scope"]["files"])
    if call("metadata")["tables"] != before["tables"]:
        raise ValueError("The dry plan changed native metadata.")
    apply_values = {"plan": plan, "actor_id": fixture["actor_id"], "restore_receipt": restore_receipt}
    try:
        call("apply", inject_failure_before_commit=True, **apply_values)
    except ValueError as error:
        if "CLEANUP_INJECTED_PRECOMMIT_FAILURE" not in str(error):
            raise
    else:
        raise ValueError("The controlled PostgreSQL precommit failure was not reached.")
    if call("metadata")["tables"] != before["tables"]:
        raise ValueError("The fault did not roll back every native metadata row.")
    assert_files(call("inspect_files", files=plan["scope"]["files"]), count, moved=False)
    try:
        call("apply", folder="abrupt", inject_abrupt_failure_before_commit=True, **apply_values)
    except ValueError as error:
        if "did not return a successful result" not in str(error):
            raise
    else:
        raise ValueError("The synthetic abrupt crash was not reached.")
    if call("metadata", folder="abrupt")["tables"] != before["tables"]:
        raise ValueError("The database did not roll back after its helper exited abruptly.")
    assert_files(call("inspect_files", folder="abrupt", files=plan["scope"]["files"]), count, moved=True)
    recovered = call("recover", folder="abrupt")
    if recovered.get("status") != "ROLLED_BACK" or recovered.get("files_restored") != count:
        raise ValueError("Fresh-session native recovery did not restore all sealed files.")
    assert_files(call("inspect_files", folder="abrupt", files=plan["scope"]["files"]), count, moved=False)
    applied = call("apply", folder="success", **apply_values)
    after = call("metadata", folder="success", audit_nonce=plan["nonce"])
    assert_transition(before, after, plan)
    if after.get("verified_artifacts", 0) <= 0:
        raise ValueError("The surviving protected artifacts were not verified byte for byte.")
    assert_files(call("inspect_files", folder="success", files=plan["scope"]["files"]), count, moved=True)
    if (docker_state.digest(backup / "backup-manifest.json") != manifest_sha or docker_state.digest(backup / "state.json") != state_sha
            or applied.get("backup_manifest_sha256") != manifest_sha or applied.get("verified_restore_project") != restored["project"]
            or applied.get("quarantined_files") != count or applied.get("external_destinations_touched") is not False):
        raise ValueError("The final native receipt does not preserve the verified backup, restore or file counts.")
    docker_state.verify_backup(backup)
    private_archives = evidence / "protected-secret-volumes-after"
    (private_archives / "volumes").mkdir(parents=True)
    state = cleanup_operational.require_quiescence(context["project"])
    for logical in ("connection_credentials", "connection_keys", "delivery_credentials", "delivery_keys"):
        saved = archived_file_hashes(backup / "volumes" / (logical + ".tar.gz"))
        if not saved:
            raise ValueError("Both protected credential and key families must actually be populated.")
        archive = docker_state._archive_volume(docker_state._volume_for(state, logical), logical, image, private_archives)
        if archived_file_hashes(private_archives / archive["path"]) != saved:
            raise ValueError("The protected encrypted credential/key bytes changed during cleanup.")
    harness.assert_main(context)
    result = {"status": "PASS", "scope": "OWNED_RESTORED_POSTGRES_COPY", "database": "POSTGRESQL", "project": context["project"],
        "source_sha": revision, "schema_version": 9, "migration": "0019_governance_people", "tables": len(after["tables"]),
        "backup_manifest_sha256": manifest_sha, "verified_state_sha256": state_sha, "verified_restore_project": restored["project"],
        "verified_restore": "STOPPED_VERIFIED", "plan_sha256": plan["plan_sha256"], "quarantined_files": count,
        "verified_surviving_artifacts": after["verified_artifacts"], "protected_secret_volumes": 4,
        "removed_counts": applied["removed_counts"], "protected_tables": sorted(PROTECTED), "reset_audit_added": 1,
        "external_destinations_touched": False, "backup_retained": True, "usual_inventory": "UNCHANGED",
        "protected_dataset_id": fixture["protected_dataset_id"], "unknown_run_id": fixture["unknown_run_id"],
        **dict.fromkeys(CHECKS, "PASS")}
    (evidence / "selective-cleanup-receipt.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
