"""Reader fault injection verifies private diagnostics, not engine certification."""
import errno
import hashlib
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from trackvance import report_executor as executor
from trackvance.operations_common import OperationError

PRIVATE = "SECRET_CELL_password=synthetic SQL SELECT private_table"


class BrokenRead(io.BytesIO):
    def readline(self, size=-1):
        if self.tell() < len(self.getvalue()):
            return super().readline(size)
        raise OSError(errno.EIO, PRIVATE)


def diagnostic_records(caplog):
    records = [record for record in caplog.records if record.name == executor.__name__]
    assert all(record.exc_info is None and record.stack_info is None for record in records)
    assert all(PRIVATE not in record.getMessage() and len(record.getMessage()) < 1024 for record in records)
    return [json.loads(record.getMessage().split("REPORT_CHANNEL_DIAGNOSTIC ", 1)[1]) for record in records]


def process_fixture(monkeypatch, pipe, returncode):
    class Process:
        def __init__(self):
            self.stdin, self.stdout, self.returncode = io.BytesIO(), pipe, returncode
            self.killed = False

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed, self.returncode = True, -9

        def wait(self, timeout=None):
            return self.returncode

    child = Process()
    monkeypatch.setattr(executor.subprocess, "Popen", lambda *args, **kwargs: child)
    # This injects only the channel fault. It neither starts nor pretends to
    # certify a Linux engine, and it does not mutate the global host platform.
    monkeypatch.setattr(executor, "sys", SimpleNamespace(platform="linux", executable=sys.executable,
                                                         base_prefix=sys.base_prefix))
    return child


@pytest.mark.parametrize("line,exception_name", [
    (("\r50% " + PRIVATE + "\n").encode(), "JSONDecodeError"),
    (b'{"rows":[["\xc3', "UnicodeDecodeError"),
])
def test_malformed_reader_records_only_metadata_and_preserves_failure_cleanup(monkeypatch, caplog, line, exception_name):
    child = process_fixture(monkeypatch, io.BytesIO(line), -9)
    with caplog.at_level(logging.ERROR, logger=executor.__name__), pytest.raises(OperationError) as failure:
        list(executor.execute_messages([], {}, "DOWNLOAD"))
    assert failure.value.code == "REPORT_CHANNEL_FAILED"
    before, after = diagnostic_records(caplog)
    assert before["event"] == "reader_failure" and after["event"] == "child_reaped"
    assert before["exception_class"] == exception_name
    assert before["line_length"] == len(line) and before["line_newline"] == line.endswith(b"\n")
    assert before["line_cr_count"] == line.count(b"\r")
    assert before["line_sha256"] == hashlib.sha256(line).hexdigest()
    assert before["json_position"] == (3 if exception_name == "JSONDecodeError" else None)
    assert before["child_returncode"] == after["child_returncode"] == -9
    assert before["stop_requested"] is False
    assert not after["coordinator_killed"] and not child.killed and child.stdout.closed


def test_read_oserror_does_not_attribute_previous_private_batch_and_identifies_cleanup_kill(monkeypatch, caplog):
    line = json.dumps({"kind": "batch", "rows": [[PRIVATE]]}).encode() + b"\n"
    child = process_fixture(monkeypatch, BrokenRead(line), None)
    messages = executor.execute_messages([], {}, "DOWNLOAD")
    with caplog.at_level(logging.ERROR, logger=executor.__name__):
        assert next(messages)["rows"] == [[PRIVATE]]
        with pytest.raises(OperationError) as failure:
            next(messages)
    assert failure.value.code == "REPORT_CHANNEL_FAILED"
    before, after = diagnostic_records(caplog)
    assert before["exception_class"] == "OSError" and before["errno"] == errno.EIO
    assert all(before[key] is None for key in ("line_length", "line_newline", "line_cr_count", "line_sha256", "json_position"))
    assert before["child_returncode"] is None and after["child_returncode"] == -9
    assert before["stop_requested"] is False
    assert after["coordinator_killed"] and child.killed and child.stdout.closed


def test_stop_snapshot_distinguishes_pipe_close_without_logging_exception_message(caplog):
    with caplog.at_level(logging.ERROR, logger=executor.__name__):
        executor._log_channel_diagnostic(executor._channel_diagnostic(
            ValueError(PRIVATE), None, "DOWNLOAD", 0, stop_requested=True))
    value, = diagnostic_records(caplog)
    assert value["stop_requested"] is True and value["exception_class"] == "ValueError"
    assert value["line_length"] is None and value["child_returncode"] == 0


def test_creator_stays_alive_through_reaping_and_is_joined_after_completion(monkeypatch):
    child = process_fixture(monkeypatch, io.BytesIO(b'{"kind":"complete"}\n'), 0)
    observed = []

    def create(*args, **kwargs):
        observed.append(threading.current_thread())
        return child

    def wait(timeout=None):
        assert observed[0].is_alive()
        return 0

    monkeypatch.setattr(executor.subprocess, "Popen", create)
    monkeypatch.setattr(child, "wait", wait)
    assert list(executor.execute_messages([], {}, "DOWNLOAD")) == [{"kind": "complete"}]
    assert observed[0] is not threading.current_thread()
    assert not observed[0].is_alive() and child.stdout.closed


def test_failed_creation_releases_concurrency_slot_and_creator_thread(monkeypatch):
    process_fixture(monkeypatch, io.BytesIO(), 0)
    monkeypatch.setattr(executor, "_slots", threading.BoundedSemaphore(1))
    observed = []

    def failed_create(*args, **kwargs):
        observed.append(threading.current_thread())
        raise OSError(errno.ENOENT, "Synthetic absent interpreter")

    monkeypatch.setattr(executor.subprocess, "Popen", failed_create)
    with pytest.raises(OSError):
        list(executor.execute_messages([], {}, "DOWNLOAD"))
    assert not observed[0].is_alive()
    assert executor._slots.acquire(blocking=False)
    executor._slots.release()


