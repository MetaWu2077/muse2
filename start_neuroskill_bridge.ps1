# ============================================================================
# NeuroSkill + OSC-LSL Bridge Launcher
#
# Starts osc_lsl_bridge.py and publishes LSL streams for NeuroSkill.
# Android phone -> WiFi/OSC -> osc_lsl_bridge.py -> LSL -> NeuroSkill
#
# Usage:
#   .\start_neuroskill_bridge.ps1
#   .\start_neuroskill_bridge.ps1 -Port 9000
# ============================================================================

param(
    [int]$Port = 5000,
    [string]$SourceId = "Muse_MuseBridge"
)

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  NeuroSkill + OSC-LSL Bridge Launcher" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Check Python environment
Write-Host "[1/3] Checking Python environment..." -ForegroundColor Yellow
$pyCheck = python -c "import pythonosc; import pylsl; import numpy; print('ok')" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Dependencies missing. Installing..." -ForegroundColor Red
    pip install python-osc pylsl numpy
}
Write-Host "  Python dependencies OK." -ForegroundColor Green

# 2. Check if NeuroSkill daemon is running
Write-Host "[2/3] Checking NeuroSkill daemon..." -ForegroundColor Yellow
$nsPidFile = "$env:LOCALAPPDATA\NeuroSkill\daemon\daemon.pid"
if (Test-Path $nsPidFile) {
    $nsPid = Get-Content $nsPidFile -Raw
    $nsPid = $nsPid.Trim()
    try {
        $proc = Get-Process -Id ([int]$nsPid) -ErrorAction Stop
        Write-Host "  NeuroSkill daemon running (PID: $nsPid)" -ForegroundColor Green
    } catch {
        Write-Host "  NeuroSkill daemon NOT running (stale PID file)" -ForegroundColor Yellow
        Write-Host "  Please launch the NeuroSkill desktop app." -ForegroundColor Yellow
    }
} else {
    Write-Host "  NeuroSkill daemon not detected." -ForegroundColor Yellow
    Write-Host "  Please launch the NeuroSkill desktop app." -ForegroundColor Yellow
}

# 3. Start the bridge
Write-Host "[3/3] Starting OSC-LSL bridge..." -ForegroundColor Yellow
Write-Host ""
Write-Host "Bridge listening on UDP port $Port" -ForegroundColor White
Write-Host "LSL source_id: $SourceId" -ForegroundColor White
Write-Host ""
Write-Host "On your Android phone:" -ForegroundColor Gray
Write-Host "  1. Connect to Muse S via Bluetooth" -ForegroundColor Gray
Write-Host "  2. Ensure same WiFi network as this PC" -ForegroundColor Gray
Write-Host "  3. Start sending OSC data from Muse Bridge app" -ForegroundColor Gray
Write-Host ""
Write-Host "NeuroSkill will auto-discover and connect to the LSL EEG stream." -ForegroundColor Green
Write-Host "Press Ctrl+C to stop." -ForegroundColor Green
Write-Host ""

python osc_lsl_bridge.py --port $Port --source-id $SourceId
