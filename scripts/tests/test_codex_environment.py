"""Small permanent-stack guards; every Docker/API/browser operation is simulated."""
import copy
import json
import re
from types import SimpleNamespace

import codex_environment as codex
import pytest
import yaml

OLD = "a" * 40
NEW = "b" * 40
IDS = {role: "sha256:" + character * 64 for role, character in zip(("backend", "web", "postgres"), "123", strict=True)}
NEW_WEB = "sha256:" + "4" * 64


class Reset:
    pass


class Loader(yaml.SafeLoader):
    pass


Loader.add_constructor("!reset", lambda loader, node: Reset())
Loader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node, deep=True))


def resolved(values):
    base = yaml.safe_load((codex.ROOT / "compose.yml").read_text())
    patch = yaml.load((codex.ROOT / "compose.codex.yml").read_text(), Loader=Loader)
    def merge(left, right):
        result = copy.deepcopy(left)
        for key, item in right.items():
            if isinstance(item, Reset):
                result.pop(key, None)
            elif isinstance(item, dict) and isinstance(result.get(key), dict):
                result[key] = merge(result[key], item)
            else:
                result[key] = copy.deepcopy(item)
        return result
    config = merge(base, patch)
    def expand(item):
        if isinstance(item, Reset):return None
        if isinstance(item, dict):return {key: expand(value) for key, value in item.items()}
        if isinstance(item, list):return [expand(value) for value in item]
        if not isinstance(item, str):return item
        for _ in range(8):
            item = re.sub(r"\$\{([A-Z_0-9]+)(?::([-?])([^{}]*))?\}", lambda m: values.get(m[1], m[3] or ""), item)
        return item
    config = expand(config)
    for service in config["services"].values():
        service["volumes"] = [{"type": "volume", "source": item.split(":", 1)[0], "target": item.split(":", 1)[1]} for item in service.get("volumes", [])]
        service["ports"] = [{"host_ip": item.split(":")[0], "published": item.split(":")[1], "target": int(item.split(":")[2])} for item in service.get("ports", [])]
    return config


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    directory = tmp_path / ".codex-local/codex-permanent"
    monkeypatch.setattr(codex, "fingerprint", lambda role, sha: {"sha256": "same-backend" if role == "backend" else "old-web" if sha == OLD else "new-web", "files": 10, "source_sha": sha})
    monkeypatch.setattr(codex, "source_version", lambda _sha: "0.8.5")
    args = SimpleNamespace(image_source_sha=OLD, engine_id="approved-engine", web_port=3205, api_port=3206, **{role + "_image": identifier for role, identifier in IDS.items()})
    codex.initialize(args, directory)
    value, values = codex.load(directory)
    defaults = {"backend": {"Env": ["PATH=/app"], "Cmd": ["python"], "Entrypoint": None, "User": "trackvance"},
        "web": {"Env": [], "Cmd": ["nginx"], "Entrypoint": None, "User": ""},
        "postgres": {"Env": ["PG_MAJOR=16"], "Cmd": ["postgres"], "Entrypoint": None, "User": ""}}
    value["_image_defaults"] = defaults
    yield directory, value, values, defaults


def resources(config):
    return {"containers": [], "volumes": [{"Name": item["name"], "Driver": "local", "Labels": item["labels"], "CreatedAt": "frozen", "Mountpoint": "/owned/" + name} for name, item in config["volumes"].items()],
        "networks": [{"Id": "network-owned-id", "Name": "trackvance-codex_default", "Driver": "bridge", "Internal": config["networks"]["default"]["internal"], "Labels": config["networks"]["default"]["labels"], "IPAM": {}}]}


