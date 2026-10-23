"""What a run is doing, moment by moment, for the status page's live view (docs/drive-inbox.md,
"Live view").

The Drive inbox script makes one small file in the user's Drive and sends its id with the
dispatch (the `live` input). The run writes what it is doing into it every two seconds or so
while something changed, with its own short-lived Drive token, and the status page reads it
every few seconds. Nothing secret goes in it: model names, page numbers, counts and times.

Also a command line for the workflow's own steps, before and after the converter:

    python live.py mark setup                        the run has started
    python live.py mark saved --status status.json   its results are saved

Both read LIVE_FILE and DRIVE_TOKEN from the environment.
"""
import argparse
import collections
import contextlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

UPLOAD = "https://www.googleapis.com/upload/drive/v3/files/"
FILES = "https://www.googleapis.com/drive/v3/files/"
TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
FILE_ID = re.compile(r"^[A-Za-z0-9_-]{10,100}$")
# The last state the converter wrote, for the workflow's own "saved" mark after it.
LAST = "live-last.json"
EVENTS = 80
# What a model's daily quota is when Google has not said yet (the user's free tier).
LIMITS = (("flash-lite", 500),)
# How often the run looks at the status page's requests (Live.watch), in seconds.
CONTROL_EVERY = 10
MODEL_ID = re.compile(r"^(gemini|gemma)-[a-z0-9.-]{1,60}$")
# The boxes the page sends to a run going (Live.watch): the steps of a book's pages.
WORK_BOXES = ("read1", "read2", "settle")


def iso(t=None):
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- what the current thread does

_CTX = threading.local()


@contextlib.contextmanager
def doing(page, kind):
    """The page and step the requests made inside this block are for: kind is "a" (first
    reading), "b" (second reading) or "settle"."""
    old = getattr(_CTX, "what", None)
    _CTX.what = (int(page or 0), str(kind or ""))
    try:
        yield
    finally:
        _CTX.what = old


def now_doing():
    return getattr(_CTX, "what", None) or (0, "")


@contextlib.contextmanager
def in_book(index):
    """The book of the run (its place in it) that the work inside this block is for."""
    old = getattr(_CTX, "book", 0)
    _CTX.book = int(index or 0)
    try:
        yield
    finally:
        _CTX.book = old


def current_book():
    return getattr(_CTX, "book", 0)


def took(seconds=None):
    """How long the current thread's last request took (set by the model pool)."""
    if seconds is not None:
        _CTX.took = seconds
    return getattr(_CTX, "took", 0.0)


# ---------------------------------------------------------------- the Drive file


class DriveFile:
    """The live file in the user's Drive, written whole each time."""

    def __init__(self, file_id, token, opener=None):
        self.id = file_id
        self.token = token
        self.open = opener or urllib.request.urlopen
        # The token was refused (it expired): no more writes in this run.
        self.dead = False
        self.fails = 0

    def _call(self, method, url, data=None):
        headers = {"Authorization": "Bearer " + self.token}
        if data is not None:
            headers["Content-Type"] = "application/json; charset=UTF-8"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with self.open(req, timeout=15) as r:
            return r.read()

    def read(self):
        """The file's JSON as the Drive inbox script left it, or {}."""
        if self.dead:
            return {}
        try:
            body = self._call("GET", FILES + self.id + "?alt=media&supportsAllDrives=true")
            data = json.loads(body.decode("utf-8")) if body else {}
            return data if isinstance(data, dict) else {}
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                self.dead = e.code == 401
            return {}
        except (urllib.error.URLError, OSError, ValueError):
            return {}

    def write(self, data):
        """True when the file was written. A refused token (401) stops all writes; a passing
        error only skips this one (the next write carries everything)."""
        if self.dead:
            return False
        try:
            self._call("PATCH", UPLOAD + self.id + "?uploadType=media&supportsAllDrives=true&fields=id", data)
            self.fails = 0
            return True
        except urllib.error.HTTPError as e:
            # 404: the user deleted the file; nothing more to write to.
            if e.code in (401, 404):
                self.dead = True
            self.fails += 1
            return False
        except (urllib.error.URLError, OSError):
            self.fails += 1
            return False


