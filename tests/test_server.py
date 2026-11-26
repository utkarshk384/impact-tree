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
            (self.root / "dir").symlink_to(outside, target_is_directory=True)
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
    def server(self):
        state = State(self.root, analyze, "fixture", cache=self.cache)
        state.compare({"base": "HEAD", "head": "WORKTREE"})
        with Server(state, 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                yield server
            finally:
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

    def test_fork_pull_head_remains_available_after_unlinking(self):
        base = self.run_git("rev-parse", "HEAD").decode().strip()
        head = self.commit("Topic head")
        self.run_git("remote", "add", "fixture", str(self.root))
        self.run_git("update-ref", "refs/pull/11/head", head)
        state = State(self.root, analyze, "fixture", cache=self.cache)
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


if __name__ == "__main__":
    unittest.main()
