import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pymupdf
from google import genai
from google.genai import types

# Bump when the OCR prompt or page format changes, so cached pages are redone.
CACHE_VERSION = "v1"

# A page whose text layer is shorter than this is treated as a scanned image.
MIN_TEXT_LAYER_CHARS = 50

# Pages with more dark pixels than this but almost no OCR text are suspicious.
INK_SUSPICIOUS_RATIO = 0.02
MIN_OCR_CHARS = 120

OCR_PROMPT = """أنت تنسخ صفحة واحدة من كتاب عربي ممسوح ضوئياً. انسخ النص كما هو حرفياً بلا تلخيص ولا حذف ولا تصحيح ولا إضافة.

قواعد الإخراج (Markdown):
- إن كانت الصفحة بعمودين فاقرأ العمود الأيمن كاملاً من أعلاه إلى أسفله، ثم العمود الأيسر.
{spread}- عنوان مقال أو بحث جديد (العنوان الكبير في رأس المقال) يُكتب في سطر مستقل يبدأ بـ "# ".
- العناوين الفرعية داخل المقال تُكتب في سطر مستقل يبدأ بـ "## ".
- افصل بين الفقرات بسطر فارغ، واجمع أسطر الفقرة الواحدة في سطر واحد.
- الشعر: كل بيت في سطر مستقل يبدأ بـ "| "، وبين الشطرين " … ".
- الحواشي (السطور الصغيرة أسفل الصفحة تحت الخط): في موضع رقم الحاشية داخل المتن اكتب [^1] (بنفس رقمها)، وفي آخر إخراجك اكتب كل حاشية في سطر مستقل هكذا: [^1]: نص الحاشية
- الكلمات الأجنبية تُنسخ بحروفها كما هي.
- تجاهل تماماً: أرقام الصفحات، والترويسة المكررة أعلى الصفحة، والعلامة المائية (مثل «جلجامش»)، والزخارف.
- لا تكتب أي تعليق أو شرح أو مقدمة، فقط نص الصفحة.
- إن كانت الصفحة فارغة أو صورة بلا نص فاكتب فقط: [[EMPTY]]"""

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


class QuotaExhausted(Exception):
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


def page_image(doc, page):
    """PNG bytes of the page at its scan's native resolution (at least 200 dpi otherwise)."""
    images = page.get_images()
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


