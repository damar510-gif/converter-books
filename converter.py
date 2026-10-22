import argparse
import hashlib
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


def model_rpm(model, default_rpm):
    # Lite models allow 15 requests a minute; Gemma and the full Flash models fewer.
    return default_rpm if "flash-lite" in model else 4


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
        # The other free models (Flash, small daily quotas): they read and settle once the two
        # Lite readers are out (set_extra).
        self.extra = []
        self.rescue = rescue
        self.rpm = rpm
        self.lock = threading.Lock()
        self.limiters = {}
        self.requests = {}
        self.exhausted = set()
        self.no_tuning = set()
        # Models this account does not have (404): unlike a used-up quota, they never come back.
        self.missing = set()
        self.quota_day = ""

    def available(self, model):
        return bool(model) and model not in self.exhausted

    def set_extra(self, models):
        self.extra = [m for m in dict.fromkeys(models) if m and m not in (self.model, self.second, self.rescue)]

    def readers(self, first=None):
        """Every free reader, the two Lite models first; first: the model to try first."""
        order = [first] if first else []
        return [m for m in dict.fromkeys(order + [self.model, self.second] + self.extra) if m]

    def free_out(self):
        """No free reader has quota left today (Gemma only rescues refused pages)."""
        return not any(self.available(m) for m in self.readers())

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
        self._limiter(model).wait()
        with self.lock:
            self.requests[model] = self.requests.get(model, 0) + 1
        # png: one page image, a list of images (several pages in one request), or None.
        images = [] if png is None else (png if isinstance(png, list) else [png])
        contents = [types.Part.from_bytes(data=i, mime_type="image/png") for i in images] + [prompt]
        response = self.client.models.generate_content(model=model, contents=contents, config=self._config(model))
        finish, text = "", ""
        if response.candidates:
            cand = response.candidates[0]
            finish = str(cand.finish_reason or "")
            # Some models send thought parts even with thinking off; keep only the answer.
            parts = cand.content.parts if cand.content and cand.content.parts else []
            text = "".join(p.text for p in parts if p.text and not getattr(p, "thought", False))
        return text.strip(), finish

    def read(self, png, prompt, model, attempts=3):
        """Returns (text, finish_reason). Raises QuotaExhausted or the last error."""
        last = None
        for attempt in range(attempts):
            if model in self.exhausted:
                raise QuotaExhausted(model)
            try:
                return self._call(model, png, prompt)
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
                        raise QuotaExhausted(model) from e
                    if "perday" in low.replace(" ", "") or "per day" in low or "daily" in low:
                        with self.lock:
                            self.exhausted.add(model)
                            self.quota_day = self.quota_day or pacific_day()
                        print(f"  {model}: daily quota used up")
                        raise QuotaExhausted(model) from e
                    m = re.search(r"retry(?:Delay)?['\"]?\s*[:=]?\s*['\"]?(\d+)", msg, re.I)
                    wait = int(m.group(1)) + 2 if m else 65
                elif "400" in msg and model not in self.no_tuning and (
                        "thinking" in low or "media_resolution" in low or "safety" in low):
                    print(f"  {model} rejected tuning options, retrying without them: {msg[:200]}")
                    self.no_tuning.add(model)
                    wait = 0
                elif any(code in msg for code in ("500", "502", "503", "504")) or "unavailable" in low \
                        or "timed out" in low or "timeout" in low or "deadline" in low:
                    wait = 20 * (2 ** attempt)
                elif "404" in msg or "not_found" in low:
                    # A model name this account does not have: not asked again in this run.
                    with self.lock:
                        self.exhausted.add(model)
                        self.missing.add(model)
                    print(f"  {model}: not found, skipped: {msg[:160]}")
                    raise QuotaExhausted(model) from e
                else:
                    raise
                print(f"  {model}: {msg[:160]} -> retry in {wait}s")
                time.sleep(wait)
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
    for model in chain + ([rescue] if rescue else []):
        if model == rescue and not refused:
            break
        if not pool.available(model):
            quota_skipped = quota_skipped or model != rescue
            continue
        try:
            text, finish = pool.read(png, prompt, model)
            via = "full"
            if not clean_ocr(text) and is_refusal(finish):
                refused = True
                via = "halves"
                text, finish = read_in_halves(pool, page, pix, prompt, model)
        except QuotaExhausted:
            quota_skipped = quota_skipped or model != rescue
            continue
        except Exception as e:
            attempted = True
            error = f"{model}: {str(e)[:250]}"
            continue
        attempted = True
        status, text = judge_reading(text, finish, ink)
        if is_refusal(finish):
            refused = True
        reading = {"model": model, "text": text, "finish": finish, "via": via, "status": status}
        if refused:
            reading["refused"] = True
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


