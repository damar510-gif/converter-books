import argparse
import collections
import hashlib
import heapq
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pymupdf
from google import genai
from google.genai import types

import crosscheck
import helperbox
import live
import review

# Bump when the OCR prompt or page format changes, so cached pages are redone.
CACHE_VERSION = "v1"
# Bump when the comparison of the two readings changes: pages are re-checked
# (and their differences settled again) without being read again.
CHECK_VERSION = 2

# A page whose text layer is shorter than this is treated as a scanned image.
MIN_TEXT_LAYER_CHARS = 50

# Pages with more dark pixels than this but almost no OCR text are suspicious.
INK_SUSPICIOUS_RATIO = 0.02
MIN_OCR_CHARS = 120

OCR_RULES = """- إن كانت الصفحة بعمودين فاقرأ العمود الأيمن كاملاً من أعلاه إلى أسفله، ثم العمود الأيسر.
{spread}- عنوان مقال أو بحث جديد (العنوان الكبير في رأس المقال) يُكتب في سطر مستقل يبدأ بـ "# ".
- العناوين الفرعية داخل المقال تُكتب في سطر مستقل يبدأ بـ "## ".
- افصل بين الفقرات بسطر فارغ، واجمع أسطر الفقرة الواحدة في سطر واحد.
- الشعر: كل بيت في سطر مستقل يبدأ بـ "| "، وبين الشطرين " … ".
- الحواشي (السطور الصغيرة أسفل الصفحة تحت الخط): في موضع رقم الحاشية داخل المتن اكتب [^1] (بنفس رقمها)، وفي آخر إخراجك اكتب كل حاشية في سطر مستقل هكذا: [^1]: نص الحاشية
- الكلمات الأجنبية تُنسخ بحروفها كما هي.
- تجاهل تماماً: أرقام الصفحات، والترويسة المكررة أعلى الصفحة، والعلامة المائية (مثل «جلجامش»)، والزخارف.
- لا تكتب أي تعليق أو شرح أو مقدمة، فقط نص الصفحة.
- إن كانت الصفحة فارغة أو صورة بلا نص فاكتب فقط: [[EMPTY]]"""

OCR_PROMPT = ("أنت تنسخ صفحة واحدة من كتاب عربي ممسوح ضوئياً. انسخ النص كما هو حرفياً بلا تلخيص ولا حذف ولا تصحيح ولا إضافة."
              "\n\nقواعد الإخراج (Markdown):\n" + OCR_RULES)

# Finish reasons where the model refused the page instead of reading it.
BLOCKED_REASONS = ("PROHIBITED_CONTENT", "SAFETY", "RECITATION", "BLOCKLIST", "SPII", "OTHER")

SAFETY_OFF = [
    types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.BLOCK_NONE)
    for c in (
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    )
]

SPREAD_HINT = "- هذه الصورة فيها صفحتان من الكتاب متجاورتان: انسخ الصفحة اليمنى كاملة أولاً ثم اليسرى.\n"


# PyMuPDF is not thread safe, even with one document per thread: every PDF call
# goes through this lock, while the slow API requests still run in parallel.
PDF_LOCK = threading.RLock()
# A page's record is saved by one reading at a time (the two reading boxes may read it at once).
RECORD_LOCK = threading.Lock()


def pdf_locked(fn):
    def wrapper(*args, **kwargs):
        with PDF_LOCK:
            return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def open_pdf(path):
    with PDF_LOCK:
        return pymupdf.open(path)


def close_pdf(doc):
    with PDF_LOCK:
        doc.close()


class QuotaExhausted(Exception):
    """The model's daily quota is used up; it is skipped for the rest of the run."""

    def __init__(self, model):
        super().__init__(f"daily quota exhausted for {model}")
        self.model = model


# A 429 naming the daily limit is believed on the third in a row, each a minute after the
# last: the model gets no other request meanwhile (one at a time), so this takes 2-3 minutes.
DAY_CHECKS = 3
DAY_CHECK_SECONDS = 60
# A 429 asking for a longer wait than this is the daily limit too, whatever quota it names: on
# 2026-10-23 one said "retry in" about 19 hours and the run slept on it until it was cancelled.
LONG_WAIT_SECONDS = 5 * 60


def quota_ids(msg):
    """The quotas a 429 names ("PerMinute", "PerDay"…), short, for the live view."""
    ids = re.findall(r"quotaId['\"]?\s*:\s*['\"]?([A-Za-z-]+)", msg)
    return ",".join(dict.fromkeys(ids))[:100]


class TimeUp(QuotaExhausted):
    """This run starts no more page work (ModelPool.stops): its Drive token, which the live
    view needs, is about to run out. The page waits for the next run, like a used-up quota."""

    def __init__(self, model):
        Exception.__init__(self, f"no more page work in this run ({model})")
        self.model = model


class Moved(QuotaExhausted):
    """The user took the model out of this step's box on the status page while its request
    waited for its turn: the page goes to the models now in the box (main's next pass)."""

    def __init__(self, model):
        Exception.__init__(self, f"{model} is no longer in this box")
        self.model = model


# The box of each kind of request (live.now_doing): first and second readings, settling.
KIND_BOX = {"a": "read1", "b": "read2", "settle": "settle"}


class RateLimiter:
    """Spaces request starts so we stay under the per-minute limit."""

    def __init__(self, per_minute):
        self.interval = 60.0 / per_minute
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            delay = self.next_at - now
            self.next_at = max(now, self.next_at) + self.interval
        if delay > 0:
            time.sleep(delay)


def apply_smart_indexing(text):
    """
    Applies smart markdown indexing (TOC) to unformatted Arabic text.
    """
    # Main headings: كتاب، باب، مقدمة، خاتمة
    text = re.sub(r'^\s*((?:ال)?كتاب|(?:ال)?باب|(?:ال)?مقدمة|(?:ال)?خاتمة)\b(.*?)$', r'# \1\2', text, flags=re.MULTILINE)

    # Subheadings: فصل، مبحث
    text = re.sub(r'^\s*((?:ال)?فصل|(?:ال)?مبحث)\b(.*?)$', r'## \1\2', text, flags=re.MULTILINE)

    return text


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def parse_pages(spec, count):
    """'1-20,25' -> [0..19, 24] (zero based). Empty spec means all pages."""
    if not spec:
        return list(range(count))
    pages = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        else:
            pages.add(int(part))
    return sorted(p - 1 for p in pages if 1 <= p <= count)


@pdf_locked
def page_image(doc, page):
    """PNG bytes of the page at its scan's native resolution (at least 200 dpi otherwise)."""
    images = page.get_images(full=True)
    if len(images) == 1:
        xref = images[0][0]
        try:
            rect = page.get_image_bbox(images[0])
            covers = rect.width >= page.rect.width * 0.9 and rect.height >= page.rect.height * 0.9
        except Exception:
            covers = False
        if covers:
            pix = pymupdf.Pixmap(doc, xref)
            if pix.alpha:
                pix = pymupdf.Pixmap(pix, 0)
            if pix.n > 3:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            if pix.width >= 1000:
                return pix.tobytes("png"), pix
    pix = page.get_pixmap(dpi=200)
    return pix.tobytes("png"), pix


@pdf_locked
def split_rects(page, pix):
    """Two clips of the page in reading order: right/left column at the gutter when
    there is one, otherwise top/bottom halves at the emptiest row."""
    gray = pymupdf.Pixmap(pymupdf.csGRAY, pix) if pix.n > 1 else pymupdf.Pixmap(pix)
    if gray.alpha:
        gray = pymupdf.Pixmap(gray, 0)
    gray.shrink(2)
    w, h, data = gray.width, gray.height, gray.samples
    stride = len(data) // h

    def dark_in_column(x):
        return sum(1 for y in range(int(h * 0.1), int(h * 0.9)) if data[y * stride + x] < 110)

    def dark_in_row(y):
        row = data[y * stride:y * stride + w]
        return sum(1 for v in row if v < 110)

    sx = page.rect.width / w
    sy = page.rect.height / h
    gutter = min(range(int(w * 0.42), int(w * 0.58)), key=dark_in_column)
    if dark_in_column(gutter) <= h * 0.005:
        x = gutter * sx
        return [pymupdf.Rect(x, 0, page.rect.width, page.rect.height), pymupdf.Rect(0, 0, x, page.rect.height)]
    cut = min(range(int(h * 0.4), int(h * 0.6)), key=dark_in_row)
    y = cut * sy
    return [pymupdf.Rect(0, 0, page.rect.width, y), pymupdf.Rect(0, y, page.rect.width, page.rect.height)]


def read_in_halves(ocr, page, pix, prompt, model):
    """Reads a refused page as two separate clips; refusals are often triggered by
    the page as a whole, not by either part."""
    dpi = max(150, round(72 * pix.width / page.rect.width))
    parts, finish = [], ""
    for rect in split_rects(page, pix):
        with PDF_LOCK:
            png = page.get_pixmap(clip=rect, dpi=dpi).tobytes("png")
        text, finish = ocr.read(png, prompt, model)
        text = clean_ocr(text)
        if not text or any(r in finish for r in BLOCKED_REASONS):
            return "", finish
        if text != "[[EMPTY]]":
            parts.append(text)
    joined = ""
    for part in parts:
        if not joined:
            joined = part
        elif joined.rstrip().endswith(TERMINAL) or not is_paragraph(part.lstrip()):
            joined = joined.rstrip() + "\n\n" + part.lstrip()
        else:
            joined = joined.rstrip() + " " + part.lstrip()
    return joined, finish


