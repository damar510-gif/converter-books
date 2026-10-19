"""Trial: can text-only models find misread words, and pick the main articles?

Text models read the saved, checked text of some pages and list words that look
misread, without changing anything. A Gemini model then looks at each page image and
says which listed words really are misread. The table-of-contents test compares each
model's choice of main articles with the one saved by the converter.

Does not change the converter's saved progress.
"""
import argparse
import glob
import json
import os
import re
import time

from google import genai
from google.genai import types

import compare_models as cm
import converter as c

SUSPECTS_PROMPT = """هذا نص صفحة من كتاب عربي قديم، نُقل من صورة ممسوحة بقارئ آلي، وقد يكون فيه كلمات أخطأ القارئ في قراءتها (حرف بدل حرف، نقطة ساقطة، كلمة مشوهة).
المطلوب: اذكر الكلمات التي يغلب على ظنك أنها **أخطاء قراءة**، لا أخطاء المؤلف ولا الإملاء القديم.
- لا تذكر الإملاء القديم المعروف (مثل «فى» و«الذى» و«هى»)، ولا أسماء الأعلام والأماكن الغريبة إلا إن كان الخطأ فيها ظاهراً.
- لا تغيّر النص، فقط اذكر الكلمات.
أجب بـ JSON فقط بهذا الشكل:
{"suspects": [{"word": "الكلمة كما هي في النص", "context": "ثلاث كلمات حولها", "fix": "ما تظنه الصواب"}]}
إن لم تجد شيئاً: {"suspects": []}

النص:
<<<
<<TEXT>>
>>>"""

VERIFY_PROMPT = """أمامك صورة صفحة من كتاب عربي مطبوع، وقائمة بكلمات من نسخة نصية لهذه الصفحة يُشك أن القارئ الآلي أخطأ فيها.
لكل كلمة: انظر في الصورة في الموضع المذكور، وقل هل الكلمة مطابقة للمطبوع حرفاً بحرف.
أجب بـ JSON فقط: {"verdicts": [{"n": رقم الكلمة, "correct": true أو false, "printed": "الكلمة كما هي مطبوعة"}]}

الكلمات:
<<LIST>>"""


def ask_json(model, prompt, png=None, client=None):
    """One request; returns (parsed JSON or None, error)."""
    if model.split(":", 1)[0] in cm.PROVIDERS:
        provider, name = model.split(":", 1)
        url, key_env = cm.PROVIDERS[provider]
        key = os.environ.get(key_env, "")
        if not key:
            return None, f"no {key_env}"
        body = {"model": name, "temperature": 0, "max_tokens": 8000,
                "messages": [{"role": "user", "content": prompt}]}
        if provider == "groq":
            body["reasoning_effort"] = "low"
        for attempt in range(3):
            r = cm.httpx.post(url, json=body, timeout=300, headers={"Authorization": f"Bearer {key}"})
            if r.status_code == 400 and "reasoning_effort" in body:
                body.pop("reasoning_effort")
                continue
            if r.status_code in (429, 500, 502, 503):
                time.sleep(60)
                continue
            if r.status_code != 200:
                return None, f"HTTP {r.status_code} {r.text[:160]}"
            try:
                text = r.json()["choices"][0]["message"]["content"] or ""
            except Exception:
                return None, f"bad answer {r.text[:160]!r}"
            return parse_json(text), ""
        return None, "rate limited"
    contents = [prompt] if png is None else [types.Part.from_bytes(data=png, mime_type="image/png"), prompt]
    for attempt in range(3):
        try:
            r = client.models.generate_content(model=model, contents=contents, config=types.GenerateContentConfig(
                max_output_tokens=8192, media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH))
            return parse_json(r.text or ""), ""
        except Exception as e:
            msg = str(e)
            if "429" in msg and "PerDay" in msg.replace(" ", ""):
                return None, "quota"
            if any(x in msg for x in ("429", "500", "503")):
                time.sleep(60)
                continue
            return None, msg[:160]
    return None, "rate limited"


