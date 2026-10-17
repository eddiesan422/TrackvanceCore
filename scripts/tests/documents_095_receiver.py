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
from urllib.parse import urlsplit


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


def serve(port, database):
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
            self.respond(200, receipt(json.loads(oracle[0]), rows, project))

        def do_POST(self):
            match = re.fullmatch(r"/documents/([a-f0-9]{12})/(oracle|headers|details)", urlsplit(self.path).path)
            if not match:
                self.respond(404, {"code": "NOT_FOUND"})
                return
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

    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18095)
    parser.add_argument("--database", required=True)
    options = parser.parse_args()
    serve(options.port, options.database)
