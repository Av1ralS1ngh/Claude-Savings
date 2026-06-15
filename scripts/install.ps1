<#
.SYNOPSIS
  Decompute one-line installer for Windows (the native equivalent of install.sh).

    powershell -ExecutionPolicy Bypass -NoProfile -Command "irm https://pub-1093626231534fd593c5355928f4074d.r2.dev/install.ps1 | iex"

  Installs the local compressor stack into an isolated venv (%USERPROFILE%\.decompute\venv) and puts the
  `decompute` / `decompute-gateway` commands on PATH, so you can run `decompute init` then `decompute claude`.

  The CLI + gateway are pure-python ("py3-none-any") wheels — one file works on every OS. The ML compression
  ENGINE (yscompress) currently ships only macOS/arm64 wheels; on Windows the gateway runs its built-in
  pure-Python fallback compression (logs / JSON / HTML / tool-output) until a win_amd64 engine wheel exists.
  If DECOMPUTE_ENGINE_SPEC is set (or a win_amd64 wheel is published at the dist URL), it is installed too.

.NOTES
  Env overrides (parallel to install.sh): DECOMPUTE_HOME, DECOMPUTE_BIN, DECOMPUTE_SDK_SPEC,
  DECOMPUTE_GATEWAY_SPEC, DECOMPUTE_ENGINE_SPEC, DECOMPUTE_DIST_URL, DECOMPUTE_INDEX_URL,
  DECOMPUTE_LICENSE_PUBLIC_KEYS, DECOMPUTE_PORT.
#>

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # speeds up Invoke-WebRequest

function Get-EnvOr([string]$name, [string]$default) {
  $v = [Environment]::GetEnvironmentVariable($name)
  if ([string]::IsNullOrWhiteSpace($v)) { return $default } else { return $v }
}

function Quote-PowerShellString([string]$value) {
  return "'" + $value.Replace("'", "''") + "'"
}

$DecomputeHome = Get-EnvOr "DECOMPUTE_HOME" (Join-Path $HOME ".decompute")
$Venv          = Join-Path $DecomputeHome "venv"
$Scripts       = Join-Path $Venv "Scripts"
# On Windows the venv Scripts dir holds the pip-generated decompute.exe / decompute-gateway.exe entry
# points; we put it on PATH directly (no separate shim dir like ~/.local/bin on POSIX).
$BinDir        = Get-EnvOr "DECOMPUTE_BIN" $Scripts
$DistUrl       = (Get-EnvOr "DECOMPUTE_DIST_URL" "https://pub-1093626231534fd593c5355928f4074d.r2.dev").TrimEnd("/")
$GwPort        = Get-EnvOr "DECOMPUTE_PORT" "8080"

Write-Host "-> Installing the Decompute compressor stack (Windows)"

# 1. Find a usable Python (3.12 or 3.13 preferred — that is what the engine wheel targets when it lands).
function Find-Python {
  # The `py` launcher is the most reliable selector on Windows.
  foreach ($ver in @("3.12", "3.13")) {
    try { & py "-$ver" -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { return @("py", "-$ver") } } catch {}
  }
  foreach ($exe in @("python3.12", "python3.13", "python", "python3")) {
    $cmd = Get-Command $exe -ErrorAction SilentlyContinue
    if ($cmd) {
      # Reject the Microsoft Store python stub (it lives under WindowsApps and exits 0 with no real interpreter).
      if ($cmd.Source -notlike "*WindowsApps*") { return @($cmd.Source) }
    }
  }
  return $null
}

