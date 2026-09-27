"""Permanent audit policy for physical SQL targets, independent of credentials."""

import hashlib
import ipaddress
import json
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .db import iso
from .models import DeliveryDestination, DeliveryDestinationVersion, DeliveryTargetPolicy


def target_identity(
    organization_id: str, sink_type: str, config: dict[str, Any], target: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    host = str(config["host"]).rstrip(".").casefold()
    try:
        host = ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    # SQL Server identifiers may be case insensitive. Conservatively enforce
    # the same policy across case aliases, including on case-sensitive servers.
    normalize = str.casefold if sink_type == "SQLSERVER" else str
    identity = {
        "organization_id": organization_id,
        "sink_type": sink_type,
        "host": host,
        "port": int(config["port"]),
        "database": normalize(str(config["database"])),
        "schema_name": normalize(str(target["schema_name"])),
        "table_name": normalize(str(target["table_name"])),
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return fingerprint, identity


def target_policy(
    db: Session, destination: DeliveryDestination, version: DeliveryDestinationVersion,
    target: dict[str, Any], *, lock: bool = False,
) -> tuple[DeliveryTargetPolicy | None, str, dict[str, Any]]:
    fingerprint, identity = target_identity(
        destination.organization_id, destination.sink_type, version.config, target
    )
    if lock and db.get_bind().dialect.name == "postgresql":
        # Lock absent policies too, so a simultaneous non-audit publication
        # cannot slip past the irreversible transition to required.
        key = int.from_bytes(bytes.fromhex(fingerprint[:16]), "big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    query = select(DeliveryTargetPolicy).where(
        DeliveryTargetPolicy.organization_id == destination.organization_id,
        DeliveryTargetPolicy.target_fingerprint == fingerprint,
    )
    if lock:
        query = query.with_for_update()
    return db.scalar(query), fingerprint, identity


def policy_dto(policy: DeliveryTargetPolicy | None, fingerprint: str) -> dict[str, Any]:
    return {
        "audit_columns_required": policy is not None,
        "policy_id": policy.id if policy else None,
        "materialized_at": iso(policy.materialized_at) if policy else None,
        "target_fingerprint": fingerprint,
    }
