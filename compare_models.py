"""Reads the same pages with several models and writes a side-by-side report.

Used to choose models: it does not change the converter's saved progress.
"""
import argparse
import base64
import glob
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pymupdf
from google import genai
from google.genai import types

import converter as c
import crosscheck

# Other providers with an OpenAI-style API, written as "provider:model" in --models.
PROVIDERS = {
    "github": ("https://models.github.ai/inference/chat/completions", "GITHUB_TOKEN"),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY"),
}


def read_openai(model, jpeg, prompt):
    """Returns (text, finish, seconds, usage, error) from an OpenAI-style provider."""
    provider, name = model.split(":", 1)
    url, key_env = PROVIDERS[provider]
    key = os.environ.get(key_env, "")
    if not key:
        return "", "", 0, {}, f"no {key_env}"
    body = {
        "model": name,
        "temperature": 0,
        # Room for "thinking" models, which spend part of the answer on reasoning.
        "max_tokens": 16000,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
            {"type": "text", "text": prompt},
        ]}],
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if provider == "github":
        body["max_tokens"] = 4096
        headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"})
    # Ask reasoning models to think little: page copying needs no reasoning.
    extras = {"openrouter": {"reasoning": {"effort": "low"}}, "groq": {"reasoning_effort": "none"}}.get(provider, {})
    body.update(extras)
    for attempt in range(3):
        t = time.time()
        try:
            r = httpx.post(url, json=body, timeout=300, headers=headers)
        except Exception as e:
            return "", "", round(time.time() - t, 1), {}, str(e)[:200]
        if r.status_code in (429, 500, 502, 503):
            print(f"  {model}: HTTP {r.status_code} {r.text[:120]} -> wait 60s")
            time.sleep(60)
            continue
        if r.status_code == 400 and extras and any(k in body for k in extras):
            # The model does not take the reasoning option: try once without it.
            for k in extras:
                body.pop(k, None)
            continue
        if r.status_code != 200:
            return "", "", round(time.time() - t, 1), {}, f"HTTP {r.status_code} {r.text[:200]}"
        try:
            d = r.json()
        except ValueError:
            return "", "", round(time.time() - t, 1), {}, f"not JSON: {r.text[:200]!r}"
        choice = (d.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        return text.strip(), str(choice.get("finish_reason", "")), round(time.time() - t, 1), d.get("usage") or {}, ""
    return "", "", 0, {}, "rate limited"


def list_free_vision_models(out):
    """OpenRouter's free models that accept images, for choosing what to test."""
    try:
        data = httpx.get("https://openrouter.ai/api/v1/models", timeout=60).json().get("data", [])
    except Exception as e:
        print(f"Could not list OpenRouter models: {e}")
        return
    names = sorted(m["id"] for m in data if m.get("id", "").endswith(":free")
                   and "image" in ((m.get("architecture") or {}).get("input_modalities") or []))
    with open(os.path.join(out, "openrouter-free-vision.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(names) + "\n")
    # Groq's model list needs the key; it shows which vision models the account has.
    key = os.environ.get("GROQ_API_KEY")
    if key:
        try:
            data = httpx.get("https://api.groq.com/openai/v1/models", timeout=60,
                             headers={"Authorization": f"Bearer {key}"}).json().get("data", [])
            with open(os.path.join(out, "groq-models.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(m.get("id", "") for m in data)) + "\n")
        except Exception as e:
            print(f"Could not list Groq models: {e}")


def pick_models(client, wanted):
    names = [m.name.split("/", 1)[-1] for m in client.models.list()]
    picked = []
    for want in wanted:
        if want.split(":", 1)[0] in PROVIDERS or want in names:
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
    ("minimal-thinking", lambda: types.GenerateContentConfig(
        max_output_tokens=8192, safety_settings=c.SAFETY_OFF,
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH,
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL))),
    ("low-thinking", lambda: types.GenerateContentConfig(
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
                # Some models return thought parts even with thinking off: keep only the answer.
                parts = r.candidates[0].content.parts if r.candidates and r.candidates[0].content else []
                text = "".join(p.text for p in (parts or []) if p.text and not getattr(p, "thought", False))
                return text.strip(), finish, i, round(time.time() - t, 1), usage, ""
            except Exception as e:
                msg = str(e)
                if "400" in msg or "INVALID_ARGUMENT" in msg:
                    print(f"  {model} rejected config '{name}': {msg[:160]}")
                    break
                if "timed out" in msg.lower() or "timeout" in msg.lower():
                    return "", "", i, round(time.time() - t, 1), {}, "timeout"
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

    # A request that takes longer than 4 minutes counts as a failure.
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                          http_options=types.HttpOptions(timeout=240_000))
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

    lock = threading.Lock()

    def write_summary():
        lines = ["| model | page | match % | chars | missing lines | seconds | tokens | config | finish/error |",
                 "|---|---|---|---|---|---|---|---|---|"]
        for r in sorted(rows, key=lambda r: (r["model"], r["page"])):
            lines.append(f"| {r['model']} | {r['page']} | {r.get('match', '')} | {r['chars']} | {r['missing_lines']} | "
                         f"{r['seconds']} | {r.get('tokens', '')} | {r['config']} | {r.get('finish', '')} {r['error'][:80]} |")
        # Average agreement with the saved, checked text: the main number to compare models by.
        by_model = {}
        for r in rows:
            if r.get("match") not in (None, ""):
                by_model.setdefault(r["model"], []).append(r.get("match", 0) or 0)
        lines += ["", "| model | pages | average match % |", "|---|---|---|"]
        for m, v in sorted(by_model.items(), key=lambda x: -sum(x[1]) / len(x[1])):
            lines.append(f"| {m} | {len(v)} | {round(sum(v) / len(v), 1)} |")
        with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return lines

    def run_model(model):
        start = 0
        local = pymupdf.open(args.pdf)
        for p in pages:
            png, pix = c.page_image(local, local[p - 1])
            prompt = c.OCR_PROMPT.format(spread=c.SPREAD_HINT if pix.width > pix.height * 1.15 else "")
            if model.split(":", 1)[0] in PROVIDERS:
                with c.PDF_LOCK:
                    jpeg = pix.tobytes("jpg", jpg_quality=90)
                text, finish, secs, usage, err = read_openai(model, jpeg, prompt)
                config = "openai-style"
                tokens = f"{usage.get('prompt_tokens', '')}/{usage.get('completion_tokens', '')}"
            else:
                text, finish, start, secs, usage, err = read(client, model, png, prompt, start)
                config = CONFIGS[start][0]
                tokens = (f"{usage.get('prompt_token_count', '')}/{usage.get('candidates_token_count', '')}"
                          f"/{usage.get('thoughts_token_count', '')}")
            text = c.clean_ocr(text)
            miss = c.missing_lines(tess[p], text, vocab) if text else []
            match = round(100 * crosscheck.compare(text, saved[p])[0], 1) if saved.get(p) else ""
            with lock:
                rows.append({"model": model, "page": p, "chars": len(text), "missing_lines": len(miss),
                             "seconds": secs, "config": config, "finish": finish, "match": match,
                             "tokens": tokens, "error": err})
                with open(os.path.join(args.out, f"p{p:03d}-{model.replace('/', '_').replace(':', '_')}.md"),
                          "w", encoding="utf-8") as f:
                    f.write(text)
                write_summary()
            print(f"{model} p{p}: chars={len(text)} missing={len(miss)} {secs}s {tokens} {finish} {err[:80]}")
            time.sleep(args.gap)

    list_free_vision_models(args.out)
    # Each model has its own quota, so they run side by side.
    with ThreadPoolExecutor(max_workers=len(models) or 1) as pool:
        list(pool.map(run_model, models))
    print("\n".join(write_summary()))


if __name__ == "__main__":
    main()
