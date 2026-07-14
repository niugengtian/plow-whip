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
from . import leases
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


def _update_registry(mutator):
    """Apply one read-modify-write under the registry lock."""
    path = _registry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with file_lock(path + ".lock"):
        registry = _load_registry()
        result = mutator(registry)
        atomic_write_json(path, registry)
        return result


def record_cli_pid(project: str, dispatch_id: str, pid: int) -> bool:
    """Persist the real CLI process independently of the mutable current Task."""
    def update(registry: dict) -> bool:
        for worker in registry.get("workers", []):
            if worker.get("project") == project and worker.get("dispatch_id") == dispatch_id:
                worker.update({
                    "cli_pid": int(pid),
                    "cli_started_at": datetime.now().isoformat(timespec="seconds"),
                })
                return True
        return False

    return _update_registry(update)


def _pid_alive(pid: int | None) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _signal_worker_groups(worker: dict, execution: dict, sig: signal.Signals) -> bool:
    """Signal surviving CLI/wrapper groups independently; either may exit first."""
    cli_pid = worker.get("cli_pid") or execution.get("cli_pid")
    pids = []
    for pid in (cli_pid, worker.get("pid")):
        if pid and int(pid) not in pids:
            pids.append(int(pid))
    sent = False
    for pid in pids:
        if not _pid_alive(pid):
            continue
        try:
            os.killpg(pid, sig)
            sent = True
        except OSError:
            continue
    return sent


def execution_is_live(execution: dict, excluding_dispatch: str | None = None) -> bool:
    """Treat a claimed execution as live before its child PID is available."""
    if execution.get("status") not in ("starting", "running", "stopping", "revoked"):
        return False
    if excluding_dispatch and execution.get("dispatch_id") == excluding_dispatch:
        return False
    if any(_pid_alive(execution.get(key)) for key in ("cli_pid", "worker_pid")):
        return True
    if execution.get("status") in ("stopping", "revoked"):
        return False
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
        protocol = af.load_protocol(project)
        previous_lease = execution.get("lease") or {}
        generation = int(previous_lease.get("generation", 0)) + 1
        execution.update({
            "dispatch_id": dispatch_id, "driver": driver, "logical_owner": agent,
            "status": "starting", "claimed_at": now,
        })
        token = None
        if leases.is_strict(protocol):
            token, metadata = leases.issue(
                af.CONFIG_DIR, project, task, protocol, dispatch_id, driver, agent, generation,
                ttl_seconds=int(protocol.get("orchestration", {}).get("lease_ttl_seconds", leases.DEFAULT_TTL_SECONDS)),
            )
            execution["lease"] = metadata
        try:
            af.save_state(project, state)
            return {"claimed": True, "execution": dict(execution), "lease_token": token}
        except RuntimeError:
            continue
    return {"claimed": False, "detail": "task claim lost to concurrent state updates"}


def reap_workers() -> dict:
    def reap(registry: dict) -> dict:
        live, finished = [], []
        for worker in registry.get("workers", []):
            if _pid_alive(worker.get("pid")) or _pid_alive(worker.get("cli_pid")):
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
        return {"live": live, "finished": finished}

    return _update_registry(reap)


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
        protocol = af.load_protocol(project)
        if leases.is_strict(protocol):
            workspace = git_flow.workspace_path(af.CONFIG_DIR, project, workflow["id"])
            workflow["git"] = git_flow.prepare_workspace(
                af.project_dir(project), workspace, workflow["id"], workflow.get("target_branch", "main")
            )
        else:
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


