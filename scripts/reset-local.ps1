[CmdletBinding(DefaultParameterSetName = 'Plan')]
param(
    [Parameter(ParameterSetName = 'Plan')]
    [string]$Project,

    [Parameter(ParameterSetName = 'Plan')]
    [string]$Output,

    [Parameter(Mandatory = $true, ParameterSetName = 'Execute')]
    [string]$Plan,

    [Parameter(Mandatory = $true, ParameterSetName = 'Execute')]
    [string]$Confirm
)

$ErrorActionPreference = 'Stop'
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepositoryRoot 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = 'python'
}
$StateScript = Join-Path $PSScriptRoot 'docker_state.py'

if ($PSCmdlet.ParameterSetName -eq 'Plan') {
    if (-not $Project) {
        $Project = $env:COMPOSE_PROJECT_NAME
    }
    if (-not $Project) {
        $EnvironmentFile = Join-Path $RepositoryRoot '.env'
        if (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf) {
            $ProjectLine = Get-Content -LiteralPath $EnvironmentFile | Where-Object {
                $_ -match '^\s*COMPOSE_PROJECT_NAME\s*='
            } | Select-Object -First 1
            if ($ProjectLine) {
                $Project = ($ProjectLine -split '=', 2)[1].Trim().Trim('"').Trim("'")
            }
        }
    }
    if (-not $Project) {
        $ComposeFile = Join-Path $RepositoryRoot 'compose.yml'
        $NameLine = Get-Content -LiteralPath $ComposeFile | Where-Object {
            $_ -match '^name:\s*[a-z0-9-]+\s*$'
        } | Select-Object -First 1
        if ($NameLine) {
            $Project = ($NameLine -split ':', 2)[1].Trim()
        }
    }
    if ($Project -notmatch '^trackvance-[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$') {
        throw 'No se detectó un COMPOSE_PROJECT_NAME Trackvance válido; usa -Project.'
    }
    if (-not $Output) {
        $PlanDirectory = Join-Path $RepositoryRoot '.codex-local\reset-plans'
        New-Item -ItemType Directory -Path $PlanDirectory -Force | Out-Null
        $Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $Output = Join-Path $PlanDirectory "$Project-$Timestamp.json"
    }
    & $Python $StateScript plan-reset --project $Project --output $Output
} else {
    & $Python $StateScript reset --plan $Plan --confirm $Confirm
}

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
