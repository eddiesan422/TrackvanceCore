"""REST POST batches: local preparation, durable markers and explicit confirmation.

Each batch is an independent remote operation. No global transaction is claimed;
neither an HTTP status nor an idempotency header proves completed processing.
"""

import hashlib
import json
import shutil
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from .artifactstore import link_artifact, storage_provider
from .data_sinks import DatabaseDataSink, DeliveryError, convert_value
from .db import iso, require_record, utcnow
from .delivery_schemas import DeliveryDraft
from .delivery_streams import DeliveryLimits
from .models import (
    Artifact,
    Configuration,
    DeliveryAttempt,
    DeliveryDestination,
    DeliveryDestinationVersion,
    Run,
)
from .rest_models import RestDeliveryBatch
from .rest_transport import RestError, endpoint, exact_json, json_path, request


class RestSink(DatabaseDataSink):
    def __init__(self, settings):
        self.settings = settings

    def test(self):
        # HEAD is a connectivity/authentication probe and sends no business rows.
        try:
            status, _ = request(self.settings.options, self.settings.password, "HEAD", max_bytes=1024)
            if not 200 <= status < 300 and status != 405:
                raise DeliveryError("REST_TEST_FAILED", "El endpoint REST no permite verificar acceso.")
        except RestError as error:
            raise DeliveryError(error.code, error.message) from None

    def schemas(self):
        return []

    def tables(self, schema_name):
        return []


def rest_metric_semantics():
    return {"version": 1, "rows_attempted": "SOURCE_ROWS_PREPARED",
            "rows_written": "SOURCE_ROWS_EXPLICITLY_CONFIRMED_BY_RECEIVER",
            "rows_inserted": "NOT_REPORTED", "rows_updated": "NOT_REPORTED",
            "bytes_sent": "JSON_PAYLOAD_WIRE_BYTES_IN_CONFIRMED_BATCHES", "physical_destination_rows": "NOT_MEASURED"}


def mapped_row(row, columns):
    result = {}
    for column in columns:
        value = convert_value(row.get(column.source_name), column.model_dump())
        # Exact decimal strings preserve scale and avoid receiver IEEE-754 loss.
        if isinstance(value, (Decimal, date, datetime)):
            value = value.isoformat() if isinstance(value, (date, datetime)) else str(value)
        result[column.target_name] = value
    return result


