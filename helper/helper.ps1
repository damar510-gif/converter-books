# The helper's work (docs/helper.md), loaded by launcher.ps1 every minute from the synced
# Drive folder. It reads requests.json (written by the status page's script), runs the
# subscription tools on this computer (Grok Build, Antigravity, Codex, Claude Code), writes the
# results under «نتائج», and reports in helper-status.json, which the status page shows.
# Windows PowerShell 5.1: no ?? or ?: operators.

$HelperVersion = '2026-10-20.9'
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
  # Claude Code on the user's Claude subscription (the same models through Antigravity use
  # the Google AI Pro quota instead). Its model aliases always name the newest of each.
  claude = @{ label = 'Claude Code'; models = @(
      @{ id = 'sonnet'; label = 'Claude Sonnet (اشتراك Claude)' },
      @{ id = 'opus'; label = 'Claude Opus (اشتراك Claude، يستهلك الحدود بسرعة)' },
      @{ id = 'haiku'; label = 'Claude Haiku (اشتراك Claude)' }) }
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
  # Installers that do not always add their folder to Path.
  foreach ($p in @("$env:USERPROFILE\.local\bin\$name.exe", "$env:USERPROFILE\.$name\bin\$name.exe",
      "$env:LOCALAPPDATA\$name\bin\$name.exe")) {
    if (Test-Path -LiteralPath $p) { return $p }
  }
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
    grok  = @(@('--version'), @('models'), @('-p', 'Reply with the single word OK.', '--output-format', 'plain', '--tools', 'read_file', '--no-auto-update'))
    agy   = @(@('--version'), @('models'), @('-p', 'Reply with the single word OK.', '--model', 'gemini-3.8-flash'))
    claude = @(@('--version'), @('--help'), @('-p', 'Reply with the single word OK.', '--model', 'haiku'))
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
      if ($effort) { $a += @('--effort', $effort) }
    }
    'codex' {
      $a = @('exec', $ToolPrompt, '--skip-git-repo-check', '--sandbox', 'read-only', '-C', $dir, '-o', $outFile)
      if ($model) { $a += @('-m', $model) }
      if ($effort) { $a += @('-c', "model_reasoning_effort=$effort") }
      $a += @('-i', 'page.png')
    }
    'claude' {
      $a = @('-p', $ToolPrompt, '--output-format', 'text', '--allowedTools', 'Read',
        '--disallowedTools', 'Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task')
      if ($model) { $a += @('--model', $model) }
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

# Each request runs in its own hidden PowerShell (a worker), so different tools work at
# the same time; one tool runs one request at a time, since its requests share its limits.
$Jobs = Join-Path $Local 'jobs'
$script:JobId = ''

function Set-Progress([string]$text) {
  if ($script:JobId) { Write-Utf8 (Join-Path $Jobs "$($script:JobId).progress") $text }
}

function Model-Label([string]$tool, [string]$model) {
  foreach ($m in $Tools[$tool].models) { if ($m.id -eq $model) { return $m.label } }
  return "$($Tools[$tool].label) $model"
}

# The six hard pages of «تراث الإنسانية» (results branch, compare/pages), read by one
# model; Claude compares the texts with the checked ones.
function Invoke-Test($req, $found) {
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
    Set-Progress "${label}: الصفحة $i من $($pages.Count)"
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

function Invoke-Request($req, $found) {
  switch ([string]$req.action) {
    'test' { return Invoke-Test $req $found }
    default { return @{ text = "طلب لا يعرفه المساعد بعد: $($req.action)"; kind = 'warn' } }
  }
}

# ---------------------------------------------------------------------------
# Status for the page
# ---------------------------------------------------------------------------

function Write-Status($found, $state, $running) {
  $running = @($running)
  $names = @()
  $missing = @()
  foreach ($name in $Tools.Keys) {
    if ($found[$name]) { $names += $Tools[$name].label } else { $missing += $Tools[$name].label }
  }
  $lines = @()
  $toolsText = 'البرامج على الجهاز: ' + $(if ($names.Count) { $names -join '، ' } else { 'لا شيء' })
  if ($missing.Count) { $toolsText += ' (غير موجود: ' + ($missing -join '، ') + ')' }
  $lines += @{ text = $toolsText }
  foreach ($j in $running) {
    $p = Join-Path $Jobs "$($j.id).progress"
    $now = if (Test-Path -LiteralPath $p) { Read-Utf8 $p } else { $j.label }
    $lines += @{ text = 'يعمل الآن: ' + $now }
  }
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
    v = 1; at = (Now-Iso); version = $HelperVersion; computer = $env:COMPUTERNAME
    busy = $(if ($running.Count) { [string]$running[0].id } else { '' })
    handled = @($state.done); lines = $lines; actions = $actions
  }
  Write-Utf8 (Join-Path $Folder 'helper-status.json') ($status | ConvertTo-Json -Depth 8)
}

# ---------------------------------------------------------------------------
# Entry points (called by launcher.ps1)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Comparing the trials (نتائج/مقارنة.md)
# ---------------------------------------------------------------------------

# There is no checked text of the six pages, so each reading is compared with all the
# others: one that agrees with the rest is probably right, an outlier probably wrong.
# Words are compared as crosscheck.py does (no diacritics, one form of alef, ya, ta
# marbuta, hamza seats, Arabic digits), with the ratio 2 x matching words / all words.

function Norm-Words([string]$text) {
  $out = New-Object 'Collections.Generic.List[string]'
  foreach ($raw in ($text -split '\s+')) {
    $w = $raw -replace '[ً-ْٰـ]', ''
    for ($d = 0; $d -le 9; $d++) {
      $w = $w.Replace([string][char](0x660 + $d), [string]$d).Replace([string][char](0x6F0 + $d), [string]$d)
    }
    $w = $w -replace '[إأآٱ]', [string][char]0x627
    $w = $w.Replace([char]0x649, [char]0x64A).Replace([char]0x629, [char]0x647).Replace([char]0x624, [char]0x648).Replace([char]0x626, [char]0x64A)
    $w = $w -replace '[^ء-ي0-9A-Za-z]', ''
    if ($w) { $out.Add($w) }
  }
  return ,$out.ToArray()
}

function Match-Ratio([string[]]$a, [string[]]$b) {
  if (-not ('HelperLcs' -as [type])) {
    Add-Type -TypeDefinition @'
public static class HelperLcs {
  public static int Length(string[] a, string[] b) {
    int[] prev = new int[b.Length + 1], cur = new int[b.Length + 1];
    for (int i = 1; i <= a.Length; i++) {
      for (int j = 1; j <= b.Length; j++)
        cur[j] = a[i - 1] == b[j - 1] ? prev[j - 1] + 1 : System.Math.Max(prev[j], cur[j - 1]);
      int[] t = prev; prev = cur; cur = t;
    }
    return prev[b.Length];
  }
}
'@
  }
  if ($a.Length + $b.Length -eq 0) { return 1.0 }
  return 2.0 * [HelperLcs]::Length($a, $b) / ($a.Length + $b.Length)
}

# The latest trial of each label, and the converter's earlier reading (pNNN.lite.md in the
# test pages folder): [{label, minutes, pages: {stem: text}}].
function Get-Trials {
  $out = @()
  $lite = @{}
  foreach ($f in @(Get-ChildItem -LiteralPath (Join-Path $Folder $TestPagesName) -Filter '*.lite.md' -ErrorAction SilentlyContinue)) {
    $lite[($f.Name -replace '\.lite\.md$', '')] = Read-Utf8 $f.FullName
  }
  if ($lite.Count) { $out += @{ label = 'Gemini 3.5 Flash-Lite (القارئ الحالي، تجربة سابقة)'; minutes = ''; pages = $lite } }
  $seen = @{}
  $dirs = @(Get-ChildItem -LiteralPath (Join-Path $Folder $ResultsName) -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like '* تجربة *' } | Sort-Object Name -Descending)
  foreach ($d in $dirs) {
    $label = ($d.Name -replace '^.*? تجربة ', '')
    if ($seen[$label]) { continue }
    $meta = Read-Json (Join-Path $d.FullName 'meta.json')
    if (-not $meta) { continue }
    $seen[$label] = $true
    $pages = @{}
    foreach ($f in @(Get-ChildItem -LiteralPath $d.FullName -Filter 'p*.md')) {
      $pages[[IO.Path]::GetFileNameWithoutExtension($f.Name)] = Read-Utf8 $f.FullName
    }
    $out += @{ label = $label; minutes = [string]$meta.minutes; pages = $pages }
  }
  return $out
}