def parse_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--pages", required=True)
    ap.add_argument("--models", required=True, help="comma separated provider:model")
    ap.add_argument("--verifiers", default="gemini-3.7-flash,gemini-3.6-flash,gemini-3-flash,gemini-2.5-flash,"
                                           "gemini-3.5-flash-lite,gemini-3.1-flash-lite")
    ap.add_argument("--work-dir", default="work")
    ap.add_argument("--out", default="compare-out")
    ap.add_argument("--gap", type=float, default=15)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    import pymupdf
    doc = pymupdf.open(args.pdf)
    pages = [i + 1 for i in c.parse_pages(args.pages, len(doc))]
    cache = glob.glob(os.path.join(args.work_dir, f"{c.file_sha(args.pdf)}-*"))
    if not cache:
        raise SystemExit("No saved pages for this book")
    records = []
    for path in sorted(glob.glob(os.path.join(cache[0], "[0-9]*.json"))):
        with open(path, encoding="utf-8") as f:
            records.append(json.load(f))
    text_of = {r["page"]: r.get("text") or "" for r in records}

    # 1) Suspects from each text model.
    found = {m: {} for m in models}
    for m in models:
        for p in pages:
            data, err = ask_json(m, SUSPECTS_PROMPT.replace("<<TEXT>>", text_of.get(p, "")))
            items = (data or {}).get("suspects") or []
            found[m][p] = [i for i in items if isinstance(i, dict) and i.get("word")]
            print(f"{m} p{p}: {len(found[m][p])} suspects {err}")
            time.sleep(args.gap)

    # 2) One image check per page for the union of suspect words.
    truth = {}
    verifiers = [v for v in args.verifiers.split(",") if v]
    for p in pages:
        words = []
        for m in models:
            for i in found[m][p]:
                if i["word"] not in [w for w, _ in words]:
                    words.append((i["word"], i.get("context", "")))
        if not words:
            continue
        listing = "\n".join(f"{n}. «{w}» في: {ctx}" for n, (w, ctx) in enumerate(words, 1))
        png, _ = c.page_image(doc, doc[p - 1])
        while verifiers:
            data, err = ask_json(verifiers[0], VERIFY_PROMPT.replace("<<LIST>>", listing), png, client)
            if err == "quota":
                verifiers.pop(0)
                continue
            break
        for v in (data or {}).get("verdicts") or []:
            try:
                w = words[int(v["n"]) - 1][0]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            truth[(p, w)] = (bool(v.get("correct")), v.get("printed", ""))
        print(f"verify p{p}: {len(words)} words, {err}")
        time.sleep(5)

    # 3) Table of contents: each model's choice of main articles vs the saved one.
    toc_rows = []
    articles = os.path.join(cache[0], "articles.json")
    if os.path.exists(articles):
        with open(articles, encoding="utf-8") as f:
            saved_main = set(json.load(f)["main"])
        md = c.assemble(sorted(records, key=lambda r: r["page"]))
        lines = md.split("\n")
        heads = [i for i, line in enumerate(lines) if line.startswith("# ")]
        plain = lambda line: re.sub(r"\[\]\{[^}]*\}", "", line).lstrip("#").strip()
        items = []
        for k, i in enumerate(heads, 1):
            after = " ".join(plain(l) for l in lines[i + 1:i + 6] if l.strip() and not l.startswith("["))[:160]
            items.append(f"{k}. {plain(lines[i])}\n   ← {after}")
        prompt = c.ARTICLES_PROMPT.format(items="\n".join(items))
        for m in models:
            data, err = ask_json(m, prompt)
            chosen = set()
            for n in (data or {}).get("main") or []:
                try:
                    chosen.add(int(n))
                except (TypeError, ValueError):
                    pass
            toc_rows.append((m, len(chosen & saved_main), len(chosen - saved_main), len(saved_main - chosen), err))

    out = ["# تجربة النماذج النصية", "", "## كشف أخطاء القراءة",
           "| model | suspects | real errors (image) | wrongly suspected | unchecked |", "|---|---|---|---|---|"]
    for m in models:
        flagged = [(p, i["word"]) for p in pages for i in found[m][p]]
        real = sum(1 for k in flagged if k in truth and not truth[k][0])
        wrong = sum(1 for k in flagged if k in truth and truth[k][0])
        out.append(f"| {m} | {len(flagged)} | {real} | {wrong} | {len(flagged) - real - wrong} |")
    out += ["", "## أخطاء حقيقية وُجدت (الكلمة في النص ← المطبوع)"]
    for (p, w), (ok, printed) in sorted(truth.items()):
        if not ok:
            by = [m.split("/")[-1] for m in models if any(i["word"] == w for i in found[m][p])]
            out.append(f"- ص {p}: «{w}» ← «{printed}» ({', '.join(by)})")
    if toc_rows:
        out += ["", "## الفهرس (مقارنة باختيار Gemini المعتمد)",
                "| model | agreed | extra | missed | error |", "|---|---|---|---|---|"]
        out += [f"| {m} | {a} | {x} | {mi} | {e[:60]} |" for m, a, x, mi, e in toc_rows]
    with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()
