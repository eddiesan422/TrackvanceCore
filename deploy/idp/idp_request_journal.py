"""Private bounded execution receipts and exact-result recovery, without models."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

VERSION = "TRACKVANCE_IDP_EXECUTION_V1"
TERMINAL = frozenset({"COMPLETE", "FAILED", "TIMEOUT", "CANCELLED", "INTERRUPTED"})
MAX_RESULT = 16 * 1024**2 - 65536
MAX_BYTES = 128 * 1024**2
MAX_RECORDS = 64
MAX_METADATA = 128 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def request_hash(route, body):
    inputs = {key: value for key, value in body.items()
              if key not in {"request_id", "admission_id", "request_budget_seconds", "rpc_contract"}}
    return digest({"route": route, "input": inputs})


def utc(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


class RequestJournal:
    """One owned directory, atomic receipts, a process lock and bounded retention.

    A configured directory survives gateway process restarts. A previous active
    admission becomes INTERRUPTED and quarantines heavy work. Its former process
    is never assumed dead from a PID or an expired timestamp.
    """

    def __init__(self, root=None, *, ttl_seconds=600, clock=time.time, source_hashes=None):
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise ValueError("REQUEST_JOURNAL_CONFIGURATION")
        self.root = Path(root) if root else None
        self.configured = root is not None
        self.ttl, self.clock = ttl_seconds, clock
        self.source_hashes = dict(source_hashes or {})
        self.lock = threading.RLock()
        self.directory = self.directory_identity = self.process_lock = None
        self.records = {}
        self.quarantined = False
        self.initialized = False
        self.stop = threading.Event()
        self.sweeper = None

    def _check_root(self):
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or self.root.is_symlink()
                or getattr(info, "st_file_attributes", 0) & 0x400
                or (info.st_dev, info.st_ino) != self.directory_identity
                or os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700)):
            raise OSError("REQUEST_JOURNAL_REPLACED")

    def _open(self, name, flags, mode=0o600):
        if not re.fullmatch(r"[a-f0-9]{32}(?:-[a-f0-9]{64})?\.(?:json|data|pending)|journal\.(?:lock|quarantine)", name):
            raise ValueError("REQUEST_JOURNAL_PATH")
        self._check_root()
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if os.open in os.supports_dir_fd:
            descriptor = os.open(name, flags, mode, dir_fd=self.directory)
        else:
            path = self.root / name
            if path.exists() and (path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
                raise OSError("REQUEST_JOURNAL_SYMLINK")
            descriptor = os.open(path, flags, mode)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise OSError("REQUEST_JOURNAL_FILE")
        return descriptor

    def _read(self, name, maximum):
        with os.fdopen(self._open(name, os.O_RDONLY), "rb") as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > maximum:
                raise ValueError("REQUEST_JOURNAL_LIMIT")
            raw = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > maximum or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise OSError("REQUEST_JOURNAL_CHANGED")
        return raw

    def _unlink(self, name):
        self._check_root()
        if os.unlink in os.supports_dir_fd:
            os.unlink(name, dir_fd=self.directory)
        else:
            (self.root / name).unlink()

    def _publish(self, name, raw):
        pending = uuid.uuid4().hex + ".pending"
        try:
            with os.fdopen(self._open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self._check_root()
            if os.rename in os.supports_dir_fd:
                os.replace(pending, name, src_dir_fd=self.directory, dst_dir_fd=self.directory)
                os.fsync(self.directory)
            else:
                os.replace(self.root / pending, self.root / name)
        finally:
            try:
                self._unlink(pending)
            except FileNotFoundError:
                pass

    def _save(self, record):
        raw = canonical(record)
        if len(raw) > MAX_METADATA:
            raise ValueError("REQUEST_JOURNAL_LIMIT")
        self._publish(record["request_id"] + ".json", raw)

    def _quarantine(self):
        self.quarantined = True
        self._publish("journal.quarantine", canonical({"version": VERSION, "resources_released": False}))

    def _initialize(self):
        if self.quarantined and not self.initialized:
            raise ValueError("REQUEST_JOURNAL_UNAVAILABLE")
        if self.initialized:
            self._check_root()
            return
        if self.root is None:
            self.root = Path(tempfile.mkdtemp(prefix="idp-requests-"))
        else:
            self.root.mkdir(mode=0o700, exist_ok=True)
        for parent in (self.root, *self.root.parents):
            info = parent.lstat()
            if parent.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise OSError("REQUEST_JOURNAL_SYMLINK")
        info = self.root.lstat()
        self.directory_identity = (info.st_dev, info.st_ino)
        initial_names = [entry.name for entry in self.root.iterdir()]
        if initial_names and "journal.lock" not in initial_names:
            raise ValueError("REQUEST_JOURNAL_UNAVAILABLE")
        self.directory = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)) if os.name == "posix" else None
        self.process_lock = self._open("journal.lock", os.O_RDWR | os.O_CREAT)
        if os.name == "posix":
            import fcntl
            fcntl.flock(self.process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt
            os.write(self.process_lock, b"0")
            os.lseek(self.process_lock, 0, os.SEEK_SET)
            msvcrt.locking(self.process_lock, msvcrt.LK_NBLCK, 1)
        names = [entry.name for entry in self.root.iterdir()]
        if len(names) > MAX_RECORDS * 54 + 2:
            raise ValueError("REQUEST_JOURNAL_LIMIT")
        self.quarantined = "journal.quarantine" in names
        for name in names:
            if re.fullmatch(r"[a-f0-9]{32}\.json", name):
                record = json.loads(self._read(name, MAX_METADATA))
                if (record.get("version") != VERSION or record.get("request_id", "") + ".json" != name
                        or record.get("status") not in TERMINAL | {"QUEUED", "RUNNING"}
                        or not isinstance(record.get("page_refs"), list) or len(record["page_refs"]) > 50
                        or any(type(record.get(key)) not in (int, float) or not math.isfinite(record[key])
                               for key in ("created_unix", "expires_unix"))):
                    raise ValueError("REQUEST_JOURNAL_CONTRACT")
                self.records[record["request_id"]] = record
                if record.get("status") not in TERMINAL:
                    record.update(status="INTERRUPTED", stage="TERMINAL", finished_at=utc(self.clock()),
                                  resources_released=False, error_code="GATEWAY_INTERRUPTED")
                    self._save(record)
                if record.get("resources_released") is not True:
                    self._quarantine()
        if len(self.records) > MAX_RECORDS:
            raise ValueError("REQUEST_JOURNAL_LIMIT")
        known = {"journal.lock", "journal.quarantine"}
        for record in self.records.values():
            known.add(record["request_id"] + ".json")
            for ref in [*record["page_refs"], *([record["result_ref"]] if record.get("result_ref") else [])]:
                if (ref.get("file") != record["request_id"] + "-" + ref.get("sha256", "") + ".data"
                        or not re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", ""))
                        or type(ref.get("bytes")) is not int or not 0 < ref["bytes"] <= 32 * 1024**2):
                    raise ValueError("REQUEST_JOURNAL_CONTRACT")
                known.add(ref["file"])
        if set(names) - known:
            self._quarantine()  # Preserve uncommitted bytes, never erase them to admit more work.
        self._bound()
        self.initialized = True
        self.sweeper = threading.Thread(target=self._sweep, name="idp-request-retention", daemon=True)
        self.sweeper.start()

    def _ready(self):
        try:
            self._initialize()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.quarantined = True
            raise ValueError("REQUEST_JOURNAL_UNAVAILABLE") from None

    def _sweep(self):
        while not self.stop.wait(min(30, self.ttl)):
            try:
                with self.lock:
                    self._prune()
            except (OSError, ValueError):
                self.quarantined = True

    def _remove(self, record):
        if record.get("status") not in TERMINAL or record.get("resources_released") is not True:
            self._quarantine()
            raise ValueError("REQUEST_JOURNAL_UNAVAILABLE")
        removed = set()
        for ref in [*record["page_refs"], *([record["result_ref"]] if record.get("result_ref") else [])]:
            if ref["file"] in removed:
                continue
            raw = self._read(ref["file"], MAX_BYTES)
            if len(raw) != ref["bytes"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                raise ValueError("REQUEST_JOURNAL_INTEGRITY")
            self._unlink(ref["file"])
            removed.add(ref["file"])
        self._unlink(record["request_id"] + ".json")
        del self.records[record["request_id"]]

    def _prune(self):
        now = self.clock()
        for record in list(self.records.values()):
            if now < record["created_unix"]:
                self._quarantine()
                raise ValueError("REQUEST_JOURNAL_CLOCK")
            if record["status"] in TERMINAL:
                if record.get("resources_released") is not True:
                    self._quarantine()
                elif now >= record["expires_unix"]:
                    self._remove(record)

    def _bound(self, extra=0):
        size = sum(sum(ref["bytes"] for ref in record["page_refs"])
                   + (record.get("result_ref") or {}).get("bytes", 0) + len(canonical(record))
                   for record in self.records.values())
        size += MAX_METADATA  # Reserve the next atomic metadata publication.
        if size + extra > MAX_BYTES:
            raise ValueError("REQUEST_JOURNAL_LIMIT")

    def _record(self, identity, admission):
        record = self.records.get(identity)
        if record is None or record["admission_id"] != admission:
            raise ValueError("REQUEST_BINDING")
        return record

    def admit(self, route, body, budget):
        with self.lock:
            self._ready()
            self._prune()
            if self.quarantined:
                raise ValueError("REQUEST_JOURNAL_UNAVAILABLE")
            self._bound(MAX_RESULT)
            identity = body["request_id"]
            if identity in self.records:
                raise ValueError("DUPLICATE_REQUEST")
            if len(self.records) >= MAX_RECORDS:
                raise ValueError("REQUEST_JOURNAL_LIMIT")
            admission = body.get("admission_id") or uuid.uuid4().hex
            if not re.fullmatch(r"[a-f0-9]{32}", admission):
                raise ValueError("REQUEST_BINDING")
            now = self.clock()
            record = {"version": VERSION, "request_id": identity, "admission_id": admission,
                "request_sha256": request_hash(route, body), "configuration_sha256": digest(body["configuration"]),
                "source_sha256": body.get("source_sha256"), "definition_sha256": digest(body.get("definition")),
                "route": route, "status": "QUEUED", "stage": "QUEUED", "page_one_based": None,
                "expected_pages": None, "emitted_pages": [], "completed_pages": [], "page_refs": [], "resources_released": None,
                "gateway_pid": os.getpid(), "child": None, "child_history": [], "cancel_requested": False,
                "runtime_files_sha256": self.source_hashes,
                "cancel_outcome": None, "error_code": None, "original_error_code": None, "cleanup_error_code": None,
                "response_delivery": "NOT_ATTEMPTED",
                "started_at": utc(now), "finished_at": None, "last_progress": None,
                "budget_seconds": budget.duration, "created_unix": now, "expires_unix": now + self.ttl}
            self._save(record)
            self.records[identity] = record
            return copy.deepcopy(record)

    def update(self, identity, admission, **changes):
        with self.lock:
            record = copy.deepcopy(self._record(identity, admission))
            if record["status"] in TERMINAL:
                raise ValueError("REQUEST_ALREADY_TERMINAL")
            child, previous = changes.get("child"), record.get("child")
            if child is not None and previous is not None:
                if child["pid"] == previous["pid"] and child["start_token"] is None:
                    changes["child"] = {**child, "start_token": previous["start_token"]}
                elif child["pid"] != previous["pid"]:
                    record["child_history"].append(previous)
                    if len(record["child_history"]) > 50:
                        raise ValueError("REQUEST_JOURNAL_LIMIT")
            record.update(changes)
            self._save(record)
            self.records[identity] = record
            return copy.deepcopy(record)

    def checkpoint(self, identity, admission, value, *, page=None, kind="PAGE", complete=True):
        if type(complete) is not bool or (page is not None and (type(page) is not int or not 1 <= page <= 50)):
            raise ValueError("REQUEST_JOURNAL_CONTRACT")
        raw = canonical(value)
        if len(raw) > 32 * 1024**2:
            raise ValueError("REQUEST_JOURNAL_LIMIT")
        with self.lock:
            record = copy.deepcopy(self._record(identity, admission))
            if record["status"] in TERMINAL:
                raise ValueError("REQUEST_ALREADY_TERMINAL")
            self._bound(len(raw))
            sha = hashlib.sha256(raw).hexdigest()
            name = identity + "-" + sha + ".data"
            ref = {"file": name, "sha256": sha, "bytes": len(raw), "page": page, "kind": kind, "complete": complete}
            if ref in record["page_refs"]:
                return copy.deepcopy(ref)
            if len(record["page_refs"]) >= 50:
                raise ValueError("REQUEST_JOURNAL_LIMIT")
            self._publish(name, raw)
            record["page_refs"].append(ref)
            if page is not None:
                record["emitted_pages"] = sorted(set(record["emitted_pages"]) | {page})
                if complete:
                    record["completed_pages"] = sorted(set(record["completed_pages"]) | {page})
            self._save(record)
            self.records[identity] = record
            return copy.deepcopy(ref)

    def finish(self, identity, admission, status, *, released, error=None, original_error=None,
               cleanup_error=None, result=None, recognition_terminal=None):
        with self.lock:
            record = copy.deepcopy(self._record(identity, admission))
            if status not in TERMINAL or record["status"] in TERMINAL:
                raise ValueError("REQUEST_ALREADY_TERMINAL")
            if result is not None:
                raw = canonical(result)
                if len(raw) > MAX_RESULT or status != "COMPLETE" or released is not True:
                    raise ValueError("REQUEST_JOURNAL_LIMIT")
                self._bound(len(raw))
                sha = hashlib.sha256(raw).hexdigest()
                name = identity + "-" + sha + ".data"
                self._publish(name, raw)
                record["result_ref"] = {"file": name, "sha256": sha, "bytes": len(raw)}
            record.update(status=status, stage="TERMINAL", finished_at=utc(self.clock()),
                resources_released=released, error_code=error, original_error_code=original_error,
                cleanup_error_code=cleanup_error, cancel_outcome="ACKNOWLEDGED" if record["cancel_requested"] else "NOT_REQUESTED",
                expires_unix=self.clock() + self.ttl)
            if recognition_terminal is not None:
                record["recognition_terminal"] = copy.deepcopy(recognition_terminal)
                record["expected_pages"] = recognition_terminal.get("expected_pages")
            self._save(record)
            self.records[identity] = record
            if released is not True:
                self._quarantine()
            return self.public(record)

    def public(self, record):
        value = copy.deepcopy(record)
        value.pop("result_ref", None)
        value.pop("recognition_terminal", None)
        value.pop("created_unix", None)
        value.pop("expires_unix", None)
        for ref in value["page_refs"]:
            ref.pop("file", None)
        expected = value["expected_pages"]
        value["pending_pages"] = ([page for page in range(1, expected + 1) if page not in value["completed_pages"]]
                                  if type(expected) is int and 1 <= expected <= 50 else None)
        return value

    def delivery(self, identity, admission, delivered):
        with self.lock:
            record = copy.deepcopy(self._record(identity, admission))
            if record["status"] not in TERMINAL:
                raise ValueError("REQUEST_ALREADY_TERMINAL")
            record["response_delivery"] = "SENT_TO_TRANSPORT" if delivered else "DISCONNECTED"
            self._save(record)
            self.records[identity] = record

    def terminal_event(self, identity):
        with self.lock:
            record = self.records.get(identity)
            return copy.deepcopy(record.get("recognition_terminal")) if record else None

    def available(self):
        try:
            with self.lock:
                self._ready()
                self._prune()
                return not self.quarantined
        except (OSError, ValueError):
            self.quarantined = True
            return False

    def get(self, identity):
        with self.lock:
            self._ready()
            self._prune()
            record = self.records.get(identity)
            if record and self._read(identity + ".json", MAX_METADATA) != canonical(record):
                self._quarantine()
                raise ValueError("REQUEST_JOURNAL_INTEGRITY")
            return self.public(record) if record else None

    def recover(self, body):
        with self.lock:
            self._ready()
            self._prune()
            record = self._record(body["request_id"], body.get("admission_id"))
            if self._read(record["request_id"] + ".json", MAX_METADATA) != canonical(record):
                self._quarantine()
                raise ValueError("REQUEST_JOURNAL_INTEGRITY")
            if (record["request_sha256"] != body.get("request_sha256")
                    or record["configuration_sha256"] != body.get("configuration_sha256")
                    or record["status"] != "COMPLETE" or record["resources_released"] is not True
                    or not record.get("result_ref")):
                raise ValueError("REQUEST_BINDING")
            ref = record["result_ref"]
            raw = self._read(ref["file"], MAX_RESULT)
            if len(raw) != ref["bytes"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                raise ValueError("REQUEST_JOURNAL_INTEGRITY")
            return {"execution": self.public(record), "result": json.loads(raw), "result_sha256": ref["sha256"]}

    def close(self):
        self.stop.set()
        if self.sweeper is not None and self.sweeper is not threading.current_thread():
            self.sweeper.join(timeout=2)
        with self.lock:
            for descriptor in (self.process_lock, self.directory):
                if descriptor is not None:
                    os.close(descriptor)
            self.process_lock = self.directory = None
