<#
  One-command install for MT5 TV Terminal.
  PowerShell:
    irm https://raw.githubusercontent.com/jokingtim2468-max/trading-app/main/install.ps1 | iex
  Command Prompt (cmd.exe, normal or "Run as administrator"):
    powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; irm https://raw.githubusercontent.com/jokingtim2468-max/trading-app/main/install.ps1 | iex"
  Downloads the latest installer from GitHub Releases, verifies its SHA-256 and opens the setup wizard.
#>
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$repo = 'jokingtim2468-max/trading-app'

function Get-RemoteText([string]$u) {
  # GitHub serves release assets as application/octet-stream; Windows PowerShell 5.1 would hand
  # back raw bytes from .Content, so download to a file and read it as text instead.
  $f = [IO.Path]::GetTempFileName()
  try { Invoke-WebRequest $u -OutFile $f -UseBasicParsing; return [IO.File]::ReadAllText($f) } finally { Remove-Item $f -Force -ErrorAction SilentlyContinue }
}

Write-Host 'MT5 TV Terminal - fetching the latest release...' -ForegroundColor Cyan
try {
  $rel = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -Headers @{ 'User-Agent' = 'MT5TVTerminal-Install' }
} catch {
  throw "No published release found on github.com/$repo yet. Run the 'Windows installer' GitHub Action (or push a VERSION bump) to publish one."
}
$exe = $rel.assets | Where-Object { $_.name -like 'MT5TVTerminal-Setup-*.exe' } | Select-Object -First 1
$sum = $rel.assets | Where-Object { $_.name -eq "$($exe.name).sha256" } | Select-Object -First 1
if (-not $exe -or -not $sum) { throw "Release $($rel.tag_name) is missing the installer or its checksum." }

$dest = Join-Path $env:TEMP $exe.name
Write-Host "Downloading $($exe.name) ($([math]::Round($exe.size / 1MB, 1)) MB)..."
Invoke-WebRequest $exe.browser_download_url -OutFile $dest -UseBasicParsing
$expected = (Get-RemoteText $sum.browser_download_url).Trim().Split()[0].ToLower()
$actual = (Get-FileHash $dest -Algorithm SHA256).Hash.ToLower()
if ($expected -ne $actual) { Remove-Item $dest -Force; throw "Checksum mismatch - download discarded (expected $expected, got $actual)." }
Write-Host "SHA-256 verified ($actual). Opening the setup wizard..." -ForegroundColor Green
Start-Process $dest -Wait
Remove-Item $dest -Force -ErrorAction SilentlyContinue
