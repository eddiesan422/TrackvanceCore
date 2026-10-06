"""Pure regressions for every resolved resource, live inheritance and CI opt-in."""
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci import compose_preflight as guard

PROJECT = "trackvance-v070-test-preflight-0123456789ab"


@pytest.fixture
def resolved(tmp_path):
    directory = tmp_path / ".codex-local/v070" / PROJECT
    directory.mkdir(parents=True)
    (directory / "test.env").write_text("POSTGRES_PASSWORD=disposable\n")
    code = tmp_path / "backend/src"
    code.mkdir(parents=True)
    services = {}
    for name in ["postgres", *sorted(guard.APP_SERVICES - {"report-worker"}), "web"]:
        environment = {"TRACKVANCE_CERTIFICATION_PROJECT": PROJECT} if name in guard.APP_SERVICES else {}
        if name == "postgres":
            environment = {"POSTGRES_USER": "tv_v070_test", "POSTGRES_DB": "tv_v070_test"}
        services[name] = {"cpus": 1, "mem_limit": 268435456, "pids_limit": 128,
                          "restart": "no", "environment": environment,
                          "networks": {"default": {}}, "volumes": [{"type": "volume", "source": "data", "target": "/data"}]}
    services["api"]["volumes"].append({"type": "bind", "source": str(code), "target": "/app/backend/src", "read_only": True})
    services["api"]["environment"]["DATABASE_URL"] = "postgresql+psycopg://tv_v070_test:disposable@postgres:5432/tv_v070_test"
    services["web"]["ports"] = [{"target": 80, "published": "32070", "host_ip": "127.0.0.1"}]
    config = {"name": PROJECT, "services": services,
              "volumes": {"data": {"name": PROJECT + "_data"}},
              "networks": {"default": {"name": PROJECT + "_default"}}}
    main_path = tmp_path / "habitual-data"
    main_path.mkdir()
    baseline = {"volumes": ["trackvance-certification_data"], "networks": ["trackvance-certification_default"],
                "containers": [{"ports": {"80/tcp": [{"HostPort": "3100"}, {"HostPort": "32123"}]},
                                "mounts": [{"type": "bind", "source": str(main_path)}],
                                "Config": {"Env": ["POSTGRES_PASSWORD=do-not-print-main-secret"]}}]}
    return config, directory, baseline, tmp_path


def validate(fixture, **kwargs):
    config, directory, baseline, root = fixture
    guard.validate_resolved(config, project=PROJECT, directory=directory,
                            main_inventory=baseline, root=root, **kwargs)


def test_complete_nine_service_config_and_owned_restart_are_accepted(resolved):
    assert len(resolved[0]["services"]) == 9
    existing = {"containers": [{"Name": "/" + PROJECT + "-api-1", "Config": {
                    "Labels": {"com.docker.compose.project": PROJECT, "com.docker.compose.service": "api"},
                    "Env": ["TRACKVANCE_CERTIFICATION_PROJECT=" + PROJECT]}}],
                "volumes": [{"Name": PROJECT + "_data", "Labels": {"com.docker.compose.project": PROJECT}}],
                "networks": [{"Name": PROJECT + "_default", "Labels": {"com.docker.compose.project": PROJECT}}]}
    validate(resolved, existing=existing)


def test_unselected_report_worker_still_requires_complete_isolation(resolved):
    """Resolved dormant definitions must be safe even for explicit nine-service up."""
    config = resolved[0]
    config["services"]["report-worker"] = deepcopy(config["services"]["worker"])
    del config["services"]["report-worker"]["pids_limit"]
    with pytest.raises(guard.ComposePreflightError, match="UNBOUNDED_SERVICE"):
        validate(resolved)
    config["services"]["report-worker"].update(cpus=0.25, mem_limit=268435456, pids_limit=128)
    validate(resolved)


