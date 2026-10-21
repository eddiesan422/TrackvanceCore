"""Only disposable connection test resources may be provisioned or deleted."""

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

spec = importlib.util.spec_from_file_location("connections_cycle", Path(__file__).with_name("connections_cycle.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize("project", ["trackvance-certification", "trackvance-core", "trackvance-e2e-other", "other"])
def test_connections_runner_rejects_nonisolated_projects(project):
    with pytest.raises(ValueError, match="proyecto aislado"):
        runner.validated_project_name(project)


@pytest.mark.parametrize("kind", ["container", "volume"])
def test_connections_runner_never_mutates_existing_resources(monkeypatch, tmp_path, kind):
    calls = []

    def execute(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[-1] == "label=com.docker.compose.project=trackvance-certification":
            return ""
        if arguments[1:3] == ["ps", "-aq"] and kind == "container":
            return "existing-container"
        if arguments[1:3] == ["volume", "ls"] and kind == "volume":
            return "existing-volume"
        return ""

    monkeypatch.setattr(runner, "execute", execute)
    monkeypatch.setattr(runner.sys, "argv", ["connections_cycle.py", "--project", "trackvance-connections-e2e-existing", "--port", "3200", "--evidence-dir", str(tmp_path)])
    assert runner.main() == 1
    assert not any("up" in command or "down" in command for command in calls)


def test_connections_runner_failure_cleans_only_new_project_and_redacts_secrets(monkeypatch, tmp_path):
    calls = []
    project = "trackvance-connections-e2e-new"

    def execute(arguments, **kwargs):
        calls.append(arguments)
        if "up" in arguments:
            raise RuntimeError("Cannot start: " + kwargs["environment"]["SOURCE_POSTGRES_PASSWORD"])
        return ""

    monkeypatch.setattr(runner, "execute", execute)
    monkeypatch.setattr(runner.secrets, "token_hex", lambda _length: "never-persist-this-credential")
    monkeypatch.setattr(runner.sys, "argv", ["connections_cycle.py", "--project", project, "--port", "3200", "--evidence-dir", str(tmp_path)])
    assert runner.main() == 1
    deletion_calls = [arguments for arguments in calls if "down" in arguments]
    assert deletion_calls == [["docker", "compose", "--env-file", str(tmp_path / "private.empty.env"),
                              "-p", project, "-f", "compose.yml", "-f", "deploy/docker/compose.connections-test.yml",
                              "-f", str(tmp_path / "private-compose.json"), "down", "-v", "--remove-orphans"]]
    evidence = (tmp_path / "result.json").read_text(encoding="utf-8")
    assert "never-persist-this-credential" not in evidence
    assert "[REDACTED]" in evidence


@pytest.mark.parametrize("source_type,count", [("POSTGRESQL", 14), ("SQLSERVER", 18)])
def test_temporal_fixture_covers_every_precision_and_timezone_policy(source_type, count):
    columns, expected, logical = runner.temporal_fixture(source_type)
    assert len(columns) == len(expected) == len(logical) == count
    assert len({name for name, _, _ in columns}) == count
    assert all(logical[f"aware_{precision}"] == "TIMESTAMP" for precision in range(7))
    assert all(logical[f"naive_{precision}"] == "STRING" for precision in range(7))
    if source_type == "POSTGRESQL":
        assert expected["aware_6"] == "2026-09-23T15:00:00.123456+00:00"
        assert expected["naive_6"] == "2026-09-23T10:00:00.123456"
    else:
        assert logical["aware_7"] == logical["naive_7"] == "STRING"
        assert expected["aware_7"] == "2026-09-23T15:00:00.1234567Z"
        assert expected["naive_7"] == "2026-09-23T10:00:00.1234567"
        assert logical["legacy_datetime"] == logical["legacy_small"] == "STRING"


@pytest.mark.parametrize("status,code", [
    (401, "UNAUTHENTICATED"), (403, "CSRF_FAILED"),
    (422, "SOURCE_UNAVAILABLE"), (503, "CREDENTIAL_STORE_UNAVAILABLE"),
    (409, "PRIVATE_TOKEN"), (500, {"private": "PRIVATE_TOKEN"}),
])
def test_unexpected_source_http_remains_failure_and_exports_only_safe_facts(monkeypatch, status, code):
    from ci.failure_diagnostics import sanitize_compose_http_failure

    api = runner.smoke.Api("http://127.0.0.1:3200", timeout=60)
    body = json.dumps({"error": {"code": code, "message": "PRIVATE_TOKEN"},
                       "url": "https://PRIVATE_TOKEN", "row": "PRIVATE_ROW"}).encode()

    def open_request(request, *, timeout):
        assert timeout == 60 and request.method == "POST"
        raise HTTPError(request.full_url, status, "PRIVATE_TOKEN", {}, io.BytesIO(body))

    monkeypatch.setattr(api.opener, "open", open_request)
    with pytest.raises(runner.smoke.SmokeFailure) as raised:
        api.post("/api/v1/connections/private-uuid/test", {}, expected=(200,))
    failure = raised.value
    result = sanitize_compose_http_failure({
        "http_status": failure.http_status, "api_error_code": failure.api_error_code,
        "message": str(failure), "url": "https://PRIVATE_TOKEN", "body": body.decode(),
    })
    assert result == {"http_status": status, **(
        {"api_error_code": code} if status in {401, 403, 422, 503} else {})}
    assert all(value not in json.dumps(result) for value in ("PRIVATE_TOKEN", "PRIVATE_ROW", "private-uuid"))


@pytest.mark.parametrize("body", [b"<html>PRIVATE_TOKEN</html>", b"[" * 2000 + b"]" * 2000,
    b'{"error":{"code":"CSRF_FAILED"},"message":"' + b"x" * 8192 + b'"}'])
def test_unexpected_proxy_body_retains_status_without_parsing_or_retrying(monkeypatch, body):
    from ci.failure_diagnostics import sanitize_compose_http_failure

    api = runner.smoke.Api("http://127.0.0.1:3200", timeout=60)
    calls = []

    def open_request(request, *, timeout):
        calls.append(request)
        raise HTTPError(request.full_url, 502, "PRIVATE_TOKEN", {}, io.BytesIO(body))

    monkeypatch.setattr(api.opener, "open", open_request)
    with pytest.raises(runner.smoke.SmokeFailure) as raised:
        api.post("/api/v1/connections/private-uuid/test", {}, expected=(200,))
    assert len(calls) == 1
    assert sanitize_compose_http_failure(vars(raised.value)) == {"http_status": 502}


@pytest.mark.parametrize("upstream", ["ready", "http502_then_ready", "http502", "starting", "timeout"])
def test_post_restart_sources_require_ready_web_route_without_repeating_posts(monkeypatch, tmp_path, upstream):
    """A healthy static page is insufficient; model a stale/unready upstream."""
    calls, posts, probes, waits = [], [], [], []
    state = {"clock": 0.0, "backend_ready": False, "web_fresh": False, "restarting": False}
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: state["clock"],
        sleep=lambda seconds: state.update(clock=state["clock"] + seconds)), raising=False)
    monkeypatch.delenv("TRACKVANCE_LOCAL_EXECUTION_ID", raising=False)

    def isolate(compose, _environment, evidence, _project):
        (evidence / "private-compose.json").write_text('{"services": {}}', encoding="utf-8")
        return compose

    def execute(arguments, **kwargs):
        calls.append(arguments)
        if arguments == ["docker", "context", "show"]:
            return "default"
        if "restart" in arguments:
            state["restarting"] = True
            services = arguments[arguments.index("restart") + 1:]
            if "api" in services:
                state["backend_ready"] = False
            if "web" in services:
                state["web_fresh"] = state["backend_ready"]
        if "up" in arguments and "--wait-timeout" in arguments:
            timeout_index = arguments.index("--wait-timeout")
            if state["restarting"]:
                waits.append(int(arguments[timeout_index + 1]))
            if not state["backend_ready"]:
                state["backend_ready"] = True
                state["clock"] += 181 if upstream == "timeout" else 50
        if "python" in arguments and arguments[-1] == "-":
            if "certify_sqlserver_streaming(" in kwargs.get("input_text", ""):
                return json.dumps({"status": "PASS", "rows": 1028,
                                   "full_scan": {"row_count": 1028}, "cancellation": {"status": "PASS"}})
            return '{"roundtrip": "PASS"}'
        return ""

    instances = []

    class Api:
        def __init__(self, base_url, timeout):
            self.base_url, self.timeout, self.csrf = base_url, timeout, None
            instances.append(self)

        def request(self, *_args, **_kwargs):
            return None

        def get(self, path):
            if path == "/api/v1/health":
                return {"version": "0.9.7"}
            if path == "/api/v1/health/ready":
                probes.append({"path": path, "timeout": self.timeout,
                               "backend_ready": state["backend_ready"], "web_fresh": state["web_fresh"]})
                transient = upstream == "http502_then_ready" and len(probes) == 1
                state["clock"] += min(40, self.timeout) if upstream in {"http502", "starting"} or transient else .1
                if upstream == "http502" or transient or not state["web_fresh"]:
                    raise runner.smoke.SmokeFailure("Synthetic upstream failure", http_status=502)
                return {"status": "starting" if upstream == "starting" else "ready"}
            if path.startswith("/api/v1/runs/"):
                return {"status": "SUCCESS"}
            if path == "/api/v1/audit-events":
                return []
            raise AssertionError("Unexpected synthetic GET")

        def post(self, path, _body, **_kwargs):
            if path == "/api/v1/auth/demo":
                return {"csrf_token": "synthetic-csrf"}
            posts.append(path)
            if not probes or upstream not in {"ready", "http502_then_ready"}:
                raise runner.smoke.SmokeFailure("Unsafe POST reached synthetic unready upstream", http_status=502)
            return {"status": "SUCCESS"}

    monkeypatch.setattr(runner, "isolate_compose", isolate)
    monkeypatch.setattr(runner, "main_inventory", lambda _reader: [])
    monkeypatch.setattr(runner, "assert_main_unchanged", lambda *_args: None)
    monkeypatch.setattr(runner, "runtime_diagnostics", lambda *_args: {"services": []})
    monkeypatch.setattr(runner, "execute", execute)
    monkeypatch.setattr(runner, "certify_read_only_accounts", lambda *_args: None)
    monkeypatch.setattr(runner, "certify_source", lambda _api, _checks, source, *_args: {
        "source_type": source, "connection_id": source.lower(), "run_id": source.lower() + "-run"})
    monkeypatch.setattr(runner, "certify_temporal_snapshots", lambda *_args: {})
    monkeypatch.setattr(runner.smoke, "Api", Api)
    monkeypatch.setattr(runner.sys, "argv", ["connections_cycle.py", "--project", "trackvance-connections-e2e-route",
        "--port", "3200", "--streaming-rows", "1028", "--skip-playwright", "--skip-regression",
        "--evidence-dir", str(tmp_path)])

    success = upstream in {"ready", "http502_then_ready"}
    assert runner.main() == (0 if success else 1)
    assert instances[0].timeout == 60
    if success:
        assert len(probes) == (2 if upstream == "http502_then_ready" else 1)
        assert all(p["backend_ready"] and p["web_fresh"] for p in probes)
        assert posts == ["/api/v1/connections/postgresql/test", "/api/v1/connections/sqlserver/test"]
        assert len(waits) == 2 and waits[0] == 180 and 0 < waits[1] < 180
        assert 0 < probes[0]["timeout"] <= 60
    else:
        assert posts == [], "No uncertain POST may be submitted or retried before route readiness"
        result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
        assert result["failed_stage"] == "restart_readiness"
        if upstream == "http502":
            assert result["http_failure"] == {"http_status": 502}
        if upstream == "timeout":
            assert result["error_type"] == "TimeoutError"
            assert not probes
            assert not any("restart" in call and call[-1] == "web" for call in calls)
