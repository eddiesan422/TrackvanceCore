"""Reproduce sibling imports from a fresh stdin process and preserve exact bytes."""

import ast
import base64
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "migration_transport.py"
spec = importlib.util.spec_from_file_location("migration_transport", SCRIPT)
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


def probe_sources(directory, *, fail=False):
    directory.mkdir()
    (directory / "physical_schema_guard.py").write_bytes("VALUE='Guarda café'\r\n".encode())
    (directory / "verify_storage.py").write_bytes(
        b"from pathlib import Path\r\nimport runpy\r\n"
        b"VALUE=runpy.run_path(str(Path(__file__).with_name('physical_schema_guard.py')))['VALUE']\r\n"
    )
    checker = (
        "import hashlib,json,sys\nfrom pathlib import Path\n"
        "import verify_storage\nfrom physical_schema_guard import VALUE\n"
        "assert __name__=='__main__'\nassert sys.argv==[__file__]\n"
        "root=Path(__file__).parent\nassert root.is_dir() and str(root)==sys.path[0]\n"
        "assert verify_storage.VALUE==VALUE=='Guarda café'\n"
    )
    if fail:
        checker += "raise RuntimeError('PROBE_CHECKER_FAILURE')\n"
    else:
        checker += (
            "print(json.dumps({'root':str(root),'status':'PASS',"
            "'hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() "
            "for p in root.glob('*.py')}}))\n"
        )
    (directory / "check_postgres_migrations.py").write_bytes(checker.encode())


def run_payload(payload):
    return subprocess.run([sys.executable, "-"], input=payload, encoding="utf-8",
                          capture_output=True, check=False)


def test_real_bundle_preserves_all_three_current_sources_byte_for_byte():
    payload = transport.migration_stdin_source()
    assignments = {node.targets[0].id: ast.literal_eval(node.value)
                   for node in ast.parse(payload).body
                   if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                   and node.targets[0].id in {"sources", "checks"}}
    assert set(assignments["sources"]) == set(transport.MIGRATION_SOURCES)
    for name in transport.MIGRATION_SOURCES:
        original = (SCRIPT.parent / name).read_bytes()
        assert base64.b64decode(assignments["sources"][name], validate=True) == original
        assert assignments["checks"][name] == hashlib.sha256(original).hexdigest()


def test_fresh_stdin_process_imports_siblings_runpy_and_real_file_then_cleans_only_own_directory(tmp_path):
    source, remote = tmp_path / "source", tmp_path / "remote"
    probe_sources(source)
    remote.mkdir()
    retained = remote / "unrelated-file"
    retained.write_bytes(b"retain exact bytes")
    payload = transport.migration_stdin_source(source, temporary_directory=str(remote))
    observed_roots = []
    for _ in range(2):
        completed = run_payload(payload)
        assert completed.returncode == 0, completed.stderr
        report = json.loads(completed.stdout)
        assert report["status"] == "PASS"
        observed_roots.append(report["root"])
        assert Path(report["root"]).parent == remote
        assert not Path(report["root"]).exists()
        assert report["hashes"] == {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                                    for name in transport.MIGRATION_SOURCES}
        assert list(remote.iterdir()) == [retained]
        assert retained.read_bytes() == b"retain exact bytes"
    assert observed_roots[0] != observed_roots[1]


@pytest.mark.parametrize("damaged", transport.MIGRATION_SOURCES)
def test_tampered_source_is_rejected_before_checker_execution_and_cleaned(tmp_path, damaged):
    source, remote = tmp_path / "source", tmp_path / "remote"
    probe_sources(source)
    remote.mkdir()
    payload = transport.migration_stdin_source(source, temporary_directory=str(remote))
    encoded = base64.b64encode((source / damaged).read_bytes()).decode("ascii")
    payload = payload.replace(encoded, base64.b64encode(b"# corrupted during transport\n").decode("ascii"))
    completed = run_payload(payload)
    assert completed.returncode != 0 and not completed.stdout
    assert "MIGRATION_TRANSPORT_HASH_MISMATCH" in completed.stderr
    assert list(remote.iterdir()) == []


def test_checker_failure_still_removes_only_the_private_directory(tmp_path):
    source, remote = tmp_path / "source", tmp_path / "remote"
    probe_sources(source, fail=True)
    remote.mkdir()
    retained = remote / "unrelated-directory"
    retained.mkdir()
    completed = run_payload(transport.migration_stdin_source(source, temporary_directory=str(remote)))
    assert completed.returncode != 0 and "PROBE_CHECKER_FAILURE" in completed.stderr
    assert list(remote.iterdir()) == [retained]
