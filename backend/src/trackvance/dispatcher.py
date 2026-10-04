"""Separate, supervised metadata dispatcher. No connector or sink execution."""

import json
import logging
import os
import signal
import threading

from .artifactstore import artifact_store
from .automation import dispatch_due
from .component_health import (
    component_status as component_status,  # noqa: PLC0414 -- compatibility reexport
)
from .db import SessionLocal, iso, utcnow
from .models import uid
from .scheduler import tick as schedule_tick

logger = logging.getLogger(__name__)


def bounded_parameter(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        raise RuntimeError(f"{name} requiere un entero.") from None
    if not lower <= value <= upper:
        raise RuntimeError(f"{name} debe estar entre {lower} y {upper}.")
    return value


def write_heartbeat(component: str, owner: str):
    if component not in {"scheduler", "events-notifications", "events-chaining"}:
        raise ValueError("Componente desconocido.")
    path = artifact_store.location(f"{component}-heartbeat.json")
    temporary = path.with_suffix(f".{owner}.tmp")
    temporary.write_text(json.dumps({"component": component, "owner": owner,
                                    "updated_at": iso(utcnow())}), encoding="utf-8")
    os.replace(temporary, path)


def dispatch_tick(*, limit=100):
    # Each dispatch transaction is retryable by cursor/occurrence uniqueness.
    schedule_tick()
    with SessionLocal() as db:
        count = dispatch_due(db, limit=limit)
        db.commit()
        return count


def heartbeat_loop(component, owner, stop):
    while not stop.is_set():
        try:
            write_heartbeat(component, owner)
        except OSError:
            logger.warning("No se pudo actualizar heartbeat %s.", component)
        stop.wait(10)


def main():
    interval = bounded_parameter("TRACKVANCE_SCHEDULER_POLL_SECONDS", 5, 1, 60)
    batch = bounded_parameter("TRACKVANCE_SCHEDULER_BATCH_SIZE", 100, 1, 1000)
    stop, owner = threading.Event(), uid()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    threading.Thread(target=heartbeat_loop, args=("scheduler", owner, stop), daemon=True).start()
    while not stop.is_set():
        try:
            dispatch_tick(limit=batch)
        except Exception:  # noqa: BLE001 -- supervised process boundary retries rolled-back dispatches
            logger.warning("El programador espera metadata disponible.")
        stop.wait(interval)


if __name__ == "__main__":
    main()
