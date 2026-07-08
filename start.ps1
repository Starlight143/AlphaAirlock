# Windows PowerShell launcher - mirrors start.sh for Windows users.
# Boots the FastAPI backend + Next.js frontend, registers a Ctrl+C trap, and
# guarantees no orphan processes survive (uses taskkill /T /F for tree-kill
# so this works on Windows PowerShell 5.1, not just PowerShell 7+).
#
# IMPORTANT: this file is intentionally ASCII-only. PowerShell 5.1 reads .ps1
# scripts as the system ANSI codepage by default, so non-ASCII characters
# (em-dashes, smart quotes, ellipses, CJK) get mojibaked and break parsing.
#
# Port handling (added): BACKEND_PORT / FRONTEND_PORT are PREFERRED ports.
# If a preferred port is already in use OR reserved by the OS (Hyper-V / WSL2 /
# Docker Desktop reserve large dynamic TCP ranges on Windows; a reserved port
# fails to bind with EACCES "permission denied", NOT EADDRINUSE), the launcher
# auto-selects the next free port and rewires NEXT_PUBLIC_API_BASE and
# ALPHA_ALLOWED_ORIGINS so the frontend and CORS keep matching the backend.
# Set PORT_AUTO_SELECT=0 to disable auto-select and fail fast on a busy port.

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -Path $ProjectRoot

$script:BackendProc  = $null
$script:FrontendProc = $null

function Write-Info($m)  { Write-Host "[start.ps1] $m" -ForegroundColor Green }
function Write-Warn2($m) { Write-Host "[start.ps1] $m" -ForegroundColor Yellow }
function Write-Err2($m)  { Write-Host "[start.ps1] $m" -ForegroundColor Red }

function Stop-ProcessTree {
    param([int]$ProcessId)
    if (-not $ProcessId -or $ProcessId -le 0) { return }
    # taskkill is shipped with every supported Windows; /T = tree, /F = force.
    & taskkill.exe /PID $ProcessId /T /F 2>$null | Out-Null
}

function Stop-All {
    Write-Host ""
    Write-Info "Shutting down Agentic Alpha System..."
    foreach ($p in @($script:BackendProc, $script:FrontendProc)) {
        if ($null -ne $p) {
            try {
                if (-not $p.HasExited) {
                    Stop-ProcessTree -ProcessId $p.Id
                }
            } catch {
                # process may have already exited; ignore.
            }
        }
    }
}

# Register engine-exit handler. Ctrl+C in the foreground loop drops into the
# finally block, which also calls Stop-All; this handler is a belt-and-braces
# guarantee for unusual exit paths.
# TreatControlCAsInput is the default but the assignment throws "handle is
# invalid" when the script is launched without an attached console
# (e.g. via Start-Process -RedirectStandardInput). Guarding it keeps the
# launcher usable both interactively and headlessly.
try { [Console]::TreatControlCAsInput = $false } catch { }
$null = Register-EngineEvent PowerShell.Exiting -Action { Stop-All }

# ---------------------------------------------------------------------------
# Small env helpers (whitelist bool parser; clamped int parser).
# ---------------------------------------------------------------------------
function Get-EnvOrDefault {
    param([string]$Name, [string]$Default)
    $v = [Environment]::GetEnvironmentVariable($Name)
    if ($null -eq $v -or $v.Trim() -eq '') { return $Default }
    return $v.Trim()
}

function Get-EnvBool {
    param([string]$Name, [bool]$Default)
    $raw = [Environment]::GetEnvironmentVariable($Name)
    if ($null -eq $raw) { return $Default }
    $raw = $raw.Trim().ToLowerInvariant()
    if ($raw -eq '') { return $Default }
    if (@('1', 'true', 'yes', 'on')  -contains $raw) { return $true }
    if (@('0', 'false', 'no', 'off') -contains $raw) { return $false }
    return $Default
}

function Get-EnvInt {
    param([string]$Name, [int]$Default, [int]$Minimum = 1, [int]$Maximum = 65535)
    $raw = [Environment]::GetEnvironmentVariable($Name)
    if ($null -eq $raw -or $raw.Trim() -eq '') { return $Default }
    $parsed = 0
    if ([int]::TryParse($raw.Trim(), [ref]$parsed)) {
        if ($parsed -lt $Minimum) { return $Minimum }
        if ($parsed -gt $Maximum) { return $Maximum }
        return $parsed
    }
    Write-Warn2 "Env $Name='$raw' is not a valid integer; using default $Default."
    return $Default
}

