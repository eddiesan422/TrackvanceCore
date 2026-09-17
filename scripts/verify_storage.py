"""Fingerprint persisted business records and verify every registered artifact.

Execute snapshot using the backend interpreter or inside its API container.
Reports contain IDs and hashes, never raw business values, users or sessions.
Comparison is intended for a quiescent restart/restore: finish pending runs first.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

TABLES = (
    "datasets", "dataset_versions", "configurations", "runs", "artifacts",
    "artifact_links", "findings", "exceptions", "audit_events", "metric_history",
)


def snapshot() -> dict:
    from sqlalchemy import select, text

    from trackvance.artifactstore import artifact_store
    from trackvance.db import Base, SessionLocal
    from trackvance.models import Artifact, Run

    report: dict = {"schema_version": 1, "tables": {}, "verified_artifacts": 0}
    with SessionLocal() as session:
        if session.scalar(select(Run.id).where(Run.status.in_(["QUEUED", "RUNNING"]))):
            raise ValueError("Finaliza las ejecuciones pendientes antes de tomar la huella.")
        report["migration"] = session.scalar(text("SELECT version_num FROM alembic_version"))
        for name in TABLES:
            fingerprints = {}
            for row in session.execute(select(Base.metadata.tables[name])).mappings():
                canonical = json.dumps(dict(row), sort_keys=True, default=str,
                                       ensure_ascii=False, separators=(",", ":"))
                fingerprints[row["id"]] = hashlib.sha256(canonical.encode()).hexdigest()
            report["tables"][name] = dict(sorted(fingerprints.items()))
        for artifact in session.scalars(select(Artifact)):
            artifact_store.verify(artifact)
            report["verified_artifacts"] += 1
    return report


def compare(before: dict, after: dict) -> None:
    if before.get("schema_version") != 1 or after.get("schema_version") != 1:
        raise ValueError("Versión de informe de integridad no reconocida.")
    if before != after:
        changed = [name for name in TABLES
                   if before["tables"].get(name) != after["tables"].get(name)]
        raise ValueError("La persistencia cambió: " + (", ".join(changed) or "migración/artifacts"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("snapshot")
    comparison = commands.add_parser("compare")
    comparison.add_argument("before", type=Path)
    comparison.add_argument("after", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            print(json.dumps(snapshot(), indent=2, sort_keys=True))
        else:
            compare(json.loads(args.before.read_text(encoding="utf-8-sig")),
                    json.loads(args.after.read_text(encoding="utf-8-sig")))
            print("OK: registros persistidos y SHA-256 de artifacts idénticos.")
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
