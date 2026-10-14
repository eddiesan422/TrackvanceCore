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
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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


def require_codex():
    if os.getenv("TRACKVANCE_CERTIFICATION_PROJECT") != "trackvance-codex":
        raise RuntimeError("Este runner exige el entorno sintético trackvance-codex.")
    return "trackvance-codex"


def receiver(rows, port, db_path, *, serve_forever=True):
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
        db.execute("CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, hash TEXT NOT NULL, count INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, digest TEXT NOT NULL)")
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
                self.respond(200, {"records": [synthetic_row(index) for index in range(first, min(count + 1, first + size))]})
            elif parsed.path == "/receipt":
                with guard, sqlite3.connect(db_path) as db:
                    count = db.execute("SELECT count(*) FROM records").fetchone()[0]
                    batch_count = db.execute("SELECT count(*) FROM batches").fetchone()[0]
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
                    "batches": batch_count, "population_sha256": aggregate.hexdigest(),
                    "expected_population_sha256": expected.hexdigest(), "expected_rows_serialized_sha256": oracle,
                    "oracle": "EVERY_RECEIVED_ROW_EXACT_AND_ORDERED_ID_SHA256", "duplicate_ids": 0,
                    "receiver_seconds": time.monotonic() - started, "max_rss_kib": usage.ru_maxrss,
                    "cpu_seconds": usage.ru_utime + usage.ru_stime})
            elif parsed.path == "/health":
                self.respond(200, {"status": "READY", "rows": rows, "project": "trackvance-codex"})
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
                    prior = db.execute("SELECT hash,count FROM batches WHERE id=?", [identity]).fetchone()
                    if prior:
                        if prior != (digest, len(records)):
                            raise ValueError()
                    else:
                        db.executemany("INSERT INTO records VALUES (?,?)", verified)
                        db.execute("INSERT INTO batches VALUES (?,?,?)", [identity, digest, len(records)])
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


