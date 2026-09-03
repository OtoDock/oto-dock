# Oto Dock Satellite Installer -- Windows.
#
# This script is a TEMPLATE. The platform's
# `GET /v1/satellite/bootstrap?os=windows` endpoint generates a
# self-extracting PowerShell wrapper that defines the env vars below
# (MACHINE_ID, MACHINE_SECRET, PLATFORM_URL) and embeds the baseline-
# tools script + satellite tarball as base64 payloads, then concatenates
# this template at the bottom.
#
# Usage (end-user, via dashboard install command):
#     powershell -ExecutionPolicy Bypass -Command "& { $r = Invoke-WebRequest -Headers @{'X-Pairing-Token'='<TOK>'} 'https://<platform>/v1/satellite/bootstrap?os=windows'; Invoke-Expression $r.Content }"
#
# Repo-local dev invocation:
#     $env:MACHINE_ID = "..."; $env:MACHINE_SECRET = "..."; $env:PLATFORM_URL = "wss://..."
#     $env:SATELLITE_REPO = "C:\path\to\oto-dock"
#     powershell -ExecutionPolicy Bypass -File install.ps1
#
# Runs as the CURRENT USER -- no administrator rights needed. The satellite
# is registered as a per-user logon Scheduled Task (Task Scheduler + an
# HKCU Add/Remove-Programs entry), NOT a system service, so a compromise
# can't reach SYSTEM and uninstall needs no elevation. (Baseline tools may
# prompt once for elevation if a package lacks a user-scope install.)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---- Disable console QuickEdit mode -----------------------------------
#
# Windows consoles ship with "QuickEdit Mode" ON. The instant the user
# CLICKS anywhere in the window (e.g. to select/copy text), the console
# enters selection mode and SUSPENDS the foreground child process the
# moment its output buffer fills — pip/venv creation appears to "freeze"
# for minutes until the user presses a key / Ctrl+C, which exits selection
# mode and the process resumes (exactly the "it continued when I hit
# Ctrl+C" symptom). Clear ENABLE_QUICK_EDIT_MODE (0x40) on stdin so a stray
# click can't pause the install. Best-effort: wrapped in try/catch and
# skipped when there's no real console (piped / non-interactive run).
try {
    if (-not ([System.Management.Automation.PSTypeName]'OtoConsole').Type) {
        Add-Type -Name OtoConsole -Namespace Win32 -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError=true)]
public static extern System.IntPtr GetStdHandle(int handle);
[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError=true)]
public static extern bool GetConsoleMode(System.IntPtr handle, out int mode);
[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError=true)]
public static extern bool SetConsoleMode(System.IntPtr handle, int mode);
'@
    }
    $STD_INPUT_HANDLE = -10
    $ENABLE_QUICK_EDIT = 0x40
    $ENABLE_EXTENDED   = 0x80
    $h = [Win32.OtoConsole]::GetStdHandle($STD_INPUT_HANDLE)
    $mode = 0
    if ([Win32.OtoConsole]::GetConsoleMode($h, [ref]$mode)) {
        # Clear QuickEdit; ExtendedFlags must be set for the change to apply.
        $new = ($mode -band (-bnot $ENABLE_QUICK_EDIT)) -bor $ENABLE_EXTENDED
        [void][Win32.OtoConsole]::SetConsoleMode($h, $new)
    }
} catch {
    # No console / not supported — install proceeds; a click could still
    # pause it, but that's the pre-existing default, not a failure.
}

# ---- Required vars (set by the bootstrap wrapper) ---------------------

# Small identifiers come in via $env: (visible to child processes).
# Large payloads come in via PowerShell script variables -- Windows
# enforces a 32,767-char per-env-var limit, and the tarball is ~480 KB
# base64. Initialize the payload vars to $null so StrictMode doesn't
# trip when this script runs in dev mode (SATELLITE_REPO instead of
# embedded payload).
if (-not (Test-Path Variable:BASELINE_TOOLS_B64))     { $BASELINE_TOOLS_B64     = $null }
if (-not (Test-Path Variable:SATELLITE_TARBALL_B64))  { $SATELLITE_TARBALL_B64  = $null }