def text_layer_record(doc, index, cache_dir):
    """A page with its own text in the PDF needs no reading: its record, or None."""
    with PDF_LOCK:
        layer = doc[index].get_text("text", sort=True).strip()
    if len(layer) < MIN_TEXT_LAYER_CHARS:
        return None
    rec = {"page": index + 1, "status": "text-layer", "text": layer}
    save_record(cache_dir, rec)
    return rec


def read_page(pool, doc, index, cache_dir, total, dual):
    """First and second reading of one page, saved to cache_dir/NNNN.json.
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
    if not need_a and not need_b:
        return rec

    png, pix = page_image(doc, page)
    ink = ink_ratio(pix)
    spread = pix.width > pix.height * 1.15
    prompt = OCR_PROMPT.format(spread=SPREAD_HINT if spread else "")
    if need_a:
        status, reading, error = read_with(pool, pool.readers(), page, png, pix, prompt, ink, rescue=pool.rescue)
        if status == "pending":
            return None
        rec = {"page": page_no, "ink": round(ink, 4), "spread": spread, "status": status,
               "text": reading["text"] if reading else "", "a": reading, **handed_back}
        if error:
            rec["error"] = error
    if dual and rec["status"] == "ok":
        # The second reading comes from a different model, so its mistakes fall elsewhere.
        # Gemma may give it only when Gemini refused this page.
        others = [m for m in pool.readers(pool.second) if m != rec["a"]["model"]]
        rescue = pool.rescue if rec["a"].get("refused") and rec["a"]["model"] != pool.rescue else ""
        status, reading, error = read_with(pool, others, page, png, pix, prompt, ink, rescue=rescue)
        if status == "pending":
            rec.pop("b", None)
        else:
            rec["b"] = reading or {"model": "", "text": "", "status": "failed"}
            rec["b"]["status"] = status
            if error:
                rec["b"]["error"] = error
    rec.pop("check", None)
    save_record(cache_dir, rec)
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
    """Settling failed: keep the reading that lost fewer lines (the main one on a
    tie). Any disagreement nobody settled marks the page unsure."""
    use_b = bool(b) and len(miss_b) < len(miss_a)
    rec["text"] = b if use_b else a
    left = len(miss_b) if use_b else len(miss_a)
    info["kept"] = "b" if use_b else "a"
    info["sample"] = [list(d) for d in diffs[:3]]
    rec["check"] = {**info, "v": CHECK_VERSION, "state": "unsure" if (left or diffs) else "single"}


def check_page(pool, doc, rec, tess, vocab):
    """Compares the two readings and settles their differences against the image.
    Returns 'done', 'wait' (tomorrow's quota) or 'retry' (temporary API errors)."""
    a = rec["a"]["text"]
    b_rec = rec.get("b") or {}
    b = b_rec.get("text", "") if b_rec.get("status") == "ok" else ""
    miss_a = missing_lines(tess, a, vocab) if tess else []
    miss_b = missing_lines(tess, b, vocab) if tess and b else []
    ratio, diffs = crosscheck.compare(a, b) if b else (0.0, [])
    info = {"ratio": round(ratio, 4), "diffs": len(diffs), "missing_a": len(miss_a)}
    if b:
        info["missing_b"] = len(miss_b)
    if not miss_a and (not b or not diffs):
        rec["text"] = a
        rec["check"] = {**info, "v": CHECK_VERSION, "state": "agreed" if b else "single"}
        return "done"

    # A page Gemini refused was read in halves or by the rescue model; asking
    # Gemini about the whole page again would only be refused again.
    if rec["a"].get("refused"):
        settle_state(rec, info, a, b, diffs, miss_a, miss_b)
        return "done"

    page = doc[rec["page"] - 1]
    png, pix = page_image(doc, page)
    rules = OCR_RULES.format(spread=SPREAD_HINT if pix.width > pix.height * 1.15 else "")
    prompt = crosscheck.reconcile_prompt(rules, a, b, diffs, list(dict.fromkeys(miss_a + miss_b)))
    answered = errored = False
    # Use the Lite reconciler with fewer requests so far, to spread the two daily quotas; the
    # other free models once both are out.
    lite = sorted((m for m in (pool.model, pool.second) if pool.available(m)), key=lambda m: pool.requests.get(m, 0))
    for model in lite + [m for m in pool.extra if pool.available(m)]:
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
        elif not crosscheck.validate_final(text, a, b):
            info["rejected"] = f"{model}: drops text the readings agree on"
        else:
            miss_f = missing_lines(tess, text, vocab) if tess else []
            if len(miss_f) <= min([len(miss_a)] + ([len(miss_b)] if b else [])):
                rec["text"] = text
                info.update(model=model, missing_final=len(miss_f))
                rec["check"] = {**info, "v": CHECK_VERSION, "state": "unsure" if miss_f else "reconciled"}
                return "done"
            info["rejected"] = f"{model}: {len(miss_f)} lines missing (readings: {len(miss_a)}/{len(miss_b)})"
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
    return "done"


def from_helper(rec):
    """The second reading came from the helper's models (helperbox.merge)."""
    return (rec.get("b") or {}).get("via") == "helper"


def check_helper_page(rec, tess, vocab):
    """check_page for a page whose second reading, and settled text, came from the
    helper's models: nothing is asked of Gemini, and their text has to pass the same tests
    as a settled text from Gemini. Returns 'done' or 'wait' (not settled by them yet)."""
    a = rec["a"]["text"]
    b = rec["b"]["text"]
    miss_a = missing_lines(tess, a, vocab) if tess else []
    miss_b = missing_lines(tess, b, vocab) if tess else []
    ratio, diffs = crosscheck.compare(a, b)
    info = {"ratio": round(ratio, 4), "diffs": len(diffs), "missing_a": len(miss_a), "missing_b": len(miss_b)}
    if not miss_a and not diffs:
        rec["text"] = a
        rec["check"] = {**info, "v": CHECK_VERSION, "state": "agreed"}
        return "done"
    final = rec.get("hfinal")
    if not final:
        return "wait"
    model = final.get("model") or rec["b"]["model"]
    text = clean_settled(final["text"])
    if not text:
        info["rejected"] = f"{model}: no answer"
    elif not crosscheck.validate_final(text, a, b):
        info["rejected"] = f"{model}: drops text the readings agree on"
    else:
        miss_f = missing_lines(tess, text, vocab) if tess else []
        if len(miss_f) <= min(len(miss_a), len(miss_b)):
            rec["text"] = text
            info.update(model=model, missing_final=len(miss_f))
            rec["check"] = {**info, "v": CHECK_VERSION, "state": "unsure" if miss_f else "reconciled"}
            return "done"
        info["rejected"] = f"{model}: {len(miss_f)} lines missing (readings: {len(miss_a)}/{len(miss_b)})"
    settle_state(rec, info, a, b, diffs, miss_a, miss_b)
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


def check_pages(pool, filepath, records, cache_dir, dual, workers, transcribe=False):
    """Runs check_page on every read page whose check is missing or out of date.
    Pages the user answered about are theirs and never checked again.
    A page with the helper's second reading is checked with check_helper_page. With
    transcribe (the helper's models are on), Gemini never settles a page the helper has
    not handed back: such pages wait for the helper. Returns the pages waiting for it."""
    for r in records:
        if not review.pin_of(r):
            upgrade_check(r)
    due = [r for r in records if r.get("status") == "ok" and r.get("a") and not review.pin_of(r)
           and (r.get("check") or {}).get("v") != CHECK_VERSION]
    by_helper = [r for r in due if from_helper(r)]
    waiting = {r["page"] for r in due if transcribe and not from_helper(r) and not r.get("hskip")}
    todo = [r for r in due if not from_helper(r) and r["page"] not in waiting and (not dual or "b" in r)]
    if not todo and not by_helper:
        return waiting
    vocab = book_vocab(records)
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as tp:
        tess = dict(zip((r["page"] for r in todo + by_helper),
                        tp.map(lambda r: tesseract_for(filepath, cache_dir, r["page"]), todo + by_helper)))
    for r in by_helper:
        if check_helper_page(r, tess[r["page"]], vocab) == "done":
            save_record(cache_dir, r)
            print(f"  check page {r['page']}: {r['check']['state']} (helper, diffs {r['check']['diffs']})")
        elif transcribe and not r.get("hskip"):
            waiting.add(r["page"])
        else:
            # The helper is off, or every model in its box refused to settle the page
            # (skip.json): Gemini settles what its models read.
            todo.append(r)
    if not todo:
        return waiting
    print(f"Checking {len(todo)} pages against each other and Tesseract")

    def work(rec):
        local = open_pdf(filepath)
        try:
            result = check_page(pool, local, rec, tess[rec["page"]], vocab)
        finally:
            close_pdf(local)
        if result != "wait":
            save_record(cache_dir, rec)
            c = rec["check"]
            print(f"  check page {rec['page']}: {c['state']} (diffs {c['diffs']}, missing {c['missing_a']})")

    with ThreadPoolExecutor(max_workers=workers) as tp:
        list(tp.map(work, todo))
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


def resolve_models(pool, names):
    """Maps each name of the other free models to a model this account has. Google renames
    models (for example "gemini-3-flash" is served as "gemini-3-flash-preview"); a name not
    found as is takes the closest "<name>-..." model. Newer Gemini Flash/Pro models the
    account has are added, each before the first model older than it.
    Names stay as given if listing fails."""
    try:
        have = {}
        for m in pool.client.models.list():
            name = (getattr(m, "name", "") or "").split("/")[-1]
            actions = getattr(m, "supported_actions", None)
            if name and (not actions or "generateContent" in actions):
                have[name] = True
    except Exception as e:
        print(f"Could not list models ({str(e)[:120]}); models used by their given names")
        return names
    if not have:
        return names
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
    out = list(dict.fromkeys(out))
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
    return out + ("\n\n" + "\n\n".join(defs) if defs else "") + "\n"


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
    """When some free readers' daily quota is used up and others still have some: the pages
    whose only missing piece is a second reading that only readers now out could give (a
    page's two readings never come from one model). Returns (pages, readers with quota
    left); no pages when every reader is out, or none is."""
    readers = [m for m in pool.readers() if m not in pool.missing] if pool and dual else []
    left = [m for m in readers if m not in pool.exhausted]
    if not left or len(left) == len(readers):
        return set(), left
    pages = set()
    for r in records:
        if r.get("status") != "ok" or review.pin_of(r) or (r.get("b") or {}).get("status") == "ok":
            continue
        owed = [m for m in readers if m != (r.get("a") or {}).get("model")]
        if owed and all(m in pool.exhausted for m in owed):
            pages.add(r["page"])
    return pages, left


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
             ("single", "قراءة واحدة فقط"), ("retry", "تعذّر حسمها بسبب خطأ مؤقت (تُعاد في التشغيل القادم)"),
             ("unsure", "لم تُحسم (مشكوك فيها)"), ("human", "روجعت يدوياً"),
             ("text-layer", "نص جاهز في الملف"), ("empty", "فارغة/صور"),
             ("ok", "قُرئت ولم تُراجع بعد"), ("suspicious", "قراءة ناقصة"), ("failed", "فشلت"),
             ("pending", "لم تُقرأ بعد")]
    counts = summary["counts"]
    lines = ["# تقرير التحويل", "", f"- عدد الصفحات: {summary['pages_total']}"]
    lines += [f"- {label}: {counts[key]}" for key, label in names if counts.get(key)]
    if pool:
        lines += [f"- طلبات {model} في هذا التشغيل: {n}" for model, n in pool.requests.items()]
        if getattr(pool, "added_models", None):
            lines.append(f"- نماذج مجانية جديدة أُضيفت تلقائياً: {'، '.join(pool.added_models)}")
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
    if summary["state"] in ("paused", "helper", "reader"):
        # The Pacific date the quota ran out on; the inbox starts again the day after.
        data["quota_day"] = quota_day or pacific_day()
    if summary["state"] in ("helper", "reader"):
        # Written as "paused" so an older inbox script (which knows none of them) still
        # waits for the next day; the current one parks the book on waiting_for.
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


