"""HTTP closure, fencing and recovery with synthetic recognizers only."""
import base64
import hashlib
import io
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_idp_gateway_098 import (
    HTTPConnection,
    post,
)
from test_idp_gateway_098 import (
    endpoint as endpoint,  # noqa: PLC0414 - shared pytest fixture export.
)
from test_idp_gateway_098 import (
    gateway as gateway,  # noqa: PLC0414 - shared pytest fixture export.
)


def body(gateway, number=1):
    raw = b"synthetic runtime boundary"
    return {"request_id": f"{number:032x}", "admission_id": f"{number + 100:032x}",
        "rpc_contract": gateway.JOURNAL_MODULE.VERSION, "configuration": {"max_parallel_jobs": 2},
        "source_sha256": hashlib.sha256(raw).hexdigest(), "source": base64.b64encode(raw).decode(),
        "filename": "document.pdf", "definition": {"fields": [], "tables": []}}


def status(endpoint, identity):
    connection = HTTPConnection(*endpoint, timeout=2)
    try:
        connection.request("GET", "/status/" + identity)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def recover_body(execution):
    return {key: execution[key] for key in ("request_id", "admission_id", "request_sha256", "configuration_sha256")}


@pytest.mark.parametrize("error,expected", [(TimeoutError(), "TIMEOUT"), (ValueError("PADDLE_FAILURE"), "FAILED")])
def test_legacy_failure_keeps_durable_terminal_after_transient_state_removal(gateway, endpoint, monkeypatch, error, expected):
    request = body(gateway)

    def fail(self, value):
        gateway.request_phase(value["request_id"], "PREDICT_NEXT", page=2, expected=3)
        gateway.request_checkpoint(value["request_id"], {"original": "synthetic page"}, page=1)
        raise error

    monkeypatch.setattr(gateway.Handler, "extract", fail)
    result, response = post(endpoint, "/extract", request)
    assert result in {422, 504}
    assert response["execution"]["status"] == expected
    assert request["request_id"] not in gateway.STATES and not gateway.ADMISSIONS
    code, terminal = status(endpoint, request["request_id"])
    assert code == 200
    assert terminal["execution"]["status"] == expected
    assert terminal["execution"]["resources_released"] is True
    assert terminal["execution"]["completed_pages"] == [1]
    assert terminal["execution"]["pending_pages"] == [2, 3]
    assert not gateway.GATE.locked() and not gateway.ACTIVE and not gateway.UPSTREAM
    assert post(endpoint, "/recover", recover_body(terminal["execution"]))[0] == 422


def test_terminal_is_on_disk_before_heavy_gate_is_released(gateway, endpoint, monkeypatch):
    request = body(gateway)
    actual_gate = gateway.GATE
    releases = []

    class CheckedGate:
        def acquire(self, **kwargs):
            return actual_gate.acquire(**kwargs)

        def release(self):
            execution = gateway.JOURNAL.get(request["request_id"])
            assert execution["status"] == "COMPLETE" and execution["resources_released"] is True
            assert json.loads((gateway.JOURNAL.root / (request["request_id"] + ".json")).read_bytes())["status"] == "COMPLETE"
            releases.append(execution["admission_id"])
            actual_gate.release()

    monkeypatch.setattr(gateway, "GATE", CheckedGate())
    monkeypatch.setattr(gateway.Handler, "extract", lambda self, value: {"source_sha256": value["source_sha256"], "engine": {}})
    code, result = post(endpoint, "/extract", request)
    assert code == 200 and result["engine"]["rpc_execution"]["status"] == "COMPLETE"
    assert releases == [request["admission_id"]]


def test_result_serialization_failure_has_a_terminal_without_private_echo(gateway, endpoint, monkeypatch):
    request = body(gateway)
    monkeypatch.setattr(gateway.Handler, "extract", lambda self, value: {"engine": {}, "private": object()})
    code, result = post(endpoint, "/extract", request)
    assert code == 422 and result["error"] == "RESULT_SERIALIZATION"
    assert result["execution"]["status"] == "FAILED" and result["execution"]["resources_released"] is True
    assert "private" not in result
    assert status(endpoint, request["request_id"])[0] == 200
    assert not gateway.GATE.locked()


