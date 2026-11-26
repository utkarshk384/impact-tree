import hashlib
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_analysis import GitRepoMixin
import edits
import repo


class WindowsEditFixture(GitRepoMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.name = "dir/nested/sample.txt"
        self.write(self.name, b"before\nnext\n")
        self.commit()
        self.path = self.write(self.name, b"current\nnext\n")
        self.file = self.editable_file()
        self.blocks = [{"index": 0, "text": "updated\nnext"}]

    def editable_file(self):
        comparison, files = self.diff()
        self.assertTrue(comparison["editable"])
        file = files[self.name]
        with patch.object(repo, "os", SimpleNamespace(name="nt")):
            repo.annotate_editable(self.root, [file])
        self.assertEqual(file["contentHash"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        return file

    def save(self, *, path=None):
        file = self.file if path is None else {**self.file, "path": path}
        return edits.save_windows_file(self.root, file, file["contentHash"], self.blocks)

    def assert_temp_removed(self):
        self.assertEqual(list(self.path.parent.glob(".change-tree-*")), [])

    def test_save_preserves_git_index(self):
        self.assertTrue(self.save())
        self.assertEqual(self.path.read_bytes(), b"updated\nnext\n")
        self.assertEqual(self.run_git("diff", "--cached"), b"")
        self.assert_temp_removed()

    def test_save_preserves_crlf_when_git_normalizes_and_last_line_is_unterminated(self):
        self.run_git("config", "core.autocrlf", "true")
        self.path.write_bytes(b"current\r\nnext")
        self.file = self.editable_file()
        self.assertTrue(self.save())
        self.assertEqual(self.path.read_bytes(), b"updated\r\nnext")

    def test_save_refuses_symlinked_parent(self):
        with TemporaryDirectory() as temporary:
            outside = Path(temporary) / "outside"
            self.path.parent.rename(outside)
            try:
                self.path.parent.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                if getattr(error, "winerror", None) != 1314:
                    raise
                self.skipTest("Windows user cannot create symbolic links")
            with self.assertRaisesRegex(edits.EditError, edits.CHANGED):
                self.save()
            self.assertEqual((outside / self.path.name).read_bytes(), b"current\nnext\n")

    def test_save_and_annotation_refuse_reparse_points(self):
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if path == self.path.parent:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return info

        file = {key: value for key, value in self.file.items() if key != "contentHash"}
        with patch("edits.os.lstat", side_effect=lstat):
            with patch.object(repo, "os", SimpleNamespace(name="nt")):
                repo.annotate_editable(self.root, [file])
            self.assertNotIn("contentHash", file)
            with self.assertRaisesRegex(edits.EditError, edits.CHANGED):
                self.save()

    def test_save_refuses_paths_with_windows_specific_traversal_or_aliases(self):
        for path in ("../sample.txt", "dir/../sample.txt", "/outside.txt", "C:/outside.txt",
                     "C:outside.txt", r"dir\..\sample.txt", r"\outside.txt", r"\\server\share\file.txt",
                     "dir/nested/sample.txt:stream", "dir/CON.txt", ".GIT/config", "Graphify-Out/artifact",
                     ".git./config", "dir /nested/sample.txt"):
            with self.subTest(path=path), self.assertRaisesRegex(edits.EditError, edits.OUTSIDE_REPO):
                self.save(path=path)

    def test_save_checks_resolved_path_stays_under_root(self):
        outside = self.root.with_name(self.root.name + "-other") / self.path.name
        with patch.object(type(self.path), "resolve", return_value=outside):
            with self.assertRaisesRegex(edits.EditError, edits.OUTSIDE_REPO):
                self.save()

    def test_save_refuses_content_changed_before_replace(self):
        fsync = os.fsync

        def change_file(fd):
            fsync(fd)
            self.path.write_bytes(b"external edit\nnext\n")

        with patch("edits.os.fsync", side_effect=change_file):
            with self.assertRaisesRegex(edits.EditError, edits.CHANGED) as error:
                self.save()
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.path.read_bytes(), b"external edit\nnext\n")
        self.assert_temp_removed()

    def test_save_refuses_replaced_ancestor_with_the_same_file(self):
        read = edits.read_windows_editable

        def replace_ancestor_before_reread(root, parts):
            if list(self.path.parent.glob(".change-tree-*")):
                ancestor = self.path.parent.parent
                moved = ancestor.rename(root / "moved")
                ancestor.mkdir()
                (moved / self.path.parent.name).rename(self.path.parent)
            return read(root, parts)

        with patch("edits.read_windows_editable", side_effect=replace_ancestor_before_reread):
            with self.assertRaisesRegex(edits.EditError, edits.CHANGED):
                self.save()
        self.assertEqual(self.path.read_bytes(), b"current\nnext\n")
        self.assert_temp_removed()

    def test_save_refuses_replaced_file_with_the_same_content(self):
        fsync = os.fsync
        replacement = self.path.with_name("replacement.txt")
        replacement.write_bytes(self.path.read_bytes())

        def change_file(fd):
            fsync(fd)
            replacement.replace(self.path)

        with patch("edits.os.fsync", side_effect=change_file):
            with self.assertRaisesRegex(edits.EditError, edits.CHANGED):
                self.save()
        self.assertEqual(self.path.read_bytes(), b"current\nnext\n")
        self.assert_temp_removed()

    def test_save_maps_replace_permission_error_and_removes_temp(self):
        with patch("edits.os.replace", side_effect=PermissionError("sharing violation")):
            with self.assertRaisesRegex(edits.EditError, edits.WINDOWS_REPLACE_DENIED) as error:
                self.save()
        self.assertEqual(error.exception.status, 423)
        self.assertEqual(self.path.read_bytes(), b"current\nnext\n")
        self.assert_temp_removed()

    def test_save_refuses_read_only_target_before_creating_temp(self):
        self.path.chmod(stat.S_IREAD)
        self.addCleanup(self.path.chmod, stat.S_IREAD | stat.S_IWRITE)

        with patch("edits.secrets.token_hex") as temp_name:
            with self.assertRaises(edits.EditError) as error:
                self.save()
        temp_name.assert_not_called()
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(str(error.exception), "The file is read-only. Clear the read-only attribute and try again.")
        self.assertEqual(self.path.read_bytes(), b"current\nnext\n")
        self.assertFalse(self.path.stat().st_mode & stat.S_IWRITE)
        self.assert_temp_removed()