if (-not $env:MACHINE_ID -or -not $env:MACHINE_SECRET -or -not $env:PLATFORM_URL) {
    Write-Host "ERROR: Missing MACHINE_ID, MACHINE_SECRET, or PLATFORM_URL." -ForegroundColor Red
    Write-Host "This script is invoked via the platform's bootstrap endpoint,"
    Write-Host "which sets these automatically:"
    Write-Host ""
    Write-Host "  powershell -ExecutionPolicy Bypass -Command `"& { iex (iwr -Headers @{'X-Pairing-Token'='<TOK>'} 'https://<platform>/v1/satellite/bootstrap?os=windows').Content }`""
    exit 1
}

# ---- Layout ------------------------------------------------------------

$OtoDir = "$env:USERPROFILE\OtoDock"
$SatDir = Join-Path $OtoDir "satellite"
$AgentsDir = Join-Path $OtoDir "agents"
$McpsDir = Join-Path $OtoDir "mcps"
$BinDir = Join-Path $SatDir "bin"
$VenvDir = Join-Path $SatDir "venv"
$TaskName = "OtoDockSatellite"

function Info { param([string]$m) Write-Host "[INFO]  $m" -ForegroundColor Blue }
function Ok   { param([string]$m) Write-Host "[OK]    $m" -ForegroundColor Green }
function Warn { param([string]$m) Write-Warning $m }
function Err  { param([string]$m) Write-Host "[ERROR] $m" -ForegroundColor Red }

Info "Installing to $OtoDir (per-user, no admin)"

# ---- Step 0: clean up any previous install (idempotent re-run) --------
#
# A prior install registers a logon Scheduled Task whose runner holds
# handles on files under satellite\. Stop the task + kill the process
# before wiping satellite\, else the wipe fails with a sharing violation.
# The per-user model never creates a Windows service, so there's nothing
# else to clean up.

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Info "Found existing $TaskName task from a previous install; removing..."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}
# Kill any lingering daemon process so its files unlock before the wipe.
Get-Process -Name OtoDockSatellite -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500

if (Test-Path $SatDir) {
    Info "Wiping previous satellite\ for a clean install..."
    # Retry -- Defender / antivirus may briefly hold handles on freshly-
    # touched binaries (esp. venv\Scripts\*.exe). ~6s window.
    for ($i = 1; $i -le 12; $i++) {
        Remove-Item $SatDir -Recurse -Force -ErrorAction SilentlyContinue
        if (-not (Test-Path $SatDir)) { break }
        Start-Sleep -Milliseconds 500
    }
    if (Test-Path $SatDir) {
        # Last resort: cmd's rmdir uses Win32 APIs directly and clears some
        # locks / read-only attrs that Remove-Item gives up on.
        cmd.exe /c rmdir /s /q "$SatDir" 2>&1 | Out-Null
    }
    if (Test-Path $SatDir) {
        Err "Could not wipe $SatDir -- a file is still locked."
        Err "Try rebooting (releases all file locks) and re-running the installer."
        exit 1
    }
}

# ---- Step 1: baseline tools -------------------------------------------

if ($env:SKIP_BASELINE_TOOLS -eq "true") {
    Info "Skipping baseline tools (SKIP_BASELINE_TOOLS=true)"
} elseif ($BASELINE_TOOLS_B64) {
    Info "Running embedded baseline-tools installer..."
    $tmp = Join-Path $env:TEMP "oto-baseline-tools-$([Guid]::NewGuid()).ps1"
    try {
        [System.IO.File]::WriteAllBytes(
            $tmp, [System.Convert]::FromBase64String($BASELINE_TOOLS_B64)
        )
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $tmp
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
} elseif ($env:SATELLITE_REPO `
          -and (Test-Path "$env:SATELLITE_REPO\scripts\install-baseline-tools.ps1")) {
    Info "Running repo-local baseline-tools..."
    & powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File "$env:SATELLITE_REPO\scripts\install-baseline-tools.ps1"
} else {
    Warn "No baseline-tools payload available -- skipping. Python/Node/Git"
    Warn "must already be installed (scripts/install-baseline-tools.ps1 has the set)."
}

# Refresh PATH so baseline-tools-installed binaries are visible.
$env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' `
          + [System.Environment]::GetEnvironmentVariable('Path','User')

# ---- Step 2: Python check ---------------------------------------------
#
# We resolve to an ABSOLUTE python.exe path (not the `py` launcher) so
# subsequent `& $PythonCmd -m venv ...` invocations behave reliably.
# `py.exe -3 -m ...` has known quirks under `& $py @args` splatting that
# can silently drop to the interactive REPL.

