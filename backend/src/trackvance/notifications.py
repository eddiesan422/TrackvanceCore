"""Read-only compatibility for notification metadata created before 0.6.1.

There is no delivery service, transport or environment-based activation in this
release. Historical records remain available without creating new attempts.
"""
from .db import iso
from .models import NotificationDeliveryRecord


def historical_notification_status() -> dict:
    return {"enabled": False, "configured": False, "provider": "NONE", "security": "NONE",
            "from_address": "", "from_name": "", "availability": "HISTORICAL_ONLY"}


def delivery_dto(record: NotificationDeliveryRecord) -> dict:
    return {key: getattr(record, key) for key in ("id", "organization_id", "event_type", "template_key", "channel", "recipient_type", "recipient_user_id", "recipient_email_snapshot", "provider_key", "attempt_number", "status", "error_code")} | {
        "created_at": iso(record.created_at), "sent_at": iso(record.sent_at), "failed_at": iso(record.failed_at)}
