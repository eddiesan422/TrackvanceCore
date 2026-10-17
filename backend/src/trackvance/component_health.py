"""Read metadata heartbeats without loading engines or opening the database."""

import json
from datetime import UTC, datetime
from pathlib import Path

from .config import STORAGE_DIR

COMPONENTS = frozenset({"scheduler", "events-notifications", "events-chaining"})
WORKER_LANES = frozenset({"DEFAULT", "DELIVERY", "ACQUISITION", "REPORT", "DOCUMENT", "COMPARISON"})


def worker_status(lane: str = "DEFAULT", *, now: datetime | None = None,
                  storage_dir: Path | None = None) -> dict[str, str | None]:
    """Read the worker's existing heartbeat and preserve its freshness contract."""
    checked = lane.strip().upper()
    if checked not in WORKER_LANES:
        raise RuntimeError("TRACKVANCE_WORKER_LANE debe ser DEFAULT, DELIVERY, ACQUISITION o REPORT.")
    offline = {"status": "OFFLINE", "last_seen": None, "lane": checked}
    try:
        root = (storage_dir or STORAGE_DIR).resolve()
        path = (root / f"worker-heartbeat-{checked.lower()}.json").resolve()
        if not path.is_relative_to(root):
            return offline
        data = json.loads(path.read_text(encoding="utf-8"))
        age = ((now or datetime.now(UTC)) - datetime.fromisoformat(data["last_seen"])).total_seconds()
        return {"status": "RUNNING" if age < 30 else "OFFLINE", "last_seen": data["last_seen"], "lane": checked}
    except (OSError, ValueError, KeyError, TypeError):
        return offline


def component_status(component: str, *, now: datetime | None = None) -> str:
    """Use the dispatcher's existing file and freshness contract for cheap probes."""
    if component not in COMPONENTS:
        raise ValueError("Componente desconocido.")
    try:
        root = STORAGE_DIR.resolve()
        path = (root / f"{component}-heartbeat.json").resolve()
        if not path.is_relative_to(root):
            return "OFFLINE"
        data = json.loads(path.read_text(encoding="utf-8"))
        instant = datetime.fromisoformat(data["updated_at"])
        age = ((now or datetime.now(UTC)) - instant).total_seconds()
    except (OSError, ValueError, KeyError, TypeError):
        return "OFFLINE"
    return "RUNNING" if 0 <= age < 30 else "OFFLINE"
