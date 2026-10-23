"""Offline tests for the status page's live view (live.py) and how the converter feeds it:
a fake Drive file stands in for the user's, and the fake Gemini client of test_converter for
the API (docs/drive-inbox.md, "Live view").

Run from the repository root:  python -m unittest discover -s tests
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import converter as c  # noqa: E402
import live  # noqa: E402
from test_converter import TRUE, WRONG, PipelineBase, answer  # noqa: E402

LIVE_ID = "live_file_1234567890"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeSink:
    """The live file: what the Drive inbox script left in it, and every write."""

    def __init__(self, start=None):
        self.start = start or {}
        self.writes = []
        self.dead = False

    def read(self):
        return dict(self.start)

    def write(self, data):
        self.writes.append(json.loads(data.decode("utf-8")))
        return True

    @property
    def last(self):
        return self.writes[-1]


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Opener:
    """urllib's urlopen for the Drive calls: records them, answers from a script."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, req, timeout=None):
        self.calls.append({"method": req.get_method(), "url": req.full_url, "auth": req.get_header("Authorization"),
                           "body": req.data})
        for key, value in self.answers.items():
            if key in req.full_url and (req.get_method() == "GET") == ("alt=media" in key or "tokeninfo" in key):
                if isinstance(value, int):
                    raise urllib.error.HTTPError(req.full_url, value, "error", {}, io.BytesIO(b"{}"))
                return Response(value if isinstance(value, bytes) else json.dumps(value).encode("utf-8"))
        return Response(b"{}")


