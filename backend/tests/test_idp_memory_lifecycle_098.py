"""Private software-only lifecycle tests: no Docker, models, native API or GPU."""
import ast
import http.client
import importlib.util
import io
import json
import math
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import CodeType, FunctionType, SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy/idp"


def load_runtime(name, filename):
    """Load test-owned modules without changing import paths or shared gateway modules."""
    previous = {key: value for key, value in sys.modules.items()
        if key == "trackvance_idp_frozen" or key.startswith("trackvance_idp_frozen.")}
    path = Path(filename)
    spec = importlib.util.spec_from_file_location(name, path if path.is_absolute() else DEPLOY / path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        for key in list(sys.modules):
            if key == "trackvance_idp_frozen" or key.startswith("trackvance_idp_frozen."):
                sys.modules.pop(key)
        sys.modules.update(previous)
    return module


cache = load_runtime("_test_idp_memory_cache_098", "idp_cache_advice.py")
memory = load_runtime("_test_idp_memory_lifecycle_098", "idp_memory_lifecycle.py")


class Clock:
    now = 0.0
    def __call__(self): return self.now
    def sleep(self, amount): self.now += amount


class BudgetTests(unittest.TestCase):
    def test_slow_body_time_is_counted_from_handler_arrival(self):
        clock = Clock(); clock.now = 2.0
        budget = memory.Budget(600, 1.0, clock=clock, started=0.0)
        with self.assertRaises(TimeoutError): budget.remaining()
        with self.assertRaises(TimeoutError): budget.remaining(cleanup=True)

    def test_one_relative_allowance_includes_queue_work_and_cleanup(self):
        clock = Clock()
        budget = memory.Budget(600, 47, clock=clock)
        self.assertEqual(budget.deadline, 47)
        clock.now = 20
        self.assertAlmostEqual(budget.remaining(), 22.3)
        self.assertEqual(budget.remaining(cleanup=True), 27)
        clock.now = 43
        with self.assertRaises(TimeoutError): budget.remaining()
        self.assertEqual(budget.remaining(cleanup=True), 4)
        clock.now = 47
        with self.assertRaises(TimeoutError): budget.remaining(cleanup=True)
        self.assertEqual(budget.join_remaining(), 0)

    def test_fallback_once_and_invalid_budgets_do_not_expand_configuration(self):
        self.assertEqual(memory.Budget(600).duration, 600)
        for invalid in (0, -1, True, math.inf, math.nan, "30", 601):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError): memory.Budget(600, invalid)


