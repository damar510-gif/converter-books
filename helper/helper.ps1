# The helper's work (docs/helper.md), loaded by launcher.ps1 every minute from the synced
# Drive folder. It reads requests.json (written by the status page's script), runs the
# subscription tools on this computer (Grok Build, Antigravity, Codex), writes the
# results under «نتائج», and reports in helper-status.json, which the status page shows.
# Windows PowerShell 5.1: no ?? or ?: operators.

$HelperVersion = '2026-10-20.4'
$ResultsName = 'نتائج'
$TestPagesName = 'صفحات الاختبار'
$PageTimeoutSec = 20 * 60

# Models per tool, as the tool's own command line names them. All are offered and the
# user chooses; the labels carry the user's warnings (Gemini 4 Argon is not for
# transcribing: it uses up the limits; Grok 4.7 Fast costs twice as much).
$Tools = [ordered]@{
  grok  = @{ label = 'Grok Build'; models = @(
      @{ id = 'grok-4.7'; label = 'Grok 4.7' },
      @{ id = 'grok-4.7-fast'; label = 'Grok 4.7 Fast (بضعف السعر)' },
      @{ id = 'grok-4.6'; label = 'Grok 4.6' },
      @{ id = 'grok-4.5'; label = 'Grok 4.5' }) }
  agy   = @{ label = 'Antigravity'; models = @(
      @{ id = 'gemini-4-argon'; label = 'Gemini 4 Argon (لا للتفريغ: يستنفد الحدود)' },
      @{ id = 'gemini-3.8-flash'; label = 'Gemini 3.8 Flash' },
      @{ id = 'claude-sonnet-5.5'; label = 'Claude Sonnet 5.5' },
      @{ id = 'claude-opus-5.5'; label = 'Claude Opus 5.5' },
      @{ id = 'gpt-oss-120b'; label = 'GPT-OSS 120B' }) }
  # Codex's /model list on the user's free ChatGPT account (2026-10-20).
  codex = @{ label = 'Codex'; models = @(
      @{ id = 'gpt-6.1-sol'; label = 'GPT-6.1 Sol' },
      @{ id = 'gpt-6-astra'; label = 'GPT-6 Astra' },
      @{ id = 'gpt-6-sol'; label = 'GPT-6 Sol' },
      @{ id = 'gpt-6-luna'; label = 'GPT-6 Luna' },
      @{ id = 'gpt-5.6-sol'; label = 'GPT-5.6 Sol' },
      @{ id = 'gpt-5.6-terra'; label = 'GPT-5.6 Terra' },
      @{ id = 'gpt-5.6-luna'; label = 'GPT-5.6 Luna' }) }
}
$Efforts = [ordered]@{ low = 'جهد منخفض'; medium = 'جهد متوسط'; high = 'جهد عالٍ' }

# The prompt each tool gets on its command line: plain ASCII, because cmd.exe shims would
# garble Arabic. The Arabic instructions are in task.md next to the page image.
$ToolPrompt = 'Read the file task.md in the current folder and follow it exactly. Your reply must be only the transcribed text of the page image page.png, nothing else.'

# The converter's reading prompt (converter.py, OCR_PROMPT), and how to look at the page.
$TaskText = @'
أنت تنسخ صفحة واحدة من كتاب عربي ممسوح ضوئياً. انسخ النص كما هو حرفياً بلا تلخيص ولا حذف ولا تصحيح ولا إضافة.

قواعد الإخراج (Markdown):
- إن كانت الصفحة بعمودين فاقرأ العمود الأيمن كاملاً من أعلاه إلى أسفله، ثم العمود الأيسر.
- عنوان مقال أو بحث جديد (العنوان الكبير في رأس المقال) يُكتب في سطر مستقل يبدأ بـ "# ".
- العناوين الفرعية داخل المقال تُكتب في سطر مستقل يبدأ بـ "## ".
- افصل بين الفقرات بسطر فارغ، واجمع أسطر الفقرة الواحدة في سطر واحد.
- الشعر: كل بيت في سطر مستقل يبدأ بـ "| "، وبين الشطرين " … ".
- الحواشي (السطور الصغيرة أسفل الصفحة تحت الخط): في موضع رقم الحاشية داخل المتن اكتب [^1] (بنفس رقمها)، وفي آخر إخراجك اكتب كل حاشية في سطر مستقل هكذا: [^1]: نص الحاشية
- الكلمات الأجنبية تُنسخ بحروفها كما هي.
- تجاهل تماماً: أرقام الصفحات، والترويسة المكررة أعلى الصفحة، والعلامة المائية (مثل «جلجامش»)، والزخارف.
- لا تكتب أي تعليق أو شرح أو مقدمة، فقط نص الصفحة.
- إن كانت الصفحة فارغة أو صورة بلا نص فاكتب فقط: [[EMPTY]]
- صورة الصفحة هي الملف page.png في هذا المجلد. انظر إليها بنفسك؛ لا تشغّل أي برنامج قراءة ضوئية ولا تكتب أي كود.
- ردّك هو نص الصفحة فقط.
'@

