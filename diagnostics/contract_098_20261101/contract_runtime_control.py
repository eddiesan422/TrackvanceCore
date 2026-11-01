"""Bounded administration of two exact existing diagnostic GPU containers.

Paths, identities and exact manager arguments are private frozen input. The
verify-stopped mode is read-only. Start/stop require explicit ROOT authorization.
No old campaign helper, model call, DB, snapshot or global cleanup is executed.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import time

OVERRIDES = {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}


def need(value, reason):
    if not value:
        raise RuntimeError(reason)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha(path)}


def read(pin):
    need(sha(pin["path"]) == pin["sha256"], "PIN_CHANGED")
    return json.loads(Path(pin["path"]).read_bytes())


def save(path, value):
    with Path(path).open("xb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    return ref(path)


def command(args, timeout=30):
    result = subprocess.run(list(map(str, args)), capture_output=True, timeout=timeout,
        env={key: value for key, value in os.environ.items() if key not in OVERRIDES},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    need(result.returncode == 0, "ADMIN_OR_READ_COMMAND_FAILED_NO_RETRY")
    return result.stdout


def docker(*args, timeout=30):
    return command(["docker", "--context", "default", *args], timeout=timeout)


def mounts(row):
    values = {item["Destination"]: item for item in row["Mounts"]}
    need(len(values) == len(row["Mounts"]), "DUPLICATE_MOUNT_DESTINATION")
    return values


def changes(before, after, prefix=()):
    """Dictionary keys are indivisible path segments, including literal '/'."""
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(set(before) | set(after)):
            if key not in before or key not in after:
                result.append({"segments": prefix + (key,), "before_present": key in before,
                               "after_present": key in after, "before": before.get(key), "after": after.get(key)})
            else:
                result.extend(changes(before[key], after[key], prefix + (key,)))
        return result
    return [] if type(before) is type(after) and before == after else [
        {"segments": prefix, "before_present": True, "after_present": True, "before": before, "after": after}]


def validate_container(previous, row, runtime, running):
    need(row["Id"] == previous["Id"] and row["Image"] == previous["Image"]
         and row["Config"] == previous["Config"] and row["HostConfig"] == previous["HostConfig"]
         and mounts(row) == mounts(previous), "PROTECTED_IMAGE_CONFIG_HOSTCONFIG_MOUNTS_CHANGED")
    selected = {runtime["gateway_id"], runtime["ollama_id"]}
    if row["Id"] not in selected:
        need(row["State"] == previous["State"] and row["NetworkSettings"] == previous["NetworkSettings"],
             "NON_GPU2_STATE_OR_NETWORK_CHANGED")
        return None
    role = "gateway" if row["Id"] == runtime["gateway_id"] else "ollama"
    need(row["Image"] == runtime[role + "_image"] and row["State"]["Running"] is running
         and (row["State"]["Pid"] > 0 if running else row["State"]["Pid"] == 0)
         and not row["State"]["OOMKilled"] and not row["State"]["Dead"]
         and row["HostConfig"].get("PortBindings") == {}, "SELECTED_RUNTIME_STATE_OR_EXCLUSION")
    differences = changes(previous["NetworkSettings"], row["NetworkSettings"])
    for item in differences:
        path = item["segments"]
        allowed = running and path in {("SandboxID",), ("SandboxKey",)}
        allowed |= (running and len(path) == 3 and path[:1] == ("Networks",)
                    and path[1] in previous["NetworkSettings"].get("Networks", {})
                    and path[2] in {"EndpointID", "Gateway", "IPAddress", "MacAddress", "IPPrefixLen",
                                    "IPv6Gateway", "GlobalIPv6Address", "GlobalIPv6PrefixLen"}
                    and item["before_present"] and item["after_present"])
        if path == ("Ports", "11434/tcp"):
            allowed = (running and role == "ollama"
                       and runtime.get("allowed_start_null_ports", {}).get(role) == ["11434/tcp"]
                       and previous["Config"].get("ExposedPorts") == {"11434/tcp": {}}
                       and row["Config"].get("ExposedPorts") == {"11434/tcp": {}}
                       and previous["HostConfig"].get("PortBindings") == {}
                       and row["HostConfig"].get("PortBindings") == {}
                       and not item["before_present"] and item["after_present"] and item["after"] is None)
        need(allowed, "UNEXPECTED_GPU_NETWORK_TRANSITION")
    return {"state_changes": changes(previous["State"], row["State"]), "network_changes": differences}


def inventory(entry, runtime, running):
    need(docker("info", "--format", "{{.ID}}").decode().strip() == runtime["engine_id"] == entry["engine_id"],
         "ENGINE_CHANGED")
    ids = docker("ps", "-aq", "--no-trunc").decode().split()
    old = {row["Id"]: row for row in entry["containers"]}
    need(set(ids) == set(old) and len(ids) == 31, "PROTECTED_CONTAINER_SET_CHANGED")
    actual = json.loads(docker("inspect", *ids))
    selected = {runtime["gateway_id"], runtime["ollama_id"]}
    transitions = {}
    for row in actual:
        transition = validate_container(old[row["Id"]], row, runtime, running)
        if transition is not None:
            transitions[row["Id"]] = transition
    names = docker("volume", "ls", "-q").decode().split()
    need(set(names) == {row["Name"] for row in entry["volumes"]}, "VOLUME_SET_CHANGED")
    volumes = json.loads(docker("volume", "inspect", *names))
    need({row["Name"]: row for row in volumes} == {row["Name"]: row for row in entry["volumes"]}, "VOLUME_METADATA_CHANGED")
    network_ids = docker("network", "ls", "-q", "--no-trunc").decode().split()
    old_networks = {row["Id"]: row for row in entry["networks"]}
    need(set(network_ids) == set(old_networks), "NETWORK_SET_CHANGED")
    networks = json.loads(docker("network", "inspect", *network_ids))
    for row in networks:
        previous = old_networks[row["Id"]]
        expected = copy.deepcopy(previous)
        if running:
            endpoints = row.get("Containers", {})
            need(set(endpoints).issubset(selected), "FOREIGN_NETWORK_ENDPOINT")
            expected["Containers"] = endpoints
            for subnet, counters in expected.get("Status", {}).get("IPAM", {}).get("Subnets", {}).items():
                added = sum(bool(item.get("IPv4Address")) and ipaddress.ip_interface(item["IPv4Address"]).ip
                            in ipaddress.ip_network(subnet) for item in endpoints.values())
                counters["IPsInUse"] += added
                counters["DynamicIPsAvailable"] -= added
        need(row == expected, "NETWORK_METADATA_OR_ENDPOINT_TRANSITION_CHANGED")
    return {"containers": actual, "volumes": volumes, "networks": networks, "authorized_gpu2_transitions": transitions}


def source_proof(runtime, out):
    out.mkdir()
    files = runtime["startup_files_sha256"]
    need(len(files) == 14, "EXACT_FOURTEEN_RUNTIME_FILES")
    copied = []
    for name, expected in files.items():
        need(Path(name).name == name and "/" not in name and "\\" not in name, "UNSAFE_RUNTIME_FILENAME")
        path = out / name
        docker("cp", runtime["gateway_id"] + ":/app/" + name, path)
        need(sha(path) == expected, "ACTUAL_STOPPED_RUNTIME_SOURCE_CHANGED")
        copied.append(ref(path))
    path = out / "qwen.manifest.json"
    docker("cp", runtime["ollama_id"] + ":" + runtime["qwen_manifest_path"], path)
    need(sha(path) == runtime["qwen_manifest_sha256"], "QWEN_MANIFEST_CHANGED")
    return {"actual_runtime_files": copied, "qwen_manifest": ref(path),
            "gateway_image_id": runtime["gateway_image"], "ollama_image_id": runtime["ollama_image"],
            "actual_gateway_source_sha": runtime["gateway_source_sha"],
            "no_whole_model_directory_hash_claim": True, "model_calls": 0}


PROBE = r'''
import http.client,json,os,subprocess
from pathlib import Path
def get(host,port,path):
 c=http.client.HTTPConnection(host,port,timeout=10)
 try:
  c.request('GET',path);r=c.getresponse();raw=r.read(1048577)
  assert r.status==200 and len(raw)<=1048576
  return json.loads(raw)
 finally:c.close()
workers=[]
for path in Path('/proc').glob('[0-9]*/cmdline'):
 try:raw=path.read_bytes()
 except OSError:continue
 if int(path.parent.name)==os.getpid():continue
 if any(key in raw for key in (b'/app/paddle_runner.py',b'/app/runtime_probe.py',b'contract_diagnostic_worker')):
  workers.append(int(path.parent.name))
n=subprocess.run(['nvidia-smi','--query-compute-apps=pid,process_name,used_memory','--format=csv,noheader,nounits'],capture_output=True,timeout=5,check=False)
assert n.returncode==0
print(json.dumps({'health':get('127.0.0.1',8088,'/health'),'ollama_ps':get('idp-ollama',11434,'/api/ps'),
 'marker_present':Path('/tmp/trackvance-documentary-evaluation.active.json').exists(),
 'active_worker_or_paddle_probe_pids':workers,'nvidia_compute':n.stdout.decode().strip(),
 'model_calls':0,'unload_calls':0}))
'''


def quiet_proof(runtime):
    raw = docker("exec", runtime["gateway_id"], runtime.get("worker_python", "/opt/venv/bin/python"),
                 "-I", "-B", "-c", PROBE, timeout=30)
    result = json.loads(raw)
    health = result["health"]
    need(health.get("ready") is True and health.get("busy") is False and health.get("gpu_concurrency") == 1
         and health.get("memory_lifecycle", {}).get("status") == "AVAILABLE"
         and health.get("runtime_provenance", {}).get("gateway", {}).get("files_sha256") == runtime["startup_files_sha256"]
         and not result["marker_present"] and not result["active_worker_or_paddle_probe_pids"]
         and result["ollama_ps"].get("models") == [] and not result["nvidia_compute"], "GPU_NOT_PROVEN_QUIET")
    return result


def check_closed(freeze):
    output = Path(freeze["output_dir"])
    summary = json.loads((output / "summary.json").read_bytes())
    records = [json.loads(line) for line in (output / "ledger.ndjson").read_bytes().splitlines()]
    need(records[0]["kind"] == "RUN_STARTED" and records[-1]["kind"] == "RUN_CLOSED", "RUN_NOT_CLOSED")
    admitted = {(row["document_id"], row["arm"]) for row in records if row["kind"] == "ATTEMPT_ADMITTED_NO_REFUND"}
    finished = {(row["document_id"], row["arm"]) for row in records if row["kind"] == "ATTEMPT_FINISHED"}
    need(admitted == finished, "UNCLOSED_ADMISSION")
    expected = {(item["document_id"], arm) for item in freeze["execution_order"] for arm in item["arms"]}
    need(len(summary["rows"]) == 12 and {(row["document_id"], row["arm"]) for row in summary["rows"]} == expected,
         "EXACT_TWELVE_RESULTS")
    for row in summary["rows"]:
        need(ref(row["result"]["path"]) == row["result"], "RESULT_CHANGED")
    return {"summary": ref(output / "summary.json"), "ledger": ref(output / "ledger.ndjson"),
            "closed_attempts": len(admitted), "results": 12}


def manager_args(plan, runtime, mode):
    for path, expected in plan["configuration_files_sha256"].items():
        need(sha(path) == expected, "MANAGER_CONFIGURATION_CHANGED")
    args = plan["start_args" if mode == "start" else "stop_args"]
    need(args[:4] == ["docker", "--context", "default", "compose"], "EXACT_EXISTING_MANAGER_REQUIRED")
    expected_tail = (["start", runtime.get("ollama_service", "idp-ollama"), runtime.get("gateway_service", "idp-gateway")]
        if mode == "start" else ["stop", "--timeout", "10", runtime.get("gateway_service", "idp-gateway"),
                                 runtime.get("ollama_service", "idp-ollama")])
    need(args[-len(expected_tail):] == expected_tail, "EXACT_TWO_EXISTING_SERVICES_ONLY")
    return args


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--verify-stopped", action="store_true")
    mode.add_argument("--start", action="store_true")
    mode.add_argument("--stop", action="store_true")
    parser.add_argument("--freeze", required=True, type=Path)
    parser.add_argument("--freeze-sha256", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--root-authorized", action="store_true")
    args = parser.parse_args()
    need(sha(args.freeze) == args.freeze_sha256, "EXACT_FROZEN_MANIFEST")
    freeze = json.loads(args.freeze.read_bytes())
    runtime, entry, plan = freeze["runtime"], read(freeze["entry"]), read(freeze["manager_plan"])
    args.out.mkdir()
    selected_mode = "start" if args.start else "stop" if args.stop else "verify-stopped"
    result = {"mode": selected_mode, "freeze": ref(args.freeze), "model_calls": 0, "DB_accesses": 0,
              "snapshots": 0, "manager_mutations": 0, "whole_model_or_volume_content_hash_claim": False}
    if args.verify_stopped:
        result["inventory"] = save(args.out / "inventory.private.json", inventory(entry, runtime, False))
        result["source_proof"] = source_proof(runtime, args.out / "runtime-source")
        result["status"] = "ACTUAL_STOPPED_IDENTITIES_AND_SOURCE_VERIFIED_READ_ONLY"
    else:
        need(args.root_authorized and freeze.get("authorized") is True
             and freeze.get("model_admission") == "ROOT_FROZEN", "EXPLICIT_ROOT_RELEASE_REQUIRED")
        if args.start:
            need(0 <= time.monotonic() - freeze["preparation_started_monotonic"] < 3600
                 and abs((time.time() - freeze["preparation_started_epoch"]) -
                         (time.monotonic() - freeze["preparation_started_monotonic"])) < 30, "PREPARATION_CLOCK_INVALID")
            read(freeze["stopped_source_receipt"])
            result["before"] = save(args.out / "before.private.json", inventory(entry, runtime, False))
            result["manager_args"] = manager_args(plan, runtime, "start")
            result["manager_output_utf8"] = command(result["manager_args"], timeout=60).decode("utf8", "replace")
            result["manager_mutations"] = 1
            result["after"] = save(args.out / "after.private.json", inventory(entry, runtime, True))
            # No polling/restarts here. ROOT may perform read-only health checks
            # during ordinary asynchronous startup; model admission is separate.
            result["status"] = "EXACT_EXISTING_GPU2_START_REQUEST_COMPLETED_NOT_MODEL_ADMISSION"
        else:
            result["closed_runner"] = check_closed(freeze)
            result["before"] = save(args.out / "before.private.json", inventory(entry, runtime, True))
            result["quiet_proof_before_stop"] = quiet_proof(runtime)
            result["manager_args"] = manager_args(plan, runtime, "stop")
            result["manager_output_utf8"] = command(result["manager_args"], timeout=60).decode("utf8", "replace")
            result["manager_mutations"] = 1
            result["after"] = save(args.out / "after.private.json", inventory(entry, runtime, False))
            result["source_proof_after_stop"] = source_proof(runtime, args.out / "runtime-source-after")
            result["poststop_ollama_ps"] = "NOT_QUERYABLE_SERVICE_STOPPED_NOT_FABRICATED_EMPTY"
            result["status"] = "DIAGNOSTIC_GPU2_STOPPED_PROTECTED_IDENTITIES_AND_METADATA_PRESERVED"
    print(json.dumps(save(args.out / "receipt.private.json", result)))


if __name__ == "__main__":
    main()
