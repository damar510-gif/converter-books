"""Places on unsure pages for the user to check by hand, and applying the user's answers.

Plain functions over the page records, Tesseract's word boxes (TSV) and the answers
file; the converter passes all of it in (see docs/human-review-plan.md). Only drawing
the page images uses PyMuPDF.

A place ("item") is before + core + after, a literal piece of the page text starting at
pos that occurs only once in it. The user either confirms the reading of core ("keep")
or gives the right text for it ("fix"). Every answer carries its place, and every run
applies all answers again: an answer already in the text is recognised and left alone.
Pages with answers are "pinned": their text is the user's and never changes again.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher

import crosscheck

# At most this many places are listed per page; "the rest of this page is right" covers the others.
MAX_ITEMS = 25
# Words of context on each side of a place: this many at first, more until the place is unique.
CONTEXT_MIN, CONTEXT_MAX = 3, 12
# Longest text an answer may carry (a whole page for the "page" kind).
MAX_ANSWER, MAX_PAGE_ANSWER = 2000, 20000
# A Tesseract line is in the text when a stretch of the text reads this much alike, letter by
# letter. Measured on a real book: lines that were there scored 0.65-0.97, lines that were not
# 0.44-0.58. Tesseract misreads many letters, so most "missing" lines are really there.
PRESENT = 0.7
# A difference is found in the page image when its words and their neighbours read this alike.
LOCATED = 0.6
# Pages are drawn at most this wide, as JPEG of this quality.
IMAGE_WIDTH, JPEG_QUALITY = 1200, 60

FOOTNOTE_LINE = re.compile(r"^\s*(\[\^[^\]\s]+\]:|\([0-9٠-٩۰-۹]{1,3}\)\s)")
BLOCK_START = re.compile(r"\s*(#{1,6}\s|\|\s|>\s|\[\^[^\]\s]+\]:)")
FOOTNOTE_REF = re.compile(r"\[\^[^\]\s]+\](?!:)")
# Differences made only of footnote numbers: the readers numbered the notes differently.
NOTES_ONLY = re.compile(r"(\s*(\[\^[^\]\s]+\]:?|\(?[0-9٠-٩۰-۹]{1,3}\)?))*\s*")
# Characters an answer may not carry: control characters and direction marks.
UNWANTED = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩﻿]")
WHITE = re.compile(r"\s*")
NOTES_AFTER = re.compile(r"\s*\[\^[^\]\s]+\]:")


def sha(text, n=10):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def base_of(text):
    """Fingerprint of a page text: an answer made on this text may be applied at its offset."""
    return sha(text or "", 12)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norms(text):
    return [w for w, _, _ in crosscheck.tokens(text)]


# ---------------------------------------------------------------- finding text


def fuzzy_find(needle, hay, slack=2, lo=0, hi=None):
    """The stretch hay[i:j] (i >= lo, j <= hi) that reads most like the word list needle,
    comparing letters, since Tesseract and the readers misspell different letters.
    Returns (ratio, i, j)."""
    hi = len(hay) if hi is None else hi
    best = (0.0, lo, lo)
    if not needle or hi <= lo:
        return best
    sm = SequenceMatcher(None, autojunk=False)
    sm.set_seq2(" ".join(needle))
    n = len(needle)
    for i in range(lo, hi):
        for w in range(max(1, n - slack), n + slack + 1):
            if i + w > hi:
                break
            sm.set_seq1(" ".join(hay[i:i + w]))
            if sm.real_quick_ratio() <= best[0] or sm.quick_ratio() <= best[0]:
                continue
            r = sm.ratio()
            if r > best[0]:
                best = (r, i, i + w)
    return best


def align(a, b):
    """Pairs words of a with words of b, in order, by how alike they read: {index in a: index in b}."""
    n, m = len(a), len(b)
    sim = [[SequenceMatcher(None, x, y).ratio() for y in b] for x in a]
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    for x in range(n - 1, -1, -1):
        for y in range(m - 1, -1, -1):
            best = max(score[x + 1][y], score[x][y + 1])
            if sim[x][y] >= 0.5:
                best = max(best, sim[x][y] + score[x + 1][y + 1])
            score[x][y] = best
    out, x, y = {}, 0, 0
    while x < n and y < m:
        if sim[x][y] >= 0.5 and score[x][y] == sim[x][y] + score[x + 1][y + 1]:
            out[x] = y
            x, y = x + 1, y + 1
        elif score[x][y] == score[x + 1][y]:
            x += 1
        else:
            y += 1
    return out


def occurrences(text, part, limit=2):
    """How many times part occurs in text (counting overlaps), up to limit."""
    n, at = 0, text.find(part)
    while at >= 0 and n < limit:
        n += 1
        at = text.find(part, at + 1)
    return n


# ---------------------------------------------------------------- Tesseract's boxes


def parse_tsv(tsv):
    """Tesseract's TSV output as lines in reading order:
    [{"box": [x0, y0, x1, y1], "text", "words": [{"text", "norm", "box"}]}]."""
    lines, by_key = [], {}
    for row in (tsv or "").split("\n")[1:]:
        f = row.split("\t")
        if len(f) < 12:
            continue
        try:
            level = int(f[0])
            key = (int(f[2]), int(f[3]), int(f[4]))
            x, y, w, h = (int(v) for v in f[6:10])
        except ValueError:
            continue
        box = [x, y, x + w, y + h]
        if level == 4:
            by_key[key] = {"box": box, "words": []}
            lines.append(by_key[key])
        elif level == 5 and key in by_key and f[11].strip():
            text = f[11].strip()
            by_key[key]["words"].append({"text": text, "norm": crosscheck.norm_word(text), "box": box})
    lines = [l for l in lines if any(w["norm"] for w in l["words"])]
    for l in lines:
        l["text"] = " ".join(w["text"] for w in l["words"])
    return lines


def line_norms(line):
    return [w["norm"] for w in line["words"] if w["norm"]]


def column_of(box, gutter):
    """0 for the right column (read first) or a page without columns, 1 for the left one."""
    if gutter is None:
        return 0
    return 0 if (box[0] + box[2]) / 2 >= gutter else 1


def column_span(lines, col, gutter, width):
    """[x0, x1] of the text in a column, so the page can be zoomed to it."""
    boxes = [l["box"] for l in lines if column_of(l["box"], gutter) == col]
    if not boxes:
        return [0, width]
    return [max(0, min(b[0] for b in boxes) - 10), min(width, max(b[2] for b in boxes) + 10)]


def pad(box, width, height, by=4):
    return [max(0, box[0] - by), max(0, box[1] - by), min(width, box[2] + by), min(height, box[3] + by)]


def union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def tsv_missing(lines, text, vocab):
    """Indexes of the Tesseract lines that the check counts as missing from text."""
    miss = set(crosscheck.missing_lines("\n".join(l["text"] for l in lines), text, vocab))
    return [k for k, l in enumerate(lines) if l["text"].strip() in miss]


# ---------------------------------------------------------------- places in the text


def context(text, start, end):
    """(before, core, after, pos): core = text[start:end] with a few words on each side,
    enough for before + core + after (which starts at pos) to occur only once in text."""
    left = [m.start() for m in crosscheck.TOKEN.finditer(text, 0, start)]
    right = [m.end() for m in crosscheck.TOKEN.finditer(text, end)]
    for k in range(CONTEXT_MIN, CONTEXT_MAX + 1):
        b = left[-k] if len(left) >= k else 0
        a = right[k - 1] if len(right) >= k else len(text)
        if occurrences(text, text[b:a]) == 1:
            break
    return text[b:start], text[start:end], text[end:a], b


def white_after(text, pos):
    """The white space starting at pos: (start, end)."""
    return pos, WHITE.match(text, pos).end()


def white_before(text, pos):
    start = pos
    while start > 0 and text[start - 1].isspace():
        start -= 1
    return start, pos


def after_word(text, toks, t):
    """The white space after word t (and any punctuation that follows it on its line)."""
    q = toks[t][2]
    for m in crosscheck.TOKEN.finditer(text, q):
        if "\n" in text[q:m.start()] or crosscheck.norm_word(m.group()):
            break
        q = m.end()
    return white_after(text, q)


def before_word(text, toks, t):
    """The white space before word t, or before its whole line when only marks such as
    "# " or "| " come before it on that line."""
    q = toks[t][1]
    line_start = text.rfind("\n", 0, q) + 1
    if not crosscheck.norm_word(text[line_start:q]):
        q = line_start
    return white_before(text, q)


def body_end(text):
    """The white space after the page's own text, before the footnotes at its end."""
    lines = text.split("\n")
    k = len(lines)
    while k > 0 and (not lines[k - 1].strip() or FOOTNOTE_LINE.match(lines[k - 1])):
        k -= 1
    return white_after(text, len("\n".join(lines[:k]).rstrip()))


