<#
  MT5 TV Terminal launcher (started hidden by launch.vbs).
    1. Auto-update: newer GitHub release -> verify SHA-256 -> silent upgrade -> relaunch.
    2. Start the backend invisibly - Native mode (bundled Python + your MT5 terminal, default)
       or Docker mode (MT5 headless under Wine + bridge containers, experimental).
    3. Wait until the bridge answers, then open the app in its own window.
  Any failure is shown in a message box and written to %LOCALAPPDATA%\MT5TVTerminal\launcher.log.
  Set MT5TV_NONINTERACTIVE=1 to suppress all UI (used by the CI smoke test).
#>
param([switch]$Stop, [switch]$NoUpdate, [switch]$NoWindow)

# Native tools (docker, pip) write progress to stderr. Under Windows PowerShell 5.1 with
# 'Stop', that turns into a terminating error - so keep 'Continue' and check exit codes.
$ErrorActionPreference = 'Continue'
$AppDir      = Split-Path -Parent $PSScriptRoot
$DataDir     = Join-Path $env:LOCALAPPDATA 'MT5TVTerminal'
$Repo        = 'jokingtim2468-max/trading-app'
$Url         = 'http://127.0.0.1:8765'
$LogFile     = Join-Path $DataDir 'launcher.log'
$Interactive = $env:MT5TV_NONINTERACTIVE -ne '1'
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

function Log($m) { "$(Get-Date -Format s)  $m" | Add-Content -Path $LogFile -Encoding UTF8 }
Log "---- launcher start (app $AppDir, PowerShell $($PSVersionTable.PSVersion))"

# ------------------------------------------------------------------ small status window
$script:Splash = $null
$script:SplashLabel = $null
if ($Interactive) {
  try {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    $f = New-Object System.Windows.Forms.Form
    $f.Text = 'MT5 TV Terminal'
    $f.Size = New-Object System.Drawing.Size(420, 130)
    $f.StartPosition = 'CenterScreen'
    $f.FormBorderStyle = 'FixedDialog'
    $f.MaximizeBox = $false; $f.MinimizeBox = $true; $f.TopMost = $true
    $f.BackColor = [System.Drawing.Color]::FromArgb(19, 23, 34)
    $f.ForeColor = [System.Drawing.Color]::FromArgb(209, 212, 220)
    $ico = Join-Path $AppDir 'installer\app.ico'
    if (Test-Path $ico) { $f.Icon = New-Object System.Drawing.Icon $ico }
    $l = New-Object System.Windows.Forms.Label
    $l.Dock = 'Fill'; $l.TextAlign = 'MiddleCenter'
    $l.Font = New-Object System.Drawing.Font('Segoe UI', 10)
    $l.Text = 'Starting...'
    $f.Controls.Add($l)
    $f.Show(); [System.Windows.Forms.Application]::DoEvents()
    $script:Splash = $f; $script:SplashLabel = $l
  } catch { Log "splash unavailable: $_" }
}
function Status($m) {
  Log $m
  if ($script:SplashLabel) { $script:SplashLabel.Text = $m; [System.Windows.Forms.Application]::DoEvents() }
}
function Close-Splash { if ($script:Splash) { $script:Splash.Close(); $script:Splash = $null } }
function Fail($text) {
  Log "ERROR $text"
  Close-Splash
  if ($Interactive) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show("$text`n`nLog file: $LogFile", 'MT5 TV Terminal', 'OK', 'Error') | Out-Null
  }
  exit 1
}
function Pump([int]$seconds) {
  $end = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $end) { if ($script:Splash) { [System.Windows.Forms.Application]::DoEvents() }; Start-Sleep -Milliseconds 200 }
}