def test_consumer_close_reaps_live_child_before_releasing_creator(monkeypatch):
    child = process_fixture(monkeypatch, io.BytesIO(b'{"kind":"schema"}\n'), None)
    creators = []

    def create(*args, **kwargs):
        creators.append(threading.current_thread())
        return child

    def wait(timeout=None):
        assert creators[0].is_alive() and child.killed
        return child.returncode

    monkeypatch.setattr(executor.subprocess, "Popen", create)
    monkeypatch.setattr(child, "wait", wait)
    messages = executor.execute_messages([], {}, "DOWNLOAD")
    assert next(messages) == {"kind": "schema"}
    messages.close()
    assert child.killed and child.returncode == -9 and child.stdout.closed
    assert not creators[0].is_alive()


def pdeath_child(tmp_path):
    """Only parent-death semantics; this toy does not certify engine confinement."""
    script = tmp_path / "parent-death-child.py"
    script.write_text('''
import ctypes, json, os, time
from pathlib import Path
payload = json.loads(input())
libc = ctypes.CDLL(None, use_errno=True)
parent = os.getppid()
assert libc.prctl(1, 9, 0, 0, 0) == 0 and os.getppid() == parent
print(json.dumps({"kind": "schema", "pid": os.getpid()}), flush=True)
deadline = time.monotonic() + 20
while not Path(payload["probe"]["release"]).exists():
    assert time.monotonic() < deadline
    time.sleep(0.01)
print(json.dumps({"kind": "batch", "rows": [["complete"]]}), flush=True)
print(json.dumps({"kind": "complete"}), flush=True)
''')
    return script


@pytest.mark.skipif(sys.platform != "linux", reason="Actual Linux parent-death thread semantics")
def test_parent_death_child_survives_transient_caller_and_different_consumer(tmp_path, monkeypatch):
    script, release = pdeath_child(tmp_path), tmp_path / "release"
    original, launched, creators = subprocess.Popen, [], []

    def create(command, **options):
        creators.append(threading.current_thread())
        process = original([*command[:-1], str(script)], **options)
        launched.append(process)
        return process

    monkeypatch.setattr(executor.subprocess, "Popen", create)
    messages = executor.execute_messages([], {}, "DOWNLOAD", probe={"release": str(release)})
    first, errors = [], []

    def caller():
        try:
            first.append(next(messages))
        except Exception as error:  # noqa: BLE001 - report a worker failure in the calling test
            errors.append(error)

    caller_thread = threading.Thread(target=caller)
    try:
        caller_thread.start()
        caller_thread.join(timeout=5)
        assert not caller_thread.is_alive() and not errors and first[0]["kind"] == "schema"
        assert launched[0].poll() is None and creators[0].is_alive()
        release.touch()
        assert list(messages) == [{"kind": "batch", "rows": [["complete"]]}, {"kind": "complete"}]
        assert launched[0].poll() == 0 and not creators[0].is_alive()
    finally:
        release.touch()
        caller_thread.join(timeout=5)
        messages.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Actual Linux coordinator death")
def test_dedicated_creator_retains_parent_death_kill_when_coordinator_dies(tmp_path):
    script, release = pdeath_child(tmp_path), tmp_path / "release"
    coordinator_script = '''
import json, sys, time
sys.path.insert(0, sys.argv[1])
from trackvance import report_executor as executor
original = executor.subprocess.Popen
def create(command, **options):
    return original([*command[:-1], sys.argv[2]], **options)
executor.subprocess.Popen = create
messages = executor.execute_messages([], {}, "DOWNLOAD", probe={"release": sys.argv[3]})
print(json.dumps(next(messages)), flush=True)
time.sleep(20)
'''
    coordinator = subprocess.Popen([sys.executable, "-I", "-B", "-c", coordinator_script,
        str(Path(__file__).resolve().parents[1] / "src"), str(script), str(release)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child_pid, child_start, child_stopped = None, None, False
    try:
        assert coordinator.stdout
        import select
        assert select.select([coordinator.stdout], [], [], 10)[0]
        schema = json.loads(coordinator.stdout.readline())
        child_pid = schema["pid"]
        assert schema["kind"] == "schema" and coordinator.poll() is None
        child_fields = Path(f"/proc/{child_pid}/stat").read_text().split(") ", 1)[1].split()
        assert child_fields[1] == str(coordinator.pid) and child_fields[0] != "Z"
        child_start = child_fields[19]
        coordinator.kill()
        assert coordinator.wait(timeout=5) == -9
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            stat = Path(f"/proc/{child_pid}/stat")
            if not stat.exists() or stat.read_text().split(") ", 1)[1].split()[0] == "Z":
                child_stopped = True
                break
            time.sleep(0.02)
        else:
            pytest.fail("Parent-death SIGKILL did not stop the coordinator's child")
    finally:
        if coordinator.poll() is None:
            coordinator.kill()
        coordinator.wait(timeout=5)
        if child_pid is not None and child_start is not None and not child_stopped:
            try:
                child_fields = Path(f"/proc/{child_pid}/stat").read_text().split(") ", 1)[1].split()
                if child_fields[19] == child_start and child_fields[0] != "Z":
                    os.kill(child_pid, 9)
            except (FileNotFoundError, ProcessLookupError):
                pass
        if coordinator.stdout:
            coordinator.stdout.close()
        if coordinator.stderr:
            coordinator.stderr.close()
