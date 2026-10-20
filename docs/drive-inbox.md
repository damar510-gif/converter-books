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
| `6- مكتمل` | Delivered books whose refinement is over (no unsure page left, or the judges stopped improving them), with their reports. |

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
| `review` | Optional. The Drive ID of the book's answers file, so the run applies the user's answers; with `:only` appended (or `:only` alone) the run only applies the answers and rebuilds the book from saved pages, with no reading, settling or judging request. |
| `helper` | Optional. The book's folder for the helper on the user's computer (`بيانات المساعد/كتب/<fileId>`), with `:transcribe` (the models in the user's box give the second reading and settle; the free model reads each page once) or `:import` (their work so far is used, the free model does the rest). See docs/helper-boxes-plan.md. |
| `judge` | Optional. The Drive ID of `بيانات المساعد/أحكام/<fileId>.json`, the helper models' verdicts on the unsure places. The run adds them to `review/judge-answers.json` and applies them like the user's answers (the user's own answer wins). |

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
- `review/judge-answers.json` (optional): answers a judge model gave when the user asked
  for one, in the same form as the user's; every run applies them, and the user's own
  answer to the same place wins.
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
| `paused` + `"waiting_for": "judges"` | Every page is read and checked, but some unsure pages (`pages_judging`) still wait for a judge whose quota ran out. The script parks the book in «2- قيد التحويل», starts the next book, and runs this one again first after midnight Pacific time (`quota_day`). |
| `paused` + `"waiting_for": "reader"` | One free reader's daily quota is used up and the other (`readers_left`) still has some; the pages in `pages_waiting` only need a second reading from the reader that is out (a page's two readings never come from one model). The script parks the book (`stage: 'readerWait'`), starts the next book with the reader that is left, and runs the parked books first, oldest first, after midnight Pacific time (`quota_day`). With both readers out the state is plain `paused` and the book keeps its turn. |
| `paused` + `"waiting_for": "helper"` | Read once by the free model; the pages in `pages_helper` wait for the helper's models (second reading and settling). The script parks the book (`stage: 'helper'`) and starts the next one; it runs this one again when the helper reports its pages finished, or with `:import` when the user empties the box, the computer is away for an hour, or the helper has not moved for six hours. |
| `incomplete` | Some pages could not be read (`pages_missing`), or their check hit temporary API errors (`pages_retry`). The EPUB exists. Starting the same book again retries only those pages. |
| `failed` | The run crashed; the EPUB may be missing. |

A `status.json` whose `updated_at` is older than the dispatch belongs to an earlier
attempt and must be ignored.

`helper_checked` counts the pages checked against a second reading from the helper's
models. A run whose verdicts file came with `judge` writes the merged
`review/judge-answers.json`.

`pages_human` lists the pages the user answered about; `review_items` counts the places
still open, and `review_applied` / `review_kept` the corrections and confirmations this
run applied. `review_conflicts_new` counts answers saved after the previous run that
could not be applied because their page changed; their places come back as new ones.
