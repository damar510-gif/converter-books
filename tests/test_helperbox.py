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
from test_converter import TRUE, WRONG, PipelineBase, answer  # noqa: E402

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

    def test_free_model_reads_once_and_the_book_waits_for_the_helper(self):
        code, st, md, fake = self.run_helper({
            "gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        })
        self.assertEqual(code, 5)
        self.assertEqual((st["state"], st["waiting_for"]), ("paused", "helper"))
        self.assertEqual(st["pages_helper"], [30, 31])
        # One reading per page, by the main free model only: no second reading, no settling.
        self.assertEqual([k for _, k in fake.calls].count("read"), 2)
        self.assertEqual({m for m, _ in fake.calls}, {"gemini-3.5-flash-lite"})
        book = self.drive.json("book.json")
        self.assertEqual((book["need"], book["need_a"], book["lite_out"]), ([30, 31], [], False))
        self.assertIn("p0030.jpg", self.drive.files)
        self.assertEqual(self.drive.files["p0031.jpg"]["data"][:2], b"\xff\xd8")
        a = self.drive.json("p0030.a.json")
        self.assertEqual((a["model"], a["text"]), ("gemini-3.5-flash-lite", WRONG.strip()))
        # A second run does not send them again.
        sent = dict(self.drive.files)
        self.run_helper({"gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
                         "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)})
        self.assertEqual(self.drive.files["p0030.a.json"]["modifiedTime"], sent["p0030.a.json"]["modifiedTime"])
        self.assertEqual(self.drive.files["p0030.jpg"]["modifiedTime"], sent["p0030.jpg"]["modifiedTime"])

    def test_the_helpers_settled_text_is_checked_and_used(self):
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
        self.run_helper(free)
        for page in (30, 31):
            self.helper_files(page, b=TRUE, final=TRUE)
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
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.drive.json("book.json")["need"], [])

    def test_the_helpers_own_two_readings_count_when_the_free_reading_reached_it_late(self):
        """A run whose Drive token ran out sent its free readings only with the next run;
        meanwhile the helper read the page twice itself and settled it: that is kept."""
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
        self.run_helper(free, pages="30")
        self.helper_files(30, ha=TRUE, b=TRUE, final=TRUE)
        code, st, md, fake = self.run_helper(free, pages="30")
        self.assertEqual(code, 0, st)
        self.assertEqual(st["helper_checked"], 1)
        rec = self.records()[30]
        self.assertEqual((rec["a"]["model"], rec["b"]["model"]), (OTHER, CHIP))
        self.assertIn(rec["check"]["state"], ("agreed", "reconciled"))
        self.assertNotIn("كاقية", md)
        self.assertEqual(fake.calls, [])

    def test_a_page_every_model_refused_to_settle_goes_back_to_gemini(self):
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
        self.run_helper(free, pages="30")
        self.helper_files(30, b=TRUE)
        self.drive.put("p0030.skip.json", {"page": 30, "reason": "refused", "models": [CHIP, OTHER]})
        code, st, md, fake = self.run_helper(free, pages="30")
        self.assertNotIn(30, st.get("pages_helper", []), st)
        self.assertIn("reconcile", [k for _, k in fake.calls], "Gemini settles it")

    def test_agreeing_readings_need_no_settled_text(self):
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
        self.run_helper(free, pages="30")
        self.helper_files(30, b=TRUE.replace("الاجتماعى", "الاجتماعي"))
        code, st, md, fake = self.run_helper(free, pages="30")
        self.assertEqual(code, 0)
        self.assertEqual(self.records()[30]["check"]["state"], "agreed")

    def test_settled_text_made_on_other_readings_is_not_used(self):
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
        self.run_helper(free, pages="30")
        self.helper_files(30, b=TRUE, final=TRUE, a_text="نص آخر")
        code, st, md, fake = self.run_helper(free, pages="30")
        self.assertEqual(code, 5)
        self.assertEqual(st["pages_helper"], [30])
        self.assertEqual(fake.calls, [])

    def test_a_bad_settled_text_leaves_the_page_unsure(self):
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
        self.run_helper(free, pages="30", extra=("--arbiter-models", ""))
        self.helper_files(30, b=TRUE, final="كلام قصير")
        code, st, md, fake = self.run_helper(free, pages="30", extra=("--arbiter-models", ""))
        self.assertEqual(code, 0)
        rec = self.records()[30]
        self.assertEqual(rec["check"]["state"], "unsure")
        self.assertIn(CHIP, rec["check"]["rejected"])
        self.assertEqual(fake.calls, [])

    def test_with_the_helper_off_gemini_settles_what_its_models_read(self):
        free = {"gemini-3.5-flash-lite": lambda p, n: answer(TRUE if "النسخة (أ)" in p else WRONG),
                "gemini-3.1-flash-lite": lambda p, n: answer(TRUE)}
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
        code, st, md, fake = self.run_helper({"gemini-3.5-flash-lite": gone, "gemini-3.1-flash-lite": gone},
                                             pages="30")
        self.assertEqual(code, 3)
        book = self.drive.json("book.json")
        self.assertEqual((book["need"], book["need_a"], book["lite_out"]), ([30], [30], True))
        self.helper_files(30, ha=WRONG, b=TRUE, final=TRUE)
        code, st, md, fake = self.run_helper({"gemini-3.5-flash-lite": gone, "gemini-3.1-flash-lite": gone},
                                             pages="30")
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
