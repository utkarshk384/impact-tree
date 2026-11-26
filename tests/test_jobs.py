from contextlib import redirect_stdout
import http.client
from io import StringIO
import json
from pathlib import Path
from queue import Queue
import subprocess
from threading import Thread
import unittest
from unittest.mock import patch

from test_analysis import GitRepoMixin
import ct
from jobs import Jobs, submit
from repo import ChangeTreeError
from server import Server, State, read_json


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
        self.notifications = Queue()

        def notify(args, cwd, timeout):
            self.notifications.put((args, cwd))
            return subprocess.CompletedProcess(args, 0, b"{}", b"")

        for target, value in (
            ("impact.refresh_graph", {"nodes": [], "links": []}),
            ("jobs.repo_cache", self.cache),
        ):
            mock = patch(target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        transport = patch("jobs.command", side_effect=notify)
        transport.start()
        self.addCleanup(transport.stop)
        self.state = State(self.root, ct.analyze, "worker-fixture", job_factory=Jobs, cache=self.cache)
        self.server = Server(self.state, 0)
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()
        self.state.jobs.connect(self.server.url, self.server.token)
        self.addCleanup(self.close_server)
        return self.compare()

    def close_server(self):
        self.state.jobs.close()
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def compare(self, task="Update defaults"):
        return self.state.compare({"base": "HEAD", "head": "WORKTREE", "task": task})

    def next_job(self):
        args, cwd = self.notifications.get(timeout=4)
        self.assertEqual(args[:3], ["hc", "send", "worker-fixture"])
        self.assertEqual(cwd, self.root)
        self.assertTrue(args[3].startswith("change-tree job "))
        path = Path(args[3].removeprefix("change-tree job "))
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
        first = self.next_job()
        self.assertEqual(len(first["files"]), 15)
        self.assertEqual(first["kind"], "files")
        self.assertEqual(first["task"], "Update defaults")
        self.assertTrue(self.notifications.empty())
        with self.assertRaisesRegex(ChangeTreeError, "every path"):
            self.done(first, {"files": {}})
        self.assertTrue(self.notifications.empty())
        self.done(first, file_result(first), cli=True)
        update = event()
        self.assertEqual(len(update["summaries"]["files"]), 15)
        self.assertEqual(update["summaryStatus"], "pending")
        second = self.next_job()
        self.assertEqual(len(second["files"]), 1)
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
        self.assertTrue(self.notifications.empty())

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
        self.assertTrue(self.notifications.empty())
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


if __name__ == "__main__":
    unittest.main()
