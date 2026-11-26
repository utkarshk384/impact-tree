import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skill/change-tree/scripts"))
import impact
import repo


class GitRepoMixin:
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.run_git("init", "-b", "main")
        self.run_git("config", "user.name", "Fixture")
        self.run_git("config", "user.email", "fixture@example.test")
        self.run_git("config", "commit.gpgsign", "false")

    def run_git(self, *args):
        return repo.git(self.root, *args)

    def write(self, path, contents):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(contents, bytes):
            file.write_bytes(contents)
        else:
            file.write_text(contents, encoding="utf-8")
        return file

    def commit(self, message="fixture"):
        self.run_git("add", "-A")
        self.run_git("commit", "-qm", message)
        return repo.commit(self.root, "HEAD")

    def diff(self, base="HEAD", head="WORKTREE"):
        comp = repo.comparison(self.root, base, head)
        return comp, {file["path"]: file for file in repo.parse_diff(repo.read_diff(comp))}


class GitFixture(GitRepoMixin, unittest.TestCase):
    def test_worktree_reads_disk_includes_untracked_and_preserves_index(self):
        self.write(".gitignore", "ignored.txt\n")
        self.write("main.py", "old\n")
        self.commit()
        self.write("main.py", "staged\n")
        self.run_git("add", "main.py")
        self.write("main.py", "on disk\n")
        self.write("new file é.py", "a = 1")
        self.write("ignored.txt", "ignore me\n")
        self.write("graphify-out/generated.json", "{}")
        self.write("image.dat", b"\x00\x01")
        index = Path(repo.decode(self.run_git("rev-parse", "--path-format=absolute", "--git-path", "index")).strip())
        before = index.read_bytes()
        _, files = self.diff()
        self.assertEqual(set(files), {"main.py", "new file é.py", "image.dat"})
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(files["main.py"]["hunks"][0]["lines"][1]["text"], "on disk")
        self.assertEqual(files["new file é.py"]["hunks"][0]["lines"][0]["text"], "a = 1")
        self.assertEqual(files["image.dat"]["note"], "Binary file")
        self.assertEqual(files["image.dat"]["hunks"], [])

    def test_plain_rename_detected_with_literal_names(self):
        before = self.write("old\tfile.py", "def function():\n    return 123\n")
        self.commit()
        before.rename(self.root / "new\nfile.py")
        _, files = self.diff()
        new = files["new\nfile.py"]
        self.assertEqual(new["status"], "M")
        self.assertEqual(new["oldPath"], "old\tfile.py")
        self.assertEqual(new["hunks"], [])

    def test_hunk_gap_and_content_hash(self):
        self.write("source.py", "".join(f"L{i}\n" for i in range(1, 41)))
        self.commit()
        changed = (self.root / "source.py").read_text().replace("L13\n", "replacement\n").replace("L33\n", "")
        self.write("source.py", changed)
        _, files = self.diff()
        file = files["source.py"]
        first, second = file["hunks"]
        self.assertEqual((first["oldStart"], first["newStart"], first["gap"]), (10, 10, 9))
        self.assertEqual((second["oldStart"], second["newStart"], second["gap"]), (30, 30, 13))
        self.assertEqual((second["oldCount"], second["newCount"]), (7, 6))
        repo.annotate_editable(self.root, [file])
        self.assertEqual(file["contentHash"], hashlib.sha256(changed.encode()).hexdigest())

    def test_three_dot_and_secondary_worktree(self):
        self.write("common", "base\n")
        initial = self.commit()
        self.run_git("branch", "topic")
        self.write("only-main", "main\n")
        self.commit()
        other = self.root / "checkout"
        self.run_git("worktree", "add", str(other), "topic")
        (other / "topic.txt").write_text("topic\n", encoding="utf-8")
        repo.git(other, "add", "topic.txt")
        repo.git(other, "commit", "-qm", "Topic")
        (other / "untracked.txt").write_text("worktree\n", encoding="utf-8")
        comp, files = self.diff("main", "topic")
        self.assertEqual(comp["diffBase"], initial)
        self.assertEqual(set(files), {"topic.txt"})
        comp, files = self.diff("main", str(other))
        self.assertEqual(set(files), {"only-main", "topic.txt", "untracked.txt"})
        self.assertFalse(comp["editable"])

    def test_css_and_deleted_importers(self):
        self.write("src/base.css", "@import './outgoing.css';\n")
        self.write("src/outgoing.css", ".example { color: red }\n")
        self.write("src/View.tsx", "import './base.css';\n")
        self.commit()
        self.write("src/base.css", "@import './incoming.css';\n")
        self.write("src/incoming.css", ".example { color: blue }\n")
        (self.root / "src/outgoing.css").unlink()
        comp, files = self.diff()
        layout = impact.analyze(self.root, comp, {"nodes": [], "links": []}, list(files.values()))
        self.assertEqual(files["src/outgoing.css"]["previousImporters"], ["src/base.css"])
        self.assertEqual(files["src/incoming.css"]["usedBy"], ["src/base.css", "src/View.tsx"])
        self.assertEqual(layout["positions"]["src/base.css"]["column"], 1)
        deleted = files["src/outgoing.css"]
        self.assertEqual(deleted["parent"], "src/base.css")
        self.assertEqual(layout["positions"]["src/outgoing.css"]["column"], 2)
        self.assertEqual(deleted["why"], "deleted · base.css imported it")
        self.assertGreater(deleted["impact"]["reach"], 0)

    def test_preflight_and_graph_bootstrap_behavior(self):
        self.write("source.py", "x = 1\n")
        self.commit()
        with patch("repo.shutil.which", return_value=None):
            with self.assertRaisesRegex(repo.ChangeTreeError, "graphify on PATH") as error:
                repo.preflight(self.root)
        self.assertEqual(error.exception.code, 2)
        graph_file = self.root / "graphify-out/graph.json"
        calls = []

        def fake_update(args, cwd, **kwargs):
            calls.append(args)
            self.write("graphify-out/graph.json", '{"nodes":[],"links":[]}')
            return type("Result", (), {"returncode": 0})()

        with patch("impact.command", side_effect=fake_update):
            impact.refresh_graph(self.root)
            impact.refresh_graph(self.root)
            self.write("source.py", "x = 2\n")
            impact.refresh_graph(self.root)
        self.assertEqual(calls, [["graphify", "update", str(self.root)]] * 2)
        self.assertEqual(json.loads(graph_file.read_text())["built_at_commit"], repo.commit(self.root, "HEAD"))
        graph_file.unlink()
        with patch("impact.command"):
            with self.assertRaisesRegex(repo.ChangeTreeError, "Run /graphify") as error:
                impact.refresh_graph(self.root)
        self.assertEqual(error.exception.code, 3)


