# The helper's work (docs/helper.md), loaded by launcher.ps1 every minute from the synced
# Drive folder. It reads requests.json (written by the status page's script), runs the
# subscription tools on this computer (Grok Build, Antigravity, Codex, Claude Code), writes the
# results under «نتائج», and reports in helper-status.json, which the status page shows.
# The models the user puts in the boxes on the page transcribe books and judge unsure
# places through the folders «كتب», «مراجعة» and «أحكام» (docs/helper-boxes-plan.md).
# Windows PowerShell 5.1: no ?? or ?: operators.

$HelperVersion = '2026-10-21.5'
$BooksName = 'كتب'
$ReviewsName = 'مراجعة'
$VerdictsName = 'أحكام'
$PageTimeoutSec = 20 * 60
# Readings are quick; settling and judging need a closer look.
$ReadEffort = 'low'
$SettleEffort = 'medium'
$JudgeEffort = 'medium'
# A claimed job whose worker died is free again after this long (more than a page's time).
$ClaimMinutes = 30
# A box worker hands over to a fresh copy of the program after this long.
$WorkerMinutes = 30

# Models per tool, as the tool's own command line names them (`grok models`, `agy models`
# in probe.txt, 2026-10-21). All are offered and the
# user chooses; "warn" carries the user's warnings (Gemini 4 Argon is not for
# transcribing: it uses up the limits; Grok 4.7 Fast costs twice as much).
$Tools = [ordered]@{
  grok  = @{ label = 'Grok Build'; models = @(
      @{ id = 'grok-4.7'; label = 'Grok 4.7' },
      @{ id = 'grok-4.7-build-fast'; label = 'Grok 4.7 Build Fast'; warn = 'بضعف السعر' },
      @{ id = 'grok-4.6'; label = 'Grok 4.6' },
      @{ id = 'grok-4.5'; label = 'Grok 4.5' }) }
  agy   = @{ label = 'Antigravity'; models = @(
      @{ id = 'gemini-4-argon'; label = 'Gemini 4 Argon'; warn = 'لا للتفريغ: يستنفد الحدود' },
      @{ id = 'gemini-3.8-flash'; label = 'Gemini 3.8 Flash' },
      @{ id = 'claude-sonnet-5-5'; label = 'Claude Sonnet 5.5' },
      @{ id = 'claude-opus-5-5'; label = 'Claude Opus 5.5' },
      @{ id = 'gpt-oss-120b'; label = 'GPT-OSS 120B' }) }
  # Codex's /model list on the user's free ChatGPT account (2026-10-20); six test pages
  # used 35% of its monthly limit.
  codex = @{ label = 'Codex'; models = @(
      @{ id = 'gpt-6.1-sol'; label = 'GPT-6.1 Sol'; warn = 'حساب مجاني: حدّه صغير' },
      @{ id = 'gpt-6-astra'; label = 'GPT-6 Astra'; warn = 'حساب مجاني: حدّه صغير' },
      @{ id = 'gpt-6-sol'; label = 'GPT-6 Sol'; warn = 'حساب مجاني: حدّه صغير' },
      @{ id = 'gpt-6-luna'; label = 'GPT-6 Luna'; warn = 'حساب مجاني: حدّه صغير' },
      @{ id = 'gpt-5.6-sol'; label = 'GPT-5.6 Sol'; warn = 'حساب مجاني: حدّه صغير' },
      @{ id = 'gpt-5.6-terra'; label = 'GPT-5.6 Terra'; warn = 'حساب مجاني: حدّه صغير' },
      @{ id = 'gpt-5.6-luna'; label = 'GPT-5.6 Luna'; warn = 'حساب مجاني: حدّه صغير' }) }
  # Claude Code on the user's Claude subscription (the same models through Antigravity use
  # the Google AI Pro quota instead). Its model aliases always name the newest of each.
  claude = @{ label = 'Claude Code'; models = @(
      @{ id = 'sonnet'; label = 'Claude Sonnet'; warn = 'رفض صفحة في التجربة' },
      @{ id = 'opus'; label = 'Claude Opus'; warn = 'يستهلك الحدود بسرعة' },
      @{ id = 'haiku'; label = 'Claude Haiku' }) }
}

# The prompt each tool gets on its command line: plain ASCII, because cmd.exe shims would
# garble Arabic. The Arabic instructions are in task.md next to the page image.
$ToolPrompts = @{
  read = 'Read the file task.md in the current folder and follow it exactly. Your reply must be only the transcribed text of the page image, nothing else.'
  settle = 'Read the file task.md in the current folder and follow it exactly. Your reply must be only the final text of the page, nothing else.'
  judge = 'Read the file task.md in the current folder and follow it exactly. Your reply must be only the JSON array that task.md asks for, nothing else.'
}

# The converter's reading rules (converter.py, OCR_RULES; <<SPREAD>> is its {spread}).
$OcrRules = @'
- إن كانت الصفحة بعمودين فاقرأ العمود الأيمن كاملاً من أعلاه إلى أسفله، ثم العمود الأيسر.
<<SPREAD>>- عنوان مقال أو بحث جديد (العنوان الكبير في رأس المقال) يُكتب في سطر مستقل يبدأ بـ "# ".
- العناوين الفرعية داخل المقال تُكتب في سطر مستقل يبدأ بـ "## ".
- افصل بين الفقرات بسطر فارغ، واجمع أسطر الفقرة الواحدة في سطر واحد.
- الشعر: كل بيت في سطر مستقل يبدأ بـ "| "، وبين الشطرين " … ".
- الحواشي (السطور الصغيرة أسفل الصفحة تحت الخط): في موضع رقم الحاشية داخل المتن اكتب [^1] (بنفس رقمها)، وفي آخر إخراجك اكتب كل حاشية في سطر مستقل هكذا: [^1]: نص الحاشية
- الكلمات الأجنبية تُنسخ بحروفها كما هي.
- تجاهل تماماً: أرقام الصفحات، والترويسة المكررة أعلى الصفحة، والعلامة المائية (مثل «جلجامش»)، والزخارف.
- لا تكتب أي تعليق أو شرح أو مقدمة، فقط نص الصفحة.
- إن كانت الصفحة فارغة أو صورة بلا نص فاكتب فقط: [[EMPTY]]
'@
$SpreadHint = "- هذه الصورة فيها صفحتان من الكتاب متجاورتان: انسخ الصفحة اليمنى كاملة أولاً ثم اليسرى.`n"

function Ocr-Rules([bool]$spread) {
  $hint = if ($spread) { $SpreadHint } else { '' }
  return $OcrRules.Replace("<<SPREAD>>", $hint).Replace("`r`n", "`n").Trim()
}

# The converter's reading prompt (converter.py, OCR_PROMPT), and how to look at the page.
function Get-ReadTask([string]$image, [bool]$spread) {
  return "أنت تنسخ صفحة واحدة من كتاب عربي ممسوح ضوئياً. انسخ النص كما هو حرفياً بلا تلخيص ولا حذف ولا تصحيح ولا إضافة.`n`n" +
    "قواعد الإخراج (Markdown):`n" + (Ocr-Rules $spread) + "`n" +
    "- صورة الصفحة هي الملف $image في هذا المجلد. انظر إليها بنفسك؛ لا تشغّل أي برنامج قراءة ضوئية ولا تكتب أي كود.`n" +
    "- ردّك هو نص الصفحة فقط.`n"
}

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

