"""queue_prep.py: the other books of a run, prepared and saved by the workflow."""

import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import queue_prep as q  # noqa: E402

ID = "1AbCdEfGhIjKlMnOp"


class QueueInput(unittest.TestCase):
    def test_only_well_formed_books_pass(self):
        books, order = q.books_input({"queue": json.dumps({"order": "2026-10-01T10:00:00Z", "books": [
            {"id": ID, "name": "تراث 4.pdf", "dir": "drive-" + ID, "title": "تراث 4", "review": ID + ":only",
             "helper": ID + ":settle", "judge": ID, "order": "2026-10-02T10:00:00Z"},
            {"id": "bad id; rm -rf /", "name": "x.pdf"},
            {"id": ID + "x", "name": "x.txt"},
            {"id": ID + "y", "name": "../../y.pdf", "dir": "../evil", "helper": ID + ":$(id)"},
            {"id": ID + "z", "name": "z.pdf", "helper": ID + ":read1+settle"}]})})
        self.assertEqual(order, "2026-10-01T10:00:00Z")
        self.assertEqual([b["id"] for b in books], [ID, ID + "z"])
        self.assertEqual((books[0]["review"], books[0]["helper"], books[0]["judge"]), (ID, ID + ":settle", ID))
        self.assertEqual((books[1]["dir"], books[1]["helper"]), ("drive-" + ID + "z", ID + ":read1+settle"))
        self.assertEqual(q.books_input({"queue": "not json"}), ([], ""))
        self.assertEqual(q.books_input({}), ([], ""))


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.old = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self.old)

    def test_progress_comes_back_without_anything_else_in_the_file(self):
        os.makedirs("src/abcdef0123456789-v1")
        with open("src/abcdef0123456789-v1/0001.json", "w") as f:
            f.write("{}")
        with open("src/evil.txt", "w") as f:
            f.write("x")
        os.makedirs(q.ROOT + "/tgz")
        with tarfile.open(q.ROOT + "/tgz/abcdef0123456789.tgz", "w:gz") as t:
            t.add("src", arcname=".")
        calls = []
        real = q.subprocess.run
        q.subprocess.run = lambda cmd, **kw: (calls.append(cmd), type("R", (), {"returncode": 0})())[1]
        try:
            self.assertTrue(q.restore_progress("abcdef0123456789"))
        finally:
            q.subprocess.run = real
        self.assertTrue(os.path.exists("work/abcdef0123456789-v1/0001.json"))
        self.assertFalse(os.path.exists("work/evil.txt"))
        self.assertEqual(calls[0][:3], ["gh", "release", "download"])

    def test_each_book_is_saved_in_its_folder_and_set_apart_from_the_runs_own(self):
        os.makedirs(q.ROOT + "/out-0/review-out")
        os.makedirs(q.ROOT + "/in-0")
        os.makedirs("work/hash0-v1")
        with open(q.ROOT + "/out-0/book.epub", "w") as f:
            f.write("epub")
        with open(q.ROOT + "/out-0/book.epub.md", "w") as f:
            f.write("text")
        with open(q.ROOT + "/out-0/status.json", "w") as f:
            json.dump({"state": "done", "pages_done": 2}, f)
        with open(q.ROOT + "/out-0/review-out/items.json", "w") as f:
            json.dump({"pages": [{"img": "p0001-a.jpg"}]}, f)
        for name in ("p0001-a.jpg", "p0002-b.jpg"):
            with open(q.ROOT + "/out-0/review-out/" + name, "w") as f:
                f.write("jpg")
        with open(q.ROOT + "/in-0/judge.json", "w") as f:
            json.dump({"v": 1, "answers": {}}, f)
        with open(q.ROOT + "/books.json", "w") as f:
            json.dump([{"dir": "drive-1", "hash": "hash0", "out": q.ROOT + "/out-0", "in": q.ROOT + "/in-0",
                        "judge_save": True, "epub": "book.epub"},
                       {"dir": "drive-2", "hash": "hash1", "out": q.ROOT + "/out-1", "in": q.ROOT + "/in-1",
                        "judge_save": False, "epub": "other.epub"}], f)
        os.makedirs("clone/drive-1/review")
        with open("clone/drive-1/old.epub", "w") as f:
            f.write("old")
        with open("clone/drive-1/review/p0009-old.jpg", "w") as f:
            f.write("old")
        q.stash()
        self.assertFalse(os.path.exists("work/hash0-v1"))
        self.assertTrue(os.path.isdir(q.ROOT + "/work/hash0-v1"))
        q.save("clone")
        self.assertEqual(sorted(os.listdir("clone/drive-1")), ["book.epub", "review", "status.json", "text.md"])
        self.assertEqual(sorted(os.listdir("clone/drive-1/review")), ["items.json", "judge-answers.json", "p0001-a.jpg"])
        with open("clone/drive-1/status.json") as f:
            st = json.load(f)
        self.assertEqual(st["state"], "done")
        self.assertIn("updated_at", st)
        # A book the run could not finish says so (the run crashed before writing its status).
        with open("clone/drive-2/status.json") as f:
            self.assertEqual(json.load(f)["state"], "failed")


if __name__ == "__main__":
    unittest.main()
