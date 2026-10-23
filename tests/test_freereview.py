"""Offline tests for the free models' review of unsure places (freereview.py): a fake Drive
folder stands in for the book's review folder, and the fake Gemini client of test_converter
for the API.

Run from the repository root:  python -m unittest discover -s tests
"""
import json
import os
import shutil
import sys
import tempfile
import types as pytypes
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pymupdf  # noqa: E402

import converter as c  # noqa: E402
import freereview as fr  # noqa: E402
import helperbox  # noqa: E402
from test_converter import FakeModels, answer  # noqa: E402
from test_helperbox import FOLDER, FakeDrive  # noqa: E402


def item(n, core, kind="diff", other=""):
    return {"id": f"p30-{n:010d}", "n": n, "kind": kind, "fp": f"{kind}|{core}|", "before": "قبل ", "core": core,
            "after": " بعد", "pos": n * 10, "options": [{"label": "other", "text": other}]}


class Rules(unittest.TestCase):
    """The same rules as the helper's (Item-Needs, Item-Answer, Is-Risky)."""

    def test_words_are_compared_as_the_helper_compares_them(self):
        self.assertEqual(fr.norm_words("إِنَّ الكتابَ ١٢ — مكتبةٌ"), ["ان", "الكتاب", "12", "مكتبه"])
        self.assertEqual(fr.common("a b c d".split(), "a c d e".split()), 3)

    def test_a_line_or_more_than_six_words_is_risky(self):
        it = item(1, "كلمة واحدة")
        self.assertFalse(fr.risky(it, {"verdict": "keep"}))
        self.assertFalse(fr.risky(it, {"verdict": "fix", "text": "كلمة أخرى"}))
        self.assertTrue(fr.risky(it, {"verdict": "fix", "text": "واحد اثنان ثلاثة أربعة خمسة ستة سبعة"}))
        self.assertTrue(fr.risky(item(2, ""), {"verdict": "fix", "text": "سطر"}))

    def test_who_judges_and_what_becomes_an_answer(self):
        it, line = item(1, "كلمة"), item(2, "", kind="line")
        a, b = "free|gemini-3.8-flash", "agy|gemini-3.8-flash"
        self.assertTrue(fr.item_needs(it, [], a))
        keep = [{"chip": b, "verdict": "keep", "text": ""}]
        self.assertFalse(fr.item_needs(it, keep, a), "one sure verdict on a small place is enough")
        self.assertEqual(fr.item_answer(it, keep), {"verdict": "keep", "text": "", "by": [b]})
        insert = [{"chip": b, "verdict": "fix", "text": "سطر ساقط كامل"}]
        self.assertTrue(fr.item_needs(line, insert, a), "a line needs a second model")
        self.assertIsNone(fr.item_answer(line, insert))
        both = insert + [{"chip": a, "verdict": "fix", "text": "سطرٌ ساقط كامل"}]
        self.assertEqual(fr.item_answer(line, both)["by"], [b, a])
        apart = [{"chip": b, "verdict": "keep", "text": ""}, {"chip": a, "verdict": "fix", "text": "غيرها"}]
        self.assertIsNone(fr.item_answer(it, apart))
        self.assertFalse(fr.item_needs(it, apart, "free|gemma-4-31b-it"), "models disagree: the user decides")

    def test_the_answer_is_read_from_the_json_in_the_reply(self):
        its = [item(1, "كلمة"), item(2, "", kind="line"), item(3, "ثالثة")]
        got = fr.parse_verdicts('هذا جوابي: [{"n": 1, "verdict": "fix", "text": "كلمه"}, {"n": 2, "verdict": "fix"},'
                                ' {"n": 3, "verdict": "maybe"}]', its)
        self.assertEqual(got[its[0]["id"]], {"verdict": "fix", "text": "كلمه"})
        self.assertEqual(got[its[1]["id"]]["verdict"], "unsure", "nothing to insert is no answer")
        self.assertEqual(got[its[2]["id"]]["verdict"], "unsure")
        self.assertIsNone(fr.parse_verdicts("لا أستطيع", its))


