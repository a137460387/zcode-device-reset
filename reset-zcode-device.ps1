# ZCode Device ID Reset Tool
# Usage: .\reset-zcode-device.ps1

$telemetryFile = "$env:USERPROFILE\.zcode\v2\telemetry-state.json"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "ZCode Device ID Reset Tool" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Step 1: Read current deviceMid
Write-Host "[1/5] Reading current deviceMid..." -ForegroundColor Yellow
$oldMid = "N/A"
if (Test-Path $telemetryFile) {
    $content = Get-Content $telemetryFile -Raw | ConvertFrom-Json
    $oldMid = $content.deviceMid
    Write-Host "   Current deviceMid: $oldMid" -ForegroundColor Green
} else {
    Write-Host "   telemetry-state.json not found." -ForegroundColor Gray
}

# Step 2: Kill zcode processes
Write-Host ""
Write-Host "[2/5] Terminating all zcode processes..." -ForegroundColor Yellow
$processes = @("ZCode", "zcode", "zcode-helper", "zcode-cli")
foreach ($proc in $processes) {
    Get-Process -Name $proc -ErrorAction SilentlyContinue | Stop-Process -Force
}
Start-Sleep -Seconds 3
Write-Host "   Done." -ForegroundColor Green

# Step 3: Delete telemetry file
Write-Host ""
Write-Host "[3/5] Deleting telemetry-state.json..." -ForegroundColor Yellow
if (Test-Path $telemetryFile) {
    Remove-Item $telemetryFile -Force
    Write-Host "   Deleted." -ForegroundColor Green
} else {
    Write-Host "   File not found, skipping." -ForegroundColor Gray
}

# Step 4: Launch zcode
Write-Host ""
Write-Host "[4/5] Launching zcode..." -ForegroundColor Yellow
$zcodePaths = @(
    "$env:LOCALAPPDATA\Programs\zcode\ZCode.exe",
    "$env:LOCALAPPDATA\zcode\ZCode.exe",
    "$env:PROGRAMFILES\zcode\ZCode.exe"
)
$zcodeExe = $zcodePaths | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($zcodeExe) {
    Start-Process $zcodeExe
    Write-Host "   Started: $zcodeExe" -ForegroundColor Green
} else {
    Write-Host "   ZCode.exe not found in common paths." -ForegroundColor Red
    Write-Host "   Please start zcode manually, then press any key..." -ForegroundColor Yellow
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}

Write-Host "   Waiting for zcode to initialize (10 seconds)..." -ForegroundColor Gray
Start-Sleep -Seconds 10

# Step 5: Check new deviceMid
Write-Host ""
Write-Host "[5/5] Checking new deviceMid..." -ForegroundColor Yellow
$newMid = "N/A"
if (Test-Path $telemetryFile) {
    $content = Get-Content $telemetryFile -Raw | ConvertFrom-Json
    $newMid = $content.deviceMid
    Write-Host "   New deviceMid: $newMid" -ForegroundColor Green
} else {
    Write-Host "   telemetry-state.json not yet created." -ForegroundColor Red
    Write-Host "   Please wait and run this script again to verify." -ForegroundColor Yellow
    Read-Host "Press Enter to exit"
    exit
}

# Result
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "RESULT" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "   Old deviceMid: $oldMid" -ForegroundColor White
Write-Host "   New deviceMid: $newMid" -ForegroundColor White
Write-Host ""

if ($oldMid -eq $newMid) {
    Write-Host "   [FAIL] deviceMid unchanged!" -ForegroundColor Red
} else {
    Write-Host "   [SUCCESS] Device ID changed successfully!" -ForegroundColor Green
}
Write-Host "========================================" -ForegroundColor Cyan

Read-Host "Press Enter to exit"
