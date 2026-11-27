from contextlib import contextmanager
import http.client
import json
from pathlib import Path
from threading import Thread
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_analysis import GitRepoMixin
from ct import analyze
from edits import CHANGED, EditError, save_file
from jobs import Jobs
from repo import git
from server import Server, State


class ServerFixture(GitRepoMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.write("dir/sample.py", "first = 1\nsecond = 2\n")
        self.commit()
        self.write("dir/sample.py", "first = 10\nsecond = 2\n")
        self.cache = self.root / ".git" / "test-cache"
        self.graph = patch("impact.refresh_graph", return_value={"nodes": [], "links": []})
        self.graph.start()
        self.addCleanup(self.graph.stop)

    def get_file(self):
        return analyze(self.root)["files"][0]

    def test_save_writes_only_server_owned_range_and_rejects_stale_hash(self):
        file = self.get_file()
        digest = file["contentHash"]
        edits = [{"index": 0, "text": "first = 100\nsecond = 2"}]
        self.assertTrue(save_file(self.root, file, digest, edits))
        self.assertEqual((self.root / file["path"]).read_bytes(), b"first = 100\nsecond = 2\n")
        self.assertEqual(self.run_git("diff", "--cached"), b"")
        with self.assertRaisesRegex(EditError, CHANGED) as error:
            save_file(self.root, file, digest, edits)
        self.assertEqual(error.exception.status, 409)

    def test_rejects_traversal_and_symlinked_parent(self):
        file = self.get_file()
        digest = file["contentHash"]
        edits = [{"index": 0, "text": "updated"}]
        with self.assertRaises(EditError):
            save_file(self.root, {**file, "path": "../sample.py"}, digest, edits)
        with TemporaryDirectory() as temporary:
            outside = Path(temporary)
            (self.root / "dir" / "sample.py").rename(outside / "sample.py")
            (self.root / "dir").rmdir()
            try:
                (self.root / "dir").symlink_to(outside, target_is_directory=True)
            except OSError as error:
                if getattr(error, "winerror", None) != 1314:
                    raise
                self.skipTest("Windows user cannot create symbolic links")
            with self.assertRaises(EditError):
                save_file(self.root, file, digest, edits)
            self.assertEqual((outside / "sample.py").read_bytes(), b"first = 10\nsecond = 2\n")

    def test_multi_hunk_save_preserves_crlf_and_missing_final_newline(self):
        base = [f"line {index}" for index in range(30)]
        self.write("sample.txt", "\r\n".join(base).encode("utf-8"))
        self.commit()
        base[0] = "new 0"
        base[-1] = "new 29"
        self.write("sample.txt", "\r\n".join(base).encode("utf-8"))
        file = self.get_file()
        edits = [{"index": 0, "text": "zero\nextra\nline 1\nline 2\nline 3"},
                 {"index": 1, "text": "line 26\nline 27\nline 28\ntwenty-nine"}]
        self.assertTrue(save_file(self.root, file, file["contentHash"], edits))
        expected = ["zero", "extra", *base[1:-1], "twenty-nine"]
        self.assertEqual((self.root / file["path"]).read_bytes(), "\r\n".join(expected).encode("utf-8"))

    @contextmanager
    def server(self, job_factory=None):
        state = State(self.root, analyze, cache=self.cache, job_factory=job_factory)
        state.compare({"base": "HEAD", "head": "WORKTREE"})
        with Server(state, 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                yield server
            finally:
                if state.jobs:
                    state.jobs.close()
                server.shutdown()
                thread.join()

    def request(self, server, method, path, body=None, **headers):
        connection = http.client.HTTPConnection(server.host, timeout=5)
        try:
            if body is not None:
                headers.setdefault("Content-Type", "application/json")
                body = json.dumps(body)
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_lines_returns_unchanged_text_of_the_new_version(self):
        with self.server() as server:
            same_origin = {"Origin": server.url, "X-Change-Tree-Token": server.token}
            _, state = self.request(server, "GET", "/api/state")
            file = state["files"][0]
            status, result = self.request(server, "POST", "/api/lines", {
                "snapshot": state["snapshot"], "path": file["path"], "start": 1, "end": 2}, **same_origin)
            self.assertEqual(status, 200)
            expected = (self.root / file["path"]).read_text().splitlines()
            self.assertEqual(result["lines"], expected[:2])
            self.assertEqual(result["total"], len(expected))
            self.assertEqual(self.request(server, "POST", "/api/lines", {
                "snapshot": state["snapshot"], "path": file["path"], "start": 1, "end": 2})[0], 403)
            self.assertEqual(self.request(server, "POST", "/api/lines", {
                "snapshot": state["snapshot"], "path": "../outside.txt", "start": 1, "end": 2}, **same_origin)[0], 404)
            self.assertEqual(self.request(server, "POST", "/api/lines", {
                "snapshot": "old", "path": file["path"], "start": 1, "end": 2}, **same_origin)[0], 409)

    def test_directory_paths_for_worktree_obey_git_ignore_rules(self):
        self.write(".gitignore", "ignored*.txt\n")
        self.write("ignored-tracked.txt", "still tracked\n")
        self.run_git("add", "-f", "ignored-tracked.txt")
        self.write("dir/with [spaces] é.txt", "unchanged\n")
        self.commit()
        self.write("untracked/new.txt", "untracked\n")
        self.write("ignored-untracked.txt", "ignored\n")
        with self.server() as server:
            status, result = self.request(server, "GET", "/api/paths")
            _, state = self.request(server, "GET", "/api/state")
            self.assertEqual(status, 200)
            self.assertEqual(result["snapshot"], state["snapshot"])
            self.assertEqual(set(result["paths"]), {
                ".gitignore", "dir/sample.py", "dir/with [spaces] é.txt", "ignored-tracked.txt", "untracked/new.txt",
            })
            self.assertFalse(result["truncated"])
            self.assertNotIn("paths", state)
            self.assertEqual(self.request(server, "GET", "/api/paths", Host="another.test")[0], 403)
            self.assertEqual(self.request(server, "GET", "/api/paths", Origin="https://another.test")[0], 403)
            unchanged = {"snapshot": state["snapshot"], "path": "dir/with [spaces] é.txt", "full": True}
            headers = {"Origin": server.url, "X-Change-Tree-Token": server.token}
            self.assertEqual(self.request(server, "POST", "/api/lines", unchanged, **headers),
                             (200, {"text": "unchanged\n"}))
            self.assertEqual(self.request(server, "POST", "/api/lines", unchanged, Origin=server.url)[0], 403)
            for path in ("../outside", ".git/config", "ignored-untracked.txt"):
                with self.subTest(path=path):
                    self.assertEqual(self.request(server, "POST", "/api/lines", {**unchanged, "path": path},
                                                  **headers)[0], 404)
            with patch("server.MAX_PREVIEW_BYTES", 4):
                self.assertEqual(self.request(server, "POST", "/api/lines", unchanged, **headers)[0], 413)

    def test_directory_paths_and_full_text_use_the_compared_commit(self):
        contents = "\r\n<head text>\r\n" + "row\r\n" * 20_001 + "x" * 16_001
        self.write("gone-from-checkout.txt", contents)
        self.write("image.bin", b"\x00\x01")
        head = self.commit("Compared head")
        (self.root / "gone-from-checkout.txt").unlink()
        self.write("only-on-disk.txt", "not in the commit\n")
        with self.server() as server:
            headers = {"Origin": server.url, "X-Change-Tree-Token": server.token}
            status, state = self.request(server, "POST", "/api/compare", {"base": head, "head": head}, **headers)
            self.assertEqual(status, 200)
            self.assertEqual(state["files"], [])
            status, result = self.request(server, "GET", "/api/paths")
            self.assertEqual(status, 200)
            self.assertEqual(result["paths"], ["dir/sample.py", "gone-from-checkout.txt", "image.bin"])
            request = {"snapshot": state["snapshot"], "path": "gone-from-checkout.txt", "full": True}
            self.assertEqual(self.request(server, "POST", "/api/lines", request, **headers),
                             (200, {"text": contents}))
            self.assertEqual(self.request(server, "POST", "/api/lines", {**request, "snapshot": "old"},
                                          **headers)[0], 409)
            self.assertEqual(self.request(server, "POST", "/api/lines", {**request, "path": "image.bin"},
                                          **headers)[0], 415)

    def test_directory_paths_cap_and_cache_are_scoped_to_snapshot(self):
        self.write("another.txt", "a tracked file")
        self.commit()
        self.write("untracked.txt", "an untracked file")
        with self.server() as server, patch("server.DIRECTORY_PATH_LIMIT", 2), patch("server.git", wraps=git) as calls:
            status, result = self.request(server, "GET", "/api/paths")
            self.assertEqual(status, 200)
            self.assertTrue(result["truncated"])
            self.assertEqual(len(result["paths"]), result["limit"])
            self.assertEqual(self.request(server, "GET", "/api/paths"), (status, result))
            self.assertEqual(calls.call_count, 1)
            headers = {"Origin": server.url, "X-Change-Tree-Token": server.token}
            status, state = self.request(server, "POST", "/api/compare", {"base": "HEAD", "head": "HEAD"}, **headers)
            self.assertEqual(status, 200)
            self.assertNotEqual(state["snapshot"], result["snapshot"])
            status, listing = self.request(server, "GET", "/api/paths")
            self.assertEqual(status, 200)
            self.assertEqual(listing["snapshot"], state["snapshot"])
            self.assertEqual(listing["paths"], ["another.txt", "dir/sample.py"])
            self.assertFalse(listing["truncated"])
            self.assertEqual(calls.call_count, 2)

    def test_full_text_does_not_follow_worktree_symlinks(self):
        file = self.write("unchanged.txt", "original")
        self.commit()
        with TemporaryDirectory() as outside, self.server() as server:
            _, listing = self.request(server, "GET", "/api/paths")
            self.assertIn("unchanged.txt", listing["paths"])
            external = Path(outside) / "outside.txt"
            external.write_text("outside")
            file.unlink()
            try:
                file.symlink_to(external)
            except OSError as error:
                if getattr(error, "winerror", None) != 1314:
                    raise
                self.skipTest("Windows user cannot create symbolic links")
            headers = {"Origin": server.url, "X-Change-Tree-Token": server.token}
            request = {"snapshot": listing["snapshot"], "path": "unchanged.txt", "full": True}
            self.assertEqual(self.request(server, "POST", "/api/lines", request, **headers)[0], 404)
            file.unlink()
            file.symlink_to(self.root / ".git" / "config")
            self.assertEqual(self.request(server, "POST", "/api/lines", request, **headers)[0], 404)

    def test_view_branch_commit_and_remote_branch_use_the_selected_tree(self):
        name = "dir/view [only] é.txt"
        contents = "\r\n<file contents>\r\n+literal plus\r\n-literal minus"
        self.write(name, contents)
        self.write("image.bin", b"\x00\x01")
        viewed = self.commit("Tree to view")
        self.run_git("branch", "saved", viewed)
        self.run_git("update-ref", "refs/remotes/origin/saved", viewed)
        (self.root / name).unlink()
        self.write("later.txt", "only in the newer commit")
        newer = self.commit("Later tree")
        self.run_git("branch", "origin/saved", newer)
        with self.server() as server, patch("server.DIRECTORY_PATH_LIMIT", 1):
            headers = {"Origin": server.url, "X-Change-Tree-Token": server.token}
            for kind, ref in (("branches", "saved"), ("commits", viewed[:12]), ("origins", "origin/saved")):
                with self.subTest(kind=kind):
                    selection = {"kind": kind, "ref": ref}
                    status, state = self.request(server, "POST", "/api/view", selection, **headers)
                    self.assertEqual(status, 200)
                    self.assertEqual(state["view"]["commit"], viewed)
                    self.assertEqual(state["comparison"], {})
                    self.assertEqual(state["files"], [])
                    status, listing = self.request(server, "GET", "/api/paths")
                    self.assertEqual(status, 200)
                    self.assertEqual(listing["snapshot"], state["snapshot"])
                    self.assertEqual(listing["paths"], ["dir/sample.py", name, "image.bin"])
                    self.assertFalse(listing["truncated"])
                    request = {"snapshot": state["snapshot"], "path": name}
                    self.assertEqual(self.request(server, "POST", "/api/file", request, **headers),
                                     (200, {"text": contents}))
                    self.assertEqual(self.request(server, "POST", "/api/file",
                                                  {**request, "path": "image.bin"}, **headers)[0], 415)
                    self.assertEqual(self.request(server, "POST", "/api/file",
                                                  {**request, "path": "later.txt"}, **headers)[0], 404)

    def test_view_worktree_lists_and_reads_live_files_from_the_selected_checkout(self):
        self.write(".gitignore", "ignored.txt\n")
        self.write("removed.txt", "tracked before removal")
        self.commit()
        with TemporaryDirectory() as parent, self.server() as server:
            linked = Path(parent).resolve() / "linked checkout"
            self.run_git("worktree", "add", "-b", "linked", str(linked))
            (linked / "dir" / "sample.py").write_text("linked contents\n", encoding="utf-8")
            (linked / "removed.txt").unlink()
            (linked / "untracked.txt").write_text("untracked", encoding="utf-8")
            (linked / "ignored.txt").write_text("ignored", encoding="utf-8")
            headers = {"Origin": server.url, "X-Change-Tree-Token": server.token}
            status, state = self.request(server, "POST", "/api/view",
                                         {"kind": "worktrees", "ref": str(linked)}, **headers)
            self.assertEqual(status, 200)
            self.assertEqual(state["view"]["label"], "Worktree · linked")
            _, listing = self.request(server, "GET", "/api/paths")
            self.assertEqual(set(listing["paths"]), {".gitignore", "dir/sample.py", "untracked.txt"})
            request = {"snapshot": state["snapshot"], "path": "dir/sample.py"}
            self.assertEqual(self.request(server, "POST", "/api/file", request, **headers),
                             (200, {"text": "linked contents\n"}))
            for path in ("../outside.txt", ".git", "ignored.txt", "removed.txt"):
                self.assertEqual(self.request(server, "POST", "/api/file", {**request, "path": path},
                                              **headers)[0], 404)
            (linked / "dir" / "sample.py").unlink()
            try:
                (linked / "dir" / "sample.py").symlink_to(self.root / "dir" / "sample.py")
            except OSError as error:
                if getattr(error, "winerror", None) != 1314:
                    raise
            else:
                self.assertEqual(self.request(server, "POST", "/api/file", request, **headers)[0], 404)
            self.write("dir/sample.py", "current contents\n")
            status, state = self.request(server, "POST", "/api/view",
                                         {"kind": "worktrees", "ref": "WORKTREE"}, **headers)
            self.assertEqual(status, 200)
            self.assertEqual(self.request(server, "POST", "/api/file",
                                          {**request, "snapshot": state["snapshot"]}, **headers),
                             (200, {"text": "current contents\n"}))
            self.assertEqual(self.request(server, "POST", "/api/file", request, **headers)[0], 409)

    def test_view_is_read_only_and_switching_back_restores_comparison(self):
        with self.server(job_factory=Jobs) as server:
            headers = {"Origin": server.url, "X-Change-Tree-Token": server.token}
            selection = {"kind": "branches", "ref": "HEAD"}
            self.assertEqual(self.request(server, "POST", "/api/view", selection)[0], 403)
            for invalid in ({}, {"kind": "branches", "ref": "missing"},
                            {"kind": "worktrees", "ref": str(self.root.parent)}):
                self.assertEqual(self.request(server, "POST", "/api/view", invalid, **headers)[0], 400)
            status, state = self.request(server, "POST", "/api/view", selection, **headers)
            self.assertEqual(status, 200)
            self.assertEqual(state["summaryStatus"], "idle")
            self.assertEqual(state["summaries"], {})
            self.assertEqual(state["lastComparison"]["head"], "WORKTREE")
            self.assertEqual(self.request(server, "POST", "/api/jobs/claim", {}, **headers),
                             (200, {"job": None}))
            request = {"snapshot": state["snapshot"], "path": "dir/sample.py"}
            self.assertEqual(self.request(server, "POST", "/api/file", request)[0], 403)
            self.assertEqual(self.request(server, "POST", "/api/save", request, **headers)[0], 403)
            self.assertEqual(self.request(server, "POST", "/api/chat", request, **headers)[0], 409)
            status, comparison = self.request(server, "POST", "/api/compare",
                                              {"base": "HEAD", "head": "WORKTREE"}, **headers)
            self.assertEqual(status, 200)
            self.assertNotIn("view", comparison)
            self.assertTrue(comparison["comparison"]["editable"])
            self.assertEqual(comparison["files"][0]["path"], request["path"])
            self.assertEqual(self.request(server, "POST", "/api/file", request, **headers)[0], 409)
            status, claim = self.request(server, "POST", "/api/jobs/claim", {}, **headers)
            self.assertEqual(status, 200)
            self.assertEqual(claim["job"]["kind"], "files")

    def test_fork_pull_head_remains_available_after_unlinking(self):
        base = self.run_git("rev-parse", "HEAD").decode().strip()
        head = self.commit("Topic head")
        self.run_git("remote", "add", "fixture", str(self.root))
        self.run_git("update-ref", "refs/pull/11/head", head)
        state = State(self.root, analyze, cache=self.cache)
        synthetic = "refs/change-tree/pr/11"
        pull = {"base": base, "head": synthetic, "headLabel": "topic", "value": 11,
                "remote": "fixture", "fetchHead": True}
        state.result["refs"] = {"prs": [pull]}
        linked = state.compare({"base": base, "head": synthetic, "pr": 11})
        self.assertEqual(linked["comparison"]["pr"], 11)
        self.assertEqual(linked["comparison"]["headCommit"], head)
        self.assertEqual(linked["comparison"]["headLabel"], "topic")
        unlinked = state.compare({"base": base, "head": synthetic})
        self.assertIsNone(unlinked["comparison"]["pr"])
        self.assertEqual(unlinked["comparison"]["headCommit"], head)

    def test_http_token_origin_and_worktree_only_save(self):
        with self.server() as server:
            same_origin = {"Origin": server.url, "X-Change-Tree-Token": server.token}
            self.assertEqual(self.request(server, "GET", "/api/state", Host="another.test")[0], 403)
            self.assertEqual(self.request(server, "POST", "/api/compare", {})[0], 403)
            self.assertEqual(self.request(server, "POST", "/api/compare", {}, Origin=server.url)[0], 403)
            self.assertEqual(self.request(server, "POST", "/api/compare", {}, Origin="https://another.test",
                                         **{"X-Change-Tree-Token": server.token})[0], 403)
            _, state = self.request(server, "GET", "/api/state")
            self.assertTrue(state["comparison"]["editable"])
            self.assertEqual(self.request(server, "POST", "/api/save",
                                         {"snapshot": state["snapshot"], "path": "unchanged.py"}, **same_origin)[0], 404)
            file = state["files"][0]
            status, revised = self.request(server, "POST", "/api/save", {
                "path": file["path"], "snapshot": state["snapshot"], "hash": file["contentHash"],
                "edits": [{"index": 0, "text": "first = 100\nsecond = 2"}],
            }, **same_origin)
            self.assertEqual(status, 200)
            self.assertNotEqual(revised["snapshot"], state["snapshot"])
            self.assertEqual((self.root / file["path"]).read_bytes(), b"first = 100\nsecond = 2\n")
            self.assertEqual(self.request(server, "POST", "/api/compare",
                                          {"base": "HEAD", "head": "HEAD"}, **same_origin)[0], 200)
            self.assertEqual(self.request(server, "POST", "/api/save", {}, **same_origin)[0], 403)


class PortTests(unittest.TestCase):
    def test_port_option_accepts_only_valid_ports(self):
        import argparse
        from ct import port_number
        self.assertEqual(port_number("4747"), 4747)
        self.assertEqual(port_number("0"), 0)
        for value in ("70000", "-1", "web"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                port_number(value)

    def test_busy_port_gives_a_clear_error(self):
        import socket
        from types import SimpleNamespace
        from repo import ChangeTreeError
        from server import serve
        with socket.socket() as busy, TemporaryDirectory() as folder:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            args = SimpleNamespace(agent="claude", model=None, effort=None, port=port, base="HEAD", head="WORKTREE", task="")
            with patch("server.server_info", return_value=None), patch("server.load_settings", return_value={}), \
                    self.assertRaises(ChangeTreeError) as error:
                serve(Path(folder), lambda *_: None, args)
            self.assertIn(f"Port {port} is in use", str(error.exception))


if __name__ == "__main__":
    unittest.main()
