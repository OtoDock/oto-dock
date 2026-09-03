# Oto Dock Satellite -- Windows Uninstaller.
#
# Run when:
#   * Manually decommissioning a satellite (interactive: prompts to confirm)
#   * From the tray "Remove this machine" item or the dashboard-delete, via
#     the satellite's _self_uninstall_and_exit (called with -Yes)
#   * From Settings > Apps (the HKCU Add/Remove-Programs QuietUninstallString
#     runs this with -Yes)
#
# All three converge here, so any of them also removes the dashboard row
# (we notify the platform below before wiping).
#
# What gets removed:
#   * The per-user logon Scheduled Task `OtoDockSatellite`
#   * The HKCU Add/Remove-Programs entry
#   * The entire %USERPROFILE%\OtoDock\ directory
#
# Files on the platform (agents, workspaces, OAuth tokens) are NOT
# affected -- they live on the proxy.
#
# Usage:
#     powershell -ExecutionPolicy Bypass -File uninstall.ps1
#     powershell -ExecutionPolicy Bypass -File uninstall.ps1 -Yes

[CmdletBinding()]
param(
    [switch]$Yes,
    # Absolute path to the OtoDock install dir. Defaults to
    # $env:USERPROFILE\OtoDock. The daemon now runs as the real user (not
    # SYSTEM), so the default is correct in every context, but the ARP
    # QuietUninstallString and the satellite's self-uninstall pass it
    # explicitly so the path is never ambiguous.
    [string]$OtoDir = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

if ([string]::IsNullOrWhiteSpace($OtoDir)) {
    $OtoDir = "$env:USERPROFILE\OtoDock"
}
$TaskName = "OtoDockSatellite"
$ArpKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\OtoDockSatellite"

$HasTask = $null -ne (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
$HasDir = Test-Path $OtoDir

if (-not $HasTask -and -not $HasDir) {
    Write-Host "No satellite install detected -- nothing to do."
    # Still clean a stray ARP key if present.
    if (Test-Path $ArpKey) { Remove-Item $ArpKey -Recurse -Force -ErrorAction SilentlyContinue }
    exit 0
}

if (-not $Yes) {
    Write-Warning "This will permanently remove:"
    Write-Warning "  - $OtoDir (satellite code, venv, agents, mcps, config)"
    Write-Warning "  - Scheduled Task '$TaskName' + Add/Remove-Programs entry"
    Write-Warning ""
    Write-Warning "Files on the platform (agents, workspaces) are NOT affected."
    $reply = Read-Host "Continue? [y/N]"
    if ($reply -notmatch '^[Yy]$') {
        Write-Host "Cancelled."
        exit 1
    }
}

# --- Notify platform to auto-clean the dashboard entry ---
# Best-effort: if it fails (network down, platform unreachable, secret
# already invalidated by a prior dashboard-side delete), local cleanup
# still proceeds. The conf file is read BEFORE the dir wipe below.
$Conf = Join-Path $OtoDir "satellite.conf"
if (Test-Path $Conf) {
    $machineId = $null
    $machineSecret = $null
    $platformWss = $null
    foreach ($line in (Get-Content $Conf -Encoding UTF8)) {
        if ($line -match '^\s*machine_id\s*=\s*(.+?)\s*$') { $machineId = $matches[1] }
        elseif ($line -match '^\s*machine_secret\s*=\s*(.+?)\s*$') { $machineSecret = $matches[1] }
        elseif ($line -match '^\s*platform_url\s*=\s*(.+?)\s*$') { $platformWss = $matches[1] }
    }
    # Strip ws:// or wss:// -> http:// or https://, strip /v1/satellite suffix
    if ($platformWss) {
        $platformHttp = $platformWss `
            -replace '^wss://', 'https://' `
            -replace '^ws://', 'http://' `
            -replace '/v1/satellite(/.*)?$', ''
    }
    if ($machineId -and $machineSecret -and $platformHttp) {
        Write-Host "Notifying platform to remove dashboard entry..."
        try {
            $uri = "$platformHttp/v1/satellite/$machineId/self-uninstall"
            Invoke-WebRequest -Method Delete `
                -Headers @{ 'X-Machine-Secret' = $machineSecret } `
                -Uri $uri `
                -TimeoutSec 5 `
                -UseBasicParsing `
                -ErrorAction Stop | Out-Null
            Write-Host "Platform notified."
        } catch {
            Write-Warning "Platform notify failed (continuing): $_"
        }
    }
}

# --- Stop the task + kill the daemon, THEN wipe ---
# The task must be unregistered BEFORE the dir wipe: its RestartCount would
# otherwise respawn the daemon mid-wipe. And the running OtoDockSatellite.exe
# holds handles on its own venv interpreter + loaded .pyd files, so it must
# be killed before those files can be deleted -- the daemon IS the user
# process; there's no separate service to stop it for us.
if ($HasTask) {
    Write-Host "Stopping + unregistering Scheduled Task '$TaskName'..."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}
# Remove the one-shot self-uninstall task that may have launched THIS script
# (satellite self-uninstall registers `OtoDockSatelliteUninstall` to escape
# the daemon's Job Object). Safe no-op for interactive/ARP-triggered runs.
Unregister-ScheduledTask -TaskName "OtoDockSatelliteUninstall" -Confirm:$false -ErrorAction SilentlyContinue

# Kill the daemon process tree (one level of children covers cmd->node CLI
# shims + MCP subprocesses). Releases file locks under satellite\venv so the
# wipe can succeed. CRITICAL: exclude our OWN pid ($PID). When this uninstall.ps1
# was launched by the daemon's self-uninstall, it is itself a CHILD of
# OtoDockSatellite.exe — so a naive "kill all children of the daemon" kills
# THIS script mid-wipe, leaving the OtoDock dir only partially removed (the
# self-uninstall race). Excluding $PID lets us
# still kill the real daemon + MCP/CLI children while surviving to finish.
$self = $PID
$daemon = Get-Process -Name OtoDockSatellite -ErrorAction SilentlyContinue
foreach ($p in $daemon) {
    try {
        Get-CimInstance Win32_Process -Filter "ParentProcessId=$($p.Id)" -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessId -ne $self } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    } catch { }
}
if ($daemon) { Start-Sleep -Milliseconds 500 }

