"""Offline tests for the reading pipeline: a fake Gemini client stands in for the API.

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

import converter as c  # noqa: E402
import crosscheck  # noqa: E402

PDF = os.path.join(ROOT, "تراث الانسانية 3-1-246.pdf")

TRUE = "وجود قوانين خاصة بالتطور ما دام يرى أنه قد برهن برهنة كافية على وجود قوانين خاصة بالاستقرار الاجتماعى " * 3
WRONG = TRUE.replace("برهنة كافية", "برهنة كاقية", 1)


def answer(text, finish="STOP"):
    part = pytypes.SimpleNamespace(text=text, thought=None)
    thought = pytypes.SimpleNamespace(text="(thinking)", thought=True)
    cand = pytypes.SimpleNamespace(finish_reason=finish, content=pytypes.SimpleNamespace(parts=[thought, part]))
    return pytypes.SimpleNamespace(candidates=[cand], text=text)


class FakeModels:
    """Scripted models. behaviour[model] is a function (prompt, call_no) -> response or exception."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def generate_content(self, model, contents, config):
        prompt = contents[-1]
        self.calls.append((model, "reconcile" if "النسخة (أ)" in prompt else "read"))
        n = sum(1 for m, _ in self.calls if m == model)
        return self.behaviour[model](prompt, n)


