"""Versioned evidence contract. Historical files are adapted in memory, never rewritten."""

import copy
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

from .artifactstore import artifact_dto
from .audit_context import Actor, legacy_actor
from .models import Artifact

SCHEMA_VERSION = 2


def configuration_hash(configuration: dict) -> str:
    return hashlib.sha256(json.dumps(configuration, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def read_manifest(path: str | Path, actor: Actor | None = None,
                  artifacts: Iterable[Artifact | dict] = ()) -> dict:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    return adapt_manifest(manifest, actor, artifacts)


def adapt_manifest(manifest: dict, actor: Actor | None = None,
                   artifacts: Iterable[Artifact | dict] = ()) -> dict:
    version = manifest.get("schema_version", 1)
    if version == SCHEMA_VERSION:
        return copy.deepcopy(manifest)
    if version != 1:
        raise ValueError(f"Versión de manifest no soportada: {version}")
    result = copy.deepcopy(manifest)
    result["schema_version"] = SCHEMA_VERSION
    result["source_schema_version"] = 1
    identity = result.get("initiated_by")
    if not isinstance(identity, dict):
        result["initiated_by"] = (actor or legacy_actor(str(identity or "Sistema"))).as_dict()
        result["identity_resolution"] = "LEGACY_DATABASE_LOOKUP" if actor else "LEGACY_UNRESOLVED_DISPLAY_NAME"
    known = [artifact_dto(value) if isinstance(value, Artifact) else value for value in artifacts]
    result["result_artifacts"] = [
        _adapt_artifact(value, known) for value in result.get("result_artifacts", [])
    ]
    return result


def _adapt_artifact(value: dict, known: list[dict]) -> dict:
    match = next((item for item in known if item.get("sha256") == value.get("sha256") and item.get("name") == value.get("name")), None)
    if match:
        return {**value, **match}
    return {**value, "artifact_id": value.get("artifact_id"), "kind": value.get("kind", "LEGACY_RESULT"),
            "size_bytes": value.get("size_bytes"), "legacy_metadata_incomplete": True}
