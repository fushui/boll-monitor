param(
    [ValidateSet("mid", "upper")]
    [string]$ExitLine = "mid",
    [string]$Output = "output/daily_scan.csv"
)

$ErrorActionPreference = "Stop"
python -m boll_strategy scan --provider astock --exit-line $ExitLine --output $Output
exit $LASTEXITCODE
