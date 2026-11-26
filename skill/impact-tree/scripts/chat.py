"""Validate chat requests and results for the local Impact Tree agent."""
from pathlib import Path, PureWindowsPath

from edits import EditError


MAX_MESSAGE = 4000
MAX_SELECTION = 2000
MAX_PATH = 4096
MAX_REPLY = 16000
MAX_REFERENCES = 10
RECENT_MESSAGES = 20


def chat_text(value, limit, label):
    if not isinstance(value, str) or len(value) > limit or not value.strip():
        raise EditError(f"{label} must be text of 1–{limit} characters.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise EditError(f"{label} must be valid UTF-8.") from error
    return value.strip()


def page_context(value, files):
    if not isinstance(value, dict) or set(value) - {"path", "block", "selectedText"}:
        raise EditError("Send valid chat page context.")
    context = {}
    if "path" in value:
        path = value["path"]
        chat_text(path, MAX_PATH, "Context path")
        file = next((file for file in files if file["path"] == path), None)
        if file is None:
            raise EditError("The context file is outside the current diff.")
        context["path"] = file["path"]
        if "block" in value:
            block = value["block"]
            if type(block) is not int or not 0 <= block < len(file["hunks"]):
                raise EditError("Choose a valid context block.")
            context["block"] = block
    elif "block" in value:
        raise EditError("A context block needs an open file.")
    if "selectedText" in value:
        context["selectedText"] = chat_text(value["selectedText"], MAX_SELECTION, "Selected text")
    return context


def reference_path(root, value):
    chat_text(value, MAX_PATH, "Reference path")
    path = Path(value)
    windows = PureWindowsPath(value)
    if (path.is_absolute() or windows.drive or "\\" in value or "\x00" in value or not path.parts
            or any(part.casefold() in (".git", "..") for part in path.parts)):
        raise EditError("Reference paths must stay inside the repository.")
    try:
        resolved = (root / path).resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise EditError("Reference paths must stay inside the repository.") from error
    if not resolved.parts or any(part.casefold() == ".git" for part in resolved.parts):
        raise EditError("Reference paths must stay inside the repository.")
    return path.as_posix()


def validate_reply(root, result):
    if not isinstance(result, dict) or not {"reply"} <= set(result) <= {"reply", "references"}:
        raise EditError("Return a chat reply and optional references.")
    reply = chat_text(result["reply"], MAX_REPLY, "Chat reply")
    references = result.get("references", [])
    if not isinstance(references, list) or len(references) > MAX_REFERENCES:
        raise EditError(f"Return up to {MAX_REFERENCES} references.")
    cleaned = []
    root = root.resolve()
    for reference in references:
        if not isinstance(reference, dict) or set(reference) != {"path", "line"}:
            raise EditError("Each reference needs a repository path and line number.")
        line = reference["line"]
        if type(line) is not int or not 1 <= line <= 2_147_483_647:
            raise EditError("Reference line numbers must be positive integers.")
        cleaned.append({"path": reference_path(root, reference["path"]), "line": line})
    return {"reply": reply, "references": cleaned}
