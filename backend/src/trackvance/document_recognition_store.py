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
from typing import Any
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
MAX_REUSE_CANDIDATES = 8
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
    links = [row for row in current if row["event"].get("stage") in {"RECOVERY_REUSE", "VERIFICATION_REQUEST"}]
    if not links:
        return [x["event"] for x in current]
    if len(links) != 1:
        raise _error()
    link = links[0]
    payload = link["event"]["payload"]
    hashes, attempts = payload.get("reused_event_hashes"), payload.get("reused_attempt_ids")
    if (not isinstance(hashes, list) or not 1 <= len(hashes) <= MAX_EVENTS
            or not all(isinstance(value, str) and SHA.fullmatch(value) for value in hashes)
            or len(set(hashes)) != len(hashes)
            or not isinstance(attempts, list) or not attempts
            or not all(isinstance(value, str) and IDENTITY.fullmatch(value) for value in attempts)
            or len(set(attempts)) != len(attempts)):
        raise _error()
    source_id = payload.get("source_original_id", original.id)
    if not isinstance(source_id, str) or not 1 <= len(source_id) <= 64:
        raise _error()
    if source_id != original.id:
        source = _reuse_source(db, original, source_id)
        source_entries = recognition_journal(db, source, hydrate=hydrate)
        if _has_peer_reuse(source_entries, source.id):
            raise _error()
        administrative = payload.get("source_administrative_event_hashes", [])
        if (not isinstance(administrative, list)
                or not all(isinstance(value, str) and SHA.fullmatch(value) for value in administrative)
                or len(set(administrative)) != len(administrative)):
            raise _error()
        source_hashes = {row["event"]["event_sha256"]: row for row in source_entries}
        if any(value not in source_hashes or source_hashes[value]["created_at"] > link["created_at"]
               for value in administrative):
            raise _error()
    else:
        source_entries = entries
    by_hash = {row["event"]["event_sha256"]: row for row in source_entries}
    if len(by_hash) != len(source_entries):
        raise _error()
    reused = []
    bindings = payload.get("recognition_bindings")
    for value in hashes:
        row = by_hash.get(value)
        if (row is None or row["event"]["attempt_id"] not in attempts or row in current
                or row["created_at"] > link["created_at"]
                or row["event"].get("source_sha256") != link["event"].get("source_sha256")
                or source_id != original.id and row["event"] not in _model_source_events([row["event"]])
                or not bindings and any(row["event"].get(key) != link["event"].get(key)
                       for key in ("configuration_sha256", "definition_sha256"))):
            raise _error()
        reused.append(row["event"])
    # A peer receipt is one hop into an older, same-family/source journal. All
    # original event/RAW identities remain attached to that source document.
    combined = reused + [x["event"] for x in current]
    if source_id != original.id and (not bindings or not _complete_model_events(reused)):
        raise _error()
    if bindings:
        from .document_recognition_compatibility import validate_recognition_bindings

        try:
            # Receipt equivalence checks the original RPC bytes and hashes, not
            # the registry's unhydrated artifact records used by DTO listings.
            verified_events = combined if hydrate else latest_recognition_events(db, original, hydrate=True)
            validate_recognition_bindings(bindings, verified_events, source_sha256=original.sha256,
                configuration_sha256=link["event"]["configuration_sha256"],
                definition_sha256=link["event"].get("definition_sha256"))
        except (ValueError, TypeError, KeyError) as error:
            raise _error() from error
    return combined


def _reuse_source(db, original, source_id):
    from .document_models import DocumentOriginal
    from .scheduler import aware

    source = db.get(DocumentOriginal, source_id)
    if (source is None or source.id == original.id or source.organization_id != original.organization_id
            or source.family_id != original.family_id or source.sha256 != original.sha256
            or aware(source.created_at) >= aware(original.created_at)):
        raise _error()
    return source


def _has_peer_reuse(entries, source_id):
    return any(row["event"].get("stage") == "RECOVERY_REUSE"
        and row["event"].get("payload", {}).get("source_original_id", source_id) != source_id for row in entries)


