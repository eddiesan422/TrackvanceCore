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
from .scheduler import SchedulerDispatchError, commit_dispatch
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
    # Each module commits independently; even a failed module cannot starve
    # Delivery. Health records completed cycles separately from process pulses.
    count, successful, errors = 0, 0, []
    try:
        scheduled = schedule_tick()
        count += scheduled
        successful += getattr(scheduled, "successful_count", scheduled)
    except SchedulerDispatchError as error:
        count += error.dispatched_count
        successful += error.successful_count
        errors.extend(error.errors)
    except Exception:  # noqa: BLE001 -- keep the delivery dispatcher independent
        errors.append({"module": "schedules", "code": "SCHEDULER_DISPATCH_FAILED"})
    try:
        with SessionLocal() as db:
            registered, jobs = commit_dispatch(db, lambda session: dispatch_due(session, limit=limit))
            count += registered
            successful += jobs
    except Exception:  # noqa: BLE001 -- supervised retry after durable diagnostics
        errors.append({"module": "delivery", "code": "SCHEDULER_MODULE_DISPATCH_FAILED"})
    write_dispatch_evidence(count=successful, errors=errors)
    if errors:
        raise SchedulerDispatchError(errors, count)
    return count


def write_dispatch_evidence(*, count, errors):
    path = artifact_store.location("scheduler-dispatch.json")
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        previous = {}
    now = iso(utcnow())
    evidence = {"last_cycle_at": now, "last_successful_cycle_at": previous.get("last_successful_cycle_at") if errors else now,
        "last_successful_dispatch_at": now if count else previous.get("last_successful_dispatch_at"),
        "dispatched_count": count, "errors": errors,
        "consecutive_failures": previous.get("consecutive_failures", 0) + 1 if errors else 0}
    temporary = path.with_suffix(f".{uid()}.tmp")
    temporary.write_text(json.dumps(evidence), encoding="utf-8")
    os.replace(temporary, path)


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
