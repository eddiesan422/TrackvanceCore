"""The product inbox receiver keeps uncertain mutations durable and once-only."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def agent_module():
    path = Path(__file__).parents[1] / "file_inbox_agent.py"
    spec = importlib.util.spec_from_file_location("file_inbox_agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def settings(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    return {"api_origin": "http://127.0.0.1:3205", "inbox": str(inbox),
            "state_directory": str(tmp_path / "state"), "poll_seconds": .01,
            "settle_seconds": .01, "max_file_bytes": 1024 * 1024,
            "min_free_bytes": 0,
            "routes": [{"suffix": ".csv", "dataset_id": "dataset-fixed", "format": "CSV",
                        "expected_header": ["id", "amount"], "reader_options": {"delimiter": ","},
                        "column_overrides": {"id": {"logical_type": "STRING"}}}]}


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.run = None
        self.rows = []
        self.fail_stage = None
        self.fail_register = None
        self.inspection = {"format": "CSV", "columns": [{"name": "id"}, {"name": "amount"}]}

    def stage(self, path, filename):
        import hashlib
        self.calls.append(("stage", filename))
        if self.fail_stage:
            raise self.fail_stage
        return {"upload": {"id": "upload-one", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "size_bytes": path.stat().st_size, "format": self.inspection["format"], "transfer_complete": True}}

    def inspect(self, upload_id, options):
        self.calls.append(("inspect", upload_id))
        return self.inspection

    def register(self, dataset_id, body, key):
        self.calls.append(("register", dataset_id, copy.deepcopy(body), key))
        self.run = {"id": "run-one", "dataset_id": dataset_id, "source_type": "UPLOAD",
                    "status": "QUEUED", "output_version_id": None,
                    "reader_options": body["reader_options"], "column_overrides": body["column_overrides"],
                    "source_snapshot": {"upload_id": body["upload_id"],
                                        "sha256": self.digest, "size_bytes": self.size,
                                        "source_format": self.inspection["format"]}}
        if self.fail_register:
            raise self.fail_register
        return self.run

    def get(self, run_id):
        self.calls.append(("get", run_id))
        return copy.deepcopy(self.run)

    def acquisitions(self, dataset_id, offset):
        self.calls.append(("list", dataset_id, offset))
        return {"items": copy.deepcopy(self.rows[offset:offset + 100]), "total": len(self.rows)}


def prepare(module, settings, api=None):
    import hashlib
    clock = [0.0]
    api = api or FakeAPI()
    config = module.validate_config(settings)
    item = Path(settings["inbox"]) / "original.csv"
    item.write_bytes(b"id,amount\n001,1.2300\n")
    api.digest = hashlib.sha256(item.read_bytes()).hexdigest()
    api.size = item.stat().st_size
    receiver = module.InboxAgent(config, api, clock=lambda: clock[0])
    return receiver, api, clock, item


def receive(receiver, clock):
    receiver.tick()
    clock[0] += 1
    receiver.tick()


def entry(receiver):
    assert len(receiver.journal["entries"]) == 1
    return next(iter(receiver.journal["entries"].values()))


def test_file_arrives_after_agent_start_and_only_stable_bytes_are_received(agent_module, settings):
    receiver, api, clock, item = prepare(agent_module, settings)
    data = item.read_bytes()
    item.unlink()
    receiver.tick()
    assert not api.calls
    item.write_bytes(data)
    receive(receiver, clock)
    assert [call[0] for call in api.calls] == ["stage", "inspect", "register"]
    assert entry(receiver)["state"] == "REGISTERED"


def test_growing_file_never_stages_a_partial_snapshot(agent_module, settings):
    receiver, api, clock, item = prepare(agent_module, settings)
    receiver.tick()
    clock[0] += 1
    item.write_bytes(item.read_bytes() + b"002,2.3400\n")
    receiver.tick()
    assert not api.calls


def test_native_success_requires_output_version_and_preserves_input(agent_module, settings):
    receiver, api, clock, item = prepare(agent_module, settings)
    original = (item.read_bytes(), item.stat().st_mtime_ns)
    receive(receiver, clock)
    api.run.update(status="SUCCESS", output_version_id="version-one")
    receiver.tick()
    assert entry(receiver)["state"] == "SUCCESS"
    assert entry(receiver)["output_version_id"] == "version-one"
    assert (item.read_bytes(), item.stat().st_mtime_ns) == original


def test_restart_resumes_known_acquisition_only_by_get(agent_module, settings):
    receiver, api, clock, _ = prepare(agent_module, settings)
    receive(receiver, clock)
    before = len(api.calls)
    restarted = agent_module.InboxAgent(receiver.config, api, clock=lambda: clock[0])
    restarted.tick()
    assert [call[0] for call in api.calls[before:]] == ["get"]


@pytest.mark.parametrize("failure", ["timeout", "disconnect"])
def test_lost_stage_response_is_never_reposted_even_after_restart(agent_module, settings, failure):
    receiver, api, clock, _ = prepare(agent_module, settings)
    api.fail_stage = agent_module.UncertainResponse(failure)
    receive(receiver, clock)
    assert entry(receiver)["state"] == "UNCERTAIN_STAGE"
    for _ in range(3):
        receiver = agent_module.InboxAgent(receiver.config, api, clock=lambda: clock[0])
        receive(receiver, clock)
    assert len([call for call in api.calls if call[0] == "stage"]) == 1
    assert not any(call[0] == "register" for call in api.calls)


def test_crash_after_stage_intent_does_not_repeat_post(agent_module, settings):
    receiver, api, clock, _ = prepare(agent_module, settings)
    receive(receiver, clock)
    record = entry(receiver)
    record["state"] = "STAGE_INTENT"
    record.pop("upload_id")
    record.pop("acquisition_id")
    receiver.save()
    api.calls.clear()
    receiver = agent_module.InboxAgent(receiver.config, api, clock=lambda: clock[0])
    receiver.tick()
    assert entry(receiver)["state"] == "UNCERTAIN_STAGE"
    assert not api.calls


@pytest.mark.parametrize("matches", [0, 2])
def test_lost_registration_zero_or_multiple_matches_blocks_without_repost(agent_module, settings, matches):
    receiver, api, clock, _ = prepare(agent_module, settings)
    api.fail_register = agent_module.UncertainResponse("lost")
    receive(receiver, clock)
    api.rows = [copy.deepcopy(api.run) for _ in range(matches)]
    for index, candidate in enumerate(api.rows):
        candidate["id"] = "run-" + str(index)
    receiver.tick()
    assert entry(receiver)["state"] == "BLOCKED_RECONCILIATION"
    assert len([call for call in api.calls if call[0] == "register"]) == 1


def test_lost_registration_one_exact_public_match_recovers_by_get(agent_module, settings):
    receiver, api, clock, _ = prepare(agent_module, settings)
    api.fail_register = agent_module.UncertainResponse("lost")
    receive(receiver, clock)
    api.rows = [api.run]
    receiver.tick()
    assert entry(receiver)["state"] == "REGISTERED"
    api.run.update(status="SUCCESS", output_version_id="v1")
    receiver.tick()
    assert entry(receiver)["state"] == "SUCCESS"
    assert len([call for call in api.calls if call[0] == "register"]) == 1


@pytest.mark.parametrize("change", ["reader_options", "column_overrides", "dataset_id", "digest"])
def test_registration_lookup_never_adopts_a_near_match(agent_module, settings, change):
    receiver, api, clock, _ = prepare(agent_module, settings)
    api.fail_register = agent_module.UncertainResponse("lost")
    receive(receiver, clock)
    candidate = copy.deepcopy(api.run)
    if change == "digest":
        candidate["source_snapshot"]["sha256"] = "0" * 64
    else:
        candidate[change] = {"different": True} if change != "dataset_id" else "other"
    api.rows = [candidate]
    receiver.tick()
    assert entry(receiver)["state"] == "BLOCKED_RECONCILIATION"


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED"])
def test_known_terminal_failure_is_preserved_without_automatic_retry(agent_module, settings, status):
    receiver, api, clock, _ = prepare(agent_module, settings)
    receive(receiver, clock)
    api.run.update(status=status, error_code="BAD_SOURCE", output_version_id=None)
    receiver.tick()
    assert entry(receiver)["state"] == status
    for _ in range(3):
        receive(receiver, clock)
    assert len([call for call in api.calls if call[0] == "register"]) == 1


def test_success_without_output_is_not_certified(agent_module, settings):
    receiver, api, clock, _ = prepare(agent_module, settings)
    receive(receiver, clock)
    api.run.update(status="SUCCESS", output_version_id=None)
    receiver.tick()
    assert entry(receiver)["state"] == "BLOCKED_RESPONSE"


@pytest.mark.parametrize("fault", ["hash", "size", "format", "header"])
def test_declared_snapshot_and_header_must_match_before_registration(agent_module, settings, fault):
    receiver, api, clock, _ = prepare(agent_module, settings)
    original_stage = api.stage
    def stage(path, filename):
        result = original_stage(path, filename)
        key = {"hash": "sha256", "size": "size_bytes", "format": "format"}.get(fault)
        if key:
            result["upload"][key] = "bad" if key != "size_bytes" else 99
        return result
    api.stage = stage
    if fault == "header":
        api.inspection["columns"] = [{"name": "amount"}, {"name": "id"}]
    receive(receiver, clock)
    assert entry(receiver)["state"] == ("BLOCKED_RESPONSE" if fault != "header" else "BLOCKED_SCHEMA")
    assert not any(call[0] == "register" for call in api.calls)


def test_completed_content_rename_and_operational_options_do_not_create_new_version(agent_module, settings):
    receiver, api, clock, item = prepare(agent_module, settings)
    receive(receiver, clock)
    api.run.update(status="SUCCESS", output_version_id="v1")
    receiver.tick()
    item.rename(item.with_name("renamed.csv"))
    changed = copy.deepcopy(settings)
    changed.update(poll_seconds=3, settle_seconds=2, request_timeout_seconds=10)
    receiver = agent_module.InboxAgent(agent_module.validate_config(changed), api, clock=lambda: clock[0])
    receiver.tick()
    clock[0] += 10
    receiver.tick()
    assert len(receiver.journal["entries"]) == 1
    assert len([call for call in api.calls if call[0] == "register"]) == 1


def test_business_schema_change_has_distinct_identity(agent_module, settings):
    config = agent_module.validate_config(settings)
    before = agent_module.business_key(config, config["routes"][0], "a" * 64)
    changed = copy.deepcopy(config)
    changed["routes"][0]["expected_header"].append("new")
    assert agent_module.business_key(changed, changed["routes"][0], "a" * 64) != before


def test_os_lock_excludes_second_receiver_and_releases_after_exit(agent_module, settings):
    lock = Path(settings["state_directory"]) / "agent.lock"
    lock.parent.mkdir()
    with agent_module.StateLock(lock), pytest.raises(agent_module.AgentError, match="STATE_LOCKED"), agent_module.StateLock(lock):
        pass
    with agent_module.StateLock(lock):
        pass


@pytest.mark.parametrize("fault", ["relative_inbox", "state_inside_inbox", "unsupported", "duplicate", "credentials"])
def test_explicit_configuration_rejects_ambiguous_routes_and_secret_material(agent_module, settings, fault):
    changed = copy.deepcopy(settings)
    if fault == "relative_inbox":
        changed["inbox"] = "relative"
    elif fault == "state_inside_inbox":
        changed["state_directory"] = str(Path(changed["inbox"]) / "state")
    elif fault == "unsupported":
        changed["routes"][0]["suffix"] = ".pdf"
    elif fault == "duplicate":
        changed["routes"].append(copy.deepcopy(changed["routes"][0]))
    else:
        changed["password"] = "never-write-this"
    with pytest.raises(agent_module.AgentError):
        agent_module.validate_config(changed)


def test_journal_contains_no_raw_errors_or_credentials(agent_module, settings):
    receiver, api, clock, _ = prepare(agent_module, settings)
    api.fail_stage = agent_module.UncertainResponse("password=never-write-this; Cookie=secret-token")
    receive(receiver, clock)
    encoded = receiver.journal_path.read_text(encoding="utf-8")
    assert "never-write-this" not in encoded and "secret-token" not in encoded
    assert json.loads(encoded)["entries"]


@pytest.mark.parametrize("format_name", ["CSV", "TXT", "XLSX"])
def test_each_declared_format_uses_same_stable_dataset_and_public_protocol(agent_module, settings, format_name):
    settings["routes"][0].update(suffix="." + format_name.lower(), format=format_name)
    receiver, api, clock, item = prepare(agent_module, settings)
    item.rename(item.with_suffix("." + format_name.lower()))
    api.inspection["format"] = format_name
    receive(receiver, clock)
    assert entry(receiver)["state"] == "REGISTERED"
    assert api.run["dataset_id"] == "dataset-fixed"
    assert api.run["source_snapshot"]["source_format"] == format_name


def test_changed_content_gets_one_new_native_acquisition(agent_module, settings):
    receiver, api, clock, item = prepare(agent_module, settings)
    receive(receiver, clock)
    api.run.update(status="SUCCESS", output_version_id="v1")
    receiver.tick()
    item.write_bytes(item.read_bytes() + b"002,2.3400\n")
    import hashlib
    api.digest = hashlib.sha256(item.read_bytes()).hexdigest()
    api.size = item.stat().st_size
    receive(receiver, clock)
    assert len(receiver.journal["entries"]) == 2
    assert len([call for call in api.calls if call[0] == "register"]) == 2


def test_stop_after_known_stage_keeps_safe_resume_without_registration(agent_module, settings):
    receiver, api, clock, _ = prepare(agent_module, settings)
    stopped = [False]
    receiver.stopping = lambda: stopped[0]
    original_stage = api.stage
    def stage(*args):
        result = original_stage(*args)
        stopped[0] = True
        return result
    api.stage = stage
    receive(receiver, clock)
    assert entry(receiver)["state"] == "STAGED"
    assert not any(call[0] == "register" for call in api.calls)
    restarted = agent_module.InboxAgent(receiver.config, api, clock=lambda: clock[0])
    restarted.tick()
    assert len([call for call in api.calls if call[0] == "stage"]) == 1
    assert len([call for call in api.calls if call[0] == "register"]) == 1


def test_pending_journal_cannot_be_retargeted_to_another_server(agent_module, settings):
    receiver, _, clock, _ = prepare(agent_module, settings)
    receive(receiver, clock)
    changed = copy.deepcopy(settings)
    changed["api_origin"] = "http://localhost:9999"
    with pytest.raises(agent_module.AgentError, match="JOURNAL_API_ORIGIN_CHANGED"):
        agent_module.InboxAgent(agent_module.validate_config(changed), FakeAPI())


def test_pending_business_route_change_blocks_without_new_mutation(agent_module, settings):
    receiver, api, clock, _ = prepare(agent_module, settings)
    receive(receiver, clock)
    changed = copy.deepcopy(settings)
    changed["routes"][0]["expected_header"].append("different")
    before = len(api.calls)
    with pytest.raises(agent_module.AgentError, match="PENDING_ROUTE_CHANGED"):
        agent_module.InboxAgent(agent_module.validate_config(changed), api)
    assert len(api.calls) == before


def test_immutable_spool_tampering_cannot_be_uploaded(agent_module, settings):
    receiver, api, clock, _ = prepare(agent_module, settings)
    receiver.stopping = lambda: True
    receive(receiver, clock)
    record = entry(receiver)
    (receiver.spool / record["spool_name"]).write_bytes(b"tampered")
    receiver.stopping = lambda: False
    receiver.tick()
    assert record["state"] == "BLOCKED_RESPONSE"
    assert not api.calls


@pytest.mark.parametrize("status", [301, 502, 503, 408])
def test_http_uncertainty_or_redirect_never_retries_a_mutation(agent_module, settings, monkeypatch, status):
    import io
    calls = []
    class Connection:
        def __init__(self, *_args, **_kwargs):
            pass
        def request(self, *args, **kwargs):
            calls.append((args, kwargs))
        def getresponse(self):
            response = io.BytesIO(b'{"error":{"code":"UPSTREAM_FAILED"}}')
            response.status = status
            return response
        def close(self):
            pass
    monkeypatch.setattr(agent_module.http.client, "HTTPConnection", Connection)
    client = agent_module.APIClient(agent_module.validate_config(settings))
    with pytest.raises(agent_module.UncertainResponse):
        client.register("dataset-fixed", {"upload_id": "upload-one"}, "key")
    assert len(calls) == 1


def test_http_upload_streams_fixed_chunks_without_loading_whole_file(agent_module, settings, monkeypatch, tmp_path):
    import io
    chunks, headers, requests = [], {}, []
    class Connection:
        def __init__(self, *_args, **_kwargs):
            pass
        def putrequest(self, *args):
            requests.append(args)
        def putheader(self, name, value):
            headers[name] = value
        def endheaders(self):
            pass
        def send(self, block):
            chunks.append(len(block))
        def getresponse(self):
            response = io.BytesIO(b'{"upload":{"id":"one"}}')
            response.status = 201
            return response
        def close(self):
            pass
    path = tmp_path / "large.csv"
    path.write_bytes(b"x" * (agent_module.BUFFER_BYTES * 3 + 7))
    monkeypatch.setattr(agent_module.http.client, "HTTPConnection", Connection)
    client = agent_module.APIClient(agent_module.validate_config(settings))
    client.stage(path, "large file.csv")
    assert chunks == [agent_module.BUFFER_BYTES] * 3 + [7]
    assert headers["Content-Length"] == str(path.stat().st_size)
    assert requests == [("POST", "/api/v1/datasets/uploads/stage?filename=large+file.csv")]


def test_atomic_journal_intents_are_durable_before_each_post(agent_module, settings):
    receiver, api, clock, _ = prepare(agent_module, settings)
    stage, register = api.stage, api.register
    def checked_stage(*args):
        saved = json.loads(receiver.journal_path.read_text(encoding="utf-8"))
        assert next(iter(saved["entries"].values()))["state"] == "STAGE_INTENT"
        return stage(*args)
    def checked_register(*args):
        saved = json.loads(receiver.journal_path.read_text(encoding="utf-8"))
        durable = next(iter(saved["entries"].values()))
        assert durable["state"] == "REGISTER_INTENT" and durable["upload_id"] == "upload-one"
        return register(*args)
    api.stage, api.register = checked_stage, checked_register
    receive(receiver, clock)
    assert [value["state"] for value in entry(receiver)["history"]] == ["STAGE_INTENT", "STAGED", "REGISTER_INTENT", "REGISTERED"]


def test_foreground_daemon_observes_later_arrival_without_per_file_command(agent_module, settings, monkeypatch, tmp_path, capsys):
    import hashlib
    import time
    item = Path(settings["inbox"]) / "later.csv"
    payload = b"id,amount\n001,1.2300\n"
    api = FakeAPI()
    api.digest, api.size = hashlib.sha256(payload).hexdigest(), len(payload)
    api.login = lambda: None
    class Stop:
        waits = 0
        def is_set(self):
            return self.waits >= 7
        def set(self):
            self.waits = 7
        def wait(self, _seconds):
            self.waits += 1
            if self.waits == 1:
                item.write_bytes(payload)
            if api.run:
                api.run.update(status="SUCCESS", output_version_id="v-later")
            time.sleep(.02)
    config_path = tmp_path / "operator.json"
    config_path.write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setattr(agent_module.threading, "Event", Stop)
    monkeypatch.setattr(agent_module.signal, "signal", lambda *_: None)
    monkeypatch.setattr(agent_module, "APIClient", lambda _config: api)
    assert agent_module.main(["--config", str(config_path)]) == 0
    states = [json.loads(line)["status"] for line in capsys.readouterr().out.splitlines()]
    assert states == ["WATCHING", "STOPPED"]
    saved = json.loads((Path(settings["state_directory"]) / "journal.json").read_text(encoding="utf-8"))
    assert next(iter(saved["entries"].values()))["state"] == "SUCCESS"
    assert len([call for call in api.calls if call[0] == "register"]) == 1


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("new_route", [False, True])
def test_unknown_registration_fences_all_new_arrivals_and_routes(agent_module, settings, restart, new_route):
    receiver, api, clock, item = prepare(agent_module, settings)
    api.fail_register = agent_module.UncertainResponse("lost")
    receive(receiver, clock)
    receiver.tick()
    assert entry(receiver)["state"] == "BLOCKED_RECONCILIATION"
    api.fail_register = None
    if new_route:
        changed = copy.deepcopy(settings)
        route = copy.deepcopy(changed["routes"][0])
        route.update(suffix=".txt", format="TXT", dataset_id="other-dataset")
        changed["routes"].append(route)
        receiver = agent_module.InboxAgent(agent_module.validate_config(changed), api, clock=lambda: clock[0])
    elif restart:
        receiver = agent_module.InboxAgent(receiver.config, api, clock=lambda: clock[0])
    (item.parent / ("second.txt" if new_route else "second.csv")).write_bytes(b"id,amount\n002,9.8700\n")
    before = len([call for call in api.calls if call[0] in {"stage", "register"}])
    for _ in range(8):
        clock[0] += 1
        receiver.tick()
    assert len([call for call in api.calls if call[0] in {"stage", "register"}]) == before
    assert len(receiver.journal["entries"]) == 1


@pytest.mark.parametrize("restart", [False, True])
def test_invalid_response_for_known_run_fences_a_second_file(agent_module, settings, restart):
    receiver, api, clock, item = prepare(agent_module, settings)
    receive(receiver, clock)
    api.run["status"] = "INVALID"
    receiver.tick()
    assert entry(receiver)["state"] == "BLOCKED_RESPONSE"
    if restart:
        receiver = agent_module.InboxAgent(receiver.config, api, clock=lambda: clock[0])
    (item.parent / "second.csv").write_bytes(b"id,amount\n002,9.8700\n")
    for _ in range(8):
        clock[0] += 1
        receiver.tick()
    assert len([call for call in api.calls if call[0] == "register"]) == 1
    assert len(receiver.journal["entries"]) == 1


def test_later_exact_lookup_after_restart_resolves_fence_without_a_second_post(agent_module, settings):
    receiver, api, clock, _ = prepare(agent_module, settings)
    api.fail_register = agent_module.UncertainResponse("lost")
    receive(receiver, clock)
    receiver.tick()
    assert entry(receiver)["state"] == "BLOCKED_RECONCILIATION"
    api.rows = [api.run]
    receiver = agent_module.InboxAgent(receiver.config, api, clock=lambda: clock[0])
    receiver.tick()
    assert entry(receiver)["state"] == "REGISTERED"
    assert len([call for call in api.calls if call[0] == "register"]) == 1


def test_in_place_copy_preserving_size_and_mtime_still_detects_changed_content(agent_module, settings):
    import hashlib
    import os
    receiver, api, clock, item = prepare(agent_module, settings)
    receive(receiver, clock)
    api.run.update(status="SUCCESS", output_version_id="v1")
    receiver.tick()
    stat = item.stat()
    payload = item.read_bytes().replace(b"001", b"002")
    item.write_bytes(payload)
    os.utime(item, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    api.digest, api.size = hashlib.sha256(payload).hexdigest(), len(payload)
    for _ in range(4):
        clock[0] += 1
        receiver.tick()
    assert len([call for call in api.calls if call[0] == "register"]) == 2
    assert len(receiver.journal["entries"]) == 2


def test_explicit_api_origin_supports_owned_local_network_http_alias(agent_module, settings):
    settings["api_origin"] = "http://api:8000"
    assert agent_module.validate_config(settings)["api_origin"] == "http://api:8000"


@pytest.mark.parametrize("upload", [None, [], "invalid", 1, True])
def test_invalid_stage_upload_shape_is_uncertain_durable_and_never_reposted(agent_module, settings, upload):
    receiver, api, clock, _ = prepare(agent_module, settings)
    posts = []
    def stage(*_args):
        posts.append(1)
        return {"upload": upload}
    api.stage = stage
    receive(receiver, clock)
    assert entry(receiver)["state"] == "UNCERTAIN_STAGE"
    for _ in range(3):
        receiver = agent_module.InboxAgent(receiver.config, api, clock=lambda: clock[0])
        receive(receiver, clock)
    assert len(posts) == 1 and not any(call[0] == "register" for call in api.calls)


def test_journal_size_bound_rejects_before_replacing_durable_state(agent_module, settings, monkeypatch):
    receiver, _, _, _ = prepare(agent_module, settings)
    receiver.save()
    original = receiver.journal_path.read_bytes()
    monkeypatch.setattr(agent_module, "MAX_JOURNAL_BYTES", len(original) + 1)
    receiver.journal["entries"]["oversize"] = {"literal": "x" * 1024}
    with pytest.raises(agent_module.AgentError, match="JOURNAL_SIZE_LIMIT"):
        receiver.save()
    assert receiver.journal_path.read_bytes() == original


def test_oversized_existing_journal_is_not_loaded(agent_module, settings, monkeypatch):
    receiver, api, _, _ = prepare(agent_module, settings)
    receiver.save()
    monkeypatch.setattr(agent_module, "MAX_JOURNAL_BYTES", receiver.journal_path.stat().st_size - 1)
    with pytest.raises(agent_module.AgentError, match="JOURNAL_SIZE_LIMIT"):
        agent_module.InboxAgent(receiver.config, api)
