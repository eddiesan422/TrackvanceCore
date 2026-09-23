"""Safe Docker state backup, fresh restore and explicitly planned reset.

The utility never discovers resources by name prefix for deletion. It requires
Docker Compose ownership labels, records exact resource identities in a short-
lived reset plan, and revalidates that inventory immediately before removal.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = ROOT / "scripts" / "verify_storage.py"
BACKUP_SCHEMA_VERSION = 2
LEGACY_BACKUP_SCHEMA_VERSION = 1
SUPPORTED_BACKUP_SCHEMA_VERSIONS = {
    LEGACY_BACKUP_SCHEMA_VERSION,
    BACKUP_SCHEMA_VERSION,
}
VERIFY_SCHEMA_VERSION = 3
LEGACY_VERIFY_SCHEMA_VERSION = 2
LEGACY_MIGRATION = "0007_monitor_scheduling"
CURRENT_MIGRATION = "0008_data_delivery"
RESET_SCHEMA_VERSION = 1
PROJECT_PATTERN = re.compile(r"trackvance-[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
PRIMARY_SERVICES = frozenset({"postgres", "api", "worker", "delivery-worker", "web"})
OPTIONAL_SERVICES = frozenset({"scheduler"})
PRIMARY_VOLUMES = frozenset(
    {
        "postgres_data",
        "trackvance_data",
        "connection_credentials",
        "connection_keys",
        "delivery_credentials",
        "delivery_keys",
    }
)
ARCHIVED_VOLUMES = (
    "trackvance_data",
    "connection_credentials",
    "connection_keys",
    "delivery_credentials",
    "delivery_keys",
)
LEGACY_ARCHIVED_VOLUMES = (
    "trackvance_data",
    "connection_credentials",
    "connection_keys",
)
DELIVERY_TABLES = frozenset(
    {
        "delivery_destinations",
        "delivery_destination_versions",
        "delivery_attempts",
    }
)
# Frozen from ``Base.metadata.tables`` at the certified v0.4.1 revision.  Do not
# derive this compatibility contract from the current model: future tables must
# not make an authentic 0.4.1 fingerprint appear complete.
LEGACY_STATE_TABLES = frozenset(
    {
        "artifact_links",
        "artifacts",
        "audit_events",
        "configurations",
        "dataset_source_bindings",
        "dataset_versions",
        "datasets",
        "exception_attachments",
        "exceptions",
        "external_connection_versions",
        "external_connections",
        "findings",
        "idempotency_keys",
        "jobs",
        "metric_history",
        "monitor_occurrences",
        "monitor_schedule_versions",
        "monitor_schedules",
        "runs",
        "sessions",
        "users",
    }
)
LEGACY_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "tables",
        "verified_artifacts",
        "verified_secrets",
        "validated_relationships",
        "migration",
    }
)
SHA256_PATTERN = re.compile(r"[a-f0-9]{64}")
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
VOLUME_LABEL = "com.docker.compose.volume"


class OperationError(ValueError):
    """Safe operational error whose message contains no command output or secrets."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def execute(
    arguments: Sequence[str],
    *,
    timeout: int = 300,
    text: bool = True,
    environment: Mapping[str, str] | None = None,
) -> str | bytes:
    try:
        result = subprocess.run(
            list(arguments),
            cwd=ROOT,
            capture_output=True,
            check=False,
            timeout=timeout,
            text=text,
            env={**os.environ, **environment} if environment else None,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise OperationError(f"No fue posible ejecutar {arguments[0]} ({type(error).__name__}).")
    if result.returncode:
        raise OperationError(f"Falló {arguments[0]} {arguments[1] if len(arguments) > 1 else ''}.")
    return result.stdout


def execute_stream(
    arguments: Sequence[str], *, stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None, timeout: int = 1800,
) -> None:
    """Transfer archives through files without buffering secrets or payloads in RAM."""
    try:
        result = subprocess.run(
            list(arguments), cwd=ROOT, stdin=stdin or subprocess.DEVNULL,
            stdout=stdout or subprocess.DEVNULL, stderr=subprocess.PIPE,
            check=False, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise OperationError(f"No fue posible transferir el archivo ({type(error).__name__}).") from error
    if result.returncode:
        raise OperationError("Falló la transferencia del archivo Docker; salida suprimida.")


def docker_json(arguments: Sequence[str]) -> Any:
    output = execute(["docker", *arguments])
    try:
        return json.loads(str(output))
    except json.JSONDecodeError as error:
        raise OperationError("Docker devolvió un inventario inválido.") from error


def validate_project(project: str) -> str:
    if not PROJECT_PATTERN.fullmatch(project):
        raise OperationError(
            "El proyecto debe usar un nombre explícito trackvance-... en minúsculas."
        )
    return project


def _inspect_many(kind: str, identifiers: list[str]) -> list[dict[str, Any]]:
    if not identifiers:
        return []
    return docker_json([kind, "inspect", *identifiers])


def inventory(project: str) -> dict[str, Any]:
    project = validate_project(project)
    filters = ["--filter", f"label={PROJECT_LABEL}={project}", "-q"]
    container_ids = str(execute(["docker", "ps", "-a", *filters])).split()
    volume_names = str(execute(["docker", "volume", "ls", *filters])).split()
    network_ids = str(execute(["docker", "network", "ls", *filters])).split()

    containers = []
    for item in _inspect_many("container", container_ids):
        labels = item.get("Config", {}).get("Labels") or {}
        if labels.get(PROJECT_LABEL) != project:
            raise OperationError("Docker devolvió un contenedor fuera del proyecto solicitado.")
        service = labels.get(SERVICE_LABEL)
        if not service:
            raise OperationError("Un contenedor del proyecto no tiene etiqueta de servicio Compose.")
        containers.append(
            {
                "id": item["Id"],
                "name": item["Name"].lstrip("/"),
                "service": service,
                "image_id": item.get("Image", ""),
                "running": bool(item.get("State", {}).get("Running")),
            }
        )

    volumes = []
    for item in _inspect_many("volume", volume_names):
        labels = item.get("Labels") or {}
        if labels.get(PROJECT_LABEL) != project:
            raise OperationError("Docker devolvió un volumen fuera del proyecto solicitado.")
        logical = labels.get(VOLUME_LABEL)
        if not logical:
            raise OperationError("Un volumen del proyecto no tiene identidad lógica Compose.")
        volumes.append({"name": item["Name"], "logical_name": logical})

    networks = []
    for item in _inspect_many("network", network_ids):
        labels = item.get("Labels") or {}
        if labels.get(PROJECT_LABEL) != project:
            raise OperationError("Docker devolvió una red fuera del proyecto solicitado.")
        networks.append({"id": item["Id"], "name": item["Name"]})

    return {
        "project": project,
        "containers": sorted(containers, key=lambda value: (value["service"], value["id"])),
        "volumes": sorted(volumes, key=lambda value: (value["logical_name"], value["name"])),
        "networks": sorted(networks, key=lambda value: value["id"]),
    }


def _resource_identity(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "project": state["project"],
        "containers": [
            {key: item[key] for key in ("id", "name", "service")}
            for item in state["containers"]
        ],
        "volumes": list(state["volumes"]),
        "networks": list(state["networks"]),
    }


def _by_key(items: Iterable[Mapping[str, Any]], key: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for item in items:
        value = str(item[key])
        if value in result:
            raise OperationError(f"Identidad Docker duplicada para {value}.")
        result[value] = item
    return result


def require_backup_inventory(state: Mapping[str, Any]) -> None:
    services = _by_key(state["containers"], "service")
    volumes = _by_key(state["volumes"], "logical_name")
    missing_services = sorted(PRIMARY_SERVICES - services.keys())
    missing_volumes = sorted(PRIMARY_VOLUMES - volumes.keys())
    unsupported_volumes = sorted(set(volumes) - PRIMARY_VOLUMES)
    if missing_services or missing_volumes:
        raise OperationError(
            "El proyecto no tiene el conjunto persistente completo de Trackvance."
        )
    if unsupported_volumes:
        raise OperationError(
            "El proyecto contiene volúmenes ajenos al respaldo base: "
            + ", ".join(unsupported_volumes)
        )
    unsupported_services = set(services) - PRIMARY_SERVICES - OPTIONAL_SERVICES
    if unsupported_services:
        raise OperationError(
            "El proyecto contiene servicios fuera del despliegue base: "
            + ", ".join(sorted(unsupported_services))
        )


def _new_directory(path: Path) -> Path:
    path = path.absolute()
    if path.exists() or path.is_symlink():
        raise OperationError("El destino ya existe; se requiere un directorio nuevo.")
    current = path.parent
    while not current.exists():
        current = current.parent
    if current.is_symlink():
        raise OperationError("El destino no puede atravesar enlaces simbólicos.")
    path.mkdir(parents=True, mode=0o700)
    return path.resolve(strict=True)


def _new_file(path: Path) -> Path:
    path = path.absolute()
    if path.exists() or path.is_symlink():
        raise OperationError("El archivo de salida ya existe.")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise OperationError("El destino no puede atravesar enlaces simbólicos.")
    return path


def _container_for(state: Mapping[str, Any], service: str) -> Mapping[str, Any]:
    matches = [item for item in state["containers"] if item["service"] == service]
    if len(matches) != 1:
        raise OperationError(f"Se esperaba exactamente un contenedor para {service}.")
    return matches[0]


def _volume_for(state: Mapping[str, Any], logical: str) -> str:
    matches = [item["name"] for item in state["volumes"] if item["logical_name"] == logical]
    if len(matches) != 1:
        raise OperationError(f"Se esperaba exactamente un volumen para {logical}.")
    return str(matches[0])


ARCHIVE_PROGRAM = r"""
import os, stat, sys, tarfile
root = '/source'
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz', format=tarfile.PAX_FORMAT) as archive:
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories.sort(); files.sort()
        for name in directories + files:
            path = os.path.join(current, name)
            mode = os.lstat(path).st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise SystemExit('unsupported volume entry')
            archive.add(path, arcname=os.path.relpath(path, root), recursive=False)
"""

EXTRACT_PROGRAM = r"""
import os, shutil, sys, tarfile
root = '/target'
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|gz') as archive:
    for member in archive:
        parts = member.name.replace('\\', '/').split('/')
        if not member.name or member.name.startswith('/') or '..' in parts:
            raise SystemExit('unsafe archive path')
        target = os.path.join(root, *parts)
        if member.isdir():
            os.makedirs(target, exist_ok=True); os.chmod(target, member.mode & 0o777)
        elif member.isfile():
            os.makedirs(os.path.dirname(target), exist_ok=True)
            source = archive.extractfile(member)
            if source is None: raise SystemExit('missing archive entry')
            with source, open(target, 'xb') as output: shutil.copyfileobj(source, output)
            os.chmod(target, member.mode & 0o777)
        else:
            raise SystemExit('unsupported archive entry')
"""


def _python_payload(program: str) -> str:
    encoded = base64.b64encode(program.encode()).decode()
    return f"import base64;exec(base64.b64decode('{encoded}'))"


def inspect_archive(path: Path) -> dict[str, Any]:
    entries: dict[str, Any] = {}
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                normalized = member.name.replace("\\", "/")
                pure = PurePosixPath(normalized)
                if (
                    not normalized
                    or pure.is_absolute()
                    or ".." in pure.parts
                    or member.issym()
                    or member.islnk()
                    or not (member.isdir() or member.isfile())
                    or normalized in entries
                ):
                    raise OperationError("El archivo de volumen contiene una entrada insegura.")
                entries[normalized] = {
                    "type": "directory" if member.isdir() else "file",
                    "size_bytes": member.size,
                    "mode": member.mode & 0o777,
                }
    except (OSError, tarfile.TarError) as error:
        raise OperationError("No se pudo validar un archivo de volumen.") from error
    return dict(sorted(entries.items()))


def _archive_volume(
    volume: str, logical: str, image_id: str, destination: Path
) -> dict[str, Any]:
    relative = f"volumes/{logical}.tar.gz"
    target = destination / relative
    # Host ownership is independent of the non-root UID in the API image.
    # No bind mount, permission escalation or complete in-memory archive is needed.
    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
        execute_stream([
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--mount",
            f"type=volume,src={volume},dst=/source,readonly",
            image_id,
            "python",
            "-c",
            _python_payload(ARCHIVE_PROGRAM),
        ], stdout=output)
    return {
        "path": relative,
        "sha256": digest(target),
        "size_bytes": target.stat().st_size,
        "entries": inspect_archive(target),
    }


def _stop_services(state: Mapping[str, Any], services: Iterable[str]) -> list[str]:
    stopped = []
    for service in services:
        try:
            container = _container_for(state, service)
        except OperationError:
            continue
        if container["running"]:
            execute(["docker", "stop", str(container["id"])], timeout=180)
            stopped.append(str(container["id"]))
    return stopped


def _restart_containers(state: Mapping[str, Any], identifiers: set[str]) -> None:
    order = ("postgres", "api", "worker", "delivery-worker", "scheduler", "web")
    for service in order:
        for container in state["containers"]:
            if container["service"] == service and container["id"] in identifiers:
                execute(["docker", "start", str(container["id"])], timeout=180)


def _copy_snapshot(
    api_id: str, destination: Path, *, command: str = "snapshot"
) -> dict[str, Any]:
    if command not in {"snapshot", "snapshot-legacy-v2"}:
        raise OperationError("Comando de huella persistente no reconocido.")
    execute(["docker", "cp", str(VERIFY_SCRIPT), f"{api_id}:/tmp/verify_storage.py"])
    output = execute(
        ["docker", "exec", api_id, "python", "/tmp/verify_storage.py", command],
        timeout=600,
    )
    try:
        value = json.loads(str(output))
    except json.JSONDecodeError as error:
        raise OperationError("La verificación persistente no produjo JSON válido.") from error
    destination.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    return value


def backup(project: str, destination: Path) -> Path:
    state = inventory(project)
    require_backup_inventory(state)
    destination = _new_directory(destination)
    (destination / "volumes").mkdir(mode=0o700)
    initially_running = {item["id"] for item in state["containers"] if item["running"]}
    postgres = _container_for(state, "postgres")
    api = _container_for(state, "api")
    if not postgres["running"] or not api["running"]:
        raise OperationError("PostgreSQL y API deben estar activos para tomar el respaldo.")
    try:
        _stop_services(state, ("web", "scheduler", "delivery-worker", "worker"))
        snapshot_state = _copy_snapshot(str(api["id"]), destination / "state.json")
        _stop_services(state, ("api",))

        remote_dump = f"/tmp/trackvance-{uuid4().hex}.dump"
        dump_command = (
            'pg_dump --format=custom --no-owner --no-acl '
            '--username="$POSTGRES_USER" --dbname="$POSTGRES_DB" '
            f"--file={remote_dump}"
        )
        execute(["docker", "exec", str(postgres["id"]), "sh", "-eu", "-c", dump_command],
                timeout=1800)
        try:
            execute(
                ["docker", "exec", str(postgres["id"]), "pg_restore", "--list", remote_dump],
                timeout=300,
            )
            execute(
                ["docker", "cp", f"{postgres['id']}:{remote_dump}",
                 str(destination / "postgres.dump")],
                timeout=600,
            )
        finally:
            execute(["docker", "exec", str(postgres["id"]), "rm", "-f", remote_dump])

        components: dict[str, Any] = {}
        for relative in ("state.json", "postgres.dump"):
            path = destination / relative
            path.chmod(0o600)
            components[relative] = {
                "path": relative,
                "sha256": digest(path),
                "size_bytes": path.stat().st_size,
            }
        for logical in ARCHIVED_VOLUMES:
            archive = _archive_volume(
                _volume_for(state, logical), logical, str(api["image_id"]), destination
            )
            components[archive["path"]] = archive

        postgres_version = str(
            execute(
                ["docker", "exec", str(postgres["id"]), "postgres", "--version"]
            )
        ).strip()
        try:
            git_revision = str(execute(["git", "rev-parse", "HEAD"])).strip()
        except OperationError:
            git_revision = "unavailable"
        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "created_at": iso(utcnow()),
            "source_project": project,
            "consistency": "quiesced",
            "migration": snapshot_state.get("migration"),
            "postgres_version": postgres_version,
            "git_revision": git_revision,
            "images": {
                item["service"]: item["image_id"]
                for item in state["containers"]
                if item["service"] in PRIMARY_SERVICES | OPTIONAL_SERVICES
            },
            "components": dict(sorted(components.items())),
        }
        manifest_path = destination / "backup-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        manifest_path.chmod(0o600)
        verify_backup(destination)
        return manifest_path
    finally:
        _restart_containers(state, initially_running)


def _safe_component_path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise OperationError("El manifest contiene una ruta insegura.")
    candidate = root.joinpath(*pure.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise OperationError("Falta un componente declarado por el manifest.") from error
    if candidate.is_symlink() or not resolved.is_relative_to(root) or not resolved.is_file():
        raise OperationError("Un componente del respaldo sale de su directorio.")
    return resolved


def archived_volumes_for_backup(schema_version: int) -> tuple[str, ...]:
    if type(schema_version) is not int:
        raise OperationError("Versión de manifest no reconocida.")
    if schema_version == BACKUP_SCHEMA_VERSION:
        return ARCHIVED_VOLUMES
    if schema_version == LEGACY_BACKUP_SCHEMA_VERSION:
        return LEGACY_ARCHIVED_VOLUMES
    raise OperationError("Versión de manifest no reconocida.")


def validate_legacy_state(state: Mapping[str, Any]) -> None:
    """Require the complete, frozen fingerprint contract emitted by v0.4.1."""

    if set(state) != LEGACY_STATE_FIELDS:
        raise OperationError("La huella 0.4.1 no tiene el inventario esperado.")
    tables = state.get("tables")
    if not isinstance(tables, Mapping) or set(tables) != LEGACY_STATE_TABLES:
        raise OperationError("La huella 0.4.1 no tiene el inventario esperado.")
    for table_hashes in tables.values():
        if not isinstance(table_hashes, Mapping):
            raise OperationError("La huella 0.4.1 contiene hashes inválidos.")
        for identifier, row_hash in table_hashes.items():
            if (
                not isinstance(identifier, str)
                or not identifier
                or not isinstance(row_hash, str)
                or SHA256_PATTERN.fullmatch(row_hash) is None
            ):
                raise OperationError("La huella 0.4.1 contiene hashes inválidos.")
    for field in (
        "verified_artifacts",
        "verified_secrets",
        "validated_relationships",
    ):
        value = state.get(field)
        if type(value) is not int or value < 0:
            raise OperationError("La huella 0.4.1 contiene contadores inválidos.")
    if state["verified_artifacts"] != len(tables["artifacts"]):
        raise OperationError("La huella 0.4.1 contiene contadores inválidos.")
    if state["verified_secrets"] != len(tables["external_connection_versions"]):
        raise OperationError("La huella 0.4.1 contiene contadores inválidos.")


def verify_backup(source: Path) -> dict[str, Any]:
    try:
        if source.is_symlink():
            raise OperationError("El origen de respaldo no puede ser un enlace simbólico.")
        root = source.resolve(strict=True)
        if not root.is_dir():
            raise OperationError("El origen de respaldo no es un directorio regular.")
        manifest_path = root / "backup-manifest.json"
        if manifest_path.is_symlink():
            raise OperationError("El manifest no puede ser un enlace simbólico.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise OperationError("No fue posible leer el manifest del respaldo.") from error
    if not isinstance(manifest, Mapping):
        raise OperationError("El manifest del respaldo no tiene una estructura válida.")
    schema_version = manifest.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version not in SUPPORTED_BACKUP_SCHEMA_VERSIONS
    ):
        raise OperationError("Versión de manifest no reconocida.")
    if manifest.get("consistency") != "quiesced":
        raise OperationError("El respaldo no declara una captura quiescente.")
    components = manifest.get("components")
    if not isinstance(components, dict):
        raise OperationError("El manifest no enumera sus componentes.")
    archived_volumes = archived_volumes_for_backup(schema_version)
    expected = {
        "state.json",
        "postgres.dump",
        *(f"volumes/{name}.tar.gz" for name in archived_volumes),
    }
    if set(components) != expected:
        raise OperationError("El conjunto de componentes del respaldo está incompleto.")

    entries = list(root.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise OperationError("El respaldo no puede contener enlaces simbólicos.")
    actual_files = {
        path.relative_to(root).as_posix() for path in entries if path.is_file()
    }
    if actual_files != expected | {"backup-manifest.json"}:
        raise OperationError("El respaldo contiene archivos no declarados.")
    for relative, record in components.items():
        if not isinstance(record, Mapping) or record.get("path") != relative:
            raise OperationError("El manifest contiene un componente inválido.")
        path = _safe_component_path(root, relative)
        if (
            record.get("sha256") != digest(path)
            or record.get("size_bytes") != path.stat().st_size
        ):
            raise OperationError("La integridad SHA-256 de un componente no coincide.")
        if (
            relative.startswith("volumes/")
            and record.get("entries") != inspect_archive(path)
        ):
            raise OperationError("El inventario interno de un volumen no coincide.")
    if not (root / "postgres.dump").read_bytes().startswith(b"PGDMP"):
        raise OperationError("El dump PostgreSQL no usa el formato custom esperado.")
    try:
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OperationError("La huella persistente no es JSON válido.") from error
    expected_state_schema = (
        VERIFY_SCHEMA_VERSION
        if schema_version == BACKUP_SCHEMA_VERSION
        else LEGACY_VERIFY_SCHEMA_VERSION
    )
    if not isinstance(state, Mapping):
        raise OperationError("La huella persistente no tiene una estructura válida.")
    if state.get("schema_version") != expected_state_schema or state.get(
        "migration"
    ) != manifest.get("migration"):
        raise OperationError("La huella persistente no coincide con el manifest.")
    if (
        schema_version == LEGACY_BACKUP_SCHEMA_VERSION
        and manifest.get("migration") != LEGACY_MIGRATION
    ):
        raise OperationError("El backup v1 no corresponde a la baseline 0.4.1 soportada.")
    if schema_version == LEGACY_BACKUP_SCHEMA_VERSION:
        validate_legacy_state(state)
    return manifest


def stage_verified_backup(source: Path, destination: Path) -> dict[str, Any]:
    """Copy one verified backup into a private root and bind all later reads to it."""

    manifest = verify_backup(source)
    root = source.resolve(strict=True)
    try:
        destination.mkdir(mode=0o700)
        relative_files = ["backup-manifest.json", *sorted(manifest["components"])]
        for relative in relative_files:
            source_path = _safe_component_path(root, relative)
            pure = PurePosixPath(relative)
            target = destination.joinpath(*pure.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with source_path.open("rb") as input_stream, os.fdopen(
                os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
            ) as output_stream:
                shutil.copyfileobj(input_stream, output_stream)
                output_stream.flush()
                os.fsync(output_stream.fileno())
    except (OSError, KeyError, TypeError) as error:
        raise OperationError("No fue posible preparar una copia privada del respaldo.") from error

    staged_manifest = verify_backup(destination)
    if staged_manifest != manifest:
        raise OperationError("El respaldo cambió mientras se preparaba la restauración.")
    for path in destination.rglob("*"):
        if path.is_file():
            path.chmod(0o400)
    return staged_manifest


def validate_restored_state(
    manifest: Mapping[str, Any],
    expected_state: Mapping[str, Any],
    restored_state: Mapping[str, Any],
    normalized_legacy_state: Mapping[str, Any] | None = None,
) -> None:
    schema_version = manifest.get("schema_version")
    if schema_version == BACKUP_SCHEMA_VERSION:
        if restored_state != expected_state:
            raise OperationError("La huella restaurada no coincide con el respaldo.")
        return
    if schema_version != LEGACY_BACKUP_SCHEMA_VERSION:
        raise OperationError("Versión de manifest no reconocida.")

    # state schema 2 never recorded the live PostgreSQL catalog.  The only
    # backwards-compatible proof is therefore the exact v0.4.1 row fingerprint
    # reconstructed below; inventing a catalog hash here would reject authentic
    # backups.  A future backup schema must record that evidence at backup time.
    tables = restored_state.get("tables")
    delivery_empty = isinstance(tables, Mapping) and all(
        tables.get(table) == {} for table in DELIVERY_TABLES
    )
    if (
        restored_state.get("schema_version") != VERIFY_SCHEMA_VERSION
        or restored_state.get("migration") != CURRENT_MIGRATION
        or restored_state.get("verified_delivery_secrets") != 0
        or restored_state.get("verified_secrets")
        != restored_state.get("verified_source_secrets")
        or not delivery_empty
    ):
        raise OperationError("La migración segura del backup 0.4.1 no quedó verificada.")
    if normalized_legacy_state != expected_state:
        raise OperationError("La huella 0.4.1 normalizada no coincide con el respaldo.")


def ensure_fresh_project(project: str) -> None:
    state = inventory(project)
    if state["containers"] or state["volumes"] or state["networks"]:
        raise OperationError("El proyecto de restauración ya tiene recursos Docker.")
    for logical in PRIMARY_VOLUMES:
        candidate = f"{project}_{logical}"
        result = subprocess.run(
            ["docker", "volume", "inspect", candidate], capture_output=True, check=False
        )
        if result.returncode == 0:
            raise OperationError("Un volumen con el nombre de destino ya existe.")


def validate_postgres_dump(source: Path) -> None:
    execute(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--mount",
            f"type=bind,src={source.resolve()},dst=/backup,readonly",
            "postgres:16-alpine",
            "pg_restore",
            "--list",
            "/backup/postgres.dump",
        ],
        timeout=300,
    )


def compose(project: str, *arguments: str, environment: Mapping[str, str] | None = None) -> str:
    return str(
        execute(
            ["docker", "compose", "-p", project, "-f", str(ROOT / "compose.yml"), *arguments],
            timeout=1800,
            environment=environment,
        )
    )


def compose_services(project: str, environment: Mapping[str, str] | None = None) -> list[str]:
    return [
        value.strip()
        for value in compose(project, "config", "--services", environment=environment).splitlines()
        if value.strip()
    ]


def _extract_volume(
    backup_root: Path, archive: str, volume: str, image_id: str
) -> None:
    with (backup_root / archive).open("rb") as source:
        execute_stream([
            "docker",
            "run",
            "--rm",
            "--interactive",
            "--network",
            "none",
            "--read-only",
            "--mount",
            f"type=volume,src={volume},dst=/target",
            image_id,
            "python",
            "-c",
            _python_payload(EXTRACT_PROGRAM),
        ], stdin=source)


def _restore_verified(
    source: Path,
    manifest: Mapping[str, Any],
    receipt_directory: Path,
    target_project: str,
    *,
    start: bool = False,
    web_port: int = 3000,
    smoke: bool = False,
) -> dict[str, Any]:
    backup_schema_version = int(manifest["schema_version"])
    archived_volumes = archived_volumes_for_backup(backup_schema_version)
    target_project = validate_project(target_project)
    if target_project == manifest.get("source_project"):
        raise OperationError("La restauración requiere un proyecto diferente del origen.")
    validate_postgres_dump(source)
    ensure_fresh_project(target_project)
    if smoke and not start:
        raise OperationError("--smoke requiere --start.")
    environment = {
        "DEMO_SEED_ENABLED": "false",
        # --smoke is intended for an isolated demo-seeded recovery drill.
        "DEMO_ACCESS_ENABLED": "true" if smoke else "false",
        "WEB_PORT": str(web_port),
        "TRACKVANCE_WEB_ORIGIN": f"http://localhost:{web_port}",
    }
    receipt_path = _new_file(receipt_directory / f"restore-{target_project}.json")
    created = False
    try:
        available_services = compose_services(target_project, environment)
        required = PRIMARY_SERVICES | OPTIONAL_SERVICES.intersection(available_services)
        if not PRIMARY_SERVICES.issubset(available_services):
            raise OperationError("Compose no declara todos los servicios base requeridos.")
        compose(target_project, "up", "-d", "--wait", "postgres", environment=environment)
        created = True
        compose(
            target_project,
            "create",
            "--build",
            *(service for service in available_services if service != "postgres"),
            environment=environment,
        )
        state = inventory(target_project)
        volumes = _by_key(state["volumes"], "logical_name")
        if set(volumes) != PRIMARY_VOLUMES:
            raise OperationError("Compose no creó el conjunto de volúmenes esperado.")
        api = _container_for(state, "api")
        postgres = _container_for(state, "postgres")
        for logical in archived_volumes:
            _extract_volume(
                source.resolve(),
                f"volumes/{logical}.tar.gz",
                str(volumes[logical]["name"]),
                str(api["image_id"]),
            )

        remote_dump = f"/tmp/trackvance-{uuid4().hex}.dump"
        execute(
            ["docker", "cp", str(source.resolve() / "postgres.dump"),
             f"{postgres['id']}:{remote_dump}"],
            timeout=600,
        )
        restore_command = (
            'pg_restore --clean --if-exists --single-transaction --exit-on-error '
            '--no-owner --no-acl --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" '
            f"{remote_dump}"
        )
        try:
            execute(
                ["docker", "exec", str(postgres["id"]), "sh", "-eu", "-c", restore_command],
                timeout=1800,
            )
        finally:
            execute(["docker", "exec", str(postgres["id"]), "rm", "-f", remote_dump])

        compose(target_project, "up", "-d", "--wait", "api", environment=environment)
        restored = inventory(target_project)
        api = _container_for(restored, "api")
        with tempfile.TemporaryDirectory(prefix="trackvance-restored-state-") as temporary:
            restored_state = _copy_snapshot(
                str(api["id"]), Path(temporary) / "state.json"
            )
        expected_state = json.loads((source.resolve() / "state.json").read_text(encoding="utf-8"))
        normalized_legacy_state = None
        if backup_schema_version == LEGACY_BACKUP_SCHEMA_VERSION:
            with tempfile.TemporaryDirectory(
                prefix="trackvance-restored-legacy-state-"
            ) as temporary:
                normalized_legacy_state = _copy_snapshot(
                    str(api["id"]),
                    Path(temporary) / "state.json",
                    command="snapshot-legacy-v2",
                )
        validate_restored_state(
            manifest,
            expected_state,
            restored_state,
            normalized_legacy_state,
        )

        if start:
            services = [
                service
                for service in available_services
                if service not in {"postgres", "api"} and service in required
            ]
            compose(target_project, "up", "-d", "--wait", *services, environment=environment)
            execute(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "doctor.py"),
                    "--base-url",
                    f"http://127.0.0.1:{web_port}",
                    "--docker",
                    "--project",
                    target_project,
                ],
                timeout=300,
                environment=environment,
            )
            if smoke:
                execute(
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "smoke_test.py"),
                        "--base-url",
                        f"http://127.0.0.1:{web_port}",
                    ],
                    timeout=1800,
                    environment=environment,
                )
        else:
            compose(target_project, "stop", environment=environment)
        receipt = {
            "schema_version": 1,
            "restored_at": iso(utcnow()),
            "source_manifest_sha256": digest(source.resolve() / "backup-manifest.json"),
            "target_project": target_project,
            "verified_state_sha256": digest(source.resolve() / "state.json"),
            "status": "RUNNING_VERIFIED" if start else "STOPPED_VERIFIED",
        }
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
        return receipt
    except Exception as error:
        if created:
            try:
                compose(target_project, "stop", environment=environment)
            except OperationError:
                pass
        raise OperationError(
            "La restauración falló; el proyecto nuevo quedó detenido para diagnóstico."
        ) from error