def reconcile_delivery(project: str, state: dict) -> dict | None:
    """Publish accepted strict work or observe a user-performed remote merge."""
    workflow = state.get("workflow") or {}
    status = workflow.get("status")
    if status not in ("delivery_ready", "awaiting_human_merge"):
        return None
    delivery = workflow.get("delivery") or {}
    commit = delivery.get("commit") or workflow.get("candidate_commit")
    target = delivery.get("target_branch") or workflow.get("target_branch", "main")
    workspace = af.task_workspace(project, state)
    if status == "awaiting_human_merge":
        if not git_flow.remote_contains(af.project_dir(project), target, commit):
            return {
                "project": project, "task_id": workflow.get("id"),
                "status": "awaiting_human_merge", "commit": commit,
            }
        workflow.update({
            "status": "done", "completed_at": datetime.now().isoformat(timespec="seconds"),
            "delivery": {**delivery, "status": "delivered", "merged": True, "detected_by": "scheduler"},
        })
        state["workflow"] = workflow
        state.setdefault("task", {}).update({"status": "done", "next_action": "", "blockers": []})
        af.save_state(project, state)
        af.append_comms(project, f"delivery detected on origin/{target}: {commit}")
        return {"project": project, "task_id": workflow.get("id"), "status": "delivered", "commit": commit}

    protocol = af.load_protocol(project)
    try:
        published = git_flow.publish_reviewed(
            workspace, workflow["id"], workflow.get("git") or {}, commit,
            auto_merge=bool(protocol.get("orchestration", {}).get("auto_merge_protected", False)),
            publisher_path=af.project_dir(project),
        )
    except git_flow.GitFlowBlocked as exc:
        workflow["delivery"] = {**delivery, "status": "delivery_blocked", "error": str(exc)[-500:]}
        state["workflow"] = workflow
        af.save_state(project, state)
        return {
            "project": project, "task_id": workflow.get("id"),
            "status": "delivery_blocked", "detail": str(exc),
        }
    workflow["delivery"] = published
    if published.get("merged"):
        workflow["status"] = "done"
        workflow["completed_at"] = datetime.now().isoformat(timespec="seconds")
        state.setdefault("task", {}).update({"status": "done", "next_action": "", "blockers": []})
    else:
        from . import tasking

        workflow["status"] = "awaiting_human_merge"
        state.setdefault("task", {}).update({
            "status": "awaiting_human_merge", "next_action": f"Merge {published['branch']} into {published['target_branch']}",
            "blockers": ["human_merge_required"],
        })
        tasking._human_inbox(project, {
            "type": "human_merge_required", "task_id": workflow["id"],
            "branch": published["branch"], "target_branch": published["target_branch"],
            "commit": published["commit"], "reason": published.get("reason", "protected merge identity unavailable"),
        })
        af.notify(f"{project}: 分支已推送，等待人工合并", ring=True)
    state["workflow"] = workflow
    af.save_state(project, state)
    return {"project": project, "task_id": workflow.get("id"), **published}


def quarantine_control_checkout(project: str, state: dict) -> dict | None:
    protocol = af.load_protocol(project)
    if not leases.is_strict(protocol):
        return None
    workflow = state.get("workflow") or {}
    if workflow.get("status") == "awaiting_human_merge":
        # This state explicitly delegates the control checkout to a human. A
        # conflicted merge may leave tracked changes until it is resolved and
        # pushed; preserve the delivery state so reconciliation can resume.
        return None
    changed = git_flow.unexpected_control_changes(af.project_dir(project))
    if not changed:
        return None
    task = state.get("task") or {}
    leases.revoke(task, "unleased control-checkout changes detected")
    task.update({
        "status": "blocked_waiting_human", "next_action": "Inspect unauthorized control-checkout changes",
        "blockers": ["unauthorized_control_changes", *changed[:5]],
    })
    if workflow:
        workflow["status"] = "blocked_waiting_human"
        state["workflow"] = workflow
    state["task"] = task
    af.save_state(project, state)
    leases.audit(
        af.CONFIG_DIR, project, "unauthorized_control_changes",
        task_id=task.get("id"), files=changed[:20],
    )
    from . import tasking

    tasking._human_inbox(project, {
        "type": "unauthorized_control_changes", "task_id": task.get("id"), "files": changed[:20],
    })
    af.notify(f"{project}: 检测到无租约主仓库改动，任务已隔离", ring=True)
    return {
        "project": project, "task_id": task.get("id"),
        "status": "quarantined_control_changes", "files": changed[:20],
    }


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
            execution = (task.get("execution") or {}) if task.get("id") == worker.get("task_id") else {}
            if not _signal_worker_groups(worker, execution, sig):
                continue
            worker["stop_requested_at"] = requested or now.isoformat(timespec="seconds")
            actions.append({"project": worker["project"], "task_id": worker["task_id"], "signal": sig.name})
        except (OSError, ValueError):
            pass
    if actions:
        by_dispatch = {item["dispatch_id"]: item for item in live}
        _update_registry(lambda registry: registry.update({
            "workers": [by_dispatch.get(item.get("dispatch_id"), item) for item in registry.get("workers", [])]
        }))
    return actions


