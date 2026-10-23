# The helper on the user's computer

A small PowerShell program on the user's Windows computer that runs the subscription
tools there (Grok Build `grok`, Antigravity `agy`, Codex `codex`, Claude Code `claude`) when the user asks for
it on the status page. It is optional: the converter itself never waits for it.

## No token leaves Google

The helper talks to nothing but files in `تحويل الكتب/بيانات المساعد`, which Google
Drive for desktop keeps in sync on the computer. The Apps Script never writes a token
or key there, and the helper never calls a Google or GitHub API.

| File in `بيانات المساعد` | Written by | Content |
|---|---|---|
| `install.ps1` | Apps Script (copy of `helper/install.ps1`) | The one public file (anyone with the link can read it): the install's first step. No secret. |
| `البرنامج/` | Apps Script (copy of `helper/`, each check, only changed files) | `launcher.ps1`, `helper.ps1`. |
| `صفحات الاختبار/` | Apps Script (copy of `compare/pages` in the results branch) | The six hard test pages (`page_image`). |
| `requests.json` | Apps Script | `{v, at, requests: [{id, action, choices, label, at}], books: [{id, title, stage, unsure}], reviews: [fileId]}` |
| `helper-status.json` | helper | `{v, at, version, computer, busy, handled: [ids], lines: [{text, kind, path, linkText}], actions: [...]}` |
| `live-<tool>.json` | the tool's box worker | The status page's live view of the tool's models, written whenever one starts or finishes a page (only when something changed): `{v, tool, at, computer, version, chips: {id: {state, text, page, kind, book, since, done, last: [{page, kind, result, seconds, at}]}}}`. `result`: `ok`, `same` (the readings agreed: no request), `refused`, `failed` or `quota`. Drive for desktop brings it to Drive within seconds; the page reads it every few seconds (`getLive`). |
| `probe.txt` | helper | Each tool's `--help`/`--version` and a one-word headless answer, rewritten when the helper's version or the set of tools changes: read it to fix the command lines in `helper.ps1`. |
| `نتائج/<date> <kind> <model>/` | helper (until 2026-10-21) | The trials of the six test pages: `pNNN.md` per page, `log.txt`, `meta.json`, and `مقارنة.md`. The trial button is gone. |
| `كتب/<fileId>/` | Apps Script makes it; the run and the helper fill it | Transcription by the models in the boxes: see "Boxes" below. |
| `مراجعة/<fileId>/` | Apps Script (copy of `drive-<fileId>/review/` in the results branch) | `items.json` and the place images; the helper adds `j-<model>-pNNNN.json` (each model's verdicts). Apps Script also writes `answered.json` (`{v, at, ids}`, each check when it changes): the places the user answered (saved, sent or not) that no run carried yet; the models skip them. The other way round, the review screen hides the places in `أحكام/<fileId>.json` that no run carried yet. |
| `أحكام/<fileId>.json` | helper | The models' verdicts as answers (the user's answer form), kept and added to; `done` when no model in the box has anything left to judge. |

`requests.json` also carries `boxes: {transcribe: [ids], review: [ids]}` (ids are
`<tool>|<model>`), and `helper-status.json` also carries `models` (every model of the tools
found: `{id, label, tool, warn}`), `chips` (`{id: {state: working|idle|exhausted|failing, text, page, kind, since, done, last}}`
for the models in the boxes: the page each is on, how many it finished and its last jobs) and `books` (`{fileId: {need, finished, book_at}}`, the helper's
work on each book's pages). `reviews` in `requests.json` lists the books open for review (a book the
user deleted is left out): the helper reviews only those folders of `مراجعة/`, even while
Drive for desktop has not yet removed a folder from the computer, and drops a verdict on a book
that left the list while it was being judged.

## Install (once)

The status page's card «المساعد على جهازك» shows one line to paste into PowerShell. It
downloads `install.ps1` from Drive and runs it:

