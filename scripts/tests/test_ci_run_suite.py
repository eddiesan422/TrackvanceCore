"""Development and final certification share full suites and bounded execution."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci import run_suite
from ci.common import load_manifest


def test_manifest_and_executable_groups_are_identical(tmp_path):
    manifest = load_manifest()
    assert set(run_suite.DEADLINES) == {group["id"] for group in manifest["groups"]}
    for group in manifest["groups"]:
        commands = run_suite.commands_for(group["id"], tmp_path)
        assert commands and all(command and cwd.is_dir() for _, command, cwd in commands)
        if group["id"] not in {"backend", "frontend"}:
            assert run_suite.DEADLINES[group["id"]] + 300 <= group["timeout_minutes"] * 60


def test_full_volume_and_existing_recovery_matrix_are_preserved(tmp_path):
    for group in ("spark-local", "spark-standalone", "catalog-reports"):
        tokens = [token for _, command, _ in run_suite.commands_for(group, tmp_path) for token in command]
        assert "1000000" in tokens
    catalog = run_suite.commands_for("catalog-reports", tmp_path)[0][1]
    assert all(value in catalog for value in ("400000", "--with-browser", "--with-recovery", "--with-ephemeral-observation"))
    recovery = run_suite.commands_for("backup-restore", tmp_path)
    assert len(recovery) == 4
    assert [command[-1] for _, command, _ in recovery[1:]] == ["0.5.1", "0.6.0", "0.6.1"]
    backend = run_suite.commands_for("backend", tmp_path)
    pytest_command = next(command for phase, command, _ in backend if phase == "unit-tests")
    assert "tests" in pytest_command and "../scripts/tests" in pytest_command


def test_expired_deadline_cannot_start_a_process(tmp_path, monkeypatch):
    monkeypatch.setattr(run_suite.subprocess, "Popen", lambda *_a, **_k: pytest.fail("Expired phase started a process"))
    with pytest.raises(TimeoutError):
        run_suite.execute(["unused"], tmp_path, "phase", 0)


def test_failed_phase_leaves_incremental_failure_before_raising(tmp_path, monkeypatch):
    class Process:
        def wait(self, **kwargs):
            return 23

    monkeypatch.setattr(run_suite.subprocess, "Popen", lambda *_a, **_k: Process())
    with pytest.raises(RuntimeError):
        run_suite.execute(["unused"], tmp_path, "phase", 5)
    proof = json.loads((tmp_path / "phase.json").read_text())
    assert proof["exit_code"] == 23 and proof["status"] == "FAIL" and not proof["timed_out"]


def test_phase_timeout_terminates_only_its_process_and_cannot_be_pass(tmp_path, monkeypatch):
    class Process:
        pid = 123
        calls = 0

        def wait(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("unused", 0.01)
            return -15

        def terminate(self):
            stopped.append(self.pid)

    stopped = []
    monkeypatch.setattr(run_suite.subprocess, "Popen", lambda *_a, **_k: Process())
    monkeypatch.setattr(run_suite.os, "killpg", lambda pid, _: stopped.append(pid), raising=False)
    with pytest.raises(RuntimeError):
        run_suite.execute(["unused"], tmp_path, "timeout", 0.01)
    proof = json.loads((tmp_path / "timeout.json").read_text())
    assert proof["timed_out"] is True and proof["status"] == "FAIL" and stopped == [123]