def _model_source_events(events):
    """Keep actual model/native inputs and outputs; administrative actions stay trace refs."""
    return [event for event in events if event.get("kind") in {"PAGE", "TERMINAL"}
        or any(item.get("kind") in {"NATIVE_SOURCE_PROBE_JSON", "QWEN_REQUEST_JSON"}
            for item in event.get("payload", {}).get("artifacts", []))]


def _complete_model_events(events):
    pages = [event for event in events if event.get("kind") == "PAGE"]
    if not pages:
        return False
    for stage in {event["stage"] for event in pages}:
        group = [event for event in events if event["stage"] == stage]
        terminal: dict[str, Any] = next((event for event in reversed(group) if event["kind"] == "TERMINAL"), {})
        count = terminal.get("expected_pages")
        expected = list(range(1, count + 1)) if type(count) is int and 1 <= count <= 50 else []
        if (not expected or terminal.get("status") != "COMPLETE" or terminal.get("resources_released") is not True
                or terminal.get("completed_pages") != expected
                or sorted(event["page_one_based"] for event in group if event["kind"] == "PAGE"
                    and event.get("payload", {}).get("recognition_status", "COMPLETE") == "COMPLETE") != expected):
            return False
    return True


def reusable_recognition_source(db, original, configuration, definition):
    """Find a bounded, compatible saved read before a new normal-flow primary.

    Selection uses source ownership and input compatibility, never extracted
    answers or approval quality. The worker persists the returned receipt under
    its own fenced attempt before adapting any source events.
    """
    from .document_automatic_verification import enabled
    from .document_models import DocumentOriginal
    from .document_recognition_compatibility import approve_recognition_reuse
    from .document_verification_service import _source_snapshots
    from .models import Job

    if not enabled(configuration):
        return None
    sources = db.scalars(select(DocumentOriginal).where(
        DocumentOriginal.organization_id == original.organization_id,
        DocumentOriginal.family_id == original.family_id, DocumentOriginal.sha256 == original.sha256,
        DocumentOriginal.id != original.id, DocumentOriginal.created_at < original.created_at,
        DocumentOriginal.extraction_id.is_not(None), DocumentOriginal.cancel_requested.is_(False),
        DocumentOriginal.status.in_({"EXTRACTED", "AUTO_VERIFIED", "REVIEW_REQUIRED", "PUBLISHED"}))
        .order_by(DocumentOriginal.created_at.desc(), DocumentOriginal.id).limit(MAX_REUSE_CANDIDATES))
    for source in sources:
        if db.scalar(select(Job.id).where(Job.document_id == source.id,
                Job.organization_id == original.organization_id, Job.status.in_({"QUEUED", "RUNNING"}))):
            continue
        entries = recognition_journal(db, source)
        if _has_peer_reuse(entries, source.id):
            continue
        resolved = latest_recognition_events(db, source, hydrate=True)
        events = _model_source_events(resolved)
        if not _complete_model_events(events):
            continue
        snapshots, definitions = _source_snapshots(db, source, configuration, definition)
        try:
            bindings = approve_recognition_reuse(events, source_sha256=original.sha256,
                configuration=configuration, definition=definition,
                configuration_snapshots=snapshots, definition_snapshots=definitions)
        except (ValueError, KeyError, TypeError):
            continue
        hashes = [event["event_sha256"] for event in events]
        return {"events": events, "recognition_bindings": bindings, "source_original_id": source.id,
            "source_administrative_event_hashes": [event["event_sha256"] for event in resolved
                if event["event_sha256"] not in set(hashes)]}
    return None


