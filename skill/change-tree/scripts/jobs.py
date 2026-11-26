"""Send summary jobs to one persistent Herdr worker and validate its results."""
from collections import deque
from dataclasses import dataclass
import http.client
import json
from pathlib import Path
import re
import secrets
import subprocess
from threading import Condition, Thread
from urllib.parse import urlsplit

from edits import EditError
from repo import ChangeTreeError, command, decode
from server import MAX_REQUEST, json_bytes, read_json, repo_cache, write_json


JOB_ID = re.compile(r"[a-f0-9]{32}")
FILE_BATCH = 15
BATCH_BYTES = 55_000


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
        summary = summaries.get(file["path"], {}).get("summary", "")
        context.append({"path": file["path"], "status": file["status"], "score": file["score"],
                        "summary": summary[:300]})
    return context


def validate_result(kind, files, has_task, result):
    def text(value, limit, label):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise EditError(f"{label} must be text of 1–{limit} characters.")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise EditError(f"{label} must be valid UTF-8.") from error
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
    cleaned = {}
    for file in files:
        path = file["path"]
        summary = provided[path]
        if not isinstance(summary, dict) or set(summary) != {"summary", "hunks"}:
            raise EditError(f"Return a summary and hunks list for {path}.")
        hunks = summary["hunks"]
        if not isinstance(hunks, list) or len(hunks) != len(file["hunks"]):
            raise EditError(f"Return {len(file['hunks'])} ordered hunk summaries for {path}.")
        sections = []
        for hunk in hunks:
            if not isinstance(hunk, dict) or set(hunk) != {"title", "summary"}:
                raise EditError(f"Each hunk in {path} requires a title and summary.")
            sections.append({"title": text(hunk["title"], 120, "Hunk title"),
                             "summary": text(hunk["summary"], 750, "Hunk summary")})
        cleaned[path] = {"summary": text(summary["summary"], 950, "File summary"), "hunks": sections}
    return {"files": cleaned}


class Jobs:
    def __init__(self, state):
        self.state = state
        self.directory = state.cache / "jobs"
        self.condition = Condition()
        self.plan = None
        self.active = None
        self.closed = False

    def connect(self, url, token):
        self.url = url
        self.token = token
        Thread(target=self.run, name="change-tree-jobs", daemon=True).start()

    def job_path(self, job_id):
        return self.directory / (job_id + ".json")

    def session_path(self, job_id):
        return self.directory / (job_id + ".session.json")

    def enqueue(self, snapshot, result, summaries):
        with self.condition:
            if self.active and self.active.plan.snapshot == snapshot:
                self.plan = self.active.plan
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
        }
        if active.kind == "files":
            payload["files"] = active.files
        else:
            payload["files"] = overview_context(plan)
        return payload

    def clean(self, job_id):
        for path in (self.job_path(job_id), self.session_path(job_id)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def fail(self, active, message):
        with self.condition:
            if self.active is not active:
                return
            self.clean(active.job_id)
            self.active = None
            active.plan.pending.clear()
            self.state.summarize(active.plan.snapshot, active.plan.summaries, "failed", message)
            self.condition.notify_all()

    def run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.active is None and self.plan and self.plan.pending)
                if self.closed:
                    return
                kind, files = self.plan.pending.popleft()
                active = Active(secrets.token_hex(16), self.plan, kind, files)
                self.active = active
                try:
                    write_json(self.job_path(active.job_id), self.dispatch_payload(active))
                    write_json(self.session_path(active.job_id), {"url": self.url, "token": self.token})
                except OSError:
                    self.fail(active, "Summaries could not be queued in the Change Tree cache.")
                    continue
            try:
                reported = command(["hc", "send", self.state.worker, f"change-tree job {self.job_path(active.job_id)}"],
                                   self.state.root, timeout=45)
                try:
                    delivery = json.loads(reported.stdout)
                except ValueError:
                    delivery = {}
                rejected = isinstance(delivery, dict) and delivery.get("started") is False
                if reported.returncode or rejected:
                    detail = decode(reported.stderr or reported.stdout).strip()
                    if detail:
                        print(f"Change Tree worker: {detail[:400]}", flush=True)
                    self.fail(active, "The summary worker could not receive this job.")
            except (OSError, subprocess.TimeoutExpired):
                self.fail(active, "The summary worker could not receive this job.")

    def done(self, body):
        job_id = body.get("jobId")
        with self.condition:
            active = self.active
            if not active or job_id != active.job_id:
                raise EditError("This job is no longer active.", 409)
            plan = active.plan
            result = validate_result(active.kind, active.files, bool(plan.comparison["task"]), body.get("result"))
            if active.kind == "files":
                files = {**plan.summaries.get("files", {}), **result["files"]}
                plan.summaries = {**plan.summaries, "files": files}
            else:
                plan.summaries = {**plan.summaries, **result}
            self.state.summarize(plan.snapshot, plan.summaries, "pending" if plan.pending else "complete")
            self.clean(active.job_id)
            self.active = None
            self.condition.notify_all()
        return {"accepted": True}

    def close(self):
        with self.condition:
            self.closed = True
            if self.active:
                self.clean(self.active.job_id)
            self.condition.notify_all()


def submit(root, job_id, result_path):
    if not JOB_ID.fullmatch(job_id):
        raise ChangeTreeError("Invalid Change Tree job ID.")
    session = read_json(repo_cache(root) / "jobs" / (job_id + ".session.json"), {})
    try:
        origin = session["url"]
        token = session["token"]
        url = urlsplit(origin)
        if not url.port or origin != f"http://127.0.0.1:{url.port}" or not isinstance(token, str):
            raise ValueError("invalid session credentials")
    except (KeyError, ValueError, TypeError) as error:
        raise ChangeTreeError("This job is unavailable. Use the repository and ID from the active job.") from error

    try:
        with Path(result_path).open("rb") as handle:
            raw = handle.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise ValueError("result too large")
        result = json.loads(raw)
    except (ValueError, OSError, UnicodeError, RecursionError) as error:
        raise ChangeTreeError(f"Could not read the job result. {error}") from error

    data = json_bytes({"jobId": job_id, "result": result})
    if len(data) > MAX_REQUEST:
        raise ChangeTreeError("The job result is too large.")
    connection = http.client.HTTPConnection("127.0.0.1", url.port, timeout=15)
    try:
        connection.request("POST", "/api/jobs/done", body=data, headers={
            "Origin": origin, "Content-Type": "application/json", "X-Change-Tree-Token": token,
            "Connection": "close",
        })
        with connection.getresponse() as response:
            if response.status == 200:
                response.read()
            else:
                try:
                    details = json.load(response)["error"]
                except (ValueError, KeyError, TypeError):
                    details = "Change Tree could not accept the job result."
                raise ChangeTreeError(details)
    except (OSError, http.client.HTTPException) as error:
        raise ChangeTreeError("Could not reach the Change Tree server for this job.") from error
    finally:
        connection.close()
