$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Push-Location $repoRoot
try {
    docker info --format '{{.ServerVersion}}'
    if ($LASTEXITCODE -ne 0) { throw 'Inicia Docker Desktop (contenedores Linux) y vuelve a ejecutar este script. Para el prototipo directo, utiliza scripts/start-local.ps1.' }
    if (-not (Test-Path -LiteralPath '.env')) {
        $bytes = New-Object byte[] 24
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $localPassword = -join ($bytes | ForEach-Object { $_.ToString('x2') })
        (Get-Content -LiteralPath '.env.example' -Raw).Replace('replace-with-a-local-password', $localPassword) | Set-Content -LiteralPath '.env' -Encoding utf8
    }
    docker compose up --build --detach --wait --wait-timeout 180
    if ($LASTEXITCODE -ne 0) { throw 'El entorno no pudo iniciar. Consulta docker compose logs.' }
    $webPort = if ($env:WEB_PORT) { $env:WEB_PORT } else { '3000' }
    Write-Host "Trackvance Core: http://localhost:$webPort" -ForegroundColor Cyan
    Write-Host 'La API aplica Alembic antes de aceptar peticiones. Los volúmenes conservan los datos al reiniciar.'
} finally { Pop-Location }