def certify(api_url, receiver_url, rows, evidence, timeout):
    project = require_codex()
    started = time.monotonic()
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
        options = {"url": receiver_url + "/bootstrap", "auth": "NONE", "collection": "records",
            "fields": [{"name": name, "logical_type": logical, "nullable": nullable} for name, logical, nullable in FIELDS],
            "pagination": {"mode": "PAGE"}, "page_size": 1000, "max_pages": 10000}
        connection = client.call("/connections", {"name": f"R090 synthetic source {nonce}", "source_type": "REST", "options": options}, expected=201)
        registered = client.call(f"/connections/{connection['id']}/acquisitions", {"name": f"R090 population {nonce}",
            "description": f"Synthetic-only REST oracle {nonce}", "schema_name": "REST", "object_name": "collection"}, expected=202, key=f"bootstrap-{nonce}")
        dataset_id = registered["dataset"]["id"]
        acquisition = wait_for(lambda: client.call(f"/acquisitions/{registered['acquisition']['id']}"),
            lambda result: result["status"] in TERMINAL, timeout=timeout, label="bootstrap-acquisition")
        assert acquisition["status"] == "SUCCESS", acquisition.get("error_code")
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
        refreshed = client.call(f"/datasets/{dataset_id}/acquisitions/refresh", {}, expected=202, key=f"million-{nonce}")
        acquisition = wait_for(lambda: client.call(f"/acquisitions/{refreshed['id']}"), lambda result: result["status"] in TERMINAL,
            timeout=timeout, label="full-rest-acquisition")
        assert acquisition["status"] == "SUCCESS", acquisition.get("error_code")
        assert acquisition["published_rows"] == rows
        source_id = acquisition["output_version_id"]
        stages["acquisition"] = {"id": acquisition["id"], "version_id": source_id, "rows": rows, "duration_seconds": acquisition["duration_seconds"], "result": "PASS"}
        def intake_occurrence():
            items = client.call(f"/intake/contracts/{contract['id']}/occurrences")["items"]
            return next((item for item in items if item["dataset_version_id"] == source_id), {})
        occurrence = wait_for(intake_occurrence, lambda result: result.get("status") in TERMINAL, timeout=timeout, label="automatic-intake")
        intake = client.call(f"/runs/{occurrence['run_id']}")
        assert intake["status"] == "SUCCESS" and intake["decision"] == "APPROVED" and intake["output_version_id"]
        stages["intake"] = {"run_id": intake["id"], "configuration_id": contract["id"], "schedule_id": schedule["id"],
            "output_version_id": intake["output_version_id"], "decision": intake["decision"], "result": "PASS"}
        def sentinel_run():
            runs = client.call("/runs")["items"]
            return next((run for run in runs if run["module"] == "sentinel" and run["dataset_version_id"] == source_id), {})
        monitor_run = wait_for(sentinel_run, lambda result: result.get("status") in TERMINAL, timeout=timeout, label="automatic-sentinel")
        assert monitor_run["status"] == "SUCCESS"
        visual = client.call(f"/runs/{monitor_run['id']}/visual-summary")
        assert visual["dataset_version_id"] == source_id and visual["scans"] == 0 and visual["indicators"]["row_count"] == rows
        expected_nulls = rows // 17
        optional = next(item for item in visual["columns"] if item["name"] == "optional")
        assert int(optional["null_count"]) == expected_nulls
        assert visual["indicators"]["duplicate_row_count"] == 0
        stages["sentinel"] = {"run_id": monitor_run["id"], "visual_version_id": visual["dataset_version_id"], "scans_when_opened": 0,
                              "optional_nulls": expected_nulls, "duplicate_rows": 0, "result": "PASS"}
        def delivery_occurrence():
            items = client.call(f"/delivery/automations/{automation['id']}/occurrences")["items"]
            return next((item for item in items if item["source_run_id"] == intake["id"]), {})
        chained = wait_for(delivery_occurrence, lambda result: result.get("status") in TERMINAL, timeout=timeout, label="chained-rest-delivery")
        delivery = client.call(f"/runs/{chained['run_id']}")
        assert delivery["status"] == "SUCCESS" and delivery["decision"] == "COMMITTED"
        assert chained["dataset_version_id"] == intake["output_version_id"]
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
        with urllib.request.urlopen(receiver_url + "/receipt", timeout=120) as response:
            receipt = json.load(response)
        assert receipt["received_rows"] == rows and receipt["population_sha256"] == receipt["expected_population_sha256"]
        assert receipt["batches"] == len(batches) and receipt["duplicate_ids"] == 0
        stages["delivery"] = {"run_id": delivery["id"], "automation_id": automation["id"], "rows": rows, "batch_count": len(batches),
                              "receiver": receipt, "result": "PASS"}
        # Full lineage metadata is bounded and must identify an unfinished
        # traversal honestly; recursively expand only returned pending branches.
        lineage = client.call(f"/catalog/datasets/{dataset_id}/lineage-graph?version_id={source_id}&max_nodes=500&max_depth=8")
        assert lineage["version_id"] == source_id
        node_ids = {node["resource_id"] for node in lineage["nodes"]}
        assert {source_id, intake["id"], intake["output_version_id"], delivery["id"], destination["destination_version_id"]} <= node_ids
        assert any(edge["state"] == "CONFIRMED" for edge in lineage["edges"])
        stages["lineage"] = {"nodes": len(lineage["nodes"]), "edges": len(lineage["edges"]),
                             "complete": lineage["complete"], "pending_branches": lineage["pending_branches"], "result": "PASS"}
        report["result"] = "PASS"
    except Exception as error:
        report["result"], report["error_type"] = "FAIL", type(error).__name__
        raise
    finally:
        report["duration_seconds"] = time.monotonic() - started
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["runner"] = {"identity": "rest_core090_cycle", "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
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
    args = parser.parse_args()
    if not 1 <= args.rows <= 1000000:
        raise ValueError("Las poblaciones del harness están acotadas a un millón; no cambia límites de producto.")
    if args.mode == "serve":
        receiver(args.rows, args.port, args.db)
    else:
        certify(args.api, args.receiver, args.rows, args.evidence, args.timeout)


if __name__ == "__main__":
    main()
