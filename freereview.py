"""The free models in the status page's review box judge the unsure places of a delivered
book, on GitHub, like the user's own models do on their computer (helper/helper.ps1, the
"judge" jobs; docs/helper-boxes-plan.md).

The places are the run's review/items.json and page images (the results branch). Every
model's verdicts live in the book's review folder in Drive ("بيانات المساعد/مراجعة/<fileId>",
j-<chip>-pNNNN.json), the free models' as the chip "free|<model>": each side sees what the
other judged, and the same rules make answers of them (Item-Needs and Item-Answer in the
helper): a model judges a place nobody judged, or a risky correction (a line, or more than six
words) only one model gave; models that disagree leave the place to the user; an answer needs
all verdicts to agree, and two models for a risky one.

The answers join the saved verdicts (review/judge-answers.json) and the review round applies
them like the user's own; the user's answer to the same place wins.

Run from the workflow:  python freereview.py --items DIR/items.json --folder ID --boxes "review=a,b;out=c"
                        --judge review-in/judge.json --summary free-review.json
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pymupdf

import converter
import helperbox
import live

FREE = "free|"
VERDICT_FILE = re.compile(r"^j-.+-p\d{4}\.json$")
# No new request after this long: the run's Drive token must last for the last writes.
REVIEW_SECONDS = 40 * 60

ARABIC_MARKS = re.compile("[ً-ْٰـ]")
NOT_WORD = re.compile("[^ء-ي0-9A-Za-z]")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_words(text):
    """The helper's Norm-Words: marks and tatweel go, digits become ASCII, the letters that
    are written several ways become one."""
    out = []
    for raw in (text or "").split():
        w = ARABIC_MARKS.sub("", raw)
        for d in range(10):
            w = w.replace(chr(0x660 + d), str(d)).replace(chr(0x6F0 + d), str(d))
        w = re.sub("[إأآٱ]", "ا", w)
        w = w.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
        w = NOT_WORD.sub("", w)
        if w:
            out.append(w)
    return out


def common(a, b):
    """The length of the longest common subsequence of two word lists."""
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            cur[j] = prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1])
        prev = cur
    return prev[-1]


def risky(item, v):
    """A correction that needs two models to agree: inserting a line, or changing more than
    six words."""
    if v.get("verdict") != "fix":
        return False
    core = item.get("core") or ""
    if not core.strip():
        return True
    a, b = norm_words(core), norm_words(v.get("text") or "")
    n = common(a, b)
    return max(len(a) - n, len(b) - n) > 6


def verdict_key(v):
    if v.get("verdict") != "fix":
        return v.get("verdict")
    return "fix|" + " ".join(norm_words(v.get("text") or ""))


def item_needs(item, vs, chip):
    """Should this model judge this place? Not if it did; yes if nobody gave a verdict yet;
    yes for a risky correction only one model gave; no once models disagree."""
    if item.get("kind") == "page" or any(v["chip"] == chip for v in vs):
        return False
    real = [v for v in vs if v["verdict"] != "unsure"]
    if not real:
        return True
    if len({verdict_key(v) for v in real}) > 1:
        return False
    return len(real) < 2 and risky(item, real[0])


def item_answer(item, vs):
    """The verdict that becomes an answer, or None: all models agree, and two of them for a
    risky one."""
    real = [v for v in vs if v["verdict"] != "unsure"]
    if not real or len({verdict_key(v) for v in real}) > 1:
        return None
    if risky(item, real[0]) and len(real) < 2:
        return None
    return {"verdict": real[0]["verdict"], "text": real[0].get("text") or "", "by": [v["chip"] for v in real]}


def judge_prompt(items):
    """The helper's judging request (Get-JudgeTask), for a model given the page image."""
    q = lambda t: "«" + (t or "").strip() + "»"
    lines = ["أمامك صورة صفحة من كتاب عربي ممسوح ضوئياً. عليها مربعات حمراء مرقّمة، كل مربع موضع شك في النص الذي "
             "كتبه البرنامج لهذه الصفحة.",
             "لكل موضع انظر في الصورة بنفسك واحكم.", ""]
    for it in items:
        options = it.get("options") or []
        other = next((o for o in options if o and o.get("label") == "other"), None)
        ocr = next((o for o in options if o and o.get("label") == "ocr"), None)
        lines.append(f"الموضع {it.get('n')}:")
        around = q((it.get("before") or "") + " [[" + (it.get("core") or "") + "]] " + (it.get("after") or ""))
        if it.get("kind") == "diff":
            lines.append(f"- النص في الكتاب: {around}")
            if other:
                lines.append(f"- قرأه قارئ آخر هكذا: {q(other.get('text'))}")
        elif not (it.get("core") or "").strip():
            lines.append(f"- قد يكون هنا سطر ساقط من النص، في موضع [[ ]]: {around}")
            if ocr:
                lines.append(f"- قرأه قارئ ثالث كثير الأخطاء هكذا: {q(ocr.get('text'))}")
            lines.append("- احكم: هل في الصورة هنا سطر غير موجود في النص؟")
        else:
            lines.append(f"- سطر ظن قارئ ثالث (كثير الأخطاء) أنه ساقط، والأرجح أنه موجود: {around}")
            if ocr:
                lines.append(f"- قراءة القارئ الثالث: {q(ocr.get('text'))}")
        if it.get("hint") == "header":
            lines.append("- قد يكون ترويسة متكررة أعلى الصفحة، والترويسة لا تُكتب في النص.")
        lines.append("")
    lines += ["طريقة الجواب: مصفوفة JSON فقط، فيها عنصر لكل موضع:",
              '- {"n": 1, "verdict": "keep"} إن كان ما بين [[ ]] مطابقاً للصورة (أو: لا يوجد سطر ساقط).',
              '- {"n": 2, "verdict": "fix", "text": "..."} والنص هو الصحيح كما هو مطبوع، يحل محل ما بين [[ ]] بالضبط '
              "(أو: السطر الساقط كاملاً ليُدرج).",
              '- {"n": 3, "verdict": "unsure"} إن لم تستطع الحكم من الصورة.',
              "انسخ الحروف كما هي مطبوعة، ولا تصحح أخطاء المؤلف أو الطبعة. أرقام الصفحات والترويسة لا تُكتب في النص."]
    return "\n".join(lines)


