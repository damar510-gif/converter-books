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


def read_page(pool, doc, index, cache_dir, total, dual):
    """First and second reading of one page, saved to cache_dir/NNNN.json.
    Returns the record, or None when the page has to wait for tomorrow's quota."""
    page_no = index + 1
    rec = load_record(cache_dir, page_no, pool.rescue if pool else "")
    if rec and rec.get("status") in ("empty", "text-layer"):
        return rec
    page = doc[index]
    if rec is None:
        with PDF_LOCK:
            layer = page.get_text("text", sort=True).strip()
        if len(layer) >= MIN_TEXT_LAYER_CHARS:
            rec = {"page": page_no, "status": "text-layer", "text": layer}
            save_record(cache_dir, rec)
            return rec

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
        status, reading, error = read_with(pool, [pool.model, pool.second], page, png, pix, prompt, ink,
                                           rescue=pool.rescue)
        if status == "pending":
            return None
        rec = {"page": page_no, "ink": round(ink, 4), "spread": spread, "status": status,
               "text": reading["text"] if reading else "", "a": reading}
        if error:
            rec["error"] = error
    if dual and rec["status"] == "ok":
        # The second reading comes from a different model, so its mistakes fall elsewhere.
        # Gemma may give it only when Gemini refused this page.
        others = [m for m in (pool.second, pool.model) if m and m != rec["a"]["model"]]
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
    # Use the reconciler with fewer requests so far, to spread the two daily quotas.
    for model in sorted((m for m in (pool.model, pool.second) if pool.available(m)),
                        key=lambda m: pool.requests.get(m, 0)):
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