function Update-Comparison {
  $file = Join-Path (Join-Path $Folder $ResultsName) 'مقارنة.md'
  $metas = @(Get-ChildItem -LiteralPath (Join-Path $Folder $ResultsName) -Recurse -Filter 'meta.json' -ErrorAction SilentlyContinue)
  if (-not $metas.Count) { return }
  $newest = ($metas | Measure-Object -Property LastWriteTimeUtc -Maximum).Maximum
  if ((Test-Path -LiteralPath $file) -and (Get-Item -LiteralPath $file).LastWriteTimeUtc -gt $newest) { return }
  $trials = @(Get-Trials)
  if ($trials.Count -lt 2) { return }
  $stems = @($trials | ForEach-Object { $_.pages.Keys } | Sort-Object -Unique)
  $words = @{}
  for ($t = 0; $t -lt $trials.Count; $t++) {
    foreach ($s in $stems) {
      $text = [string]$trials[$t].pages[$s]
      # A refusal or an empty answer is not a reading of the page.
      $words["$t|$s"] = if ($text.Length -ge 500) { Norm-Words $text } else { $null }
    }
  }
  $sb = New-Object Text.StringBuilder
  [void]$sb.AppendLine("# مقارنة التجارب ($(Now-Iso))")
  [void]$sb.AppendLine('')
  [void]$sb.AppendLine('لا يوجد نص مدقق لهذه الصفحات، فكل قراءة تُقارن بالقراءات الأخرى: الرقم متوسط تطابقها معها (100 = تطابق تام). «—» = رفض أو لم يقرأ.')
  [void]$sb.AppendLine('')
  [void]$sb.AppendLine('| القراءة | المتوسط | ' + ($stems -join ' | ') + ' | الدقائق |')
  [void]$sb.AppendLine('|---|---|' + ('---|' * $stems.Count) + '---|')
  $pair = @{}
  for ($t = 0; $t -lt $trials.Count; $t++) {
    $cells = @()
    $all = @()
    foreach ($s in $stems) {
      $mine = $words["$t|$s"]
      $r = @()
      if ($mine) {
        for ($o = 0; $o -lt $trials.Count; $o++) {
          if ($o -eq $t -or -not $words["$o|$s"]) { continue }
          $v = Match-Ratio $mine $words["$o|$s"]
          $r += $v
          $pair["$t|$o"] = @($pair["$t|$o"]) + $v
        }
      }
      if ($r.Count) {
        $m = ($r | Measure-Object -Average).Average
        $all += $m
        $cells += ('{0:N1}' -f (100 * $m))
      } else { $cells += '—' }
    }
    $avg = if ($all.Count) { '{0:N1}' -f (100 * ($all | Measure-Object -Average).Average) } else { '—' }
    [void]$sb.AppendLine("| $($trials[$t].label) | $avg | " + ($cells -join ' | ') + " | $($trials[$t].minutes) |")
  }
  [void]$sb.AppendLine('')
  [void]$sb.AppendLine('## كل اثنين معاً (متوسط الصفحات التي قرأها الاثنان)')
  [void]$sb.AppendLine('')
  for ($t = 0; $t -lt $trials.Count; $t++) {
    for ($o = $t + 1; $o -lt $trials.Count; $o++) {
      $v = @($pair["$t|$o"] | Where-Object { $_ -ne $null })
      if ($v.Count) {
        [void]$sb.AppendLine("- $($trials[$t].label) ↔ $($trials[$o].label): " + ('{0:N1}' -f (100 * ($v | Measure-Object -Average).Average)))
      }
    }
  }
  Write-Utf8 $file $sb.ToString()
  Write-Log 'comparison written'
}