def parse_verdicts(text, items):
    """The JSON array of verdicts in a model's answer: {item id: {verdict, text}}, or None."""
    start, end = (text or "").find("["), (text or "").rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        got = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(got, list):
        return None
    out = {}
    for it in items:
        v = next((x for x in got if isinstance(x, dict) and str(x.get("n")) == str(it.get("n"))), None)
        verdict, fix = "unsure", ""
        if v:
            verdict, fix = str(v.get("verdict") or ""), str(v.get("text") or "")
            if verdict not in ("keep", "fix", "unsure"):
                verdict = "unsure"
            # An insertion with nothing to insert is not an answer.
            if verdict == "fix" and not (it.get("core") or "").strip() and not fix.strip():
                verdict = "unsure"
        out[it["id"]] = {"verdict": verdict, "text": fix if verdict == "fix" else ""}
    return out


def chip_slug(chip):
    return re.sub(r"[^A-Za-z0-9.-]", "_", chip)


class Folder:
    """The book's review folder in Drive: every model's verdict files, and the places the user
    answered (answered.json from the status page's script)."""

    def __init__(self, folder, drive):
        self.folder = folder
        self.drive = drive
        self.files = drive.listing(folder)
        self.lock = threading.Lock()

    def json(self, name):
        f = self.files.get(name)
        if not f:
            return None
        try:
            return json.loads(self.drive.get(f["id"]).decode("utf-8-sig"))
        except (ValueError, helperbox.DriveError):
            return None

    def verdicts(self):
        """{item id: [{chip, verdict, text}]}."""
        out = {}
        for name in sorted(self.files):
            if not VERDICT_FILE.match(name):
                continue
            j = self.json(name)
            if not isinstance(j, dict) or not isinstance(j.get("verdicts"), dict):
                continue
            for k, v in j["verdicts"].items():
                if isinstance(v, dict):
                    out.setdefault(k, []).append({"chip": str(j.get("chip") or ""), "verdict": str(v.get("verdict") or ""),
                                                  "text": str(v.get("text") or "")})
        return out

    def answered(self):
        j = self.json("answered.json")
        return set(map(str, (j or {}).get("ids") or []))

    def save(self, chip, page, verdicts):
        """j-<chip>-pNNNN.json, added to what the model said before on the page."""
        name = f"j-{chip_slug(chip)}-p{int(page):04d}.json"
        old = self.json(name) or {}
        allv = dict(old.get("verdicts") or {})
        allv.update(verdicts)
        body = json.dumps({"chip": chip, "page": int(page), "at": now(), "verdicts": allv}, ensure_ascii=False).encode("utf-8")
        with self.lock:
            known = self.files.get(name)
        try:
            if known:
                self.drive.update(known["id"], body, "application/json")
            else:
                fid = self.drive.create(self.folder, name, body, "application/json")
                with self.lock:
                    self.files[name] = {"id": fid, "modifiedTime": ""}
            return True
        except helperbox.DriveError as e:
            print(f"  could not save {name}: {e}")
            return False


