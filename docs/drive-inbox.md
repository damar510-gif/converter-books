# Drive inbox: how the Google Apps Script and the GitHub workflow talk

The user drops a book into a Google Drive folder. A Google Apps Script that runs in
the user's own Google account starts the conversion on GitHub Actions and files the
EPUB when it is ready. GitHub never holds a long-lived Google credential.

## Drive folders

Created by `setup()` under one root folder `تحويل الكتب`:

| Folder | Meaning |
|---|---|
| `1- جديد` | The user drops books here (`.pdf`, `.docx`, `.txt`). |
| `2- قيد التحويل` | The book currently being converted (one at a time). |
| `3- جاهز` | Finished EPUBs, with their reports. |
| `4- الأصل` | Original files of finished books. |
| `5- مشكلة` | Books that failed, each with a short note. |
| `6- مكتمل` | Delivered books with no unsure page left (after the user's review or their own models' verdicts), with their reports. |

The answers the user gives on the status page (see "Results" below) are kept in
`تحويل الكتب/بيانات المراجعة/review-<fileId>.json`, created on first use.

## Starting a conversion

`POST /repos/{repo}/actions/workflows/convert.yml/dispatches`, with
`{"ref": <branch>, "inputs": {...}}`:

| Input | Value |
|---|---|
| `drive_file_id` | Drive file ID of the book. |
| `drive_token` | `ScriptApp.getOAuthToken()`, valid for about an hour. The workflow reads it from the event payload and masks it, so it never appears in logs. |
| `drive_file_name` | Original file name (keeps the extension). |
| `result_dir` | `drive-<fileId>`. |
| `title` | Book title (the file name without its extension). |
| `review` | Optional. The Drive ID of the book's answers file, so the run applies the user's answers; with `:only` appended (or `:only` alone) the run only applies the answers and rebuilds the book from saved pages, with no reading or settling request. |
| `helper` | Optional. The book's folder for the helper on the user's computer (`بيانات المساعد/كتب/<fileId>`), with what the user's models do in the run: the boxes of the board they are in, `:read1`, `:read2`, `:settle` joined with `+` (older scripts: `:transcribe`, `:read`, `:settle`), or `:import` (their work so far is used, the free models do the rest). The free models and the user's share a step both can take: the free models take the later half of those pages, the user's the earlier half; a step only one side can take is that side's. In a free models' review round (`:only`): the book's review folder `بيانات المساعد/مراجعة/<fileId>` with `:review` (see "The free models' review" below). See docs/helper-boxes-plan.md. |
| `judge` | Optional. The Drive ID of `بيانات المساعد/أحكام/<fileId>.json`, the helper models' verdicts on the unsure places. The run adds them to `review/judge-answers.json` and applies them like the user's answers (the user's own answer wins). |
| `live` | Optional. The Drive ID of `تحويل الكتب/بيانات البث/live.json`, the live view's file (see "Live view" below), with `:<Drive ID>` of `بيانات البث/control.json`, the status page's requests to the run (`{"v": 1, "retry": {model: when}}`: «أعد المحاولة الآن» on a free model; the run reads it every 10 seconds and puts a model asked for after its dispatch back to work). With it, the run writes what it does there every two seconds or so, and with the free models alone it starts no page work after its Drive token would run out (state `paused` + `waiting_for: "time"`). |
| `models` | Sent with every run since 2026-10-23: the free models in the board's boxes (Script Property `MODEL_BOXES`, which holds the user's models too; before the user changes the board, what the two older boards held, or the converter's own), `read1=a,b;read2=c;settle=d,e;review=f`, and `;out=g,h`: the models a run found out of quota on the current Pacific day (Script Property `USAGE`), which the run does not ask again. Only the models of a box do its step, all of them side by side (each page goes to one that has no request in flight, in turn; no order between them): `read1` the first reading, `read2` the second (never the model that gave the page's first), `settle` the settling, `review` the unsure places of delivered books (freereview.py). An empty box means nobody does that step, and no model works outside the boxes (Gemma reads a refused page only from a box). One model may be in several boxes. Without the input (an older script) the converter keeps its own roles (the two Lite readers read, each first in its own reading and the other after it; the Flash models settle, then the Lite readers; Gemma for refused pages). The workflow passes it on only when it matches that form (`BOXES_RE`). |