$PyCmd = @(Find-Python | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
if ($PyCmd.Count -eq 0) {
  Write-Host "error: Python 3.12 or 3.13 is required but was not found." -ForegroundColor Red
  Write-Host "       Install it with:  winget install Python.Python.3.12" -ForegroundColor Yellow
  Write-Host "       (or from https://www.python.org/downloads/), then re-run this installer." -ForegroundColor Yellow
  exit 1
}
$PyExe = [string]$PyCmd[0]
$PyArgs = @()
if ($PyCmd.Count -gt 1) { $PyArgs = @($PyCmd[1..($PyCmd.Count - 1)]) }
Write-Host ("  python : " + (($PyExe, $PyArgs) -join " "))
Write-Host ("  venv   : " + $Venv)

# 2. Resolve each package source: env override > local repo checkout > the R2 dist bucket.
$here = if ($PSScriptRoot) { $PSScriptRoot } else { $null }
function Resolve-Spec([string]$envName, [string]$repoRel, [string]$distFile) {
  $spec = [Environment]::GetEnvironmentVariable($envName)
  if (-not [string]::IsNullOrWhiteSpace($spec)) { return $spec }
  if ($here) {
    $local = Join-Path $here $repoRel
    if (Test-Path (Join-Path $local "pyproject.toml")) { return (Resolve-Path $local).Path }
  }
  return "$DistUrl/$distFile"
}
$SdkSpec = Resolve-Spec "DECOMPUTE_SDK_SPEC"     "..\packages\python-sdk" "decompute_ai-0.1.0-py3-none-any.whl"
$GwSpec  = Resolve-Spec "DECOMPUTE_GATEWAY_SPEC" "..\apps\api"            "decompute_gateway-0.1.0-py3-none-any.whl"
Write-Host ("  cli    : " + $SdkSpec)
Write-Host ("  gateway: " + $GwSpec)

# Engine: optional on Windows. Use it only if the user set DECOMPUTE_ENGINE_SPEC or a win_amd64 wheel is
# fetchable at the dist URL. Otherwise the gateway runs pure-Python fallback compression.
$PyTag = (& $PyExe @PyArgs -c "import sys;print(f'cp{sys.version_info.major}{sys.version_info.minor}')") 2>$null
$EngineSpec = [Environment]::GetEnvironmentVariable("DECOMPUTE_ENGINE_SPEC")
if ([string]::IsNullOrWhiteSpace($EngineSpec)) {
  $candidate = "$DistUrl/yscompress-0.22.4-$PyTag-$PyTag-win_amd64.whl"
  try {
    Invoke-WebRequest -Method Head -Uri $candidate -TimeoutSec 8 -UseBasicParsing | Out-Null
    $EngineSpec = $candidate   # a published Windows engine wheel exists — use it
  } catch {
    $EngineSpec = $null        # none yet — fallback compression
  }
}
if ($EngineSpec) { Write-Host ("  engine : " + $EngineSpec) }
else { Write-Host "  engine : (none for Windows yet -> pure-Python fallback compression)" -ForegroundColor Yellow }

$IndexArgs = @()
$idx = [Environment]::GetEnvironmentVariable("DECOMPUTE_INDEX_URL")
if (-not [string]::IsNullOrWhiteSpace($idx)) { $IndexArgs = @("--extra-index-url", $idx) }

# 3. Create the venv and upgrade pip.
& $PyExe @PyArgs -m venv $Venv
$VenvPy = Join-Path $Scripts "python.exe"
& $VenvPy -m pip install --quiet --upgrade pip

# 4. Install the wheels. Two passes mirror install.sh: pass 1 resolves deps; pass 2 force-reinstalls the
#    wheel CONTENTS so a re-published fix (same version) actually reaches the user on update.
$specs = @()
if ($EngineSpec) { $specs += $EngineSpec }   # engine first (gateway imports it) when present
$specs += $GwSpec
$specs += $SdkSpec
foreach ($s in $specs) { & $VenvPy -m pip install --quiet @IndexArgs $s }
foreach ($s in $specs) { & $VenvPy -m pip install --quiet --force-reinstall --no-deps @IndexArgs $s }

# 5. Warm the ML model only if the engine is actually installed (no engine -> nothing to warm).
if ($EngineSpec) {
  Write-Host "-> downloading the compression model (~150 MB, one-time)..."
  $env:HF_HUB_OFFLINE = "0"; $env:TRANSFORMERS_OFFLINE = "0"
  try { & $VenvPy -c "from headroom.transforms.kompress_compressor import _load_kompress; _load_kompress()" 2>$null | Out-Null; Write-Host "  ok compression model ready" }
  catch { Write-Host "  warn: could not fetch the model now; compression uses the lighter path until cached." -ForegroundColor Yellow }
}

# 6. Persist gateway.env: BYOK by default, and pin the data dir to the install dir (so the gateway never
#    depends on its launch cwd — same reasoning as install.sh / launchd cwd=/).
New-Item -ItemType Directory -Force -Path $DecomputeHome | Out-Null
$GwEnv = Join-Path $DecomputeHome "gateway.env"
$envLines = @("DECOMPUTE_MOCK_ANTHROPIC=false", "DECOMPUTE_DATA_DIR=$DecomputeHome")
$pubkeys = [Environment]::GetEnvironmentVariable("DECOMPUTE_LICENSE_PUBLIC_KEYS")
if (-not [string]::IsNullOrWhiteSpace($pubkeys)) { $envLines += "DECOMPUTE_LICENSE_PUBLIC_KEYS=$pubkeys" }
Set-Content -Path $GwEnv -Value $envLines -Encoding ascii

# 7. Put the venv Scripts dir on the USER PATH (no admin needed) so `decompute` works in new terminals.
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($null -eq $userPath) { $userPath = "" }
if (($userPath -split ";") -notcontains $BinDir) {
  $newPath = if ($userPath.TrimEnd(";")) { ($userPath.TrimEnd(";") + ";" + $BinDir) } else { $BinDir }
  [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
  $env:Path = $env:Path + ";" + $BinDir   # also for THIS session so the health check below resolves it
  Write-Host "  Added $BinDir to your PATH (User) - open a NEW terminal for 'decompute' to be found."
} else {
  Write-Host "  ($BinDir already on your User PATH)"
}

# 8. Auto-start + self-heal via Task Scheduler (the launchd KeepAlive equivalent), per-user, no admin.
$gwExe = Join-Path $Scripts "decompute-gateway.exe"
$gwLog = Join-Path $DecomputeHome "gateway.log"
$taskName = "DecomputeGateway"
try {
  $taskCommand = "`$env:DECOMPUTE_HOME = $(Quote-PowerShellString $DecomputeHome); `$env:DECOMPUTE_DATA_DIR = $(Quote-PowerShellString $DecomputeHome); & $(Quote-PowerShellString $gwExe) *>> $(Quote-PowerShellString $gwLog)"
  $taskArgs = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command `"$taskCommand`""
  $action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $taskArgs
  $trigger = New-ScheduledTaskTrigger -AtLogOn
  # RestartCount/Interval = self-heal on crash, mirroring launchd KeepAlive+ThrottleInterval.
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
  Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force -ErrorAction Stop | Out-Null
  Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
  Write-Host "  ok gateway set to auto-start and self-heal (Task Scheduler: $taskName)"
} catch {
  Write-Host "  warn: could not register the auto-start task ($($_.Exception.Message)). The on-demand 'decompute claude' start still works." -ForegroundColor Yellow
}

# 9. Verify the gateway came up (Task Scheduler launch is async; on-demand start is the fallback).
Write-Host -NoNewline "  -> starting the gateway"
$ok = $false
for ($i = 0; $i -lt 20; $i++) {
  try { Invoke-WebRequest -Uri "http://127.0.0.1:$GwPort/health" -TimeoutSec 2 -UseBasicParsing | Out-Null; $ok = $true; break } catch { Write-Host -NoNewline "."; Start-Sleep -Seconds 1 }
}
Write-Host ""
if ($ok) {
  Write-Host "ok gateway is up on http://127.0.0.1:$GwPort" -ForegroundColor Green
} else {
  Write-Host "warn: the gateway did not respond within 20s - it may still be warming up." -ForegroundColor Yellow
  Write-Host ("    Logs:          Get-Content -Tail 40 '" + $gwLog + "'")
  Write-Host ("    Start by hand: & '" + $gwExe + "'")
}

Write-Host ""
Write-Host "Next:"
Write-Host "  decompute init        # choose your Claude plan / API key + gateway URL"
Write-Host "  decompute claude      # route Claude Code through the gateway (auto-starts it)"
Write-Host "  decompute doctor      # verify the connection"
