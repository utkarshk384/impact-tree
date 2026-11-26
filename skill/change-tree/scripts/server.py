"""Threaded localhost app, comparison state, and authenticated write endpoints."""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from tempfile import NamedTemporaryFile
from threading import Condition, Lock, Thread
from urllib.parse import urlsplit

from edits import CHANGED, EditError, save_file
from repo import ChangeTreeError, git, refs


WEB = Path(__file__).resolve().parents[1] / "web"
ASSETS = {"/": ("index.html", "text/html"), "/styles.css": ("styles.css", "text/css"),
          "/app.js": ("app.js", "text/javascript"), "/tree.js": ("tree.js", "text/javascript")}
MAX_REQUEST = 6_000_000
MAX_EXPAND_LINES = 20_000


def json_bytes(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def write_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json_bytes(data))
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def cache_json(path, data):
    try:
        write_json(path, data)
    except OSError as error:
        print(f"Change Tree cache: {public_error(error)}", file=sys.stderr)


def read_json(path, default):
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError, UnicodeError):
        return default


def repo_cache(root):
    key = hashlib.sha256(os.fsencode(str(root))).hexdigest()[:24]
    return Path.home() / ".cache" / "change-tree" / key


def server_info_path(root):
    return repo_cache(root) / "server.json"


def server_info(root):
    """Return the running server's URL and token, or None when no server answers."""
    info = read_json(server_info_path(root), None)
    if not isinstance(info, dict) or not isinstance(info.get("url"), str) or not isinstance(info.get("token"), str):
        return None
    port = urlsplit(info["url"]).port
    if not port or info["url"] != f"http://127.0.0.1:{port}":
        return None
    import http.client
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", "/api/ping", headers={"Host": f"127.0.0.1:{port}"})
        with connection.getresponse() as response:
            response.read()
            return info if response.status == 200 else None
    except (OSError, ValueError):
        return None
    finally:
        connection.close()


def public_error(error):
    return " ".join(str(error).splitlines()) or "Change Tree could not analyze this comparison."