# ---------------------------------------------------------------------------
# Port selection helpers.
# Test-PortFree actually attempts the bind (the only reliable way to detect an
# OS-reserved/excluded port on Windows, which a "is anyone listening?" check
# would wrongly report as free). It binds the same address family the real
# service will use so the probe matches reality.
# ---------------------------------------------------------------------------
function Resolve-BindAddress {
    param([string]$HostName)
    $h = ''
    if ($null -ne $HostName) { $h = $HostName.Trim() }
    if ($h -eq '' -or $h -eq '0.0.0.0' -or $h -eq '*' -or $h -eq '::' -or $h -eq '[::]') {
        return [System.Net.IPAddress]::Any
    }
    if ($h -eq 'localhost') { return [System.Net.IPAddress]::Loopback }
    $parsed = $null
    if ([System.Net.IPAddress]::TryParse($h, [ref]$parsed)) { return $parsed }
    # Unknown hostname: probe loopback (best effort).
    return [System.Net.IPAddress]::Loopback
}

function Get-BrowserHost {
    # Map a bind host to something a local browser / the frontend can dial.
    param([string]$BindHost)
    $h = ''
    if ($null -ne $BindHost) { $h = $BindHost.Trim() }
    if ($h -eq '' -or $h -eq '0.0.0.0' -or $h -eq '*' -or $h -eq '::' -or $h -eq '[::]') {
        return '127.0.0.1'
    }
    return $h
}

function Test-PortFree {
    param([System.Net.IPAddress]$Address, [int]$Port)
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener($Address, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($null -ne $listener) { try { $listener.Stop() } catch { } }
    }
}

function Get-FreeTcpPort {
    param(
        [System.Net.IPAddress]$Address,
        [int]$Preferred,
        [bool]$AutoSelect = $true,
        [int]$ScanCount = 512,
        [int[]]$Exclude = @(),
        [string]$Label = 'service'
    )
    if ((Test-PortFree -Address $Address -Port $Preferred) -and (-not ($Exclude -contains $Preferred))) {
        return $Preferred
    }
    if (-not $AutoSelect) {
        throw "[$Label] preferred port $Preferred on $($Address.ToString()) is in use or OS-reserved and PORT_AUTO_SELECT=0. Free the port or choose another."
    }
    for ($p = $Preferred + 1; ($p -le ($Preferred + $ScanCount)) -and ($p -le 65535); $p++) {
        if (($Exclude -contains $p)) { continue }
        if (Test-PortFree -Address $Address -Port $p) { return $p }
    }
    # Guaranteed fallback: let the OS hand us an ephemeral port. The OS never
    # allocates a reserved/excluded port, so this always yields a bindable one.
    for ($attempt = 0; $attempt -lt 16; $attempt++) {
        $l = $null
        try {
            $l = New-Object System.Net.Sockets.TcpListener($Address, 0)
            $l.Start()
            $chosen = ([System.Net.IPEndPoint]$l.LocalEndpoint).Port
        } catch {
            $chosen = 0
        } finally {
            if ($null -ne $l) { try { $l.Stop() } catch { } }
        }
        if ($chosen -gt 0 -and (-not ($Exclude -contains $chosen))) { return $chosen }
    }
    throw "[$Label] could not find any free TCP port near $Preferred on $($Address.ToString())."
}

# ---------------------------------------------------------------------------
# Resolve npm.cmd's real path on disk. On Windows `npm` is a .cmd shim;
# Start-Process refuses to launch it as a Win32 executable, so we must hand
# Start-Process the .cmd path itself (or fall back to cmd.exe /c).
# ---------------------------------------------------------------------------
function Resolve-NpmExecutable {
    $cands = @('npm.cmd', 'npm.ps1', 'npm.bat', 'npm.exe', 'npm')
    foreach ($c in $cands) {
        $info = Get-Command $c -ErrorAction SilentlyContinue
        if ($info -and $info.Source) {
            return @{ Path = $info.Source; UseCmdWrapper = $false }
        }
    }
    # Last resort: spawn through cmd.exe /c npm
    $cmd = Get-Command cmd.exe -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        return @{ Path = $cmd.Source; UseCmdWrapper = $true }
    }
    throw "npm not found on PATH and cmd.exe is unavailable - install Node.js first."
}

# Same trick for python (almost always python.exe, but be defensive).
function Resolve-PythonExecutable {
    $info = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $info) { $info = Get-Command python -ErrorAction SilentlyContinue }
    if (-not $info) { throw "python not found on PATH - install Python 3.11+ first." }
    return $info.Source
}