def payload_bytes(target, rows, batch_id):
    return json.dumps({**target.static_body, "batch_id": batch_id, target.rows_key: rows},
                      ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _context(db, organization_id, draft, *, control=None, release_metadata=False):
    from .delivery_service import (
        DeliveryOperationError,
        _owned_version,
        _source_type_mismatches,
        exact_destination_version,
        settings_for,
    )
    version, dataset, artifact, records = _owned_version(db, draft.dataset_version_id, organization_id,
                                                        control=control, release_metadata=release_metadata)
    destination = db.scalar(select(DeliveryDestination).where(DeliveryDestination.id == draft.destination_id,
        DeliveryDestination.organization_id == organization_id, DeliveryDestination.deleted.is_(False)))
    if destination is None or not destination.enabled or destination.sink_type != "REST":
        raise DeliveryOperationError(412, "REST_DESTINATION_REQUIRED", "Selecciona un destino REST habilitado para este target.")
    destination_version = exact_destination_version(db, destination, draft.destination_version_id)
    missing = {c.source_name for c in draft.columns} - set(records.columns)
    if missing or _source_type_mismatches(version, draft):
        raise DeliveryOperationError(412, "FAILED_PRECONDITION", "El mapping REST debe utilizar campos existentes sin cambiar sus tipos lógicos.")
    if version.row_count > DeliveryLimits.configured().max_rows:
        raise DeliveryOperationError(412, "DELIVERY_ROW_LIMIT", "La población supera el límite de Delivery.")
    settings = settings_for(destination, destination_version)
    endpoint(settings.options["url"])
    return version, dataset, artifact, records, destination, destination_version, settings


def rest_preview(db, organization_id, draft, *, limit=8):
    version, _, _, records, _, _, _ = _context(db, organization_id, draft)
    sample = records.select([column.source_name for column in draft.columns]).head(limit).to_dicts()
    return {"dataset_version_id": version.id,
        "columns": [{"source_name": c.source_name, "target_name": c.target_name, "ordinal": c.ordinal,
                     "source_type": c.target_type, "target_type": c.target_type} for c in draft.columns],
        "source_rows": sample, "destination_rows": [mapped_row(row, draft.columns) for row in sample],
        "sampled_rows": len(sample)}


def _payloads(records, draft, run_id, check=None):
    target = draft.target
    pending: list[dict] = []
    ordinal = pending_bytes = 0
    overhead = len(payload_bytes(target, [], "0" * 36))
    deadline = time.monotonic() + DeliveryLimits.configured().preparation_timeout_seconds

    def prepared():
        if ordinal >= target.max_batches:
            raise DeliveryError("REST_BATCH_LIMIT", "La entrega supera el limite finito de lotes; aumenta filas por lote dentro del presupuesto.")
        identity = str(uuid5(NAMESPACE_URL, f"trackvance:rest:{run_id}:{ordinal}"))
        body = payload_bytes(target, pending, identity)
        if len(body) > target.max_payload_bytes:
            raise DeliveryError("REST_PAYLOAD_SIZE_LIMIT", "Un lote REST supera su limite de bytes.")
        return identity, ordinal, pending, body

    for row in records:
        if check and not len(pending):
            check()
        if time.monotonic() >= deadline:
            raise DeliveryError("REST_PREPARATION_TIMEOUT", "La preparacion REST excedio el tiempo maximo.")
        mapped = mapped_row(row, draft.columns)
        size = len(json.dumps(mapped, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
        if overhead + size > target.max_payload_bytes:
            raise DeliveryError("REST_PAYLOAD_SIZE_LIMIT", "Un registro REST supera el limite de bytes por lote.")
        if pending and (len(pending) >= target.batch_rows or overhead + pending_bytes + 1 + size > target.max_payload_bytes):
            yield prepared()
            ordinal += 1
            pending, pending_bytes = [], 0
        pending_bytes += size + (1 if pending else 0)
        pending.append(mapped)
    if pending:
        yield prepared()


def rest_preflight(db, organization_id, draft, *, control=None, release_metadata=False):
    version, dataset, artifact, records, destination, revision, settings = _context(
        db, organization_id, draft, control=control, release_metadata=release_metadata)
    # Full population validation; no network request to the writing endpoint.
    row_count = batches_count = 0
    for _, _, rows, _ in _payloads(records, draft, "preflight", control):
        row_count += len(rows)
        batches_count += 1
    if row_count != version.row_count:
        raise DeliveryError("REST_POPULATION_MISMATCH", "El conteo local no coincide con la versión publicada.")
    return {"status": "PASS", "checks": [
        {"code": "LOCAL_POPULATION", "status": "PASS", "message": "Mapping y población completos verificados localmente."},
        {"code": "NO_REMOTE_WRITES", "status": "PASS", "message": "Preflight no envía datos al endpoint REST."}],
        "source": {"dataset_id": dataset.id, "dataset_version_id": version.id, "rows": row_count,
                   "canonical_sha256": artifact.sha256},
        "destination": {"destination_id": destination.id, "destination_version_id": revision.id,
                        "sink_type": "REST", "idempotency_supported": settings.options.get("idempotency_supported", False)},
        "target": {**draft.target.model_dump(), "planned_batches": batches_count,
                   "transaction_scope": "PER_BATCH", "decimal_encoding": "EXACT_STRING"}, "system_audit": {}}


def batch_dto(batch):
    return {"id": batch.id, "run_id": batch.run_id, "delivery_attempt_id": batch.delivery_attempt_id,
        "ordinal": batch.ordinal, "payload_sha256": batch.payload_sha256, "payload_artifact_id": batch.payload_artifact_id,
        "rows": batch.row_count, "payload_bytes": batch.bytes_sent,
        "confirmed_payload_bytes": batch.bytes_sent if batch.status == "COMMITTED" else None,
        "attempts": batch.attempts, "status": batch.status,
        "response_status": batch.response_status, "response_sha256": batch.response_sha256,
        "error_code": batch.error_code, "confirmed_at": iso(batch.confirmed_at)}


def confirmed_response(target, status, body, batch):
    if status == 202:
        raise RestError("REST_ACCEPTED_PENDING", "HTTP 202 sólo confirma aceptación; el procesamiento del lote permanece incierto.", uncertain=True)
    try:
        document = exact_json(body)
        identity = json_path(document, target.batch_id_path)
        count = json_path(document, target.rows_path)
        accepted = json_path(document, target.confirmation_path)
        if identity != batch.id:
            raise ValueError()
        if target.payload_hash_path and json_path(document, target.payload_hash_path) != batch.payload_sha256:
            raise ValueError()
        if accepted is False and type(count) is Decimal and count == 0:
            raise RestError("REST_BATCH_REJECTED", "El receptor confirmó explícitamente que rechazó el lote sin procesar filas.")
        match_type = type(accepted) is type(target.confirmation_value) or (type(accepted) is Decimal and type(target.confirmation_value) is int)
        if (status not in {200, 201} or not match_type or accepted != target.confirmation_value
                or type(count) is not Decimal or count != batch.row_count):
            raise ValueError()
    except RestError as error:
        if error.code == "REST_BATCH_REJECTED":
            raise
        raise RestError("REST_CONFIRMATION_MISSING", "El receptor no confirmó identidad, conteo y resultado del lote.", uncertain=True) from None
    except ValueError:
        raise RestError("REST_CONFIRMATION_MISMATCH", "La respuesta no confirma íntegramente el lote enviado.", uncertain=True) from None


def reconcile_rest_batches(db, run, attempt):
    """Recover facts already confirmed per batch without any remote operation."""
    batches = db.scalars(select(RestDeliveryBatch).where(RestDeliveryBatch.run_id == run.id).order_by(RestDeliveryBatch.ordinal)).all()
    confirmed = [item for item in batches if item.status == "COMMITTED"]
    confirmed_rows = sum(item.row_count for item in confirmed)
    if (attempt.status == "STARTED" and all(item.status == "COMMITTED" for item in batches)
            and confirmed_rows == attempt.rows_attempted):
        revision = db.get(DeliveryDestinationVersion, attempt.destination_version_id)
        idempotent = bool(revision and revision.config.get("options", {}).get("idempotency_supported", False))
        attempt.status, attempt.finished_at = "COMMITTED", utcnow()
        attempt.rows_written = confirmed_rows
        attempt.bytes_sent = sum(item.bytes_sent for item in confirmed)
        run.metrics = {**run.metrics, "sink_type": "REST", "delivery_outcome": "SUCCESS",
            "delivery_attempt_id": attempt.id, "attempt_number": attempt.attempt_number,
            "rows_attempted": attempt.rows_attempted, "rows_written": confirmed_rows,
            "rows_inserted": None, "rows_updated": None, "bytes_sent": attempt.bytes_sent,
            "destination_id": run.execution_plan["destination_id"],
            "destination_version_id": run.execution_plan["destination_version_id"],
            "target": run.execution_plan["target"], "write_strategy": "REST_POST",
            "batch_count": len(batches), "confirmed_batches": len(confirmed),
            "transaction_scope": "PER_BATCH", "batches": [batch_dto(item) for item in batches]}
        run.metrics = {**run.metrics, "metric_semantics": rest_metric_semantics(), "idempotency_supported": idempotent}
    elif attempt.status in {"STARTED", "UNKNOWN"}:
        for item in batches:
            if item.status == "STARTED":
                item.status, item.error_code = "UNKNOWN", "REST_CONFIRMATION_LOST"
        attempt.rows_written, attempt.bytes_sent = confirmed_rows, sum(item.bytes_sent for item in confirmed)
        run.metrics = {**run.metrics, "sink_type": "REST", "rows_written": confirmed_rows,
            "batch_count": len(batches), "confirmed_batches": len(confirmed),
            "delivery_outcome": "UNKNOWN", "batches": [batch_dto(item) for item in batches]}


def execute_rest_delivery(db, run: Run, *, lease_owner=None):
    from .automation import AutomationError, authorize_automated_run, claim_delivery_target
    from .delivery_service import (
        DeliveryOperationError,
        _assert_dispatch_snapshot,
        _delivery_idempotency,
        _fence_delivery_job,
        _fence_delivery_result,
        _fence_delivery_start,
        _publish_delivery_evidence,
        _verified_frame,
        authorize_delivery_initiator,
        delivery_control,
    )
    from .services import audit
    if run.status in {"SUCCESS", "FAILED", "PARTIAL", "UNKNOWN", "CANCELLED", "FAILED_PRECONDITION"}:
        return
    config = require_record(db, Configuration, run.config_id)
    draft = DeliveryDraft.model_validate(config.config)
    control = delivery_control(run.id, lease_owner)
    prior = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.run_id == run.id).order_by(DeliveryAttempt.attempt_number.desc()))
    if prior:
        # No ambiguous batch is replayed, even with an advertised idempotency header.
        if prior.status == "COMMITTED":
            run.status, run.decision = "SUCCESS", "COMMITTED"
        else:
            prior.status = "UNKNOWN"
            prior.error_code = "REST_CONFIRMATION_LOST"
            prior.error_message = "Se perdió la confirmación durable de la entrega REST; requiere revisión externa."
            run.status, run.decision, run.error = "UNKNOWN", "UNKNOWN", prior.error_message
            for batch in db.scalars(select(RestDeliveryBatch).where(RestDeliveryBatch.run_id == run.id, RestDeliveryBatch.status == "STARTED")):
                batch.status, batch.error_code = "UNKNOWN", "REST_CONFIRMATION_LOST"
        run.finished_at = utcnow()
        db.commit()
        return
    staged = []
    try:
        _assert_dispatch_snapshot(db, run, draft)
        authorize_automated_run(db, run)
        authorize_delivery_initiator(db, run, draft)
        source, _, artifact, records, destination, revision, settings = _context(
            db, run.organization_id, draft, control=control, release_metadata=True)
        count = 0
        for identity, ordinal, rows, body in _payloads(records, draft, run.id, control):
            path = storage_provider.temporary_path(".json")
            if shutil.disk_usage(path.parent).free - len(body) < DeliveryLimits.configured().reserve_disk_bytes:
                raise DeliveryError("RESOURCE_DISK_INSUFFICIENT", "No queda reserva de disco para preparar el payload REST completo.")
            path.write_bytes(body)
            staged.append((identity, ordinal, len(rows), path, hashlib.sha256(body).hexdigest(), len(body)))
            count += len(rows)
        if count != source.row_count:
            raise DeliveryError("REST_POPULATION_MISMATCH", "La población preparada no coincide con la versión publicada.")
        _verified_frame(source, artifact, control)
        _fence_delivery_start(db, run, lease_owner)
        _assert_dispatch_snapshot(db, run, draft)
        authorize_automated_run(db, run)
        authorize_delivery_initiator(db, run, draft)
        claim_delivery_target(db, run)
        attempt = DeliveryAttempt(organization_id=run.organization_id, run_id=run.id,
            destination_version_id=revision.id, attempt_number=1, idempotency_key=_delivery_idempotency(run, config),
            status="STARTED", target_locator=settings.options["url"][:300], rows_attempted=source.row_count)
        db.add(attempt)
        db.flush()
        batches = []
        for identity, ordinal, rows, path, digest, size in staged:
            payload_artifact = storage_provider.put_file(db, path, "REST_DELIVERY_PAYLOAD", run.organization_id,
                name=f"rest-batch-{ordinal}.json", media_type="application/json")
            batch = RestDeliveryBatch(id=identity, organization_id=run.organization_id, run_id=run.id,
                delivery_attempt_id=attempt.id, ordinal=ordinal, payload_sha256=digest,
                payload_artifact_id=payload_artifact.id, row_count=rows, bytes_sent=size, status="PREPARED", attempts=0)
            db.add(batch)
            batches.append(batch)
            link_artifact(db, run.organization_id, "REST_BATCH_PAYLOAD", "RUN", run.id, "ARTIFACT", payload_artifact.id)
        run.status, run.started_at, run.progress_stage = "RUNNING", run.started_at or utcnow(), "Preparación REST completa"
        audit(db, "DELIVERY_STARTED", "delivery_attempt", attempt.id, "Lotes REST preparados; sin transacción global", "Delivery Worker", run.organization_id,
              {"batch_count": len(batches), "rows": count}, run_id=run.id)
        db.commit()
    except (DeliveryError, DeliveryOperationError, AutomationError, RestError) as error:
        if error.code == "WORKER_LEASE_LOST":
            raise
        _fence_delivery_job(db, run, lease_owner)
        run.status = "CANCELLED" if error.code == "RUN_CANCELLED" else "FAILED_PRECONDITION"
        run.decision, run.error, run.finished_at = "FAILED", error.message, utcnow()
        db.commit()
        return
    finally:
        for _, _, _, path, _, _ in staged:
            Path(path).unlink(missing_ok=True)
    confirmed_rows = sent_bytes = 0
    deadline = time.monotonic() + 1800
    for batch in batches:
        try:
            control()
            # The scan checkpoint is throttled. Every batch needs a fresh
            # cancellation fence serialized with the API before its STARTED
            # marker, even when a small delivery finishes within 500 ms.
            locked = db.scalar(select(Run).where(Run.id == run.id).with_for_update().execution_options(populate_existing=True))
            if locked is None or locked.cancel_requested or locked.status == "CANCELLED":
                raise DeliveryOperationError(409, "RUN_CANCELLED", "La entrega REST fue cancelada antes de iniciar el siguiente lote.")
            _fence_delivery_job(db, run, lease_owner)
            _assert_dispatch_snapshot(db, run, draft)
            authorize_automated_run(db, run)
            authorize_delivery_initiator(db, run, draft)
            payload_artifact = require_record(db, Artifact, batch.payload_artifact_id)
            body = storage_provider.materialize(payload_artifact).read_bytes()
            if hashlib.sha256(body).hexdigest() != batch.payload_sha256:
                raise DeliveryError("REST_PAYLOAD_INTEGRITY", "El payload REST no conserva su hash preparado.")
            batch.status, batch.attempts = "STARTED", batch.attempts + 1
            db.commit()  # Durable identity/hash/payload precede the remote POST.
            headers = {"X-Trackvance-Batch-Id": batch.id, "X-Trackvance-Payload-SHA256": batch.payload_sha256}
            if settings.options.get("idempotency_supported", False):
                headers[settings.options.get("idempotency_header", "Idempotency-Key")] = batch.id
            status, response = request(settings.options, settings.password, "POST", payload=body, headers=headers,
                                       max_bytes=65536, deadline=deadline)
            _fence_delivery_result(db, run, attempt, lease_owner)
            batch.response_status, batch.response_sha256 = status, hashlib.sha256(response).hexdigest()
            confirmed_response(draft.target, status, response, batch)
            batch.status, batch.confirmed_at = "COMMITTED", utcnow()
            confirmed_rows += batch.row_count
            sent_bytes += batch.bytes_sent
            attempt.rows_written, attempt.bytes_sent = confirmed_rows, sent_bytes
            run.progress_percent = min(90, int(90 * confirmed_rows / max(1, source.row_count)))
            db.commit()
        except (RestError, DeliveryError, DeliveryOperationError, AutomationError) as error:
            if error.code == "WORKER_LEASE_LOST":
                raise
            _fence_delivery_job(db, run, lease_owner)
            uncertain = isinstance(error, RestError) and error.uncertain
            if batch.status == "STARTED" and not isinstance(error, RestError):
                uncertain = True
            batch.status, batch.error_code = "UNKNOWN" if uncertain else "FAILED", error.code
            attempt.status, attempt.error_code, attempt.error_message = "UNKNOWN" if uncertain else "FAILED", error.code, error.message
            run.status = "UNKNOWN" if uncertain else "PARTIAL" if confirmed_rows else "CANCELLED" if error.code == "RUN_CANCELLED" else "FAILED"
            run.decision, run.error = run.status, error.message
            break
    else:
        attempt.status, run.status, run.decision, run.error = "COMMITTED", "SUCCESS", "COMMITTED", None
    attempt.rows_written, attempt.bytes_sent, attempt.finished_at = confirmed_rows, sent_bytes, utcnow()
    run.finished_at = utcnow()
    run.metrics = {"sink_type": "REST", "delivery_outcome": run.status, "rows_attempted": source.row_count,
        "rows_written": confirmed_rows, "bytes_sent": sent_bytes, "rows_inserted": None, "rows_updated": None,
        "delivery_attempt_id": attempt.id, "attempt_number": 1, "batch_count": len(batches),
        "confirmed_batches": sum(b.status == "COMMITTED" for b in batches), "transaction_scope": "PER_BATCH",
        "idempotency_supported": settings.options.get("idempotency_supported", False),
        "destination_id": destination.id, "destination_version_id": revision.id,
        "target": draft.target.model_dump(), "write_strategy": draft.write_strategy,
        "batches": [batch_dto(batch) for batch in batches], "metric_semantics": rest_metric_semantics()}
    run.progress_stage = "Entrega REST completa" if run.status == "SUCCESS" else "Entrega REST parcial" if run.status == "PARTIAL" else "Entrega REST requiere revisión" if run.status == "UNKNOWN" else "Entrega REST detenida"
    audit(db, "DELIVERY_COMMITTED" if run.status == "SUCCESS" else "DELIVERY_UNKNOWN" if run.status == "UNKNOWN" else "DELIVERY_FAILED",
          "delivery_attempt", attempt.id, run.progress_stage, "Delivery Worker", run.organization_id,
          {"status": run.status, "confirmed_rows": confirmed_rows, "batch_count": len(batches)}, run_id=run.id)
    db.commit()
    if run.status == "SUCCESS":
        try:
            receipt, evidence = _publish_delivery_evidence(db, run, config, source, artifact, destination, revision, draft, attempt)
            run.evidence_path, run.progress_percent = evidence.path, 100
            run.metrics = {**run.metrics, "receipt_artifact_id": receipt.id}
            db.commit()
        except Exception:  # noqa: BLE001 - confirmed remote batches must survive evidence failure.
            db.rollback()
            run.metrics = {**run.metrics, "evidence_status": "PENDING_REPAIR"}
            db.commit()