# Run a console program hidden, capture its output to the log, return the exit code.
function Invoke-Hidden([string]$exe, [string[]]$argv, [string]$cwd = $AppDir) {
  $out = [IO.Path]::GetTempFileName(); $err = [IO.Path]::GetTempFileName()
  try {
    $p = Start-Process -FilePath $exe -ArgumentList $argv -WorkingDirectory $cwd -WindowStyle Hidden `
           -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    while (-not $p.HasExited) { if ($script:Splash) { [System.Windows.Forms.Application]::DoEvents() }; Start-Sleep -Milliseconds 300 }
    Get-Content $out, $err -ErrorAction SilentlyContinue | ForEach-Object { Log "  $_" }
    return $p.ExitCode
  } catch { Log "  failed to run ${exe}: $_"; return -1 }
  finally { Remove-Item $out, $err -Force -ErrorAction SilentlyContinue }
}

function Get-RemoteText([string]$u) {
  # GitHub release assets are application/octet-stream; PowerShell 5.1 would return bytes.
  $f = [IO.Path]::GetTempFileName()
  try { Invoke-WebRequest $u -OutFile $f -UseBasicParsing; return [IO.File]::ReadAllText($f) }
  finally { Remove-Item $f -Force -ErrorAction SilentlyContinue }
}

function Test-Bridge {
  try { $r = Invoke-WebRequest "$Url/health" -UseBasicParsing -TimeoutSec 3; return ($r.Content | ConvertFrom-Json) }
  catch { return $null }
}

$Config = [pscustomobject]@{ mode = 'native' }
$cfgPath = Join-Path $AppDir 'config.json'
if (Test-Path $cfgPath) { try { $Config = Get-Content $cfgPath -Raw | ConvertFrom-Json } catch { Log "bad config.json: $_" } }
Log "mode: $($Config.mode)"

try {
  # ---------------------------------------------------------------- stop
  if ($Stop) {
    Status 'Stopping...'
    if ($Config.mode -eq 'docker' -and (Get-Command docker -ErrorAction SilentlyContinue)) {
      Invoke-Hidden 'docker' @('compose', '-f', "`"$AppDir\docker-compose.yml`"", 'down') | Out-Null
    }
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
      Where-Object { $_.CommandLine -like '*uvicorn*app.main:app*' } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Close-Splash; exit 0
  }

  # ---------------------------------------------------------------- auto-update
  if (-not $NoUpdate) {
    Status 'Checking for updates...'
    try {
      $local = [version](Get-Content "$AppDir\VERSION" -Raw).Trim()
      $rel = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -TimeoutSec 8 `
               -Headers @{ 'User-Agent' = 'MT5TVTerminal-Updater' }
      $remote = [version]($rel.tag_name.TrimStart('v'))
      if ($remote -gt $local) {
        $exe = $rel.assets | Where-Object { $_.name -like 'MT5TVTerminal-Setup-*.exe' } | Select-Object -First 1
        $sum = $rel.assets | Where-Object { $_.name -eq "$($exe.name).sha256" } | Select-Object -First 1
        if ($exe -and $sum) {
          Status "Downloading update $remote..."
          $tmp = Join-Path $env:TEMP $exe.name
          Invoke-WebRequest $exe.browser_download_url -OutFile $tmp -UseBasicParsing
          $expected = (Get-RemoteText $sum.browser_download_url).Trim().Split()[0].ToLower()
          $actual = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
          if ($expected -eq $actual) {
            Status "Installing update $remote..."
            # Stop the running bridge so its files can be replaced; the installer relaunches us.
            Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
              Where-Object { $_.CommandLine -like '*uvicorn*app.main:app*' } |
              ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Process $tmp -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/RELAUNCH=1'
            Close-Splash; exit 0
          }
          Log "update checksum mismatch (expected $expected got $actual) - skipped"
          Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        }
      } else { Log "up to date ($local)" }
    } catch { Log "update check skipped: $($_.Exception.Message)" }
  }

  # ---------------------------------------------------------------- backend
  $health = Test-Bridge
  if ($health) {
    Log 'bridge already running'
  } elseif ($Config.mode -eq 'docker') {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
      Fail "Docker mode needs Docker Desktop, which is not installed.`n`nInstall it (https://www.docker.com/products/docker-desktop/) or re-run the installer and choose Native mode."
    }
    Status 'Starting Docker...'
    if ((Invoke-Hidden 'docker' @('info')) -ne 0) {
      $dd = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
      if (Test-Path $dd) { Start-Process $dd -WindowStyle Minimized }
      $ok = $false
      for ($i = 0; $i -lt 60; $i++) { Pump 3; if ((Invoke-Hidden 'docker' @('info')) -eq 0) { $ok = $true; break } }
      if (-not $ok) { Fail 'Docker Desktop did not start within 3 minutes.' }
    }
    Status 'Starting the MT5 and bridge containers (first start builds them: 5-15 min)...'
    $code = Invoke-Hidden 'docker' @('compose', 'up', '-d', '--build')
    if ($code -ne 0) { Fail "Starting the containers failed (exit $code)." }
  } else {
    # Native: Python is bundled in the installer under runtime\python with every package.
    $py = Join-Path $AppDir 'runtime\python\pythonw.exe'
    if (-not (Test-Path $py)) { Fail "The bundled Python runtime is missing ($py). Please reinstall." }
    $mt5 = @("$env:ProgramFiles\MetaTrader 5\terminal64.exe") +
           @(Get-ChildItem "$env:ProgramFiles\*\terminal64.exe", "${env:ProgramFiles(x86)}\*\terminal64.exe" -ErrorAction SilentlyContinue | ForEach-Object FullName)
    if (-not ($mt5 | Where-Object { $_ -and (Test-Path $_) })) {
      Log 'no MetaTrader 5 terminal found - charts via TradingView only until MT5 is installed'
    }
    Status 'Starting the trading bridge...'
    $env:AGENT_STATE_DIR = Join-Path $DataDir 'state'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $bridgeLog = Join-Path $DataDir 'bridge.log'
    Start-Process -FilePath $py -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8765') `
      -WorkingDirectory "$AppDir\backend" -WindowStyle Hidden -RedirectStandardError $bridgeLog `
      -RedirectStandardOutput (Join-Path $DataDir 'bridge.out.log') | Out-Null
  }

  Status 'Waiting for MT5 and TradingView feeds...'
  $limit = if ($Config.mode -eq 'docker') { 900 } else { 120 }
  $end = (Get-Date).AddSeconds($limit)
  while (-not $health -and (Get-Date) -lt $end) { Pump 2; $health = Test-Bridge }
  if (-not $health) {
    $tail = ''
    $bl = Join-Path $DataDir 'bridge.log'
    if (Test-Path $bl) { $tail = (Get-Content $bl -Tail 12) -join "`n" }
    Fail "The trading bridge did not start.`n`n$tail"
  }
  Log "bridge up: mt5=$($health.mt5.status) external_gave_up=$($health.external.gave_up)"

  # ---------------------------------------------------------------- app window
  if (-not $NoWindow -and $Interactive) {
    Status 'Opening the terminal...'
    $edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") |
      Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($edge) { Start-Process $edge -ArgumentList "--app=$Url", "--user-data-dir=`"$DataDir\window`"", '--start-maximized' }
    else { Start-Process $Url }
    Pump 3
  }
  Close-Splash
  exit 0
} catch {
  Fail "Unexpected error: $($_.Exception.Message)`n$($_.ScriptStackTrace)"
}
