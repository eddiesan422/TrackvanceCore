"""Reusable delivery port with an SMTP adapter; content exists only in memory."""
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .audit_context import Actor
from .config import WEB_ORIGIN
from .db import iso, utcnow
from .models import NotificationDeliveryRecord, User
from .services import audit


@dataclass(frozen=True)
class NotificationMessage:
    recipient: str
    subject: str
    body: str = field(repr=False)


class NotificationDelivery(Protocol):
    def deliver(self, message: NotificationMessage) -> None: ...


class NotificationError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def enabled(key: str) -> bool:
    return os.getenv(key, "false").casefold() == "true"


def smtp_status() -> dict:
    security = os.getenv("TRACKVANCE_SMTP_SECURITY", "STARTTLS").upper()
    configured = (enabled("TRACKVANCE_SMTP_ENABLED") and bool(os.getenv("TRACKVANCE_SMTP_HOST"))
                  and bool(os.getenv("TRACKVANCE_SMTP_FROM_ADDRESS"))
                  and security in {"STARTTLS", "SSL", "TLS", "NONE"}
                  and (security != "NONE" or enabled("TRACKVANCE_SMTP_ALLOW_INSECURE")))
    return {"enabled": enabled("TRACKVANCE_SMTP_ENABLED"), "configured": configured,
            "provider": "SMTP", "security": security,
            "from_address": os.getenv("TRACKVANCE_SMTP_FROM_ADDRESS", ""),
            "from_name": os.getenv("TRACKVANCE_SMTP_FROM_NAME", "Trackvance")}


class SMTPNotificationDelivery:
    def deliver(self, message: NotificationMessage) -> None:
        status = smtp_status()
        if not status["configured"]:
            raise NotificationError("NO_PROVIDER")
        email = EmailMessage()
        email["From"] = formataddr((status["from_name"], status["from_address"]))
        email["To"], email["Subject"] = message.recipient, message.subject
        email.set_content(message.body)
        try:
            host = os.environ["TRACKVANCE_SMTP_HOST"]
            port = int(os.getenv("TRACKVANCE_SMTP_PORT", "587"))
            client = (smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context())
                      if status["security"] in {"SSL", "TLS"} else smtplib.SMTP(host, port, timeout=15))
            with client:
                if status["security"] == "STARTTLS":
                    client.starttls(context=ssl.create_default_context())
                if os.getenv("TRACKVANCE_SMTP_USERNAME"):
                    client.login(os.environ["TRACKVANCE_SMTP_USERNAME"], os.getenv("TRACKVANCE_SMTP_PASSWORD", ""))
                client.send_message(email)
        except smtplib.SMTPAuthenticationError:
            raise NotificationError("SMTP_AUTH_FAILED") from None
        except (OSError, smtplib.SMTPException, ValueError):
            raise NotificationError("SMTP_DELIVERY_FAILED") from None


def delivery_dto(record: NotificationDeliveryRecord) -> dict:
    return {key: getattr(record, key) for key in ("id", "organization_id", "event_type", "template_key", "channel", "recipient_type", "recipient_user_id", "recipient_email_snapshot", "provider_key", "attempt_number", "status", "error_code")} | {
        "created_at": iso(record.created_at), "sent_at": iso(record.sent_at), "failed_at": iso(record.failed_at)}


class NotificationService:
    def __init__(self, adapter: NotificationDelivery | None = None):
        self.adapter = adapter or SMTPNotificationDelivery()

    def send(self, db: Session, user: User, *, event_type: str, template_key: str,
             message: NotificationMessage) -> NotificationDeliveryRecord:
        attempt = (db.scalar(select(func.max(NotificationDeliveryRecord.attempt_number)).where(
            NotificationDeliveryRecord.recipient_user_id == user.id,
            NotificationDeliveryRecord.template_key == template_key)) or 0) + 1
        record = NotificationDeliveryRecord(organization_id=user.organization_id,
            event_type=event_type, template_key=template_key, recipient_user_id=user.id,
            recipient_email_snapshot=user.email, attempt_number=attempt, status="PENDING")
        db.add(record)
        db.commit()  # Durable metadata before delivery; never serialize message content.
        try:
            self.adapter.deliver(message)
            record.status, record.sent_at = "SENT", utcnow()
        except NotificationError as exc:
            record.status, record.error_code, record.failed_at = "FAILED", exc.code, utcnow()
        except Exception:  # noqa: BLE001 - adapter boundary must suppress sensitive provider errors.
            # Third-party adapters must not leak message bodies or SMTP exceptions.
            record.status, record.error_code, record.failed_at = "FAILED", "DELIVERY_FAILED", utcnow()
        audit(db, "NOTIFICATION_" + record.status, "notification", record.id,
              "Intento de entrega de notificación", Actor("SYSTEM", "notification-service", "Notificaciones"),
              user.organization_id, {"status": record.status, "error_code": record.error_code,
                                     "user_id": user.id, "attempt_number": attempt})
        db.commit()
        return record

    def temporary_credentials(self, db: Session, user: User, password: str) -> NotificationDeliveryRecord:
        message = NotificationMessage(user.email, "Trackvance · Credenciales temporales", (
            f"Hola {user.name},\n\nUsername: {user.username}\nContraseña temporal: {password}\n"
            f"Expira: {iso(user.temporary_password_expires_at)}\n"
            f"Trackvance: {os.getenv('TRACKVANCE_PUBLIC_URL', WEB_ORIGIN).rstrip('/')}\n\n"
            "Debes cambiar esta contraseña en el primer acceso. No compartas estas credenciales.\n"
            "Si no esperabas este mensaje, contacta a tu administrador.\n"))
        return self.send(db, user, event_type="USER_TEMPORARY_CREDENTIALS",
                         template_key="USER_TEMPORARY_CREDENTIALS", message=message)
