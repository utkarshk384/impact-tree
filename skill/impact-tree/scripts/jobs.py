"""Queue jobs for the agent that runs the skill, and validate the results.

The server does not start or message an agent. The agent that ran the skill claims
jobs with `ct.py jobs wait`, gives each job to a sub-agent, and the sub-agent
submits the result with `ct.py jobs done`."""
from collections import deque
from dataclasses import dataclass
import http.client
import json
from pathlib import Path
import re
import importlib.util
import secrets
import time
from threading import Condition, Timer
from urllib.parse import urlsplit

from chat import MAX_MESSAGE, RECENT_MESSAGES, chat_text, page_context, validate_reply
from edits import CHANGED, EditError
from repo import ChangeTreeError
from server import MAX_REQUEST, cache_json, json_bytes, read_json, repo_cache, server_info, write_json


JOB_ID = re.compile(r"[a-f0-9]{32}")
FILE_BATCH = 15
BATCH_BYTES = 55_000
MAX_ACTIVE = 3
CLAIM_TIMEOUT = 20 * 60
STE_LINT = Path(__file__).resolve().parents[1] / "vendor" / "asd-ste100" / "ste_lint.py"
STE_IGNORED = {"synonym-rotation"}
CHAT_PENDING = {"queued", "ready", "running"}
_ste = None


def ste_findings(value):
    """Return the hard ASD-STE100 findings for one summary text."""
    global _ste
    if _ste is None:
        spec = importlib.util.spec_from_file_location("change_tree_ste_lint", STE_LINT)
        _ste = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_ste)
    findings, _words = _ste.lint(value)
    return [finding for finding in findings
            if finding["level"] == "advisory-free" and finding["rule"] not in STE_IGNORED]


@dataclass
class Plan:
    snapshot: str
    comparison: dict
    pending: deque
    summaries: dict
    files: list


@dataclass
class Active:
    job_id: str
    plan: Plan
    kind: str
    files: list
    claimed: float


@dataclass
class ChatClaim:
    job_id: str
    snapshot: str
    messages: set
    claimed: float