class LiveState(unittest.TestCase):
    def test_requests_waiting_in_flight_and_answered(self):
        clock = Clock()
        lv = live.Live(FakeSink(), clock=clock)
        lv.role("gemini-3.5-flash-lite", "read1")
        h1 = lv.begin("gemini-3.5-flash-lite", 12, "a")
        h2 = lv.begin("gemini-3.5-flash-lite", 13, "a")
        m = lv.snapshot()["models"]["gemini-3.5-flash-lite"]
        self.assertEqual((m["queued"], m["busy"], m["role"]), (2, [], "read1"))
        lv.sent(h1)
        snap = lv.snapshot()
        self.assertEqual(snap["models"]["gemini-3.5-flash-lite"]["queued"], 1)
        self.assertEqual(snap["models"]["gemini-3.5-flash-lite"]["busy"], [[12, "a", int(clock.t)]])
        self.assertEqual(snap["pages"]["now"], [12])
        lv.done(h1, True)
        lv.done(h2, False)  # failed while waiting for its turn
        m = lv.snapshot()["models"]["gemini-3.5-flash-lite"]
        self.assertEqual((m["queued"], m["busy"], m["ok"]), (0, [], 1))
        self.assertEqual(lv.snapshot()["pages"]["now"], [])

    def test_a_slow_down_a_day_out_and_the_feed(self):
        clock = Clock()
        lv = live.Live(FakeSink(), clock=clock)
        lv.note("gemini-3.1-flash-lite", "rest", 7, "b", secs=40)
        lv.note("gemini-3.1-flash-lite", "ok", 8, "b", secs=3.24, n=1800)
        lv.note("gemini-3.8-flash", "out", 9, "settle", n=20)
        lv.note("agy|gemini-3.8-flash", "settled", 10, "settle", extra="helper")
        snap = lv.snapshot()
        lite = snap["models"]["gemini-3.1-flash-lite"]
        self.assertEqual((lite["rl"], lite["limit"], lite["out"]), (1, 500, False))
        self.assertEqual(lite["rest"], live.iso(clock.t + 40))
        self.assertEqual(lite["last"], [["ok", 8, "b", 3.2, int(clock.t)]])
        flash = snap["models"]["gemini-3.8-flash"]
        self.assertEqual((flash["out"], flash["limit"]), (True, 20))
        # The user's own models are in the feed, not among the free models.
        self.assertNotIn("agy|gemini-3.8-flash", snap["models"])
        self.assertEqual([e["r"] for e in snap["events"]], ["rest", "ok", "out", "settled"])
        self.assertEqual(snap["events"][1], {"i": 2, "t": int(clock.t), "m": "gemini-3.1-flash-lite", "p": 8,
                                             "k": "b", "r": "ok", "s": 3.2, "n": 1800})
        clock.t += 41
        self.assertEqual(lv.snapshot()["models"]["gemini-3.1-flash-lite"]["rest"], "")

    def test_the_feed_keeps_the_latest_events(self):
        lv = live.Live(FakeSink(), clock=Clock())
        for i in range(live.EVENTS + 25):
            lv.note("m", "ok", i + 1, "a")
        events = lv.snapshot()["events"]
        self.assertEqual(len(events), live.EVENTS)
        self.assertEqual(events[-1]["i"], live.EVENTS + 25)

    def test_pages_phase_and_end(self):
        lv = live.Live(FakeSink(), clock=Clock())
        for p, st in ((1, "done"), (2, "read2"), (3, "read2"), (4, "unread"), (5, "unsure")):
            lv.page(p, st)
        lv.set_phase("read", pages=3)
        lv.set_stops(read=1_800_000_100, settle=1_800_000_200)
        snap = lv.snapshot()
        self.assertEqual(snap["pages"], {"total": 5, "done": 1, "unsure": 1, "read2": 2, "read1": 0, "unread": 1,
                                         "failed": 0, "helper": 0, "now": []})
        self.assertEqual((snap["phase"], snap["info"]), ("read", {"pages": 3}))
        self.assertEqual(snap["stops"], {"read": live.iso(1_800_000_100), "settle": live.iso(1_800_000_200)})
        self.assertEqual(snap["events"][-1]["r"], "phase")
        lv.finish("paused", waiting=1)
        self.assertEqual(lv.sink.last["end"], {"state": "paused", "waiting": 1})
        self.assertEqual(lv.sink.last["phase"], "end")

    def test_writes_in_the_background_keep_what_the_script_wrote(self):
        sink = FakeSink({"file": "book123", "book": "كتاب", "dispatched": "2026-10-23T07:00:00Z", "phase": "queued"})
        lv = live.Live(sink, every=0.05)
        lv.start()
        lv.note("m", "ok", 1, "a")
        for _ in range(100):
            if any(w["events"] for w in sink.writes):
                break
            live.time.sleep(0.02)
        lv.stop()
        self.assertGreaterEqual(len(sink.writes), 2)
        self.assertEqual(sink.last["file"], "book123")
        self.assertEqual(sink.last["book"], "كتاب")
        self.assertEqual(sink.last["dispatched"], "2026-10-23T07:00:00Z")
        self.assertNotEqual(sink.last["phase"], "queued")
        self.assertEqual([w["seq"] for w in sink.writes], sorted({w["seq"] for w in sink.writes}))

    def test_without_a_file_nothing_is_written(self):
        lv = live.open_live("", "token")
        self.assertFalse(lv.on)
        lv.start()
        lv.note("m", "ok", 1, "a")
        lv.finish("done")
        self.assertFalse(lv.flush(force=True))
        self.assertFalse(live.open_live("bad id!", "token").on)
        self.assertFalse(live.open_live(LIVE_ID, "").on)