# ---------------------------------------------------------------------------
# Self-healing dependency install. The common Windows breakage is a prior
# lxml install with no RECORD file (interrupted/wheel-cache corruption), which
# makes pip refuse to uninstall it to satisfy trafilatura -> the whole install
# aborts. We detect the failure, force-reinstall lxml cleanly, then retry once;
# if it still fails we WARN and continue (the only affected feature is full-text
# article enrichment, which is lazy-imported and OFF by default).
# ---------------------------------------------------------------------------
function Install-PythonDeps {
    param([string]$PythonExe)
    Write-Info "Step 1/4: Installing Python dependencies"
    if ($env:SKIP_PIP_INSTALL) {
        Write-Warn2 "SKIP_PIP_INSTALL is set - skipping pip install"
        return
    }

    & $PythonExe -m pip install --quiet --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -eq 0) { return }

    Write-Warn2 "pip install failed (exit $LASTEXITCODE). Attempting known-issue repair (lxml RECORD file)..."
    & $PythonExe -m pip install --disable-pip-version-check --force-reinstall --no-deps "lxml>=5.2,<6"
    & $PythonExe -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -eq 0) {
        Write-Info "Dependency repair succeeded."
        return
    }

    Write-Warn2 "pip install still failing after repair (exit $LASTEXITCODE)."
    Write-Warn2 "Continuing startup: the only feature that needs the failing dep is"
    Write-Warn2 "full-text article enrichment (trafilatura/lxml) - lazy-imported and"
    Write-Warn2 "OFF by default (ARTICLE_ENRICH_ENABLED=0). Core app is unaffected."
}

