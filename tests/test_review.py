"""Offline tests for review.py: places for the user to check, and applying their answers.

Run from the repository root:  python -m unittest discover -s tests
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import crosscheck  # noqa: E402
import review  # noqa: E402

# Six lines of a page, no two sharing a pair of words.
SENTENCES = [
    "كانت المدينة القديمة تقع على ضفاف النهر الكبير منذ قرون طويلة",
    "وقد بنى سكانها الأوائل أسواراً عالية تحميهم من غارات البدو",
    "ويذكر المؤرخون أن تجارتها ازدهرت حين مرت بها قوافل الحرير",
    "فاجتمع فيها العلماء والشعراء والأطباء من أقطار شتى",
    "وألفوا كتباً كثيرة في الفلك والطب والحساب والأدب",
    "ثم أصابها الوباء فهجرها أهلها وبقيت أطلالها شاهدة على مجدها",
]


def tsv(lines, height=1000):
    """Tesseract TSV for [(text, (x0, y0, x1, y1))], one block per line, words right to left."""
    rows = ["level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            f"1\t1\t0\t0\t0\t0\t0\t0\t1000\t{height}\t-1\t"]
    for n, (text, (x0, y0, x1, y1)) in enumerate(lines, 1):
        rows.append(f"4\t1\t{n}\t1\t1\t0\t{x0}\t{y0}\t{x1 - x0}\t{y1 - y0}\t-1\t")
        words = text.split()
        step = (x1 - x0) / len(words)
        for k, w in enumerate(words, 1):
            right = x1 - (k - 1) * step
            rows.append(f"5\t1\t{n}\t1\t1\t{k}\t{int(right - step + 3)}\t{y0}\t{int(step - 3)}\t{y1 - y0}\t90\t{w}")
    return "\n".join(rows)


def column_lines(texts, x0=520, x1=980, top=60, step=40):
    return [(t, (x0, top + k * step, x1, top + k * step + 30)) for k, t in enumerate(texts)]


def answer(item, verdict, text="", base=None, at="2026-10-20T10:00:00Z", page=None):
    out = {k: item[k] for k in ("kind", "fp", "before", "core", "after", "pos")}
    out.update(verdict=verdict, text=text, at=at, page=page or int(item["id"][1:].split("-")[0]))
    if "place" in item:
        out["place"] = item["place"]
    if base:
        out["base"] = base
    return out


def unsure(page, text, other="", kept="a", **check):
    rec = {"page": page, "status": "ok", "text": text, "a": {"status": "ok", "text": text},
           "check": {"state": "unsure", **check}}
    if other:
        rec["b"] = {"status": "ok", "text": other}
        rec["check"].setdefault("kept", kept)
    return rec


class CompareSpans(unittest.TestCase):
    PAIRS = [
        ("قد برهن برهنة كافية على وجود", "قد برهن كافية على وجود"),
        ("أول الكلام هنا ثم آخره", "كلام زائد أول الكلام هنا ثم آخره وزيادة"),
        ("فى الكتاب الذى قرأه إبراهيم ، وقال", "في الكتاب الذي قرأه ابراهيم وقال كلاماً"),
        ("ا ب ج د هـ و ز ح ط ي", "ا ب س د هـ و ص ح ط ي ك"),
    ]

    def test_same_differences_as_compare(self):
        for a, b in self.PAIRS:
            spans = crosscheck.compare_spans(a, b, gap=0)
            self.assertEqual([(s["final"], s["other"]) for s in spans], crosscheck.compare(a, b)[1])
            for s in spans:
                if s["final"]:
                    self.assertEqual(a[s["start"]:s["end"]], s["final"])
                if s["other"]:
                    self.assertEqual(b[s["other_start"]:s["other_end"]], s["other"])

    def test_insertions_sit_after_the_previous_word(self):
        a, b = "أول الكلام هنا", "زائد أول الكلام وسط هنا"
        spans = crosscheck.compare_spans(a, b, gap=0)
        self.assertEqual([(s["start"], s["end"]) for s in spans], [(0, 0), (len("أول الكلام"),) * 2])

    def test_close_differences_merge(self):
        a = "ا ب ج د هـ و ز ح ط ي ك ل"
        b = "ا س ج د ص و ز ح ط ق ك ل"
        spans = crosscheck.compare_spans(a, b)
        # ب/س and هـ/ص have two equal words between them; ي/ق is three words further.
        self.assertEqual([s["final"] for s in spans], ["ب ج د هـ", "ي"])
        self.assertEqual(spans[0]["other"], "س ج د ص")


class Places(unittest.TestCase):
    def test_context_grows_until_unique(self):
        text = "قال الشيخ في الكتاب كلاماً ثم قال الشيخ في الكتاب شيئاً آخر"
        start = text.index("كلاماً")
        before, core, after, pos = review.context(text, start, start + len("كلاماً"))
        self.assertEqual(core, "كلاماً")
        self.assertEqual(text.count(before + core + after), 1)
        self.assertEqual(text[pos:pos + len(before + core + after)], before + core + after)
        self.assertEqual(before, "الشيخ في الكتاب ")
        # The same word twice: the context grows until it is told apart.
        text = "ا ب ج د ه و ا ب ج د ز"
        start = text.index("د")
        before, core, after, _ = review.context(text, start, start + 1)
        self.assertEqual(text.count(before + core + after), 1)

    def test_diff_places(self):
        text = " ".join(SENTENCES)
        other = text.replace("النهر الكبير", "النهر الكببر").replace("قوافل الحرير", "قوافل الحريز")
        items = review.diff_items(7, text, other, [])
        self.assertEqual([(i["core"], i["options"][0]["text"]) for i in items],
                         [("الكبير", "الكببر"), ("الحرير", "الحريز")])
        for i in items:
            self.assertEqual(text[i["pos"]:].index(i["before"] + i["core"] + i["after"]), 0)
            self.assertTrue(i["id"].startswith("p7-"))
        self.assertEqual(items[0]["fp"], "diff|الكبير|الكببر")

    def test_moved_passages_and_note_numbers_are_not_differences(self):
        a, b = SENTENCES[:3], SENTENCES[3:]
        text = " ".join(a + b) + "\n\n[^1]: حاشية\n[^2]: أخرى"
        other = " ".join(b + a) + "\n\n[^2]: حاشية\n[^3]: أخرى"
        self.assertEqual(review.diff_items(1, text, other, []), [])

    def test_words_the_other_reading_dropped(self):
        text = " ".join(SENTENCES)
        other = text.replace(SENTENCES[2] + " ", "")
        tess = review.norms(text)
        items = review.diff_items(1, text, other, tess)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["core"], SENTENCES[2])
        self.assertEqual(items[0]["hint"], "likely-ok")
        # Tesseract did not see them: no hint.
        self.assertNotIn("hint", review.diff_items(1, text, other, review.norms(other))[0])

    def test_words_only_the_other_reading_has(self):
        text = SENTENCES[0]
        other = text.replace("النهر الكبير", "النهر العظيم الكبير")
        item = review.diff_items(1, text, other, [])[0]
        self.assertEqual(item["core"], " ")
        self.assertEqual(item["options"][0]["text"], "العظيم")
        new, result, span = review.apply_answer(text, answer(item, "fix", "العظيم"), trust_pos=True)
        self.assertEqual((new, result), (other, "applied"))
        self.assertEqual(new[span[0]:span[1]].strip(), "العظيم")


class Tesseract(unittest.TestCase):
    def test_tsv_lines(self):
        lines = review.parse_tsv(tsv([("كلمة أولى", (100, 10, 500, 40)), ("ينظمها ‏ أو", (100, 50, 500, 80))]))
        self.assertEqual([l["text"] for l in lines], ["كلمة أولى", "ينظمها ‏ أو"])
        self.assertEqual(review.line_norms(lines[1]), ["ينظمها", "او"])
        self.assertEqual(lines[0]["box"], [100, 10, 500, 40])
        self.assertEqual(review.parse_tsv(""), [])

    def test_columns_from_the_gutter(self):
        self.assertEqual(review.column_of([600, 0, 950, 30], 500), 0)
        self.assertEqual(review.column_of([50, 0, 450, 30], 500), 1)
        self.assertEqual(review.column_of([50, 0, 450, 30], None), 0)


class Missing(unittest.TestCase):
    def setUp(self):
        self.lines = review.parse_tsv(tsv(column_lines(SENTENCES)))
        self.vocab = set(crosscheck.norm_words(" ".join(SENTENCES)))

    def items(self, text, heads=None, others=()):
        miss = review.tsv_missing(self.lines, text, self.vocab)
        return review.missing_items(3, text, self.lines, miss, list(others), 500, (1000, 1000), heads or [])

    def test_line_dropped_in_the_middle(self):
        text = "\n\n".join([" ".join(SENTENCES[:3]), " ".join(SENTENCES[4:])])
        items = self.items(text, others=[" ".join(SENTENCES)])
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual((item["kind"], item["place"], item["core"]), ("missing", "after", "\n\n"))
        self.assertTrue(item["before"].endswith("قوافل الحرير"))
        self.assertEqual(item["options"][0], {"label": "other", "text": SENTENCES[3]})
        self.assertEqual(item["options"][1]["label"], "ocr")
        self.assertEqual(item["boxes"], [[516, 176, 984, 214]])
        new, result, _ = review.apply_answer(text, answer(item, "fix", SENTENCES[3]), trust_pos=True)
        self.assertEqual(result, "applied")
        self.assertIn("قوافل الحرير " + SENTENCES[3] + "\n\n" + SENTENCES[4], new)

    def test_lines_missing_one_after_another_share_a_place(self):
        text = " ".join(SENTENCES[:2] + SENTENCES[4:])
        items = self.items(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(items[0]["boxes"]), 2)
        self.assertEqual(items[0]["options"][-1]["text"], SENTENCES[2] + " " + SENTENCES[3])
        new, _, _ = review.apply_answer(text, answer(items[0], "fix", items[0]["options"][-1]["text"]))
        self.assertEqual(new, " ".join(SENTENCES))

    def test_first_and_last_lines(self):
        text = " ".join(SENTENCES[1:])
        item = self.items(text)[0]
        self.assertEqual((item["place"], item["core"], item["before"]), ("start", "", ""))
        new, _, _ = review.apply_answer(text, answer(item, "fix", SENTENCES[0]))
        self.assertEqual(new, SENTENCES[0] + "\n" + text)

        text = " ".join(SENTENCES[:-1]) + "\n\n[^1]: حاشية"
        item = self.items(text)[0]
        self.assertEqual(item["place"], "after")
        new, _, _ = review.apply_answer(text, answer(item, "fix", SENTENCES[-1]))
        self.assertEqual(new, " ".join(SENTENCES[:-1]) + "\n" + SENTENCES[-1] + "\n\n[^1]: حاشية")

    def test_line_after_a_heading_and_as_a_heading(self):
        text = "## عنوان القسم\n\n" + " ".join(SENTENCES[1:])
        lines = review.parse_tsv(tsv(column_lines(["عنوان القسم"] + SENTENCES)))
        miss = review.tsv_missing(lines, text, self.vocab)
        item = review.missing_items(3, text, lines, miss, [], 500, (1000, 1000), [])[0]
        new, _, _ = review.apply_answer(text, answer(item, "fix", SENTENCES[0]))
        self.assertEqual(new, "## عنوان القسم\n\n" + SENTENCES[0] + " " + " ".join(SENTENCES[1:]))
        new, _, _ = review.apply_answer(text, answer(item, "fix", "## " + SENTENCES[0]))
        self.assertIn("## عنوان القسم\n\n## " + SENTENCES[0] + "\n\n", new)

    def test_line_tesseract_misread_is_shown_as_read(self):
        garbled = "فاجتمع فبها العلماء والشعرا والأطباء مں أقطاز شتى"
        lines = review.parse_tsv(tsv(column_lines(SENTENCES[:3] + [garbled] + SENTENCES[4:])))
        text = " ".join(SENTENCES)
        # A line the text has, only misread by Tesseract, is not missing (2026-10-25).
        miss = review.tsv_missing(lines, text, self.vocab | set(crosscheck.norm_words(garbled)))
        self.assertEqual(miss, [])
        item = review.missing_items(3, text, lines, [3], [], 500, (1000, 1000), [])[0]
        self.assertEqual((item["core"], item["hint"]), (SENTENCES[3], "likely-ok"))

    def test_running_header_and_heading_hints(self):
        lines = [("تراث الإنسانية الجزء الثالث المجلد", (520, 20, 980, 50))] + column_lines(SENTENCES[1:], top=100)
        lines = review.parse_tsv(tsv(lines))
        text = " ".join(SENTENCES[1:])
        vocab = self.vocab | set(crosscheck.norm_words(lines[0]["text"]))
        heads = [[review.line_norms(lines[0])], [review.line_norms(lines[0])]]
        item = review.missing_items(3, text, lines, [0], [], 500, (1000, 1000), heads)[0]
        self.assertEqual(item["hint"], "header")
        self.assertEqual(review.tsv_missing(lines, text, vocab), [0])
        tall = [("عنوان كبير جداً في رأس المقال الجديد", (520, 100, 980, 160))] + column_lines(SENTENCES, top=200)
        lines = review.parse_tsv(tsv(tall))
        item = review.missing_items(3, " ".join(SENTENCES), lines, [0], [], 500, (1000, 1000), [])[0]
        self.assertEqual(item["hint"], "heading")


class Locate(unittest.TestCase):
    def test_box_on_the_differing_word(self):
        text = " ".join(SENTENCES)
        other = text.replace("النهر الكبير", "النهر الكببر")
        lines = review.parse_tsv(tsv(column_lines(SENTENCES)))
        pages, _ = review.build([unsure(5, text, other)], {5: {"tsv": tsv(column_lines(SENTENCES)), "w": 1000,
                                                                 "h": 1000, "gutter": None}})
        item = pages[0]["items"][0]
        self.assertTrue(item["located"])
        self.assertEqual(len(item["boxes"]), 1)
        word = [w for w in lines[0]["words"] if w["text"] == "الكبير"][0]["box"]
        box = item["boxes"][0]
        self.assertTrue(box[0] <= word[0] and box[2] >= word[2] and box[1] <= word[1])
        self.assertEqual(item["col"][0] >= 500, True)

    def test_unsure_when_found_twice_or_not_at_all(self):
        twice = SENTENCES[:2] + SENTENCES[:2]
        seq_lines = review.parse_tsv(tsv(column_lines(twice)))
        seq = [(n, w) for n, line in enumerate(seq_lines) for w in line["words"] if w["norm"]]
        hay = [w["norm"] for _, w in seq]
        text = " ".join(SENTENCES)
        start = text.index("الكبير")
        item = review.make_item(1, "diff", text, start, start + len("الكبير"), "x")
        self.assertIsNone(review.locate(item, seq, hay, seq_lines, None, (1000, 1000)))
        other_lines = review.parse_tsv(tsv(column_lines(["كلام لا علاقة له بالصفحة أبداً هنا"] * 3)))
        seq = [(n, w) for n, line in enumerate(other_lines) for w in line["words"] if w["norm"]]
        self.assertIsNone(review.locate(item, seq, [w["norm"] for _, w in seq], other_lines, None, (1000, 1000)))


class Answers(unittest.TestCase):
    TEXT = "قال الشيخ في الكتاب كلاماً حسناً ثم سكت طويلاً وقال الشيخ في الكتاب شيئاً آخر"

    def item(self, word, text=None):
        text = text or self.TEXT
        start = text.index(word)
        return review.make_item(2, "diff", text, start, start + len(word), "diff|" + word)

    def test_kept_applied_already_conflict(self):
        item = self.item("حسناً")
        self.assertEqual(review.apply_answer(self.TEXT, answer(item, "keep"))[1], "kept")
        new, result, span = review.apply_answer(self.TEXT, answer(item, "fix", "جميلاً"))
        self.assertEqual(result, "applied")
        self.assertEqual(new, self.TEXT.replace("حسناً", "جميلاً"))
        self.assertEqual(new[span[0]:span[1]], "جميلاً")
        self.assertEqual(review.apply_answer(new, answer(item, "fix", "جميلاً"))[:2], (new, "already"))
        changed = self.TEXT.replace("كلاماً حسناً ثم", "قولاً")
        self.assertEqual(review.apply_answer(changed, answer(item, "fix", "جميلاً"))[:2], (changed, "conflict"))
        self.assertEqual(review.apply_answer(self.TEXT, answer(item, "fix", "حسناً"))[1], "kept")

    def test_offset_used_only_on_the_same_text(self):
        text = "ا ب ج د ه و ز ا ب ج د ه و ز"
        item = review.make_item(2, "diff", text, 6, 7, "x")
        item.update(before="ب ج ", after=" ه و")  # a context that is no longer unique
        new, result, _ = review.apply_answer(text, answer(item, "fix", "س"), trust_pos=False)
        self.assertEqual(result, "conflict")
        item["pos"] = 2
        new, result, _ = review.apply_answer(text, answer(item, "fix", "س"), trust_pos=True)
        self.assertEqual((new, result), ("ا ب ج س ه و ز ا ب ج د ه و ز", "applied"))

    def test_answer_text_is_cleaned(self):
        item = self.item("حسناً")
        new, _, _ = review.apply_answer(self.TEXT, answer(item, "fix", "جميل\r\nجداً‏\x07" + "ب" * 3000))
        self.assertIn("جميل\nجداً" + "ب" * (review.MAX_ANSWER - 10), new)
        self.assertNotIn("‏", new)
        self.assertNotIn("\x07", new)
        self.assertNotIn("ب" * (review.MAX_ANSWER - 8), new)

    def test_footnote_marks_survive_a_correction(self):
        text = "قال الشيخ في الكتاب[^1] كلاماً حسناً ثم سكت"
        item = review.make_item(2, "diff", text, text.index("الكتاب"), text.index(" كلاماً"), "x")
        new, _, _ = review.apply_answer(text, answer(item, "fix", "الكتب"))
        self.assertEqual(new, "قال الشيخ في الكتب [^1] كلاماً حسناً ثم سكت")

    def test_rest_and_page(self):
        rest = {"kind": "rest", "verdict": "keep", "ids": ["p2-a"], "page": 2}
        self.assertEqual(review.apply_answer(self.TEXT, rest)[1], "kept")
        whole = review.make_item(2, "page", self.TEXT, 0, len(self.TEXT), "page")
        new, result, _ = review.apply_answer(self.TEXT, answer(whole, "fix", "نص جديد كله"), trust_pos=True)
        self.assertEqual((new, result), ("نص جديد كله", "applied"))


class Overlay(unittest.TestCase):
    def test_neighbouring_answers_and_later_runs(self):
        text = " ".join(SENTENCES)
        # Four words apart: not one place, but their contexts overlap.
        other = text.replace("الكبير منذ", "الكبير جدا منذ").replace("وقد بنى", "وقد بنت")
        other = other.replace("تحميهم", "تحمهم")
        rec = unsure(4, text, other)
        pages, _ = review.build([rec], {})
        items = pages[0]["items"]
        self.assertEqual(len(items), 3)
        base = pages[0]["base"]
        # Places a few words apart: their contexts overlap.
        self.assertEqual([i["core"] for i in items], [" ", "بنى", "تحميهم"])
        answers = {items[0]["id"]: answer(items[0], "fix", "جدا", base=base),
                   items[1]["id"]: answer(items[1], "keep", base=base),
                   items[2]["id"]: answer(items[2], "fix", "تحرسهم", base=base, at="2026-10-21T00:00:00Z")}
        out = review.overlay([rec], answers, 2)
        self.assertEqual(out["results"], {items[0]["id"]: "applied", items[1]["id"]: "kept",
                                          items[2]["id"]: "applied"})
        self.assertEqual(out["touched"], [4])
        self.assertEqual(out["seen"], "2026-10-21T00:00:00Z")
        self.assertIn("الكبير جدا منذ قرون طويلة وقد بنى سكانها الأوائل أسواراً عالية تحرسهم من", rec["text"])
        human = rec["check"]["human"]
        self.assertEqual(sorted(human["ids"]), sorted(answers))
        self.assertEqual(rec["text"][slice(*human["spans"][items[2]["id"]])], "تحرسهم")
        self.assertEqual(rec["text"][slice(*human["spans"][items[0]["id"]])].strip(), "جدا")
        # Nothing left: the page is the user's now.
        pages, became = review.build([rec], {4: {"tsv": "", "w": 1000, "h": 1000}}, answers, out["results"])
        self.assertEqual((pages, became), ([], [4]))
        self.assertEqual(rec["check"]["state"], "human")
        # The next run leaves the answers alone.
        again = review.overlay([rec], answers, 2)
        self.assertEqual(set(again["results"].values()), {"already"})
        self.assertEqual(again["touched"], [])

    def test_conflicts_and_their_count(self):
        text = " ".join(SENTENCES)
        rec = unsure(4, text, text.replace("الكبير", "الكببر"))
        item = review.build([rec], {})[0][0]["items"][0]
        rec["text"] = text.replace("النهر الكبير منذ", "شيء آخر")
        answers = {item["id"]: answer(item, "fix", "العظيم", at="2026-10-20T10:00:00Z")}
        self.assertEqual(review.overlay([rec], answers, 2, seen_before="2026-10-19T00:00:00Z")["conflicts_new"], 1)
        self.assertEqual(review.overlay([rec], answers, 2, seen_before="2026-10-20T10:00:00Z")["conflicts_new"], 0)
        self.assertNotIn("human", rec["check"])

    def test_answered_places_stay_hidden(self):
        text = " ".join(SENTENCES)
        rec = unsure(4, text, text.replace("الكبير", "الكببر").replace("الحرير", "الحريز"))
        items = review.build([rec], {})[0][0]["items"]
        # Corrected to a third word: the new difference with the other reading is the correction itself.
        answers = {items[0]["id"]: answer(items[0], "fix", "العظيم", base=review.base_of(text))}
        out = review.overlay([rec], answers, 2)
        pages, became = review.build([rec], {}, answers, out["results"])
        self.assertEqual([i["core"] for i in pages[0]["items"]], ["الحرير"])
        # "The rest of this page is right" covers the places it lists.
        rest = {"p4-rest-1": {"kind": "rest", "verdict": "keep", "page": 4, "ids": [pages[0]["items"][0]["id"]],
                              "fps": [], "at": "2026-10-21T00:00:00Z"}}
        answers.update(rest)
        out = review.overlay([rec], answers, 2)
        pages, became = review.build([rec], {4: {"tsv": ""}}, answers, out["results"])
        self.assertEqual((pages, became), ([], [4]))

    def test_pins(self):
        rec = review.pin_record(None, 9, {"text": "نص المستخدم", "ids": ["p9-x"], "at": "t1", "state": "human"}, 2)
        self.assertEqual((rec["text"], rec["check"]["state"], rec["a"]["model"]), ("نص المستخدم", "human", "human"))
        newer = {"page": 9, "status": "ok", "text": "أحدث", "check": {"state": "unsure", "human": {"at": "t2"}}}
        self.assertEqual(review.pin_record(newer, 9, {"text": "أقدم", "at": "t1"}, 2)["text"], "أحدث")

    def test_too_many_places_and_page_without_places(self):
        words = [f"كلمة{n}" for n in range(200)]
        text = " ".join(words)
        other = " ".join("x" + w if n % 4 == 0 else w for n, w in enumerate(words))
        pages, _ = review.build([unsure(1, text, other)], {})
        self.assertEqual(len(pages[0]["items"]), review.MAX_ITEMS)
        self.assertEqual(pages[0]["more"], 50 - review.MAX_ITEMS)
        self.assertEqual([i["n"] for i in pages[0]["items"]], list(range(1, 26)))
        pages, _ = review.build([unsure(2, text, model="judge", missing_final=1)], {})
        self.assertEqual([i["kind"] for i in pages[0]["items"]], ["page"])
        self.assertEqual(pages[0]["items"][0]["core"], text)


class Images(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def png(self, width):
        import pymupdf
        doc = pymupdf.open()
        page = doc.new_page(width=width, height=width * 1.25)
        page.draw_rect(pymupdf.Rect(10, 10, width - 10, 40), fill=(0, 0, 0))
        data = page.get_pixmap(colorspace=pymupdf.csGRAY).tobytes("png")
        doc.close()
        return data

    def test_page_images(self):
        import pymupdf
        text = " ".join(SENTENCES)
        rec = unsure(5, text, text.replace("الكبير", "الكببر"))
        data = {"tsv": tsv(column_lines(SENTENCES, x0=1100, x1=1900)), "w": 2000, "h": 2500, "gutter": None}
        pages, _ = review.build([rec], {5: data})
        png = self.png(2000)
        review.write(self.tmp, pages, {5: png}, "كتاب", "abc", [rec], "", {})
        with open(os.path.join(self.tmp, "items.json"), encoding="utf-8") as f:
            items = json.load(f)
        page = items["pages"][0]
        self.assertEqual((items["book"], items["book_hash"], page["w"], page["h"]), ("كتاب", "abc", 1200, 1500))
        self.assertRegex(page["img"], r"^p0005-[0-9a-f]{10}\.jpg$")
        jpeg = pymupdf.Pixmap(os.path.join(self.tmp, page["img"]))
        self.assertEqual((jpeg.width, jpeg.n), (1200, 3))
        self.assertTrue(all(v <= 1200 for b in page["items"][0]["boxes"] for v in b))
        # Same page and places: same name.
        self.assertEqual(review.image_name(5, png, pages[0]["items"]), page["img"])
        # Nothing is laid over the words: inside a frame the page stays as it was (white here).
        x0, y0, x1, y1 = page["items"][0]["boxes"][0]
        inside = jpeg.pixel((x0 + x1) // 2, (y0 + y1) // 2)
        self.assertTrue(all(v >= 245 for v in inside), inside)
        red = [jpeg.pixel(x, (y0 + y1) // 2) for x in range(x0 - 1, x0 + 2)]
        self.assertTrue(any(r > 150 and g < 120 and b < 120 for r, g, b in red), red)
        # Without the page image there are no boxes to show.
        pages, _ = review.build([rec], {5: data})
        review.write(self.tmp, pages, {}, "كتاب", "abc", [rec], "", {})
        with open(os.path.join(self.tmp, "items.json"), encoding="utf-8") as f:
            page = json.load(f)["pages"][0]
        self.assertEqual((page["img"], page["items"][0]["boxes"], page["items"][0]["located"]), ("", [], False))


if __name__ == "__main__":
    unittest.main()
