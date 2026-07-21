# Oto Dock baseline dev tooling installer -- Windows.
#
# Parallel to scripts/install-baseline-tools.sh. Single source of truth
# for what a Windows host needs:
#   Tier 1 (always): git, gh, python3 + pipx, node + npm + pnpm, uv,
#                    jq, ripgrep, curl (built-in on Win10+)
#   CLIs:    claude (npm), codex (npm)
#
# Idempotent -- re-running is a no-op for already-installed tools. Each
# install wrapped in try/catch so a single failure doesn't abort the
# whole run; final report lists any skipped tools.
#
# Usage (typically called from install.ps1, not run directly):
#     powershell -ExecutionPolicy Bypass -File install-baseline-tools.ps1
#
# Opt-outs (mirror the Linux script):
#     $env:SKIP_TIER_1        = "true"   # skip Tier 1 winget packages
#     $env:SKIP_CLAUDE_CLI    = "true"   # skip claude + codex npm installs

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
# Continue on per-package errors -- we want to install as much as
# possible even if individual packages fail.
$ErrorActionPreference = 'Continue'

# ---- Prereq check ------------------------------------------------------

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Warning "winget not found. This installer needs winget (Windows 10 1809+ or Windows 11)."
    Write-Warning "Install 'App Installer' from the Microsoft Store, or upgrade Windows."
    exit 1
}

# ---- Helpers -----------------------------------------------------------

function Refresh-Path {
    # winget installs update HKLM/HKCU PATH but the current PS session
    # doesn't see new entries until we rebuild $env:Path from the registry.
    $machine = [System.Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [System.Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Invoke-Native {
    # Run a native command and capture all output (stdout + stderr) as
    # plain strings, then print only on failure. Without this wrapper, the
    # naive ``... 2>&1 | Out-String | Write-Host`` pattern PowerShell uses
    # everywhere renders each stderr line from native commands (npm, pip,
    # winget, etc.) as a scary multi-line ``NativeCommandError`` block —
    # even when the command's exit code was 0 and the stderr text was a
    # harmless info notice (e.g. pip's "A new release of pip is
    # available"). End-users running install.ps1 see red text and assume
    # something broke. Capturing into a variable + only echoing on real
    # failure (non-zero $LASTEXITCODE) keeps the install log clean.
    #
    # The ``ForEach-Object { "$_" }`` coercion is load-bearing: ErrorRecord
    # objects coming through ``2>&1`` are converted to their .ToString()
    # message text (single line) instead of being rendered by PowerShell's
    # default formatter (multi-line traceback). Without it, captured
    # output still prints as a fake traceback when echoed on failure.
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Cmd
    )
    $output = & $Cmd 2>&1 | ForEach-Object { "$_" }
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        return $true
    }
    Write-Warning "[baseline]   $Name failed (exit $exitCode):"
    foreach ($line in $output) {
        if ($line) { Write-Host "    $line" }
    }
    return $false
}

function Get-RealPythonExe {
    # Absolute path to a REAL Python >=3.10, or $null.
    #
    # Do NOT trust a bare `Get-Command python`: on a fresh Windows it resolves
    # to the Microsoft Store "App execution alias" stubs (python.exe /
    # python3.exe under %LOCALAPPDATA%\Microsoft\WindowsApps). Those satisfy
    # Get-Command but, when no real Python is installed, print "Python was not
    # found..." and exit 9009 — they are not a usable interpreter, and a naive
    # "already present" check on them skips the real winget install (which then
    # breaks pipx and the satellite venv). So actually run `--version`, require
    # exit 0 + a "Python X.Y" (>=3.10) banner, and reject the stub's redirect
    # notice. This script runs with $ErrorActionPreference='Continue', so the
    # stub's stderr can't abort us here.
    $cands = @()
    foreach ($name in @('python', 'python3')) {
        $g = Get-Command $name -ErrorAction SilentlyContinue
        if ($g -and $g.Source) { $cands += $g.Source }
    }
    # The py launcher (ships with a real install) can name sys.executable.
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py -and $py.Source) {
        $exe = (& $py.Source -3 -c "import sys; print(sys.executable)" 2>&1 |
                ForEach-Object { "$_" } | Select-Object -Last 1)
        if ($exe) { $cands += "$exe".Trim() }
    }
    foreach ($exe in $cands) {
        if (-not $exe -or -not (Test-Path $exe)) { continue }
        $out = (& $exe --version 2>&1 | ForEach-Object { "$_" }) -join "`n"
        if ($LASTEXITCODE -eq 0 -and
            $out -notmatch 'was not found|Microsoft Store|App execution alias' -and
            $out -match 'Python (\d+)\.(\d+)') {
            if ([int]$matches[1] -gt 3 -or ([int]$matches[1] -eq 3 -and [int]$matches[2] -ge 10)) {
                return $exe
            }
        }
    }
    return $null
}

