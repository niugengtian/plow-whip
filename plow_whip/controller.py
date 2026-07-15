"""Reliable, model-free controller receipts and bounded Desktop wakeups.

The append-only ledger is machine-private.  Project state remains the task
truth; this module only records delivery and consumption of result pointers.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import agent_flow as af
from .io_utils import atomic_write_json, file_lock


RETRY_DELAYS_MINUTES = (0, 5, 15)
FAILOVER_AFTER_MINUTES = 20
MAX_CONTROLLER_FAILOVERS = 1


def _now() -> datetime:
    return datetime.now().astimezone()


def _ledger_path() -> str:
    return os.path.join(af.CONFIG_DIR, "logs", "controller-receipts.jsonl")


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _safe_ref(value: str | None) -> str | None:
    if not value:
        return None
    if str(value).startswith("sha256:"):
        return str(value)
    return "sha256:" + hashlib.sha256(str(value).encode()).hexdigest()


def _read_events() -> list[dict]:
    path = _ledger_path()
    if not os.path.exists(path):
        return []
    events = []
    try:
        with open(path, encoding="utf-8") as file:
            for line in file:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and value.get("event_id"):
                    events.append(value)
    except OSError:
        return []
    return events


def _append(record: dict) -> dict:
    path = _ledger_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    value = {"at": _iso(), **record}
    line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    with file_lock(path + ".lock"):
        with open(path, "a", encoding="utf-8") as file:
            file.write(line)
            file.flush()
            os.fsync(file.fileno())
    return value


def _append_receipt_once(record: dict) -> bool:
    """Atomically suppress duplicate producers for one deterministic event id."""
    path = _ledger_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with file_lock(path + ".lock"):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as file:
                for line in file:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if value.get("kind") == "receipt" and value.get("event_id") == record["event_id"]:
                        return False
        value = {"at": _iso(), **record}
        with open(path, "a", encoding="utf-8") as file:
            file.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
            file.flush()
            os.fsync(file.fileno())
    return True


def states(project: str | None = None) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for event in _read_events():
        if project and event.get("project") != project:
            continue
        event_id = event["event_id"]
        current = merged.setdefault(event_id, {})
        if event.get("reset_attempts"):
            current["delivery_attempts"] = 0
            current["last_attempt_at"] = None
        current.update({
            key: value for key, value in event.items()
            if key != "reset_attempts" and not (key == "kind" and current)
        })
    return merged


def _execution_identity(project: str, task_id: str, dispatch_id: str) -> dict:
    try:
        state = af.load_state(project)
    except (OSError, ValueError):
        return {}
    task = state.get("task") or {}
    candidates = []
    if task.get("id") == task_id:
        candidates.append(task)
    for container in (state.get("workflow") or {}, state.get("goal") or {}):
        candidates.extend(item for item in container.get("completed", []) if item.get("id") == task_id)
    for item in candidates:
        execution = item.get("execution") or {}
        lease = execution.get("lease") or {}
        if not dispatch_id or not execution.get("dispatch_id") or execution.get("dispatch_id") == dispatch_id:
            return {
                "dispatch_id": dispatch_id or execution.get("dispatch_id"),
                "lease_generation": lease.get("generation"),
                "lease_id": lease.get("id"),
                "session_ref": _safe_ref(execution.get("session_id")),
            }
    return {"dispatch_id": dispatch_id}


def _event_id(project: str, task_id: str, dispatch_id: str, generation, event_type: str) -> str:
    raw = json.dumps(
        [project, task_id, dispatch_id, generation, event_type],
        ensure_ascii=False, separators=(",", ":"),
    )
    return "CR-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def record_receipt(
    project: str,
    task_id: str,
    dispatch_id: str,
    *,
    event_type: str,
    result_ref: str,
    execution_agent: str | None = None,
    evidence: dict | None = None,
) -> dict:
    """Create one idempotent receipt without copying result text."""
    identity = _execution_identity(project, task_id, dispatch_id)
    event_id = _event_id(
        project, task_id, dispatch_id,
        identity.get("lease_generation"), event_type,
    )
    from . import codex_desktop

    binding = codex_desktop.bound_control(project)
    _append_receipt_once({
        "kind": "receipt",
        "event_id": event_id,
        "status": "queued",
        "project": project,
        "task_id": task_id,
        "dispatch_id": dispatch_id,
        "lease_generation": identity.get("lease_generation"),
        "lease_id": identity.get("lease_id"),
        "execution_agent": execution_agent,
        "execution_session_ref": identity.get("session_ref"),
        "event_type": event_type,
        "result_ref": result_ref,
        "controller_ref": binding.get("thread_ref"),
        "created_at": _iso(),
        "delivery_attempts": 0,
        "failovers": 0,
        "evidence": evidence or {},
    })
    return states()[event_id]


def record_dispatch_result(
    project: str,
    task_id: str,
    dispatch_id: str,
    *,
    success: bool,
    result_ref: str,
    execution_agent: str | None = None,
    evidence: dict | None = None,
) -> dict:
    return record_receipt(
        project, task_id, dispatch_id,
        event_type="completion" if success else "execution_failure",
        result_ref=result_ref,
        execution_agent=execution_agent,
        evidence=evidence,
    )


def record_health_incident(worker: dict, evidence: dict) -> dict:
    return record_receipt(
        worker["project"], worker["task_id"], worker.get("dispatch_id", ""),
        event_type="suspected_hang",
        result_ref=worker.get("log_file", ""),
        execution_agent=worker.get("agent"),
        evidence=evidence,
    )


def _matches_current(receipt: dict) -> bool:
    """Reject a receipt if the same task id now belongs to a newer execution."""
    try:
        state = af.load_state(receipt["project"])
    except (OSError, ValueError):
        return False
    task = state.get("task") or {}
    if task.get("id") == receipt.get("task_id"):
        execution = task.get("execution") or {}
        current_dispatch = execution.get("dispatch_id")
        current_generation = (execution.get("lease") or {}).get("generation")
        if current_dispatch and receipt.get("dispatch_id") and current_dispatch != receipt["dispatch_id"]:
            return False
        if (current_generation is not None and receipt.get("lease_generation") is not None
                and current_generation != receipt["lease_generation"]):
            return False
        return True
    for container in (state.get("workflow") or {}, state.get("goal") or {}):
        for completed in container.get("completed", []):
            if completed.get("id") != receipt.get("task_id"):
                continue
            execution = completed.get("execution") or {}
            return not execution.get("dispatch_id") or execution.get("dispatch_id") == receipt.get("dispatch_id")
    return False


def pending(project: str | None = None) -> list[dict]:
    values = []
    for receipt in states(project).values():
        if receipt.get("kind") != "receipt" or receipt.get("status") in ("consumed", "stale", "failed"):
            continue
        if not _matches_current(receipt):
            _append({
                "kind": "transition", "event_id": receipt["event_id"],
                "project": receipt["project"], "status": "stale", "stale_at": _iso(),
            })
            continue
        values.append(receipt)
    return sorted(values, key=lambda item: item.get("created_at", ""))


def consume(project: str, event_id: str) -> dict:
    from . import codex_desktop

    receipt = states(project).get(event_id)
    if not receipt:
        raise KeyError(f"unknown controller receipt: {event_id}")
    if receipt.get("status") == "consumed":
        return receipt
    if not codex_desktop.authorize_control(project):
        raise PermissionError("only the current bound controller can consume a receipt")
    if not _matches_current(receipt):
        _append({
            "kind": "transition", "event_id": event_id, "project": project,
            "status": "stale", "stale_at": _iso(),
        })
        return states(project)[event_id]
    binding = codex_desktop.bound_control(project)
    _append({
        "kind": "transition", "event_id": event_id, "project": project,
        "status": "consumed", "consumed_at": _iso(),
        "controller_ref": binding.get("thread_ref"),
    })
    return states(project)[event_id]


def start_pack(project: str) -> dict:
    receipts = pending(project)[:3]
    return {
        "mode": "short-transaction",
        "contract": "Dispatch atomically, persist awaiting_receipt, then end the turn. Never wait or poll child sessions in this controller turn.",
        "pending": [
            {
                "event_id": item["event_id"],
                "type": item.get("event_type"),
                "task_id": item.get("task_id"),
                "result_ref": item.get("result_ref"),
                "consume": f"plow-whip --project {project} controller consume --event-id {item['event_id']}",
            }
            for item in receipts
        ],
    }


class _StdioAppServer:
    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.process = None
        self.request_id = 0

    def __enter__(self):
        codex = shutil.which("codex")
        if not codex:
            raise RuntimeError("codex executable is unavailable")
        self.process = subprocess.Popen(
            [codex, "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self.request("initialize", {
            "clientInfo": {"name": "plow-whip", "title": "plow-whip controller", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        self.notify("initialized", {})
        return self

    def __exit__(self, *_):
        if not self.process:
            return
        try:
            self.process.stdin.close()
        except (AttributeError, OSError):
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()

    def _write(self, value: dict) -> None:
        assert self.process and self.process.stdin
        self.process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict) -> dict:
        assert self.process and self.process.stdout
        self.request_id += 1
        request_id = self.request_id
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = _now() + timedelta(seconds=self.timeout)
        while _now() < deadline:
            remaining = max(0.1, (deadline - _now()).total_seconds())
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                break
            raw = self.process.stdout.readline()
            if not raw:
                break
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if value.get("id") != request_id:
                continue
            if value.get("error"):
                raise RuntimeError(json.dumps(value["error"], ensure_ascii=False))
            return value.get("result") or {}
        raise TimeoutError(f"app-server request timed out: {method}")


def _app_server_paths() -> tuple[str, str, str]:
    runtime = os.path.join(af.CONFIG_DIR, "runtime", "controller-app-server")
    return (
        os.path.join(runtime, "control.sock"),
        os.path.join(runtime, "process.json"),
        os.path.join(af.CONFIG_DIR, "logs", "controller-app-server.log"),
    )


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _ensure_app_server(codex: str | None = None, timeout: int = 10) -> str:
    """Keep a framework-owned stdio broker alive beyond one scheduler process."""
    socket_path, state_path, log_path = _app_server_paths()
    os.makedirs(os.path.dirname(socket_path), exist_ok=True)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    state = {}
    try:
        with open(state_path, encoding="utf-8") as file:
            state = json.load(file)
    except (OSError, json.JSONDecodeError):
        pass
    if os.path.exists(socket_path) and _pid_alive(state.get("pid")):
        return socket_path
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    with open(log_path, "a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "plow_whip.controller", "--broker", socket_path],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    atomic_write_json(state_path, {
        "pid": process.pid, "socket": socket_path, "started_at": _iso(),
    })
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(socket_path):
            return socket_path
        if process.poll() is not None:
            break
        time.sleep(0.05)
    raise RuntimeError(f"controller app-server failed to start; inspect {log_path}")


def _broker_request(action: str, **params) -> dict:
    socket_path = _ensure_app_server()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(15)
        client.connect(socket_path)
        client.sendall((json.dumps({"action": action, **params}, separators=(",", ":")) + "\n").encode())
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
    if not chunks:
        raise RuntimeError("controller app-server broker returned no response")
    result = json.loads(b"".join(chunks).split(b"\n", 1)[0])
    if result.get("error"):
        raise RuntimeError(result["error"])
    return result


def _broker_main(socket_path: str) -> int:
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    os.makedirs(os.path.dirname(socket_path), exist_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    os.chmod(socket_path, 0o600)
    server.listen(8)
    try:
        with _StdioAppServer(timeout=30) as app:
            while True:
                connection, _ = server.accept()
                with connection:
                    try:
                        raw = b""
                        while b"\n" not in raw:
                            chunk = connection.recv(65536)
                            if not chunk:
                                break
                            raw += chunk
                        request = json.loads(raw.split(b"\n", 1)[0])
                        action = request.get("action")
                        if action == "status":
                            response = {"status": _thread_status(request["thread_id"], app)}
                        elif action == "wake":
                            status = _thread_status(request["thread_id"], app)
                            if status not in ("idle", "completed", "notLoaded", "unknown"):
                                response = {"accepted": False, "status": "busy", "thread_status": status}
                            else:
                                started = app.request("turn/start", {
                                    "threadId": request["thread_id"],
                                    "input": [{"type": "text", "text": request["prompt"], "text_elements": []}],
                                })
                                response = {
                                    "accepted": True, "status": "delivered",
                                    "turn_id": (started.get("turn") or {}).get("id"),
                                }
                        elif action == "new":
                            started = app.request("thread/start", {
                                "cwd": request["cwd"], "ephemeral": False,
                                "developerInstructions": request.get("developer_instructions"),
                            })
                            response = {"thread_id": (started.get("thread") or {}).get("id")}
                        else:
                            raise ValueError(f"unknown broker action: {action}")
                    except Exception as exc:
                        response = {"error": f"{type(exc).__name__}: {exc}"}
                    connection.sendall((json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
    finally:
        server.close()
        if os.path.exists(socket_path):
            os.unlink(socket_path)
    return 0


def _thread_status(thread_id: str, client: _StdioAppServer) -> str:
    result = client.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
    status = ((result.get("thread") or {}).get("status") or {})
    return status.get("type", "unknown") if isinstance(status, dict) else str(status)


def _wake(thread_id: str, prompt: str) -> dict:
    return _broker_request("wake", thread_id=thread_id, prompt=prompt)


def _new_controller(project: str, role: str | None) -> dict:
    from . import codex_desktop

    result = _broker_request(
        "new", cwd=af.project_dir(project),
        developer_instructions=(
            f"You are the {role or 'controller'} control-session for plow-whip project {project}. "
            "Use short atomic dispatches and never babysit child sessions in a foreground turn."
        ),
    )
    thread_id = result.get("thread_id")
    if not thread_id:
        raise RuntimeError("app-server did not return a new controller thread id")
    return codex_desktop.set_control_thread(project, thread_id, role=role, reason="bounded_failover")


def _wake_prompt(receipt: dict) -> str:
    return (
        f"plow-whip controller receipt {receipt['event_id']} for project {receipt['project']}.\n"
        f"Task: {receipt['task_id']}  Event: {receipt.get('event_type')}\n"
        f"Read the persisted result/evidence at: {receipt.get('result_ref')}\n"
        "Reconcile it with the current project task truth. Do not redispatch completed work and do not poll child sessions. "
        "Do not restate the execution result to the human; keep any visible acknowledgement to one line. "
        "After the result has been handled, record consumption with:\n"
        f"plow-whip --project {receipt['project']} controller consume --event-id {receipt['event_id']}"
    )


def _due(receipt: dict, now: datetime) -> bool:
    attempts = int(receipt.get("delivery_attempts", 0))
    if attempts >= len(RETRY_DELAYS_MINUTES):
        return False
    if attempts == 0:
        return True
    try:
        last = datetime.fromisoformat(receipt.get("last_attempt_at", ""))
    except (TypeError, ValueError):
        return True
    delay = RETRY_DELAYS_MINUTES[attempts]
    return now - last >= timedelta(minutes=delay)


def process_project(project: str, now: datetime | None = None) -> list[dict]:
    from . import codex_desktop

    now = now or _now()
    outcomes = []
    for receipt in pending(project):
        binding = codex_desktop.bound_control(project)
        thread_id = binding.get("thread_id")
        if not thread_id:
            outcomes.append({"event_id": receipt["event_id"], "status": "queued_no_controller"})
            continue
        attempts = int(receipt.get("delivery_attempts", 0))
        created = datetime.fromisoformat(receipt["created_at"])
        age = now - created
        if attempts >= len(RETRY_DELAYS_MINUTES):
            failovers = int(receipt.get("failovers", 0))
            if age < timedelta(minutes=FAILOVER_AFTER_MINUTES):
                outcomes.append({"event_id": receipt["event_id"], "status": "waiting_controller"})
                continue
            if failovers >= MAX_CONTROLLER_FAILOVERS:
                _append({
                    "kind": "transition", "event_id": receipt["event_id"], "project": project,
                    "status": "failed", "failed_at": _iso(now),
                    "failure": "controller recovery exhausted",
                })
                af.notify(f"{project}: controller recovery exhausted for {receipt['event_id']}", ring=True)
                outcomes.append({"event_id": receipt["event_id"], "status": "failed"})
                continue
            try:
                binding = _new_controller(project, binding.get("role"))
            except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
                outcomes.append({"event_id": receipt["event_id"], "status": "failover_failed", "detail": str(exc)[-300:]})
                continue
            _append({
                "kind": "transition", "event_id": receipt["event_id"], "project": project,
                "status": "queued", "controller_ref": binding.get("thread_ref"),
                "failovers": failovers + 1, "reset_attempts": True,
            })
            receipt = states(project)[receipt["event_id"]]
            thread_id = binding.get("thread_id")
            attempts = 0
        if not _due(receipt, now):
            outcomes.append({"event_id": receipt["event_id"], "status": "waiting_retry"})
            continue
        try:
            delivered = _wake(thread_id, _wake_prompt(receipt))
        except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
            delivered = {"accepted": False, "status": "wake_failed", "detail": str(exc)[-300:]}
        status = "delivered" if delivered.get("accepted") else "queued"
        _append({
            "kind": "transition", "event_id": receipt["event_id"], "project": project,
            "status": status, "last_attempt_at": _iso(now),
            "delivery_attempts": attempts + 1,
            "controller_ref": binding.get("thread_ref"),
            "delivery_status": delivered.get("status"),
            "turn_ref": delivered.get("turn_id"),
        })
        outcomes.append({"event_id": receipt["event_id"], **delivered})
    return outcomes


def process_projects(projects: list[str]) -> list[dict]:
    outcomes = []
    for project in dict.fromkeys(projects):
        outcomes.extend({"project": project, **item} for item in process_project(project))
    return outcomes


def reconcile_result_files() -> list[dict]:
    """Rebuild receipts after a crash between result persistence and enqueue."""
    root = Path(af.CONFIG_DIR) / "runtime" / "results"
    recorded = []
    if not root.is_dir():
        return recorded
    for path in root.glob("*.json"):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        project = result.get("project")
        task_id = result.get("task_id")
        dispatch_id = result.get("dispatch_id") or path.stem
        if not project or not task_id:
            continue
        recorded.append(record_dispatch_result(
            project, task_id, dispatch_id,
            success=bool(result.get("success")), result_ref=str(path),
            execution_agent=result.get("logical_owner") or result.get("agent"),
            evidence={
                "returncode": result.get("returncode"),
                "status": result.get("status"),
                "pid": result.get("pid"),
            },
        ))
    return recorded


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--broker":
        raise SystemExit(_broker_main(sys.argv[2]))
    raise SystemExit("usage: python -m plow_whip.controller --broker <socket>")
