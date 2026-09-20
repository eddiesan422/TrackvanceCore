import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "reset-local.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def test_reset_wrapper_delegates_only_to_guarded_plan_and_execute_commands():
    script = SCRIPT.read_text()
    assert "plan-reset --project $Project --output $Output" in script
    assert "reset --plan $Plan --confirm $Confirm" in script
    assert "down -v" not in script
    assert "volume prune" not in script
    assert "Remove-Item" not in script


def test_reset_wrapper_requires_trackvance_project_and_literal_confirmation_argument():
    script = SCRIPT.read_text()
    assert "^trackvance-" in script
    # Project is optional because the Plan path resolves it from the environment,
    # .env or Compose before applying the same guard explicitly.
    assert "ValidatePattern" not in script
    assert "$Project -notmatch '^trackvance-" in script
    assert "Parameter(Mandatory = $true, ParameterSetName = 'Execute')" in script
    assert "[string]$Confirm" in script


def test_reset_wrapper_detects_project_without_executing_environment_content():
    script = SCRIPT.read_text()
    assert "$env:COMPOSE_PROJECT_NAME" in script
    assert "COMPOSE_PROJECT_NAME\\s*=" in script
    assert "Get-Content -LiteralPath $EnvironmentFile" in script
    assert "Invoke-Expression" not in script


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell no está disponible")
def test_reset_wrapper_resolves_optional_project_before_applying_guard():
    environment = {**os.environ, "COMPOSE_PROJECT_NAME": "foreign-project"}
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT)],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "COMPOSE_PROJECT_NAME Trackvance" in output
    assert "cannot be validated" not in output