$PythonCmd = $null

function Test-PythonVersion {
    param([string] $Exe)
    if (-not $Exe -or -not (Test-Path $Exe)) { return $null }
    # Probe defensively. On a fresh Windows, the python.exe / python3.exe that
    # Get-Command resolves are the Microsoft Store "App execution alias" stubs
    # under %LOCALAPPDATA%\Microsoft\WindowsApps: they satisfy Get-Command /
    # Test-Path but, when no real Python is installed, print "Python was not
    # found..." to stderr and exit 9009. Under this script's
    # $ErrorActionPreference='Stop' (also set by the bootstrap header), that
    # native-command stderr is promoted to a TERMINATING error — which would
    # abort the whole installer right here instead of letting us fall through to
    # the py-launcher / direct-path fallbacks below. Relax the preference
    # locally (function scope only) and wrap in try/catch so a stub degrades to
    # $null, never a crash.
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        $verOut = (& $Exe --version 2>&1 | ForEach-Object { "$_" }) -join "`n"
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0) { return $null }
    # A stub can occasionally exit 0 while still emitting the redirect notice;
    # treat any "not found" / Store / alias banner as "not a real Python".
    if ($verOut -match 'was not found|Microsoft Store|App execution alias') {
        return $null
    }
    if ($verOut -match 'Python (\d+)\.(\d+)') {
        return @{ Major = [int]$matches[1]; Minor = [int]$matches[2] }
    }
    return $null
}

# Try python / python3 on PATH first.
foreach ($candidate in @('python', 'python3')) {
    $resolved = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $resolved) { continue }
    $v = Test-PythonVersion $resolved.Source
    if ($v -and $v.Major -ge 3 -and $v.Minor -ge 10) {
        $PythonCmd = $resolved.Source
        Ok "Python $($v.Major).$($v.Minor) found ($PythonCmd)"
        break
    }
}

# Fall back: ask the py launcher for the real sys.executable path so we
# can invoke it directly afterwards. Wrapped in try/catch: `py -3` throws
# under $ErrorActionPreference='Stop' when the launcher exists but has no
# 3.x registered (another fresh-machine state).
if (-not $PythonCmd) {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        try {
            $resolvedExe = (& $pyLauncher.Source -3 -c "import sys; print(sys.executable)" 2>&1 |
                            ForEach-Object { "$_" } | Select-Object -Last 1).Trim()
        } catch { $resolvedExe = $null }
        $v = Test-PythonVersion $resolvedExe
        if ($v -and $v.Major -ge 3 -and $v.Minor -ge 10) {
            $PythonCmd = $resolvedExe
            Ok "Python $($v.Major).$($v.Minor) found via py launcher: $PythonCmd"
        }
    }
}

# Final fallback: scan the well-known winget / python.org install dirs
# directly. Covers the case where a real Python IS installed but the
# WindowsApps alias shadows it on PATH, or PATH hasn't refreshed in this
# session yet. winget Python.Python.3.12 (per-user) lands under
# %LOCALAPPDATA%\Programs\Python\Python3xx\; machine-wide installs under
# %ProgramFiles%\Python3xx\.
if (-not $PythonCmd) {
    $pyGlobs = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe'),
        (Join-Path ${env:ProgramFiles} 'Python3*\python.exe')
    )
    foreach ($glob in $pyGlobs) {
        $hits = Get-ChildItem -Path $glob -ErrorAction SilentlyContinue |
                Sort-Object FullName -Descending
        foreach ($hit in $hits) {
            $v = Test-PythonVersion $hit.FullName
            if ($v -and $v.Major -ge 3 -and $v.Minor -ge 10) {
                $PythonCmd = $hit.FullName
                Ok "Python $($v.Major).$($v.Minor) found at $PythonCmd"
                break
            }
        }
        if ($PythonCmd) { break }
    }
}

if (-not $PythonCmd) {
    Err "Python 3.10+ required but not found after baseline-tools install."
    Err "Most often this is the Windows 'App execution aliases' for Python:"
    Err "if typing 'python' opens the Microsoft Store or prints 'Python was"
    Err "not found', those stubs are shadowing the real install. Fix via"
    Err "Settings > Apps > Advanced app settings > App execution aliases —"
    Err "turn OFF python.exe and python3.exe, then re-run this installer."
    Err "Or install Python manually: winget install -e --id Python.Python.3.12"
    exit 1
}