try {
    $pythonExe = Resolve-PythonExecutable

    # --- Step 1: dependencies (self-healing) -------------------------------
    Install-PythonDeps -PythonExe $pythonExe

    # --- Step 2: synthetic dataset -----------------------------------------
    Write-Info "Step 2/4: Ensuring synthetic_btc.csv exists"
    $DataCsv = Join-Path $ProjectRoot 'backend\data\synthetic_btc.csv'
    if (-not (Test-Path $DataCsv)) {
        & $pythonExe backend\core\data_gen.py
    } else {
        Write-Info "Synthetic dataset already exists ($DataCsv)"
    }

    # Provider-aware key warning. Defaults to 'anthropic' when LLM_PROVIDER is unset.
    $providerRaw = if ($env:LLM_PROVIDER) { $env:LLM_PROVIDER.Trim().ToLower() } else { 'anthropic' }
    switch -Regex ($providerRaw) {
        '^(openrouter|openai|openai[-_]compatible)$' {
            if (-not $env:OPENROUTER_API_KEY) {
                Write-Warn2 "LLM_PROVIDER=$providerRaw but OPENROUTER_API_KEY is not set - agent calls will fail."
            }
        }
        '^(|auto)$' {
            if (-not $env:ANTHROPIC_API_KEY -and -not $env:OPENROUTER_API_KEY) {
                Write-Warn2 "Neither ANTHROPIC_API_KEY nor OPENROUTER_API_KEY is set - agent calls will fail."
            }
        }
        default {
            if (-not $env:ANTHROPIC_API_KEY) {
                Write-Warn2 "LLM_PROVIDER=$providerRaw but ANTHROPIC_API_KEY is not set - agent calls will fail."
            }
        }
    }

    # --- Port selection (preferred -> auto-select free) --------------------
    $autoSelect   = Get-EnvBool 'PORT_AUTO_SELECT' $true
    $backendHost  = Get-EnvOrDefault 'BACKEND_HOST' '127.0.0.1'
    $frontendHost = Get-EnvOrDefault 'FRONTEND_HOST' '0.0.0.0'
    $backendPref  = Get-EnvInt 'BACKEND_PORT' 8000
    $frontendPref = Get-EnvInt 'FRONTEND_PORT' 3000

    $backendAddr  = Resolve-BindAddress $backendHost
    $frontendAddr = Resolve-BindAddress $frontendHost

    $backendPort  = Get-FreeTcpPort -Address $backendAddr  -Preferred $backendPref  -AutoSelect $autoSelect -Label 'backend'
    # Exclude the backend port so the frontend never collides with it (matters
    # only when both bind overlapping interfaces / share a preferred value).
    $frontendPort = Get-FreeTcpPort -Address $frontendAddr -Preferred $frontendPref -AutoSelect $autoSelect -Exclude @($backendPort) -Label 'frontend'

    if ($backendPort -ne $backendPref) {
        Write-Warn2 "Backend port $backendPref unavailable (in use or OS-reserved) - using $backendPort instead."
    }
    if ($frontendPort -ne $frontendPref) {
        Write-Warn2 "Frontend port $frontendPref unavailable (in use or OS-reserved) - using $frontendPort instead."
    }

    # Wire the frontend -> backend URL and the backend CORS allowlist to the
    # actually-selected ports. override=False in the backend env loader means
    # these shell values win over any .env file, so the wiring is authoritative.
    $backendBrowserHost  = Get-BrowserHost $backendHost
    $frontendBrowserHost = Get-BrowserHost $frontendHost
    $apiBase = "http://${backendBrowserHost}:$backendPort"

    $originList = New-Object System.Collections.Generic.List[string]
    $existingOrigins = Get-EnvOrDefault 'ALPHA_ALLOWED_ORIGINS' ''
    if ($existingOrigins -ne '') {
        foreach ($o in $existingOrigins.Split(',')) {
            $t = $o.Trim()
            if ($t -ne '' -and (-not $originList.Contains($t))) { $originList.Add($t) }
        }
    }
    foreach ($o in @("http://localhost:$frontendPort", "http://127.0.0.1:$frontendPort")) {
        if (-not $originList.Contains($o)) { $originList.Add($o) }
    }
    if ($frontendBrowserHost -ne 'localhost' -and $frontendBrowserHost -ne '127.0.0.1') {
        $extra = "http://${frontendBrowserHost}:$frontendPort"
        if (-not $originList.Contains($extra)) { $originList.Add($extra) }
    }

    $env:NEXT_PUBLIC_API_BASE = $apiBase
    $env:ALPHA_ALLOWED_ORIGINS = [string]::Join(',', $originList)
    $env:BACKEND_HOST = $backendHost
    $env:BACKEND_PORT = "$backendPort"

    # --- Step 3: backend ---------------------------------------------------
    Write-Info "Step 3/4: Launching FastAPI backend (http://${backendBrowserHost}:$backendPort)"
    $script:BackendProc = Start-Process -FilePath $pythonExe `
        -ArgumentList '-m', 'uvicorn', 'backend.app.main:app', '--host', $backendHost, '--port', "$backendPort" `
        -WorkingDirectory $ProjectRoot -PassThru -NoNewWindow
    Write-Info "Backend PID: $($script:BackendProc.Id)"

    # --- Step 4: frontend --------------------------------------------------
    Write-Info "Step 4/4: Launching Next.js frontend (http://${frontendBrowserHost}:$frontendPort)"
    $FrontendDir = Join-Path $ProjectRoot 'frontend'
    if (-not (Test-Path (Join-Path $FrontendDir 'node_modules'))) {
        Write-Warn2 "node_modules missing - running 'npm install' (one-time)"
        Push-Location $FrontendDir
        try {
            npm install --no-audit --no-fund
        } finally {
            Pop-Location
        }
    }

    # PORT is a belt-and-braces fallback; the explicit '-p' below is what Next
    # actually honours (last '-p' wins over the one baked into package.json).
    $env:PORT = "$frontendPort"
    $frontendArgs = @('run', 'dev', '--', '-p', "$frontendPort", '-H', $frontendHost)

    $npmRes = Resolve-NpmExecutable
    if ($npmRes.UseCmdWrapper) {
        $script:FrontendProc = Start-Process -FilePath $npmRes.Path `
            -ArgumentList (@('/c', 'npm') + $frontendArgs) `
            -WorkingDirectory $FrontendDir -PassThru -NoNewWindow
    } else {
        $script:FrontendProc = Start-Process -FilePath $npmRes.Path `
            -ArgumentList $frontendArgs `
            -WorkingDirectory $FrontendDir -PassThru -NoNewWindow
    }
    Write-Info "Frontend PID: $($script:FrontendProc.Id)  (launcher: $($npmRes.Path))"

    Write-Info "All services up. Press Ctrl+C to stop both."
    Write-Info "  Backend  : http://${backendBrowserHost}:$backendPort   (docs: /docs)"
    Write-Info "  Frontend : http://${frontendBrowserHost}:$frontendPort"
    Write-Info "  API base : $($env:NEXT_PUBLIC_API_BASE)   CORS: $($env:ALPHA_ALLOWED_ORIGINS)"

    # Block until either child exits OR user hits Ctrl+C (which raises
    # PipelineStoppedException that the finally block catches).
    while ($true) {
        Start-Sleep -Seconds 2
        if ($script:BackendProc.HasExited) {
            Write-Err2 "Backend exited unexpectedly (code $($script:BackendProc.ExitCode))"
            break
        }
        if ($script:FrontendProc.HasExited) {
            Write-Err2 "Frontend exited unexpectedly (code $($script:FrontendProc.ExitCode))"
            break
        }
    }
}
finally {
    Stop-All
}