@pytest.mark.parametrize("field", ["POSTGRES_USER", "POSTGRES_DB"])
def test_database_identity_cannot_be_inherited_for_postgres(resolved, field):
    resolved[0]["services"]["postgres"]["environment"][field] = "habitual"
    with pytest.raises(guard.ComposePreflightError, match="LIVE_DATABASE_IDENTITY"):
        validate(resolved)


def test_declared_owned_network_cannot_use_the_host_driver(resolved):
    resolved[0]["networks"]["default"]["driver"] = "host"
    with pytest.raises(guard.ComposePreflightError, match="HOST_NETWORK_DRIVER"):
        validate(resolved)


@pytest.mark.parametrize("project", ["trackvance-connections-e2e-1234-0123456789ab",
                                    "trackvance-delivery-e2e-1234-012345",
                                    "trackvance-bench-1234-012345",
                                    "trackvance-delivery-bench-1234-01234567",
                                    "trackvance-connections-e2e-0123456789ab",
                                    "trackvance-delivery-e2e-0123456789ab",
                                    "trackvance-bench-0123456789ab",
                                    "trackvance-delivery-bench-0123456789ab"])
def test_real_historical_pid_uuid_namespaces_validate_every_service(resolved, project):
    config, directory, baseline, root = resolved
    config["name"] = project
    for descriptors in (config["volumes"], config["networks"]):
        for descriptor in descriptors.values():
            descriptor["name"] = descriptor["name"].replace(PROJECT, project)
    for name, service in config["services"].items():
        if name in guard.APP_SERVICES:
            service["environment"]["TRACKVANCE_CERTIFICATION_PROJECT"] = project
    guard.validate_resolved(config, project=project, directory=directory,
                            main_inventory=baseline, root=root)


@pytest.mark.parametrize("damage", [None, "writable", "other_service", "other_file", "other_target"])
def test_only_the_exact_benchmark_nginx_readonly_web_bind_is_allowed(resolved, damage):
    config, _, _, root = resolved
    nginx = root / "deploy/docker/nginx.benchmark.conf"
    nginx.parent.mkdir(parents=True)
    nginx.write_text("# synthetic test configuration\n")
    mount = {"type": "bind", "source": str(nginx), "target": "/etc/nginx/conf.d/default.conf", "read_only": True}
    name = "web"
    if damage == "writable":
        mount["read_only"] = False
    elif damage == "other_service":
        name = "api"
    elif damage == "other_file":
        mount["source"] = str(nginx.with_name("nginx.conf"))
    elif damage == "other_target":
        mount["target"] = "/etc/nginx/nginx.conf"
    config["services"][name]["volumes"].append(mount)
    if damage:
        with pytest.raises(guard.ComposePreflightError, match="UNOWNED_BIND_PATH"):
            validate(resolved)
    else:
        validate(resolved)


@pytest.mark.parametrize("kind", ["volumes", "networks"])
@pytest.mark.parametrize("damage", ["protected", "external", "different_namespace", "wrong_label", "duplicate_name", "driver_bind"])
def test_all_named_resources_reject_shared_or_unowned_identity(resolved, kind, damage):
    resources = resolved[0][kind]
    descriptor = next(iter(resources.values()))
    if damage == "protected":
        descriptor["name"] = resolved[2][kind][0]
    elif damage == "external":
        descriptor["external"] = True
    elif damage == "different_namespace":
        descriptor["name"] = "another-test_data"
    elif damage == "wrong_label":
        descriptor["labels"] = {"com.docker.compose.project": "foreign"}
    elif damage == "duplicate_name":
        resources["duplicate"] = deepcopy(descriptor)
    else:
        descriptor["driver_opts"] = {"type": "none", "device": "/host/data", "o": "bind"}
    with pytest.raises(guard.ComposePreflightError):
        validate(resolved)


@pytest.mark.parametrize("field,value", [("privileged", True), ("cap_add", ["SYS_ADMIN"]),
    ("devices", ["/dev/sda"]), ("network_mode", "host"), ("pid", "host"), ("ipc", "host"),
    ("restart", "always"), ("container_name", "trackvance-certification-web-1"),
    ("env_file", [".env"]), ("cpus", float("inf")), ("mem_limit", 0), ("pids_limit", -1)])
