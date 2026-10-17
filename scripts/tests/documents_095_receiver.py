"""Small synthetic document Delivery oracle. Caller owns isolated orchestration.

Requires TRACKVANCE_CERTIFICATION_PROJECT=trackvance-codex. No business source,
credentials, Docker management or automatic replay. A test registers its known
fixture oracle before publication; the receiver verifies every delivered row.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def receipt(oracle, rows, project="trackvance-codex"):
    headers = [row for structure, row in rows if structure == "headers"]
    details = [row for structure, row in rows if structure == "details"]
    observed = Counter((row["invoice"], row["total"], row["date"]) for row in headers)
    expected = Counter((row["invoice"], row["total"], row["date"]) for row in oracle)
    identities = [row["document_id"] for row in headers]
    detail_counts = Counter((row["document_id"], row["sku"], row["amount"]) for row in details)
    expected_details = Counter((identity, sku, amount) for identity in identities for sku, amount in [("SKU1", "10.25"), ("SKU2", "20.50")])
    complete = observed == expected and len(set(identities)) == len(identities) and detail_counts == expected_details
    return {"status": "PASS" if complete else "INCOMPLETE", "project": project,
            "header_rows": len(headers), "detail_rows": len(details), "expected_documents": len(oracle),
            "all_values_exact": complete, "header_detail_identity_consistent": complete,
            "received_sha256": hashlib.sha256(canonical(sorted(rows, key=lambda row: canonical(row)))).hexdigest(),
            "oracle_sha256": hashlib.sha256(canonical(oracle)).hexdigest()}


def acquisition_records():
    """Exact small REST proposal/reader oracle; decimal 1.0 is syntactically decimal."""
    records = [{"id": f"{index:06d}", "quantity": index, "amount": 1.0, "flag": True,
        "day": "2026-10-16", "at": "2026-10-16T08:00:00.123456-05:00",
        "company": {"name": "Synthetic Ω"}, "tags": ["not-expanded"]} for index in range(1, 4)]
    records[-1]["later"] = "last-only"
    return records


def serve(port, database, *, serve_forever=True):
    project = os.getenv("TRACKVANCE_CERTIFICATION_PROJECT", "")
    if project != "trackvance-codex" and not re.fullmatch(r"trackvance-v070-test-e2e-[a-z0-9-]*[a-f0-9]{12}", project):
        raise RuntimeError("El receptor documental exige trackvance-codex o un proyecto E2E desechable comprobado.")
    folder = Path(os.getenv("TRACKVANCE_DOCUMENT_FIXTURE_DIR", "/tmp/documents-095-fixture")).resolve()
    target = Path(database).resolve()
    if not target.is_relative_to(folder) or not re.fullmatch(r"documents-095-[a-f0-9]{12}\.sqlite", target.name):
        raise RuntimeError("La DB debe ser una identidad documents-095-{nonce12}.sqlite propia del fixture.")
    folder.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target) as db:
        db.execute("CREATE TABLE IF NOT EXISTS oracles(nonce TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, nonce TEXT NOT NULL, structure TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS delivery_calls(seq INTEGER PRIMARY KEY AUTOINCREMENT, nonce TEXT NOT NULL, structure TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS acquisition_fixtures(nonce TEXT PRIMARY KEY, failures INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS acquisition_calls(seq INTEGER PRIMARY KEY AUTOINCREMENT, nonce TEXT NOT NULL, page INTEGER NOT NULL, page_size INTEGER NOT NULL, status INTEGER NOT NULL)")
    guard = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, value):
            body = canonical(value)
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
            path = urlsplit(self.path).path
            if path == "/health":
                self.respond(200, {"project": project, "fixture": "DOCUMENT_ORACLE_095", "status": "READY"})
                return
            acquisition = re.fullmatch(r"/acquisition/([a-f0-9]{12})/(source|receipt)", path)
            if acquisition:
                nonce, action = acquisition.groups()
                with guard, sqlite3.connect(target) as db:
                    if action == "receipt":
                        calls = [{"page": page, "page_size": size, "status": status} for page, size, status in db.execute(
                            "SELECT page,page_size,status FROM acquisition_calls WHERE nonce=? ORDER BY seq", [nonce])]
                        failure = db.execute("SELECT failures FROM acquisition_fixtures WHERE nonce=?", [nonce]).fetchone()
                        self.respond(200, {"project": project, "expected_rows": 3, "calls": calls, "failures_remaining": failure[0] if failure else None})
                        return
                    try:
                        query = parse_qs(urlsplit(self.path).query)
                        page, size = int(query.get("page", [1])[0]), int(query.get("page_size", [100])[0])
                        if page < 1 or not 1 <= size <= 100:
                            raise ValueError()
                    except (ValueError, TypeError):
                        self.respond(422, {"code": "INVALID_SYNTHETIC_PAGINATION"})
                        return
                    remaining = db.execute("SELECT failures FROM acquisition_fixtures WHERE nonce=?", [nonce]).fetchone()
                    status = 503 if page == 2 and remaining and remaining[0] > 0 else 200
                    if status == 503:
                        db.execute("UPDATE acquisition_fixtures SET failures=failures-1 WHERE nonce=?", [nonce])
                    db.execute("INSERT INTO acquisition_calls(nonce,page,page_size,status) VALUES (?,?,?,?)", [nonce, page, size, status])
                if status == 503:
                    self.respond(503, {"code": "CONTROLLED_SOURCE_TRANSIENT"})
                else:
                    records = acquisition_records()
                    self.respond(200, {"records": records[(page - 1) * size:page * size]})
                return
            match = re.fullmatch(r"/documents/([a-f0-9]{12})/receipt", path)
            if not match:
                self.respond(404, {"code": "NOT_FOUND"})
                return
            with guard, sqlite3.connect(target) as db:
                oracle = db.execute("SELECT payload FROM oracles WHERE nonce=?", [match[1]]).fetchone()
                if not oracle:
                    self.respond(404, {"code": "ORACLE_NOT_REGISTERED"})
                    return
                rows = [(structure, row) for structure, payload in db.execute("SELECT structure,payload FROM batches WHERE nonce=? ORDER BY id", [match[1]])
                        for row in json.loads(payload)]
                attempts = db.execute("SELECT COUNT(*) FROM delivery_calls WHERE nonce=?", [match[1]]).fetchone()[0]
            self.respond(200, {**receipt(json.loads(oracle[0]), rows, project), "post_attempts": attempts})

        def do_POST(self):
            arm = re.fullmatch(r"/acquisition/([a-f0-9]{12})/(arm|faults)", urlsplit(self.path).path)
            if arm:
                try:
                    count = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= count <= 128:
                        raise ValueError()
                    payload = json.loads(self.rfile.read(count)) if count else {}
                    failures = payload.get("failures", 1)
                    if type(failures) is not int or not 0 <= failures <= 8:
                        raise ValueError()
                except (ValueError, TypeError, AttributeError):
                    self.respond(422, {"code": "INVALID_SYNTHETIC_FAILURE_COUNT"})
                    return
                with guard, sqlite3.connect(target) as db:
                    found = db.execute("SELECT 1 FROM acquisition_fixtures WHERE nonce=?", [arm[1]]).fetchone()
                    if arm[2] == "arm":
                        if found:
                            self.respond(409, {"code": "SYNTHETIC_FAULTS_ALREADY_ARMED"})
                            return
                        db.execute("INSERT INTO acquisition_fixtures(nonce,failures) VALUES (?,?)", [arm[1], failures])
                    else:
                        if not found:
                            self.respond(404, {"code": "SYNTHETIC_FAULTS_NOT_ARMED"})
                            return
                        db.execute("UPDATE acquisition_fixtures SET failures=? WHERE nonce=?", [failures, arm[1]])
                self.respond(201 if arm[2] == "arm" else 200, {"armed": True, "page": 2, "failures": failures})
                return
            match = re.fullmatch(r"/documents/([a-f0-9]{12})/(oracle|headers|details)", urlsplit(self.path).path)
            if not match:
                self.respond(404, {"code": "NOT_FOUND"})
                return
            if match[2] != "oracle":
                with guard, sqlite3.connect(target) as db:
                    db.execute("INSERT INTO delivery_calls(nonce,structure) VALUES (?,?)", [match[1], match[2]])
            try:
                count = int(self.headers.get("Content-Length", "0"))
                if not 1 <= count <= 1024 * 1024:
                    raise ValueError()
                body = self.rfile.read(count)
                payload = json.loads(body)
                nonce, structure = match[1], match[2]
                with guard, sqlite3.connect(target) as db:
                    if structure == "oracle":
                        documents = payload["documents"]
                        if not isinstance(documents, list) or not 1 <= len(documents) <= 20:
                            raise ValueError()
                        for row in documents:
                            if set(row) != {"invoice", "total", "date"} or not re.fullmatch(r"0[0-9]+", row["invoice"]) or row["total"] != "1234.50" or row["date"] != "2026-10-01":
                                raise ValueError()
                        db.execute("INSERT INTO oracles(nonce,payload) VALUES (?,?)", [nonce, canonical(documents).decode()])
                        self.respond(201, {"registered": True, "documents": len(documents)})
                        return
                    if not db.execute("SELECT 1 FROM oracles WHERE nonce=?", [nonce]).fetchone():
                        raise ValueError()
                    identity, rows = payload["batch_id"], payload["records"]
                    digest = hashlib.sha256(body).hexdigest()
                    if self.headers.get("Idempotency-Key") != identity or self.headers.get("X-Trackvance-Payload-SHA256") != digest:
                        raise ValueError()
                    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
                        raise ValueError()
                    columns = {"document_id", "invoice", "total", "date"} if structure == "headers" else {"document_id", "sku", "amount"}
                    if any(set(row) != columns or any(not isinstance(v, str) for v in row.values()) for row in rows):
                        raise ValueError()
                    prior = db.execute("SELECT digest FROM batches WHERE id=?", [identity]).fetchone()
                    if prior:
                        # A replay is visible even with identical bytes. The product
                        # must reuse its receipt, not POST again after completion.
                        self.respond(409, {"code": "DUPLICATE_POST_FORBIDDEN"})
                        return
                    db.execute("INSERT INTO batches(id,nonce,structure,digest,payload) VALUES (?,?,?,?,?)", [identity, nonce, structure, digest, canonical(rows).decode()])
                self.respond(200, {"accepted": True, "rows": len(rows), "batch_id": identity, "payload_sha256": digest})
            except (ValueError, KeyError, TypeError, sqlite3.IntegrityError):
                self.respond(422, {"code": "SYNTHETIC_ORACLE_CONTRACT"})

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    if serve_forever:
        server.serve_forever()
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18095)
    parser.add_argument("--database", required=True)
    options = parser.parse_args()
    serve(options.port, options.database)
