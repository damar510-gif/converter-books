"""Offline tests for the exchange with the helper on the user's computer: a fake Drive
folder stands in for the book's folder, and the fake Gemini client of test_converter for
the API (docs/helper-boxes-plan.md).

Run from the repository root:  python -m unittest discover -s tests
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import converter as c  # noqa: E402
import helperbox  # noqa: E402
from test_converter import PDF, TRUE, WRONG, PipelineBase, answer  # noqa: E402

FOLDER = "folder_1234567890"
CHIP = "agy|gemini-3.8-flash"
OTHER = "grok|grok-4.7"


class FakeDrive:
    """The book's folder: name -> {"id", "modifiedTime", "data"}."""

    def __init__(self):
        self.files = {}
        self.n = 0
        self.gets = []
        self.dead = False
        self.started = helperbox.time.time()

    def put(self, name, data):
        self.n += 1
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        old = self.files.get(name)
        self.files[name] = {"id": old["id"] if old else f"id{self.n}", "modifiedTime": f"2026-10-20T10:{self.n:02d}:00Z",
                            "data": body}

    def json(self, name):
        return json.loads(self.files[name]["data"].decode("utf-8"))

    def listing(self, folder):
        assert folder == FOLDER
        return {n: {"id": f["id"], "modifiedTime": f["modifiedTime"]} for n, f in self.files.items()}

    def get(self, file_id):
        self.gets.append(file_id)
        return next(f["data"] for f in self.files.values() if f["id"] == file_id)

    def create(self, folder, name, data, mime):
        assert folder == FOLDER
        self.put(name, data)
        return self.files[name]["id"]

    def update(self, file_id, data, mime):
        name = next(n for n, f in self.files.items() if f["id"] == file_id)
        self.put(name, data)


def asked(fake):
    """The requests a run made, leaving out the one that picks the cover."""
    return [k for _, k in fake.calls if k != "cover"]