def test_completed_result_recovery_never_invokes_recognizer_again(gateway, endpoint, monkeypatch):
    calls = []
    request = body(gateway)

    def extract(self, value):
        calls.append(value["request_id"])
        return {"source_sha256": value["source_sha256"], "lines": [{"text": "synthetic source"}], "engine": {}}

    monkeypatch.setattr(gateway.Handler, "extract", extract)
    code, result = post(endpoint, "/extract", request)
    assert code == 200
    receipt = result["engine"]["rpc_execution"]
    code, recovered = post(endpoint, "/recover", recover_body(receipt))
    assert code == 200 and recovered["result"]["lines"] == result["lines"]
    assert recovered["result_sha256"] == gateway.JOURNAL_MODULE.digest(recovered["result"])
    assert calls == [request["request_id"]]
    code, duplicate = post(endpoint, "/extract", request)
    assert code == 422 and duplicate["error"] == "DUPLICATE_REQUEST"
    assert duplicate["execution"]["admission_id"] == receipt["admission_id"]
    assert calls == [request["request_id"]]


def test_cancel_requires_current_admission_nonce(gateway, endpoint, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    request = body(gateway)
    replies = []

    def extract(self, value):
        entered.set()
        assert release.wait(timeout=2)
        if value["request_id"] in gateway.CANCELLED:
            raise ValueError("CANCELLED")
        return {"source_sha256": value["source_sha256"], "engine": {}}

    monkeypatch.setattr(gateway.Handler, "extract", extract)
    worker = threading.Thread(target=lambda: replies.append(post(endpoint, "/extract", request)))
    try:
        worker.start()
        assert entered.wait(timeout=2)
        assert post(endpoint, "/cancel", {"request_id": request["request_id"], "admission_id": "f" * 32}) == (200, {"cancelled": False})
        assert request["request_id"] not in gateway.CANCELLED
        assert post(endpoint, "/cancel", {"request_id": request["request_id"], "admission_id": request["admission_id"]}) == (200, {"cancelled": True})
        release.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert replies[0][1]["execution"]["status"] == "CANCELLED"
        assert replies[0][1]["execution"]["cancel_outcome"] == "ACKNOWLEDGED"
        assert not gateway.CANCELLED and not gateway.ADMISSIONS
    finally:
        release.set()
        worker.join(timeout=2)


def test_cleanup_failure_cannot_publish_success_or_claim_resource_release(gateway, endpoint, monkeypatch):
    request = body(gateway)

    def cleanup(identity, budget, *, cleanup=False):
        if cleanup:
            raise gateway.MEMORY.UnsafeLifecycle("GPU_OWNER_QUIESCENCE_UNVERIFIED")
        return {"status": "EMPTY_RESIDENCY_OBSERVED"}

    monkeypatch.setattr(gateway, "memory_barrier", cleanup)
    monkeypatch.setattr(gateway.Handler, "extract", lambda self, value: {"source_sha256": value["source_sha256"], "engine": {}})
    code, result = post(endpoint, "/extract", request)
    assert code == 503
    assert result["execution"]["status"] == "FAILED"
    assert result["execution"]["resources_released"] is False
    assert result["execution"]["cleanup_error_code"] == "GPU_OWNER_QUIESCENCE_UNVERIFIED"
    assert gateway.MEMORY_STATE["status"] == "UNAVAILABLE" and gateway.JOURNAL.quarantined
    assert post(endpoint, "/recover", recover_body(result["execution"]))[0] == 422


def test_queued_cancel_has_terminal_without_claiming_another_requests_gate(gateway, endpoint):
    request = body(gateway)
    replies = []
    gateway.GATE.acquire()
    worker = threading.Thread(target=lambda: replies.append(post(endpoint, "/extract", request)))
    try:
        worker.start()
        deadline = time.monotonic() + 1
        while request["request_id"] not in gateway.ADMISSIONS and time.monotonic() < deadline:
            time.sleep(.01)
        assert request["request_id"] in gateway.ADMISSIONS
        assert post(endpoint, "/cancel", {"request_id": request["request_id"], "admission_id": request["admission_id"]}) == (200, {"cancelled": True})
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert replies[0][1]["execution"]["status"] == "CANCELLED"
        assert replies[0][1]["execution"]["resources_released"] is True
        assert gateway.GATE.locked()
    finally:
        gateway.GATE.release()
        worker.join(timeout=2)


def test_disconnected_http_client_can_recover_complete_bytes_without_new_work(gateway, endpoint, monkeypatch):
    request = body(gateway)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def extract(self, value):
        calls.append(value["request_id"])
        entered.set()
        assert release.wait(timeout=2)
        return {"source_sha256": value["source_sha256"], "lines": [{"text": "synthetic raw"}], "engine": {}}

    monkeypatch.setattr(gateway.Handler, "extract", extract)
    raw = json.dumps(request).encode()
    client = socket.create_connection(endpoint, timeout=2)
    try:
        client.sendall(b"POST /extract HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(raw)).encode() + b"\r\n\r\n" + raw)
        assert entered.wait(timeout=2)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        client.close()
        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            execution = gateway.JOURNAL.get(request["request_id"])
            if execution["response_delivery"] == "DISCONNECTED":
                break
            time.sleep(.01)
        assert execution["status"] == "COMPLETE" and execution["response_delivery"] == "DISCONNECTED"
        assert execution["resources_released"] is True
        code, recovered = post(endpoint, "/recover", recover_body(execution))
        assert code == 200 and recovered["result"]["lines"] == [{"text": "synthetic raw"}]
        assert calls == [request["request_id"]] and not gateway.GATE.locked()
    finally:
        client.close()
        release.set()


def test_incomplete_qwen_page_checkpoint_keeps_page_pending(gateway):
    request = body(gateway)
    receipt = gateway.JOURNAL.admit("/recognize", request, SimpleNamespace(duration=30))
    gateway.ADMISSION_TOKENS[request["request_id"]] = receipt["admission_id"]
    gateway.request_update(request["request_id"], expected_pages=1)
    session = object.__new__(gateway.RECOGNITION.Session)
    session.identity, session.sequence, session.total, session.started = request["request_id"], 0, 0, False
    session.api = {"request_checkpoint": gateway.request_checkpoint}
    session.budget = SimpleNamespace(join_remaining=lambda: 1)
    session.handler = SimpleNamespace(connection=SimpleNamespace(settimeout=lambda value: None),
        send_response=lambda value: None, send_header=lambda *args: None, end_headers=lambda: None,
        wfile=io.BytesIO())
    session.write({"sequence": 1, "kind": "PAGE", "stage": "QWEN_VISUAL", "page_one_based": 1,
                   "payload": {"recognition_status": "INCOMPLETE"}})
    value = gateway.JOURNAL.get(request["request_id"])
    assert value["emitted_pages"] == [1] and value["completed_pages"] == [] and value["pending_pages"] == [1]
    assert value["page_refs"][0]["complete"] is False


@pytest.mark.skipif(os.name != "posix", reason="Actual owned POSIX session with pidfd signalling")
def test_reaps_live_owned_descendant_after_its_original_parent_exits(gateway):
    script = """
import subprocess,sys,time
child=subprocess.Popen([sys.executable,'-I','-B','-c','import time; time.sleep(20)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print(child.pid,flush=True)
time.sleep(.1)
"""
    process = subprocess.Popen([sys.executable, "-I", "-B", "-c", script],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    owned = gateway.child_identity(process)
    try:
        descendant = int(process.stdout.readline())
        process.wait(timeout=2)
        assert process.returncode == 0
        proof = gateway.real_process_group_quiescence(process, gateway.MEMORY.Budget(30, 2), start_token=owned["start_token"])
        assert proof["status"] == "NO_LIVE_OWNED_SESSION_MEMBERS"
        assert any(member["pid"] == descendant and member["sid"] == process.pid for member in proof["signalled_members"])
        if Path(f"/proc/{descendant}/stat").exists():
            assert Path(f"/proc/{descendant}/stat").read_text().rsplit(")", 1)[1].split()[0] in {"Z", "X"}
    finally:
        try:
            os.killpg(process.pid, 9)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
        process.stdout.close()


@pytest.mark.skipif(os.name != "posix", reason="Actual owned POSIX session with pidfd signalling")
def test_different_leader_start_identity_is_never_signalled(gateway):
    process = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(20)"], start_new_session=True)
    try:
        with pytest.raises(gateway.MEMORY.UnsafeLifecycle):
            gateway.real_process_group_quiescence(process, gateway.MEMORY.Budget(30, 2), start_token="0")
        assert process.poll() is None
    finally:
        os.killpg(process.pid, 9)
        process.wait(timeout=2)


@pytest.mark.skipif(os.name != "posix", reason="Actual POSIX child group, no GPU")
def test_timeout_reaps_real_cpu_child_and_retains_its_completed_page(gateway, endpoint, monkeypatch):
    request = body(gateway)
    request["configuration"].update(timeout_seconds=30, paddle_model="PaddleOCR-VL-1.6",
        llm_model=gateway.LOCK["qwen"]["model"], prompt_version="trackvance-idp-098-v1")
    request["request_budget_seconds"] = 1.2
    actual_popen = subprocess.Popen
    child_script = """
import hashlib,json,os,sys,time
from pathlib import Path
p=Path(sys.argv[1]); source=json.loads((p/'request.json').read_bytes())['source_sha256']; hashes=json.loads(sys.argv[2])
page={'source_sha256':source,'source_hashes':hashes,'page':1,'complete_page':True,'original':{'synthetic':'raw'},'lines':[]}
raw=json.dumps(page).encode(); name='legacy-page-001.json'
with (p/name).open('wb') as f: f.write(raw); f.flush(); os.fsync(f.fileno())
manifest={'source_sha256':source,'source_hashes':hashes,'expected_pages':2,'pages':[{'page':1,'complete_page':True,'artifacts':[{'path':name,'kind':'PADDLE_LEGACY_PAGE_JSON','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}]}]}
pending=p/'legacy.manifest.json.pending'
with pending.open('wb') as f: f.write(json.dumps(manifest).encode()); f.flush(); os.fsync(f.fileno())
pending.replace(p/'legacy.manifest.json')
time.sleep(10)
"""

    def cpu_child(command, **kwargs):
        return actual_popen([sys.executable, "-I", "-B", "-c", child_script, command[-1], json.dumps(gateway.CHILD_SOURCE_HASHES)], **kwargs)

    gateway.RUNTIME["ready"] = True
    monkeypatch.setattr(gateway, "verify_models", lambda: True)
    monkeypatch.setattr(gateway.subprocess, "Popen", cpu_child)
    monkeypatch.setattr(gateway.MEMORY, "process_group_quiescence", gateway.real_process_group_quiescence)
    code, result = post(endpoint, "/extract", request)
    assert code == 504 and result["error"] == "INFERENCE_TIMEOUT"
    execution = result["execution"]
    assert execution["completed_pages"] == [1] and execution["pending_pages"] == [2]
    assert execution["child"]["returncode"] == -9
    assert execution["child"]["start_token"] is not None
    assert execution["resources_released"] is True
    assert not gateway.ACTIVE and not gateway.GATE.locked()
    assert status(endpoint, request["request_id"])[1]["execution"]["status"] == "TIMEOUT"