class ResolveModels(unittest.TestCase):
    def test_renamed_models_are_found(self):
        listed = [pytypes.SimpleNamespace(name="models/" + n, supported_actions=["generateContent"])
                  for n in ("gemini-3.8-flash", "gemini-3-flash-preview", "gemini-3-flash-lite-preview",
                            "gemini-2.5-flash", "gemini-2.5-flash-image")]
        pool = pytypes.SimpleNamespace(client=pytypes.SimpleNamespace(
            models=pytypes.SimpleNamespace(list=lambda: listed)))
        got = c.resolve_models(pool, ["gemini-3.8-flash", "gemini-3-flash", "gemini-2.5-flash", "gemini-9-flash"])
        self.assertEqual(got, ["gemini-3.8-flash", "gemini-3-flash-preview", "gemini-2.5-flash", "gemini-9-flash"])

    def test_listing_failure_keeps_names(self):
        pool = pytypes.SimpleNamespace(client=pytypes.SimpleNamespace(models=object()))
        self.assertEqual(c.resolve_models(pool, ["a", "b"]), ["a", "b"])


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        os.environ["GEMINI_API_KEY"] = "test"
        self._which = c.shutil.which
        c.shutil.which = lambda name: None  # no Tesseract: keeps the tests fast and deterministic
        self._sleep = c.time.sleep
        c.time.sleep = lambda s: None
        self.addCleanup(self.restore)

    def restore(self):
        c.shutil.which = self._which
        c.time.sleep = self._sleep

    def run_book(self, behaviour, pages="30-31", extra=()):
        fake = FakeModels(behaviour)
        c.genai.Client = lambda **kw: pytypes.SimpleNamespace(models=fake)
        out = os.path.join(self.tmp, "book.epub")
        status = os.path.join(self.tmp, "status.json")
        sys.argv = ["converter.py", PDF, "-o", out, "--pages", pages, "--work-dir", os.path.join(self.tmp, "work"),
                    "--report", os.path.join(self.tmp, "report.md"), "--status", status, "--rpm", "6000",
                    "--keep-markdown", *extra]
        with self.assertRaises(SystemExit) as exit_:
            c.main()
        with open(status, encoding="utf-8") as f:
            st = json.load(f)
        with open(out + ".md", encoding="utf-8") as f:
            md = f.read()
        return exit_.exception.code, st, md, fake

    def records(self):
        work = os.path.join(self.tmp, "work")
        d = os.path.join(work, os.listdir(work)[0])
        recs = {}
        for name in os.listdir(d):
            if name.endswith(".json") and name[:4].isdigit():
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    r = json.load(f)
                recs[r["page"]] = r
        return recs

    def test_agreement_needs_no_third_request(self):
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE.replace("الاجتماعى", "الاجتماعي")),
        })
        self.assertEqual(code, 0)
        self.assertEqual(st["state"], "done")
        self.assertEqual({r["check"]["state"] for r in self.records().values()}, {"agreed"})
        self.assertNotIn("reconcile", [k for _, k in fake.calls])
        self.assertNotIn("(thinking)", md)

    def test_disagreement_is_settled_against_the_image(self):
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE if "النسخة (أ)" in p else WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE if "النسخة (أ)" in p else TRUE),
        })
        self.assertEqual(code, 0)
        recs = self.records()
        self.assertEqual({r["check"]["state"] for r in recs.values()}, {"reconciled"})
        self.assertNotIn("كاقية", md)
        # Settling requests are spread over both models' quotas.
        used = {m for m, k in fake.calls if k == "reconcile"}
        self.assertEqual(used, {"gemini-3.5-flash-lite", "gemini-3.1-flash-lite"})

    def test_bad_settlement_keeps_a_reading(self):
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer("كلام قصير" if "النسخة (أ)" in p else WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer("كلام قصير" if "النسخة (أ)" in p else TRUE),
        }, pages="30", extra=("--arbiter-models", ""))
        self.assertEqual(code, 0)
        rec = self.records()[30]
        self.assertIn(rec["check"]["state"], ("kept", "unsure"))
        self.assertGreater(len(rec["text"]), 200)

    def test_refused_page_goes_to_rescue_model(self):
        refuse = lambda p, n: answer("", "FinishReason.PROHIBITED_CONTENT")
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": refuse,
            "gemini-3.1-flash-lite": refuse,
            "gemma-4-26b-a4b-it": lambda p, n: answer(TRUE),
        }, pages="30")
        rec = self.records()[30]
        self.assertEqual(rec["a"]["model"], "gemma-4-26b-a4b-it")
        self.assertEqual(rec["b"]["status"], "suspicious")
        self.assertEqual(code, 0)
        self.assertIn("وجود قوانين", md)

    def test_quota_runs_out_then_resumes_next_day(self):
        def quota_after(limit, text):
            def f(p, n):
                if n > limit:
                    raise Exception("429 RESOURCE_EXHAUSTED quotaId GenerateRequestsPerDayPerProjectPerModel-FreeTier")
                return answer(text if "النسخة (أ)" not in p else TRUE)
            return f
        # Gemma is always available: it must not take over pages when Gemini runs out.
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": quota_after(1, WRONG),
            "gemini-3.1-flash-lite": quota_after(2, TRUE),
            "gemma-4-26b-a4b-it": lambda p, n: answer("خطأ جيما " * 60),
        }, pages="30-32")
        self.assertEqual(code, 3)
        self.assertEqual(st["state"], "paused")
        self.assertTrue(st["quota_day"])
        self.assertTrue(st["pages_waiting"])
        self.assertNotIn("gemma-4-26b-a4b-it", {m for m, _ in fake.calls})
        # Next day: quotas are back and only the missing work is done.
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE if "النسخة (أ)" in p else WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
            "gemma-4-26b-a4b-it": lambda p, n: answer(TRUE),
        }, pages="30-32")
        self.assertEqual(code, 0, st)
        self.assertEqual(st["state"], "done")
        self.assertNotIn("كاقية", md)
        self.assertNotIn("جيما", md)

    def test_settling_error_is_retried_on_the_next_run(self):
        def overloaded(p, n):
            if "النسخة (أ)" in p:
                raise Exception("503 UNAVAILABLE")
            return answer(WRONG)
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": overloaded,
            "gemini-3.1-flash-lite": lambda p, n: (_ for _ in ()).throw(Exception("503 UNAVAILABLE"))
            if "النسخة (أ)" in p else answer(TRUE),
        }, pages="30")
        self.assertEqual(code, 2)
        self.assertEqual(st["state"], "incomplete")
        self.assertEqual(st["pages_retry"], [30])
        # Next run: the API works again and the page gets settled.
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE if "النسخة (أ)" in p else WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        }, pages="30")
        self.assertEqual(code, 0)
        self.assertEqual(self.records()[30]["check"]["state"], "reconciled")
        self.assertNotIn("كاقية", md)

    def test_quota_only_for_second_model_does_not_bring_in_gemma(self):
        def gone(p, n):
            raise Exception("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.1-flash-lite": gone,
            "gemma-4-26b-a4b-it": lambda p, n: answer(TRUE),
        }, pages="30")
        self.assertEqual(code, 3)
        self.assertNotIn("gemma-4-26b-a4b-it", {m for m, _ in fake.calls})

    def test_unsure_page_goes_to_arbiter_and_notice_in_book(self):
        # The two Lite models cannot settle (their answer is too short), the arbiter can.
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer("قصير" if "النسخة (أ)" in p else WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer("قصير" if "النسخة (أ)" in p else TRUE),
            # One request left today on the strongest judge; the next judge cannot settle.
            "gemini-3.8-flash": lambda p, n: answer(TRUE) if n == 1 else (_ for _ in ()).throw(
                Exception("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier")),
            "gemini-3.7-flash": lambda p, n: answer("قصير"),
        }, pages="30-31", extra=("--arbiter-limit", "1"))
        recs = self.records()
        self.assertEqual(recs[30]["check"]["state"], "reconciled")
        self.assertEqual(recs[30]["check"]["arbiter"], "gemini-3.8-flash: settled")
        # Page 31 was over today's arbiter limit: still unsure, and the book says so.
        self.assertEqual(recs[31]["check"]["state"], "unsure")
        self.assertIn("قراءة الصفحة 31 من الأصل غير مؤكدة", md)
        # Reading is done; only the judges are missing: the book waits for them ("judging").
        self.assertEqual(code, 4)
        self.assertEqual(st["state"], "paused")
        self.assertEqual(st["waiting_for"], "judges")
        self.assertEqual(st["pages_judging"], [31])
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.8-flash": lambda p, n: answer(TRUE),
        }, pages="30-31")
        self.assertEqual(code, 0, st)
        self.assertEqual(self.records()[31]["check"]["state"], "reconciled")
        self.assertNotIn("قراءة الصفحة 30 من الأصل غير مؤكدة", md)

    def test_cover_is_picked_from_the_first_pages(self):
        def lite(p, n):
            if "غلاف الكتاب" in p:
                return answer('{"cover": 3}')
            return answer(TRUE)
        code, st, md, fake = self.run_book({"gemini-3.5-flash-lite": lite, "gemini-3.1-flash-lite": lite},
                                           pages="1-6")
        work = os.path.join(self.tmp, "work")
        with open(os.path.join(work, os.listdir(work)[0], "cover.json")) as f:
            self.assertEqual(json.load(f)["index"], 2)

    def test_single_mode(self):
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
        }, pages="30", extra=("--single",))
        self.assertEqual(code, 0)
        self.assertEqual(self.records()[30]["check"]["state"], "single")
        self.assertEqual({m for m, _ in fake.calls}, {"gemini-3.5-flash-lite"})