def page_png(path):
    """The page image (a JPEG with the places' boxes drawn) as PNG bytes for the API."""
    pix = pymupdf.Pixmap(path)
    try:
        return pix.tobytes("png")
    finally:
        pix = None


def review(pool, pages, image_dir, folder, models, answered, deadline, lv=live.NULL):
    """Each page's places the free models should judge (item_needs), shared by the models of
    the box, one request at a time per model. Returns (all verdicts, requests asked, places a
    model with quota left would still judge)."""
    verdicts = folder.verdicts()
    lock = threading.Lock()
    chips = {m: FREE + m for m in models}
    asked = [0]

    def todo(pg, chip):
        return [it for it in pg.get("items") or [] if it.get("id") not in answered
                and item_needs(it, verdicts.get(it["id"], []), chip)]

    def one(pg):
        img = os.path.join(image_dir, str(pg.get("img") or ""))
        if not pg.get("img") or not os.path.exists(img):
            return
        png = None
        tried = set()
        while time.time() < deadline:
            with lock:
                pick = next(((m, its) for m in pool.share(models, "review") if m not in tried and pool.available(m)
                             for its in [todo(pg, chips[m])] if its), None)
            if not pick:
                return
            model, items = pick
            tried.add(model)
            png = png or page_png(img)
            with live.doing(pg["page"], "review"):
                try:
                    text, finish = pool.read(png, judge_prompt(items), model)
                except converter.QuotaExhausted:
                    continue
                except Exception as e:  # one model's failure leaves the page to the others
                    print(f"  {model}: page {pg['page']}: {str(e)[:160]}")
                    continue
            with lock:
                asked[0] += 1
            got = parse_verdicts(text, items)
            if got is None:
                print(f"  {model}: page {pg['page']}: no verdicts in its answer ({finish[:40]})")
                lv.note(model, "noanswer", pg["page"], "review", secs=live.took())
                continue
            folder.save(chips[model], pg["page"], got)
            with lock:
                for k, v in got.items():
                    verdicts.setdefault(k, []).append({"chip": chips[model], **v})
            sure = sum(1 for v in got.values() if v["verdict"] != "unsure")
            lv.note(model, "judged", pg["page"], "review", secs=live.took(), n=sure)
            print(f"  {model}: page {pg['page']}: {len(got)} places, {sure} judged")

    with ThreadPoolExecutor(max_workers=max(1, len(models)) * 2) as tp:
        list(tp.map(one, pages))
    left = sum(1 for pg in pages for it in pg.get("items") or [] if it.get("id") not in answered
               and any(pool.available(m) and item_needs(it, verdicts.get(it["id"], []), chips[m]) for m in models))
    return verdicts, asked[0], left