# One page with one tool: {text, code, seconds, timedOut, err}. The folder holds the page
# image and task.md; kind picks the short command-line prompt (read, settle or judge).
function Invoke-Page([string]$tool, [string]$exe, [string]$model, [string]$effort, [string]$dir,
    [string]$image = 'page.png', [string]$kind = 'read') {
  $outFile = Join-Path $dir 'out.md'
  $prompt = $ToolPrompts[$kind]
  switch ($tool) {
    # Read-only everywhere: each tool may look at the page folder and nothing else; no
    # shell, no edits, no web, and never a flag that skips the tool's permission checks.
    'grok' {
      $a = @('-p', $prompt, '--cwd', $dir, '--output-format', 'plain', '--tools', 'read_file,list_dir',
        '--no-auto-update', '--disable-web-search', '--no-subagents')
      if ($model) { $a += @('-m', $model) }
      if ($effort) { $a += @('--effort', $effort) }
    }
    'agy' {
      $a = @('-p', $prompt)
      if ($model) { $a += @('--model', $model) }
      if ($effort) { $a += @('--effort', $effort) }
    }
    'codex' {
      $a = @('exec', $prompt, '--skip-git-repo-check', '--sandbox', 'read-only', '-C', $dir, '-o', $outFile)
      if ($model) { $a += @('-m', $model) }
      if ($effort) { $a += @('-c', "model_reasoning_effort=$effort") }
      $a += @('-i', $image)
    }
    'claude' {
      $a = @('-p', $prompt, '--output-format', 'text', '--allowedTools', 'Read',
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

# One tool at a time across all the helper's processes: its requests share its limits.
function Use-Tool([string]$tool, [scriptblock]$block) {
  $m = New-Object Threading.Mutex($false, "Local\ConverterHelperTool-$tool")
  $held = $false
  try {
    try { $held = $m.WaitOne() } catch [Threading.AbandonedMutexException] { $held = $true }
    return (& $block)
  } finally {
    if ($held) { $m.ReleaseMutex() }
    $m.Dispose()
  }
}

function Invoke-Request($req, $found) {
  switch ([string]$req.action) {
    default { return @{ text = "طلب لا يعرفه المساعد بعد: $($req.action)"; kind = 'warn' } }
  }
}

# ---------------------------------------------------------------------------
# The boxes: models the user put to work from the status page
# (docs/helper-boxes-plan.md). One worker process per tool takes the jobs one by one.
# ---------------------------------------------------------------------------

$ChipPattern = '^(grok|agy|codex|claude)\|[A-Za-z0-9._-]{1,60}$'
# What a tool says when its plan's limit is used up.
$QuotaPattern = '(?i)(rate.?limit|quota|usage limit|limit (reached|exceeded|resets)|exceeded your|too many requests|\b429\b|resource.?exhausted|out of credits|credit balance|upgrade (your|to)|weekly limit|daily limit|hit your limit)'
# A model that would not read the page (Claude refused one in the trial: "copyright").
$RefusalPattern = '(?i)(copyright|can.?t (help|assist|transcribe|reproduce|provide)|cannot (help|assist|transcribe|reproduce|provide)|unable to (help|assist|transcribe)|i.?m sorry|حقوق|لا يمكنني|لا أستطيع|عذراً|عذرا)'
$Claims = Join-Path $Local 'claims'

function Get-Boxes($reqs) {
  $out = @{ transcribe = @(); review = @(); times = @{}; reviews = $null }
  # The books open for review, as the script lists them ($null from an older script: every folder).
  if ($reqs -and $null -ne $reqs.reviews) { $out.reviews = @(@($reqs.reviews) | Where-Object { $_ } | ForEach-Object { [string]$_ }) }
  if ($reqs -and $reqs.boxes) {
    foreach ($k in 'transcribe', 'review') {
      $out[$k] = @(@($reqs.boxes.$k) | Where-Object { $_ -and ([string]$_) -match $ChipPattern } | ForEach-Object { [string]$_ })
    }
    # When each model may work, set on the page: {start, stop} ('' = now / never).
    if ($reqs.boxes.times) {
      foreach ($p in $reqs.boxes.times.PSObject.Properties) {
        $out.times[$p.Name] = @{ start = (Iso-Of $p.Value.start); stop = (Iso-Of $p.Value.stop) }
      }
    }
  }
  return $out
}

# '' when the model may work now, else why not: 'later' (before its start) or 'over' (after its stop).
function Chip-Window($boxes, [string]$chip) {
  $t = $null
  if ($boxes -and $boxes.times) { $t = $boxes.times[$chip] }
  if (-not $t) { return '' }
  $now = (Get-Date).ToUniversalTime()
  try { if ($t.start -and ([DateTime]::Parse($t.start)).ToUniversalTime() -gt $now) { return 'later' } } catch { }
  try { if ($t.stop -and ([DateTime]::Parse($t.stop)).ToUniversalTime() -le $now) { return 'over' } } catch { }
  return ''
}

function Chip-Tool([string]$chip) { return ($chip -split '\|', 2)[0] }
function Chip-Model([string]$chip) { return ($chip -split '\|', 2)[1] }
function Chip-Slug([string]$chip) { return ($chip -replace '[^A-Za-z0-9.-]', '_') }

function Page-Name([int]$page, [string]$suffix) { return ('p{0:D4}.{1}' -f $page, $suffix) }

function Text-Sha([string]$text) {
  $sha = [Security.Cryptography.SHA1]::Create()
  try { $bytes = $sha.ComputeHash($Utf8.GetBytes($text)) } finally { $sha.Dispose() }
  return (-join ($bytes | ForEach-Object { $_.ToString('x2') })).Substring(0, 12)
}

# Times read back from JSON (PowerShell 7 turns ISO strings into dates; 5.1 does not).
function Iso-Of($v) {
  if ($v -is [DateTime]) { return $v.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
  return [string]$v
}

function Local-Time([string]$iso) {
  try { return ([DateTime]::Parse($iso)).ToLocalTime().ToString('HH:mm') } catch { return '' }
}

# --- Chip states: one file per tool, written by its worker ---

function Load-Chips([string]$tool) {
  $o = Read-Json (Join-Path $Local "chips-$tool.json")
  $h = @{}
  if ($o) {
    foreach ($p in $o.PSObject.Properties) {
      $h[$p.Name] = @{ state = [string]$p.Value.state; text = [string]$p.Value.text; until = [string]$p.Value.until
        fails = [int]$p.Value.fails; outs = [int]$p.Value.outs }
    }
  }
  return $h
}

function All-Chips {
  $h = @{}
  foreach ($t in $Tools.Keys) { $c = Load-Chips $t; foreach ($k in $c.Keys) { $h[$k] = $c[$k] } }
  return $h
}

# Changes one chip's state: @{state; text; until; fails; outs} (fields left out stay).
function Set-Chip([string]$chip, $changes) {
  $tool = Chip-Tool $chip
  $h = Load-Chips $tool
  $c = $h[$chip]
  if (-not $c) { $c = @{ state = 'idle'; text = ''; until = ''; fails = 0; outs = 0 } }
  foreach ($k in $changes.Keys) { $c[$k] = $changes[$k] }
  $h[$chip] = $c
  Write-Utf8 (Join-Path $Local "chips-$tool.json") ($h | ConvertTo-Json -Depth 4)
  return $c
}

# Not resting after a used-up limit or repeated failures.
function Chip-Ready($c) {
  if (-not $c -or -not $c.until) { return $true }
  try { return ([DateTime]::Parse($c.until)).ToUniversalTime() -le (Get-Date).ToUniversalTime() } catch { return $true }
}

function Ready-Chips($list, $found, $states, $boxes) {
  return @(@($list) | Where-Object { $_ -and $found[(Chip-Tool $_)] -and (Chip-Ready $states[$_]) -and -not (Chip-Window $boxes $_) })
}

# --- Claims: one job, one worker (local files: all workers are on this computer) ---

function Claim-Stale([string]$path) {
  try {
    $owner = [int](([IO.File]::ReadAllText($path)) -split ' ')[0]
    if (((Get-Date) - (Get-Item -LiteralPath $path).LastWriteTime).TotalMinutes -gt $ClaimMinutes) { return $true }
    return -not (Get-Process -Id $owner -ErrorAction SilentlyContinue)
  } catch { return $true }
}

function Claim-Busy([string]$key) {
  $path = Join-Path $Claims "$key.claim"
  return (Test-Path -LiteralPath $path) -and -not (Claim-Stale $path)
}

function Claim-Job([string]$key) {
  New-Item -ItemType Directory -Force -Path $Claims | Out-Null
  $path = Join-Path $Claims "$key.claim"
  if (Test-Path -LiteralPath $path) {
    if (-not (Claim-Stale $path)) { return $false }
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
  }
  try {
    $fs = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
    try { $b = $Utf8.GetBytes("$PID $(Now-Iso)"); $fs.Write($b, 0, $b.Length) } finally { $fs.Close() }
    return $true
  } catch { return $false }
}

function Drop-Claim([string]$key) {
  Remove-Item -LiteralPath (Join-Path $Claims "$key.claim") -Force -ErrorAction SilentlyContinue
}

function Clear-StaleClaims {
  if (-not (Test-Path -LiteralPath $Claims)) { return }
  foreach ($f in @(Get-ChildItem -LiteralPath $Claims -Filter '*.claim')) {
    if (Claim-Stale $f.FullName) { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue }
  }
}

# --- Pages a model would not read (refused, or failed twice): another model gets them ---

function Get-Refused([string]$book) {
  $h = @{}
  $dir = Join-Path (Join-Path $Local 'refused') $book
  if (-not (Test-Path -LiteralPath $dir)) { return $h }
  foreach ($f in @(Get-ChildItem -LiteralPath $dir -Filter '*.txt')) {
    foreach ($line in ((Read-Utf8 $f.FullName) -split "`n")) {
      $parts = $line.Trim() -split ' ', 2
      if ($parts.Count -eq 2 -and $parts[0] -match '^\d+$') {
        $p = [int]$parts[0]
        if (-not $h.ContainsKey($p)) { $h[$p] = @() }
        $h[$p] += $parts[1]
      }
    }
  }
  return $h
}

function Add-Refused([string]$book, [int]$page, [string]$chip) {
  $dir = Join-Path (Join-Path $Local 'refused') $book
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  [IO.File]::AppendAllText((Join-Path $dir "$(Chip-Slug $chip).txt"), "$page $chip`n", $Utf8)
}

# --- Words, as crosscheck.py compares them ---

function Ensure-Diff {
  if ('HelperDiff' -as [type]) { return }
  Add-Type -TypeDefinition @'
using System.Collections.Generic;
public static class HelperDiff {
  // The places where two word lists differ, as [i1, i2, j1, j2] (an LCS alignment).
  public static List<int[]> Spans(string[] a, string[] b) {
    int n = a.Length, m = b.Length;
    int[,] L = new int[n + 1, m + 1];
    for (int i = n - 1; i >= 0; i--)
      for (int j = m - 1; j >= 0; j--)
        L[i, j] = a[i] == b[j] ? L[i + 1, j + 1] + 1 : System.Math.Max(L[i + 1, j], L[i, j + 1]);
    var spans = new List<int[]>();
    int x = 0, y = 0, sx = -1, sy = -1;
    while (x < n || y < m) {
      if (x < n && y < m && a[x] == b[y]) {
        if (sx >= 0) { spans.Add(new int[] { sx, x, sy, y }); sx = -1; }
        x++; y++;
      } else {
        if (sx < 0) { sx = x; sy = y; }
        if (y >= m || (x < n && L[x + 1, y] >= L[x, y + 1])) x++; else y++;
      }
    }
    if (sx >= 0) spans.Add(new int[] { sx, n, sy, m });
    return spans;
  }
  public static int Common(string[] a, string[] b) {
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

# Each word with letters or digits: {raw: [as written], norm: [as compared]}.
function Get-Words([string]$text) {
  $raw = New-Object 'Collections.Generic.List[string]'
  $norm = New-Object 'Collections.Generic.List[string]'
  foreach ($w in ($text -split '\s+')) {
    if (-not $w) { continue }
    $n = Norm-Words $w
    if ($n.Length) { $raw.Add($w); $norm.Add($n[0]) }
  }
  return @{ raw = $raw.ToArray(); norm = $norm.ToArray() }
}

# The places where the readings (a) and (b) differ: [{a, b}] (their words as written).
function Get-DiffSpans([string]$a, [string]$b) {
  Ensure-Diff
  $wa = Get-Words $a
  $wb = Get-Words $b
  $out = @()
  foreach ($s in [HelperDiff]::Spans([string[]]$wa.norm, [string[]]$wb.norm)) {
    $out += @{ a = ($wa.raw[$s[0]..($s[1] - 1)] -join ' '); b = ($wb.raw[$s[2]..($s[3] - 1)] -join ' ') }
    if ($s[1] -le $s[0]) { $out[-1].a = '' }
    if ($s[3] -le $s[2]) { $out[-1].b = '' }
  }
  return ,$out
}

# --- Transcription: books in «كتب», with book.json from the conversion run ---

function Get-TranscribeBooks {
  $root = Join-Path $Folder $BooksName
  $out = @()
  if (-not (Test-Path -LiteralPath $root)) { return }
  foreach ($d in @(Get-ChildItem -LiteralPath $root -Directory | Sort-Object CreationTimeUtc)) {
    $book = Read-Json (Join-Path $d.FullName 'book.json')
    if (-not $book) { continue }
    $need = @(@($book.need) | Where-Object { $null -ne $_ } | ForEach-Object { [int]$_ })
    if (-not $need.Count) { continue }
    $names = @{}
    foreach ($f in @(Get-ChildItem -LiteralPath $d.FullName -File)) { $names[$f.Name] = $true }
    $out += @{ id = $d.Name; dir = $d.FullName; title = [string]$book.title; at = (Iso-Of $book.at); need = $need
      needA = @(@($book.need_a) | Where-Object { $null -ne $_ } | ForEach-Object { [int]$_ }); names = $names }
  }
  return $out
}

# One page's files: a (the free model's reading), ha, hb, final, skip, img; first = the
# reading the settled text is made on; settled = a settled text made on these readings.
function Get-PageFiles($book, [int]$page) {
  $s = @{ page = $page }
  foreach ($k in 'a', 'ha', 'hb', 'final', 'skip') {
    $name = Page-Name $page "$k.json"
    $s[$k] = $null
    if ($book.names[$name]) { $s[$k] = Read-Json (Join-Path $book.dir $name) }
  }
  $s.img = $null
  if ($book.names[(Page-Name $page 'jpg')]) { $s.img = Join-Path $book.dir (Page-Name $page 'jpg') }
  $s.first = $null
  if ($s.a -and $s.a.text) { $s.first = $s.a } elseif ($s.ha -and $s.ha.text) { $s.first = $s.ha }
  $s.settled = [bool]($s.first -and $s.hb -and $s.final -and [string]$s.final.a_sha -eq (Text-Sha ([string]$s.first.text)) -and
    [string]$s.final.b_sha -eq (Text-Sha ([string]$s.hb.text)))
  return $s
}

# The next transcription job for this model: settle a page, then give second readings,
# then first readings (only when the free model ran out and another model can give the
# second). The two readings of a page always come from two different models.
function Find-TranscribeJob([string]$chip, $books, [int]$readyCount) {
  foreach ($kind in 'settle', 'readB', 'readA') {
    if ($kind -eq 'readA' -and $readyCount -lt 2) { continue }
    foreach ($book in $books) {
      $refused = Get-Refused $book.id
      foreach ($p in $book.need) {
        if ($book.names[(Page-Name $p 'skip.json')] -or (@($refused[$p]) -contains $chip)) { continue }
        if ($kind -eq 'readA' -and $book.needA -notcontains $p) { continue }
        $key = "$($book.id)-p$p-$kind"
        if (Claim-Busy $key) { continue }
        $s = Get-PageFiles $book $p
        if (-not $s.img) { continue }
        $ok = $false
        if ($kind -eq 'settle') { $ok = $s.first -and $s.hb -and -not $s.settled }
        elseif ($kind -eq 'readB') { $ok = (-not $s.hb) -and -not ($s.first -and [string]$s.first.model -eq $chip) }
        else { $ok = (-not $s.a) -and (-not $s.ha) -and -not ($s.hb -and [string]$s.hb.model -eq $chip) }
        if ($ok -and (Claim-Job $key)) { return @{ kind = $kind; book = $book; page = $p; files = $s; chip = $chip; key = $key } }
      }
    }
  }
  return $null
}

function Is-Spread($s) {
  if ($s.a -and $null -ne $s.a.spread) { return [bool]$s.a.spread }
  try {
    Add-Type -AssemblyName System.Drawing -ErrorAction Stop
    $img = [System.Drawing.Image]::FromFile($s.img)
    try { return $img.Width -gt $img.Height * 1.15 } finally { $img.Dispose() }
  } catch { return $false }
}

# The converter's settling request (crosscheck.reconcile_prompt), for a model that looks at
# the image file itself.
function Get-SettleTask([string]$a, [string]$b, $spans, $missing, [bool]$spread) {
  $short = { param($t) if ($t.Length -gt 80) { $t.Substring(0, 80) } elseif ($t) { $t } else { '—' } }
  $listed = @($spans | Select-Object -First 40 | ForEach-Object { '- (أ): ' + (& $short $_.a) + ' | (ب): ' + (& $short $_.b) })
  if (@($spans).Count -gt 40) { $listed += "- … و$(@($spans).Count - 40) موضعاً آخر" }
  if (-not $listed.Count) { $listed = @('- لا توجد') }
  $hint = ''
  $lines = @(@($missing) | Where-Object { $_ } | Select-Object -First 5)
  if ($lines.Count) {
    $hint = "`nأسطر يبدو أنها سقطت من النسختين أو من إحداهما (من قارئ ثالث فيه أخطاء حروف، فاعتمد على الصورة):`n" +
      (($lines | ForEach-Object { "- $_" }) -join "`n") + "`n"
  }
  return "أمامك صورة صفحة من كتاب عربي ممسوح ضوئياً (الملف page.jpg في هذا المجلد)، ونسختان لنصها كتبهما قارئان آليان مختلفان: (أ) و(ب).`n" +
    "المطلوب نسخة نهائية واحدة مطابقة للصورة حرفاً بحرف.`n" +
    "- في كل موضع تختلف فيه النسختان انظر في الصورة واكتب المطبوع فعلاً، وقد يكون الصواب غير الاثنين.`n" +
    "- إن كان في الصورة سطر أو كلمات سقطت من النسختين فأضفها في مكانها.`n" +
    "- لا تحذف شيئاً مطبوعاً ولا تضف ما ليس في الصورة، ولا تصحح أخطاء المؤلف أو الطبعة.`n" +
    "- اكتب الحروف كما هي مطبوعة، ومنها «ى» و«ي» في آخر الكلمة.`n" +
    "- انظر إلى الصورة بنفسك؛ لا تشغّل أي برنامج قراءة ضوئية ولا تكتب أي كود.`n`n" +
    "قواعد الإخراج (Markdown):`n" + (Ocr-Rules $spread) + "`n`n" +
    "مواضع الاختلاف بين النسختين (للتنبيه فقط، والحكم للصورة):`n" + ($listed -join "`n") + "`n" + $hint +
    "النسخة (أ):`n<<<`n$a`n>>>`n`nالنسخة (ب):`n<<<`n$b`n>>>`n`nاكتب النسخة النهائية فقط، بلا أي تعليق.`n"
}

function Write-PageFile($book, [int]$page, [string]$kind, $data) {
  Write-Utf8 (Join-Path $book.dir (Page-Name $page "$kind.json")) ($data | ConvertTo-Json -Depth 5)
}

# --- What came back from a tool ---

function Test-Quota($r) {
  return ($r.code -ne 0 -or -not $r.text) -and (([string]$r.err + ' ' + [string]$r.text) -match $QuotaPattern)
}

function Word-Count([string]$text) { return (Norm-Words $text).Length }

function Test-Refusal([string]$text, [string]$first) {
  $n = Word-Count $text
  if ($n -lt 80 -and $text -match $RefusalPattern) { return $true }
  # Far shorter than the other reading of the page: not a reading of it.
  $m = Word-Count $first
  return $m -ge 60 -and $n -lt 0.3 * $m
}

# The tool's limit is used up: rest two hours, doubling each time it happens again (at most 12).
function Rest-Chip([string]$chip) {
  $c = (Load-Chips (Chip-Tool $chip))[$chip]
  $outs = 1
  if ($c) { $outs = [int]$c.outs + 1 }
  $hours = [Math]::Min(12, 2 * [Math]::Pow(2, $outs - 1))
  $until = (Get-Date).ToUniversalTime().AddHours($hours).ToString('yyyy-MM-ddTHH:mm:ssZ')
  Set-Chip $chip @{ state = 'exhausted'; text = "نفد الرصيد، يعاود نحو $(Local-Time $until)"; until = $until; outs = $outs } | Out-Null
  Write-Log "$chip used up its limit; rests $hours h"
}

# A failure that is not the limit: the page gets one more try, then goes to another model;
# a model failing three times in a row rests half an hour.
function Fail-Job($job, [string]$why) {
  $key = "$($job.chip)|$($job.book.id)|$($job.page)"
  $n = 1
  if ($script:PageFails.ContainsKey($key)) { $n = $script:PageFails[$key] + 1 }
  $script:PageFails[$key] = $n
  $c = (Load-Chips (Chip-Tool $job.chip))[$job.chip]
  $fails = 1
  if ($c) { $fails = [int]$c.fails + 1 }
  if ($fails -ge 3) {
    $until = (Get-Date).ToUniversalTime().AddMinutes(30).ToString('yyyy-MM-ddTHH:mm:ssZ')
    Set-Chip $job.chip @{ state = 'failing'; text = "تعطّل ($why)، يعاود نحو $(Local-Time $until)"; until = $until; fails = 0 } | Out-Null
  } else {
    Set-Chip $job.chip @{ fails = $fails } | Out-Null
  }
  Write-Log "$($job.chip) $($job.kind) p$($job.page) of $($job.book.id) failed: $why"
  return $n -ge 2
}

function Job-Done([string]$chip) { Set-Chip $chip @{ fails = 0; outs = 0 } | Out-Null }

# --- Running one transcription job ---

function Invoke-TranscribeJob($job, [string]$exe, [string]$work, $boxes) {
  $s = $job.files
  $p = $job.page
  $book = $job.book
  $chip = $job.chip
  $tool = Chip-Tool $chip
  $spread = Is-Spread $s
  $what = @{ settle = 'يحسم'; readB = 'يقرأ'; readA = 'يقرأ (القراءة الأولى)' }[$job.kind]
  Set-Chip $chip @{ state = 'working'; text = "$what ص $p من «$($book.title)»"; until = '' } | Out-Null
  if ($job.kind -eq 'settle') {
    $a = [string]$s.first.text
    $b = [string]$s.hb.text
    $missing = @()
    if ($s.a -and $s.first -eq $s.a) { $missing = @($s.a.missing) }
    $spans = Get-DiffSpans $a $b
    if (-not @($spans).Count -and -not @($missing | Where-Object { $_ }).Count) {
      # The readings agree: nothing to ask.
      Write-PageFile $book $p 'final' @{ page = $p; model = [string]$s.hb.model; text = $a; a_sha = (Text-Sha $a)
        b_sha = (Text-Sha $b); same = $true; at = (Now-Iso) }
      return
    }
    Write-Utf8 (Join-Path $work 'task.md') (Get-SettleTask $a $b $spans $missing $spread)
    $effort = $SettleEffort
    $prompt = 'settle'
  } else {
    Write-Utf8 (Join-Path $work 'task.md') (Get-ReadTask 'page.jpg' $spread)
    $effort = $ReadEffort
    $prompt = 'read'
  }
  Copy-Item -LiteralPath $s.img -Destination (Join-Path $work 'page.jpg') -Force
  $r = Use-Tool $tool { Invoke-Page $tool $exe (Chip-Model $chip) $effort $work 'page.jpg' $prompt }
  $text = ([string]$r.text).Replace("`r`n", "`n").Trim()
  if (Test-Quota $r) { Rest-Chip $chip; return }
  $first = ''
  if ($s.first) { $first = [string]$s.first.text }
  if ($job.kind -eq 'settle') { $first = [string]$s.hb.text }
  $refused = $r.code -eq 0 -and $text -and (Test-Refusal $text $first)
  if (-not $refused -and ($r.code -ne 0 -or -not $text -or $r.timedOut)) {
    $why = if ($r.timedOut) { 'انتهى الوقت' } elseif (-not $text) { 'رد فارغ' } else { "خطأ $($r.code)" }
    if (-not (Fail-Job $job $why)) { return }
    $refused = $true
  }
  if ($refused) {
    Add-Refused $book.id $p $chip
    $all = Get-Refused $book.id
    $left = @(@($boxes.transcribe) | Where-Object { @($all[$p]) -notcontains $_ })
    if (-not $left.Count) {
      Write-PageFile $book $p 'skip' @{ page = $p; reason = 'refused'; models = @($all[$p]); at = (Now-Iso) }
    }
    Write-Log "$chip would not do $($job.kind) on p$p of $($book.id)"
    return
  }
  Job-Done $chip
  if ($job.kind -eq 'settle') {
    Write-PageFile $book $p 'final' @{ page = $p; model = $chip; text = $text; a_sha = (Text-Sha ([string]$s.first.text))
      b_sha = (Text-Sha ([string]$s.hb.text)); at = (Now-Iso); seconds = $r.seconds }
  } else {
    Write-PageFile $book $p $(if ($job.kind -eq 'readA') { 'ha' } else { 'hb' }) @{ page = $p; model = $chip; text = $text
      at = (Now-Iso); seconds = $r.seconds }
  }
}

# --- Review: books in «مراجعة», with items.json and the place images from the run ---

function Get-ReviewBooks($boxes) {
  $root = Join-Path $Folder $ReviewsName
  $out = @()
  if (-not (Test-Path -LiteralPath $root)) { return }
  foreach ($d in @(Get-ChildItem -LiteralPath $root -Directory | Sort-Object CreationTimeUtc)) {
    # A book the script no longer lists (deleted, or nothing open) is left alone, even while
    # Drive for desktop has not yet removed its folder from this computer.
    if (-not (Review-Listed $boxes $d.Name)) { continue }
    $items = Read-Json (Join-Path $d.FullName 'items.json')
    if (-not $items) { continue }
    $out += @{ id = $d.Name; dir = $d.FullName; at = (Iso-Of $items.generated_at); pages = @($items.pages | Where-Object { $_ }) }
  }
  return $out
}

function Review-Listed($boxes, [string]$id) {
  return -not ($boxes -and $null -ne $boxes.reviews -and @($boxes.reviews) -notcontains $id)
}

# Every model's verdicts on a book's places: {item id: [{chip, verdict, text}]}.
function Get-Verdicts($book) {
  $h = @{}
  foreach ($f in @(Get-ChildItem -LiteralPath $book.dir -Filter 'j-*.json' -File -ErrorAction SilentlyContinue)) {
    $j = Read-Json $f.FullName
    if (-not $j -or -not $j.verdicts) { continue }
    foreach ($p in $j.verdicts.PSObject.Properties) {
      if (-not $h.ContainsKey($p.Name)) { $h[$p.Name] = @() }
      $h[$p.Name] += @{ chip = [string]$j.chip; verdict = [string]$p.Value.verdict; text = [string]$p.Value.text }
    }
  }
  return $h
}

# A correction that needs two models to agree: inserting a line, or changing more than six words.
function Is-Risky($item, $v) {
  if ([string]$v.verdict -ne 'fix') { return $false }
  $core = [string]$item.core
  if (-not $core.Trim()) { return $true }
  Ensure-Diff
  $a = [string[]](Norm-Words $core)
  $b = [string[]](Norm-Words ([string]$v.text))
  $common = [HelperDiff]::Common($a, $b)
  return [Math]::Max($a.Length - $common, $b.Length - $common) -gt 6
}

function Verdict-Key($v) {
  if ([string]$v.verdict -ne 'fix') { return [string]$v.verdict }
  return 'fix|' + ((Norm-Words ([string]$v.text)) -join ' ')
}

# Should this model judge this place? Not if it did; yes if nobody gave a verdict yet; yes
# for a risky correction only one model gave; no once models disagree (the user decides).
function Item-Needs($item, $vs, [string]$chip) {
  if ([string]$item.kind -eq 'page') { return $false }
  if (@($vs | Where-Object { $_.chip -eq $chip }).Count) { return $false }
  $real = @($vs | Where-Object { $_.verdict -ne 'unsure' })
  if (-not $real.Count) { return $true }
  if (@($real | ForEach-Object { Verdict-Key $_ } | Select-Object -Unique).Count -gt 1) { return $false }
  return $real.Count -lt 2 -and (Is-Risky $item $real[0])
}

# The verdict that becomes an answer, or $null: all models agree, and two of them for a risky one.
function Item-Answer($item, $vs) {
  $real = @($vs | Where-Object { $_.verdict -ne 'unsure' })
  if (-not $real.Count -or @($real | ForEach-Object { Verdict-Key $_ } | Select-Object -Unique).Count -gt 1) { return $null }
  if ((Is-Risky $item $real[0]) -and $real.Count -lt 2) { return $null }
  return @{ verdict = $real[0].verdict; text = $real[0].text; by = @($real | ForEach-Object { $_.chip }) }
}

function Find-ReviewJob([string]$chip, $books) {
  foreach ($book in $books) {
    $verdicts = Get-Verdicts $book
    foreach ($pg in $book.pages) {
      $img = Join-Path $book.dir ([string]$pg.img)
      if (-not $pg.img -or -not (Test-Path -LiteralPath $img)) { continue }
      $todo = @()
      foreach ($it in @($pg.items | Where-Object { $_ })) {
        $vs = $verdicts[[string]$it.id]
        if (-not $vs) { $vs = @() }
        if (Item-Needs $it $vs $chip) { $todo += $it }
      }
      if (-not $todo.Count) { continue }
      $key = "rv-$($book.id)-p$($pg.page)"
      if (Claim-Job $key) { return @{ kind = 'judge'; book = $book; page = $pg; items = $todo; img = $img; chip = $chip; key = $key } }
    }
  }
  return $null
}

function Get-JudgeTask($items) {
  $q = { param($t) '«' + ([string]$t).Trim() + '»' }
  $sb = New-Object Text.StringBuilder
  [void]$sb.AppendLine('أمامك صورة صفحة من كتاب عربي ممسوح ضوئياً (الملف page.jpg في هذا المجلد). عليها مربعات حمراء مرقّمة، كل مربع موضع شك في النص الذي كتبه البرنامج لهذه الصفحة.')
  [void]$sb.AppendLine('لكل موضع انظر في الصورة بنفسك (كبّر عند الحاجة) واحكم. لا تشغّل أي برنامج ولا تكتب أي كود.')
  [void]$sb.AppendLine('')
  foreach ($it in $items) {
    $other = @($it.options | Where-Object { $_ -and $_.label -eq 'other' } | Select-Object -First 1)
    $ocr = @($it.options | Where-Object { $_ -and $_.label -eq 'ocr' } | Select-Object -First 1)
    [void]$sb.AppendLine("الموضع $($it.n):")
    $around = (& $q ([string]$it.before + ' [[' + [string]$it.core + ']] ' + [string]$it.after))
    if ([string]$it.kind -eq 'diff') {
      [void]$sb.AppendLine("- النص في الكتاب: $around")
      if ($other.Count) { [void]$sb.AppendLine("- قرأه قارئ آخر هكذا: $(& $q $other[0].text)") }
    } elseif (-not ([string]$it.core).Trim()) {
      [void]$sb.AppendLine("- قد يكون هنا سطر ساقط من النص، في موضع [[ ]]: $around")
      if ($ocr.Count) { [void]$sb.AppendLine("- قرأه قارئ ثالث كثير الأخطاء هكذا: $(& $q $ocr[0].text)") }
      [void]$sb.AppendLine('- احكم: هل في الصورة هنا سطر غير موجود في النص؟')
    } else {
      [void]$sb.AppendLine("- سطر ظن قارئ ثالث (كثير الأخطاء) أنه ساقط، والأرجح أنه موجود: $around")
      if ($ocr.Count) { [void]$sb.AppendLine("- قراءة القارئ الثالث: $(& $q $ocr[0].text)") }
    }
    if ([string]$it.hint -eq 'header') { [void]$sb.AppendLine('- قد يكون ترويسة متكررة أعلى الصفحة، والترويسة لا تُكتب في النص.') }
    [void]$sb.AppendLine('')
  }
  [void]$sb.AppendLine('طريقة الجواب: مصفوفة JSON فقط، فيها عنصر لكل موضع:')
  [void]$sb.AppendLine('- {"n": 1, "verdict": "keep"} إن كان ما بين [[ ]] مطابقاً للصورة (أو: لا يوجد سطر ساقط).')
  [void]$sb.AppendLine('- {"n": 2, "verdict": "fix", "text": "..."} والنص هو الصحيح كما هو مطبوع، يحل محل ما بين [[ ]] بالضبط (أو: السطر الساقط كاملاً ليُدرج).')
  [void]$sb.AppendLine('- {"n": 3, "verdict": "unsure"} إن لم تستطع الحكم من الصورة.')
  [void]$sb.AppendLine('انسخ الحروف كما هي مطبوعة، ولا تصحح أخطاء المؤلف أو الطبعة. أرقام الصفحات والترويسة لا تُكتب في النص.')
  return $sb.ToString()
}

# The JSON array of verdicts in a tool's answer: {item id: {verdict, text}}, or $null.
function Parse-Verdicts([string]$text, $items) {
  $start = $text.IndexOf('[')
  $end = $text.LastIndexOf(']')
  if ($start -lt 0 -or $end -le $start) { return $null }
  $list = @()
  try {
    foreach ($x in (ConvertFrom-Json -InputObject $text.Substring($start, $end - $start + 1))) { $list += $x }
  } catch { return $null }
  $out = @{}
  foreach ($it in $items) {
    $v = @($list | Where-Object { $_ -and $null -ne $_.n -and [string]$_.n -eq [string]$it.n } | Select-Object -First 1)
    $verdict = 'unsure'
    $fix = ''
    if ($v.Count) {
      $verdict = [string]$v[0].verdict
      $fix = [string]$v[0].text
      if ($verdict -notin @('keep', 'fix', 'unsure')) { $verdict = 'unsure' }
      # An insertion with nothing to insert is not an answer.
      if ($verdict -eq 'fix' -and -not ([string]$it.core).Trim() -and -not $fix.Trim()) { $verdict = 'unsure' }
    }
    $out[[string]$it.id] = @{ verdict = $verdict; text = $(if ($verdict -eq 'fix') { $fix } else { '' }) }
  }
  return $out
}

function Save-Verdicts($book, [string]$chip, [int]$page, $verdicts) {
  $path = Join-Path $book.dir ('j-{0}-p{1:D4}.json' -f (Chip-Slug $chip), $page)
  $all = [ordered]@{}
  $old = Read-Json $path
  if ($old -and $old.verdicts) { foreach ($p in $old.verdicts.PSObject.Properties) { $all[$p.Name] = $p.Value } }
  foreach ($k in $verdicts.Keys) { $all[$k] = $verdicts[$k] }
  Write-Utf8 $path (@{ chip = $chip; page = $page; at = (Now-Iso); verdicts = $all } | ConvertTo-Json -Depth 5)
}

function Invoke-JudgeJob($job, [string]$exe, [string]$work, $titles) {
  $chip = $job.chip
  $tool = Chip-Tool $chip
  $p = [int]$job.page.page
  $title = $titles[$job.book.id]
  Set-Chip $chip @{ state = 'working'; text = "يراجع ص $p من «$title»"; until = '' } | Out-Null
  Copy-Item -LiteralPath $job.img -Destination (Join-Path $work 'page.jpg') -Force
  Write-Utf8 (Join-Path $work 'task.md') (Get-JudgeTask $job.items)
  $r = Use-Tool $tool { Invoke-Page $tool $exe (Chip-Model $chip) $JudgeEffort $work 'page.jpg' 'judge' }
  if (Test-Quota $r) { Rest-Chip $chip; return }
  $verdicts = $null
  if ($r.code -eq 0 -and $r.text) { $verdicts = Parse-Verdicts ([string]$r.text) $job.items }
  if ($null -eq $verdicts) {
    $why = if ($r.timedOut) { 'انتهى الوقت' } elseif ($r.code -ne 0) { "خطأ $($r.code)" } else { 'رد غير مفهوم' }
    if (-not (Fail-Job @{ chip = $chip; kind = 'judge'; book = $job.book; page = $p } $why)) { return }
    # Twice: this model leaves these places to the others (or to the user).
    $verdicts = @{}
    foreach ($it in $job.items) { $verdicts[[string]$it.id] = @{ verdict = 'unsure'; text = '' } }
  } else {
    Job-Done $chip
  }
  # The book left the review while the model judged it (deleted, or nothing open): its
  # verdicts are not kept, and nothing is written in a folder that is going.
  $now = Get-Boxes (Read-Json (Join-Path $Folder 'requests.json'))
  if (-not (Review-Listed $now ([string]$job.book.id)) -or -not (Test-Path -LiteralPath $job.book.dir)) {
    Set-Chip $chip @{ state = 'idle'; text = 'لا عمل له الآن'; until = '' } | Out-Null
    Write-Log "$chip`: $($job.book.id) left the review, verdicts on p$p dropped"
    return
  }
  Save-Verdicts $job.book $chip $p $verdicts
}

# The models' verdicts become answers, in the form of the user's own (docs/drive-inbox.md):
# «أحكام/<book>.json», kept and added to, with done when no model in the box has anything
# left to judge. The status page's script applies them in a review round.
function Update-Verdicts($boxes, $found, $states) {
  $books = @(Get-ReviewBooks $boxes)
  if (-not $books.Count) { return }
  $dir = Join-Path $Folder $VerdictsName
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $ready = @(Ready-Chips $boxes.review $found $states $boxes)
  foreach ($book in $books) {
    $verdicts = Get-Verdicts $book
    $file = Join-Path $dir "$($book.id).json"
    $old = Read-Json $file
    $answers = [ordered]@{}
    if ($old -and $old.answers) { foreach ($p in $old.answers.PSObject.Properties) { $answers[$p.Name] = $p.Value } }
    $count = $answers.Count
    $open = 0
    foreach ($pg in $book.pages) {
      foreach ($it in @($pg.items | Where-Object { $_ })) {
        if ([string]$it.kind -eq 'page') { continue }
        $id = [string]$it.id
        $vs = $verdicts[$id]
        if (-not $vs) { $vs = @() }
        foreach ($c in $ready) { if (Item-Needs $it $vs $c) { $open++; break } }
        if ($answers.Contains($id)) { continue }
        $v = Item-Answer $it $vs
        if (-not $v) { continue }
        $a = [ordered]@{ kind = [string]$it.kind; page = [int]$pg.page; verdict = $v.verdict; fp = [string]$it.fp
          before = [string]$it.before; core = [string]$it.core; after = [string]$it.after; pos = [int]$it.pos
          base = [string]$pg.base; place = [string]$it.place; at = (Now-Iso); by = @($v.by) }
        if ($v.verdict -eq 'fix') { $a.text = [string]$v.text }
        $answers[$id] = $a
      }
    }
    $done = $open -eq 0
    if ($old -and $count -eq $answers.Count -and [bool]$old.done -eq $done) { continue }
    Write-Utf8 $file ([ordered]@{ v = 1; fileId = $book.id; items_at = $book.at; done = $done; answers = $answers; at = (Now-Iso) } |
      ConvertTo-Json -Depth 6)
    Write-Log "verdicts of $($book.id): $($answers.Count) answers, done=$done"
  }
}

# --- The worker of one tool ---

function Get-Titles {
  $t = @{}
  $reqs = Read-Json (Join-Path $Folder 'requests.json')
  if ($reqs) { foreach ($b in @($reqs.books | Where-Object { $_ })) { $t[[string]$b.id] = [string]$b.title } }
  return $t
}

function Invoke-BoxJob($job, [string]$exe, $boxes, $titles) {
  $work = Join-Path $Local "work\box-$(Chip-Tool $job.chip)"
  Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force -Path $work | Out-Null
  try {
    if ($job.kind -eq 'judge') { Invoke-JudgeJob $job $exe $work $titles }
    else { Invoke-TranscribeJob $job $exe $work $boxes }
  } finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
  }
}

# The next job for one of this tool's models in the boxes, taking them in turn.
function Find-BoxJob([string]$tool, $boxes, $found, [int]$turn) {
  $mine = @(@($boxes.transcribe) + @($boxes.review) | Where-Object { (Chip-Tool $_) -eq $tool } | Select-Object -Unique)
  if (-not $mine.Count) { return $null }
  $states = All-Chips
  $books = $null
  $rbooks = $null
  for ($i = 0; $i -lt $mine.Count; $i++) {
    $chip = $mine[($turn + $i) % $mine.Count]
    if (-not (Chip-Ready $states[$chip]) -or (Chip-Window $boxes $chip)) { continue }
    if ($boxes.transcribe -contains $chip) {
      if ($null -eq $books) { $books = @(Get-TranscribeBooks) }
      $job = Find-TranscribeJob $chip $books (@(Ready-Chips $boxes.transcribe $found $states $boxes).Count)
      if ($job) { return $job }
    }
    if ($boxes.review -contains $chip) {
      if ($null -eq $rbooks) { $rbooks = @(Get-ReviewBooks $boxes) }
      $job = Find-ReviewJob $chip $rbooks
      if ($job) { return $job }
    }
    # Nothing for this model now: it is not shown at work on its last page any more.
    $st = $states[$chip]
    if ($st -and [string]$st.state -eq 'working') { Set-Chip $chip @{ state = 'idle'; text = 'لا عمل له الآن'; until = '' } | Out-Null }
  }
  return $null
}

function Invoke-BoxWorker([string]$tool) {
  $script:JobId = "box-$tool"
  $script:PageFails = @{}
  $begin = Get-Date
  $turn = 0
  while ($true) {
    Update-Path
    $found = Get-Tools
    $boxes = Get-Boxes (Read-Json (Join-Path $Folder 'requests.json'))
    $mine = @(@($boxes.transcribe) + @($boxes.review) | Where-Object { (Chip-Tool $_) -eq $tool } | Select-Object -Unique)
    if (-not $found[$tool] -or -not $mine.Count) { break }
    $job = Find-BoxJob $tool $boxes $found $turn
    $turn++
    if ($job) {
      try {
        Invoke-BoxJob $job $found[$tool] $boxes (Get-Titles)
      } catch {
        Write-Log "box job failed: $_"
      } finally {
        Drop-Claim $job.key
      }
    } else {
      $states = All-Chips
      foreach ($c in $mine) {
        if ((Chip-Ready $states[$c]) -and -not (Chip-Window $boxes $c)) { Set-Chip $c @{ state = 'idle'; text = 'لا عمل له الآن'; until = '' } | Out-Null }
      }
    }
    if (((Get-Date) - $begin).TotalMinutes -ge $WorkerMinutes) { break }
    if (-not $job) { Start-Sleep -Seconds 60 }
  }
}

# One worker per tool with models in the boxes (each runs until it has worked half an
# hour, then the next cycle starts a fresh one with the newest program).
function Start-BoxWorkers($boxes, $found) {
  $tools = @(@($boxes.transcribe) + @($boxes.review) | ForEach-Object { Chip-Tool $_ } | Select-Object -Unique)
  foreach ($tool in $tools) {
    if (-not $found[$tool]) { continue }
    $info = Read-Json (Join-Path $Jobs "box-$tool.json")
    if ($info -and (Get-Process -Id ([int]$info.pid) -ErrorAction SilentlyContinue)) { continue }
    $p = Start-Hidden "box-$tool"
    New-Item -ItemType Directory -Force -Path $Jobs | Out-Null
    Write-Utf8 (Join-Path $Jobs "box-$tool.json") (@{ pid = $p.Id; at = (Now-Iso) } | ConvertTo-Json)
    Write-Log "box worker for $tool started (pid $($p.Id))"
  }
}

# The helper's work on each book's pages, for the status page's script: {book: {need,
# finished, book_at}}. Worked out again only when the book's folder changed.
function Get-BookProgress {
  $cachePath = Join-Path $Local 'progress.json'
  $cache = Read-Json $cachePath
  $out = [ordered]@{}
  $next = @{}
  foreach ($book in @(Get-TranscribeBooks)) {
    $stamp = (@(Get-ChildItem -LiteralPath $book.dir -File | ForEach-Object { $_.LastWriteTimeUtc.Ticks }) |
      Measure-Object -Maximum).Maximum
    $sig = "$($book.names.Count):${stamp}:$($book.at)"
    $known = $null
    if ($cache -and $cache.PSObject.Properties.Name -contains $book.id) { $known = $cache.($book.id) }
    if ($known -and [string]$known.sig -eq $sig) {
      $fin = [int]$known.finished
    } else {
      $fin = 0
      foreach ($p in $book.need) {
        if ($book.names[(Page-Name $p 'skip.json')] -or (Get-PageFiles $book $p).settled) { $fin++ }
      }
    }
    $out[$book.id] = @{ need = $book.need.Count; finished = $fin; book_at = $book.at }
    $next[$book.id] = @{ sig = $sig; finished = $fin }
  }
  Write-Utf8 $cachePath ($next | ConvertTo-Json -Depth 3)
  return $out
}

# ---------------------------------------------------------------------------
# Status for the page
# ---------------------------------------------------------------------------

function Write-Status($found, $state, $running, $boxes) {
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

  # The boxes on the page: every model of the tools found, what each model in a box is
  # doing, and the helper's work on each book.
  $models = @()
  foreach ($name in $Tools.Keys) {
    if (-not $found[$name]) { continue }
    foreach ($m in $Tools[$name].models) {
      $models += @{ id = "$name|$($m.id)"; label = $m.label; tool = $Tools[$name].label; warn = [string]$m.warn }
    }
  }
  $chips = [ordered]@{}
  if ($boxes) {
    $states = All-Chips
    foreach ($c in @(@($boxes.transcribe) + @($boxes.review) | Select-Object -Unique)) {
      $st = $states[$c]
      $window = Chip-Window $boxes $c
      if (-not $found[(Chip-Tool $c)]) { $chips[$c] = @{ state = 'failing'; text = 'برنامجه غير موجود على الجهاز' } }
      elseif ($window -eq 'later') { $chips[$c] = @{ state = 'idle'; text = "ينتظر وقت البدء: نحو $(Local-Time $boxes.times[$c].start)" } }
      elseif ($window -eq 'over') { $chips[$c] = @{ state = 'idle'; text = 'توقّف في الوقت الذي حددته' } }
      elseif ($st -and -not (Chip-Ready $st)) { $chips[$c] = @{ state = $st.state; text = $st.text } }
      elseif ($st -and $st.state -eq 'working') { $chips[$c] = @{ state = 'working'; text = $st.text } }
      elseif ($st) { $chips[$c] = @{ state = 'idle'; text = $(if ($st.text) { $st.text } else { 'لا عمل له الآن' }) } }
    }
  }
  $books = [ordered]@{}
  try { $books = Get-BookProgress } catch { Write-Log "progress failed: $_" }
  # No buttons of the helper's own for now (the trial of the six test pages is over).
  $actions = @()
  $status = [ordered]@{
    v = 1; at = (Now-Iso); version = $HelperVersion; computer = $env:COMPUTERNAME
    busy = $(if ($running.Count) { [string]$running[0].id } else { '' })
    handled = @($state.done); lines = $lines; actions = $actions; models = $models; chips = $chips; books = $books
  }
  Write-Utf8 (Join-Path $Folder 'helper-status.json') ($status | ConvertTo-Json -Depth 8)
}

# ---------------------------------------------------------------------------
# Entry points (called by launcher.ps1)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Words as crosscheck.py compares them
# ---------------------------------------------------------------------------

# No diacritics, one form of alef, ya, ta marbuta and hamza seats, Arabic digits as Latin.

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

# The tool a request uses (none of the current kinds needs one).
function Request-Tool($req) {
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

# A hidden PowerShell running one request (its id) or one tool's box worker ("box-<tool>").
function Start-Hidden([string]$id) {
  # The worker loads the same copy of this file that the launcher saved (core.ps1).
  $q = { param($t) "'" + ($t -replace "'", "''") + "'" }
  $worker = Join-Path $Local 'worker.ps1'
  $text = "param([string]`$Id)`r`n`$ErrorActionPreference = 'Stop'`r`n" +
    "`$Local = $(& $q $Local)`r`n`$Folder = $(& $q $Folder)`r`n" +
    "function Write-Log([string]`$t) { try { Add-Content -Path (Join-Path `$Local 'helper.log') -Value ((Get-Date).ToString('yyyy-MM-dd HH:mm:ss') + ' [' + `$Id + '] ' + `$t) -Encoding UTF8 } catch { } }`r`n" +
    "function Read-Utf8([string]`$path) { return [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes(`$path)).TrimStart([char]0xFEFF) }`r`n" +
    ". ([scriptblock]::Create((Read-Utf8 (Join-Path `$Local 'core.ps1'))))`r`n" +
    "if (`$Id -like 'box-*') { Invoke-BoxWorker (`$Id.Substring(4)) } else { Invoke-HelperWorker `$Id }`r`n"
  [IO.File]::WriteAllText($worker, $text, (New-Object Text.UTF8Encoding $true))
  return Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -PassThru -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', ('"' + $worker + '"'), '-Id', $id)
}

function Start-Worker($req) {
  New-Item -ItemType Directory -Force -Path $Jobs | Out-Null
  $id = [string]$req.id
  Write-Utf8 (Join-Path $Jobs "$id.req.json") ($req | ConvertTo-Json -Depth 6)
  $p = Start-Hidden $id
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
    Write-Status $found $state @() $null
    try { Invoke-Probe $found } catch { Write-Log "probe failed: $_" }
    $state.probed = $probeKey
  }
  $running = @(Update-Jobs $state)
  $busyTools = @($running | ForEach-Object { $_.tool })
  $reqs = Read-Json (Join-Path $Folder 'requests.json')
  if ($reqs) {
    foreach ($r in @($reqs.requests)) {
      if (-not ($r -and $r.id) -or (@($state.done) -contains $r.id) -or (@($running | ForEach-Object { $_.id }) -contains $r.id)) { continue }
      # "أعد المحاولة الآن" on the page: the model stops resting (no worker needed).
      if ([string]$r.action -eq 'retry') {
        $chip = [string]$r.choices.model
        if ($chip -match $ChipPattern) {
          Set-Chip $chip @{ state = 'idle'; text = 'يعاود الآن'; until = ''; fails = 0; outs = 0 } | Out-Null
          Write-Log "$chip tries again now (asked on the page)"
        }
        $state.done = @($state.done) + [string]$r.id
        continue
      }
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
  $boxes = Get-Boxes $reqs
  try { Start-BoxWorkers $boxes $found } catch { Write-Log "box workers: $_" }
  try { Update-Verdicts $boxes $found (All-Chips) } catch { Write-Log "verdicts failed: $_" }
  Clear-StaleClaims
  Save-State $state
  Write-Status $found $state $running $boxes
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
