"""Apply worktree edits using server-owned hunk coordinates."""
from contextlib import ExitStack
import errno
import hashlib
import os
from pathlib import PurePosixPath
import secrets
import stat

from repo import WINDOWS_READ_ONLY, WORKTREE_EDITING_SUPPORTED, hunk_matches


class EditError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


CHANGED = "The file changed on disk. Compare again to load the latest version."


def read_file(directory, name):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(name, flags, dir_fd=directory), "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise EditError(CHANGED, 409)
        return handle.read(8_000_001), info


def revised_content(data, hunks, edits):
    text = data.decode("utf-8")
    rows = text.split("\n")
    trailing_newline = text.endswith("\n")
    if trailing_newline or not text:
        rows.pop()
    changes = []
    seen = set()
    for edit in edits:
        if not isinstance(edit, dict):
            raise EditError("Invalid edited block.")
        block, replacement = edit.get("index"), edit.get("text")
        if type(block) is not int or block in seen or not 0 <= block < len(hunks):
            raise EditError("Invalid edited block.")
        if not isinstance(replacement, str) or "\x00" in replacement:
            raise EditError("Invalid edited text.")
        try:
            replacement.encode("utf-8")
        except UnicodeEncodeError as error:
            raise EditError("Edited text must be valid UTF-8.") from error
        seen.add(block)
        hunk = hunks[block]
        count = hunk["newCount"]
        start = hunk["newStart"] - 1 if count else hunk["newStart"]
        if not 0 <= start <= len(rows) or start + count > len(rows) or not hunk_matches(rows, hunk):
            raise EditError(CHANGED, 409)
        old_rows = rows[start:start + count]
        # The browser normalizes textarea line endings. Keep untouched rows
        # byte-for-byte and match the newline convention for replacements.
        crlf = sum(row.endswith("\r") for row in old_rows) > len(old_rows) / 2
        if not old_rows:
            crlf = text.count("\r\n") > text.count("\n") / 2
        replacement_rows = replacement.replace("\r\n", "\n").split("\n") if replacement else []
        if crlf:
            replacement_rows = [row + "\r" if i < len(replacement_rows) - 1 or
                                start + count < len(rows) or trailing_newline else row
                                for i, row in enumerate(replacement_rows)]
        changes.append((start, count, replacement_rows))
    for start, count, replacement in sorted(changes, reverse=True):
        rows[start:start + count] = replacement
    return ("\n".join(rows) + ("\n" if rows and trailing_newline else "")).encode("utf-8")


def save_file(root, file, expected_hash, edits):
    if not WORKTREE_EDITING_SUPPORTED:
        raise EditError(WINDOWS_READ_ONLY, 403)
    raw_path = file["path"]
    parts = PurePosixPath(raw_path).parts
    if (not parts or raw_path.startswith("/") or parts[0] == "graphify-out"
            or any(part in (".git", "..") for part in parts)):
        raise EditError("The file path must stay inside the repository.")
    if not file.get("contentHash"):
        raise EditError("This file cannot be edited.")
    if expected_hash != file["contentHash"]:
        raise EditError(CHANGED, 409)
    if not isinstance(edits, list) or not edits or len(edits) > len(file["hunks"]):
        raise EditError("Send at least one edited block.")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as directories:
        fd = os.open(root, directory_flags)
        directories.callback(os.close, fd)
        name = parts[-1]
        temp = None
        try:
            for part in parts[:-1]:
                fd = os.open(part, directory_flags, dir_fd=fd)
                directories.callback(os.close, fd)
            data, info = read_file(fd, name)
            if hashlib.sha256(data).hexdigest() != expected_hash:
                raise EditError(CHANGED, 409)
            revised = revised_content(data, file["hunks"], edits)
            if revised == data:
                return False
            temp = ".change-tree-" + secrets.token_hex(12)
            with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd), "wb") as handle:
                handle.write(revised)
                os.fchmod(handle.fileno(), stat.S_IMODE(info.st_mode) & 0o777)
                handle.flush()
                os.fsync(handle.fileno())
            current, current_info = read_file(fd, name)
            parent = os.stat(root.joinpath(*parts[:-1]), follow_symlinks=False)
            open_parent = os.fstat(fd)
            if current != data or not os.path.samestat(info, current_info) or not os.path.samestat(parent, open_parent):
                raise EditError(CHANGED, 409)
            os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
            temp = None
            return True
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError) as error:
            raise EditError(CHANGED, 409) from error
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise EditError(CHANGED, 409) from error
            raise
        finally:
            if temp is not None:
                os.unlink(temp, dir_fd=fd)