class State:
    def __init__(self, root, analyze, job_factory=None, cache=None):
        self.root = root
        self.analyze = analyze
        self.cache = cache or repo_cache(root)
        self.changed = Condition()
        self.operations = Lock()
        self.sequence = 0
        self.result = {"repo": root.name, "comparison": {}, "files": [], "layout": {}, "error": None,
                       "refs": {}, "snapshot": None, "loading": True, "summaries": {},
                       "summaryStatus": "idle"}
        self.jobs = job_factory(self) if job_factory else None

    def initialize(self, base, head, task):
        self.compare({"base": base, "head": head, "task": task})
        try:
            references = refs(self.root)
        except (OSError, ChangeTreeError):
            references = {"prNote": "Could not load repository references."}
        with self.changed:
            self.result = {**self.result, "refs": references, "refsReady": True}
            self.notify()

    def notify(self):
        self.sequence += 1
        self.changed.notify_all()

    def get(self):
        with self.changed:
            return self.result

    def events(self, sequence):
        with self.changed:
            changed = self.changed.wait_for(lambda: self.sequence != sequence, timeout=15)
            selected = ("snapshot", "summaries", "summaryStatus", "summaryError", "refsReady")
            return self.sequence, {key: self.result.get(key) for key in selected} if changed else None

    def comparison_path(self, snapshot):
        return self.cache / "comparisons" / (snapshot + ".json")

    def publish(self, result, previous=None, edited_path=None):
        snapshot = hashlib.sha256(json_bytes(result)).hexdigest()
        path = self.comparison_path(snapshot)
        cached = read_json(path, {}) if not result["error"] else {}
        summaries = cached.get("summaries", {})
        if previous and previous["snapshot"] != snapshot:
            retained = {file["path"] for file in result["files"] if file["path"] != edited_path}
            saved = previous["summaries"].get("files", {})
            summaries = {**summaries, "files": {**{path: value for path, value in saved.items() if path in retained},
                                               **summaries.get("files", {})}}
        if not result["error"]:
            cache_json(path, {"analysis": result, "summaries": summaries})
        with self.changed:
            self.result = {**result, "refs": self.result["refs"], "refsReady": self.result.get("refsReady", False),
                           "snapshot": snapshot, "loading": False, "summaries": summaries,
                           "summaryStatus": "pending" if result["files"] else "idle"}
            self.notify()
        if self.jobs:
            self.jobs.enqueue(snapshot, result, summaries)
        return self.get()

    def summarize(self, snapshot, result, status, error=None):
        path = self.comparison_path(snapshot)
        with self.changed:
            if result:
                cached = read_json(path, {})
                cache_json(path, {**cached, "summaries": result})
            if self.result["snapshot"] == snapshot:
                self.result = {**self.result, "summaries": result, "summaryStatus": status, "summaryError": error}
                self.notify()

    def analyze_choice(self, base, head, task, pr=None):
        try:
            return self.analyze(self.root, base, head, task, pr)
        except (OSError, ChangeTreeError) as error:
            return {
                "repo": self.root.name, "files": [], "layout": {},
                "comparison": {"base": base, "head": head, "task": task, "pr": pr, "editable": False,
                               "baseLabel": base, "headLabel": "Working tree" if head == "WORKTREE" else head},
                "error": public_error(error), "errorCode": getattr(error, "code", 1),
            }

    def compare(self, body):
        base, head, task, pr = (body.get("base"), body.get("head"), body.get("task", ""), body.get("pr"))
        if (not all(isinstance(value, str) and value and len(value) <= 1024 and "\x00" not in value
                    for value in (base, head)) or not isinstance(task, str) or len(task) > 10000):
            raise EditError("Choose valid Base and Compare references and a short task summary.")
        with self.operations:
            pulls = self.get()["refs"].get("prs", [])
            if pr is not None:
                pull = next((item for item in pulls if item["value"] == pr and type(pr) is int), None)
                if not pull or (base, head) != (pull["base"], pull["head"]):
                    raise EditError("Choose an open pull request or unlink it.")
            # A fork's head remains a valid choice when the user unlinks the
            # PR or changes its base while keeping that head.
            fetched = next((item for item in pulls if item["head"] == head and item["fetchHead"]), None)
            if fetched:
                number, remote = fetched["value"], fetched["remote"]
                try:
                    git(self.root, "fetch", "--no-tags", "--no-write-fetch-head", "--", remote,
                        f"+refs/pull/{number}/head:{head}", timeout=30,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
                except (ChangeTreeError, subprocess.TimeoutExpired) as error:
                    raise EditError(f"Could not fetch pull request #{number}. {public_error(error)}") from error
            result = self.analyze_choice(base, head, task.strip(), pr)
            if fetched:
                comparison = result["comparison"]
                label = fetched["headLabel"]
                comparison["headLabel"] = label
                if comparison.get("diffBase"):
                    comparison["description"] = f"Three-dot diff · merge base {comparison['diffBase'][:7]} → {label}"
            return self.publish(result)

    def lines(self, body):
        """Return unchanged lines of a changed file's new version, so the page can expand diff gaps."""
        current = self.get()
        comp = current["comparison"]
        if body.get("snapshot") != current["snapshot"]:
            raise EditError(CHANGED, 409)
        file = next((file for file in current["files"] if file["path"] == body.get("path")), None)
        if file is None or file["status"] == "D" or file.get("binary"):
            raise EditError("This file has no unchanged lines to show.", 404)
        start, end = body.get("start"), body.get("end")
        if not all(type(value) is int for value in (start, end)) or start < 1 or end < start:
            raise EditError("Choose a valid line range.")
        end = min(end, start + MAX_EXPAND_LINES - 1)
        try:
            if comp.get("mode") == "worktree":
                target = Path(comp["target"]).resolve()
                path = (target / file["path"]).resolve()
                if target not in path.parents or not path.is_file():
                    raise EditError("The file is outside the compared worktree.", 404)
                data = path.read_bytes()
            else:
                data = git(self.root, "cat-file", "blob", f"{comp['headCommit']}:{file['path']}")
        except ChangeTreeError as error:
            raise EditError(f"Could not read the file. {public_error(error)}", 404) from error
        text = data.decode("utf-8", errors="replace").splitlines()
        return {"lines": [line[:16000] for line in text[start - 1:end]], "start": start, "total": len(text)}

    def save(self, body):
        with self.operations:
            previous = self.get()
            comp = previous["comparison"]
            if not comp.get("editable") or comp.get("head") != "WORKTREE":
                raise EditError("Choose the current working tree to edit files.", 403)
            if body.get("snapshot") != previous["snapshot"]:
                raise EditError(CHANGED, 409)
            file = next((file for file in previous["files"] if file["path"] == body.get("path")), None)
            if file is None:
                raise EditError("The file is outside the current diff.", 404)
            if not save_file(self.root, file, body.get("hash"), body.get("edits")):
                return previous
            result = self.analyze_choice(comp["base"], comp["head"], comp["task"], comp["pr"])
            return self.publish(result, previous, file["path"])


class Handler(BaseHTTPRequestHandler):
    server_version = "ChangeTree"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def response_headers(self, status, kind, size=None):
        self.send_response(status)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        if size is not None:
            self.send_header("Content-Length", str(size))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                         "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()

    def send_bytes(self, status, kind, payload):
        self.response_headers(status, kind, len(payload))
        self.wfile.write(payload)

    def json(self, status, data):
        self.send_bytes(status, "application/json", json_bytes(data))

    def check_request(self, write=False):
        if self.headers.get("Host") != self.server.host:
            self.close_connection = True
            self.json(403, {"error": "Invalid server address."})
            return False
        origin = self.headers.get("Origin")
        if (write and origin != self.server.url) or (origin and origin != self.server.url):
            self.close_connection = True
            self.json(403, {"error": "This request must come from the Change Tree page."})
            return False
        if write and not hmac.compare_digest(self.headers.get("X-Change-Tree-Token", "").encode(),
                                             self.server.token.encode()):
            self.close_connection = True
            self.json(403, {"error": "Invalid session token."})
            return False
        return True

    def body(self):
        if self.headers.get_content_type() != "application/json":
            self.close_connection = True
            raise EditError("Send a JSON request.", 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise EditError("Invalid request size.") from error
        if not 0 < length <= MAX_REQUEST or self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise EditError("Invalid request size.", 413)
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError, RecursionError) as error:
            raise EditError("Invalid JSON request.") from error
        if not isinstance(payload, dict):
            raise EditError("Send a JSON object.")
        return payload

    def do_GET(self):
        if not self.check_request():
            return
        path = urlsplit(self.path).path
        if path in ASSETS:
            filename, kind = ASSETS[path]
            payload = (WEB / filename).read_bytes()
            if path == "/":
                payload = payload.replace(b"__CHANGE_TREE_TOKEN__", self.server.token.encode())
            self.send_bytes(200, kind, payload)
        elif path == "/api/state":
            self.json(200, self.server.state.get())
        elif path == "/api/ping":
            self.json(200, {"ok": True})
        elif path == "/api/events":
            self.stream_events()
        else:
            self.json(404, {"error": "Page not found."})

    def stream_events(self):
        self.close_connection = True
        self.response_headers(200, "text/event-stream")
        sequence = -1
        try:
            while True:
                sequence, event = self.server.state.events(sequence)
                payload = b": keepalive\n\n" if event is None else b"data: " + json_bytes(event) + b"\n\n"
                self.wfile.write(payload)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def do_POST(self):
        if not self.check_request(write=True):
            return
        path = urlsplit(self.path).path
        state = self.server.state
        try:
            if path == "/api/compare":
                self.json(200, state.compare(self.body()))
            elif path == "/api/save":
                self.json(200, state.save(self.body()))
            elif path == "/api/lines":
                self.json(200, state.lines(self.body()))
            elif path == "/api/jobs/claim" and state.jobs:
                wait = self.body().get("wait", 0)
                if not isinstance(wait, (int, float)) or isinstance(wait, bool):
                    raise EditError("wait must be a number of seconds.")
                self.json(200, state.jobs.claim(wait))
            elif path == "/api/jobs/done" and state.jobs:
                self.json(200, state.jobs.done(self.body()))
            elif path == "/api/shutdown":
                self.body()
                self.json(200, {"stopping": True})
                Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.close_connection = True
                self.json(404, {"error": "Endpoint not found."})
        except EditError as error:
            self.json(error.status, {"error": str(error)})
        except OSError as error:
            self.json(500, {"error": public_error(error)})


class Server(ThreadingHTTPServer):
    def __init__(self, state, port):
        super().__init__(("127.0.0.1", port), Handler)
        self.host = f"127.0.0.1:{self.server_port}"
        self.url = f"http://{self.host}"
        self.token = secrets.token_urlsafe(32)
        self.state = state


def serve(root, analyze, args, job_factory=None):
    running = server_info(root)
    if running:
        print(f"CHANGE_TREE_RUNNING {running['url']}", flush=True)
        raise ChangeTreeError(f"Change Tree is already running for this repository at {running['url']}. "
                              "Use that page, or stop it with `ct.py stop`.", 5)
    state = State(root, analyze, job_factory=job_factory)
    with Server(state, args.port) as server:
        info_path = server_info_path(root)
        write_json(info_path, {"url": server.url, "token": server.token, "pid": os.getpid()})
        os.chmod(info_path, 0o600)
        Thread(target=state.initialize, args=(args.base, args.head, args.task), daemon=True).start()
        print(f"CHANGE_TREE_URL {server.url}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if state.jobs:
                state.jobs.close()
            if read_json(info_path, {}).get("token") == server.token:
                info_path.unlink(missing_ok=True)
            print("CHANGE_TREE_STOPPED", flush=True)
