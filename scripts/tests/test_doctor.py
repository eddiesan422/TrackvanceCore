import importlib.util
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
        ],
        "volumes": [
            {"logical_name": "trackvance_data", "name": "data-volume"},
            {"logical_name": "connection_credentials", "name": "credential-volume"},
            {"logical_name": "connection_keys", "name": "key-volume"},
        ],
    }
    fake_module = SimpleNamespace(inventory=lambda _project: state)
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
            }
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
            }
        return {**common, "/var/lib/trackvance-keys": "key-volume"}

    monkeypatch.setattr(doctor, "_inspect_mounts", mounts)
    assert not doctor.recovery_checks("trackvance-recovery-test", 64)[
        "Montajes de recuperación"
    ]


def test_recovery_ready_requires_explicit_project(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["doctor.py", "--docker", "--recovery-ready"])
    assert doctor.main() == 2
    assert "requiere --docker y --project" in capsys.readouterr().err
