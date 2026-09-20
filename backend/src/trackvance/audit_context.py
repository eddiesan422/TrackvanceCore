"""Request identities shared by HTTP and worker boundaries, independent of display labels."""

from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid5


@dataclass(frozen=True)
class Actor:
    type: str
    id: str
    display_name: str

    def __post_init__(self) -> None:
        if self.type not in {"USER", "WORKER", "SYSTEM"} or not self.id:
            raise ValueError("El actor debe tener un tipo permitido y una identidad estable.")

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


actor_context: ContextVar[Actor | None] = ContextVar("trackvance_actor", default=None)
request_id_context: ContextVar[str | None] = ContextVar("trackvance_request_id", default=None)


def legacy_actor(display_name: str, organization_id: str = "") -> Actor:
    """An unresolved historical display label is never asserted to be a real user."""
    return Actor(
        "WORKER" if display_name.casefold() == "worker" else "SYSTEM",
        str(uuid5(NAMESPACE_URL, f"trackvance:legacy-actor:{organization_id}:{display_name}")),
        display_name,
    )


_AUDIT_KEYS = frozenset({
    "dataset_id", "dataset_version_id", "source_version_id", "target_version_id", "artifact_id",
    "artifact_ids", "kind", "sha256", "size_bytes", "row_count", "module", "status", "decision",
    "version", "from_state", "to_state", "state", "configuration_id", "config_id", "config_hash",
    "filename", "format", "source_type", "source_run_id", "parent_version_id", "user_id", "role",
    "reason_code", "error_code", "legacy_identity", "actor_identity_resolution", "fields",
    "attempt", "attempts", "schema_version", "permission", "result_artifact_id", "sensitivity",
    "reader_key", "reader_version", "sheet_name", "delimiter",
    "connection_id", "connection_version_id", "connection_version", "schema_name", "object_name",
    "object_kind", "captured_at", "sampled_rows", "canonical_artifact_id", "enabled", "draft",
    "schedule_id", "schedule_version_id", "planned_at", "dispatched_at", "coalesced_intervals",
    "interval_seconds", "assigned_user_id", "priority", "sla_hours", "due_at",
    "auto_resolve_enabled", "closure_type", "changed_fields", "active", "sessions_revoked", "policy",
    "origin_run_id", "validation_run_id", "criterion",
})


def sanitize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Only operation metadata is auditable; data rows, credentials and nested payloads are dropped."""
    result: dict[str, Any] = {}
    for key, value in (metadata or {}).items():
        if key not in _AUDIT_KEYS:
            continue
        if isinstance(value, str):
            result[key] = value[:500]
        elif value is None or isinstance(value, (bool, int, float)):
            result[key] = value
        elif isinstance(value, list):
            result[key] = [str(item)[:120] for item in value[:100] if isinstance(item, (str, int))]
    return result
