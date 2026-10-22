# Tests for helper/helper.ps1: the box workers' jobs (transcription and review), with the
# tools replaced by a scripted fake and the Drive folder by a temporary one.
#
#   pwsh -NoProfile -File tests/helper/run_tests.ps1
#
# Exit code is non-zero when a test fails.
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Tmp = Join-Path ([IO.Path]::GetTempPath()) ('helper-test-' + [Guid]::NewGuid().ToString('N'))
$Local = Join-Path $Tmp 'local'
$Folder = Join-Path $Tmp 'بيانات المساعد'
New-Item -ItemType Directory -Force -Path $Local, $Folder | Out-Null
$script:Log = @()
function Write-Log([string]$t) { $script:Log += $t }
function Read-Utf8([string]$path) { return [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($path)).TrimStart([char]0xFEFF) }
. ([scriptblock]::Create((Read-Utf8 (Join-Path $Root 'helper/helper.ps1'))))

# --- Fakes ---

$script:Calls = @()
$script:Answer = { param($tool, $model, $kind, $task) @{ text = ''; code = 1; err = 'no answer scripted' } }
function Get-Tools { return [ordered]@{ grok = 'grok.exe'; agy = 'agy.exe'; codex = $null; claude = 'claude.exe' } }
function Invoke-Page([string]$tool, [string]$exe, [string]$model, [string]$effort, [string]$dir, [string]$image = 'page.png', [string]$kind = 'read') {
  $task = Read-Utf8 (Join-Path $dir 'task.md')
  if (-not (Test-Path -LiteralPath (Join-Path $dir $image))) { throw "no image $image" }
  $script:Calls += @{ tool = $tool; model = $model; kind = $kind; effort = $effort; task = $task }
  $r = & $script:Answer $tool $model $kind $task
  return @{ text = [string]$r.text; code = $(if ($null -ne $r.code) { $r.code } else { 0 }); seconds = 1; timedOut = $false; err = [string]$r.err }
}

$GEMINI = 'agy|gemini-3.8-flash'
$GROK = 'grok|grok-4.7'
$CLAUDE = 'claude|sonnet'
$RIGHT = ('وجود قوانين خاصة بالتطور ما دام يرى أنه قد برهن برهنة كافية على وجود قوانين خاصة بالاستقرار الاجتماعى ' * 3).Trim()
$WRONG = ([regex]'برهنة كافية').Replace($RIGHT, 'برهنة كاقية', 1)

function Reset-World {
  Remove-Item -LiteralPath $Local, $Folder -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force -Path $Local, $Folder | Out-Null
  $script:Calls = @()
  $script:PageFails = @{}
  $script:Log = @()
}

function Set-Boxes($transcribe, $review) {
  Write-Utf8 (Join-Path $Folder 'requests.json') (@{ v = 1; requests = @(); books = @(@{ id = 'book1'; title = 'كتاب الاختبار' })
      boxes = @{ transcribe = @($transcribe); review = @($review) } } | ConvertTo-Json -Depth 5)
}

function Make-Book([string]$id, $need, $needA, [bool]$liteOut, $readings) {
  $dir = Join-Path (Join-Path $Folder $BooksName) $id
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  Write-Utf8 (Join-Path $dir 'book.json') (@{ v = 1; title = 'كتاب الاختبار'; total = 10; need = @($need); need_a = @($needA)
      lite_out = $liteOut; at = '2026-10-20T10:00:00Z' } | ConvertTo-Json)
  foreach ($p in $need) { [IO.File]::WriteAllBytes((Join-Path $dir (Page-Name $p 'jpg')), [byte[]](0xff, 0xd8, $p)) }
  foreach ($p in $readings.Keys) {
    Write-Utf8 (Join-Path $dir (Page-Name $p 'a.json')) (@{ page = $p; model = 'gemini-3.5-flash-lite'; text = $readings[$p]
        spread = $false; missing = @() } | ConvertTo-Json)
  }
  return $dir
}

function Read-PageFile([string]$dir, [int]$p, [string]$kind) { return Read-Json (Join-Path $dir (Page-Name $p "$kind.json")) }

function Refused-Count([int]$p) {
  $r = (Get-Refused 'book1')[$p]
  if ($r) { return @($r).Count }
  return 0
}