class CrossCheck(unittest.TestCase):
    def test_spelling_variants_are_not_differences(self):
        ratio, diffs = crosscheck.compare("فى الكتاب الذى قرأه إبراهيم", "في الكتاب الذي قرأه ابراهيم")
        self.assertEqual(diffs, [])
        self.assertEqual(ratio, 1.0)

    def test_differences_are_listed(self):
        ratio, diffs = crosscheck.compare("قد برهن برهنة كافية", "قد برهن كافية")
        self.assertEqual(diffs, [("برهنة", "")])

    def test_prompt_survives_braces(self):
        prompt = crosscheck.reconcile_prompt("rules", "نص {أ}", "نص {ب}", [("{أ}", "{ب}")], ["سطر"])
        self.assertIn("نص {أ}", prompt)
        self.assertIn("سطر", prompt)

    def test_validate_final(self):
        self.assertTrue(crosscheck.validate_final(TRUE, WRONG, TRUE))
        self.assertFalse(crosscheck.validate_final("قصير", WRONG, TRUE))
        self.assertFalse(crosscheck.validate_final("", WRONG, TRUE))

    def test_validate_final_rejects_dropping_agreed_words(self):
        words = TRUE.split()
        dropped = " ".join(words[:10] + words[16:])  # six agreed words gone (about 10%)
        self.assertFalse(crosscheck.validate_final(dropped, WRONG, TRUE))

    def test_validate_final_needs_the_line_one_reading_dropped(self):
        line = "وهذا سطر كامل سقط من القراءة الثانية وحدها"
        full = TRUE + " " + line + " " + TRUE
        short = TRUE + " " + TRUE
        self.assertFalse(crosscheck.validate_final(short, full, short))
        self.assertTrue(crosscheck.validate_final(full, full, short))


if __name__ == "__main__":
    unittest.main()
