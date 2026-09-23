"""Asynchronous job submission port with a persistent local adapter."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session

from .models import Job, Run


@runtime_checkable
class JobQueue(Protocol):
    """Application-facing queue port.

    The local adapter persists a database job consumed through leases.  A
    future Redis/Celery adapter can publish after persisting the same run while
    keeping the API and module services unchanged.
    """

    key: str

    def submit(
        self,
        db: Session,
        run: Run,
        *,
        executable: bool = True,
        lane: str = "DEFAULT",
    ) -> Job: ...


class DatabaseJobQueue:
    """Transactional PostgreSQL/SQLite queue used by the local worker."""

    key = "DATABASE"

    def submit(
        self,
        db: Session,
        run: Run,
        *,
        executable: bool = True,
        lane: str = "DEFAULT",
    ) -> Job:
        if lane not in {"DEFAULT", "DELIVERY"}:
            raise ValueError("Lane de trabajo no soportado.")
        job = Job(
            organization_id=run.organization_id,
            run_id=run.id,
            status="QUEUED" if executable else "FAILED",
            lane=lane,
        )
        db.add(job)
        return job


job_queue: JobQueue = DatabaseJobQueue()