def token_seconds_left(token, opener=None):
    """How long the Drive token still works, from Google (None when it cannot say)."""
    if not token:
        return None
    url = TOKENINFO + "?" + urllib.parse.urlencode({"access_token": token})
    try:
        with (opener or urllib.request.urlopen)(urllib.request.Request(url), timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        left = int(data.get("expires_in"))
        return left if left >= 0 else None
    except Exception:
        return None


def quota_limit(message):
    """The daily limit a 429 from Gemini names ("limit: 20" or quotaValue '500'), or None."""
    m = re.search(r"quotaValue['\"]?\s*:\s*['\"]?(\d+)", message) or re.search(r"\blimit:\s*(\d+)", message)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- the run's state


class Live:
    """Everything the status page shows about a run. All methods are cheap and thread safe;
    with no file (sink None: a run started without the live view) nothing is ever written."""

    def __init__(self, sink=None, clock=time.time, every=2.0, keep=None):
        self.sink = sink
        self.clock = clock
        self.every = every
        # Where the last state is kept for the workflow's "saved" mark (None: not kept).
        self.keep = keep
        self.lock = threading.RLock()
        self.base = {}
        self.models = collections.OrderedDict()
        self.events = collections.deque(maxlen=EVENTS)
        self.event_no = 0
        self.seq = 0
        # Each page's state, by (book, page): a run may work on several books (set_books).
        self.pages = {}
        self.books = []
        self.inflight = collections.Counter()
        self.phase = "start"
        self.info = {}
        self.stops = {}
        self.end = None
        self.started = iso(clock())
        self.dirty = True
        self.last_write = 0.0
        self.thread = None
        self.stopping = threading.Event()
        # The status page's requests during the run (watch): a file the Drive inbox script
        # writes, {"retry": {model: when}, "boxes": {...}, "helper": {...}, "boxes_at": when},
        # read every CONTROL_EVERY seconds. boxes_at: when the boxes this run took were saved.
        self.control = None
        self.on_retry = None
        self.on_boxes = None
        self.retries = {}
        self.boxes_seen = ""
        self.boxes_at = ""
        self.control_at = 0.0

    @property
    def on(self):
        return self.sink is not None

    # --- models ---

    def _model(self, name):
        m = self.models.get(name)
        if m is None:
            m = {"role": "", "busy": [], "queued": 0, "ok": 0, "err": 0, "rl": 0, "out": False,
                 "out_at": "", "missing": False, "rest": 0, "limit": 0, "last": []}
            for part, n in LIMITS:
                if part in name:
                    m["limit"] = n
            self.models[name] = m
        return m

    def role(self, name, role):
        if not name:
            return
        with self.lock:
            m = self._model(name)
            if not m["role"]:
                m["role"] = role
            self.dirty = True

    def begin(self, name, page=0, kind=""):
        """A request is about to wait for its turn (the per-minute pace); returns its handle."""
        with self.lock:
            m = self._model(name)
            m["queued"] += 1
            self.dirty = True
            return [name, int(page or 0), kind, 0.0, current_book()]

    def sent(self, h):
        """The request has left for Google."""
        with self.lock:
            m = self._model(h[0])
            m["queued"] = max(0, m["queued"] - 1)
            h[3] = self.clock()
            m["busy"].append(h)
            if h[1]:
                self.inflight[(h[4], h[1])] += 1
            self.dirty = True

    def done(self, h, answered):
        """The request came back (answered: a reply, not an error)."""
        with self.lock:
            m = self._model(h[0])
            if h in m["busy"]:
                m["busy"].remove(h)
                if h[1]:
                    key = (h[4], h[1])
                    self.inflight[key] -= 1
                    if self.inflight[key] <= 0:
                        del self.inflight[key]
            elif not h[3]:
                # Never sent (it failed while waiting for its turn).
                m["queued"] = max(0, m["queued"] - 1)
            if answered:
                m["ok"] += 1
            self.dirty = True

    def note(self, name, result, page=0, kind="", secs=None, n=None, extra=""):
        """One thing that happened, shown in the page's feed: a model's answer on a page
        (result "ok", "empty", "short", "refused", "settled"…), a model told to slow down
        ("rest"), out for the day ("out"), and so on (docs/drive-inbox.md)."""
        with self.lock:
            now = self.clock()
            # The user's own models ("<tool>|<model>") have their own place on the page.
            if name and "|" not in name:
                m = self._model(name)
                if result == "rest":
                    m["rl"] += 1
                    m["rest"] = int(now + (secs or 0))
                elif result == "out":
                    m["out"] = True
                    m["out_at"] = iso(now)
                    if n:
                        m["limit"] = int(n)
                elif result == "missing":
                    m["missing"] = True
                elif result in ("error", "retry"):
                    m["err"] += 1
                if result not in ("rest", "retry"):
                    m["last"] = ([[result, int(page or 0), kind, round(secs, 1) if secs is not None else None, int(now)]]
                                 + m["last"])[:4]
            self.event_no += 1
            ev = {"i": self.event_no, "t": int(now), "m": name or "", "p": int(page or 0), "k": kind, "r": result}
            if secs is not None:
                ev["s"] = round(float(secs), 1)
            if n is not None:
                ev["n"] = int(n)
            if extra:
                ev["x"] = str(extra)[:120]
            if len(self.books) > 1:
                # Which book of the run (set_books) the event is about.
                ev["b"] = current_book()
            self.events.append(ev)
            self.dirty = True

    def revived(self, name):
        """The user sent this model back to work during the run (the status page)."""
        with self.lock:
            m = self._model(name)
            m["out"] = False
            m["out_at"] = ""
            m["rest"] = 0
            self.dirty = True
        self.note(name, "back")

    # --- pages and the run ---

    def page(self, page, state, book=None):
        """A page's state now: done, unsure, read2 (both readings, not settled), read1,
        unread, failed or helper (its next step is with the user's own models). book: its
        place in the run (the current thread's book by default, in_book)."""
        key = (current_book() if book is None else int(book), page)
        with self.lock:
            if self.pages.get(key) != state:
                self.pages[key] = state
                self.dirty = True

    def set_books(self, books):
        """The run's books, in its order ([{file, book}]): their pages are shown apart."""
        with self.lock:
            self.books = [dict(b) for b in books]
            self.dirty = True

    def set_phase(self, phase, **info):
        with self.lock:
            changed = phase != self.phase
            self.phase = phase
            self.info = {k: v for k, v in info.items() if v is not None}
            self.dirty = True
        if changed:
            self.note("", "phase", kind="run", extra=phase)

    def set_stops(self, read=None, settle=None):
        """No reading starts after read, and no settling after settle (epoch seconds)."""
        with self.lock:
            self.stops = {k: v for k, v in (("read", read), ("settle", settle)) if v}
            self.dirty = True

    def finish(self, state, **info):
        """The converter's work is over: state is the book's (done, paused, time, helper…)."""
        with self.lock:
            self.end = {"state": state, **{k: v for k, v in info.items() if v is not None}}
            self.phase = "end"
            self.dirty = True
        self.note("", "end", kind="run", extra=state)
        self.stop()

    def snapshot(self):
        with self.lock:
            now = self.clock()
            counts = collections.Counter(self.pages.values())
            multi = len(self.books) > 1
            models = {}
            for name, m in self.models.items():
                models[name] = {
                    "role": m["role"], "queued": m["queued"], "ok": m["ok"], "err": m["err"], "rl": m["rl"],
                    "out": m["out"], "out_at": m["out_at"], "missing": m["missing"], "limit": m["limit"],
                    "rest": iso(m["rest"]) if m["rest"] > now else "",
                    "busy": [[h[1], h[2], int(h[3])] + ([h[4]] if multi else []) for h in m["busy"]][:8],
                    "last": m["last"]}
            self.seq += 1
            data = dict(self.base)
            data.update({
                "v": 1, "seq": self.seq, "at": iso(now), "started": self.started,
                "run": os.environ.get("GITHUB_RUN_ID", ""), "run_n": os.environ.get("GITHUB_RUN_NUMBER", ""),
                "phase": self.phase, "info": self.info,
                "stops": {k: iso(v) for k, v in self.stops.items()},
                "pages": self._pages(counts, len(self.pages), 0 if multi else None),
                "models": models, "events": list(self.events)})
            if multi:
                data["books"] = []
                for i, b in enumerate(self.books):
                    mine = collections.Counter(v for (bk, _), v in self.pages.items() if bk == i)
                    data["books"].append({**b, "pages": self._pages(mine, sum(mine.values()), i)})
            if self.end:
                data["end"] = self.end
            self.dirty = False
            return data

    def _pages(self, counts, total, book):
        """Pages by state, and those being read or settled now (of one book; None: any)."""
        now = sorted(p for (bk, p) in self.inflight if book is None or bk == book)[:12]
        return {"total": total, **{k: counts.get(k, 0) for k in
                ("done", "unsure", "read2", "read1", "unread", "failed", "helper")}, "now": now}

    # --- writing ---

    def flush(self, force=False):
        """Writes the state now if something changed (or when force)."""
        if not self.on or (not self.dirty and not force):
            return False
        data = json.dumps(self.snapshot(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.last_write = self.clock()
        if self.keep:
            try:
                with open(self.keep, "wb") as f:
                    f.write(data)
            except OSError:
                pass
        return bool(self.sink.write(data))

    def watch(self, on_retry, on_boxes=None):
        """on_retry(model) is called for each «أعد المحاولة الآن» pressed on the status page
        after the run was dispatched; on_boxes(free, helper) when the user changed the boxes
        after it: free = {box: [free models]}, helper = {box: the user's own models work
        there} (None from a script that does not say). on_boxes returns True when the run
        took them (boxes_at). Both come from the requests file, when open_live was given one."""
        self.on_retry = on_retry
        self.on_boxes = on_boxes

    def check_control(self):
        """Reads the requests file and acts on what is new. A request made before the run was
        dispatched is skipped: the run was started knowing it (the script's list of models out
        of quota today leaves that model out, and the boxes it sent are the ones saved)."""
        if not self.control or not (self.on_retry or self.on_boxes):
            return
        self.control_at = self.clock()
        data = self.control.read()
        if not isinstance(data, dict):
            return
        since = str(self.base.get("dispatched") or "")
        boxes, at = data.get("boxes"), str(data.get("boxes_at") or "")
        if self.on_boxes and isinstance(boxes, dict) and at and at != self.boxes_seen:
            self.boxes_seen = at
            if at > since:
                free = {k: [str(m) for m in boxes[k][:24] if MODEL_ID.match(str(m))]
                        for k in WORK_BOXES if isinstance(boxes.get(k), list)}
                helper = data.get("helper")
                helper = {k: bool(helper.get(k)) for k in WORK_BOXES} if isinstance(helper, dict) else None
                try:
                    if self.on_boxes(free, helper):
                        self.boxes_at = at
                except Exception as e:  # never stops the conversion
                    print(f"  live view: boxes: {str(e)[:120]}")
        retry = data.get("retry")
        if not self.on_retry or not isinstance(retry, dict):
            return
        for model, at in list(retry.items())[:40]:
            model, at = str(model), str(at)
            if not MODEL_ID.match(model) or self.retries.get(model) == at:
                continue
            self.retries[model] = at
            if at <= since:
                continue
            try:
                self.on_retry(model)
            except Exception as e:  # never stops the conversion
                print(f"  live view: retry {model}: {str(e)[:120]}")

    def start(self):
        """Writes in the background while something changed, at most every `every` seconds."""
        if not self.on or self.thread:
            return
        old = self.sink.read() if hasattr(self.sink, "read") else {}
        # What the Drive inbox script wrote at the dispatch (which book, when) stays.
        self.base = {k: old[k] for k in ("file", "book", "dispatched", "round") if k in old}

        def run():
            while not self.stopping.wait(0.5):
                if self.dirty and self.clock() - self.last_write >= self.every:
                    try:
                        self.flush()
                    except Exception as e:  # never stops the conversion
                        print(f"  live view: {str(e)[:120]}")
                if self.control and self.clock() - self.control_at >= CONTROL_EVERY:
                    try:
                        self.check_control()
                    except Exception as e:
                        print(f"  live view: {str(e)[:120]}")

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self.flush(force=True)

    def stop(self):
        """The last state is written before returning."""
        if self.thread:
            self.stopping.set()
            self.thread.join(timeout=10)
            self.thread = None
        if self.on:
            try:
                self.flush(force=True)
            except Exception as e:
                print(f"  live view: {str(e)[:120]}")


NULL = Live(None)


def open_live(file_id, token, control_id=""):
    """The run's live view, or a silent one when it has no file or no token. control_id: the
    status page's requests file (Live.watch), when the Drive inbox script sent one."""
    if not file_id or not FILE_ID.match(file_id) or not token:
        return Live(None)
    lv = Live(DriveFile(file_id, token), keep=LAST)
    if control_id and FILE_ID.match(control_id):
        lv.control = DriveFile(control_id, token)
    return lv


# ---------------------------------------------------------------- the workflow's marks


def mark(phase, status_path="", env=None, opener=None, clock=time.time):
    """The workflow's own news, before the converter starts ("setup") and once the results
    are saved ("saved", with the book's state from status.json). Returns True when written."""
    env = os.environ if env is None else env
    file_id, token = env.get("LIVE_FILE", ""), env.get("DRIVE_TOKEN", "")
    if not FILE_ID.match(file_id or "") or not token:
        return False
    drive = DriveFile(file_id, token, opener)
    data = {}
    if phase != "setup" and os.path.exists(LAST):
        try:
            with open(LAST, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
    now_there = drive.read()
    if data and now_there.get("dispatched") and now_there.get("dispatched") != data.get("dispatched"):
        # The script has already started the next run (and said so in the file): ours is old news.
        return False
    if not data:
        data = now_there
        if phase == "setup":
            data = {k: data[k] for k in ("file", "book", "dispatched", "round") if k in data}
    now = clock()
    data.update(v=1, phase=phase, at=iso(now), seq=int(data.get("seq") or 0) + 1,
                run=env.get("GITHUB_RUN_ID", ""), run_n=env.get("GITHUB_RUN_NUMBER", ""))
    if phase == "setup":
        data["started"] = iso(now)
    if status_path and os.path.exists(status_path):
        try:
            with open(status_path, encoding="utf-8") as f:
                s = json.load(f)
            data["end"] = dict(data.get("end") or {}, state=s.get("state", ""), waiting_for=s.get("waiting_for", ""),
                               pages_total=s.get("pages_total", 0), pages_done=s.get("pages_done", 0))
        except (OSError, ValueError):
            pass
    return drive.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="The status page's live view: the workflow's own marks")
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mark")
    m.add_argument("phase", choices=("setup", "saved"))
    m.add_argument("--status", default="")
    args = parser.parse_args(argv)
    ok = mark(args.phase, args.status)
    print("live view: " + (args.phase + " written" if ok else "not written"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