function Install-WingetPackage {
    param(
        [Parameter(Mandatory)] [string] $Id,
        [string] $DisplayName = $Id,
        [string] $CheckCmd = '',
        [scriptblock] $Probe = $null
    )
    # Fast skip: if the tool is already present, don't pay winget's slow
    # network "is there an upgrade?" round-trip (the catalog query takes
    # several seconds even when nothing has changed). A custom -Probe (used for
    # Python, whose WindowsApps "App execution alias" stub would fool a bare
    # Get-Command into reporting a usable interpreter that isn't there) takes
    # precedence over the name-on-PATH -CheckCmd test. Pairing-time baseline
    # only needs the tool PRESENT; upgrading already-installed tools is the
    # user's call. Mirrors the `command -v` guards in install-baseline-tools.sh.
    $present = $false
    if ($Probe) {
        $present = [bool](& $Probe)
    } elseif ($CheckCmd -and (Get-Command $CheckCmd -ErrorAction SilentlyContinue)) {
        $present = $true
    }
    if ($present) {
        Write-Host "[baseline]   $DisplayName already present -- skipping" -ForegroundColor Green
        return $true
    }
    Write-Host "[baseline] $DisplayName ($Id) - downloading via winget..." -ForegroundColor Cyan
    try {
        # `Start-Process -NoNewWindow -Wait` runs winget as a CHILD process
        # that inherits this PowerShell's console handles (stdin/stdout/stderr)
        # directly. winget then sees a real TTY and renders its live progress
        # bar (downloaded MiB / total MiB - percent - speed - ETA) straight to
        # the screen. The direct `& winget ...` syntax instead pipes stdout
        # through PowerShell's stream machinery, which winget detects as "not
        # a TTY" and silently downgrades to no-progress output — defeating
        # the cyan header's intent.
        #
        # `-PassThru` returns the Process object so we can read `.ExitCode`
        # (LASTEXITCODE is not reliably set through Start-Process).
        # `--disable-interactivity` blocks Y/N consent prompts only — it does
        # NOT suppress progress bars (distinct from `--silent`, which we
        # intentionally don't pass).
        $proc = Start-Process winget `
            -ArgumentList @(
                'install', '--id', $Id, '--exact',
                '--accept-source-agreements', '--accept-package-agreements',
                '--disable-interactivity'
            ) `
            -NoNewWindow -Wait -PassThru
        $rc = $proc.ExitCode
        if ($rc -eq 0 -or $rc -eq -1978335189) {
            # -1978335189 = NO_APPLICABLE_UPGRADE (already at latest).
            Write-Host "[baseline]   $DisplayName OK" -ForegroundColor Green
            return $true
        }
        Write-Warning "[baseline]   $DisplayName failed (winget exit $rc) -- continuing"
        return $false
    } catch {
        Write-Warning "[baseline]   $DisplayName error: $_ -- continuing"
        return $false
    }
}

# ---- Tier 1 ------------------------------------------------------------

$skipped = @()