def _stop_revoked_workers(live: list[dict], grace_seconds: int = 30) -> list[dict]:
    """Gracefully stop revoked workers, then force-kill on a later scheduler tick."""
    actions = []
    now = datetime.now()
    for worker in live:
        try:
            state = af.load_state(worker["project"])
            task = state.get("task") or {}
            execution = task.get("execution") or {}
            lease = execution.get("lease") or {}
            authorized = (
                task.get("id") == worker.get("task_id")
                and task.get("status") in ("active", "in_progress")
                and execution.get("dispatch_id") == worker.get("dispatch_id")
                and (not leases.is_strict(af.load_protocol(worker["project"])) or leases.metadata_active(lease))
            )
            if authorized:
                continue
            requested = worker.get("stop_requested_at")
            try:
                requested_at = datetime.fromisoformat(requested) if requested else None
            except ValueError:
                requested_at = None
            sig = (
                signal.SIGKILL
                if requested_at and now - requested_at >= timedelta(seconds=grace_seconds)
                else signal.SIGTERM
            )
            if not _signal_worker_groups(worker, execution, sig):
                continue
            worker["stop_requested_at"] = requested or now.isoformat(timespec="seconds")
            if task.get("id") == worker.get("task_id") and execution.get("dispatch_id") == worker.get("dispatch_id"):
                execution["status"] = "stopping"
                execution["stop_requested_at"] = worker["stop_requested_at"]
                task["execution"] = execution
                state["task"] = task
                try:
                    af.save_state(worker["project"], state)
                except RuntimeError:
                    pass
            actions.append({
                "project": worker["project"], "task_id": worker["task_id"],
                "signal": sig.name, "reason": "lease_revoked_or_task_frozen",
            })
        except (OSError, ValueError, KeyError):
            continue
    if actions:
        updated = {item.get("dispatch_id"): item for item in live}
        _update_registry(lambda registry: registry.update({
            "workers": [updated.get(item.get("dispatch_id"), item) for item in registry.get("workers", [])]
        }))
    return actions