class QuiescenceTests(unittest.TestCase):
    pin: ClassVar = {"model": "owned:exact", "manifest_sha256": "a" *64}
    def resident(self): return {"name": self.pin["model"], "model": self.pin["model"], "digest": self.pin["manifest_sha256"]}
    def test_unload_ack_requires_later_empty_ps_and_same_finite_budget(self):
        clock, calls = Clock(), []
        budget = memory.Budget(30, 1, clock=clock)
        ps = [{"models": [self.resident()]}, {"models": [self.resident()]}, {"models": []}]
        def request(path, body, cleanup):
            calls.append((path, body, cleanup))
            if path == "/ps": return ps.pop(0)
            if path == "/tags": return {"models": [self.resident()]}
            return {"model": self.pin["model"], "done": True, "done_reason": "unload"}
        result = memory.quiet_ollama(budget, request, self.pin, lambda: True, cleanup=True, sleep=clock.sleep)
        self.assertEqual(result["polls"], 3)
        self.assertEqual(sum(path == "/generate" for path, *_ in calls), 1)
        self.assertEqual(calls[2][1], {"model": self.pin["model"], "prompt": "", "keep_alive": 0, "stream": False})
        self.assertTrue(all(cleanup for _, _, cleanup in calls))

    def test_ack_without_empty_ps_expires_instead_of_certifying_release(self):
        clock = Clock()
        budget = memory.Budget(30, .2, clock=clock)
        def request(path, _body, _cleanup):
            if path in {"/ps", "/tags"}: return {"models": [self.resident()]}
            return {"model": self.pin["model"], "done": True, "done_reason": "unload"}
        with self.assertRaises(TimeoutError):
            memory.quiet_ollama(budget, request, self.pin, lambda: True, cleanup=True, sleep=clock.sleep)
        self.assertLessEqual(clock.now, .2)

    def test_foreign_wrong_digest_duplicate_and_unknown_residency_never_unload(self):
        for models in ([{**self.resident(), "name": "foreign"}], [{**self.resident(), "digest": "b"*64}],
                [self.resident(), self.resident()], None, {}):
            calls = []
            def request(path, *_args, calls=calls, models=models):
                calls.append(path); return {"models": models}
            with self.subTest(models=models), self.assertRaises(memory.UnsafeLifecycle):
                memory.quiet_ollama(memory.Budget(30), request, self.pin, lambda: True)
            self.assertEqual(calls, ["/ps"])

    def test_unreaped_reader_prevents_even_residency_rpc(self):
        with self.assertRaisesRegex(memory.UnsafeLifecycle, "OWNER_QUIESCENCE"):
            memory.quiet_ollama(memory.Budget(30), lambda *_: self.fail("RPC while reader active"), self.pin, lambda: False)

    def test_tag_rebinding_and_invalid_ack_are_not_unload_proof(self):
        for bad_tag in (True, False):
            calls = []
            def request(path, *_args, calls=calls, bad_tag=bad_tag):
                calls.append(path)
                if path == "/ps": return {"models": [self.resident()]}
                if path == "/tags": return {"models": [{**self.resident(), "digest": "b"*64 if bad_tag else self.pin["manifest_sha256"]}]}
                return {"model": self.pin["model"], "done": True, "done_reason": "stop"}
            with self.subTest(bad_tag=bad_tag), self.assertRaises(memory.UnsafeLifecycle):
                memory.quiet_ollama(memory.Budget(30), request, self.pin, lambda: True)
            self.assertEqual("/generate" in calls, not bad_tag)


