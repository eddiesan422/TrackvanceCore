param([switch]$SkipInstall)
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runtimeRoot = Join-Path $repoRoot '.local'
$backendRoot = Join-Path $repoRoot 'backend'
$frontendRoot = Join-Path $repoRoot 'frontend'
$pythonExe = Join-Path $backendRoot '.venv\Scripts\python.exe'
$processFile = Join-Path $runtimeRoot 'processes.json'
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

if (Test-Path -LiteralPath $processFile) {
    $running = @(Get-Content -LiteralPath $processFile -Raw | ConvertFrom-Json | Where-Object {
        $p = Get-Process -Id $_.pid -ErrorAction SilentlyContinue
        $p -and $p.StartTime.ToUniversalTime().ToString('o') -eq $_.started
    })
    if ($running.Count -gt 0) {
        Write-Host 'Trackvance ya tiene procesos locales activos. Abre http://localhost:3000 o ejecuta scripts/stop-local.ps1 antes de reiniciar.'
        exit 0
    }
}

foreach ($port in @(3000, 8000)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "El puerto $port esta ocupado. Libera ese puerto antes de iniciar Trackvance."
    }
}

if (-not (Test-Path -LiteralPath $pythonExe)) {
    $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $bundledPython) {
        & $bundledPython -m venv (Join-Path $backendRoot '.venv')
    } else {
        python -m venv (Join-Path $backendRoot '.venv')
    }
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear el entorno Python.' }
}
if (-not $SkipInstall) {
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($uvCommand) {
        & $uvCommand.Source sync --frozen --directory $backendRoot --python $pythonExe
    } else {
        throw 'Instala uv con python -m pip install uv y vuelve a ejecutar el script. Se usa uv.lock para conservar las versiones verificadas.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'No se pudieron instalar las dependencias Python.' }
    Push-Location $frontendRoot
    try {
        pnpm install --frozen-lockfile
        if ($LASTEXITCODE -ne 0) { throw 'No se pudieron instalar las dependencias de la interfaz.' }
    } finally { Pop-Location }
}

$env:DATABASE_URL = 'sqlite:///' + (Join-Path $runtimeRoot 'trackvance.db').Replace('\', '/')
$env:TRACKVANCE_STORAGE_ROOT = Join-Path $runtimeRoot 'storage'
$env:TRACKVANCE_STORAGE_DIR = $env:TRACKVANCE_STORAGE_ROOT
$env:TRACKVANCE_SECRETS_DIR = Join-Path $runtimeRoot 'credentials'
$env:TRACKVANCE_SECRET_KEY_FILE = Join-Path $runtimeRoot 'keys\master.key'
$sourceSecretsDir = $env:TRACKVANCE_SECRETS_DIR
$sourceSecretKeyFile = $env:TRACKVANCE_SECRET_KEY_FILE
$isolatedSourceSecretsDir = Join-Path $runtimeRoot 'delivery_worker_no_source_credentials'
$isolatedSourceSecretKeyFile = Join-Path $runtimeRoot 'delivery_worker_no_source_keys\master.key'
$destinationSecretsDir = Join-Path $runtimeRoot 'delivery_credentials'
$destinationSecretKeyFile = Join-Path $runtimeRoot 'delivery_keys\master.key'
$isolatedDestinationSecretsDir = Join-Path $runtimeRoot 'worker_no_delivery_credentials'
$isolatedDestinationSecretKeyFile = Join-Path $runtimeRoot 'worker_no_delivery_keys\master.key'
$env:TRACKVANCE_DESTINATION_SECRETS_DIR = $destinationSecretsDir
$env:TRACKVANCE_DESTINATION_SECRET_KEY_FILE = $destinationSecretKeyFile
$env:TRACKVANCE_WEB_ORIGIN = 'http://localhost:3000'
$env:DEMO_ACCESS_ENABLED = 'true'
$env:DEMO_SEED_ENABLED = 'true'
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$children = [Collections.Generic.List[object]]::new()
function Start-TrackvanceProcess($Name, $Executable, $Arguments, $WorkingDirectory) {
    $p = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimeRoot "$Name.out.log") -RedirectStandardError (Join-Path $runtimeRoot "$Name.err.log")
    $children.Add([PSCustomObject]@{name=$Name; pid=$p.Id; started=$p.StartTime.ToUniversalTime().ToString('o')})
    ConvertTo-Json -InputObject @($children.ToArray()) | Set-Content -LiteralPath $processFile -Encoding utf8
}
try {
    Start-TrackvanceProcess 'api' $pythonExe @('-m','uvicorn','trackvance.api:app','--host','127.0.0.1','--port','8000') $backendRoot
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            $health = Invoke-RestMethod 'http://127.0.0.1:8000/health/ready' -TimeoutSec 2
            $ready = $true
            break
        } catch { Start-Sleep -Milliseconds 1000 }
    }
    if (-not $ready) { throw 'La API no inicio. Consulta .local/api.err.log.' }
    $env:TRACKVANCE_WORKER_LANE = 'DEFAULT'
    $env:TRACKVANCE_DESTINATION_SECRETS_DIR = $isolatedDestinationSecretsDir
    $env:TRACKVANCE_DESTINATION_SECRET_KEY_FILE = $isolatedDestinationSecretKeyFile
    Start-TrackvanceProcess 'worker' $pythonExe @('-m','trackvance.worker') $backendRoot
    $env:TRACKVANCE_WORKER_LANE = 'DELIVERY'
    $env:TRACKVANCE_SECRETS_DIR = $isolatedSourceSecretsDir
    $env:TRACKVANCE_SECRET_KEY_FILE = $isolatedSourceSecretKeyFile
    $env:TRACKVANCE_DESTINATION_SECRETS_DIR = $destinationSecretsDir
    $env:TRACKVANCE_DESTINATION_SECRET_KEY_FILE = $destinationSecretKeyFile
    Start-TrackvanceProcess 'delivery-worker' $pythonExe @('-m','trackvance.worker') $backendRoot
    $env:TRACKVANCE_WORKER_LANE = 'DEFAULT'
    $env:TRACKVANCE_SECRETS_DIR = $sourceSecretsDir
    $env:TRACKVANCE_SECRET_KEY_FILE = $sourceSecretKeyFile
    $env:TRACKVANCE_DESTINATION_SECRETS_DIR = $isolatedDestinationSecretsDir
    $env:TRACKVANCE_DESTINATION_SECRET_KEY_FILE = $isolatedDestinationSecretKeyFile
    $nodeExe = (Get-Command node -ErrorAction Stop).Source
    Start-TrackvanceProcess 'web' $nodeExe @('node_modules/vite/bin/vite.js','--host','127.0.0.1','--port','3000','--strictPort') $frontendRoot
    $webReady = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $null = Invoke-WebRequest 'http://127.0.0.1:3000' -UseBasicParsing -TimeoutSec 2
            $webReady = $true
            break
        } catch { Start-Sleep -Milliseconds 1000 }
    }
    if (-not $webReady) { throw 'La interfaz no inicio. Consulta .local/web.err.log.' }
    Write-Host ''
    Write-Host 'Trackvance Core esta listo: http://localhost:3000' -ForegroundColor Cyan
    Write-Host 'Datos persistentes y registros: .local/'
    Write-Host 'Para detenerlo: powershell -ExecutionPolicy Bypass -File scripts/stop-local.ps1'
} catch {
    & (Join-Path $PSScriptRoot 'stop-local.ps1')
    throw
}
