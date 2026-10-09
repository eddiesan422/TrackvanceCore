import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "doctor.py"
spec = importlib.util.spec_from_file_location("trackvance_doctor", SCRIPT)
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)


@pytest.fixture
def recovery_inventory(monkeypatch):
    state = {
        "containers": [
            {"id": "api-id", "service": "api"},
            {"id": "worker-id", "service": "worker"},
            {"id": "delivery-worker-id", "service": "delivery-worker"},
            {"id": "acquisition-worker-id", "service": "acquisition-worker"},
            {"id": "report-worker-id", "service": "report-worker"},
            {"id": "scheduler-id", "service": "scheduler"},
            {"id": "events-notifications-id", "service": "events-notifications"},
            {"id": "events-chaining-id", "service": "events-chaining"},
        ],
        "volumes": [
            {"logical_name": "trackvance_data", "name": "data-volume"},
            {"logical_name": "connection_credentials", "name": "credential-volume"},
            {"logical_name": "connection_keys", "name": "key-volume"},
            {
                "logical_name": "delivery_credentials",
                "name": "delivery-credential-volume",
            },
            {"logical_name": "delivery_keys", "name": "delivery-key-volume"},
        ],
    }
    snapshot_spec = importlib.util.spec_from_file_location("doctor_snapshot_tools", SCRIPT.with_name("docker_state.py"))
    snapshot_tools = importlib.util.module_from_spec(snapshot_spec)
    snapshot_spec.loader.exec_module(snapshot_tools)
    fake_module = SimpleNamespace(inventory=lambda _project: state, snapshot_bootstrap=snapshot_tools.snapshot_bootstrap)
    monkeypatch.setitem(sys.modules, "docker_state", fake_module)
    monkeypatch.setattr(doctor, "command_check", lambda *_args, **_kwargs: (True, "OK"))
    return state


def test_recovery_ready_checks_secret_mount_isolation(recovery_inventory, monkeypatch):
    def mounts(container_id):
        if container_id == "api-id":
            return {
                "/var/lib/trackvance": "data-volume",
                "/var/lib/trackvance-credentials": "credential-volume",
                "/var/lib/trackvance-keys": "key-volume",
                "/var/lib/trackvance-delivery-credentials": "delivery-credential-volume",
                "/var/lib/trackvance-delivery-keys": "delivery-key-volume",
            }
        if container_id == "delivery-worker-id":
            return {
                "/var/lib/trackvance": "data-volume",
                "/var/lib/trackvance-delivery-credentials": "delivery-credential-volume",
                "/var/lib/trackvance-delivery-keys": "delivery-key-volume",
            }
        if container_id == "acquisition-worker-id":
            return {"/var/lib/trackvance": "data-volume", "/var/lib/trackvance-credentials": "credential-volume",
                    "/var/lib/trackvance-keys": "key-volume"}
        return {"/var/lib/trackvance": "data-volume"}

    monkeypatch.setattr(doctor, "_inspect_mounts", mounts)
    checks = doctor.recovery_checks("trackvance-recovery-test", 64)
    assert checks == {
        "Montajes de recuperación": True,
        "Huella/linaje/SecretStore": True,
        "Espacio de recuperación": True,
    }


def test_recovery_ready_rejects_worker_secret_mount(recovery_inventory, monkeypatch):
    def mounts(container_id):
        common = {"/var/lib/trackvance": "data-volume"}
        if container_id == "api-id":
            return {
                **common,
                "/var/lib/trackvance-credentials": "credential-volume",
                "/var/lib/trackvance-keys": "key-volume",
                "/var/lib/trackvance-delivery-credentials": "delivery-credential-volume",
                "/var/lib/trackvance-delivery-keys": "delivery-key-volume",
            }
        if container_id == "delivery-worker-id":
            return {
                **common,
                "/var/lib/trackvance-delivery-credentials": "delivery-credential-volume",
                "/var/lib/trackvance-delivery-keys": "delivery-key-volume",
            }
        return {**common, "/var/lib/trackvance-delivery-keys": "delivery-key-volume"}

    monkeypatch.setattr(doctor, "_inspect_mounts", mounts)
    assert not doctor.recovery_checks("trackvance-recovery-test", 64)[
        "Montajes de recuperación"
    ]


