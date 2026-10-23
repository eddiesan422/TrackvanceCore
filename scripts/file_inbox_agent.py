#!/usr/bin/env python3
"""Receive arriving CSV/TXT/XLSX files through the existing public acquisition API.

Run ``python scripts/file_inbox_agent.py --config /absolute/operator-config.json``.
Credentials are read only from TRACKVANCE_INBOX_USERNAME/PASSWORD in the process
environment. This optional foreground process is stopped by default. It never
moves/deletes original files, creates datasets, installs an OS service, or changes
Core limits. Its journal and immutable spool must be outside the watched inbox.

ON_CHANGE identity includes the destination, reader/schema and content, excluding
file names, polling intervals, credentials and local spool paths. Lost mutation
responses are not replayed. In particular, a lost staging response requires manual
review: the current public API cannot look up an unknown upload ID. Known failed
acquisitions are retained, without automatic retries; use the existing product
operation explicitly after inspecting the native terminal failure.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import signal
import sys
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

BUFFER_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_JOURNAL_ENTRIES = 10_000
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
PENDING = {"SNAPSHOT", "STAGED", "REGISTERED", "BLOCKED_OBSERVATION"}
REGISTER_UNCONFIRMED = {"REGISTER_INTENT", "UNCERTAIN_REGISTER", "BLOCKED_RECONCILIATION"}
FENCED = {"STAGE_INTENT", "UNCERTAIN_STAGE", "BLOCKED_RESPONSE"}
TERMINAL_ACQUISITIONS = {"SUCCESS", "FAILED", "CANCELLED"}


class AgentError(Exception):
    """Only fixed public diagnostic codes may be printed or journalled."""


class UncertainResponse(AgentError):
    """The remote mutation may have committed; never automatically repeat it."""


class RejectedResponse(AgentError):
    def __init__(self, status, code="REQUEST_REJECTED"):
        self.status = status
        self.code = code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) else "REQUEST_REJECTED"
        super().__init__(self.code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(BUFFER_BYTES):
            digest.update(block)
    return digest.hexdigest()


def validate_config(raw):
    """Configuration is literal metadata, never credentials or source data."""
    allowed = {"api_origin", "inbox", "state_directory", "poll_seconds", "settle_seconds",
               "request_timeout_seconds", "max_file_bytes", "min_free_bytes", "routes"}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise AgentError("CONFIG_UNKNOWN_KEYS")
    config = json.loads(canonical(raw))
    origin = urlsplit(config.get("api_origin", ""))
    if (origin.scheme not in {"http", "https"} or not origin.hostname or origin.username
            or origin.password or origin.path not in {"", "/"} or origin.query or origin.fragment):
        raise AgentError("CONFIG_API_ORIGIN_REQUIRED")
    config["api_origin"] = config["api_origin"].rstrip("/")
    for name in ("inbox", "state_directory"):
        value = config.get(name)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise AgentError("CONFIG_ABSOLUTE_DIRECTORIES_REQUIRED")
        config[name] = str(Path(value).resolve())
    inbox, state = Path(config["inbox"]), Path(config["state_directory"])
    if state == inbox or state.is_relative_to(inbox) or inbox.is_relative_to(state):
        raise AgentError("CONFIG_SEPARATE_DIRECTORIES_REQUIRED")
    if not inbox.is_dir():
        raise AgentError("CONFIG_INBOX_MUST_EXIST")
    bounds = {"poll_seconds": (1, .01, 300), "settle_seconds": (2, .01, 3600),
              "request_timeout_seconds": (120, 1, 120),
              "max_file_bytes": (512 * 1024**2, 1, 8 * 1024**3),
              "min_free_bytes": (256 * 1024**2, 0, 1024**4)}
    for name, (default, lower, upper) in bounds.items():
        value = config.setdefault(name, default)
        if (type(value) not in {int, float} or not math.isfinite(value)
                or not lower <= value <= upper or (name.endswith("bytes") and type(value) is not int)):
            raise AgentError("CONFIG_LIMIT_INVALID")
    routes = config.get("routes")
    if not isinstance(routes, list) or not 1 <= len(routes) <= 3:
        raise AgentError("CONFIG_ROUTES_REQUIRED")
    suffixes = set()
    for route in routes:
        if not isinstance(route, dict) or set(route) - {"suffix", "dataset_id", "format", "expected_header", "reader_options", "column_overrides"}:
            raise AgentError("CONFIG_ROUTE_INVALID")
        suffix = route.get("suffix")
        if suffix not in {".csv", ".txt", ".xlsx"} or suffix in suffixes:
            raise AgentError("CONFIG_SUFFIX_AMBIGUOUS")
        suffixes.add(suffix)
        if route.get("format") != suffix[1:].upper():
            raise AgentError("CONFIG_FORMAT_MISMATCH")
        if not isinstance(route.get("dataset_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", route["dataset_id"]):
            raise AgentError("CONFIG_DATASET_REQUIRED")
        header = route.get("expected_header")
        if (not isinstance(header, list) or not 1 <= len(header) <= 100
                or any(not isinstance(name, str) or not name or len(name) > 240 for name in header)
                or len(set(header)) != len(header)):
            raise AgentError("CONFIG_HEADER_REQUIRED")
        options = route.setdefault("reader_options", {})
        if not isinstance(options, dict) or set(options) - {"sheet_name", "delimiter"}:
            raise AgentError("CONFIG_READER_OPTIONS_INVALID")
        for key, value in list(options.items()):
            if value is None or value == "":
                del options[key]
            elif not isinstance(value, str) or len(value) > 240:
                raise AgentError("CONFIG_READER_OPTIONS_INVALID")
        delimiter = options.get("delimiter")
        if delimiter is not None and (len(delimiter) != 1 or delimiter in "\r\n\0"):
            raise AgentError("CONFIG_DELIMITER_INVALID")
        overrides = route.setdefault("column_overrides", {})
        if (not isinstance(overrides, dict) or set(overrides) - set(header)
                or len(canonical(overrides)) > 32 * 1024):
            raise AgentError("CONFIG_COLUMN_OVERRIDES_INVALID")
        for column, value in overrides.items():
            value = {"logical_type": value} if isinstance(value, str) else value
            if (not isinstance(value, dict) or set(value) - {"logical_type", "semantic_tag"}
                    or value.get("logical_type") not in {None, "STRING", "DECIMAL", "INT64", "DATE", "TIMESTAMP", "BOOLEAN"}
                    or value.get("semantic_tag") not in {None, "IDENTIFIER"}
                    or (value.get("semantic_tag") == "IDENTIFIER" and value.get("logical_type") not in {None, "STRING"})):
                raise AgentError("CONFIG_COLUMN_OVERRIDES_INVALID")
            overrides[column] = {key: item for key, item in value.items() if item is not None}
    return config


def business_key(config, route, content_sha256):
    business = {name: route[name] for name in ("dataset_id", "format", "expected_header", "reader_options", "column_overrides")}
    business.update(api_origin=config["api_origin"], content_sha256=content_sha256)
    return hashlib.sha256(canonical(business).encode("utf-8")).hexdigest()


def atomic_json(path, value):
    temporary = path.with_suffix(".new")
    encoded = (canonical(value) + "\n").encode("utf-8")
    if len(encoded) > MAX_JOURNAL_BYTES:
        raise AgentError("JOURNAL_SIZE_LIMIT")
    with temporary.open("wb") as target:
        target.write(encoded)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class StateLock:
    """An OS lock, held for the entire foreground daemon lifetime."""
    def __init__(self, path):
        self.path, self.file = path, None

    def __enter__(self):
        self.file = self.path.open("a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
            os.fsync(self.file.fileno())
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise AgentError("STATE_LOCKED") from None
        return self

    def __exit__(self, *_):
        if os.name == "nt":
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()


class APIClient:
    """Bounded streaming public HTTP client; no redirects, proxies or POST retries."""
    def __init__(self, config):
        self.origin = urlsplit(config["api_origin"])
        self.timeout = config["request_timeout_seconds"]
        self.cookie = self.csrf = None

    def request(self, method, path, body=None, file=None, key=None, expected=200):
        connection_type = http.client.HTTPSConnection if self.origin.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(self.origin.hostname, self.origin.port, timeout=self.timeout)
        headers = {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        if key:
            headers["Idempotency-Key"] = key
        encoded = canonical(body).encode("utf-8") if body is not None else None
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        try:
            if file is None:
                connection.request(method, "/api/v1" + path, body=encoded, headers=headers)
            else:
                headers.update({"Content-Type": "application/octet-stream", "Content-Length": str(file.stat().st_size)})
                connection.putrequest(method, "/api/v1" + path)
                for name, value in headers.items():
                    connection.putheader(name, value)
                connection.endheaders()
                with file.open("rb") as source:
                    while block := source.read(BUFFER_BYTES):
                        connection.send(block)
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise UncertainResponse("RESPONSE_LIMIT")
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                raise UncertainResponse("RESPONSE_INVALID") from None
            if 400 <= response.status < 500 and response.status != 408:
                error = result.get("error", {}) if isinstance(result, dict) else {}
                code = error.get("code") if isinstance(error, dict) else None
                raise RejectedResponse(response.status, code)
            if response.status != expected or not isinstance(result, dict):
                raise UncertainResponse("RESPONSE_UNCONFIRMED")
            if path == "/auth/login":
                cookie = SimpleCookie()
                cookie.load(response.getheader("Set-Cookie", ""))
                if "trackvance_session" not in cookie or not isinstance(result.get("csrf_token"), str):
                    raise AgentError("AUTH_SESSION_REQUIRED")
                self.cookie = "trackvance_session=" + cookie["trackvance_session"].value
                self.csrf = result["csrf_token"]
                if result.get("user", {}).get("must_change_password"):
                    raise AgentError("AUTH_PASSWORD_CHANGE_REQUIRED")
            return result
        except (OSError, http.client.HTTPException):
            raise UncertainResponse("TRANSPORT_UNCONFIRMED") from None
        finally:
            connection.close()

    def login(self):
        username = os.environ.get("TRACKVANCE_INBOX_USERNAME")
        password = os.environ.get("TRACKVANCE_INBOX_PASSWORD")
        if not username or not password:
            raise AgentError("AUTH_ENVIRONMENT_REQUIRED")
        self.request("POST", "/auth/login", {"username": username, "password": password})

    def stage(self, path, filename):
        return self.request("POST", "/datasets/uploads/stage?" + urlencode({"filename": filename}), file=path, expected=201)

    def inspect(self, upload_id, options):
        return self.request("GET", "/datasets/uploads/" + quote(upload_id, safe="") + "/inspect?" + urlencode({"reader_options": canonical(options)}))

    def register(self, dataset_id, body, key):
        return self.request("POST", "/datasets/" + quote(dataset_id, safe="") + "/acquisitions", body, key=key, expected=202)

    def get(self, run_id):
        return self.request("GET", "/acquisitions/" + quote(run_id, safe=""))

    def acquisitions(self, dataset_id, offset):
        return self.request("GET", "/acquisitions?" + urlencode({"dataset_id": dataset_id, "offset": offset, "limit": 100}))


def file_identity(stat):
    identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    # Windows stat() exposes creation time here, while fstat() can expose change
    # time. Compare the actual handle/file identity and nanosecond mtime there.
    return identity if os.name == "nt" else (*identity, stat.st_ctime_ns)


class InboxAgent:
    def __init__(self, config, api, *, clock=time.monotonic, stopping=None):
        self.config, self.api, self.clock = config, api, clock
        self.stopping = stopping or (lambda: False)
        self.state = Path(config["state_directory"])
        self.state.mkdir(parents=True, exist_ok=True)
        self.spool = self.state / "spool"
        self.spool.mkdir(exist_ok=True)
        self.journal_path = self.state / "journal.json"
        if self.journal_path.exists() and self.journal_path.stat().st_size > MAX_JOURNAL_BYTES:
            raise AgentError("JOURNAL_SIZE_LIMIT")
        self.journal = json.loads(self.journal_path.read_text(encoding="utf-8")) if self.journal_path.exists() else {"schema_version": 1, "api_origin": config["api_origin"], "entries": {}}
        if self.journal.get("schema_version") != 1 or not isinstance(self.journal.get("entries"), dict):
            raise AgentError("JOURNAL_INVALID")
        if self.journal.get("api_origin") != config["api_origin"]:
            raise AgentError("JOURNAL_API_ORIGIN_CHANGED")
        for key, item in self.journal["entries"].items():
            if (not isinstance(item, dict) or item.get("key") != key
                    or not isinstance(item.get("content_sha256"), str)
                    or not re.fullmatch("[a-f0-9]{64}", item["content_sha256"])
                    or key != business_key(config, item["route"], item["content_sha256"])
                    or item.get("spool_name") != key + item["route"]["suffix"]):
                raise AgentError("JOURNAL_ENTRY_INVALID")
            if item.get("state") in PENDING | REGISTER_UNCONFIRMED | FENCED and item["route"] not in config["routes"]:
                raise AgentError("PENDING_ROUTE_CHANGED")
        self.observed, self.reconciled = {}, set()

    def save(self):
        atomic_json(self.journal_path, self.journal)

    def set_state(self, item, state, code=None):
        if item.get("state") == state and item.get("diagnostic_code") == code:
            return
        item.update(state=state, updated_at_unix=time.time())
        item.pop("diagnostic_code", None)
        if code:
            item["diagnostic_code"] = code
        history = item.setdefault("history", [])
        history.append({"state": state, "at_unix": item["updated_at_unix"], "code": code})
        item["history"] = history[-64:]
        self.save()

    def snapshot(self, path, route):
        before = path.stat(follow_symlinks=False)
        if before.st_size > self.config["max_file_bytes"] or not before.st_size:
            raise AgentError("SOURCE_SIZE_OUT_OF_BOUNDS")
        if shutil.disk_usage(self.state).free - before.st_size < self.config["min_free_bytes"]:
            raise AgentError("SPOOL_DISK_INSUFFICIENT")
        temporary = self.spool / "snapshot.partial"
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        digest, size = hashlib.sha256(), 0
        try:
            with os.fdopen(descriptor, "rb") as source, temporary.open("wb") as target:
                if file_identity(os.fstat(source.fileno())) != file_identity(before):
                    raise AgentError("SOURCE_CHANGED_DURING_SNAPSHOT")
                while block := source.read(BUFFER_BYTES):
                    size += len(block)
                    if size > self.config["max_file_bytes"]:
                        raise AgentError("SOURCE_SIZE_OUT_OF_BOUNDS")
                    digest.update(block)
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
                if file_identity(os.fstat(source.fileno())) != file_identity(before):
                    raise AgentError("SOURCE_CHANGED_DURING_SNAPSHOT")
            if file_identity(path.stat(follow_symlinks=False)) != file_identity(before) or size != before.st_size:
                raise AgentError("SOURCE_CHANGED_DURING_SNAPSHOT")
            content = digest.hexdigest()
            key = business_key(self.config, route, content)
            destination = self.spool / (key + route["suffix"])
            if destination.exists():
                if destination.stat().st_size != size or digest_file(destination) != content:
                    raise AgentError("SPOOL_CONTENT_MISMATCH")
                temporary.unlink()
            else:
                os.replace(temporary, destination)
            return key, destination, content, size
        finally:
            # Only this agent's identified partial snapshot; never an input file.
            temporary.unlink(missing_ok=True)

    def content_probe(self, path):
        """A stable metadata cache cannot prove unchanged content (e.g. copy2)."""
        before = path.stat(follow_symlinks=False)
        if not 0 < before.st_size <= self.config["max_file_bytes"]:
            raise AgentError("SOURCE_SIZE_OUT_OF_BOUNDS")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        digest, size = hashlib.sha256(), 0
        with os.fdopen(descriptor, "rb") as source:
            if file_identity(os.fstat(source.fileno())) != file_identity(before):
                raise AgentError("SOURCE_CHANGED_DURING_SNAPSHOT")
            while block := source.read(BUFFER_BYTES):
                size += len(block)
                if size > self.config["max_file_bytes"]:
                    raise AgentError("SOURCE_SIZE_OUT_OF_BOUNDS")
                digest.update(block)
            if file_identity(os.fstat(source.fileno())) != file_identity(before):
                raise AgentError("SOURCE_CHANGED_DURING_SNAPSHOT")
        if size != before.st_size or file_identity(path.stat(follow_symlinks=False)) != file_identity(before):
            raise AgentError("SOURCE_CHANGED_DURING_SNAPSHOT")
        return digest.hexdigest()

    def reconcile(self, item):
        matches, offset = {}, 0
        for _ in range(100):
            page = self.api.acquisitions(item["route"]["dataset_id"], offset)
            rows, total = page.get("items"), page.get("total")
            if not isinstance(rows, list) or type(total) is not int or total < 0:
                raise AgentError("RECONCILIATION_RESPONSE_INVALID")
            for run in rows:
                if self.matches(item, run):
                    matches[run["id"]] = run
            offset += len(rows)
            if offset >= total:
                break
            if not rows:
                raise AgentError("RECONCILIATION_PAGE_INCOMPLETE")
        else:
            raise AgentError("RECONCILIATION_PAGE_LIMIT")
        if len(matches) != 1:
            self.set_state(item, "BLOCKED_RECONCILIATION", "EXACT_PUBLIC_MATCH_REQUIRED")
            return
        item["acquisition_id"] = next(iter(matches))
        self.set_state(item, "REGISTERED", "RECOVERED_BY_READ_ONLY_LOOKUP")

    @staticmethod
    def matches(item, run):
        if not isinstance(run, dict):
            return False
        snapshot, route = run.get("source_snapshot", {}), item["route"]
        return (isinstance(snapshot, dict) and isinstance(run.get("id"), str)
                and run.get("source_type") == "UPLOAD" and run.get("dataset_id") == route["dataset_id"]
                and snapshot.get("upload_id") == item.get("upload_id")
                and snapshot.get("sha256") == item["content_sha256"]
                and snapshot.get("size_bytes") == item["size_bytes"]
                and snapshot.get("source_format") == route["format"]
                and run.get("reader_options") == route["reader_options"]
                and run.get("column_overrides") == route["column_overrides"])

    def advance(self, item):
        state = item["state"]
        if state == "STAGE_INTENT":
            self.set_state(item, "UNCERTAIN_STAGE", "STAGE_RESPONSE_NOT_DURABLE")
            return
        if state in REGISTER_UNCONFIRMED:
            try:
                self.reconcile(item)
            except (AgentError, KeyError, TypeError):
                self.set_state(item, "BLOCKED_RECONCILIATION", "EXACT_PUBLIC_MATCH_REQUIRED")
            return
        if item.get("acquisition_id") and state not in TERMINAL_ACQUISITIONS:
            try:
                run = self.api.get(item["acquisition_id"])
                if not self.matches(item, run) or run.get("id") != item["acquisition_id"]:
                    raise AgentError("ACQUISITION_IDENTITY_MISMATCH")
                status = run.get("status")
                if status == "SUCCESS":
                    output = run.get("output_version_id")
                    if not isinstance(output, str) or not output:
                        raise AgentError("SUCCESS_OUTPUT_REQUIRED")
                    item["output_version_id"] = output
                    item["published_rows"] = run.get("published_rows")
                    self.set_state(item, "SUCCESS")
                elif status in {"FAILED", "CANCELLED"}:
                    code = run.get("error_code")
                    code = code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) else "NATIVE_TERMINAL_FAILURE"
                    self.set_state(item, status, code)
                elif status in {"QUEUED", "RUNNING"}:
                    self.set_state(item, "REGISTERED")
                else:
                    raise AgentError("ACQUISITION_STATUS_INVALID")
            except (RejectedResponse, UncertainResponse):
                self.set_state(item, "BLOCKED_OBSERVATION", "KNOWN_ACQUISITION_GET_UNAVAILABLE")
            except (AgentError, KeyError, TypeError):
                self.set_state(item, "BLOCKED_RESPONSE", "ACQUISITION_RESPONSE_INVALID")
            return
        route = item["route"]
        if state == "SNAPSHOT":
            if self.stopping():
                return
            path = self.spool / item["spool_name"]
            if path.stat().st_size != item["size_bytes"] or digest_file(path) != item["content_sha256"]:
                self.set_state(item, "BLOCKED_RESPONSE", "SPOOL_CONTENT_MISMATCH")
                return
            self.set_state(item, "STAGE_INTENT")
            try:
                response = self.api.stage(path, item["filename"])
                upload = response.get("upload") if isinstance(response, dict) else None
                if not isinstance(upload, dict):
                    self.set_state(item, "UNCERTAIN_STAGE", "UPLOAD_RESPONSE_INVALID")
                    return
                if (not isinstance(upload.get("id"), str) or not upload["id"]
                        or upload.get("sha256") != item["content_sha256"]
                        or upload.get("size_bytes") != item["size_bytes"]
                        or upload.get("format") != route["format"] or upload.get("transfer_complete") is not True):
                    self.set_state(item, "BLOCKED_RESPONSE", "UPLOAD_SNAPSHOT_MISMATCH")
                    return
                item["upload_id"] = upload["id"]
                self.set_state(item, "STAGED")
            except RejectedResponse as error:
                self.set_state(item, "FAILED_STAGE", error.code)
                return
            except (UncertainResponse, KeyError, TypeError):
                self.set_state(item, "UNCERTAIN_STAGE", "STAGE_RESPONSE_NOT_DURABLE")
                return
        if item["state"] == "STAGED":
            if self.stopping():
                return
            try:
                inspection = self.api.inspect(item["upload_id"], route["reader_options"])
                columns = inspection.get("columns", [])
                header = [column.get("name") if isinstance(column, dict) else None for column in columns] if isinstance(columns, list) else []
                if header != route["expected_header"] or inspection.get("format") != route["format"]:
                    self.set_state(item, "BLOCKED_SCHEMA", "DECLARED_HEADER_MISMATCH")
                    return
            except (RejectedResponse, UncertainResponse):
                # GET has no new business effect. A later tick/restart may retry it.
                self.set_state(item, "STAGED", "STAGED_INSPECTION_UNAVAILABLE")
                return
            if self.stopping():
                return
            item["request"] = {"upload_id": item["upload_id"], "reader_options": route["reader_options"], "column_overrides": route["column_overrides"]}
            self.set_state(item, "REGISTER_INTENT")
            try:
                run = self.api.register(route["dataset_id"], item["request"], "inbox-" + item["key"])
                if not self.matches(item, run):
                    self.set_state(item, "UNCERTAIN_REGISTER", "REGISTER_RESPONSE_INVALID")
                    return
                item["acquisition_id"] = run["id"]
                self.set_state(item, "REGISTERED")
            except RejectedResponse as error:
                self.set_state(item, "FAILED_REGISTER", error.code)
            except (UncertainResponse, KeyError, TypeError):
                self.set_state(item, "UNCERTAIN_REGISTER", "REGISTER_RESPONSE_NOT_DURABLE")

    def tick(self):
        # Restart recovery always precedes discovering another file. Only one
        # acquisition is observed/registered at a time, across all declared routes.
        # A fence is global: a different filename, route or restart cannot bypass
        # a remote acquisition whose terminal result is still unknown.
        for item in self.journal["entries"].values():
            if item["state"] in REGISTER_UNCONFIRMED:
                if item["key"] not in self.reconciled:
                    self.reconciled.add(item["key"])
                    self.advance(item)
                return
        for item in self.journal["entries"].values():
            if item.get("acquisition_id") and item["state"] not in TERMINAL_ACQUISITIONS:
                self.advance(item)
                return
        for item in self.journal["entries"].values():
            if item["state"] in FENCED:
                if item["state"] == "STAGE_INTENT":
                    self.advance(item)
                return
        for item in self.journal["entries"].values():
            if item["state"] in PENDING:
                self.advance(item)
                return
        suffixes = {route["suffix"]: route for route in self.config["routes"]}
        paths = sorted(Path(self.config["inbox"]).iterdir())
        if len(paths) > MAX_JOURNAL_ENTRIES:
            raise AgentError("INBOX_ENTRY_LIMIT")
        for path in paths:
            if path.suffix.lower() not in suffixes or path.is_symlink() or getattr(path, "is_junction", lambda: False)() or not path.is_file():
                continue
            if path.resolve().parent != Path(self.config["inbox"]):
                continue
            stat = path.stat(follow_symlinks=False)
            identity = file_identity(stat)
            previous = self.observed.get(str(path))
            if previous is None or previous[0] != identity:
                self.observed[str(path)] = (identity, self.clock())
                continue
            if self.clock() - previous[1] < self.config["settle_seconds"]:
                continue
            route = suffixes[path.suffix.lower()]
            try:
                content = self.content_probe(path)
                if business_key(self.config, route, content) in self.journal["entries"]:
                    continue
                key, spool, digest, size = self.snapshot(path, route)
                if digest != content:
                    raise AgentError("SOURCE_CHANGED_DURING_SNAPSHOT")
            except AgentError as error:
                if str(error) != "SOURCE_CHANGED_DURING_SNAPSHOT":
                    raise
                self.observed.pop(str(path), None)
                return
            if key in self.journal["entries"]:
                continue
            if len(self.journal["entries"]) >= MAX_JOURNAL_ENTRIES:
                raise AgentError("JOURNAL_ENTRY_LIMIT")
            item = {"key": key, "state": "SNAPSHOT", "route": route, "filename": path.name,
                    "spool_name": spool.name, "content_sha256": digest, "size_bytes": size,
                    "received_at_unix": time.time()}
            self.journal["entries"][key] = item
            self.save()
            self.advance(item)
            return


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    stopping = threading.Event()
    for name in ("SIGINT", "SIGTERM"):
        signal.signal(getattr(signal, name), lambda *_: stopping.set())
    try:
        if not args.config.is_absolute():
            raise AgentError("CONFIG_ABSOLUTE_FILE_REQUIRED")
        with args.config.open("rb") as source:
            raw = source.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise AgentError("CONFIG_SIZE_LIMIT")
        config = validate_config(json.loads(raw))
        state = Path(config["state_directory"])
        state.mkdir(parents=True, exist_ok=True)
        with StateLock(state / "agent.lock"):
            client = APIClient(config)
            client.login()
            receiver = InboxAgent(config, client, stopping=stopping.is_set)
            print(canonical({"status": "WATCHING", "implementation_version": "0.9.8", "publication_policy": "ON_CHANGE", "agent_sha256": digest_file(Path(__file__))}), flush=True)
            while not stopping.is_set():
                receiver.tick()
                stopping.wait(config["poll_seconds"])
        print(canonical({"status": "STOPPED"}), flush=True)
        return 0
    except (AgentError, OSError, ValueError, TypeError) as error:
        # Never serialize exception text, HTTP bodies, URLs or auth material.
        code = str(error) if isinstance(error, AgentError) else "INBOX_AGENT_STOPPED_REVIEW_REQUIRED"
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
            code = "INBOX_AGENT_STOPPED_REVIEW_REQUIRED"
        print(canonical({"status": "STOPPED_ERROR", "code": code}), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
