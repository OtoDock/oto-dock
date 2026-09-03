# Oto Dock Satellite -- per-user logon-task entry point (Windows).
#
# The OtoDockSatellite Scheduled Task runs THIS script (not `python -m
# satellite` directly) so we can perform the auto-update swap BEFORE Python
# imports the satellite package. On Windows, Python locks loaded .py/.pyd
# files, so renaming the running satellite/ dir from within Python fails
# mid-swap. When the satellite stages an update it extracts to satellite.new/,
# writes the .update-pending marker, and exits; the task restarts us here and
# the swap happens before any Python imports.
#
# The task is registered (install.ps1) with WorkingDirectory = <OtoDock>
# (the PARENT of the dir we may rename), so the swap's `Move-Item $SatDir`
# never fails with "directory in use as the current working directory".

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Encoding env: keep mojibake out of the log and unbuffer stdout so the
# log stays live.
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

# Resolve paths from this script's location so it works regardless of the
# task's working directory. $PSScriptRoot is always <OtoDock>\satellite\.
$SatDir = $PSScriptRoot
$OtoDir = Split-Path -Parent $SatDir
$Marker = Join-Path $OtoDir ".update-pending"
$RollbackMarker = Join-Path $OtoDir ".rollback-pending"
$UpdateInProgress = Join-Path $OtoDir ".update_in_progress"
$SatNew = Join-Path $OtoDir "satellite.new"
$SatPrev = Join-Path $OtoDir "satellite.previous"
$LogFile = Join-Path $OtoDir "satellite.log"
$RunnerLog = Join-Path $OtoDir "runner.log"

# Append-only runner log so the auto-update SWAP outcome is VISIBLE. The Python
# daemon owns satellite.log; runner.ps1's swap runs pre-import and previously
# left NO trace anywhere, so a silently-failed swap (the slow-Windows-laptop
# orphan-lock) looked like nothing happened. Add-Content is wrapped +
# SilentlyContinue so a logging hiccup can NEVER break the launch (the same
# reason we never stream-redirect to satellite.log — see the hand-off note below).
function Log-Runner {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [runner] $Message"
    Write-Host $line
    # Cap runner.log so it can't grow unbounded over many restarts/updates: when
    # it passes ~1 MB keep only the last 200 lines. Best-effort + SilentlyContinue
    # so a logging hiccup can NEVER break the launch.
    try {
        if ((Test-Path -LiteralPath $RunnerLog) -and
            ((Get-Item -LiteralPath $RunnerLog -ErrorAction SilentlyContinue).Length -gt 1MB)) {
            $tail = Get-Content -LiteralPath $RunnerLog -Tail 200 -ErrorAction SilentlyContinue
            Set-Content -LiteralPath $RunnerLog -Value $tail -ErrorAction SilentlyContinue
        }
    } catch { }
    try { Add-Content -LiteralPath $RunnerLog -Value $line -ErrorAction SilentlyContinue } catch { }
}

# Retrying directory swap: Windows / Defender briefly hold handles on
# freshly-written venv files; the lock surfaces as IOException OR
# UnauthorizedAccessException ("Access is denied"), so we catch ALL
# exceptions. Returns $true on success. On a failed second move it rolls the
# first move back so the box is never left without $Dst (the half-swap wedge
# that bricked the 0.4.27 auto-update).
function Invoke-DirSwap {
    param([string]$Src, [string]$Dst, [string]$Backup)
    $maxAttempts = 12
    $delayMs = 250
    for ($i = 1; $i -le $maxAttempts; $i++) {
        $movedToBackup = $false
        try {
            if (Test-Path $Backup) {
                Remove-Item $Backup -Recurse -Force -ErrorAction SilentlyContinue
                if (Test-Path $Backup) { cmd.exe /c rmdir /s /q "$Backup" 2>&1 | Out-Null }
            }
            if (Test-Path $Dst) {
                Move-Item $Dst $Backup -Force -ErrorAction Stop
                $movedToBackup = $true
            }
            Move-Item $Src $Dst -Force -ErrorAction Stop
            return $true
        } catch {
            if ($movedToBackup -and -not (Test-Path $Dst)) {
                # Second move failed -> undo the first so $Dst always exists.
                try { Move-Item $Backup $Dst -Force -ErrorAction Stop } catch { }
            }
            if ($i -eq $maxAttempts) {
                Log-Runner "swap FAILED after $maxAttempts attempts ($($_.Exception.GetType().Name)): $_"
                return $false
            }
            Log-Runner "swap attempt $i blocked ($($_.Exception.GetType().Name)); retrying in ${delayMs}ms"
            Start-Sleep -Milliseconds $delayMs
            $delayMs = [Math]::Min($delayMs * 2, 4000)
        }
    }
    return $false
}

