"""Reads the same pages with several models and writes a side-by-side report.

Used to choose models: it does not change the converter's saved progress.
"""
import argparse
import glob
import json
import os
import time

import pymupdf
from google import genai
from google.genai import types

import converter as c


def pick_models(client, wanted):
    names = [m.name.split("/", 1)[-1] for m in client.models.list()]
    picked = []
    for want in wanted:
        if want in names:
            picked.append(want)
            continue
        # Loose match, e.g. "gemma-4-31b" -> "gemma-4-31b-it".
        match = sorted(n for n in names if want in n)
        if match:
            picked.append(match[0])
        else:
            print(f"Model not found: {want}")
    print("Available gemma/flash-lite models:", [n for n in names if "gemma" in n or "lite" in n])
    return picked


CONFIGS = [
    ("full", lambda: types.GenerateContentConfig(
        max_output_tokens=8192, safety_settings=c.SAFETY_OFF,
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH,
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW))),
    ("no-thinking", lambda: types.GenerateContentConfig(
        max_output_tokens=8192, safety_settings=c.SAFETY_OFF,
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH)),
    ("plain", lambda: types.GenerateContentConfig(max_output_tokens=8192)),
]


def read(client, model, png, prompt, start_config):
    """Returns (text, finish, config_name, seconds, usage, error)."""
    for i in range(start_config, len(CONFIGS)):
        name, make = CONFIGS[i]
        for attempt in range(3):
            t = time.time()
            try:
                r = client.models.generate_content(
                    model=model,
                    contents=[types.Part.from_bytes(data=png, mime_type="image/png"), prompt],
                    config=make())
                finish = str(r.candidates[0].finish_reason) if r.candidates else ""
                usage = r.usage_metadata.model_dump(exclude_none=True) if r.usage_metadata else {}
                return (r.text or "").strip(), finish, i, round(time.time() - t, 1), usage, ""
            except Exception as e:
                msg = str(e)
                if "400" in msg or "INVALID_ARGUMENT" in msg:
                    print(f"  {model} rejected config '{name}': {msg[:160]}")
                    break
                if "429" in msg or "503" in msg or "500" in msg or "UNAVAILABLE" in msg:
                    print(f"  {model}: {msg[:120]} -> wait 60s")
                    time.sleep(60)
                    continue
                return "", "", i, 0, {}, msg[:300]
    return "", "", len(CONFIGS) - 1, 0, {}, "all configs rejected"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--pages", required=True)
    ap.add_argument("--models", required=True, help="comma separated")
    ap.add_argument("--work-dir", default="work")
    ap.add_argument("--out", default="compare")
    ap.add_argument("--gap", type=float, default=20, help="seconds between requests to one model")
    args = ap.parse_args()

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    models = pick_models(client, [m.strip() for m in args.models.split(",") if m.strip()])
    doc = pymupdf.open(args.pdf)
    pages = [i + 1 for i in c.parse_pages(args.pages, len(doc))]

    # The main converter's saved reading of every page, for the vocabulary and as a baseline.
    cache = glob.glob(os.path.join(args.work_dir, f"{c.file_sha(args.pdf)}-*"))
    saved = {}
    if cache:
        for path in glob.glob(os.path.join(cache[0], "*.json")):
            if os.path.basename(path)[:4].isdigit():
                with open(path, encoding="utf-8") as f:
                    rec = json.load(f)
                saved[rec["page"]] = rec.get("text") or ""
    vocab = set()
    for text in saved.values():
        vocab.update(c.norm_words(text))

    os.makedirs(args.out, exist_ok=True)
    rows = []
    tess = {}
    for p in pages:
        png, _ = c.page_image(doc, doc[p - 1])
        tess[p] = c.tesseract_text(png)
        if p in saved:
            miss = c.missing_lines(tess[p], saved[p], vocab)
            rows.append({"model": "saved (main run)", "page": p, "chars": len(saved[p]),
                         "missing_lines": len(miss), "seconds": "", "config": "", "error": ""})
            with open(os.path.join(args.out, f"p{p:03d}-saved.md"), "w", encoding="utf-8") as f:
                f.write(saved[p])

    for model in models:
        start = 0
        for p in pages:
            png, pix = c.page_image(doc, doc[p - 1])
            prompt = c.OCR_PROMPT.format(spread=c.SPREAD_HINT if pix.width > pix.height * 1.15 else "")
            text, finish, start, secs, usage, err = read(client, model, png, prompt, start)
            text = c.clean_ocr(text)
            miss = c.missing_lines(tess[p], text, vocab) if text else []
            rows.append({"model": model, "page": p, "chars": len(text), "missing_lines": len(miss),
                         "seconds": secs, "config": CONFIGS[start][0], "finish": finish,
                         "tokens": usage.get("total_token_count", ""), "error": err})
            with open(os.path.join(args.out, f"p{p:03d}-{model}.md"), "w", encoding="utf-8") as f:
                f.write(text)
            print(f"{model} p{p}: chars={len(text)} missing={len(miss)} {secs}s {finish} {err[:80]}")
            time.sleep(args.gap)

    lines = ["| model | page | chars | missing lines | seconds | tokens | config | finish/error |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['model']} | {r['page']} | {r['chars']} | {r['missing_lines']} | {r['seconds']} | "
                     f"{r.get('tokens', '')} | {r['config']} | {r.get('finish', '')} {r['error'][:80]} |")
    with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
