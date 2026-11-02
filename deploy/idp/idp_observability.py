"""Bounded operator-only diagnostics and conservative Paddle admission."""
from __future__ import annotations

import faulthandler
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

PHASE_LIMIT = 32768
STDERR_LIMIT = 262144
RECORD_LIMIT = 65536
PARENT_TRACE_LIMIT = 32768
MAX_RECORDS = 4
MAX_TOTAL_BYTES = MAX_RECORDS * (STDERR_LIMIT + RECORD_LIMIT)
STAGES = frozenset({"IMPORT_PADDLE", "IMPORT_PADDLEOCR", "CREATE_PIPELINE", "PREDICT_NEXT",
    "POSTPROCESS_PAGE", "SERIALIZE_OBSERVATIONS", "HASH_MODEL", "WRITE_RESULT", "DONE",
    "OPEN_SOURCE", "RENDER_PAGE", "PERSIST_ORIGINAL_PAGE"})
SAFE_ERRORS = frozenset({"INFERENCE_TIMEOUT", "PADDLE_FAILURE", "CANCELLED", "INFERENCE_FAILED",
    "PADDLE_RESULT_LIMIT", "GPU_OWNER_QUIESCENCE_UNVERIFIED"})


class PaddleAdmission:
    """Sticky serial-service circuit; an opaque failure is never an OOM claim.

    The gateway calls this only under its heavy-work gate, after an actual
    Paddle child spawn. Cancellation and invalid input do not prove recovery.
    Only completed work with confirmed closure clears the consecutive failures.
    Recovery requires an operator-controlled service restart, not an HTTP reset.
    """
    def __init__(self):
        self.consecutive_failures, self.reason, self.last_category = 0, None, None

    def snapshot(self):
        return {"status": "SUSPENDED" if self.reason else "AVAILABLE", "reason": self.reason,
            "consecutive_failures": self.consecutive_failures, "last_category": self.last_category}

    def observe(self, code, *, resources_released):
        if self.reason is not None:
            return self.snapshot()
        if resources_released is not True:
            self.reason, self.last_category = "GPU_CLOSURE_UNCONFIRMED", "CLOSURE_UNCONFIRMED"
        elif code is None:
            self.consecutive_failures, self.last_category = 0, "SUCCESS"
        elif code == "CANCELLED":
            self.last_category = "CANCELLATION"
        else:
            self.last_category = "TIMEOUT" if code == "INFERENCE_TIMEOUT" else "UNKNOWN"
            self.consecutive_failures += 1
            if self.consecutive_failures >= 3:
                self.reason = "THREE_UNRECOVERED_PADDLE_FAILURES_CAUSE_UNPROVEN"
        return self.snapshot()


def valid_child_request(request, source_sha256):
    """Accept the two declared child request shapes without accepting extras."""
    if (not isinstance(request, dict) or request.get("source_sha256") != source_sha256
            or not isinstance(request.get("filename"), str)
            or request["filename"] not in {"document.pdf", "document.png", "document.jpg", "document.jpeg"}):
        return False
    if set(request) == {"filename", "source_sha256", "regions"}:
        return isinstance(request["regions"], list) and len(request["regions"]) <= 4
    if set(request) == {"filename", "source_sha256", "operation", "remaining_work_seconds"}:
        duration = request["remaining_work_seconds"]
        return (isinstance(request["operation"], str) and request["operation"] in {"STRUCTURED", "RENDER"}
            and type(duration) in (int, float)
            and math.isfinite(duration) and 0 < duration <= 1800)
    return False