# ---- Step 3: long path support (best effort) --------------------------
#
# Writes HKLM, so it needs admin. Under the per-user (no-admin) install
# this simply fails and is skipped -- long agent paths (>260 chars) may
# then fail, but that's rare. Enable manually (elevated) if needed.

try {
    Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' `
        -Name 'LongPathsEnabled' -Value 1 -Type DWord -ErrorAction Stop
    Info "Long path support enabled (effective after reboot)"
} catch {
    # Expected on a per-user (non-admin) install — this HKLM tweak is
    # OPTIONAL, not required. Info (not Warn) so it doesn't read as a failure.
    Info "Skipped optional long-path registry tweak (needs admin; not required)."
}

# ---- Step 4: directory tree -------------------------------------------

Info "Creating directory structure at $OtoDir..."
foreach ($d in @($SatDir, $AgentsDir, $McpsDir)) {
    New-Item -ItemType Directory -Path $d -Force | Out-Null
}
Ok "Directories created"

# ---- Step 5: extract satellite tarball --------------------------------

if ($SATELLITE_TARBALL_B64) {
    Info "Extracting embedded satellite package..."
    $tarball = Join-Path $env:TEMP "oto-satellite-$([Guid]::NewGuid()).tar.gz"
    try {
        [System.IO.File]::WriteAllBytes(
            $tarball, [System.Convert]::FromBase64String($SATELLITE_TARBALL_B64)
        )
        # tar.exe ships with Windows 10 1803+ at C:\Windows\System32\tar.exe.
        # Step 0 wiped $SatDir so there should be no extant files to clash,
        # but include implicit overwrite anyway.
        & tar -xzf $tarball -C $SatDir
        if ($LASTEXITCODE -ne 0) {
            throw "tar extraction failed (exit $LASTEXITCODE). " +
                  "Reboot to release file locks, then re-run the installer."
        }
        Ok "Satellite package extracted"
    } finally {
        Remove-Item $tarball -Force -ErrorAction SilentlyContinue
    }
} elseif ($env:SATELLITE_REPO -and (Test-Path "$env:SATELLITE_REPO\satellite")) {
    Info "Copying satellite package from $env:SATELLITE_REPO (repo-local install)..."
    Copy-Item -Recurse -Force "$env:SATELLITE_REPO\satellite\*" $SatDir
    Ok "Satellite package copied"
} else {
    Err "No satellite payload available (set SATELLITE_TARBALL_B64 or SATELLITE_REPO)."
    exit 1
}

# ---- Step 6: venv + Python deps ---------------------------------------

if (-not (Test-Path $VenvDir)) {
    Info "Creating venv at $VenvDir ..."
    & $PythonCmd -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        Err "venv creation failed"
        exit 1
    }
}
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip = Join-Path $VenvDir "Scripts\pip.exe"

Info "Installing Python dependencies..."
# --no-input: never block waiting on stdin (belt-and-suspenders alongside the
# QuickEdit fix above). --progress-bar off: pip's animated bar is noise when
# stdout isn't a TTY. -q keeps it quiet otherwise.
& $VenvPy -m pip install -q --no-input --upgrade pip
& $VenvPip install -q --no-input --progress-bar off -r (Join-Path $SatDir "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Err "pip install failed"
    exit 1
}
Ok "Python dependencies installed"

# ---- Step 7: satellite.conf -------------------------------------------