class ImpactTest(unittest.TestCase):
    def test_graph_direction_and_manifest_concepts(self):
        graph = {
            "directed": False,
            "nodes": [{"id": "b", "source_file": "dependency.ts"},
                      {"id": "a", "source_file": "importer.ts"},
                      {"id": "p", "source_file": "package.json", "file_type": "concept", "label": "react"},
                      {"id": "d", "source_file": "unrelated.ts"}],
            "links": [{"source": "b", "target": "a", "source_file": "importer.ts", "relation": "imports"},
                      {"source": "a", "target": "p", "relation": "imports_from"},
                      {"source": "a", "target": "d", "relation": "contains"}],
        }
        self.assertEqual(impact.graph_edges(graph, Path("/test")), {"importer.ts": {"dependency.ts"}})
        self.assertEqual(impact.graph_edges(graph, Path("/test"), {"package.json": {"react"}}),
                         {"importer.ts": {"dependency.ts", "package.json"}})

    def test_score_weights_and_reverse_reach(self):
        paths = {"first.py": {"second.py"}, "second.py": {"third.py"}}
        self.assertEqual(impact.reach_from("first.py", paths), {"second.py": 1, "third.py": 2})
        file = {"added": 500, "removed": 500}
        impact.score_file(file, {"second.py": 1, "third.py": 2}, 80, "API change", {"second.py"}, 3)
        self.assertEqual((file["score"], file["band"]), (59, "medium"))
        self.assertIn("1 changed file uses it", file["why"])

    def test_widely_imported_file_outranks_leaf_after_calibration(self):
        paths = {"core.ts", "leaf.ts", *(f"caller{i}.ts" for i in range(20))}
        reverse = {"core.ts": {f"caller{i}.ts" for i in range(20)}, "leaf.ts": {"caller0.ts"}}
        reference = impact.reach_reference(paths, reverse)
        core, leaf = ({"added": 10, "removed": 5} for _ in range(2))
        for file, path in ((core, "core.ts"), (leaf, "leaf.ts")):
            impact.score_file(file, impact.reach_from(path, reverse), 55, "new file", paths, reference)
        self.assertEqual(core["band"], "high")
        self.assertGreater(core["score"], leaf["score"])

        reverse.update({path: {"caller0.ts", "caller1.ts", "caller2.ts", "caller3.ts"}
                        for path in paths if path.startswith("caller")})
        busier_reference = impact.reach_reference(paths, reverse)
        self.assertGreater(busier_reference, reference)
        prior = leaf["impact"]["reach"]
        impact.score_file(leaf, {"caller0.ts": 1}, 55, "new file", paths, busier_reference)
        self.assertLess(leaf["impact"]["reach"], prior)

    def test_children_follow_their_parent_rows_before_score(self):
        paths = [("api.ts", 96), ("config.ts", 93), ("helper.ts", 89), ("viewer.ts", 60),
                 ("removed.ts", 87), ("page.ts", 30)]
        files = [{"path": path, "score": score, "usedBy": []} for path, score in paths]
        edges = {"helper.ts": {"api.ts"}, "viewer.ts": {"config.ts"},
                 "removed.ts": {"viewer.ts"}, "page.ts": {"helper.ts"}}
        dropped = {"removed.ts": {"viewer.ts"}}
        result = impact.place_tree(files, edges, dropped)["positions"]
        self.assertLess(result["helper.ts"]["y"], result["viewer.ts"]["y"])
        self.assertLess(result["page.ts"]["y"], result["removed.ts"]["y"])

    def test_layout_transitive_parent_and_cycle(self):
        rows = [("api.ts", 89), ("client.ts", 75), ("cycle.ts", 68), ("page.ts", 55),
                ("isolated.py", 13), ("README.md", 5), (".gitignore", 3)]
        files = [{"path": name, "score": score, "usedBy": []} for name, score in rows]
        edges = {"client.ts": {"helper.ts", "cycle.ts"}, "cycle.ts": {"client.ts"},
                 "helper.ts": {"api.ts"}, "page.ts": {"client.ts"}}
        layout = impact.place_tree(files, edges)
        pos = layout["positions"]
        self.assertEqual([pos[name]["column"] for name, _ in rows], [0, 1, 1, 2, 0, None, None])
        self.assertEqual([pos[name]["x"] for name in ("api.ts", "client.ts", "page.ts")], [196, 540, 884])
        self.assertEqual(pos["isolated.py"]["band"][0] - pos["api.ts"]["band"][1], 48)
        self.assertGreater(pos["README.md"]["y"], pos["isolated.py"]["y"] + 96)
        page = files[3]
        self.assertEqual(page["parent"], "api.ts")
        self.assertEqual(page["extraDependencies"], ["client.ts", "cycle.ts"])
        self.assertTrue(files[-1]["standalone"])
        self.assertEqual(pos["README.md"]["y"], pos[".gitignore"]["y"])


if __name__ == "__main__":
    unittest.main()
