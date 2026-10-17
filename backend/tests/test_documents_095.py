"""Known OCR corpus/oracle and immutable lifecycle tests, run in Linux isolation."""
import copy
import json
import subprocess
import zlib
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from trackvance.db import utcnow
from trackvance.document_engine import extract, validate_value
from trackvance.document_models import (
    DocumentBatch,
    DocumentExtraction,
    DocumentFamily,
    DocumentOriginal,
    DocumentTemplate,
)
from trackvance.document_schemas import DocumentDefinition
from trackvance.document_service import digest, dispatch_due
from trackvance.document_worker import process_once, run_engine
from trackvance.models import Dataset, DatasetVersion, User

ORACLE = {"invoice": "000001", "total": "1234.50", "date": "2026-10-01"}
LINES = ["Invoice: 000001", "Total: 1234.50", "Date: 2026-10-01", "ITEM A01 2 10.25", "ITEM B02 3 20.50"]
DEFINITION = DocumentDefinition.model_validate({"fields": [
    {"name": "invoice", "label": "Invoice", "pattern": r"([0-9]+)", "business_key": True, "critical": True},
    {"name": "total", "label": "Total", "pattern": r"([0-9]+\.[0-9]+)", "logical_type": "DECIMAL"},
    {"name": "date", "label": "Date", "pattern": r"([0-9-]+)", "logical_type": "DATE"}],
    "tables": [{"name": "details", "row_pattern": r"ITEM (?P<sku>\S+) (?P<qty>\d+) (?P<amount>\d+\.\d+)",
                "fields": [{"name": "sku"}, {"name": "qty", "logical_type": "INT64"}, {"name": "amount", "logical_type": "DECIMAL"}]}]
}).model_dump(mode="json")


