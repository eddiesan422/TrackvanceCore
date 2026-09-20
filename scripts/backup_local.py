"""Verified SQLite, ArtifactStore and SecretStore backups for direct execution.

This utility is for scripts/start-local.ps1. Docker/PostgreSQL backups use pg_dump
and the named persistent volumes, described in docs/development/operations.md.
"""

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

BACKUP_SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {1, BACKUP_SCHEMA_VERSION}
PORTABLE_DIRECTORIES = ("credentials", "keys")
MASTER_KEY = "keys/master.key"
LOCAL_SECRET_REFERENCE = re.compile(r"local:[a-f0-9]{32}")
PATH_COLUMNS = {
    "dataset_versions": ("original_path", "canonical_path"),
    "runs": ("result_path", "evidence_path"),
    "artifacts": ("path",),
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def path_entries(connection: sqlite3.Connection):
    tables = table_names(connection)
    for table, columns in PATH_COLUMNS.items():
        if table not in tables:
            continue
        for column in columns:
            # Identifiers come only from the fixed allowlist above.
            for identifier, value in connection.execute(f'SELECT id, "{column}" FROM "{table}"'):
                if value:
                    yield table, column, identifier, value


def connection_secret_files(connection: sqlite3.Connection) -> set[str]:
    """Return the portable credential paths required by the database snapshot."""
    if "external_connection_versions" not in table_names(connection):
        return set()
    required = set()
    query = (
        "SELECT organization_id, secret_reference FROM external_connection_versions "
        "WHERE secret_reference IS NOT NULL AND secret_reference != ''"
    )
    for organization_id, reference in connection.execute(query):
        if (
            not isinstance(organization_id, str)
            or not organization_id
            or not isinstance(reference, str)
            or not LOCAL_SECRET_REFERENCE.fullmatch(reference)
        ):
            raise ValueError("La base de datos contiene una referencia de credencial local inválida.")
        scope = hashlib.sha256(organization_id.encode()).hexdigest()
        required.add((Path("credentials") / scope / f"{reference[6:]}.secret").as_posix())
    return required


def fresh_destination(destination: Path, source: Path) -> Path:
    destination = destination.resolve()
    if destination == source or destination.is_relative_to(source):
        raise ValueError("El destino no puede estar dentro del origen.")
    if destination.exists():
        raise ValueError("El destino ya existe. Usa un directorio nuevo para conservar los datos.")
    destination.mkdir(parents=True)
    return destination


def runtime_directory(runtime: Path, name: str, *, required: bool) -> Path | None:
    candidate = runtime / name
    if not candidate.exists() and not candidate.is_symlink():
        if required:
            raise ValueError(f"Falta el directorio requerido del runtime: {name}")
        return None
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(runtime) or not resolved.is_dir():
        raise ValueError(f"El directorio {name} debe estar dentro del runtime.")
    return resolved


def directory_files(root: Path):
    for candidate in root.rglob("*"):
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise ValueError("Los directorios persistentes no pueden contener enlaces.")
        if candidate.is_file():
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise ValueError("Un archivo persistente apunta fuera de su directorio.")
            yield resolved


def copy_verified(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if source.stat().st_size != target.stat().st_size or digest(source) != digest(target):
        raise ValueError("Un archivo cambió durante la copia; conserva este intento para diagnóstico.")


def backup(runtime: Path, destination: Path) -> None:
    runtime = runtime.resolve(strict=True)
    database = (runtime / "trackvance.db").resolve(strict=True)
    if not database.is_relative_to(runtime) or not database.is_file():
        raise ValueError("La base de datos debe estar dentro del runtime.")
    storage = runtime_directory(runtime, "storage", required=True)
    assert storage is not None
    destination = fresh_destination(destination, runtime)
    with (
        closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(destination / "trackvance.db")) as snapshot,
    ):
        source.backup(snapshot)
        # A source using WAL transfers its journal setting. Consolidate the copy
        # into one portable DB before hashing; never back up live sidecar files.
        snapshot.execute("PRAGMA journal_mode=DELETE")
        if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("La base de datos no supera integrity_check.")
        artifact_paths = {Path(row[3]).resolve(strict=True) for row in path_entries(snapshot)}
    for path in sorted(artifact_paths):
        if not path.is_relative_to(storage) or not path.is_file():
            raise ValueError("Un artifact apunta fuera del almacenamiento declarado.")
        copy_verified(path, destination / "storage" / path.relative_to(storage))
    for directory_name in PORTABLE_DIRECTORIES:
        source_directory = runtime_directory(runtime, directory_name, required=False)
        if source_directory is None:
            continue
        for path in directory_files(source_directory):
            copy_verified(
                path,
                destination / directory_name / path.relative_to(source_directory),
            )
    files = {
        path.relative_to(destination).as_posix(): {
            "sha256": digest(path),
            "size_bytes": path.stat().st_size,
        }
        for path in destination.rglob("*")
        if path.is_file()
    }
    manifest = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "source_storage": str(storage),
        "files": files,
    }
    (destination / "backup-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    verify(destination)
    print(f"Backup verificado: {destination} ({len(files)} archivos)")


def manifest_files(source: Path, manifest: dict) -> dict[str, dict]:
    files = manifest.get("files")
    if not isinstance(files, dict) or "trackvance.db" not in files:
        raise ValueError("Formato de backup no reconocido.")
    validated = {}
    for name, expected in files.items():
        if not isinstance(name, str) or not name:
            raise ValueError("Ruta de backup inválida.")
        relative = Path(name)
        if (
            relative.is_absolute()
            or name != relative.as_posix()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError("Ruta de backup inválida.")
        try:
            path = (source / relative).resolve(strict=True)
        except OSError:
            raise ValueError(f"Integridad de archivo incorrecta: {name}") from None
        if not path.is_relative_to(source) or not path.is_file():
            raise ValueError("Ruta de backup inválida.")
        if (
            not isinstance(expected, dict)
            or not isinstance(expected.get("size_bytes"), int)
            or expected["size_bytes"] < 0
            or not isinstance(expected.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected["sha256"])
        ):
            raise ValueError("Metadatos de integridad inválidos.")
        try:
            valid_content = (
                path.stat().st_size == expected["size_bytes"]
                and digest(path) == expected["sha256"]
            )
        except OSError:
            valid_content = False
        if not valid_content:
            raise ValueError(f"Integridad de archivo incorrecta: {name}")
        validated[name] = expected
    return validated


def validate_connection_material(connection: sqlite3.Connection, file_names: set[str]) -> None:
    required_credentials = connection_secret_files(connection)
    if not required_credentials:
        return
    if MASTER_KEY not in file_names or not required_credentials.issubset(file_names):
        raise ValueError(
            "El backup no incluye las credenciales y la clave requeridas por Conexiones."
        )


def verify(source: Path) -> dict:
    source = source.resolve(strict=True)
    manifest = json.loads((source / "backup-manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError("Formato de backup no reconocido.")
    files = manifest_files(source, manifest)
    with closing(
        sqlite3.connect((source / "trackvance.db").as_uri() + "?mode=ro", uri=True)
    ) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Integridad de base de datos incorrecta.")
        source_storage = manifest.get("source_storage")
        if not isinstance(source_storage, str) or not source_storage:
            raise ValueError("Formato de backup no reconocido.")
        old_storage = Path(source_storage)
        for _, _, _, value in path_entries(connection):
            try:
                relative = Path(value).relative_to(old_storage)
            except (TypeError, ValueError):
                raise ValueError(
                    "Un artifact de la base de datos no pertenece al almacenamiento declarado."
                ) from None
            name = (Path("storage") / relative).as_posix()
            if name not in files:
                raise ValueError("Un artifact de la base de datos no está incluido en el backup.")
        validate_connection_material(connection, set(files))
    return manifest


def restore(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    manifest = verify(source)
    destination = fresh_destination(destination, source)
    for name in sorted(manifest["files"]):
        copy_verified(source / name, destination / name)
    old_storage = Path(manifest["source_storage"])
    new_storage = destination / "storage"
    for directory in (new_storage, destination / "credentials", destination / "keys"):
        directory.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(destination / "trackvance.db")) as connection:
        for table, column, identifier, value in list(path_entries(connection)):
            replacement = str(new_storage / Path(value).relative_to(old_storage))
            connection.execute(
                f'UPDATE "{table}" SET "{column}"=? WHERE id=?', (replacement, identifier)
            )
        connection.commit()
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("La restauración detectó referencias inconsistentes.")
        validate_connection_material(connection, set(manifest["files"]))
    for name, expected in manifest["files"].items():
        if name != "trackvance.db" and digest(destination / name) != expected["sha256"]:
            raise ValueError("La copia restaurada perdió integridad.")
    (destination / "restoration.json").write_text(
        json.dumps(
            {
                "restored_at": datetime.now(UTC).isoformat(),
                "backup": str(source),
                "database_sha256_before_path_relocation": manifest["files"]["trackvance.db"][
                    "sha256"
                ],
                "database_sha256": digest(destination / "trackvance.db"),
                "note": (
                    "Solo se reubicaron rutas internas; IDs, versiones, configuración, "
                    "credenciales cifradas y bytes de artifacts se conservaron."
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Restauración verificada en directorio nuevo: {destination}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["backup", "verify", "restore"])
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.operation != "verify" and args.destination is None:
        parser.error("--destination es obligatorio para backup/restore")
    try:
        if args.operation == "backup":
            backup(args.source, args.destination)
        elif args.operation == "restore":
            restore(args.source, args.destination)
        else:
            manifest = verify(args.source)
            print(f"Integridad verificada: {len(manifest['files'])} archivos")
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
