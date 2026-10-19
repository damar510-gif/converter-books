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

Only one conversion runs at a time. The workflow also declares
`concurrency: convert`, so a second dispatch waits.

## Results

The workflow writes to branch `results`, folder `<result_dir>/`:

- `status.json`: see below. It is always written, even when the conversion crashed.
- the EPUB (its name is in `status.json`), `report.md` and `text.md`.

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
  "quota_day": "2026-10-19",
  "run_id": 123,
  "run_number": 9,
  "updated_at": "2026-10-19T10:00:00Z"
}
```

| State | Meaning |
|---|---|
| `done` | Every page was read. Some may be listed in `pages_unsure`: the two readings disagreed and the difference could not be settled, or a line seems to be missing. Those pages are still in the book, and the report shows the words in question. |
| `paused` | The day's Gemini quota ran out (`pages_waiting`). Progress is saved. Start the same book again after midnight Pacific time; `quota_day` is the Pacific date the quota ran out. |
| `incomplete` | Some pages could not be read (`pages_missing`), or their check hit temporary API errors (`pages_retry`). The EPUB exists. Starting the same book again retries only those pages. |
| `failed` | The run crashed; the EPUB may be missing. |

A `status.json` whose `updated_at` is older than the dispatch belongs to an earlier
attempt and must be ignored.
