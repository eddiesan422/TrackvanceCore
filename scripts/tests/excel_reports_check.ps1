[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ExpectedFile,
    [Parameter(Mandatory = $true)][string]$EvidenceDirectory
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-PrivateJson([string]$Target, $Value) {
    $temporary = $Target + '.partial'
    [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 12), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $Target -Force
}
function Release-OwnedCom($Value) {
    if ($null -ne $Value -and [Runtime.InteropServices.Marshal]::IsComObject($Value)) {
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($Value)
    }
}
function Check-Row($Matrix, $ExpectedValues) {
    if ($Matrix.Rank -ne 2 -or $Matrix.GetLength(0) -ne 1 -or $Matrix.GetLength(1) -ne 11) { throw 'EXCEL_ROW_SHAPE' }
    $firstRow = $Matrix.GetLowerBound(0)
    $firstColumn = $Matrix.GetLowerBound(1)
    for ($column = 0; $column -lt 11; $column++) {
        $actual = $Matrix.GetValue($firstRow, $firstColumn + $column)
        $expectedValue = $ExpectedValues[$column]
        if ($null -eq $expectedValue) {
            if ($null -ne $actual) { throw 'EXCEL_NULL_SAMPLE' }
        } elseif ($expectedValue -is [bool]) {
            if ($actual -isnot [bool] -or $actual -ne $expectedValue) { throw 'EXCEL_BOOLEAN_SAMPLE' }
        } elseif ($column -eq 3) {
            if ($actual -isnot [double] -and $actual -isnot [int] -and $actual -isnot [decimal]) { throw 'EXCEL_INTEGER_CELL_TYPE' }
            if ([decimal]$actual -ne [decimal]$expectedValue) { throw 'EXCEL_INTEGER_SAMPLE' }
        } elseif ($column -eq 8 -and $expectedValue -ceq '' -and $null -eq $actual) {
            # Excel may expose explicit empty inline text as an empty cell.
            # XML/openpyxl certify the null/empty distinction for the full file.
        } elseif ($actual -isnot [string] -or $actual -cne $expectedValue) {
            throw ('EXCEL_EXACT_TEXT_SAMPLE_COLUMN_' + $column)
        }
    }
}

$directory = (Resolve-Path -LiteralPath $EvidenceDirectory).Path
$expectedPath = (Resolve-Path -LiteralPath $ExpectedFile).Path
if (-not $directory.Contains([IO.Path]::DirectorySeparatorChar + '.codex-local' + [IO.Path]::DirectorySeparatorChar)) { throw 'EXCEL_PRIVATE_EVIDENCE_REQUIRED' }
if (-not $expectedPath.StartsWith($directory + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'EXCEL_EXPECTATION_OUTSIDE_OWNED_SCOPE' }
$expected = [IO.File]::ReadAllText($expectedPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
$sourceFile = (Resolve-Path -LiteralPath $expected.file).Path
if (-not $sourceFile.Contains([IO.Path]::DirectorySeparatorChar + '.codex-local' + [IO.Path]::DirectorySeparatorChar) -or [IO.Path]::GetExtension($sourceFile) -ne '.xlsx') { throw 'EXCEL_PRIVATE_XLSX_REQUIRED' }
$beforeExcelIds = @(Get-Process -Name EXCEL -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$creationWindow = [DateTime]::UtcNow.ToFileTimeUtc()
$application = $null; $workbooks = $null; $workbook = $null; $sheets = $null; $sheet = $null
$used = $null; $usedRows = $null; $usedColumns = $null; $cells = $null; $sampleRange = $null
$excelProcessId = $null; $owned = $false
$result = [ordered]@{ status = 'FAIL'; source_sha = $expected.source_sha; rows = [long]$expected.rows; columns = 11
    scope = 'EXCEL_NATIVE_OPEN_DIMENSIONS_AND_BOUNDARY_SAMPLES'; full_population_hash_in_excel = $false
    open_policy = 'XL_NORMAL_LOAD_0_NO_RECOVERY'; microsoft_reference = 'https://learn.microsoft.com/en-us/office/vba/api/excel.workbooks.open'
    preexisting_excel_pids = $beforeExcelIds; preexisting_options_touched = $false }
try {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class ExcelReportWindowOwner {
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
}
'@
    $application = New-Object -ComObject Excel.Application
    [uint32]$windowProcessId = 0
    [void][ExcelReportWindowOwner]::GetWindowThreadProcessId([IntPtr]$application.Hwnd, [ref]$windowProcessId)
    $excelProcessId = [int]$windowProcessId
    $excelProcess = Get-Process -Id $excelProcessId
    if ($excelProcessId -le 0 -or $beforeExcelIds -contains $excelProcessId -or $excelProcess.ProcessName -ine 'EXCEL' -or $excelProcess.StartTime.ToUniversalTime().ToFileTimeUtc() -lt $creationWindow) { throw 'EXCEL_NEW_PROCESS_OWNERSHIP_NOT_PROVED' }
    $owned = $true
    $owner = [ordered]@{ status = 'OWNED'; pid = $excelProcessId; process_creation_filetime = $excelProcess.StartTime.ToUniversalTime().ToFileTimeUtc()
        executable = $excelProcess.Path; preexisting_excel_pids = $beforeExcelIds; source_sha = $expected.source_sha }
    Write-PrivateJson (Join-Path $directory 'ownership.json') $owner
    # No application option or process affinity changes occur before the PID
    # has been proved new. A foreign COM instance is only released, never Quit.
    $allowedAffinity = $excelProcess.ProcessorAffinity.ToInt64()
    $oneProcessor = $allowedAffinity -band (-$allowedAffinity)
    if ($oneProcessor -eq 0) { throw 'EXCEL_CPU_AFFINITY_UNAVAILABLE' }
    $excelProcess.ProcessorAffinity = [IntPtr]$oneProcessor
    $excelProcess.Refresh()
    if ($excelProcess.ProcessorAffinity.ToInt64() -ne $oneProcessor) { throw 'EXCEL_CPU_AFFINITY_NOT_APPLIED' }
    $result.active_stage = 'SET_OWNED_APPLICATION_OPTIONS'
    $application.Visible = $false
    $application.DisplayAlerts = $false
    $application.AutomationSecurity = 3
    $workbooks = $application.Workbooks
    $missing = [Type]::Missing
    $result.active_stage = 'OPEN_NORMAL_LOAD'
    # Explicit final argument 0 = xlNormalLoad. Microsoft documents that the
    # object model does not attempt recovery under this load policy.
    [object[]]$openArguments = @($sourceFile, 0, $true, $missing, '', '', $true, $missing, $missing, $false, $false, $missing, $false, $false, 0)
    $workbook = $workbooks.GetType().InvokeMember('Open', [Reflection.BindingFlags]::InvokeMethod, $null, $workbooks, $openArguments)
    $result.active_stage = 'CHECK_DIMENSIONS_AND_FORMULAS'
    $sheets = $workbook.Worksheets
    if ($sheets.Count -ne 1 -or $workbook.ReadOnly -ne $true -or $workbook.FileFormat -ne 51) { throw 'EXCEL_WORKBOOK_FORMAT' }
    $sheet = $sheets.Item(1)
    $used = $sheet.UsedRange
    $usedRows = $used.Rows; $usedColumns = $used.Columns; $cells = $used.Cells
    if ($used.Row -ne 1 -or $used.Column -ne 1 -or $usedRows.Count -ne ([long]$expected.rows + 1) -or $usedColumns.Count -ne 11) { throw 'EXCEL_COMPLETE_DIMENSIONS' }
    if ($cells.HasFormula -isnot [bool] -or $cells.HasFormula -ne $false) { throw 'EXCEL_FORMULA_CREATED' }
    $sampleRange = $sheet.Range('A1:K1')
    $result.active_stage = 'CHECK_HEADERS'
    $header = $sampleRange.Value2
    for ($column = 0; $column -lt 11; $column++) {
        if ($header.GetValue($header.GetLowerBound(0), $header.GetLowerBound(1) + $column) -cne $expected.headers[$column]) { throw 'EXCEL_HEADERS' }
    }
    Release-OwnedCom $sampleRange; $sampleRange = $null
    foreach ($sample in $expected.samples) {
        $result.active_stage = 'CHECK_BOUNDARY_SAMPLES'
        $sampleRange = $sheet.Range(('A' + $sample.worksheet_row + ':K' + $sample.worksheet_row))
        Check-Row $sampleRange.Value2 $sample.values
        Release-OwnedCom $sampleRange; $sampleRange = $null
    }
    $result.status = 'PASS'
    $result.excel_process_id = $excelProcessId
    $result.excel_version = $application.Version
    $result.excel_build = $application.Build
    $result.physical_rows = $usedRows.Count
    $result.formulas_present = $false
    $result.boundary_typed_values = 'PASS'
    $result.excel_cpu_affinity_mask = $oneProcessor
    $result.sample_worksheet_rows = @($expected.samples | ForEach-Object { $_.worksheet_row })
    $result.null_empty_distinction = 'FULL_XML_AND_OPENPYXL_ORACLES_ONLY'
    $result.active_stage = 'COMPLETE'
} catch {
    [IO.File]::WriteAllText((Join-Path $directory 'native-error.private.txt'), $_.Exception.ToString(), [Text.UTF8Encoding]::new($false))
    $result.error_type = $_.Exception.GetType().Name
    $underlyingException = $_.Exception
    while ($null -ne $underlyingException.InnerException) { $underlyingException = $underlyingException.InnerException }
    $result.error_hresult = ('0x{0:X8}' -f ($underlyingException.HResult -band 0xffffffffL))
    $result.error_script_line = $_.InvocationInfo.ScriptLineNumber
    $code = $_.Exception.Message
    $result.error_code = if ($code -match '^EXCEL_[A-Z_0-9]+$') { $code } else { 'EXCEL_NORMAL_OPEN_OR_COM_FAILED' }
} finally {
    foreach ($value in @($sampleRange, $cells, $usedColumns, $usedRows, $used, $sheet, $sheets)) { Release-OwnedCom $value }
    if ($owned) {
        try { if ($null -ne $workbook) { $workbook.Close($false) } }
        catch { $result.status = 'FAIL'; $result.cleanup_error = 'EXCEL_OWNED_WORKBOOK_CLOSE_FAILED' }
        try { $application.Quit() }
        catch { $result.status = 'FAIL'; $result.cleanup_error = 'EXCEL_OWNED_APPLICATION_QUIT_FAILED' }
    }
    foreach ($value in @($workbook, $workbooks, $application)) { Release-OwnedCom $value }
    [GC]::Collect(); [GC]::WaitForPendingFinalizers()
    $result.owned_instance_verified = $owned
    Write-PrivateJson (Join-Path $directory 'excel-result.json') $result
}
if ($result.status -ne 'PASS') { exit 1 }