# Use forward slashes in the INI to dodge configparser's backslash-escape
# eating. Python's pathlib handles forward slashes transparently on Windows.
function ToForward { param([string]$p) return $p.Replace('\', '/') }

# Bare CLI names on purpose: the daemon resolves the actual binary at
# runtime (pin-verified, npm dirs scanned explicitly — host/cli_versions.py),
# so the conf must not freeze whatever this installer shell happens to
# resolve. A capture here once pinned a stale shadowed copy for good.

Info "Writing satellite.conf..."
$confPath = Join-Path $OtoDir "satellite.conf"
$confContent = @"
[satellite]
machine_id = $env:MACHINE_ID
machine_secret = $env:MACHINE_SECRET
platform_url = $env:PLATFORM_URL
agents_dir = $(ToForward $AgentsDir)
mcps_dir = $(ToForward $McpsDir)

[cli]
claude_bin = claude

[codex]
codex_bin = codex
"@
# Write UTF-8 *without* BOM. Windows PowerShell 5.1's `Set-Content
# -Encoding UTF8` prepends a BOM, which Python's configparser rejects
# as garbage in the first section header ('﻿[satellite]'). Using
# .NET directly with UTF8Encoding(False) is the portable way to write
# BOM-less UTF-8 from any PS version.
[System.IO.File]::WriteAllText(
    $confPath, $confContent, [System.Text.UTF8Encoding]::new($false)
)
# Restrict to current user -- secret in plaintext on disk. The daemon runs
# as this user (no SYSTEM), so only the user needs access.
icacls $confPath /inheritance:r /grant:r "$($env:USERNAME):F" 2>&1 | Out-Null
Ok "Configuration written"

# ---- Step 8: register per-user logon Scheduled Task -------------------
#
# Per-user model: a logon-triggered Scheduled Task runs the satellite in
# the installing user's own interactive session (RunLevel Limited = no
# admin token), so the daemon can draw a tray icon and a compromise can't
# reach SYSTEM. runner.ps1 performs the pre-import auto-update swap, then
# launches `python -m satellite`.

$RunnerPs1 = Join-Path $SatDir "runner.ps1"
if (-not (Test-Path $RunnerPs1)) {
    Err "runner.ps1 not found in the satellite package at $RunnerPs1"
    exit 1
}

$principalId = "$env:USERDOMAIN\$env:USERNAME"
Info "Registering scheduled task '$TaskName' for $principalId (no admin)..."

# conhost --headless: Win11 delegates console apps to Windows Terminal by
# default, and WT IGNORES -WindowStyle Hidden for scheduled tasks — the
# daemon ran inside a VISIBLE terminal window that killed it when closed
# (Win10's legacy conhost honors the hide, hence the machine split).
# Forcing a headless classic console host shows no window under either
# terminal; conhost propagates the child's exit code, so the task's
# restart-on-failure (boot-guard) semantics are preserved.
$action = New-ScheduledTaskAction `
    -Execute "conhost.exe" `
    -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$RunnerPs1`"" `
    -WorkingDirectory $OtoDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $principalId
$principal = New-ScheduledTaskPrincipal -UserId $principalId `
    -LogonType Interactive -RunLevel Limited
# Restart-on-FAILURE (3x, 1 min apart) is what lets the boot-guard's
# crash-attempt counter advance. A clean exit-0 (update / rollback) is
# NOT auto-restarted -- the daemon relaunches
# itself via `schtasks /Run` (config.py::_relaunch_self). IgnoreNew keeps a
# second instance from starting if one is already running. ExecutionTimeLimit
# 0 = run forever. Battery flags keep it alive on laptops.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Ok "Scheduled task registered"

# Start now so the install doesn't require a logoff/login.
Info "Starting $TaskName..."
Start-ScheduledTask -TaskName $TaskName

# ---- Step 8b: Add/Remove Programs entry (HKCU, no admin) --------------
#
# Lets the user uninstall from Settings > Apps. Converges on the SAME
# uninstall.ps1 as the tray "Remove this machine" and the dashboard-delete,
# so a Control-Panel uninstall also removes the dashboard row.

# DisplayVersion from the extracted config.py (nested package layout on a
# tarball install, flat on a repo-local copy -- check both).
$satVersion = ""
foreach ($c in @((Join-Path $SatDir "satellite\config.py"), (Join-Path $SatDir "config.py"))) {
    if (Test-Path $c) {
        $vm = Select-String -Path $c -Pattern 'SATELLITE_VERSION\s*=\s*"([^"]+)"' `
              -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($vm) { $satVersion = $vm.Matches[0].Groups[1].Value; break }
    }
}
$arpKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\OtoDockSatellite"
$uninstallPs1 = Join-Path $SatDir "uninstall.ps1"
$arpIcon = Join-Path $BinDir "otodock.ico"
$uninstallCmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstallPs1`" -OtoDir `"$OtoDir`""
New-Item -Path $arpKey -Force | Out-Null
Set-ItemProperty $arpKey -Name DisplayName          -Value "OtoDock Satellite"
Set-ItemProperty $arpKey -Name UninstallString      -Value $uninstallCmd
Set-ItemProperty $arpKey -Name QuietUninstallString -Value "$uninstallCmd -Yes"
Set-ItemProperty $arpKey -Name Publisher            -Value "OtoDock"
Set-ItemProperty $arpKey -Name InstallLocation      -Value $OtoDir
if ($satVersion)        { Set-ItemProperty $arpKey -Name DisplayVersion -Value $satVersion }
if (Test-Path $arpIcon) { Set-ItemProperty $arpKey -Name DisplayIcon    -Value $arpIcon }
Set-ItemProperty $arpKey -Name NoModify -Value 1 -Type DWord
Set-ItemProperty $arpKey -Name NoRepair -Value 1 -Type DWord
Ok "Add/Remove Programs entry created"

