"""Token-free scheduler reducer and bounded background worker registry."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timedelta

from . import agent_flow as af
from . import health
from . import git_flow
from .io_utils import atomic_write_json, file_lock


def _runtime_dir() -> str:
    return os.path.join(af.CONFIG_DIR, "runtime")


def _registry_path() -> str:
    return os.path.join(_runtime_dir(), "workers.json")


def _load_registry() -> dict:
    path = _registry_path()
    if not os.path.exists(path):
        return {"workers": []}
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return {"workers": []}


def _save_registry(value: dict) -> None:
    path = _registry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with file_lock(path + ".lock"):
        atomic_write_json(path, value)


def _pid_alive(pid: int | None) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def execution_is_live(execution: dict, excluding_dispatch: str | None = None) -> bool:
    """Treat a claimed execution as live before its child PID is available."""
    if execution.get("status") not in ("starting", "running"):
        return False
    if excluding_dispatch and execution.get("dispatch_id") == excluding_dispatch:
        return False
    if any(_pid_alive(execution.get(key)) for key in ("cli_pid", "worker_pid")):
        return True
    claimed = execution.get("claimed_at") or execution.get("started_at") or execution.get("cli_started_at")
    try:
        claimed_at = datetime.fromisoformat(claimed) if claimed else None
    except ValueError:
        claimed_at = None
    return bool(claimed_at and datetime.now() - claimed_at < timedelta(minutes=2))


def claim_task(project: str, task_id: str, dispatch_id: str, driver: str, agent: str) -> dict:
    """Atomically claim one Task before spawning any direct or scheduled executor."""
    for _ in range(5):
        state = af.load_state(project)
        task = state.get("task") or {}
        if task.get("id") != task_id or task.get("status") not in ("active", "in_progress"):
            return {"claimed": False, "detail": "task changed or is no longer active"}
        execution = task.setdefault("execution", {})
        if execution_is_live(execution, excluding_dispatch=dispatch_id):
            return {
                "claimed": False,
                "detail": f"task already executing as {execution.get('dispatch_id') or 'unknown dispatch'}",
                "execution": dict(execution),
            }
        now = datetime.now().isoformat(timespec="seconds")
        execution.update({
            "dispatch_id": dispatch_id, "driver": driver, "logical_owner": agent,
            "status": "starting", "claimed_at": now,
        })
        try:
            af.save_state(project, state)
            return {"claimed": True, "execution": dict(execution)}
        except RuntimeError:
            continue
    return {"claimed": False, "detail": "task claim lost to concurrent state updates"}


def reap_workers() -> dict:
    registry = _load_registry()
    live, finished = [], []
    for worker in registry.get("workers", []):
        if _pid_alive(worker.get("pid")):
            live.append(worker)
            continue
        result = {}
        if os.path.exists(worker.get("result_file", "")):
            try:
                with open(worker["result_file"], encoding="utf-8") as file:
                    result = json.load(file)
            except (OSError, json.JSONDecodeError):
                pass
        finished.append({**worker, "result": result})
    registry["workers"] = live
    _save_registry(registry)
    return {"live": live, "finished": finished}


def _driver_for(project: str, state: dict) -> str | None:
    task = state.get("task") or {}
    if task.get("requested_driver"):
        return task["requested_driver"]
    owner = task.get("owner")
    return af.load_protocol(project).get("agents", {}).get(owner, {}).get("driver")


def _ensure_task_branch(project: str, state: dict) -> tuple[dict, str | None]:
    workflow = state.get("workflow") or {}
    if not workflow.get("code_change") or (workflow.get("git") or {}).get("branch"):
        return state, None
    try:
        workflow["git"] = git_flow.prepare_branch(
            af.project_dir(project), workflow["id"], workflow.get("target_branch", "main")
        )
        state["workflow"] = workflow
        af.save_state(project, state)
        return af.load_state(project), None
    except git_flow.GitFlowBlocked as exc:
        detail = str(exc)
        workflow["git"] = {
            "status": "pending", "target_branch": workflow.get("target_branch", "main"), "error": detail,
        }
        if health.classify_failure(detail) == "implementation":
            state["task"].update({
                "status": "blocked_waiting_human", "next_action": "Resolve Git branch preparation blocker",
                "blockers": [detail],
            })
            workflow["status"] = "blocked_waiting_human"
        state["workflow"] = workflow
        af.save_state(project, state)
        return state, detail


def _stop_open_circuit_workers(live: list[dict], grace_seconds: int = 30) -> list[dict]:
    actions = []
    now = datetime.now()
    for worker in live:
        if not health.is_open(af.CONFIG_DIR, worker.get("driver")):
            continue
        requested = worker.get("stop_requested_at")
        try:
            requested_at = datetime.fromisoformat(requested) if requested else None
        except ValueError:
            requested_at = None
        sig = signal.SIGKILL if requested_at and now - requested_at >= timedelta(seconds=grace_seconds) else signal.SIGTERM
        try:
            state = af.load_state(worker["project"])
            task = state.get("task") or {}
            if task.get("id") == worker.get("task_id"):
                cli_pid = (task.get("execution") or {}).get("cli_pid")
                if cli_pid and _pid_alive(cli_pid):
                    os.killpg(int(cli_pid), sig)
            os.killpg(int(worker["pid"]), sig)
            worker["stop_requested_at"] = requested or now.isoformat(timespec="seconds")
            actions.append({"project": worker["project"], "task_id": worker["task_id"], "signal": sig.name})
        except (OSError, ValueError):
            pass
    if actions:
        registry = _load_registry()
        by_dispatch = {item["dispatch_id"]: item for item in live}
        registry["workers"] = [by_dispatch.get(item.get("dispatch_id"), item) for item in registry.get("workers", [])]
        _save_registry(registry)
    return actions


def _spawn(project: str, state: dict, driver: str) -> dict:
    task = state["task"]
    dispatch_id = f"DP-{uuid.uuid4().hex[:12]}"
    claim = claim_task(project, task["id"], dispatch_id, driver, task["owner"])
    if not claim["claimed"]:
        return {
            "project": project, "task_id": task["id"], "driver": driver,
            "dispatch_id": dispatch_id, "status": "skipped_running", "detail": claim.get("detail", "claim rejected"),
        }
    runtime = _runtime_dir()
    result_dir = os.path.join(runtime, "results")
    gate_dir = os.path.join(runtime, "gates")
    log_dir = os.path.join(af.CONFIG_DIR, "logs", "workers")
    for path in (result_dir, gate_dir, log_dir):
        os.makedirs(path, exist_ok=True)
    result_file = os.path.join(result_dir, f"{dispatch_id}.json")
    gate_file = os.path.join(gate_dir, f"{dispatch_id}.json")
    log_file = os.path.join(log_dir, f"{dispatch_id}.log")
    package_root = os.path.dirname(af.PACKAGE_DIR)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, env.get("PYTHONPATH", "")]))
    command = [
        sys.executable, "-m", "plow_whip.worker", "--project", project,
        "--agent", task["owner"], "--driver", driver, "--task-id", task["id"],
        "--dispatch-id", dispatch_id, "--result-file", result_file, "--start-gate", gate_file,
    ]
    with open(log_file, "a", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, cwd=af.project_dir(project), env=env, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    worker = {
        "project": project, "task_id": task["id"], "agent": task["owner"], "driver": driver,
        "dispatch_id": dispatch_id, "pid": process.pid, "result_file": result_file, "log_file": log_file,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    fresh = af.load_state(project)
    if (
        fresh.get("task", {}).get("id") != task["id"]
        or (fresh.get("task", {}).get("execution") or {}).get("dispatch_id") != dispatch_id
    ):
        os.killpg(process.pid, signal.SIGTERM)
        return {**worker, "status": "cancelled_claim_lost"}
    fresh["task"].setdefault("execution", {}).update({
        "dispatch_id": dispatch_id, "worker_pid": process.pid, "driver": driver,
        "status": "running", "started_at": worker["started_at"],
    })
    af.save_state(project, fresh)
    registry = _load_registry()
    registry.setdefault("workers", []).append(worker)
    _save_registry(registry)
    atomic_write_json(gate_file, {"ready": True})
    return {**worker, "status": "started"}


def dispatch_projects(projects: list[str]) -> dict:
    """Reap, probe open circuits, enforce limits, and spawn one worker per eligible Task."""
    reaped = reap_workers()
    probes = health.probe_open_circuits(af.CONFIG_DIR, required=3)
    stopped = _stop_open_circuit_workers(reaped["live"])
    live = [item for item in reaped["live"] if _pid_alive(item.get("pid"))]
    counts = {driver: sum(1 for item in live if item.get("driver") == driver) for driver in health.DRIVERS}
    outcomes = []
    for project in dict.fromkeys(projects):
        state = af.load_state(project)
        task = state.get("task") or {}
        if task.get("placeholder"):
            outcomes.append({"project": project, "task_id": task.get("id"), "status": "skipped_placeholder"})
            continue
        if not state.get("automation_enabled", True) or task.get("status") not in ("active", "in_progress"):
            outcomes.append({"project": project, "status": "skipped_inactive"})
            continue
        state, branch_error = _ensure_task_branch(project, state)
        task = state.get("task") or {}
        if branch_error:
            outcomes.append({"project": project, "task_id": task.get("id"), "status": "paused_git", "detail": branch_error})
            continue
        running = next((item for item in live if item.get("project") == project and item.get("task_id") == task.get("id")), None)
        if running:
            outcomes.append({"project": project, "task_id": task.get("id"), "status": "skipped_running", "pid": running["pid"]})
            continue
        execution = task.get("execution") or {}
        if execution_is_live(execution):
            outcomes.append({
                "project": project, "task_id": task.get("id"), "status": "skipped_running",
                "pid": execution.get("cli_pid") or execution.get("worker_pid"),
                "dispatch_id": execution.get("dispatch_id"),
            })
            continue
        driver = _driver_for(project, state)
        if driver not in health.DRIVERS and driver != "zellij":
            outcomes.append({"project": project, "task_id": task.get("id"), "status": "skipped_no_cli", "driver": driver})
            continue
        if driver in health.DRIVERS and health.is_open(af.CONFIG_DIR, driver):
            outcomes.append({"project": project, "task_id": task.get("id"), "status": "paused_circuit", "driver": driver})
            continue
        maximum = int(af.load_protocol(project).get("orchestration", {}).get("max_concurrency_per_driver", 5))
        if counts.get(driver, 0) >= maximum:
            outcomes.append({"project": project, "task_id": task.get("id"), "status": "queued_concurrency", "driver": driver})
            continue
        started = _spawn(project, state, driver)
        outcomes.append(started)
        if started.get("status") == "started":
            live.append(started)
            counts[driver] = counts.get(driver, 0) + 1
    return {"workers": outcomes, "finished": reaped["finished"], "health_probes": probes, "stopped": stopped, "counts": counts}
