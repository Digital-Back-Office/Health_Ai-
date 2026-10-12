<#
  Health AI - start everything for local development.

    .\dev.ps1                         database + backend + frontend + ngrok tunnel, then opens the app
    .\dev.ps1 -NoTunnel               skip ngrok (UI work only - the agent can't reach the backend)
    .\dev.ps1 -Ngrok "C:\tools\ngrok.exe"   use a specific ngrok executable

  ngrok is found in this order: -Ngrok, `ngrok` on your PATH, then `npx ngrok` (the npm package).

  If PowerShell refuses to run scripts, double-click dev.cmd instead
  (or run: powershell -ExecutionPolicy Bypass -File .\dev.ps1).

  Each part opens in its own window; close a window (or press Ctrl+C in it) to stop that part.
  If the ngrok URL changes, this script updates PUBLIC_BASE_URL in backend\.env and runs
  `python -m app.cli setup-agent` so the ElevenLabs agent points at the new URL.
  Tip: set NGROK_DOMAIN in backend\.env to your free static ngrok domain and the URL never changes.
#>
param(
    [switch]$NoTunnel,
    [string]$Ngrok = ""
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$envFile = Join-Path $backend ".env"
$python = Join-Path $backend ".venv\Scripts\python.exe"

function Step([string]$message) { Write-Host "`n==> $message" -ForegroundColor Cyan }

function Get-EnvValue([string]$key) {
    $line = Get-Content -Encoding UTF8 $envFile | Where-Object { $_ -match "^$key=" } | Select-Object -First 1
    if ($line) { return ($line -replace "^$key=", "").Trim() }
    return ""
}

function Set-EnvValue([string]$key, [string]$value) {
    $lines = [System.Collections.Generic.List[string]](Get-Content -Encoding UTF8 $envFile)
    $index = $lines.FindIndex({ param($l) $l -match "^$key=" })
    if ($index -ge 0) { $lines[$index] = "$key=$value" } else { $lines.Add("$key=$value") }
    # UTF-8 without BOM (a BOM would corrupt the first key for the backend).
    [System.IO.File]::WriteAllLines($envFile, $lines)
}

# Note: always use 127.0.0.1, not "localhost" - on Windows "localhost" tries IPv6 (::1)
# first, and a refused IPv6 connection takes ~2 s to fail, which stalls every check.
[System.Net.WebRequest]::DefaultWebProxy = $null  # never send local checks through a proxy

function Wait-Http([string]$url, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        try { Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 3 | Out-Null; return $true } catch { Start-Sleep -Milliseconds 500 }
    }
    return $false
}

function Test-PortOpen([int]$port) {
    # Dev servers may listen on IPv4 or IPv6 only; accept either.
    foreach ($address in "127.0.0.1", "::1") {
        $client = New-Object System.Net.Sockets.TcpClient($(if ($address -eq "::1") { "InterNetworkV6" } else { "InterNetwork" }))
        try {
            if ($client.ConnectAsync($address, $port).Wait(500) -and $client.Connected) { return $true }
        } catch { } finally { $client.Dispose() }
    }
    return $false
}

function Wait-Port([int]$port, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortOpen $port) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Test-PortFree([int]$port) {
    return -not (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
}

if (-not (Test-Path $envFile)) { throw "backend\.env not found - copy backend\.env.example to backend\.env first." }
if (-not (Test-Path $python)) { throw "Python virtualenv not found - see README section 1 (pip install -e "".[dev]"")." }

# 1. Database ------------------------------------------------------------------------
Step "Starting Postgres (Docker)"
docker compose -f (Join-Path $root "docker-compose.yml") up -d db
if ($LASTEXITCODE -ne 0) { throw "Docker didn't start the database - is Docker Desktop running?" }

# 2. Backend -------------------------------------------------------------------------
if (Test-PortFree 8000) {
    Step "Starting backend on http://localhost:8000 (new window)"
    $backendCommand = "`$Host.UI.RawUI.WindowTitle = 'Health AI - backend'; Set-Location '$backend'; & '$python' -m app.cli migrate; & '$python' -m uvicorn app.main:app --port 8000 --reload --reload-dir app"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCommand
} else {
    Step "Backend already running on port 8000"
}
if (-not (Wait-Http "http://127.0.0.1:8000/healthz" 90)) { throw "Backend didn't start - check the 'Health AI - backend' window." }
Write-Host "Backend is up."

# 3. Frontend ------------------------------------------------------------------------
if (Test-PortFree 5173) {
    Step "Starting frontend on http://localhost:5173 (new window)"
    $frontendCommand = "`$Host.UI.RawUI.WindowTitle = 'Health AI - frontend'; Set-Location '$frontend'; npm run dev"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $frontendCommand
} else {
    Step "Frontend already running on port 5173"
}

# 4. Tunnel for ElevenLabs -----------------------------------------------------------
function Get-TunnelUrl {
    try {
        return ((Invoke-RestMethod http://127.0.0.1:4040/api/tunnels -TimeoutSec 2).tunnels |
            Where-Object { $_.public_url -like "https://*" } | Select-Object -First 1).public_url
    } catch { return $null }
}

if (-not $NoTunnel) {
    Step "Starting ngrok tunnel to port 8000"
    $public = Get-TunnelUrl  # an ngrok that is already running is reused
    if (-not $public) {
        # How to run ngrok: -Ngrok, ngrok on PATH, or the npm package via npx.
        $runner = $null
        foreach ($candidate in @($Ngrok, "ngrok") | Where-Object { $_ }) {
            $command = Get-Command $candidate -ErrorAction SilentlyContinue
            if ($command) { $runner = @{ File = $command.Source; Args = @() }; break }
        }
        if (-not $runner) {
            $npx = Get-Command npx.cmd -ErrorAction SilentlyContinue
            if ($npx) { $runner = @{ File = $npx.Source; Args = @("--yes", "ngrok") } }
        }
        if (-not $runner) {
            Write-Warning "ngrok not found (no ngrok on PATH and no npx). Install Node.js or ngrok, or run: .\dev.ps1 -Ngrok 'C:\path\to\ngrok.exe'"
        } else {
            $ngrokArgs = $runner.Args + @("http", "8000")
            $domain = Get-EnvValue "NGROK_DOMAIN"
            if ($domain) { $ngrokArgs += "--url=$domain" }
            Write-Host "  $([System.IO.Path]::GetFileName($runner.File)) $($ngrokArgs -join ' ')"
            Start-Process $runner.File -ArgumentList $ngrokArgs
            # The first npx run may download ngrok, so allow up to a minute.
            for ($i = 0; $i -lt 60 -and -not $public; $i++) {
                Start-Sleep -Seconds 1
                $public = Get-TunnelUrl
            }
        }
    }
    if (-not $public) {
        Write-Warning "No tunnel URL from ngrok (http://127.0.0.1:4040) - check the ngrok window."
        Write-Warning "Without the tunnel the page loads, but Sophie can't use her tools or the LLM."
    } else {
        $current = Get-EnvValue "PUBLIC_BASE_URL"
        if ($public.TrimEnd("/") -ne $current.TrimEnd("/")) {
            Step "Tunnel URL is new ($public) - updating PUBLIC_BASE_URL and re-pointing the ElevenLabs agent"
            Set-EnvValue "PUBLIC_BASE_URL" $public
            Push-Location $backend
            try { & $python -m app.cli setup-agent } finally { Pop-Location }
        } else {
            Write-Host "Tunnel: $public (the agent already points here)"
        }
    }
}

# 5. Open the app --------------------------------------------------------------------
if (-not (Wait-Port 5173 60)) { Write-Warning "Frontend isn't answering on port 5173 yet - check the 'Health AI - frontend' window." }
Step "Ready"
Write-Host "  Patient page:     http://localhost:5173"
Write-Host "  Staff dashboard:  http://localhost:5173/#/staff   (sign in with ADMIN_TOKEN from backend\.env)"
Write-Host "  UI preview:       http://localhost:5173/#/preview (no voice minutes used)"
Write-Host "  API docs:         http://localhost:8000/docs"
Start-Process "http://localhost:5173"