class Retries(unittest.TestCase):
    """«أعد المحاولة الآن» on the status page while a run goes: the requests file (Live.watch)."""

    def test_only_requests_made_after_the_dispatch_count_and_each_once(self):
        lv = live.Live(FakeSink(), clock=Clock())
        lv.base = {"dispatched": "2026-10-23T07:00:00.000Z"}
        requests = {"retry": {"gemini-3.5-flash-lite": "2026-10-23T06:59:00.000Z",
                              "gemini-3.1-flash-lite": "2026-10-23T07:05:00.000Z",
                              "rm -rf /": "2026-10-23T07:05:00.000Z"}}
        lv.control = FakeSink(requests)
        back = []
        lv.watch(back.append)
        lv.check_control()
        lv.check_control()
        self.assertEqual(back, ["gemini-3.1-flash-lite"])
        requests["retry"]["gemini-3.5-flash-lite"] = "2026-10-23T07:10:00.000Z"
        lv.control = FakeSink(requests)
        lv.check_control()
        self.assertEqual(back, ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"])

    def test_boxes_changed_after_the_dispatch_are_taken_once(self):
        lv = live.Live(FakeSink(), clock=Clock())
        lv.base = {"dispatched": "2026-10-23T07:00:00.000Z"}
        got = []
        lv.watch(None, lambda free, helper: got.append((free, helper)) or True)
        boxes = {"read1": ["gemma-4-31b-it", "rm -rf /"], "read2": [], "settle": "x", "review": ["gemma-4-31b-it"]}
        lv.control = FakeSink({"boxes": boxes, "helper": {"read2": 1}, "boxes_at": "2026-10-23T06:59:00.000Z"})
        lv.check_control()
        self.assertEqual(got, [], "the run was started with these boxes")
        lv.control = FakeSink({"boxes": boxes, "helper": {"read2": 1}, "boxes_at": "2026-10-23T07:05:00.000Z"})
        lv.check_control()
        lv.check_control()
        self.assertEqual(got, [({"read1": ["gemma-4-31b-it"], "read2": []},
                                {"read1": False, "read2": True, "settle": False})])
        self.assertEqual(lv.boxes_at, "2026-10-23T07:05:00.000Z")

    def test_boxes_the_run_could_not_take_are_not_said_taken(self):
        lv = live.Live(FakeSink(), clock=Clock())
        lv.watch(None, lambda free, helper: False)
        lv.control = FakeSink({"boxes": {"read1": []}, "boxes_at": "2026-10-23T07:05:00.000Z"})
        lv.check_control()
        self.assertEqual(lv.boxes_at, "")

    def test_a_model_sent_back_is_no_longer_out_on_the_page(self):
        lv = live.Live(FakeSink(), clock=Clock())
        lv.note("gemini-3.5-flash-lite", "out", 30, "a", n=500)
        lv.revived("gemini-3.5-flash-lite")
        m = lv.snapshot()["models"]["gemini-3.5-flash-lite"]
        self.assertEqual((m["out"], m["out_at"]), (False, ""))
        self.assertEqual(lv.snapshot()["events"][-1]["r"], "back")

    def test_the_requests_file_comes_with_the_live_file(self):
        lv = live.open_live(LIVE_ID, "token", "control_file_1234567890")
        self.assertEqual(lv.control.id, "control_file_1234567890")
        self.assertIsNone(live.open_live(LIVE_ID, "token", "bad id").control)


class DriveCalls(unittest.TestCase):
    def test_the_file_is_written_whole_with_the_token(self):
        opener = Opener()
        f = live.DriveFile(LIVE_ID, "ya29.token", opener)
        self.assertTrue(f.write(b'{"v":1}'))
        call = opener.calls[0]
        self.assertEqual(call["method"], "PATCH")
        self.assertTrue(call["url"].startswith(live.UPLOAD + LIVE_ID + "?uploadType=media"))
        self.assertEqual(call["auth"], "Bearer ya29.token")
        self.assertEqual(call["body"], b'{"v":1}')

    def test_a_refused_token_stops_the_writes_a_passing_error_does_not(self):
        opener = Opener({"uploadType=media": 503})
        f = live.DriveFile(LIVE_ID, "t", opener)
        self.assertFalse(f.write(b"{}"))
        self.assertFalse(f.dead)
        opener.answers = {"uploadType=media": 401}
        self.assertFalse(f.write(b"{}"))
        self.assertTrue(f.dead)
        n = len(opener.calls)
        self.assertFalse(f.write(b"{}"))
        self.assertEqual(len(opener.calls), n)

    def test_reading_what_the_script_left(self):
        opener = Opener({"alt=media": {"file": "x", "phase": "queued"}})
        self.assertEqual(live.DriveFile(LIVE_ID, "t", opener).read(), {"file": "x", "phase": "queued"})
        self.assertEqual(live.DriveFile(LIVE_ID, "t", Opener({"alt=media": 404})).read(), {})

    def test_how_long_the_token_works(self):
        self.assertEqual(live.token_seconds_left("t", Opener({"tokeninfo": {"expires_in": "3012"}})), 3012)
        self.assertIsNone(live.token_seconds_left("t", Opener({"tokeninfo": 400})))
        self.assertIsNone(live.token_seconds_left("", Opener()))

    def test_the_limit_google_names(self):
        self.assertEqual(live.quota_limit("Quota exceeded for metric: x, limit: 20, model: gemini-3.8-flash"), 20)
        self.assertEqual(live.quota_limit("{'quotaValue': '500'}"), 500)
        self.assertIsNone(live.quota_limit("429 Too Many Requests"))


class Marks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self.cwd)
        self.env = {"LIVE_FILE": LIVE_ID, "DRIVE_TOKEN": "t", "GITHUB_RUN_ID": "77", "GITHUB_RUN_NUMBER": "9"}

    def written(self, opener):
        return json.loads([x for x in opener.calls if x["method"] == "PATCH"][-1]["body"].decode("utf-8"))

    def test_setup_keeps_the_book_and_starts_afresh(self):
        opener = Opener({"alt=media": {"file": "b1", "book": "كتاب", "dispatched": "D", "phase": "queued",
                                       "events": [{"i": 1}]}})
        self.assertTrue(live.mark("setup", env=self.env, opener=opener, clock=lambda: 1_800_000_000))
        data = self.written(opener)
        self.assertEqual((data["file"], data["book"], data["dispatched"]), ("b1", "كتاب", "D"))
        self.assertEqual((data["phase"], data["run"], data["run_n"]), ("setup", "77", "9"))
        self.assertEqual(data["started"], live.iso(1_800_000_000))
        self.assertNotIn("events", data)

    def test_saved_adds_the_books_state_to_the_last_state(self):
        with open(live.LAST, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "seq": 40, "phase": "end", "file": "b1", "end": {"state": "time"}}, f)
        with open("status.json", "w", encoding="utf-8") as f:
            json.dump({"state": "paused", "waiting_for": "time", "pages_total": 9, "pages_done": 4}, f)
        opener = Opener()
        self.assertTrue(live.mark("saved", "status.json", env=self.env, opener=opener))
        data = self.written(opener)
        self.assertEqual((data["phase"], data["seq"], data["file"]), ("saved", 41, "b1"))
        self.assertEqual(data["end"], {"state": "paused", "waiting_for": "time", "pages_total": 9, "pages_done": 4})

    def test_saved_never_overwrites_the_next_run(self):
        with open(live.LAST, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "seq": 40, "phase": "end", "file": "b1", "dispatched": "D1"}, f)
        opener = Opener({"alt=media": {"file": "b1", "dispatched": "D2", "phase": "queued"}})
        self.assertFalse(live.mark("saved", env=self.env, opener=opener))
        self.assertEqual([x["method"] for x in opener.calls], ["GET"])

    def test_no_file_or_token_writes_nothing(self):
        opener = Opener()
        self.assertFalse(live.mark("saved", env={"LIVE_FILE": "", "DRIVE_TOKEN": "t"}, opener=opener))
        self.assertFalse(live.mark("saved", env={"LIVE_FILE": LIVE_ID, "DRIVE_TOKEN": ""}, opener=opener))
        self.assertEqual(opener.calls, [])


