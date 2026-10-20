# The helper's loop (docs/helper.md). install.ps1 copies this file to
# %LOCALAPPDATA%\ConverterHelper and runs it at every sign-in, hidden. Each minute it
# loads the newest helper.ps1 from the synced Drive folder and runs one cycle, so the
# helper updates itself whenever helper/ changes in the repository.
param([switch]$Install)
$ErrorActionPreference = 'Stop'

$Local = Join-Path $env:LOCALAPPDATA 'ConverterHelper'
$Config = [IO.File]::ReadAllText((Join-Path $Local 'config.json')) | ConvertFrom-Json
$Folder = [string]$Config.folder
$Program = Join-Path $Folder 'البرنامج'

function Write-Log([string]$text) {
  $log = Join-Path $Local 'helper.log'
  try {
    if ((Test-Path $log) -and (Get-Item $log).Length -gt 1MB) { Move-Item -Force $log "$log.old" }
    Add-Content -Path $log -Value ((Get-Date).ToString('yyyy-MM-dd HH:mm:ss') + ' ' + $text) -Encoding UTF8
  } catch { }
}

function Read-Utf8([string]$path) {
  return [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($path)).TrimStart([char]0xFEFF)
}

# The newest helper.ps1, or the last copy that loaded when Drive cannot be read.
function Get-Core {
  $copy = Join-Path $Local 'core.ps1'
  try {
    $text = Read-Utf8 (Join-Path $Program 'helper.ps1')
    $block = [scriptblock]::Create($text)
    [IO.File]::WriteAllText($copy, $text, (New-Object Text.UTF8Encoding $true))
    return $block
  } catch {
    Write-Log "helper.ps1 not loaded from Drive: $_"
    return [scriptblock]::Create((Read-Utf8 $copy))
  }
}

if ($Install) {
  . (Get-Core)
  Invoke-HelperInstall
  return
}

$mutex = New-Object Threading.Mutex($false, 'Local\ConverterBooksHelper')
if (-not $mutex.WaitOne(0)) { return }
Write-Log 'started'
while ($true) {
  try {
    . (Get-Core)
    Invoke-HelperCycle
  } catch {
    Write-Log "cycle failed: $_"
  }
  Start-Sleep -Seconds 60
}