1. Finds the synced folder (`<drive>:\*\تحويل الكتب\بيانات المساعد`, or under the user's
   folder). If missing, installs Google Drive for desktop (`winget`) and waits while the
   user signs in.
2. Copies `launcher.ps1` to `%LOCALAPPDATA%\ConverterHelper` (with a BOM: Windows
   PowerShell 5.1 reads a file without one as ANSI) and runs it with `-Install`, which
   adds a hidden start at every sign-in (`Startup\ConverterHelper.vbs`).

The tools are installed and signed in by the user beforehand, separately (the user's
choice); the helper uses whichever it finds, and finds tools added later too.

## Each minute

`launcher.ps1` loads `البرنامج/helper.ps1` (so a change in `helper/` reaches the
computer within about ten minutes, with no step from the user) and runs one cycle: finds the
tools, probes them when needed, starts each new request in its own hidden PowerShell
(`worker.ps1`, files in `%LOCALAPPDATA%\ConverterHelper\jobs`), so different tools work
at the same time while one tool takes one request at a time (its requests share its
limits), files the finished ones, and writes
`helper-status.json`. The status page shows its `lines` and its `actions` (buttons with
choices), so new buttons need no change to `Code.gs` or `Page.html`. The Apps Script drops
a request from `requests.json` once its id is in `handled`.

## Tools

Read-only: each tool sees one page folder (`page.png` and `task.md`, the converter's
reading prompt in Arabic; the command line itself is ASCII because `.cmd` shims garble
Arabic). No shell, edits, web or permission-skipping flags. Every model is offered and
the user chooses; the labels warn that Gemini 4 Argon is not for transcribing (it uses up
the limits) and that Grok 4.7 Fast costs twice as much.

## Actions

None for now: the trial of the six test pages (`test`) was removed on 2026-10-21, once the
user had chosen the models. A new kind of request is a function in `Invoke-Request` and a
button in `actions` (Write-Status).

## Boxes (docs/helper-boxes-plan.md)

The user moves models into the left box of «التفريغ» or «مراجعة المشكوك فيه» on the status
page (`setHelperBoxes`). Each cycle starts one hidden worker per tool with models in a box
(`Invoke-BoxWorker`, `jobs/box-<tool>.json`); it takes jobs one by one, the tool's models
in turn, and hands over to a fresh copy of the program after half an hour. A job is claimed
with a file in `%LOCALAPPDATA%\ConverterHelper\claims`, so two workers never take the same.

**Transcription** (`كتب/<fileId>/`). The conversion run writes `book.json` (`need`: the
pages left to the helper, `need_a`: those without a first reading it could settle on,
`lite_out`), `pNNNN.jpg` and `pNNNN.a.json` (the free model's reading, with `missing`: lines
Tesseract saw that it may have dropped). The helper writes `pNNNN.hb.json` (the second
reading), `pNNNN.final.json` (the settled text with `a_sha`/`b_sha`, sha1 of the two
readings, first 12 hex), `pNNNN.ha.json` (a first reading: only when the free model ran out
and another model in the box can give the second) and `pNNNN.skip.json` (every model in the
box refused the page: the free model takes it). Jobs in order: settle, second reading,
first reading; the two readings of a page always come from two models. Readings run at low
effort, settling and judging at medium. Agreeing readings get `final.json` with no request.

**Review** (`مراجعة/<fileId>/`). A model judges a page's places from the image (with the
numbered frames) and `task.md`, answering a JSON array (`keep`, `fix` with `text`, or
`unsure`). Each cycle turns the verdicts into answers: all models agree, and a risky
correction (inserting a line, or changing more than six words) needs two of them. Places
the models disagree on stay for the user; whole-page places are not offered to models.

A tool's error naming its limit (429, quota, usage limit…) rests the model two hours,
doubling each time (at most 12); a page a model refused, or failed twice, goes to another.
Each model's state is in `chips-<tool>.json` and refused pages in `refused/<fileId>/`.