def answers_of(pages, verdicts, answered):
    """The verdicts that become answers, in the form of the user's own (and the helper's)."""
    out = {}
    for pg in pages:
        for it in pg.get("items") or []:
            if it.get("kind") == "page" or it.get("id") in answered:
                continue
            v = item_answer(it, verdicts.get(it["id"], []))
            if not v:
                continue
            a = {"kind": it.get("kind"), "page": int(pg["page"]), "verdict": v["verdict"], "fp": it.get("fp") or "",
                 "before": it.get("before") or "", "core": it.get("core") or "", "after": it.get("after") or "",
                 "pos": int(it.get("pos") or 0), "base": pg.get("base") or "", "place": it.get("place") or "",
                 "at": now(), "by": v["by"]}
            if v["verdict"] == "fix":
                a["text"] = v["text"]
            out[it["id"]] = a
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="The free models judge a delivered book's unsure places")
    p.add_argument("--items", required=True, help="review/items.json of the book (its images beside it)")
    p.add_argument("--folder", required=True, help="The book's review folder in Drive")
    p.add_argument("--boxes", default="", help="The workflow's models input: review=a,b;out=c")
    p.add_argument("--judge", required=True, help="The saved verdicts (answers form): the new answers join them")
    p.add_argument("--summary", default="free-review.json")
    p.add_argument("--live", default="")
    args = p.parse_args(argv)

    boxes = converter.parse_boxes(args.boxes, ("review", "out"))
    models = boxes.get("review") or []
    token = os.environ.get("DRIVE_TOKEN", "")
    summary = {"models": models, "asked": 0, "answers": 0, "left": 0, "at": now()}
    try:
        with open(args.items, encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        items = {}
    pages = [pg for pg in items.get("pages") or [] if pg.get("items")]
    if not models or not pages or not token or not helperbox.FOLDER_ID.match(args.folder or ""):
        print("Free review: nothing to do" + ("" if token else " (no Drive token)"))
        with open(args.summary, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False)
        return 0
    pool = converter.ModelPool(models[0], "", "", 10)
    pool.set_boxes({"read1": [], "read2": [], "settle": []})
    have = converter.list_models(pool)
    models = converter.match_models(have, models) if have else models
    pool.set_out(boxes.get("out") or [])
    lv = live.open_live(args.live, token)
    pool.live = lv
    for m in models:
        lv.role(m, "review")
    lv.set_phase("review", pages=len(pages))
    lv.start()
    folder = Folder(args.folder, helperbox.Drive(token))
    answered = folder.answered()
    print(f"Free review: {sum(len(pg['items']) for pg in pages)} places on {len(pages)} pages, models: {', '.join(models)}")
    verdicts, asked, left = review(pool, pages, os.path.dirname(args.items), folder, models, answered,
                                   time.time() + REVIEW_SECONDS, lv)
    answers = answers_of(pages, verdicts, answered)
    try:
        with open(args.judge, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    merged = dict(saved.get("answers") or {}) if isinstance(saved.get("answers"), dict) else {}
    new = {k: v for k, v in answers.items() if merged.get(k, {}).get("verdict") != v["verdict"]
           or merged.get(k, {}).get("text") != v.get("text")}
    merged.update(answers)
    if new:
        os.makedirs(os.path.dirname(args.judge) or ".", exist_ok=True)
        with open(args.judge, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "answers": merged}, f, ensure_ascii=False)
    summary.update(models=models, asked=asked, answers=len(new), left=left, requests=dict(pool.requests),
                   models_out=sorted(pool.exhausted - pool.missing))
    with open(args.summary, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False)
    print(f"Free review: {asked} requests, {len(new)} new answers, {left} places a model could still judge")
    lv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