class Review(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        os.environ["GEMINI_API_KEY"] = "test"
        os.environ["DRIVE_TOKEN"] = "token"
        self.addCleanup(os.environ.pop, "DRIVE_TOKEN", None)
        self.drive = FakeDrive()
        self._drive, helperbox.Drive = helperbox.Drive, lambda token: self.drive
        self.addCleanup(setattr, helperbox, "Drive", self._drive)
        self._sleep, c.time.sleep = c.time.sleep, lambda s: None
        self.addCleanup(setattr, c.time, "sleep", self._sleep)
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20), False)
        pix.set_rect(pix.irect, (255, 255, 255))
        for n in (30, 31):
            pix.save(os.path.join(self.tmp, f"p{n:04d}-0123456789.jpg"))
        self.pages = [{"page": 30, "base": "b30", "img": "p0030-0123456789.jpg", "items": [item(1, "كاقية"), item(2, "", kind="line")]},
                      {"page": 31, "base": "b31", "img": "p0031-0123456789.jpg",
                       "items": [dict(item(3, "الاريوباجوس"), id="p31-0000000003")]}]
        with open(os.path.join(self.tmp, "items.json"), "w", encoding="utf-8") as f:
            json.dump({"v": 1, "pages": self.pages}, f, ensure_ascii=False)
        self.judge = os.path.join(self.tmp, "judge.json")

    def run_review(self, behaviour, boxes):
        fake = FakeModels(behaviour)
        c.genai.Client = lambda **kw: pytypes.SimpleNamespace(models=fake)
        summary = os.path.join(self.tmp, "summary.json")
        fr.main(["--items", os.path.join(self.tmp, "items.json"), "--folder", FOLDER, "--boxes", boxes,
                 "--judge", self.judge, "--summary", summary])
        with open(summary, encoding="utf-8") as f:
            return json.load(f), fake

    def test_the_free_models_judge_share_the_pages_and_their_answers_join_the_saved_ones(self):
        with open(self.judge, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "answers": {"old": {"verdict": "keep"}}}, f)
        # The user's own model already judged the line on page 30 (a line needs two).
        self.drive.put("j-agy_gemini-3.8-flash-p0030.json", {"chip": "agy|gemini-3.8-flash", "page": 30, "verdicts": {
            "p30-0000000002": {"verdict": "fix", "text": "سطر ساقط كامل هنا"}}})
        self.drive.put("answered.json", {"ids": ["p31-0000000003"]})

        def judge(p, n):
            if "الموضع 2" in p:
                return answer('[{"n": 1, "verdict": "fix", "text": "كافية"}, {"n": 2, "verdict": "fix", "text": "سطرٌ ساقط كامل هنا"}]')
            return answer('[{"n": 1, "verdict": "fix", "text": "كافية"}]')
        st, fake = self.run_review({"gemini-3.8-flash": judge, "gemma-4-31b-it": judge},
                                   "review=gemini-3.8-flash,gemma-4-31b-it;out=")
        with open(self.judge, encoding="utf-8") as f:
            answers = json.load(f)["answers"]
        self.assertIn("old", answers)
        self.assertEqual((answers["p30-0000000001"]["verdict"], answers["p30-0000000001"]["text"]), ("fix", "كافية"))
        self.assertEqual(answers["p30-0000000002"]["by"][0], "agy|gemini-3.8-flash")
        self.assertNotIn("p31-0000000003", answers, "the user answered it")
        self.assertEqual(st["asked"], 1, "one model judged page 30; page 31 is the user's")
        saved = [n for n in self.drive.files if n.startswith("j-free_")]
        self.assertEqual(len(saved), 1, saved)
        self.assertEqual(st["left"], 0)

    def test_a_model_out_of_quota_is_not_asked_and_its_places_are_left(self):
        never = lambda p, n: self.fail("out today")
        st, fake = self.run_review({"gemini-3.8-flash": never}, "review=gemini-3.8-flash;out=gemini-3.8-flash")
        self.assertEqual((st["asked"], st["answers"]), (0, 0))
        self.assertFalse(os.path.exists(self.judge))
        self.assertEqual(st["models_out"], ["gemini-3.8-flash"])

    def test_nothing_to_do_without_a_model(self):
        st, fake = self.run_review({}, "review=")
        self.assertEqual(st["asked"], 0)


if __name__ == "__main__":
    unittest.main()