def test_service_checks_include_last_service_not_only_api(resolved, field, value):
    resolved[0]["services"]["web"][field] = value
    with pytest.raises(guard.ComposePreflightError):
        validate(resolved)


@pytest.mark.parametrize("published,host", [(3100, "127.0.0.1"), (32123, "127.0.0.1"), (32070, "0.0.0.0"), (0, "127.0.0.1")])
def test_ports_preserve_main_bindings_and_require_explicit_loopback(resolved, published, host):
    resolved[0]["services"]["web"]["ports"][0].update(published=published, host_ip=host)
    with pytest.raises(guard.ComposePreflightError, match="PORT"):
        validate(resolved)


@pytest.mark.parametrize("damage", ["protected_child", "protected_parent", "arbitrary_bind", "writable_code", "socket", "unknown_volume"])
def test_mounts_cannot_alias_live_paths_or_escape_private_context(resolved, damage):
    config, _, _, root = resolved
    mount = config["services"]["api"]["volumes"][-1]
    if damage == "protected_child":
        mount["source"] = str(root / "habitual-data/child")
    elif damage == "protected_parent":
        mount["source"] = str(root)
    elif damage == "arbitrary_bind":
        mount["source"] = str(root / "unowned")
    elif damage == "writable_code":
        mount["read_only"] = False
    elif damage == "socket":
        mount["source"] = "/var/run/docker.sock"
    else:
        mount.update(type="volume", source="unlisted")
    with pytest.raises(guard.ComposePreflightError):
        validate(resolved)


@pytest.mark.parametrize("damage", ["live_secret", "database", "sso", "sso_credentials", "smtp", "project_marker"])
def test_resolved_environment_fails_closed_without_exposing_values(resolved, damage):
    values = resolved[0]["services"]["api"]["environment"]
    if damage == "live_secret":
        values["PRIVATE_TOKEN"] = "do-not-print-main-secret"
    elif damage == "database":
        values["DATABASE_URL"] = "postgresql://trackvance:do-not-print-main-secret@production/live"
    elif damage == "sso":
        values["TRACKVANCE_SSO_GOOGLE_ENABLED"] = "true"
    elif damage == "sso_credentials":
        values["TRACKVANCE_SSO_GOOGLE_CLIENT_SECRET"] = "do-not-print-main-secret"
    elif damage == "smtp":
        values["TRACKVANCE_SMTP_ENABLED"] = "true"
    else:
        values["TRACKVANCE_CERTIFICATION_PROJECT"] = "foreign"
    with pytest.raises(guard.ComposePreflightError) as raised:
        validate(resolved)
    assert "do-not-print-main-secret" not in str(raised.value)


def test_mock_oidc_requires_explicit_overlay_and_mock_discovery(resolved):
    config = resolved[0]
    config["services"]["mock-oidc"] = {"cpus": .5, "mem_limit": 268435456, "pids_limit": 128,
                                      "command": ["python", "/test/mock_oidc.py"],
                                      "environment": {"MOCK_OIDC_INTERNAL_URL": "http://mock-oidc:9000",
                                          "MOCK_OIDC_PUBLIC_URL": "http://127.0.0.1:32071", "MOCK_OIDC_CLIENT_SECRET": "generated-mock-secret"},
                                      "networks": {"default": {}}}
    values = config["services"]["api"]["environment"]
    values.update(TRACKVANCE_SSO_TEST_MODE="true", TRACKVANCE_SSO_GOOGLE_ENABLED="true",
                  TRACKVANCE_SSO_GOOGLE_CLIENT_ID="trackvance-disposable-client",
                  TRACKVANCE_SSO_GOOGLE_CLIENT_SECRET="generated-mock-secret",
                  TRACKVANCE_SSO_GOOGLE_DISCOVERY_URL="http://mock-oidc:9000/google/.well-known/openid-configuration")
    with pytest.raises(guard.ComposePreflightError, match="IMPLICIT_MOCK"):
        validate(resolved)
    validate(resolved, allow_mock_oidc=True)
    values["TRACKVANCE_SSO_GOOGLE_DISCOVERY_URL"] = "https://accounts.google.com/.well-known/openid-configuration"
    with pytest.raises(guard.ComposePreflightError, match="LIVE_SSO_CONFIGURATION"):
        validate(resolved, allow_mock_oidc=True)


