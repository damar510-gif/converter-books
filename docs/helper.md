# The helper on the user's computer

A small PowerShell program on the user's Windows computer that runs the subscription
tools there (Grok Build `grok`, Antigravity `agy`, Codex `codex`) when the user asks for
it on the status page. It is optional: the converter itself never waits for it.

## No token leaves Google

The helper talks to nothing but files in `تحويل الكتب/بيانات المساعد`, which Google
Drive for desktop keeps in sync on the computer. The Apps Script never writes a token
or key there, and the helper never calls a Google or GitHub API.

| File in `بيانات المساعد` | Written by | Content |
|---|---|---|
| `install.ps1` | Apps Script (copy of `helper/install.ps1`) | The one public file (anyone with the link can read it): the install's first step. No secret. |
| `البرنامج/` | Apps Script (copy of `helper/`, hourly, only changed files) | `launcher.ps1`, `helper.ps1`. |
| `صفحات الاختبار/` | Apps Script (copy of `compare/pages` in the results branch) | The six hard test pages (`page_image`). |
| `requests.json` | Apps Script | `{v, at, requests: [{id, action, choices, label, at}], books: [{id, title, stage, unsure}]}` |
| `helper-status.json` | helper | `{v, at, version, computer, busy, handled: [ids], lines: [{text, kind, path, linkText}], actions: [...]}` |
| `probe.txt` | helper | Each tool's `--help`/`--version` and a one-word headless answer, rewritten when the helper's version or the set of tools changes: read it to fix the command lines in `helper.ps1`. |
| `نتائج/<date> <kind> <model>/` | helper | `pNNN.md` per page, `log.txt`, `meta.json`. |

## Install (once)

The status page's card «المساعد على جهازك» shows one line to paste into PowerShell. It
downloads `install.ps1` from Drive and runs it:

1. Finds the synced folder (`<drive>:\*\تحويل الكتب\بيانات المساعد`, or under the user's
   folder). If missing, installs Google Drive for desktop (`winget`) and waits while the
   user signs in.
2. Copies `launcher.ps1` to `%LOCALAPPDATA%\ConverterHelper` (with a BOM: Windows
   PowerShell 5.1 reads a file without one as ANSI) and runs it with `-Install`, which
   installs Codex if missing (`chatgpt.com/codex/install.ps1`), opens its ChatGPT sign-in,
   and adds a hidden start at every sign-in (`Startup\ConverterHelper.vbs`).

## Each minute

`launcher.ps1` loads `البرنامج/helper.ps1` (so a change in `helper/` reaches the
computer within the hour, with no step from the user) and runs one cycle: finds the
tools, probes them when needed, runs the first request it has not done, and writes
`helper-status.json`. The status page shows its `lines` and its `actions` (buttons with
choices), so new buttons need no change to `Code.gs` or `Page.html`. The Apps Script drops
a request from `requests.json` once its id is in `handled`.

## Tools

Read-only: each tool sees one page folder (`page.png` and `task.md`, the converter's
reading prompt in Arabic; the command line itself is ASCII because `.cmd` shims garble
Arabic). No shell, edits, web or permission-skipping flags. Gemini 4 Argon is never
offered for transcription, and Grok 4.7 Fast (twice the price) is left out.

## Actions

| `action` | `choices` | What it does |
|---|---|---|
| `test` | `model` = `tool\|model id`, `effort` = `low\|medium\|high` | Reads the six test pages; Claude compares the results with the checked texts (`crosscheck.compare`). |

Planned: reading a whole book and judging its unsure places with a chosen model.