# Log rotation is owned by the Python daemon (RotatingFileHandler on
# $env:OTO_SATELLITE_LOG, set just before exec below) — runner.ps1 does not
# touch the log file.

# --- Boot-guard rollback (pre-import). The daemon's crash-loop detector
# can't swap back in-process on Windows (the running venv is locked), so it
# drops .rollback-pending and relaunches us. Restore the last known-good
# code from satellite.previous\ BEFORE any Python import. Checked first: a
# rollback supersedes any pending forward update.
if (Test-Path $RollbackMarker) {
    Log-Runner "applying boot-guard rollback (satellite.previous -> satellite)..."
    if (Test-Path $SatPrev) {
        # Swap satellite.previous -> satellite, stashing the broken current
        # build at satellite.new (cleared on the next successful update).
        if (Invoke-DirSwap -Src $SatPrev -Dst $SatDir -Backup $SatNew) {
            Log-Runner "rollback done"
        } else {
            Log-Runner "rollback swap FAILED -- leaving markers for next start"
        }
    } else {
        Log-Runner ".rollback-pending present but satellite.previous\ missing -- clearing"
    }
    # Clear both markers so the rolled-back code starts clean. (Best-effort:
    # if the swap failed above, a retry happens on the next task start.)
    Remove-Item $RollbackMarker -Force -ErrorAction SilentlyContinue
    Remove-Item $UpdateInProgress -Force -ErrorAction SilentlyContinue
}

# --- Apply a staged auto-update (pre-import, so no satellite files are open).
elseif (Test-Path $Marker) {
    Log-Runner "applying staged update (satellite.new -> satellite)..."
    if (-not (Test-Path $SatNew)) {
        Log-Runner ".update-pending present but satellite.new\ missing -- clearing marker"
        Remove-Item $Marker -Force -ErrorAction SilentlyContinue
    } else {
        # Swap satellite.new -> satellite, backing the current build up at
        # satellite.previous (the rollback copy the boot guard / auth-finalize
        # remove once the new code authenticates). Invoke-DirSwap handles the
        # Defender lock window + the half-swap rollback that, left unguarded,
        # bricked the 0.4.27 auto-update on Windows.
        if (Invoke-DirSwap -Src $SatNew -Dst $SatDir -Backup $SatPrev) {
            # Marker cleared only AFTER the swap succeeds.
            Remove-Item $Marker -Force -ErrorAction SilentlyContinue
            Log-Runner "swap done -- now running the staged version"
        } else {
            # Marker left in place: the task's restart-on-failure (or the next
            # logon) retries once the lock clears. Invoke-DirSwap did NOT leave
            # a half-swapped tree, so the OLD code still runs meanwhile.
            Log-Runner "swap NOT applied -- leaving .update-pending; OLD code runs (will retry next start)"
        }
    }
}

# Move into the satellite dir so `python -m satellite` resolves the nested
# package layout (<SatDir>\satellite\__init__.py).
Set-Location $SatDir

$Python = Join-Path $SatDir "venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "[runner] python.exe not found at $Python"
    exit 1
}

# Friendly process name: copy python.exe -> OtoDockSatellite.exe so Task
# Manager shows "OtoDockSatellite.exe" instead of "python.exe". python.exe
# from `python -m venv` is a true interpreter on Windows (not a stub), so a
# byte copy preserves all behaviour. -Force overwrites any prior copy
# (idempotent; survives auto-update venv recreation because runner.ps1 runs
# on every start).
$Branded = Join-Path $SatDir "venv\Scripts\OtoDockSatellite.exe"
try {
    Copy-Item $Python $Branded -Force
} catch {
    Write-Warning "[runner] failed to create $Branded ($_); falling back to python.exe"
    $Branded = $Python
}

# Hand off to Python. The daemon writes its OWN log file (see
# __main__._setup_logging, which adds a FileHandler on
# $env:OTO_SATELLITE_LOG) — runner.ps1 does NOT redirect the streams.
# Reason: the satellite logs to stderr, and a PowerShell stream redirect
# (`*>> $LogFile`) under `$ErrorActionPreference='Stop'` promotes the FIRST
# native-stderr line to a terminating "NativeCommandError", killing the
# daemon on its very first log line and leaving an empty log — exactly what
# broke the 0.5.0 Windows bring-up (Last task result 0x1, empty
# satellite.log). Letting Python own the file sidesteps PowerShell's stream
# machinery entirely. We still drop to 'Continue' so the daemon's own
# non-zero exit isn't re-raised before we can propagate it.
$ErrorActionPreference = 'Continue'
$env:OTO_SATELLITE_LOG = $LogFile
& $Branded -m satellite
exit $LASTEXITCODE