@pdf_locked
def ink_ratio(pix):
    """Fraction of dark pixels, sampled. Tells blank pages from pages with text."""
    gray = pymupdf.Pixmap(pymupdf.csGRAY, pix) if pix.n > 1 else pix
    samples = gray.samples
    step = max(1, len(samples) // 200000)
    sampled = samples[::step]
    dark = sum(1 for v in sampled if v < 110)
    return dark / max(1, len(sampled))


# Per-model settings. Gemma 4 only has thinking on/off, and its free tier allows few
# tokens per minute, so it is paced much slower than the Gemini models.
THINKING = {"gemini-3.1-flash-lite": types.ThinkingLevel.MINIMAL}


# Requests a minute per model, well under the free tier's own limits (the user's decision,
# 2026-10-23: "no hurry, the key is free: do not press it to its limit"; a per-minute 429
# had stopped both readers). 500 Lite requests a day at 5 a minute last 100 minutes.
LITE_RPM = 5
FLASH_RPM = 1
GEMMA_RPM = 4


def model_rpm(model, default_rpm):
    if "flash-lite" in model:
        return min(default_rpm, LITE_RPM)
    return GEMMA_RPM if "gemma" in model else FLASH_RPM


class ModelPool:
    """All Gemini API calls: pacing per model, retries, and models whose daily
    quota ran out. Each model has its own free quota, so they are tracked apart."""

    def __init__(self, primary, second, rescue, rpm):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set; scanned pages cannot be read.")
        # A request that hangs longer than this counts as failed and is retried.
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=300_000))
        self.model = primary
        self.second = second
        # The stronger free models (Flash, small daily quotas): they only settle, strongest
        # first, before the Lite models (set_extra).
        self.extra = []
        self.rescue = rescue
        # The status page's boxes for the free models (set_boxes): {"read1": [...], "read2":
        # [...], "settle": [...]}, each in the user's order. A role without one keeps the above.
        self.boxes = {}
        # strict: the boxes came from the status page (set_boxes): no model works outside them,
        # an empty box means nobody does that step, and Gemma reads a refused page only from a
        # box (the user's decision, 2026-10-23: "I decide who works").
        self.strict = False
        # Bumped each time the boxes are set: main goes over the pages again when the user
        # changed them during a pass (boxes_changed).
        self.box_gen = 0
        # The user's own models' boxes changed during the run: it starts no more page work
        # (past), and the Drive inbox starts the next run with them at once.
        self.halted = False
        # The free models this account has, for the status page's boxes (free_catalog).
        self.catalog = []
        self.rpm = rpm
        self.lock = threading.Lock()
        self.limiters = {}
        self.requests = {}
        self.exhausted = set()
        self.no_tuning = set()
        # Models this account does not have (404): unlike a used-up quota, they never come back.
        self.missing = set()
        self.quota_day = ""
        # The status page's live view (live.py): what each model is doing.
        self.live = live.NULL
        # No reading starts after stops["read"], no settling after stops["settle"] (epoch
        # seconds; none when empty); time_up: one was refused for it.
        self.stops = {}
        self.time_up = False
        # 429s in a row that read like the daily limit, per model: believed on the DAY_CHECKS-th.
        # On 2026-10-23 a per-minute 429 stopped both readers for the day after 8 and 3
        # requests, while Google counted 10 and 5 of 500.
        self.day_hits = {}
        # One request at a time per model (the user's rule, 2026-10-23): the next one leaves
        # only once the last one is answered. Requests to different models go side by side.
        self.turns = {}
        # Where each box's turn is (share).
        self.turns_at = {}
        # Models the user sent back to work during this run (revive).
        self.revived = set()

    def day_limit(self, model):
        """Counts a 429 naming the daily limit; True on the DAY_CHECKS-th in a row."""
        with self.lock:
            self.day_hits[model] = self.day_hits.get(model, 0) + 1
            return self.day_hits[model] >= DAY_CHECKS

    def set_out(self, models):
        """Models already found out of quota today by an earlier run (the status page knows):
        not asked again, unless the user sends one back (revive)."""
        with self.lock:
            self.exhausted.update(m for m in models if m)

    def revive(self, model):
        """The user pressed «أعد المحاولة الآن» for this model during the run: it works again
        (a model this account does not have stays out)."""
        with self.lock:
            # The page may name a model by its short name ("gemini-3-flash" for "-preview").
            names = [m for m in self.exhausted if m == model or m.startswith(model + "-")] or [model]
            names = [m for m in names if m not in self.missing]
            if not names:
                return False
            for m in names:
                self.exhausted.discard(m)
                self.day_hits.pop(m, None)
                self.revived.add(m)
        for m in names:
            self.live.revived(m)
            print(f"  {m}: back to work (asked on the status page)")
        return True

    def _turn(self, model):
        with self.lock:
            if model not in self.turns:
                self.turns[model] = threading.Lock()
            return self.turns[model]

    def available(self, model):
        return bool(model) and model not in self.exhausted

    def past(self, kind):
        """No more page work of this kind ("read" or "settle") in this run (stops, halted)."""
        stop = self.stops.get(kind)
        if self.halted or (stop and time.time() > stop):
            self.time_up = True
            return True
        return False

    def set_extra(self, models):
        self.extra = [m for m in dict.fromkeys(models) if m and m not in (self.model, self.second, self.rescue)]

    def set_boxes(self, boxes):
        """The free models the user put in the status page's boxes: only they give the first
        reading, only they the second, only they settle, each first to last (the next one
        when the one before has no quota left). An empty box: nobody does that step. Without
        a second reading, its box is empty."""
        new = {k: list(dict.fromkeys(m for m in (boxes.get(k) or []) if m)) for k in BOX_KEYS}
        if not self.second:
            new["read2"] = []
        with self.lock:
            self.strict = True
            self.boxes = new
            self.box_gen += 1

    def readers(self, first=None):
        """The two Lite readers; first: the one to try first."""
        order = [first] if first else []
        return [m for m in dict.fromkeys(order + [self.model, self.second]) if m]

    def reading(self, kind):
        """Who gives a page's first ("a") or second ("b") reading, first to last: the models in
        its box on the status page, else both Lite readers, each first in its own reading."""
        box = self.boxes.get("read1" if kind == "a" else "read2")
        if self.strict:
            return list(box or [])
        return list(box) if box else self.readers(self.second if kind == "b" else None)

    def can_read(self, kind, besides=""):
        """A model with quota left can give this reading; besides: the model that gave the
        page's other reading (a page's two readings never come from one model)."""
        return any(self.available(m) for m in self.reading(kind) if m != besides)

    def settlers(self, lite=True):
        """Who settles a page: the models in the settling box on the status page, every one of
        them (they share the pages, share); else the Flash models strongest first, then the Lite
        readers (fewer requests first, to spread their quotas). lite False, without the boxes:
        the helper's models are on and settle instead of the Lite readers."""
        box = self.boxes.get("settle")
        if self.strict:
            return [m for m in box or [] if self.available(m)]
        if box:
            return [m for m in box if self.available(m) and (lite or model_rank(m) > (0,))]
        out = [m for m in self.extra if self.available(m)]
        if lite:
            out += sorted((m for m in (self.model, self.second) if self.available(m)),
                          key=lambda m: self.requests.get(m, 0))
        return out

    def free_out(self):
        """No free reader has quota left today (Gemma only rescues refused pages, unless the
        user put it in a reading box)."""
        return not any(self.available(m) for m in self.reading("a") + self.reading("b"))

    def share(self, models, role=""):
        """The models of a box share its work (the user's rule, 2026-10-23: no order between
        them): those with no request in flight first, from a point that moves on with every
        call, so each gets pages. Without the status page's boxes the order stays as given."""
        models = [m for m in models if m]
        if not self.strict or len(models) < 2:
            return models
        key = (role,) + tuple(models)
        with self.lock:
            self.turns_at[key] = start = (self.turns_at.get(key, -1) + 1) % len(models)
        turn = models[start:] + models[:start]
        idle = [m for m in turn if not self._turn(m).locked()]
        return idle + [m for m in turn if m not in idle]

    def rescue_for(self):
        """The model that reads a page Gemini refused: Gemma, unless the boxes say who works."""
        return "" if self.strict else self.rescue

    def any_left(self):
        """Some free model in the boxes still has quota today (a reader or a settler)."""
        return not self.free_out() or bool(self.settlers())

    def _limiter(self, model):
        with self.lock:
            if model not in self.limiters:
                self.limiters[model] = RateLimiter(model_rpm(model, self.rpm))
            return self.limiters[model]

    def _config(self, model):
        if model in self.no_tuning:
            return types.GenerateContentConfig(max_output_tokens=8192, safety_settings=SAFETY_OFF)
        level = types.ThinkingLevel.MINIMAL if "gemma" in model else THINKING.get(model, types.ThinkingLevel.LOW)
        return types.GenerateContentConfig(
            max_output_tokens=8192,
            safety_settings=SAFETY_OFF,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH,
            thinking_config=types.ThinkingConfig(thinking_level=level),
        )

    def _call(self, model, png, prompt):
        # The live view shows each request waiting for its turn, then on its way.
        h = self.live.begin(model, *live.now_doing())
        answered = False
        try:
            self._limiter(model).wait()
            self.live.sent(h)
            with self.lock:
                self.requests[model] = self.requests.get(model, 0) + 1
            # png: one page image, a list of images (several pages in one request), or None.
            images = [] if png is None else (png if isinstance(png, list) else [png])
            contents = [types.Part.from_bytes(data=i, mime_type="image/png") for i in images] + [prompt]
            began = time.time()
            response = self.client.models.generate_content(model=model, contents=contents, config=self._config(model))
            live.took(time.time() - began)
            answered = True
        finally:
            self.live.done(h, answered)
        finish, text = "", ""
        if response.candidates:
            cand = response.candidates[0]
            finish = str(cand.finish_reason or "")
            # Some models send thought parts even with thinking off; keep only the answer.
            parts = cand.content.parts if cand.content and cand.content.parts else []
            text = "".join(p.text for p in parts if p.text and not getattr(p, "thought", False))
        return text.strip(), finish

    def read(self, png, prompt, model, attempts=3):
        """Returns (text, finish_reason). Raises QuotaExhausted (TimeUp for page work once its
        stop time has passed) or the last error. The model gets one request at a time: this
        waits until it has answered the one before (and any wait Google asked for after it)."""
        page, kind = live.now_doing()
        if model in self.exhausted:
            raise QuotaExhausted(model)
        # Shown as waiting for its turn meanwhile.
        h = self.live.begin(model, page, kind)
        with self._turn(model):
            self.live.done(h, False)
            if self.strict and kind in KIND_BOX and model not in self.boxes.get(KIND_BOX[kind], ()):
                raise Moved(model)
            return self._read(png, prompt, model, attempts, page, kind)

    def _read(self, png, prompt, model, attempts, page, kind):
        last = None
        stop = ("settle" if kind == "settle" else "read") if kind in ("a", "b", "settle") else None
        attempt = 0
        while attempt < attempts:
            if model in self.exhausted:
                raise QuotaExhausted(model)
            if stop and self.past(stop):
                raise TimeUp(model)
            attempt += 1
            try:
                got = self._call(model, png, prompt)
                with self.lock:
                    self.day_hits.pop(model, None)
                return got
            except Exception as e:
                last = e
                msg = str(e)
                low = msg.lower()
                if "429" in msg or "resource_exhausted" in low:
                    if "limit: 0" in low:
                        # No free quota for this model at all: unusable on this key, so
                        # no page waits for it.
                        with self.lock:
                            self.exhausted.add(model)
                            self.missing.add(model)
                        print(f"  {model}: no free quota on this key, skipped")
                        self.live.note(model, "missing", page, kind)
                        raise QuotaExhausted(model) from e
                    m = re.search(r"retry(?:Delay)?['\"]?\s*[:=]?\s*['\"]?(\d+)", msg, re.I)
                    wait = int(m.group(1)) + 2 if m else 65
                    if "perday" in low.replace(" ", "") or "per day" in low or "daily" in low \
                            or wait > LONG_WAIT_SECONDS:
                        if self.day_limit(model):
                            with self.lock:
                                self.exhausted.add(model)
                                self.quota_day = self.quota_day or pacific_day()
                            print(f"  {model}: daily quota used up: {msg[:300]}")
                            self.live.note(model, "out", page, kind, n=live.quota_limit(msg), extra=quota_ids(msg))
                            raise QuotaExhausted(model) from e
                        # Not believed yet: wait and ask again (no attempt used up).
                        attempt -= 1
                        wait = DAY_CHECK_SECONDS if wait > LONG_WAIT_SECONDS else max(wait, DAY_CHECK_SECONDS)
                        print(f"  {model}: 429 naming the daily limit, checking again in {wait}s: {msg[:300]}")
                        self.live.note(model, "rest", page, kind, secs=wait, extra="check-day " + quota_ids(msg))
                        time.sleep(wait)
                        continue
                    else:
                        self.live.note(model, "rest", page, kind, secs=wait, extra=quota_ids(msg))
                elif "400" in msg and model not in self.no_tuning and (
                        "thinking" in low or "media_resolution" in low or "safety" in low):
                    print(f"  {model} rejected tuning options, retrying without them: {msg[:200]}")
                    self.no_tuning.add(model)
                    wait = 0
                elif any(code in msg for code in ("500", "502", "503", "504")) or "unavailable" in low \
                        or "timed out" in low or "timeout" in low or "deadline" in low:
                    wait = 20 * (2 ** attempt)
                    self.live.note(model, "retry", page, kind, secs=wait, extra=msg[:80])
                elif "404" in msg or "not_found" in low:
                    # A model name this account does not have: not asked again in this run.
                    with self.lock:
                        self.exhausted.add(model)
                        self.missing.add(model)
                    print(f"  {model}: not found, skipped: {msg[:160]}")
                    self.live.note(model, "missing", page, kind)
                    raise QuotaExhausted(model) from e
                else:
                    self.live.note(model, "error", page, kind, extra=msg[:100])
                    raise
                print(f"  {model}: {msg[:160]} -> retry in {wait}s")
                time.sleep(wait)
        self.live.note(model, "error", page, kind, extra=str(last)[:100])
        raise last