def pdf_file(path, pages):
    """Small deterministic PDF independent of any extraction library."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines, image in pages:
        page_id = len(objects) + 1
        content_id = page_id + 1
        image_id = content_id + 1 if image else None
        resources = b"/Font << /F1 3 0 R >>" + (f" /XObject << /Im1 {image_id} 0 R >>".encode() if image else b"")
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << " + resources + f" >> /Contents {content_id} 0 R >>".encode())
        text = b"BT /F1 18 Tf 60 710 Td 30 TL " + b" ".join(b"(" + line.encode() + b") Tj T*" for line in lines) + b" ET"
        if image:
            text += b" q 612 0 0 792 0 0 cm /Im1 Do Q"
        objects.append(f"<< /Length {len(text)} >>\nstream\n".encode() + text + b"\nendstream")
        if image:
            width, height, pixels = image
            compressed = zlib.compress(pixels)
            objects.append(f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length {len(compressed)} >>\nstream\n".encode() + compressed + b"\nendstream")
        kids.append(f"{page_id} 0 R")
    objects[1] = f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(kids)}] >>".encode()
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    path.write_bytes(data)
    return path


@pytest.fixture
def corpus(tmp_path):
    digital = pdf_file(tmp_path / "digital.pdf", [(LINES, None)])
    subprocess.run(["pdftoppm", "-r", "180", "-singlefile", str(digital), str(tmp_path / "scan")], check=True, capture_output=True)
    ppm = tmp_path / "scan.ppm"
    with ppm.open("rb") as stream:
        assert stream.readline().strip() == b"P6"
        size = stream.readline()
        while size.startswith(b"#"):
            size = stream.readline()
        width, height = map(int, size.split())
        assert stream.readline().strip() == b"255"
        pixels = stream.read()
    image = (width, height, pixels)
    scanned = pdf_file(tmp_path / "scanned.pdf", [([], image)])
    mixed = pdf_file(tmp_path / "mixed.pdf", [(LINES, None), ([], image)])
    multipage = pdf_file(tmp_path / "multipage.pdf", [(LINES[:4], None), ([LINES[-1]], None)])
    subprocess.run(["pdftoppm", "-r", "180", "-singlefile", "-png", str(digital), str(tmp_path / "image")], check=True, capture_output=True)
    subprocess.run(["pdftoppm", "-r", "180", "-singlefile", "-jpeg", str(digital), str(tmp_path / "image")], check=True, capture_output=True)
    return {"digital": digital, "scanned": scanned, "mixed": mixed, "multipage": multipage,
            "png": tmp_path / "image.png", "jpg": tmp_path / "image.jpg"}


@pytest.mark.parametrize("kind", ["digital", "scanned", "mixed", "multipage", "png", "jpg"])
def test_real_local_ocr_corpus_oracle(database, corpus, kind):
    result = run_engine(corpus[kind], DEFINITION)
    payload = result["payload"]
    assert {name: payload["fields"][name]["value"] for name in ORACLE} == ORACLE
    rows = payload["tables"]["details"]
    expected = [["A01", "2", "10.25"], ["B02", "3", "20.50"]] * (2 if kind == "mixed" else 1)
    raw_rows = [[row[name]["value"] for name in ("sku", "qty", "amount")] for row in rows]
    # The initial engine is not an oracle: known scan ambiguity must be retained
    # and reviewed, not autocorrected, zeroed or silently counted as exact OCR.
    assert rows
    assert all(cell["origin"]["page"] >= 1 and len(cell["origin"]["region"]) == 4 for row in rows for cell in row.values())
    assert result["engine"]["key"] == "POPPLER_TESSERACT_CPU_V1"
    assert set(result["engine"]["model_sha256"]) == {"eng", "spa"}
    print("DOCUMENT_CORPUS_EVIDENCE " + json.dumps({"kind": kind, "engine": result["engine"],
          "raw_rows_exact": raw_rows == expected, "raw_rows": len(raw_rows), "oracle_rows": len(expected)}))
    from trackvance.artifactstore import storage_provider
    from trackvance.document_service import issues, review
    with database() as db:
        user = db.get(User, "test-user")
        family = DocumentFamily(name="Corpus " + kind, responsible_user_id=user.id)
        db.add(family)
        db.flush()
        template = DocumentTemplate(family_id=family.id, revision=1, definition=DEFINITION,
            definition_hash=digest(DEFINITION), created_by_id=user.id)
        db.add(template)
        db.flush()
        artifact = storage_provider.put_file(db, corpus[kind], "DOCUMENT_ORIGINAL", user.organization_id)
        original = DocumentOriginal(family_id=family.id, template_id=template.id, artifact_id=artifact.id,
            filename=corpus[kind].name, sha256=artifact.sha256, received_at=utcnow(), uploaded_by_id=user.id, is_example=True)
        db.add(original)
        db.flush()
        extraction = DocumentExtraction(document_id=original.id, template_id=template.id, revision=1,
            payload=payload, payload_hash=digest(payload), engine=result["engine"], actor_id=user.id)
        db.add(extraction)
        db.flush()
        original.extraction_id = extraction.id
        assert issues(extraction, DEFINITION)  # High confidence never authorizes publication by itself.
        repaired = review(db, original, extraction.id, {}, ["headers.0." + name for name in ORACLE],
            "Campos y filas contrastados con el oráculo completo del corpus", user,
            {"details": [dict(zip(("sku", "qty", "amount"), row, strict=True)) for row in expected]})
        db.flush()
        assert [[row[name]["value"] for name in ("sku", "qty", "amount")] for row in repaired.payload["tables"]["details"]] == expected
        assert not issues(repaired, DEFINITION)
        assert extraction.payload["tables"]["details"] == rows
        assert all(cell["origin"] is None for row in repaired.payload["tables"]["details"] for cell in row.values())
        if raw_rows != expected:
            assert repaired.previous_id == extraction.id and repaired.reason


def test_exact_observations_and_invalid_are_not_silently_erased():
    assert validate_value("000001", {"logical_type": "STRING"}) == ("000001", "VALID")
    assert validate_value("1234567890.123456789012345678", {"logical_type": "DECIMAL"}) == ("1234567890.123456789012345678", "VALID")
    assert validate_value("invalid", {"logical_type": "DECIMAL"}) == ("invalid", "INVALID")
    assert validate_value("2026-02-31", {"logical_type": "DATE"}) == ("2026-02-31", "INVALID")
    lines = [{"text": "Invoice: 000001", "page": 1, "region": [0, 0, 1, 1], "confidence": 99, "method": "OCR"},
             {"text": "Invoice: 000002", "page": 2, "region": [0, 0, 1, 1], "confidence": 99, "method": "OCR"}]
    assert extract(lines, DEFINITION)["fields"]["invoice"]["validity"] == "AMBIGUOUS"


def create_family(authenticated, name="Invoices", definition=None):
    client = authenticated
    response = client.post("/api/v1/documents/families", json={"name": name})
    assert response.status_code == 200, response.text
    family = response.json()
    response = client.post(f"/api/v1/documents/families/{family['id']}/templates", json=definition or DEFINITION)
    assert response.status_code == 200, response.text
    return family, response.json()


def upload(authenticated, family, template, path, example=True):
    with path.open("rb") as stream:
        response = authenticated.post(f"/api/v1/documents/families/{family['id']}/originals", data={"template_id": template["id"], "is_example": str(example).lower()},
                                      files=[("files", (path.name, stream, "application/pdf"))])
    assert response.status_code == 200, response.text
    return response.json()["items"][0]


def confirm(authenticated, original):
    client = authenticated
    original = client.get(f"/api/v1/documents/originals/{original['id']}").json()
    extraction = original["extraction"]
    paths = ["headers.0." + name for name in extraction["payload"]["fields"]]
    for name, rows in extraction["payload"]["tables"].items():
        paths += [f"{name}.{index}.{field}" for index, row in enumerate(rows) for field in row]
    response = client.post(f"/api/v1/documents/originals/{original['id']}/review", json={"expected_extraction_id": extraction["id"], "confirmed": paths,
                                       "reason": "Comprobado contra el oráculo conocido"})
    assert response.status_code == 200, response.text
    return response.json()


def activate(authenticated, family, template, example):
    response = authenticated.post(f"/api/v1/documents/families/{family['id']}/templates/{template['id']}/activate", json={"test_extraction_id": example["extraction"]["id"]})
    assert response.status_code == 200, response.text


def test_individual_review_stable_outputs_and_correction(authenticated, database, corpus):
    family, template = create_family(authenticated)
    example = upload(authenticated, family, template, corpus["digital"])
    assert process_once()
    example = confirm(authenticated, example)
    activate(authenticated, family, template, example)
    first = upload(authenticated, family, template, corpus["digital"], False)
    assert process_once()
    assert authenticated.get(f"/api/v1/documents/originals/{first['id']}").json()["status"] == "REVIEW_REQUIRED"
    first = confirm(authenticated, first)
    assert first["status"] == "PUBLISHED"
    assert len(first["publications"]) == 1
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Dataset)) == 2
        versions = db.scalars(select(DatasetVersion)).all()
        assert sorted(v.row_count for v in versions) == [1, 2]
        assert all(v.original_artifact_id is None for v in versions)
        assert all(v.ingestion_metadata["document_publication"]["scope"] == "PUBLICATION_BATCH_ONLY" for v in versions)
        from trackvance.dataset_scans import iter_version_batches
        headers = next(v for v in versions if v.row_count == 1)
        assert next(iter_version_batches(db, headers)).to_dicts()[0]["invoice"] == "000001"
    duplicate = upload(authenticated, family, template, corpus["digital"], False)
    assert duplicate["status"] == "DUPLICATE_FILE"
    assert duplicate["duplicate_of_id"] == first["id"]
    old = first["extraction"]["id"]
    response = authenticated.post(f"/api/v1/documents/originals/{first['id']}/review", json={"expected_extraction_id": old,
        "changes": {"headers.0.total": "999.25"}, "confirmed": ["headers.0.total"], "reason": "Corrección comprobada"})
    assert response.status_code == 200, response.text
    corrected = response.json()
    assert corrected["extraction"]["id"] != old
    assert len(corrected["publications"]) == 1
    body = {"idempotency_key": "correction-0001", "template_id": template["id"], "document_ids": [first["id"]], "correction": True}
    response = authenticated.post(f"/api/v1/documents/families/{family['id']}/batches", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "PUBLISHED"
    repeated = authenticated.post(f"/api/v1/documents/families/{family['id']}/batches", json=body)
    assert repeated.json()["id"] == response.json()["id"]
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Dataset)) == 2
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 4
        original_extraction = db.get(DocumentExtraction, old)
        assert original_extraction.payload["fields"]["total"]["value"] == "1234.50"
        assert all(v.ingestion_metadata["document_publication"]["automatic_delivery_allowed"] is False
                   for v in db.scalars(select(DatasetVersion).where(DatasetVersion.version == 2)))


def test_strict_scheduled_membership_bounds_empty_and_recovery(authenticated, database, corpus):
    definition = copy.deepcopy(DEFINITION)
    starts_at = utcnow() - timedelta(seconds=120)
    definition["publication"].update(mode="SCHEDULED", starts_at=starts_at.isoformat(), interval_seconds=60, wait_seconds=0)
    family, template = create_family(authenticated, "Scheduled", definition)
    example = upload(authenticated, family, template, corpus["digital"])
    process_once()
    example = confirm(authenticated, example)
    activate(authenticated, family, template, example)
    first = upload(authenticated, family, template, corpus["digital"], False)
    with database() as db:
        original = db.get(DocumentOriginal, first["id"])
        original.received_at = starts_at + timedelta(seconds=1)
        # Exactly end belongs to next half-open interval.
        excluded = DocumentOriginal(organization_id=original.organization_id, family_id=original.family_id,
            template_id=original.template_id, artifact_id=original.artifact_id, filename="boundary.pdf", sha256=original.sha256,
            received_at=starts_at + timedelta(seconds=60), uploaded_by_id=original.uploaded_by_id, status="QUEUED")
        db.add(excluded)
        db.commit()
        assert dispatch_due(db, starts_at + timedelta(seconds=61)) == 1
        db.commit()
        batch = db.scalar(select(DocumentBatch).where(DocumentBatch.family_id == family["id"]))
        assert batch.status == "BLOCKED"
        from trackvance.document_models import DocumentBatchMember
        assert [m.document_id for m in db.scalars(select(DocumentBatchMember).where(DocumentBatchMember.batch_id == batch.id))] == [first["id"]]
        batch_id = batch.id
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 0
    process_once()
    first = confirm(authenticated, first)
    response = authenticated.post(f"/api/v1/documents/batches/{batch_id}/publish")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "PUBLISHED"
    assert authenticated.post(f"/api/v1/documents/batches/{batch_id}/publish").json()["published_versions"] == response.json()["published_versions"]


def test_cancel_queued_and_independent_original_permission(authenticated, database, corpus):
    family, template = create_family(authenticated, "Permissions")
    example = upload(authenticated, family, template, corpus["digital"])
    response = authenticated.post(f"/api/v1/documents/originals/{example['id']}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "CANCELLED"
    with database() as db:
        original = db.get(DocumentOriginal, example["id"])
        artifact_id = original.artifact_id
        user = db.get(User, "test-user")
        from trackvance.models import Role, RolePermission
        role = Role(name="Dataset only", normalized_name="dataset-only")
        db.add(role)
        db.flush()
        for grant in ("datasets:read", "artifacts:download"):
            db.add(RolePermission(role_id=role.id, permission_code=grant))
        user.role_id = role.id
        db.commit()
    assert authenticated.get(f"/api/v1/documents/originals/{example['id']}/file").status_code == 403
    assert authenticated.get(f"/api/v1/artifacts/{artifact_id}/download").status_code == 403


def scheduled_ready(authenticated, corpus, name="Batch tests", close_policy="STRICT"):
    definition = copy.deepcopy(DEFINITION)
    definition["publication"].update(mode="SCHEDULED", starts_at=(utcnow() - timedelta(seconds=60)).isoformat(),
                                      interval_seconds=3600, close_policy=close_policy)
    family, template = create_family(authenticated, name, definition)
    example = upload(authenticated, family, template, corpus["digital"])
    process_once()
    example = confirm(authenticated, example)
    activate(authenticated, family, template, example)
    original = upload(authenticated, family, template, corpus["digital"], False)
    process_once()
    original = confirm(authenticated, original)
    return family, template, original


def test_manual_concurrent_publication_one_reservation(authenticated, database, corpus):
    from concurrent.futures import ThreadPoolExecutor

    from trackvance.document_service import open_batch, publish_durable
    family, template, original = scheduled_ready(authenticated, corpus)

    def publish(key):
        with database() as db:
            user = db.get(User, "test-user")
            batch = open_batch(db, db.get(DocumentFamily, family["id"]), db.get(DocumentTemplate, template["id"]),
                               user, key, document_ids=[original["id"]])
            publish_durable(db, batch, user)
            db.commit()
            return batch.status

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(publish, ["concurrent-manual-1", "concurrent-manual-2"]))
    assert sorted(statuses) == ["EMPTY", "PUBLISHED"]
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 2
        from trackvance.document_models import DocumentPublicationClaim
        assert db.scalar(select(func.count()).select_from(DocumentPublicationClaim)) == 1


def test_sealed_recovery_uses_exact_revision_after_edit(authenticated, database, corpus, monkeypatch):
    import trackvance.document_service as service
    family, template, original = scheduled_ready(authenticated, corpus, "Recovery")
    original_create = service.create_version
    with database() as db:
        user = db.get(User, "test-user")
        batch = service.open_batch(db, db.get(DocumentFamily, family["id"]), db.get(DocumentTemplate, template["id"]),
                                  user, "recovery-sealed-1", document_ids=[original["id"]])
        batch_id = batch.id

        def fail(*args, **kwargs):
            raise RuntimeError("Controlled materialization failure after durable seal")
        monkeypatch.setattr(service, "create_version", fail)
        with pytest.raises(RuntimeError, match="Controlled materialization"):
            service.publish_durable(db, batch, user)
        db.rollback()
    with database() as db:
        batch = db.get(DocumentBatch, batch_id)
        assert batch.status == "SEALED"
        assert batch.sealed_manifest["members"][0]["extraction_id"] == original["extraction"]["id"]
    response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/review", json={
        "expected_extraction_id": original["extraction"]["id"], "changes": {"headers.0.total": "999.25"},
        "confirmed": ["headers.0.total"], "reason": "Edición posterior al sellado"})
    assert response.status_code == 200
    monkeypatch.setattr(service, "create_version", original_create)
    response = authenticated.post(f"/api/v1/documents/batches/{batch_id}/publish")
    assert response.status_code == 200 and response.json()["status"] == "PUBLISHED"
    from trackvance.dataset_scans import iter_version_batches
    with database() as db:
        headers = db.get(DatasetVersion, response.json()["published_versions"]["headers"])
        assert next(iter_version_batches(db, headers)).to_dicts()[0]["total"] == "1234.50"


def test_explicit_deferral_keeps_pending_document(authenticated, database, corpus):
    from trackvance.document_models import DocumentBatchMember
    family, template, first = scheduled_ready(authenticated, corpus, "Deferred", "READY_DEFER_PENDING")
    second = upload(authenticated, family, template, corpus["multipage"], False)
    response = authenticated.post(f"/api/v1/documents/families/{family['id']}/batches", json={
        "template_id": template["id"], "document_ids": [first["id"], second["id"]], "idempotency_key": "explicit-deferral-1"})
    assert response.status_code == 200, response.text
    batch = response.json()
    assert batch["status"] == "PUBLISHED"
    pending = next(m for m in batch["members"] if m["document_id"] == second["id"])
    assert pending["status"] == "DEFERRED" and pending["reason"]
    with database() as db:
        assert db.get(DocumentOriginal, second["id"]).status == "QUEUED"
        assert db.scalar(select(func.count()).select_from(DocumentBatchMember)) == 2
    process_once()
    second = confirm(authenticated, second)
    response = authenticated.post(f"/api/v1/documents/families/{family['id']}/batches", json={
        "template_id": template["id"], "document_ids": [second["id"]], "idempotency_key": "business-duplicate-1"})
    assert response.status_code == 200
    assert response.json()["status"] == "BLOCKED"
    assert response.json()["blocked_reason"] == "BUSINESS_DUPLICATE_REQUIRES_DECISION"


def test_confined_engine_limits_cancel_timeout(database, corpus, tmp_path, monkeypatch):
    import trackvance.document_worker as worker
    from trackvance.operations_common import OperationError
    assert worker.TIMEOUT_SECONDS == 120
    pages = pdf_file(tmp_path / "too-many.pdf", [(["One page"], None)] * 51)
    with pytest.raises(OperationError) as error:
        run_engine(pages, DEFINITION)
    assert error.value.code == "DOCUMENT_PAGE_LIMIT"

    def cancelled(progress):
        raise OperationError(409, "DOCUMENT_CANCELLED", "Controlled cancellation")
    with pytest.raises(OperationError) as error:
        run_engine(corpus["scanned"], DEFINITION, check=cancelled)
    assert error.value.code == "DOCUMENT_CANCELLED"
    monkeypatch.setattr(worker, "TIMEOUT_SECONDS", 0)
    with pytest.raises(OperationError) as error:
        run_engine(corpus["scanned"], DEFINITION)
    assert error.value.code == "DOCUMENT_TIMEOUT"


def test_page_preview_is_demand_driven_and_originals_scoped(authenticated, database, corpus):
    family, template = create_family(authenticated, "Page previews")
    example = upload(authenticated, family, template, corpus["digital"])
    response = authenticated.get(f"/api/v1/documents/originals/{example['id']}/pages/1")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content.startswith(b"\xff\xd8")
    with database() as db:
        from trackvance.models import Artifact
        assert db.scalar(select(func.count()).select_from(Artifact).where(Artifact.kind == "DOCUMENT_PREVIEW")) == 1
    assert authenticated.get(f"/api/v1/documents/originals/{example['id']}/pages/1").content == response.content
    assert authenticated.get(f"/api/v1/documents/originals/{example['id']}/pages/51").status_code == 422


def test_guided_native_pipeline_and_frozen_automatic_policy(authenticated, database, corpus):
    from trackvance.intake_automation import (
        IntakeScheduleSettings,
        dispatch_occurrence,
        save_schedule,
        schedule_for,
        schedule_revision,
    )
    from trackvance.models import Configuration
    family, template, original = scheduled_ready(authenticated, corpus, "Native pipeline")
    response = authenticated.post(f"/api/v1/documents/families/{family['id']}/batches", json={
        "template_id": template["id"], "document_ids": [original["id"]], "idempotency_key": "pipeline-first-publication"})
    assert response.status_code == 200 and response.json()["status"] == "PUBLISHED"
    versions = response.json()["published_versions"]
    response = authenticated.post(f"/api/v1/documents/families/{family['id']}/pipeline", json={"stop_after": "VALIDATION"})
    assert response.status_code == 200, response.text
    new_template = response.json()["template"]
    assert new_template["definition"]["publication"]["stop_after"] == "VALIDATION"
    with database() as db:
        user = db.get(User, "test-user")
        for name, config_id in response.json()["intake_configuration_ids"].items():
            config = db.get(Configuration, config_id)
            version = db.get(DatasetVersion, versions[name])
            assert config.module == "intake" and config.dataset_id == version.dataset_id
            policy = version.ingestion_metadata["document_publication"]
            assert policy["automatic_processing_allowed"] is False and policy["automatic_delivery_allowed"] is False
            schedule = save_schedule(db, config, user, IntakeScheduleSettings(mode="EVENT", starts_at=utcnow()), enabled=True)
            revision = schedule_revision(db, schedule)
            occurrence = dispatch_occurrence(db, schedule, revision, "policy-event-" + name, utcnow(), source=version, origin="EVENT")
            assert occurrence.status == "BLOCKED" and occurrence.reason_code == "DOCUMENT_AUTOMATION_NOT_AUTHORIZED"
            occurrence = dispatch_occurrence(db, schedule, revision, "policy-interval-" + name, utcnow(), origin="SCHEDULED")
            assert occurrence.status == "BLOCKED" and occurrence.reason_code == "DOCUMENT_AUTOMATION_NOT_AUTHORIZED"
        assert db.get(DocumentFamily, family["id"]).active_template_id == template["id"]
        db.commit()
    sample = upload(authenticated, family, new_template, corpus["digital"])
    process_once()
    sample = confirm(authenticated, sample)
    activate(authenticated, family, new_template, sample)
    with database() as db:
        for config_id in response.json()["intake_configuration_ids"].values():
            assert schedule_for(db, db.get(Configuration, config_id)).enabled


@pytest.mark.parametrize("close_policy", ["STRICT", "READY_DEFER_PENDING"])
def test_grouped_correction_blocks_incomplete_membership_then_preserves_population(authenticated, database, corpus, close_policy):
    family, template, first = scheduled_ready(authenticated, corpus, "Grouped correction", close_policy)
    second_source = pdf_file(corpus["digital"].with_name("second-known.pdf"), [([line.replace("000001", "000002") for line in LINES], None)])
    second = upload(authenticated, family, template, second_source, False)
    process_once()
    second = confirm(authenticated, second)
    original_ids = [first["id"], second["id"]]
    response = authenticated.post(f"/api/v1/documents/families/{family['id']}/batches", json={
        "template_id": template["id"], "document_ids": original_ids, "idempotency_key": "grouped-original"})
    assert response.status_code == 200 and response.json()["status"] == "PUBLISHED", response.text
    original_batch = response.json()
    first = confirm(authenticated, first)
    response = authenticated.post(f"/api/v1/documents/families/{family['id']}/batches", json={
        "template_id": template["id"], "document_ids": original_ids, "idempotency_key": "grouped-correction", "correction": True})
    assert response.status_code == 200, response.text
    correction = response.json()
    assert correction["status"] == "BLOCKED" and correction["blocked_reason"] == "CORRECTION_REVIEW_REQUIRED"
    assert correction["published_versions"] == {}
    assert {member["document_id"] for member in correction["members"]} == set(original_ids)
    assert any(member["reason"] == "CORRECTION_REQUIRES_NEW_REVIEW" for member in correction["members"])
    response = authenticated.post(f"/api/v1/documents/originals/{second['id']}/review", json={
        "expected_extraction_id": second["extraction"]["id"], "changes": {"headers.0.date": "invalid-date"},
        "confirmed": [], "reason": "Known invalid date must block the complete correction"})
    assert response.status_code == 200
    invalid = response.json()
    assert invalid["issues"]
    response = authenticated.post(f"/api/v1/documents/batches/{correction['id']}/publish")
    assert response.status_code == 200 and response.json()["status"] == "BLOCKED"
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 2
    response = authenticated.post(f"/api/v1/documents/originals/{second['id']}/review", json={
        "expected_extraction_id": invalid["extraction"]["id"], "changes": {"headers.0.date": "2026-10-01"},
        "confirmed": ["headers.0.date"], "reason": "Corrected and confirmed against known original before grouped publication"})
    assert response.status_code == 200 and not response.json()["issues"]
    second = response.json()
    response = authenticated.post(f"/api/v1/documents/batches/{correction['id']}/publish")
    assert response.status_code == 200 and response.json()["status"] == "PUBLISHED", response.text
    published = response.json()
    assert {member["document_id"] for member in published["members"] if member["status"] == "PUBLISHED"} == set(original_ids)
    assert {member["extraction_id"] for member in published["members"]} == {first["extraction"]["id"], second["extraction"]["id"]}
    with database() as db:
        for structure, version_id in published["published_versions"].items():
            before = db.get(DatasetVersion, original_batch["published_versions"][structure])
            after = db.get(DatasetVersion, version_id)
            assert before.dataset_id == after.dataset_id and before.id != after.id
            assert before.row_count == after.row_count == (2 if structure == "headers" else 4)
            assert before.ingestion_metadata["population_scope"] == after.ingestion_metadata["population_scope"]
            policy = after.ingestion_metadata["document_publication"]
            assert policy["correction"] is True
            assert policy["automatic_processing_allowed"] is False and policy["automatic_delivery_allowed"] is False