def test_recovery_ready_rejects_source_secret_on_delivery_worker(
    recovery_inventory, monkeypatch
):
    def mounts(container_id):
        common = {"/var/lib/trackvance": "data-volume"}
        if container_id == "api-id":
            return {
                **common,
                "/var/lib/trackvance-credentials": "credential-volume",
                "/var/lib/trackvance-keys": "key-volume",
                "/var/lib/trackvance-delivery-credentials": "delivery-credential-volume",
                "/var/lib/trackvance-delivery-keys": "delivery-key-volume",
            }
        if container_id == "delivery-worker-id":
            return {
                **common,
                "/var/lib/trackvance-keys": "key-volume",
                "/var/lib/trackvance-delivery-credentials": "delivery-credential-volume",
                "/var/lib/trackvance-delivery-keys": "delivery-key-volume",
            }
        return common

    monkeypatch.setattr(doctor, "_inspect_mounts", mounts)
    assert not doctor.recovery_checks("trackvance-recovery-test", 64)[
        "Montajes de recuperación"
    ]


def test_recovery_ready_requires_explicit_project(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["doctor.py", "--docker", "--recovery-ready"])
    assert doctor.main() == 2
    assert "requiere --docker y --project" in capsys.readouterr().err


@pytest.mark.parametrize("exposed_service", ["acquisition-worker-id", "scheduler-id", "events-chaining-id", "events-notifications-id"])
def test_recovery_rejects_secret_volume_on_unprivileged_service(
    exposed_service, recovery_inventory, monkeypatch
):
    def mounts(container_id):
        common = {"/var/lib/trackvance": "data-volume"}
        source = {"/var/lib/trackvance-credentials": "credential-volume", "/var/lib/trackvance-keys": "key-volume"}
        destination = {"/var/lib/trackvance-delivery-credentials": "delivery-credential-volume", "/var/lib/trackvance-delivery-keys": "delivery-key-volume"}
        if container_id == "api-id":
            return {**common, **source, **destination}
        if container_id == "delivery-worker-id":
            return {**common, **destination}
        if container_id == "acquisition-worker-id":
            common.update(source)
        if container_id == exposed_service:
            common["/unexpected-secret"] = "delivery-key-volume"
        return common

    monkeypatch.setattr(doctor, "_inspect_mounts", mounts)
    assert not doctor.recovery_checks("trackvance-recovery-test", 64)["Montajes de recuperación"]


def test_docker_diagnostics_require_exact_certification_context_project(monkeypatch, tmp_path):
    project = "trackvance-v070-test-core-0123456789ab"
    monkeypatch.setitem(sys.modules, "certification_v070", SimpleNamespace(
        load_context=lambda _path: (tmp_path, {"project": project})
    ))
    with pytest.raises(ValueError, match="proyecto exacto"):
        doctor._compose_prefix("trackvance-certification", tmp_path)
    prefix = doctor._compose_prefix(project, tmp_path)
    assert prefix[:4] == ["docker", "compose", "--env-file", str(tmp_path / "test.env")]
    assert str(tmp_path / "compose.json") in prefix