# One job of one tool, as its worker would do it; returns the job (or $null).
function Work([string]$tool) {
  $boxes = Get-Boxes (Read-Json (Join-Path $Folder 'requests.json'))
  $job = Find-BoxJob $tool $boxes (Get-Tools) 0
  if ($job) {
    try { Invoke-BoxJob $job (Get-Tools)[$tool] $boxes (Get-Titles) } finally { Drop-Claim $job.key }
  }
  return $job
}

$script:Failed = 0
$script:Count = 0
function Test([string]$name, [scriptblock]$body) {
  $script:Count++
  Reset-World
  try {
    & $body
    Write-Host "PASS  $name"
  } catch {
    $script:Failed++
    Write-Host "FAIL  $name`n        $($_.Exception.Message)`n        $($_.InvocationInfo.PositionMessage)"
  }
}
function Check([bool]$ok, [string]$what) { if (-not $ok) { throw "check failed: $what" } }
function Same($a, $b, [string]$what) { if ([string]$a -ne [string]$b) { throw "$what`: expected «$b», got «$a»" } }

# --- Tests ---

Test 'texts are named as the converter names them (sha1, 12 characters)' {
  Same (Text-Sha 'وجود قوانين خاصة بالتطور') '01741194b569' 'Arabic'
  Same (Text-Sha "نص`nبسطرين") '91bb9d8e6543' 'two lines'
  Same (Text-Sha '') 'da39a3ee5e6b' 'empty'
}

Test 'the differences between two readings are listed as the converter lists them' {
  $spans = Get-DiffSpans $WRONG $RIGHT
  Same @($spans).Count 1 'one difference'
  Same $spans[0].a 'كاقية' 'reading (a)'
  Same $spans[0].b 'كافية' 'reading (b)'
  $same = Get-DiffSpans $RIGHT $RIGHT.Replace('الاجتماعى', 'الاجتماعي')
  Same @($same).Count 0 'spelling variants are not differences'
  $task = Get-SettleTask $WRONG $RIGHT $spans @('سطر قد يكون ساقطاً') $false
  Check ($task.Contains('- (أ): كاقية | (ب): كافية')) 'the difference is listed'
  Check ($task.Contains("النسخة (أ):`n<<<`n$WRONG`n>>>")) 'reading (a) in full'
  Check ($task.Contains('- سطر قد يكون ساقطاً')) 'the missing line is listed'
  Check ($task.Contains('page.jpg')) 'the image is named'
}

Test 'transcription: the free reading is checked by a second reading and settled; agreeing readings need no request' {
  Set-Boxes @($GEMINI) @()
  $dir = Make-Book 'book1' @(1, 2) @() $false @{ 1 = $WRONG; 2 = $RIGHT }
  $script:Answer = { param($tool, $model, $kind, $task) @{ text = $RIGHT } }
  $job = Work 'agy'
  Same $job.kind 'readB' 'second readings first while nothing is ready to settle'
  Same (Read-PageFile $dir 1 'hb').model $GEMINI 'second reading of page 1'
  Same $script:Calls[0].effort 'low' 'readings at low effort'
  $job = Work 'agy'
  Same "$($job.kind) $($job.page)" 'settle 1' 'then page 1 is settled'
  $final = Read-PageFile $dir 1 'final'
  Same $final.text $RIGHT 'settled text'
  Same $final.a_sha (Text-Sha $WRONG) 'made on the free reading'
  Same $final.b_sha (Text-Sha $RIGHT) 'and on the second reading'
  Same $script:Calls[1].kind 'settle' 'a settling request'
  Same $script:Calls[1].effort 'medium' 'settling at medium effort'
  $job = Work 'agy'
  Same "$($job.kind) $($job.page)" 'readB 2' 'page 2'
  $calls = $script:Calls.Count
  $job = Work 'agy'
  Same "$($job.kind) $($job.page)" 'settle 2' 'page 2 is settled'
  Same $script:Calls.Count $calls 'agreeing readings: no request'
  Check ([bool](Read-PageFile $dir 2 'final').same) 'marked as the same'
  Same (Work 'agy') '' 'nothing left'
  $progress = Get-BookProgress
  Same "$($progress['book1'].need)/$($progress['book1'].finished)" '2/2' 'progress for the status page'
}