def clean_ocr(text):
    text = text.replace("\r\n", "\n")
    text = re.sub(r"^```[a-zA-Z]*\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    lines = []
    for line in text.split("\n"):
        s = line.strip()
        # Stray page numbers such as "- ٢٤ -" or "٢٤" alone on a line.
        if re.fullmatch(r"[-–—\s]*[0-9٠-٩۰-۹]{1,4}[-–—\s]*", s) and s:
            continue
        if re.fullmatch(r"[-—–\s]*page[-—–\s]*", s, flags=re.I):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def is_refusal(finish):
    return any(r in finish for r in BLOCKED_REASONS)


def judge_reading(text, finish, ink):
    """('ok' | 'empty' | 'suspicious', cleaned text) for one model answer."""
    text = clean_ocr(text)
    empty = text == "[[EMPTY]]" or not text
    if is_refusal(finish):
        # A refusal is never an empty page, and text cut off by a refusal is incomplete.
        return "suspicious", "" if empty else text
    if empty and ink < INK_SUSPICIOUS_RATIO:
        return "empty", ""
    if not empty and "MAX_TOKENS" not in finish and (len(text) >= MIN_OCR_CHARS or ink < INK_SUSPICIOUS_RATIO * 2):
        return "ok", text
    return "suspicious", "" if empty else text


def read_with(pool, chain, page, png, pix, prompt, ink, rescue=""):
    """Tries the Gemini models in order until one reads the page; a refused page is
    tried again in two halves with the same model. The rescue model (Gemma) is used
    only when Gemini refused the page, never because a quota ran out.

    Returns (status, reading, error). status is 'pending' when a model was skipped
    for its daily quota and none could read the page, so it waits for tomorrow."""
    error, best = "", None
    quota_skipped = refused = False
    attempted = False
    # The rescue model in a reading box (the status page) is one of its readers there.
    extra = [rescue] if rescue and rescue not in chain else []
    for i, model in enumerate(chain + extra):
        saving = i >= len(chain)
        if saving and not refused:
            break
        if not pool.available(model):
            quota_skipped = quota_skipped or not saving
            continue
        try:
            text, finish = pool.read(png, prompt, model)
            via = "full"
            if not clean_ocr(text) and is_refusal(finish):
                refused = True
                via = "halves"
                text, finish = read_in_halves(pool, page, pix, prompt, model)
        except QuotaExhausted:
            quota_skipped = quota_skipped or not saving
            continue
        except Exception as e:
            attempted = True
            error = f"{model}: {str(e)[:250]}"
            continue
        attempted = True
        status, text = judge_reading(text, finish, ink)
        if is_refusal(finish):
            refused = True
        # Always said, so a reading by the rescue model from a reading box is not taken for a
        # refused page's (load_record).
        reading = {"model": model, "text": text, "finish": finish, "via": via, "status": status,
                   "refused": refused}
        if status in ("ok", "empty"):
            return status, reading, ""
        error = f"{model}: chars={len(text)} finish={finish}"
        if best is None or len(text) > len(best["text"]):
            best = reading
    if quota_skipped or not attempted:
        return "pending", None, error
    return ("suspicious" if best else "failed"), best, error


def record_path(cache_dir, page_no):
    return os.path.join(cache_dir, f"{page_no:04d}.json")


def load_record(cache_dir, page_no, rescue):
    path = record_path(cache_dir, page_no)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
    except ValueError:
        # A file cut off mid-write (the job was killed): read the page again.
        return None
    if rec.get("status") == "ok" and "a" not in rec:
        # Saved before pages got a second reading: its text is the main model's reading.
        models = rec.get("models") or ["?"]
        rec["a"] = {"model": models[0], "text": rec.get("text", ""), "finish": "", "status": "ok",
                    "via": "halves" if any("(halves)" in m for m in models) else "full"}
    a = rec.get("a") or {}
    if a.get("via") == "halves" or (a.get("model") == rescue and "refused" not in a):
        a["refused"] = True
    return rec


def save_record(cache_dir, rec):
    # Written to a temporary file first, so a killed job never leaves half a record.
    path = record_path(cache_dir, rec["page"])
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def scrub(rec, drop):
    """What a tool on the user's computer wrote around the page ("I'll read `task.md` …") goes
    from a saved record (helperbox.clean_text). drop: a helper reading that is no page text at
    all (an apology about its files) goes too, so the page is read or settled again; never in
    a review round, which reads nothing. Returns True when the record changed."""
    before = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    for key in ("a", "b"):
        r = rec.get(key)
        if not r or r.get("via") != "helper":
            continue
        r["text"] = helperbox.clean_text(r.get("text", ""))
        if drop and not helperbox.usable(r["text"]):
            if key == "a":
                keep = {k: rec[k] for k in ("page", "hskip", "ink", "spread") if k in rec}
                rec.clear()
                rec.update(keep, status="pending")
                return True
            for k in ("b", "check", "hfinal"):
                rec.pop(k, None)
    if rec.get("hfinal"):
        rec["hfinal"]["text"] = helperbox.clean_text(rec["hfinal"]["text"])
        if drop and not helperbox.usable(rec["hfinal"]["text"]):
            rec.pop("hfinal")
            rec.pop("check", None)
    if "|" in str((rec.get("check") or {}).get("model") or ""):
        # Settled by a model on the user's computer: its text is the book's.
        rec["text"] = helperbox.clean_text(rec.get("text", ""))
        if drop and not helperbox.usable(rec["text"]):
            rec["text"] = (rec.get("a") or {}).get("text", "")
            rec.pop("check", None)
    return json.dumps(rec, ensure_ascii=False, sort_keys=True) != before


def text_layer_record(doc, index, cache_dir):
    """A page with its own text in the PDF needs no reading: its record, or None."""
    with PDF_LOCK:
        layer = doc[index].get_text("text", sort=True).strip()
    if len(layer) < MIN_TEXT_LAYER_CHARS:
        return None
    rec = {"page": index + 1, "status": "text-layer", "text": layer}
    save_record(cache_dir, rec)
    return rec


def page_state(rec):
    """A page's state for the live view (live.Live.page)."""
    if not rec or rec.get("status") == "pending":
        return "unread"
    if rec.get("status") in ("empty", "text-layer") or review.pin_of(rec):
        return "done"
    if rec.get("status") in ("failed", "suspicious"):
        return "failed"
    c = rec.get("check") or {}
    if c.get("v") == CHECK_VERSION and c.get("state") in ("agreed", "reconciled", "single", "unsure", "human"):
        return "unsure" if c.get("state") == "unsure" else "done"
    return "read2" if (rec.get("b") or {}).get("status") == "ok" else "read1"


def note_reading(pool, page_no, kind, status, reading, error):
    """One reading of a page in the live view: who read it, how it went, how long it took."""
    reading = reading or {}
    model = reading.get("model") or (error.split(":", 1)[0] if error else "")
    result = {"ok": "ok", "empty": "empty", "failed": "error"}.get(status, "short")
    if reading.get("refused") and status != "ok":
        result = "refused"
    pool.live.note(model, result, page_no, kind, secs=live.took(), n=len(reading.get("text") or ""),
                   extra="halves" if reading.get("via") == "halves" else "")


def box_of(reading, key):
    """The box whose model gave this reading: "read1" or "read2". A page's first reading
    ("a", the first in time) is the first reading box's unless it says otherwise ("box"), and
    its second ("b") the second's: each box reads the pages it has not read yet, in any order
    (the user's question, 2026-10-24: "why does the second reading wait for the first?")."""
    return (reading or {}).get("box") or ("read1" if key == "a" else "read2")


def owed_step(rec):
    """The step ("a": the first reading box, "b": the second) whose reading a page read once
    still lacks."""
    return "b" if box_of(rec.get("a"), "a") == "read1" else "a"


def read_page(pool, doc, index, cache_dir, total, dual, step=None):
    """First and second reading of one page, saved to cache_dir/NNNN.json; step "a" gives
    only the first, "b" only the second (the run's steps, run_steps).
    Returns the record, or None when the page has to wait for tomorrow's quota."""
    page_no = index + 1
    rec = load_record(cache_dir, page_no, pool.rescue if pool else "")
    if rec and rec.get("status") in ("empty", "text-layer"):
        return rec
    page = doc[index]
    if rec is None:
        layer = text_layer_record(doc, index, cache_dir)
        if layer:
            return layer

    handed_back = {"hskip": True} if (rec or {}).get("hskip") else {}
    need_a = rec is None or rec.get("status") != "ok"
    b = (rec or {}).get("b")
    need_b = dual and (need_a or b is None or b.get("status") in ("failed", "suspicious"))
    # Which box gives each reading: the step's own (a box reads a page none of its models read
    # yet, first or second in time), else the first box first.
    kind_a, kind_b = "a", "b"
    if step:
        if not need_a and owed_step(rec) != step:
            # This box already read the page.
            need_b = False
        need_a, need_b = need_a, need_b and not need_a
        kind_a = kind_b = step
    if not need_a and not need_b:
        return rec
    if need_a and (not pool.can_read(kind_a) or pool.past("read")):
        # Nobody can read it in this run: no image is made for nothing.
        return None
    if not need_a and (not pool.can_read(kind_b, rec["a"]["model"]) or pool.past("read")):
        # Nobody can give its second reading in this run: it waits for one (as a reading
        # waiting for quota does below), and no image is made for nothing.
        if (rec.pop("b", None), rec.pop("check", None)) != (None, None):
            save_record(cache_dir, rec)
        return rec

    png, pix = page_image(doc, page)
    ink = ink_ratio(pix)
    spread = pix.width > pix.height * 1.15
    prompt = OCR_PROMPT.format(spread=SPREAD_HINT if spread else "")
    if need_a:
        with live.doing(page_no, kind_a):
            status, reading, error = read_with(pool, pool.share(pool.reading(kind_a), kind_a), page, png, pix, prompt,
                                               ink, rescue=pool.rescue_for())
        if status == "pending":
            return None
        note_reading(pool, page_no, kind_a, status, reading, error)
        if reading and kind_a != "a":
            reading["box"] = KIND_BOX[kind_a]
        rec = {"page": page_no, "ink": round(ink, 4), "spread": spread, "status": status,
               "text": reading["text"] if reading else "", "a": reading, **handed_back}
        if error:
            rec["error"] = error
        if step:
            return save_first(pool, cache_dir, rec, step, dual, total)
    if dual and need_b and rec["status"] == "ok":
        # The second reading comes from a different model, so its mistakes fall elsewhere.
        # Gemma may give it only when Gemini refused this page (and, with the status page's
        # boxes, only from a box).
        others = [m for m in pool.share(pool.reading(kind_b), kind_b) if m != rec["a"]["model"]]
        rescue = pool.rescue_for() if rec["a"].get("refused") and rec["a"]["model"] != pool.rescue else ""
        with live.doing(page_no, kind_b):
            status, reading, error = read_with(pool, others, page, png, pix, prompt, ink, rescue=rescue)
        if status == "pending":
            rec.pop("b", None)
        else:
            note_reading(pool, page_no, kind_b, status, reading, error)
            rec["b"] = reading or {"model": "", "text": "", "status": "failed"}
            rec["b"]["status"] = status
            if kind_b != "b":
                rec["b"]["box"] = KIND_BOX[kind_b]
            if error:
                rec["b"]["error"] = error
    rec.pop("check", None)
    save_record(cache_dir, rec)
    pool.live.page(page_no, page_state(rec))
    second = (rec.get("b") or {}).get("model", "-") if dual else "off"
    print(f"  page {page_no}/{total}: {rec['status']} ({(rec.get('a') or {}).get('model', '-')} + {second})")
    return rec


def save_first(pool, cache_dir, rec, step, dual, total):
    """Saves a page's first reading by the step's box. The other box may have read the page
    meanwhile (the two read a page at once if they come to it together): then this reading
    is its second, unless both came from one model (a model in both boxes)."""
    page_no = rec["page"]
    with RECORD_LOCK:
        now = load_record(cache_dir, page_no, pool.rescue if pool else "")
        if now and now.get("status") == "ok":
            reading = rec["a"]
            if dual and rec["status"] == "ok" and reading["model"] != now["a"]["model"] and \
                    box_of(now["a"], "a") != KIND_BOX[step] and (now.get("b") or {}).get("status") != "ok":
                reading.pop("box", None)
                if KIND_BOX[step] != "read2":
                    reading["box"] = KIND_BOX[step]
                now["b"] = reading
                now.pop("check", None)
                save_record(cache_dir, now)
            elif dual and rec["status"] == "ok" and box_of(now["a"], "a") != KIND_BOX[step]:
                # Both boxes read it with one model: this box reads it again (Book.do).
                now = dict(now, again=True)
            rec = now
        else:
            save_record(cache_dir, rec)
    pool.live.page(page_no, page_state(rec))
    second = (rec.get("b") or {}).get("model", "-") if dual else "off"
    print(f"  page {page_no}/{total}: {rec['status']} ({(rec.get('a') or {}).get('model', '-')} + {second})")
    return rec


SCAFFOLD = re.compile(r"^\s*(<<<|>>>|النسخة (النهائية|\(أ\)|\(ب\))\s*:?)\s*$")


def clean_settled(text):
    """Drops prompt scaffolding a model may echo back around its answer."""
    lines = [l for l in clean_ocr(text).split("\n") if not SCAFFOLD.match(l)]
    return "\n".join(lines).strip()


# Giving up on a page after this many runs whose settling request kept failing.
MAX_CHECK_ERRORS = 3


def settle_state(rec, info, a, b, diffs, miss_a, miss_b):
    """Settling failed: keep the reading that lost fewer lines; on a tie, the one from the
    user's own models when they gave the second (stronger than Lite, 2026-10-22), else the
    main one. Any disagreement nobody settled marks the page unsure."""
    use_b = bool(b) and (len(miss_b) < len(miss_a)
                         or (len(miss_b) == len(miss_a) and (rec.get("b") or {}).get("via") == "helper"))
    rec["text"] = b if use_b else a
    left = len(miss_b) if use_b else len(miss_a)
    info["kept"] = "b" if use_b else "a"
    info["sample"] = [list(d) for d in diffs[:3]]
    rec["check"] = {**info, "v": CHECK_VERSION, "state": "unsure" if (left or diffs) else "single"}


def check_page(pool, doc, rec, tess, vocab, lite=True):
    """Compares the two readings and settles their differences against the image.
    lite: the Lite readers may settle too (pool.settlers).
    Returns 'done', 'wait' (no settler with quota) or 'retry' (temporary API errors)."""
    a = rec["a"]["text"]
    b_rec = rec.get("b") or {}
    b = b_rec.get("text", "") if b_rec.get("status") == "ok" else ""
    miss_a = missing_lines(tess, a, vocab) if tess else []
    miss_b = missing_lines(tess, b, vocab) if tess and b else []
    ratio, diffs = crosscheck.compare(a, b) if b else (0.0, [])
    info = {"ratio": round(ratio, 4), "diffs": len(diffs), "missing_a": len(miss_a)}
    if b:
        info["missing_b"] = len(miss_b)
    page_no = rec["page"]
    if b_rec.get("status") == "failed" and not b_rec.get("model"):
        # Every model asked for its second reading hit an API error (Gemma's 500s,
        # 2026-10-23): the next run asks again rather than keep one reading, up to
        # MAX_CHECK_ERRORS runs.
        rec["read_errors"] = rec.get("read_errors", 0) + 1
        if rec["read_errors"] < MAX_CHECK_ERRORS:
            rec["text"] = a
            rec["check"] = {**info, "state": "retry"}
            return "retry"
    if not miss_a and (not b or not diffs):
        rec["text"] = a
        rec["check"] = {**info, "v": CHECK_VERSION, "state": "agreed" if b else "single"}
        pool.live.note("", "agreed" if b else "single", page_no, "page")
        return "done"

    # A page Gemini refused was read in halves or by the rescue model; asking
    # Gemini about the whole page again would only be refused again.
    if rec["a"].get("refused"):
        settle_state(rec, info, a, b, diffs, miss_a, miss_b)
        pool.live.note("", rec["check"]["state"], page_no, "page")
        return "done"

    if pool.past("settle") or not pool.settlers(lite):
        # Nobody can settle it in this run: no image is made for nothing.
        return "wait"
    page = doc[page_no - 1]
    png, pix = page_image(doc, page)
    rules = OCR_RULES.format(spread=SPREAD_HINT if pix.width > pix.height * 1.15 else "")
    prompt = crosscheck.reconcile_prompt(rules, a, b, diffs, list(dict.fromkeys(miss_a + miss_b)))
    answered = errored = False
    with live.doing(page_no, "settle"):
        for model in pool.share(pool.settlers(lite), "settle"):
            try:
                text, finish = pool.read(png, prompt, model)
            except QuotaExhausted:
                continue
            except Exception as e:
                # Often an overload of one model only: try the other one.
                errored = True
                info["error"] = str(e)[:200]
                continue
            answered = True
            text = clean_settled(text)
            if not text or "MAX_TOKENS" in finish or is_refusal(finish):
                info["rejected"] = f"{model}: no answer ({finish[:40]})"
                pool.live.note(model, "noanswer", page_no, "settle", secs=live.took())
            elif not crosscheck.validate_final(text, a, b):
                info["rejected"] = f"{model}: drops text the readings agree on"
                pool.live.note(model, "drops", page_no, "settle", secs=live.took())
            else:
                miss_f = missing_lines(tess, text, vocab) if tess else []
                if len(miss_f) <= min([len(miss_a)] + ([len(miss_b)] if b else [])):
                    rec["text"] = text
                    info.update(model=model, missing_final=len(miss_f))
                    rec["check"] = {**info, "v": CHECK_VERSION, "state": "unsure" if miss_f else "reconciled"}
                    pool.live.note(model, "unsure" if miss_f else "settled", page_no, "settle", secs=live.took(),
                                   n=len(diffs))
                    return "done"
                info["rejected"] = f"{model}: {len(miss_f)} lines missing (readings: {len(miss_a)}/{len(miss_b)})"
                pool.live.note(model, "lines", page_no, "settle", secs=live.took(), n=len(miss_f))
            break
    if not answered:
        if not errored:
            return "wait"
        rec["check_errors"] = rec.get("check_errors", 0) + 1
        if rec["check_errors"] < MAX_CHECK_ERRORS:
            rec["text"] = a
            rec["check"] = {**info, "state": "retry"}
            return "retry"
    settle_state(rec, info, a, b, diffs, miss_a, miss_b)
    if rec["check"]["state"] == "unsure":
        pool.live.note("", "unsure", page_no, "page")
    return "done"


def from_helper(rec):
    """The second reading came from the helper's models (helperbox.merge)."""
    return (rec.get("b") or {}).get("via") == "helper"


def check_helper_page(rec, tess, vocab, lv=live.NULL):
    """check_page for a page whose second reading, and settled text, came from the
    helper's models: nothing is asked of Gemini, and their text has to pass the same tests
    as a settled text from Gemini. Returns 'done' or 'wait' (not settled by them yet).
    lv: the live view, told how their settled text fared."""
    a = rec["a"]["text"]
    b = rec["b"]["text"]
    miss_a = missing_lines(tess, a, vocab) if tess else []
    miss_b = missing_lines(tess, b, vocab) if tess else []
    ratio, diffs = crosscheck.compare(a, b)
    info = {"ratio": round(ratio, 4), "diffs": len(diffs), "missing_a": len(miss_a), "missing_b": len(miss_b)}
    page_no = rec["page"]
    if not miss_a and not diffs:
        rec["text"] = a
        rec["check"] = {**info, "v": CHECK_VERSION, "state": "agreed"}
        lv.note("", "agreed", page_no, "page")
        return "done"
    final = rec.get("hfinal")
    if not final:
        return "wait"
    model = final.get("model") or rec["b"]["model"]
    text = clean_settled(final["text"])
    if not text:
        info["rejected"] = f"{model}: no answer"
        lv.note(model, "noanswer", page_no, "settle", extra="helper")
    elif not crosscheck.validate_final(text, a, b):
        info["rejected"] = f"{model}: drops text the readings agree on"
        lv.note(model, "drops", page_no, "settle", extra="helper")
    else:
        miss_f = missing_lines(tess, text, vocab) if tess else []
        if len(miss_f) <= min(len(miss_a), len(miss_b)):
            rec["text"] = text
            info.update(model=model, missing_final=len(miss_f))
            rec["check"] = {**info, "v": CHECK_VERSION, "state": "unsure" if miss_f else "reconciled"}
            lv.note(model, "unsure" if miss_f else "settled", page_no, "settle", n=len(diffs), extra="helper")
            return "done"
        info["rejected"] = f"{model}: {len(miss_f)} lines missing (readings: {len(miss_a)}/{len(miss_b)})"
        lv.note(model, "lines", page_no, "settle", n=len(miss_f), extra="helper")
    settle_state(rec, info, a, b, diffs, miss_a, miss_b)
    if rec["check"]["state"] == "unsure":
        lv.note("", "unsure", page_no, "page")
    return "done"


def upgrade_check(rec):
    """Checks saved by CHECK_VERSION 1 that are still valid under the current rules
    are kept without spending a request; the others are checked again."""
    c = rec.get("check") or {}
    if c.get("v") != 1:
        return
    a = (rec.get("a") or {}).get("text", "")
    b_rec = rec.get("b") or {}
    b = b_rec.get("text", "") if b_rec.get("status") == "ok" else ""
    if c.get("state") in ("agreed", "single"):
        c["v"] = CHECK_VERSION
    elif c.get("state") == "reconciled" and crosscheck.validate_final(rec.get("text", ""), a, b):
        c["v"] = CHECK_VERSION
    else:
        rec.pop("check", None)
        rec["text"] = a


# The missing-line check that skips lines Tesseract misread but the text has
# (crosscheck.LINE_FOUND, 2026-10-25). Unsure pages checked before it are checked again once.
LINES_CHECK = 2


def recheck_lines(records, filepath, cache_dir, lv=live.NULL):
    """Pages left unsure by the old missing-line check are checked again with the texts
    already saved: nothing is asked of a model. A settled text that only lost "missing" lines
    becomes settled; a page whose readings agreed becomes agreed; a page the helper's models
    settled is checked again as theirs. Pages that need a new settling stay unsure."""
    old = [r for r in records if r.get("status") == "ok" and not review.pin_of(r)
           and (r.get("check") or {}).get("v") == CHECK_VERSION
           and r["check"].get("state") == "unsure" and r["check"].get("lines") != LINES_CHECK]
    if not old:
        return
    vocab = book_vocab(records)
    cleared = 0
    for r in old:
        tess = tesseract_for(filepath, cache_dir, r["page"])
        if not tess:
            continue
        c = r["check"]
        if r.get("hfinal") and (r.get("b") or {}).get("status") == "ok":
            check_helper_page(r, tess, vocab, lv)
        elif c.get("model") and not c.get("rejected"):
            miss = missing_lines(tess, r.get("text", ""), vocab)
            c["missing_final"] = len(miss)
            if not miss:
                c["state"] = "reconciled"
        elif not c.get("diffs") and not missing_lines(tess, r.get("text", ""), vocab):
            c["state"] = "agreed" if (r.get("b") or {}).get("status") == "ok" else "single"
        r["check"]["lines"] = LINES_CHECK
        cleared += r["check"]["state"] != "unsure"
        save_record(cache_dir, r)
        lv.page(r["page"], page_state(r))
    print(f"Missing-line check again: {cleared} of {len(old)} unsure pages are no longer unsure")


def tesseract_for(filepath, cache_dir, page_no, kind="txt"):
    """Tesseract's reading of a page ("txt") or its word boxes ("tsv"), cached; empty when
    Tesseract is missing."""
    if not shutil.which("tesseract"):
        return ""
    tess_dir = os.path.join(cache_dir, "tesseract")
    os.makedirs(tess_dir, exist_ok=True)
    path = os.path.join(tess_dir, f"{page_no:04d}.{kind}")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    local = open_pdf(filepath)
    try:
        png, _ = page_image(local, local[page_no - 1])
    finally:
        close_pdf(local)
    tess = tesseract_tsv(png) if kind == "tsv" else tesseract_text(png)
    with open(path, "w", encoding="utf-8") as f:
        f.write(tess)
    return tess


def book_vocab(records):
    vocab = set()
    for r in records:
        vocab.update(norm_words(r.get("text") or ""))
        vocab.update(norm_words((r.get("b") or {}).get("text") or ""))
    return vocab


def check_pages(pool, filepath, records, cache_dir, dual, workers, reads=False, settles=False, done=()):
    """Runs check_page on every read page whose check is missing or out of date.
    Pages the user answered about are theirs and never checked again.
    A page with the helper's second reading is checked with check_helper_page. A page whose
    next step is the helper's (reading it when reads, settling its two readings when settles)
    waits for them, unless they handed it back. A page the run's steps checked (done) is
    left. Returns the pages waiting for them."""
    for r in records:
        if not review.pin_of(r):
            upgrade_check(r)
    due = [r for r in records if r.get("status") == "ok" and r.get("a") and not review.pin_of(r)
           and (r.get("check") or {}).get("v") != CHECK_VERSION]
    # The helper's models gave the second reading, or settled the free models' two readings.
    theirs = lambda r: from_helper(r) or bool(r.get("hfinal"))
    turn = lambda r: not r.get("hskip") and (settles if (r.get("b") or {}).get("status") == "ok" else reads)
    by_helper = [r for r in due if theirs(r)]
    waiting = {r["page"] for r in due if not theirs(r) and turn(r)}
    todo = [r for r in due if not theirs(r) and r["page"] not in waiting and (not dual or "b" in r)
            and r["page"] not in done]
    lv = pool.live if pool else live.NULL
    if not todo and not by_helper:
        for p in waiting:
            lv.page(p, "helper")
        return waiting
    vocab = book_vocab(records)
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as tp:
        tess = dict(zip((r["page"] for r in todo + by_helper),
                        tp.map(lambda r: tesseract_for(filepath, cache_dir, r["page"]), todo + by_helper)))
    for r in by_helper:
        if check_helper_page(r, tess[r["page"]], vocab, lv) == "done":
            save_record(cache_dir, r)
            lv.page(r["page"], page_state(r))
            print(f"  check page {r['page']}: {r['check']['state']} (helper, diffs {r['check']['diffs']})")
        elif turn(r):
            waiting.add(r["page"])
        else:
            # The helper is off, or every model in its box refused to settle the page
            # (skip.json): Gemini settles what its models read.
            todo.append(r)
    for p in waiting:
        lv.page(p, "helper")
    if not todo:
        return waiting
    print(f"Checking {len(todo)} pages against each other and Tesseract")
    results = collections.Counter()
    lock = threading.Lock()

    def work(rec):
        local = open_pdf(filepath)
        try:
            result = check_page(pool, local, rec, tess[rec["page"]], vocab)
        finally:
            close_pdf(local)
        with lock:
            results[result] += 1
        if result != "wait":
            save_record(cache_dir, rec)
            lv.page(rec["page"], page_state(rec))
            c = rec["check"]
            print(f"  check page {rec['page']}: {c['state']} (diffs {c['diffs']}, missing {c['missing_a']})")

    with ThreadPoolExecutor(max_workers=workers) as tp:
        list(tp.map(work, todo))
    if results["wait"]:
        # One line for them all: no settler is left in this run (quota, or its time is up).
        lv.note("", "wait", kind="page", n=results["wait"])
    return waiting


# Free models added on their own when Google offers them: Gemini Flash or Pro, version 3
# or later. Lite, Gemma, Nano, image/audio models and dated variants never match.
NEW_MODEL = re.compile(r"^gemini-(\d+(?:\.\d+)?)-(flash|pro)(?:-preview|-latest)?$")


def model_rank(name):
    """(version, Pro before Flash) for ordering models strongest first; (0,) if unknown."""
    m = NEW_MODEL.match(re.sub(r"-(preview|latest|exp)(-[\w-]*)?$|-\d{3}$", "", name))
    if not m:
        return (0,)
    major, _, minor = m.group(1).partition(".")
    return (int(major), int(minor or 0), 1 if m.group(2) == "pro" else 0)


def list_models(pool):
    """{name: True} for the models this account can ask for text; empty when listing fails."""
    have = {}
    try:
        for m in pool.client.models.list():
            name = (getattr(m, "name", "") or "").split("/")[-1]
            actions = getattr(m, "supported_actions", None)
            if name and (not actions or "generateContent" in actions):
                have[name] = True
    except Exception as e:
        print(f"Could not list models ({str(e)[:120]}); models used by their given names")
        return {}
    return have


def match_models(have, names):
    """Each name as a model this account has (have: list_models). Google renames models (for
    example "gemini-3-flash" is served as "gemini-3-flash-preview"); a name not found as is
    takes the closest "<name>-..." model, and stays as given when there is none."""
    skip = ("lite", "image", "tts", "audio", "live", "embedding")
    out = []
    for n in names:
        if n in have:
            out.append(n)
            continue
        close = [a for a in have if a.startswith(n + "-") and not any(s in a[len(n):] for s in skip)]
        close.sort(key=lambda a: (a != n + "-preview", a != n + "-latest", len(a), a))
        if close:
            print(f"Model {n} is served as {close[0]}")
            out.append(close[0])
        else:
            print(f"Model {n} is not available to this account")
            out.append(n)
    return list(dict.fromkeys(out))


def resolve_models(pool, names, have=None):
    """Maps each name of the other free models to a model this account has (match_models).
    Newer Gemini Flash/Pro models the account has are added, each before the first model older
    than it. Names stay as given if listing fails. have: list_models(pool), when asked already."""
    have = list_models(pool) if have is None else have
    if not have:
        return names
    out = match_models(have, names)
    base = lambda a: re.sub(r"-(preview|latest)$", "", a)
    known = {base(a) for a in out}
    added = []
    # Same rank: the plain name first (it outlives its -preview/-latest aliases).
    for a in sorted(have, key=lambda a: (model_rank(a), a == base(a)), reverse=True):
        m = NEW_MODEL.match(a)
        if not m or int(m.group(1).split(".")[0]) < 3 or base(a) in known:
            continue
        known.add(base(a))
        at = next((i for i, j in enumerate(out) if model_rank(j) < model_rank(a)), len(out))
        out.insert(at, a)
        added.append(a)
    if added:
        print(f"New free models added: {', '.join(added)}")
        try:
            pool.added_models = added
        except AttributeError:
            pass
    return out


# The free models the status page offers for its boxes: Gemini Flash-Lite, Flash and Pro from
# 2.5 on, and Gemma from 4 on.
CATALOG_MODEL = re.compile(r"^(?:gemini-(\d+(?:\.\d+)?)-(flash-lite|flash|pro)|gemma-(\d+)-[a-z0-9-]+-it)(-preview|-latest)?$")


def free_catalog(have):
    """The free models this account has (have: list_models) for the status page's boxes, in
    rows: Flash-Lite, Flash, Pro, each newest first, then Gemma. A -preview or -latest alias
    only stands for a model without its plain name."""
    best = {}
    for name in have:
        m = CATALOG_MODEL.match(name)
        if not m:
            continue
        if m.group(1):
            version = (tuple(int(x) for x in m.group(1).split(".")) + (0,))[:2]
            if version < (2, 5):
                continue
            row = ("flash-lite", "flash", "pro").index(m.group(2))
        else:
            version = (int(m.group(3)),)
            if version < (4,):
                continue
            row = 3
        base = name[:len(name) - len(m.group(4) or "")]
        if base not in best or name == base or (best[base][2] != base and name.endswith("-preview")):
            best[base] = (row, version, name)
    rows = sorted(best.values(), key=lambda x: (x[0], [-v for v in x[1]], [-ord(c) for c in x[2]]))
    return [name for _, _, name in rows]


# The status page's boxes for the free models: who gives the first reading, who the second,
# who settles.
BOX_KEYS = ("read1", "read2", "settle")


def parse_boxes(text, keys=BOX_KEYS):
    """The workflow's "models" input, "read1=a,b;read2=c;settle=d,e;out=f" (set from the status
    page's boxes; out: the models an earlier run found out of quota today), as {"read1": ["a",
    "b"], ...}; an empty box stays, empty. keys: the parts wanted (("out",) for the list)."""
    out = {}
    for part in (text or "").split(";"):
        key, _, names = part.partition("=")
        if key.strip() in keys:
            out[key.strip()] = [n for n in dict.fromkeys(x.strip() for x in names.split(",")) if n]
    return out


def helper_modes(box):
    """Which steps the user's own models take in this run (the helper input it was given)."""
    return {"read1": bool(box and box.reads1), "read2": bool(box and box.reads2), "settle": bool(box and box.settles)}


def boxes_changed(pool, have, modes, free, helper):
    """The user changed the boxes on the status page during the run (live.py, 2026-10-23: "why
    not give it pages at once?"): the free models now in them work within seconds, each in its
    box, and one taken out takes no new page (main goes over the pages left again, box_gen).
    When the user's own models' part changed, this run cannot hand them their pages (the helper
    input is fixed at the dispatch): it starts no new page work, and the Drive inbox starts the
    next run with the boxes as they are, at once (boxes_at in status.json says which boxes this
    run took). modes: helper_modes of this run. Returns True when the run took the boxes."""
    if helper is not None and helper != modes:
        if not pool.halted:
            pool.halted = True
            print("The boxes of the user's own models changed: no new page work in this run, the next one takes them")
            pool.live.note("", "restart", kind="page")
        return False
    new = {k: match_models(have, v) if have else v for k, v in free.items() if k in BOX_KEYS}
    if any(pool.boxes.get(k, []) != v for k, v in new.items()):
        pool.set_boxes({**pool.boxes, **new})
        for k, v in new.items():
            for m in v:
                pool.live.role(m, k)
        print("Boxes changed during the run: " + "; ".join(f"{k}={','.join(v) or '-'}" for k, v in pool.boxes.items()))
        pool.live.note("", "boxes", kind="page")
    return True


FOOTNOTE_DEF = re.compile(r"^\[\^([^\]\s]+)\]:\s*(.*)$")
# Footnotes the model left in the book's own style: "(1) text" at the end of the page.
PLAIN_NOTE = re.compile(r"^\(([0-9٠-٩۰-۹]{1,3})\)\s*(.+)$")
PLAIN_REF = re.compile(r"(?<!^)\(([0-9٠-٩۰-۹]{1,3})\)", re.M)
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
FOOTNOTE_REF = re.compile(r"\[\^([^\]\s]+)\](?!:)")
TERMINAL = tuple(".!?؟:»\")…]*؛")


def split_page(page_no, text):
    """Splits a page into body blocks, footnote definitions with page-unique labels,
    and notes whose marker could not be found in the text."""
    body_lines, notes = [], {}
    for line in text.split("\n"):
        m = FOOTNOTE_DEF.match(line.strip())
        if m:
            notes[m.group(1).translate(DIGITS)] = m.group(2).strip()
        else:
            body_lines.append(line)

    # Trailing "(n) text" lines are footnotes too.
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    plain = []
    while body_lines:
        m = PLAIN_NOTE.match(body_lines[-1].strip())
        if m:
            plain.insert(0, (m.group(1).translate(DIGITS), m.group(2).strip()))
            body_lines.pop()
        elif not body_lines[-1].strip():
            body_lines.pop()
        else:
            break
    body = "\n".join(body_lines)

    used = set()

    def ref(m):
        label = m.group(1).translate(DIGITS)
        if label in notes:
            used.add(label)
            return f"[^p{page_no}-{label}]"
        return f"({m.group(1)})"

    body = FOOTNOTE_REF.sub(ref, body)
    for label, note in plain:
        if label in notes:
            continue
        for m in PLAIN_REF.finditer(body):
            if m.group(1).translate(DIGITS) == label:
                body = body[:m.start()] + f"[^p{page_no}-{label}]" + body[m.end():]
                notes[label] = note
                used.add(label)
                break
        else:
            notes[label] = note

    defs, orphans = [], []
    for label, note in notes.items():
        if label in used:
            defs.append(f"[^p{page_no}-{label}]: {note}")
        else:
            orphans.append(f"({label}) {note}")
    blocks = [b.strip() for b in re.split(r"\n\s*\n", body) if b.strip()]
    return blocks, defs, orphans


def is_paragraph(block):
    return not re.match(r"^(#|\||>|[-*+]\s|\d+\.\s)", block)


def notice_words(check, places):
    """Up to three (read, other) word pairs for a page's notice: from the places still
    open for review when there are any, otherwise from the check's sample."""
    if places is None:
        return [tuple(x) for x in check.get("sample", [])[:3]]
    out = []
    for it in places:
        if it.get("hint") == "likely-ok" or it["kind"] == "page":
            continue
        other = next((o["text"] for o in it.get("options", [])), "")
        out.append((it["core"].strip(), other.strip()))
    return out[:3]


def safe_rules(md):
    """A line of dashes from a page (a model drawing the footnote line) becomes a
    plain rule between blank lines: pandoc otherwise reads "---" with text right
    after it as a YAML settings block (and fails), or the line above as a heading."""
    return re.sub(r"\n*^[ \t]*(?:-[ \t]*){3,}$\n*", "\n\n* * *\n\n", md, flags=re.M)


def assemble(records, review_pages=None):
    """The book's Markdown. review_pages (from review.build) gives the places still open
    on each unsure page, for its notice; pages the user finished have none."""
    # Each block is [text, kind]; notes sit between pages without breaking a
    # sentence that continues on the next page.
    open_places = None if review_pages is None else {p["page"]: p["items"] for p in review_pages}
    blocks, defs, last_heading = [], [], None
    marker = ""
    for rec in records:
        # An empty span at the start of each page; add_page_list() turns it into the
        # EPUB page marker, so readers can show the book's own page numbers.
        marker += f"[]{{#pg-{rec['page']} .pagebreak}}"
        text = rec.get("text") or ""
        if rec["status"] == "text-layer":
            text = re.sub(r"[ \t]*\n(?!\s*\n)", " ", text)
        if not text:
            if rec["status"] in ("failed", "suspicious") or rec.get("missing"):
                blocks.append([f"> **[الصفحة {rec['page']} لم تُقرأ]**", "marker"])
            continue
        page_blocks, page_defs, orphans = split_page(rec["page"], text)
        defs.extend(page_defs)
        for i, block in enumerate(page_blocks):
            if block.startswith("#") and len(block) > 150:
                # A paragraph the model marked as a heading by mistake.
                block = block.lstrip("#").strip()
            if block.startswith("#"):
                if rec["page"] == 1:
                    # The cover's big words are not chapters.
                    block = block.lstrip("#").strip()
                elif block == last_heading:
                    # Running headers repeated on each page come back as the same heading.
                    continue
                else:
                    last_heading = block
            if marker and not block.startswith("#"):
                # Not inside a heading: the marker would also land in the table of contents.
                m = re.match(r"^(#{1,6} |\| |> )?", block)
                block = block[:m.end()] + marker + block[m.end():]
                marker = ""
            if i == 0 and is_paragraph(block):
                j = len(blocks) - 1
                while j >= 0 and blocks[j][1] == "note":
                    j -= 1
                if (j >= 0 and blocks[j][1] == "body" and is_paragraph(blocks[j][0])
                        and not blocks[j][0].endswith(TERMINAL)):
                    blocks[j][0] += " " + block
                    continue
            blocks.append([block, "body"])
        for note in orphans:
            blocks.append([f"::: note\n{note}\n:::", "note"])
        check = rec.get("check") or {}
        if check.get("state") == "unsure":
            # Tell the reader right in the book which page to compare with the original.
            short = lambda t: (t[:40] + "…") if len(t) > 40 else (t or "—")
            places = None if open_places is None else open_places.get(rec["page"], [])
            words = "، ".join(f"«{short(x)}» أو «{short(y)}»" for x, y in notice_words(check, places))
            text = f"⚠ تنبيه: قراءة الصفحة {rec['page']} من الأصل غير مؤكدة؛ يُنصح بمطابقتها بالأصل."
            if words:
                text += f" مواضع الشك: {words}."
            blocks.append([f"::: unsure\n{text}\n:::", "note"])
    if marker:
        blocks.append([marker, "body"])
    out = "\n\n".join(b for b, _ in blocks)
    return safe_rules(out + ("\n\n" + "\n\n".join(defs) if defs else "")).strip("\n") + "\n"


def add_page_list(epub_path):
    """Marks the page spans as EPUB page breaks and adds the page list to the
    navigation document, rewriting the EPUB in place."""
    with zipfile.ZipFile(epub_path) as zin:
        names = zin.namelist()
        items = {n: zin.read(n) for n in names}
    span = re.compile(r'<span (?:id="pg-(\d+)" class="pagebreak"|class="pagebreak" id="pg-(\d+)")></span>')
    entries = []
    for name in sorted(n for n in names if n.startswith("EPUB/text/") and n.endswith(".xhtml")):
        html = items[name].decode("utf-8")

        def mark(m):
            n = m.group(1) or m.group(2)
            entries.append((int(n), name[len("EPUB/"):]))
            return f'<span epub:type="pagebreak" role="doc-pagebreak" id="pg-{n}" aria-label="{n}"></span>'

        items[name] = span.sub(mark, html).encode("utf-8")
    if not entries or "EPUB/nav.xhtml" not in items:
        return 0
    entries.sort()
    links = "".join(f'<li><a href="{href}#pg-{n}">{n}</a></li>' for n, href in entries)
    nav = items["EPUB/nav.xhtml"].decode("utf-8")
    nav = nav.replace("</body>", f'<nav epub:type="page-list" id="page-list" hidden="hidden"><ol>{links}</ol></nav>\n</body>', 1)
    items["EPUB/nav.xhtml"] = nav.encode("utf-8")
    tmp = epub_path + ".tmp"
    with zipfile.ZipFile(tmp, "w") as zout:
        zout.writestr(zipfile.ZipInfo("mimetype"), items.pop("mimetype", b"application/epub+zip"),
                      compress_type=zipfile.ZIP_STORED)
        for name in names:
            if name in items:
                zout.writestr(name, items[name], compress_type=zipfile.ZIP_DEFLATED)
    os.replace(tmp, epub_path)
    return len(entries)


ARTICLES_PROMPT = """هذه قائمة مرقمة بالعناوين الكبيرة التي وُجدت في كتاب عربي، ومع كل عنوان بداية النص الذي يليه.
المطلوب: حدد العناوين التي تبدأ مقالاً أو بحثاً أو فصلاً رئيسياً مستقلاً في الكتاب (في كتب المقالات يكون عادة اسم الكتاب المدروس ومؤلفه، ويليه اسم كاتب المقال).
العناوين الداخلية داخل المقال (أقسامه، نصوصه، نماذجه، تراجم، أبواب فرعية) وصفحات الغلاف والعنوان ليست رئيسية.
أجب بـ JSON فقط بهذا الشكل: {{"main": [أرقام العناوين الرئيسية]}}

{items}"""


def keep_main_headings(md, ocr, cache_dir):
    """Leaves "# " only on the headings that start a main article, so the table of
    contents lists articles alone; the others become sub-headings in the text."""
    lines = md.split("\n")
    heads = [i for i, line in enumerate(lines) if line.startswith("# ")]
    if len(heads) < 2:
        return md

    def plain(line):
        return re.sub(r"\[\]\{[^}]*\}", "", line).lstrip("#").strip()

    items = []
    for k, i in enumerate(heads, 1):
        after = " ".join(plain(l) for l in lines[i + 1:i + 6] if l.strip() and not l.startswith("["))[:160]
        items.append(f"{k}. {plain(lines[i])}\n   ← {after}")
    key = hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()[:16]
    titles = [plain(lines[i]) for i in heads]
    path = os.path.join(cache_dir, "articles.json")
    main = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        # The same headings with other text after them (a correction near a heading)
        # keep the choice made for them.
        if saved.get("key") == key or saved.get("titles") == titles:
            main = set(saved["main"])
    if main is None:
        if not ocr:
            return md
        try:
            main = None
            for model in (m for m in (ocr.model, ocr.second) if ocr.available(m)):
                try:
                    text, _ = ocr.read(None, ARTICLES_PROMPT.format(items="\n".join(items)), model)
                    main = set(int(n) for n in json.loads(re.search(r"\{.*\}", text, re.S).group(0))["main"])
                    break
                except Exception as e:
                    print(f"  {model} could not pick the main articles: {str(e)[:160]}")
            if main is None:
                return md
        except Exception as e:
            print(f"Could not pick the main articles, keeping all headings: {e}")
            return md
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"key": key, "main": sorted(main), "titles": titles}, f, ensure_ascii=False)
    if not main:
        return md
    for k, i in enumerate(heads, 1):
        if k not in main:
            lines[i] = "#" + lines[i]
    print(f"Table of contents: {len(main)} main articles out of {len(heads)} headings")
    return "\n".join(lines)