def test_invalid_context_aborts_before_any_docker_command(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv", ["doctor.py", "--docker", "--project", "trackvance-certification", "--certification-context", str(tmp_path)])
    monkeypatch.setattr(doctor, "inspect_health", lambda _url: {"API": True})
    monkeypatch.setattr(doctor, "_compose_prefix", lambda *_args: (_ for _ in ()).throw(ValueError("scope")))
    calls = []
    monkeypatch.setattr(doctor, "command_check", lambda *args, **_kwargs: calls.append(args))
    assert doctor.main() == 2
    assert not calls
    assert "contexto de diagnóstico inválido" in capsys.readouterr().err


@pytest.mark.parametrize("exit_code", [0, 7])
def test_command_diagnostic_measures_real_process_without_exposing_output_or_environment(
    exit_code, monkeypatch
):
    marker = "private-doctor-diagnostic-test-value"
    monkeypatch.setenv("TV_DOCTOR_PRIVATE_TEST", marker)
    passed, detail = doctor.command_check([
        sys.executable, "-c",
        ("import os,sys; print(os.environ['TV_DOCTOR_PRIVATE_TEST']); "
         "print(os.environ['TV_DOCTOR_PRIVATE_TEST'], file=sys.stderr); "
         f"sys.exit({exit_code})"),
    ])
    assert passed is (exit_code == 0)
    assert detail["reason"] == ("OK" if passed else "EXIT_CODE")
    assert detail["exit_code"] == exit_code
    assert 0 < detail["duration_seconds"] < 15
    assert detail["timeout_seconds"] == 15
    assert set(detail) == {"reason", "duration_seconds", "timeout_seconds", "exit_code"}
    assert marker not in json.dumps(detail)


def test_real_command_timeout_is_distinct_from_nonzero_exit_and_redacts_arguments():
    marker = "private-doctor-timeout-argument"
    passed, detail = doctor.command_check(
        [sys.executable, "-c", "import time; time.sleep(10)", marker], timeout=.2
    )
    assert not passed and detail["reason"] == "TIMEOUT"
    assert detail["exit_code"] is None
    assert detail["duration_seconds"] >= .2
    assert detail["timeout_seconds"] == .2
    assert marker not in json.dumps(detail)


def test_missing_executable_diagnostic_does_not_expose_exception_filename(tmp_path):
    command = str(tmp_path / "private-missing-doctor-command")
    passed, detail = doctor.command_check([command])
    assert not passed and detail["reason"] == "EXECUTABLE_NOT_FOUND"
    assert detail["exit_code"] is None and detail["duration_seconds"] >= 0
    assert command not in json.dumps(detail)


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("reason,exit_code", [("TIMEOUT", None), ("EXIT_CODE", 1)])
def test_doctor_reports_safe_acquisition_failure_and_keeps_all_real_probes(
    monkeypatch, capsys, as_json, reason, exit_code
):
    monkeypatch.setattr(sys, "argv", ["doctor.py", "--docker", "--project", "private-fixture"]
                        + (["--json"] if as_json else []))
    monkeypatch.setattr(doctor, "inspect_health", lambda _url: {"API": True})
    calls = []

    def diagnostic(arguments, timeout=15):
        calls.append((arguments, timeout))
        failed = "acquisition-worker" in arguments
        return not failed, {"reason": reason if failed else "OK",
                            "duration_seconds": 15.123 if failed else 1.234,
                            "timeout_seconds": timeout,
                            "exit_code": exit_code if failed else 0}

    monkeypatch.setattr(doctor, "command_check", diagnostic)
    assert doctor.main() == 1
    output = capsys.readouterr().out
    assert "15.123" in output and reason in output
    assert len(calls) == 9 and all(timeout == 15 for _, timeout in calls)
    probes = {args[args.index("-T") + 1]: args[-1] for args, _ in calls if "exec" in args}
    assert set(probes) == {"worker", "delivery-worker", "acquisition-worker", "report-worker",
                           "scheduler", "events-notifications", "events-chaining"}
    for service, lane in (("worker", "DEFAULT"), ("delivery-worker", "DELIVERY"),
                          ("acquisition-worker", "ACQUISITION"), ("report-worker", "REPORT")):
        assert probes[service] == ("from trackvance.worker import worker_status; "
                                  f"raise SystemExit(0 if worker_status('{lane}')['status'] == 'RUNNING' else 1)")
    for component in ("scheduler", "events-notifications", "events-chaining"):
        assert probes[component] == ("from trackvance.component_health import component_status; "
                                    f"raise SystemExit(0 if component_status('{component}') == 'RUNNING' else 1)")
    if as_json:
        payload = json.loads(output)
        assert not payload["ok"] and not payload["checks"]["Worker Compose ACQUISITION"]
        assert payload["command_diagnostics"]["Worker Compose ACQUISITION"]["exit_code"] == exit_code
    else:
        assert "FAIL  Worker Compose ACQUISITION (" + reason in output
        assert "OK  Worker Compose DEFAULT\n" in output


def test_timeout_exception_output_and_command_are_never_included(monkeypatch):
    marker = "private-doctor-timeout-output"

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired([marker], 15, output=marker, stderr=marker)

    monkeypatch.setattr(doctor.subprocess, "run", timeout)
    passed, detail = doctor.command_check([marker])
    assert not passed and detail["reason"] == "TIMEOUT"
    assert marker not in json.dumps(detail)