Test 'transcription: with the free quota out, two models give the two readings, never one model both' {
  Set-Boxes @($GEMINI) @()
  $dir = Make-Book 'book1' @(3) @(3) $true @{}
  $script:Answer = { param($tool, $model, $kind, $task) @{ text = $RIGHT } }
  $job = Work 'agy'
  Same $job.kind 'readB' 'one model: only the second reading'
  Same (Work 'agy') '' 'it does not give the first reading too'
  Set-Boxes @($GEMINI, $GROK) @()
  $job = Work 'agy'
  Same $job '' 'still not the same model'
  $job = Work 'grok'
  Same "$($job.kind) $($job.page)" 'readA 3' 'the other model gives the first reading'
  Same (Read-PageFile $dir 3 'ha').model $GROK 'first reading by Grok'
  $job = Work 'agy'
  Same $job.kind 'settle' 'then it is settled'
  Same (Read-PageFile $dir 3 'final').a_sha (Text-Sha $RIGHT) 'on the first reading of Grok'
}

Test 'transcription: a page every model now in the box refused goes back, even when the box changed after the refusals' {
  Set-Boxes @($CLAUDE, $GEMINI, $GROK) @()
  $dir = Make-Book 'book1' @(5) @() $false @{ 5 = $RIGHT }
  $script:Answer = { param($tool, $model, $kind, $task) @{ text = 'عذراً، لا أستطيع نسخ هذه الصفحة.' } }
  Same (Work 'claude').kind 'readB' 'Claude refused'
  Same (Work 'agy').kind 'readB' 'Gemini refused'
  Check (-not (Test-Path -LiteralPath (Join-Path $dir (Page-Name 5 'skip.json')))) 'Grok may still read it'
  # The user takes Grok out of the box: nobody left would take the page.
  Set-Boxes @($CLAUDE, $GEMINI) @()
  Same (Work 'claude') '' 'not Claude'
  Same (Work 'agy') '' 'not Gemini'
  $boxes = Get-Boxes (Read-Json (Join-Path $Folder 'requests.json'))
  $progress = Get-BookProgress $boxes (Get-Tools)
  Same (Read-PageFile $dir 5 'skip').reason 'refused' 'handed back to the free models'
  Same "$($progress.book1.finished)/$($progress.book1.need)" '1/1' 'the book is finished for the helper'
}

Test 'transcription: a refused page goes to another model, and back to the free way when every model refused' {
  Set-Boxes @($CLAUDE, $GEMINI) @()
  $dir = Make-Book 'book1' @(5) @() $false @{ 5 = $RIGHT }
  $script:Answer = { param($tool, $model, $kind, $task) @{ text = "I'm sorry, but I can't help transcribe copyrighted text." } }
  $job = Work 'claude'
  Same $job.kind 'readB' 'Claude tried'
  Check (-not (Test-Path -LiteralPath (Join-Path $dir (Page-Name 5 'hb.json')))) 'no reading from a refusal'
  Same (Work 'claude') '' 'Claude does not try that page again'
  Check (-not (Test-Path -LiteralPath (Join-Path $dir (Page-Name 5 'skip.json')))) 'another model may still read it'
  $script:Answer = { param($tool, $model, $kind, $task) @{ text = 'عذراً، لا أستطيع نسخ هذه الصفحة.' } }
  $job = Work 'agy'
  Same $job.kind 'readB' 'Gemini tried too'
  Same (Read-PageFile $dir 5 'skip').reason 'refused' 'handed back to the free way'
}

