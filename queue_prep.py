"""The other books of a run (the workflow's "queue" input, sent by the Drive inbox script):
the oldest unfinished books after the run's own, whose pages go through the same steps
(converter.py --queue). See docs/drive-inbox.md, "The run's books".

  prepare            download each book from Drive, restore its progress and review state,
                     and write queue/manifest.json for the converter
  stash              after the conversion, move their progress out of work/ (so the run's own
                     book's cache and permanent copy hold only its own)
  upload             their progress to the "progress" release, one file per book
  save <results>     their results into a clone of the results branch

The Drive token is read from the event file and never printed (the workflow masks it)."""

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

ID = re.compile(r"^[A-Za-z0-9_-]{10,100}$")
HELPER = re.compile(r"^[A-Za-z0-9_-]{10,100}:((read1|read2|settle)(\+(read1|read2|settle))*|transcribe|read|import)$")
ORDER = re.compile(r"^[0-9T:.Z-]{4,30}$")
# A book's folder in the results branch ("drive-<file id>"), and its progress folder.
RESULT_DIR = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
MAX_BOOKS = 6
ROOT = "queue"
CACHE_VERSION = "v1"


def event():
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f) or {}).get("inputs") or {}
    except (OSError, ValueError):
        return {}


def books_input(inputs):
    """[{id, name, dir, title, review, helper, judge, order}] from the "queue" input, checked;
    and the run's own book's order."""
    try:
        data = json.loads(inputs.get("queue") or "{}")
    except ValueError:
        print("::warning::The queue input is not JSON; this run converts its own book only.")
        return [], ""
    lead_order = str(data.get("order") or "") if isinstance(data, dict) else ""
    out = []
    for b in (data.get("books") if isinstance(data, dict) else None) or []:
        if not isinstance(b, dict):
            continue
        path = str(b.get("path") or "")
        if path and not b.get("id"):
            # A book in the repository (trial runs): a PDF under the workspace.
            real = os.path.realpath(path)
            if real.startswith(os.path.realpath(".") + os.sep) and real.lower().endswith(".pdf") and os.path.isfile(real):
                folder = str(b.get("dir") or "")
                if RESULT_DIR.match(folder):
                    out.append({"id": "", "path": real, "name": os.path.basename(real), "dir": folder,
                                "title": str(b.get("title") or os.path.basename(real)[:-4])[:200],
                                "review": "", "helper": "", "judge": "", "order": ""})
            continue
        if not ID.match(str(b.get("id") or "")):
            continue
        name = os.path.basename(str(b.get("name") or "book.pdf")).translate({ord(c): None for c in "\r\n*?[]\\"})
        if not name.lower().endswith(".pdf"):
            continue
        folder = str(b.get("dir") or "drive-" + b["id"])
        if not RESULT_DIR.match(folder):
            continue
        review = str(b.get("review") or "").split(":")[0]
        out.append({"id": b["id"], "name": name, "dir": folder, "title": str(b.get("title") or name[:-4])[:200],
                    "review": review if ID.match(review) else "",
                    "helper": str(b.get("helper") or "") if HELPER.match(str(b.get("helper") or "")) else "",
                    "judge": str(b.get("judge") or "") if ID.match(str(b.get("judge") or "")) else "",
                    "order": str(b.get("order") or "") if ORDER.match(str(b.get("order") or "")) else ""})
        if len(out) >= MAX_BOOKS:
            break
    return out, lead_order if ORDER.match(lead_order) else ""


def drive_get(file_id, token, dest):
    req = urllib.request.Request(f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true",
                                 headers={"Authorization": "Bearer " + token})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            return True
        except Exception as e:
            print(f"  Drive {file_id}: {str(e)[:120]}")
            time.sleep(5 * (attempt + 1))
    return False


