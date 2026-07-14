"""Persistent DeepSeek-powered executor for bounded project tasks."""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import subprocess
from datetime import datetime

from .brain import Brain
from .io_utils import atomic_write_json, file_lock


MAX_TOOL_OUTPUT = 12000
DEFAULT_CONTEXT_CHARS = 400000
SENSITIVE_NAMES = {".env", ".env.local", ".env.production", "credentials.json"}
FORBIDDEN_COMMANDS = {"rm", "sudo", "env", "printenv", "set", "export", "shutdown", "reboot"}
FORBIDDEN_GIT_ACTIONS = {"commit", "push", "merge", "rebase", "reset", "clean", "switch", "checkout"}
ALLOWED_EXECUTABLES = {
    "python", "python3", "pytest", "git", "node", "npm", "npx", "pnpm", "yarn",
    "go", "cargo", "make", "ruff", "mypy", "eslint", "tsc",
}
SENSITIVE_ENV = re.compile(r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)

TOOLS = [
    {"type": "function", "function": {"name": "read_file", "description": "Read a UTF-8 project file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "max_lines": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "list_files", "description": "List project files using a glob pattern.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "search_text", "description": "Search text inside project files.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "apply_patch", "description": "Apply a unified Git patch inside the project.", "parameters": {"type": "object", "properties": {"patch": {"type": "string"}}, "required": ["patch"]}}},
    {"type": "function", "function": {"name": "run_command", "description": "Run one local command without shell operators in the project sandbox.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "git_diff", "description": "Read the current project Git diff.", "parameters": {"type": "object", "properties": {}}}},
]


class SandboxViolation(ValueError):
    pass


class SimpleTasker:
    def __init__(
        self,
        project_path: str,
        task_id: str,
        client=None,
        context_limit_chars: int = DEFAULT_CONTEXT_CHARS,
        session_dir: str | None = None,
    ):
        self.project_path = os.path.realpath(project_path)
        self.task_id = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
        session_dir = session_dir or os.path.join(self.project_path, "collab", "memory", "sessions")
        os.makedirs(session_dir, exist_ok=True)
        self.session_path = os.path.join(session_dir, f"{self.task_id}_simple_tasker.jsonl")
        self.meta_path = os.path.join(session_dir, f"{self.task_id}_simple_tasker.meta.json")
        self.client = client or Brain()
        self.context_limit_chars = context_limit_chars

    def _events(self) -> list[dict]:
        if not os.path.exists(self.session_path):
            return []
        events = []
        with open(self.session_path, encoding="utf-8") as file:
            for line in file:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events

    def _append(self, event: dict) -> dict:
        with file_lock(self.session_path + ".lock"):
            events = self._events()
            record = {
                "seq": (events[-1].get("seq", len(events)) if events else 0) + 1,
                "at": datetime.now().isoformat(timespec="seconds"),
                **event,
            }
            with open(self.session_path, "a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        return record

    def _save_meta(self, status: str, **extra) -> dict:
        value = {
            "task_id": self.task_id, "session_id": f"simple-tasker:{self.task_id}",
            "status": status, "session_file": os.path.basename(self.session_path),
            "key_ref": getattr(self.client, "last_key_ref", None),
            "updated_at": datetime.now().isoformat(timespec="seconds"), **extra,
        }
        atomic_write_json(self.meta_path, value)
        return value

    def _safe_path(self, value: str) -> str:
        if not value or "\x00" in value:
            raise SandboxViolation("invalid empty path")
        candidate = os.path.realpath(os.path.join(self.project_path, value)) if not os.path.isabs(value) else os.path.realpath(value)
        if os.path.commonpath([self.project_path, candidate]) != self.project_path:
            raise SandboxViolation(f"path escapes project sandbox: {value}")
        if os.path.basename(candidate) in SENSITIVE_NAMES or candidate.endswith((".pem", ".key")):
            raise SandboxViolation(f"sensitive file access denied: {value}")
        return candidate

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 240) -> dict:
        target = self._safe_path(path)
        start = max(1, int(start_line))
        limit = min(max(1, int(max_lines)), 500)
        with open(target, encoding="utf-8", errors="replace") as file:
            lines = file.readlines()
        return {"path": os.path.relpath(target, self.project_path), "start_line": start, "content": "".join(lines[start - 1:start - 1 + limit])}

    def list_files(self, pattern: str = "**/*") -> dict:
        if os.path.isabs(pattern) or ".." in pattern.split(os.sep):
            raise SandboxViolation("glob must stay inside project")
        found = []
        for path in glob.iglob(os.path.join(self.project_path, pattern), recursive=True):
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, self.project_path)
            if rel.startswith(".git/") or rel.startswith("collab/memory/sessions/"):
                continue
            if os.path.basename(path) in SENSITIVE_NAMES or path.endswith((".pem", ".key")):
                continue
            found.append(rel)
            if len(found) >= 500:
                break
        return {"files": sorted(found), "truncated": len(found) >= 500}

    def search_text(self, query: str, path: str = ".") -> dict:
        root = self._safe_path(path)
        command = [
            "rg", "-n", "--no-heading", "--color", "never",
            "--glob", "!.env*", "--glob", "!*.pem", "--glob", "!*.key", "--glob", "!credentials.json",
            "--", query, root,
        ]
        if not shutil_which("rg"):
            raise RuntimeError("rg is required for search_text")
        result = subprocess.run(command, cwd=self.project_path, capture_output=True, text=True, check=False)
        output = result.stdout[-MAX_TOOL_OUTPUT:]
        return {"returncode": result.returncode, "output": output}

    def apply_patch(self, patch: str) -> dict:
        paths = []
        for match in re.finditer(r"^(?:---|\+\+\+)\s+(?:[ab]/)?([^\t\n]+)", patch, re.M):
            value = match.group(1)
            if value == "/dev/null":
                continue
            self._safe_path(value)
            paths.append(value)
        if not paths:
            raise SandboxViolation("patch does not name a project file")
        check = subprocess.run(
            ["git", "apply", "--check", "--whitespace=nowarn", "-"], cwd=self.project_path,
            input=patch, capture_output=True, text=True, check=False,
        )
        if check.returncode:
            return {"applied": False, "error": (check.stderr or check.stdout)[-MAX_TOOL_OUTPUT:]}
        applied = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"], cwd=self.project_path,
            input=patch, capture_output=True, text=True, check=False,
        )
        return {"applied": applied.returncode == 0, "paths": sorted(set(paths)), "error": applied.stderr[-MAX_TOOL_OUTPUT:]}

    def run_command(self, command: str, timeout: int = 120) -> dict:
        if any(char in command for char in ("|", ";", ">", "<", "`", "\n", "\r")) or "$(" in command or "${" in command:
            raise SandboxViolation("shell operators are not allowed")
        args = shlex.split(command)
        if not args or args[0] in FORBIDDEN_COMMANDS:
            raise SandboxViolation(f"command is not allowed: {args[0] if args else ''}")
        executable = os.path.basename(args[0])
        if executable not in ALLOWED_EXECUTABLES:
            if not os.path.isabs(args[0]) and os.sep not in args[0]:
                raise SandboxViolation(f"executable is not allowlisted: {args[0]}")
            self._safe_path(args[0])
        if executable in ("python", "python3"):
            if "-c" in args or "-" in args[1:]:
                raise SandboxViolation("inline Python is not allowed")
            if len(args) > 1 and args[1] == "-m":
                if len(args) < 3 or args[2].split(".")[0] not in {"unittest", "pytest", "compileall"}:
                    raise SandboxViolation("Python module is not allowlisted")
            elif len(args) > 1 and not args[1].startswith("-"):
                self._safe_path(args[1])
        if executable == "node" and any(item in args for item in ("-e", "--eval", "-p", "--print")):
            raise SandboxViolation("inline Node.js is not allowed")
        if args[0] == "git" and len(args) > 1 and args[1] in FORBIDDEN_GIT_ACTIONS:
            raise SandboxViolation(f"Git lifecycle command is owned by plow-whip: {args[1]}")
        for item in args[1:]:
            if item == ".." or item.startswith("../"):
                raise SandboxViolation("command argument escapes project")
            if os.path.isabs(item):
                self._safe_path(item)
        try:
            env = {key: value for key, value in os.environ.items() if not SENSITIVE_ENV.search(key)}
            result = subprocess.run(
                args, cwd=self.project_path, capture_output=True, text=True, check=False,
                timeout=min(max(1, int(timeout)), 900), env=env,
            )
            output = (result.stdout + result.stderr)[-MAX_TOOL_OUTPUT:]
            return {"returncode": result.returncode, "output": output}
        except subprocess.TimeoutExpired as exc:
            return {"returncode": 124, "output": f"command timed out: {exc}"}

    def git_diff(self) -> dict:
        result = subprocess.run(
            ["git", "diff", "--no-ext-diff"], cwd=self.project_path,
            capture_output=True, text=True, check=False,
        )
        return {"returncode": result.returncode, "output": result.stdout[-MAX_TOOL_OUTPUT:]}

    def _tool_result(self, name: str, arguments: dict) -> dict:
        handlers = {
            "read_file": self.read_file, "list_files": self.list_files,
            "search_text": self.search_text, "apply_patch": self.apply_patch,
            "run_command": self.run_command, "git_diff": self.git_diff,
        }
        if name not in handlers:
            return {"error": f"unknown tool: {name}"}
        try:
            return handlers[name](**arguments)
        except (OSError, ValueError, RuntimeError) as exc:
            return {"error": str(exc)}

    def _messages(self) -> list[dict]:
        events = self._events()
        summaries = [event for event in events if event.get("type") == "summary"]
        latest = summaries[-1] if summaries else None
        through = latest.get("through_seq", 0) if latest else 0
        messages = [{"role": "system", "content": self._system_prompt()}]
        if latest:
            messages.append({"role": "system", "content": f"Persisted session summary:\n{latest['content']}"})
        messages.extend(
            event["message"] for event in events
            if event.get("type") == "message" and event.get("seq", 0) > through
        )
        return messages

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are plow-whip simple-tasker. Work only through the provided project-sandbox tools. "
            "Never read secrets, manage Git branches/commits/pushes, or escape the project. Inspect before editing, "
            "run relevant tests, and keep the change bounded. If the task is ambiguous, architectural, or beyond a "
            "small implementation, return JSON {\"status\":\"needs_planner\",\"reason\":\"...\"}. When done, "
            "return JSON {\"status\":\"completed\",\"summary\":\"...\",\"verify_commands\":[\"...\"]}."
        )

    def _maybe_compact(self) -> None:
        messages = self._messages()
        encoded = json.dumps(messages, ensure_ascii=False)
        if len(encoded) <= self.context_limit_chars:
            return
        last_seq = max((event.get("seq", 0) for event in self._events()), default=0)
        prompt = [
            {"role": "system", "content": "Compress this execution history. Preserve task intent, files changed, commands, failures, decisions, and exact next action."},
            {"role": "user", "content": encoded[-self.context_limit_chars:]},
        ]
        summary_message = self.client.chat(prompt, max_tokens=1200)
        self._append({"type": "summary", "through_seq": last_seq, "content": summary_message.get("content", "")})

    @staticmethod
    def _final_payload(content: str) -> dict | None:
        value = (content or "").strip()
        candidates = [value]
        candidates.extend(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", value, re.S | re.I))
        for candidate in reversed(candidates):
            try:
                parsed = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
            if parsed.get("status") in ("completed", "needs_planner", "failed"):
                return parsed
        return None

    def run(self, task: str, context: str = "", max_turns: int = 30) -> dict:
        events = self._events()
        if not any(event.get("type") == "message" and event.get("message", {}).get("role") == "user" for event in events):
            content = task if not context else f"Context:\n{context}\n\nTask:\n{task}"
            self._append({"type": "message", "message": {"role": "user", "content": content}})
        self._save_meta("running")
        format_retries = 0
        for _ in range(max_turns):
            self._maybe_compact()
            message = self.client.chat(self._messages(), tools=TOOLS, max_tokens=4000)
            assistant = {key: value for key, value in message.items() if key in ("role", "content", "tool_calls", "reasoning_content")}
            assistant.setdefault("role", "assistant")
            self._append({"type": "message", "message": assistant})
            tool_calls = assistant.get("tool_calls") or []
            if tool_calls:
                for call in tool_calls:
                    function = call.get("function", {})
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except json.JSONDecodeError as exc:
                        result = {"error": f"invalid tool arguments: {exc}"}
                    else:
                        result = self._tool_result(function.get("name", ""), arguments)
                    self._append({"type": "tool", "name": function.get("name"), "result": result})
                    self._append({
                        "type": "message", "message": {
                            "role": "tool", "tool_call_id": call.get("id"),
                            "content": json.dumps(result, ensure_ascii=False)[:MAX_TOOL_OUTPUT],
                        },
                    })
                continue
            payload = self._final_payload(assistant.get("content") or "")
            if payload:
                self._save_meta(payload["status"], summary=payload.get("summary") or payload.get("reason", ""))
                return {
                    **payload, "session_id": f"simple-tasker:{self.task_id}",
                    "session_file": self.session_path, "key_ref": getattr(self.client, "last_key_ref", None),
                }
            format_retries += 1
            if format_retries >= 2:
                result = {"status": "failed", "reason": "model did not return structured completion status"}
                self._save_meta("failed", summary=result["reason"])
                return {**result, "session_id": f"simple-tasker:{self.task_id}", "session_file": self.session_path}
            self._append({
                "type": "message", "message": {
                    "role": "user", "content": "Return the required final JSON status now, or continue with tools if work remains."
                },
            })
        result = {"status": "failed", "reason": f"tool loop exceeded {max_turns} turns"}
        self._save_meta("failed", summary=result["reason"])
        return {**result, "session_id": f"simple-tasker:{self.task_id}", "session_file": self.session_path}

    def archive(self) -> dict:
        return self._save_meta("archived", archived_at=datetime.now().isoformat(timespec="seconds"))


def shutil_which(binary: str) -> str | None:
    # Local indirection makes the tool easy to test without patching global process state.
    import shutil
    return shutil.which(binary)
