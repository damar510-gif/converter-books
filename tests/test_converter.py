"""Offline tests for the reading pipeline: a fake Gemini client stands in for the API.

Run from the repository root:  python -m unittest discover -s tests
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import types as pytypes
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import converter as c  # noqa: E402
import live  # noqa: E402
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

    def test_new_gemini_models_join_the_free_models(self):
        names = ("gemini-4-pro-preview", "gemini-3.9-flash", "gemini-3.9-flash-lite", "gemini-3.8-flash",
                 "gemini-3-flash-preview", "gemini-3.9-flash-image", "gemma-5-27b-it", "gemini-2.0-flash",
                 "gemini-3.9-flash-preview-09-2026", "gemini-3.6-pro", "gemini-nano-3")
        listed = [pytypes.SimpleNamespace(name="models/" + n, supported_actions=["generateContent"]) for n in names]
        pool = pytypes.SimpleNamespace(client=pytypes.SimpleNamespace(
            models=pytypes.SimpleNamespace(list=lambda: listed)))
        got = c.resolve_models(pool, ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3-flash", "gemini-2.5-flash"])
        self.assertEqual(got, ["gemini-4-pro-preview", "gemini-3.9-flash", "gemini-3.8-flash", "gemini-3.7-flash",
                               "gemini-3.6-pro", "gemini-3-flash-preview", "gemini-2.5-flash"])
        self.assertEqual(pool.added_models, ["gemini-4-pro-preview", "gemini-3.9-flash", "gemini-3.6-pro"])

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


class PipelineBase(unittest.TestCase):
    """Runs the converter on pages of the test book with scripted models (no tests here)."""

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
        """extra: more converter options; the Flash models are left out unless a test names them."""
        fake = FakeModels(behaviour)
        c.genai.Client = lambda **kw: pytypes.SimpleNamespace(models=fake)
        out = os.path.join(self.tmp, "book.epub")
        status = os.path.join(self.tmp, "status.json")
        sys.argv = ["converter.py", PDF, "-o", out, "--pages", pages, "--work-dir", os.path.join(self.tmp, "work"),
                    "--report", os.path.join(self.tmp, "report.md"), "--status", status, "--rpm", "6000",
                    "--keep-markdown", "--extra-models", "", *extra]
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


class Pipeline(PipelineBase):
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
        }, pages="30")
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
        self.assertNotIn("waiting_for", st)
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

    def test_second_reading_error_is_retried_not_kept_single(self):
        # Run 61 (2026-10-23): Gemma's 500s left pages with one reading, delivered as done.
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.1-flash-lite": lambda p, n: (_ for _ in ()).throw(Exception("500 INTERNAL")),
        }, pages="30")
        self.assertEqual(code, 2)
        self.assertEqual(st["pages_retry"], [30])
        self.assertIn("وجود قوانين", md)
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        }, pages="30")
        self.assertEqual(code, 0)
        rec = self.records()[30]
        self.assertEqual((rec["b"]["model"], rec["check"]["state"]), ("gemini-3.1-flash-lite", "agreed"))

    def test_quota_only_for_second_model_does_not_bring_in_gemma(self):
        def gone(p, n):
            raise Exception("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.1-flash-lite": gone,
            "gemma-4-26b-a4b-it": lambda p, n: answer(TRUE),
        }, pages="30")
        self.assertEqual(code, 6)
        self.assertNotIn("gemma-4-26b-a4b-it", {m for m, _ in fake.calls})

    def test_one_reader_out_parks_the_book_for_it_and_only_its_readings_are_done_next_day(self):
        def gone(p, n):
            raise Exception("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.1-flash-lite": gone,
        })
        self.assertEqual(code, 6)
        self.assertEqual((st["state"], st["waiting_for"]), ("paused", "reader"))
        self.assertEqual(st["readers_left"], ["gemini-3.5-flash-lite"])
        self.assertEqual(st["pages_waiting"], [30, 31])
        self.assertTrue(st["quota_day"])
        # The next day: only the missing second readings are asked for.
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        })
        self.assertEqual(code, 0, st)
        self.assertEqual([m for m, k in fake.calls if k == "read"], ["gemini-3.1-flash-lite"] * 2)
        self.assertEqual({r["check"]["state"] for r in self.records().values()}, {"agreed"})

    def test_settling_uses_the_reader_that_is_left(self):
        def gone_for_settling(p, n):
            if "النسخة (أ)" in p:
                raise Exception("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier")
            return answer(TRUE)
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE if "النسخة (أ)" in p else WRONG),
            "gemini-3.1-flash-lite": gone_for_settling,
        }, pages="30")
        self.assertEqual(code, 0, st)
        check = self.records()[30]["check"]
        self.assertEqual((check["state"], check["model"]), ("reconciled", "gemini-3.5-flash-lite"))
        self.assertNotIn("كاقية", md)

    def test_unsure_page_is_delivered_with_its_notice_and_no_judge_is_asked(self):
        # The two Lite models cannot settle (their answer is too short): the page stays unsure
        # for the user's own models or the user; no free model judges it.
        never = lambda p, n: self.fail("no free model judges an unsure page")
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer("قصير" if "النسخة (أ)" in p else WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer("قصير" if "النسخة (أ)" in p else TRUE),
            "gemini-3.8-flash": never,
        }, pages="30-31", extra=("--extra-models", "gemini-3.8-flash"))
        self.assertEqual(code, 0, st)
        self.assertEqual((st["state"], st["pages_unsure"]), ("done", [30, 31]))
        self.assertNotIn("pages_judging", st)
        self.assertEqual({r["check"]["state"] for r in self.records().values()}, {"unsure"})
        self.assertIn("قراءة الصفحة 31 من الأصل غير مؤكدة", md)

    def test_flash_models_settle_first_and_never_read(self):
        never = lambda p, n: self.fail("a Flash model does not read")
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.8-flash": lambda p, n: answer(TRUE) if "النسخة (أ)" in p else never(p, n),
        }, pages="30", extra=("--extra-models", "gemini-3.8-flash"))
        self.assertEqual(code, 0, st)
        rec = self.records()[30]
        self.assertEqual((rec["a"]["model"], rec["b"]["model"]), ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"))
        self.assertEqual((rec["check"]["state"], rec["check"]["model"]), ("reconciled", "gemini-3.8-flash"))
        self.assertEqual([m for m, k in fake.calls if k == "reconcile"], ["gemini-3.8-flash"])
        self.assertNotIn("كاقية", md)
        # Its quota used up: a Lite reader settles.
        def gone(p, n):
            raise Exception("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        shutil.rmtree(os.path.join(self.tmp, "work"))
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE if "النسخة (أ)" in p else WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.8-flash": gone,
        }, pages="30", extra=("--extra-models", "gemini-3.8-flash"))
        self.assertEqual(code, 0, st)
        self.assertEqual(self.records()[30]["check"]["state"], "reconciled")
        self.assertIn(self.records()[30]["check"]["model"], ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"))

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
        }, pages="30", extra=("--review-out", out))
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


class FreeBoxes(PipelineBase):
    """The status page's boxes for the free models (--boxes): who reads first, who reads
    second and who settles, each in the user's order."""

    GONE = Exception("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier")

    def gone(self, p, n):
        raise self.GONE

    def test_gemma_reads_first_a_lite_model_second_and_a_flash_model_settles(self):
        code, st, md, fake = self.run_book({
            "gemma-4-31b-it": lambda p, n: answer(WRONG),
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.8-flash": lambda p, n: answer(TRUE),
        }, pages="30", extra=("--boxes", "read1=gemma-4-31b-it;read2=gemini-3.5-flash-lite;settle=gemini-3.8-flash"))
        self.assertEqual(code, 0, st)
        rec = self.records()[30]
        self.assertEqual((rec["a"]["model"], rec["b"]["model"]), ("gemma-4-31b-it", "gemini-3.5-flash-lite"))
        self.assertEqual((rec["check"]["state"], rec["check"]["model"]), ("reconciled", "gemini-3.8-flash"))
        self.assertNotIn("كاقية", md)
        self.assertEqual({m for m, k in fake.calls if k == "read"}, {"gemma-4-31b-it", "gemini-3.5-flash-lite"})
        self.assertEqual([m for m, k in fake.calls if k == "reconcile"], ["gemini-3.8-flash"])
        self.assertEqual(st["boxes"], {"read1": ["gemma-4-31b-it"], "read2": ["gemini-3.5-flash-lite"],
                                       "settle": ["gemini-3.8-flash"]})

    def test_gemma_settles_when_the_user_puts_it_there(self):
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
            "gemma-4-31b-it": lambda p, n: answer(TRUE),
        }, pages="30", extra=("--boxes", "read1=gemini-3.5-flash-lite;read2=gemini-3.1-flash-lite;settle=gemma-4-31b-it"))
        self.assertEqual(code, 0, st)
        self.assertEqual(self.records()[30]["check"]["model"], "gemma-4-31b-it")

    def test_a_box_whose_models_are_out_waits_and_no_other_box_fills_in(self):
        # Without boxes the second reader would give first readings too (both Lite readers
        # in both readings); a box is only its own models.
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": self.gone,
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        }, extra=("--boxes", "read1=gemini-3.5-flash-lite;read2=gemini-3.1-flash-lite;settle=gemini-3.1-flash-lite"))
        self.assertEqual(code, 3, st)
        self.assertEqual(st["state"], "paused")
        self.assertNotIn("waiting_for", st, "no first reader is left: the next book could not start either")
        # The second box gave its own reading of each page (2026-10-24), never the first box's.
        recs = self.records().values()
        self.assertEqual({(r["a"]["model"], c.box_of(r["a"], "a")) for r in recs}, {("gemini-3.1-flash-lite", "read2")})
        self.assertFalse(any("b" in r for r in recs))
        self.assertEqual(st["pages_waiting"], [30, 31])

    def test_the_second_box_out_parks_the_book_while_the_first_reads_on(self):
        code, st, md, fake = self.run_book({
            "gemma-4-31b-it": lambda p, n: answer(TRUE),
            "gemini-3.1-flash-lite": self.gone,
        }, extra=("--boxes", "read1=gemma-4-31b-it;read2=gemini-3.1-flash-lite;settle=gemini-3.8-flash"))
        self.assertEqual((st["state"], st["waiting_for"]), ("paused", "reader"))
        self.assertEqual(st["readers_left"], ["gemma-4-31b-it"])
        self.assertEqual({r["a"]["model"] for r in self.records().values()}, {"gemma-4-31b-it"})

    def test_one_model_in_both_reading_boxes_never_reads_a_page_twice(self):
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        }, pages="30", extra=("--boxes", "read1=gemini-3.5-flash-lite;read2=gemini-3.5-flash-lite,gemini-3.1-flash-lite;"
                                         "settle=gemini-3.8-flash"))
        self.assertEqual(code, 0, st)
        rec = self.records()[30]
        self.assertEqual((rec["a"]["model"], rec["b"]["model"]), ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"))

    def test_the_rescue_model_in_a_reading_box_reads_like_any_reader(self):
        # Gemma 4 26B, the model for refused pages, put in the first box: its reading is no
        # refused page's, so the next run settles the page as usual.
        boxes = ("--boxes", "read1=gemma-4-26b-a4b-it;read2=gemini-3.5-flash-lite;settle=gemini-3.8-flash")
        code, st, md, fake = self.run_book({
            "gemma-4-26b-a4b-it": lambda p, n: answer(WRONG),
            "gemini-3.5-flash-lite": self.gone,
        }, pages="30", extra=boxes)
        self.assertEqual(st["state"], "paused")
        self.assertEqual(self.records()[30]["a"]["model"], "gemma-4-26b-a4b-it")
        code, st, md, fake = self.run_book({
            "gemma-4-26b-a4b-it": lambda p, n: answer(WRONG),
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.8-flash": lambda p, n: answer(TRUE),
        }, pages="30", extra=boxes)
        self.assertEqual(code, 0, st)
        self.assertEqual((self.records()[30]["check"]["state"], self.records()[30]["check"]["model"]),
                         ("reconciled", "gemini-3.8-flash"))
        self.assertNotIn("كاقية", md)

    def test_empty_boxes_mean_nobody_works_and_the_book_waits_for_the_user(self):
        code, st, md, fake = self.run_book({}, extra=("--boxes", "read1=;read2=;settle="))
        self.assertEqual(code, 8, st)
        self.assertEqual((st["state"], st["waiting_for"]), ("paused", "boxes"))
        self.assertEqual(asked_models(fake), set())
        self.assertEqual(st["boxes"], {"read1": [], "read2": [], "settle": []})

    def test_an_empty_settling_box_settles_nothing_and_says_so(self):
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.8-flash": lambda p, n: self.fail("not in a box"),
        }, pages="30", extra=("--boxes", "read1=gemini-3.5-flash-lite;read2=gemini-3.1-flash-lite;settle=",
                              "--extra-models", "gemini-3.8-flash"))
        self.assertEqual((st["state"], st["waiting_for"]), ("paused", "boxes"))
        self.assertNotIn("reconcile", [k for _, k in fake.calls])

    def test_gemma_reads_no_refused_page_unless_it_is_in_a_box(self):
        refused = lambda p, n: answer("", finish="PROHIBITED_CONTENT")
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": refused,
            "gemini-3.1-flash-lite": refused,
            "gemma-4-26b-a4b-it": lambda p, n: self.fail("Gemma is in no box"),
        }, pages="30", extra=("--boxes", "read1=gemini-3.5-flash-lite;read2=gemini-3.1-flash-lite;settle=gemini-3.1-flash-lite"))
        self.assertNotIn("gemma-4-26b-a4b-it", asked_models(fake))
        self.assertNotIn("rescue", json.dumps(st))

    def test_models_found_out_today_are_not_asked_again(self):
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.8-flash": lambda p, n: self.fail("out today"),
        }, pages="30", extra=("--boxes", "read1=gemini-3.5-flash-lite;read2=gemini-3.1-flash-lite;"
                                         "settle=gemini-3.8-flash,gemini-3.1-flash-lite;out=gemini-3.8-flash"))
        self.assertEqual(code, 0, st)
        self.assertEqual(self.records()[30]["check"]["model"], "gemini-3.1-flash-lite")
        self.assertIn("gemini-3.8-flash", st["models_out"])

    def test_one_request_at_a_time_per_model(self):
        import threading
        busy, most, lock = {}, {}, threading.Lock()

        def counted(model, text):
            def reply(p, n):
                with lock:
                    busy[model] = busy.get(model, 0) + 1
                    most[model] = max(most.get(model, 0), busy[model])
                threading.Event().wait(0.05)
                with lock:
                    busy[model] -= 1
                return answer(text)
            return reply
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": counted("gemini-3.5-flash-lite", TRUE),
            "gemini-3.1-flash-lite": counted("gemini-3.1-flash-lite", TRUE),
        }, pages="28-33")
        self.assertEqual(code, 0, st)
        self.assertEqual(most, {"gemini-3.5-flash-lite": 1, "gemini-3.1-flash-lite": 1})

    def test_the_daily_limit_is_believed_on_the_third_429_in_a_row_and_asked_no_more(self):
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": self.gone,
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        }, pages="28-33", extra=("--boxes", "read1=gemini-3.5-flash-lite;read2=gemini-3.1-flash-lite;settle="))
        self.assertEqual([m for m, _ in fake.calls].count("gemini-3.5-flash-lite"), c.DAY_CHECKS)
        self.assertEqual(st["models_out"], ["gemini-3.5-flash-lite"])

    def test_the_models_of_a_box_share_its_pages_with_no_order(self):
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE if "النسخة (أ)" in p else WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        }, pages="28-33", extra=("--boxes", "read1=gemini-3.5-flash-lite,gemini-3.1-flash-lite;"
                                            "read2=gemini-3.5-flash-lite,gemini-3.1-flash-lite;"
                                            "settle=gemini-3.5-flash-lite,gemini-3.1-flash-lite"))
        self.assertEqual(code, 0, st)
        firsts = [r["a"]["model"] for r in self.records().values()]
        self.assertEqual(set(firsts), {"gemini-3.5-flash-lite", "gemini-3.1-flash-lite"}, firsts)
        for r in self.records().values():
            self.assertNotEqual(r["a"]["model"], r["b"]["model"])
        settlers = {r["check"]["model"] for r in self.records().values() if r["check"].get("model")}
        self.assertEqual(settlers, {"gemini-3.5-flash-lite", "gemini-3.1-flash-lite"})

    def test_a_model_sent_back_to_work_works_again(self):
        pool = c.ModelPool("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemma-4-26b-a4b-it", 10)
        pool.missing.add("gemma-4-26b-a4b-it")
        pool.set_out(["gemini-3.5-flash-lite", "gemini-3-flash-preview", "gemma-4-26b-a4b-it"])
        self.assertFalse(pool.available("gemini-3.5-flash-lite"))
        self.assertTrue(pool.revive("gemini-3.5-flash-lite"))
        self.assertTrue(pool.revive("gemini-3-flash"))
        self.assertFalse(pool.revive("gemma-4-26b-a4b-it"), "a model this account lacks stays out")
        self.assertEqual(pool.exhausted, {"gemma-4-26b-a4b-it"})
        self.assertEqual(pool.revived, {"gemini-3.5-flash-lite", "gemini-3-flash-preview"})

    def test_the_boxes_and_the_catalog(self):
        self.assertEqual(c.parse_boxes("read1=a,b,a;read2=c;settle=;other=x"),
                         {"read1": ["a", "b"], "read2": ["c"], "settle": []})
        self.assertEqual(c.parse_boxes(""), {})
        self.assertEqual(c.parse_boxes("read1=a;out=b,c", ("out",)), {"out": ["b", "c"]})
        have = dict.fromkeys(["gemini-3.5-flash-lite", "gemini-3.1-flash-lite-preview", "gemini-3.1-flash-lite",
                              "gemini-3.8-flash", "gemini-3-flash-preview", "gemini-3.1-pro-preview", "gemini-2.5-flash",
                              "gemini-2.0-flash", "gemini-3.8-flash-image", "gemini-flash-latest", "gemma-4-26b-a4b-it",
                              "gemma-4-31b-it", "gemma-3-27b-it", "gemini-embedding-2", "gemini-robotics-er-2-preview",
                              "gemini-3.8-flash-preview-11-2026"], True)
        self.assertEqual(c.free_catalog(have), [
            "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash", "gemini-3-flash-preview",
            "gemini-2.5-flash", "gemini-3.1-pro-preview", "gemma-4-31b-it", "gemma-4-26b-a4b-it"])

    # The user changes the boxes during a run (2026-10-23: "why not give it pages at once?").

    def changing(self, boxes, helper=None, on=1):
        """A run whose first reader's call number `on` is when the user changes the boxes on
        the status page (the requests file, read there and then)."""
        lv = live.Live(None)
        lv.control = Requests()
        self._open_live = c.live.open_live
        c.live.open_live = lambda *a, **k: lv
        self.addCleanup(setattr, c.live, "open_live", self._open_live)

        def change(n):
            if n == on:
                lv.control.data = {"boxes": boxes, "boxes_at": "2026-10-23T20:00:00.000Z"}
                if helper is not None:
                    lv.control.data["helper"] = helper
                lv.check_control()
        return change

    def test_a_model_put_in_a_box_during_the_run_takes_pages_in_it(self):
        # The second box is empty at the start; while page 31 is read, a Lite model comes
        # into it and into the settling box: page 31 gets its second reading as usual, and
        # page 30, passed over before, gets its own in the same run.
        change = self.changing({"read1": ["gemma-4-31b-it"], "read2": ["gemini-3.1-flash-lite"],
                                "settle": ["gemini-3.1-flash-lite"]}, on=2)
        code, st, md, fake = self.run_book({
            "gemma-4-31b-it": lambda p, n: (change(n), answer(WRONG))[1],
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        }, extra=("--boxes", "read1=gemma-4-31b-it;read2=;settle=", "--workers", "1"))
        self.assertEqual(code, 0, st)
        for r in self.records().values():
            self.assertEqual((r["a"]["model"], r["b"]["model"]), ("gemma-4-31b-it", "gemini-3.1-flash-lite"))
            self.assertEqual((r["check"]["state"], r["check"]["model"]), ("reconciled", "gemini-3.1-flash-lite"))
        self.assertEqual(st["boxes_at"], "2026-10-23T20:00:00.000Z")
        self.assertNotIn("كاقية", md)

    def test_a_model_taken_out_of_its_box_takes_no_new_page(self):
        change = self.changing({"read1": ["gemini-3.5-flash-lite"], "read2": ["gemini-3.1-flash-lite"],
                                "settle": []})
        code, st, md, fake = self.run_book({
            "gemma-4-31b-it": lambda p, n: (change(n), answer(TRUE))[1],
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        }, extra=("--boxes", "read1=gemma-4-31b-it;read2=gemini-3.1-flash-lite;settle=", "--workers", "1"))
        self.assertEqual(code, 0, st)
        self.assertEqual([m for m, k in fake.calls if k == "read"].count("gemma-4-31b-it"), 1)
        # Page 31: the second box read it first (from the end of the book), the new first box after.
        r = self.records()[31]
        self.assertEqual({r["a"]["model"], r["b"]["model"]}, {"gemini-3.5-flash-lite", "gemini-3.1-flash-lite"})

    def test_a_request_waiting_for_a_model_taken_out_goes_to_the_others(self):
        pool = c.ModelPool("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemma-4-26b-a4b-it", 10)
        pool.set_boxes({"read1": ["gemma-4-31b-it"], "read2": ["gemini-3.1-flash-lite"], "settle": []})
        self.assertTrue(c.boxes_changed(pool, [], c.helper_modes(None),
                                        {"read1": ["gemini-3.5-flash-lite"], "read2": ["gemini-3.1-flash-lite"]}, None))
        with live.doing(30, "a"):
            self.assertRaises(c.Moved, pool.read, b"png", "prompt", "gemma-4-31b-it")
        self.assertEqual(pool.reading("a"), ["gemini-3.5-flash-lite"])
        self.assertEqual(pool.box_gen, 2)

    def test_a_change_of_the_users_own_models_leaves_the_pages_to_the_next_run(self):
        # This run was given no work for the user's models: one put in the second box now
        # gets its pages from the next run, which the Drive inbox starts at once.
        change = self.changing({"read1": ["gemma-4-31b-it"], "read2": ["gemini-3.1-flash-lite"], "settle": []},
                               helper={"read1": False, "read2": True, "settle": False})
        code, st, md, fake = self.run_book({
            "gemma-4-31b-it": lambda p, n: (change(n), answer(TRUE))[1],
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        }, extra=("--boxes", "read1=gemma-4-31b-it;read2=gemini-3.1-flash-lite;settle=", "--workers", "1"))
        self.assertEqual((code, st["state"]), (7, "paused"), st)
        self.assertEqual(st["waiting_for"], "time")
        self.assertNotIn("boxes_at", st, "the run did not take these boxes")
        # No page begun after the change (the second box may have been reading one meanwhile).
        reads = [m for m, k in fake.calls if k == "read"]
        self.assertEqual(reads.count("gemma-4-31b-it"), 1)
        self.assertLessEqual(reads.count("gemini-3.1-flash-lite"), 1)


    def test_a_model_this_key_cannot_use_is_said_so(self):
        gone = Exception("404 NOT_FOUND. This model models/gemini-2.5-flash is no longer available to new users.")
        code, st, md, fake = self.run_book({
            "gemini-2.5-flash": lambda p, n: (_ for _ in ()).throw(gone),
            "gemma-4-31b-it": lambda p, n: answer(TRUE),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        }, extra=("--boxes", "read1=gemini-2.5-flash,gemma-4-31b-it;read2=gemini-3.1-flash-lite;settle="))
        self.assertEqual(code, 0, st)
        self.assertEqual(st["models_missing"], ["gemini-2.5-flash"])
        self.assertEqual({r["a"]["model"] for r in self.records().values()}, {"gemma-4-31b-it"})


