"""Git comparisons and reference discovery."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory


class ChangeTreeError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def command(args, cwd, *, env=None, input=None, timeout=None):
    return subprocess.run(
        args, cwd=cwd, env=env, input=input, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def decode(data):
    return data.decode("utf-8", errors="replace")


def git(root, *args, env=None, input=None, timeout=None):
    result = command(["git", "--no-optional-locks", "-C", str(root), *args], None, env=env, input=input, timeout=timeout)
    if result.returncode:
        raise ChangeTreeError(decode(result.stderr).strip() or "Git command failed.")
    return result.stdout


def repo_root(path):
    try:
        return Path(os.fsdecode(git(path, "rev-parse", "--show-toplevel").rstrip(b"\n"))).resolve()
    except (ChangeTreeError, OSError) as error:
        raise ChangeTreeError("Impact Tree requires a Git working tree.", 2) from error


def preflight(path):
    if not shutil.which("graphify"):
        raise ChangeTreeError("Impact Tree requires graphify on PATH.", 2)
    return repo_root(path)


def commit(root, ref):
    return decode(git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")).strip()


def worktrees(root):
    raw = git(root, "worktree", "list", "--porcelain", "-z")
    result = []
    for entry in raw.split(b"\0\0"):
        fields = {}
        for item in entry.strip(b"\0").split(b"\0"):
            key, _, value = item.partition(b" ")
            fields[key] = os.fsdecode(value)
        if b"worktree" not in fields or b"bare" in fields or b"prunable" in fields:
            continue
        path = Path(fields[b"worktree"]).resolve()
        branch = fields.get(b"branch", "").removeprefix("refs/heads/")
        is_current = path == root
        label = branch or fields.get(b"HEAD", "")[:7] or path.name
        result.append({
            "value": "WORKTREE" if is_current else str(path), "path": str(path),
            "label": f"Worktree · {label}", "commit": fields.get(b"HEAD"),
            "note": "This checkout, with its uncommitted changes" if is_current else str(path),
        })
    return result


WORKTREE_EDITING_SUPPORTED = os.name != "nt"
WINDOWS_READ_ONLY = "File editing is unavailable on Windows."
WORKTREE_PATH_PREFIXES = tuple(
    prefix + separator for prefix in (".", "..", "~") for separator in (os.sep, os.altsep) if separator
)


def comparison(root, base, head, task="", pr=None):
    base_commit = commit(root, base)
    target = root
    if head != "WORKTREE":
        if os.path.isabs(head) or head.startswith(WORKTREE_PATH_PREFIXES):
            target = (root / Path(head).expanduser()).resolve()
            if not any(Path(entry["path"]) == target for entry in worktrees(root)):
                raise ChangeTreeError("Compare path must be a worktree of this repository.")
            if target == root:
                head = "WORKTREE"
        else:
            candidate = (root / head).resolve()
            if candidate.exists() and any(Path(entry["path"]) == candidate for entry in worktrees(root)):
                target = candidate
                if target == root:
                    head = "WORKTREE"

    is_worktree = head == "WORKTREE" or target != root
    if is_worktree:
        head_commit = commit(target, "HEAD")
        diff_base = base_commit
        head_label = "Working tree" if head == "WORKTREE" else next(
            entry["label"] for entry in worktrees(root) if Path(entry["path"]) == target
        )
        semantics = "Base commit → working tree, including untracked files"
    else:
        head_commit = commit(root, head)
        diff_base = decode(git(root, "merge-base", base_commit, head_commit)).strip()
        head_label = head
        semantics = f"Three-dot diff · merge base {diff_base[:7]} → {head_label}"
    result = {
        "base": base, "head": str(target) if target != root else head, "task": task.strip(), "pr": pr,
        "baseLabel": f"HEAD · {base_commit[:7]}" if base == "HEAD" else base,
        "headLabel": head_label, "baseCommit": base_commit, "headCommit": head_commit,
        "diffBase": diff_base, "mode": "worktree" if is_worktree else "three-dot",
        "description": semantics, "target": str(target),
        "editable": head == "WORKTREE" and WORKTREE_EDITING_SUPPORTED,
    }
    if head == "WORKTREE" and not WORKTREE_EDITING_SUPPORTED:
        result["editUnavailable"] = WINDOWS_READ_ONLY
    return result


EXCLUDE_GRAPH = ":(exclude,top)graphify-out"
DIFF_FLAGS = ("-M", "--raw", "--numstat", "-z", "--patch", "--no-color", "--no-ext-diff",
              "--no-textconv", "--submodule=short", "--diff-merges=off", "--unified=3",
              "--abbrev=64", "--output-indicator-new=+", "--output-indicator-old=-",
              "--output-indicator-context= ")
MAX_DIFF_LINES = 3000
MAX_LINE_LENGTH = 16000
HUNK_HEADER = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def read_diff(comp):
    root = Path(comp["target"])
    diff = ("-c", "diff.suppressBlankEmpty=false", "diff")
    if comp["mode"] != "worktree":
        return git(root, *diff, *DIFF_FLAGS, comp["diffBase"], comp["headCommit"], "--", EXCLUDE_GRAPH)

    # Snapshot the disk into a disposable index and object store. This lets Git
    # pair renames to untracked paths without staging anything in the real repo.
    with TemporaryDirectory(prefix="change-tree-diff-") as scratch:
        temporary = Path(scratch)
        objects = temporary / "objects"
        objects.mkdir()
        actual_objects = git(root, "rev-parse", "--path-format=absolute", "--git-path", "objects")
        actual_index = git(root, "rev-parse", "--path-format=absolute", "--git-path", "index")
        index = Path(os.fsdecode(actual_index.rstrip(b"\n")))
        if index.is_file():
            shutil.copy2(index, temporary / "index")
        env = {
            **os.environ,
            "GIT_INDEX_FILE": str(temporary / "index"),
            "GIT_OBJECT_DIRECTORY": str(objects),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.pathsep.join(filter(None, (
                os.fsdecode(actual_objects.rstrip(b"\n")), os.environ.get("GIT_ALTERNATE_OBJECT_DIRECTORIES"),
            ))),
        }
        add = ("-c", "core.splitIndex=false", "-c", "core.fsmonitor=false", "add")
        git(root, *add, "-u", "--", ".", env=env)
        untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", ".", EXCLUDE_GRAPH)
        if untracked:
            git(root, "--literal-pathspecs", *add, "--pathspec-from-file=-", "--pathspec-file-nul",
                env=env, input=untracked)
        return git(root, *diff, *DIFF_FLAGS, "--cached", comp["diffBase"], "--", EXCLUDE_GRAPH, env=env)


def parse_diff(data):
    if not data:
        return []
    records, _, patch = data.partition(b"\0\0")
    tokens = iter(records.split(b"\0"))
    files = {}
    for token in tokens:
        if token.startswith(b":"):
            old_mode, new_mode, _, new_oid, raw_status = token[1:].split()
            code = raw_status[:1]
            old_path = os.fsdecode(next(tokens))
            path = os.fsdecode(next(tokens)) if code in (b"R", b"C") else old_path
            status = decode(code) if code in (b"A", b"D") else "M"
            file = {
                "path": path, "name": Path(path).name, "status": status,
                "added": 0, "removed": 0, "binary": False, "hunks": [],
                "newMode": decode(new_mode), "oldMode": decode(old_mode), "blob": decode(new_oid),
            }
            if code == b"R":
                file.update(oldPath=old_path, note=f"renamed from {old_path}")
            if b"160000" in (old_mode, new_mode):
                file["note"] = "Submodule reference"
            elif b"120000" in (old_mode, new_mode):
                file["note"] = "Symbolic link"
            files[path] = file
        else:
            add, remove, name = token.split(b"\t", 2)
            if not name:  # Renames encode both names as separate NUL records.
                next(tokens)
                name = next(tokens)
            file = files[os.fsdecode(name)]
            if add == b"-":
                file["binary"] = True
                file["note"] = "; ".join(filter(None, [file.get("note"), "Binary file"]))
            else:
                file["added"], file["removed"] = int(add), int(remove)

    # Mode changes can produce more than one patch for a single raw entry.
    # Read the selected path from the patch's new or deleted side headers.
    for section in re.split(rb"(?m)^diff --git ", patch)[1:]:
        headers, _, body = section.partition(b"\n@@ ")
        if not body:
            continue
        destinations = re.findall(rb"(?m)^(?:\+\+\+|---) (.+)$", headers)
        destination = destinations[-1] if destinations[-1] != b"/dev/null" else destinations[-2]
        path = os.fsdecode(unquote_git_path(destination.rstrip(b"\t"))[2:])
        file = files[path]
        if not file["binary"]:
            parse_hunks(file, b"@@ " + body)
    return list(files.values())


def unquote_git_path(path):
    if not path.startswith(b'"'):
        return path
    # Git C-quotes whitespace and non-ASCII bytes. Octal escapes represent
    # raw bytes, including sequences that are not UTF-8.
    escapes = {b"t": b"\t", b"n": b"\n", b"r": b"\r", b"a": b"\a", b"b": b"\b",
               b"f": b"\f", b"v": b"\v", b'"': b'"', b"\\": b"\\"}

    def unescape(match):
        raw = match[1]
        return escapes[raw] if raw in escapes else bytes([int(raw, 8)])

    return re.sub(rb"\\([0-7]{3}|.)", unescape, path[1:-1])


def parse_hunks(file, patch):
    file["hunks"] = []
    count = 0
    previous_end = 0
    hunk = None
    for row in patch.split(b"\n"):
        header = HUNK_HEADER.match(row)
        if header:
            old_start, old_count, new_start, new_count = (int(x or b"1") for x in header.groups())
            # A zero-count range names the line before the insertion/deletion.
            start = new_start - 1 if new_count else new_start
            hunk = {
                "oldStart": old_start, "oldCount": old_count, "newStart": new_start, "newCount": new_count,
                "gap": max(0, start - previous_end), "lines": [], "added": 0, "removed": 0,
            }
            previous_end = start + new_count
            file["hunks"].append(hunk)
        elif hunk is not None and row[:1] in (b" ", b"+", b"-", b"\\"):
            count += 1
            if row.startswith(b"+"):
                hunk["added"] += 1
            elif row.startswith(b"-"):
                hunk["removed"] += 1
            if count > MAX_DIFF_LINES or len(row) > MAX_LINE_LENGTH:
                file["truncated"] = True
                if count > MAX_DIFF_LINES:
                    continue
            kind = {b" ": "context", b"+": "add", b"-": "del", b"\\": "note"}[row[:1]]
            hunk["lines"].append({"kind": kind, "text": decode(row[1:MAX_LINE_LENGTH])})
    if file.get("truncated"):
        file["hunks"] = [hunk for hunk in file["hunks"] if hunk["lines"]]
        note = f"Diff preview limited to {MAX_DIFF_LINES:,} lines and {MAX_LINE_LENGTH:,} bytes per line"
        file["note"] = "; ".join(filter(None, [file.get("note"), note]))


def annotate_editable(root, files):
    for file in files:
        path = root / file["path"]
        if (file["newMode"] not in ("100644", "100755") or file["status"] == "D"
                or file.get("truncated") or file["binary"] or not file["hunks"]):
            continue
        try:
            if path.is_symlink() or path.resolve() != path or not path.is_file() or path.stat().st_size > 8_000_000:
                continue
            data = path.read_bytes()
            lines = data.decode("utf-8").split("\n")
        except (OSError, UnicodeError, ValueError):
            continue
        if all(hunk_matches(lines, hunk) for hunk in file["hunks"]):
            file["contentHash"] = hashlib.sha256(data).hexdigest()


def hunk_matches(lines, hunk):
    count = hunk["newCount"]
    start = hunk["newStart"] - 1 if count else hunk["newStart"]
    expected = [line["text"] for line in hunk["lines"] if line["kind"] in ("add", "context")]
    return [line.removesuffix("\r") for line in lines[start:start + count]] == [
        line.removesuffix("\r") for line in expected
    ]


def refs(root):
    commits = []
    try:
        log = git(root, "log", "HEAD", "-50", "-z", "--format=%H%x00%h%x00%s").rstrip(b"\0")
    except ChangeTreeError:
        log = b""
    if log:
        parts = iter(decode(log).split("\0"))
        for sha, short, subject in zip(parts, parts, parts):
            commits.append({"value": sha, "label": short, "note": subject})

    branches, origins, pulls = [], [], []
    for row in decode(git(root, "for-each-ref", "--format=%(refname)%00%(HEAD)%00%(subject)",
                          "refs/heads", "refs/remotes")).splitlines():
        ref, current, subject = row.split("\0", 2)
        if ref.startswith("refs/remotes/"):
            name = ref.removeprefix("refs/remotes/")
            if not name.endswith("/HEAD"):
                origins.append({"value": name, "label": name, "note": subject})
        else:
            name = ref.removeprefix("refs/heads/")
            note = "Current branch"
            if current != "*":
                try:
                    ahead, behind = decode(git(root, "rev-list", "--left-right", "--count", f"{ref}...HEAD")).split()
                    note = f"{ahead} ahead, {behind} behind current checkout"
                except ChangeTreeError:
                    note = subject
            branches.append({"value": name, "label": name, "note": note})
    branches.insert(0, {"value": "HEAD", "label": "HEAD", "note": "Current checkout"})
    checkout_list = worktrees(root)
    pr_note = "Open PRs require gh and a signed-in GitHub account."
    if shutil.which("gh"):
        try:
            result = command(["gh", "pr", "list", "--json",
                              "number,title,baseRefName,headRefName,isCrossRepository"], root,
                             env={**os.environ, "GH_PROMPT_DISABLED": "1"}, timeout=8)
            if result.returncode == 0:
                pr_note = "No open pull requests"
                known = {item["label"] for item in branches + origins}
                remotes = decode(git(root, "remote")).splitlines()
                remote = "origin" if "origin" in remotes else next(iter(remotes), "origin")

                def best_ref(name):
                    for ref in (f"{remote}/{name}", name):
                        if ref in known:
                            return ref
                    return name

                for pr in json.loads(result.stdout):
                    number, base, head = pr["number"], pr["baseRefName"], pr["headRefName"]
                    head_ref = best_ref(head)
                    needs_fetch = head_ref not in known or pr.get("isCrossRepository", False)
                    pulls.append({
                        "value": number, "label": f"PR #{number} · {head} → {base}", "note": pr["title"],
                        "base": best_ref(base), "head": f"refs/change-tree/pr/{number}" if needs_fetch else head_ref,
                        "headLabel": head if needs_fetch else head_ref, "remote": remote, "fetchHead": needs_fetch,
                    })
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            pass
    return {"commits": commits, "branches": branches, "origins": origins,
            "worktrees": checkout_list, "prs": pulls, "prNote": pr_note}
