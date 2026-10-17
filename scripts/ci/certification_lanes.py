"""Bound inherited 0.9.5 lanes that historical suites never start."""
from pathlib import Path

UNUSED_CORE095_LANES = frozenset({"document-worker", "comparison-worker"})
UNUSED_CORE095_PROFILE = "certification-unused095"


def inactive_core095_lanes(root: Path, project: str, backend_image: str) -> dict:
    return {name: {"image": backend_image, "profiles": [UNUSED_CORE095_PROFILE],
        "mem_limit": "128m", "cpus": 0.125, "pids_limit": 64,
        "environment": {"PYTHONPATH": "/app/backend/src", "TRACKVANCE_CERTIFICATION_PROJECT": project},
        "volumes": [{"type": "bind", "source": str(root / relative), "target": "/app/" + relative,
            "read_only": True} for relative in ("backend/src", "backend/migrations", "scripts")]}
        for name in sorted(UNUSED_CORE095_LANES)}