def make_item(page, kind, text, start, end, fp, **extra):
    before, core, after, pos = context(text, start, end)
    item = {"id": f"p{page}-{sha(f'{kind}|{before}|{core}|{after}|{fp}')}", "fp": fp, "kind": kind,
            "before": before, "core": core, "after": after, "pos": pos}
    item.update(extra)
    return item


def moved(run, pairs):
    """A run of 5+ words whose word pairs are nearly all in the other text: it was put
    somewhere else there (another column order, notes at the end), not lost."""
    p = set(zip(run, run[1:]))
    return len(run) >= 5 and len(p & pairs) >= 0.8 * len(p)


def diff_items(page, text, other, tess_words):
    """The places where the page text and the other reading differ."""
    ta, tb = norms(text), norms(other)
    pairs_a, pairs_b = set(zip(ta, ta[1:])), set(zip(tb, tb[1:]))

    def skip(s):
        # Footnote numbers only, or text the other reading has elsewhere, on both sides.
        fw, ow = ta[s["i1"]:s["i2"]], tb[s["j1"]:s["j2"]]
        return (NOTES_ONLY.fullmatch(s["final"]) or moved(fw, pairs_b)) \
            and (NOTES_ONLY.fullmatch(s["other"]) or moved(ow, pairs_a))

    out = []
    for s in crosscheck.compare_spans(text, other, skip=skip):
        fw, ow = ta[s["i1"]:s["i2"]], tb[s["j1"]:s["j2"]]
        if s["start"] == s["end"]:
            start, end = white_after(text, s["start"])
        else:
            start, end = s["start"], s["end"]
        other_text = other[s["other_start"]:s["other_end"]] if s["other_start"] is not None else ""
        extra = {}
        # Text only this reading has that Tesseract saw too: the other reading dropped it.
        if len(fw) >= 5 and len(ow) <= 1 and tess_words and fuzzy_find(fw, tess_words)[0] >= PRESENT:
            extra["hint"] = "likely-ok"
        out.append(make_item(page, "diff", text, start, end,
                             f"diff|{' '.join(norms(text[start:end]))}|{' '.join(norms(other_text))}",
                             options=[{"label": "other", "text": other_text}], **extra))
    return out


