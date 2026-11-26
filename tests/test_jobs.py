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
from jobs import Jobs, request, submit, wait
from repo import ChangeTreeError
from server import Server, State, read_json, write_json


def file_result(job):
    return {"files": {
        file["path"]: {"summary": "This file changes a public setting.", "why": "The task asks for new defaults.",
                       "hunks": [{"title": "Update setting", "summary": "Updates the task's default value.",
                                  "why": "The old value was zero."}
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
        _, _, kind, model, effort, path = line.split(" ", 5)
        self.assertEqual((model, effort), (self.state.settings["model"], self.state.settings["effort"]))
        path = Path(path)
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

    def chat(self, message, context=None, **body):
        return request(self.root, "POST", "/api/chat", {"message": message, "context": context or {}, **body}, 4)

    def test_chat_has_priority_and_a_slot_outside_the_summary_limit(self):
        start = self.start_jobs()
        path = start["files"][0]["path"]
        context = {"path": path, "block": 0, "selectedText": "SETTING = 1"}
        self.assertEqual(self.chat("What changed?", context)["chatStatus"], "pending")
        self.chat("Why did it change?")

        with patch("jobs.MAX_ACTIVE", 1):
            first = self.next_job(cli=True)
            self.assertEqual(first["kind"], "chat")
            self.assertEqual(first["comparisonId"], start["snapshot"])
            self.assertEqual(first["repo"], str(self.root))
            self.assertEqual(first["comparison"]["base"], "HEAD")
            self.assertEqual(first["comparison"]["head"], "WORKTREE")
            self.assertEqual(first["comparison"]["task"], "Update defaults")
            self.assertEqual(first["context"], context)
            self.assertEqual([item["text"] for item in first["messages"]], ["What changed?"])
            self.assertEqual(first["thread"], [])
            self.assertEqual(len(first["files"]), 16)

            summary = self.next_job()
            self.assertEqual(summary["kind"], "files")
            self.chat("What should I test?")
            self.no_job()
            self.done(first, {"reply": "The default changed."})
            second = self.next_job()
            self.assertEqual(second["kind"], "chat")
            self.assertEqual([item["text"] for item in second["messages"]], ["Why did it change?", "What should I test?"])
            self.assertEqual([item["text"] for item in second["thread"]], ["What changed?", "The default changed."])
            self.no_job()
            self.done(summary, file_result(summary))
            self.done(second, {"reply": "Test callers that omit the setting."})
            self.assertEqual(self.state.get()["chatStatus"], "idle")
            self.assertEqual([item["role"] for item in self.state.get()["chat"]],
                             ["user", "assistant", "user", "user", "assistant"])

            summary = self.next_job()
            self.chat("Which setting changed?")
            third = self.next_job()
            self.assertEqual(third["kind"], "chat")
            evidence = next(item for item in third["files"] if item["path"] == path)
            self.assertEqual(evidence["why"], "The task asks for new defaults.")

    def test_chat_persists_per_comparison_and_finishes_after_switching(self):
        original = self.start_jobs(files=1)
        self.chat("Where is the setting?")
        job = self.next_job()
        self.chat("What uses it?")
        different = self.compare(task="Other task")
        self.assertNotEqual(different["snapshot"], original["snapshot"])
        self.assertEqual(different["chat"], [])
        with self.assertRaisesRegex(ChangeTreeError, "changed on disk"):
            self.chat("Stale question", snapshot=original["snapshot"])
        reply = {"reply": "Set in `setting00.py:1`; it is a constant.",
                 "references": [{"path": "setting00.py", "line": 1}]}
        self.done(job, reply, cli=True)
        followup = self.next_job()
        self.assertEqual(followup["comparisonId"], original["snapshot"])
        self.assertEqual(followup["messages"][0]["text"], "What uses it?")
        self.done(followup, {"reply": "Read the configuration module."})
        self.assertEqual(self.state.get()["chat"], [])
        reopened = self.compare()
        self.assertEqual(reopened["chat"][1]["text"], reply["reply"])
        self.assertEqual(reopened["chat"][1]["references"], reply["references"])

        restored = State(self.root, ct.analyze, job_factory=Jobs, cache=self.cache)
        self.addCleanup(restored.jobs.close)
        result = restored.compare({"base": "HEAD", "head": "WORKTREE", "task": "Update defaults"})
        self.assertEqual(result["chat"], reopened["chat"])
        self.assertEqual(result["chatStatus"], "idle")

    def test_chat_rejects_bad_context_and_replies(self):
        self.start_jobs(files=1)
        for context in ({"path": "../outside"}, {"path": "setting00.py", "block": True},
                        {"block": 0}, {"selectedText": "x" * 2001}, {"selectedText": ["text"]}):
            with self.subTest(context=context), self.assertRaises(ChangeTreeError):
                self.chat("Question", context)
        with self.assertRaisesRegex(ChangeTreeError, "Chat message"):
            self.chat(" ")
        self.assertEqual(self.state.get()["chat"], [])
        self.chat("Question")
        job = self.next_job()
        for result in ({"reply": " "}, {"reply": ["bad"]}, {"reply": "x" * 16001},
                       {"reply": "Answer", "references": None}, {"reply": "Answer", "extra": True},
                       {"reply": "Answer", "references": [{}] * 11},
                       {"reply": "Answer", "references": [{"path": "setting00.py", "line": True}]}):
            with self.subTest(result=result), self.assertRaises(ChangeTreeError):
                self.done(job, result)
        unsafe_paths = ["../outside", "/tmp/outside", "C:\\outside", ".git/config"]
        try:
            (self.root / "outside").symlink_to(self.root.parent, target_is_directory=True)
            (self.root / "git-alias").symlink_to(self.root / ".git", target_is_directory=True)
        except OSError as error:
            if getattr(error, "winerror", None) != 1314:  # Windows user cannot create symbolic links.
                raise
        else:
            unsafe_paths.extend(["outside/file.py", "git-alias/config"])
        for path in unsafe_paths:
            with self.subTest(path=path), self.assertRaisesRegex(ChangeTreeError, "inside the repository"):
                self.done(job, {"reply": "Answer", "references": [{"path": path, "line": 1}]})
        self.assertEqual(self.state.get()["chatStatus"], "pending")
        self.done(job, {"reply": "Answer; no STE lint."})

    def test_chat_expires_retries_and_clears(self):
        original = self.start_jobs(files=1)
        self.chat("What changed?")
        with patch("jobs.CLAIM_TIMEOUT", 0.2):
            expired = self.next_job()
            with self.state.changed:
                self.assertTrue(self.state.changed.wait_for(
                    lambda: self.state.result["chatStatus"] == "failed", timeout=3))
        state = self.state.get()
        self.assertEqual(state["chat"][0]["status"], "failed")
        self.assertFalse(self.state.jobs.job_path(expired["jobId"]).exists())
        with self.assertRaisesRegex(ChangeTreeError, "no longer active"):
            self.done(expired, {"reply": "Late answer."})

        retry = request(self.root, "POST", "/api/chat/retry", {"id": state["chat"][0]["id"]}, 4)
        self.assertEqual(retry["chatStatus"], "pending")
        fresh = self.next_job()
        self.assertEqual(fresh["messages"], expired["messages"])
        self.chat("Do I need a new test?")
        cleared = request(self.root, "POST", "/api/chat/clear", {}, 4)
        self.assertEqual(cleared, {"chat": [], "chatStatus": "idle"})
        self.assertEqual(read_json(self.state.jobs.chat_path(original["snapshot"]), None), [])
        self.assertEqual(self.compare()["chat"], [])
        with self.assertRaisesRegex(ChangeTreeError, "no longer active"):
            self.done(fresh, {"reply": "Stale answer."})
        self.chat("New thread.")
        self.assertEqual([item["text"] for item in self.next_job()["messages"]], ["New thread."])

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
        with self.assertRaisesRegex(ChangeTreeError, "No Impact Tree server"):
            wait(self.root, 0)

    def test_settings_reach_claimed_jobs_and_are_saved(self):
        self.start_jobs(files=1)
        config = self.cache / "config-home"
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(config)}):
            self.assertEqual(self.state.update_settings({"model": "sonnet", "effort": "high"}),
                             {"settings": {"model": "sonnet", "effort": "high"}})
            self.assertEqual(read_json(config / "change-tree" / "config.json", {}), {"claude": {"model": "sonnet", "effort": "high"}})
            with self.assertRaisesRegex(Exception, "Effort for sonnet must be"):
                self.state.update_settings({"effort": "extreme"})
            with self.assertRaisesRegex(Exception, "Choose a Claude Code model"):
                self.state.update_settings({"model": "bad model; rm"})
        code, line = wait(self.root, 4)
        self.assertEqual(line.split(" ")[3:5], ["sonnet", "high"])

    def test_codex_models_come_from_the_codex_model_cache(self):
        from server import catalog, clean_settings, load_settings, save_settings
        self.start_jobs(files=1)
        codex_home = self.cache / "codex-home"
        config = self.cache / "config-home"
        write_json(codex_home / "models_cache.json", {"models": [
            {"slug": "gpt-a", "display_name": "GPT A", "visibility": "list", "priority": 2,
             "supported_reasoning_levels": [{"effort": "low"}, {"effort": "ultra"}]},
            {"slug": "gpt-hidden", "visibility": "hide", "supported_reasoning_levels": [{"effort": "low"}]},
            {"slug": "gpt-b", "display_name": "GPT B", "visibility": "list", "priority": 1,
             "supported_reasoning_levels": [{"effort": "medium"}]},
        ]})
        with patch.dict("os.environ", {"CODEX_HOME": str(codex_home), "XDG_CONFIG_HOME": str(config)}):
            options = catalog("codex")
            self.assertEqual([item["value"] for item in options["models"]], ["gpt-b", "gpt-a"])
            self.assertEqual(clean_settings({"model": "gpt-a", "effort": "ultra"}, agent="codex"),
                             {"model": "gpt-a", "effort": "ultra"})
            with self.assertRaisesRegex(Exception, "Effort for gpt-b"):
                clean_settings({"model": "gpt-b", "effort": "ultra"}, agent="codex")
            with self.assertRaisesRegex(Exception, "Choose a Codex model"):
                clean_settings({"model": "sonnet"}, agent="codex")
            self.assertEqual([item["value"] for item in catalog("claude")["models"]], ["haiku", "sonnet", "opus", "fable"])
            save_settings("codex", {"model": "gpt-a", "effort": "low"})
            save_settings("claude", {"model": "opus", "effort": "max"})
            self.assertEqual(load_settings("codex"), {"model": "gpt-a", "effort": "low"})
            self.assertEqual(load_settings("claude"), {"model": "opus", "effort": "max"})

    def test_reasons_are_required_and_risks_are_optional(self):
        self.start_jobs(files=1)
        job = self.next_job()
        path = job["files"][0]["path"]
        result = file_result(job)
        del result["files"][path]["why"]
        with self.assertRaisesRegex(ChangeTreeError, "summary, why, hunks"):
            self.done(job, result)
        result = file_result(job)
        del result["files"][path]["hunks"][0]["why"]
        with self.assertRaisesRegex(ChangeTreeError, "title, summary and why"):
            self.done(job, result)
        result = file_result(job)
        result["files"][path]["risk"] = "Other modules may still read the old default; they now get a new value."
        with self.assertRaisesRegex(ChangeTreeError, "File risk breaks the ASD-STE100"):
            self.done(job, result)
        result["files"][path]["risk"] = "Other modules may still read the old default. They now get a new value."
        result["files"][path]["hunks"][0]["risk"] = ""
        self.done(job, result)
        saved = self.state.get()["summaries"]["files"][path]
        self.assertEqual(saved["risk"], "Other modules may still read the old default. They now get a new value.")
        self.assertNotIn("risk", saved["hunks"][0])
        self.assertEqual(saved["hunks"][0]["why"], "The old value was zero.")


if __name__ == "__main__":
    unittest.main()