Only one conversion runs at a time. The workflow also declares
`concurrency: convert`, so a second dispatch waits.

## Results

The workflow writes to branch `results`, folder `<result_dir>/`:

- `status.json`: see below. It is always written, even when the conversion crashed.
- the EPUB (its name is in `status.json`), `report.md` and `text.md`.
- `human.json`: the pages the user answered about, with their text. The next run
  restores it from here, and those pages never change again.
- `review/items.json`: the places on unsure pages still open for the user to check,
  with one JPEG per page (`review/pNNNN-<hash>.jpg`, the places framed and numbered;
  boxes are in the JPEG's pixels). `answers_seen` is the time of the latest answer the
  run used: an answer saved later hides its place until the next run.
- `review/judge-answers.json` (optional): the verdicts of the user's own models (the
  helper's review box), in the same form as the user's answers; every run applies them,
  and the user's own answer to the same place wins.
- `review/review-applied.json` (what the run did with each answer: `kept`, `applied`,
  `already` or `conflict`) and `review/answers.json` (the answers file it used).

`items.json`:

```json
{"v": 1, "book": "title", "book_hash": "<16 hex>", "generated_at": "…Z", "answers_seen": "…Z",
 "pages": [{"page": 19, "base": "<12 hex>", "img": "p0019-<10 hex>.jpg", "w": 1182, "h": 1482, "more": 0,
   "items": [{"id": "p19-<10 hex>", "fp": "…", "kind": "diff | missing | page", "n": 1,
     "before": "…", "core": "…", "after": "…", "pos": 1234, "place": "after",
     "options": [{"label": "other | ocr", "text": "…"}],
     "boxes": [[0, 0, 10, 10]], "col": [0, 600], "located": true, "hint": "likely-ok | header | heading"}]}]}
```

An answer (in the answers file, under `answers`, keyed by the place's `id`) copies the
place's `page`, `kind`, `fp`, `before`, `core`, `after`, `pos`, `place` and the page's
`base`, and adds `verdict` (`keep`: the reading is right, or `fix`: the right text is
`text`) and `at`. "The rest of this page is right" is
`{"kind": "rest", "page": 19, "ids": [...], "fps": [...], "verdict": "keep", "at": "…"}`
under its own id (`p19-rest-<time>`).

Read the files with `GET /repos/{repo}/contents/<path>?ref=results` and the header
`Accept: application/vnd.github.raw`. A missing file returns 404.

`status.json`:

```json
{
  "state": "done | paused | incomplete | failed",
  "book": "title",
  "epub": "file name of the EPUB in the same folder, or empty",
  "pages_total": 246,
  "pages_done": 246,
  "pages_unsure": [12, 40],
  "pages_missing": [],
  "pages_waiting": [],
  "pages_retry": [],
  "pages_human": [19],
  "review_items": 74,
  "review_applied": 3,
  "review_kept": 5,
  "review_conflicts_new": 0,
  "review_seen": "2026-10-19T09:00:00Z",
  "quota_day": "2026-10-19",
  "run_id": 123,
  "run_number": 9,
  "updated_at": "2026-10-19T10:00:00Z"
}
```

| State | Meaning |
|---|---|
| `done` | Every page was read. Some may be listed in `pages_unsure`: the two readings disagreed and the difference could not be settled, or a line seems to be missing. Those pages are still in the book, and the report shows the words in question. |
| `paused` | The day's reading quota ran out (`pages_waiting`). Progress is saved. Start the same book again after midnight Pacific time; `quota_day` is the Pacific date the quota ran out. |
| `paused` + `"waiting_for": "judges"` | Older versions only (no free model judges any more): the script delivers the book; a book an older script parked for the judges runs once more and is delivered. |
| `paused` + `"waiting_for": "reader"` | Some free readers' daily quota is used up and others (`readers_left`) still have some; the pages in `pages_waiting` only need a second reading that only the readers now out could give (a page's two readings never come from one model). The script parks the book (`stage: 'readerWait'`), starts the next book with the readers left, and runs the parked books first, oldest first, after midnight Pacific time (`quota_day`). With every reader out the state is plain `paused` and the book keeps its turn. |
| `paused` + `"waiting_for": "time"` | Only for a run that got a `live` file, with the free models alone: it stopped starting page work before its Drive token ran out (the live view needs it), and the free readers still have quota (`free_out` false). The script starts the next run at once (at most `MAX_TIME_RUNS` a Pacific day; after that the book waits for the next day like plain `paused`). |
| `paused` + `"waiting_for": "boxes"` | No model in any box, free or on the user's computer, could take the next step of the pages waiting (the user emptied those boxes). The script parks the book (`stage: 'boxWait'`, no daily pause counted) and runs it again once the boxes change and someone can work. |
| `paused` + `"waiting_for": "helper"` | The pages in `pages_helper` are with the helper's models (they go from the start of the book; the free models took their share, `free_done` pages, from its end). The script parks the book (`stage: 'helper'`, shown under «الآن») and runs it again: at once when `free_left` (the free models stopped for time with quota left), on a later Pacific day than `quota_day` (their quota renewed), when the helper reports its pages finished, or with `:import` when the user empties the box, the computer is away for an hour, or the helper has not moved for six hours. |
| `incomplete` | Some pages could not be read (`pages_missing`), or their check hit temporary API errors (`pages_retry`). The EPUB exists. Starting the same book again retries only those pages. |
| `failed` | The run crashed; the EPUB may be missing. |

A `status.json` whose `updated_at` is older than the dispatch belongs to an earlier
attempt and must be ignored.

`helper_checked` counts the pages checked against a second reading from the helper's
models. A run whose verdicts file came with `judge` writes the merged
`review/judge-answers.json`.

Every run also writes `stages` (how many pages are `done`, `unsure`, read twice and waiting to
be settled `read2`, read once `read1`, `unread`, `failed`, or with the user's models `helper`),
`requests` (each free model's answered requests in this run) and `models_out` (the free models
whose daily quota the run found used up). The script keeps `stages` for the page («الآن»), and
adds `requests` and `models_out` up per Pacific day (Script Property `USAGE`, each run once).
A run that could list the account's models writes `free_models` (the free models the page
offers for its boxes: Flash-Lite, Flash and Pro from 2.5 on, Gemma from 4 on) and `free_default`
(`{read1, read2, settle}`: the converter's own boxes, shown until the user changes them); the
script keeps both (Script Property `FREE_MODELS`). A run given `models` writes the boxes it used
as `boxes`.

`pages_human` lists the pages the user answered about; `review_items` counts the places
still open, and `review_applied` / `review_kept` the corrections and confirmations this
run applied. `review_conflicts_new` counts answers saved after the previous run that
could not be applied because their page changed; their places come back as new ones.

## The free models' review

With free models in the board's review box, the script starts a review round (`:only`, no
conversion) for a delivered book with open places (`freeReviewNext_`): one their review has not
seen, after the review box changed, after the book was converted again (`convertedAt`), or the
next Pacific day when places were left for lack of quota. The run's step "Free models review the
unsure places" (freereview.py) takes the places and images from the results branch
(`review/items.json`), the verdicts of every model from the book's review folder in Drive
(`j-<chip>-pNNNN.json`, the free models' as the chip `free|<model>`, the user's models' from
their helper), and asks each free model in the box for the places it should judge, by the
helper's own rules (a place nobody judged; a risky correction, a line or more than six words,
only one model gave; nothing once models disagree). Its verdicts go to the folder, so the user's
models see them; the verdicts that become answers (all agree, two models for a risky one) join
`review/judge-answers.json`, and the round applies them (the user's own answer wins). The run
writes what it did as `free_review: {models, asked, answers, left}` in status.json; the script
keeps it on the book's review (`rv.fr`) and starts the next round only when something changed.
A round that applied nothing keeps the book as it was, without a mail; one that failed leaves the
user's own review untouched.

## Live view

What a run does, second by second, for the status page (`live.py`, `Page.html`):

1. **The file.** `تحويل الكتب/بيانات البث/live.json`, made on first use (Script Property
   `LIVE_FILE_ID`). Before each dispatch the script writes `{v, seq: 0, phase: "queued", file,
   book, dispatched, round}`, and sends the file's ID as the `live` input.
2. **The run writes it** with its own Drive token (`PATCH …/upload/drive/v3/files/<id>?uploadType=media`):
   - the workflow's first step after Python marks `phase: "setup"` (`python live.py mark setup`);
   - the converter keeps the script's `file`, `book`, `dispatched` and `round`, and writes
     `{v, seq, at, started, run, run_n, phase, info, stops, pages, models, events, end}` every two
     seconds or so while something changed;
   - once the results are pushed, the workflow marks `phase: "saved"` with the book's state from
     status.json (`python live.py mark saved --status status.json`).
   A refused token (401) stops the writes; a passing error only skips one.
3. **The page reads it** with `getLive(json)` every 2.5 seconds while a run is going (every 12
   otherwise; never while hidden or during a review). `json` is `{after, run, ev, helper}`: the
   journal lines and the run's events the page already has are left out, and `helper` asks for
   the helper's live files too. `due` is true once the run marked `saved` and no check took its
   results in yet: the page then calls `checkNow()`, so the next book (or the next run) starts
   in seconds instead of up to ten minutes. While the live file is quiet (the run setting up,
   saving, or one started without a live file) `getLive` adds the GitHub run's steps (`run:
   {id, n, status, steps: [{n, s, c, at}]}`, asked at most every 20 seconds).

`phase`: `queued`, `setup`, `start`, `apply` (a review round), `free` (the free models' share
while the user's models work on the book), `read`, `helper` (files exchanged with the user's
models), `check` (comparing and settling), `build`, `end`, `saved`.

`pages`: `{total, done, unsure, read2, read1, unread, failed, helper, now: [pages in flight]}`.

`models`: for each free model `{role (read1, read2, settle, rescue), queued (requests waiting for
their turn under the per-minute pace), busy: [[page, kind, sent at]], ok, err, rl (slow-downs
asked by Google), out, out_at, missing, limit (daily, when known), rest (until when it waits),
last: [[result, page, kind, seconds, at]]}`.

`events` (the latest 80): `{i, t, m (model, or "<tool>|<model>" for the user's), p (page), k
(a, b, settle, page, helper, run), r (result), s (seconds), n (number), x (extra)}`. Results:
`ok`, `empty`, `short`, `refused`, `error`, `rest`, `retry`, `out`, `missing` (a request);
`settled`, `unsure`, `drops`, `lines`, `noanswer` (a settling); `agreed`, `single`, `unsure`,
`wait` (a page, `n` pages waiting for a settler); `sent` (`n` pages given to the user's models);
`phase` and `end` (`x` names them).

`stops`: `{read, settle}`, when the run starts no more reading, and no more settling: with the
free models alone and a live file, eight minutes before the token runs out (asked of Google's
tokeninfo; 55 minutes guessed otherwise), the last twelve minutes of it for settling only.

## Journal

What the script did, in the user's words (Script Properties `JOURNAL`, the last 40, and
`JOURNAL_N`): `{i, t, k, x, b}` = number, time, kind (`start`, `end`, `pause`, `park`,
`deliver`, `problem`, `revive`, `gone`, `error`), text, book. `getStatus` gives the last 20 and
`getLive` the new ones; «الآن» shows them under «آخر ما حدث». `checkNow()` («افحص الآن») is one
check that returns the lines it added, or `line`: what the books wait for when nothing changed.

## Run now

`runNow(fileId)` («شغّل الآن»): the book waiting for its quota's new day, or parked (for the
helper's models or a reader) while no book holds the slot, starts a run at once. It is refused
while another run is going, or while another book holds the slot. The run's pause, if the quota
is still out, is no pause of the book's own (`MAX_PAUSES`). The run hands the user's models what
they can do (with `:settle`, the pages read twice and not settled).

## A book back from «5- مشكلة»

A book the user moves out of «5- مشكلة» (back to «1- جديد» to try again, or anywhere else) is no
problem any more: the page stops listing it among the finished books at once, and the next check
drops its job and its note («… - سبب المشكلة.txt»). In «1- جديد» it starts afresh in its turn.