class ReaderTests(unittest.TestCase):
    def test_unexpected_operation_error_is_prompt_terminal_failure_with_private_cause(self):
        original = AttributeError("private upstream /credentials/detail")
        published, withdrawn = [], []
        class Connection:
            sock = None
            def connect(self): pass
            def close(self): pass
        def operation(_connection): raise original
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, "^LLM_FAILURE$") as caught:
            memory.read_joined(operation, memory.Budget(600, 1), lambda: False, published.append,
                withdrawn.append, connection_factory=lambda *_args, **_kwargs: Connection())
        self.assertIs(caught.exception.__cause__, original)
        self.assertEqual(published, withdrawn)
        self.assertLess(time.monotonic() - started, .5)

    def test_real_delayed_chunked_response_remains_valid_beyond_poll_interval(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *_args): pass
            def do_GET(self):
                self.send_response(200); self.send_header("Connection", "close")
                self.send_header("Transfer-Encoding", "chunked"); self.end_headers(); self.wfile.flush()
                time.sleep(.12)
                self.wfile.write(b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"); self.wfile.flush()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}); serving.start()
        published, withdrawn = [], []
        def factory(_host, _port, **kwargs): return http.client.HTTPConnection(*server.server_address, **kwargs)
        def operation(connection):
            connection.request("GET", "/chunked")
            response = connection.getresponse()
            try: return response.read(1024)
            finally: response.close()
        try:
            self.assertEqual(memory.read_joined(operation, memory.Budget(30, 1), lambda: False,
                published.append, withdrawn.append, connection_factory=factory), b"hello world")
            self.assertEqual(published, withdrawn)
        finally:
            server.shutdown(); server.server_close(); serving.join()

    def test_real_dribbled_body_cannot_extend_original_deadline(self):
        release = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_GET(self):
                self.send_response(200); self.send_header("Connection", "close"); self.end_headers()
                try:
                    while not release.wait(.02):
                        self.wfile.write(b"x"); self.wfile.flush()
                except OSError:
                    pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}); serving.start()
        published, withdrawn = [], []
        def factory(_host, _port, **kwargs): return http.client.HTTPConnection(*server.server_address, **kwargs)
        def operation(connection):
            connection.request("GET", "/dribble")
            response = connection.getresponse()
            try: return response.readline(1024)
            finally: response.close()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(TimeoutError, "INFERENCE_TIMEOUT"):
                memory.read_joined(operation, memory.Budget(30, .3), lambda: False,
                    published.append, withdrawn.append, connection_factory=factory)
            self.assertLess(time.monotonic() - started, .7)
            self.assertEqual(published, withdrawn)
        finally:
            release.set(); server.shutdown(); server.server_close(); serving.join()

    def test_real_connection_close_stalled_body_cancel_joins_detached_socket_reader(self):
        headers, release_server, cancelled = (threading.Event() for _ in range(3))
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_GET(self):
                self.send_response(200); self.send_header("Connection", "close")
                self.end_headers(); self.wfile.flush()
                release_server.wait(2)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}); serving.start()
        published, withdrawn, detached = [], [], []
        def factory(_host, _port, **kwargs): return http.client.HTTPConnection(*server.server_address, **kwargs)
        def operation(connection):
            connection.request("GET", "/stall")
            response = connection.getresponse()
            try:
                detached.append(connection.sock is None)
                headers.set()
                return response.readline(64)
            finally:
                response.close()
        def cancel(): headers.wait(1); cancelled.set()
        trigger = threading.Thread(target=cancel); trigger.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(ValueError, "CANCELLED"):
                memory.read_joined(operation, memory.Budget(30, 1), cancelled.is_set, published.append, withdrawn.append,
                    connection_factory=factory)
            self.assertLess(time.monotonic() - started, .5)
            self.assertEqual(detached, [True])
            self.assertEqual(published, withdrawn)
        finally:
            release_server.set(); trigger.join(); server.shutdown(); server.server_close(); serving.join()

    def test_cancel_during_connect_closes_late_socket_before_request_can_continue(self):
        entered, connected, cancelled, socket_closed = (threading.Event() for _ in range(4))
        sent = []
        class Connection:
            sock = None
            def connect(self):
                entered.set(); connected.wait(1)
                self.sock = SimpleNamespace(shutdown=lambda _how: socket_closed.set(), close=socket_closed.set, settimeout=lambda _: None)
            def close(self):
                if self.sock is not None: socket_closed.set()
        def operation(connection):
            connection.connect(); sent.append("request")
        def cancel():
            entered.wait(1); cancelled.set(); time.sleep(.07); connected.set()
        trigger = threading.Thread(target=cancel); trigger.start()
        try:
            with self.assertRaisesRegex(ValueError, "CANCELLED"):
                memory.read_joined(operation, memory.Budget(30, .5), cancelled.is_set, lambda _: None, lambda _: None,
                    connection_factory=lambda *_args, **_kwargs: Connection())
            self.assertFalse(sent); self.assertTrue(socket_closed.is_set())
        finally:
            connected.set(); trigger.join()

    def test_cancel_interrupts_delayed_headers_and_joins_before_withdraw(self):
        reading, stopped, cancelled = threading.Event(), threading.Event(), threading.Event()
        published, withdrawn = [], []
        class Connection:
            sock = SimpleNamespace(shutdown=lambda _how: stopped.set())
            def connect(self): pass
            def close(self): stopped.set()
        def operation(_connection):
            reading.set(); stopped.wait(1)
            raise OSError("synthetic closed socket")
        def cancel():
            reading.wait(1); cancelled.set()
        trigger = threading.Thread(target=cancel); trigger.start()
        with self.assertRaisesRegex(ValueError, "CANCELLED"):
            memory.read_joined(operation, memory.Budget(30, .5), cancelled.is_set, published.append, withdrawn.append,
                connection_factory=lambda *_args, **_kwargs: Connection())
        trigger.join()
        self.assertEqual(published, withdrawn)
        self.assertTrue(stopped.is_set())

    def test_uninterruptible_reader_is_retained_as_unknown_owner(self):
        stop = threading.Event()
        published, withdrawn = [], []
        class Connection:
            sock = None
            def connect(self): pass
            def close(self): pass
        def operation(_connection): stop.wait(.5)
        try:
            with self.assertRaises(memory.UnsafeLifecycle):
                memory.read_joined(operation, memory.Budget(30, .05), lambda: False, published.append, withdrawn.append,
                    connection_factory=lambda *_args, **_kwargs: Connection())
            self.assertEqual(len(published), 1)
            self.assertFalse(withdrawn)
        finally:
            stop.set(); time.sleep(.02)


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Mirror the deployed source_path mapping; shared backend modules are
        # copied into the disposable gateway image directory, never sys.path.
        directory = tempfile.TemporaryDirectory(prefix="trackvance-memory-gateway-test-")
        cls.addClassCleanup(directory.cleanup)
        captured = Path(directory.name)
        for path in DEPLOY.iterdir():
            if path.is_file():
                shutil.copyfile(path, captured / path.name)
        for name in ("document_semantic_fragments.py", "document_template_builder_rules.py", "document_literal_tables.py"):
            shutil.copyfile(ROOT / "backend/src/trackvance" / name, captured / name)
        cls.gateway = load_runtime("_test_idp_memory_gateway_098", captured / "server.py")
    @classmethod
    def tearDownClass(cls): cls.gateway.FAILURE_DIAGNOSTICS.close(clear=True)
    def setUp(self):
        self.gateway.MEMORY_STATE.clear(); self.gateway.MEMORY_STATE["status"] = "AVAILABLE"
        self.gateway.MEMORY_FAILURE = None
        for state in (self.gateway.ADMISSIONS, self.gateway.UPSTREAM, self.gateway.ACTIVE, self.gateway.BUDGETS, self.gateway.STATES): state.clear()
    def post(self, path, body, terminal_hook=None):
        handler = self.gateway.Handler.__new__(self.gateway.Handler)
        raw = json.dumps(body).encode()
        handler.path, handler.headers, handler.rfile = path, {"Content-Length": str(len(raw))}, io.BytesIO(raw)
        replies = []
        def send(status, value):
            if terminal_hook is not None: terminal_hook()
            replies.append((status, value))
        handler.send = send
        handler.do_POST(); return replies[-1]
    def test_cleanup_unknown_quarantines_both_routes_and_retains_unmodified_candidate(self):
        g = self.gateway
        candidate = {"source_sha256": "s", "engine": {"elapsed_seconds": .1}, "lines": [{"text": "source"}]}
        with patch.object(g.Handler, "extract", return_value=candidate) as extract, \
                patch.object(g, "memory_barrier", side_effect=g.MEMORY.UnsafeLifecycle("GPU_FOREIGN_RESIDENCY")) as barrier:
            first = self.post("/extract", {"request_id": "a"*32, "configuration": {"timeout_seconds": 30}})
            self.assertEqual(first, (503, {"error": "GPU_FOREIGN_RESIDENCY"}))
            self.assertIs(g.MEMORY_FAILURE["candidate"], candidate)
            self.assertNotIn("memory_lifecycle", candidate["engine"])
            original_failure = g.MEMORY_FAILURE
            for path in ("/extract", "/interpret"):
                self.assertEqual(self.post(path, {"request_id": "b"*32, "configuration": {"timeout_seconds": 30}})[0], 503)
            self.assertEqual(extract.call_count, 1)
            self.assertEqual(barrier.call_count, 1)  # Quarantined calls cannot attempt implicit cleanup/recovery.
            self.assertIs(g.MEMORY_FAILURE, original_failure)
            self.assertIs(g.MEMORY_FAILURE["candidate"], candidate)
            self.assertEqual(g.MEMORY_STATE["reason"], "GPU_FOREIGN_RESIDENCY")
        self.assertFalse(g.GATE.locked()); self.assertFalse(g.ADMISSIONS); self.assertFalse(g.BUDGETS)
        self.assertEqual(self.post("/cancel", {"request_id": "c"*32}), (200, {"cancelled": False}))

    def test_unexpected_upstream_exception_is_fixed_non_success_at_http_boundary(self):
        g = self.gateway
        class Connection:
            sock = None
            def __init__(self, *_args, **_kwargs): pass
            def connect(self): pass
            def close(self): pass
        def extract(_self, body):
            def operation(_connection): raise AttributeError("private upstream /credentials/detail")
            return g.upstream_read(body["request_id"], g.BUDGETS[body["request_id"]], operation)
        with patch.object(g.Handler, "extract", extract), patch.object(g.http.client, "HTTPConnection", Connection),                 patch.object(g, "memory_barrier", return_value={"status": "EMPTY_RESIDENCY_OBSERVED"}):
            status, value = self.post("/extract", {"request_id": "7"*32, "configuration": {"timeout_seconds": 600}, "request_budget_seconds": 1})
        self.assertEqual((status, value), (422, {"error": "LLM_FAILURE"}))
        self.assertFalse(g.UPSTREAM); self.assertFalse(g.GATE.locked())

    def test_advisory_errors_are_metadata_and_never_rewrite_candidate(self):
        g = self.gateway
        candidate = {"engine": {"elapsed_seconds": .1}, "lines": [{"text": "unchanged"}]}
        with patch.object(g.Handler, "extract", return_value=candidate), \
                patch.object(g, "memory_barrier", return_value={"status": "EMPTY_RESIDENCY_OBSERVED"}), \
                patch.object(g, "cache_hygiene", return_value={"status": "PARTIAL_ADVICE_OS_ERRORS", "advice_os_errors": 2}):
            status, value = self.post("/extract", {"request_id": "d"*32, "configuration": {"timeout_seconds": 30}})
        self.assertEqual(status, 200)
        self.assertEqual(value["lines"], [{"text": "unchanged"}])
        self.assertEqual(value["engine"]["memory_lifecycle"]["cache_advice"]["advice_os_errors"], 2)
        self.assertEqual(g.MEMORY_STATE["status"], "AVAILABLE")

    def test_budget_expiring_in_queue_cannot_reset_after_gate_or_leave_admission(self):
        g = self.gateway
        self.assertTrue(g.GATE.acquire(blocking=False))
        try:
            with patch.object(g.Handler, "extract", side_effect=AssertionError("expired queue started model")):
                status, _ = self.post("/extract", {"request_id": "e"*32, "configuration": {"timeout_seconds": 600}, "request_budget_seconds": .05})
            self.assertIn(status, (422, 504))
            self.assertTrue(g.GATE.locked())
            self.assertFalse(g.BUDGETS); self.assertFalse(g.ADMISSIONS); self.assertFalse(g.STATES)
        finally:
            g.GATE.release()

    def test_explicit_null_budget_does_not_take_legacy_timeout_fallback(self):
        status, value = self.post("/extract", {"request_id": "f"*32, "configuration": {"timeout_seconds": 600}, "request_budget_seconds": None})
        self.assertEqual((status, value), (422, {"error": "REQUEST_BUDGET_INVALID"}))
        self.assertFalse(self.gateway.ADMISSIONS)

    def test_cancel_accepted_during_cleanup_cannot_become_success(self):
        g, identity = self.gateway, "c"*32
        candidate = {"engine": {"elapsed_seconds": 0}, "lines": []}
        def cleanup(*_args, **_kwargs):
            g.CANCELLED.add(identity)
            return {"status": "EMPTY_RESIDENCY_OBSERVED"}
        with patch.object(g.Handler, "extract", return_value=candidate), patch.object(g, "memory_barrier", side_effect=cleanup):
            status, value = self.post("/extract", {"request_id": identity, "configuration": {"timeout_seconds": 30}})
        self.assertEqual((status, value), (422, {"error": "CANCELLED"}))
        self.assertFalse(g.ADMISSIONS); self.assertFalse(g.CANCELLED); self.assertFalse(g.GATE.locked())

    def test_cancel_after_atomic_terminal_decision_is_not_accepted(self):
        g, identity = self.gateway, "f"*32
        cancellations = []
        with patch.object(g.Handler, "extract", return_value={"engine": {"elapsed_seconds": 0}}), \
                patch.object(g, "memory_barrier", return_value={"status": "EMPTY_RESIDENCY_OBSERVED"}):
            status, _ = self.post("/extract", {"request_id": identity, "configuration": {"timeout_seconds": 30}},
                terminal_hook=lambda: cancellations.append(self.post("/cancel", {"request_id": identity})))
        self.assertEqual(status, 200)
        self.assertEqual(cancellations, [(200, {"cancelled": False})])


