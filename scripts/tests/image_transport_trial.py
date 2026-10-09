"""Genuine, bounded classic-to-containerd transfer of two tiny synthetic images.

This proves transport only, never product certification. No inner containers are
created. The privileged DinD daemon has its own network/volume and loopback port.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci import image_bundle
from ci.host_resources import limit_cpu_affinity
from ci.local_resources import canonical_hash, image_consumers, image_identity
from ci.owned_cleanup import PROJECT
from image_transport_fixtures import image_archive

GIB = 1024**3
OWNER = "io.trackvance.image-transport-trial"
HOST = "npipe:////./pipe/dockerDesktopLinuxEngine"
HELPER = "docker:28-dind"
LABEL = "com.docker.compose.project"
TTL = 600


def write(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def endpoint(value):
    if value != HOST and not re.fullmatch(r"tcp://127\.0\.0\.1:[1-9][0-9]{0,4}", value):
        raise ValueError("Trial endpoint must be the explicit host pipe or a loopback port")
    if value != HOST and int(value.rsplit(":", 1)[1]) > 65535:
        raise ValueError("Invalid loopback port")
    return value


def environment(config, host):
    result = os.environ.copy()
    for key in ("DOCKER_CONTEXT", "DOCKER_AUTH_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        result.pop(key, None)
    result.update(DOCKER_HOST=endpoint(host), DOCKER_CONFIG=str(config))
    return result


def anonymous_config(value):
    return (isinstance(value, dict) and value.get("auths") == {"https://index.docker.io/v1/": {}}
            and set(value) <= {"auths", "cliPluginsExtraDirs"}
            and isinstance(value.get("cliPluginsExtraDirs", []), list)
            and all(isinstance(path, str) for path in value.get("cliPluginsExtraDirs", [])))


def capacity(info, rows, external_memory, external_cpus):
    memory, cpus = 0, 0.0
    for row in rows:
        if row.get("State", {}).get("Status") not in {"running", "paused", "restarting"}:
            continue
        config = row["HostConfig"]
        if not config.get("Memory") or not config.get("NanoCpus"):
            raise ValueError("Active unbounded container cannot promise trial capacity")
        memory += config["Memory"]
        cpus += config["NanoCpus"] / 10**9
    required = max(memory, external_memory) + int(1.5 * GIB) + 2 * GIB
    required_cpus = max(cpus, external_cpus) + 2
    if required > info["MemTotal"] or required_cpus > info["NCPU"]:
        raise ValueError("Aggregate external reservations plus trial and reserve exceed capacity")
    return {"required_memory_bytes": required, "required_cpus": required_cpus,
            "external_memory_bytes": max(memory, external_memory),
            "external_cpus": max(cpus, external_cpus), "reserve_memory_bytes": 2 * GIB}


def container_args(project, helper):
    if not PROJECT.fullmatch(project) or not image_bundle.DIGEST.fullmatch(helper):
        raise ValueError("Source creation needs a recognized UUID project and immutable helper")
    return ["run", "--detach", "--name", project + "-source", "--label", LABEL + "=" + project,
            "--label", OWNER + "=" + project, "--privileged", "--memory", "768m", "--memory-swap", "768m",
            "--cpus", "1", "--pids-limit", "256", "--restart", "no", "--network", project + "-net",
            "--mount", "type=volume,src=" + project + "-data,dst=/var/lib/docker",
            "--publish", "127.0.0.1::2375", "--env", "DOCKER_TLS_CERTDIR=", helper,
            "dockerd", "--host=unix:///var/run/docker.sock", "--host=tcp://0.0.0.0:2375",
            "--storage-driver=vfs", "--feature=containerd-snapshotter=false"]


def source_profile(row, project, helper, *, cleanup=False):
    config = row.get("HostConfig", {})
    labels = row.get("Config", {}).get("Labels") or {}
    mounts = row.get("Mounts", [])
    ports = row.get("NetworkSettings", {}).get("Ports", {}).get("2375/tcp") or []
    bindings = config.get("PortBindings", {}).get("2375/tcp") or []
    if (row.get("Image") != helper or labels.get(LABEL) != project or labels.get(OWNER) != project
            or config.get("Memory") != 768 * 1024**2 or config.get("MemorySwap") != 768 * 1024**2
            or config.get("NanoCpus") != 10**9 or config.get("PidsLimit") != 256
            or config.get("RestartPolicy", {}).get("Name") != "no" or not config.get("Privileged")
            or len(mounts) != 1 or mounts[0].get("Type") != "volume"
            or mounts[0].get("Name") != project + "-data" or mounts[0].get("Destination") != "/var/lib/docker"
            or set(row.get("NetworkSettings", {}).get("Networks", {})) != {project + "-net"}
            or len(bindings) != 1 or bindings[0].get("HostIp") != "127.0.0.1"
            or (not cleanup or row.get("State", {}).get("Running"))
            and (len(ports) != 1 or ports[0].get("HostIp") != "127.0.0.1")):
        raise ValueError("Actual source isolation or resource profile differs from registered intent")
    return endpoint("tcp://127.0.0.1:" + ports[0]["HostPort"]) if ports else None


def removable_image(row, baseline, registered, consumers):
    identity = image_identity(row)
    if identity["image_id"] in baseline:
        raise ValueError("A preexisting image is protected")
    if identity != registered or any(c["image_id"] == identity["image_id"] for c in consumers):
        raise ValueError("Image identity changed or any stopped/running consumer remains")
    return identity["image_id"]


class Trial:
    def __init__(self, directory, config, sha, external_memory, external_cpus):
        if directory.exists() or directory.is_symlink() or not image_bundle.SHA.fullmatch(sha):
            raise ValueError("Trial needs a fresh evidence directory and exact Git SHA")
        if config.is_symlink() or not (config / "config.json").is_file():
            raise ValueError("Explicit existing private Docker configuration required")
        config_data = json.loads((config / "config.json").read_text(encoding="utf-8"))
        if not anonymous_config(config_data):
            raise ValueError("Trial requires the anonymous private configuration without helpers")
        directory.mkdir(parents=True)
        self.directory, self.config, self.sha = directory, config, sha
        self.project = "trackvance-v070-test-image-transport-" + uuid4().hex[:12]
        self.deadline = time.monotonic() + TTL
        self.external_memory, self.external_cpus = external_memory, external_cpus
        self.registry = {"schema_version": 1, "project": self.project, "source_sha": sha,
                         "created_at": datetime.now(UTC).isoformat(), "intents": [], "images": []}
        self.receipt = {"schema_version": 1, "kind": "SYNTHETIC_CROSS_STORE_TRIAL", "certifies_085": False,
                        "source_sha": sha, "project": self.project, "status": "RUNNING",
                        "host_endpoint": HOST, "privileges": "Privileged temporary source daemon only",
                        "budget": {"memory_bytes": int(1.5 * GIB), "cpus": 2, "pids_source": 256,
                                   "ttl_seconds": TTL, "maximum_synthetic_archive_bytes": 1024**2},
                        "docker_config_sha256": image_bundle.file_digest(config / "config.json")}

    def docker(self, *args, host=HOST, cleanup=False):
        timeout = 45 if cleanup else min(90, self.deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("Trial TTL expired")
        result = subprocess.run(["docker", *args], env=environment(self.config, host),
                                capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=False)
        if result.returncode:
            raise ValueError("Trial Docker operation failed: " + args[0])
        if args[0] == "load":
            self.receipt.setdefault("loads", []).append({"endpoint": host,
                "loaded_image_id_lines": re.findall(r"^Loaded image ID: sha256:[a-f0-9]{64}$", result.stdout, re.MULTILINE)})
        return result.stdout

    def rows(self):
        ids = self.docker("ps", "-aq", "--no-trunc", cleanup=True).split()
        return json.loads(self.docker("inspect", *ids, cleanup=True)) if ids else []

    def protected(self):
        return {row["Id"]: {"image": row["Image"], "state": row["State"]["Status"],
                "config": canonical_hash(row["Config"]), "host": canonical_hash(row["HostConfig"]),
                "mounts": canonical_hash(row["Mounts"])} for row in self.rows()
                if not PROJECT.fullmatch((row.get("Config", {}).get("Labels") or {}).get(LABEL, ""))}

    def guard(self):
        if self.protected() != self.registry["protected"]:
            raise ValueError("Protected containers changed; refusing another trial mutation")
        rows = [r for r in self.rows() if r["Id"] != self.registry.get("source_container")]
        info = json.loads(self.docker("info", "--format", "{{json .}}"))
        self.receipt["capacity"] = capacity(info, rows, self.external_memory, self.external_cpus)

    def intent(self, action, identity):
        self.guard()
        if (action == "create-volume" and identity in self.registry["baseline_volumes"]
                or action == "create-network" and identity in self.registry["baseline_network_names"]
                or action == "create-source" and any(r.get("Name") == "/" + identity for r in self.rows())):
            raise ValueError("Creation intent collides with preexisting protected identity")
        self.registry["intents"].append({"action": action, "identity": identity,
                                         "at": datetime.now(UTC).isoformat()})
        write(self.directory / "registry.json", self.registry)

    def save(self, identifier, path):
        remaining = min(60, self.deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("Trial TTL expired")
        process = subprocess.Popen(["docker", "save", identifier], env=self.transfer_environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        timer = threading.Timer(remaining, process.kill)
        timer.start()
        try:
            with path.open("xb") as target:
                size = 0
                while chunk := process.stdout.read(65536):
                    size += len(chunk)
                    if size > 1024**2:
                        raise ValueError("Synthetic export exceeds its 1 MiB budget")
                    target.write(chunk)
            if process.wait(timeout=5):
                raise ValueError("Synthetic export failed or exceeded its deadline")
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()

    @contextlib.contextmanager
    def transfer(self, host):
        previous_docker, previous_save = image_bundle.docker, image_bundle.save_image
        self.transfer_environment = environment(self.config, host)
        image_bundle.docker = lambda *args: self.docker(*args, host=host)
        image_bundle.save_image = self.save
        try:
            yield
        finally:
            image_bundle.docker, image_bundle.save_image = previous_docker, previous_save

    def register_images(self):
        ids = set(self.docker("image", "ls", "-aq", "--no-trunc", cleanup=True).split())
        for identifier in sorted(ids - set(self.registry["baseline_images"])):
            row = json.loads(self.docker("image", "inspect", identifier, cleanup=True))[0]
            labels = row.get("Config", {}).get("Labels") or {}
            if labels.get(OWNER) == self.project and labels.get("org.opencontainers.image.revision") == self.sha:
                record = image_identity(row)
                if not any(r["image_id"] == identifier for r in self.registry["images"]):
                    from ci.image_content import archive_identity
                    with tempfile.TemporaryDirectory(prefix="identity-", dir=self.directory) as scratch, self.transfer(HOST):
                        archive = Path(scratch) / "image.tar"
                        self.save(identifier, archive)
                        proof = archive_identity(archive, identifier, self.sha)
                    if proof["configuration_digest"] not in self.registry.get("expected_configurations", {}).values():
                        raise ValueError("Unregistered configuration cannot be adopted for image cleanup")
                    self.registry["images"].append(record)
        write(self.directory / "registry.json", self.registry)

    def run(self):
        self.registry.update(protected=self.protected(), baseline_images=sorted(set(
            self.docker("image", "ls", "-aq", "--no-trunc").split())),
            baseline_containers=sorted(r["Id"] for r in self.rows()),
            baseline_volumes=self.docker("volume", "ls", "-q").split(),
            baseline_networks=self.docker("network", "ls", "-q", "--no-trunc").split(),
            baseline_network_names=self.docker("network", "ls", "--format", "{{.Name}}").split())
        write(self.directory / "registry.json", self.registry)
        write(self.directory / "trial.json", self.receipt)
        self.receipt["host_before"] = {key: value for key, value in json.loads(
            self.docker("info", "--format", "{{json .}}")).items()
            if key in {"ID", "ServerVersion", "Driver", "DriverStatus", "MemTotal", "NCPU"}}
        self.receipt["affinity"] = limit_cpu_affinity(1)
        try:
            self.guard()
            present = self.docker("image", "ls", "-q", "--no-trunc", HELPER).split()
            if not present:
                self.intent("pull-helper", HELPER)
                self.docker("pull", HELPER)
            helper_row = json.loads(self.docker("image", "inspect", HELPER))[0]
            helper = helper_row["Id"]
            if (helper_row.get("Os") != "linux" or helper_row.get("Architecture") != "amd64"
                    or set(helper_row.get("Config", {}).get("Volumes") or {}) != {"/var/lib/docker"}):
                raise ValueError("Helper platform or anonymous storage differs from intended DinD")
            self.registry["helper"] = image_identity(helper_row)
            write(self.directory / "registry.json", self.registry)
            for kind, suffix in (("volume", "-data"), ("network", "-net")):
                name = self.project + suffix
                self.intent("create-" + kind, name)
                args = [kind, "create", "--label", LABEL + "=" + self.project,
                        "--label", OWNER + "=" + self.project]
                if kind == "network":
                    args.append("--internal")
                identifier = self.docker(*args, name).strip()
                self.registry[kind] = identifier
                write(self.directory / "registry.json", self.registry)
            self.intent("create-source", self.project + "-source")
            identifier = self.docker(*container_args(self.project, helper)).strip()
            self.registry["source_container"] = identifier
            write(self.directory / "registry.json", self.registry)
            row = json.loads(self.docker("inspect", identifier))[0]
            source = source_profile(row, self.project, helper)
            self.receipt["source_endpoint"] = source
            while True:
                try:
                    info = json.loads(self.docker("info", "--format", "{{json .}}", host=source))
                    break
                except ValueError:
                    if self.deadline - time.monotonic() < 60:
                        raise
                    time.sleep(0.5)
            if (not info["ServerVersion"].startswith("28.") or info["Driver"] != "vfs"
                    or self.docker("ps", "-aq", host=source).strip()):
                raise ValueError("Source is not an empty real Docker 28 classic image store")
            self.receipt["source_engine"] = {key: info.get(key) for key in ("ID", "ServerVersion", "Driver", "DriverStatus")}
            fixture = self.directory / "fixtures"
            fixture.mkdir()
            references, expected = {}, {}
            for role in ("backend", "web"):
                path = fixture / (role + ".tar")
                labels = {"org.opencontainers.image.revision": self.sha,
                          "org.opencontainers.image.version": "0.8.5", OWNER: self.project}
                content = image_archive(path, self.sha, role, config_change={
                    "config": {"Labels": labels, "Env": ["FIXTURE_ROLE=" + role]}})
                expected[role] = content["configuration_digest"]
                self.registry["expected_configurations"] = expected
                self.intent("load-source-image", content["configuration_digest"])
                self.docker("load", "--input", str(path), host=source)
                row = json.loads(self.docker("image", "inspect", content["tag"], host=source))[0]
                if row["Id"] != content["configuration_digest"]:
                    raise ValueError("Real source Engine ID is not the exact classic configuration digest")
                references[role] = row["Id"]
            bundle = self.directory / "bundle"
            with self.transfer(source):
                image_bundle.export_images(bundle, self.sha, references["backend"], references["web"])
            self.receipt["original_manifest_sha256"] = image_bundle.file_digest(bundle / "images.json")
            # Actual Docker save by immutable ID must exercise the untagged path.
            from ci.image_content import archive_identity
            for role in ("backend", "web"):
                if archive_identity(bundle / (role + ".tar"), expected[role], self.sha)["references"]:
                    raise ValueError("Real immutable-ID export did not exercise the untagged load path")
            self.intent("load-host-images", expected)
            try:
                with self.transfer(HOST):
                    proof = image_bundle.load_images(bundle / "images.json", self.sha, self.directory / "transfer.json")
                    mapping = image_bundle.validated_host_mapping(bundle / "images.json", self.sha, bundle / "host-images.json")
            finally:
                self.register_images()
            if any(mapping[role] == expected[role] for role in expected):
                raise ValueError("Real host did not exercise distinct target versus configuration image IDs")
            if self.docker("ps", "-aq", host=source).strip():
                raise ValueError("Source daemon unexpectedly has an inner container")
            self.receipt.update(status="PASS", measurements=proof["measurements"],
                                mapping_sha256=proof["host_mapping_sha256"], source_ids=expected, host_ids=mapping,
                                original_manifest_unchanged=proof["manifest_sha256"] == self.receipt["original_manifest_sha256"],
                                inner_containers=0)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
            self.receipt.update(status="FAIL", error_type=type(error).__name__, error=str(error)[:300])
        finally:
            self.receipt["cleanup"] = self.cleanup()
            try:
                self.receipt["protected_unchanged"] = self.protected() == self.registry["protected"]
                self.receipt["host_after"] = {key: value for key, value in json.loads(
                    self.docker("info", "--format", "{{json .}}", cleanup=True)).items()
                    if key in {"ID", "ServerVersion", "Driver", "DriverStatus", "MemTotal", "NCPU"}}
                self.receipt["baseline_storage_and_images_present"] = (
                    set(self.registry["baseline_volumes"]) <= set(self.docker("volume", "ls", "-q", cleanup=True).split())
                    and set(self.registry["baseline_networks"]) <= set(self.docker("network", "ls", "-q", "--no-trunc", cleanup=True).split())
                    and set(self.registry["baseline_images"]) <= set(self.docker("image", "ls", "-aq", "--no-trunc", cleanup=True).split()))
                self.receipt["docker_config_unchanged"] = self.receipt["docker_config_sha256"] == image_bundle.file_digest(self.config / "config.json")
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                self.receipt.update(protected_unchanged=False, protection_error_type=type(error).__name__)
            if (self.receipt["cleanup"]["status"] != "PASS" or not self.receipt["protected_unchanged"]
                    or not self.receipt.get("baseline_storage_and_images_present") or not self.receipt.get("docker_config_unchanged")
                    or self.receipt.get("host_after", {}).get("ID") != self.receipt["host_before"].get("ID")):
                self.receipt["status"] = "FAIL"
            self.receipt["finished_at"] = datetime.now(UTC).isoformat()
            write(self.directory / "trial.json", self.receipt)
        return self.receipt

    def cleanup(self):
        removed, failures = [], []
        try:
            self.register_images()
            for row in self.rows():
                if row["Id"] in self.registry["baseline_containers"]:
                    continue
                labels = row.get("Config", {}).get("Labels") or {}
                if labels.get(OWNER) != self.project:
                    continue
                source_profile(row, self.project, self.registry["helper"]["image_id"], cleanup=True)
                check = json.loads(self.docker("inspect", row["Id"], cleanup=True))[0]
                source_profile(check, self.project, self.registry["helper"]["image_id"], cleanup=True)
                self.docker("stop", "--time", "15", row["Id"], cleanup=True)
                self.docker("rm", row["Id"], cleanup=True)
                removed.append(row["Id"])
            for kind, suffix, key in (("volume", "-data", "Name"), ("network", "-net", "Id")):
                intent = "create-" + kind
                if not any(i["action"] == intent for i in self.registry["intents"]):
                    continue
                name = self.project + suffix
                rows = json.loads(self.docker(kind, "inspect", name, cleanup=True))
                row = rows[0]
                if ((row.get("Labels") or {}).get(OWNER) != self.project
                        or (row.get("Labels") or {}).get(LABEL) != self.project):
                    raise ValueError("Cleanup resource owner differs from durable creation intent")
                if (kind == "volume" and self.docker("ps", "-aq", "--filter", "volume=" + name, cleanup=True).strip()
                        or kind == "network" and row.get("Containers")):
                    raise ValueError("Cleanup resource still has consumers")
                check = json.loads(self.docker(kind, "inspect", row[key], cleanup=True))[0]
                if check != row:
                    raise ValueError("Cleanup resource changed during consumer revalidation")
                self.docker(kind, "rm", row[key], cleanup=True)
                removed.append(row[key])
            candidates = [*self.registry["images"]]
            if self.registry.get("helper"):
                candidates.append(self.registry["helper"])
            for record in candidates:
                if record["image_id"] in self.registry["baseline_images"]:
                    continue
                row = json.loads(self.docker("image", "inspect", record["image_id"], cleanup=True))[0]
                consumers = image_consumers(lambda *args: self.docker(*args, cleanup=True))
                identifier = removable_image(row, set(self.registry["baseline_images"]), record, consumers)
                check = json.loads(self.docker("image", "inspect", identifier, cleanup=True))[0]
                removable_image(check, set(self.registry["baseline_images"]), record,
                                image_consumers(lambda *args: self.docker(*args, cleanup=True)))
                self.docker("image", "rm", identifier, cleanup=True)
                removed.append(identifier)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
            failures.append({"error_type": type(error).__name__, "error": str(error)[:300]})
        return {"status": "FAIL" if failures else "PASS", "removed": removed, "failures": failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--docker-config", required=True, type=Path)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--external-memory-gib", required=True, type=float)
    parser.add_argument("--external-cpus", required=True, type=float)
    args = parser.parse_args()
    if not 0 <= args.external_memory_gib <= 24 or not 0 <= args.external_cpus <= 16:
        parser.error("External reservations must be finite and bounded")
    git = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    if git != args.sha:
        parser.error("Trial source must be the current committed tool SHA")
    result = Trial(args.directory, args.docker_config, args.sha,
                   int(args.external_memory_gib * GIB), args.external_cpus).run()
    print(json.dumps({key: result[key] for key in ("status", "certifies_085", "project")}))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