def best_in(words, text):
    """The literal piece of text that reads like the word list, or ''."""
    toks = crosscheck.tokens(text)
    r, i, j = fuzzy_find(words, [n for n, _, _ in toks])
    return text[toks[i][1]:toks[j - 1][2]] if r >= PRESENT else ""


def missing_items(page, text, lines, miss, others, gutter, size, heads):
    """Places for the Tesseract lines the check counted as missing. A line found in the
    text after all (Tesseract misread it) is shown as it was read, "likely right"; a line
    really missing gets a place to insert it, next to its neighbours in the image (lines
    missing one after another share one place)."""
    width, height = size
    toks = crosscheck.tokens(text)
    hay = [n for n, _, _ in toks]
    found = {}
    miss_set = set(miss)

    def where(k):
        if k not in found:
            found[k] = fuzzy_find(line_norms(lines[k]), hay)
        return found[k]

    def neighbour(k, step, col):
        x = k + step
        while 0 <= x < len(lines):
            if x not in miss_set and len(line_norms(lines[x])) >= 3 \
                    and (col is None or column_of(lines[x]["box"], gutter) == col) and where(x)[0] >= PRESENT:
                return x
            x += step
        return None

    def options(ks):
        words = [w for k in ks for w in line_norms(lines[k])]
        out = [{"label": "other", "text": got} for got in (best_in(words, o) for o in others) if got][:1]
        return out + [{"label": "ocr", "text": " ".join(lines[k]["text"] for k in ks)}]

    heights = sorted(l["box"][3] - l["box"][1] for l in lines)
    median = heights[len(heights) // 2] if heights else 0
    out, groups = [], {}
    for k in miss:
        line = lines[k]
        col = column_of(line["box"], gutter)
        r, i, j = where(k)
        if r >= PRESENT:
            out.append(make_item(page, "missing", text, toks[i][1], toks[j - 1][2], "missing|" + " ".join(line_norms(line)),
                                 options=options([k]), hint="likely-ok", boxes=[pad(line["box"], width, height)],
                                 col=column_span(lines, col, gutter, width), located=True))
            continue
        prev, nxt = neighbour(k, -1, col), neighbour(k, 1, col)
        if prev is None and nxt is None:
            prev, nxt = neighbour(k, -1, None), neighbour(k, 1, None)
        if prev is not None:
            (start, end), place = after_word(text, toks, where(prev)[2] - 1), "after"
        elif nxt is not None:
            (start, end), place = before_word(text, toks, where(nxt)[1]), "before"
        else:
            (start, end), place = body_end(text), "end"
        if start == 0 and place != "end":
            place = "start"
        groups.setdefault((start, end, place), []).append(k)
    for (start, end, place), ks in groups.items():
        first = lines[ks[0]]
        item = make_item(page, "missing", text, start, end,
                         "missing|" + " ".join(w for k in ks for w in line_norms(lines[k])), options=options(ks),
                         place=place, boxes=[pad(lines[k]["box"], width, height) for k in ks],
                         col=column_span(lines, column_of(first["box"], gutter), gutter, width), located=True)
        if first["box"][1] < 0.08 * height and repeats(line_norms(first), heads) >= 2:
            item["hint"] = "header"
        elif median and first["box"][3] - first["box"][1] >= 1.4 * median:
            item["hint"] = "heading"
        out.append(item)
    return out


def repeats(words, heads):
    """On how many other pages a line like this one is among the first lines (a running header)."""
    target = " ".join(words)
    n = 0
    for page_heads in heads or []:
        if any(SequenceMatcher(None, target, " ".join(h), autojunk=False).ratio() >= PRESENT for h in page_heads):
            n += 1
    return n


def locate(item, seq, hay, lines, gutter, size):
    """Boxes of a difference in the page image: its words and their neighbours are looked
    for among Tesseract's words, by how alike they read. None when not found for sure."""
    width, height = size
    b, c, a = norms(item["before"])[-6:], norms(item["core"]), norms(item["after"])[:6]
    needle = b + c + a
    if len(needle) < 3:
        return None
    r, i, j = fuzzy_find(needle, hay, slack=3)
    if r < LOCATED:
        return None
    other = max(fuzzy_find(needle, hay, slack=3, hi=i)[0], fuzzy_find(needle, hay, slack=3, lo=j)[0])
    if other >= r - 0.03:
        return None
    pairs = align(needle, hay[i:j])
    nb, nc = len(b), len(c)
    before_hits = [pairs[x] for x in range(nb) if x in pairs]
    after_hits = [pairs[x] for x in range(nb + nc, len(needle)) if x in pairs]
    core_hits = [pairs[x] for x in range(nb, nb + nc) if x in pairs]
    if core_hits:
        lo, hi = min(core_hits), max(core_hits)
        if nb not in pairs and before_hits:
            lo = min(lo, before_hits[-1] + 1)
        if nb + nc - 1 not in pairs and after_hits:
            hi = max(hi, after_hits[0] - 1)
    elif before_hits and after_hits:
        lo, hi = before_hits[-1], after_hits[0]
    else:
        return None
    if hi < lo:
        return None
    by_line = {}
    for x in range(i + lo, i + hi + 1):
        line_no, word = seq[x]
        by_line.setdefault(line_no, []).append(word["box"])
    boxes = [pad(union(v), width, height) for _, v in sorted(by_line.items())]
    first = lines[min(by_line)]
    return {"boxes": boxes, "col": column_span(lines, column_of(first["box"], gutter), gutter, width),
            "located": True}


# ---------------------------------------------------------------- the answers


def clean_answer(text, limit):
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    return UNWANTED.sub("", text)[:limit]


def replacement(ans, new):
    """What takes the place of core when the answer says the right text is new."""
    core, before, after = ans.get("core", ""), ans.get("before", ""), ans.get("after", "")
    if core.strip() or ans.get("kind") == "page":
        # Footnote marks the correction dropped are kept, after it.
        lost = [m for m in FOOTNOTE_REF.findall(core) if m not in new]
        if lost and not new.strip():
            return "".join(lost)
        return new + ("".join(" " + m for m in lost) if lost else "")
    new = new.strip()
    if not new:
        return core
    if ans.get("kind") != "missing":
        # Words one reading has between two others.
        return (" " if before else "") + new + (core or (" " if after else ""))
    place = ans.get("place") or ("start" if not before else "end" if not after.strip() else "after")
    heading = new.startswith("#")
    if place == "start":
        return new + ("\n\n" if heading or BLOCK_START.match(after) else "\n") + core
    if place == "end" or NOTES_AFTER.match(after):
        return ("\n\n" if heading else "\n") + new + core
    if heading:
        return "\n\n" + new + "\n\n"
    if place == "before":
        return core + new + ("\n\n" if BLOCK_START.match(after) else " ")
    last = before[before.rfind("\n") + 1:]
    if last.startswith("#"):
        # Right after a heading: the line starts the paragraph that follows it.
        return core + new + ("\n\n" if BLOCK_START.match(after) else " ")
    if last.startswith("| "):
        return "\n| " + re.sub(r"^\|\s*", "", new) + core
    return " " + new + core


def find_place(text, before, core, after, insertion):
    """Offset of core in text, found by its context: the whole of it first, then less of it
    (a neighbouring correction may have changed the rest). None if not found exactly once."""
    b_starts = [m.start() for m in crosscheck.TOKEN.finditer(before)]
    a_ends = [m.end() for m in crosscheck.TOKEN.finditer(after)]
    tried = set()
    for k in (CONTEXT_MAX + 1, 3, 2, 1):
        if insertion and k < 2:
            break
        b = before[b_starts[-k]:] if len(b_starts) >= k else before
        a = after[:a_ends[k - 1]] if len(a_ends) >= k else after
        if (b, a) in tried:
            continue
        tried.add((b, a))
        target = b + core + a
        if target and occurrences(text, target) == 1:
            return text.find(target) + len(b)
    return None


def apply_answer(text, ans, trust_pos=False):
    """Applies one answer to a page text: (new text, result, (start, end) of the answered
    place in the new text or None). result is "kept" (the reading was right), "applied",
    "already" (the text has the correction) or "conflict" (the place is no longer there).
    trust_pos: the text is the one the answer was made on, at least up to its place."""
    if ans.get("kind") == "rest" or ans.get("verdict") == "keep":
        return text, "kept", None
    if ans.get("verdict") != "fix":
        return text, "conflict", None
    before, core, after = ans.get("before", ""), ans.get("core", ""), ans.get("after", "")
    new = replacement(ans, clean_answer(ans.get("text"), MAX_PAGE_ANSWER if ans.get("kind") == "page"
                                        else MAX_ANSWER))
    if new == core:
        return text, "kept", None
    insertion = not core.strip() and ans.get("kind") != "page"
    pos = ans.get("pos")
    at = None
    if trust_pos and isinstance(pos, int) and text.startswith(before + core + after, pos):
        at = pos + len(before)
    if at is None:
        at = find_place(text, before, core, after, insertion)
    if at is not None:
        return text[:at] + new + text[at + len(core):], "applied", (at, at + len(new))
    at = find_place(text, before, new, after, insertion)
    if at is not None:
        return text, "already", (at, at + len(new))
    return text, "conflict", None


def pin_of(rec):
    return (rec.get("check") or {}).get("human")


def pin_record(rec, page_no, pin, check_version):
    """The page as the user left it: their text over the saved record, or a record made
    from the pin alone. Pinned pages are never read, settled or judged again."""
    if rec is None:
        rec = {"page": page_no, "status": "ok",
               "a": {"model": "human", "text": pin["text"], "finish": "", "via": "full", "status": "ok"}}
    check = rec.get("check") or {}
    old = check.get("human") or {}
    if old and old.get("at", "") > pin.get("at", ""):
        return rec
    rec["status"] = "ok"
    rec["text"] = pin["text"]
    rec["check"] = {**check, "v": check_version, "state": pin.get("state") or check.get("state") or "unsure",
                    "human": {"ids": list(pin.get("ids", [])), "spans": dict(pin.get("spans", {})),
                              "at": pin.get("at", "")}}
    return rec


def overlay(records, answers, check_version, seen_before=""):
    """Applies the user's answers to the page texts and pins the pages they touched.

    Answers already applied in an earlier run (their id is in the page's pin) are left
    alone. The others are applied together at their offsets when the page text is the
    one they were made on, and otherwise one by one, found by their context.
    Returns {"results": {answer id: result}, "touched": [pages], "seen": latest answer
    time, "conflicts_new": conflicts among answers newer than seen_before}."""
    results, touched, seen, conflicts_new = {}, [], "", 0
    by_page = {}
    for aid, ans in (answers or {}).items():
        if not isinstance(ans, dict):
            continue
        seen = max(seen, str(ans.get("at", "")))
        try:
            by_page.setdefault(int(ans.get("page")), []).append((aid, ans))
        except (TypeError, ValueError):
            continue
    for rec in records:
        todo = by_page.get(rec["page"])
        if not todo or not rec.get("text"):
            continue
        pin = pin_of(rec) or {}
        done = set(pin.get("ids", []))
        spans = {k: list(v) for k, v in (pin.get("spans") or {}).items()}
        text = rec["text"]
        fresh = [(aid, ans) for aid, ans in todo if aid not in done]
        for aid, _ in todo:
            if aid in done:
                results[aid] = "already"
        # Answers made on this very text are applied at their offsets, last first, so the
        # earlier offsets stay right; the others are found by their context.
        same = base_of(text)
        fresh.sort(key=lambda x: (x[1].get("base") != same, -(x[1].get("pos") or 0)))
        for aid, ans in fresh:
            text, result, span = apply_answer(text, ans, trust_pos=ans.get("base") == same)
            results[aid] = result
            if result == "conflict":
                if str(ans.get("at", "")) > seen_before:
                    conflicts_new += 1
                continue
            done.add(aid)
            if result == "applied":
                delta = (span[1] - span[0]) - len(ans.get("core", ""))
                for k, (s, e) in spans.items():
                    if s >= span[0] + len(ans.get("core", "")):
                        spans[k] = [s + delta, e + delta]
            if span:
                spans[aid] = list(span)
        if done != set(pin.get("ids", [])) or text != rec["text"]:
            rec["text"] = text
            ats = [str(a.get("at", "")) for i, a in todo if i in done] + [pin.get("at", "")]
            rec["check"] = {**(rec.get("check") or {}), "v": check_version,
                            "human": {"ids": sorted(done), "spans": spans, "at": max(ats)}}
            touched.append(rec["page"])
    return {"results": results, "touched": touched, "seen": seen, "conflicts_new": conflicts_new}


# ---------------------------------------------------------------- the review


def answered(answers, results):
    """(ids, (page, fp) pairs) of places the user's answers settled."""
    ids, fps = set(), set()
    for aid, ans in (answers or {}).items():
        if not isinstance(ans, dict) or results.get(aid) not in ("kept", "applied", "already"):
            continue
        try:
            page = int(ans.get("page"))
        except (TypeError, ValueError):
            continue
        if ans.get("kind") == "rest":
            ids.update(ans.get("ids") or [])
            fps.update((page, fp) for fp in ans.get("fps") or [])
        else:
            ids.add(aid)
            if ans.get("fp"):
                fps.add((page, ans["fp"]))
    return ids, fps


def build(records, pages, answers=None, results=None, vocab=None, heads=None):
    """The places left for the user on each unsure page, in the order they are shown.

    pages: {page number: {"tsv", "w", "h", "gutter"}} for the unsure pages whose image
    was read by Tesseract. answers / results: the answers file and what overlay() did
    with each, so answered places are left out. A pinned page with nothing left becomes
    "human". Returns (review pages, pages that became "human")."""
    ids, fps = answered(answers, results or {})
    out, became_human = [], []
    for rec in sorted(records, key=lambda r: r["page"]):
        check = rec.get("check") or {}
        if check.get("state") != "unsure" or not rec.get("text"):
            continue
        page, text = rec["page"], rec["text"]
        data = pages.get(page) or {}
        lines = parse_tsv(data.get("tsv", ""))
        size = (data.get("w") or 1, data.get("h") or 1)
        gutter = data.get("gutter")
        seq = [(n, w) for n, line in enumerate(lines) for w in line["words"] if w["norm"]]
        hay = [w["norm"] for _, w in seq]
        items = []
        readings = {k: (rec.get(k) or {}).get("text", "") for k in ("a", "b")
                    if (rec.get(k) or {}).get("status") == "ok"}
        if "kept" in check and "model" not in check:
            other = readings.get("b" if check["kept"] == "a" else "a", "")
            if other:
                items += diff_items(page, text, other, hay)
        if lines:
            others = [t for t in readings.values() if t and t != text]
            page_heads = [h for p, h in (heads or {}).items() if p != page]
            diffs = [(it["pos"] + len(it["before"]), it["pos"] + len(it["before"]) + len(it["core"])) for it in items]
            # A line Tesseract misread inside a difference is that difference.
            items += [it for it in missing_items(page, text, lines, tsv_missing(lines, text, vocab or set()), others,
                                                 gutter, size, page_heads)
                      if it.get("hint") != "likely-ok" or not overlaps(it, diffs)]
        for it in items:
            it["options"] = [o for o in it.get("options", []) if norms(o["text"]) != norms(it["core"])
                             or not it["core"].strip()]
        human = pin_of(rec) or {}
        fixed = [v for k, v in (human.get("spans") or {}).items()]
        items = [it for it in items if it["id"] not in ids and (page, it["fp"]) not in fps
                 and not overlaps(it, fixed)]
        if not items:
            whole = make_item(page, "page", text, 0, len(text), "page", options=[], boxes=[], located=False)
            done = whole["id"] in ids or (page, "page") in fps
            # Without Tesseract's boxes, lines the check counted as missing cannot be shown.
            if human and (done or data.get("tsv") is not None or not lines_flagged(check)):
                rec["check"] = {**check, "state": "human"}
                became_human.append(page)
                continue
            if done:
                continue
            items = [whole]
        for it in items:
            if "boxes" not in it:
                it.update(locate(it, seq, hay, lines, gutter, size) or {"boxes": [], "located": False})
        # The biggest differences first when there are too many, and the "likely right" ones last.
        keep = sorted(items, key=lambda it: (it.get("hint") == "likely-ok", -weight(it)))[:MAX_ITEMS]
        keep.sort(key=lambda it: (it.get("hint") == "likely-ok", it["pos"]))
        for n, it in enumerate(keep, 1):
            it["n"] = n
        out.append({"page": page, "base": base_of(text), "w": size[0], "h": size[1], "more": len(items) - len(keep),
                    "items": keep})
    return out, became_human


def lines_flagged(check):
    """How many lines the check counted as missing from the page text."""
    if "model" in check:
        return check.get("missing_final", 0)
    return check.get("missing_" + check.get("kept", "a"), 0)


def weight(item):
    """How many words a place is about."""
    return max([len(norms(item["core"]))] + [len(norms(o["text"])) for o in item.get("options", [])])


def overlaps(item, spans):
    """True when the place covers text an applied correction wrote (it is the correction)."""
    start = item["pos"] + len(item["before"])
    end = start + len(item["core"])
    return any(s < max(end, start + 1) and e > start for s, e in spans)


# ---------------------------------------------------------------- output


def scale_of(width):
    return min(1.0, IMAGE_WIDTH / width) if width else 1.0


def render(png, items):
    """JPEG of the page with every place framed and numbered. The frames are thin lines with
    nothing over the words, so the reading inside stays as clear as the rest of the page."""
    import pymupdf
    pix = pymupdf.Pixmap(png)
    w, h = pix.width, pix.height
    s = scale_of(w)
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=w, height=h)
        page.insert_image(page.rect, stream=png)
        boxed = False
        for it in items:
            for box in it.get("boxes", []):
                # About one pixel wide in the JPEG, whatever the scan's size.
                page.draw_rect(pymupdf.Rect(box), color=(0.85, 0, 0), width=1.2 / s)
                boxed = True
            if it.get("boxes"):
                x0, y0, x1, _ = it["boxes"][0]
                x = x1 + 3 if x1 + 24 < w else max(0, x0 - 24)
                page.insert_text((x, max(16, y0 + 16)), str(it["n"]), fontsize=18, color=(0.85, 0, 0))
        out = page.get_pixmap(matrix=pymupdf.Matrix(s, s), colorspace=pymupdf.csRGB if boxed else pymupdf.csGRAY)
        return out.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
    finally:
        doc.close()