class Queue(PipelineBase):
    """Several books in one run (--queue, the Drive inbox's oldest unfinished books): each
    step's models take the oldest page waiting for it, in any book (2026-10-23)."""

    def other_book(self, name="other"):
        """A second book: pages 30-31 of the test book, saved as a PDF of its own."""
        import pymupdf
        src = pymupdf.open(PDF)
        doc = pymupdf.open()
        doc.insert_pdf(src, from_page=29, to_page=30)
        path = os.path.join(self.tmp, name + ".pdf")
        doc.save(path)
        out = os.path.join(self.tmp, name + "-out")
        manifest = os.path.join(self.tmp, name + ".json")
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump([{"pdf": path, "out": out, "title": "كتاب آخر", "order": "2026-10-02"}], f)
        return manifest, out

    def test_the_books_of_a_run_each_get_their_book_and_status(self):
        manifest, out = self.other_book()
        seen = []

        def reader(p, n):
            if p.startswith("أنت تنسخ"):
                seen.append(live.current_book())
            return answer(TRUE)
        code, st, md, fake = self.run_book({
            "gemma-4-31b-it": reader,
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        }, extra=("--boxes", "read1=gemma-4-31b-it;read2=gemini-3.1-flash-lite;settle=", "--workers", "1",
                  "--queue", manifest))
        self.assertEqual(code, 0, st)
        # The first reader read each page of both books once.
        self.assertEqual(sorted(seen), [0, 0, 1, 1])
        with open(os.path.join(out, "status.json"), encoding="utf-8") as f:
            other = json.load(f)
        self.assertEqual((other["state"], other["pages_done"], other["book"]), ("done", 2, "كتاب آخر"))
        self.assertTrue(os.path.exists(os.path.join(out, "other.epub")))
        self.assertTrue(os.path.exists(os.path.join(out, "report.md")))
        # The run's requests are counted once, with the first book (the other book's table of
        # contents and cover asked a model too).
        self.assertEqual(st["requests"]["gemma-4-31b-it"], 4)
        self.assertGreaterEqual(st["requests"]["gemini-3.1-flash-lite"], 4)
        self.assertNotIn("requests", other)

    def test_a_page_read_twice_is_settled_while_the_next_pages_are_read(self):
        settled = threading.Event()

        def first(p, n):
            if n == 2:
                # Before: no page was settled until every page was read.
                self.assertTrue(settled.wait(20), "page 30 was not settled while page 31 was read")
            return answer(WRONG)

        def settler(p, n):
            settled.set()
            return answer(TRUE)
        code, st, md, fake = self.run_book({
            "gemma-4-31b-it": first,
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.5-flash-lite": settler,
        }, extra=("--boxes", "read1=gemma-4-31b-it;read2=gemini-3.1-flash-lite;settle=gemini-3.5-flash-lite",
                  "--workers", "1"))
        self.assertEqual(code, 0, st)
        self.assertEqual({r["check"]["state"] for r in self.records().values()}, {"reconciled"})
        self.assertNotIn("كاقية", md)

    def test_the_second_reader_goes_on_with_the_next_book_while_the_first_waits(self):
        # The first book's pages are read once already and wait only for a second reading,
        # the other book is new: the second reader works on the first book while the first
        # reader reads the other one.
        manifest, out = self.other_book()
        self.run_book({"gemini-3.5-flash-lite": lambda p, n: answer(TRUE)},
                      extra=("--boxes", "read1=gemini-3.5-flash-lite;read2=;settle="))
        books = []

        def reading(step):
            def f(p, n):
                if p.startswith("أنت تنسخ"):
                    books.append((step, live.current_book()))
                return answer(TRUE)
            return f
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": reading("a"),
            "gemini-3.1-flash-lite": reading("b"),
        }, extra=("--boxes", "read1=gemini-3.5-flash-lite;read2=gemini-3.1-flash-lite;settle=", "--queue", manifest))
        self.assertEqual(code, 0, st)
        self.assertEqual(sorted(books), [("a", 1), ("a", 1), ("b", 0), ("b", 0), ("b", 1), ("b", 1)])
        with open(os.path.join(out, "status.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["state"], "done")

    def test_the_second_reading_box_does_not_wait_for_the_first(self):
        # The user's question (2026-10-24): the first box's model was failing, and the second
        # box's two models had nothing to do. Now each box reads the pages it has not read.
        second_read = threading.Event()

        def first(p, n):
            if p.startswith("أنت تنسخ"):
                self.assertTrue(second_read.wait(20), "the second box waited for the first")
            return answer(TRUE)

        def second(p, n):
            second_read.set()
            return answer(TRUE)
        code, st, md, fake = self.run_book({
            "gemma-4-31b-it": first,
            "gemini-3.1-flash-lite": second,
        }, extra=("--boxes", "read1=gemma-4-31b-it;read2=gemini-3.1-flash-lite;settle=", "--workers", "1"))
        self.assertEqual(code, 0, st)
        recs = self.records()
        for r in recs.values():
            # One reading from each box, whichever came first.
            self.assertEqual({c.box_of(r["a"], "a"), c.box_of(r["b"], "b")}, {"read1", "read2"}, r)
            self.assertEqual({r["a"]["model"], r["b"]["model"]}, {"gemma-4-31b-it", "gemini-3.1-flash-lite"})
        self.assertEqual(recs[31]["a"]["box"], "read2", "the second box began at the end of the book")

    def test_both_reading_boxes_may_read_one_page_at_once(self):
        # The user (2026-10-24): the boxes are separate, so nothing stops one page being read
        # by both at the same time; both readings are kept.
        both = threading.Barrier(2, timeout=20)

        def reader(p, n):
            if p.startswith("أنت تنسخ") and n == 1:
                both.wait()
            return answer(TRUE)
        code, st, md, fake = self.run_book({
            "gemma-4-31b-it": reader,
            "gemini-3.1-flash-lite": reader,
        }, pages="30", extra=("--boxes", "read1=gemma-4-31b-it;read2=gemini-3.1-flash-lite;settle=", "--workers", "1"))
        self.assertEqual(code, 0, st)
        r = self.records()[30]
        self.assertEqual({r["a"]["model"], r["b"]["model"]}, {"gemma-4-31b-it", "gemini-3.1-flash-lite"})
        self.assertEqual({c.box_of(r["a"], "a"), c.box_of(r["b"], "b")}, {"read1", "read2"})


class Needs(unittest.TestCase):
    def test_each_box_says_which_models_could_take_its_pages(self):
        pool = c.ModelPool("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemma-4-26b-a4b-it", 10)
        pool.set_boxes({"read1": ["gemma-4-31b-it"], "read2": ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
                        "settle": []})
        ok = {"status": "ok", "text": "x"}
        recs = [{"page": 1, "status": "pending"},
                {"page": 2, "status": "ok", "a": dict(ok, model="gemma-4-31b-it")},
                {"page": 3, "status": "ok", "a": dict(ok, model="gemini-3.5-flash-lite", box="read2")},
                {"page": 4, "status": "ok", "a": dict(ok, model="gemma-4-31b-it"), "b": dict(ok, model="gemini-3.1-flash-lite")}]
        n = c.book_needs(pool, recs, True)
        lites = "gemini-3.1-flash-lite,gemini-3.5-flash-lite"
        self.assertEqual(n["read1"], {"gemma-4-31b-it": 2}, "page 1, and page 3 read by the second box")
        self.assertEqual(n["read2"], {lites: 2})
        self.assertEqual(n["settle"], {}, "an empty settling box: nobody")


class StepQueues(unittest.TestCase):
    def test_oldest_book_first_and_a_page_at_work_is_never_queued_twice(self):
        steps = c.Steps()
        steps.put("b", (1, 1))
        steps.put("b", (0, 9))
        self.assertEqual(steps.take("b"), (0, 9), "the oldest book's page first")
        steps.put("a", (0, 5))
        item = steps.take("a")
        steps.tried.clear()   # what a change of the boxes does (rescan)
        self.assertFalse(steps.put("a", (0, 5)), "being read now: not queued again")
        steps.done("a", item)
        self.assertTrue(steps.put("a", (0, 5)))

    def test_the_second_box_takes_unread_pages_from_the_end_and_a_page_read_once_moves_up(self):
        steps = c.Steps()
        for p in (1, 2, 3):
            steps.put("b", (0, p), back=True)
        steps.put("b", (1, 7))   # read once already: waits for its second reading
        self.assertEqual(steps.take("b"), (1, 7))
        self.assertEqual(steps.take("b"), (0, 3), "unread pages last page first")
        self.assertTrue(steps.put("b", (0, 1)), "page 1 was read once meanwhile: it moves up")
        self.assertFalse(steps.put("b", (0, 1), back=True))
        self.assertEqual([steps.take("b"), steps.take("b")], [(0, 1), (0, 2)])


class Requests:
    """The status page's requests file during a run (live.py), as the test writes it."""

    def __init__(self):
        self.data = {}

    def read(self):
        return self.data


def asked_models(fake):
    return {m for m, k in fake.calls if k != "cover"}


class DashLines(unittest.TestCase):
    def test_dash_line_never_reaches_pandoc_as_yaml_or_heading(self):
        # Book 2_k2opt (run 38): "---" right before a footnote made pandoc fail.
        recs = [{"page": 1, "status": "ok", "text": "نص الصفحة\n---\nStanford (W.B.), op. cit., p. 10"}]
        md = c.assemble(recs)
        self.assertNotRegex(md, r"(?m)^-{3,}\s*$")
        self.assertIn("\n\n* * *\n\n", md)
        self.assertEqual(c.safe_rules("a -- b\n- item"), "a -- b\n- item")


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
