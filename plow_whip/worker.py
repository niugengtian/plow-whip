"""One background worker process for exactly one Task/CLI binding."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime

from . import agent_flow as af
from . import health, routing, tasking
from .dispatch import dispatch
from .drive import build_drive_prompt
from .io_utils import atomic_write_json


def _fallback(project: str, current_driver: str) -> dict | None:
    state = af.load_state(project)
    task = state.get("task") or {}
    data = af.load_protocol(project)
    excluded = {current_driver}
    if current_driver != "simple_tasker":
        excluded.add("simple_tasker")
    choices = routing.candidates(
        data,
        role=task.get("required_role"),
        capabilities=task.get("required_capabilities"),
        exclude_drivers=excluded,
        driver_available=lambda driver: driver in ("codex_cli", "cursor_cli", "simple_tasker", "zellij"),
    )
    return choices[0] if choices else None


def _record_failure(project: str, task_id: str, driver: str, result: dict) -> dict:
    detail = result.get("detail", "")
    category = health.classify_failure(detail)
    if category == "network":
        network = health.probe_network()
        affected = health.DRIVERS if not network.get("overseas_success") else (driver,)
        for affected_driver in affected:
            health.open_circuit(af.CONFIG_DIR, affected_driver, category, detail)
    elif category == "service":
        health.open_circuit(af.CONFIG_DIR, driver, category, detail)
    state = af.load_state(project)
    task = state.get("task") or {}
    if task.get("id") != task_id:
        return {"category": category, "state_changed": True}
    attempts = task.setdefault("attempts", {})
    if category not in ("network", "service"):
        attempts[driver] = int(attempts.get(driver, 0)) + 1
    execution = task.setdefault("execution", {})
    execution.update({
        "driver": driver, "status": "circuit_open" if category in ("network", "service") else "failed",
        "failure_category": category, "detail": detail[-1000:], "finished_at": datetime.now().isoformat(timespec="seconds"),
    })
    retry_count = int(attempts.get(driver, 0))
    if retry_count >= 3:
        if driver == "simple_tasker" and category == "implementation":
            af.save_state(project, state)
            tasking.escalate_to_planner(project, f"simple-tasker failed {retry_count} implementation attempts: {detail}")
            return {"category": category, "action": "planner", "attempts": retry_count}
        route = _fallback(project, driver)
        if route:
            task["owner"] = route["agent"]
            task["requested_driver"] = route["driver"]
            task["next_action"] = f"Continue the same task from preserved files/session after {driver} failed: {detail[-300:]}"
            execution.update({"status": "failover_ready", "next_driver": route["driver"]})
    af.save_state(project, state)
    return {"category": category, "attempts": retry_count, "action": execution.get("status")}


def run(project: str, agent: str, driver: str, task_id: str, dispatch_id: str) -> dict:
    state = af.load_state(project)
    task = state.get("task") or {}
    if task.get("id") != task_id or task.get("status") not in ("active", "in_progress"):
        return {"success": False, "status": "stale_worker", "detail": "task changed before worker start"}
    prompt = build_drive_prompt(
        project, agent, task.get("next_action") or task.get("title", ""),
        requested_by="plow-whip-supervisor", project_path=af.task_workspace(project, state), dispatch_id=dispatch_id,
    )
    result = dispatch(
        agent, project, prompt, force_channel=driver, dispatch_id=dispatch_id, task_id=task_id,
    )
    if not result.get("success"):
        result["failure"] = _record_failure(project, task_id, driver, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--driver", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--start-gate", required=True)
    args = parser.parse_args()
    deadline = time.monotonic() + 10
    while not os.path.exists(args.start_gate) and time.monotonic() < deadline:
        time.sleep(0.05)
    if not os.path.exists(args.start_gate):
        atomic_write_json(args.result_file, {"success": False, "status": "worker_gate_timeout"})
        return 1
    try:
        result = run(args.project, args.agent, args.driver, args.task_id, args.dispatch_id)
    except Exception as exc:  # Worker boundary: persist the failure for the next local tick.
        result = {"success": False, "status": "worker_crash", "detail": f"{type(exc).__name__}: {exc}"}
        try:
            result["failure"] = _record_failure(args.project, args.task_id, args.driver, result)
        except Exception as record_exc:
            result["record_error"] = f"{type(record_exc).__name__}: {record_exc}"
    os.makedirs(os.path.dirname(args.result_file), exist_ok=True)
    atomic_write_json(args.result_file, result)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