def runtime(config, defaults, names=codex.ACTIVE):
    value = resources(config)
    for index, name in enumerate(names):
        service = config["services"][name]
        role = "postgres" if name in {"postgres", "test-postgres"} else "web" if name == "web" else "backend"
        default = defaults[role]
        environment = dict(entry.split("=", 1) for entry in default["Env"])
        environment.update(service.get("environment", {}))
        labels = {**service["labels"], "com.docker.compose.project": codex.PROJECT, "com.docker.compose.service": name}
        value["containers"].append({"Id": str(index) * 64, "Image": service["image"],
            "Config": {"Image": service["image"], "Env": [key + "=" + str(item) for key, item in environment.items()], "Labels": labels,
                "Cmd": service.get("command", default["Cmd"]), "Entrypoint": default["Entrypoint"], "User": default["User"]},
            "HostConfig": {"Memory": codex.memory_bytes(service["mem_limit"]), "NanoCpus": int(float(service["cpus"]) * 10**9), "PidsLimit": 128,
                "Privileged": False, "NetworkMode": "trackvance-codex_default", "CapDrop": service.get("cap_drop", []), "SecurityOpt": service.get("security_opt", []),
                "PortBindings": {str(port["target"]) + "/tcp": [{"HostIp": port["host_ip"], "HostPort": str(port["published"])}] for port in service["ports"]}},
            "State": {"Running": True, "StartedAt": "frozen"}, "NetworkSettings": {"Networks": {"trackvance-codex_default": {}}},
            "Mounts": [{"Type": "volume", "Name": config["volumes"][mount["source"]]["name"], "Destination": mount["target"], "RW": True} for mount in service["volumes"]]})
    return value


def test_fresh_config_never_inherits_user_or_shell_credentials(fixture, monkeypatch):
    directory, _value, values, _defaults = fixture
    monkeypatch.setenv("POSTGRES_PASSWORD", "user-private-password")
    monkeypatch.setenv("DOCKER_CONFIG", "operator-selected-anonymous-config")
    monkeypatch.setenv("DOCKER_HOST", "npipe://approved-engine")
    monkeypatch.setenv("DOCKER_CONTEXT", "operator-context")
    monkeypatch.setenv("DOCKER_AUTH_CONFIG", "must-never-be-forwarded")
    env = codex.process_environment(values)
    assert values["POSTGRES_PASSWORD"] != "user-private-password" and len({values[k] for k in ("POSTGRES_PASSWORD", "CODEX_ADMIN_PASSWORD", "CODEX_DESTINATION_PASSWORD")}) == 3
    assert env["DOCKER_CONFIG"] == "operator-selected-anonymous-config" and env["DOCKER_HOST"] == "npipe://approved-engine" and env["DOCKER_CONTEXT"] == "operator-context"
    assert "DOCKER_AUTH_CONFIG" not in env and codex.load(directory)[1] == values


def test_overlay_caps_all_roles_and_has_separate_destination_on_standard_bridge(fixture):
    _directory, _value, values, _defaults = fixture
    config = resolved(values)
    budget = codex.validate_resolved(config, values, {"containers": [], "volumes": [], "networks": []})
    assert budget == {"all_services_cpus": 4, "all_services_memory_bytes": 6 * 1024**3}
    assert set(config["volumes"]) == set(codex.APP_VOLUMES) | {"test_destination_data"}
    assert all(config["services"][name]["profiles"] == ["codex-automatic"] for name in codex.OPTIONAL)
    assert config["services"]["worker"]["environment"]["TRACKVANCE_SPARK_DRIVER_MEMORY_MB"] == "512"
    assert config["services"]["worker"]["environment"]["TRACKVANCE_SPARK_EXECUTOR_MEMORY_MB"] == "512"
    assert values["TRACKVANCE_WEB_ORIGIN"] == "http://127.0.0.1:3205"
    assert config["networks"]["default"]["internal"] is False


def test_compose_reset_tag_is_explicit_on_every_backend_service():
    # Compose's reset processing needs the tag on the service mapping itself;
    # a tag inherited only through YAML's merge anchor retains the base build.
    document = yaml.compose((codex.ROOT / "compose.codex.yml").read_text())
    services = next(value for key, value in document.value if key.value == "services")
    nodes = {key.value: value for key, value in services.value}
    for role in codex.BACKEND:
        reset = next(value for key, value in nodes[role].value if key.value == "build")
        assert reset.tag == "!reset" and reset.value == "null"


