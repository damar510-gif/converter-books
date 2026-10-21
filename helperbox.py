"""The files a run exchanges with the helper on the user's computer (helper/helper.ps1),
through the book's folder in Drive: see docs/helper-boxes-plan.md and docs/helper.md.

The run's own Drive token (about an hour, from the Drive inbox script) is the only
credential, and the helper is never called directly: it works on the files.

From the run:    book.json, pNNNN.jpg (the page image) and pNNNN.a.json (the main reader's
                 text, with the lines Tesseract saw that it may have dropped).
From the helper: pNNNN.ha.json (a first reading, when the free readers cannot give one),
                 pNNNN.hb.json (the second reading), pNNNN.final.json (its settled text,
                 made on the two readings named by a_sha and b_sha) and pNNNN.skip.json
                 (no model in the box could read the page: it goes the usual way).
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import review

API = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_ID = re.compile(r"^[A-Za-z0-9_-]{10,100}$")
FROM_HELPER = re.compile(r"^p(\d{4})\.(ha|hb|final|skip)\.json$")
# The token lasts about an hour from the dispatch: nothing is sent after this long.
TOKEN_LIFE = 50 * 60
# A check state that needs nothing more from the helper.
SETTLED = ("agreed", "reconciled", "unsure", "single", "human")
EMPTY = "[[EMPTY]]"


def sha(text):
    """How the helper names the readings its settled text was made on (helper.ps1, Text-Sha)."""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DriveError(Exception):
    def __init__(self, code, message):
        super().__init__(f"Drive {code}: {message}")
        self.code = code


class Drive:
    """The few Drive REST calls the exchange needs."""

    def __init__(self, token, opener=None):
        self.token = token
        self.started = time.time()
        # The token was refused (it expired): no more requests in this run.
        self.dead = False
        self.open = opener or urllib.request.urlopen

    def call(self, method, url, data=None, ctype=None, raw=False):
        if self.dead:
            raise DriveError(401, "token expired")
        headers = {"Authorization": "Bearer " + self.token}
        if ctype:
            headers["Content-Type"] = ctype
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with self.open(req, timeout=120) as r:
                    body = r.read()
                return body if raw else (json.loads(body) if body else {})
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    self.dead = True
                    raise DriveError(401, "token expired") from e
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise DriveError(e.code, e.read()[:200].decode("utf-8", "replace")) from e
            except (urllib.error.URLError, OSError) as e:
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise DriveError(0, str(e)[:200]) from e

    def listing(self, folder):
        """{name: {"id", "modifiedTime"}} of the folder's files (the newest of two with one name)."""
        out, token = {}, ""
        while True:
            q = {"q": f"'{folder}' in parents and trashed = false", "pageSize": "1000",
                 "fields": "nextPageToken,files(id,name,modifiedTime)",
                 "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if token:
                q["pageToken"] = token
            data = self.call("GET", API + "?" + urllib.parse.urlencode(q))
            for f in data.get("files", []):
                old = out.get(f.get("name"))
                if not old or str(f.get("modifiedTime", "")) > str(old.get("modifiedTime", "")):
                    out[f["name"]] = {"id": f["id"], "modifiedTime": str(f.get("modifiedTime", ""))}
            token = data.get("nextPageToken")
            if not token:
                return out

    def get(self, file_id):
        return self.call("GET", f"{API}/{file_id}?alt=media&supportsAllDrives=true", raw=True)

    def create(self, folder, name, data, mime):
        boundary = uuid.uuid4().hex
        meta = json.dumps({"name": name, "parents": [folder]}, ensure_ascii=False)
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode("utf-8") + data + \
            f"\r\n--{boundary}--\r\n".encode("utf-8")
        got = self.call("POST", UPLOAD + "?uploadType=multipart&supportsAllDrives=true&fields=id,modifiedTime",
                        body, f"multipart/related; boundary={boundary}")
        return got.get("id", "")

    def update(self, file_id, data, mime):
        self.call("PATCH", f"{UPLOAD}/{file_id}?uploadType=media&supportsAllDrives=true&fields=id", data, mime)


class HelperBox:
    """One book's folder. spec is "<folder id>:transcribe" (the helper's models give the
    second reading and settle the page) or "<folder id>:import" (their work so far is used,
    and the free readers do the rest)."""

    def __init__(self, folder, transcribe, drive, cache_dir):
        self.folder = folder
        self.transcribe = transcribe
        self.drive = drive
        self.dir = os.path.join(cache_dir, "helper")
        os.makedirs(self.dir, exist_ok=True)
        self.files = {}
        self.lock = threading.Lock()
        self.thread = None
        self.notes = []
        self.images_sent = 0

    @classmethod
    def open(cls, spec, token, cache_dir):
        folder, _, mode = (spec or "").partition(":")
        if not FOLDER_ID.match(folder) or not token:
            print("Helper folder ignored: " + ("no Drive token" if not token else "bad folder id"))
            return None
        return cls(folder, mode == "transcribe", Drive(token), cache_dir)

    def note(self, text):
        with self.lock:
            if text not in self.notes:
                self.notes.append(text)
        print("  helper: " + text)

    def fresh(self):
        return not self.drive.dead and time.time() - self.drive.started < TOKEN_LIFE

    def fetch(self):
        """The helper's files, by page: {page: {"ha"|"hb"|"final"|"skip": {...}}}. A file
        downloaded by an earlier run (same modifiedTime) is not downloaded again."""
        listed = True
        try:
            if self.fresh():
                self.files = self.drive.listing(self.folder)
            else:
                listed = False
        except DriveError as e:
            listed = False
            self.note(f"could not list the helper folder ({e})")
        index_path = os.path.join(self.dir, "index.json")
        index = review.load(index_path) or {}
        todo = [(n, f) for n, f in self.files.items() if FROM_HELPER.match(n) and index.get(n) != f["modifiedTime"]]

        def get(item):
            name, f = item
            try:
                data = self.drive.get(f["id"])
            except DriveError as e:
                self.note(f"could not download {name} ({e})")
                return None
            with open(os.path.join(self.dir, name), "wb") as out:
                out.write(data)
            return name, f["modifiedTime"]

        with ThreadPoolExecutor(max_workers=8) as tp:
            for got in tp.map(get, todo):
                if got:
                    index[got[0]] = got[1]
        review.dump(index_path, index)
        if todo:
            print(f"Helper: {len(todo)} new files from the helper")
        out = {}
        for name in sorted(os.listdir(self.dir)):
            m = FROM_HELPER.match(name)
            # A file gone from the folder no longer counts (when the folder could be listed).
            if not m or (listed and name not in self.files):
                continue
            data = review.load(os.path.join(self.dir, name))
            if isinstance(data, dict):
                out.setdefault(int(m.group(1)), {})[m.group(2)] = data
        return out

    def start_images(self, pages, render):
        """Sends the images of these pages that the folder lacks, in the background, so the
        helper can start on them while the free model reads."""
        todo = [p for p in pages if f"p{p:04d}.jpg" not in self.files]
        if not todo or not self.fresh():
            return

        def one(p):
            if not self.fresh():
                return
            name = f"p{p:04d}.jpg"
            try:
                fid = self.drive.create(self.folder, name, render(p), "image/jpeg")
            except DriveError as e:
                self.note(f"could not send page images ({e})")
                return
            with self.lock:
                self.files[name] = {"id": fid, "modifiedTime": ""}
                self.images_sent += 1

        def run():
            with ThreadPoolExecutor(max_workers=4) as tp:
                list(tp.map(one, todo))

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def wait_images(self):
        if self.thread:
            self.thread.join()
            self.thread = None
            if self.images_sent:
                print(f"Helper: {self.images_sent} page images sent")

    def put_json(self, name, data):
        """Writes (or replaces) one JSON file in the folder. False when it could not."""
        if not self.fresh():
            return False
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            known = self.files.get(name)
            if known:
                self.drive.update(known["id"], body, "application/json")
            else:
                fid = self.drive.create(self.folder, name, body, "application/json")
                with self.lock:
                    self.files[name] = {"id": fid, "modifiedTime": ""}
            return True
        except DriveError as e:
            self.note(f"could not write {name} ({e})")
            return False

    def put_readings(self, payloads):
        """{page: payload} as pNNNN.a.json; returns the pages written."""
        def one(item):
            page, payload = item
            return page if self.put_json(f"p{page:04d}.a.json", payload) else None

        with ThreadPoolExecutor(max_workers=8) as tp:
            return {p for p in tp.map(one, sorted(payloads.items())) if p}


# ---------------------------------------------------------------- the page records


def settled(rec, check_version):
    c = (rec or {}).get("check") or {}
    return c.get("v") == check_version and c.get("state") in SETTLED


def as_reading(out):
    """A helper file's reading in the form of the record's readings. "sha" names the text
    exactly as the helper wrote it, which its settled text refers to."""
    text = str(out.get("text") or "")
    clean = text.replace("\r\n", "\n").strip()
    return {"model": str(out.get("model") or "helper"), "text": clean, "finish": "", "via": "helper",
            "status": "ok", "sha": sha(text)}


def has_text(out):
    return isinstance(out, dict) and isinstance(out.get("text"), str) and out["text"].strip() != ""


def reading_sha(reading):
    return reading.get("sha") or sha(reading.get("text", ""))


def own_pair(ha, hb, fin):
    """The helper read the page twice itself (two models) and settled on those readings."""
    return has_text(ha) and has_text(hb) and has_text(fin) and EMPTY not in (ha["text"].strip(), hb["text"].strip()) \
        and str(ha.get("model") or "") != str(hb.get("model") or "") \
        and fin.get("a_sha") == sha(ha["text"]) and fin.get("b_sha") == sha(hb["text"])


def merge(rec, outs, page_no, check_version):
    """Puts the helper's files for one page into its record: (record, changed). A page the
    user pinned, or one already settled, is left alone; so is a settled text made on other
    readings than the record's."""
    if not outs or (rec and (review.pin_of(rec) or settled(rec, check_version))):
        return rec, False
    changed = False
    ha, hb, fin = outs.get("ha"), outs.get("hb"), outs.get("final")
    if outs.get("skip") and not (rec or {}).get("hskip"):
        rec = rec or {"page": page_no, "status": "pending"}
        rec["hskip"] = True
        changed = True
    # The helper's own two readings and its settled text on them, made while the free
    # model's reading had not reached it (a run whose Drive token ran out sends its readings
    # only with the next run): a page read by two models and settled, taken as it is.
    if own_pair(ha, hb, fin) and (rec is None or reading_sha(rec.get("a") or {}) != sha(ha["text"])):
        keep = {k: v for k, v in (rec or {}).items() if k in ("hskip", "ink", "spread")}
        a, b = as_reading(ha), as_reading(hb)
        rec = {"page": page_no, "status": "ok", "text": a["text"], "a": a, "b": b, **keep,
               "hfinal": {"model": str(fin.get("model") or b["model"]), "text": fin["text"]}}
        return rec, True
    if (rec is None or rec.get("status") != "ok") and has_text(ha):
        keep = {k: v for k, v in (rec or {}).items() if k == "hskip"}
        if ha["text"].strip() == EMPTY:
            rec = {"page": page_no, "status": "empty", "text": "", **keep}
        else:
            a = as_reading(ha)
            rec = {"page": page_no, "status": "ok", "text": a["text"], "a": a, **keep}
        changed = True
    if not rec or rec.get("status") != "ok" or not rec.get("a"):
        return rec, changed
    if has_text(hb) and hb["text"].strip() != EMPTY and str(hb.get("model") or "") != rec["a"].get("model"):
        b = as_reading(hb)
        old = rec.get("b") or {}
        if old.get("model") != b["model"] or old.get("text") != b["text"]:
            rec["b"] = b
            rec.pop("check", None)
            changed = True
    b = rec.get("b") or {}
    good = has_text(fin) and b.get("via") == "helper" and fin.get("a_sha") == reading_sha(rec["a"]) \
        and fin.get("b_sha") == reading_sha(b)
    if good:
        final = {"model": str(fin.get("model") or b.get("model", "")), "text": fin["text"]}
        if rec.get("hfinal") != final:
            rec["hfinal"] = final
            rec.pop("check", None)
            changed = True
    elif "hfinal" in rec:
        del rec["hfinal"]
        changed = True
    return rec, changed


def needs_helper(rec, check_version):
    """A scanned page the helper's models can still help with (None: not read yet)."""
    if rec is None:
        return True
    if review.pin_of(rec) or rec.get("hskip") or settled(rec, check_version):
        return False
    return rec.get("status") in ("ok", "pending", "failed", "suspicious")


def needs_first_reading(rec):
    """No first reading the helper could settle on: the free readers ran out or failed."""
    return rec is None or rec.get("status") in ("pending", "failed", "suspicious")