norm_words = crosscheck.norm_words


def tesseract_text(png, *extra):
    try:
        r = subprocess.run(["tesseract", "stdin", "stdout", "-l", "ara", "--psm", "3", *extra],
                           input=png, capture_output=True, timeout=180,
                           # One thread per process: we already run several in parallel.
                           env={**os.environ, "OMP_THREAD_LIMIT": "1"})
        return r.stdout.decode("utf-8", "ignore")
    except Exception as e:
        print(f"  tesseract failed: {e}")
        return ""


def tesseract_tsv(png):
    """Tesseract's words with their boxes, in the pixels of the same page image (TSV)."""
    return tesseract_text(png, "tsv")


missing_lines = crosscheck.missing_lines


def run_pandoc(md_path, output_path, title, cover_path=None, toc=True):
    cmd = [
        "pandoc", md_path, "-o", output_path,
        "-f", "markdown-tex_math_dollars-raw_tex-raw_html-subscript-superscript-smart",
        "--metadata", f"title={title}",
        "--metadata", "lang=ar",
        "--metadata", "dir=rtl",
        "--split-level=1",
    ]
    css = os.path.join(os.path.dirname(os.path.abspath(__file__)), "epub.css")
    if os.path.exists(css):
        cmd.extend(["--css", css])
    if toc:
        cmd.extend(["--toc", "--toc-depth=1"])
    if cover_path and os.path.exists(cover_path):
        cmd.extend(["--epub-cover-image", cover_path])
    print(f"Running pandoc to generate {output_path}...")
    subprocess.run(cmd, check=True)
    print(f"Successfully generated EPUB: {output_path}")