Test 'transcription: a used-up limit rests the model without counting against the page' {
  Set-Boxes @($GEMINI) @()
  $dir = Make-Book 'book1' @(1) @() $false @{ 1 = $RIGHT }
  $script:Answer = { param($tool, $model, $kind, $task) @{ code = 1; err = 'Error: 429 Too Many Requests. You have exceeded your usage limit.' } }
  $job = Work 'agy'
  Same $job.kind 'readB' 'tried'
  $c = (Load-Chips 'agy')[$GEMINI]
  Same $c.state 'exhausted' 'resting'
  Check (([DateTime]::Parse($c.until)).ToUniversalTime() -gt (Get-Date).ToUniversalTime().AddMinutes(100)) 'for about two hours'
  Same (Work 'agy') '' 'no job while resting'
  Same (Refused-Count 1) 0 'the page is not given up'
  # A tool error (a crash, signed out, network down) says nothing about the page: never given up for it.
  Set-Chip $GEMINI @{ state = 'idle'; until = '' } | Out-Null
  $script:Answer = { param($tool, $model, $kind, $task) @{ code = 2; err = 'crashed' } }
  Work 'agy' | Out-Null
  Work 'agy' | Out-Null
  Same (Refused-Count 1) 0 'not for a tool error'
  # The tool ran but gave nothing back, twice: the page goes to the others.
  Set-Chip $GEMINI @{ state = 'idle'; until = ''; fails = 0 } | Out-Null
  $script:PageFails = @{}
  $script:Answer = { param($tool, $model, $kind, $task) @{ text = '' } }
  Work 'agy' | Out-Null
  Same (Refused-Count 1) 0 'not after one empty answer'
  Work 'agy' | Out-Null
  Same (Refused-Count 1) 1 'after two'
}

Test 'transcription: a page the helper read twice and settled itself is done, even when the free reading arrives later' {
  Set-Boxes @($GEMINI, $GROK) @()
  $dir = Make-Book 'book1' @(5) @() $false @{}
  Write-Utf8 (Join-Path $dir (Page-Name 5 'ha.json')) (@{ page = 5; model = $GROK; text = $RIGHT } | ConvertTo-Json)
  Write-Utf8 (Join-Path $dir (Page-Name 5 'hb.json')) (@{ page = 5; model = $GEMINI; text = $RIGHT } | ConvertTo-Json)
  Write-Utf8 (Join-Path $dir (Page-Name 5 'final.json')) (@{ page = 5; model = $GEMINI; text = $RIGHT
      a_sha = (Text-Sha $RIGHT); b_sha = (Text-Sha $RIGHT) } | ConvertTo-Json)
  # The free model's reading of the page reaches the helper only now.
  Write-Utf8 (Join-Path $dir (Page-Name 5 'a.json')) (@{ page = 5; model = 'gemini-3.5-flash-lite'; text = $WRONG
      spread = $false; missing = @() } | ConvertTo-Json)
  Same (Work 'agy') '' 'nothing to settle again'
  Same (Work 'grok') '' 'nor to read'
  Same @($script:Calls).Count 0 'no request'
}

Test 'transcription: [[EMPTY]] for a page with words is a refusal, a tool error is not, and a deleted book is left alone' {
  Set-Boxes @($GEMINI, $GROK) @()
  $dir = Make-Book 'book1' @(7) @() $false @{ 7 = $RIGHT }
  $script:Answer = { param($tool, $model, $kind, $task) if ($tool -eq 'agy') { @{ text = '[[EMPTY]]' } } else { @{ text = ''; code = 1; err = 'signed out' } } }
  Work 'agy' | Out-Null
  Check (-not (Test-Path -LiteralPath (Join-Path $dir (Page-Name 7 'hb.json')))) 'no empty second reading kept'
  Same (Refused-Count 7) 1 'Gemini refused the page'
  Work 'grok' | Out-Null
  Work 'grok' | Out-Null
  Same (Refused-Count 7) 1 'a signed-out Grok does not refuse the page'
  Write-Utf8 (Join-Path $Folder 'requests.json') (@{ v = 1; requests = @(); books = @(); gone = @('book1')
      boxes = @{ transcribe = @($GEMINI, $GROK); review = @() } } | ConvertTo-Json -Depth 5)
  $script:Calls = @()
  Same (Work 'grok') '' 'a deleted book gets no job'
  Same @($script:Calls).Count 0 'and no request'
}

Test 'claims: two workers never take the same job' {
  Set-Boxes @($GEMINI, $GROK) @()
  Make-Book 'book1' @(1) @() $false @{ 1 = $RIGHT } | Out-Null
  $boxes = Get-Boxes (Read-Json (Join-Path $Folder 'requests.json'))
  $a = Find-BoxJob 'agy' $boxes (Get-Tools) 0
  $b = Find-BoxJob 'grok' $boxes (Get-Tools) 0
  Same $a.kind 'readB' 'the first takes the second reading'
  Same $b '' 'the other finds nothing else to do'
  Drop-Claim $a.key
  Same (Find-BoxJob 'grok' $boxes (Get-Tools) 0).kind 'readB' 'free again once released'
}

