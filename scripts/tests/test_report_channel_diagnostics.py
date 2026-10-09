"""Only closed metadata survives bounded capture before owned cleanup."""
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import report_channel_diagnostics as diagnostics

PROJECT = "trackvance-v080-test-catalog-reports-0123456789ab"
IDENTITY = "a" * 64
PRIVATE = "synthetic-password SQL SELECT private_cells"


def record():
    return {"event": "reader_failure", "profile": "DOWNLOAD", "exception_class": "JSONDecodeError",
            "json_position": 4, "errno": None, "line_length": 39053, "line_newline": True,
            "line_cr_count": 1, "line_sha256": "b" * 64, "child_returncode": None,
            "coordinator_killed": False, "stop_requested": False}


@pytest.mark.parametrize("change", [
    {"raw_line": PRIVATE}, {"sql": PRIVATE}, {"exception_class": PRIVATE}, {"profile": [PRIVATE]},
    {"line_sha256": PRIVATE}, {"errno": PRIVATE}, {"line_length": True}, {"json_position": 99999999},
    {"line_newline": PRIVATE}, {"child_returncode": 999}, {"line_cr_count": 99999999}, {"stop_requested": PRIVATE},
])
def test_log_json_cannot_extend_schema_or_hide_private_values_in_metadata(change):
    assert diagnostics.parse_record(diagnostics.MARKER + json.dumps({**record(), **change}).encode()) is None


def test_closed_metadata_accepts_logging_prefix_but_drops_noise_and_oversized_records():
    value = record()
    assert diagnostics.parse_record(b"ERROR:trackvance.report_executor:" + diagnostics.MARKER + json.dumps(value).encode()) == value
    assert diagnostics.parse_record(PRIVATE.encode()) is None
    assert diagnostics.parse_record(diagnostics.MARKER + b"{" + b"x" * diagnostics.LINE_LIMIT) is None


def install_docker_fixture(monkeypatch, log, *, owner=PROJECT):
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        output = IDENTITY.encode() + b"\n" if "ps" in args else json.dumps({"project": owner, "service": "api"}).encode()
        return SimpleNamespace(returncode=0, stdout=output)

    class Process:
        def __init__(self, args, **kwargs):
            calls.append(args)
            self.stdout, self.returncode = io.BytesIO(log), None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(diagnostics.subprocess, "run", command)
    monkeypatch.setattr(diagnostics.subprocess, "Popen", Process)
    return calls


def test_capture_validates_exact_api_owner_and_retains_only_metadata(monkeypatch, tmp_path):
    value = record()
    log = (PRIVATE + "\n").encode() + diagnostics.MARKER + json.dumps(value).encode() + b"\n"
    calls = install_docker_fixture(monkeypatch, log)
    target = tmp_path / "diagnostics.private.json"
    summary = diagnostics.capture_api_diagnostics(["docker", "compose", "--project-name", PROJECT], PROJECT, target)
    receipt = json.loads(target.read_text())
    assert summary["status"] == "CAPTURED" and summary["records"] == 1
    assert receipt["records"] == [value] and PRIVATE not in target.read_text()
    assert calls[-1] == ["docker", "logs", "--tail", "2000", IDENTITY]


def test_drifted_or_habitual_api_cannot_be_read(monkeypatch, tmp_path):
    calls = install_docker_fixture(monkeypatch, PRIVATE.encode(), owner="trackvance-certification")
    target = tmp_path / "diagnostics.private.json"
    summary = diagnostics.capture_api_diagnostics(["docker", "compose"], PROJECT, target)
    assert summary["status"] == "UNAVAILABLE" and not summary["records"]
    assert all("logs" not in command for command in calls)
    assert PRIVATE not in target.read_text()


def test_log_scan_and_retained_record_count_are_finite(monkeypatch, tmp_path):
    line = diagnostics.MARKER + json.dumps(record()).encode() + b"\n"
    install_docker_fixture(monkeypatch, line * 5000)
    target = tmp_path / "diagnostics.private.json"
    summary = diagnostics.capture_api_diagnostics(["docker", "compose"], PROJECT, target)
    receipt = json.loads(target.read_text())
    assert summary["status"] == "PARTIAL" and summary["records"] == diagnostics.RECORD_LIMIT
    assert receipt["scan_truncated"] and receipt["records_truncated"]
    assert target.stat().st_size < 64 * 1024


def test_failed_tier_captures_before_evidence_copy_can_fail(monkeypatch, tmp_path):
    import catalog_reports_cycle as runner

    events = []

    def run(command, directory, stage):
        events.append(stage)
        raise RuntimeError("Synthetic stage failure")

    def capture(compose, project, target):
        events.append("capture")
        return {"status": "CAPTURED", "records": 2}

    monkeypatch.setattr(runner, "run", run)
    monkeypatch.setattr(runner, "capture_api_diagnostics", capture)
    monkeypatch.setattr(runner.guard, "compose_args", lambda directory, context: ["docker", "compose"])
    summary = {}
    with pytest.raises(RuntimeError, match="Synthetic stage failure"):
        runner.report_tier(tmp_path, {"project": PROJECT}, 1000000, summary)
    assert events == ["reports-1000000", "capture", "copy-1000000"]
    assert summary["channel_diagnostics"] == {"status": "CAPTURED", "records": 2}


def test_diagnostic_write_failure_preserves_original_tier_failure(monkeypatch, tmp_path):
    import catalog_reports_cycle as runner

    def run(*args):
        raise RuntimeError("Original tier failure")

    def capture(*args):
        raise OSError(PRIVATE)

    monkeypatch.setattr(runner, "run", run)
    monkeypatch.setattr(runner, "capture_api_diagnostics", capture)
    monkeypatch.setattr(runner.guard, "compose_args", lambda directory, context: ["docker", "compose"])
    summary = {}
    with pytest.raises(RuntimeError, match="Original tier failure"):
        runner.report_tier(tmp_path, {"project": PROJECT}, 1000000, summary)
    assert summary["channel_diagnostics"] == {"status": "UNAVAILABLE", "error_type": "OSError"}
    assert PRIVATE not in json.dumps(summary)