EXIT_CODES = {"done": 0, "incomplete": 2, "paused": 3, "helper": 5, "reader": 6}


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


def send_readings(box, filepath, records, cache_dir):
    """The free model's readings the helper has not got yet (pNNNN.a.json), each with the
    lines Tesseract saw that it may have dropped, as the settling request lists them."""
    todo = [r for r in records if r.get("status") == "ok" and (r.get("a") or {}).get("via") != "helper"
            and helperbox.needs_helper(r, CHECK_VERSION) and r["a"].get("up") != helperbox.sha(r["a"]["text"])]
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
    sent = box.put_readings(payloads)
    for r in todo:
        if r["page"] in sent:
            r["a"]["up"] = helperbox.sha(r["a"]["text"])
            save_record(cache_dir, r)
    print(f"Helper: {len(sent)} readings sent")


def tell_helper(box, args, total, recs, lite_out, share=()):
    """book.json: the pages the helper's models may still work on ("need"), and those
    without a first reading they could settle on ("need_a"). The pages the free models take
    in this run (share) are left out of it while the run works on them. Nothing when the
    helper is off for this run, so it stops working on the book."""
    need = sorted(p for p, r in recs.items() if helperbox.needs_helper(r, CHECK_VERSION) and p not in share) \
        if box.transcribe else []
    box.put_json("book.json", {"v": 1, "title": args.title, "total": total, "need": need,
                               "need_a": [p for p in need if helperbox.needs_first_reading(recs[p])],
                               "lite_out": bool(lite_out), "at": helperbox.now(),
                               "run": os.environ.get("GITHUB_RUN_ID", "")})
    return need