# ---------------------------------------------------------------------------
# Small tools
# ---------------------------------------------------------------------------

$Utf8 = New-Object Text.UTF8Encoding $false

function Now-Iso { return (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }

function Write-Utf8([string]$path, [string]$text) { [IO.File]::WriteAllText($path, $text, $Utf8) }

function Read-Json([string]$path) {
  if (-not (Test-Path -LiteralPath $path)) { return $null }
  try { return (Read-Utf8 $path) | ConvertFrom-Json } catch { return $null }
}

function Load-State {
  $s = Read-Json (Join-Path $Local 'state.json')
  if (-not $s) { $s = New-Object PSObject }
  foreach ($k in 'done', 'results') {
    if (-not ($s.PSObject.Properties.Name -contains $k)) { $s | Add-Member -NotePropertyName $k -NotePropertyValue @() }
  }
  if (-not ($s.PSObject.Properties.Name -contains 'probed')) { $s | Add-Member -NotePropertyName probed -NotePropertyValue '' }
  return $s
}

function Save-State($s) {
  $s.done = @($s.done | Select-Object -Last 50)
  $s.results = @($s.results | Select-Object -Last 5)
  Write-Utf8 (Join-Path $Local 'state.json') ($s | ConvertTo-Json -Depth 6)
}

# Tools installed after the helper started are found too.
function Update-Path {
  $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}

# Only what Windows can start: npm also puts an extension-less shell script next to the
# .cmd, and Get-Command lists it too.
function Find-Tool([string]$name) {
  $c = Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue |
    Where-Object { $_.Source -match '\.(exe|cmd|bat)$' } |
    Sort-Object { if ($_.Source -match '\.exe$') { 0 } else { 1 } } | Select-Object -First 1
  if ($c) { return $c.Source }
  return $null
}

function Quote-Arg([string]$a) {
  if ($a -ne '' -and $a -notmatch '[\s"]') { return $a }
  $a = $a -replace '(\\*)"', '$1$1\"'
  $a = $a -replace '(\\+)$', '$1$1'
  return '"' + $a + '"'
}

# Runs a program with no window and no input: {code, out, err, seconds, timedOut}.
function Invoke-Tool([string]$exe, [string[]]$arguments, [string]$cwd, [int]$timeoutSec) {
  $line = ($arguments | ForEach-Object { Quote-Arg $_ }) -join ' '
  $psi = New-Object Diagnostics.ProcessStartInfo
  if ($exe -match '\.(cmd|bat)$') {
    $psi.FileName = $env:ComSpec
    $psi.Arguments = '/d /s /c "' + (Quote-Arg $exe) + ' ' + $line + '"'
  } else {
    $psi.FileName = $exe
    $psi.Arguments = $line
  }
  $psi.WorkingDirectory = $cwd
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $psi.RedirectStandardInput = $true
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.StandardOutputEncoding = $Utf8
  $psi.StandardErrorEncoding = $Utf8
  $psi.EnvironmentVariables['NO_COLOR'] = '1'
  $start = Get-Date
  $p = [Diagnostics.Process]::Start($psi)
  $p.StandardInput.Close()
  $out = $p.StandardOutput.ReadToEndAsync()
  $err = $p.StandardError.ReadToEndAsync()
  $timedOut = -not $p.WaitForExit($timeoutSec * 1000)
  if ($timedOut) { & taskkill.exe /T /F /PID $p.Id 2>&1 | Out-Null }
  $p.WaitForExit()
  return @{ code = $p.ExitCode; out = $out.Result; err = $err.Result; timedOut = $timedOut
    seconds = [int]((Get-Date) - $start).TotalSeconds }
}

function Tail([string]$text, [int]$n) {
  if (-not $text) { return '' }
  if ($text.Length -le $n) { return $text }
  return $text.Substring($text.Length - $n)
}

# ---------------------------------------------------------------------------
# Tools on this computer
# ---------------------------------------------------------------------------

function Get-Tools {
  $out = [ordered]@{}
  foreach ($name in $Tools.Keys) { $out[$name] = Find-Tool $name }
  return $out
}

# What each tool's command line accepts, and whether it answers without a window:
# probe.txt in the helper folder, read when the commands below need fixing.
function Invoke-Probe($found) {
  $tmp = Join-Path $Local 'probe'
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  $sb = New-Object Text.StringBuilder
  [void]$sb.AppendLine("helper $HelperVersion on $env:COMPUTERNAME, PowerShell $($PSVersionTable.PSVersion), $(Now-Iso)")
  $checks = @{
    grok  = @(@('--version'), @('--help'), @('-p', 'Reply with the single word OK.', '--output-format', 'plain', '--tools', 'read_file', '--no-auto-update'))
    agy   = @(@('--version'), @('--help'), @('-p', 'Reply with the single word OK.', '--model', 'gemini-3.8-flash'))
    codex = @(@('--version'), @('login', 'status'), @('exec', '--help'), @('exec', '--skip-git-repo-check', '--sandbox', 'read-only', 'Reply with the single word OK.'))
  }
  foreach ($name in $Tools.Keys) {
    [void]$sb.AppendLine('')
    [void]$sb.AppendLine("===== $name : $($found[$name])")
    if (-not $found[$name]) { continue }
    foreach ($a in $checks[$name]) {
      try {
        $r = Invoke-Tool $found[$name] $a $tmp 180
        [void]$sb.AppendLine("--- $name $($a -join ' ')  (exit $($r.code), $($r.seconds)s$(if ($r.timedOut) { ', timed out' }))")
        [void]$sb.AppendLine((Tail $r.out 6000))
        if ($r.err) { [void]$sb.AppendLine('[stderr] ' + (Tail $r.err 3000)) }
      } catch {
        [void]$sb.AppendLine("--- $name $($a -join ' ') failed: $_")
      }
    }
  }
  Write-Utf8 (Join-Path $Folder 'probe.txt') $sb.ToString()
}

# One page with one tool: {text, code, seconds, timedOut, err}.
function Invoke-Page([string]$tool, [string]$exe, [string]$model, [string]$effort, [string]$dir) {
  $outFile = Join-Path $dir 'out.md'
  switch ($tool) {
    # Read-only everywhere: each tool may look at the page folder and nothing else; no
    # shell, no edits, no web, and never a flag that skips the tool's permission checks.
    'grok' {
      $a = @('-p', $ToolPrompt, '--cwd', $dir, '--output-format', 'plain', '--tools', 'read_file,list_dir',
        '--no-auto-update', '--disable-web-search', '--no-subagents')
      if ($model) { $a += @('-m', $model) }
      if ($effort) { $a += @('--effort', $effort) }
    }
    'agy' {
      $a = @('-p', $ToolPrompt)
      if ($model) { $a += @('--model', $model) }
    }
    'codex' {
      $a = @('exec', $ToolPrompt, '--skip-git-repo-check', '--sandbox', 'read-only', '-C', $dir, '-o', $outFile)
      if ($model) { $a += @('-m', $model) }
      if ($effort) { $a += @('-c', "model_reasoning_effort=$effort") }
      $a += @('-i', 'page.png')
    }
  }
  $r = Invoke-Tool $exe $a $dir $PageTimeoutSec
  $text = $r.out
  if ($tool -eq 'codex' -and (Test-Path -LiteralPath $outFile)) { $text = Read-Utf8 $outFile }
  $text = ([string]$text).Trim()
  if ($text -match '^```[a-z]*\r?\n([\s\S]*?)\r?\n```$') { $text = $Matches[1].Trim() }
  return @{ text = $text; code = $r.code; seconds = $r.seconds; timedOut = $r.timedOut; err = $r.err }
}

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

$script:Progress = ''

function Model-Label([string]$tool, [string]$model) {
  foreach ($m in $Tools[$tool].models) { if ($m.id -eq $model) { return $m.label } }
  return "$($Tools[$tool].label) $model"
}

# The six hard pages of «تراث الإنسانية» (results branch, compare/pages), read by one
# model; Claude compares the texts with the checked ones.
function Invoke-Test($req, $found, $state) {
  $parts = ([string]$req.choices.model) -split '\|', 2
  $tool = $parts[0]
  $model = if ($parts.Count -gt 1) { $parts[1] } else { '' }
  $effort = [string]$req.choices.effort
  if (-not $Tools.Contains($tool)) { throw "unknown tool '$tool'" }
  if (-not $found[$tool]) { throw "$($Tools[$tool].label) is not installed" }
  if (-not $Efforts.Contains($effort)) { $effort = 'medium' }
  $label = (Model-Label $tool $model) + ' — ' + $Efforts[$effort]

  $pages = @(Get-ChildItem -LiteralPath (Join-Path $Folder $TestPagesName) -Filter '*.png' | Sort-Object Name)
  if (-not $pages.Count) { throw 'no test pages in the Drive folder yet' }
  $name = (Get-Date).ToString('yyyy-MM-dd HH-mm') + ' تجربة ' + ($label -replace '[\\/:*?"<>|]', '-')
  $outDir = Join-Path (Join-Path $Folder $ResultsName) $name
  New-Item -ItemType Directory -Force -Path $outDir | Out-Null
  $work = Join-Path $Local "work\$($req.id)"

  $log = New-Object Text.StringBuilder
  [void]$log.AppendLine("tool=$tool model=$model effort=$effort exe=$($found[$tool]) helper=$HelperVersion started=$(Now-Iso)")
  $meta = @()
  $ok = 0
  $begin = Get-Date
  $i = 0
  foreach ($png in $pages) {
    $i++
    $script:Progress = "${label}: الصفحة $i من $($pages.Count)"
    Write-Status $found $state $req.id
    $stem = [IO.Path]::GetFileNameWithoutExtension($png.Name)
    $dir = Join-Path $work $stem
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Copy-Item -LiteralPath $png.FullName -Destination (Join-Path $dir 'page.png') -Force
    Write-Utf8 (Join-Path $dir 'task.md') $TaskText
    try {
      $r = Invoke-Page $tool $found[$tool] $model $effort $dir
    } catch {
      $r = @{ text = ''; code = -1; seconds = 0; timedOut = $false; err = "$_" }
    }
    if ($r.text -and $r.code -eq 0) { $ok++ }
    Write-Utf8 (Join-Path $outDir "$stem.md") $r.text
    [void]$log.AppendLine("$stem exit=$($r.code) seconds=$($r.seconds) chars=$($r.text.Length) timedOut=$($r.timedOut)")
    if ($r.code -ne 0 -or -not $r.text) { [void]$log.AppendLine('  stderr: ' + (Tail $r.err 2000)) }
    $meta += @{ page = $stem; code = $r.code; seconds = $r.seconds; chars = $r.text.Length; timedOut = $r.timedOut }
    Write-Utf8 (Join-Path $outDir 'log.txt') $log.ToString()
  }
  $minutes = [int][Math]::Ceiling(((Get-Date) - $begin).TotalMinutes)
  Write-Utf8 (Join-Path $outDir 'meta.json') (@{ request = $req.id; tool = $tool; model = $model; effort = $effort
      helper = $HelperVersion; finished = (Now-Iso); minutes = $minutes; pages = $meta } | ConvertTo-Json -Depth 5)
  Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
  $kind = if ($ok -eq $pages.Count) { '' } elseif ($ok) { 'warn' } else { 'bad' }
  return @{ text = "تجربة ${label}: قُرئت $ok من $($pages.Count) صفحات في $minutes دقيقة."; kind = $kind
    path = "$ResultsName/$name"; linkText = 'النتائج' }
}

function Invoke-Request($req, $found, $state) {
  switch ([string]$req.action) {
    'test' { return Invoke-Test $req $found $state }
    default { return @{ text = "طلب لا يعرفه المساعد بعد: $($req.action)"; kind = 'warn' } }
  }
}

# ---------------------------------------------------------------------------
# Status for the page
# ---------------------------------------------------------------------------

function Write-Status($found, $state, [string]$busy) {
  $names = @()
  $missing = @()
  foreach ($name in $Tools.Keys) {
    if ($found[$name]) { $names += $Tools[$name].label } else { $missing += $Tools[$name].label }
  }
  $lines = @()
  $toolsText = 'البرامج على الجهاز: ' + $(if ($names.Count) { $names -join '، ' } else { 'لا شيء' })
  if ($missing.Count) { $toolsText += ' (غير موجود: ' + ($missing -join '، ') + ')' }
  $lines += @{ text = $toolsText }
  if ($busy -and $script:Progress) { $lines += @{ text = 'التقدم: ' + $script:Progress } }
  foreach ($r in @($state.results)) { if ($r) { $lines += $r } }

  $choices = @()
  foreach ($name in $Tools.Keys) {
    if (-not $found[$name]) { continue }
    foreach ($m in $Tools[$name].models) {
      $choices += @{ value = "$name|$($m.id)"; label = "$($m.label) — $($Tools[$name].label)" }
    }
  }
  $actions = @()
  if ($choices.Count) {
    $actions += @{
      id = 'test'; label = 'جرّب نموذجاً على صفحات الاختبار الست'; button = 'ابدأ التجربة'
      note = 'يقرأ الصفحات الست الصعبة من «تراث الإنسانية»، ثم يقارن Claude النص بالنص المدقق.'
      options = @(
        @{ name = 'model'; label = 'النموذج'; choices = $choices },
        @{ name = 'effort'; label = 'الجهد'; choices = @($Efforts.Keys | ForEach-Object { @{ value = $_; label = $Efforts[$_] } }) })
    }
  }
  $status = [ordered]@{
    v = 1; at = (Now-Iso); version = $HelperVersion; computer = $env:COMPUTERNAME; busy = $busy
    handled = @($state.done); lines = $lines; actions = $actions
  }
  Write-Utf8 (Join-Path $Folder 'helper-status.json') ($status | ConvertTo-Json -Depth 8)
}

# ---------------------------------------------------------------------------
# Entry points (called by launcher.ps1)
# ---------------------------------------------------------------------------

function Invoke-HelperCycle {
  Update-Path
  $found = Get-Tools
  $state = Load-State
  $probeKey = $HelperVersion + ':' + (($Tools.Keys | Where-Object { $found[$_] }) -join ',')
  if ($state.probed -ne $probeKey) {
    Write-Status $found $state ''
    try { Invoke-Probe $found } catch { Write-Log "probe failed: $_" }
    $state.probed = $probeKey
    Save-State $state
  }
  $reqs = Read-Json (Join-Path $Folder 'requests.json')
  $next = $null
  if ($reqs) {
    foreach ($r in @($reqs.requests)) {
      if ($r -and $r.id -and -not (@($state.done) -contains $r.id)) { $next = $r; break }
    }
  }
  if (-not $next) {
    Write-Status $found $state ''
    return
  }
  Write-Log "request $($next.id) $($next.action)"
  try {
    $result = Invoke-Request $next $found $state
  } catch {
    Write-Log "request $($next.id) failed: $_"
    $result = @{ text = "تعذّر تنفيذ الطلب «$($next.label)»: $_"; kind = 'bad' }
  }
  $script:Progress = ''
  $result.at = (Now-Iso)
  $state.done = @($state.done) + $next.id
  $state.results = @($state.results) + $result
  Save-State $state
  Write-Status $found $state ''
}

function Invoke-HelperInstall {
  try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
  Update-Path
  $found = Get-Tools
  # The tools themselves (Grok Build, Antigravity, Codex) are installed and signed in by
  # the user beforehand; the helper uses whichever it finds, now and later.

  # Start at every sign-in, hidden (a Unicode .vbs: the user's folder name may be Arabic).
  $startup = [Environment]::GetFolderPath('Startup')
  $vbs = Join-Path $startup 'ConverterHelper.vbs'
  $cmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""' + (Join-Path $Local 'launcher.ps1') + '""'
  [IO.File]::WriteAllText($vbs, 'CreateObject("WScript.Shell").Run "' + $cmd + '", 0, False' + "`r`n", [Text.Encoding]::Unicode)
  Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + $vbs + '"')

  $names = @($Tools.Keys | Where-Object { $found[$_] } | ForEach-Object { $Tools[$_].label })
  Write-Host ''
  Write-Host ('تم. المساعد يعمل الآن، ويبدأ وحده كلما فتحت الجهاز. البرامج التي وجدها: ' + $(if ($names.Count) { $names -join '، ' } else { 'لا شيء' }))
  Write-Host 'يمكنك إغلاق هذه النافذة. في صفحة الحالة يظهر «متصل» خلال دقيقتين.'
}
