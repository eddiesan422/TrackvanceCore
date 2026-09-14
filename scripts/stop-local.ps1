$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$processFile = Join-Path $repoRoot '.local\processes.json'
if (-not (Test-Path -LiteralPath $processFile)) {
    Write-Host 'No hay procesos de Trackvance registrados.'
    exit 0
}
function Stop-TrackvanceTree([int]$ProcessId) {
    $descendants = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction SilentlyContinue)
    foreach ($descendant in $descendants) { Stop-TrackvanceTree ([int]$descendant.ProcessId) }
    Stop-Process -Id $ProcessId -ErrorAction SilentlyContinue
}
foreach ($entry in @(Get-Content -LiteralPath $processFile -Raw | ConvertFrom-Json)) {
    $p = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
    if ($p -and $p.StartTime.ToUniversalTime().ToString('o') -eq $entry.started) {
        Stop-TrackvanceTree $p.Id
        Write-Host "Detenido: $($entry.name)"
    }
}
Remove-Item -LiteralPath $processFile
Write-Host 'Los datos permanecen guardados en .local/.'