# ---- Step 8b-2: Start Menu launcher (re-open after Quit) --------------
#
# Show the satellite as a normal app in the Start Menu so the user can
# relaunch it after the tray "Quit" WITHOUT logging off. The shortcut runs a
# tiny VBScript that triggers the existing logon Scheduled Task: this is
# silent (no console window) and single-instance (the task's
# MultipleInstances=IgnoreNew makes a second click a no-op while it's running),
# and it reuses runner.ps1's staged-update swap. Clicking it shows nothing on
# screen — the tray icon just appears and connects.
$launcherVbs = Join-Path $OtoDir "start-tray.vbs"
@'
' OtoDock Satellite -- silent tray launcher (no console window). Starts the
' daemon via its logon Scheduled Task: launches it if it was Quit, no-ops if
' already running. Invoked by the Start Menu shortcut.
CreateObject("WScript.Shell").Run "schtasks /Run /TN OtoDockSatellite", 0, False
'@ | Set-Content -Path $launcherVbs -Encoding ASCII

try {
    $startMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
    if (-not (Test-Path $startMenuDir)) { New-Item -ItemType Directory -Path $startMenuDir -Force | Out-Null }
    $lnkPath = Join-Path $startMenuDir "OtoDock Satellite.lnk"
    $wsh = New-Object -ComObject WScript.Shell
    $lnk = $wsh.CreateShortcut($lnkPath)
    $lnk.TargetPath       = "wscript.exe"
    $lnk.Arguments        = "`"$launcherVbs`""
    $lnk.WorkingDirectory = $OtoDir
    if (Test-Path $arpIcon) { $lnk.IconLocation = $arpIcon }
    $lnk.Description       = "Start the OtoDock Satellite (connects in the background; no window)"
    $lnk.Save()
    Ok "Start Menu shortcut created (re-open after Quit)"
} catch {
    Warn "Could not create Start Menu shortcut (non-fatal): $_"
}

# ---- Step 8c: verify it came up --------------------------------------

Start-Sleep -Seconds 3
$proc = Get-Process -Name OtoDockSatellite -ErrorAction SilentlyContinue
if (-not $proc) {
    Warn "Satellite process not visible yet. The task is registered and will"
    Warn "(re)start at logon. If it never comes up, run runner.ps1 directly to"
    Warn "see the error:"
    Warn "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$RunnerPs1`""
    $taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($taskInfo) { Warn ("  Last task result: 0x{0:X}" -f $taskInfo.LastTaskResult) }
} else {
    Ok "Satellite running"
}

# ---- Done -------------------------------------------------------------

Write-Host ""
Write-Host "Satellite installation complete!" -ForegroundColor Green
Write-Host ""
Write-Host "Machine ID: $env:MACHINE_ID"
Write-Host ""
Write-Host "Running as: $principalId (no admin / not SYSTEM)"
Write-Host "Status: Get-ScheduledTask $TaskName | Get-ScheduledTaskInfo"
Write-Host "Logs:   Get-Content $OtoDir\satellite.log -Wait"
Write-Host ""
Write-Host "Autostart: starts automatically when you log in." -ForegroundColor Cyan
Write-Host "  Pause now ............ right-click the OtoDock tray icon -> Pause"
Write-Host "  Stop now ............. tray icon -> Quit"
Write-Host "  Re-open after Quit ... Start Menu -> 'OtoDock Satellite'  (silent; tray only)"
Write-Host "  Turn OFF autostart ... Task Scheduler (taskschd.msc) ->"
Write-Host "                         Task Scheduler Library -> $TaskName -> Disable"
Write-Host "                         (or: Disable-ScheduledTask -TaskName $TaskName)"
Write-Host "  Turn ON  autostart ... same place -> Enable"
Write-Host "  Remove completely .... Settings > Apps > OtoDock Satellite > Uninstall"
