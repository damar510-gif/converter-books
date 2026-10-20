# The helper's one-time install (docs/helper.md). The status page shows the line that
# downloads this file and runs it in PowerShell. It holds no secret: the helper works
# only through the "تحويل الكتب" folder that Google Drive for desktop keeps on the computer.
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

function Say([string]$text) { Write-Host ''; Write-Host $text }

$RootName = 'تحويل الكتب'
$HelperName = 'بيانات المساعد'
$ProgramName = 'البرنامج'

# "G:\My Drive\تحويل الكتب\بيانات المساعد" (streaming), or under the user's folder (mirroring).
function Find-HelperFolder {
  $places = @()
  foreach ($d in (Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue)) {
    if ($d.Root) { $places += (Join-Path $d.Root "*\$RootName\$HelperName") }
  }
  $places += (Join-Path $env:USERPROFILE "*\$RootName\$HelperName")
  foreach ($p in $places) {
    foreach ($hit in @(Get-Item -Path $p -ErrorAction SilentlyContinue)) {
      if (Test-Path -LiteralPath (Join-Path $hit.FullName "$ProgramName\launcher.ps1")) { return $hit.FullName }
    }
  }
  return $null
}

function Find-DriveApp {
  $exe = Get-ChildItem -Path (Join-Path $env:ProgramFiles 'Google\Drive File Stream\*\GoogleDriveFS.exe') -ErrorAction SilentlyContinue |
    Sort-Object FullName | Select-Object -Last 1
  if ($exe) { return $exe.FullName }
  return $null
}

$folder = Find-HelperFolder
if (-not $folder) {
  $app = Find-DriveApp
  if (-not $app) {
    Say 'المساعد يحتاج برنامج Google Drive لسطح المكتب. أثبّته الآن، انتظر قليلاً…'
    if (Get-Command winget -ErrorAction SilentlyContinue) {
      winget install --id Google.GoogleDrive -e --silent --accept-source-agreements --accept-package-agreements | Out-Host
      $app = Find-DriveApp
    }
    if (-not $app) {
      Say 'لم أستطع تثبيته وحدي. نزّله من https://www.google.com/drive/download/ وثبّته، ثم الصق السطر مرة أخرى.'
      return
    }
  }
  if (-not (Get-Process -Name GoogleDriveFS -ErrorAction SilentlyContinue)) { Start-Process -FilePath $app }
  Say 'إن ظهرت نافذة Google Drive فسجّل الدخول بحسابك نفسه (الذي فيه مجلد «تحويل الكتب»). أنا أنتظر هنا…'
  for ($i = 0; $i -lt 120 -and -not $folder; $i++) {
    Start-Sleep -Seconds 15
    $folder = Find-HelperFolder
  }
  if (-not $folder) {
    Say 'لم يظهر مجلد «تحويل الكتب» على الجهاز بعد. تأكد أن Google Drive مفتوح بحسابك، ثم الصق السطر مرة أخرى.'
    return
  }
}

$local = Join-Path $env:LOCALAPPDATA 'ConverterHelper'
New-Item -ItemType Directory -Force -Path $local | Out-Null
$withBom = New-Object Text.UTF8Encoding $true
$config = @{ folder = $folder } | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $local 'config.json'), $config, $withBom)
# Saved with a byte order mark: Windows PowerShell reads a file without one as ANSI.
$launcher = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes((Join-Path $folder "$ProgramName\launcher.ps1"))).TrimStart([char]0xFEFF)
[IO.File]::WriteAllText((Join-Path $local 'launcher.ps1'), $launcher, $withBom)

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $local 'launcher.ps1') -Install