def parent_trace(error):
    """Private exception chain, never locals; explicitly bounded and hashed."""
    if error is None:
        return b"", 0
    raw = "".join(traceback.format_exception(type(error), error, error.__traceback__)).encode("utf-8", errors="replace")
    original_size = len(raw)
    if original_size > PARENT_TRACE_LIMIT:
        raw = raw[:PARENT_TRACE_LIMIT // 2] + raw[-PARENT_TRACE_LIMIT // 2:]
    return raw, original_size


def checked_hashes(value):
    names = {"paddle_runner.py", "pipeline.yml", "idp_observability.py"}
    if (not isinstance(value, dict) or not {"paddle_runner.py", "pipeline.yml"} <= set(value) <= names
            or any(not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{64}", item) for item in value.values())):
        raise ValueError("DIAGNOSTIC_PROVENANCE")
    return dict(value)


def identity(info):
    return info.st_dev, info.st_ino, info.st_uid


def file_identity(info):
    return (*identity(info), info.st_ctime_ns, info.st_size)


def open_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        os.close(descriptor)
        raise OSError("DIAGNOSTIC_DIRECTORY_IDENTITY")
    return descriptor


def read_at(directory, name, limit, *, tail=False):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > 64 * 1024**2:
            raise OSError("DIAGNOSTIC_FILE_TYPE_OR_LIMIT")
        if tail and before.st_size > limit:
            prefix = min(65536, limit)
            raw = stream.read(prefix)
            stream.seek(before.st_size - (limit - prefix))
            raw += stream.read(limit - prefix)
        else:
            raw = stream.read(limit + 1)
            if len(raw) > limit:
                raise OSError("DIAGNOSTIC_FILE_LIMIT")
        after = os.fstat(stream.fileno())
        if (identity(before), before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                identity(after), after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise OSError("DIAGNOSTIC_FILE_CHANGED")
        return raw, before.st_size


def validate_phase(raw, expected_hashes, source_sha256):
    expected_hashes = checked_hashes(expected_hashes)
    if len(raw) > PHASE_LIMIT:
        raise ValueError("DIAGNOSTIC_PHASE_LIMIT")
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {"version", "events", "events_truncated", "files_sha256", "source_sha256"}
            or type(value["version"]) is not int or value["version"] != 1 or type(value["events_truncated"]) is not bool
            or value["files_sha256"] != expected_hashes or value["source_sha256"] not in (None, source_sha256)
            or not isinstance(value["events"], list) or not 1 <= len(value["events"]) <= 128):
        raise ValueError("DIAGNOSTIC_PHASE_CONTRACT")
    previous = -1
    for event in value["events"]:
        if (not isinstance(event, dict) or set(event) not in ({"stage", "elapsed_seconds"}, {"stage", "elapsed_seconds", "page_ordinal"})
                or not isinstance(event["stage"], str) or event["stage"] not in STAGES or type(event["elapsed_seconds"]) not in (int, float)
                or not math.isfinite(event["elapsed_seconds"]) or not 0 <= event["elapsed_seconds"] <= 86400
                or event["elapsed_seconds"] < previous
                or ("page_ordinal" in event and (type(event["page_ordinal"]) is not int or not 1 <= event["page_ordinal"] <= 50))):
            raise ValueError("DIAGNOSTIC_PHASE_CONTRACT")
        previous = event["elapsed_seconds"]
    return value


def phase_from_job(folder, expected_identity, source_hashes, source_sha256):
    descriptor = None
    try:
        descriptor = open_directory(folder)
        if identity(os.fstat(descriptor)) != tuple(expected_identity):
            return None
        return validate_phase(read_at(descriptor, "phase.json", PHASE_LIMIT)[0], source_hashes, source_sha256)
    except Exception:
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


class NullChildDiagnostics:
    def arm(self):
        return False

    def mark(self, *_args, **_kwargs):
        return False

    def bind_source(self, _source):
        return False

    def close(self):
        return False


class ChildDiagnostics:
    def __init__(self, folder, source_hashes, enable_stacks=False):
        self.hashes = checked_hashes(source_hashes)
        self.enabled = enable_stacks is True
        self.events, self.started, self.source = [], time.monotonic(), None
        self.armed, self.truncated = False, False
        self.directory = open_directory(folder)

    def arm(self):
        if not self.enabled:
            return False
        try:
            self.armed = True  # A partially successful setup must also be cancelled.
            faulthandler.enable(file=os.sys.stderr, all_threads=True)
            faulthandler.dump_traceback_later(60, repeat=False, file=os.sys.stderr)
            return True
        except Exception:
            self._cancel()
            return False

    def _cancel(self):
        if self.armed:
            try:
                faulthandler.cancel_dump_traceback_later()
            except Exception:
                return False
            else:
                self.armed = False
        return True

    def bind_source(self, source):
        if isinstance(source, str) and re.fullmatch(r"[a-f0-9]{64}", source):
            self.source = source
            return True
        return False

    def mark(self, stage, page=None):
        temporary, owned = None, None
        try:
            if self.directory is None or stage not in STAGES or (page is not None and (type(page) is not int or not 1 <= page <= 50)):
                return False
            event = {"stage": stage, "elapsed_seconds": round(time.monotonic() - self.started, 4)}
            if page is not None:
                event["page_ordinal"] = page
            self.events.append(event)
            if len(self.events) > 128:
                self.events.pop(0)
                self.truncated = True
            raw = json.dumps({"version": 1, "events": self.events, "events_truncated": self.truncated,
                "files_sha256": self.hashes, "source_sha256": self.source}, allow_nan=False, separators=(",", ":")).encode()
            if len(raw) > PHASE_LIMIT:
                return False
            temporary = "phase-" + uuid.uuid4().hex + ".pending"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=self.directory)
            owned = file_identity(os.fstat(descriptor))
            with os.fdopen(descriptor, "wb") as stream:
                try:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                finally:
                    owned = file_identity(os.fstat(stream.fileno()))
            os.replace(temporary, "phase.json", src_dir_fd=self.directory, dst_dir_fd=self.directory)
            temporary = None
            return True
        except Exception:
            return False
        finally:
            if temporary is not None and owned is not None:
                try:
                    if file_identity(os.stat(temporary, dir_fd=self.directory, follow_symlinks=False)) == owned:
                        os.unlink(temporary, dir_fd=self.directory)
                except OSError:
                    pass

    def close(self):
        result = self._cancel()
        descriptor, self.directory = self.directory, None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except Exception:
                result = False
        return result


def make_child_diagnostics(folder, source_hashes, enable_stacks=False):
    try:
        return ChildDiagnostics(folder, source_hashes, enable_stacks)
    except Exception:
        return NullChildDiagnostics()


class FailureRing:
    """Four <=320KiB records, one hourly-expiry sweeper, held directory FDs."""

    def __init__(self, source_hashes, *, base=None, ttl_seconds=3600):
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise ValueError("DIAGNOSTIC_TTL")
        self.hashes, self.base, self.ttl = checked_hashes(source_hashes), Path(base or tempfile.gettempdir()), ttl_seconds
        self.root, self.directory, self.root_identity = None, None, None
        self.records, self.lock, self.stop, self.thread = [], threading.Lock(), threading.Event(), None
        self.quarantined = False

    def _ensure_root(self):
        try:
            if self.directory is None:
                self.root = Path(tempfile.mkdtemp(prefix="idp-failures-", dir=self.base))
                self.root.chmod(0o700)
                self.directory = open_directory(self.root)
                self.root_identity = identity(os.fstat(self.directory))
            if (identity(self.root.lstat()) != self.root_identity or self.root.is_symlink()
                    or stat.S_IMODE(self.root.lstat().st_mode) != 0o700):
                raise OSError("DIAGNOSTIC_RING_REPLACED")
        except Exception:
            self.quarantined = True  # Never create repeated untracked roots after partial setup.
            raise

    def _remove(self, entries):
        complete = True
        for name, expected in entries:
            try:
                info = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
                if file_identity(info) == expected and stat.S_ISREG(info.st_mode):
                    os.unlink(name, dir_fd=self.directory)
                else:
                    complete = False
            except FileNotFoundError:
                continue
            except OSError:
                complete = False
        self.quarantined |= not complete
        return complete

    def _prune(self, now, maximum=MAX_RECORDS):
        while self.records and (len(self.records) > maximum or now - self.records[0]["created"] >= self.ttl):
            if not self._remove(self.records[0]["files"]):
                break
            self.records.pop(0)

    def prune(self, now=None):
        with self.lock:
            if self.directory is not None:
                self._prune(time.monotonic() if now is None else now)

    def _sweep(self):
        while not self.stop.wait(min(60, max(1, self.ttl))):
            try:
                self.prune()
            except Exception:
                pass

    def _publish(self, name, raw, created):
        temporary = name + ".pending-" + uuid.uuid4().hex
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=self.directory)
        created.append((temporary, file_identity(os.fstat(descriptor))))
        with os.fdopen(descriptor, "wb") as stream:
            try:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
                # Hard-link publication is atomic and refuses an existing final name.
                os.link(temporary, name, src_dir_fd=self.directory, dst_dir_fd=self.directory, follow_symlinks=False)
                created.append((name, file_identity(os.fstat(stream.fileno()))))
                os.unlink(temporary, dir_fd=self.directory)
                created[:] = [entry for entry in created if entry[0] != temporary]
            finally:
                info = os.fstat(stream.fileno())
                created[:] = [(entry, file_identity(info) if expected[:3] == identity(info) else expected)
                    for entry, expected in created]

    def retain(self, folder, expected_job_identity, request_id, source_sha256, code, process, *, error=None):
        created, job, registered = [], None, None
        try:
            returncode = process.poll()
            if (not re.fullmatch(r"[a-f0-9]{32}", request_id) or not re.fullmatch(r"[a-f0-9]{64}", source_sha256)
                    or code not in SAFE_ERRORS or type(returncode) is not int):
                return None
            folder = Path(folder)
            if folder.parent != self.base or not folder.name.startswith("idp-"):
                return None
            job = open_directory(folder)
            if identity(os.fstat(job)) != tuple(expected_job_identity):
                return None
            request_raw, _ = read_at(job, "request.json", 16384)
            request = json.loads(request_raw)
            if not valid_child_request(request, source_sha256):
                return None
            stderr, original_size = read_at(job, "error.log", STDERR_LIMIT, tail=True)
            trace, trace_original_size = parent_trace(error)
            phase = None
            try:
                phase = validate_phase(read_at(job, "phase.json", PHASE_LIMIT)[0], self.hashes, source_sha256)
            except (OSError, ValueError, TypeError, KeyError):
                pass
            record = {"version": 2, "request_id": request_id, "source_sha256": source_sha256,
                "error": code, "source_hashes": self.hashes, "child_metadata": phase,
                "request_contract": "LEGACY" if "regions" in request else request["operation"],
                "child_returncode": returncode, "child_exit_status": returncode if returncode >= 0 else None,
                "child_termination_signal": -returncode if returncode < 0 else None,
                "stderr_original_bytes": original_size, "stderr_captured_bytes": len(stderr),
                "stderr_truncated": original_size > len(stderr), "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                "parent_trace_original_bytes": trace_original_size, "parent_trace_captured_bytes": len(trace),
                "parent_trace_truncated": trace_original_size > len(trace),
                "parent_trace_sha256": hashlib.sha256(trace).hexdigest(),
                "source_binding": "PARENT_VERIFIED_REQUEST; actual container/image binding is external"}
            raw = json.dumps(record, allow_nan=False, separators=(",", ":")).encode()
            if len(raw) + len(trace) > RECORD_LIMIT or len(stderr) + len(trace) + len(raw) > MAX_TOTAL_BYTES // MAX_RECORDS:
                return None
            with self.lock:
                if self.quarantined or self.stop.is_set():
                    return None
                self._ensure_root()
                self._prune(time.monotonic(), maximum=MAX_RECORDS - 1)
                if self.quarantined:
                    return None
                token = uuid.uuid4().hex
                try:
                    self._publish(token + ".stderr", stderr, created)
                    if trace:
                        self._publish(token + ".parenttrace", trace, created)
                    self._publish(token + ".json", raw, created)
                    registered = {"created": time.monotonic(), "files": list(created), "bytes": len(stderr) + len(trace) + len(raw)}
                    self.records.append(registered)
                    if self.thread is None:
                        candidate_thread = threading.Thread(target=self._sweep, name="idp-diagnostic-retention", daemon=True)
                        candidate_thread.start()
                        self.thread = candidate_thread
                    return token
                except Exception:
                    if registered is not None:
                        self.records.remove(registered)
                    self._remove(created)
                    return None
        except Exception:
            return None
        finally:
            if job is not None:
                try:
                    os.close(job)
                except OSError:
                    pass

    def close(self, clear=False):
        self.stop.set()
        if self.thread is not None and self.thread is not threading.current_thread() and self.thread.is_alive():
            self.thread.join(timeout=1)
        with self.lock:
            if self.directory is not None:
                if clear:
                    self._prune(float("inf"), maximum=0)
                os.close(self.directory)
                self.directory = None