# While the helper's models work on a book, the free models start no page after this long:
# the run's Drive token lasts about an hour, and the pages they leave must go back to the
# helper's models (book.json) before it runs out. The Drive inbox starts the next run soon.
FREE_PASS_SECONDS = 35 * 60


def free_share(recs):
    """The pages the free models take in a run while the helper's models work on the book:
    those every model in the helper's box handed back, then the later half of the pages left
    (the helper's models go from the start of the book), the last page first."""
    left = sorted(p for p, r in recs.items() if helperbox.needs_helper(r, CHECK_VERSION))
    back = sorted((p for p, r in recs.items() if r and r.get("hskip") and r.get("status") not in ("empty", "text-layer")
                   and not review.pin_of(r) and not helperbox.settled(r, CHECK_VERSION)), reverse=True)
    return back + sorted(left[(len(left) + 1) // 2:], reverse=True)


def free_pass(pool, filepath, share, outs, cache_dir, total, workers, deadline, vocab):
    """The free models' pages while the helper's models work on the book (free_share): each
    read twice by two free models (once, when the helper's models already gave the second
    reading), then settled like any page. No page starts after the deadline, or once every
    free model is out for the day. Returns the pages settled."""
    done = set()
    lock = threading.Lock()

    def one(p):
        if time.time() > deadline or pool.free_out():
            return
        local = open_pdf(filepath)
        try:
            rec = load_record(cache_dir, p, pool.rescue)
            hb = (outs.get(p) or {}).get("hb")
            given = not (rec and rec.get("status") == "ok") and helperbox.has_text(hb) \
                and hb["text"].strip() != helperbox.EMPTY
            rec = read_page(pool, local, p - 1, cache_dir, total, dual=not given)
            if given and rec and rec.get("status") == "ok":
                rec, changed = helperbox.merge(rec, outs[p], p, CHECK_VERSION)
                if changed:
                    save_record(cache_dir, rec)
            if not rec or rec.get("status") != "ok" or ((rec.get("b") or {}).get("status") != "ok"
                                                        and not rec["a"].get("refused")):
                return
            tess = tesseract_for(filepath, cache_dir, p)
            if from_helper(rec) and rec.get("hfinal"):
                result = check_helper_page(rec, tess, vocab)
            else:
                result = check_page(pool, local, rec, tess, vocab)
            if result == "wait":
                return
            save_record(cache_dir, rec)
            c = rec.get("check") or {}
            print(f"  free page {p}: {c.get('state')} (diffs {c.get('diffs')})")
            if result == "done":
                with lock:
                    done.add(p)
        finally:
            close_pdf(local)

    with ThreadPoolExecutor(max_workers=workers) as tp:
        list(tp.map(one, share))
    return done


def generate_epub_from_pdf(filepath, output_path, args):
    doc = pymupdf.open(filepath)
    selected = parse_pages(args.pages, len(doc))
    book_hash = file_sha(filepath)
    cache_dir = os.path.join(args.work_dir, f"{book_hash}-{CACHE_VERSION}")
    os.makedirs(cache_dir, exist_ok=True)
    print(f"{len(doc)} pages, processing {len(selected)}; progress saved in {cache_dir}")
    pins, human, notes = load_pins(args.human, book_hash)
    # The answers of the user's own models (the helper's review box) count like the user's,
    # but the user's answer to the same place wins.
    answers = {}
    for path in (args.judge_answers, args.answers):
        got = (review.load(path) or {}).get("answers") if path else None
        if isinstance(got, dict):
            answers.update(got)

    pool = None
    needs_ocr = any(len(doc[i].get_text().strip()) < MIN_TEXT_LAYER_CHARS for i in selected)
    if needs_ocr:
        pool = ModelPool(args.model, args.second_model if args.dual else "", args.rescue_model, args.rpm)
        if not args.review_only:
            # Every free model reads and settles: the Flash models once both Lite readers are out.
            pool.set_extra(resolve_models(pool, [m.strip() for m in args.extra_models.split(",") if m.strip()]))
    dual = bool(pool and args.dual and args.second_model)

    # The helper on the user's computer (docs/helper-boxes-plan.md): its files are taken
    # first, so the free models do not read again what its models already read.
    box = None
    if pool and dual and args.helper and not args.review_only:
        box = helperbox.HelperBox.open(args.helper, os.environ.get("DRIVE_TOKEN", ""), cache_dir)
    transcribe = bool(box and box.transcribe)

    def take_helper_files(recs):
        """recs: {page: record or None}; the helper's latest files merged in and saved.
        Returns the files, by page."""
        outs = box.fetch()
        for p in list(recs):
            if p in outs:
                rec, changed = helperbox.merge(recs[p], outs[p], p, CHECK_VERSION)
                if changed:
                    recs[p] = rec
                    save_record(cache_dir, rec)
        return outs

    share, free_done = [], set()
    if box:
        start = {}
        for i in selected:
            rec = load_record(cache_dir, i + 1, args.rescue_model)
            if (i + 1) in pins or (rec and rec.get("status") in ("text-layer", "empty")):
                continue
            with PDF_LOCK:
                layer = len(doc[i].get_text("text", sort=True).strip())
            if rec is not None or layer < MIN_TEXT_LAYER_CHARS:
                start[i + 1] = rec
        outs = take_helper_files(start)
        # With the helper's models on, the free models and they share the book: the free
        # models take its end in this run, and the helper's models do not touch it meanwhile.
        share = free_share(start) if transcribe else []
        need = tell_helper(box, args, len(doc), start, False, share)
        if transcribe:
            box.start_images(sorted(set(need) | set(share)), lambda p: page_jpeg(filepath, p))
            print(f"Free models: {len(share)} pages from the end of the book; the helper's models: {len(need)}")
            free_done = free_pass(pool, filepath, share, outs, cache_dir, len(doc), args.workers,
                                  box.drive.started + FREE_PASS_SECONDS, book_vocab([r for r in start.values() if r]))
            print(f"Free models settled {len(free_done)} pages")

    def work(i):
        rec = load_record(cache_dir, i + 1, args.rescue_model)
        if (i + 1) in pins or (rec and review.pin_of(rec)):
            # A page the user answered about is theirs: it is never read again.
            if (i + 1) in pins:
                rec = review.pin_record(rec, i + 1, pins[i + 1], CHECK_VERSION)
                save_record(cache_dir, rec)
            return rec
        if args.review_only:
            # Only what is saved: a page never read keeps the book paused.
            return rec
        # Each worker opens its own document: PyMuPDF objects are not thread safe.
        local = open_pdf(filepath)
        try:
            if transcribe:
                # The free models' pages were done above; the rest is the helper's models'.
                return rec if rec is not None else text_layer_record(local, i, cache_dir)
            return read_page(pool, local, i, cache_dir, len(doc), dual)
        finally:
            close_pdf(local)

    # Stage 1: two readings of every page. Stage 2: compare them and settle differences.
    with ThreadPoolExecutor(max_workers=args.workers) as tp:
        records = [rec or {"page": i + 1, "status": "pending"} for i, rec in zip(selected, tp.map(work, selected))]
    # The user's answers go first: the pages they touch are pinned before any check.
    applied = review.overlay(records, answers, CHECK_VERSION, (human or {}).get("answers_seen", ""))
    for r in records:
        if r["page"] in applied["touched"]:
            save_record(cache_dir, r)
    if applied["results"]:
        print(f"Answers: {len(applied['results'])}, pages pinned: {compress(sorted(applied['touched']))}")
    helper_wait = set()
    if box:
        if transcribe:
            send_readings(box, filepath, records, cache_dir)
        # What the helper did while the free models worked.
        recs = {r["page"]: r for r in records if r.get("status") not in ("text-layer", "empty")}
        take_helper_files(recs)
        records = [recs.get(r["page"], r) for r in records]
    if pool and not args.review_only:
        helper_wait = check_pages(pool, filepath, records, cache_dir, dual, args.workers, transcribe)
    free_left = False
    if transcribe:
        # Every page left goes back to the helper's models; the free models take their share
        # again in the next run.
        left = {r["page"]: r for r in records if r.get("status") not in ("text-layer", "empty")}
        helper_wait |= {p for p, r in left.items() if helperbox.needs_helper(r, CHECK_VERSION)}
        tell_helper(box, args, len(doc), left, pool.free_out())
        # Worth another run right away: the free models finished pages and still have quota.
        free_left = bool(free_done) and not pool.free_out() and bool(free_share(left))
    if box:
        box.wait_images()
        notes = notes + [f"المساعد: {n}" for n in box.notes]

    review_pages = images = None
    if args.review_out:
        unsure = [r["page"] for r in records if (r.get("check") or {}).get("state") == "unsure" and r.get("text")]
        data, images = review_pages_data(filepath, cache_dir, unsure)
        vocab = set()
        for r in records:
            vocab.update(norm_words(r.get("text") or ""))
            vocab.update(norm_words((r.get("b") or {}).get("text") or ""))
        review_pages, became_human = review.build(records, data, answers, applied["results"], vocab,
                                                  page_heads(cache_dir, [r["page"] for r in records]))
        for r in records:
            if r["page"] in became_human:
                save_record(cache_dir, r)
        print(f"Review: {sum(len(p['items']) for p in review_pages)} places on {len(review_pages)} pages")

    reader_wait, readers_left = waiting_for_reader(None if args.review_only else pool, records, dual)
    summary = summarize(records, dual, review_pages, applied, helper_wait, reader_wait, readers_left)
    if transcribe:
        summary.update(free_left=free_left, free_done=len(free_done))
    md = assemble(records, review_pages)
    if not args.pages:
        md = keep_main_headings(md, pool, cache_dir)
    write_report(args.report, summary, pool, notes)

    md_path = output_path + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    cover_path = output_path + ".cover.png"
    # A review round asks no model to pick the cover: the one picked before, or the first page.
    has_cover = extract_cover_from_pdf(doc, cover_path,
                                       pick_cover_page(doc, None if args.review_only else pool, cache_dir))
    try:
        run_pandoc(md_path, output_path, args.title, cover_path if has_cover else None)
        print(f"Page list: {add_page_list(output_path)} pages")
    finally:
        if has_cover and os.path.exists(cover_path):
            os.remove(cover_path)
        if not args.keep_markdown and os.path.exists(md_path):
            os.remove(md_path)
    if args.review_out:
        review.write(args.review_out, review_pages, images, args.title, book_hash, records, applied["seen"],
                     applied["results"])
    write_status(args.status, summary, args.title, os.path.basename(output_path), pool.quota_day if pool else "")
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


def process_file(filepath, output_path, args):
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.pdf':
        return generate_epub_from_pdf(filepath, output_path, args)
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
                        or "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3-flash,gemini-2.5-flash",
                        help="The other free models (small daily quotas): they read and settle pages once "
                             "both Lite readers are out")
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
    parser.add_argument("--helper", default="",
                        help="The book's Drive folder for the helper on the user's computer, as "
                             "<folder id>:transcribe (its models give the second reading and settle) or "
                             "<folder id>:import (their work so far is used); the Drive token is in DRIVE_TOKEN")
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
