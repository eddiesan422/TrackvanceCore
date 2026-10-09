"""One-shot selective operational removal; never a startup/reset/volume operation.

The operator supplies confirmed test dataset IDs. Protected dependencies are
excluded, never inferred as test-owned. A private backup and stopped dispatchers
are prerequisites; a sealed plan is rechecked under database locks before writes.
Files move to recoverable quarantine outside the live store before metadata commit.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import tarfile
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast
from uuid import uuid4

from sqlalchemy import delete, inspect, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from .db import Base, utcnow
from .models import AuditEvent, User
from .operations_common import OperationError

ALLOWED = frozenset({"datasets", "dataset_versions", "configurations", "runs", "jobs", "findings", "exceptions",
    "exception_attachments", "artifacts", "artifact_links", "idempotency_keys", "metric_history",
    "monitor_schedules", "monitor_schedule_versions", "monitor_occurrences", "acquisition_runs", "acquisition_uploads",
    "delivery_attempts", "delivery_reviews", "delivery_automations", "delivery_automation_versions", "delivery_occurrences",
    "delivery_input_claims", "outbox_events", "event_consumptions", "internal_notifications", "report_definitions",
    "report_revisions", "report_contexts", "report_executions", "strict_approvals", "governance_history",
    "column_documentation", "glossary_associations", "dataset_source_bindings"})
TYPE_TABLE = {"DATASET": "datasets", "DATASET_VERSION": "dataset_versions", "RUN": "runs", "ARTIFACT": "artifacts",
    "CONFIGURATION": "configurations", "EXCEPTION": "exceptions", "ACQUISITION": "acquisition_runs",
    "REPORT_DEFINITION": "report_definitions", "REPORT_REVISION": "report_revisions", "REPORT_CONTEXT": "report_contexts",
    "REPORT_EXECUTION": "report_executions", "DELIVERY_ATTEMPT": "delivery_attempts",
    "MONITOR_OCCURRENCE": "monitor_occurrences", "DELIVERY_OCCURRENCE": "delivery_occurrences",
    "CONNECTION_VERSION": "external_connection_versions", "DELIVERY_DESTINATION": "delivery_destinations",
    "DELIVERY_DESTINATION_VERSION": "delivery_destination_versions", "DELIVERY_TARGET_POLICY": "delivery_target_policies"}
PATH_FIELDS = {"dataset_versions": ("original_path", "canonical_path"), "runs": ("result_path", "evidence_path"),
               "artifacts": ("path",), "acquisition_uploads": ("path",)}
ACTIVE = frozenset({"QUEUED", "RUNNING", "LEASED", "STARTED"})


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False).encode()).hexdigest()


def error(code: str, message: str) -> NoReturn:
    raise OperationError(409, code, message)


def sync_directory(path: Path) -> None:
    """Persist directory entries on the certified Linux runtime."""
    if os.name == "nt":
        return  # Windows fixtures do not claim Linux/power-loss durability.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_mkdir(path: Path, *, exist_ok: bool = True) -> None:
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.mkdir(parents=True, exist_ok=exist_ok, mode=0o700)
    for created in reversed(missing):
        sync_directory(created.parent)
        sync_directory(created)


def relocate(source: Path, target: Path, expected_hash: str) -> None:
    """Prefer rename; an exclusive fsynced copy makes cross-mount recovery safe."""
    try:
        source.rename(target)
        sync_directory(target.parent)
        if source.parent != target.parent:
            sync_directory(source.parent)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        with source.open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        with target.open("rb") as reader:
            if hashlib.file_digest(reader, "sha256").hexdigest() != expected_hash:
                error("CLEANUP_FILE_COPY_INVALID", "La copia a cuarentena no conserva el hash original.")
        with source.open("rb") as reader:
            if hashlib.file_digest(reader, "sha256").hexdigest() != expected_hash:
                error("CLEANUP_PLAN_DRIFT", "El archivo de origen cambió durante la copia a cuarentena.")
        sync_directory(target.parent)
        source.unlink()
        sync_directory(source.parent)


def save_json(root: Path, name: str, value: dict) -> None:
    temporary = root / ("." + name + "." + uuid4().hex)
    with temporary.open("x", encoding="utf-8") as stream:
        temporary.chmod(0o600)
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, root / name)
    sync_directory(root)


def verify_recoverable_backup(backup: Path, manifest: dict, restore_receipt: dict, scope: dict) -> str:
    """Bind a stopped isolated restore and every selected byte to this backup."""
    with (backup / "backup-manifest.json").open("rb") as stream:
        manifest_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    with (backup / "state.json").open("rb") as stream:
        state_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    target = restore_receipt.get("target_project")
    if (type(restore_receipt.get("schema_version")) is not int or restore_receipt.get("schema_version") != 1
            or restore_receipt.get("status") != "STOPPED_VERIFIED"
            or not isinstance(target, str) or not target.startswith("trackvance-") or target == scope["project"]
            or restore_receipt.get("source_manifest_sha256") != manifest_sha
            or restore_receipt.get("verified_state_sha256") != state_sha):
        error("CLEANUP_RESTORE_UNVERIFIED", "El backup requiere una restauración aislada detenida y verificable de los mismos bytes.")
    if json.loads((backup / "backup-manifest.json").read_text(encoding="utf-8-sig")) != manifest:
        error("CLEANUP_BACKUP_MISMATCH", "El manifest del backup cambió durante su verificación.")
    expected = {entry["path"]: entry for entry in scope["files"]}
    found = set()
    try:
        with tarfile.open(backup / "volumes" / "trackvance_data.tar.gz", "r|gz") as archive:
            for member in archive:
                path = PurePosixPath(member.name.replace("\\", "/")).as_posix()
                if path not in expected:
                    continue
                entry = expected[path]
                if path in found or not member.isfile() or member.size != entry["bytes"]:
                    error("CLEANUP_BACKUP_FILES_MISMATCH", "El backup no conserva un archivo seleccionado con su tamaño exacto.")
                reader = archive.extractfile(member)
                if reader is None:
                    error("CLEANUP_BACKUP_FILES_MISMATCH", "El backup no contiene un archivo seleccionado recuperable.")
                with reader:
                    file_hash = hashlib.sha256()
                    while block := reader.read(1024 * 1024):
                        file_hash.update(block)
                    if file_hash.hexdigest() != entry["sha256"]:
                        error("CLEANUP_BACKUP_FILES_MISMATCH", "Los bytes respaldados no coinciden con el plan selectivo.")
                found.add(path)
    except (OSError, tarfile.TarError):
        error("CLEANUP_BACKUP_FILES_MISMATCH", "El volumen operativo del backup no se puede comprobar.")
    if found != set(expected):
        error("CLEANUP_BACKUP_FILES_MISMATCH", "El backup no incluye todos los archivos seleccionados.")
    return manifest_sha


def file_entry(path: str, storage: Path) -> dict:
    raw = Path(path)
    candidate = raw if raw.is_absolute() else storage / raw
    if candidate.is_symlink() or not candidate.is_file():
        error("CLEANUP_FILE_UNVERIFIED", "Un archivo seleccionado no existe o es un enlace; no se puede limpiar.")
    actual = candidate.resolve(strict=True)
    if not actual.is_relative_to(storage) or actual == storage:
        error("CLEANUP_PATH_PROTECTED", "La ruta seleccionada está fuera del almacenamiento operativo.")
    # Reject any symlinked intermediate directory, even if it resolves in scope.
    if any(item.is_symlink() for item in [candidate, *candidate.parents] if item != storage.parent):
        error("CLEANUP_PATH_PROTECTED", "La ruta seleccionada contiene un enlace.")
    with actual.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": actual.relative_to(storage).as_posix(), "sha256": sha, "bytes": actual.stat().st_size}


def population(db: Session) -> tuple[dict, dict, set[tuple]]:
    rows = {}
    for name, table in sorted(Base.metadata.tables.items()):
        items = [dict(item) for item in db.execute(select(table)).mappings()]
        rows[name] = {row_identity(name, item): item for item in items}
    physical = inspect(db.connection())
    if set(physical.get_table_names()) != set(Base.metadata.tables) | {"alembic_version"}:
        error("CLEANUP_SCHEMA_DRIFT", "Las tablas físicas no corresponden al contrato de esta herramienta.")
    schema = {name: {"columns": [(col.name, str(col.type), col.nullable) for col in table.columns],
        "pk": [col.name for col in table.primary_key],
        "foreign_keys": sorted((fk.parent.name, fk.column.table.name, fk.column.name) for fk in table.foreign_keys)}
        for name, table in sorted(Base.metadata.tables.items())}
    for name, table in Base.metadata.tables.items():
        if {item["name"] for item in physical.get_columns(name)} != set(table.c.keys()):
            error("CLEANUP_SCHEMA_DRIFT", "Las columnas físicas no corresponden al contrato de esta herramienta.")
        physical_fks = physical.get_foreign_keys(name)
        expected_fks = Counter((fk.parent.name, fk.column.table.name, fk.column.name) for fk in table.foreign_keys)
        actual_fks = Counter((item["constrained_columns"][0], item["referred_table"], item["referred_columns"][0])
            for item in physical_fks if len(item["constrained_columns"]) == len(item["referred_columns"]) == 1)
        if actual_fks != expected_fks or any(len(item["constrained_columns"]) != 1 for item in physical_fks):
            error("CLEANUP_SCHEMA_DRIFT", "Las referencias físicas no corresponden al contrato de esta herramienta.")
        schema[name]["physical"] = {
            "columns": [(item["name"], str(item["type"]), item["nullable"]) for item in physical.get_columns(name)],
            "pk": physical.get_pk_constraint(name), "foreign_keys": physical.get_foreign_keys(name),
            "unique": physical.get_unique_constraints(name), "checks": physical.get_check_constraints(name),
            "indexes": physical.get_indexes(name)}
    edges = set()
    indexes = {(name, col.name): {item[col.name]: identity for identity, item in rows[name].items()}
               for name, table in Base.metadata.tables.items() for col in table.columns if col.primary_key}
    for name, table in Base.metadata.tables.items():
        for identity, row in rows[name].items():
            child = (name, identity)
            for fk in table.foreign_keys:
                value = row[fk.parent.name]
                if value is not None:
                    parent = indexes[(fk.column.table.name, fk.column.name)].get(value)
                    if parent is None:
                        error("CLEANUP_REFERENTIAL_DRIFT", "El inventario contiene una referencia rota; no se puede limpiar.")
                    parent_row = rows[fk.column.table.name][parent]
                    if row.get("organization_id") is not None and parent_row.get("organization_id") is not None and row["organization_id"] != parent_row["organization_id"]:
                        error("CLEANUP_REFERENTIAL_DRIFT", "Una referencia pertenece a otra organización; no se puede limpiar.")
                    edges.add((child, (fk.column.table.name, parent)))
            for column, target in (("parent_version_id", "dataset_versions"), ("source_run_id", "runs"),
                                   ("output_version_id", "dataset_versions"), ("configuration_id", "configurations")):
                value = row.get(column)
                if value:
                    logical_parent = rows.get(target, {}).get(str(value))
                    if logical_parent is None or logical_parent.get("organization_id") != row.get("organization_id"):
                        error("CLEANUP_REFERENTIAL_DRIFT", "Un linaje operativo está roto o pertenece a otra organización.")
                    if (target, str(value)) != child:
                        edges.add((child, (target, str(value))))
            references = []
            if name == "artifact_links":
                references = [(str(row[f"{side}_type"]).upper(), str(row[f"{side}_id"])) for side in ("source", "target")]
            elif name == "outbox_events":
                references = [(str(row["aggregate_type"]).upper(), str(row["aggregate_id"]))]
            elif name == "internal_notifications":
                references = [(str(row["resource_type"]).upper(), str(row["resource_id"]))]
            for entity_type, value in references:
                polymorphic_target = TYPE_TABLE.get(entity_type)
                if polymorphic_target is None:
                    # Never adopt an unfamiliar consumer. An ambiguous matching
                    # ID protects every possible operational ancestor instead.
                    edges.update((child, (candidate, value)) for candidate in ALLOWED if value in rows[candidate])
                    continue
                polymorphic_parent = rows[polymorphic_target].get(value)
                if polymorphic_parent is None or polymorphic_parent.get("organization_id") != row.get("organization_id"):
                    error("CLEANUP_REFERENTIAL_DRIFT", "Una referencia polimórfica está rota o pertenece a otra organización.")
                edges.add((child, (polymorphic_target, value)))
            if name in {"report_contexts", "report_revisions"}:
                document = row.get("snapshot") if name == "report_contexts" else row.get("draft")
                for source in (document or {}).get("sources", []):
                    for column, target in (("input_dataset_id", "datasets"), ("dataset_id", "datasets"),
                            ("output_dataset_id", "datasets"), ("approval_run_id", "runs"), ("canonical_artifact_id", "artifacts"),
                            ("input_version_id", "dataset_versions"), ("output_version_id", "dataset_versions"),
                            ("contract_id", "configurations"), ("contract_revision_id", "configurations")):
                        value = str(source.get(column) or "")
                        if value:
                            if value not in rows[target] or rows[target][value].get("organization_id") != row.get("organization_id"):
                                error("CLEANUP_REFERENTIAL_DRIFT", "Una fuente congelada está rota o pertenece a otra organización.")
                            edges.add((child, (target, value)))
                    for value in source.get("contract_revision_ids", []):
                        if str(value) not in rows["configurations"] or rows["configurations"][str(value)].get("organization_id") != row.get("organization_id"):
                            error("CLEANUP_REFERENTIAL_DRIFT", "Una revisión de fuente congelada está rota o pertenece a otra organización.")
                        edges.add((child, ("configurations", str(value))))
    return rows, schema, edges


def row_identity(table: str, row: dict) -> str:
    if table == "role_permissions":
        return json.dumps([row["role_id"], row["permission_code"]], separators=(",", ":"))
    if table == "oidc_login_attempts":
        return str(row["state_hash"])
    return str(row["id"])


def deletion_order(selected: set[tuple], edges: set[tuple]) -> list[tuple]:
    pending, order = set(selected), []
    while pending:
        # Child first, including row-level self references and explicit lineage.
        parents = {parent for child, parent in edges if child in pending and parent in pending}
        ready = sorted(pending - parents)
        if not ready:
            error("CLEANUP_DEPENDENCY_CYCLE", "El plan contiene un ciclo; requiere una decisión específica antes de limpiar.")
        order.extend(ready)
        pending.difference_update(ready)
    return order


def compute(db: Session, project: str, organization_id: str, dataset_ids: list[str], storage: Path) -> dict:
    storage = storage.resolve(strict=True)
    if not project.startswith("trackvance-") or not dataset_ids or len(set(dataset_ids)) != len(dataset_ids):
        error("CLEANUP_SCOPE_INVALID", "Indica un proyecto Trackvance y los IDs explícitos de datasets de prueba.")
    rows, schema, edges = population(db)
    for identity in dataset_ids:
        dataset = rows["datasets"].get(identity)
        if not dataset or dataset["organization_id"] != organization_id:
            error("CLEANUP_SCOPE_INVALID", "Un dataset solicitado no pertenece a la organización declarada.")
    for name in ("runs", "jobs", "report_executions", "acquisition_runs", "event_consumptions"):
        if any(item.get("status") in ACTIVE for item in rows[name].values()):
            error("CLEANUP_NOT_QUIESCENT", "Resuelve trabajos y leases activos antes del backup y la limpieza.")
    for name in ("monitor_schedules", "delivery_automations"):
        if any(item.get("enabled") for item in rows[name].values()):
            error("CLEANUP_DISPATCH_ACTIVE", "Pausa las programaciones antes del backup y del plan selectivo.")
    selected = {("datasets", identity) for identity in dataset_ids}
    uncertain = set()
    # Scope-sensitive records cannot be adopted merely because one FK matches.
    def allowed(name: str, item: dict) -> bool:
        if name not in ALLOWED or item.get("organization_id") != organization_id:
            return False
        type_columns = {"artifact_links": ("source_type", "target_type"), "outbox_events": ("aggregate_type",),
                        "internal_notifications": ("resource_type",)}
        if any(str(item[column]).upper() not in TYPE_TABLE for column in type_columns.get(name, ())):
            return False
        if name == "datasets":
            return item["id"] in dataset_ids
        if name == "dataset_versions":
            return item["dataset_id"] in dataset_ids
        if name == "configurations":
            return item["dataset_id"] in dataset_ids and (not item.get("target_dataset_id") or item["target_dataset_id"] in dataset_ids)
        if name == "delivery_attempts" and item.get("status") == "UNKNOWN":
            return False
        if name == "runs":
            return item.get("status") != "UNKNOWN"
        if name in {"report_contexts", "report_revisions"}:
            document = item.get("snapshot") if name == "report_contexts" else item.get("draft")
            source_ids = {source.get("input_dataset_id") or source.get("dataset_id") for source in (document or {}).get("sources", [])}
            return bool(source_ids) and source_ids <= set(dataset_ids)
        if name == "delivery_automation_versions":
            siblings = [row for row in rows[name].values() if row["automation_id"] == item["automation_id"]]
            return all(allowed("configurations", rows["configurations"][row["configuration_id"]]) for row in siblings)
        return True
    changed = True
    while changed:
        previous = set(selected)
        for child, parent in edges:
            if parent in selected and child not in selected:
                if allowed(child[0], rows[child[0]][child[1]]):
                    selected.add(child)
                else:
                    uncertain.add(child)
        # Remove orphaned acquisition uploads and artifact identities only when
        # explicitly referenced by selected records; generic parents are protected.
        for child, parent in edges:
            if child in selected and parent[0] in {"artifacts", "acquisition_uploads"} and allowed(parent[0], rows[parent[0]][parent[1]]):
                selected.add(parent)
        for identity, item in rows["artifacts"].items():
            if allowed("artifacts", item) and any(item["path"] == row.get(field) for name, key in selected for row in [rows[name][key]]
                   for field in PATH_FIELDS.get(name, ())):
                selected.add(("artifacts", identity))
        # Outbox/notifications and frozen Reportes bindings use polymorphic IDs.
        for name, type_col, id_col in (("outbox_events", "aggregate_type", "aggregate_id"),
                                      ("internal_notifications", "resource_type", "resource_id")):
            for identity, item in rows[name].items():
                target = TYPE_TABLE.get(str(item[type_col]).upper())
                if target and (target, str(item[id_col])) in selected and allowed(name, item):
                    selected.add((name, identity))
        for identity, item in rows["report_revisions"].items():
            sources = (item.get("draft") or {}).get("sources", [])
            ids = {source.get("input_dataset_id") or source.get("dataset_id") for source in sources}
            if ids & set(dataset_ids) and ids <= set(dataset_ids) and allowed("report_revisions", item):
                selected.add(("report_revisions", identity))
        for identity, item in rows["report_definitions"].items():
            revisions = [key for key, row in rows["report_revisions"].items() if row["definition_id"] == identity]
            if revisions and all(("report_revisions", key) in selected for key in revisions) and allowed("report_definitions", item):
                selected.add(("report_definitions", identity))
        for identity, item in rows["delivery_automations"].items():
            revisions = [key for key, row in rows["delivery_automation_versions"].items() if row["automation_id"] == identity]
            if revisions and all(("delivery_automation_versions", key) in selected for key in revisions) and allowed("delivery_automations", item):
                selected.add(("delivery_automations", identity))
        for identity, item in rows["report_contexts"].items():
            sources = (item.get("snapshot") or {}).get("sources", [])
            ids = {source.get("input_dataset_id") or source.get("dataset_id") for source in sources}
            if ids & set(dataset_ids):
                if ids and ids <= set(dataset_ids) and allowed("report_contexts", item):
                    selected.add(("report_contexts", identity))
                else:
                    uncertain.add(("report_contexts", identity))
        changed = previous != selected
    # A surviving protected consumer always protects its selected ancestors.
    original_selected = set(selected)
    exceptions = []
    changed = True
    while changed:
        previous = set(selected)
        for child, parent in edges:
            if parent in selected and child not in selected:
                selected.remove(parent)
                exceptions.append({"table": parent[0], "id": parent[1], "consumer_table": child[0], "consumer_id": child[1]})
        changed = previous != selected
    # Preserve an entire dependent branch when its root was protected, including
    # UNKNOWN outcomes and external target barriers; do not partially erase it.
    changed = True
    while changed:
        previous = set(selected)
        for child, parent in edges:
            if child in selected and parent in original_selected and parent not in selected:
                selected.remove(child)
        for parent_table, child_table, column in (("delivery_automations", "delivery_automation_versions", "automation_id"),
                                                   ("report_definitions", "report_revisions", "definition_id")):
            for selected_identity in list(selected):
                if selected_identity[0] == parent_table and any(row[column] == selected_identity[1] and (child_table, key) not in selected
                        for key, row in rows[child_table].items()):
                    selected.remove(selected_identity)
        changed = previous != selected
    sql_edges = {(child, parent) for child, parent in edges if any(
        target == parent[0] and rows[child[0]][child[1]].get(column) == rows[parent[0]][parent[1]].get(target_column)
        for column, target, target_column in schema[child[0]]["foreign_keys"])}
    order = deletion_order(selected, sql_edges)
    paths = {row[field] for name, key in selected for row in [rows[name][key]]
             for field in PATH_FIELDS.get(name, ()) if row.get(field)}
    protected_paths = {row[field] for name, items in rows.items() for key, row in items.items() if (name, key) not in selected
                       for field in PATH_FIELDS.get(name, ()) if row.get(field)}
    normalized_selected = {(Path(path) if Path(path).is_absolute() else storage / path).resolve() for path in paths}
    normalized_protected = {(Path(path) if Path(path).is_absolute() else storage / path).resolve() for path in protected_paths}
    if normalized_selected & normalized_protected:
        error("CLEANUP_FILE_SHARED", "Un archivo seleccionado sostiene una referencia protegida; revisa el alcance.")
    files_by_path = {}
    absent = []
    for path in paths:
        candidate = Path(path) if Path(path).is_absolute() else storage / path
        if not candidate.exists() and all(name == "acquisition_uploads" and row.get("status") in {"CONSUMED", "EXPIRED"}
                for name, key in selected for row in [rows[name][key]]
                for field in PATH_FIELDS.get(name, ()) if row.get(field) == path):
            if not candidate.resolve().is_relative_to(storage):
                error("CLEANUP_PATH_PROTECTED", "Una referencia ausente está fuera del almacenamiento operativo.")
            absent.append(candidate.resolve().relative_to(storage).as_posix())
            continue
        entry = file_entry(path, storage)
        files_by_path[entry["path"]] = entry
    files = sorted(files_by_path.values(), key=lambda item: item["path"])
    revisions = list(db.scalars(text("SELECT version_num FROM alembic_version")))
    if revisions != ["0019_governance_people"]:
        error("CLEANUP_SCHEMA_DRIFT", "La limpieza 0.8.5 requiere exactamente la revisión 0019 certificada.")
    revision = revisions[0]
    return {"project": project, "organization_id": organization_id, "dataset_ids": sorted(dataset_ids),
        "migration": revision, "schema_sha256": digest(schema), "population_sha256": digest(rows),
        "state_tables": {name: {key: digest(row) for key, row in sorted(items.items())} for name, items in rows.items()},
        "delete": {name: sorted(identity for table, identity in selected if table == name) for name in sorted(rows)},
        "delete_order": order, "files": files, "removed_counts": {name: sum(table == name for table, _ in selected) for name in sorted(rows)},
        "protected_counts": {name: len(items) - sum(table == name for table, _ in selected) for name, items in sorted(rows.items())},
        "exceptions": sorted(exceptions, key=lambda item: (item["table"], item["id"], item["consumer_table"], item["consumer_id"])),
        "uncertain_consumers": sorted(uncertain), "already_absent_upload_files": sorted(absent), "external_destinations_touched": False}


def create_plan(db: Session, project: str, organization_id: str, dataset_ids: list[str], storage: Path) -> dict:
    now = utcnow()
    plan = {"schema_version": 1, "created_at": now.isoformat(), "expires_at": (now + timedelta(minutes=15)).isoformat(),
            "nonce": uuid4().hex, "scope": compute(db, project, organization_id, dataset_ids, storage)}
    return {**plan, "plan_sha256": digest(plan)}


def lock_metadata(db: Session) -> None:
    connection = db.connection()
    if connection.dialect.name == "postgresql":
        names = ",".join(connection.dialect.identifier_preparer.quote(name) for name in sorted([*Base.metadata.tables, "alembic_version"]))
        connection.execute(text(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE"))
    elif connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        error("CLEANUP_DATABASE_UNSUPPORTED", "Este almacén de metadata no está certificado para limpieza.")


def apply_plan(db: Session, plan: dict, storage: Path, quarantine: Path, backup: Path,
               verify_backup: Callable[[Path], dict], *, project: str, actor_id: str,
               restore_receipt: dict,
               verify_quiescence: Callable[[], None],
               before_commit: Callable[[], None] | None = None) -> dict:
    sealed = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if plan.get("schema_version") != 1 or digest(sealed) != plan.get("plan_sha256"):
        error("CLEANUP_PLAN_INVALID", "El plan cambió o usa un contrato desconocido.")
    if datetime.fromisoformat(plan["expires_at"]) <= datetime.now(UTC) or plan["scope"]["project"] != project:
        error("CLEANUP_PLAN_EXPIRED", "El plan expiró o pertenece a otra instalación.")
    storage, backup, quarantine = storage.resolve(strict=True), backup.resolve(strict=True), quarantine.resolve()
    if quarantine.exists() or quarantine.is_relative_to(storage) or storage.is_relative_to(quarantine):
        error("CLEANUP_QUARANTINE_INVALID", "Usa una cuarentena nueva fuera del almacenamiento operativo.")
    if (backup.is_relative_to(storage) or storage.is_relative_to(backup)
            or backup.is_relative_to(quarantine) or quarantine.is_relative_to(backup)):
        error("CLEANUP_BACKUP_PROTECTED", "El backup debe permanecer fuera de las rutas de limpieza y cuarentena.")
    manifest = verify_backup(backup)
    if manifest.get("source_project") != project or manifest.get("migration") != plan["scope"]["migration"]:
        error("CLEANUP_BACKUP_MISMATCH", "El backup verificado no corresponde al proyecto y revisión del plan.")
    state = json.loads((backup / "state.json").read_text(encoding="utf-8"))
    if state.get("tables") != plan["scope"]["state_tables"]:
        error("CLEANUP_BACKUP_MISMATCH", "El backup no conserva la población exacta del plan; captura uno nuevo.")
    manifest_sha = verify_recoverable_backup(backup, manifest, restore_receipt, plan["scope"])
    if db.in_transaction():
        error("CLEANUP_TRANSACTION_ACTIVE", "La aplicación requiere una sesión nueva sin transacciones pendientes.")
    moved = []
    committed = False
    durable_mkdir(quarantine, exist_ok=False)
    try:
        with db.begin():
            verify_quiescence()
            lock_metadata(db)
            scope = plan["scope"]
            current = compute(db, project, scope["organization_id"], scope["dataset_ids"], storage)
            if digest(current) != digest(scope):
                error("CLEANUP_PLAN_DRIFT", "Los registros, dependencias o archivos cambiaron; genera un plan nuevo.")
            actor = db.get(User, actor_id)
            from .permissions import effective_permissions
            if not actor or actor.organization_id != scope["organization_id"] or "users:manage" not in effective_permissions(db, actor):
                error("CLEANUP_OPERATOR_INVALID", "La limpieza requiere un administrador activo de la organización.")
            recovery = {"schema_version": 1, "plan_sha256": plan["plan_sha256"], "files": scope["files"],
                "nonce": plan["nonce"], "project": project, "organization_id": scope["organization_id"],
                "migration": scope["migration"], "storage": str(storage), "status": "PREPARED"}
            save_json(quarantine, "recovery.json", {**recovery, "journal_sha256": digest(recovery)})
            for entry in scope["files"]:
                source = storage / entry["path"]
                target = quarantine / "files" / entry["path"]
                durable_mkdir(target.parent)
                if file_entry(str(source), storage) != entry:
                    error("CLEANUP_PLAN_DRIFT", "Un archivo cambió durante la limpieza.")
                moved.append((source, target, entry))
                relocate(source, target, entry["sha256"])
            for name, identity in scope["delete_order"]:
                table = Base.metadata.tables[name]
                result = cast(CursorResult, db.execute(delete(table).where(table.c.id == identity)))
                if result.rowcount != 1:
                    error("CLEANUP_PLAN_DRIFT", "Un registro cambió durante la limpieza.")
            db.add(AuditEvent(organization_id=scope["organization_id"], event_type="OPERATIONAL_TEST_DATA_REMOVED",
                actor="Local operator", actor_type="USER", actor_id=actor_id, subject_type="operational_cleanup",
                subject_id=plan["nonce"], message="Borrado selectivo excepcional de datos operativos de prueba",
                metadata_json={"plan_sha256": plan["plan_sha256"], "removed_counts": scope["removed_counts"],
                               "protected_counts": scope["protected_counts"], "external_destinations_touched": False}))
            db.flush()
            if before_commit:
                before_commit()
            verify_quiescence()
        committed = True
        receipt = {"schema_version": 1, "status": "METADATA_COMMITTED_FILES_QUARANTINED", "project": project,
            "plan_sha256": plan["plan_sha256"], "backup_manifest_sha256": manifest_sha,
            "verified_restore_project": restore_receipt["target_project"],
            "removed_counts": scope["removed_counts"], "protected_counts": scope["protected_counts"],
            "exceptions": scope["exceptions"], "quarantined_files": len(moved), "external_destinations_touched": False}
        save_json(quarantine, "receipt.json", receipt)
        return receipt
    finally:
        if not committed:
            conflicts, restored = [], 0
            for source, target, entry in reversed(moved):
                try:
                    if target.exists() and not source.exists():
                        if file_entry(str(target), quarantine / "files") != entry:
                            error("CLEANUP_RECOVERY_CONFLICT", "Una copia movida cambió; requiere recuperación desde el backup.")
                        durable_mkdir(source.parent)
                        relocate(target, source, entry["sha256"])
                        restored += 1
                    elif source.is_file():
                        if file_entry(str(source), storage) != entry:
                            error("CLEANUP_RECOVERY_CONFLICT", "El archivo operativo cambió; conserva ambas copias para recuperación.")
                        if target.is_file():
                            # An interrupted cross-mount copy is removable only
                            # while its live original still matches the seal.
                            target.unlink()
                            sync_directory(target.parent)
                    else:
                        error("CLEANUP_RECOVERY_CONFLICT", "Falta un archivo operativo y su copia; requiere recuperación del backup.")
                except (OSError, OperationError):
                    conflicts.append(entry["path"])
            # Retain the recovery journal, even for rollback, so abrupt failures
            # have an explicit filesystem inventory for manual recovery.
            save_json(quarantine, "rollback.json", {"status": "RECOVERY_REQUIRED" if conflicts else "ROLLED_BACK",
                "files_restored": restored, "conflict_files": conflicts})
            if conflicts:
                error("CLEANUP_RECOVERY_REQUIRED", "La metadata revirtió; hay archivos en conflicto y el backup debe recuperarse antes de reiniciar servicios.")


def recover_quarantine(db: Session, quarantine: Path, storage: Path, *, project: str,
                       verify_quiescence: Callable[[], None]) -> dict:
    """Restore interrupted precommit moves; committed removals remain quarantined.

    A fresh verified DB is authoritative: the committed audit marker prevents
    resurrection after commit. Every restored path must still have a live metadata
    reference, remain under the configured store, and match the journal hash.
    """
    if db.in_transaction():
        error("CLEANUP_TRANSACTION_ACTIVE", "La recuperación requiere una sesión nueva sin transacciones pendientes.")
    with db.begin():
        verify_quiescence()
        lock_metadata(db)
        result = _recover_locked(db, quarantine, storage, project=project)
        verify_quiescence()
        return result


def _recover_locked(db: Session, quarantine: Path, storage: Path, *, project: str) -> dict:
    storage, quarantine = storage.resolve(strict=True), quarantine.resolve(strict=True)
    if quarantine.is_relative_to(storage) or storage.is_relative_to(quarantine):
        error("CLEANUP_QUARANTINE_INVALID", "La cuarentena no puede contener el almacenamiento operativo.")
    journal_path = quarantine / "recovery.json"
    if journal_path.is_symlink():
        error("CLEANUP_RECOVERY_INVALID", "El inventario de recuperación no puede ser un enlace.")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    sealed = {key: value for key, value in journal.items() if key != "journal_sha256"}
    if (journal.get("schema_version") != 1 or journal.get("project") != project or journal.get("storage") != str(storage)
            or not journal.get("organization_id") or journal.get("journal_sha256") != digest(sealed)):
        error("CLEANUP_RECOVERY_INVALID", "La cuarentena pertenece a otro almacén o proyecto.")
    marker = db.scalar(select(AuditEvent).where(AuditEvent.event_type == "OPERATIONAL_TEST_DATA_REMOVED",
        AuditEvent.subject_id == journal["nonce"]))
    if marker:
        if marker.organization_id != journal["organization_id"] or marker.metadata_json.get("plan_sha256") != journal["plan_sha256"]:
            error("CLEANUP_RECOVERY_INVALID", "El marcador de commit no corresponde al journal sellado.")
        return {"status": "METADATA_COMMITTED_FILES_QUARANTINED", "files_restored": 0}
    rows, _, _ = population(db)
    if journal.get("migration") != "0019_governance_people" or list(db.scalars(text("SELECT version_num FROM alembic_version"))) != [journal["migration"]]:
        error("CLEANUP_SCHEMA_DRIFT", "La recuperación requiere la misma revisión certificada del journal.")
    live = {(Path(row[field]) if Path(row[field]).is_absolute() else storage / row[field]).resolve()
            for name, items in rows.items() for row in items.values() for field in PATH_FIELDS.get(name, ())
            if row.get(field) and row.get("organization_id") == journal["organization_id"]}
    prepared = []
    retained_copies = 0
    for entry in journal["files"]:
        path = Path(entry["path"])
        source, target = storage / path, quarantine / "files" / path
        if path.is_absolute() or ".." in path.parts or source.resolve() not in live or not source.resolve().is_relative_to(storage):
            error("CLEANUP_RECOVERY_INVALID", "Una ruta de recuperación no tiene una referencia operativa verificable.")
        if target.exists():
            if source.exists():
                if file_entry(str(source), storage) != entry:
                    error("CLEANUP_RECOVERY_CONFLICT", "El archivo operativo difiere de la copia de recuperación.")
                # Interrupted cross-mount copy: the intact live original proves
                # rollback. Keep any partial copy in quarantine for inspection.
                retained_copies += 1
            else:
                if file_entry(str(target), quarantine / "files") != entry:
                    error("CLEANUP_RECOVERY_CONFLICT", "Un archivo de cuarentena cambió o está incompleto.")
                prepared.append((source, target, entry["sha256"]))
        elif not source.exists() or file_entry(str(source), storage) != entry:
            error("CLEANUP_RECOVERY_CONFLICT", "Un archivo de recuperación no está disponible con su hash original.")
    for source, target, expected_hash in prepared:
        durable_mkdir(source.parent)
        relocate(target, source, expected_hash)
    receipt = {"status": "ROLLED_BACK", "files_restored": len(prepared), "retained_copy_files": retained_copies,
        "plan_sha256": journal["plan_sha256"]}
    save_json(quarantine, "rollback.json", receipt)
    return receipt
