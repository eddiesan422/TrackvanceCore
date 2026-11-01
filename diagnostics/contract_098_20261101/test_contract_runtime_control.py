"""CPU-only regression for metadata guards; historical snapshots remain read-only."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest


def load_module(path):
    spec = importlib.util.spec_from_file_location("contract_controller_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def suite(control, entry, hold, runtime):
    baseline = {row["Id"]: row for row in entry["containers"]}
    selected = {runtime["gateway_id"], runtime["ollama_id"]}
    runtime = copy.deepcopy(runtime)
    runtime["allowed_start_null_ports"] = {"ollama": ["11434/tcp"]}
    # The preserved HOLD contains observed state and losslessly usable values of
    # each known network leaf. This is a labelled reconstruction, not a claimed
    # full running inspect snapshot.
    running = {}
    for observed in hold["GPU2_before_stop"]:
        row = copy.deepcopy(baseline[observed["id"]])
        row["State"] = observed["state"]
        for difference in observed["network_diff"]:
            path = difference["path"]
            if path.startswith("/Ports/"):
                row["NetworkSettings"]["Ports"][path[len("/Ports/"):]] = difference["after"]
            elif path.startswith("/Networks/"):
                matches = [(name, path[len("/Networks/" + name + "/"):])
                    for name in row["NetworkSettings"]["Networks"] if path.startswith("/Networks/" + name + "/")]
                assert len(matches) == 1
                name, key = matches[0]
                assert "/" not in key
                row["NetworkSettings"]["Networks"][name][key] = difference["after"]
            else:
                key = path[1:]
                assert key in {"SandboxID", "SandboxKey"}
                row["NetworkSettings"][key] = difference["after"]
        running[row["Id"]] = row
    ollama = running[runtime["ollama_id"]]
    prior = baseline[runtime["ollama_id"]]

    class GuardRegression(unittest.TestCase):
        def blocked(self, value, previous=prior, active=True):
            with self.assertRaises(RuntimeError):
                control.validate_container(previous, value, runtime, active)

        def test_01_exact_observed_literal_port_transition(self):
            self.assertNotIn("11434/tcp", prior["NetworkSettings"]["Ports"])
            self.assertIsNone(ollama["NetworkSettings"]["Ports"]["11434/tcp"])
            self.assertEqual(ollama["HostConfig"]["PortBindings"], {})
            for identity, row in running.items():
                evidence = control.validate_container(baseline[identity], row, runtime, True)
                if identity == runtime["ollama_id"]:
                    difference = next(x for x in evidence["network_changes"] if x["segments"] == ("Ports", "11434/tcp"))
                    self.assertFalse(difference["before_present"])
                    self.assertTrue(difference["after_present"])
                    self.assertIsNone(difference["after"])
            # Reproduce the old textual-path defect without executing its helper.
            old_last = "/Ports/11434/tcp".split("/")[-1]
            self.assertEqual(old_last, "tcp")
            self.assertNotIn(old_last, {"Ports", "SandboxID", "SandboxKey", "IPAddress"})

        def test_02_real_host_port_or_ip_publication_denied(self):
            for binding in ({"HostIp": "127.0.0.1", "HostPort": "11434"},
                            {"HostIp": "0.0.0.0", "HostPort": "44444"}):
                value = copy.deepcopy(ollama)
                value["HostConfig"]["PortBindings"] = {"11434/tcp": [binding]}
                self.blocked(value)
                value = copy.deepcopy(ollama)
                value["NetworkSettings"]["Ports"]["11434/tcp"] = [binding]
                self.blocked(value)

        def test_03_unauthorized_network_and_all_mount_attributes_denied(self):
            value = copy.deepcopy(ollama)
            name = next(iter(value["NetworkSettings"]["Networks"]))
            value["NetworkSettings"]["Networks"][name]["NetworkID"] = "UNAUTHORIZED_NETWORK"
            self.blocked(value)
            value = copy.deepcopy(ollama)
            value["NetworkSettings"]["Networks"]["foreign/network"] = {}
            self.blocked(value)
            for key in prior["Mounts"][0]:
                value = copy.deepcopy(ollama)
                value["Mounts"][0][key] = "UNAUTHORIZED_ATTRIBUTE"
                self.blocked(value)
            value = copy.deepcopy(ollama)
            value["Mounts"] = list(reversed(value["Mounts"]))
            control.validate_container(prior, value, runtime, True)

        def test_04_other_slash_keys_remain_indivisible(self):
            items = control.changes({"a/b": {"c/d": 1}}, {"a/b": {"c/d": 2}})
            self.assertEqual(items[0]["segments"], ("a/b", "c/d"))
            value = copy.deepcopy(ollama)
            value["NetworkSettings"]["other/key"] = None
            self.blocked(value)
            value = copy.deepcopy(ollama)
            value["NetworkSettings"]["Ports"]["9999/tcp"] = None
            self.blocked(value)

        def test_05_absence_null_empty_and_nonempty_are_distinct(self):
            for after in (None, {}, [], "", [{"HostPort": "1"}]):
                differences = control.changes({}, {"11434/tcp": after})
                self.assertFalse(differences[0]["before_present"])
                self.assertTrue(differences[0]["after_present"])
                self.assertEqual(differences[0]["segments"], ("11434/tcp",))
            self.assertTrue(control.changes({"x": None}, {"x": {}}))
            self.assertTrue(control.changes({"x": {}}, {}))
            for after in ({}, [], "", [{"HostPort": "1"}]):
                value = copy.deepcopy(ollama)
                value["NetworkSettings"]["Ports"]["11434/tcp"] = after
                self.blocked(value)
            earlier = copy.deepcopy(prior)
            earlier["NetworkSettings"]["Ports"]["11434/tcp"] = None
            value = copy.deepcopy(ollama)
            del value["NetworkSettings"]["Ports"]["11434/tcp"]
            self.blocked(value, previous=earlier)
            gateway = copy.deepcopy(running[runtime["gateway_id"]])
            gateway["NetworkSettings"]["Ports"]["11434/tcp"] = None
            self.blocked(gateway, previous=baseline[runtime["gateway_id"]])

        def test_06_other_identity_state_config_and_global_guards_preserved(self):
            for section, key, new_value in (("State", "OOMKilled", True), ("State", "Dead", True),
                                            ("State", "Pid", 0), ("Config", "ExposedPorts", {})):
                value = copy.deepcopy(ollama)
                value[section][key] = new_value
                self.blocked(value)
            for key in ("Image", "Id"):
                value = copy.deepcopy(ollama)
                value[key] = "UNAUTHORIZED"
                self.blocked(value)
            other = next(row for row in entry["containers"] if row["Id"] not in selected)
            value = copy.deepcopy(other)
            value["State"]["Running"] = True
            self.blocked(value, previous=other, active=False)
            self.blocked(ollama, active=False)
            original = control.docker
            def simulated(*args, **_kwargs):
                if args[:1] == ("info",):
                    return runtime["engine_id"].encode()
                if args[:1] == ("ps",):
                    return "\n".join(baseline).encode()
                if args[:1] == ("inspect",):
                    return json.dumps(entry["containers"]).encode()
                if args[:2] == ("volume", "ls"):
                    return "\n".join(row["Name"] for row in entry["volumes"]).encode()
                if args[:2] == ("volume", "inspect"):
                    return json.dumps(entry["volumes"]).encode()
                if args[:2] == ("network", "ls"):
                    return "\n".join(row["Id"] for row in entry["networks"]).encode()
                if args[:2] == ("network", "inspect"):
                    return json.dumps(entry["networks"]).encode()
                raise AssertionError(args)
            control.docker = simulated
            try:
                control.inventory(entry, runtime, False)
                def bad_network(*args, **kwargs):
                    if args[:2] == ("network", "inspect"):
                        values = copy.deepcopy(entry["networks"])
                        values[0]["Labels"]["foreign/key"] = "unauthorized"
                        return json.dumps(values).encode()
                    return simulated(*args, **kwargs)
                control.docker = bad_network
                with self.assertRaises(RuntimeError):
                    control.inventory(entry, runtime, False)
            finally:
                control.docker = original
    return unittest.defaultTestLoader.loadTestsFromTestCase(GuardRegression)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--controller", required=True, type=Path)
    parser.add_argument("--entry", required=True, type=Path)
    parser.add_argument("--hold", required=True, type=Path)
    parser.add_argument("--runtime-freeze", required=True, type=Path)
    args = parser.parse_args()
    control = load_module(args.controller)
    result = unittest.TextTestRunner(verbosity=2).run(suite(control, json.loads(args.entry.read_bytes()),
        json.loads(args.hold.read_bytes()), json.loads(args.runtime_freeze.read_bytes())["runtime"]))
    print(json.dumps({"status": "PASS" if result.wasSuccessful() else "FAIL", "CPU_cases": result.testsRun,
        "models": 0, "Docker_calls": 0, "controller_sha256": hashlib.sha256(args.controller.read_bytes()).hexdigest(),
        "historical_running_snapshot_scope": "RECONSTRUCTED_FROM_BASELINE_PLUS_PRESERVED_OBSERVED_HOLD_LEAF_VALUES",
        "source_snapshots_unmodified": True}))
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
