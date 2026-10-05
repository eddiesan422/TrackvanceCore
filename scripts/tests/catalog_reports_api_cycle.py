"""Full-population Catálogo/Reportes certification inside UUID-owned PostgreSQL.

Only the caller starts Docker resources. Inputs, approvals and publications use
the real HTTP API and leased workers; direct database access is verification.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import http.client
import http.cookiejar
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from uuid import uuid4
from xml.etree.ElementTree import iterparse


class Client:
    def __init__(self):
        self.base, self.csrf = "http://127.0.0.1:8000/api/v1", ""
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def call(self, path, body=None, *, method=None, expected=200):
        headers = {"X-CSRF-Token": self.csrf}
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=1900) as response:
                status, value = response.status, json.load(response)
        except urllib.error.HTTPError as error:
            status, value = error.code, json.load(error)
        if status != expected:
            code = (value.get("error") or {}).get("code", "UNKNOWN")
            raise AssertionError(f"{path.split('?')[0]}: HTTP {status}, expected {expected}, code={code}")
        if "csrf_token" in value:
            self.csrf = value["csrf_token"]
        return value

    def upload(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=1900)
        try:
            headers = {"Content-Type": "application/octet-stream", "Content-Length": str(path.stat().st_size),
                       "X-CSRF-Token": self.csrf,
                       "Cookie": "; ".join(f"{c.name}={c.value}" for c in self.cookies)}
            connection.putrequest("POST", "/api/v1/datasets/uploads/stage?filename=" + urllib.parse.quote(path.name))
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.endheaders()
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    connection.send(chunk)
            response = connection.getresponse()
            value = json.loads(response.read())
            assert response.status == 201, value.get("error", {}).get("code")
            return value["upload"]["id"]
        finally:
            connection.close()

    def stream(self, context, format="CSV"):
        request = urllib.request.Request(self.base + "/reports/download",
            data=json.dumps({"context_id": context, "format": format}).encode(),
            headers={"Content-Type": "application/json", "X-CSRF-Token": self.csrf})
        return self.opener.open(request, timeout=1900)


def wait(client, path, timeout=1900):
    began = time.monotonic()
    while time.monotonic() - began < timeout:
        value = client.call(path)
        if value["status"] not in {"QUEUED", "RUNNING", "PENDING"}:
            return value
        time.sleep(0.5)
    raise AssertionError("The durable worker did not reach a terminal state.")


def source_key(index):
    return f"{index:08d}ñ"


def source_value(alias, index):
    return f"{alias}:á,{index}:\"texto\""


def oracle_rows(kind, rows):
    half = rows // 2
    population = range(half, rows) if kind == "INNER" else range(half, rows + half) if kind == "RIGHT" else range(rows + half) if kind == "FULL" else range(rows)
    for index in population:
        a, b = index < rows, half <= index < rows + half
        yield [source_key(index) if a else None, source_value("a", index) if a else None,
               source_key(index) if b else None, source_value("b", index) if b else None]


def many_to_many_rows(rows):
    for left in range(rows):
        for right in range(rows // 2, rows + rows // 2):
            if left % 7 == right % 7:
                yield [source_key(left), source_value("a", left), source_key(right), source_value("b", right)]


def rows_hash(rows):
    digest, count = hashlib.sha256(), 0
    for row in rows:
        digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
        count += 1
    return {"rows": count, "sha256": digest.hexdigest()}


def xlsx_rows(content):
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(content)) as package, package.open("xl/worksheets/sheet1.xml") as sheet:
        for _, element in iterparse(sheet, events=("end",)):
            if element.tag == namespace + "row":
                values = [None] * 4
                for cell in element:
                    assert cell.get("t") == "inlineStr" and cell.find(namespace + "f") is None
                    column = ord(cell.get("r")[0]) - ord("A")
                    values[column] = cell.find(namespace + "is/" + namespace + "t").text or ""
                yield values
                element.clear()


def acquire(client, directory, alias, rows, macro, domain):
    first = rows // 2 if alias == "b" else 0
    path = directory / f"source-{alias}-{rows}.csv"
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["key", "zone", "value", "amount"])
        for index in range(first, first + rows):
            writer.writerow([source_key(index), f"z{index % 7}", source_value(alias, index), f"{index + 1}.12345678"])
    upload_id = client.upload(path)
    dataset = client.call("/datasets", {"name": f"Certificación {alias} {rows} {uuid4().hex[:8]}"}, expected=201)
    # Optional classification permits upload, while reporting stays unavailable.
    assert dataset.get("macro_domain_id") is None and dataset.get("domain_id") is None
    acquisition = client.call(f"/datasets/{dataset['id']}/acquisitions", {"upload_id": upload_id}, expected=202)
    acquired = wait(client, "/acquisitions/" + acquisition["id"])
    assert acquired["status"] == "SUCCESS" and acquired["processed_rows"] == rows
    client.call(f"/datasets/{dataset['id']}/governance", {"expected_version": 1,
                "macro_domain_id": macro, "domain_id": domain, "information_classification": "INTERNAL"}, method="PATCH")
    contract = client.call("/intake/contracts", {"name": f"Contrato {alias} {uuid4().hex[:8]}", "dataset_id": dataset["id"],
        "config": {"required_columns": ["key", "zone"], "positive_columns": ["amount"], "max_error_rate": 0}}, expected=201)
    run = client.call("/intake/runs", {"contract_id": contract["id"], "dataset_version_id": acquired["output_version_id"],
        "requested_engine": "AUTO"}, expected=202)
    approved = wait(client, "/runs/" + run["id"])
    assert approved["status"] == "SUCCESS" and approved["decision"] == "APPROVED" and approved["metrics"]["total_rows"] == rows, {
        "status": approved["status"], "decision": approved.get("decision"),
        "total_rows": approved.get("metrics", {}).get("total_rows"), "expected_rows": rows}
    return {"alias": alias, "input_dataset_id": dataset["id"], "contract_id": contract["id"],
            "contract_revision_ids": [contract["id"]], "policy": "LATEST_APPROVED"}, approved


def draft(sources, kind="INNER", *, triple=False):
    joins = [{"left_alias": "a", "right_alias": "b", "type": kind, "keys": [
        {"left_column": "key", "right_column": "key"}, {"left_column": "zone", "right_column": "zone"}],
        "expected_cardinality": "1:1", "allow_many_to_many": False}]
    columns = [{"source_alias": alias, "column": col, "alias": alias + "_" + col}
               for alias in ("a", "b") for col in ("key", "value")]
    if triple:
        joins.append({"left_alias": "a", "right_alias": "c", "type": "LEFT", "keys": [
            {"left_column": "key", "right_column": "key"}], "expected_cardinality": "1:1", "allow_many_to_many": False})
        columns.append({"source_alias": "c", "column": "value", "alias": "c_value"})
    return {"mode": "GUIDED", "sources": sources, "columns": columns, "joins": joins,
            "order_by": [{"source_alias": "a", "column": "key", "direction": "ASC"},
                         {"source_alias": "b", "column": "key", "direction": "ASC"}]}


def verify_materialized(execution, expected):
    import polars as pl

    from trackvance.artifactstore import storage_provider
    from trackvance.db import SessionLocal
    from trackvance.models import Artifact, DatasetVersion

    with SessionLocal() as db:
        version = db.get(DatasetVersion, execution["output_version_id"])
        assert version.version == 1 and version.row_count == expected["rows"] and version.source_run_id is None
        artifact = db.get(Artifact, version.canonical_artifact_id)
        numbering = version.ingestion_metadata
        assert numbering["row_numbering"] == "DERIVED_RECORD_NUMBER"
        number_column = numbering["record_number_column"]
        paths = storage_provider.dataset_paths(artifact)
        digest, count = hashlib.sha256(), 0
        for path in paths:
            for row in pl.read_parquet(path).iter_rows(named=True):
                count += 1
                assert row[number_column] == count
                values = [row[name] for name in ("a_key", "a_value", "b_key", "b_value")]
                if "c_value" in row:
                    values.append(row["c_value"])
                digest.update(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
        assert count == expected["rows"] and digest.hexdigest() == expected["sha256"], "The complete canonical population differs from the independent oracle."
        return {"status": "PASS", "rows": count, "sha256": digest.hexdigest(), "parts": len(paths)}


def decode_csv(value):
    if value == r"\N":
        return None
    if value.startswith(("\\\\", "''")):
        return value[1:]
    if value.startswith("'") and value[1:].lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return value[1:]
    return value


def certify(rows, directory):
    from sqlalchemy.engine import make_url
    project = os.environ.get("TRACKVANCE_CERTIFICATION_PROJECT", "")
    url = make_url(os.environ.get("DATABASE_URL", ""))
    assert re.fullmatch(r"trackvance-v080-test-[a-z0-9-]+-[a-f0-9]{12}", project)
    assert url.get_backend_name() == "postgresql" and url.username == url.database == "tv_v080_test"
    directory.mkdir(parents=True, exist_ok=True)
    client = Client()
    client.call("/auth/demo", {})
    macro = client.call("/catalog/macrodomains", {"name": "Certificación " + uuid4().hex[:8]}, expected=201)
    domain = client.call("/catalog/domains", {"name": "Reportes", "macro_domain_id": macro["id"]}, expected=201)
    sources, approvals = [], []
    result = {"version": "0.8.0", "status": "FAIL", "project": project, "rows_per_source": rows, "sources": [], "joins": []}
    began = time.monotonic()
    for alias in ("a", "b", "c"):
        source, approval = acquire(client, directory, alias, rows, macro["id"], domain["id"])
        sources.append(source)
        approvals.append(approval)
        result["sources"].append({"alias": alias, "rows": rows, "approval_id": approval["id"], "status": "PASS"})
    for kind in ("INNER", "LEFT", "RIGHT", "FULL"):
        started = time.monotonic()
        query = draft(sources[:2], kind)
        context = client.call("/reports/resolve", {"draft": query})
        preview = client.call("/reports/preview", {"context_id": context["context_id"]})
        assert preview["status"] == "SUCCESS" and len(preview["rows"]) == min(10, rows // 2)
        # Explicit ordering puts unmatched right rows after the matching rows.
        expected_rows = list(oracle_rows(kind, rows)) if rows <= 2000 else None
        expected = rows_hash(iter(expected_rows) if expected_rows is not None else oracle_rows(kind, rows))
        preview_values = [[r[n] for n in ("a_key", "a_value", "b_key", "b_value")] for r in preview["rows"]]
        first_expected = []
        for row in oracle_rows(kind, rows):
            first_expected.append(row)
            if len(first_expected) == 10:
                break
        assert preview_values == first_expected
        body = {"context_id": context["context_id"], "idempotency_key": "cert-" + uuid4().hex,
                "name": f"Resultado {kind} {rows} {uuid4().hex[:8]}", "macro_domain_id": macro["id"], "domain_id": domain["id"]}
        generation = client.call("/reports/datasets", body)
        assert client.call("/reports/datasets", body)["id"] == generation["id"]
        complete = wait(client, "/reports/executions/" + generation["id"])
        assert complete["status"] == "SUCCESS", complete.get("error_code")
        integrity = verify_materialized(complete, expected)
        measured = {key: value for key, value in complete["metrics"].items() if isinstance(value, (int, float, bool))}
        sample_execution = client.call("/reports/executions/" + preview["execution_id"])
        result.setdefault("resource_metrics", []).append({"join": kind, "dataset": measured,
            "preview": {key: value for key, value in sample_execution["metrics"].items() if isinstance(value, (int, float, bool))}})
        result["joins"].append({"type": kind, "preview_rows": len(preview["rows"]), "materialized": integrity,
                                "seconds": round(time.monotonic() - started, 3), "status": "PASS"})
        if kind == "INNER":
            # SQL and guided mode share the same frozen inputs and join policy.
            sql = {**query, "mode": "SQL", "sql": 'SELECT a.key AS a_key,a.value AS a_value,b.key AS b_key,b.value AS b_value FROM a INNER JOIN b ON a.key=b.key AND a.zone=b.zone ORDER BY a.key,b.key'}
            sql_context = client.call("/reports/resolve", {"draft": sql})
            assert client.call("/reports/preview", {"context_id": sql_context["context_id"]})["rows"] == preview["rows"]
            limited = {**sql, "sql": sql["sql"] + " LIMIT 100000"}
            download_context = client.call("/reports/resolve", {"draft": limited})
            with client.stream(download_context["context_id"]) as response:
                execution_id = response.headers["X-Report-Execution-Id"]
                reader = csv.reader(io.TextIOWrapper(response, encoding="utf-8", newline=""))
                assert next(reader) == ["a_key", "a_value", "b_key", "b_value"]
                downloaded = rows_hash([decode_csv(v) for v in row] for row in reader)
            from itertools import islice
            assert downloaded == rows_hash(islice(oracle_rows("INNER", rows), 100000))
            transmission = client.call("/reports/executions/" + execution_id)
            assert transmission["status"] == "SUCCESS" and transmission["transmission_status"] == "COMPLETE"
            result["csv"] = {"status": "PASS", **downloaded, "max_rows": 100000, "generation": transmission["generation_status"], "transmission": transmission["transmission_status"]}
            xlsx_context = client.call("/reports/resolve", {"draft": {**sql, "sql": sql["sql"] + " LIMIT 50000"}})
            with client.stream(xlsx_context["context_id"], "XLSX") as response:
                execution_id = response.headers["X-Report-Execution-Id"]
                content = response.read(64 * 1024**2 + 1)
                assert len(content) <= 64 * 1024**2
            decoded = xlsx_rows(content)
            assert next(decoded) == ["a_key", "a_value", "b_key", "b_value"]
            exported = rows_hash(decoded)
            assert exported == rows_hash(islice(oracle_rows("INNER", rows), 50000))
            transmission = client.call("/reports/executions/" + execution_id)
            assert transmission["status"] == "SUCCESS" and transmission["transmission_status"] == "COMPLETE"
            result["xlsx"] = {"status": "PASS", **exported, "bytes": len(content), "max_rows": 50000,
                              "generation": transmission["generation_status"], "transmission": transmission["transmission_status"]}
            for export_format, limit in (("CSV", 100000), ("XLSX", 50000)) if rows > 200000 else ():
                excessive = {**sql, "sql": sql["sql"] + f" LIMIT {limit + 1}"}
                exceeded = client.call("/reports/resolve", {"draft": excessive})
                execution_id = None
                try:
                    with client.stream(exceeded["context_id"], export_format) as response:
                        execution_id = response.headers["X-Report-Execution-Id"]
                        while response.read(65536):
                            pass
                except (http.client.IncompleteRead, urllib.error.URLError, OSError):
                    pass  # HTTP headers may already be transmitted; terminal state remains authoritative.
                assert execution_id is not None
                failed = wait(client, "/reports/executions/" + execution_id)
                assert failed["status"] == "FAILED" and failed["generation_status"] == "FAILED" and failed["transmission_status"] == "INTERRUPTED"
                assert failed["output_version_id"] is None and failed["error_code"] == "REPORT_RESULT_LIMIT"
                result[export_format.lower() + "_above_limit"] = {"status": "PASS", "requested_rows": limit + 1, "execution_status": failed["status"], "error_code": failed["error_code"]}
    triple = draft(sources, "LEFT", triple=True)
    context = client.call("/reports/resolve", {"draft": triple})
    assert len(client.call("/reports/preview", {"context_id": context["context_id"]})["rows"]) == 10
    generated = client.call("/reports/datasets", {"context_id": context["context_id"], "idempotency_key": "cert-" + uuid4().hex,
        "name": "Resultado triple " + uuid4().hex[:8], "macro_domain_id": macro["id"], "domain_id": domain["id"]})
    complete = wait(client, "/reports/executions/" + generated["id"])
    assert complete["status"] == "SUCCESS", complete.get("error_code")
    triple_expected = rows_hash([*row, source_value("c", index)] for index, row in enumerate(oracle_rows("LEFT", rows)))
    result["three_sources"] = verify_materialized(complete, triple_expected)
    nm = draft(sources[:2])
    nm["joins"][0].update({"keys": [{"left_column": "zone", "right_column": "zone"}],
                           "expected_cardinality": "N:M", "allow_many_to_many": True})
    nm_context = client.call("/reports/resolve", {"draft": nm})
    if rows == 120:
        assert len(client.call("/reports/preview", {"context_id": nm_context["context_id"]})["rows"]) == 10
        publication = client.call("/reports/datasets", {"context_id": nm_context["context_id"], "idempotency_key": "cert-" + uuid4().hex,
            "name": "N:M acotado " + uuid4().hex[:8], "macro_domain_id": macro["id"], "domain_id": domain["id"]})
        published = wait(client, "/reports/executions/" + publication["id"])
        assert published["status"] == "SUCCESS"
        result["bounded_many_to_many"] = verify_materialized(published, rows_hash(many_to_many_rows(rows)))
    else:
        excess = client.call("/reports/preview", {"context_id": nm_context["context_id"]}, expected=422)
        code = (excess.get("error") or {}).get("code")
        assert code in {"REPORT_JOIN_LIMIT", "REPORT_JOIN_EXPANSION"}, code
        result["many_to_many_expansion_rejected"] = {"status": "PASS", "error_code": code}
    # Current parent blocks revoke already-frozen contexts and native derived reads.
    block = client.call(f"/catalog/datasets/{sources[0]['input_dataset_id']}/blocks", {"scope": "CONTENT", "reason": "Prueba de propagación"}, expected=201)
    rejected = client.call("/reports/preview", {"context_id": context["context_id"]}, expected=403)
    assert (rejected.get("error") or {}).get("code")
    client.call("/dataset-versions/" + complete["output_version_id"] + "/preview", expected=403)
    client.call("/catalog/blocks/" + block["id"], {"expected_version": 1, "active": False, "reason": "Fin de la prueba sintética"}, method="PATCH")
    result["parent_block_propagation"] = "PASS"
    result["duration_seconds"] = round(time.monotonic() - began, 3)
    result["status"] = "PASS"
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, choices=(120, 400000, 1000000), default=120)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    summary = certify(args.rows, args.evidence)
    print(json.dumps({"status": summary["status"], "rows_per_source": args.rows, "duration_seconds": summary["duration_seconds"]}))