class CachePolicyTests(unittest.TestCase):
    def test_actual_advice_main_treats_timeout_as_advisory_before_oserror(self):
        resource = SimpleNamespace(setrlimit=lambda *_args: None,
            RLIMIT_AS=1, RLIMIT_CPU=2, RLIMIT_NOFILE=3, RLIMIT_FSIZE=4, RLIMIT_CORE=5)
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"resource": resource}))
            stack.enter_context(patch.object(sys, "argv", ["helper", "1"]))
            stack.enter_context(patch.object(cache.signal, "SIGALRM", 14, create=True))
            stack.enter_context(patch.object(cache.signal, "ITIMER_REAL", 0, create=True))
            stack.enter_context(patch.object(cache.signal, "signal"))
            stack.enter_context(patch.object(cache.signal, "setitimer", create=True))
            stack.enter_context(patch.object(cache, "advise", side_effect=TimeoutError("synthetic deadline")))
            stack.enter_context(redirect_stdout(output))
            cache.main()
        self.assertEqual(json.loads(output.getvalue())["status"], "ADVICE_TIMEOUT")

    def test_only_fixed_ro_model_and_software_roots_with_no_user_path(self):
        self.assertEqual(cache.ROOTS, ("/opt/venv", "/models/paddle", "/models/layout", "/models/ollama"))
        base = {"st_mode": stat.S_IFREG | 0o644, "st_uid": 65532, "st_dev": 4, "st_nlink": 1}
        cache.validate_file(SimpleNamespace(**base), 4)
        for changed in ({"st_mode": stat.S_IFLNK}, {"st_uid": 0}, {"st_dev": 5}, {"st_nlink": 2}):
            with self.subTest(changed=changed), self.assertRaises(cache.OwnershipUnknown):
                cache.validate_file(SimpleNamespace(**{**base, **changed}), 4)


