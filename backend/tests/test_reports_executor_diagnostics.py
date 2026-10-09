"""Reader fault injection verifies private diagnostics, not engine certification."""
import errno
import hashlib
import io
import json
import logging
import sys
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