function Request-Tool($req) {
  if ([string]$req.action -eq 'test') { return ((([string]$req.choices.model) -split '\|', 2)[0]) }
  return ''
}

# The requests whose workers are still running; finished or vanished ones are filed.
function Update-Jobs($state) {
  $running = @()
  if (-not (Test-Path -LiteralPath $Jobs)) { return $running }
  foreach ($f in @(Get-ChildItem -LiteralPath $Jobs -Filter '*.running.json')) {
    $j = Read-Json $f.FullName
    $id = $f.Name -replace '\.running\.json$', ''
    $resultFile = Join-Path $Jobs "$id.result.json"
    $result = $null
    if (Test-Path -LiteralPath $resultFile) {
      $result = Read-Json $resultFile
      if (-not $result) { $result = @{ text = "انتهى الطلب «$($j.label)» بلا نتيجة مقروءة."; kind = 'bad' } }
    } elseif (-not ($j -and (Get-Process -Id ([int]$j.pid) -ErrorAction SilentlyContinue))) {
      $result = @{ text = "توقف الطلب «$($j.label)» قبل أن ينتهي (أُغلق الجهاز؟). اطلبه مرة أخرى."; kind = 'bad' }
    }
    if ($result) {
      Write-Log "request $id finished"
      $state.done = @($state.done) + $id
      $state.results = @($state.results) + $result
      foreach ($x in 'running.json', 'result.json', 'req.json', 'progress') {
        Remove-Item -LiteralPath (Join-Path $Jobs "$id.$x") -Force -ErrorAction SilentlyContinue
      }
    } else {
      $running += @{ id = $id; tool = [string]$j.tool; label = [string]$j.label }
    }
  }
  return $running
}