Test 'review: safe verdicts become answers; a risky one needs a second model; models that disagree leave it to the user' {
  Set-Boxes @() @($GEMINI, $GROK)
  $dir = Join-Path (Join-Path $Folder $ReviewsName) 'book1'
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  [IO.File]::WriteAllBytes((Join-Path $dir 'p0003-0123456789.jpg'), [byte[]](0xff, 0xd8))
  $items = @(
    @{ id = 'p3-0000000001'; n = 1; kind = 'diff'; fp = 'diff|1'; before = 'قبل '; core = 'كلمة'; after = ' بعد'; pos = 10
      options = @(@{ label = 'other'; text = 'كلمه' }) },
    @{ id = 'p3-0000000002'; n = 2; kind = 'missing'; fp = 'missing|2'; before = 'آخر السطر'; core = ''; after = 'أول السطر'; pos = 40
      place = 'after'; options = @(@{ label = 'ocr'; text = 'سطر سقط من النص' }) },
    @{ id = 'p3-0000000003'; n = 3; kind = 'diff'; fp = 'diff|3'; before = 'في '; core = 'هذا المكان'; after = ' كلام'; pos = 80
      options = @(@{ label = 'other'; text = 'ذلك المكان' }) },
    @{ id = 'p3-0000000004'; n = 4; kind = 'page'; fp = 'page'; before = ''; core = 'الصفحة كلها'; after = ''; pos = 0 })
  Write-Utf8 (Join-Path $dir 'items.json') (@{ v = 1; generated_at = '2026-10-20T10:00:00Z'
      pages = @(@{ page = 3; base = 'base00000000'; img = 'p0003-0123456789.jpg'; items = $items }) } | ConvertTo-Json -Depth 8)
  $script:Answer = { param($tool, $model, $kind, $task)
    if ($tool -eq 'agy') { return @{ text = '```json' + "`n" + '[{"n":1,"verdict":"keep"},{"n":2,"verdict":"fix","text":"سطر سقط من النص كاملاً"},{"n":3,"verdict":"fix","text":"ذلك المكان"}]' + "`n" + '```' } }
    return @{ text = '[{"n":2,"verdict":"fix","text":"سطر سقط من النص كاملاً"}]' } }
  $job = Work 'agy'
  Same $job.kind 'judge' 'Gemini judges the page'
  Same @($job.items).Count 3 'every place but the whole page'
  Check ($script:Calls[0].task.Contains('الموضع 2:')) 'places are listed by number'
  Update-Verdicts (Get-Boxes (Read-Json (Join-Path $Folder 'requests.json'))) (Get-Tools) (All-Chips)
  $v = Read-Json (Join-Path (Join-Path $Folder $VerdictsName) 'book1.json')
  Same (@($v.answers.PSObject.Properties.Name) -join ',') 'p3-0000000001,p3-0000000003' 'the keep and the two-word fix'
  Same $v.answers.'p3-0000000003'.text 'ذلك المكان' 'its text'
  Same $v.answers.'p3-0000000001'.base 'base00000000' 'answers carry the page base'
  Same $v.done 'False' 'the inserted line waits for a second model'
  $job = Work 'grok'
  Same (@($job.items | ForEach-Object { $_.id }) -join ',') 'p3-0000000002' 'Grok sees only the risky place'
  Update-Verdicts (Get-Boxes (Read-Json (Join-Path $Folder 'requests.json'))) (Get-Tools) (All-Chips)
  $v = Read-Json (Join-Path (Join-Path $Folder $VerdictsName) 'book1.json')
  Same $v.answers.'p3-0000000002'.text 'سطر سقط من النص كاملاً' 'two models agreed on the line'
  Same (@($v.answers.'p3-0000000002'.by) -join ',') "$GEMINI,$GROK" 'by both'
  Same $v.done 'True' 'nothing left to judge'
  Same (Work 'agy') '' 'and no more jobs'
}

