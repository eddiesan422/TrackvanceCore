"""Verified SQLite + ArtifactStore backups, restored only into a new directory.

This utility is for scripts/start-local.ps1. Docker/PostgreSQL backups use pg_dump
and the named artifact volume, described in docs/development/operations.md.
"""

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

PATH_COLUMNS = {
    "dataset_versions": ("original_path", "canonical_path"),
    "runs": ("result_path", "evidence_path"),
    "artifacts": ("path",),
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def path_entries(connection: sqlite3.Connection):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, columns in PATH_COLUMNS.items():
        if table not in tables:
            continue
        for column in columns:
            # Identifiers come only from the fixed allowlist above.
            for identifier, value in connection.execute(f'SELECT id, "{column}" FROM "{table}"'):
                if value:
                    yield table, column, identifier, value


def fresh_destination(destination: Path, source: Path) -> Path:
    destination = destination.resolve()
    if destination == source or destination.is_relative_to(source):
        raise ValueError("El destino no puede estar dentro del origen.")
    if destination.exists():
        raise ValueError("El destino ya existe. Usa un directorio nuevo para conservar los datos.")
    destination.mkdir(parents=True)
    return destination


def backup(runtime: Path, destination: Path) -> None:
    runtime = runtime.resolve(strict=True)
    database = (runtime / "trackvance.db").resolve(strict=True)
    storage = (runtime / "storage").resolve(strict=True)
    destination = fresh_destination(destination, runtime)
    with (closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source,
          closing(sqlite3.connect(destination / "trackvance.db")) as snapshot):
        source.backup(snapshot)
        # A source using WAL transfers its journal setting. Consolidate the copy
        # into one portable DB before hashing; never back up live sidecar files.
        snapshot.execute("PRAGMA journal_mode=DELETE")
        if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("La base de datos no supera integrity_check.")
        paths = {Path(row[3]).resolve(strict=True) for row in path_entries(snapshot)}
    for path in sorted(paths):
        if not path.is_relative_to(storage) or not path.is_file():
            raise ValueError("Un artifact apunta fuera del almacenamiento declarado.")
        target = destination / "storage" / path.relative_to(storage)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        if digest(path) != digest(target):
            raise ValueError("Un artifact cambió durante la copia; conserva este intento para diagnóstico.")
    files = {
        path.relative_to(destination).as_posix(): {"sha256": digest(path), "size_bytes": path.stat().st_size}
        for path in destination.rglob("*") if path.is_file()
    }
    manifest = {"schema_version": 1, "created_at": datetime.now(UTC).isoformat(),
                "source_storage": str(storage), "files": files}
    (destination / "backup-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    verify(destination)
    print(f"Backup verificado: {destination} ({len(files)} archivos)")


def verify(source: Path) -> dict:
    source = source.resolve(strict=True)
    manifest = json.loads((source / "backup-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or "trackvance.db" not in manifest.get("files", {}):
        raise ValueError("Formato de backup no reconocido.")
    for name, expected in manifest["files"].items():
        path = (source / name).resolve(strict=True)
        if not path.is_relative_to(source) or not path.is_file():
            raise ValueError("Ruta de backup inválida.")
        if path.stat().st_size != expected["size_bytes"] or digest(path) != expected["sha256"]:
            raise ValueError(f"Integridad de archivo incorrecta: {name}")
    with closing(sqlite3.connect((source / "trackvance.db").as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Integridad de base de datos incorrecta.")
        old_storage = Path(manifest["source_storage"])
        for _, _, _, value in path_entries(connection):
            relative = Path(value).relative_to(old_storage)
            name = (Path("storage") / relative).as_posix()
            if name not in manifest["files"]:
                raise ValueError("Un artifact de la base de datos no está incluido en el backup.")
    return manifest


def restore(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    manifest = verify(source)
    destination = fresh_destination(destination, source)
    for name in manifest["files"]:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    old_storage = Path(manifest["source_storage"])
    new_storage = destination / "storage"
    new_storage.mkdir(exist_ok=True)
    with closing(sqlite3.connect(destination / "trackvance.db")) as connection:
        for table, column, identifier, value in list(path_entries(connection)):
            replacement = str(new_storage / Path(value).relative_to(old_storage))
            connection.execute(f'UPDATE "{table}" SET "{column}"=? WHERE id=?', (replacement, identifier))
        connection.commit()
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("La restauración detectó referencias inconsistentes.")
    for name, expected in manifest["files"].items():
        if name != "trackvance.db" and digest(destination / name) != expected["sha256"]:
            raise ValueError("La copia restaurada perdió integridad.")
    (destination / "restoration.json").write_text(json.dumps({
        "restored_at": datetime.now(UTC).isoformat(), "backup": str(source),
        "database_sha256_before_path_relocation": manifest["files"]["trackvance.db"]["sha256"],
        "database_sha256": digest(destination / "trackvance.db"),
        "note": "Solo se reubicaron rutas internas; IDs, versiones, configuración y bytes de artifacts se conservaron.",
    }, indent=2), encoding="utf-8")
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