def image_name(page, png, items):
    boxes = json.dumps([it.get("boxes", []) for it in items])
    return f"p{page:04d}-{hashlib.sha1(png + boxes.encode()).hexdigest()[:10]}.jpg"


def scaled(entry):
    """The page entry as items.json has it: sizes and boxes in the JPEG's pixels."""
    s = scale_of(entry["w"])
    fix = lambda b: [round(v * s) for v in b]
    items = []
    for it in entry["items"]:
        it = dict(it)
        if it.get("boxes"):
            it["boxes"] = [fix(b) for b in it["boxes"]]
        if it.get("col"):
            it["col"] = fix(it["col"])
        items.append(it)
    return {**entry, "w": round(entry["w"] * s), "h": round(entry["h"] * s), "items": items}


def write(out_dir, review_pages, images, book, book_hash, records, seen, results):
    """items.json, the page images, human.json and review-applied.json in out_dir.
    images: {page number: PNG bytes} of the pages under review."""
    os.makedirs(out_dir, exist_ok=True)
    pages = []
    for entry in review_pages:
        png = images.get(entry["page"])
        if png:
            entry["img"] = image_name(entry["page"], png, entry["items"])
            path = os.path.join(out_dir, entry["img"])
            if not os.path.exists(path):
                with open(path, "wb") as f:
                    f.write(render(png, entry["items"]))
        else:
            entry["img"] = ""
            for it in entry["items"]:
                it.update(boxes=[], located=False)
        pages.append(scaled(entry))
    dump(os.path.join(out_dir, "items.json"),
         {"v": 1, "book": book, "book_hash": book_hash, "generated_at": now(), "answers_seen": seen,
          "pages": pages})
    pins = {}
    for rec in records:
        pin = pin_of(rec)
        if pin:
            pins[str(rec["page"])] = {"text": rec.get("text", ""), "state": (rec.get("check") or {}).get("state", ""),
                                      **pin}
    dump(os.path.join(out_dir, "human.json"),
         {"v": 1, "book_hash": book_hash, "answers_seen": seen, "pages": pins})
    dump(os.path.join(out_dir, "review-applied.json"), {"at": now(), "results": results})


def dump(path, data):
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def load(path):
    """A JSON file, or None when it is missing or broken."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
