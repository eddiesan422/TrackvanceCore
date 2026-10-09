"""Private stdin/stdout protocol for the host-supervised one-shot cleanup tool."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from uuid import uuid4


def main() -> int:
    request = json.loads(sys.stdin.readline())
    # Read the private configuration before importing engine/storage singletons.
    os.environ["DATABASE_URL"] = request["database_url"]
    os.environ["DEMO_ACCESS_ENABLED"] = "false"
    os.environ["DEMO_SEED_ENABLED"] = "false"
    from trackvance import models
    from trackvance.db import SessionLocal
    from trackvance.operational_cleanup import (
        apply_plan,
        create_plan,
        digest,
        file_entry,
        population,
        recover_quarantine,
    )
    from trackvance.operations_common import OperationError

    def quiescent() -> None:
        nonce = uuid4().hex
        print(json.dumps({"kind": "quiescence_request", "nonce": nonce}), flush=True)
        proof = json.loads(sys.stdin.readline())
        if proof.get("nonce") != nonce or proof.get("quiescent") is not True:
            raise ValueError("Live host inventory did not authorize the database/file phase.")

    quiescent()
    storage = Path(request["storage"])
    with SessionLocal() as db:
        action = request["action"]
        if action == "plan":
            if db.get_bind().dialect.name == "postgresql":
                db.connection().exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            result = create_plan(db, request["project"], request["organization_id"], request["dataset_ids"], storage)
        elif action == "apply":
            sys.path.insert(0, "/cleanup-scripts")
            import docker_state  # type: ignore[import-not-found]
            def injected_failure() -> None:
                if request.get("inject_abrupt_failure_before_commit"):
                    os._exit(86)  # Synthetic crash: leave the durable journal for a fresh recovery session.
                raise OperationError(409, "CLEANUP_INJECTED_PRECOMMIT_FAILURE", "Synthetic isolated-copy rollback checkpoint.")
            fault = request.get("inject_failure_before_commit", False) or request.get("inject_abrupt_failure_before_commit", False)
            if fault and not re.fullmatch(r"trackvance-v080-test-restore085-[a-f0-9]{12}", request["project"]):
                raise ValueError("Failure injection is restricted to the owned synthetic restore copy.")
            result = apply_plan(db, request["plan"], storage, Path(request["quarantine"]), Path("/cleanup-backup"),
                docker_state.verify_backup, project=request["project"], actor_id=request["actor_id"], verify_quiescence=quiescent,
                restore_receipt=request["restore_receipt"], before_commit=injected_failure if fault else None)
        elif action == "recover":
            result = recover_quarantine(db, Path(request["quarantine"]), storage,
                project=request["project"], verify_quiescence=quiescent)
        elif action == "inspect_files":
            counts = {"originals_intact": 0, "originals_absent": 0, "originals_mismatched": 0,
                      "quarantine_intact": 0, "quarantine_absent": 0, "quarantine_mismatched": 0}
            for entry in request["files"]:
                relative = Path(entry["path"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("Invalid inspection path.")
                for label, root in (("originals", storage), ("quarantine", Path(request["quarantine"]) / "files")):
                    candidate = root / relative
                    outcome = "absent" if not candidate.exists() else "intact" if file_entry(str(candidate), root.resolve()) == entry else "mismatched"
                    counts[label + "_" + outcome] += 1
            result = {"status": "PASS", **counts}
        elif action == "metadata":
            if db.get_bind().dialect.name != "postgresql":
                raise ValueError("The native SQL verification receipt requires PostgreSQL.")
            db.connection().exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            rows, _schema, _edges = population(db)
            sys.path.insert(0, "/cleanup-scripts")
            import verify_storage  # type: ignore[import-not-found]
            row_lists = {name: list(items.values()) for name, items in rows.items()}
            foreign_keys = [(name, fk.parent.name, fk.column.table.name, fk.column.name)
                for name, table in models.Base.metadata.tables.items() for fk in table.foreign_keys]
            checks = verify_storage.validate_relationships(row_lists, foreign_keys)
            for artifact in row_lists["artifacts"]:
                entry = file_entry(artifact["path"], storage.resolve())
                if entry["sha256"] != artifact["sha256"] or entry["bytes"] != artifact["size_bytes"]:
                    raise ValueError("A surviving artifact does not match its immutable metadata.")
            markers = [row for row in row_lists["audit_events"] if row["subject_type"] == "operational_cleanup"
                and row["subject_id"] == request.get("audit_nonce")]
            result = {"schema_version": 9, "migration": "0019_governance_people", "database": "POSTGRESQL",
                "tables": {name: {key: digest(row) for key, row in sorted(items.items())} for name, items in sorted(rows.items())},
                "validated_relationships": checks, "verified_artifacts": len(row_lists["artifacts"]),
                "physical_schema": "PASS", "sql_inspection": "READ_ONLY_VALIDATED",
                "audit_markers": [{"id": row["id"], "event_type": row["event_type"],
                    "plan_sha256": row["metadata_json"].get("plan_sha256"),
                    "removed_counts": row["metadata_json"].get("removed_counts"),
                    "external_destinations_touched": row["metadata_json"].get("external_destinations_touched")} for row in markers]}
        else:
            raise ValueError("Unknown cleanup action.")
    print(json.dumps({"kind": "result", "result": result}), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - private subprocess boundary must redact all driver errors
        # No URL, row content, credentials or opaque reference reaches stderr.
        print(json.dumps({"kind": "failure", "error_type": type(exc).__name__,
            "error_code": getattr(exc, "code", "CLEANUP_FAILED")}), flush=True)
        raise SystemExit(1) from None