def ink_ratio(pix):
    """Fraction of dark pixels, sampled. Tells blank pages from pages with text."""
    gray = pymupdf.Pixmap(pymupdf.csGRAY, pix) if pix.n > 1 else pix
    samples = gray.samples
    step = max(1, len(samples) // 200000)
    sampled = samples[::step]
    dark = sum(1 for v in sampled if v < 110)
    return dark / max(1, len(sampled))


class GeminiOCR:
    def __init__(self, model, fallback_model, per_minute, fallback_limit):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set; scanned pages cannot be read.")
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.fallback_model = fallback_model
        self.fallback_left = fallback_limit
        self.limiter = RateLimiter(per_minute)
        self.lock = threading.Lock()
        self.requests = {}
        self.quota_hit = threading.Event()
        self.use_tuning = True

    def _config(self):
        if not self.use_tuning:
            return types.GenerateContentConfig(max_output_tokens=8192, safety_settings=SAFETY_OFF)
        return types.GenerateContentConfig(
            max_output_tokens=8192,
            safety_settings=SAFETY_OFF,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
        )

    def _call(self, model, png, prompt):
        self.limiter.wait()
        with self.lock:
            self.requests[model] = self.requests.get(model, 0) + 1
        response = self.client.models.generate_content(
            model=model,
            contents=[types.Part.from_bytes(data=png, mime_type="image/png"), prompt],
            config=self._config(),
        )
        finish = ""
        if response.candidates:
            finish = str(response.candidates[0].finish_reason or "")
        return (response.text or "").strip(), finish

    def read(self, png, prompt, model, attempts=3):
        """Returns (text, finish_reason). Raises QuotaExhausted or the last error."""
        last = None
        for attempt in range(attempts):
            if self.quota_hit.is_set():
                raise QuotaExhausted(self.model)
            try:
                return self._call(model, png, prompt)
            except Exception as e:
                last = e
                msg = str(e)
                low = msg.lower()
                if "429" in msg or "resource_exhausted" in low:
                    if "perday" in low.replace(" ", "") or "per day" in low or "daily" in low:
                        raise QuotaExhausted(model) from e
                    wait = 65
                elif "400" in msg and self.use_tuning and ("thinking" in low or "media_resolution" in low):
                    print(f"  Model rejected tuning options, retrying without them: {msg[:200]}")
                    self.use_tuning = False
                    wait = 0
                elif any(code in msg for code in ("500", "502", "503", "504")) or "unavailable" in low or "timed out" in low:
                    wait = 20 * (2 ** attempt)
                else:
                    raise
                print(f"  {model}: {msg[:160]} -> retry in {wait}s")
                time.sleep(wait)
        raise last

    def take_fallback(self):
        with self.lock:
            if not self.fallback_model or self.fallback_left <= 0:
                return None
            self.fallback_left -= 1
            return self.fallback_model


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


def ocr_page(ocr, doc, index, cache_dir, total):
    """OCRs one page into cache_dir/NNNN.json. Returns the record."""
    path = os.path.join(cache_dir, f"{index + 1:04d}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
        if rec.get("status") in ("ok", "empty", "text-layer"):
            return rec

    page = doc[index]
    layer = page.get_text("text", sort=True).strip()
    if len(layer) >= MIN_TEXT_LAYER_CHARS:
        rec = {"page": index + 1, "status": "text-layer", "text": layer}
    else:
        png, pix = page_image(doc, page)
        ink = ink_ratio(pix)
        spread = pix.width > pix.height * 1.15
        prompt = OCR_PROMPT.format(spread=SPREAD_HINT if spread else "")
        rec = {"page": index + 1, "ink": round(ink, 4), "spread": spread, "status": "failed"}
        model = ocr.model
        tried = []
        halves_tried = False
        while model:
            tried.append(model)
            try:
                text, finish = ocr.read(png, prompt, model)
                if not clean_ocr(text) and any(r in finish for r in BLOCKED_REASONS) and not halves_tried:
                    halves_tried = True
                    tried.append(model + " (halves)")
                    text, finish = read_in_halves(ocr, page, pix, prompt, model)
            except QuotaExhausted as e:
                if e.model == ocr.model:
                    # Primary model is out for today: stop, the page stays pending.
                    ocr.quota_hit.set()
                    return None
                print(f"  {e}; no more fallback requests this run")
                ocr.fallback_left = 0
                break
            except Exception as e:
                rec.update(status="failed", error=str(e)[:300], models=tried)
                text, finish = None, ""
            if text is not None:
                text = clean_ocr(text)
                empty = text == "[[EMPTY]]" or not text
                truncated = "MAX_TOKENS" in finish
                if empty and ink < INK_SUSPICIOUS_RATIO:
                    rec.update(status="empty", text="", models=tried)
                    break
                if not empty and not truncated and (len(text) >= MIN_OCR_CHARS or ink < INK_SUSPICIOUS_RATIO * 2):
                    rec.update(status="ok", text=text, models=tried)
                    rec.pop("error", None)
                    break
                rec.update(status="suspicious", text="" if empty else text, models=tried,
                           error=f"chars={len(text)} finish={finish}")
            # One try with the stronger model, then give up until the next run.
            model = ocr.take_fallback() if model == ocr.model else None
        print(f"  page {index + 1}/{total}: {rec['status']} ({', '.join(tried)})")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    return rec


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
    for rec in records:
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
    out = "\n\n".join(b for b, _ in blocks)
    return out + ("\n\n" + "\n\n".join(defs) if defs else "") + "\n"


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
        cmd.extend(["--toc", "--toc-depth=2"])
    if cover_path and os.path.exists(cover_path):
        cmd.extend(["--epub-cover-image", cover_path])
    print(f"Running pandoc to generate {output_path}...")
    subprocess.run(cmd, check=True)
    print(f"Successfully generated EPUB: {output_path}")


def write_report(path, records, selected, ocr, quota_stopped):
    by_status = {}
    for rec in records:
        by_status.setdefault(rec["status"], []).append(rec["page"])
    missing = [r["page"] for r in records if r["status"] in ("failed", "suspicious", "pending")]
    lines = ["# تقرير التحويل", ""]
    lines.append(f"- الصفحات المطلوبة: {len(selected)}")
    names = {"ok": "قُرئت بنجاح", "text-layer": "نص جاهز في الملف", "empty": "فارغة/صور",
             "suspicious": "مشكوك فيها", "failed": "فشلت", "pending": "لم تُعالج بعد"}
    for key, label in names.items():
        if key in by_status:
            lines.append(f"- {label}: {len(by_status[key])}")
    if ocr:
        for model, n in ocr.requests.items():
            lines.append(f"- طلبات {model} في هذا التشغيل: {n}")
    if quota_stopped:
        lines += ["", "**توقف التشغيل لانتهاء الحد اليومي لـ Gemini.** ما أُنجز محفوظ؛ "
                  "شغّل نفس الكتاب غداً وسيكمل الصفحات الناقصة فقط."]
    if missing:
        lines += ["", "الصفحات الناقصة: " + compress(missing),
                  "", "أعد التشغيل لنفس الكتاب ليحاول فيها مرة أخرى."]
    else:
        lines += ["", "**كل الصفحات اكتملت.**"]
    for rec in records:
        if rec.get("error") and rec["status"] != "ok":
            lines.append(f"- ص {rec['page']}: {rec['error'][:200]}")
    report = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    return missing


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


def extract_cover_from_pdf(doc, cover_path):
    try:
        if len(doc) > 0:
            doc[0].get_pixmap(dpi=150).save(cover_path)
            return True
    except Exception as e:
        print(f"Warning: Could not extract cover from PDF: {e}")
    return False


def generate_epub_from_pdf(filepath, output_path, args):
    doc = pymupdf.open(filepath)
    selected = parse_pages(args.pages, len(doc))
    cache_dir = os.path.join(args.work_dir, f"{file_sha(filepath)}-{CACHE_VERSION}")
    os.makedirs(cache_dir, exist_ok=True)
    print(f"{len(doc)} pages, processing {len(selected)}; progress saved in {cache_dir}")

    ocr = None
    needs_ocr = any(len(doc[i].get_text().strip()) < MIN_TEXT_LAYER_CHARS for i in selected)
    if needs_ocr:
        ocr = GeminiOCR(args.model, args.fallback_model, args.rpm, args.fallback_limit)

    results = {}

    def work(i):
        # Each worker opens its own document: PyMuPDF objects are not thread safe.
        local = pymupdf.open(filepath)
        try:
            return i, ocr_page(ocr, local, i, cache_dir, len(doc))
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, rec in pool.map(work, selected):
            results[i] = rec

    quota_stopped = bool(ocr and ocr.quota_hit.is_set())
    records = [results[i] or {"page": i + 1, "status": "pending"} for i in selected]
    missing = write_report(args.report, records, selected, ocr, quota_stopped)

    md_path = output_path + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(assemble(records))

    cover_path = output_path + ".cover.png"
    has_cover = extract_cover_from_pdf(doc, cover_path)
    try:
        run_pandoc(md_path, output_path, args.title, cover_path if has_cover else None)
    finally:
        if has_cover and os.path.exists(cover_path):
            os.remove(cover_path)
        if not args.keep_markdown and os.path.exists(md_path):
            os.remove(md_path)
    return 0 if not missing else 2


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
    return 0


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
    parser.add_argument("--model", default=os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash-lite")
    parser.add_argument("--fallback-model", default=os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.8-flash"))
    parser.add_argument("--fallback-limit", type=int, default=15,
                        help="Max requests to the fallback model per run (its daily limit is small)")
    parser.add_argument("--rpm", type=float, default=12, help="Requests per minute to stay under")
    parser.add_argument("--workers", type=int, default=4)
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
        print(f"Error during conversion pipeline: {e}")
        sys.exit(1)
    sys.exit(code)


if __name__ == "__main__":
    main()
