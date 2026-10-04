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
            {"id": "delivery-worker-id", "service": "delivery-worker"},
            {"id": "acquisition-worker-id", "service": "acquisition-worker"},
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