# --- Remove install dir (retry + cmd rmdir fallback for AV-held handles) ---
if ($HasDir) {
    Write-Host "Removing $OtoDir ..."
    $removed = $false
    for ($i = 1; $i -le 12; $i++) {
        Remove-Item $OtoDir -Recurse -Force -ErrorAction SilentlyContinue
        if (-not (Test-Path $OtoDir)) { $removed = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $removed) {
        # Win32 rmdir clears some locks / read-only attrs Remove-Item gives up on.
        cmd.exe /c rmdir /s /q "$OtoDir" 2>&1 | Out-Null
        $removed = -not (Test-Path $OtoDir)
    }
    if (-not $removed) {
        Write-Warning "Some files in $OtoDir could not be deleted (may be in use)."
        Write-Warning "Reboot and delete manually if it persists."
    }
}

# --- Remove the Add/Remove-Programs entry ---
if (Test-Path $ArpKey) {
    Remove-Item $ArpKey -Recurse -Force -ErrorAction SilentlyContinue
}

# --- Remove the Start Menu shortcut (the .vbs launcher lives under $OtoDir,
#     already wiped above; the .lnk is in the per-user Start Menu) ---
$startMenuLnk = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\OtoDock Satellite.lnk"
if (Test-Path $startMenuLnk) {
    Remove-Item $startMenuLnk -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Satellite uninstall complete." -ForegroundColor Green
Write-Host ""
Write-Host "Removed:" -ForegroundColor Cyan
Write-Host "  - Scheduled Task '$TaskName' + Add/Remove-Programs entry"
Write-Host "  - $OtoDir (code, venv, agents, mcps, config, sessions)"
Write-Host ""
Write-Host "Left in place (shared with other software on this machine):" -ForegroundColor Cyan
Write-Host "  - git, gh, python, node, pnpm, uv, jq, ripgrep"
Write-Host "  - npm globals: @anthropic-ai/claude-code, @openai/codex"
Write-Host ""
Write-Host "Pairing OtoDock did not record which of these were already" -ForegroundColor DarkGray
Write-Host "installed before, so removing them automatically would risk" -ForegroundColor DarkGray
Write-Host "breaking unrelated tooling. For a full cleanup, remove them" -ForegroundColor DarkGray
Write-Host "manually:" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  winget uninstall Git.Git GitHub.cli Python.Python.3.12 ``"
Write-Host "                   OpenJS.NodeJS.LTS jqlang.jq ``"
Write-Host "                   BurntSushi.ripgrep.MSVC"
Write-Host "  npm uninstall -g @anthropic-ai/claude-code @openai/codex pnpm"
Write-Host ""
Write-Host "To re-pair, generate a new install command from the dashboard"
Write-Host "(Remote Machines -> Pair new machine)."