def pacific_day():
    """Gemini's daily quotas reset at midnight Pacific time."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.fromtimestamp(time.time() - 8 * 3600, timezone.utc).strftime("%Y-%m-%d")


def waiting_for_reader(pool, records, dual):
    """When some free readers' daily quota is used up and others still have some, a first
    reader among them (the next book starts with first readings): the pages whose only missing
    piece is a second reading that only readers now out could give (a page's two readings
    never come from one model). Returns (pages, readers with quota left); no pages when every
    reader is out, or none is, or no first reader is left."""
    if not pool or not dual:
        return set(), []
    first = [m for m in pool.reading("a") if m not in pool.missing]
    second = [m for m in pool.reading("b") if m not in pool.missing]
    readers = list(dict.fromkeys(first + second))
    left = [m for m in readers if m not in pool.exhausted]
    if not left or len(left) == len(readers) or not any(m in left for m in first):
        return set(), left
    pages = set()
    for r in records:
        if r.get("status") != "ok" or review.pin_of(r) or (r.get("b") or {}).get("status") == "ok":
            continue
        box = second if owed_step(r) == "b" else first
        owed = [m for m in box if m != (r.get("a") or {}).get("model")]
        if owed and all(m in pool.exhausted for m in owed):
            pages.add(r["page"])
    return pages, left


def book_needs(pool, records, dual):
    """The pages waiting for each box, whoever holds them now (the helper's models too), by the
    free models that could take them: {"read1": {"model,model": n}, "read2": …, "settle": …}.
    The Drive inbox starts a run when one of those models has quota left (each box goes its
    own way, 2026-10-24). A page nobody read waits for both reading boxes; a page read once
    only for a model of the other box that did not read it."""
    n = {"read1": {}, "read2": {}, "settle": {}}

    def add(box, models):
        key = ",".join(sorted(set(m for m in models if m)))
        if key:
            n[box][key] = n[box].get(key, 0) + 1
    for r in records:
        if review.pin_of(r) or r.get("status") in ("empty", "text-layer"):
            continue
        if r.get("status") != "ok":
            add("read1", pool.reading("a"))
            if dual:
                add("read2", pool.reading("b"))
        elif dual and (r.get("b") or {}).get("status") != "ok":
            k = owed_step(r)
            add(KIND_BOX[k], [m for m in pool.reading(k) if m != (r.get("a") or {}).get("model")])
        elif (r.get("check") or {}).get("v") != CHECK_VERSION:
            add("settle", pool.boxes.get("settle") or [] if pool.strict else pool.settlers())
    return n


def summarize(records, dual, review_pages=None, applied=None, helper_wait=(), reader_wait=(), readers_left=()):
    """Book state for status.json and the report. review_pages and applied (from
    review.build and review.overlay) add the review's numbers; helper_wait are the pages
    waiting for the helper's models (check_pages), reader_wait those waiting for a reader
    whose quota ran out while readers_left still have some (waiting_for_reader)."""
    helper = sorted(set(helper_wait))
    # A page the helper's models still work on counts only as theirs.
    missing = [r["page"] for r in records if r["status"] in ("failed", "suspicious") and r["page"] not in helper_wait]
    retry = [r["page"] for r in records if (r.get("check") or {}).get("state") == "retry" and r["page"] not in helper_wait]
    # Pages the user answered about are finished, whatever the checks would say.
    waiting = [r["page"] for r in records if r["page"] not in helper_wait
               and (r["status"] == "pending"
                    or (r["status"] == "ok" and r["page"] not in retry and not review.pin_of(r)
                        and ((r.get("check") or {}).get("v") != CHECK_VERSION or (dual and "b" not in r))))]
    unsure = [r["page"] for r in records if (r.get("check") or {}).get("state") == "unsure"]
    states = {}
    for r in records:
        key = (r.get("check") or {}).get("state") or r["status"]
        states[key] = states.get(key, 0) + 1
    # Waiting pages need tomorrow's quota; retry pages hit temporary API errors and
    # can be tried again right away.
    # "helper": the pages left are with the helper's models on the user's computer (the free
    # models take their share again in the next run). The Drive inbox parks such a book.
    state = "paused" if waiting else "helper" if helper else "incomplete" if missing or retry else "done"
    # "reader": paused only for second readings owed by the reader that is out today, while
    # the other reader still has quota: the Drive inbox parks the book and starts the next
    # one with the reader that is left. With both readers out it stays "paused".
    if state == "paused" and readers_left and set(waiting) <= set(reader_wait):
        state = "reader"
    samples = {r["page"]: r["check"].get("sample", []) for r in records
               if (r.get("check") or {}).get("state") == "unsure"}
    # Why each unsure page stayed unsure (for tuning the check).
    reasons = {}
    for r in records:
        c = r.get("check") or {}
        if c.get("state") == "unsure":
            reasons[r["page"]] = "؛ ".join(x for x in (c.get("rejected"), c.get("error"), c.get("arbiter"),
                                                      "قراءة واحدة" if r.get("a", {}).get("refused") else "")
                                         if x)
    out = {"state": state, "pages_total": len(records),
           "pages_done": len(records) - len(missing) - len(waiting) - len(retry) - len(helper),
           "pages_helper": helper,
           "pages_unsure": unsure, "pages_missing": missing, "pages_waiting": waiting,
           "pages_retry": retry, "counts": states, "samples": samples, "reasons": reasons,
           "pages_human": [r["page"] for r in records if review.pin_of(r)],
           # Pages checked against a second reading from the helper's models.
           "helper_checked": sum(1 for r in records if from_helper(r) and (r.get("check") or {}).get("v") == CHECK_VERSION)}
    if state == "reader":
        out["readers_left"] = list(readers_left)
    if review_pages is not None:
        results = list((applied or {}).get("results", {}).values())
        out.update(review_items=sum(len(p["items"]) + p.get("more", 0) for p in review_pages),
                   review_applied=results.count("applied"), review_kept=results.count("kept"),
                   review_conflicts_new=(applied or {}).get("conflicts_new", 0),
                   review_seen=(applied or {}).get("seen", ""))
    return out


def write_report(path, summary, pool, notes=()):
    names = [("agreed", "القراءتان متطابقتان"), ("reconciled", "صُححت بمقارنة القراءتين على الصورة"),
             ("single", "قراءة واحدة فقط"), ("retry", "تعذّرت قراءتها الثانية أو حسمها بسبب خطأ مؤقت (تُعاد في التشغيل القادم)"),
             ("unsure", "لم تُحسم (مشكوك فيها)"), ("human", "روجعت يدوياً"),
             ("text-layer", "نص جاهز في الملف"), ("empty", "فارغة/صور"),
             ("ok", "قُرئت ولم تُراجع بعد"), ("suspicious", "قراءة ناقصة"), ("failed", "فشلت"),
             ("pending", "لم تُقرأ بعد")]
    counts = summary["counts"]
    lines = ["# تقرير التحويل", "", f"- عدد الصفحات: {summary['pages_total']}"]
    lines += [f"- {label}: {counts[key]}" for key, label in names if counts.get(key)]
    if pool:
        lines += [f"- طلبات {model} في هذا التشغيل: {n}" for model, n in pool.requests.items()]
        if getattr(pool, "added_models", None) and not pool.boxes.get("settle"):
            lines.append(f"- نماذج مجانية جديدة أُضيفت تلقائياً: {'، '.join(pool.added_models)}")
        titles = {"read1": "القراءة الأولى", "read2": "القراءة الثانية", "settle": "الحسم"}
        lines += [f"- {titles[k]} (من صفحة الحالة): {'، '.join(v)}" for k, v in pool.boxes.items()]
    if summary.get("review_applied") or summary.get("review_kept"):
        lines.append(f"- طُبّقت من مراجعتك: {summary['review_applied']} تصحيحاً و{summary['review_kept']} تأكيداً")
    if summary.get("review_conflicts_new"):
        lines.append(f"- إجابات لم تُطبَّق لأن الصفحة تغيّرت (تظهر من جديد): {summary['review_conflicts_new']}")
    if summary.get("review_items"):
        lines.append(f"- مواضع تنتظر مراجعتك: {summary['review_items']}")
    if summary.get("helper_checked"):
        lines.append(f"- صفحات قرأتها نماذجك على الجهاز قراءةً ثانية: {summary['helper_checked']}")
    if summary.get("free_done"):
        lines.append(f"- صفحات أنهتها النماذج المجانية من آخر الكتاب بجانب نماذجك: {summary['free_done']}")
    lines += [f"- {n}" for n in notes]
    if summary["state"] == "helper":
        lines += ["", f"**بقيت {len(summary['pages_helper'])} صفحة تعمل عليها نماذجك على الجهاز من أول الكتاب، "
                  "والنماذج المجانية من آخره.** يكمل الكتاب حين تنتهي."]
    if summary["state"] == "reader":
        lines += ["", f"**نفد اليوم رصيد أحد القارئين، فتنتظر {len(summary['pages_waiting'])} صفحة قراءتها الثانية منه** "
                  "بعد تجدد رصيده. يبدأ الكتاب التالي الآن بالقارئ الآخر، ثم يكمل هذا الكتاب."]
    if summary["state"] == "paused":
        lines += ["", "**توقف التشغيل لانتهاء الحد اليومي لـ Gemini.** ما أُنجز محفوظ، "
                  "وتشغيل نفس الكتاب بعد منتصف الليل (بتوقيت أمريكا الغربي) يكمل الباقي فقط."]
    if summary["pages_missing"]:
        lines += ["", "صفحات لم تُقرأ: " + compress(summary["pages_missing"])]
    if summary["pages_unsure"]:
        lines += ["", "صفحات مشكوك فيها (اختلفت القراءتان ولم يُحسم الخلاف، أو يبدو أن فيها سطراً ساقطاً): "
                  + compress(summary["pages_unsure"])]
        # A few of the unsettled differences, so the exact words can be checked in the book.
        for page, sample in list(summary["samples"].items())[:30]:
            short = lambda t: (t[:60] + " …") if len(t) > 60 else (t or "—")
            pairs = "؛ ".join(f"«{short(x)}» / «{short(y)}»" for x, y in sample)
            why = summary.get("reasons", {}).get(page, "")
            lines.append(f"- ص {page}: {pairs}" + (f"  [{why}]" if why else ""))
    if summary["state"] == "done":
        lines += ["", "**كل الصفحات اكتملت.**"]
    report = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)


def write_status(path, summary, title, epub_name, quota_day=""):
    """status.json for the Drive inbox (see docs/drive-inbox.md)."""
    if not path:
        return
    data = {k: v for k, v in summary.items() if k not in ("counts", "samples", "reasons")}
    data.update(book=title, epub=epub_name or "",
                updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    if summary["state"] in ("paused", "helper", "reader", "time", "boxes"):
        # The Pacific date the quota ran out on; the inbox starts again the day after.
        data["quota_day"] = quota_day or pacific_day()
    if summary["state"] in ("helper", "reader", "time", "boxes"):
        # Written as "paused" so an older inbox script (which knows none of them) still
        # waits for the next day; the current one parks the book on waiting_for ("time": it
        # starts the next run at once; only runs it gave a live file to stop for time).
        data.update(state="paused", waiting_for=summary["state"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def compress(pages):
    out, start, prev = [], None, None
    for p in pages:
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = p
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return "، ".join(out)


COVER_PROMPT = """هذه أول صفحات كتاب ممسوح ضوئياً، بالترتيب، وكل صورة صفحة واحدة: الصورة 1 هي الصفحة 1، وهكذا.
أيها **غلاف الكتاب** (الواجهة الخارجية: عليها عنوان الكتاب بخط كبير، وغالباً زخرفة أو لون أو اسم الناشر)؟
صفحة العنوان الداخلية البيضاء ليست غلافاً إلا إن لم يوجد غلاف. الصفحة الفارغة أو الختم أو العلامة المائية وحدها ليست غلافاً.
أجب بـ JSON فقط: {"cover": رقم الصفحة، أو 0 إن لم يكن بينها غلاف}"""

COVER_PAGES = 5


@pdf_locked
def page_thumb(doc, index):
    return doc[index].get_pixmap(dpi=60).tobytes("png")


def pick_cover_page(doc, pool, cache_dir):
    """Index of the cover among the first pages: one request showing them together,
    remembered in cover.json. The first page when no model can tell."""
    path = os.path.join(cache_dir, "cover.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("index", 0)
    count = min(COVER_PAGES, len(doc))
    if not pool or count < 2:
        return 0
    thumbs = [page_thumb(doc, i) for i in range(count)]
    for model in (m for m in (pool.model, pool.second) if pool.available(m)):
        try:
            text, _ = pool.read(thumbs, COVER_PROMPT, model)
            m = re.search(r"\{.*\}", text, re.S)
            n = int(json.loads(m.group(0))["cover"]) if m else 0
        except Exception as e:
            print(f"  {model} could not pick the cover: {str(e)[:120]}")
            continue
        index = n - 1 if 1 <= n <= count else 0
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"index": index, "model": model}, f)
        print(f"Cover: page {index + 1}")
        return index
    return 0


def extract_cover_from_pdf(doc, cover_path, index=0):
    try:
        if len(doc) > index:
            png, _ = page_image(doc, doc[index])
            with open(cover_path, "wb") as f:
                f.write(png)
            return True
    except Exception as e:
        print(f"Warning: Could not extract cover from PDF: {e}")
    return False


EXIT_CODES = {"done": 0, "incomplete": 2, "paused": 3, "helper": 5, "reader": 6, "time": 7, "boxes": 8}


def load_pins(path, book_hash):
    """The pages the user answered about, from human.json: ({page: pin}, human.json, notes)."""
    data = review.load(path) if path else None
    if not data:
        return {}, {}, []
    if data.get("book_hash") != book_hash:
        return {}, {}, ["ملف المراجعة السابق لكتاب آخر، فلم يُستعمل"]
    pins = {}
    for page, pin in (data.get("pages") or {}).items():
        if isinstance(pin, dict) and isinstance(pin.get("text"), str) and page.isdigit():
            pins[int(page)] = pin
    return pins, data, []


def review_pages_data(filepath, cache_dir, pages):
    """For the pages under review: their images ({page: PNG}) and, for review.build, their
    size, the gutter between their columns and Tesseract's word boxes (cached)."""
    data, images = {}, {}
    local = open_pdf(filepath)
    try:
        for p in pages:
            page = local[p - 1]
            png, pix = page_image(local, page)
            right = split_rects(page, pix)[0]
            # The first clip is the right column when the page has two.
            gutter = right.x0 * pix.width / page.rect.width if right.x0 > 0 else None
            images[p] = png
            data[p] = {"w": pix.width, "h": pix.height, "gutter": gutter}
    finally:
        close_pdf(local)
    if not shutil.which("tesseract"):
        return data, images
    tess_dir = os.path.join(cache_dir, "tesseract")
    os.makedirs(tess_dir, exist_ok=True)

    def boxes(p):
        path = os.path.join(tess_dir, f"{p:04d}.tsv")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
        tsv = tesseract_tsv(images[p])
        with open(path, "w", encoding="utf-8") as f:
            f.write(tsv)
        return tsv

    with ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as tp:
        for p, tsv in zip(pages, tp.map(boxes, pages)):
            data[p]["tsv"] = tsv
    return data, images


def page_heads(cache_dir, pages):
    """The first lines Tesseract saw on each page ({page: [words of each line]}), to tell
    running headers."""
    heads = {}
    for p in pages:
        path = os.path.join(cache_dir, "tesseract", f"{p:04d}.txt")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                lines = [review.norms(l) for l in f.read().split("\n")]
            heads[p] = [l for l in lines if len(l) >= 2][:3]
    return heads


def page_jpeg(filepath, page_no):
    """The page image the helper's models look at: the one Gemini gets, as JPEG."""
    local = open_pdf(filepath)
    try:
        _, pix = page_image(local, local[page_no - 1])
        with PDF_LOCK:
            return pix.tobytes("jpg", jpg_quality=90)
    finally:
        close_pdf(local)


def free_second(rec):
    """The free models' second reading of a page, when the helper's models are to settle it."""
    b = rec.get("b") or {}
    return b if b.get("status") == "ok" and b.get("via") != "helper" and b.get("text") else None


def send_readings(box, filepath, records, cache_dir, lv=live.NULL, pages=None):
    """The free models' readings the helper has not got yet: pNNNN.a.json, with the lines
    Tesseract saw that it may have dropped (as the settling request lists them), and
    pNNNN.b.json, a second reading the Flash models could not settle on. pages: only these.
    A reading counts as sent only while the book's folder has it: a folder made anew for the
    book (2026-10-23) has none of the readings sent to the one before."""
    def owed(r, key, reading):
        return reading.get("up") != helperbox.sha(reading["text"]) or not box.has(f"p{r['page']:04d}.{key}.json")

    todo = [r for r in records if r.get("status") == "ok" and (r.get("a") or {}).get("via") != "helper"
            and (pages is None or r["page"] in pages) and helperbox.needs_helper(r, CHECK_VERSION)
            and (owed(r, "a", r["a"]) or (free_second(r) and owed(r, "b", free_second(r))))]
    if not todo or not box.fresh():
        return
    vocab = book_vocab(records)
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as tp:
        tess = dict(zip((r["page"] for r in todo),
                        tp.map(lambda r: tesseract_for(filepath, cache_dir, r["page"]), todo)))
    payloads = {}
    for r in todo:
        t = tess[r["page"]]
        payloads[r["page"]] = {"page": r["page"], "model": r["a"]["model"], "text": r["a"]["text"],
                               "spread": bool(r.get("spread")),
                               "missing": (missing_lines(t, r["a"]["text"], vocab) if t else [])[:5],
                               "at": helperbox.now()}
    seconds = {r["page"]: {"page": r["page"], "model": free_second(r)["model"], "text": free_second(r)["text"],
                           "at": helperbox.now()} for r in todo if free_second(r)}
    sent = box.put_readings(payloads)
    sent_b = box.put_readings(seconds, "b")
    for r in todo:
        if r["page"] in sent:
            r["a"]["up"] = helperbox.sha(r["a"]["text"])
        if r["page"] in sent_b:
            r["b"]["up"] = helperbox.sha(r["b"]["text"])
        if r["page"] in sent or r["page"] in sent_b:
            save_record(cache_dir, r)
    print(f"Helper: {len(sent)} first and {len(sent_b)} second readings sent")
    if sent or sent_b:
        lv.note("", "sent", kind="helper", n=len(set(sent) | set(sent_b)))


def tell_helper(box, args, total, recs, lite_out, share=()):
    """book.json: the pages the helper's models may still work on ("need"), and those
    without a first reading they could settle on ("need_a"). The pages the free models take
    in this run (share) are left out of it while the run works on them. Nothing when the
    helper is off for this run, so it stops working on the book."""
    need = sorted(p for p, r in recs.items() if helper_can(r, box) and p not in share) if box.transcribe else []
    box.put_json("book.json", {"v": 1, "title": args.title, "order": getattr(args, "order", "") or "",
                               "total": total, "need": need,
                               "need_a": [p for p in need if box.reads1 and helperbox.needs_first_reading(recs[p])],
                               "lite_out": bool(lite_out), "at": helperbox.now(),
                               "run": os.environ.get("GITHUB_RUN_ID", "")})
    return need


# While the helper's models work on a book, the free models start no page after this long:
# the run's Drive token lasts about an hour, and the pages they leave must go back to the
# helper's models (book.json) before it runs out. The Drive inbox starts the next run soon.
FREE_PASS_SECONDS = 35 * 60
# The pages the free models leave during their pass go to the helper's models at most this often.
HAND_BACK_SECONDS = 60


# With the live view on (live.py), the free models alone start no page work after the run's
# Drive token would run out, less a margin for the work under way and the last writes; the
# last part of that time is for settling only, so the pages read are settled in the same run.
TOKEN_GUESS_SECONDS = 55 * 60
TOKEN_MARGIN_SECONDS = 8 * 60
SETTLE_SECONDS = 12 * 60
# Less time than this left on the token: no stop (an odd token never makes runs loop).
MIN_RUN_SECONDS = 5 * 60


def run_stops(token, now=None, left=None):
    """{"read": t, "settle": t} (epoch seconds) for ModelPool.stops, or {} when the token's
    time left is too short to plan with. left: the token's seconds left (asked of Google)."""
    now = time.time() if now is None else now
    left = live.token_seconds_left(token) if left is None else left
    budget = (TOKEN_GUESS_SECONDS if left is None else left) - TOKEN_MARGIN_SECONDS
    if budget < MIN_RUN_SECONDS:
        return {}
    settle = min(SETTLE_SECONDS, budget * 0.3)
    return {"read": now + budget - settle, "settle": now + budget}


def nobody_for(pool, box, records, dual):
    """True when no model in any box, free or on the user's computer (box), could take the
    next step of a page left waiting, whatever its quota: the user emptied those boxes."""
    reads1, reads2 = bool(box and box.reads1), bool(box and box.reads2)
    settles = bool(box and box.settles)
    for r in records:
        if review.pin_of(r) or r.get("status") in ("empty", "text-layer"):
            continue
        if r.get("status") != "ok":
            if pool.reading("a") or reads1 or (dual and pool.reading("b")):
                return False
        elif dual and (r.get("b") or {}).get("status") != "ok":
            k = owed_step(r)
            if [m for m in pool.reading(k) if m != (r.get("a") or {}).get("model")] or (reads2 if k == "b" else reads1):
                return False
        elif (r.get("check") or {}).get("v") != CHECK_VERSION:
            if pool.boxes.get("settle") or settles or r["a"].get("refused"):
                return False
    return True


def free_share(recs, half=True, can=None, dev=None):
    """The pages the free models take in a run while the helper's models work on the book:
    those every model in the helper's box handed back, then the later half of the pages left
    (the helper's models work from the start of the book), the last page first. half=False:
    every page left. can(rec): the free models can take this page's next step now (free_can);
    the others are not taken, so the helper's models get them at once. dev(rec): the helper's
    models can take it too (helper_can); only those pages are shared by halves, the free
    models take the others whole."""
    ok = (lambda p: True) if can is None else (lambda p: can(recs[p]))
    left = sorted(p for p, r in recs.items() if helperbox.needs_helper(r, CHECK_VERSION) and ok(p))
    back = sorted((p for p, r in recs.items() if r and r.get("hskip") and r.get("status") not in ("empty", "text-layer")
                   and not review.pin_of(r) and not helperbox.settled(r, CHECK_VERSION) and ok(p)), reverse=True)
    if half:
        both = [p for p in left if dev is None or dev(recs[p])]
        left = sorted(set(left) - set(both)) + both[(len(both) + 1) // 2:]
    return back + sorted(left, reverse=True)


def free_can(pool, rec, lite):
    """The free models can take this page's next step in this run: a model in its box with
    quota left reads it, gives its second reading, or settles its two readings (lite: the Lite
    readers, and Gemma, may settle; a page handed back by the helper's models always may)."""
    if rec is None or rec.get("status") != "ok":
        return pool.can_read("a") or pool.can_read("b")
    if (rec.get("b") or {}).get("status") != "ok":
        return pool.can_read(owed_step(rec), (rec.get("a") or {}).get("model", ""))
    if rec["a"].get("refused") or rec.get("hfinal"):
        # Settled without asking a model (settle_state, check_helper_page).
        return True
    return bool(pool.settlers(lite or bool(rec.get("hskip"))))


def helper_can(rec, box):
    """The helper's models can take this page's next step: its first reading (box.reads1),
    its second (box.reads2), or settling its two readings (box.settles)."""
    if not helperbox.needs_helper(rec, CHECK_VERSION):
        return False
    if not rec or rec.get("status") != "ok":
        return box.reads1
    if (rec.get("b") or {}).get("status") != "ok":
        return box.reads2
    return box.settles


# The run's steps (the user's design, 2026-10-23: "separate the boxes so that nobody waits"):
# each step has its own workers, and each takes the page that has waited longest for that step
# in any book of the run, the oldest book's first. "a": the first reading, "b": the second,
# "c": the check, and settling when the readings differ.
STEPS = ("a", "b", "c")


class Steps:
    """The pages waiting for each step of the run, oldest book first, and the work going on.
    A page goes through a step at most once in a run, once more when the user changed the
    boxes (rescan). Workers stop when nothing waits and nothing is being worked on."""

    def __init__(self, rescan=None):
        self.cv = threading.Condition()
        self.queues = {k: [] for k in STEPS}
        # Each page waiting, with its place in its step's queue.
        self.queued = {}
        self.tried = set()
        # The pages being worked on now, by step: never queued again meanwhile (rescan).
        self.working = set()
        self.busy = 0
        self.error = None
        self.rescan = rescan or (lambda steps: False)

    def put(self, step, item, back=False):
        """back: after the pages that waited for this step, last page first (the second reading
        box takes the pages nobody read yet from the end of each book, so the two reading
        boxes meet in the middle)."""
        with self.cv:
            key = (step,) + tuple(item)
            order = (1, item[0], -item[1]) if back else (0,) + tuple(item)
            if key in self.tried or key in self.working or self.queued.get(key, order) < order:
                return False
            if self.queued.get(key) == order:
                return False
            # A page waiting at the back moves up (read once, it waits for its other reading).
            self.queued[key] = order
            heapq.heappush(self.queues[step], (order, tuple(item)))
            self.cv.notify_all()
            return True

    def forget(self, step, item):
        """The page may go through this step once more in this run (put again at once)."""
        with self.cv:
            self.tried.discard((step,) + tuple(item))
            self.working.discard((step,) + tuple(item))

    def did(self, step, item):
        """The page already went through this step in this run."""
        return (step,) + tuple(item) in self.tried

    def take(self, step):
        """The next page for this step, or None once the run's steps are over."""
        with self.cv:
            while True:
                if self.error:
                    return None
                # The user changed the boxes: the pages passed over go to the models now there.
                self.rescan(self)
                while self.queues[step] and self.queued.get((step,) + self.queues[step][0][1]) != self.queues[step][0][0]:
                    # Its place moved up: this one is left over.
                    heapq.heappop(self.queues[step])
                if self.queues[step]:
                    item = heapq.heappop(self.queues[step])[1]
                    self.queued.pop((step,) + item)
                    self.tried.add((step,) + item)
                    self.working.add((step,) + item)
                    self.busy += 1
                    return item
                if not self.busy and not self.queued:
                    self.cv.notify_all()
                    return None
                self.cv.wait(1)

    def done(self, step, item, error=None):
        with self.cv:
            self.working.discard((step,) + tuple(item))
            self.busy -= 1
            if error and not self.error:
                self.error = error
            self.cv.notify_all()


def run_steps(pool, books, workers):
    """The free models' work on every book of the run: each step's workers take the oldest page
    waiting for it, and a page goes on to its next step at once (a page read twice is settled
    while the next pages are read). Raises the first error a worker met."""
    gen = [pool.box_gen]

    def rescan(steps):
        if pool.box_gen == gen[0]:
            return False
        gen[0] = pool.box_gen
        steps.tried.clear()
        for b in books:
            b.scan(steps, pool)
        return True

    steps = Steps(rescan)
    for b in books:
        b.scan(steps, pool)

    def worker(step):
        while True:
            item = steps.take(step)
            if item is None:
                return
            error = None
            try:
                b = books[item[0]]
                with live.in_book(b.index):
                    nxt = b.do(step, item[1], pool, steps)
                # Before this one counts as done, so the steps never end in between.
                for k, back in nxt or ():
                    steps.put(k, item, back)
            except Exception as e:
                error = e
            steps.done(step, item, error)

    threads = [threading.Thread(target=worker, args=(step,), daemon=True) for step in STEPS for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if steps.error:
        raise steps.error
    return steps


class Book:
    """One book of the run. The Drive inbox sends the oldest unfinished books together (the
    first is the run's own), and the models of each box take the oldest page waiting for
    their step in any of them (run_steps). index: its place in the run."""

    def __init__(self, index, filepath, output_path, args):
        self.index = index
        self.filepath = filepath
        self.output_path = output_path
        self.args = args
        self.doc = pymupdf.open(filepath)
        self.selected = parse_pages(args.pages, len(self.doc))
        self.hash = file_sha(filepath)
        self.cache_dir = os.path.join(args.work_dir, f"{self.hash}-{CACHE_VERSION}")
        os.makedirs(self.cache_dir, exist_ok=True)
        print(f"{len(self.doc)} pages, processing {len(self.selected)}; progress saved in {self.cache_dir}")
        self.pins, self.human, self.notes = load_pins(args.human, self.hash)
        # The answers of the user's own models (the helper's review box) count like the user's,
        # but the user's answer to the same place wins.
        self.answers = {}
        for path in (args.judge_answers, args.answers):
            got = (review.load(path) or {}).get("answers") if path else None
            if isinstance(got, dict):
                self.answers.update(got)
        self.needs_ocr = any(len(self.doc[i].get_text().strip()) < MIN_TEXT_LAYER_CHARS for i in self.selected)
        self.dual = False
        self.box = None
        self.transcribe = False
        self.outs = {}
        self.share = []
        self.start = {}
        self.free_pages = []
        self.lite = True
        self.vocab = set()
        self.applied = {"touched": set(), "results": [], "seen": ""}
        self.free_done, self.free_moved = set(), set()
        self.handed, self.hand_lock, self.hand_at = set(), threading.Lock(), [0.0]
        self.waits = 0
        self.records_now = []
        # The pages that went through the run's check step (check_pages leaves them).
        self.checked = set()
        self.state, self.summary = "", {}

    def rescue(self, pool):
        return pool.rescue if pool else self.args.rescue_model

    def records(self, pool):
        """Every selected page's record as saved ("pending" for one never read)."""
        out = []
        for i in self.selected:
            rec = load_record(self.cache_dir, i + 1, self.rescue(pool))
            out.append(rec or {"page": i + 1, "status": "pending"})
        return out

    def take_helper_files(self, recs):
        """recs: {page: record or None}; the helper's latest files merged in and saved.
        Returns the files, by page."""
        outs = self.box.fetch()
        for p in list(recs):
            if p in outs:
                rec, changed = helperbox.merge(recs[p], outs[p], p, CHECK_VERSION)
                if changed:
                    recs[p] = rec
                    save_record(self.cache_dir, rec)
        return outs

    def back(self, p, lv):
        """A page the free models leave during the run (no model of its box has quota left)
        goes to the helper's models within a minute, with its readings."""
        if not self.transcribe:
            return
        with self.hand_lock:
            self.handed.add(p)
            if time.time() - self.hand_at[0] < HAND_BACK_SECONDS:
                return
            self.hand_at[0] = time.time()
            pages = set(self.handed)
        rescue = self.args.rescue_model
        recs = dict(self.start)
        recs.update({q: load_record(self.cache_dir, q, rescue) for q in pages})
        send_readings(self.box, self.filepath, [r for r in recs.values() if r], self.cache_dir, lv, pages=pages)
        tell_helper(self.box, self.args, len(self.doc), recs, False, [q for q in self.share if q not in pages])

    def helper_second(self, p, rec):
        """The helper's models gave the second reading of a page with no first one yet."""
        hb = (self.outs.get(p) or {}).get("hb")
        return self.transcribe and (rec is None or rec.get("status") != "ok") and helperbox.has_text(hb) and \
            hb["text"].strip() != helperbox.EMPTY

    def next_steps(self, p, rec, steps):
        """The steps this page waits for, from the free models, as [(step, back)] (Steps.put):
        none when it waits for nobody in this run (done, pinned, the helper's, or a step it
        already went through). A page nobody read waits for both reading boxes: whichever
        takes it first gives its first reading, the other its second."""
        if p in self.pins or (rec and review.pin_of(rec)):
            return []
        if rec is not None and rec.get("status") in ("empty", "text-layer"):
            return []
        if rec is None or rec.get("status") != "ok":
            if self.helper_second(p, rec):
                # The helper's models gave its second reading: only the first box reads it.
                return [("a", False)]
            return [("a", False)] + ([("b", True)] if self.dual else [])
        if (rec.get("check") or {}).get("v") == CHECK_VERSION:
            return []
        b = rec.get("b")
        second = (b or {}).get("status") == "ok"
        owed = owed_step(rec)
        if self.dual and not second and not steps.did(owed, (self.index, p)) and \
                (b is None or b.get("status") in ("failed", "suspicious")):
            return [(owed, False)]
        if self.transcribe:
            # The free models settle their two readings (or a refused page's one); a page the
            # helper's models settled is checked without asking a model.
            return [("c", False)] if second or rec["a"].get("refused") or not self.dual else []
        if from_helper(rec) or rec.get("hfinal") or (self.dual and "b" not in rec):
            # Their readings are checked after the steps (check_pages); a page with no second
            # reading waits for one.
            return []
        return [("c", False)]

    def scan(self, steps, pool):
        for p in self.free_pages:
            rec = load_record(self.cache_dir, p, pool.rescue)
            if rec and not review.pin_of(rec):
                upgrade_check(rec)
            for k, back in self.next_steps(p, rec, steps):
                steps.put(k, (self.index, p), back)

    def do(self, step, p, pool, steps):
        """One step of one page by the free models. Returns the page's next steps (next_steps).
        The two reading boxes may read one page at the same time (save_first)."""
        rec = load_record(self.cache_dir, p, pool.rescue)
        if step == "b" and self.helper_second(p, rec):
            # The helper's models gave its second reading: only the first box reads it.
            return []
        if step in ("a", "b") and self.transcribe and not free_can(pool, rec, self.lite):
            # Nobody here can take its next step now (out of quota): the helper's models.
            self.back(p, pool.live)
            return []
        local = open_pdf(self.filepath)
        try:
            if step in ("a", "b"):
                had = (bool(rec) and rec.get("status") == "ok", bool(rec) and (rec.get("b") or {}).get("status") == "ok")
                given = self.helper_second(p, rec)
                rec = read_page(pool, local, p - 1, self.cache_dir, len(self.doc), self.dual and not given, step=step)
                if given and rec and rec.get("status") == "ok":
                    rec, changed = helperbox.merge(rec, self.outs[p], p, CHECK_VERSION)
                    if changed:
                        save_record(self.cache_dir, rec)
                if rec and rec.pop("again", False):
                    steps.forget(step, (self.index, p))
                    return [(step, False)]
                if rec:
                    self.vocab.update(norm_words(rec.get("text") or ""))
                    self.vocab.update(norm_words((rec.get("b") or {}).get("text") or ""))
                    if (rec.get("status") == "ok", (rec.get("b") or {}).get("status") == "ok") != had:
                        self.free_moved.add(p)
                return self.next_steps(p, rec, steps) if rec else []
            tess = tesseract_for(self.filepath, self.cache_dir, p)
            if self.transcribe and rec.get("hfinal"):
                result = check_helper_page(rec, tess, self.vocab, pool.live)
            else:
                result = check_page(pool, local, rec, tess, self.vocab, lite=self.lite or bool(rec.get("hskip")))
            if result == "wait":
                # No settler left in this run: the helper's models settle it (send_readings).
                if self.transcribe:
                    self.back(p, pool.live)
                else:
                    with self.hand_lock:
                        self.waits += 1
                return []
            save_record(self.cache_dir, rec)
            pool.live.page(p, page_state(rec))
            c = rec["check"]
            print(f"  check page {p}: {c['state']} (diffs {c.get('diffs')}, missing {c.get('missing_a')})")
            if result == "done" and self.transcribe:
                with self.hand_lock:
                    self.free_done.add(p)
                    self.free_moved.add(p)
            return []
        finally:
            close_pdf(local)


def generate_epub_from_pdf(filepath, output_path, args, others=()):
    """One book, with the other books of the run (others: (filepath, output_path, args) each,
    the Drive inbox's queue after this book): their pages go through the same steps, each
    step's models taking the oldest page waiting (run_steps). Each book gets its own EPUB,
    report and status; the run's own facts (requests, models out of quota) go with the first.
    Returns the first book's exit code, or the first unfinished book's once it is done."""
    specs = [(filepath, output_path, args)] + list(others)
    books = []
    for i, (path, out, bargs) in enumerate(specs):
        try:
            books.append(Book(i, path, out, bargs))
        except Exception as e:
            if not i:
                raise
            print(f"Book {path} left out of this run: {e}")
            write_status(bargs.status, failed_summary(str(e)), bargs.title, "")
    lead = books[0]

    # The status page's live view (live.py): silent when the run was started without one.
    lv = live.open_live(args.live, os.environ.get("DRIVE_TOKEN", ""), args.control)
    if len(books) > 1:
        lv.set_books([{"file": getattr(b.args, "drive_id", "") or "", "book": b.args.title} for b in books])
    for b in books:
        with live.in_book(b.index):
            for i in b.selected:
                rec = load_record(b.cache_dir, i + 1, args.rescue_model)
                if rec and not review.pin_of(rec) and scrub(rec, drop=not args.review_only):
                    save_record(b.cache_dir, rec)
                lv.page(i + 1, "done" if (i + 1) in b.pins else page_state(rec))
    lv.set_phase("apply" if args.review_only else "start")
    lv.start()

    pool = None
    if any(b.needs_ocr for b in books):
        pool = ModelPool(args.model, args.second_model if args.dual else "", args.rescue_model, args.rpm)
        pool.live = lv
        if not args.review_only:
            # The Flash models settle (strongest first), unless the user's boxes on the status
            # page say who reads and who settles.
            have = list_models(pool)
            pool.set_extra(resolve_models(pool, [m.strip() for m in args.extra_models.split(",") if m.strip()], have))
            boxes = parse_boxes(args.boxes)
            if boxes:
                pool.set_boxes({k: match_models(have, v) if have else v for k, v in boxes.items()})
                print("Free models' boxes: " + "; ".join(f"{k}={','.join(v) or '-'}" for k, v in pool.boxes.items()))
            out = parse_boxes(args.boxes, ("out",)).get("out") or []
            if out:
                pool.set_out(match_models(have, out) if have else out)
                print("Out of quota today (an earlier run found it): " + ", ".join(out))
            # «أعد المحاولة الآن» on the status page during the run: the model works again.
            lv.watch(pool.revive)
            pool.catalog = free_catalog(have)
            roles = [(pool.model, "read1"), (pool.second, "read2")] if not pool.boxes else \
                [(m, "read1") for m in pool.reading("a")] + ([(m, "read2") for m in pool.reading("b")] if pool.second else [])
            settle = pool.boxes.get("settle", []) if pool.strict else pool.boxes.get("settle") or pool.extra
            for model, role in roles + [(m, "settle") for m in settle] + \
                    ([(pool.rescue, "rescue")] if pool.rescue_for() else []):
                lv.role(model, role)
    dual = bool(pool and args.dual and args.second_model)

    for b in books:
        b.dual = dual
        b.records_now = b.records(pool)
        # A page the user answered about is theirs: it is never read again, and their answers
        # go first, before any check.
        for r in b.records_now:
            if r["page"] in b.pins:
                pinned = review.pin_record(r if r.get("status") != "pending" else None, r["page"], b.pins[r["page"]],
                                           CHECK_VERSION)
                r.clear()
                r.update(pinned)
                save_record(b.cache_dir, r)
        b.applied = review.overlay(b.records_now, b.answers, CHECK_VERSION, (b.human or {}).get("answers_seen", ""))
        for r in b.records_now:
            if r["page"] in b.applied["touched"]:
                save_record(b.cache_dir, r)
        if b.applied["results"]:
            print(f"Answers: {len(b.applied['results'])}, pages pinned: {compress(sorted(b.applied['touched']))}")

    # The helper on the user's computer (docs/helper-boxes-plan.md): its files are taken
    # first, so the free models do not read again what its models already read.
    for b in books:
        if not (pool and dual and b.args.helper and not args.review_only):
            continue
        with live.in_book(b.index):
            prepare_helper(b, pool, lv)
    transcribe = any(b.transcribe for b in books)
    if pool and pool.strict and not args.review_only:
        # A model the user puts in a box during the run works there within seconds; one taken
        # out takes no new page (boxes_changed).
        modes = helper_modes(lead.box)
        lv.watch(pool.revive, lambda free, helper: boxes_changed(pool, have, modes, free, helper))

    if lv.on and pool and not args.review_only and not transcribe:
        # The live view needs the run's Drive token: no page work starts after it would run
        # out, and the Drive inbox starts the next run at once (state "time").
        pool.stops = run_stops(os.environ.get("DRIVE_TOKEN", ""))
        lv.set_stops(**pool.stops)

    gen = pool.box_gen if pool else 0
    if pool and not args.review_only:
        for b in books:
            if not b.transcribe:
                b.free_pages = [i + 1 for i in b.selected]
            b.vocab = book_vocab([r for r in b.records_now if r])
        if not transcribe:
            lv.set_phase("read", pages=sum(1 for b in books for r in b.records_now
                                           if r["page"] not in b.pins and page_state(r) in ("unread", "read1")))
        # With the helper's models on, no page starts after the free models' time (the Drive
        # token must last for handing the pages left to them), and a model's wait for its
        # quota ends there too.
        stops, time_up = dict(pool.stops), pool.time_up
        if transcribe:
            deadline = min(b.box.drive.started for b in books if b.transcribe) + FREE_PASS_SECONDS
            pool.stops = {"read": deadline, "settle": deadline}
        try:
            steps = run_steps(pool, books, args.workers)
            for b in books:
                b.checked = {k[2] for k in steps.tried if k[0] == "c" and k[1] == b.index}
        finally:
            if transcribe:
                pool.stops, pool.time_up = stops, time_up
        for b in books:
            if b.waits:
                with live.in_book(b.index):
                    # One line for them all: no settler is left in this run (quota, or its time is up).
                    lv.note("", "wait", kind="page", n=b.waits)
            if b.transcribe:
                print(f"Free models settled {len(b.free_done)} pages")

    codes = []
    for b in books:
        try:
            with live.in_book(b.index):
                codes.append(finish_book(b, pool, lv, args, gen, b is lead))
        except Exception as e:
            if b is lead:
                raise
            print(f"Book {b.filepath} could not be finished: {e}")
            write_status(b.args.status, failed_summary(str(e)), b.args.title, "")
            codes.append(1)
    lv.finish(lead.state, unsure=len(lead.summary["pages_unsure"]), waiting=len(lead.summary["pages_waiting"]),
              helper=len(lead.summary["pages_helper"]), missing=len(lead.summary["pages_missing"]),
              out=lead.summary.get("models_out"))
    return next((c for c in codes if c), 0)


def failed_summary(reason):
    """status.json of a book of the run that could not be converted at all."""
    return {"state": "failed", "reason": reason[:300], "pages_total": 0, "pages_done": 0, "pages_unsure": [],
            "pages_missing": [], "pages_waiting": [], "pages_retry": [], "pages_helper": [], "pages_human": []}


def prepare_helper(b, pool, lv):
    """The book's folder for the helper's models: their files merged in, and, while they
    transcribe, the pages split between them and the free models (free_share), with the page
    images and the free models' readings of their pages."""
    args = b.args
    b.box = helperbox.HelperBox.open(args.helper, os.environ.get("DRIVE_TOKEN", ""), b.cache_dir)
    b.transcribe = bool(b.box.transcribe)
    b.lite = not b.box.settles if b.transcribe else True
    start = {}
    for i in b.selected:
        rec = load_record(b.cache_dir, i + 1, args.rescue_model)
        if (i + 1) in b.pins or (rec and (rec.get("status") in ("text-layer", "empty") or review.pin_of(rec))):
            continue
        with PDF_LOCK:
            layer = len(b.doc[i].get_text("text", sort=True).strip())
        if rec is not None or layer < MIN_TEXT_LAYER_CHARS:
            start[i + 1] = rec
    b.start = start
    b.outs = b.take_helper_files(start)
    # With the helper's models on, the free models and they share the book: the free models
    # take its end in this run, and the helper's models do not touch it meanwhile. Only the
    # pages the free models can move on now (a model in their box with quota): the others are
    # the helper's models' at once.
    can = lambda r: free_can(pool, r, not b.box.settles)
    dev = lambda r: helper_can(r, b.box)
    b.share = free_share(start, can=can, dev=dev) if b.transcribe else []
    need = tell_helper(b.box, args, len(b.doc), start, False, b.share)
    if b.transcribe:
        b.free_pages = sorted(b.share)
        b.box.start_images(sorted(set(need) | set(b.share)), lambda p: page_jpeg(b.filepath, p))
        # Their pages' readings go now, not at the end of the run (on 2026-10-23 a run whose
        # Drive token ran out first sent 494 images and no reading: nothing to settle on).
        send_readings(b.box, b.filepath, [r for r in start.values() if r], b.cache_dir, lv, pages=set(need))
        print(f"Free models: {len(b.share)} pages from the end of the book; the helper's models: {len(need)}")
        lv.set_phase("free", pages=len(b.share), helper=len(need), reads=b.box.reads, settles=b.box.settles,
                     reads1=b.box.reads1, reads2=b.box.reads2)


def finish_book(b, pool, lv, args, gen, lead):
    """After the run's steps: the helper's files and turn, the last checks, the review places,
    and the book's EPUB, report and status. Returns its exit code."""
    bargs = b.args
    records = []
    for i in b.selected:
        rec = load_record(b.cache_dir, i + 1, args.rescue_model)
        if rec is None and not args.review_only:
            local = open_pdf(b.filepath)
            try:
                rec = text_layer_record(local, i, b.cache_dir)
            finally:
                close_pdf(local)
        records.append(rec or {"page": i + 1, "status": "pending"})
    helper_wait = set()
    box, transcribe = b.box, b.transcribe
    if box:
        lv.set_phase("helper")
        if transcribe:
            send_readings(box, b.filepath, records, b.cache_dir, lv)
        # What the helper did while the free models worked.
        recs = {r["page"]: r for r in records if r.get("status") not in ("text-layer", "empty")}
        b.take_helper_files(recs)
        records = [recs.get(r["page"], r) for r in records]
    recheck_lines(records, b.filepath, b.cache_dir, lv)
    if pool and not args.review_only:
        lv.set_phase("check", pages=sum(1 for r in records if page_state(r) in ("read2", "read1")))
        helper_wait = check_pages(pool, b.filepath, records, b.cache_dir, b.dual, args.workers,
                                  transcribe and box.reads2, transcribe and box.settles, done=b.checked)
    free_left = False
    if transcribe:
        # Every page left goes back to the helper's models; the free models take their share
        # again in the next run.
        left = {r["page"]: r for r in records if r.get("status") not in ("text-layer", "empty")}
        helper_wait |= {p for p, r in left.items() if helper_can(r, box)}
        tell_helper(box, bargs, len(b.doc), left, pool.free_out())
        # Worth another run right away: the free models finished pages (or the user sent one
        # back to work) and still have something they can do.
        free_left = (bool(b.free_moved) or bool(pool.revived)) and \
            bool(free_share(left, can=lambda r: free_can(pool, r, not box.settles), dev=lambda r: helper_can(r, box)))
    notes = b.notes
    if box:
        box.wait_images()
        notes = notes + [f"المساعد: {n}" for n in box.notes]

    review_pages = images = None
    applied = b.applied
    if bargs.review_out:
        unsure = [r["page"] for r in records if (r.get("check") or {}).get("state") == "unsure" and r.get("text")]
        data, images = review_pages_data(b.filepath, b.cache_dir, unsure)
        vocab = set()
        for r in records:
            vocab.update(norm_words(r.get("text") or ""))
            vocab.update(norm_words((r.get("b") or {}).get("text") or ""))
        review_pages, became_human = review.build(records, data, b.answers, applied["results"], vocab,
                                                  page_heads(b.cache_dir, [r["page"] for r in records]))
        for r in records:
            if r["page"] in became_human:
                save_record(b.cache_dir, r)
        print(f"Review: {sum(len(p['items']) for p in review_pages)} places on {len(review_pages)} pages")

    lv.set_phase("build")
    dual = b.dual
    reader_wait, readers_left = waiting_for_reader(None if args.review_only else pool, records, dual)
    summary = summarize(records, dual, review_pages, applied, helper_wait, reader_wait, readers_left)
    if transcribe:
        summary.update(free_left=free_left, free_done=len(b.free_done))
        if summary["state"] == "paused" and free_left:
            # Pages not read yet only because the run's time was up: the next run reads them.
            summary["state"] = "helper"
    if summary["state"] == "paused" and pool and (pool.time_up or pool.revived or pool.box_gen != gen) \
            and pool.any_left():
        # Stopped before the Drive token ran out, with quota left (or the user sent a model back
        # to work, or changed the boxes, during the run): the next run goes on at once.
        summary["state"] = "time"
    if summary["state"] == "paused" and pool and pool.strict and not args.review_only \
            and nobody_for(pool, box, records, dual):
        # No model in any box (free or on the user's computer) can take the waiting pages'
        # next step: the book waits for the user to put one there, not for a new day.
        summary["state"] = "boxes"
    if lead and lv.boxes_at:
        # The boxes this run took during it (Live.watch): a change it did not take makes the
        # Drive inbox start the next run at once.
        summary["boxes_at"] = lv.boxes_at
    summary["stages"] = dict(collections.Counter("helper" if r["page"] in helper_wait else page_state(r)
                                                 for r in records))
    if pool and not args.review_only:
        summary["needs"] = book_needs(pool, records, dual)
    md = assemble(records, review_pages)
    if not bargs.pages:
        md = keep_main_headings(md, pool, b.cache_dir)
    write_report(bargs.report, summary, pool, notes)

    output_path = b.output_path
    md_path = output_path + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    cover_path = output_path + ".cover.png"
    # A review round asks no model to pick the cover: the one picked before, or the first page.
    has_cover = extract_cover_from_pdf(b.doc, cover_path,
                                       pick_cover_page(b.doc, None if args.review_only else pool, b.cache_dir))
    try:
        run_pandoc(md_path, output_path, bargs.title, cover_path if has_cover else None)
        print(f"Page list: {add_page_list(output_path)} pages")
    finally:
        if has_cover and os.path.exists(cover_path):
            os.remove(cover_path)
        if not bargs.keep_markdown and os.path.exists(md_path):
            os.remove(md_path)
    if bargs.review_out:
        review.write(bargs.review_out, review_pages, images, bargs.title, b.hash, records, applied["seen"],
                     applied["results"])
    if pool and lead:
        # Every request of the run (the cover's and the table of contents' too, every book's),
        # and the models whose daily quota it found used up: with the first book only, so the
        # Drive inbox counts them once.
        summary["requests"] = dict(pool.requests)
        summary["models_out"] = sorted(pool.exhausted - pool.missing)
        # The free models this account has, for the status page's boxes; what the boxes are
        # until the user changes them (the readers, then the settlers); and the boxes used.
        # Models this key cannot use (404, or no free quota): the status page stops offering them.
        if pool.missing:
            summary["models_missing"] = sorted(pool.missing)
        if pool.catalog:
            summary["free_models"] = [m for m in pool.catalog if m not in pool.missing]
            # Each Lite reader gives the readings the other cannot (as without boxes).
            readers = [m for m in (pool.model, pool.second) if m]
            summary["free_default"] = {"read1": readers, "read2": readers[::-1] if pool.second else [],
                                       "settle": list(dict.fromkeys(pool.extra + readers))}
        if pool.boxes:
            summary["boxes"] = pool.boxes
    write_status(bargs.status, summary, bargs.title, os.path.basename(output_path), pool.quota_day if pool else "")
    for r in records:
        lv.page(r["page"], "helper" if r["page"] in helper_wait else page_state(r))
    b.state, b.summary = summary["state"], summary
    return EXIT_CODES[summary["state"]]


def generate_epub_from_txt(filepath, output_path, args):
    with open(filepath, 'r', encoding='utf-8') as f:
        text = f.read()

    text = apply_smart_indexing(text)

    md_path = output_path + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        run_pandoc(md_path, output_path, args.title)
    finally:
        if os.path.exists(md_path):
            os.remove(md_path)
    write_status(args.status, plain_summary(), args.title, os.path.basename(output_path))
    return 0


def generate_epub_from_docx(filepath, output_path, args):
    # Pass DOCX directly to Pandoc to preserve chapters and footnotes!
    cmd = [
        "pandoc",
        filepath,
        "-o", output_path,
        "--metadata", f"title={args.title}",
        "-M", "dir=rtl",
        "-M", "lang=ar"
    ]
    print(f"Running pandoc to generate {output_path} from DOCX directly...")
    subprocess.run(cmd, check=True)
    print(f"Successfully generated EPUB: {output_path}")
    write_status(args.status, plain_summary(), args.title, os.path.basename(output_path))
    return 0


def plain_summary():
    return {"state": "done", "pages_total": 0, "pages_done": 0, "pages_unsure": [], "pages_missing": [],
            "pages_waiting": [], "pages_retry": [], "counts": {}, "samples": {}, "reasons": {}}


# What the helper's models do in a run (helperInput_ in Code.gs; the workflow checks it too).
HELPER_SPEC = re.compile(r"^[A-Za-z0-9_-]+:((read1|read2|settle)(\+(read1|read2|settle))*|transcribe|read|import)$")


def queue_books(path, args):
    """The other books of the run (--queue: the Drive inbox's oldest unfinished books after this
    one, prepared by the workflow) as (filepath, output_path, args) each. A book's outputs go in
    its own folder: the EPUB (with its .md), report.md, status.json and review-out."""
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)
    out = []
    for e in entries if isinstance(entries, list) else []:
        pdf = str(e.get("pdf") or "")
        if not pdf.lower().endswith(".pdf") or not os.path.isfile(pdf):
            print(f"Queue: {pdf or '?'} left out (no PDF)")
            continue
        folder = str(e.get("out") or "")
        os.makedirs(folder, exist_ok=True)
        name = os.path.splitext(os.path.basename(pdf))[0]
        bargs = argparse.Namespace(**vars(args))
        helper = str(e.get("helper") or "")
        given = lambda k: str(e.get(k) or "") if e.get(k) and os.path.isfile(str(e.get(k))) else ""
        bargs.__dict__.update(
            title=str(e.get("title") or name), pages="", order=str(e.get("order") or ""),
            report=os.path.join(folder, "report.md"), status=os.path.join(folder, "status.json"),
            review_out=os.path.join(folder, "review-out"), keep_markdown=True,
            answers=given("answers"), judge_answers=given("judge"), human=given("human"),
            helper=helper if HELPER_SPEC.match(helper) else "")
        out.append((pdf, os.path.join(folder, name + ".epub"), bargs))
    return out


def process_file(filepath, output_path, args):
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.pdf':
        others = queue_books(args.queue, args) if args.queue else []
        return generate_epub_from_pdf(filepath, output_path, args, others)
    if ext == '.docx':
        return generate_epub_from_docx(filepath, output_path, args)
    if ext == '.txt':
        return generate_epub_from_txt(filepath, output_path, args)
    raise ValueError(f"Unsupported file type: {ext}")


def main():
    parser = argparse.ArgumentParser(description="Convert PDF, DOCX, TXT to EPUB")
    parser.add_argument("input", help="Path to input file")
    parser.add_argument("-o", "--output", help="Path to output EPUB file (optional)")
    parser.add_argument("--title", help="Book title (defaults to the file name)")
    parser.add_argument("--pages", default="", help="Pages to process, e.g. 1-20,25 (default: all)")
    parser.add_argument("--work-dir", default="work", help="Where per-page OCR progress is kept")
    parser.add_argument("--report", default="report.md", help="Where to write the conversion report")
    parser.add_argument("--model", default=os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash-lite",
                        help="Main reader")
    parser.add_argument("--second-model", default=os.environ.get("GEMINI_SECOND_MODEL") or "gemini-3.1-flash-lite",
                        help="Second, independent reader (its own daily quota)")
    parser.add_argument("--rescue-model", default=os.environ.get("GEMINI_RESCUE_MODEL") or "gemma-4-26b-a4b-it",
                        help="For pages both Gemini models refuse")
    parser.add_argument("--extra-models", default=os.environ.get("GEMINI_EXTRA_MODELS")
                        or os.environ.get("GEMINI_ARBITER_MODELS")
                        or "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3-flash",
                        help="The other free models (small daily quotas): they read and settle pages once "
                             "both Lite readers are out")
    parser.add_argument("--boxes", default="",
                        help="The free models in the status page's boxes, as read1=a,b;read2=c;settle=d,e: only "
                             "they give the first reading, the second, and settle, each first to last")
    parser.add_argument("--single", dest="dual", action="store_false",
                        help="Read each page once (no second reading and no comparison)")
    parser.add_argument("--rpm", type=float, default=10, help="Requests per minute per Gemini model")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--status", default="", help="Where to write status.json (for the Drive inbox)")
    parser.add_argument("--keep-markdown", action="store_true")
    parser.add_argument("--review-out", default="", help="Where to write the places for the user to check")
    parser.add_argument("--answers", default="", help="The user's answers (JSON from the Drive inbox)")
    parser.add_argument("--judge-answers", default="",
                        help="The answers of the user's own models (same form as --answers); the user's answers win")
    parser.add_argument("--human", default="", help="human.json of the previous run: the pages the user answered")
    parser.add_argument("--review-only", action="store_true",
                        help="Apply the answers and rebuild the book from saved pages only: no reading "
                             "or settling request")
    parser.add_argument("--live", default="",
                        help="The Drive file of the status page's live view (live.py); the Drive token is in "
                             "DRIVE_TOKEN. With it, no page work starts after the token would run out")
    parser.add_argument("--control", default="",
                        help="The Drive file of the status page's requests during the run («أعد المحاولة "
                             "الآن» on a free model), read with the live view's token")
    parser.add_argument("--helper", default="",
                        help="The book's Drive folder for the helper on the user's computer, as "
                             "<folder id>:transcribe (its models read and settle), :read (they only read), "
                             ":settle (they only settle; the free models read every page) or "
                             "<folder id>:import (their work so far is used); the Drive token is in DRIVE_TOKEN")
    parser.add_argument("--queue", default="",
                        help="A JSON list of the other books of the run, after this one in the Drive inbox's "
                             "order: [{pdf, out, title, answers, judge, human, helper, order}]. Each box's "
                             "models take the oldest page waiting for their step in any of them")
    parser.add_argument("--order", default="", help="The book's place in the Drive inbox's order (book.json)")
    args = parser.parse_args()

    input_path = args.input
    if not os.path.exists(input_path):
        print(f"Error: File not found - {input_path}")
        sys.exit(1)

    base_name = os.path.splitext(os.path.basename(input_path))[0]
    output_path = args.output or f"{base_name}.epub"
    args.title = args.title or base_name

    print(f"Processing {input_path}...")
    try:
        code = process_file(input_path, output_path, args)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error during conversion pipeline: {e}")
        sys.exit(1)
    sys.exit(code)


if __name__ == "__main__":
    main()
