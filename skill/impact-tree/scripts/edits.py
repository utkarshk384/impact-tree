"""Apply worktree edits using server-owned hunk coordinates."""
from contextlib import ExitStack
import errno
import hashlib
import ntpath
import os
from pathlib import PurePosixPath, PureWindowsPath
import secrets
import stat


class EditError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


CHANGED = "The file changed on disk. Compare again to load the latest version."
OUTSIDE_REPO = "The file path must stay inside the repository."
WINDOWS_REPLACE_DENIED = "Close the file in other programs and try again."


def read_file(directory, name):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(name, flags, dir_fd=directory), "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise EditError(CHANGED, 409)
        return handle.read(8_000_001), info


def hunk_matches(lines, hunk):
    count = hunk["newCount"]
    start = hunk["newStart"] - 1 if count else hunk["newStart"]
    expected = [line["text"] for line in hunk["lines"] if line["kind"] in ("add", "context")]
    return [line.removesuffix("\r") for line in lines[start:start + count]] == [
        line.removesuffix("\r") for line in expected
    ]


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
        ended_rows = old_rows[:-1] if start + count == len(rows) and not trailing_newline else old_rows
        crlf = sum(row.endswith("\r") for row in ended_rows) > len(ended_rows) / 2
        if not ended_rows:
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


def relative_edit_parts(raw_path):
    parts = PurePosixPath(raw_path).parts
    if (not parts or raw_path.startswith("/") or parts[0] == "graphify-out"
            or any(part in (".git", "..") for part in parts)):
        raise EditError(OUTSIDE_REPO)
    return parts


def validate_edit(file, expected_hash, edits):
    if not file.get("contentHash"):
        raise EditError("This file cannot be edited.")
    if expected_hash != file["contentHash"]:
        raise EditError(CHANGED, 409)
    if not isinstance(edits, list) or not edits or len(edits) > len(file["hunks"]):
        raise EditError("Send at least one edited block.")


def save_file(root, file, expected_hash, edits):
    if os.name == "nt":
        return save_windows_file(root, file, expected_hash, edits)
    parts = relative_edit_parts(file["path"])
    validate_edit(file, expected_hash, edits)
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


def windows_edit_parts(raw_path):
    parts = relative_edit_parts(raw_path)
    is_reserved = getattr(ntpath, "isreserved", lambda name: PureWindowsPath(name).is_reserved())
    if (parts[0].lower() == "graphify-out"
            or any(part.lower() == ".git" or "\\" in part or ":" in part
                   or part.endswith((" ", ".")) or is_reserved(part) for part in parts)):
        raise EditError(OUTSIDE_REPO)
    return parts


def is_reparse_point(info):
    return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def windows_stat(path, is_expected_type):
    info = os.lstat(path)
    is_junction = getattr(path, "is_junction", None)
    if (not is_expected_type(info.st_mode) or is_reparse_point(info)
            or (is_junction is not None and is_junction())):
        raise EditError(CHANGED, 409)
    return info


def inspect_windows_path(root, parts):
    root = root.absolute()
    path = root
    directories = [windows_stat(path, stat.S_ISDIR)]
    for part in parts[:-1]:
        path = path / part
        directories.append(windows_stat(path, stat.S_ISDIR))
    path = path / parts[-1]
    info = windows_stat(path, stat.S_ISREG)
    try:
        resolved = PureWindowsPath(path.resolve(strict=True))
    except RuntimeError as error:  # A link loop on older Python versions.
        raise EditError(CHANGED, 409) from error
    if not resolved.is_relative_to(PureWindowsPath(root)):
        raise EditError(OUTSIDE_REPO)
    # Reject aliases such as short names for protected directories.
    if resolved != PureWindowsPath(path):
        raise EditError(CHANGED, 409)
    return path, directories, info


def same_windows_entry(before, after):
    if before.st_ino or after.st_ino:
        return os.path.samestat(before, after)
    # Creation time is stable across writes and supplies a weaker check without file IDs.
    before_created = getattr(before, "st_birthtime_ns", before.st_ctime_ns)
    after_created = getattr(after, "st_birthtime_ns", after.st_ctime_ns)
    return before.st_dev == after.st_dev and before_created == after_created


def same_windows_path(directories, info, current_directories, current_info):
    return (
        info.st_mode == current_info.st_mode and same_windows_entry(info, current_info)
        and all(same_windows_entry(before, after) for before, after in zip(directories, current_directories))
    )


def read_windows_editable(root, parts):
    path, directories, info = inspect_windows_path(root, parts)
    if info.st_size > 8_000_000:
        raise EditError(CHANGED, 409)
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or is_reparse_point(opened) or not same_windows_entry(info, opened):
            raise EditError(CHANGED, 409)
        data = handle.read(8_000_001)
    _, current_directories, current_info = inspect_windows_path(root, parts)
    if len(data) > 8_000_000 or not same_windows_path(directories, info, current_directories, current_info):
        raise EditError(CHANGED, 409)
    return path, directories, info, data


def clean_windows_temp(temp, original):
    try:
        current = windows_stat(temp, stat.S_ISREG)
        if original is not None and not same_windows_entry(original, current):
            return
        # Windows refuses to unlink read-only files.
        if not current.st_mode & stat.S_IWRITE:
            os.chmod(temp, stat.S_IREAD | stat.S_IWRITE)
        temp.unlink()
    except (EditError, OSError):
        pass  # Keep the original save failure if the temp file is no longer accessible.


def save_windows_file(root, file, expected_hash, edits):
    parts = windows_edit_parts(file["path"])
    validate_edit(file, expected_hash, edits)
    temp = temp_info = None
    try:
        path, directories, info, data = read_windows_editable(root, parts)
        if hashlib.sha256(data).hexdigest() != expected_hash:
            raise EditError(CHANGED, 409)
        revised = revised_content(data, file["hunks"], edits)
        if revised == data:
            return False
        _, current_directories, current_info = inspect_windows_path(root, parts)
        if not same_windows_path(directories, info, current_directories, current_info):
            raise EditError(CHANGED, 409)
        if not info.st_mode & stat.S_IWRITE:
            raise EditError("The file is read-only. Clear the read-only attribute and try again.", 403)
        candidate = path.with_name(".change-tree-" + secrets.token_hex(12))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(candidate, flags, 0o600)
        temp = candidate
        with os.fdopen(fd, "wb") as handle:
            temp_info = os.fstat(handle.fileno())
            handle.write(revised)
            handle.flush()
            os.fsync(handle.fileno())
        _, current_directories, current_info, current = read_windows_editable(root, parts)
        if current != data or not same_windows_path(directories, info, current_directories, current_info):
            raise EditError(CHANGED, 409)
        if not same_windows_entry(temp_info, windows_stat(temp, stat.S_ISREG)):
            raise EditError(CHANGED, 409)
        os.chmod(temp, stat.S_IREAD | (info.st_mode & stat.S_IWRITE))
        try:
            os.replace(temp, path)
        except PermissionError as error:
            raise EditError(WINDOWS_REPLACE_DENIED, 423) from error
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
            clean_windows_temp(temp, temp_info)
