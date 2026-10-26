"""Immutable recognition journals through the existing organization storage port.

The worker owns lease checks and transaction commits. Inference services never
receive this module, a database connection, or application storage credentials.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from collections import defaultdict
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from .artifactstore import link_artifact, storage_provider
from .models import Artifact, ArtifactLink
from .operations_common import OperationError

EVENT_RELATION = "DOCUMENT_RECOGNITION_EVENT"
RAW_RELATION = "DOCUMENT_RECOGNITION_RAW"
EVENT_KIND = "DOCUMENT_RECOGNITION_EVENT"
MAX_ARTIFACT_BYTES = 16 * 1024**2
MAX_PAGE_BYTES = 32 * 1024**2
MAX_EVENTS = 1000
SHA = re.compile(r"[0-9a-f]{64}")
IDENTITY = re.compile(r"[A-Za-z0-9_.:-]{1,120}")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _error():
    return OperationError(422, "DOCUMENT_RECOGNITION_INTEGRITY", "El resultado original no corresponde al intento y fuente autorizados.")


def _identity(*parts):
    return uuid5(NAMESPACE_URL, "trackvance:document-recognition:" + ":".join(map(str, parts))).hex


def _subject_type(subject):
    from .document_models import DocumentReference

    return "DOCUMENT_REFERENCE" if isinstance(subject, DocumentReference) else "DOCUMENT"


def _put(db, original, data, kind, identity, name, media_type):
    temporary = storage_provider.temporary_path(".recognition")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            import os
            os.fsync(stream.fileno())
        return storage_provider.put_file(db, temporary, kind, original.organization_id, name,
            artifact_id=identity, media_type=media_type)
    finally:
        temporary.unlink(missing_ok=True)


def persist_recognition_event(db, original, event, *, attempt_id, configuration_sha256,
                              job_id, worker_attempt_number):
    """Validate raw bytes independently and promote before the caller adapts them."""
    try:
        if (not isinstance(event, dict) or event.get("version") != 1
                or event.get("kind") not in {"PHASE", "PAGE", "TERMINAL"}
                or event.get("source_sha256") != original.sha256
                or event.get("configuration_sha256") != configuration_sha256
                or event.get("attempt_id") != attempt_id
                or not IDENTITY.fullmatch(str(event.get("request_id", "")))
                or type(event.get("sequence")) is not int or not 0 <= event["sequence"] <= MAX_EVENTS
                or not isinstance(event.get("stage"), str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", event["stage"])
                or not isinstance(event.get("payload"), dict) or "durable_refs" in event
                or not SHA.fullmatch(configuration_sha256)):
            raise ValueError()
        if len(canonical(event)) > MAX_PAGE_BYTES * 2:
            raise ValueError()
        event_hash = hashlib.sha256(canonical({key: value for key, value in event.items()
            if key not in {"event_sha256", "durable_refs"}})).hexdigest()
        if event.get("event_sha256") is not None and event["event_sha256"] != event_hash:
            raise ValueError()
        page = event.get("page_one_based")
        expected = event.get("expected_pages")
        if expected is not None and (type(expected) is not int or not 1 <= expected <= 50):
            raise ValueError()
        if event["kind"] == "PAGE" and (type(expected) is not int or type(page) is not int or not 1 <= page <= expected):
            raise ValueError()
        artifacts = event["payload"].get("artifacts", [])
        if not isinstance(artifacts, list) or len(artifacts) > 256 or event["kind"] == "PAGE" and not artifacts:
            raise ValueError()
        decoded = []
        for item in artifacts:
            if (not isinstance(item, dict) or not isinstance(item.get("content_b64"), str)
                    or set(item) - {"kind", "media_type", "sha256", "bytes", "content_b64"}
                    or not isinstance(item.get("kind"), str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", item["kind"])
                    or item.get("media_type") not in {"application/json", "text/html", "image/png", "application/x-ndjson", "text/plain"}
                    or type(item.get("bytes")) is not int or not 0 <= item["bytes"] <= MAX_ARTIFACT_BYTES
                    or not isinstance(item.get("sha256"), str) or not SHA.fullmatch(item["sha256"])
                    or len(item["content_b64"]) > 4 * ((MAX_ARTIFACT_BYTES + 2) // 3)):
                raise ValueError()
            raw = base64.b64decode(item["content_b64"], validate=True)
            if len(raw) != item["bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValueError()
            decoded.append((item, raw))
        if sum(len(raw) for _, raw in decoded) > MAX_PAGE_BYTES:
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise _error() from None

    manifest_id = _identity(original.organization_id, original.id, attempt_id, event["request_id"], event["sequence"])
    stored = copy.deepcopy(event)
    stored_artifacts = []
    for index, (item, raw) in enumerate(decoded):
        identity = _identity(manifest_id, index, item["sha256"])
        suffix = {"application/json": ".json", "text/html": ".html", "image/png": ".png"}.get(item["media_type"], ".bin")
        artifact = _put(db, original, raw, "DOCUMENT_RECOGNITION_RAW", identity,
            f"recognition-{event['stage'][:40]}-p{page or 0}-{index}{suffix}", item["media_type"])
        record = {key: value for key, value in item.items() if key != "content_b64"}
        record.update(artifact_id=artifact.id, page=page, stage=event["stage"], attempt_id=attempt_id)
        stored_artifacts.append(record)
    if "artifacts" in stored["payload"] or stored_artifacts:
        stored["payload"]["artifacts"] = stored_artifacts
    envelope = {"schema_version": 1, "document_id": original.id, "subject_type": _subject_type(original), "organization_id": original.organization_id,
        "job_id": job_id, "worker_attempt_number": worker_attempt_number, "event_sha256": event_hash, "event": stored}
    if len(canonical(envelope)) > 2 * 1024**2:
        raise _error()
    manifest = _put(db, original, canonical(envelope), EVENT_KIND, manifest_id,
        f"recognition-event-{event['sequence']}.json", "application/json")
    link_artifact(db, original.organization_id, EVENT_RELATION, _subject_type(original), original.id, "ARTIFACT", manifest.id)
    for record in stored_artifacts:
        link_artifact(db, original.organization_id, RAW_RELATION, "ARTIFACT", manifest.id, "ARTIFACT", record["artifact_id"])
    return {"manifest_artifact_id": manifest.id, "event_sha256": event_hash, "attempt_id": attempt_id,
        "source_sha256": original.sha256, "configuration_sha256": configuration_sha256, "artifacts": stored_artifacts}


def recognition_journal(db, original, *, hydrate=False, attempt_id=None):
    """Read verified source-bound journals; no mutable provider paths are trusted."""
    artifacts = list(db.scalars(select(Artifact).join(ArtifactLink, ArtifactLink.target_id == Artifact.id).where(
        Artifact.organization_id == original.organization_id, Artifact.kind == EVENT_KIND,
        ArtifactLink.organization_id == original.organization_id, ArtifactLink.relation == EVENT_RELATION,
        ArtifactLink.source_type == _subject_type(original), ArtifactLink.source_id == original.id,
        ArtifactLink.target_type == "ARTIFACT").order_by(Artifact.created_at, Artifact.id).limit(MAX_EVENTS + 1)))
    if len(artifacts) > MAX_EVENTS:
        raise _error()
    result = []
    hydrated_bytes = 0
    for artifact in artifacts:
        if artifact.size_bytes > 2 * 1024**2:
            raise _error()
        envelope = json.loads(storage_provider.materialize(artifact).read_bytes())
        event = envelope.get("event", {})
        if (envelope.get("schema_version") != 1 or envelope.get("document_id") != original.id
                or envelope.get("subject_type", "DOCUMENT") != _subject_type(original)
                or envelope.get("organization_id") != original.organization_id
                or event.get("source_sha256") != original.sha256):
            raise _error()
        if attempt_id is not None and event.get("attempt_id") != attempt_id:
            continue
        refs = copy.deepcopy(event.get("payload", {}).get("artifacts", []))
        if hydrate:
            for record in event["payload"].get("artifacts", []):
                raw = db.get(Artifact, record["artifact_id"])
                link = db.scalar(select(ArtifactLink.id).where(ArtifactLink.organization_id == original.organization_id,
                    ArtifactLink.relation == RAW_RELATION, ArtifactLink.source_type == "ARTIFACT",
                    ArtifactLink.source_id == artifact.id, ArtifactLink.target_type == "ARTIFACT", ArtifactLink.target_id == record["artifact_id"]))
                if (not raw or not link or raw.organization_id != original.organization_id
                        or raw.sha256 != record["sha256"] or raw.size_bytes != record["bytes"]):
                    raise _error()
                hydrated_bytes += raw.size_bytes
                if hydrated_bytes > 128 * 1024**2:
                    raise OperationError(413, "DOCUMENT_REPLAY_BYTES_LIMIT", "La reproducción supera su límite de 128 MiB de resultados originales.")
                record["content_b64"] = base64.b64encode(storage_provider.materialize(raw).read_bytes()).decode("ascii")
                # Durable registry metadata is separate from the immutable RPC content.
                for key in ["artifact_id", "page", "stage", "attempt_id"]:
                    record.pop(key, None)
            if hashlib.sha256(canonical({key: value for key, value in event.items()
                    if key not in {"event_sha256", "durable_refs"}})).hexdigest() != envelope["event_sha256"]:
                raise _error()
        event["durable_refs"] = {"manifest_artifact_id": artifact.id, "event_sha256": envelope["event_sha256"],
            "attempt_id": event.get("attempt_id"), "source_sha256": original.sha256,
            "configuration_sha256": event.get("configuration_sha256"), "artifacts": refs}
        result.append({"event": event, "manifest_artifact_id": artifact.id,
            "worker_attempt_number": envelope["worker_attempt_number"], "created_at": artifact.created_at})
    return result


def latest_recognition_events(db, original, *, hydrate=False):
    entries = recognition_journal(db, original, hydrate=hydrate)
    grouped = defaultdict(list)
    for row in entries:
        grouped[row["event"]["attempt_id"]].append(row)
    if not grouped:
        return []
    current = max(grouped.values(), key=lambda group: (max(x["worker_attempt_number"] for x in group), max(x["created_at"] for x in group)))
    return [x["event"] for x in current]


def recognition_metadata(db, original):
    events = latest_recognition_events(db, original)
    if not events:
        return None, []
    pages = sorted({e["page_one_based"] for e in events if e["kind"] == "PAGE"})
    count = max((e["expected_pages"] for e in events if type(e.get("expected_pages")) is int), default=0)
    expected = list(range(1, count + 1))
    terminal = next((e for e in reversed(events) if e["kind"] == "TERMINAL"), None)
    last = events[-1]
    completed = sorted({e["page_one_based"] for e in events if e["kind"] == "PAGE"
        and e["payload"].get("recognition_status", "COMPLETE") == "COMPLETE"})
    if terminal:
        completed = [page for page in completed if page in terminal.get("completed_pages", [])]
    incomplete = {e["page_one_based"] for e in events if e["kind"] == "PAGE"
        and e["payload"].get("recognition_status") == "INCOMPLETE"}
    complete = bool(count and terminal and terminal.get("status") == "COMPLETE" and pages == expected
        and completed == expected and not incomplete)
    from .document_models import DocumentPublicationClaim

    publication = bool(getattr(original, "extraction_id", None) and db.scalar(select(DocumentPublicationClaim.id).where(
        DocumentPublicationClaim.organization_id == original.organization_id,
        DocumentPublicationClaim.document_id == original.id,
        DocumentPublicationClaim.extraction_id == original.extraction_id)))
    execution = {"attempt_id": last["attempt_id"], "stage": last["stage"], "expected_pages": expected,
        "emitted_pages": pages, "pending_pages": sorted(set(expected) - set(completed) | incomplete),
        "recognition_complete": complete, "validation_complete": False,
        "publication_complete": publication,
        "resources_released": bool(terminal and terminal.get("resources_released") is True)}
    metadata = [record for e in events for record in e["durable_refs"]["artifacts"]]
    return execution, metadata


def recognition_artifact(db, original, artifact_id):
    for row in recognition_journal(db, original):
        for record in row["event"]["durable_refs"]["artifacts"]:
            if record["artifact_id"] == artifact_id:
                artifact = db.get(Artifact, artifact_id)
                if artifact and artifact.organization_id == original.organization_id:
                    return artifact
    raise OperationError(404, "DOCUMENT_RECOGNITION_ARTIFACT_NOT_FOUND", "El original no contiene ese resultado de reconocimiento.")