@pytest.mark.parametrize("compose_key, runtime_key, code", [
    ("command", "Cmd", "CODEX_RUNTIME_COMMAND_DRIFT"),
    ("entrypoint", "Entrypoint", "CODEX_RUNTIME_PROCESS_IDENTITY_DRIFT"),
])
def test_normalized_null_process_fields_inherit_image_defaults_but_empty_override_is_explicit(fixture, compose_key, runtime_key, code):
    _directory, value, values, defaults = fixture
    config = resolved(values)
    defaults["web"]["Entrypoint"] = ["/docker-entrypoint.sh"]
    defaults["postgres"]["Entrypoint"] = ["docker-entrypoint.sh"]
    for name in ("web", "postgres"):
        config["services"][name]["command"] = None
        config["services"][name]["entrypoint"] = None
    existing = runtime(config, defaults)
    for row in existing["containers"]:
        name = row["Config"]["Labels"]["com.docker.compose.service"]
        if name in {"web", "postgres"}:
            role = "web" if name == "web" else "postgres"
            row["Config"]["Cmd"] = defaults[role]["Cmd"]
            row["Config"]["Entrypoint"] = defaults[role]["Entrypoint"]
    codex.validate_owned(existing, config, value)
    config["services"]["web"][compose_key] = []
    with pytest.raises(ValueError, match=code):
        codex.validate_owned(existing, config, value)
    web = next(row for row in existing["containers"] if row["Config"]["Labels"]["com.docker.compose.service"] == "web")
    web["Config"][runtime_key] = []
    codex.validate_owned(existing, config, value)


@pytest.mark.parametrize("change", ["env", "memory", "port", "mount", "privileged", "network", "internal-network", "baseline-config", "recreated-volume"])
def test_actual_runtime_drift_never_adopted(fixture, change):
    _directory, value, values, defaults = fixture
    config = resolved(values); existing = runtime(config, defaults)
    codex.validate_owned(existing, config, value)
    value["runtime_identities"] = {row["Config"]["Labels"]["com.docker.compose.service"]: codex.runtime_identity(row) for row in existing["containers"]}
    stable = codex.stable_inventory(existing); value["resources"] = {key: stable[key] for key in ("volumes", "networks")}
    row = next(row for row in existing["containers"] if row["Config"]["Labels"]["com.docker.compose.service"] == "api")
    if change == "env":row["Config"]["Env"].append("AMBIENT_PRIVATE=not-approved")
    elif change == "memory":row["HostConfig"]["Memory"] += 1
    elif change == "port":row["HostConfig"]["PortBindings"]["8000/tcp"][0]["HostIp"] = "0.0.0.0"
    elif change == "mount":row["Mounts"][0]["Name"] = "trackvance-certification_trackvance_data"
    elif change == "privileged":row["HostConfig"]["Privileged"] = True
    elif change == "network":row["NetworkSettings"]["Networks"]["foreign-network"] = {}
    elif change == "internal-network":existing["networks"][0]["Internal"] = True
    elif change == "baseline-config":row["Config"]["UnknownFutureField"] = "changed"
    else:existing["volumes"][0]["CreatedAt"] = "recreated"
    with pytest.raises(ValueError):codex.validate_owned(existing, config, value)