def file_context(file):
    hunks = []
    allowance = min(22, max(2, 220 // max(1, len(file["hunks"]))))
    for index, hunk in enumerate(file["hunks"]):
        rows = hunk["lines"]
        if len(rows) > allowance:
            leading = (allowance + 1) // 2
            rows = rows[:leading] + rows[-(allowance - leading):]
        excerpt = [{"kind": row["kind"], "text": row["text"][:280]} for row in rows]
        hunks.append({
            "index": index, "newStart": hunk["newStart"], "newCount": hunk["newCount"],
            "oldStart": hunk["oldStart"], "oldCount": hunk["oldCount"],
            "gap": hunk["gap"], "added": hunk["added"], "removed": hunk["removed"],
            "lines": excerpt,
            "trimmed": len(excerpt) != len(hunk["lines"]) or any(len(row["text"]) > 280 for row in rows),
        })
    selected = ("path", "status", "note", "added", "removed", "score", "impact", "why", "oldPath")
    return {**{key: file[key] for key in selected if key in file},
            "hunks": hunks, "usedBy": file["usedBy"][:25], "usedByCount": len(file["usedBy"])}


def batches(files):
    batch = []
    size = 0
    for file in files:
        context = file_context(file)
        next_size = len(json_bytes(context))
        if batch and (len(batch) == FILE_BATCH or size + next_size > BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(context)
        size += next_size
    if batch:
        yield batch


def overview_context(plan):
    summaries = plan.summaries.get("files", {})
    context = []
    for file in plan.files:
        entry = summaries.get(file["path"], {})
        risks = [entry.get("risk")] + [hunk.get("risk") for hunk in entry.get("hunks", [])]
        context.append({"path": file["path"], "status": file["status"], "score": file["score"],
                        "summary": entry.get("summary", "")[:300], "why": entry.get("why", "")[:200],
                        "risks": [item[:200] for item in risks if item][:5]})
    return context


def validate_result(kind, files, has_task, result):
    def text(value, limit, label):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise EditError(f"{label} must be text of 1–{limit} characters.")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise EditError(f"{label} must be valid UTF-8.") from error
        findings = ste_findings(value)
        if findings:
            problems = "; ".join(f"{finding['rule']} at '{finding['match']}': {finding['message']}"
                                 for finding in findings[:5])
            raise EditError(f"{label} breaks the ASD-STE100 writing rules. Rewrite it. {problems}")
        return value.strip()

    if not isinstance(result, dict):
        raise EditError("Job result must be a JSON object.")
    if kind == "comparison":
        expected = {"headline", "summary", "taskFit"} if has_task else {"headline", "summary"}
        comparison = result.get("comparison")
        if set(result) != {"comparison"} or not isinstance(comparison, dict) or set(comparison) != expected:
            raise EditError("Return exactly a comparison headline and summary, plus taskFit when a task is present.")
        return {"comparison": {name: text(comparison[name], limit, name) for name, limit in
                               {"headline": 180, "summary": 950, "taskFit": 1800}.items() if name in expected}}

    provided = result.get("files")
    if (set(result) != {"files"} or not isinstance(provided, dict)
            or set(provided) != {file["path"] for file in files}):
        raise EditError("Return a files object keyed by every path in the job.")

    def risk(entry, label):
        # A risk is optional. Leave it out, or send an empty string, when nothing looks risky.
        value = entry.get("risk", "")
        if value in ("", None):
            return None
        return text(value, 500, label)

    cleaned = {}
    for file in files:
        path = file["path"]
        summary = provided[path]
        if not isinstance(summary, dict) or not {"summary", "why", "hunks"} <= set(summary) <= {"summary", "why", "risk", "hunks"}:
            raise EditError(f"Return summary, why, hunks and an optional risk for {path}.")
        hunks = summary["hunks"]
        if not isinstance(hunks, list) or len(hunks) != len(file["hunks"]):
            raise EditError(f"Return {len(file['hunks'])} ordered hunk summaries for {path}.")
        sections = []
        for hunk in hunks:
            if not isinstance(hunk, dict) or not {"title", "summary", "why"} <= set(hunk) <= {"title", "summary", "why", "risk"}:
                raise EditError(f"Each hunk in {path} requires a title, summary and why, and may have a risk.")
            section = {"title": text(hunk["title"], 120, "Hunk title"),
                       "summary": text(hunk["summary"], 750, "Hunk summary"),
                       "why": text(hunk["why"], 500, "Hunk why")}
            flagged = risk(hunk, "Hunk risk")
            if flagged:
                section["risk"] = flagged
            sections.append(section)
        entry = {"summary": text(summary["summary"], 950, "File summary"),
                 "why": text(summary["why"], 600, "File why"), "hunks": sections}
        flagged = risk(summary, "File risk")
        if flagged:
            entry["risk"] = flagged
        cleaned[path] = entry
    return {"files": cleaned}


class Jobs:
    def __init__(self, state):
        self.state = state
        self.directory = state.cache / "jobs"
        self.condition = Condition()
        self.plan = None
        self.active = {}
        self.chat_threads = {}
        self.chat_pending = {}
        self.chat_active = None
        self.chat_timer = None
        self.closed = False

    def job_path(self, job_id):
        return self.directory / (job_id + ".json")

    def chat_path(self, snapshot):
        return self.state.comparison_path(snapshot).with_suffix(".chat.json")

    def open_chat(self, snapshot):
        """Load the thread under the queue lock when a comparison opens."""
        if snapshot not in self.chat_threads:
            thread = read_json(self.chat_path(snapshot), [])
            if not isinstance(thread, list):
                thread = []
            # A worker from the previous server cannot submit to this queue.
            restored = [{**item, "status": "failed"} if item.get("status") == "running" else item
                        for item in thread]
            restored = self.schedule_chat(restored)
            if restored != thread:
                cache_json(self.chat_path(snapshot), restored)
            self.remember_chat(snapshot, restored)
        return self.chat_view(self.chat_threads[snapshot])

    @staticmethod
    def chat_view(thread):
        statuses = {item.get("status") for item in thread}
        status = "pending" if statuses & CHAT_PENDING else "failed" if "failed" in statuses else "idle"
        return {"chat": thread, "chatStatus": status}

    @staticmethod
    def schedule_chat(thread):
        # Hold later messages for a fresh job once the scheduled reply finishes.
        if any(item.get("status") in ("ready", "running") for item in thread):
            return thread
        return [{**item, "status": "ready"} if item.get("status") == "queued" else item for item in thread]

    def remember_chat(self, snapshot, thread):
        self.chat_threads[snapshot] = thread
        if any(item.get("status") == "ready" for item in thread):
            self.chat_pending.setdefault(snapshot, None)
        else:
            self.chat_pending.pop(snapshot, None)
        self.condition.notify_all()

    def store_chat(self, snapshot, thread, *, best_effort=False):
        thread = self.schedule_chat(thread)
        save = cache_json if best_effort else write_json
        save(self.chat_path(snapshot), thread)
        self.remember_chat(snapshot, thread)
        result = self.chat_view(thread)
        self.state.update_chat(snapshot, result)
        return result

    def change_chat(self, body, action):
        with self.condition:
            current = self.state.result
            snapshot = current["snapshot"]
            if self.closed or not snapshot or current["error"] or current.get("view"):
                raise EditError("Open a comparison to use chat.", 409)
            if body.get("snapshot", snapshot) != snapshot:
                raise EditError(CHANGED, 409)
            thread = self.chat_threads[snapshot]
            if action == "clear":
                result = self.store_chat(snapshot, [])
                active = self.chat_active
                if active and active.snapshot == snapshot:
                    self.release_chat()
                return result
            if action == "retry":
                for index, item in enumerate(thread):
                    if item["id"] == body.get("id") and item.get("status") == "failed":
                        thread = [*thread[:index], {**item, "status": "queued"}, *thread[index + 1:]]
                        break
                else:
                    raise EditError("This message is no longer available to retry.", 409)
            else:
                message = chat_text(body.get("message"), MAX_MESSAGE, "Chat message")
                context = page_context(body.get("context", {}), current["files"])
                thread = [*thread, {"id": secrets.token_hex(16), "role": "user", "text": message,
                                    "context": context, "status": "queued"}]
            if sum(item.get("status") == "queued" for item in thread) > RECENT_MESSAGES:
                raise EditError("Wait for a reply before sending more messages.", 429)
            return self.store_chat(snapshot, thread)

    def claim_chat(self):
        snapshot = next(iter(self.chat_pending))
        thread = self.chat_threads[snapshot]
        messages = [item for item in thread if item.get("status") == "ready"]
        # The current comparison may have changed while this thread waited.
        current = self.state.result
        if current["snapshot"] != snapshot:
            cached = read_json(self.state.comparison_path(snapshot), {})
            if not isinstance(cached, dict) or "analysis" not in cached:
                failed = [{**item, "status": "failed"} if item.get("status") in CHAT_PENDING else item
                          for item in thread]
                self.store_chat(snapshot, failed, best_effort=True)
                return None
            current = {**cached["analysis"], "summaries": cached.get("summaries", {})}
        comparison = {key: current["comparison"][key] for key in
                      ("base", "head", "baseCommit", "headCommit", "diffBase", "description", "task", "target")}
        plan = Plan(snapshot, comparison, deque(), current["summaries"], current["files"])
        job_id = secrets.token_hex(16)
        path = self.job_path(job_id)
        write_json(path, {
            "jobId": job_id, "comparisonId": snapshot, "kind": "chat", "repo": str(self.state.root),
            "comparison": comparison, "files": overview_context(plan),
            "thread": [item for item in thread if item.get("status") not in CHAT_PENDING][-RECENT_MESSAGES:],
            "messages": [{key: item[key] for key in ("id", "text", "context")} for item in messages],
            "context": messages[-1]["context"],
        })
        try:
            self.store_chat(snapshot, [{**item, "status": "running"} if item.get("status") == "ready" else item
                                       for item in thread])
        except OSError:
            self.clean(job_id)
            raise
        ids = {item["id"] for item in messages}
        active = self.chat_active = ChatClaim(job_id, snapshot, ids, time.monotonic())
        self.chat_timer = Timer(CLAIM_TIMEOUT, self.timeout_chat, args=(active,))
        self.chat_timer.daemon = True
        self.chat_timer.start()
        return self.job_response(job_id, "chat", 0, len(self.chat_pending))

    def release_chat(self):
        self.clean(self.chat_active.job_id)
        self.chat_active = None
        self.chat_timer.cancel()
        self.chat_timer = None
        self.condition.notify_all()

    def timeout_chat(self, active):
        with self.condition:
            if self.chat_active is not active:
                return
            thread = self.chat_threads[active.snapshot]
            failed = [{**item, "status": "failed"} if item["id"] in active.messages else item for item in thread]
            self.store_chat(active.snapshot, failed, best_effort=True)
            self.release_chat()

    def finish_chat(self, active, value):
        result = validate_reply(self.state.root, value)
        thread = []
        insert_at = 0
        for item in self.chat_threads[active.snapshot]:
            if item["id"] in active.messages:
                item = {key: value for key, value in item.items() if key != "status"}
                insert_at = len(thread) + 1
            thread.append(item)
        thread.insert(insert_at, {"id": active.job_id, "role": "assistant", "text": result["reply"],
                                 "references": result["references"]})
        self.store_chat(active.snapshot, thread)
        self.release_chat()
        return {"accepted": True}

    def enqueue(self, snapshot, result, summaries):
        with self.condition:
            running = next((item.plan for item in self.active.values() if item.plan.snapshot == snapshot), None)
            if running:
                self.plan = running
                self.state.summarize(snapshot, self.plan.summaries, "pending")
                return
            if self.plan and self.plan.snapshot == snapshot and self.plan.pending:
                return
            if not result["files"]:
                self.plan = None
                return
            comparison = result["comparison"]
            context = {key: comparison[key] for key in ("baseLabel", "headLabel", "description", "task")}
            files = result["files"]
            missing = [file for file in files if file["path"] not in summaries.get("files", {})]
            pending = deque(("files", batch) for batch in batches(missing))
            if "comparison" not in summaries:
                pending.append(("comparison", []))
            self.plan = Plan(snapshot, context, pending, summaries, files)
            if not pending:
                self.state.summarize(snapshot, summaries, "complete")
            self.condition.notify_all()

    def dispatch_payload(self, active):
        plan = active.plan
        payload = {
            "jobId": active.job_id, "comparisonId": plan.snapshot, "kind": active.kind,
            "task": plan.comparison["task"], "comparison": plan.comparison,
            "fileCount": len(plan.files),
            "style": "ASD-STE100: follow the writing rules in the Worker mode section of SKILL.md.",
        }
        payload["files"] = active.files if active.kind == "files" else overview_context(plan)
        return payload

    def clean(self, job_id):
        try:
            self.job_path(job_id).unlink(missing_ok=True)
        except OSError:
            pass

    def expire(self):
        """Put jobs back in the queue when nobody submitted them in time."""
        now = time.monotonic()
        if self.chat_active and now - self.chat_active.claimed >= CLAIM_TIMEOUT:
            self.timeout_chat(self.chat_active)
        for job_id, item in list(self.active.items()):
            if now - item.claimed < CLAIM_TIMEOUT:
                continue
            del self.active[job_id]
            self.clean(job_id)
            if item.plan is self.plan:
                item.plan.pending.appendleft((item.kind, item.files))

    def ready(self):
        """Return True when the next queued job can start now."""
        if not self.plan or not self.plan.pending or len(self.active) >= MAX_ACTIVE:
            return False
        kind = self.plan.pending[0][0]
        # The comparison overview needs every file summary, so it waits for the file jobs.
        return kind != "comparison" or not any(item.plan is self.plan for item in self.active.values())

    def claim(self, wait):
        deadline = time.monotonic() + max(0, min(wait, 3600))
        with self.condition:
            while True:
                self.expire()
                if self.closed:
                    return {"closed": True}
                if self.chat_pending and not self.chat_active:
                    chat = self.claim_chat()
                    if chat:
                        return chat
                    continue
                if self.ready():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"job": None}
                self.condition.wait(timeout=min(remaining, 30))
            kind, files = self.plan.pending.popleft()
            active = Active(secrets.token_hex(16), self.plan, kind, files, time.monotonic())
            try:
                write_json(self.job_path(active.job_id), self.dispatch_payload(active))
            except OSError:
                self.plan.pending.appendleft((kind, files))
                raise EditError("The job could not be written to the Impact Tree cache.", 500)
            self.active[active.job_id] = active
            return self.job_response(active.job_id, kind, len(files), len(self.plan.pending))

    def job_response(self, job_id, kind, files, remaining):
        return {"job": {"id": job_id, "kind": kind, "path": str(self.job_path(job_id)),
                        "files": files, "remaining": remaining,
                        "model": self.state.settings["model"], "effort": self.state.settings["effort"]}}

    def done(self, body):
        job_id = body.get("jobId")
        if not isinstance(job_id, str):
            raise EditError("This job is no longer active. Claim a new job.", 409)
        with self.condition:
            chat = self.chat_active
            if chat and job_id == chat.job_id:
                if time.monotonic() - chat.claimed >= CLAIM_TIMEOUT:
                    self.timeout_chat(chat)
                else:
                    return self.finish_chat(chat, body.get("result"))
            active = self.active.get(job_id)
            if not active:
                raise EditError("This job is no longer active. Claim a new job.", 409)
            plan = active.plan
            result = validate_result(active.kind, active.files, bool(plan.comparison["task"]), body.get("result"))
            if active.kind == "files":
                files = {**plan.summaries.get("files", {}), **result["files"]}
                plan.summaries = {**plan.summaries, "files": files}
            else:
                plan.summaries = {**plan.summaries, **result}
            del self.active[job_id]
            self.clean(job_id)
            busy = plan.pending or any(item.plan is plan for item in self.active.values())
            self.state.summarize(plan.snapshot, plan.summaries, "pending" if busy else "complete")
            self.condition.notify_all()
        return {"accepted": True}

    def close(self):
        with self.condition:
            self.closed = True
            if self.chat_active:
                self.timeout_chat(self.chat_active)
            for job_id in list(self.active):
                self.clean(job_id)
            self.active.clear()
            self.condition.notify_all()


def request(root, method, path, body, timeout):
    """Send an authenticated request to the running Impact Tree server for this repository."""
    info = server_info(root)
    if not info:
        raise ChangeTreeError("No Impact Tree server is running for this repository.", 3)
    origin = info["url"]
    port = urlsplit(origin).port
    data = json_bytes(body)
    if len(data) > MAX_REQUEST:
        raise ChangeTreeError("The request is too large.")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=data, headers={
            "Origin": origin, "Content-Type": "application/json", "X-Change-Tree-Token": info["token"],
            "Connection": "close",
        })
        with connection.getresponse() as response:
            raw = response.read()
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {}
            if response.status != 200:
                raise ChangeTreeError(payload.get("error", "The Impact Tree server refused the request."))
            return payload
    except (OSError, http.client.HTTPException) as error:
        raise ChangeTreeError("Could not reach the Impact Tree server for this repository.", 3) from error
    finally:
        connection.close()


def wait(root, seconds):
    """Claim the next job. Return (exit code, line to print)."""
    payload = request(root, "POST", "/api/jobs/claim", {"wait": seconds}, timeout=seconds + 30)
    if payload.get("closed"):
        return 3, "CHANGE_TREE_STOPPED"
    job = payload.get("job")
    if not job:
        return 4, "CHANGE_TREE_IDLE"
    model, effort = job.get("model", "default"), job.get("effort", "default")
    return 0, f"CHANGE_TREE_JOB {job['id']} {job['kind']} {model} {effort} {job['path']}"


def submit(root, job_id, result_path):
    if not JOB_ID.fullmatch(job_id):
        raise ChangeTreeError("Invalid Impact Tree job ID.")
    try:
        with Path(result_path).open("rb") as handle:
            raw = handle.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise ValueError("result too large")
        result = json.loads(raw)
    except (ValueError, OSError, UnicodeError, RecursionError) as error:
        raise ChangeTreeError(f"Could not read the job result. {error}") from error
    request(root, "POST", "/api/jobs/done", {"jobId": job_id, "result": result}, timeout=30)