Test 'review: only the books the script lists; a book that leaves while judged keeps no verdict; an idle model is not shown at work' {
  $requests = { param($reviews)
    Write-Utf8 (Join-Path $Folder 'requests.json') (@{ v = 1; requests = @(); books = @(@{ id = 'book1'; title = 'كتاب الاختبار' })
        boxes = @{ transcribe = @(); review = @($GEMINI) }; reviews = @($reviews) } | ConvertTo-Json -Depth 5) }
  $dir = Join-Path (Join-Path $Folder $ReviewsName) 'book1'
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  [IO.File]::WriteAllBytes((Join-Path $dir 'p0003-0123456789.jpg'), [byte[]](0xff, 0xd8))
  $items = @(@{ id = 'p3-0000000001'; n = 1; kind = 'diff'; fp = 'diff|1'; before = 'قبل '; core = 'كلمة'; after = ' بعد'; pos = 10
      options = @(@{ label = 'other'; text = 'كلمه' }) })
  Write-Utf8 (Join-Path $dir 'items.json') (@{ v = 1; generated_at = 'x'; pages = @(@{ page = 3; base = 'b'; img = 'p0003-0123456789.jpg'; items = $items }) } |
    ConvertTo-Json -Depth 8)
  $verdictFiles = { @(Get-ChildItem -LiteralPath $dir -Filter 'j-*.json' -File).Count }

  # Deleted: its folder is still on the computer, but not listed. The model is not shown at work.
  & $requests @()
  Set-Chip $GEMINI @{ state = 'working'; text = 'يراجع ص 3 من «كتاب الاختبار»'; until = '' } | Out-Null
  Same (Work 'agy') '' 'no job on a book the script does not list'
  Same (All-Chips)[$GEMINI].state 'idle' 'shown idle'
  Same @($script:Calls).Count 0 'no request'

  # Listed, but it leaves while the model judges it: no verdict is kept.
  & $requests @('book1')
  $script:Answer = { param($tool, $model, $kind, $task)
    & $requests @()
    return @{ text = '[{"n":1,"verdict":"keep"}]' } }
  $job = Work 'agy'
  Same $job.kind 'judge' 'judged while listed'
  Same (& $verdictFiles) 0 'its verdict dropped'
  Same (All-Chips)[$GEMINI].state 'idle' 'and the model idle'

  # Listed and still listed: the verdict is kept.
  & $requests @('book1')
  $script:Answer = { param($tool, $model, $kind, $task) @{ text = '[{"n":1,"verdict":"keep"}]' } }
  Same (Work 'agy').kind 'judge' 'judged'
  Same (& $verdictFiles) 1 'verdict kept'
}

Test 'review: when two models disagree on a risky place, it stays for the user' {
  Set-Boxes @() @($GEMINI, $GROK)
  $dir = Join-Path (Join-Path $Folder $ReviewsName) 'book1'
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  [IO.File]::WriteAllBytes((Join-Path $dir 'p0007-0123456789.jpg'), [byte[]](0xff, 0xd8))
  $items = @(@{ id = 'p7-0000000001'; n = 1; kind = 'missing'; fp = 'm'; before = 'أ'; core = ''; after = 'ب'; pos = 5
      options = @(@{ label = 'ocr'; text = 'سطر' }) })
  Write-Utf8 (Join-Path $dir 'items.json') (@{ v = 1; generated_at = 'x'; pages = @(@{ page = 7; base = 'b'; img = 'p0007-0123456789.jpg'; items = $items }) } |
    ConvertTo-Json -Depth 8)
  $script:Answer = { param($tool, $model, $kind, $task)
    if ($tool -eq 'agy') { return @{ text = '[{"n":1,"verdict":"fix","text":"سطر أول"}]' } }
    return @{ text = '[{"n":1,"verdict":"keep"}]' } }
  Work 'agy' | Out-Null
  Work 'grok' | Out-Null
  Update-Verdicts (Get-Boxes (Read-Json (Join-Path $Folder 'requests.json'))) (Get-Tools) (All-Chips)
  $v = Read-Json (Join-Path (Join-Path $Folder $VerdictsName) 'book1.json')
  Same @($v.answers.PSObject.Properties).Count 0 'no answer'
  Same $v.done 'True' 'and done'
}