class ClientBudgetTests(unittest.TestCase):
    def test_actual_invoke_publishes_remaining_phase_global_allowance_without_editing_snapshot(self):
        source = (ROOT / "backend/src/trackvance/document_local_inference.py").read_text(encoding="utf8")
        node = next(item for item in ast.parse(source).body if isinstance(item, ast.FunctionDef) and item.name == "_invoke")
        calls, state = [], {"now": 100.0}
        class Executor:
            def __init__(self, **_kwargs): pass
            def submit(self, _request, path, body, **options):
                calls.append((path, body, options))
                return SimpleNamespace(done=lambda: True, result=lambda: {"actual": "response"})
            def shutdown(self, **_kwargs): pass
        namespace = {"time": SimpleNamespace(monotonic=lambda: state["now"]), "ThreadPoolExecutor": Executor,
            "_RequestControl": lambda: None, "_request": None, "RemoteInferenceTimeout": RuntimeError}
        compiled = compile(ast.Module(body=[node], type_ignores=[]), "<exact-private-invoke>", "exec")
        function_code = next(item for item in compiled.co_consts if isinstance(item, CodeType) and item.co_name == "_invoke")
        invoke = FunctionType(function_code, namespace)
        invoke.__kwdefaults__ = {"minimum_progress": 15, "budget": None, "phase_deadline": None, "optional": False}
        configuration = {"timeout_seconds": 600, "provider": "PADDLE_QWEN_LOCAL"}
        body = {"request_id": "a"*32, "configuration": configuration}
        def check(_progress): state["now"] = 105.0
        budget = SimpleNamespace(require=lambda: 12.0)
        response = invoke("/extract", body, timeout=600, started=80, check=check, budget=budget, phase_deadline=130)
        self.assertEqual(response, {"actual": "response"})
        self.assertEqual(calls[0][1]["request_budget_seconds"], 12.0)
        self.assertEqual(calls[0][2]["timeout"], 12.0)
        self.assertNotIn("request_budget_seconds", body)
        self.assertIs(body["configuration"], configuration)


if __name__ == "__main__": unittest.main()
