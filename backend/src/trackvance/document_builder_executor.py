"""Bounded draft rules over already extracted reference geometry.

The API retains the draft and stores a test only after a complete child result.
The child uses the existing document confinement and receives no DB credentials.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TIMEOUT_SECONDS = 30
MAX_BYTES = 64 * 1024**2
MAX_REFERENCES = 20
MEMORY_BYTES = 768 * 1024**2


def _region(value):
    return (isinstance(value, list) and len(value) == 4
        and all(isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 1 for v in value)
        and value[0] < value[2] and value[1] < value[3])


def _valid_request(request):
    from trackvance.document_representation import validate_representation
    from trackvance.document_template_builder_rules import valid_page_geometry

    if not isinstance(request, dict) or request.get("operation") not in {"EXTRACT", "DISCOVER"}:
        return False
    references = request.get("references")
    if not isinstance(references, list) or not 1 <= len(references) <= MAX_REFERENCES:
        return False
    ids = []
    for reference in references:
        if not isinstance(reference, dict) or not isinstance(reference.get("id"), str) or not 1 <= len(reference["id"]) <= 128:
            return False
        ids.append(reference["id"])
        representation = reference.get("document_representation")
        if representation is not None:
            try:
                validate_representation(representation)
            except (KeyError, TypeError, ValueError):
                return False
        lines = reference.get("lines")
        if not isinstance(lines, list) or len(lines) > 100000:
            return False
        characters = word_characters = 0
        for line in lines:
            if (not isinstance(line, dict) or not isinstance(line.get("text"), str)
                or not isinstance(line.get("page"), int) or not 1 <= line["page"] <= 50
                or line.get("method") not in ({"DIGITAL", "OCR", "PADDLE_BLOCK"} if representation is not None else {"DIGITAL", "OCR"})
                or not _region(line.get("region"))):
                return False
            characters += len(line["text"])
            words = line.get("words")
            if representation is not None and line.get("method") == "PADDLE_BLOCK" and words is None:
                if line.get("geometry_kind") != "QUERY_REGION" or characters > 2_000_000:
                    return False
                continue
            if not isinstance(words, list) or not words or len(words) > 100000:
                return False
            for word in words:
                if not isinstance(word, dict) or not isinstance(word.get("text"), str) or not _region(word.get("region")):
                    return False
                word_characters += len(word["text"])
            if max(characters, word_characters) > 2_000_000:
                return False
        pages = ({page["page_one_based"] for page in representation["pages"]} if representation is not None else {line["page"] for line in lines})
        if "page_geometry" in reference and not valid_page_geometry(reference["page_geometry"], pages):
            return False
    if len(set(ids)) != len(ids):
        return False
    return request["operation"] != "EXTRACT" or isinstance(request.get("definition"), dict)


def _write_json(path, value):
    """Incremental encoding refuses the next chunk before exceeding the file budget."""
    count = 0
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    with path.open("wb") as stream:
        for chunk in encoder.iterencode(value):
            data = chunk.encode("utf-8")
            count += len(data)
            if count > MAX_BYTES:
                raise ValueError("DOCUMENT_BUILDER_BYTES_LIMIT")
            stream.write(data)


def _valid_result(result, request):
    from trackvance.document_template_builder_rules import (
        source_digest,
        valid_header_observation,
        valid_page_geometry,
    )

    if not isinstance(result, dict):
        return False
    definition = request.get("definition")
    if request["operation"] == "DISCOVER":
        definition = result.get("definition")
        if not isinstance(definition, dict) or not isinstance(definition.get("fields"), list) or not isinstance(definition.get("tables"), list):
            return False
        if not definition["fields"]:
            if any("document_representation" in reference for reference in request["references"]):
                from trackvance.document_schemas import DocumentDraftDefinition

                try:
                    DocumentDraftDefinition.model_validate(definition)
                except ValueError:
                    return False
                return result.get("results") == {}
            return not definition["tables"] and ("results" not in result or result["results"] == {})
        result = result.get("results")
    if (not isinstance(definition, dict) or not isinstance(definition.get("fields"), list)
        or not isinstance(definition.get("tables"), list)):
        return False
    fields = definition["fields"]
    tables = definition["tables"]
    if (any(not isinstance(field, dict) or not isinstance(field.get("name"), str) for field in fields)
        or any(not isinstance(table, dict) or not isinstance(table.get("name"), str) or not isinstance(table.get("fields"), list)
               or any(not isinstance(field, dict) or not isinstance(field.get("name"), str) for field in table["fields"]) for table in tables)):
        return False
    names = {field["name"] for field in fields}
    columns = {table["name"]: {field["name"] for field in table["fields"]} for table in tables}
    if len(names) != len(fields) or len(columns) != len(tables) or any(len(columns[table["name"]]) != len(table["fields"]) for table in tables):
        return False
    if not isinstance(result, dict) or set(result) != {reference["id"] for reference in request["references"]}:
        return False
    sources = {reference["id"]: reference["lines"] for reference in request["references"]}
    geometries = {reference["id"]: reference.get("page_geometry") for reference in request["references"]}
    geometry_present = {reference["id"]: "page_geometry" in reference for reference in request["references"]}
    representations = {reference["id"]: reference.get("document_representation") for reference in request["references"]}
    definition_digest = None
    for identity, payload in result.items():
        if (not isinstance(payload, dict) or not isinstance(payload.get("fields"), dict)
            or not isinstance(payload.get("tables"), dict) or not isinstance(payload.get("table_unparsed"), dict)
            or "header_resolutions" in payload):
            return False
        expected_pages = (sorted({page["page_one_based"] for page in representations[identity]["pages"]})
            if representations[identity] is not None else sorted({line["page"] for line in sources[identity]}))
        if (set(payload["fields"]) != names or set(payload["tables"]) != set(columns)
            or set(payload["table_unparsed"]) != set(columns) or payload.get("lines") != sources[identity]
            or payload.get("pages") != expected_pages
            or payload.get("page_geometry") != geometries[identity]):
            return False
        if representations[identity] is not None and payload.get("document_representation") != representations[identity]:
            return False
        if (("page_geometry" in payload) != geometry_present[identity]
            or geometry_present[identity] and (not valid_page_geometry(payload["page_geometry"], payload["pages"])
                or json.dumps(payload["page_geometry"], sort_keys=True) != json.dumps(geometries[identity], sort_keys=True))):
            return False
        cells = list(payload["fields"].values())
        for table, rows in payload["tables"].items():
            if not isinstance(rows, list) or len(rows) > 10000 or any(not isinstance(row, dict) for row in rows):
                return False
            if any(set(row) != columns[table] for row in rows):
                return False
            cells.extend(cell for row in rows for cell in row.values())
        for cell in cells:
            if (not isinstance(cell, dict) or not {"value", "observed", "validity", "origin", "confidence", "reviewed"}.issubset(cell)
                or cell["validity"] not in {"VALID", "INVALID", "MISSING", "AMBIGUOUS"}
                or cell["value"] is not None and not isinstance(cell["value"], str)
                or cell["observed"] is not None and not isinstance(cell["observed"], str)
                or not isinstance(cell["reviewed"], bool)):
                return False
        if any(not isinstance(regions, list) or any(not isinstance(region, dict) for region in regions)
               for regions in payload["table_unparsed"].values()):
            return False
        observations = [(table, region) for table, regions in payload["table_unparsed"].items() for region in regions
                        if region.get("reason") in {"HEADER_UNVERIFIED", "CONTINUATION_CONTEXT_UNVERIFIED"}
                        or "header_observation" in region]
        if observations:
            source_hash = source_digest(sources[identity])
            if definition_digest is None:
                definition_digest = source_digest(definition)
            if any(not valid_header_observation(region, sources[identity], definition, table,
                       source_hash=source_hash, definition_hash=definition_digest) for table, region in observations):
                return False
        period = definition.get("business_period_field")
        if payload.get("business_period") != ({"field": period, **payload["fields"][period]} if period else None):
            return False
        arithmetic = definition.get("arithmetic", [])
        if definition.get("invoice_controls"):
            from .document_engine import arithmetic_checks

            if payload.get("arithmetic") != arithmetic_checks(payload, definition):
                return False
        elif arithmetic:
            expected_checks = [(rule, index) for rule in arithmetic
                for index in range(len(payload["tables"][rule["table"]]) if rule.get("table") else 1)]
            checks = payload.get("arithmetic")
            if not isinstance(checks, list) or len(checks) != len(expected_checks):
                return False
            for check, (rule, index) in zip(checks, expected_checks, strict=True):
                if (not isinstance(check, dict) or check.get("name") != rule["name"] or check.get("table") != rule.get("table")
                    or check.get("row") != index or check.get("scale") != rule["scale"] or check.get("rounding") != "ROUND_HALF_UP"
                    or check.get("status") not in {"VALID", "DISCREPANCY", "NOT_EVALUATED"}
                    or check["status"] != "NOT_EVALUATED" and (not isinstance(check.get("expected"), str) or not isinstance(check.get("observed"), str))):
                    return False
    return True


def _environment(folder):
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent", "TMPDIR": str(folder),
        "TZ": "UTC", "OMP_THREAD_LIMIT": "1", "OMP_NUM_THREADS": "1", "DEMO_ACCESS_ENABLED": "false",
        "DEMO_SEED_ENABLED": "false", "DATABASE_URL": "sqlite:///:memory:",
        "TRACKVANCE_STORAGE_DIR": str(folder / "child-storage")}
    runtime = Path(sys.base_prefix) / "lib"
    if runtime.is_dir():
        environment["LD_LIBRARY_PATH"] = str(runtime)
    return environment


def run_builder(operation, references, definition=None):
    from .operations_common import OperationError

    if sys.platform != "linux":
        raise OperationError(503, "DOCUMENT_SANDBOX_UNAVAILABLE", "La prueba documental requiere el servicio Linux aislado.")
    request = {"operation": operation, "references": references, "definition": definition}
    if not _valid_request(request):
        raise OperationError(422, "DOCUMENT_BUILDER_INPUT", "La lectura de referencias no tiene una estructura válida dentro de los límites documentales.")
    temporary = storage_provider.temporary_path(".builder")
    folder = Path(tempfile.mkdtemp(prefix=temporary.stem + "-", dir=temporary.parent))
    process = None
    try:
        try:
            _write_json(folder / "request.json", request)
        except (ValueError, OverflowError):
            raise OperationError(422, "DOCUMENT_BUILDER_BYTES_LIMIT", "Las referencias y reglas superan el límite conjunto de 64 MiB.") from None
        started = time.monotonic()
        process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()), str(folder)],
            cwd=folder, env=_environment(folder), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        while process.poll() is None:
            if time.monotonic() - started >= TIMEOUT_SECONDS:
                raise OperationError(422, "DOCUMENT_BUILDER_TIMEOUT", "La prueba o el descubrimiento excedió 30 segundos. El borrador se conserva; reduce referencias o simplifica las reglas antes de repetir.")
            time.sleep(.02)
        path = folder / "result.json"
        if path.exists() and path.stat().st_size > MAX_BYTES:
            raise OperationError(422, "DOCUMENT_BUILDER_BYTES_LIMIT", "El resultado de la prueba supera 64 MiB. El borrador se conserva.")
        if not path.exists() or process.returncode:
            raise OperationError(422, "DOCUMENT_BUILDER_EXECUTION_FAILURE", "La prueba documental no produjo un resultado completo dentro de sus límites. El borrador se conserva.")
        try:
            data = json.loads(path.read_bytes())
        except (ValueError, UnicodeError):
            raise OperationError(422, "DOCUMENT_BUILDER_EXECUTION_FAILURE", "La prueba documental no produjo una respuesta válida. El borrador se conserva.") from None
        if isinstance(data, dict) and data.get("error"):
            code = data["error"]
            if code not in {"DOCUMENT_SANDBOX_UNAVAILABLE", "DOCUMENT_BUILDER_BYTES_LIMIT", "DOCUMENT_DISCOVERY_VARIANTS_LIMIT"}:
                code = "DOCUMENT_BUILDER_EXECUTION_FAILURE"
            raise OperationError(422, code, "No fue posible completar la prueba documental dentro de sus límites. El borrador y las pruebas anteriores se conservan.")
        if not _valid_result(data, request):
            raise OperationError(422, "DOCUMENT_BUILDER_EXECUTION_FAILURE", "La prueba documental devolvió una estructura incompleta. El borrador se conserva.")
        return data
    finally:
        if process:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        shutil.rmtree(folder)


def verify_automatic_payload(payload, definition):
    """Recheck body coverage in the existing confined EXTRACT worker.

    Only the server's immutable automatic result is eligible. This proof is
    required before a header-only source confirmation, never a new OCR/READ or
    a client-supplied assertion that a region contains no products.
    """
    from .document_template_builder_rules import source_digest

    if not isinstance(payload, dict) or "header_resolutions" in payload or not isinstance(payload.get("lines"), list):
        return False
    reference = {"id": "header-source-verification", "lines": payload["lines"]}
    if "page_geometry" in payload:
        reference["page_geometry"] = payload["page_geometry"]
    actual = run_builder("EXTRACT", [reference], definition)[reference["id"]]
    # OCR operational envelopes may also contain diagnostics/processor metadata.
    # Every extraction business value and every region must still match exactly.
    keys = {"fields", "tables", "pages", "lines", "table_unparsed", "business_period"}
    if "page_geometry" in actual or "page_geometry" in payload:
        keys.add("page_geometry")
    if "arithmetic" in actual or "arithmetic" in payload:
        keys.add("arithmetic")
    if not keys <= payload.keys() or not keys <= actual.keys():
        return False
    return source_digest({key: payload[key] for key in keys}) == source_digest({key: actual[key] for key in keys})


def _child(folder):
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    # The code path is fixed by this installed executable, never by the request.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from trackvance.document_discovery import discover, extract_reference
    from trackvance.document_engine import confine
    from trackvance.document_schemas import DocumentDefinition
    from trackvance.operations_common import OperationError

    try:
        path = folder / "request.json"
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("DOCUMENT_BUILDER_BYTES_LIMIT")
        request = json.loads(path.read_bytes())
        if not _valid_request(request):
            raise ValueError("DOCUMENT_BUILDER_INPUT")
        # ZoneInfo reads immutable platform metadata before filesystem confinement.
        ZoneInfo("America/Bogota")
        if isinstance(request.get("definition"), dict):
            ZoneInfo(request["definition"].get("publication", {}).get("timezone", "America/Bogota"))
        confine(folder)
        references = request["references"]
        sources = [SimpleNamespace(id=reference["id"], payload={"lines": reference["lines"],
            **{key: reference[key] for key in ("page_geometry", "document_representation") if key in reference}}) for reference in references]
        if request["operation"] == "DISCOVER":
            result = discover(sources)
        else:
            definition = DocumentDefinition.model_validate(request["definition"]).model_dump(mode="json")
            result = {reference.id: extract_reference(reference, definition) for reference in sources}
        _write_json(folder / "result.json", result)
    except (ArithmeticError, KeyError, MemoryError, OSError, RecursionError, TypeError, ValueError, OperationError) as error:
        code = error.code if isinstance(error, OperationError) else str(error)
        safe = code if code in {"DOCUMENT_SANDBOX_UNAVAILABLE", "DOCUMENT_BUILDER_BYTES_LIMIT", "DOCUMENT_DISCOVERY_VARIANTS_LIMIT"} else "DOCUMENT_BUILDER_EXECUTION_FAILURE"
        _write_json(folder / "result.json", {"error": safe})


if __name__ == "__main__":
    _child(Path(sys.argv[1]).resolve())
else:
    from .artifactstore import storage_provider
