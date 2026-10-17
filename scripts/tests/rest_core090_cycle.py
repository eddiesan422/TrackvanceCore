"""Synthetic real REST -> Sentinel -> automatic Intake -> chained REST oracle.

The caller owns Docker orchestration. This script only runs a transient HTTP
receiver or calls the API of the preserved trackvance-codex environment. It never
starts services, deletes resources, copies installation data or prints secrets.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import resource
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

FIELDS = [("id", "STRING", False), ("ordinal", "INT64", False), ("amount", "DECIMAL", False),
          ("category", "STRING", False), ("optional", "STRING", True), ("observed_at", "TIMESTAMP", False)]
TERMINAL = {"SUCCESS", "FAILED", "FAILED_PRECONDITION", "PARTIAL", "UNKNOWN", "CANCELLED"}


def synthetic_row(ordinal):
    return {"id": f"{ordinal:09d}", "ordinal": ordinal,
        "amount": "100000000000000000000.0001" if ordinal % 100000 == 0 else f"{ordinal // 10000}.{ordinal % 10000:04d}",
        "category": f"category-{ordinal % 7}", "optional": None if ordinal % 17 == 0 else f"optional-{ordinal % 11}",
        "observed_at": "2026-10-14T09:30:00.123456-05:00"}


def canonical_row(row):
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def expected_digest(count):
    digest = hashlib.sha256()
    for index in range(1, count + 1):
        digest.update(canonical_row(synthetic_row(index)))
    return digest.hexdigest()


def batch_metadata_digest(batches):
    """Hash actual API evidence in ID order, independently from the receiver DB."""
    digest = hashlib.sha256()
    for batch in sorted(batches, key=lambda item: item["id"]):
        digest.update(canonical_row({key: batch[key] for key in ("id", "payload_sha256", "rows", "payload_bytes")}))
    return digest.hexdigest()


def source_request_proof(value, rows, page_size):
    """Independent durable HTTP sequence: every page accepted exactly once."""
    assert value["complete"] is True and value["transient_faults"] is True
    requests = [item for item in value["requests"] if item["endpoint"] == "/source"]
    pages = (rows + page_size - 1) // page_size
    expected = []
    for page in range(1, pages + 2):
        failures = (1 if page == 2 else 2 if page == 5 else 0) if page <= pages else 0
        expected.extend((page, 503) for _ in range(failures))
        expected.append((page, 200))
    assert [(item["page"], item["status"]) for item in requests] == expected
    assert all(item["page_size"] == page_size and item["at"] for item in requests)
    assert len({item["seq"] for item in requests}) == len(requests)
    return {"result": "PASS", "requests": requests, "successful_pages": pages + 1,
        "transient_failures": sum(status == 503 for _, status in expected),
        "same_page_retried_without_skip": True, "terminal_empty_page": pages + 1,
        "oracle": "DURABLE_RECEIVER_EXACT_PAGE_STATUS_AND_PARAMETERS"}


def receiver_json(base, path, *, timeout=120):
    with urllib.request.urlopen(base + path, timeout=timeout) as response:
        return json.load(response)


def version_snapshot(version, rows):
    """Persist exact metadata, without treating a sample as population proof."""
    assert version["row_count"] == rows and version["column_count"] == len(FIELDS)
    assert version["profile_status"] == "READY" and version["profile"]["row_count"] == rows
    assert [(column["name"], column["logical_type"]) for column in version["schema"]] == [
        (name, logical) for name, logical, _ in FIELDS]
    profiles = {column["name"]: column for column in version["profile"]["columns"]}
    assert profiles["optional"]["null_count"] == rows // 17
    assert profiles["id"]["distinct_count"] == rows and profiles["ordinal"]["distinct_count"] == rows
    assert all(profiles[name].get("parse_error_count", 0) == 0 for name, _, _ in FIELDS)
    canonical = next(artifact for artifact in version["artifacts"] if artifact["artifact_id"] == version["canonical_artifact_id"])
    assert len(canonical["sha256"]) == 64 and canonical["size_bytes"] > 0
    assert len(version["schema_hash"]) == 64
    return {key: version[key] for key in ("id", "dataset_id", "version", "source_type", "row_count", "column_count",
        "schema_hash", "sha256", "size_bytes", "profile_status", "schema", "parent_version_id", "source_run_id",
        "canonical_artifact_id")} | {"canonical_artifact": canonical,
        "profile_columns": [{key: column[key] for key in ("name", "logical_type", "null_count", "distinct_count", "parse_error_count")
                             if key in column} for column in version["profile"]["columns"]]}


def run_timing(run):
    started = datetime.fromisoformat(run["started_at"])
    finished = datetime.fromisoformat(run["finished_at"])
    seconds = (finished - started).total_seconds()
    assert seconds >= 0
    return {"created_at": run["created_at"], "started_at": run["started_at"], "finished_at": run["finished_at"],
            "execution_seconds": seconds}


def verify_comparison_report(path, rows, summary, before_id, after_id, keys):
    """Independent complete-value oracle; never import the product comparator."""
    seen = bytearray(rows + 1)
    records = 0
    with sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True) as db:
        metadata = {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM report_metadata")}
        assert metadata["summary"] == summary
        assert metadata["before_version_id"] == before_id and metadata["after_version_id"] == after_id
        assert metadata["settings"]["keys"] == keys
        for kind, multiplicity, before, after, changed in db.execute(
                "SELECT kind,multiplicity,before_json,after_json,changed_columns FROM details ORDER BY id"):
            assert kind == "UNCHANGED" and multiplicity == 1 and json.loads(changed) == []
            old, new = json.loads(before), json.loads(after)
            assert old == new and set(old) == {name for name, *_ in FIELDS}
            ordinal = old["ordinal"]
            assert type(ordinal) is int and 1 <= ordinal <= rows and not seen[ordinal]
            expected = synthetic_row(ordinal)
            for column in ("id", "category", "optional"):
                assert old[column] == expected[column]
            assert isinstance(old["amount"], str) and Decimal(old["amount"]) == Decimal(expected["amount"])
            assert datetime.fromisoformat(old["observed_at"]).astimezone(UTC) == datetime.fromisoformat(expected["observed_at"]).astimezone(UTC)
            seen[ordinal] = 1
            records += 1
    assert records == rows and sum(seen) == rows
    assert summary["counts"] == {"ADDED": 0, "ABSENT": 0, "MODIFIED": 0, "UNCHANGED": rows}
    return {"result": "PASS", "detail_records": records, "unique_ordinals": rows, "values_checked": rows * len(FIELDS) * 2,
            "oracle": "EVERY_PERSISTED_BEFORE_AFTER_VALUE_AND_COMPLETE_UNIQUE_ID_BITMAP",
            "bounded_bitmap_bytes": len(seen), "no_product_comparator_imports": True}


def require_codex():
    if os.getenv("TRACKVANCE_CERTIFICATION_PROJECT") != "trackvance-codex":
        raise RuntimeError("Este runner exige el entorno sintético trackvance-codex.")
    return "trackvance-codex"


def receiver(rows, port, db_path, *, serve_forever=True, transient_faults=False):
    require_codex()
    fixture_root = Path(os.getenv("TRACKVANCE_REST_FIXTURE_DIR", "/fixture")).resolve()
    database_path = Path(db_path).resolve()
    if not database_path.is_relative_to(fixture_root) or not database_path.name.startswith("rest-core090-"):
        raise RuntimeError("El receptor sólo escribe su DB rest-core090-* dentro de TRACKVANCE_REST_FIXTURE_DIR.")
    db_path = str(database_path)
    started = time.monotonic()
    guard = threading.Lock()
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, hash TEXT NOT NULL, count INTEGER NOT NULL, payload_bytes INTEGER NOT NULL)")
        if "payload_bytes" not in {column[1] for column in db.execute("PRAGMA table_info(batches)")}:
            raise RuntimeError("El receptor anterior no conserva bytes; usa un archivo rest-core090-* nuevo para la evidencia completa.")
        db.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, digest TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS source_requests (seq INTEGER PRIMARY KEY AUTOINCREMENT, endpoint TEXT NOT NULL, page INTEGER NOT NULL, page_size INTEGER NOT NULL, status INTEGER NOT NULL, at TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS source_faults (page INTEGER PRIMARY KEY, remaining INTEGER NOT NULL, configured INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS post_requests (seq INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL, at TEXT NOT NULL)")
        if transient_faults:
            db.executemany("INSERT OR IGNORE INTO source_faults VALUES (?,?,?)", [(2, 1, 1), (5, 2, 2)])
        db.execute("CREATE TABLE IF NOT EXISTS identity (rows INTEGER NOT NULL)")
        prior = db.execute("SELECT rows FROM identity").fetchone()
        if prior and prior[0] != rows:
            raise RuntimeError("La identidad del receptor no coincide con su población sintética.")
        if not prior:
            db.execute("INSERT INTO identity VALUES (?)", [rows])
    oracle = expected_digest(rows)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, value):
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path in {"/source", "/bootstrap"}:
                query = parse_qs(parsed.query)
                try:
                    page = int(query.get("page", ["1"])[0])
                    size = int(query.get("page_size", ["1000"])[0])
                    if page < 1 or not 1 <= size <= 10000:
                        raise ValueError()
                except ValueError:
                    self.respond(422, {"error": "INVALID_PAGINATION"})
                    return
                count = min(rows, 25) if parsed.path == "/bootstrap" else rows
                first = (page - 1) * size + 1
                with guard, sqlite3.connect(db_path) as db:
                    failure = db.execute("SELECT remaining FROM source_faults WHERE page=?", [page]).fetchone() if transient_faults and parsed.path == "/source" and first <= count else None
                    status = 503 if failure and failure[0] > 0 else 200
                    if status == 503:
                        db.execute("UPDATE source_faults SET remaining=remaining-1 WHERE page=?", [page])
                    db.execute("INSERT INTO source_requests(endpoint,page,page_size,status,at) VALUES (?,?,?,?,?)",
                        [parsed.path, page, size, status, datetime.now(UTC).isoformat()])
                if status == 503:
                    self.respond(503, {"error": "CONTROLLED_TRANSIENT_GET"})
                    return
                self.respond(200, {"records": [synthetic_row(index) for index in range(first, min(count + 1, first + size))]})
            elif parsed.path == "/source-receipt":
                try:
                    after = int(parse_qs(parsed.query).get("after_seq", [0])[0])
                    if after < 0:
                        raise ValueError()
                except ValueError:
                    self.respond(422, {"error": "INVALID_RECEIPT_CURSOR"})
                    return
                with guard, sqlite3.connect(db_path) as db:
                    requests = [{"seq": seq, "endpoint": endpoint, "page": page, "page_size": size, "status": status, "at": at}
                        for seq, endpoint, page, size, status, at in db.execute("SELECT seq,endpoint,page,page_size,status,at FROM source_requests WHERE seq>? ORDER BY seq LIMIT 5000", [after])]
                    last = db.execute("SELECT coalesce(max(seq),0) FROM source_requests").fetchone()[0]
                    faults = [{"page": page, "remaining": remaining, "configured": configured} for page, remaining, configured in db.execute("SELECT page,remaining,configured FROM source_faults ORDER BY page")]
                self.respond(200, {"requests": requests, "last_seq": last, "faults": faults, "transient_faults": transient_faults,
                    "complete": not requests or requests[-1]["seq"] == last})
            elif parsed.path == "/receipt":
                with guard, sqlite3.connect(db_path) as db:
                    count = db.execute("SELECT count(*) FROM records").fetchone()[0]
                    batch_count = db.execute("SELECT count(*) FROM batches").fetchone()[0]
                    post_calls = db.execute("SELECT count(*) FROM post_requests").fetchone()[0]
                    metadata_digest = hashlib.sha256()
                    confirmed_rows, payload_bytes = 0, 0
                    # Build from the receiver's durable data rather than from
                    # API evidence or the helper that hashes that evidence.
                    for identity, digest, batch_rows, batch_bytes in db.execute("SELECT id,hash,count,payload_bytes FROM batches ORDER BY id"):
                        encoded = json.dumps({"id": identity, "payload_sha256": digest, "rows": batch_rows, "payload_bytes": batch_bytes},
                                             ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                        metadata_digest.update(encoded)
                        confirmed_rows += batch_rows
                        payload_bytes += batch_bytes
                    # The receiver stores per-row digest and hashes the complete
                    # ordered population independently from source file hashes.
                    aggregate = hashlib.sha256()
                    for identity, digest in db.execute("SELECT id,digest FROM records ORDER BY id"):
                        aggregate.update(f"{identity}:{digest}\n".encode())
                    expected = hashlib.sha256()
                    for index in range(1, rows + 1):
                        row = synthetic_row(index)
                        expected.update(f"{row['id']}:{hashlib.sha256(canonical_row(row)).hexdigest()}\n".encode())
                usage = resource.getrusage(resource.RUSAGE_SELF)
                self.respond(200, {"project": "trackvance-codex", "expected_rows": rows, "received_rows": count,
                    "batches": batch_count, "post_calls": post_calls, "population_sha256": aggregate.hexdigest(),
                    "batch_metadata_sha256": metadata_digest.hexdigest(), "confirmed_rows": confirmed_rows,
                    "payload_bytes": payload_bytes, "batch_metadata_order": "ID_ASCENDING_UTF8_JSON_SORTED_KEYS_LF",
                    "expected_population_sha256": expected.hexdigest(), "expected_rows_serialized_sha256": oracle,
                    "oracle": "EVERY_RECEIVED_ROW_EXACT_AND_ORDERED_ID_SHA256", "duplicate_ids": 0,
                    "receiver_seconds": time.monotonic() - started, "max_rss_kib": usage.ru_maxrss,
                    "cpu_seconds": usage.ru_utime + usage.ru_stime})
            elif parsed.path == "/health":
                self.respond(200, {"status": "READY", "rows": rows, "project": "trackvance-codex", "transient_faults": transient_faults})
            else:
                self.respond(404, {"error": "NOT_FOUND"})

        def do_POST(self):
            if urlsplit(self.path).path != "/write":
                self.respond(404, {"error": "NOT_FOUND"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 1 <= size <= 8 * 1024**2:
                    raise ValueError()
                body = self.rfile.read(size)
                if len(body) != size:
                    raise ValueError()
                payload = json.loads(body)
                identity = payload["batch_id"]
                digest = hashlib.sha256(body).hexdigest()
                records = payload["records"]
                if self.headers.get("Idempotency-Key") != identity or self.headers.get("X-Trackvance-Payload-SHA256") != digest:
                    raise ValueError()
                if not isinstance(records, list) or not 1 <= len(records) <= 10000:
                    raise ValueError()
                verified = []
                for row in records:
                    index = row.get("ordinal")
                    if type(index) is not int or not 1 <= index <= rows or row != synthetic_row(index):
                        raise ValueError()
                    verified.append((row["id"], hashlib.sha256(canonical_row(row)).hexdigest()))
                with guard, sqlite3.connect(db_path) as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("INSERT INTO post_requests(batch_id,at) VALUES (?,?)", [identity, datetime.now(UTC).isoformat()])
                    prior = db.execute("SELECT hash,count,payload_bytes FROM batches WHERE id=?", [identity]).fetchone()
                    if prior:
                        if prior != (digest, len(records), size):
                            raise ValueError()
                    else:
                        db.executemany("INSERT INTO records VALUES (?,?)", verified)
                        db.execute("INSERT INTO batches VALUES (?,?,?,?)", [identity, digest, len(records), size])
                self.respond(200, {"accepted": True, "rows": len(records), "batch_id": identity, "payload_sha256": digest})
            except (ValueError, KeyError, TypeError, sqlite3.IntegrityError):
                # No received dataset values or credentials are returned/logged.
                self.respond(422, {"accepted": False, "rows": 0, "batch_id": locals().get("identity", ""),
                    "payload_sha256": locals().get("digest", ""), "error": "ORACLE_OR_IDEMPOTENCY_REJECTED"})

    print(json.dumps({"status": "READY", "project": "trackvance-codex", "rows": rows, "port": port}), flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    if serve_forever:
        server.serve_forever()
    return server


class Client:
    def __init__(self, base):
        self.base, self.csrf = base.rstrip("/"), ""
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, path, body=None, *, expected=200, method=None, key=None):
        headers = {"X-CSRF-Token": self.csrf}
        if key:
            headers["Idempotency-Key"] = key
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = urllib.request.Request(self.base + "/api/v1" + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=60) as response:
                status, value = response.status, json.load(response)
        except urllib.error.HTTPError as error:
            status = error.code
            # Parse errors for diagnosis, never print arbitrary upstream content.
            value = json.load(error)
        if status != expected:
            code = value.get("error", {}).get("code") if isinstance(value.get("error"), dict) else value.get("code")
            raise AssertionError(f"HTTP {status}, expected {expected}, endpoint {path.split('?')[0]}, code {code}.")
        if isinstance(value, dict) and "csrf_token" in value:
            self.csrf = value["csrf_token"]
        return value

    def download_digest(self, path):
        digest, size = hashlib.sha256(), 0
        with self.opener.open(self.base + "/api/v1" + path, timeout=120) as response:
            assert response.status == 200
            while block := response.read(65536):
                digest.update(block)
                size += len(block)
        assert size > 0
        return {"sha256": digest.hexdigest(), "bytes": size, "scope": "COMPLETE_PERSISTED_REPORT_STREAM"}

    def comparison_report(self, identity, rows, summary, before_id, after_id, keys):
        digest, size = hashlib.sha256(), 0
        with tempfile.TemporaryDirectory(prefix="core095-comparison-oracle-") as directory:
            path = Path(directory) / "report.sqlite"
            with self.opener.open(self.base + f"/api/v1/version-comparisons/{identity}/download", timeout=120) as response, path.open("xb") as target:
                assert response.status == 200
                while block := response.read(65536):
                    size += len(block)
                    assert size <= 2 * 1024**3
                    digest.update(block)
                    target.write(block)
            proof = verify_comparison_report(path, rows, summary, before_id, after_id, keys)
        return {"sha256": digest.hexdigest(), "bytes": size, "scope": "COMPLETE_PERSISTED_REPORT_STREAM", "oracle": proof}


def certify_update_comparison(client, receiver_url, dataset_id, source_id, connection_id, contract_id,
                              automation_id, actor_id, rows, nonce, timeout, original_receipt):
    """Use native pause/version/policy/compare APIs; never repeat external writes."""
    automation = client.call(f"/delivery/automations/{automation_id}")
    paused_delivery = client.call(f"/delivery/automations/{automation_id}/versions", {
        "configuration_id": automation["configuration_id"], "name": automation["name"], "enabled": False,
        "responsible_user_id": automation["responsible_user_id"], "settings": automation["settings"],
        "expected_version": automation["version"]}, expected=201)
    assert paused_delivery["enabled"] is False
    schedule = client.call(f"/intake/contracts/{contract_id}/schedule")
    paused_intake = client.call(f"/intake/contracts/{contract_id}/schedule", {"enabled": False,
        "responsible_user_id": schedule["responsible_user_id"], "settings": schedule["settings"], "expected_version": schedule["version"]})
    assert paused_intake["enabled"] is False
    existing = client.call(f"/datasets/{dataset_id}/source-settings")
    body = {"expected_version": existing["version"], "connection_id": connection_id, "schema_name": "REST",
        "object_name": "collection", "column_overrides": {}, "responsible_user_id": actor_id,
        "interval_seconds": 86400, "timezone": "America/Bogota", "enabled": False,
        "publication_policy": "ON_CHANGE", "empty_policy": "BLOCK"}
    settings = client.call(f"/datasets/{dataset_id}/source-settings", body, method="PUT")
    versions_before = client.call(f"/datasets/{dataset_id}")["versions"]
    run = client.call(f"/datasets/{dataset_id}/acquisitions/refresh", {}, expected=202, key=f"unchanged-{nonce}")
    unchanged = wait_for(lambda: client.call(f"/acquisitions/{run['id']}"), lambda result: result["status"] in TERMINAL,
        timeout=timeout, label="same-full-on-change")
    assert unchanged["status"] == "SUCCESS" and unchanged["stage"] == "UNCHANGED" and unchanged["output_version_id"] is None
    assert unchanged["total_rows"] == rows and len(client.call(f"/datasets/{dataset_id}")["versions"]) == len(versions_before)
    body.update(expected_version=settings["version"], publication_policy="EVERY_CUT")
    client.call(f"/datasets/{dataset_id}/source-settings", body, method="PUT")
    run = client.call(f"/datasets/{dataset_id}/acquisitions/refresh", {}, expected=202, key=f"same-cut-{nonce}")
    repeated = wait_for(lambda: client.call(f"/acquisitions/{run['id']}"), lambda result: result["status"] in TERMINAL,
        timeout=timeout, label="same-full-every-cut")
    assert repeated["status"] == "SUCCESS" and repeated["output_version_id"] and repeated["output_version_id"] != source_id
    assert repeated["published_rows"] == rows
    versions = client.call(f"/datasets/{dataset_id}")["versions"]
    assert len(versions) == len(versions_before) + 1
    second_version = next(item for item in versions if item["id"] == repeated["output_version_id"])
    second_snapshot = version_snapshot(second_version, rows)
    queued = client.call(f"/datasets/{dataset_id}/comparisons", {"before_version_id": source_id,
        "after_version_id": repeated["output_version_id"], "keys": ["id"]}, expected=202)
    compared = wait_for(lambda: client.call(f"/version-comparisons/{queued['id']}"), lambda result: result["status"] in TERMINAL,
        timeout=timeout, label="full-snapshot-comparison")
    assert compared["status"] == "SUCCESS", compared.get("error_code")
    summary = compared["summary"]
    assert summary["complete_population"] is True and summary["mode"] == "KEYED"
    assert summary["before_rows"] == summary["after_rows"] == rows
    assert summary["counts"] == {"ADDED": 0, "ABSENT": 0, "MODIFIED": 0, "UNCHANGED": rows}
    assert summary["schema_changes"] == []
    details = client.call(f"/version-comparisons/{queued['id']}/rows?kind=UNCHANGED&offset=0&limit=1")
    assert details["records"] == rows and details["total"] == rows and len(details["items"]) == 1
    first = details["items"][0]
    assert first["before"] == first["after"] and first["before"]["id"] == "000000001" and first["changed_columns"] == []
    report = client.comparison_report(queued["id"], rows, summary, source_id, repeated["output_version_id"], ["id"])
    fallback_reports = []
    for keys in (["category"], []):
        registered = client.call(f"/datasets/{dataset_id}/comparisons", {"before_version_id": source_id,
            "after_version_id": repeated["output_version_id"], "keys": keys}, expected=202)
        result = wait_for(lambda: client.call(f"/version-comparisons/{registered['id']}"), lambda item: item["status"] in TERMINAL,
            timeout=timeout, label="full-ambiguous-key-comparison" if keys else "full-no-key-comparison")
        assert result["status"] == "SUCCESS" and result["summary"]["mode"] == "MULTISET"
        assert result["summary"]["complete_population"] is True
        assert result["summary"]["before_rows"] == result["summary"]["after_rows"] == rows
        if keys:
            assert any("AMBIGUOUS_KEYS" in warning for warning in result["summary"]["warnings"])
        fallback_reports.append({"id": result["id"], "artifact_id": result["artifact_id"], "summary": result["summary"],
            "report": client.comparison_report(result["id"], rows, result["summary"], source_id, repeated["output_version_id"], keys)})
    final_receipt = receiver_json(receiver_url, "/receipt")
    for key in ("received_rows", "post_calls", "batches", "population_sha256", "batch_metadata_sha256", "confirmed_rows", "payload_bytes"):
        assert final_receipt[key] == original_receipt[key]
    return {"result": "PASS", "population": rows, "paused_intake_version": paused_intake["version"],
        "paused_delivery_version": paused_delivery["version"], "on_change": {"id": unchanged["id"], "stage": unchanged["stage"],
        "duration_seconds": unchanged["duration_seconds"], "output_version_id": None},
        "every_cut": {"id": repeated["id"], "duration_seconds": repeated["duration_seconds"], "version": second_snapshot},
        "comparison": {"id": compared["id"], "artifact_id": compared["artifact_id"], "summary": summary, "report": report},
        "fallback_comparisons": fallback_reports,
        "no_additional_delivery_posts": True, "receiver_post_calls": final_receipt["post_calls"]}


def wait_for(callback, predicate, *, timeout, label):
    deadline, last_status = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        value = callback()
        if predicate(value):
            return value
        status = value.get("status") if isinstance(value, dict) else None
        if status != last_status:
            print(json.dumps({"stage": label, "status": status, "elapsed_wait_seconds": round(timeout - (deadline - time.monotonic()), 1)}), flush=True)
            last_status = status
        time.sleep(2)
    raise AssertionError(f"Timeout real esperando {label}.")


def lineage_proof(client, source_dataset_id, source_version_id, output_dataset_id, output_version_id,
                  intake_run_id, delivery_run_id, destination_version_id):
    """Verify actual core relationships in bounded, authorized selected-version views.

    The API has no branch cursor. Selecting the approved output is a second
    supported graph root; an unfinished technical traversal remains unfinished.
    """
    source_key, output_key = f"DATASET_VERSION:{source_version_id}", f"DATASET_VERSION:{output_version_id}"
    intake_key, delivery_key = f"RUN:{intake_run_id}", f"RUN:{delivery_run_id}"
    destination_key = f"DELIVERY_DESTINATION_VERSION:{destination_version_id}"
    critical = {source_key, output_key, intake_key, delivery_key, destination_key}
    relationships = [
        ("RUN_INPUT", intake_key, source_key, source_key, intake_key),
        ("RUN_OUTPUT", intake_key, output_key, intake_key, output_key),
        ("DERIVED_FROM", output_key, source_key, source_key, output_key),
        ("DELIVERY_INPUT", output_key, delivery_key, output_key, delivery_key),
        ("DELIVERED_TO", delivery_key, destination_key, delivery_key, destination_key),
    ]
    nodes, edges, pending, views = {}, {}, set(), []

    def observed_core():
        found = []
        for relation, stored_source, stored_target, displayed_source, displayed_target in relationships:
            edge = next((item for item in edges.values() if item["stored_relation"] == relation
                         and item["stored_source"] == stored_source and item["stored_target"] == stored_target
                         and item["source"] == displayed_source and item["target"] == displayed_target
                         and item["state"] == ("CONFIRMED" if relation == "DELIVERED_TO" else "RECORDED")), None)
            if edge:
                found.append({key: edge[key] for key in ("id", "stored_relation", "stored_source", "stored_target", "source", "target", "state")})
        return found

    for role, dataset_id, version_id in [("SOURCE", source_dataset_id, source_version_id),
                                         ("APPROVED_OUTPUT", output_dataset_id, output_version_id)]:
        graph = client.call(f"/catalog/datasets/{dataset_id}/lineage-graph?version_id={version_id}&max_nodes=500&max_depth=8")
        assert graph["root_id"] == f"DATASET_VERSION:{version_id}" and graph["version_id"] == version_id
        assert graph["limits"] == {"max_depth": 8, "max_nodes": 500}
        assert type(graph["complete"]) is bool and graph["complete"] == (not graph["pending_branches"])
        identities = {node["id"] for node in graph["nodes"]}
        assert len(identities) == len(graph["nodes"]) <= 500
        root = next(node for node in graph["nodes"] if node["id"] == graph["root_id"])
        assert root["type"] == "DATASET_VERSION" and root["resource_id"] == version_id and root["dataset_id"] == dataset_id
        assert set(graph["pending_branches"]) <= identities
        assert len({edge["id"] for edge in graph["edges"]}) == len(graph["edges"])
        assert all(edge["source"] in identities and edge["target"] in identities for edge in graph["edges"])
        for node in graph["nodes"]:
            assert node["id"] == f"{node['type']}:{node['resource_id']}"
            nodes[node["id"]] = node
        for edge in graph["edges"]:
            edges[edge["id"]] = edge
        pending.update(graph["pending_branches"])
        graph_hash = hashlib.sha256(json.dumps(graph, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        views.append({"role": role, "dataset_id": dataset_id, "root_id": graph["root_id"], "version_id": version_id,
                      "nodes": len(graph["nodes"]), "edges": len(graph["edges"]), "complete": graph["complete"],
                      "pending_branches": graph["pending_branches"], "limits": graph["limits"], "metadata_sha256": graph_hash})
        if critical <= nodes.keys() and len(observed_core()) == len(relationships):
            break
    core = observed_core()
    core_complete = critical <= nodes.keys() and len(core) == len(relationships)
    if core_complete:
        assert nodes[intake_key]["status"] == "SUCCESS" and nodes[intake_key]["decision"] == "APPROVED"
        assert nodes[delivery_key]["status"] == "SUCCESS" and nodes[delivery_key]["decision"] == "COMMITTED"
        assert nodes[intake_key]["module"] == "intake" and nodes[delivery_key]["module"] == "DELIVERY"
        assert nodes[source_key]["dataset_id"] == source_dataset_id and nodes[output_key]["dataset_id"] == output_dataset_id
    metadata = {"nodes": [nodes[key] for key in sorted(nodes)], "edges": [edges[key] for key in sorted(edges)]}
    return {"proof_version": 2, "verification_scope": "CORE_REGISTERED_CHAIN_WITH_BOUNDED_VIEWS",
            "nodes": len(nodes), "edges": len(edges), "complete": all(view["complete"] for view in views),
            "pending_branches": sorted(pending), "views": views, "view_count": len(views),
            "critical_node_ids": sorted(critical), "missing_critical_node_ids": sorted(critical - nodes.keys()),
            "core_edges": core, "core_edge_count": len(core), "core_links_complete": core_complete,
            "metadata_sha256": hashlib.sha256(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "core_edges_sha256": hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "result": "PASS" if core_complete else "FAIL"}


def business_lineage_proof(client, dataset_id, version_id, output_version_id,
                           intake_run_id, delivery_run_id, destination_version_id, expected_batches):
    """Follow the same source-root business pages exposed by the interface.

    The technical legacy oracle above stays independent. No second dataset root,
    larger global limit, local artifact filtering or invented edge can complete
    this business path. Branch pages may be merged only through server cursors.
    A browser check is a separate required receipt, never inferred from this API.
    """
    root = f"DATASET_VERSION:{version_id}"
    output, intake = f"DATASET_VERSION:{output_version_id}", f"RUN:{intake_run_id}"
    delivery, destination = f"RUN:{delivery_run_id}", f"DELIVERY_DESTINATION_VERSION:{destination_version_id}"
    required = [(root, intake, "RUN_INPUT"), (intake, output, "RUN_OUTPUT"),
                (root, output, "DERIVED_FROM"), (output, delivery, "DELIVERY_INPUT"),
                (delivery, destination, "DELIVERED_TO")]
    critical = {root, output, intake, delivery, destination}
    nodes, edges, views, seen_cursors = {}, {}, [], set()
    queue = [None]
    def complete():
        return critical <= nodes.keys() and all(any(
            edge["source"] == source and edge["target"] == target
            and relation in edge["stored_relations"]
            and edge["state"] == ("CONFIRMED" if relation == "DELIVERED_TO" else "RECORDED")
            for edge in edges.values()) for source, target, relation in required)
    while queue and len(views) < 16:
        cursor = queue.pop(0)
        if cursor in seen_cursors:
            continue
        seen_cursors.add(cursor)
        query = {"projection": "business", "version_id": version_id, "max_nodes": 100, "max_depth": 4}
        if cursor:
            query["cursor"] = cursor
        graph = client.call(f"/catalog/datasets/{dataset_id}/lineage-graph?" + urlencode(query))
        assert graph["projection"] == "business" and graph["root_id"] == root and graph["version_id"] == version_id
        assert graph["limits"] == {"max_nodes": 100, "max_depth": 4, "adjacency_page_size": 80, "max_path": 64}
        identities = {node["id"] for node in graph["nodes"]}
        assert len(identities) == len(graph["nodes"]) <= 100
        assert graph["anchor_id"] in identities
        assert all(node["type"] != "ARTIFACT" and node["id"] == f"{node['type']}:{node['resource_id']}"
                   for node in graph["nodes"])
        assert all(edge["source"] in identities and edge["target"] in identities
                   and edge["evidence_count"] == len(edge["evidence_ids"]) > 0 for edge in graph["edges"])
        assert graph["complete"] == (not graph["pending_branches"])
        for node in graph["nodes"]:
            nodes[node["id"]] = node
        for edge in graph["edges"]:
            edges[edge["id"]] = edge
        views.append({"root_id": graph["root_id"], "anchor_id": graph["anchor_id"], "nodes": len(identities),
                      "edges": len(graph["edges"]), "complete": graph["complete"],
                      "metadata_relationships_scanned": graph["metadata_relationships_scanned"],
                      "metadata_sha256": hashlib.sha256(json.dumps(graph, sort_keys=True, separators=(",", ":")).encode()).hexdigest()})
        if complete():
            break
        queue.extend(branch["cursor"] for branch in graph["branches"] if branch["direction"] in {"both", "downstream"})
    assert complete(), "The interface's source-root business continuation must reach the actual confirmed destination"
    assert nodes[intake]["status"] == "SUCCESS" and nodes[intake]["decision"] == "APPROVED"
    assert nodes[delivery]["status"] == "SUCCESS" and nodes[delivery]["decision"] == "COMMITTED"
    groups = nodes[delivery]["technical_groups"]
    payloads = next(group["relationship_count"] for group in groups if group["relation"] == "REST_BATCH_PAYLOAD")
    assert payloads == expected_batches
    return {"result": "PASS", "proof_version": 1, "projection": "business", "single_source_root": True,
            "view_count": len(views), "views": views, "core_links_complete": True,
            "business_nodes": len(nodes), "artifact_nodes": 0, "grouped_batch_payloads": payloads,
            "limits_per_page": {"max_nodes": 100, "max_depth": 4}, "browser_review": "SEPARATE_REQUIRED_EVIDENCE"}


def certify(api_url, receiver_url, rows, evidence, timeout, *, core095=False):
    project = require_codex()
    started = time.monotonic()
    cpu_started = resource.getrusage(resource.RUSAGE_SELF)
    nonce = uuid.uuid4().hex[:12]
    evidence_path = Path(evidence).resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    if evidence_path.exists():
        raise RuntimeError("El archivo de evidencia ya existe; conserva el histórico y usa otro nombre.")
    report = {"profile": "deep" if rows == 1000000 else "functional-small", "project": project,
              "rows": rows, "nonce": nonce, "started_at": datetime.now(UTC).isoformat(), "result": "RUNNING", "checks": {}}
    stages = report["checks"]
    try:
        client = Client(api_url)
        client.call("/auth/login", {"email": os.environ["TRACKVANCE_ADMIN_EMAIL"], "password": os.environ["TRACKVANCE_ADMIN_PASSWORD"]})
        actor = client.call("/me")["user"]
        with urllib.request.urlopen(receiver_url + "/health", timeout=30) as response:
            health = json.load(response)
        assert health["project"] == project and health["rows"] == rows
        if core095:
            assert health["transient_faults"] is True
        options = {"url": receiver_url + "/bootstrap", "auth": "NONE", "collection": "records",
            "fields": [{"name": name, "logical_type": logical, "nullable": nullable} for name, logical, nullable in FIELDS],
            "pagination": {"mode": "PAGE"}, "page_size": 1000, "max_pages": 10000}
        if core095:
            options.update(retries=3, retry_wait_seconds=1)
        connection = client.call("/connections", {"name": f"R090 synthetic source {nonce}", "source_type": "REST", "options": options}, expected=201)
        registered = client.call(f"/connections/{connection['id']}/acquisitions", {"name": f"R090 population {nonce}",
            "description": f"Synthetic-only REST oracle {nonce}", "schema_name": "REST", "object_name": "collection"}, expected=202, key=f"bootstrap-{nonce}")
        assert registered["dataset"] is None and registered["acquisition"]["dataset_id"] is None
        acquisition = wait_for(lambda: client.call(f"/acquisitions/{registered['acquisition']['id']}"),
            lambda result: result["status"] in TERMINAL, timeout=timeout, label="bootstrap-acquisition")
        assert acquisition["status"] == "SUCCESS", acquisition.get("error_code")
        dataset_id = acquisition["dataset_id"]
        bootstrap_version = acquisition["output_version_id"]
        contract = client.call("/intake/contracts", {"name": f"R090 Intake automatic {nonce}", "dataset_id": dataset_id,
            "config": {"required_columns": [name for name, _, nullable in FIELDS if not nullable], "max_error_rate": 0}}, expected=201)
        schedule = client.call(f"/intake/contracts/{contract['id']}/schedule", {"enabled": True, "responsible_user_id": actor["id"],
            "settings": {"mode": "EVENT", "timezone": "America/Bogota", "starts_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "requested_engine": "AUTO"}})
        destination = client.call("/delivery/destinations", {"name": f"R090 oracle destination {nonce}", "sink_type": "REST",
            "options": {"url": receiver_url + "/write", "auth": "NONE", "idempotency_supported": True}}, expected=201)
        draft = {"dataset_version_id": bootstrap_version, "destination_id": destination["id"], "destination_version_id": destination["destination_version_id"],
            "target": {"mode": "REST", "batch_rows": 1000, "confirmation_path": "accepted", "confirmation_value": True,
                       "rows_path": "rows", "batch_id_path": "batch_id", "payload_hash_path": "payload_sha256"},
            "columns": [{"source_name": name, "target_name": name, "target_type": logical, "ordinal": index, "nullable": nullable,
                         **({"precision": 38, "scale": 4} if logical == "DECIMAL" else {})} for index, (name, logical, nullable) in enumerate(FIELDS)]}
        assert client.call("/delivery/preview", draft)["sampled_rows"] > 0
        assert client.call("/delivery/preflight", draft)["status"] == "PASS"
        publication = client.call("/delivery/configurations", {"name": f"R090 chained REST {nonce}", **draft}, expected=201)
        automation = client.call("/delivery/automations", {"name": f"R090 automatic delivery {nonce}", "configuration_id": publication["id"],
            "settings": {"mode": "CHAINED", "timezone": "America/Bogota", "starts_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                         "source_policy": "INTAKE_OUTPUT", "intake_configuration_id": contract["id"]}}, expected=201)
        options["url"] = receiver_url + "/source"
        client.call(f"/connections/{connection['id']}", {"version": connection["version"], "options": options}, method="PATCH")
        source_start = receiver_json(receiver_url, "/source-receipt")["last_seq"] if core095 else None
        refreshed = client.call(f"/datasets/{dataset_id}/acquisitions/refresh", {}, expected=202, key=f"million-{nonce}")
        acquisition = wait_for(lambda: client.call(f"/acquisitions/{refreshed['id']}"), lambda result: result["status"] in TERMINAL,
            timeout=timeout, label="full-rest-acquisition")
        assert acquisition["status"] == "SUCCESS", acquisition.get("error_code")
        assert acquisition["published_rows"] == rows
        if core095:
            stages["source_retries"] = source_request_proof(receiver_json(receiver_url, f"/source-receipt?after_seq={source_start}"), rows, 1000)
        source_id = acquisition["output_version_id"]
        source_version = next(version for version in client.call(f"/datasets/{dataset_id}")["versions"] if version["id"] == source_id)
        assert source_version["source_type"] == "REST"
        stages["acquisition"] = {"id": acquisition["id"], "version_id": source_id, "rows": rows,
            "duration_seconds": acquisition["duration_seconds"], "version": version_snapshot(source_version, rows), "result": "PASS"}
        def intake_occurrence():
            items = client.call(f"/intake/contracts/{contract['id']}/occurrences")["items"]
            return next((item for item in items if item["dataset_version_id"] == source_id), {})
        occurrence = wait_for(intake_occurrence, lambda result: result.get("status") in TERMINAL, timeout=timeout, label="automatic-intake")
        intake = client.call(f"/runs/{occurrence['run_id']}")
        assert intake["status"] == "SUCCESS" and intake["decision"] == "APPROVED" and intake["output_version_id"]
        intake_manifest = client.call(f"/runs/{intake['id']}/evidence")
        output_identity = next(output for output in intake_manifest["outputs"] if output["dataset_version_id"] == intake["output_version_id"])
        output_version = next(version for version in client.call(f"/datasets/{output_identity['dataset_id']}")["versions"]
                              if version["id"] == intake["output_version_id"])
        assert output_version["source_type"] == "INTAKE_OUTPUT" and output_version["parent_version_id"] == source_id
        assert output_version["source_run_id"] == intake["id"]
        output_snapshot = version_snapshot(output_version, rows)
        assert output_snapshot["canonical_artifact"]["sha256"] == output_identity["canonical_sha256"]
        stages["intake"] = {"run_id": intake["id"], "configuration_id": contract["id"], "schedule_id": schedule["id"],
            "output_version_id": intake["output_version_id"], "decision": intake["decision"], "timing": run_timing(intake),
            "version": output_snapshot, "result": "PASS"}
        def sentinel_run():
            runs = client.call("/runs")["items"]
            return next((run for run in runs if run["module"] == "sentinel" and run["dataset_version_id"] == source_id), {})
        monitor_run = wait_for(sentinel_run, lambda result: result.get("status") in TERMINAL, timeout=timeout, label="automatic-sentinel")
        assert monitor_run["status"] == "SUCCESS"
        monitor_run = client.call(f"/runs/{monitor_run['id']}")
        visual = client.call(f"/runs/{monitor_run['id']}/visual-summary")
        assert visual["dataset_version_id"] == source_id and visual["scans"] == 0 and visual["indicators"]["row_count"] == rows
        expected_nulls = rows // 17
        optional = next(item for item in visual["columns"] if item["name"] == "optional")
        assert int(optional["null_count"]) == expected_nulls
        assert visual["indicators"]["duplicate_row_count"] == 0
        stages["sentinel"] = {"run_id": monitor_run["id"], "visual_version_id": visual["dataset_version_id"], "scans_when_opened": 0,
                              "optional_nulls": expected_nulls, "duplicate_rows": 0, "timing": run_timing(monitor_run), "result": "PASS"}
        def delivery_occurrence():
            items = client.call(f"/delivery/automations/{automation['id']}/occurrences")["items"]
            return next((item for item in items if item["source_run_id"] == intake["id"]), {})
        chained = wait_for(delivery_occurrence, lambda result: result.get("status") in TERMINAL, timeout=timeout, label="chained-rest-delivery")
        delivery = client.call(f"/runs/{chained['run_id']}")
        assert delivery["status"] == "SUCCESS" and delivery["decision"] == "COMMITTED"
        assert chained["dataset_version_id"] == delivery["dataset_version_id"] == intake["output_version_id"]
        batches = []
        while True:
            page = client.call(f"/delivery/runs/{delivery['id']}/rest-batches?limit=200&offset={len(batches)}")
            batches.extend(page["items"])
            if len(batches) == page["total"]:
                break
            assert page["items"], "La paginacion de evidencia no puede truncar la poblacion de lotes."
        assert sum(batch["rows"] for batch in batches) == rows
        assert all(batch["status"] == "COMMITTED" and batch["attempts"] == 1 for batch in batches)
        assert len({batch["id"] for batch in batches}) == len(batches)
        assert all(batch["run_id"] == delivery["id"] and batch["confirmed_payload_bytes"] == batch["payload_bytes"]
                   and len(batch["payload_sha256"]) == 64 for batch in batches)
        batch_digest = batch_metadata_digest(batches)
        payload_bytes = sum(batch["payload_bytes"] for batch in batches)
        with urllib.request.urlopen(receiver_url + "/receipt", timeout=120) as response:
            receipt = json.load(response)
        assert receipt["received_rows"] == rows and receipt["population_sha256"] == receipt["expected_population_sha256"]
        assert receipt["batches"] == len(batches) and receipt["duplicate_ids"] == 0
        assert receipt["batch_metadata_sha256"] == batch_digest
        assert receipt["confirmed_rows"] == rows and receipt["payload_bytes"] == payload_bytes
        if core095:
            assert receipt["post_calls"] == len(batches)
        stages["delivery"] = {"run_id": delivery["id"], "automation_id": automation["id"], "rows": rows, "batch_count": len(batches),
            "destination_version_id": destination["destination_version_id"],
            "batch_metadata_sha256": batch_digest, "batch_metadata_order": receipt["batch_metadata_order"],
            "payload_bytes": payload_bytes, "all_attempts": 1, "all_statuses": "COMMITTED", "timing": run_timing(delivery),
            "receiver": receipt, "result": "PASS"}
        stages["lineage"] = lineage_proof(client, dataset_id, source_id, output_identity["dataset_id"],
                                          intake["output_version_id"], intake["id"], delivery["id"],
                                          destination["destination_version_id"])
        assert stages["lineage"]["result"] == "PASS"
        stages["business_lineage"] = business_lineage_proof(client, dataset_id, source_id,
            intake["output_version_id"], intake["id"], delivery["id"], destination["destination_version_id"], len(batches))
        if core095:
            stages["update_and_comparison"] = certify_update_comparison(client, receiver_url, dataset_id, source_id,
                connection["id"], contract["id"], automation["id"], actor["id"], rows, nonce, timeout, receipt)
        report["result"] = "PASS"
    except Exception as error:
        report["result"], report["error_type"] = "FAIL", type(error).__name__
        raise
    finally:
        report["duration_seconds"] = time.monotonic() - started
        report["finished_at"] = datetime.now(UTC).isoformat()
        usage = resource.getrusage(resource.RUSAGE_SELF)
        report["runner"] = {"identity": "rest_core090_cycle", "max_rss_kib": usage.ru_maxrss,
            "cpu_seconds": usage.ru_utime + usage.ru_stime - cpu_started.ru_utime - cpu_started.ru_stime}
        evidence_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"result": report["result"], "rows": rows, "duration_seconds": report["duration_seconds"], "evidence": str(evidence_path)}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["serve", "certify"])
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument("--port", type=int, default=18090)
    parser.add_argument("--db", default="/fixture/rest-core090-receiver.sqlite")
    parser.add_argument("--api", default="http://api:8000")
    parser.add_argument("--receiver", default="http://rest-fixture:18090")
    parser.add_argument("--evidence", default="/evidence/rest-core090.json")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--transient-faults", action="store_true", help="Inject only source GET failures on pages 2 and 5.")
    parser.add_argument("--core095", action="store_true", help="Require retry, no-change, every-cut and full comparison evidence.")
    args = parser.parse_args()
    if not 1 <= args.rows <= 1000000:
        raise ValueError("Las poblaciones del harness están acotadas a un millón; no cambia límites de producto.")
    if args.mode == "serve":
        receiver(args.rows, args.port, args.db, transient_faults=args.transient_faults)
    else:
        certify(args.api, args.receiver, args.rows, args.evidence, args.timeout, core095=args.core095)


if __name__ == "__main__":
    main()