class RunStops(unittest.TestCase):
    def test_the_last_part_is_for_settling(self):
        stops = c.run_stops("t", now=1000, left=3300)
        budget = 3300 - c.TOKEN_MARGIN_SECONDS
        self.assertEqual(stops, {"read": 1000 + budget - c.SETTLE_SECONDS, "settle": 1000 + budget})

    def test_unknown_token_life_is_guessed_and_a_short_one_plans_nothing(self):
        self.assertEqual(c.run_stops("t", now=0, left=None)["settle"], c.TOKEN_GUESS_SECONDS - c.TOKEN_MARGIN_SECONDS)
        self.assertEqual(c.run_stops("t", now=0, left=c.TOKEN_MARGIN_SECONDS + 60), {})


class LiveBase(PipelineBase):
    """The converter with the live view on: a fake Drive file records every write (no tests here)."""

    def setUp(self):
        super().setUp()
        self.sink = FakeSink({"file": "book1", "dispatched": "2026-10-23T07:00:00Z"})
        self._drive_file, self._left, self._last = live.DriveFile, live.token_seconds_left, live.LAST
        live.DriveFile = lambda file_id, token, opener=None: self.sink
        live.token_seconds_left = lambda token, opener=None: 3000
        live.LAST = os.path.join(self.tmp, "live-last.json")
        os.environ["DRIVE_TOKEN"] = "drive-token"
        self.addCleanup(self.restore_live)

    def restore_live(self):
        live.DriveFile, live.token_seconds_left, live.LAST = self._drive_file, self._left, self._last
        os.environ.pop("DRIVE_TOKEN", None)

    def run_live(self, behaviour, pages="30-31", extra=()):
        return self.run_book(behaviour, pages=pages, extra=("--live", LIVE_ID, *extra))