Test 'the status page gets the models, what each model in a box is doing, and the progress of each book' {
  Set-Boxes @($GEMINI, 'codex|gpt-5.6-terra') @($GROK)
  Make-Book 'book1' @(1, 2) @() $false @{ 1 = $RIGHT } | Out-Null
  Set-Chip $GEMINI @{ state = 'working'; text = 'يقرأ ص 1' } | Out-Null
  $state = Load-State
  Write-Status (Get-Tools) $state @() (Get-Boxes (Read-Json (Join-Path $Folder 'requests.json')))
  $s = Read-Json (Join-Path $Folder 'helper-status.json')
  Check (@($s.models | ForEach-Object { $_.id }) -contains $GEMINI) 'models of the tools found'
  Check (-not (@($s.models | ForEach-Object { $_.id }) -contains 'codex|gpt-5.6-terra')) 'not of tools missing'
  Same (@($s.models | Where-Object { $_.id -eq 'agy|gemini-4-argon' })[0].warn) 'لا للتفريغ: يستنفد الحدود' 'with the warnings'
  Same $s.chips.$GEMINI.text 'يقرأ ص 1' 'what Gemini does'
  Same $s.chips.'codex|gpt-5.6-terra'.state 'failing' 'a model whose tool is missing'
  Same "$($s.books.book1.need)/$($s.books.book1.finished)" '2/0' 'progress'
  Same (Iso-Of $s.books.book1.book_at) '2026-10-20T10:00:00Z' 'with the time of book.json'
}

Test '"try again now" on the page: a resting model works again at once' {
  Set-Boxes @($GEMINI) @()
  Make-Book 'book1' @(1) @() $false @{ 1 = $RIGHT } | Out-Null
  $script:Answer = { param($tool, $model, $kind, $task) @{ code = 1; err = '429 rate limit reached' } }
  Work 'agy' | Out-Null
  Same (Load-Chips 'agy')[$GEMINI].state 'exhausted' 'resting'
  $reqs = Read-Json (Join-Path $Folder 'requests.json')
  $reqs | Add-Member -NotePropertyName requests -NotePropertyValue @(@{ id = 'r1'; action = 'retry'; choices = @{ model = $GEMINI } }) -Force
  Write-Utf8 (Join-Path $Folder 'requests.json') ($reqs | ConvertTo-Json -Depth 6)
  function Start-Hidden([string]$id) { return [pscustomobject]@{ Id = $PID } }
  function Invoke-Tool { throw 'no tools here' }
  Invoke-HelperCycle
  $c = (Load-Chips 'agy')[$GEMINI]
  Check (Chip-Ready $c) 'ready again'
  Same $c.outs 0 'the waits start from two hours again'
  Check (@((Load-State).done) -contains 'r1') 'the request is done'
  $script:Answer = { param($tool, $model, $kind, $task) @{ text = $RIGHT } }
  Same (Work 'agy').kind 'readB' 'and takes work'
}

Test 'times set on the page: a model waits for its start, and stops after its stop' {
  Make-Book 'book1' @(1) @() $false @{ 1 = $RIGHT } | Out-Null
  $script:Answer = { param($tool, $model, $kind, $task) @{ text = $RIGHT } }
  $later = (Get-Date).ToUniversalTime().AddMinutes(90).ToString('yyyy-MM-ddTHH:mm:ssZ')
  $past = (Get-Date).ToUniversalTime().AddMinutes(-5).ToString('yyyy-MM-ddTHH:mm:ssZ')
  $write = { param($times)
    Write-Utf8 (Join-Path $Folder 'requests.json') (@{ v = 1; requests = @(); books = @()
        boxes = @{ transcribe = @($GEMINI); review = @(); times = $times } } | ConvertTo-Json -Depth 6) }
  & $write @{ $GEMINI = @{ start = $later; stop = '' } }
  Same (Work 'agy') '' 'not before its start'
  Write-Status (Get-Tools) (Load-State) @() (Get-Boxes (Read-Json (Join-Path $Folder 'requests.json')))
  Check ((Read-Json (Join-Path $Folder 'helper-status.json')).chips.$GEMINI.text -like 'ينتظر وقت البدء*') 'the page says when it starts'
  & $write @{ $GEMINI = @{ start = ''; stop = $past } }
  Same (Work 'agy') '' 'not after its stop'
  & $write @{ $GEMINI = @{ start = $past; stop = $later } }
  Same (Work 'agy').kind 'readB' 'works between the two'
}

Remove-Item -LiteralPath $Tmp -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ''
Write-Host "$($script:Count - $script:Failed) of $($script:Count) tests passed$(if ($script:Failed) { ", $($script:Failed) FAILED" })"
exit $(if ($script:Failed) { 1 } else { 0 })
