from contextlib import redirect_stdout
import http.client
from io import StringIO
import json
from pathlib import Path
from threading import Thread
import unittest
from unittest.mock import patch

from test_analysis import GitRepoMixin
import ct
from jobs import Jobs, submit, wait
from repo import ChangeTreeError
from server import Server, State, read_json, write_json


def file_result(job):
    return {"files": {
        file["path"]: {"summary": "This file changes a public setting.",
                       "hunks": [{"title": "Update setting", "summary": "Updates the task's default value."}
                                 for _ in file["hunks"]]}
        for file in job["files"]
    }}


class JobsFixture(GitRepoMixin, unittest.TestCase):
    def start_jobs(self, files=16):
        names = [f"setting{index:02}.py" for index in range(files)]
        for name in names:
            self.write(name, "SETTING = 0\n")
        self.commit()
        for name in names:
            self.write(name, "SETTING = 1\n")
        self.cache = self.root / ".git" / "test-cache"
        for target, value in (
            ("impact.refresh_graph", {"nodes": [], "links": []}),
            ("server.repo_cache", self.cache),
        ):
            mock = patch(target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.state = State(self.root, ct.analyze, job_factory=Jobs, cache=self.cache)
        self.server = Server(self.state, 0)
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()
        write_json(self.cache / "server.json", {"url": self.server.url, "token": self.server.token})
        self.addCleanup(self.close_server)
        return self.compare()

    def close_server(self):
        self.state.jobs.close()
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def compare(self, task="Update defaults"):
        return self.state.compare({"base": "HEAD", "head": "WORKTREE", "task": task})

    def no_job(self):
        self.assertEqual(wait(self.root, 0), (4, "CHANGE_TREE_IDLE"))

    def next_job(self, cli=False):
        if cli:
            printed = StringIO()
            with redirect_stdout(printed):
                self.assertEqual(ct.main(["jobs", "wait", "--repo", str(self.root), "--timeout", "4"]), 0)
            line = printed.getvalue().strip()
        else:
            code, line = wait(self.root, 4)
            self.assertEqual(code, 0, line)
        self.assertTrue(line.startswith("CHANGE_TREE_JOB "), line)
        path = Path(line.split(" ", 3)[3])
        self.assertEqual(path.parent, self.cache / "jobs")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        return read_json(path, None)

    def done(self, job, result, cli=False):
        result_path = self.cache / "result-fixture.json"
        result_path.write_text(json.dumps(result), encoding="utf-8")
        if cli:
            printed = StringIO()
            with redirect_stdout(printed):
                self.assertEqual(ct.main(["jobs", "--repo", str(self.root), "done", job["jobId"], str(result_path)]), 0)
            self.assertIn(job["jobId"], printed.getvalue())
        else:
            submit(self.root, job["jobId"], result_path)

    def test_batches_stream_sequentially_and_cache_files_after_save(self):
        start = self.start_jobs()
        event_stream = http.client.HTTPConnection(self.server.host, timeout=4)
        self.addCleanup(event_stream.close)
        event_stream.request("GET", "/api/events")
        stream = event_stream.getresponse()
        self.addCleanup(stream.close)
        self.assertEqual(stream.status, 200)

        def event():
            prefix = b"data: "
            line = stream.readline()
            self.assertTrue(line.startswith(prefix), line)
            self.assertEqual(stream.readline(), b"\n")
            return json.loads(line[len(prefix):])

        self.assertEqual(event()["snapshot"], start["snapshot"])
        first = self.next_job(cli=True)
        self.assertEqual(len(first["files"]), 15)
        self.assertEqual(first["kind"], "files")
        self.assertEqual(first["task"], "Update defaults")
        # File batches run in parallel, so sub-agents can work at the same time.
        second = self.next_job()
        self.assertEqual(len(second["files"]), 1)
        # The overview waits until every file batch is done.
        self.no_job()
        with self.assertRaisesRegex(ChangeTreeError, "every path"):
            self.done(first, {"files": {}})
        self.done(first, file_result(first), cli=True)
        update = event()
        self.assertEqual(len(update["summaries"]["files"]), 15)
        self.assertEqual(update["summaryStatus"], "pending")
        self.no_job()
        self.done(second, file_result(second))
        overview = self.next_job()
        self.assertEqual(overview["kind"], "comparison")
        self.assertEqual(len(overview["files"]), 16)
        self.assertTrue(all(file["summary"] for file in overview["files"]))
        summary = {"headline": "Update the defaults", "summary": "Defaults increase from zero to one."}
        with self.assertRaisesRegex(ChangeTreeError, "taskFit"):
            self.done(overview, {"comparison": summary})
        summary["taskFit"] = "All settings support the task."
        self.done(overview, {"comparison": summary})
        self.assertEqual(self.state.get()["summaryStatus"], "complete")
        self.assertEqual(self.state.get()["summaries"]["comparison"], summary)
        cached = read_json(self.state.comparison_path(start["snapshot"]), {})
        self.assertEqual(len(cached["summaries"]["files"]), 16)
        reopened = self.compare()
        self.assertEqual(reopened["summaryStatus"], "complete", reopened.get("error"))
        self.no_job()

        file = self.state.get()["files"][0]
        saved = self.state.save({"snapshot": start["snapshot"], "hash": file["contentHash"], "path": file["path"],
                                 "edits": [{"index": 0, "text": "SETTING = 2"}]})
        self.assertEqual(len(saved["summaries"]["files"]), 15)
        refresh = self.next_job()
        self.assertEqual([entry["path"] for entry in refresh["files"]], [file["path"]])
        self.done(refresh, file_result(refresh))
        refreshed_overview = self.next_job()
        self.done(refreshed_overview, {"comparison": summary})
        self.assertEqual(self.state.get()["summaryStatus"], "complete")

    def test_finishing_old_job_keeps_new_comparison_active(self):
        original = self.start_jobs(files=1)
        first = self.next_job()
        next_comparison = self.compare(task="")
        self.assertNotEqual(original["snapshot"], next_comparison["snapshot"])
        self.done(first, file_result(first))
        current = self.next_job()
        self.assertEqual(current["comparisonId"], next_comparison["snapshot"])
        self.assertEqual(self.state.get()["summaries"], {})
        self.done(current, file_result(current))
        overview = self.next_job()
        invalid = {"headline": "Update setting", "summary": "Changes the value.", "taskFit": "No task"}
        with self.assertRaisesRegex(ChangeTreeError, "taskFit"):
            self.done(overview, {"comparison": invalid})
        del invalid["taskFit"]
        self.done(overview, {"comparison": invalid})
        self.assertEqual(self.state.get()["summaries"]["comparison"], invalid)

    def test_summaries_must_follow_ste_rules(self):
        self.start_jobs(files=1)
        job = self.next_job()
        result = file_result(job)
        path = job["files"][0]["path"]
        result["files"][path]["summary"] = "This is a seamless change; it updates the setting."
        with self.assertRaisesRegex(ChangeTreeError, "ASD-STE100.*semicolon"):
            self.done(job, result)
        result["files"][path]["summary"] = "This change updates the setting."
        self.done(job, result)

    def test_unsubmitted_jobs_return_to_the_queue(self):
        self.start_jobs(files=1)
        job = self.next_job()
        with patch("jobs.CLAIM_TIMEOUT", 0):
            again = self.next_job()
        self.assertEqual([file["path"] for file in again["files"]], [file["path"] for file in job["files"]])
        with self.assertRaisesRegex(ChangeTreeError, "no longer active"):
            self.done(job, file_result(job))
        self.done(again, file_result(again))

    def test_wait_reports_a_missing_server(self):
        self.start_jobs(files=1)
        (self.cache / "server.json").unlink()
        with self.assertRaisesRegex(ChangeTreeError, "No Change Tree server"):
            wait(self.root, 0)


if __name__ == "__main__":
    unittest.main()