class LivePipeline(LiveBase):
    def test_every_reading_and_settling_is_in_the_feed(self):
        code, st, md, fake = self.run_live({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE if "النسخة (أ)" in p else WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        })
        self.assertEqual(code, 0)
        last = self.sink.last
        self.assertEqual((last["phase"], last["end"]["state"], last["file"]), ("end", "done", "book1"))
        self.assertEqual(last["pages"]["done"], 2)
        self.assertEqual(last["models"]["gemini-3.5-flash-lite"]["role"], "read1")
        self.assertEqual(last["models"]["gemini-3.1-flash-lite"]["role"], "read2")
        events = [(e["p"], e["k"], e["r"]) for e in last["events"] if e["k"] in ("a", "b", "settle")]
        for p in (30, 31):
            self.assertIn((p, "a", "ok"), events)
            self.assertIn((p, "b", "ok"), events)
            self.assertIn((p, "settle", "settled"), events)
        phases = [e["x"] for e in last["events"] if e["r"] == "phase"]
        self.assertEqual(phases, ["read", "check", "build"])
        oks = sum(m["ok"] for m in last["models"].values())
        self.assertEqual(oks, len([k for _, k in fake.calls]))
        # The state for the workflow's own "saved" mark.
        with open(live.LAST, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["seq"], last["seq"])
        self.assertEqual(st["stages"], {"done": 2})
        self.assertEqual(st["requests"], {m: sum(1 for x, _ in fake.calls if x == m) for m, _ in fake.calls})

    def test_no_page_work_after_the_stop_time_and_the_next_run_goes_on(self):
        c_stops = c.run_stops
        c.run_stops = lambda token: {"read": 1.0, "settle": 1.0}
        self.addCleanup(setattr, c, "run_stops", c_stops)
        code, st, md, fake = self.run_live({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        })
        self.assertEqual(code, 7)
        self.assertEqual((st["state"], st["waiting_for"]), ("paused", "time"))
        self.assertEqual(asked_pages(fake), [])
        self.assertEqual(self.sink.last["end"]["state"], "time")
        self.assertEqual(st["stages"], {"unread": 2})

    def test_a_used_up_quota_is_a_plain_pause_even_after_the_stop_time(self):
        c_stops = c.run_stops
        out = Exception("429 RESOURCE_EXHAUSTED. quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier, limit: 500")
        c.run_stops = lambda token: {"read": 4_000_000_000, "settle": 4_000_000_000}
        self.addCleanup(setattr, c, "run_stops", c_stops)

        def gone(p, n):
            raise out
        code, st, md, fake = self.run_live({"gemini-3.5-flash-lite": gone, "gemini-3.1-flash-lite": gone})
        self.assertEqual(code, 3)
        self.assertEqual(st["state"], "paused")
        self.assertNotIn("waiting_for", st)
        self.assertEqual(sorted(st["models_out"]), ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"])
        models = self.sink.last["models"]
        self.assertTrue(models["gemini-3.5-flash-lite"]["out"])
        self.assertEqual(models["gemini-3.5-flash-lite"]["limit"], 500)
        self.assertIn("out", [e["r"] for e in self.sink.last["events"]])

    def test_without_the_live_view_nothing_changes(self):
        code, st, md, fake = self.run_book({
            "gemini-3.5-flash-lite": lambda p, n: answer(TRUE),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        })
        self.assertEqual(code, 0)
        self.assertEqual(self.sink.writes, [])


class LiveHelperPipeline(LiveBase):
    """The live view while the user's models settle (the settling box): the run says what it
    handed them."""

    def setUp(self):
        super().setUp()
        from test_helperbox import FakeDrive
        import helperbox
        self.drive = FakeDrive()
        self._helper_drive = helperbox.Drive
        helperbox.Drive = lambda token: self.drive
        self.addCleanup(setattr, helperbox, "Drive", self._helper_drive)

    def test_pages_no_free_model_could_settle_go_to_the_users_models(self):
        from test_helperbox import FOLDER
        code, st, md, fake = self.run_live({
            "gemini-3.5-flash-lite": lambda p, n: answer(WRONG),
            "gemini-3.1-flash-lite": lambda p, n: answer(TRUE),
        }, extra=("--helper", f"{FOLDER}:settle"))
        self.assertEqual(code, 5)
        last = self.sink.last
        self.assertEqual(last["pages"]["helper"], 2)
        sent = [e for e in last["events"] if e["r"] == "sent"]
        # Each page as soon as the free models leave it (one at once, the next with the rest).
        self.assertEqual({e["k"] for e in sent}, {"helper"})
        self.assertEqual(sum(e["n"] for e in sent), 2)
        self.assertIn("free", [e["x"] for e in last["events"] if e["r"] == "phase"])
        self.assertEqual(st["stages"], {"helper": 2})


def asked_pages(fake):
    return [k for _, k in fake.calls if k != "cover"]


if __name__ == "__main__":
    unittest.main()


class DailyLimitChecks(unittest.TestCase):
    """A 429 naming the daily limit is believed only on the third, each after a wait (2026-10-23:
    one stopped both readers after 8 and 3 requests while Google counted 10 and 5 of 500)."""

    def pool(self, answers):
        calls = []

        def generate_content(model, contents, config):
            calls.append(model)
            r = answers.pop(0)
            if isinstance(r, Exception):
                raise r
            return answer(TRUE)
        self._client, self._sleep = c.genai.Client, c.time.sleep
        os.environ["GEMINI_API_KEY"] = "test"
        c.genai.Client = lambda **kw: type("C", (), {"models": type("M", (), {"generate_content": staticmethod(generate_content)})})()
        self.slept = []
        c.time.sleep = lambda s: self.slept.append(s)
        self.addCleanup(self.restore)
        return c.ModelPool("m", "", "", 6000), calls

    def restore(self):
        c.genai.Client, c.time.sleep = self._client, self._sleep

    DAY = Exception("429 RESOURCE_EXHAUSTED. {'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier', "
                    "'quotaValue': '500'} Please retry in 20s.")

    def test_one_429_naming_the_day_is_checked_again_and_the_model_goes_on(self):
        pool, calls = self.pool([self.DAY, "ok"])
        text, _ = pool.read(None, "x", "gemini-9-flash-lite")
        self.assertEqual(text, TRUE.strip())
        self.assertNotIn("gemini-9-flash-lite", pool.exhausted)
        self.assertEqual(len([s for s in self.slept if s >= c.DAY_CHECK_SECONDS]), 1)
        self.assertEqual(pool.quota_day, "")

    def test_the_third_in_a_row_is_the_daily_limit(self):
        pool, calls = self.pool([self.DAY, self.DAY, self.DAY])
        with self.assertRaises(c.QuotaExhausted):
            pool.read(None, "x", "gemini-9-flash-lite")
        self.assertIn("gemini-9-flash-lite", pool.exhausted)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len([s for s in self.slept if s >= c.DAY_CHECK_SECONDS]), 2)

    def test_a_429_asking_to_wait_hours_is_checked_like_the_daily_limit(self):
        # 2026-10-23: a 429 naming the per-minute quota said "retry in" ~19 hours; the run slept on it.
        long = Exception("429 RESOURCE_EXHAUSTED. {'quotaId': 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'} "
                         "{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '69118s'}")
        pool, calls = self.pool([long, long, long])
        with self.assertRaises(c.QuotaExhausted):
            pool.read(None, "x", "gemini-9-flash-lite")
        self.assertIn("gemini-9-flash-lite", pool.exhausted)
        self.assertEqual(len([s for s in self.slept if s >= c.DAY_CHECK_SECONDS]), 2)
        self.assertLessEqual(max(self.slept), c.DAY_CHECK_SECONDS)
