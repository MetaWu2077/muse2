<#
Muse Cloud Server - Deploy Script

First-time setup (do this ONCE):
  1. Copy your SSH key to the server:
     type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh ubuntu@118.24.80.184 "cat >> ~/.ssh/authorized_keys"
     (enter password ONCE when prompted)

  2. Then deploy anytime without password:
     .\deploy.ps1

Password-based deploy (requires sshpass):
  choco install sshpass        # one-time install
  .\deploy.ps1 -Password "yourpassword"
#>
param(
    [string]$Password = ""
)

$Server    = "118.24.80.184"
$User      = "ubuntu"
$RemoteDir = "/home/ubuntu/muse-cloud-server"
$LocalDir  = $PSScriptRoot

# ----------------------------------------------------------
# Fill password here (or use -Password parameter)
# Leave empty to use SSH key (recommended)
# ----------------------------------------------------------
if (-not $Password) {
    $Password = ""   # <-- $Password = "yourpassword"
}
# ----------------------------------------------------------

# Build SSH/SCP commands
$useCmdExe = $false

if ($Password) {
    $sshpass = Get-Command sshpass -ErrorAction SilentlyContinue
    if (-not $sshpass) {
        $candidates = @(
            "C:\Program Files\Git\usr\bin\sshpass.exe",
            "C:\Program Files (x86)\Git\usr\bin\sshpass.exe",
            "C:\ProgramData\chocolatey\bin\sshpass.exe",
            "$env:LOCALAPPDATA\Programs\Git\usr\bin\sshpass.exe"
        )
        foreach ($c in $candidates) {
            if (Test-Path $c) { $sshpass = $c; break }
        }
    }
    if ($sshpass) {
        $SSH  = "sshpass -p `"$Password`" ssh -o StrictHostKeyChecking=no"
        $SCP  = "sshpass -p `"$Password`" scp -o StrictHostKeyChecking=no -O"
        $useCmdExe = $true
    } else {
        Write-Host ""
        Write-Host "ERROR: Password set but sshpass not found." -ForegroundColor Red
        Write-Host ""
        Write-Host "Option 1 (recommended): Setup SSH key once, then never enter passwords again:" -ForegroundColor Yellow
        Write-Host "  type `$env:USERPROFILE\.ssh\id_ed25519.pub | ssh ${User}@${Server} `"cat >> ~/.ssh/authorized_keys`"" -ForegroundColor White
        Write-Host ""
        Write-Host "Option 2: Install sshpass for password-based deploy:" -ForegroundColor Yellow
        Write-Host "  choco install sshpass" -ForegroundColor White
        Write-Host ""
        exit 1
    }
} else {
    $SSH  = "ssh"
    $SCP  = "scp -O"
}

# Test SSH connection first
Write-Host ""
Write-Host "=== Muse Cloud Server Deploy ==="
Write-Host "Server: $Server"
Write-Host ""

# Quick connection test
if ($useCmdExe) {
    $testCmd = "$SSH ${User}@${Server} echo OK"
    cmd /c $testCmd 2>&1 | Out-Null
    $ok = ($LASTEXITCODE -eq 0)
} else {
    ssh ${User}@${Server} echo OK 2>&1 | Out-Null
    $ok = ($LASTEXITCODE -eq 0)
}
if (-not $ok) {
    Write-Host "ERROR: Cannot connect to server." -ForegroundColor Red
    Write-Host "Check password, network, or setup SSH key first." -ForegroundColor Yellow
    exit 1
}
Write-Host "Connection: OK"

# === Step 1: Package and upload all files in ONE scp ===
Write-Host "[1/2] Packaging and uploading files..."

$tarName = "deploy.tar.gz"
$tarPath = Join-Path $env:TEMP $tarName
$filesToPack = @()

Push-Location $LocalDir
try {
    $rootFiles = @(
        "server.py", "config.py", "requirements.txt", "dashboard.html",
        "init_db.sql", "session_manager.py", "session_context.py",
        "report_generator.py", ".env"
    )
    foreach ($f in $rootFiles) {
        if (Test-Path $f) { $filesToPack += $f }
    }
    if (Test-Path "storage") {
        Get-ChildItem "storage\*.py" | ForEach-Object {
            $filesToPack += "storage\$($_.Name)"
        }
    }

    # Include amused-src Python modules (report_generator.py depends on muse_raw_stream)
    # Copy to a temp flat dir first, so tar doesn't embed ".." paths
    $amusedsrcDir = "..\amused-src"
    $tempAmused = "amused-src-temp"
    if (Test-Path $amusedsrcDir) {
        New-Item -ItemType Directory -Force $tempAmused | Out-Null
        foreach ($pyFile in @("muse_raw_stream.py", "muse_athena_protocol.py")) {
            $fullPath = Join-Path $amusedsrcDir $pyFile
            if (Test-Path $fullPath) {
                Copy-Item $fullPath (Join-Path $tempAmused $pyFile) -Force
                $filesToPack += "$tempAmused\$pyFile"
            }
        }
    }

    # Create tar.gz
    $tarArgs = @("-czf", $tarPath) + $filesToPack
    & tar $tarArgs 2>$null
    if ($LASTEXITCODE -ne 0) {
        $tarArgs = @("-cf", $tarPath) + $filesToPack
        & tar $tarArgs 2>$null
    }
    if (-not (Test-Path $tarPath)) {
        Write-Host "  ERROR: tar packaging failed" -ForegroundColor Red
        exit 1
    }

    $sizeKB = [math]::Round((Get-Item $tarPath).Length / 1024, 1)
    Write-Host "  Package: $sizeKB KB"

    if ($useCmdExe) {
        $scpCmd = "$SCP `"$tarPath`" ${User}@${Server}:${RemoteDir}/deploy.tar.gz"
        cmd /c $scpCmd
        $ok = ($LASTEXITCODE -eq 0)
    } else {
        scp -O $tarPath ${User}@${Server}:${RemoteDir}/deploy.tar.gz
        $ok = ($LASTEXITCODE -eq 0)
    }
    if (-not $ok) {
        Write-Host "  Upload FAILED!" -ForegroundColor Red
        Remove-Item $tarPath -Force -ErrorAction SilentlyContinue
        exit 1
    }
    Write-Host "  Upload OK"
} finally {
    Pop-Location
    Remove-Item $tarPath -Force -ErrorAction SilentlyContinue
    Remove-Item "amused-src-temp" -Recurse -Force -ErrorAction SilentlyContinue
}