@pytest.mark.parametrize("kind", ["containers", "volumes", "networks"])
def test_preexisting_names_cannot_replace_project_ownership_labels(resolved, kind):
    resource = {"Name": PROJECT + "_data", "Labels": {"com.docker.compose.project": "foreign"}}
    if kind == "containers":
        resource = {"Name": "/" + PROJECT + "-api-1", "Config": {"Labels": {"com.docker.compose.project": "foreign"}}}
    with pytest.raises(guard.ComposePreflightError, match="NOT_OWNED"):
        validate(resolved, existing={kind: [resource]})


def test_opt_in_preflight_reads_only_resolved_config_and_inventories(resolved, monkeypatch):
    config, directory, _, root = resolved
    monkeypatch.setattr(guard, "ROOT", root)
    calls = []
    def run(arguments):
        calls.append(arguments)
        return json.dumps(config) if "config" in arguments else ""
    compose = ["docker", "compose", "--env-file", str(directory / "test.env"), "-p", PROJECT, "-f", "compose.yml"]
    guard.preflight(compose, {}, project=PROJECT, directory=directory, run=lambda _: pytest.fail("No CI opt-in"))
    guard.preflight(compose, {"TRACKVANCE_CI_IMAGE_MANIFEST": "manifest.json"}, project=PROJECT, directory=directory, run=run)
    assert calls[0] == [*compose, "config", "--format", "json"]
    assert len(calls) == 7 and all(not set(call) & {"up", "down", "build", "exec", "run", "rm"} for call in calls)


def test_preflight_rejects_private_env_escape_before_docker(resolved):
    _, directory, _, _ = resolved
    compose = ["docker", "compose", "--env-file", str(directory.parent / "live.env"), "-p", PROJECT]
    with pytest.raises(guard.ComposePreflightError, match="PRIVATE_ENV"):
        guard.preflight(compose, {"TRACKVANCE_CI_IMAGE_MANIFEST": "manifest.json"}, project=PROJECT,
                        directory=directory, run=lambda _: pytest.fail("Must fail before Docker"))


def test_v070_up_checks_configuration_before_command_but_other_operations_do_not(tmp_path, monkeypatch):
    import certification_v070
    calls = []
    monkeypatch.setattr(guard, "preflight", lambda *_args, **_kwargs: calls.append("preflight"))
    monkeypatch.setattr(certification_v070, "command", lambda *_args, **_kwargs: calls.append("command") or "")
    context = {"project": PROJECT}
    certification_v070.compose(tmp_path, context, ["up", "--no-build"])
    assert calls == ["preflight", "command"]
    calls.clear()
    certification_v070.compose(tmp_path, context, ["down", "--volumes"])
    assert calls == ["command"]


def test_recovery_up_preflights_scoped_config_without_changing_restore_services(tmp_path, monkeypatch):
    import docker_backup_cycle
    import v070_recovery
    env = tmp_path / "test.env"
    env.write_text("POSTGRES_USER=tv_v070_test\nPOSTGRES_DB=tv_v070_test\nPOSTGRES_PASSWORD=disposable\n")
    calls = []
    monkeypatch.setattr(guard, "preflight", lambda *_args, **_kwargs: calls.append("preflight"))
    monkeypatch.setattr(docker_backup_cycle, "execute", lambda *_args, **_kwargs: calls.append("command") or "")
    adapter = v070_recovery.compose_adapter(PROJECT, env, tmp_path / "restore-compose.yml", {"POSTGRES_PASSWORD": "disposable"})
    adapter(PROJECT, "up", "--no-build", "postgres", "api", "web")
    assert calls == ["preflight", "command"]