def test_new_worktree_uses_same_common_git_private_path(tmp_path, monkeypatch):
    common = tmp_path / "normal-checkout/.git"; common.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(codex, "command", lambda argv: calls.append(argv) or str(common))
    assert codex.private_directory(None) == tmp_path / "normal-checkout/.codex-local/codex-permanent"
    assert calls == [["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]]


def test_init_changed_web_bootstraps_reusing_backend_and_pg(fixture, monkeypatch):
    directory, value, values, defaults = fixture
    calls = []; started = False
    empty = {"containers": [], "volumes": [], "networks": []}
    def invoke(argv, **kwargs):
        nonlocal started
        calls.append(argv)
        if argv[:2] == ["git", "diff"]:return ""
        if argv[:3] == ["docker", "buildx", "inspect"]:return "default"
        if argv[:3] == ["docker", "image", "ls"]:return ""
        if argv[:3] == ["docker", "buildx", "build"]:return ""
        if argv[:3] == ["docker", "image", "inspect"]:
            if "--format" in argv:return NEW_WEB
            return json.dumps([{"Id": NEW_WEB, "Config": {**defaults["web"], "Labels": {"org.opencontainers.image.revision": NEW, "org.opencontainers.image.version": "0.8.5"}}}])
        if "config" in argv:return json.dumps(resolved(values))
        assert "up" in argv and "--no-start" not in argv
        assert argv[-len(codex.ACTIVE):] == list(codex.ACTIVE)
        started = True
        return ""
    monkeypatch.setattr(codex, "command", invoke)
    monkeypatch.setattr(codex, "prepare", lambda *args: (["docker", "compose"], {}, resolved(values), empty, codex.stable_inventory(empty), {}))
    monkeypatch.setattr(codex, "inventory", lambda project, _env: runtime(resolved(values), defaults) if project == codex.PROJECT and started else empty)
    monkeypatch.setattr(codex, "check_health", lambda *_args: {})
    class Socket:
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def bind(self, address):assert address[0] == "127.0.0.1"
    monkeypatch.setattr(codex.socket, "socket", Socket)
    args = SimpleNamespace(stage="update", services=None, build_changed=True, backend_image=None, web_image=None, image_source_sha=None)
    result = codex.execute_stage(args, directory, value, values, NEW)
    assert result["status"] == "PASS" and value["images"]["backend"]["id"] == IDS["backend"] and value["images"]["backend"]["source_sha"] == OLD
    assert value["images"]["web"]["id"] == NEW_WEB and value["images"]["postgres"]["id"] == IDS["postgres"]
    builds = [argv for argv in calls if argv[:3] == ["docker", "buildx", "build"]]
    assert len(builds) == 1 and "deploy/docker/frontend.Dockerfile" in builds[0] and "backend/Dockerfile" not in builds[0]
    assert list(directory.glob("operation-*.intent.json"))


def test_update_unchanged_component_bytes_is_verified_noop(fixture, monkeypatch):
    directory, value, values, defaults = fixture
    config = resolved(values); existing = runtime(config, defaults)
    calls = []
    empty = {"containers": [], "volumes": [], "networks": []}
    monkeypatch.setattr(codex, "prepare", lambda *_args: (["docker", "compose"], {}, config, existing, codex.stable_inventory(empty), {}))
    monkeypatch.setattr(codex, "inventory", lambda project, _env: existing if project == codex.PROJECT else empty)
    def command(argv, **kwargs):
        calls.append(argv)
        assert "config" in argv and "up" not in argv and "build" not in argv
        return json.dumps(config)
    monkeypatch.setattr(codex, "command", command)
    monkeypatch.setattr(codex, "check_health", lambda *_args: {})
    args = SimpleNamespace(stage="update", services=None, build_changed=False, backend_image=None, web_image=None, image_source_sha=None)
    result = codex.execute_stage(args, directory, value, values, OLD)
    assert result["status"] == "PASS_NOOP" and len(calls) == 1 and value["images"]["backend"]["source_sha"] == OLD


def test_persistent_c03_runner_uses_only_target_case_and_no_secrets(fixture, monkeypatch):
    directory, value, values, defaults = fixture
    config = resolved(values); existing = runtime(config, defaults)
    empty = {"containers": [], "volumes": [], "networks": []}; calls = []
    monkeypatch.setattr(codex, "prepare", lambda *_args: (["docker", "compose"], {}, config, existing, codex.stable_inventory(empty), {}))
    monkeypatch.setattr(codex, "inventory", lambda *_args: empty)
    monkeypatch.setattr(codex, "check_health", lambda *_args: {})
    pnpm = "C:/isolated-tools/pnpm.cmd"
    resolved_commands = []
    monkeypatch.setattr(codex.shutil, "which", lambda name: resolved_commands.append(name) or pnpm)
    fixture_file = directory / "fixtures/c08503.json"
    fixture_file.write_text('{"status":"PASS","existing_id":"preserved"}')
    def command(argv, **kwargs):
        if argv[0] == "git":return OLD
        calls.append((argv, kwargs["environment"]))
        return ""
    monkeypatch.setattr(codex, "command", command)
    result = codex.test_c08503(directory, value, values)
    argv, environment = calls[0]
    assert result["status"] == "PASS" and argv[-2:] == ["--grep", "C085-03"]
    assert argv[0] == pnpm and resolved_commands == ["pnpm.cmd" if codex.os.name == "nt" else "pnpm"]
    assert environment["TV_E2E_URL"] == "http://127.0.0.1:3205" and environment["COMPOSE_PROJECT_NAME"] == codex.PROJECT
    assert environment["TV_C08503_FIXTURE_MANIFEST"] == str(fixture_file)
    assert not any(key in environment for key in ("POSTGRES_PASSWORD", "CODEX_ADMIN_PASSWORD", "CODEX_DESTINATION_PASSWORD"))
    assert json.loads(fixture_file.read_text())["existing_id"] == "preserved"


def test_command_failure_retains_private_output_without_printing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(codex, "ROOT", tmp_path)
    monkeypatch.setattr(codex.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=7, stdout="private-runtime-output", stderr="private-failure-detail"))
    with pytest.raises(ValueError, match="CODEX_COMMAND_FAILED_docker"):
        codex.command(["docker", "buildx", "build"])
    paths = list((tmp_path / ".codex-local/codex-command-failures").glob("*.private.json"))
    assert len(paths) == 1
    diagnostic = json.loads(paths[0].read_text())
    assert diagnostic["returncode"] == 7 and diagnostic["stdout"] == "private-runtime-output" and diagnostic["stderr"] == "private-failure-detail"
    assert not capsys.readouterr().out and not capsys.readouterr().err


def test_destination_fixture_uses_real_me_route_and_existing_exact_target(fixture, monkeypatch):
    directory, value, values, defaults = fixture
    config = resolved(values); existing = runtime(config, defaults)
    empty = {"containers": [], "volumes": [], "networks": []}
    monkeypatch.setattr(codex, "prepare", lambda *_args: ([], {}, config, existing, codex.stable_inventory(empty), {}))
    monkeypatch.setattr(codex, "inventory", lambda *_args: empty)
    monkeypatch.setattr(codex, "check_health", lambda *_args: {})
    target = {"id": "owned-destination", "version": 1, "name": "codex_test_postgres", "sink_type": "POSTGRESQL", "host": "test-postgres", "port": 5432, "database": "tv_codex_destination", "username": "tv_codex_destination"}
    path = directory / "fixtures/test-destination.json"
    path.write_text(json.dumps({"status": "PASS", "project": codex.PROJECT, "api_origin": values["TRACKVANCE_WEB_ORIGIN"], "organization_id": "org-trackvance-demo", "destination_id": target["id"], "version": 1}))
    calls = []
    class Response:
        status = 200
        def __init__(self, url, body):self.url = url; self.body = body
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def read(self, _limit):return json.dumps(self.body).encode()
    class Opener:
        def open(self, request, timeout):
            assert timeout == 30
            relative = request.full_url.removeprefix(values["TRACKVANCE_WEB_ORIGIN"] + "/api/v1")
            calls.append((relative, request.get_method()))
            result = {"/auth/demo": {"csrf_token": "private-session-token"}, "/me": {"organization": {"id": "org-trackvance-demo"}, "user": {"id": "user-demo-admin", "permissions": ["destinations:manage"]}}, "/delivery/destinations/owned-destination": target, "/delivery/destinations/owned-destination/test": {"status": "SUCCESS"}}[relative]
            return Response(request.full_url, result)
    monkeypatch.setattr(codex.urllib.request, "build_opener", lambda *_args: Opener())
    result = codex.fixture_destination(directory, value, values)
    assert result["status"] == "PASS" and calls == [("/auth/demo", "POST"), ("/me", "GET"), ("/delivery/destinations/owned-destination", "GET"), ("/delivery/destinations/owned-destination/test", "POST")]
    assert "private-session-token" not in path.read_text() and values["CODEX_DESTINATION_PASSWORD"] not in path.read_text()
