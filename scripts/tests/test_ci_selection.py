import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci.common import load_manifest
from scripts.ci.select_suites import select

SHA = "a" * 40


@pytest.mark.parametrize("path", ["backend/src/trackvance/api.py", "backend/uv.lock", "frontend/package.json",
    "frontend/src/routes.tsx", "scripts/tests/corrections_cycle.py", "scripts/ci/scenarios.json", "deploy/docker/compose.yml",
    ".github/workflows/ci.yml", "docs/development/architecture.md", "docs/operations/guide.md", "unknown/new.file",
    "../README.md", "/README.md", "C:/README.md", "backend/tests/test_permissions.py"])
def test_uncertain_or_transverse_changes_force_full_even_when_fast_requested(path):
    result = select("fast", [path], source_sha=SHA)
    assert result["mode"] == "full" and result["certifies_final"] is False
    assert len(result["groups"]) == 19


@pytest.mark.parametrize("mode,files,extra", [("auto", None, {}), ("fast", [], {"diff_uncertain": True}),
    ("full", ["README.md"], {}), ("fast", ["README.md"], {"commit_message": "Fix docs [ci full]"})])
def test_explicit_full_and_missing_diff_never_become_fast(mode, files, extra):
    assert select(mode, files, source_sha=SHA, **extra)["mode"] == "full"


def test_safe_documentation_selects_same_critical_tests_without_certifying_final():
    result = select("auto", ["README.md"], source_sha=SHA)
    assert result["mode"] == "fast" and not result["certifies_final"] and not result["final_eligible"]
    assert result["groups"] == ["backend", "frontend", "compose-critical"]
    assert result["matrix"]["include"] == [{"group": "compose-critical", "job_key": "suite-compose-critical", "timeout_minutes": 35}]


def test_manifest_preserves_original_jobs_and_all_large_xlsx_variants():
    manifest = load_manifest()
    groups = manifest["groups"]
    assert len(groups) == 19
    assert len({j for g in groups for j in g["covers_legacy_jobs"]}) == 16
    scenarios = [s for g in groups for s in g["scenarios"]]
    assert len(scenarios) == 64 and len({s["id"] for s in scenarios}) == 64
    acquisitions = next(g for g in groups if g["id"] == "corrections-acquisition")["scenarios"]
    assert {(s.get("rows"), s.get("variant")) for s in acquisitions if s["validator"] == "xlsx-acquisition"} == {
        (rows, variant) for rows in (100000, 100001, 400000, 1000000) for variant in ("inline", "shared")}
    assert len([s for g in groups if g["id"].startswith("corrections-") for s in g["scenarios"]]) == 17
    assert len(select("full", [], source_sha=SHA)["matrix"]["include"]) == 17


def test_selection_never_caches_an_existing_pass_result():
    result = select("full", [], source_sha=SHA, cache_mode="cold")
    assert result["cache_mode"] == "cold" and result["source_sha"] == SHA
    assert "PASS" not in json.dumps(result)


def test_long_suite_matrix_priority_keeps_manifest_group_and_scenario_order():
    result = select("full", [], source_sha=SHA)
    assert [g["group"] for g in result["matrix"]["include"][:8]] == [
        "catalog-reports", "corrections-recovery", "corrections-acquisition", "corrections-dispatch",
        "corrections-browser", "async-volume-1024", "async-volume-100", "async-volume-500"]
    assert result["groups"] == [g["id"] for g in load_manifest()["groups"]]
