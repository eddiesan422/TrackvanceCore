"""The observer rejects transient writes that an after-only listing would miss."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
SPEC = importlib.util.spec_from_file_location("reports_ephemeral_http", Path(__file__).with_name("reports_ephemeral_http.py"))
assert SPEC and SPEC.loader
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)


def test_real_descriptor_annotations_allow_only_ipc_and_read_only_files():
    report = observer.analyze_trace('''1710000000.1 openat(AT_FDCWD, "/selected.parquet", O_RDONLY|O_CLOEXEC) = 8</selected.parquet>
1710000000.2 write(3<pipe:[1234]>, ""..., 1000) = 1000
1710000000.3 writev(9<TCP:[127.0.0.1:80->127.0.0.1:4321]>, [...], 1) = 1000
1710000000.4 openat(AT_FDCWD, "/dev/null", O_RDWR|O_CLOEXEC) = 7</dev/null>
1710000000.5 mmap(NULL, 1000, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0) = 0x777
''')
    assert report["status"] == "PASS" and not report["violations"]
    assert report["observed_syscalls"] == 5
    assert "/selected" not in str(report) and report["raw_trace_published"] is False


@pytest.mark.parametrize("line,reason", [
    ('openat(AT_FDCWD, "/tmp/removed", O_WRONLY|O_CREAT|O_TRUNC, 0600) = 8</tmp/removed>', "writable_open"),
    ('openat(AT_FDCWD, "/var/cache/nginx/proxy_temp/123", O_RDWR|O_CREAT, 0600) = -1 EACCES', "writable_open"),
    ('write(8</tmp/removed (deleted)>, ""..., 1000) = 1000', "regular_or_unclassified_write"),
    ('write(8, ""..., 1000) = 1000', "regular_or_unclassified_write"),
    ('unlink("/tmp/removed") = 0', "filesystem_mutation"),
    ('mmap(NULL, 1000, PROT_READ|PROT_WRITE, MAP_SHARED, 8</tmp/removed>, 0) = 0x777', "shared_file_mapping"),
])
def test_during_execution_rejects_attempts_even_denied_or_deleted_later(line, reason):
    report = observer.analyze_trace("1710000000.2 " + line)
    assert report["status"] == "FAIL" and report["violations"][reason] == 1


def test_an_empty_trace_cannot_certify_a_flow():
    assert observer.analyze_trace("")["status"] == "FAIL"