class HelperPipeline(PipelineBase):
    def setUp(self):
        super().setUp()
        self.drive = FakeDrive()
        self._drive_class = helperbox.Drive
        helperbox.Drive = lambda token: self.drive
        os.environ["DRIVE_TOKEN"] = "drive-token"
        self.addCleanup(self.restore_drive)

    def restore_drive(self):
        helperbox.Drive = self._drive_class
        os.environ.pop("DRIVE_TOKEN", None)

    def run_helper(self, behaviour, mode="transcribe", pages="30-31", extra=()):
        return self.run_book(behaviour, pages=pages, extra=("--helper", f"{FOLDER}:{mode}", *extra))

    def helper_files(self, page, b=None, final=None, a_text=None, ha=None, model=CHIP):
        """What the helper writes for one page, made on the page's current first reading."""
        a = ha if ha is not None else (a_text if a_text is not None else self.drive.json(f"p{page:04d}.a.json")["text"])
        if ha is not None:
            self.drive.put(f"p{page:04d}.ha.json", {"page": page, "model": OTHER, "text": ha})
        if b is not None:
            self.drive.put(f"p{page:04d}.hb.json", {"page": page, "model": model, "text": b})
        if final is not None:
            self.drive.put(f"p{page:04d}.final.json", {"page": page, "model": model, "text": final,
                                                        "a_sha": helperbox.sha(a), "b_sha": helperbox.sha(b)})

    def put_record(self, page, text, model="gemini-3.5-flash-lite"):
        """A page the free models read once before this design (the book of 2026-10-21): the
        helper's models give its second reading and settle it."""
        d = os.path.join(self.tmp, "work", f"{c.file_sha(PDF)}-{c.CACHE_VERSION}")
        os.makedirs(d, exist_ok=True)
        c.save_record(d, {"page": page, "status": "ok", "ink": 0.05, "spread": False, "text": text,
                          "a": {"model": model, "text": text, "finish": "STOP", "via": "full", "status": "ok"}})

    def test_the_free_models_take_the_end_of_the_book_and_the_helpers_models_its_start(self):
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(TRUE if "النسخة (أ)" in p else WRONG),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
                "gemini-3.8-flash": lambda p, n: answer(TRUE)}
        code, st, md, fake = self.run_helper(free, extra=("--extra-models", "gemini-3.8-flash"))
        self.assertEqual(code, 5)
        self.assertEqual((st["state"], st["waiting_for"]), ("paused", "helper"))
        # Page 31 (the later half): read by the two Lite readers, settled by Flash; page 30
        # not read at all: it is the helper's models'.
        self.assertEqual(st["pages_helper"], [30])
        self.assertEqual((st["free_done"], st["free_left"]), (1, False))
        self.assertEqual([(m, k) for m, k in fake.calls if k != "cover"],
                         [("gemini-3.5-flash-lite", "read"), ("gemini-3.1-flash-lite", "read"), ("gemini-3.8-flash", "reconcile")])
        rec = self.records()[31]
        self.assertEqual((rec["check"]["state"], rec["check"]["model"]), ("reconciled", "gemini-3.8-flash"))
        self.assertNotIn(30, self.records())
        book = self.drive.json("book.json")
        self.assertEqual((book["need"], book["need_a"], book["lite_out"]), ([30], [30], False))
        # Both pages have their image, so either side can take any page later.
        self.assertEqual(self.drive.files["p0030.jpg"]["data"][:2], b"\xff\xd8")
        self.assertIn("p0031.jpg", self.drive.files)
        self.assertNotIn("p0031.a.json", self.drive.files)
        # A second run asks nothing more of the free models and sends no image again.
        sent = dict(self.drive.files)
        code, st, md, fake = self.run_helper(free, extra=("--extra-models", "gemini-3.8-flash"))
        self.assertEqual((code, asked(fake)), (5, []))
        self.assertEqual(self.drive.files["p0030.jpg"]["modifiedTime"], sent["p0030.jpg"]["modifiedTime"])

    def test_lite_never_settles_while_the_helpers_models_are_on(self):
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
        code, st, md, fake = self.run_helper(free)
        self.assertEqual(code, 5)
        self.assertNotIn("reconcile", asked(fake))
        self.assertEqual(st["pages_helper"], [30, 31])
        # Both free readings of page 31 go to the helper's models, which settle on them.
        self.assertEqual(self.drive.json("p0031.a.json")["text"], WRONG.strip())
        self.assertEqual(self.drive.json("p0031.b.json")["model"], "gemini-3.1-flash-lite")
        self.drive.put("p0031.final.json", {"page": 31, "model": CHIP, "text": TRUE,
                                            "a_sha": helperbox.sha(WRONG.strip()), "b_sha": helperbox.sha(TRUE.strip())})
        code, st, md, fake = self.run_helper(free)
        rec = self.records()[31]
        self.assertEqual((rec["b"]["model"], rec["check"]["state"], rec["check"]["model"]),
                         ("gemini-3.1-flash-lite", "reconciled", CHIP))
        self.assertNotIn("reconcile", asked(fake))
        # With the helper's models off, Lite settles again.
        code, st, md, fake = self.run_helper(free, mode="import")
        self.assertIn("reconcile", asked(fake))

    def test_the_free_models_go_on_right_away_while_they_have_quota(self):
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
        code, st, md, fake = self.run_helper(free, pages="28-31")
        self.assertEqual(code, 5)
        # The later half (31, then 30) is settled; half of what is left (29) is theirs next.
        self.assertEqual((st["pages_helper"], st["free_done"], st["free_left"]), ([28, 29], 2, True))
        self.assertEqual(self.drive.json("book.json")["need"], [28, 29])

    def test_no_page_starts_after_the_free_models_time_is_up(self):
        self.addCleanup(setattr, c, "FREE_PASS_SECONDS", c.FREE_PASS_SECONDS)
        c.FREE_PASS_SECONDS = -1
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
        code, st, md, fake = self.run_helper(free, pages="28-31")
        self.assertEqual((code, asked(fake)), (5, []))
        self.assertEqual((st["pages_helper"], st["free_left"]), ([28, 29, 30, 31], False))
        # Nothing stays held back from the helper's models once the run ends.
        self.assertEqual(self.drive.json("book.json")["need"], [28, 29, 30, 31])

    def test_the_free_models_use_a_second_reading_the_helpers_models_gave(self):
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
                "gemini-3.8-flash": lambda p, n: answer(TRUE)}
        self.drive.put("p0031.hb.json", {"page": 31, "model": CHIP, "text": TRUE})
        code, st, md, fake = self.run_helper(free, extra=("--extra-models", "gemini-3.8-flash"))
        rec = self.records()[31]
        self.assertEqual((rec["a"]["model"], rec["b"]["model"]), ("gemini-3.5-flash-lite", CHIP))
        self.assertEqual(rec["check"]["state"], "reconciled")
        # One free reading only; the page is settled once.
        self.assertEqual([(m, k) for m, k in fake.calls if k == "read"], [("gemini-3.5-flash-lite", "read")])
        self.assertEqual(asked(fake).count("reconcile"), 1)
        self.assertNotIn("كاقية", md)

    def test_the_helpers_settled_text_is_checked_and_used(self):
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
        for page in (30, 31):
            self.put_record(page, WRONG)
            self.helper_files(page, b=TRUE, final=TRUE, a_text=WRONG)
        code, st, md, fake = self.run_helper(free)
        self.assertEqual(code, 0, st)
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["helper_checked"], 2)
        recs = self.records()
        self.assertEqual({r["check"]["state"] for r in recs.values()}, {"reconciled"})
        self.assertEqual(recs[30]["check"]["model"], CHIP)
        self.assertEqual(recs[30]["b"]["model"], CHIP)
        self.assertNotIn("كاقية", md)
        # Gemini read nothing again and settled nothing.
        self.assertEqual(asked(fake), [])
        self.assertEqual(self.drive.json("book.json")["need"], [])

    def test_the_helpers_own_two_readings_count_when_the_free_reading_reached_it_late(self):
        """A run whose Drive token ran out sent its free readings only with the next run;
        meanwhile the helper read the page twice itself and settled it: that is kept."""
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
        self.put_record(30, WRONG)
        self.helper_files(30, ha=TRUE, b=TRUE, final=TRUE)
        code, st, md, fake = self.run_helper(free, pages="30")
        self.assertEqual(code, 0, st)
        self.assertEqual(st["helper_checked"], 1)
        rec = self.records()[30]
        self.assertEqual((rec["a"]["model"], rec["b"]["model"]), (OTHER, CHIP))
        self.assertIn(rec["check"]["state"], ("agreed", "reconciled"))
        self.assertNotIn("كاقية", md)
        self.assertEqual(asked(fake), [])

    def test_a_page_every_model_refused_to_settle_goes_back_to_gemini(self):
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
        self.put_record(30, WRONG)
        self.run_helper(free, pages="30")
        self.helper_files(30, b=TRUE)
        self.drive.put("p0030.skip.json", {"page": 30, "reason": "refused", "models": [CHIP, OTHER]})
        code, st, md, fake = self.run_helper(free, pages="30")
        self.assertNotIn(30, st.get("pages_helper", []), st)
        self.assertIn("reconcile", [k for _, k in fake.calls], "Gemini settles it")

    def test_agreeing_readings_need_no_settled_text(self):
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
        self.put_record(30, TRUE)
        self.run_helper(free, pages="30")
        self.helper_files(30, b=TRUE.replace("الاجتماعى", "الاجتماعي"))
        code, st, md, fake = self.run_helper(free, pages="30")
        self.assertEqual(code, 0)
        self.assertEqual(self.records()[30]["check"]["state"], "agreed")

    def test_settled_text_made_on_other_readings_is_not_used(self):
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
        self.put_record(30, WRONG)
        self.run_helper(free, pages="30")
        self.helper_files(30, b=TRUE, final=TRUE, a_text="نص آخر")
        code, st, md, fake = self.run_helper(free, pages="30")
        self.assertEqual(code, 5)
        self.assertEqual(st["pages_helper"], [30])
        self.assertEqual(fake.calls, [])

    def test_a_bad_settled_text_leaves_the_page_unsure(self):
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
        self.put_record(30, WRONG)
        self.run_helper(free, pages="30")
        self.helper_files(30, b=TRUE, final="كلام قصير")
        code, st, md, fake = self.run_helper(free, pages="30")
        self.assertEqual(code, 0)
        rec = self.records()[30]
        self.assertEqual(rec["check"]["state"], "unsure")
        self.assertIn(CHIP, rec["check"]["rejected"])
        self.assertEqual(fake.calls, [])

    def test_with_the_helper_off_gemini_settles_what_its_models_read(self):
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(TRUE if "النسخة (أ)" in p else WRONG),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
        self.put_record(30, WRONG)
        self.run_helper(free, pages="30")
        self.helper_files(30, b=TRUE)
        code, st, md, fake = self.run_helper(free, mode="import", pages="30")
        self.assertEqual(code, 0, st)
        rec = self.records()[30]
        self.assertEqual((rec["check"]["state"], rec["b"]["model"]), ("reconciled", CHIP))
        self.assertEqual([k for _, k in fake.calls], ["reconcile"])
        # The helper is told to stop working on the book.
        self.assertEqual(self.drive.json("book.json")["need"], [])

    def test_a_page_handed_back_goes_the_usual_way(self):
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
        self.run_helper(free, pages="30")
        self.drive.put("p0030.skip.json", {"page": 30, "reason": "refused", "models": [CHIP]})
        code, st, md, fake = self.run_helper(free, pages="30")
        self.assertEqual(code, 0, st)
        rec = self.records()[30]
        self.assertEqual((rec["check"]["state"], rec["b"]["model"]), ("agreed", "gemini-3.1-flash-lite"))

    def test_when_the_free_quota_is_out_two_helper_models_do_it_all(self):
        def gone(p, n):
            raise Exception("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        code, st, md, fake = self.run_helper({"gemini-3.5-flash-lite": gone, "gemini-3.1-flash-lite": gone})
        self.assertEqual(code, 5)
        self.assertEqual((st["pages_helper"], st["free_left"]), ([30, 31], False))
        book = self.drive.json("book.json")
        self.assertEqual((book["need"], book["need_a"], book["lite_out"]), ([30, 31], [30, 31], True))
        for page in (30, 31):
            self.helper_files(page, ha=WRONG, b=TRUE, final=TRUE)
        code, st, md, fake = self.run_helper({"gemini-3.5-flash-lite": gone, "gemini-3.1-flash-lite": gone})
        self.assertEqual(code, 0, st)
        rec = self.records()[30]
        self.assertEqual((rec["a"]["model"], rec["b"]["model"], rec["check"]["state"]), (OTHER, CHIP, "reconciled"))
        self.assertNotIn("كاقية", md)


class Merge(unittest.TestCase):
    def test_settled_and_pinned_pages_are_left_alone(self):
        outs = {"hb": {"model": CHIP, "text": TRUE}}
        done = {"page": 3, "status": "ok", "a": {"model": "m", "text": WRONG}, "check": {"v": c.CHECK_VERSION, "state": "agreed"}}
        self.assertEqual(helperbox.merge(done, outs, 3, c.CHECK_VERSION), (done, False))
        pinned = {"page": 3, "status": "ok", "a": {"model": "m", "text": WRONG}, "check": {"human": {"ids": []}}}
        self.assertEqual(helperbox.merge(pinned, outs, 3, c.CHECK_VERSION), (pinned, False))

    def test_the_same_model_never_gives_both_readings(self):
        rec = {"page": 3, "status": "ok", "text": WRONG, "a": {"model": CHIP, "text": WRONG, "via": "helper"}}
        got, changed = helperbox.merge(rec, {"hb": {"model": CHIP, "text": TRUE}}, 3, c.CHECK_VERSION)
        self.assertFalse(changed)
        self.assertNotIn("b", got)

    def test_settled_text_is_kept_only_while_it_matches(self):
        rec = {"page": 3, "status": "ok", "text": WRONG, "a": {"model": "m", "text": WRONG}}
        outs = {"hb": {"model": CHIP, "text": TRUE},
                "final": {"model": CHIP, "text": TRUE, "a_sha": helperbox.sha(WRONG), "b_sha": helperbox.sha(TRUE)}}
        got, changed = helperbox.merge(rec, outs, 3, c.CHECK_VERSION)
        self.assertTrue(changed)
        self.assertEqual(got["hfinal"]["text"], TRUE)
        got["a"]["text"] = "قراءة جديدة"
        got, changed = helperbox.merge(got, outs, 3, c.CHECK_VERSION)
        self.assertTrue(changed)
        self.assertNotIn("hfinal", got)


    def test_the_helpers_own_pair_is_taken_over_a_free_reading_it_never_got(self):
        rec = {"page": 3, "status": "ok", "text": WRONG, "ink": 0.1, "spread": False,
               "a": {"model": "gemini-3.5-flash-lite", "text": WRONG}}
        outs = {"ha": {"model": OTHER, "text": TRUE + "\n"}, "hb": {"model": CHIP, "text": TRUE},
                "final": {"model": CHIP, "text": TRUE, "a_sha": helperbox.sha(TRUE + "\n"), "b_sha": helperbox.sha(TRUE)}}
        got, changed = helperbox.merge(dict(rec), outs, 3, c.CHECK_VERSION)
        self.assertTrue(changed)
        self.assertEqual((got["a"]["model"], got["b"]["model"], got["hfinal"]["text"], got["spread"]),
                         (OTHER, CHIP, TRUE, False))
        self.assertEqual(helperbox.merge(got, outs, 3, c.CHECK_VERSION), (got, False), "taken once")
        # Both readings by one model are not a pair: the free reading stays.
        outs["ha"]["model"] = CHIP
        got, _ = helperbox.merge(dict(rec), outs, 3, c.CHECK_VERSION)
        self.assertEqual(got["a"]["model"], "gemini-3.5-flash-lite")
        self.assertNotIn("hfinal", got)


class ToolTalk(unittest.TestCase):
    """What a tool on the user's computer wrote about its task (book 1_k2opt, 2026-10-22)."""

    def test_the_words_before_the_page_go_and_an_apology_is_no_reading(self):
        self.assertEqual(helperbox.clean_text("I'll read `task.md` and follow its instructions." + TRUE), TRUE)
        self.assertEqual(helperbox.clean_text("Here is the text:\n\n# مقدمة\nنص"), "# مقدمة\nنص")
        self.assertEqual(helperbox.clean_text("# مقدمة\nنص"), "# مقدمة\nنص")
        self.assertFalse(helperbox.usable("لم أتمكن من قراءة محتوى `task.md` لأن النص ظهر بترميز غير سليم."))
        self.assertTrue(helperbox.usable(TRUE))
        rec = {"page": 3, "status": "ok", "text": WRONG, "a": {"model": "m", "text": WRONG}}
        apology = {"hb": {"model": "codex|gpt-6-luna", "text": "I can’t read `task.md` because its contents are corrupted."}}
        self.assertEqual(helperbox.merge(dict(rec), apology, 3, c.CHECK_VERSION)[0].get("b"), None)
        got, _ = helperbox.merge(dict(rec), {"hb": {"model": CHIP, "text": "I'll read `task.md`." + TRUE}}, 3, c.CHECK_VERSION)
        self.assertEqual(got["b"]["text"], TRUE.strip())

    def test_a_saved_page_is_cleaned_and_an_apology_read_again(self):
        talk = "I'll read `task.md` and follow its instructions."
        settled = {"page": 7, "status": "ok", "text": talk + TRUE, "a": {"model": "m", "text": TRUE},
                   "b": {"model": OTHER, "text": talk + TRUE, "via": "helper", "status": "ok"},
                   "check": {"v": c.CHECK_VERSION, "state": "reconciled", "model": OTHER}}
        self.assertTrue(c.scrub(settled, True))
        self.assertEqual((settled["text"], settled["b"]["text"], settled["check"]["state"]), (TRUE, TRUE, "reconciled"))
        apology = {"page": 13, "status": "ok", "text": TRUE, "a": {"model": "m", "text": TRUE},
                   "b": {"model": "codex|gpt-6-luna", "text": "I can’t read `task.md`.", "via": "helper", "status": "ok"},
                   "check": {"v": c.CHECK_VERSION, "state": "reconciled", "model": CHIP}}
        kept = json.loads(json.dumps(apology))
        self.assertFalse(c.scrub(kept, False))
        self.assertIn("b", kept, "a review round reads nothing, so it drops nothing")
        self.assertTrue(c.scrub(apology, True))
        self.assertNotIn("b", apology)
        self.assertNotIn("check", apology)


class Fetch(unittest.TestCase):
    def test_files_already_downloaded_are_not_downloaded_again(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        drive = FakeDrive()
        drive.put("p0001.hb.json", {"model": CHIP, "text": TRUE})
        drive.put("p0001.jpg", b"\xff\xd8")
        box = helperbox.HelperBox(FOLDER, True, drive, tmp)
        self.assertEqual(box.fetch(), {1: {"hb": {"model": CHIP, "text": TRUE}}})
        self.assertEqual(len(drive.gets), 1)
        self.assertEqual(box.fetch()[1]["hb"]["text"], TRUE)
        self.assertEqual(len(drive.gets), 1)
        drive.put("p0001.hb.json", {"model": CHIP, "text": WRONG})
        self.assertEqual(box.fetch()[1]["hb"]["text"], WRONG)
        self.assertEqual(len(drive.gets), 2)


if __name__ == "__main__":
    unittest.main()