def restore(
    source: Path,
    target_project: str,
    *,
    start: bool = False,
    web_port: int = 3000,
    smoke: bool = False,
) -> dict[str, Any]:
    try:
        receipt_directory = source.resolve(strict=True).parent
    except OSError as error:
        raise OperationError("No fue posible leer el origen de respaldo.") from error
    with tempfile.TemporaryDirectory(prefix="trackvance-verified-backup-") as temporary:
        staged_source = Path(temporary) / "backup"
        manifest = stage_verified_backup(source, staged_source)
        return _restore_verified(
            staged_source,
            manifest,
            receipt_directory,
            target_project,
            start=start,
            web_port=web_port,
            smoke=smoke,
        )


def create_reset_plan(project: str, output: Path, ttl_minutes: int = 15) -> dict[str, Any]:
    state = inventory(project)
    resources = _resource_identity(state)
    if not resources["containers"] and not resources["volumes"] and not resources["networks"]:
        raise OperationError("El proyecto no tiene recursos que eliminar.")
    created = utcnow()
    body = {
        "schema_version": RESET_SCHEMA_VERSION,
        "project": project,
        "created_at": iso(created),
        "expires_at": iso(created + timedelta(minutes=ttl_minutes)),
        "nonce": uuid4().hex,
        "resources": resources,
    }
    body["plan_sha256"] = canonical_hash(body)
    output = _new_file(output)
    output.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
    return body


