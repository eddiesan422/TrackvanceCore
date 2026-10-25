"""Synthetic stdlib fixtures only. No models, PDFs, GPU or external services."""
import atexit
import base64
import hashlib
import http.client
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[2]
APP_FIXTURE = tempfile.TemporaryDirectory(prefix="idp-observability-source-fixture-")
ROOT = Path(APP_FIXTURE.name)
atexit.register(APP_FIXTURE.cleanup)
for source in (REPOSITORY / "deploy/idp").iterdir():
    if source.is_file():
        (ROOT / source.name).write_bytes(source.read_bytes())
for name in ("document_semantic_fragments.py", "document_template_builder_rules.py", "document_literal_tables.py"):
    (ROOT / name).write_bytes((REPOSITORY / "backend/src/trackvance" / name).read_bytes())

SPEC = importlib.util.spec_from_file_location("candidate_observability", ROOT / "idp_observability.py")
obs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(obs)
HASHES = {"paddle_runner.py": "a" * 64, "pipeline.yml": "b" * 64}
SOURCE = hashlib.sha256(b"synthetic source only").hexdigest()
FINISHED = SimpleNamespace(poll=lambda: 0)


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "Linux runtime filesystem contract")
class SecurityFixtures(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="idp-observation-fixtures-")
        self.base = Path(self.scratch.name)
        self.rings, self.children = [], []

    def tearDown(self):
        for child in self.children:
            child.close()
        for ring in self.rings:
            ring.close(clear=True)
        self.scratch.cleanup()

    def job(self, stderr=b"private synthetic SECRET_DOCUMENT_VALUE\n"):
        folder = Path(tempfile.mkdtemp(prefix="idp-", dir=self.base))
        (folder / "error.log").write_bytes(stderr)
        (folder / "request.json").write_text(json.dumps({"filename": "document.pdf", "source_sha256": SOURCE, "regions": []}))
        return folder

    def ring(self, **kwargs):
        ring = obs.FailureRing(HASHES, base=self.base, **kwargs)
        self.rings.append(ring)
        return ring

    def child(self, folder, enabled=False):
        child = obs.make_child_diagnostics(folder, HASHES, enabled)
        self.children.append(child)
        return child

    def retain(self, ring, folder, **kwargs):
        return ring.retain(folder, kwargs.get("job_identity", obs.identity(folder.lstat())),
            kwargs.get("request_id", "c" * 32), kwargs.get("source", SOURCE),
            kwargs.get("code", "INFERENCE_TIMEOUT"), kwargs.get("process", FINISHED))

    def test_metadata_is_source_bound_fixed_and_bounded(self):
        folder = self.job()
        child = self.child(folder)
        self.assertTrue(child.bind_source(SOURCE))
        for _ in range(150):
            self.assertTrue(child.mark("PREDICT_NEXT", 1))
        raw = (folder / "phase.json").read_bytes()
        value = obs.validate_phase(raw, HASHES, SOURCE)
        self.assertEqual(len(value["events"]), 128)
        self.assertTrue(value["events_truncated"])
        self.assertLessEqual(len(raw), obs.PHASE_LIMIT)
        self.assertNotIn(b"SECRET_DOCUMENT_VALUE", raw)
        self.assertEqual(stat.S_IMODE((folder / "phase.json").stat().st_mode), 0o600)

    def test_phase_schema_rejects_untrusted_values_and_provenance(self):
        folder = self.job()
        self.child(folder).mark("IMPORT_PADDLE")
        good = json.loads((folder / "phase.json").read_bytes())
        variants = []
        for key, value in (("version", True), ("source_sha256", "d" * 64), ("files_sha256", {})):
            variants.append({**good, key: value})
        for event in ({"stage": "SECRET_DOCUMENT_VALUE", "elapsed_seconds": 0},
            {"stage": [], "elapsed_seconds": 0}, {"stage": "PREDICT_NEXT", "elapsed_seconds": -0.1},
            {"stage": "PREDICT_NEXT", "elapsed_seconds": float("nan")},
            {"stage": "PREDICT_NEXT", "elapsed_seconds": True},
            {"stage": "PREDICT_NEXT", "elapsed_seconds": 0, "page_ordinal": True},
            {"stage": "PREDICT_NEXT", "elapsed_seconds": 0, "text": "SECRET_DOCUMENT_VALUE"}):
            variants.append({**good, "events": [event]})
        for variant in variants:
            with self.subTest(variant_keys=sorted(variant)), self.assertRaises(ValueError):
                obs.validate_phase(json.dumps(variant).encode(), HASHES, SOURCE)

    def test_timer_is_opt_in_one_shot_and_cancelled_on_close(self):
        folder = self.job()
        with patch.object(obs.faulthandler, "enable") as enable, patch.object(obs.faulthandler, "dump_traceback_later") as dump, patch.object(obs.faulthandler, "cancel_dump_traceback_later") as cancel:
            disabled = self.child(folder)
            self.assertFalse(disabled.arm())
            dump.assert_not_called()
            enabled = self.child(folder, True)
            self.assertTrue(enabled.arm())
            self.assertEqual(dump.call_args.args, (60,))
            self.assertFalse(dump.call_args.kwargs["repeat"])
            self.assertTrue(enabled.close())
            enable.assert_called_once()
            cancel.assert_called_once()

    def test_timer_setup_failure_cancels_partial_setup_without_raising(self):
        with patch.object(obs.faulthandler, "enable"), patch.object(obs.faulthandler, "dump_traceback_later", side_effect=OSError("synthetic failure")), patch.object(obs.faulthandler, "cancel_dump_traceback_later") as cancel:
            child = self.child(self.job(), True)
            self.assertFalse(child.arm())
            cancel.assert_called_once()

    def test_timer_cancel_failure_is_best_effort_and_a_later_close_retries(self):
        with patch.object(obs.faulthandler, "enable"), patch.object(obs.faulthandler, "dump_traceback_later"), patch.object(obs.faulthandler, "cancel_dump_traceback_later", side_effect=[OSError("synthetic close failure"), None]) as cancel:
            child = self.child(self.job(), True)
            child.arm()
            self.assertFalse(child.close())
            self.assertTrue(child.close())
            self.assertEqual(cancel.call_count, 2)

    def test_setup_failure_returns_null_diagnostics(self):
        child = obs.make_child_diagnostics(self.base / "missing", HASHES, True)
        self.assertFalse(child.arm())
        self.assertFalse(child.mark("IMPORT_PADDLE"))
        self.assertFalse(child.close())

    def test_phase_disk_failure_and_name_collision_are_best_effort(self):
        folder = self.job()
        child = self.child(folder)
        with patch.object(obs.os, "fsync", side_effect=OSError("disk full")):
            self.assertFalse(child.mark("CREATE_PIPELINE"))
        self.assertEqual(list(folder.glob("*.pending")), [])
        foreign = folder / ("phase-" + "e" * 32 + ".pending")
        foreign.write_bytes(b"foreign fixture")
        with patch.object(obs.uuid, "uuid4", return_value=SimpleNamespace(hex="e" * 32)):
            self.assertFalse(child.mark("PREDICT_NEXT"))
        self.assertEqual(foreign.read_bytes(), b"foreign fixture")
        self.assertTrue(child.close())
        self.assertFalse(child.mark("DONE"))

    def test_phase_replace_does_not_follow_a_symlink(self):
        folder = self.job()
        target = self.base / "foreign"
        target.write_bytes(b"preserve")
        (folder / "phase.json").symlink_to(target)
        self.assertTrue(self.child(folder).mark("IMPORT_PADDLE"))
        self.assertEqual(target.read_bytes(), b"preserve")

    def test_ring_private_permissions_count_bytes_and_no_stderr_in_metadata(self):
        folder = self.job(b"SECRET_DOCUMENT_VALUE\n" * 20000)
        child = self.child(folder)
        child.bind_source(SOURCE)
        child.mark("PREDICT_NEXT")
        ring = self.ring()
        for index in range(9):
            self.assertIsNotNone(self.retain(ring, folder, request_id=format(index, "032x")))
        self.assertEqual(len(ring.records), 4)
        self.assertEqual(len(list(ring.root.iterdir())), 8)
        self.assertLessEqual(sum(p.stat().st_size for p in ring.root.iterdir()), obs.MAX_TOTAL_BYTES)
        self.assertEqual(stat.S_IMODE(ring.root.stat().st_mode), 0o700)
        for path in ring.root.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            if path.suffix == ".json":
                self.assertNotIn(b"SECRET_DOCUMENT_VALUE", path.read_bytes())
                metadata = json.loads(path.read_bytes())
                self.assertEqual(metadata["source_sha256"], SOURCE)
                self.assertTrue(metadata["stderr_truncated"])
                self.assertLessEqual(metadata["stderr_captured_bytes"], obs.STDERR_LIMIT)
        self.assertEqual(len([t for t in threading.enumerate() if t.name == "idp-diagnostic-retention"]), 1)

    def test_ring_refuses_running_child_wrong_job_source_and_unlisted_errors(self):
        folder, ring = self.job(), self.ring()
        for kwargs in ({"process": SimpleNamespace(poll=lambda: None)}, {"job_identity": (0, 0, 0)},
            {"source": "d" * 64}, {"request_id": "../foreign"}, {"code": "PRIVATE_RAW_EXCEPTION"}):
            with self.subTest(keys=sorted(kwargs)):
                self.assertIsNone(self.retain(ring, folder, **kwargs))
        self.assertIsNone(ring.root)

    def test_symlink_and_fifo_stderr_fail_closed_without_blocking(self):
        folder, ring = self.job(), self.ring()
        (folder / "error.log").unlink()
        target = self.base / "private-other"
        target.write_bytes(b"foreign")
        (folder / "error.log").symlink_to(target)
        self.assertIsNone(self.retain(ring, folder))
        (folder / "error.log").unlink()
        os.mkfifo(folder / "error.log")
        started = time.monotonic()
        self.assertIsNone(self.retain(ring, folder))
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(target.read_bytes(), b"foreign")

    def test_atomic_metadata_failure_rolls_back_stderr_and_pending_files(self):
        folder, ring = self.job(), self.ring()
        original = obs.os.link

        def fail_metadata(source, destination, **kwargs):
            if destination.endswith(".json"):
                raise OSError("synthetic metadata failure")
            return original(source, destination, **kwargs)

        with patch.object(obs.os, "link", side_effect=fail_metadata):
            self.assertIsNone(self.retain(ring, folder))
        self.assertEqual(ring.records, [])
        self.assertEqual(list(ring.root.iterdir()), [])

    def test_cleaner_start_failure_rolls_back_and_next_attempt_can_recover(self):
        folder, ring = self.job(), self.ring()
        with patch.object(obs.threading.Thread, "start", side_effect=RuntimeError("synthetic thread failure")):
            self.assertIsNone(self.retain(ring, folder))
        self.assertIsNone(ring.thread)
        self.assertEqual(ring.records, [])
        self.assertEqual(list(ring.root.iterdir()), [])
        self.assertIsNotNone(self.retain(ring, folder))
        ring.close(clear=True)
        self.assertIsNone(self.retain(ring, folder))

    def test_failed_root_setup_quarantines_instead_of_creating_unbounded_directories(self):
        folder, ring = self.job(), self.ring()
        with patch.object(obs.Path, "chmod", side_effect=OSError("synthetic root setup failure")):
            for _ in range(20):
                self.assertIsNone(self.retain(ring, folder))
        self.assertTrue(ring.quarantined)
        self.assertEqual(len(list(self.base.glob("idp-failures-*"))), 1)
        self.assertEqual(list(ring.root.iterdir()), [])

    def test_final_name_collision_never_removes_preexisting_file(self):
        folder, ring = self.job(), self.ring()
        ring._ensure_root()
        foreign = ring.root / ("f" * 32 + ".stderr")
        foreign.write_bytes(b"foreign collision fixture")
        with patch.object(obs.uuid, "uuid4", return_value=SimpleNamespace(hex="f" * 32)):
            self.assertIsNone(self.retain(ring, folder))
        self.assertEqual(foreign.read_bytes(), b"foreign collision fixture")
        self.assertEqual(len(list(ring.root.iterdir())), 1)

    def test_cleanup_replacement_is_preserved_and_quarantines_further_writes(self):
        folder, ring = self.job(), self.ring()
        self.assertIsNotNone(self.retain(ring, folder))
        name = ring.records[0]["files"][0][0]
        foreign = ring.root / name
        foreign.unlink()
        foreign.write_bytes(b"preserve replacement")
        ring.prune(now=float("inf"))
        self.assertTrue(ring.quarantined)
        self.assertEqual(foreign.read_bytes(), b"preserve replacement")
        self.assertIsNone(self.retain(ring, folder))

    def test_ttl_reclaims_records_without_another_request(self):
        folder, ring = self.job(), self.ring(ttl_seconds=1)
        self.assertIsNotNone(self.retain(ring, folder))
        deadline = time.monotonic() + 3
        while ring.records and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(ring.records, [])
        self.assertEqual(list(ring.root.iterdir()), [])

    def test_oversized_or_forged_phase_is_not_republished(self):
        folder, ring = self.job(), self.ring()
        (folder / "phase.json").write_bytes(b"PRIVATE_SYNTHETIC_VALUE" * 4000)
        self.assertIsNotNone(self.retain(ring, folder))
        metadata = json.loads(next(ring.root.glob("*.json")).read_bytes())
        self.assertIsNone(metadata["child_metadata"])
        self.assertNotIn("PRIVATE_SYNTHETIC_VALUE", json.dumps(metadata))


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "Linux runtime filesystem contract")
class RunnerFixtures(unittest.TestCase):
    def test_fake_predictor_keeps_options_and_results_with_broken_diagnostic_io(self):
        self.run_runner_fixture(broken_diagnostics=True)

    def test_fake_predictor_failure_remains_identical_and_cancels_watchdog(self):
        self.run_runner_fixture(prediction_failure=True)

    def run_runner_fixture(self, broken_diagnostics=False, prediction_failure=False):
        spec = importlib.util.spec_from_file_location("runner_diagnostic_fixture", ROOT / "paddle_runner.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        calls = []
        failure = RuntimeError("synthetic original predictor exception")

        class Predictor:
            def __init__(self, **kwargs):
                calls.append(("construct", kwargs))

            def predict(self, *args, **kwargs):
                calls.append(("predict", args, kwargs))
                if prediction_failure:
                    raise failure
                yield SimpleNamespace(json={"res": {"width": 100, "height": 100, "parsing_res_list": []}})

        paddle = SimpleNamespace(__version__="synthetic", is_compiled_with_cuda=lambda: True,
            device=SimpleNamespace(cuda=SimpleNamespace(device_count=lambda: 1, get_device_name=lambda _: "synthetic GPU")))
        original_open = open

        def open_model(path, *args, **kwargs):
            return io.BytesIO(b"synthetic model bytes") if path == "/models/paddle/model.safetensors" else original_open(path, *args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="idp-runner-fixture-") as directory:
            folder = Path(directory)
            (folder / "request.json").write_text(json.dumps({"filename": "document.pdf", "source_sha256": SOURCE, "regions": []}))
            with patch.dict(sys.modules, {"paddle": paddle, "paddleocr": SimpleNamespace(PaddleOCRVL=Predictor)}), patch.object(runner.importlib.metadata, "version", return_value="synthetic"), patch.object(sys, "argv", [str(ROOT / "paddle_runner.py"), str(folder)]), patch.dict(os.environ, {"TRACKVANCE_IDP_STACK_DIAGNOSTICS": "1"}), patch("builtins.open", side_effect=open_model), patch.object(obs.faulthandler, "enable"), patch.object(obs.faulthandler, "dump_traceback_later"), patch.object(obs.faulthandler, "cancel_dump_traceback_later") as cancel:
                if broken_diagnostics:
                    with patch.object(obs.os, "fsync", side_effect=OSError("synthetic diagnostic disk failure")):
                        runner.main()
                elif prediction_failure:
                    with self.assertRaises(RuntimeError) as raised:
                        runner.main()
                    self.assertIs(raised.exception, failure)
                cancel.assert_called_once()
            if not prediction_failure:
                result = json.loads((folder / "result.json").read_bytes())
                self.assertEqual(result["lines"], [])
                self.assertEqual(result["source_sha256"], SOURCE)
                self.assertFalse(result["engine"]["independent_row_coverage"])
            self.assertEqual(calls[0][1]["pipeline_version"], "v1.6")
            self.assertEqual(calls[0][1]["device"], "gpu:0")
            self.assertIs(calls[0][1]["use_queues"], False)
            self.assertEqual(calls[0][1]["vl_rec_max_concurrency"], 1)
            self.assertEqual(calls[1], ("predict", (str(folder / "document.pdf"),), {}))


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "Linux runtime filesystem contract")
class HTTPFailureFixture(unittest.TestCase):
    def test_timeout_reaps_before_private_retention_and_keeps_http_error_exact(self):
        self.timeout_fixture()

    def test_retention_failure_does_not_replace_normalized_http_timeout(self):
        self.timeout_fixture(retention_failure=True)

    def timeout_fixture(self, retention_failure=False):
        spec = importlib.util.spec_from_file_location("gateway_diagnostic_fixture", ROOT / "server.py")
        gateway = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gateway)
        gateway.RUNTIME["ready"] = True
        state = {"returncode": None, "reaped": False, "retained": False}
        ring = gateway.FAILURE_DIAGNOSTICS

        class Child:
            pid = 989898

            def __init__(self, command, **kwargs):
                kwargs["stderr"].write(b"SECRET_DOCUMENT_VALUE\n")

            def poll(self):
                return state["returncode"]

            def wait(self, timeout):
                if state["returncode"] is None:
                    clock["now"] = 28.0
                    raise subprocess.TimeoutExpired("synthetic", timeout)
                state["reaped"] = True
                return state["returncode"]

        def kill(_pid, _signal):
            state["returncode"] = -9

        original = ring.retain

        def retain(*args):
            self.assertTrue(state["reaped"])
            state["retained"] = True
            if retention_failure:
                raise OSError("synthetic diagnostic failure with SECRET_DOCUMENT_VALUE")
            return original(*args)

        # Admission and Budget share one controlled clock. Time stays zero
        # until the already-created synthetic child waits; then only work time
        # expires, leaving the same caller allowance available for reap/retention.
        clock = {"now": 0.0}
        monotonic = lambda: clock["now"]
        real_budget = gateway.MEMORY.Budget
        def budget(configured, requested=None, *, started=None):
            return real_budget(configured, requested, clock=monotonic, started=started)
        def barrier(*_args, **_kwargs):
            self.assertFalse(gateway.ACTIVE)
            self.assertFalse(gateway.UPSTREAM)
            return {"status": "EMPTY_RESIDENCY_OBSERVED"}
        raw = b"synthetic only"
        body = {"request_id": "e" * 32, "filename": "document.pdf", "source": base64.b64encode(raw).decode(),
            "source_sha256": hashlib.sha256(raw).hexdigest(), "configuration": {"timeout_seconds": 30,
                "paddle_model": "PaddleOCR-VL-1.6", "llm_model": gateway.LOCK["qwen"]["model"],
                "prompt_version": "trackvance-idp-098-v2"}}
        server = gateway.ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(gateway, "verify_models", return_value=True), patch.object(gateway.subprocess, "Popen", Child), patch.object(gateway.os, "killpg", side_effect=kill), patch.object(gateway, "time", SimpleNamespace(monotonic=monotonic)), patch.object(gateway.MEMORY, "Budget", side_effect=budget), patch.object(gateway, "memory_barrier", side_effect=barrier), patch.object(gateway, "CACHE_HYGIENE", False), patch.object(ring, "retain", side_effect=retain):
                connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                connection.request("POST", "/extract", body=json.dumps(body), headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                content = response.read()
                connection.close()
            self.assertEqual(response.status, 504)
            self.assertEqual(json.loads(content), {"error": "INFERENCE_TIMEOUT"})
            self.assertNotIn(b"SECRET_DOCUMENT_VALUE", content)
            self.assertTrue(state["retained"])
            self.assertFalse(gateway.ACTIVE)
            self.assertEqual(len(ring.records), 0 if retention_failure else 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            ring.close(clear=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