def verification_request_for(db, original, *, worker_attempt_number=None):
    """Read the latest native request for this exact original and extraction.

    Only the native service can persist these phase events. It freezes the new
    verification configuration without changing the original base snapshot.
    """
    rows = [row for row in recognition_journal(db, original)
            if row["event"].get("stage") == "VERIFICATION_REQUEST"
            and row["event"].get("kind") == "PHASE"
            and (worker_attempt_number is None or row["worker_attempt_number"] <= worker_attempt_number)]
    if not rows:
        return None
    row = max(rows, key=lambda value: (value["worker_attempt_number"], value["created_at"]))
    event = row["event"]
    payload = event["payload"]
    if payload.get("expected_extraction_id") != getattr(original, "extraction_id", None):
        return None
    if (payload.get("version") != "TRACKVANCE_VERIFICATION_REQUEST_098_V1"
            or not isinstance(payload.get("configuration"), dict)
            or hashlib.sha256(canonical(payload["configuration"])).hexdigest() != event["configuration_sha256"]
            or payload.get("configuration_sha256") != event["configuration_sha256"]):
        raise _error()
    return copy.deepcopy(event)


def recognition_metadata(db, original):
    events = latest_recognition_events(db, original)
    if not events:
        return None, []
    primary = next((e["stage"] for e in events if e["kind"] == "PAGE"), None)
    primary_events = [e for e in events if e["stage"] == primary]
    pages = sorted({e["page_one_based"] for e in primary_events if e["kind"] == "PAGE"})
    count = max((e["expected_pages"] for e in events if type(e.get("expected_pages")) is int), default=0)
    expected = list(range(1, count + 1))
    terminal = next((e for e in reversed(primary_events) if e["kind"] == "TERMINAL"), None)
    last = events[-1]
    completed = sorted({e["page_one_based"] for e in primary_events if e["kind"] == "PAGE"
        and e["payload"].get("recognition_status", "COMPLETE") == "COMPLETE"})
    if terminal:
        completed = [page for page in completed if page in terminal.get("completed_pages", [])]
    incomplete = {e["page_one_based"] for e in primary_events if e["kind"] == "PAGE"
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
        "resources_released": bool(terminal and all(e.get("resources_released") is True for e in events if e["kind"] == "TERMINAL"))}
    verification = next((e["payload"]["verification"] for e in reversed(events)
                         if e.get("stage") == "AUTOMATIC_VERIFICATION"
                         and isinstance(e.get("payload", {}).get("verification"), dict)), None)
    if verification is not None:
        execution["verification"] = copy.deepcopy(verification)
    request_position = next((index for index in range(len(events) - 1, -1, -1)
                             if events[index].get("stage") == "VERIFICATION_REQUEST"), -1)
    progress_position = next((index for index in range(len(events) - 1, -1, -1)
                              if events[index].get("stage") == "AUTOMATIC_VERIFICATION"), -1)
    if (request_position > progress_position and original.status in {"QUEUED", "RUNNING"}
            and events[request_position]["payload"].get("expected_extraction_id") == original.extraction_id):
        execution["verification"] = {"policy_version": "TRACKVANCE_AUTOMATIC_VERIFICATION_098_V1",
            "status": "QUEUED" if original.status == "QUEUED" else "VERIFYING", "primary_reused": True,
            "requested_reason": events[request_position]["payload"].get("reason"),
            "reasons_before": [], "remaining_reasons": [], "resolved_paths": [],
            "scope": {"kind": "WHOLE_DOCUMENT", "pages": expected},
            "max_secondary_calls": 1, "rereads_allowed": 0,
            "automatic_approval_granted": False, "human_reviewed": False}
    metadata = [record for e in events for record in e["durable_refs"]["artifacts"]]
    return execution, metadata


def recognition_artifact(db, original, artifact_id):
    events = [row["event"] for row in recognition_journal(db, original)]
    events.extend(latest_recognition_events(db, original))
    for event in events:
        for record in event["durable_refs"]["artifacts"]:
            if record["artifact_id"] == artifact_id:
                artifact = db.get(Artifact, artifact_id)
                if artifact and artifact.organization_id == original.organization_id:
                    return artifact
    raise OperationError(404, "DOCUMENT_RECOGNITION_ARTIFACT_NOT_FOUND", "El original no contiene ese resultado de reconocimiento.")