def sha16(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def git_show(path, dest):
    """A file of the results branch (fetched by the workflow), or False."""
    r = subprocess.run(["git", "show", f"origin/results:{path}"], capture_output=True)
    if r.returncode or not r.stdout.strip():
        return False
    with open(dest, "wb") as f:
        f.write(r.stdout)
    return True


def answers_of(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("answers") if isinstance(data.get("answers"), dict) else None
    except (OSError, ValueError, AttributeError):
        return None


def restore_progress(book_hash):
    """The book's pages read in earlier runs, from its permanent copy (the "progress" release)."""
    if os.path.isdir(os.path.join("work", f"{book_hash}-{CACHE_VERSION}")):
        return True
    tmp = os.path.join(ROOT, "tgz")
    os.makedirs(tmp, exist_ok=True)
    r = subprocess.run(["gh", "release", "download", "progress", "--repo", os.environ.get("GITHUB_REPOSITORY", ""),
                        "-p", f"{book_hash}.tgz", "-D", tmp, "--clobber"], capture_output=True)
    path = os.path.join(tmp, f"{book_hash}.tgz")
    if r.returncode or not os.path.exists(path):
        return False
    os.makedirs("work", exist_ok=True)
    with tarfile.open(path) as t:
        # Only the book's own progress folder, whatever else the file holds.
        wanted = [m for m in t.getmembers() if m.name.lstrip("./").startswith(f"{book_hash}-{CACHE_VERSION}")
                  and not m.issym() and not m.islnk() and ".." not in m.name.split("/")]
        t.extractall("work", members=wanted, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    os.remove(path)
    return True


def prepare(lead_hash=""):
    inputs = event()
    books, lead_order = books_input(inputs)
    if lead_order and os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as f:
            f.write(f"LEAD_ORDER={lead_order}\n")
    if not books:
        return
    token = str(inputs.get("drive_token") or "")
    os.makedirs(ROOT, exist_ok=True)
    seen = {lead_hash} if lead_hash else set()
    manifest, saved = [], []
    for i, b in enumerate(books):
        din, dout = os.path.join(ROOT, f"in-{i}"), os.path.join(ROOT, f"out-{i}")
        os.makedirs(din, exist_ok=True)
        pdf = os.path.join(din, b["name"])
        if b.get("path"):
            shutil.copy(b["path"], pdf)
        elif not drive_get(b["id"], token, pdf):
            print(f"::warning::Could not download «{b['title']}» from Google Drive; it waits for the next run.")
            continue
        book_hash = sha16(pdf)
        if book_hash in seen:
            continue
        seen.add(book_hash)
        restored = restore_progress(book_hash)
        human, judge = os.path.join(din, "human.json"), os.path.join(din, "judge.json")
        git_show(f"{b['dir']}/human.json", human)
        git_show(f"{b['dir']}/review/judge-answers.json", judge)
        if answers_of(judge) is None and os.path.exists(judge):
            os.remove(judge)
        answers = os.path.join(din, "answers.json")
        if b["review"] and not (drive_get(b["review"], token, answers) and answers_of(answers) is not None):
            print(f"::warning::Could not read the answers of «{b['title']}».")
            if os.path.exists(answers):
                os.remove(answers)
        judge_save = False
        if b["judge"]:
            # The helper models' verdicts join the saved ones (the newer one on a place wins).
            got = os.path.join(din, "helper-judge.json")
            if drive_get(b["judge"], token, got) and answers_of(got) is not None:
                merged = dict(answers_of(judge) or {})
                merged.update(answers_of(got))
                with open(judge, "w", encoding="utf-8") as f:
                    json.dump({"v": 1, "answers": merged}, f, ensure_ascii=False)
                judge_save = True
            if os.path.exists(got):
                os.remove(got)
        manifest.append({"pdf": pdf, "out": dout, "title": b["title"], "helper": b["helper"], "order": b["order"],
                         "answers": answers if os.path.exists(answers) else "",
                         "judge": judge if os.path.exists(judge) else "",
                         "human": human if os.path.exists(human) else ""})
        saved.append({"dir": b["dir"], "hash": book_hash, "out": dout, "in": din, "judge_save": judge_save,
                      "epub": os.path.splitext(b["name"])[0] + ".epub"})
        print(f"Book {i + 1}: «{b['title']}» ({book_hash}), progress {'restored' if restored else 'new'}")
    with open(os.path.join(ROOT, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    with open(os.path.join(ROOT, "books.json"), "w", encoding="utf-8") as f:
        json.dump(saved, f, ensure_ascii=False, indent=1)


def saved_books():
    try:
        with open(os.path.join(ROOT, "books.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def stash():
    for b in saved_books():
        src = os.path.join("work", f"{b['hash']}-{CACHE_VERSION}")
        if os.path.isdir(src):
            dest = os.path.join(ROOT, "work", f"{b['hash']}-{CACHE_VERSION}")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.rmtree(dest, ignore_errors=True)
            shutil.move(src, dest)


def upload():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    for b in saved_books():
        folder = f"{b['hash']}-{CACHE_VERSION}"
        if not os.path.isdir(os.path.join(ROOT, "work", folder)):
            continue
        path = os.path.join(ROOT, f"{b['hash']}.tgz")
        with tarfile.open(path, "w:gz") as t:
            t.add(os.path.join(ROOT, "work", folder), arcname=folder)
        for attempt in range(3):
            if not subprocess.run(["gh", "release", "upload", "progress", path, "--repo", repo, "--clobber"]).returncode:
                break
            time.sleep(10 * (attempt + 1))
        else:
            print(f"::warning::Could not save the progress of {b['dir']}.")
        os.remove(path)


def save(clone):
    """Each book's EPUB, text, report, status and places to check, as the workflow saves the
    run's own (status.json written even when the run crashed)."""
    for b in saved_books():
        out, din = b["out"], b["in"]
        status = os.path.join(out, "status.json")
        data = {}
        try:
            with open(status, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {"state": "failed", "reason": "crash", "epub": "", "pages_total": 0, "pages_done": 0,
                    "pages_unsure": [], "pages_missing": [], "pages_waiting": []}
        data.update(run_id=int(os.environ.get("GITHUB_RUN_ID") or 0), run_number=int(os.environ.get("GITHUB_RUN_NUMBER") or 0),
                    updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        dest = os.path.join(clone, b["dir"])
        os.makedirs(dest, exist_ok=True)
        epub = os.path.join(out, b["epub"])
        if os.path.exists(epub):
            for old in os.listdir(dest):
                if old.endswith(".epub"):
                    os.remove(os.path.join(dest, old))
            shutil.copy(epub, dest)
        if os.path.exists(epub + ".md"):
            shutil.copy(epub + ".md", os.path.join(dest, "text.md"))
        if os.path.exists(os.path.join(out, "report.md")):
            shutil.copy(os.path.join(out, "report.md"), os.path.join(dest, "report.md"))
        with open(os.path.join(dest, "status.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        items = os.path.join(out, "review-out", "items.json")
        if not os.path.exists(items):
            continue
        # The places for the user to check, with their page images; only from a book finished.
        review = os.path.join(dest, "review")
        os.makedirs(review, exist_ok=True)
        rout = os.path.join(out, "review-out")
        for name in os.listdir(rout):
            if name.endswith(".jpg") and not os.path.exists(os.path.join(review, name)):
                shutil.copy(os.path.join(rout, name), review)
        shutil.copy(items, os.path.join(review, "items.json"))
        with open(items, encoding="utf-8") as f:
            keep = {p.get("img") for p in (json.load(f).get("pages") or [])}
        for name in os.listdir(review):
            if name.endswith(".jpg") and name not in keep:
                os.remove(os.path.join(review, name))
        for name, to in (("human.json", os.path.join(dest, "human.json")),
                         ("review-applied.json", os.path.join(review, "review-applied.json"))):
            if os.path.exists(os.path.join(rout, name)):
                shutil.copy(os.path.join(rout, name), to)
        if os.path.exists(os.path.join(din, "answers.json")):
            shutil.copy(os.path.join(din, "answers.json"), os.path.join(review, "answers.json"))
        if b.get("judge_save") and os.path.exists(os.path.join(din, "judge.json")):
            shutil.copy(os.path.join(din, "judge.json"), os.path.join(review, "judge-answers.json"))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "prepare":
        prepare(os.environ.get("BOOK_HASH", ""))
    elif cmd == "stash":
        stash()
    elif cmd == "upload":
        upload()
    elif cmd == "save" and len(argv) > 2:
        save(argv[2])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
