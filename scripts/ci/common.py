"""Strict parsers shared by certification tools; no network or Docker operations."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,160}")
ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name("scenarios.json")


class EvidenceError(ValueError):
    """A safe diagnostic code; do not include artifact contents in messages."""


def require(condition: Any, code: str) -> None:
    if not condition:
        raise EvidenceError(code)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, "DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    require(path.is_file() and not path.is_symlink(), "MISSING_OR_LINKED_EVIDENCE")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(EvidenceError("NONFINITE_JSON")))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError("INVALID_JSON") from error


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def relative_file(base: Path, value: str) -> Path:
    require(isinstance(value, str) and "\\" not in value, "INVALID_EVIDENCE_PATH")
    parts = PurePosixPath(value)
    require(not parts.is_absolute() and value and all(p not in {"..", "."} for p in parts.parts), "EVIDENCE_PATH_ESCAPE")
    require(not re.match(r"^[A-Za-z]:", value), "EVIDENCE_PATH_ESCAPE")
    target = base / parts
    require(target.resolve().is_relative_to(base.resolve()), "EVIDENCE_PATH_ESCAPE")
    current = base
    for component in parts.parts:
        current = current / component
        require(not current.is_symlink() and not getattr(current, "is_junction", lambda: False)(), "LINKED_EVIDENCE")
    require(target.is_file(), "MISSING_EVIDENCE_ATTACHMENT")
    return target


def load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    data = load_json(path)
    require(isinstance(data, dict) and data.get("schema_version") == 1, "INVALID_MANIFEST")
    groups = data.get("groups")
    require(isinstance(groups, list) and groups, "EMPTY_MANIFEST")
    ids: set[str] = set()
    scenarios: set[str] = set()
    for group in groups:
        require(isinstance(group, dict) and IDENTIFIER.fullmatch(group.get("id", "")), "INVALID_GROUP")
        require(group["id"] not in ids, "DUPLICATE_MANIFEST_GROUP")
        ids.add(group["id"])
        require(isinstance(group.get("scenarios"), list) and group["scenarios"], "EMPTY_GROUP")
        for scenario in group["scenarios"]:
            require(isinstance(scenario, dict) and IDENTIFIER.fullmatch(scenario.get("id", "")), "INVALID_SCENARIO")
            require(scenario["id"] not in scenarios, "DUPLICATE_MANIFEST_SCENARIO")
            require(isinstance(scenario.get("validator"), str), "MISSING_CONTENT_VALIDATOR")
            scenarios.add(scenario["id"])
    require(set(data.get("fast_groups", [])) <= ids, "INVALID_FAST_GROUP")
    return data


def group_spec(manifest: dict[str, Any], group: str) -> dict[str, Any]:
    values = [item for item in manifest["groups"] if item["id"] == group]
    require(len(values) == 1, "UNKNOWN_GROUP")
    return values[0]