def reset(plan_path: Path, confirmation: str) -> dict[str, Any]:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise OperationError("No fue posible leer el plan de reset.") from error
    expected_hash = plan.pop("plan_sha256", None)
    if plan.get("schema_version") != RESET_SCHEMA_VERSION:
        raise OperationError("Versión del plan de reset no reconocida.")
    if not isinstance(expected_hash, str) or canonical_hash(plan) != expected_hash:
        raise OperationError("El plan de reset fue modificado.")
    project = validate_project(str(plan.get("project", "")))
    try:
        expires = datetime.fromisoformat(str(plan["expires_at"]))
    except (KeyError, ValueError) as error:
        raise OperationError("El plan de reset no tiene una expiración válida.") from error
    if utcnow() > expires:
        raise OperationError("El plan de reset expiró; genera uno nuevo.")
    required = f"RESET:{project}:{expected_hash[:12]}"
    if confirmation != required:
        raise OperationError(f"Confirmación literal requerida: {required}")
    current = _resource_identity(inventory(project))
    if current != plan.get("resources"):
        raise OperationError("El inventario Docker cambió desde la creación del plan.")

    resources = plan["resources"]
    if resources["containers"]:
        execute(["docker", "rm", "-f", *(item["id"] for item in resources["containers"])])
    if resources["volumes"]:
        execute(["docker", "volume", "rm", *(item["name"] for item in resources["volumes"])])
    if resources["networks"]:
        execute(["docker", "network", "rm", *(item["id"] for item in resources["networks"])])
    receipt = {
        "schema_version": 1,
        "project": project,
        "plan_sha256": expected_hash,
        "completed_at": iso(utcnow()),
        "removed": {
            "containers": len(resources["containers"]),
            "volumes": len(resources["volumes"]),
            "networks": len(resources["networks"]),
        },
    }
    receipt_path = plan_path.with_name(plan_path.stem + "-receipt.json")
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    return receipt


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory_parser = commands.add_parser("inventory")
    inventory_parser.add_argument("--project", required=True)
    backup_parser = commands.add_parser("backup")
    backup_parser.add_argument("--project", required=True)
    backup_parser.add_argument("--destination", required=True, type=Path)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--source", required=True, type=Path)
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("--source", required=True, type=Path)
    restore_parser.add_argument("--target-project", required=True)
    restore_parser.add_argument("--start", action="store_true")
    restore_parser.add_argument("--smoke", action="store_true")
    restore_parser.add_argument("--web-port", type=int, default=3000)
    plan_parser = commands.add_parser("plan-reset")
    plan_parser.add_argument("--project", required=True)
    plan_parser.add_argument("--output", required=True, type=Path)
    plan_parser.add_argument("--ttl-minutes", type=int, default=15)
    reset_parser = commands.add_parser("reset")
    reset_parser.add_argument("--plan", required=True, type=Path)
    reset_parser.add_argument("--confirm", required=True)
    return parser.parse_args()


def main() -> int:
    options = parse_arguments()
    try:
        if options.command == "inventory":
            result = inventory(options.project)
        elif options.command == "backup":
            result = {"manifest": str(backup(options.project, options.destination))}
        elif options.command == "verify":
            result = verify_backup(options.source)
        elif options.command == "restore":
            if not 1 <= options.web_port <= 65535:
                raise OperationError("El puerto web no es válido.")
            result = restore(
                options.source,
                options.target_project,
                start=options.start,
                web_port=options.web_port,
                smoke=options.smoke,
            )
        elif options.command == "plan-reset":
            if not 1 <= options.ttl_minutes <= 60:
                raise OperationError("La vigencia del plan debe estar entre 1 y 60 minutos.")
            result = create_reset_plan(
                options.project, options.output, options.ttl_minutes
            )
            token = f"RESET:{options.project}:{result['plan_sha256'][:12]}"
            result = {**result, "required_confirmation": token,
                      "backup_recommended": True}
        else:
            result = reset(options.plan, options.confirm)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, OperationError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