function Start-Worker($req) {
  New-Item -ItemType Directory -Force -Path $Jobs | Out-Null
  $id = [string]$req.id
  Write-Utf8 (Join-Path $Jobs "$id.req.json") ($req | ConvertTo-Json -Depth 6)
  # The worker loads the same copy of this file that the launcher saved (core.ps1).
  $q = { param($t) "'" + ($t -replace "'", "''") + "'" }
  $worker = Join-Path $Local 'worker.ps1'
  $text = "param([string]`$Id)`r`n`$ErrorActionPreference = 'Stop'`r`n" +
    "`$Local = $(& $q $Local)`r`n`$Folder = $(& $q $Folder)`r`n" +
    "function Write-Log([string]`$t) { try { Add-Content -Path (Join-Path `$Local 'helper.log') -Value ((Get-Date).ToString('yyyy-MM-dd HH:mm:ss') + ' [' + `$Id + '] ' + `$t) -Encoding UTF8 } catch { } }`r`n" +
    "function Read-Utf8([string]`$path) { return [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes(`$path)).TrimStart([char]0xFEFF) }`r`n" +
    ". ([scriptblock]::Create((Read-Utf8 (Join-Path `$Local 'core.ps1'))))`r`n" +
    "Invoke-HelperWorker `$Id`r`n"
  [IO.File]::WriteAllText($worker, $text, (New-Object Text.UTF8Encoding $true))
  $p = Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -PassThru -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', ('"' + $worker + '"'), '-Id', $id)
  $label = if ($req.label) { [string]$req.label } else { [string]$req.action }
  Write-Utf8 (Join-Path $Jobs "$id.running.json") (@{ pid = $p.Id; tool = (Request-Tool $req); label = $label; at = (Now-Iso) } | ConvertTo-Json)
  Write-Log "request $id started ($($req.action), pid $($p.Id))"
}

# One request, in its own process (worker.ps1).
function Invoke-HelperWorker([string]$id) {
  $script:JobId = $id
  $resultFile = Join-Path $Jobs "$id.result.json"
  try {
    Update-Path
    $req = Read-Json (Join-Path $Jobs "$id.req.json")
    $result = Invoke-Request $req (Get-Tools)
  } catch {
    Write-Log "failed: $_"
    $result = @{ text = "تعذّر تنفيذ الطلب «$($req.label)»: $_"; kind = 'bad' }
  }
  $result.at = (Now-Iso)
  Write-Utf8 $resultFile ($result | ConvertTo-Json -Depth 5)
}

function Invoke-HelperCycle {
  Update-Path
  $found = Get-Tools
  $state = Load-State
  $probeKey = $HelperVersion + ':' + (($Tools.Keys | Where-Object { $found[$_] }) -join ',')
  if ($state.probed -ne $probeKey) {
    Write-Status $found $state @()
    try { Invoke-Probe $found } catch { Write-Log "probe failed: $_" }
    $state.probed = $probeKey
  }
  $running = @(Update-Jobs $state)
  $busyTools = @($running | ForEach-Object { $_.tool })
  $reqs = Read-Json (Join-Path $Folder 'requests.json')
  if ($reqs) {
    foreach ($r in @($reqs.requests)) {
      if (-not ($r -and $r.id) -or (@($state.done) -contains $r.id) -or (@($running | ForEach-Object { $_.id }) -contains $r.id)) { continue }
      $tool = Request-Tool $r
      if ($tool -and ($busyTools -contains $tool)) { continue }
      try {
        Start-Worker $r
        $running += @{ id = [string]$r.id; tool = $tool; label = [string]$r.label }
        $busyTools += $tool
      } catch {
        Write-Log "request $($r.id) could not start: $_"
      }
    }
  }
  if (-not $running.Count) {
    try { Update-Comparison } catch { Write-Log "comparison failed: $_" }
  }
  Save-State $state
  Write-Status $found $state $running
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
