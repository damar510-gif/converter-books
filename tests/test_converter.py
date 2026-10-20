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
        self.calls.append((model, "reconcile" if "النسخة (أ)" in prompt else "cover" if "غلاف الكتاب" in prompt
                           else "read"))
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

    def test_new_gemini_models_join_the_judges(self):
        names = ("gemini-4-pro-preview", "gemini-3.9-flash", "gemini-3.9-flash-lite", "gemini-3.8-flash",
                 "gemini-3-flash-preview", "gemini-3.9-flash-image", "gemma-5-27b-it", "gemini-2.0-flash",
                 "gemini-3.9-flash-preview-09-2026", "gemini-3.6-pro", "gemini-nano-3")
        listed = [pytypes.SimpleNamespace(name="models/" + n, supported_actions=["generateContent"]) for n in names]
        pool = pytypes.SimpleNamespace(client=pytypes.SimpleNamespace(
            models=pytypes.SimpleNamespace(list=lambda: listed)))
        got = c.resolve_models(pool, ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3-flash", "gemini-2.5-flash"])
        self.assertEqual(got, ["gemini-4-pro-preview", "gemini-3.9-flash", "gemini-3.8-flash", "gemini-3.7-flash",
                               "gemini-3.6-pro", "gemini-3-flash-preview", "gemini-2.5-flash"])
        self.assertEqual(pool.added_judges, ["gemini-4-pro-preview", "gemini-3.9-flash", "gemini-3.6-pro"])

    def test_model_without_free_quota_is_skipped_at_once(self):
        calls = []

        def generate_content(model, contents, config):
            calls.append(model)
            raise Exception("429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
                            "generate_content_free_tier_requests, limit: 0, model: gemini-4-pro")
        client, sleep = c.genai.Client, c.time.sleep
        os.environ["GEMINI_API_KEY"] = "test"
        c.genai.Client = lambda **kw: pytypes.SimpleNamespace(models=pytypes.SimpleNamespace(
            generate_content=generate_content))
        c.time.sleep = lambda s: None
        try:
            pool = c.ModelPool("a", "b", "", 60)
            with self.assertRaises(c.QuotaExhausted):
                pool.read(None, "x", "gemini-4-pro")
        finally:
            c.genai.Client, c.time.sleep = client, sleep
        self.assertEqual(calls, ["gemini-4-pro"])
        self.assertIn("gemini-4-pro", pool.missing)
        self.assertEqual(pool.quota_day, "")

    def test_plain_name_preferred_and_dated_names_ranked(self):
        names = ("gemini-4-flash-preview", "gemini-4-flash", "gemini-3.8-flash-preview-11-2026", "gemini-3-pro-preview")
        listed = [pytypes.SimpleNamespace(name="models/" + n, supported_actions=["generateContent"]) for n in names]
        pool = pytypes.SimpleNamespace(client=pytypes.SimpleNamespace(
            models=pytypes.SimpleNamespace(list=lambda: listed)))
        got = c.resolve_models(pool, ["gemini-3.8-flash"])
        self.assertEqual(got, ["gemini-4-flash", "gemini-3.8-flash-preview-11-2026", "gemini-3-pro-preview"])

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

    def review_round(self):
        """Page 30 left unsure with its two readings' difference, and the places written."""
        out = os.path.join(self.tmp, "rv1")
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer("قصير" if "النسخة (أ)" in p else WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer("قصير" if "النسخة (أ)" in p else TRUE),
        }, pages="30", extra=("--arbiter-models", "", "--review-out", out))
        with open(os.path.join(out, "items.json"), encoding="utf-8") as f:
            return code, st, md, json.load(f)

    def test_answers_are_applied_without_any_request(self):
        code, st, md, items = self.review_round()
        self.assertEqual(st["pages_unsure"], [30])
        self.assertEqual(st["review_items"], 1)
        self.assertIn("قراءة الصفحة 30 من الأصل غير مؤكدة", md)
        self.assertIn("«كاقية» أو «كافية»", md)
        page = items["pages"][0]
        item = page["items"][0]
        self.assertEqual((page["page"], item["kind"], item["core"]), (30, "diff", "كاقية"))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "rv1", page["img"])))
        answers = {"v": 1, "answers": {item["id"]: {
            **{k: item[k] for k in ("kind", "fp", "before", "core", "after", "pos")},
            "page": 30, "base": page["base"], "verdict": "fix", "text": "كافية", "at": "2026-10-20T10:00:00Z"}}}
        path = os.path.join(self.tmp, "answers.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(answers, f, ensure_ascii=False)
        refuse = lambda p, n: (_ for _ in ()).throw(AssertionError("no request expected"))
        out = os.path.join(self.tmp, "rv2")
        code, st, md, fake = self.run_book({"gemini-3.5-flash-lite": refuse, "gemini-3.1-flash-lite": refuse},
                                           pages="30", extra=("--review-only", "--answers", path, "--human",
                                                              os.path.join(self.tmp, "rv1", "human.json"),
                                                              "--review-out", out))
        self.assertEqual(fake.calls, [])
        self.assertEqual(code, 0)
        self.assertEqual((st["pages_unsure"], st["pages_human"]), ([], [30]))
        self.assertEqual((st["review_applied"], st["review_items"], st["review_seen"]),
                         (1, 0, "2026-10-20T10:00:00Z"))
        self.assertNotIn("كاقية", md)
        self.assertNotIn("غير مؤكدة", md)
        self.assertEqual(self.records()[30]["check"]["state"], "human")
        with open(os.path.join(out, "human.json"), encoding="utf-8") as f:
            human = json.load(f)
        self.assertEqual(list(human["pages"]), ["30"])
        self.assertNotIn("كاقية", human["pages"]["30"]["text"])
        # A later run with judges and fresh readings leaves the page as the user made it.
        shutil.rmtree(os.path.join(self.tmp, "work"))
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer("قصير" if "النسخة (أ)" in p else WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer("قصير" if "النسخة (أ)" in p else TRUE),
            "gemini-3.8-flash": refuse,
        }, pages="30", extra=("--human", os.path.join(out, "human.json"), "--answers", path))
        self.assertEqual([k for _, k in fake.calls if k != "cover"], [])
        self.assertNotIn("كاقية", md)
        self.assertEqual(st["pages_human"], [30])

    def test_judge_answers_apply_and_the_users_win(self):
        code, st, md, items = self.review_round()
        page = items["pages"][0]
        item = page["items"][0]
        base = {**{k: item[k] for k in ("kind", "fp", "before", "core", "after", "pos")}, "page": 30,
                "base": page["base"], "at": "2026-10-20T10:00:00Z"}
        judge = os.path.join(self.tmp, "judge.json")
        with open(judge, "w", encoding="utf-8") as f:
            json.dump({"answers": {item["id"]: {**base, "verdict": "fix", "text": "كافية"}}}, f, ensure_ascii=False)
        refuse = lambda p, n: (_ for _ in ()).throw(AssertionError("no request expected"))
        code, st, md, fake = self.run_book({"gemini-3.5-flash-lite": refuse, "gemini-3.1-flash-lite": refuse},
                                           pages="30", extra=("--review-only", "--judge-answers", judge, "--review-out",
                                                              os.path.join(self.tmp, "rv-j")))
        self.assertNotIn("كاقية", md)
        self.assertEqual(st["review_applied"], 1)
        # Same place, the user says the reading is right: theirs wins.
        shutil.rmtree(os.path.join(self.tmp, "work"))
        self.review_round()
        user = os.path.join(self.tmp, "user.json")
        with open(user, "w", encoding="utf-8") as f:
            json.dump({"answers": {item["id"]: {**base, "verdict": "keep"}}}, f, ensure_ascii=False)
        code, st, md, fake = self.run_book({"gemini-3.5-flash-lite": refuse, "gemini-3.1-flash-lite": refuse},
                                           pages="30", extra=("--review-only", "--judge-answers", judge,
                                                              "--answers", user, "--review-out",
                                                              os.path.join(self.tmp, "rv-u")))
        self.assertIn("كاقية", md)
        self.assertEqual((st["review_applied"], st["review_kept"]), (0, 1))

    def test_review_only_needs_saved_pages(self):
        refuse = lambda p, n: (_ for _ in ()).throw(AssertionError("no request expected"))
        code, st, md, fake = self.run_book({"gemini-3.5-flash-lite": refuse, "gemini-3.1-flash-lite": refuse},
                                           pages="30", extra=("--review-only",))
        self.assertEqual((code, st["state"], st["pages_waiting"], fake.calls), (3, "paused", [30], []))

    def test_answers_of_another_book_are_ignored(self):
        human = os.path.join(self.tmp, "human.json")
        with open(human, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "book_hash": "0" * 16, "pages": {"30": {"text": "نص آخر", "ids": [], "at": "x"}}}, f)
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        }, pages="30", extra=("--human", human))
        self.assertNotIn("نص آخر", md)
        self.assertEqual(st["pages_human"], [])
        with open(os.path.join(self.tmp, "report.md"), encoding="utf-8") as f:
            self.assertIn("لكتاب آخر", f.read())

    def test_table_of_contents_survives_a_correction_near_a_heading(self):
        md = "# مقدمة الكتاب\n\nنص أول\n\n# المقال الأول\n\nنص المقال\n\n# قسم داخلي\n\nنص القسم\n"
        pool = pytypes.SimpleNamespace(model="m", second="", available=lambda m: True,
                                       read=lambda png, prompt, model: ('{"main": [2]}', "STOP"))
        first = c.keep_main_headings(md, pool, self.tmp)
        self.assertIn("\n# المقال الأول", first)
        self.assertIn("\n## قسم داخلي", first)
        # The text after a heading changed: the same choice, without asking again.
        refuse = pytypes.SimpleNamespace(model="m", second="", available=lambda m: True,
                                         read=lambda *a: (_ for _ in ()).throw(AssertionError("asked")))
        again = c.keep_main_headings(md.replace("نص المقال", "نص المقال مصححاً"), refuse, self.tmp)
        self.assertEqual(again, first.replace("نص المقال", "نص المقال مصححاً"))

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