# === Step 2: Extract, install, restart ===
Write-Host "[2/2] Installing and restarting..."

$remoteCmd = "cd $RemoteDir && mkdir -p storage && tar -xzf deploy.tar.gz && rm deploy.tar.gz && mkdir -p ../amused-src && mv amused-src-temp/*.py ../amused-src/ 2>/dev/null; rmdir amused-src-temp 2>/dev/null && pip3 install -r requirements.txt -q 2>&1 | tail -1 && (kill `$(cat /tmp/muse_server.pid 2>/dev/null) 2>/dev/null; sleep 1; true) && nohup python3 server.py > server.log 2>&1 & echo `$! > /tmp/muse_server.pid && sleep 4 && curl -s http://localhost:8000/health && echo '' || echo 'WARN: health check failed, check server.log'"

if ($useCmdExe) {
    $sshCmd = "$SSH ${User}@${Server} ""$remoteCmd"""
    $result = cmd /c $sshCmd 2>&1
    $ok = ($LASTEXITCODE -eq 0)
} else {
    $result = ssh ${User}@${Server} $remoteCmd 2>&1
    $ok = ($LASTEXITCODE -eq 0)
}

Write-Host ""
if ($ok) {
    Write-Host "=== Deploy complete! ==="
    Write-Host "Server response: $result"
    Write-Host "Dashboard: http://${Server}:8000/dashboard"
} else {
    Write-Host "Remote setup FAILED." -ForegroundColor Red
    Write-Host "Response: $result" -ForegroundColor Yellow
}
Write-Host ""
