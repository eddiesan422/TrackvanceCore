"""Carry the unchanged migration checker and its local imports over stdin."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

MIGRATION_SOURCES = (
    "check_postgres_migrations.py",
    "verify_storage.py",
    "physical_schema_guard.py",
)


def migration_stdin_source(
    source_directory: Path | None = None, *, temporary_directory: str = "/tmp"
) -> str:
    """Return a stdlib bootstrap for a fresh Python process in the owned API.

    Only its newly created private directory is removed. The real checker runs
    as __main__ with a real __file__, so sibling imports and runpy remain valid.
    """
    source_directory = source_directory or Path(__file__).resolve().parent
    contents = {name: (source_directory / name).read_bytes() for name in MIGRATION_SOURCES}
    sources = {name: base64.b64encode(data).decode("ascii") for name, data in contents.items()}
    checks = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    return (
        "import base64,hashlib,pathlib,runpy,shutil,sys,tempfile\n"
        f"sources={sources!r}\nchecks={checks!r}\n"
        f"temporary_parent=pathlib.Path({temporary_directory!r}).resolve()\n"
        "root=pathlib.Path(tempfile.mkdtemp(prefix='trackvance-migration-',dir=temporary_parent))\n"
        "original_path=sys.path[:]\noriginal_argv=sys.argv[:]\n"
        "try:\n"
        "    for name,encoded in sources.items():\n"
        "        data=base64.b64decode(encoded,validate=True)\n"
        "        if hashlib.sha256(data).hexdigest()!=checks[name]:\n"
        "            raise SystemExit('MIGRATION_TRANSPORT_HASH_MISMATCH')\n"
        "        target=root/name\n"
        "        target.write_bytes(data)\n"
        "        if hashlib.sha256(target.read_bytes()).hexdigest()!=checks[name]:\n"
        "            raise SystemExit('MIGRATION_TRANSPORT_HASH_MISMATCH')\n"
        "    sys.path.insert(0,str(root))\n"
        "    sys.argv=[str(root/'check_postgres_migrations.py')]\n"
        "    runpy.run_path(sys.argv[0],run_name='__main__')\n"
        "finally:\n"
        "    sys.path[:]=original_path\n    sys.argv=original_argv\n"
        "    if root.resolve().parent!=temporary_parent or not root.name.startswith('trackvance-migration-'):\n"
        "        raise RuntimeError('MIGRATION_TRANSPORT_CLEANUP_SCOPE')\n"
        "    shutil.rmtree(root)\n"
    )
