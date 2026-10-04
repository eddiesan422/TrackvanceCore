"""Read metadata heartbeats without loading engines or opening the database."""

import json
from datetime import UTC, datetime

from .config import STORAGE_DIR

COMPONENTS = frozenset({"scheduler", "events-notifications", "events-chaining"})


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