def renew_live_leases(live: list[dict]) -> list[dict]:
    """Extend signed metadata for live strict workers without replacing tokens."""
    renewed = []
    for worker in live:
        if not (_pid_alive(worker.get("pid")) or _pid_alive(worker.get("cli_pid"))):
            continue
        for _ in range(3):
            try:
                project = worker["project"]
                state = af.load_state(project)
                protocol = af.load_protocol(project)
                if not leases.is_strict(protocol):
                    break
                task = state.get("task") or {}
                execution = task.get("execution") or {}
                lease = execution.get("lease") or {}
                ttl = max(60, int(protocol.get("orchestration", {}).get(
                    "lease_ttl_seconds", leases.DEFAULT_TTL_SECONDS,
                )))
                try:
                    expires_at = datetime.fromisoformat(str(lease.get("expires_at") or ""))
                    remaining = (expires_at - datetime.now()).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    remaining = -1
                if (
                    task.get("id") != worker.get("task_id")
                    or task.get("status") not in ("active", "in_progress")
                    or execution.get("status") not in ("starting", "running")
                    or execution.get("dispatch_id") != worker.get("dispatch_id")
                    or execution.get("worker_pid") != worker.get("pid")
                    or lease.get("status") != "active"
                    or remaining > ttl / 2
                ):
                    break
                now = datetime.now()
                lease.update({
                    "expires_at": (now + timedelta(seconds=ttl)).isoformat(timespec="seconds"),
                    "renewed_at": now.isoformat(timespec="seconds"),
                    "renewals": int(lease.get("renewals", 0)) + 1,
                })
                execution["lease"] = lease
                task["execution"] = execution
                state["task"] = task
                af.save_state(project, state)
                leases.audit(
                    af.CONFIG_DIR, project, "lease_renewed",
                    task_id=task.get("id"), dispatch_id=execution.get("dispatch_id"), lease_id=lease.get("id"),
                )
                renewed.append({
                    "project": project, "task_id": task.get("id"),
                    "dispatch_id": execution.get("dispatch_id"), "expires_at": lease["expires_at"],
                })
                break
            except leases.StateIntegrityError:
                break
            except RuntimeError:
                continue
            except KeyError:
                break
    return renewed


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
    if claim.get("lease_token"):
        env[leases.TOKEN_ENV] = claim["lease_token"]
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
    _update_registry(lambda registry: registry.setdefault("workers", []).append(worker))
    atomic_write_json(gate_file, {"ready": True})
    return {**worker, "status": "started"}


def dispatch_projects(projects: list[str]) -> dict:
    """Reap, probe open circuits, enforce limits, and spawn one worker per eligible Task."""
    reaped = reap_workers()
    probes = health.probe_open_circuits(af.CONFIG_DIR, required=3)
    renewed = renew_live_leases(reaped["live"])
    stopped = _stop_open_circuit_workers(reaped["live"])
    stopped.extend(_stop_revoked_workers(reaped["live"]))
    live = [
        item for item in reaped["live"]
        if _pid_alive(item.get("pid")) or _pid_alive(item.get("cli_pid"))
    ]
    counts = {driver: sum(1 for item in live if item.get("driver") == driver) for driver in health.DRIVERS}
    outcomes = []
    for project in dict.fromkeys(projects):
        try:
            state = af.load_state(project)
        except leases.StateIntegrityError as exc:
            leases.audit(af.CONFIG_DIR, project, "state_integrity_failed", detail=str(exc))
            outcomes.append({
                "project": project,
                "status": "paused_invalid_state",
                "detail": str(exc),
            })
            continue
        quarantined = quarantine_control_checkout(project, state)
        if quarantined:
            outcomes.append(quarantined)
            continue
        delivery = reconcile_delivery(project, state)
        if delivery:
            outcomes.append(delivery)
            continue
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
        protocol = af.load_protocol(project)
        if leases.is_strict(protocol) and driver == "zellij":
            outcomes.append({
                "project": project, "task_id": task.get("id"), "status": "paused_unsupported_driver",
                "driver": driver, "detail": "strict workers require a lease-capable CLI driver; zellij is legacy-only",
            })
            continue
        if driver not in health.DRIVERS and driver != "zellij":
            outcomes.append({"project": project, "task_id": task.get("id"), "status": "skipped_no_cli", "driver": driver})
            continue
        if driver in health.DRIVERS and health.is_open(af.CONFIG_DIR, driver):
            outcomes.append({"project": project, "task_id": task.get("id"), "status": "paused_circuit", "driver": driver})
            continue
        maximum = int(protocol.get("orchestration", {}).get("max_concurrency_per_driver", 5))
        if counts.get(driver, 0) >= maximum:
            outcomes.append({"project": project, "task_id": task.get("id"), "status": "queued_concurrency", "driver": driver})
            continue
        started = _spawn(project, state, driver)
        outcomes.append(started)
        if started.get("status") == "started":
            live.append(started)
            counts[driver] = counts.get(driver, 0) + 1
    return {
        "workers": outcomes, "finished": reaped["finished"], "health_probes": probes,
        "renewed": renewed, "stopped": stopped, "counts": counts,
    }
