"""Shared local administration errors and optimistic case updates."""

from typing import Any, cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from .models import ExceptionCase


class OperationError(Exception):
    def __init__(self, status: int, code: str, message: str, details=None):
        self.status, self.code, self.message, self.details = status, code, message, details


def save_case(db: Session, case: ExceptionCase, expected_version: int, changes: dict[str, Any]) -> None:
    """All manual and worker writes use the same compare-and-swap boundary."""
    with db.no_autoflush:
        result = db.execute(update(ExceptionCase).where(
            ExceptionCase.id == case.id,
            ExceptionCase.organization_id == case.organization_id,
            ExceptionCase.version == expected_version,
        ).values(**changes, version=expected_version + 1).execution_options(synchronize_session=False))
    if cast(CursorResult, result).rowcount != 1:
        raise OperationError(409, "VERSION_CONFLICT", "Otra persona modificó esta excepción. Actualiza los datos.")
    db.refresh(case)