def tesseract_for(filepath, cache_dir, page_no):
    """Tesseract's reading of a page, cached; empty when Tesseract is missing."""
    if not shutil.which("tesseract"):
        return ""
    tess_dir = os.path.join(cache_dir, "tesseract")
    os.makedirs(tess_dir, exist_ok=True)
    path = os.path.join(tess_dir, f"{page_no:04d}.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    local = open_pdf(filepath)
    try:
        png, _ = page_image(local, local[page_no - 1])
    finally:
        close_pdf(local)
    tess = tesseract_text(png)
    with open(path, "w", encoding="utf-8") as f:
        f.write(tess)
    return tess


def check_pages(pool, filepath, records, cache_dir, dual, workers):
    """Runs check_page on every read page whose check is missing or out of date."""
    for r in records:
        upgrade_check(r)
    todo = [r for r in records if r.get("status") == "ok" and r.get("a")
            and (r.get("check") or {}).get("v") != CHECK_VERSION and (not dual or "b" in r)]
    if not todo:
        return
    vocab = set()
    for r in records:
        vocab.update(norm_words(r.get("text") or ""))
        vocab.update(norm_words((r.get("b") or {}).get("text") or ""))
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as tp:
        tess = dict(zip((r["page"] for r in todo),
                        tp.map(lambda r: tesseract_for(filepath, cache_dir, r["page"]), todo)))
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


# Each unsure page is shown to at most this many judges.
MAX_JUDGES = 2


def arbitrate(pool, filepath, records, cache_dir, models, limit):
    """Shows pages still unsure to stronger models with small daily quotas, one
    after another as each quota runs out. A page a judge could not settle may be
    shown to one more judge. Quota errors leave pages for the next run."""
    todo = [r for r in records if (r.get("check") or {}).get("state") == "unsure"
            and len(r["check"].get("judges", [])) < MAX_JUDGES][:limit]
    models = [m for m in models if m]
    if not todo or not any(pool.available(m) for m in models):
        return
    print(f"Asking {', '.join(models)} about {len(todo)} unsure pages")
    vocab = set()
    for r in records:
        vocab.update(norm_words(r.get("text") or ""))
    local = open_pdf(filepath)
    try:
        for rec in todo:
            judges = rec["check"].setdefault("judges", [])
            free = [m for m in models if pool.available(m) and m not in judges]
            if not free:
                if not any(pool.available(m) for m in models):
                    break
                continue
            a = rec["a"]["text"]
            b_rec = rec.get("b") or {}
            b = b_rec.get("text", "") if b_rec.get("status") == "ok" else ""
            tess = tesseract_for(filepath, cache_dir, rec["page"])
            miss_a = missing_lines(tess, a, vocab) if tess else []
            miss_b = missing_lines(tess, b, vocab) if tess and b else []
            _, diffs = crosscheck.compare(a, b) if b else (0, [])
            png, pix = page_image(local, local[rec["page"] - 1])
            rules = OCR_RULES.format(spread=SPREAD_HINT if pix.width > pix.height * 1.15 else "")
            prompt = crosscheck.reconcile_prompt(rules, a, b, diffs, list(dict.fromkeys(miss_a + miss_b)))
            model = None
            for m in free:
                try:
                    text, finish = pool.read(png, prompt, m)
                    model = m
                    break
                except QuotaExhausted:
                    continue
                except Exception as e:
                    if "404" in str(e) or "not found" in str(e).lower():
                        # A model name this account does not have: skip it for the run.
                        pool.exhausted.add(m)
                        pool.missing.add(m)
                        continue
                    text, finish, model = "", f"error {str(e)[:100]}", m
                    break
            if model is None:
                continue
            judges.append(model)
            text = clean_settled(text)
            if not text or is_refusal(finish) or "MAX_TOKENS" in finish or finish.startswith("error"):
                verdict = f"no answer: {finish[:80]}"
            elif not crosscheck.validate_final(text, a, b):
                verdict = "rejected: drops text the readings agree on"
            else:
                miss_f = missing_lines(tess, text, vocab) if tess else []
                if len(miss_f) > min([len(miss_a)] + ([len(miss_b)] if b else [])):
                    verdict = f"rejected: {len(miss_f)} lines missing"
                else:
                    # The stronger model's text is used even if a line still seems missing;
                    # the page then stays flagged in the book.
                    rec["text"] = text
                    rec["check"].update(model=model, missing_final=len(miss_f),
                                        state="unsure" if miss_f else "reconciled")
                    verdict = "settled" if not miss_f else f"used, {len(miss_f)} lines may be missing"
                    if miss_f:
                        # Its text is in the book; another judge would not see more.
                        judges.extend(["-"] * MAX_JUDGES)
            rec["check"]["arbiter"] = f"{model}: {verdict}"
            save_record(cache_dir, rec)
            print(f"  judge page {rec['page']}: {rec['check']['arbiter']}")
    finally:
        close_pdf(local)


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


def assemble(records):
    # Each block is [text, kind]; notes sit between pages without breaking a
    # sentence that continues on the next page.
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
            words = "، ".join(f"«{short(x)}» أو «{short(y)}»" for x, y in check.get("sample", [])[:3])
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
    path = os.path.join(cache_dir, "articles.json")
    main = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("key") == key:
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
            json.dump({"key": key, "main": sorted(main)}, f)
    if not main:
        return md
    for k, i in enumerate(heads, 1):
        if k not in main:
            lines[i] = "#" + lines[i]
    print(f"Table of contents: {len(main)} main articles out of {len(heads)} headings")
    return "\n".join(lines)


def norm_words(text):
    text = re.sub(r"[\u064B-\u0652\u0640]", "", text)
    text = re.sub("[إأآا]", "ا", text)
    text = text.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return [w for w in re.findall(r"[\u0621-\u064A]+", text) if len(w) > 2]


def tesseract_text(png):
    try:
        r = subprocess.run(["tesseract", "stdin", "stdout", "-l", "ara", "--psm", "3"],
                           input=png, capture_output=True, timeout=180,
                           # One thread per process: we already run several in parallel.
                           env={**os.environ, "OMP_THREAD_LIMIT": "1"})
        return r.stdout.decode("utf-8", "ignore")
    except Exception as e:
        print(f"  tesseract failed: {e}")
        return ""


def missing_lines(tess, text, vocab):
    """Lines Tesseract saw on the page that the model's text does not contain.
    Tesseract makes many letter mistakes, so only lines made mostly of words that
    appear elsewhere in the book count, and a line is missing when almost none of
    its word pairs are in the page text."""
    w = norm_words(text)
    pairs = set(zip(w, w[1:]))
    out = []
    for line in tess.split("\n"):
        lw = norm_words(line)
        if len(lw) < 5:
            continue
        known = sum(x in vocab for x in lw) / len(lw)
        lp = list(zip(lw, lw[1:]))
        if known >= 0.7 and sum(x in pairs for x in lp) / len(lp) <= 0.15:
            out.append(line.strip())
    return out


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


def summarize(records, dual):
    """Book state for status.json and the report."""
    missing = [r["page"] for r in records if r["status"] in ("failed", "suspicious")]
    retry = [r["page"] for r in records if (r.get("check") or {}).get("state") == "retry"]
    waiting = [r["page"] for r in records if r["status"] == "pending" or r.get("judge_waiting")
               or (r["status"] == "ok" and r["page"] not in retry
                   and ((r.get("check") or {}).get("v") != CHECK_VERSION or (dual and "b" not in r)))]
    unsure = [r["page"] for r in records if (r.get("check") or {}).get("state") == "unsure"]
    states = {}
    for r in records:
        key = (r.get("check") or {}).get("state") or r["status"]
        states[key] = states.get(key, 0) + 1
    # Waiting pages need tomorrow's quota; retry pages hit temporary API errors and
    # can be tried again right away.
    state = "paused" if waiting else ("incomplete" if missing or retry else "done")
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
    return {"state": state, "pages_total": len(records),
            "pages_done": len(records) - len(missing) - len(waiting) - len(retry),
            "pages_unsure": unsure, "pages_missing": missing, "pages_waiting": waiting,
            "pages_retry": retry, "counts": states, "samples": samples, "reasons": reasons}


def write_report(path, summary, pool):
    names = [("agreed", "القراءتان متطابقتان"), ("reconciled", "صُححت بمقارنة القراءتين على الصورة"),
             ("single", "قراءة واحدة فقط"), ("retry", "تعذّر حسمها بسبب خطأ مؤقت (تُعاد في التشغيل القادم)"),
             ("unsure", "لم تُحسم (مشكوك فيها)"), ("text-layer", "نص جاهز في الملف"), ("empty", "فارغة/صور"),
             ("ok", "قُرئت ولم تُراجع بعد"), ("suspicious", "قراءة ناقصة"), ("failed", "فشلت"),
             ("pending", "لم تُقرأ بعد")]
    counts = summary["counts"]
    lines = ["# تقرير التحويل", "", f"- عدد الصفحات: {summary['pages_total']}"]
    lines += [f"- {label}: {counts[key]}" for key, label in names if counts.get(key)]
    if pool:
        lines += [f"- طلبات {model} في هذا التشغيل: {n}" for model, n in pool.requests.items()]
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
    if summary["state"] == "paused":
        # The Pacific date the quota ran out on; the inbox starts again the day after.
        data["quota_day"] = quota_day or pacific_day()
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


EXIT_CODES = {"done": 0, "incomplete": 2, "paused": 3}


def generate_epub_from_pdf(filepath, output_path, args):
    doc = pymupdf.open(filepath)
    selected = parse_pages(args.pages, len(doc))
    cache_dir = os.path.join(args.work_dir, f"{file_sha(filepath)}-{CACHE_VERSION}")
    os.makedirs(cache_dir, exist_ok=True)
    print(f"{len(doc)} pages, processing {len(selected)}; progress saved in {cache_dir}")

    pool = None
    needs_ocr = any(len(doc[i].get_text().strip()) < MIN_TEXT_LAYER_CHARS for i in selected)
    if needs_ocr:
        pool = ModelPool(args.model, args.second_model if args.dual else "", args.rescue_model, args.rpm)
    dual = bool(pool and args.dual and args.second_model)

    def work(i):
        # Each worker opens its own document: PyMuPDF objects are not thread safe.
        local = open_pdf(filepath)
        try:
            return read_page(pool, local, i, cache_dir, len(doc), dual)
        finally:
            close_pdf(local)

    # Stage 1: two readings of every page. Stage 2: compare them and settle differences.
    with ThreadPoolExecutor(max_workers=args.workers) as tp:
        records = [rec or {"page": i + 1, "status": "pending"} for i, rec in zip(selected, tp.map(work, selected))]
    if pool:
        check_pages(pool, filepath, records, cache_dir, dual, args.workers)
        judges = [m for m in args.arbiter_models.split(",") if m]
        # One pass per judge a page may see, so a page the first judge could not settle
        # reaches the next one in the same run while quota is left.
        for _ in range(MAX_JUDGES):
            arbitrate(pool, filepath, records, cache_dir, judges, args.arbiter_limit)
        # A book is finished only when every unsure page has been shown to the judges it can
        # still see; pages left for tomorrow's judge quota keep the book paused.
        for r in records:
            c = r.get("check") or {}
            if c.get("state") == "unsure" and len(c.get("judges", [])) < MAX_JUDGES \
                    and any(m not in pool.missing and m not in c.get("judges", []) for m in judges):
                r["judge_waiting"] = True

    summary = summarize(records, dual)
    md = assemble(records)
    if not args.pages:
        md = keep_main_headings(md, pool, cache_dir)
    write_report(args.report, summary, pool)

    md_path = output_path + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    cover_path = output_path + ".cover.png"
    has_cover = extract_cover_from_pdf(doc, cover_path, pick_cover_page(doc, pool, cache_dir))
    try:
        run_pandoc(md_path, output_path, args.title, cover_path if has_cover else None)
        print(f"Page list: {add_page_list(output_path)} pages")
    finally:
        if has_cover and os.path.exists(cover_path):
            os.remove(cover_path)
        if not args.keep_markdown and os.path.exists(md_path):
            os.remove(md_path)
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
    parser.add_argument("--arbiter-models", default=os.environ.get("GEMINI_ARBITER_MODELS")
                        or "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3-flash,gemini-2.5-flash",
                        help="Stronger models (20 requests a day each) asked in turn about pages still unsure")
    parser.add_argument("--arbiter-limit", type=int, default=100, help="Unsure pages sent to judges per run")
    parser.add_argument("--single", dest="dual", action="store_false",
                        help="Read each page once (no second reading and no comparison)")
    parser.add_argument("--rpm", type=float, default=10, help="Requests per minute per Gemini model")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--status", default="", help="Where to write status.json (for the Drive inbox)")
    parser.add_argument("--keep-markdown", action="store_true")
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
