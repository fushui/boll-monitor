$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = "C:\Python314\python.exe"
$LogDirectory = Join-Path $ProjectRoot "logs"
$LogFile = Join-Path $LogDirectory "push_report.log"

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python not found: $PythonExe"
}

New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
$StartedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -LiteralPath $LogFile -Encoding UTF8 -Value "`r`n[$StartedAt] START"

Push-Location $ProjectRoot
try {
    & $PythonExe "push_report.py" *>> $LogFile
    $ExitCode = $LASTEXITCODE
} catch {
    Add-Content -LiteralPath $LogFile -Encoding UTF8 -Value ("ERROR: " + $_.Exception.Message)
    $ExitCode = 1
} finally {
    Pop-Location
}

$FinishedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -LiteralPath $LogFile -Encoding UTF8 -Value "[$FinishedAt] END exit=$ExitCode"
exit $ExitCode
