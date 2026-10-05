"""Isolation rejects overlap before any container can be started or removed."""

import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from certification_v080 import IsolationError, validate_resolved


@pytest.fixture
def isolated(tmp_path):
    project = "trackvance-v080-test-security-012345abcdef"
    context = {"project": project, "main_before": {
        "volumes": ["usual_data"], "containers": [{"ports": {
            "80/tcp": [{"HostPort": "3100"}]}, "networks": ["usual_network"],
            "mounts": [{"type": "bind", "source": str(tmp_path / "usual"),
                        "target": "/data", "name": None}]}]}}
    directory = tmp_path / "isolated"
    directory.mkdir()
    config = {"name": project, "volumes": {"data": {"name": project + "_data"}},
              "networks": {"default": {"name": project + "_default"}},
              "services": {"api": {"mem_limit": 512 * 1024**2, "cpus": 1,
                  "restart": "no", "volumes": [{"type": "volume", "source": "data"}],
                  "ports": [{"published": "32080", "host_ip": "127.0.0.1"}]}}}
    return config, context, directory


def test_private_named_volumes_are_allowed(isolated):
    validate_resolved(*isolated)


@pytest.mark.parametrize("change", [
    lambda c, d: c["volumes"]["data"].update(name="usual_data"),
    lambda c, d: c["volumes"]["data"].update(external=True),
    lambda c, d: c["networks"]["default"].update(name="usual_network"),
    lambda c, d: c["networks"]["default"].update(external=True),
    lambda c, d: c["services"]["api"]["ports"][0].update(published="3100"),
    lambda c, d: c["services"]["api"]["ports"][0].update(host_ip="0.0.0.0"),
    lambda c, d: c["services"]["api"].update(privileged=True),
    lambda c, d: c["services"]["api"].update(network_mode="host"),
    lambda c, d: c["services"]["api"].update(restart="always"),
    lambda c, d: c["services"]["api"].update(mem_limit=10 * 1024**3),
    lambda c, d: c["services"]["api"].update(cpus=None),
    lambda c, d: c.update(secrets={"real": {"file": str(d.parent / ".env")}}),
    lambda c, d: c["services"]["api"].update(environment={"TRACKVANCE_SSO_GOOGLE_ENABLED": "true"}),
    lambda c, d: c["services"]["api"].update(volumes=[{
        "type": "bind", "source": str(d.parent / "usual" / "nested"), "read_only": True}]),
    lambda c, d: c["services"]["api"].update(volumes=[{
        "type": "bind", "source": str(d.parent), "read_only": True}]),
    lambda c, d: c["services"]["api"].update(volumes=[{
        "type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}]),
])
def test_dangerous_overlap_or_unbounded_runtime_is_rejected(isolated, change):
    config, context, directory = isolated
    config = deepcopy(config)
    change(config, directory)
    with pytest.raises(IsolationError):
        validate_resolved(config, context, directory)


def test_private_fixture_bind_allowed(isolated):
    config, context, directory = isolated
    config["services"]["api"]["volumes"] = [{
        "type": "bind", "source": str(directory / "fixtures"), "read_only": True}]
    validate_resolved(config, context, directory)