if ($env:SKIP_TIER_1 -ne 'true') {
    Write-Host ""
    Write-Host "=== Tier 1: coding essentials ===" -ForegroundColor Yellow
    # Python uses a -Probe (real-interpreter test) instead of -CheckCmd: a bare
    # `Get-Command python` is satisfied by the Microsoft Store "App execution
    # alias" stub, which would wrongly skip the real winget install on a fresh
    # machine. See Get-RealPythonExe.
    $tier1 = @(
        @{ Id = 'Git.Git';                       Name = 'Git';           Check = 'git' }
        @{ Id = 'GitHub.cli';                    Name = 'GitHub CLI';    Check = 'gh' }
        @{ Id = 'Python.Python.3.12';            Name = 'Python 3.12';   Probe = { [bool](Get-RealPythonExe) } }
        @{ Id = 'OpenJS.NodeJS.LTS';             Name = 'Node.js (LTS)'; Check = 'node' }
        @{ Id = 'jqlang.jq';                     Name = 'jq';            Check = 'jq' }
        @{ Id = 'BurntSushi.ripgrep.MSVC';       Name = 'ripgrep';       Check = 'rg' }
    )
    foreach ($pkg in $tier1) {
        $check = if ($pkg.ContainsKey('Check')) { $pkg['Check'] } else { '' }
        $probe = if ($pkg.ContainsKey('Probe')) { $pkg['Probe'] } else { $null }
        if (-not (Install-WingetPackage -Id $pkg.Id -DisplayName $pkg.Name `
                                        -CheckCmd $check -Probe $probe)) {
            $skipped += $pkg.Name
        }
    }
    Refresh-Path
} else {
    Write-Host "Skipping Tier 1 (SKIP_TIER_1=true)"
}

# ---- Tier 2 (optional heavies) ----------------------------------------

# No Tier 2 winget installs on Windows. Office / PDF / image editing
# live in file-tools-mcp (Docker, on the proxy host); video work lives
# in video-mcp. Bash-pipeline PDF / SQLite tooling on Windows is left
# to the user since winget packages for poppler / sqlite are not
# canonical — agents on Windows route through the MCPs.

# ---- uv (Astral PowerShell installer) ---------------------------------

Write-Host ""
Write-Host "=== uv (Python version manager) ===" -ForegroundColor Yellow
# PINNED to an exact version (uv is 0.x + fast-moving). Keep in sync with
# VERSIONS.md (UV_VERSION). The Astral installer takes the version in the URL path.
$UvVersion = if ($env:UV_VERSION) { $env:UV_VERSION } else { '0.11.24' }
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Host "[baseline] uv already present" -ForegroundColor Green
} else {
    try {
        Write-Host "[baseline] installing uv $UvVersion via Astral PowerShell installer..."
        # Use Invoke-RestMethod (irm) so the response body comes back as
        # a string ready for Invoke-Expression. Invoke-WebRequest's
        # .Content can be a byte[] (especially with -UseBasicParsing)
        # which iex refuses with a "Cannot convert byte[] to String" error.
        Invoke-RestMethod -Uri "https://astral.sh/uv/$UvVersion/install.ps1" | Invoke-Expression
        Refresh-Path
        # Astral installs to %USERPROFILE%\.local\bin which it adds to user PATH;
        # add to current session manually.
        $uvPath = Join-Path $env:USERPROFILE ".local\bin"
        if (Test-Path $uvPath) { $env:Path = "$uvPath;$env:Path" }
        Write-Host "[baseline]   uv OK" -ForegroundColor Green
    } catch {
        Write-Warning "[baseline]   uv install failed: $_"
        $skipped += 'uv'
    }
}

# ---- pipx (via Python pip) ---------------------------------------------
#
# Resolve a REAL python (not the WindowsApps Store stub) and invoke it by
# absolute path -- `& $realPy` -- so pipx installs even if the stub still
# shadows bare `python` on this session's PATH. After Tier 1 above installed
# Python.Python.3.12 (+ Refresh-Path), this is that freshly-installed
# interpreter.

Write-Host ""
Write-Host "=== pipx ===" -ForegroundColor Yellow
$realPy = Get-RealPythonExe
if ($realPy) {
    # .GetNewClosure() binds $realPy into the scriptblock so it resolves when
    # Invoke-Native runs the block in its own scope.
    if (Invoke-Native -Name 'pipx' -Cmd ({
        & $realPy -m pip install --user --quiet --upgrade pipx
    }).GetNewClosure()) {
        Write-Host "[baseline]   pipx OK" -ForegroundColor Green
    } else {
        $skipped += 'pipx'
    }
} else {
    Write-Warning "[baseline]   pipx skipped -- no real Python found (Store alias only)."
    $skipped += 'pipx'
}

# ---- sympy (via Python pip) --------------------------------------------
#
# Symbolic maths in the agents' python -- the file-tools maths workflow
# (transcribe LaTeX -> compute -> write equations back). Keep the pin in
# sync with VERSIONS.md (SYMPY_VERSION).

Write-Host ""
Write-Host "=== sympy ===" -ForegroundColor Yellow
if ($realPy) {
    if (Invoke-Native -Name 'sympy' -Cmd ({
        & $realPy -m pip install --user --quiet sympy==1.14.0
    }).GetNewClosure()) {
        Write-Host "[baseline]   sympy OK" -ForegroundColor Green
    } else {
        $skipped += 'sympy'
    }
} else {
    Write-Warning "[baseline]   sympy skipped -- no real Python found (Store alias only)."
    $skipped += 'sympy'
}

# ---- pnpm + claude + codex (via npm) ----------------------------------

if (Get-Command npm -ErrorAction SilentlyContinue) {
    Write-Host ""
    Write-Host "=== npm-installed tools ===" -ForegroundColor Yellow

    # Skip the npm reinstall when pnpm is already on PATH. `npm install -g`
    # always hits the registry to resolve + re-verify the global tree (several
    # slow seconds) even when nothing changes — that was the long pause on the
    # "=== npm-installed tools ===" line. Mirrors the claude/codex guards below
    # and the `command -v pnpm` guard in install-baseline-tools.sh. Pairing
    # only needs pnpm PRESENT; upgrading is the user's call.
    # PINNED — keep PnpmVersion in sync with VERSIONS.md (PNPM_VERSION).
    $PnpmVersion = if ($env:PNPM_VERSION) { $env:PNPM_VERSION } else { '11.9.0' }
    if (Get-Command pnpm -ErrorAction SilentlyContinue) {
        Write-Host "[baseline]   pnpm already present -- skipping" -ForegroundColor Green
    } else {
        Write-Host "[baseline] pnpm $PnpmVersion..."
        if (Invoke-Native -Name 'pnpm' -Cmd { npm install -g "pnpm@$PnpmVersion" }) {
            Write-Host "[baseline]   pnpm OK" -ForegroundColor Green
        } else {
            $skipped += 'pnpm'
        }
    }

    # Pinned CLI versions — keep in sync with VERSIONS.md (CLAUDE_CODE_VERSION /
    # CODEX_VERSION). Mirrors install-baseline-tools.sh: the platform runs a
    # VERIFIED CLI (in-app auto-update disabled), so a mismatched install is
    # UPGRADED to the pin, not skipped.
    $ClaudeCodeVersion = if ($env:CLAUDE_CODE_VERSION) { $env:CLAUDE_CODE_VERSION } else { '2.1.215' }
    $CodexVersion      = if ($env:CODEX_VERSION) { $env:CODEX_VERSION } else { '0.144.6' }

    function Install-PinnedCli {
        # Install OR upgrade an npm-global CLI to the EXACT pinned version.
        param([string]$Label, [string]$Pkg, [string]$BinCmd, [string]$Want)
        $have = $null
        if (Get-Command $BinCmd -ErrorAction SilentlyContinue) {
            $verOut = (& $BinCmd --version 2>$null | Out-String)
            if ($verOut -match '(\d+\.\d+\.\d+)') { $have = $Matches[1] }
            if ($have -eq $Want) {
                Write-Host "[baseline]   $Label at pinned $Want" -ForegroundColor Green
                return $true
            }
            Write-Host "[baseline] Upgrading $Label $have -> $Want..."
        } else {
            Write-Host "[baseline] Installing $Label $Want..."
        }
        $spec = "$Pkg@$Want"
        return (Invoke-Native -Name "$Label (npm)" -Cmd ({ npm install -g $spec }.GetNewClosure()))
    }

    if ($env:SKIP_CLAUDE_CLI -ne 'true') {
        if (Install-PinnedCli -Label 'claude-code' -Pkg '@anthropic-ai/claude-code' -BinCmd 'claude.cmd' -Want $ClaudeCodeVersion) {
            Write-Host "[baseline]   claude-code OK" -ForegroundColor Green
        } else {
            $skipped += 'claude-code (npm)'
        }
        if (Install-PinnedCli -Label 'codex' -Pkg '@openai/codex' -BinCmd 'codex.cmd' -Want $CodexVersion) {
            Write-Host "[baseline]   codex OK" -ForegroundColor Green
        } else {
            $skipped += 'codex (npm)'
        }
    } else {
        Write-Host "Skipping claude/codex (SKIP_CLAUDE_CLI=true)"
    }
} else {
    Write-Warning "npm not found after baseline install -- claude/codex/pnpm skipped"
    $skipped += 'claude-code, codex, pnpm (npm missing)'
}

# ---- Summary ----------------------------------------------------------

Write-Host ""
if ($skipped.Count -eq 0) {
    Write-Host "[baseline] all tools installed successfully." -ForegroundColor Green
} else {
    Write-Warning "[baseline] some tools were skipped:"
    foreach ($t in $skipped) { Write-Warning "  - $t" }
    Write-Warning "These can be installed manually later. Run this script again or"
    Write-Warning "use winget/npm directly."
}
