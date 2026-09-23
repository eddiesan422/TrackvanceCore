"""Run execution port and the local processing adapter.

The portable rule compiler in :mod:`trackvance.portable_engine` describes rule
semantics.  This port sits one level above it and executes a complete immutable
run, including evidence and lineage.  Workers can therefore change delivery or
compute infrastructure without changing module services.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session

from .models import Run


@runtime_checkable
class ExecutionEngine(Protocol):
    """Application port for executing one persisted Trackvance run."""

    key: str
    supported_processing_engines: frozenset[str]

    def execute(
        self,
        db: Session,
        run: Run,
        *,
        lease_owner: str | None = None,
        observed_at: datetime | None = None,
    ) -> None: ...


class LocalExecutionEngine:
    """In-process adapter using the current Polars/Python implementation."""

    key = "LOCAL"
    supported_processing_engines = frozenset({"POLARS"})

    def execute(
        self,
        db: Session,
        run: Run,
        *,
        lease_owner: str | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        # Delayed import keeps the port independent from application services
        # and avoids a services -> execution -> services import cycle.
        if run.module == "DELIVERY":
            from .delivery_service import execute_delivery_run

            execute_delivery_run(db, run, lease_owner=lease_owner)
            return
        from .services import execute_run

        execute_run(db, run, lease_owner=lease_owner, observed_at=observed_at)


execution_engine: ExecutionEngine = LocalExecutionEngine()
