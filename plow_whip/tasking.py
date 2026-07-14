"""Model-free task intake, planning confirmation, and workflow advancement."""

from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime

from . import git_flow
from . import leases
from . import protocol as proto
from . import routing
from .io_utils import file_lock


DRIVER_ALIASES = {
    "codex": "codex_cli", "codex-cli": "codex_cli", "codex_cli": "codex_cli",
    "cursor": "cursor_cli", "cursor-cli": "cursor_cli", "cursor_cli": "cursor_cli",
    "deepseek": "simple_tasker", "simple-tasker": "simple_tasker", "simple_tasker": "simple_tasker",
}


def _prepare_git(project: str, workflow: dict) -> dict:
    from . import agent_flow as af

    data = af.load_protocol(project)
    target = workflow.get("target_branch", "main")
    if leases.is_strict(data):
        workspace = git_flow.workspace_path(af.CONFIG_DIR, project, workflow["id"])
        return git_flow.prepare_workspace(af.project_dir(project), workspace, workflow["id"], target)
    return git_flow.prepare_branch(af.project_dir(project), workflow["id"], target)
EXPLICIT_DRIVER_PATTERNS = (
    (re.compile(r"\b(?:codex[ _-]?cli)\b", re.I), "codex_cli"),
    (re.compile(r"\b(?:cursor[ _-]?cli)\b", re.I), "cursor_cli"),
    (re.compile(r"\b(?:simple[ _-]?tasker|deepseek)\b", re.I), "simple_tasker"),
)
BOUNDED_DIRECT = (
    "审查", "review", "检查代码", "核对", "只读", "运行测试", "run tests",
    "格式化", "format", "翻译", "translate", "解释", "summarize",
)
COMPLEX_SIGNALS = (
    "平台", "系统", "完整产品", "从零", "架构", "重构", "迁移", "多模块", "全栈",
    "无人值守", "权限体系", "支付系统", "交易系统", "工作流引擎", "设计并实现",
    "platform", "architecture", "migration", "full-stack", "end-to-end product",
)
VAGUE_SIGNALS = (
    "搞一下", "做一下", "优化一下", "完善一下", "处理一下", "看看", "想办法",
    "make it better", "improve it", "fix it", "build me something",
)
READ_ONLY_SIGNALS = ("审查", "review", "核对", "只读", "检查", "分析", "解释", "status")
WRITE_SIGNALS = (
    "修复", "修改", "实现", "编写", "写一个", "新增", "删除", "重构", "迁移",
    "fix", "implement", "change", "modify", "write", "add", "delete", "refactor", "migrate",
)
NEGATED_WRITE_PATTERNS = (
    re.compile(r"(?:禁止|不要|不得|不允许|无需)\s*(?:修改|修复|写入|编辑|改动|删除)(?:任何)?(?:文件|代码|内容)?", re.I),
    re.compile(r"\b(?:do not|don't|must not|without)\s+(?:modify|fix|write|edit|change|delete)\b", re.I),
)


def _has_positive_write_signal(text: str) -> bool:
    remaining = text
    for pattern in NEGATED_WRITE_PATTERNS:
        remaining = pattern.sub("", remaining)
    return any(item in remaining for item in WRITE_SIGNALS)


def _human_inbox(project: str, record: dict) -> None:
    from . import agent_flow as af

    path = os.path.join(af.project_collab_dir(project), "human_inbox.jsonl")
    with file_lock(path + ".lock"):
        with open(path, "a", encoding="utf-8") as file:
            file.write(json.dumps({
                "at": datetime.now().isoformat(timespec="seconds"), **record,
            }, ensure_ascii=False, separators=(",", ":")) + "\n")


def _requested_driver(text: str, requested_cli: str | None) -> str | None:
    if requested_cli:
        normalized = DRIVER_ALIASES.get(requested_cli.strip().lower())
        if not normalized:
            raise ValueError(f"unsupported requested CLI: {requested_cli}")
        return normalized
    for pattern, driver in EXPLICIT_DRIVER_PATTERNS:
        if pattern.search(text):
            return driver
    return None


def classify_task(text: str, requested_cli: str | None = None) -> dict:
    """Conservative local classifier. Ambiguity always goes to the planner."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        raise ValueError("task text cannot be empty")
    lower = cleaned.lower()
    driver = _requested_driver(cleaned, requested_cli)
    complex_hits = [item for item in COMPLEX_SIGNALS if item in lower]
    bounded = any(item in lower for item in BOUNDED_DIRECT)
    vague = len(cleaned) < 8 or any(item in lower for item in VAGUE_SIGNALS)
    read_only = (
        bounded
        and any(item in lower for item in READ_ONLY_SIGNALS)
        and not _has_positive_write_signal(lower)
    )

    if driver and bounded and not complex_hits:
        return {
            "route": "direct", "driver": driver, "confidence": 0.99,
            "reason": "explicit CLI and bounded task", "code_change": not read_only,
        }
    if complex_hits:
        return {
            "route": "needs_planner", "driver": None, "confidence": 0.98,
            "reason": f"broad/complex scope: {', '.join(complex_hits[:3])}", "code_change": True,
        }
    if vague:
        return {
            "route": "needs_planner", "driver": None, "confidence": 0.95,
            "reason": "task is vague or underspecified", "code_change": True,
        }
    if driver:
        return {
            "route": "direct", "driver": driver, "confidence": 0.9,
            "reason": "explicit CLI with a concrete request", "code_change": not read_only,
        }
    if len(cleaned) <= 260:
        return {
            "route": "simple", "driver": "simple_tasker", "confidence": 0.9,
            "reason": "bounded concrete task with no complex-scope signal", "code_change": not read_only,
        }
    return {
        "route": "needs_planner", "driver": None, "confidence": 0.95,
        "reason": "scope exceeds the conservative direct-task boundary", "code_change": True,
    }


def _agent_for_driver(data: dict, driver: str, role: str | None = None) -> str:
    preferred = driver if driver in data.get("agents", {}) else None
    if preferred:
        meta = data["agents"][preferred]
        if (meta.get("enabled", True) and meta.get("schedulable", True)
                and (not role or role in meta.get("roles", []))):
            return preferred
    choices = routing.candidates(data, role=role, driver_available=lambda item: item == driver)
    if not choices:
        raise ValueError(f"no enabled agent can execute driver={driver} role={role or '*'}")
    return choices[0]["agent"]


def planner_owner(data: dict, preferred: str | None = None) -> str:
    selected = preferred or data.get("orchestration", {}).get("default_planner") or "codex_cli"
    if not preferred and selected == "goal-planner":
        selected = "codex_cli"
    if selected in DRIVER_ALIASES:
        selected = _agent_for_driver(data, DRIVER_ALIASES[selected], role="planner")
    meta = data.get("agents", {}).get(selected)
    if (not meta or not meta.get("enabled", True) or not meta.get("schedulable", True)
            or "planner" not in meta.get("roles", [])):
        raise ValueError(f"configured planner is not an enabled planner: {selected}")
    return selected


def _review_task(data: dict, root_id: str, title: str, implementation_owner: str) -> dict:
    implementation = data.get("agents", {}).get(implementation_owner, {})
    implementation_driver = implementation.get("driver")
    choices = routing.candidates(
        data, role="reviewer", capabilities=["review"],
        exclude_agents={implementation_owner}, exclude_drivers={implementation_driver},
        driver_available=lambda driver: driver in ("codex_cli", "cursor_cli", "simple_tasker", "zellij"),
    )
    if not choices:
        choices = routing.candidates(
            data, role="reviewer", capabilities=["review"], exclude_agents={implementation_owner},
            driver_available=lambda driver: driver in ("codex_cli", "cursor_cli", "simple_tasker", "zellij"),
        )
    if not choices:
        choices = routing.candidates(
            data, role="reviewer", capabilities=["review"],
            driver_available=lambda driver: driver in ("codex_cli", "cursor_cli", "simple_tasker", "zellij"),
        )
    if not choices:
        raise ValueError("no executable reviewer is configured")
    owner = choices[0]["agent"]
    return {
        "id": f"{root_id}-REVIEW", "title": f"Independent review: {title}",
        "owner": owner, "status": "active", "stage": "review", "final": True,
        "next_action": "Independently inspect the task branch diff, rerun acceptance checks, and report approve or reject.",
        "acceptance": ["Implementation matches the task", "All checks pass", "No unresolved high-risk finding"],
        "verify_commands": [], "rule_tags": ["review"], "last_output": "", "blockers": [],
        "decision_ids": [], "cli_sessions": {}, "required_role": "reviewer",
        "required_capabilities": ["review"],
        "independent_session": owner == implementation_owner or data["agents"][owner].get("driver") == implementation_driver,
    }


def submit(
    project: str,
    text: str,
    requested_cli: str | None = None,
    planner: str | None = None,
    target_branch: str | None = None,
    source: str = "current_session",
    interaction: dict | None = None,
    replace: bool = False,
) -> dict:
    from . import agent_flow as af

    state = af.load_state(project)
    current = state.get("task", {})
    if current.get("status") in ("active", "in_progress") and current.get("id") != "T-001" and not replace:
        raise ValueError(f"task {current.get('id')} is still active; use --replace only after deliberate cancellation")
    data = proto.ensure(af.project_dir(project), project, af.get_agents(), af.get_agent_meta())
    decision = classify_task(text, requested_cli)
    if decision["route"] == "simple" and "simple-tasker" not in data.get("agents", {}):
        data.setdefault("agents", {})["simple-tasker"] = proto.normalize_agent("simple-tasker")
        proto.save(af.project_dir(project), data)
        proto.write_handbook(af.project_dir(project), data)
        agent_dir = os.path.join(af.conversations_dir(project), "simple-tasker")
        os.makedirs(agent_dir, exist_ok=True)
        af._write_session_template("simple-tasker", project, os.path.join(agent_dir, "current.md"))
    task_id = f"T-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    target = target_branch or data.get("orchestration", {}).get("default_target_branch", "main")
    workflow = {
        "id": task_id, "title": text, "status": "planning" if decision["route"] == "needs_planner" else "active",
        "route": decision["route"], "classification": decision, "source": source,
        "interaction": interaction or {}, "target_branch": target,
        "code_change": bool(decision["code_change"]), "completed": [], "queue": [],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }

    if decision["route"] == "needs_planner":
        owner = planner_owner(data, planner)
        task = {
            "id": f"{task_id}-PLAN", "title": f"Plan: {text}", "owner": owner,
            "status": "active", "stage": "planning", "route": "planner",
            "next_action": "Create a concrete 1-7 milestone plan and submit it with `plow-whip plan propose`; do not implement yet.",
            "acceptance": ["Milestones are independently verifiable", "Final milestone is independent acceptance"],
            "verify_commands": [], "rule_tags": ["planning"], "last_output": "", "blockers": [],
            "decision_ids": [], "cli_sessions": {}, "required_role": "planner", "required_capabilities": [],
        }
    else:
        owner = _agent_for_driver(data, decision["driver"], role="implementation" if decision["code_change"] else None)
        task = {
            "id": task_id, "title": text, "goal": text, "owner": owner,
            "status": "active", "stage": "implementation" if decision["code_change"] else "direct",
            "route": decision["route"], "requested_driver": decision["driver"], "next_action": text,
            "acceptance": [], "verify_commands": [], "rule_tags": [], "last_output": "", "blockers": [],
            "decision_ids": [], "cli_sessions": {}, "required_role": "implementation" if decision["code_change"] else None,
            "required_capabilities": ["simple-task"] if decision["route"] == "simple" else [],
        }
        if decision["route"] == "simple":
            task["verification_policy"] = "sandboxed"
        if decision["code_change"]:
            try:
                workflow["git"] = _prepare_git(project, workflow)
            except git_flow.GitFlowBlocked as exc:
                workflow["git"] = {"status": "pending", "target_branch": target, "error": str(exc)}
            workflow["queue"] = [_review_task(data, task_id, text, owner)]

    state["task"] = task
    state["workflow"] = workflow
    state["automation_enabled"] = True
    state["phase"] = task_id
    af.save_state(project, state)
    af.append_comms(project, f"task submitted: {task_id}; route={decision['route']}; reason={decision['reason']}")
    return {"project": project, "task": task, "workflow": workflow, "classification": decision}


def _plan_milestones(data: dict, workflow: dict, raw: list[dict]) -> list[dict]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 7:
        raise ValueError("plan must contain 1 to 7 milestones")
    if not raw[-1].get("final_acceptance"):
        raise ValueError("last milestone must declare final_acceptance=true")
    milestones = []
    implementation_agents: set[str] = set()
    implementation_drivers: set[str] = set()
    for index, item in enumerate(raw, 1):
        final = index == len(raw)
        title = str(item.get("title", "")).strip()
        acceptance = [str(value).strip() for value in item.get("acceptance", []) if str(value).strip()]
        if not title or not acceptance:
            raise ValueError("every milestone needs title and acceptance")
        role = proto.normalize_role(str(item.get("role") or ("reviewer" if final else "implementation")))
        capabilities = [str(value).strip().lower() for value in item.get("capabilities", []) if str(value).strip()]
        owner = item.get("owner")
        requested_driver = _requested_driver("", item.get("cli")) if item.get("cli") else None
        if requested_driver == "simple_tasker":
            raise ValueError("planner-to-simple-tasker milestone routing is reserved for a future version")
        if owner:
            if owner not in proto.schedulable_agents(data):
                raise ValueError(f"milestone owner is unknown or non-schedulable: {owner}")
        elif requested_driver:
            owner = _agent_for_driver(data, requested_driver, role=role)
        else:
            choices = routing.candidates(
                data, role=role, capabilities=capabilities,
                exclude_agents=implementation_agents if final else None,
                exclude_drivers=implementation_drivers if final else None,
                driver_available=lambda driver: driver in ("codex_cli", "cursor_cli", "zellij"),
            )
            if not choices and final:
                choices = routing.candidates(
                    data, role=role, capabilities=capabilities, exclude_agents=implementation_agents,
                    driver_available=lambda driver: driver in ("codex_cli", "cursor_cli", "zellij"),
                )
            if not choices:
                choices = routing.candidates(
                    data, role=role, capabilities=capabilities,
                    driver_available=lambda driver: driver in ("codex_cli", "cursor_cli", "zellij"),
                )
            if not choices:
                raise ValueError(f"no executable agent matches role={role} capabilities={capabilities}")
            owner = choices[0]["agent"]
        if final and data["agents"][owner].get("driver") in implementation_drivers:
            alternatives = routing.candidates(
                data, role=role, capabilities=capabilities,
                exclude_agents=implementation_agents, exclude_drivers=implementation_drivers,
                driver_available=lambda driver: driver in ("codex_cli", "cursor_cli", "zellij"),
            )
            if alternatives:
                owner = alternatives[0]["agent"]
        milestone = {
            "id": f"{workflow['id']}-M{index}", "title": title, "owner": owner,
            "status": "active", "stage": "review" if final else "implementation", "final": final,
            "next_action": item.get("next_action") or title, "acceptance": acceptance,
            "verify_commands": item.get("verify_commands") or [], "rule_tags": item.get("rule_tags") or [],
            "last_output": "", "blockers": [], "decision_ids": [], "cli_sessions": {},
            "required_role": role, "required_capabilities": capabilities,
            "independent_session": final and (
                owner in implementation_agents or data["agents"][owner].get("driver") in implementation_drivers
            ),
        }
        milestones.append(milestone)
        if not final:
            implementation_agents.add(owner)
            implementation_drivers.add(data["agents"][owner].get("driver"))
    return milestones


def propose_plan(project: str, context_summary: str, plan: list[dict]) -> dict:
    from . import agent_flow as af

    state = af.load_state(project)
    workflow = state.get("workflow") or {}
    task = state.get("task") or {}
    if workflow.get("status") != "planning" or task.get("stage") != "planning":
        raise ValueError("current task is not waiting for a planner proposal")
    data = af.load_protocol(project)
    milestones = _plan_milestones(data, workflow, plan)
    workflow.update({
        "status": "awaiting_confirmation", "context_summary": context_summary[:2000],
        "plan": milestones, "proposed_at": datetime.now().isoformat(timespec="seconds"),
    })
    task.update({
        "status": "blocked_waiting_human", "next_action": "Wait for human plan confirmation",
        "blockers": ["plan_confirmation_required"], "last_output": context_summary[:2000],
    })
    state["workflow"] = workflow
    state["task"] = task
    af.save_state(project, state)
    af.append_comms(project, f"@human plan confirmation required for {workflow['id']}: {context_summary[:500]}")
    _human_inbox(project, {
        "type": "plan_confirmation", "task_id": workflow["id"],
        "source": workflow.get("source"), "interaction": workflow.get("interaction", {}),
        "context_summary": workflow.get("context_summary", ""), "plan": milestones,
    })
    af.notify(f"{project}: 计划等待确认", ring=True)
    return {"project": project, "workflow": workflow, "task": task}


def confirm_plan(project: str) -> dict:
    from . import agent_flow as af

    state = af.load_state(project)
    workflow = state.get("workflow") or {}
    if workflow.get("status") != "awaiting_confirmation" or not workflow.get("plan"):
        raise ValueError("no proposed plan is waiting for confirmation")
    if workflow.get("code_change") and not (workflow.get("git") or {}).get("branch"):
        try:
            workflow["git"] = _prepare_git(project, workflow)
        except git_flow.GitFlowBlocked as exc:
            workflow["git"] = {
                "status": "pending", "target_branch": workflow.get("target_branch", "main"), "error": str(exc),
            }
    plan = copy.deepcopy(workflow["plan"])
    task = plan.pop(0)
    workflow.update({
        "status": "active", "queue": plan, "confirmed_at": datetime.now().isoformat(timespec="seconds")
    })
    state["workflow"] = workflow
    state["task"] = task
    state["automation_enabled"] = True
    af.save_state(project, state)
    af.append_comms(project, f"plan confirmed: {workflow['id']}; execution resumed unattended")
    return {"project": project, "workflow": workflow, "task": task}


def reject_plan(project: str, reason: str) -> dict:
    from . import agent_flow as af

    state = af.load_state(project)
    workflow = state.get("workflow") or {}
    if workflow.get("status") != "awaiting_confirmation":
        raise ValueError("no proposed plan is waiting for rejection")
    workflow["status"] = "planning"
    workflow["rejection_reason"] = reason
    task = state["task"]
    task.update({
        "status": "active", "next_action": f"Revise the plan using human feedback: {reason}",
        "blockers": [], "stage": "planning",
    })
    state["workflow"] = workflow
    state["task"] = task
    af.save_state(project, state)
    return {"project": project, "workflow": workflow, "task": task}


def request_decision(project: str, summary: str, options: list[str], recommended: str | None = None) -> dict:
    """Freeze exactly one task and persist a bounded human choice request."""
    from . import agent_flow as af

    choices = [str(item).strip() for item in options if str(item).strip()]
    if not 2 <= len(choices) <= 3:
        raise ValueError("a decision request needs 2 or 3 concrete options")
    if recommended and recommended not in choices:
        raise ValueError("recommended choice must exactly match one option")
    state = af.load_state(project)
    task = state.get("task") or {}
    if task.get("status") not in ("active", "in_progress"):
        raise ValueError("current task is not executing")
    workflow = state.get("workflow") or {}
    decision_id = f"D-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    request = {
        "id": decision_id, "status": "pending", "summary": summary[:2000],
        "options": choices, "recommended": recommended,
        "requested_at": datetime.now().isoformat(timespec="seconds"),
        "workflow_resume_status": workflow.get("status", "active"),
    }
    leases.revoke(task, "human decision required")
    task.update({
        "status": "blocked_waiting_human", "next_action": "Wait for human decision",
        "blockers": ["decision_required"], "decision_request": request,
    })
    workflow["status"] = "blocked_waiting_human"
    state["task"] = task
    state["workflow"] = workflow
    af.save_state(project, state)
    _human_inbox(project, {
        "type": "decision_required", "task_id": task.get("id"),
        "source": workflow.get("source"), "interaction": workflow.get("interaction", {}),
        **request,
    })
    af.append_comms(project, f"@human decision required {decision_id}: {summary[:500]}")
    af.notify(f"{project}: 任务等待人工决策", ring=True)
    return {"project": project, "decision": request, "task": task}


def answer_decision(project: str, choice: str, note: str = "") -> dict:
    """Record the human choice and make the frozen task schedulable again."""
    from . import agent_flow as af

    state = af.load_state(project)
    task = state.get("task") or {}
    request = task.get("decision_request") or {}
    if request.get("status") != "pending":
        raise ValueError("no decision is waiting for an answer")
    options = request.get("options") or []
    selected = str(choice).strip()
    if selected.isdigit() and 1 <= int(selected) <= len(options):
        selected = options[int(selected) - 1]
    if selected not in options:
        raise ValueError("choice must be an option value or its 1-based number")
    answered_at = datetime.now().isoformat(timespec="seconds")
    request.update({"status": "answered", "choice": selected, "note": note[:1000], "answered_at": answered_at})
    task.setdefault("decision_ids", []).append(request["id"])
    task.update({
        "status": "active", "next_action": f"Continue using human decision: {selected}",
        "blockers": [], "decision_request": request,
    })
    workflow = state.get("workflow") or {}
    workflow["status"] = request.get("workflow_resume_status") or "active"
    state["task"] = task
    state["workflow"] = workflow
    af.save_state(project, state)
    decisions = os.path.join(af.project_memory_dir(project), "DECISIONS.md")
    with file_lock(decisions + ".lock"):
        with open(decisions, "a", encoding="utf-8") as file:
            file.write(f"\n| {request['id']} | {answered_at[:10]} | {selected} — {note[:500]} | Accepted |\n")
    af.append_comms(project, f"decision answered {request['id']}: {selected}; scheduler may resume")
    return {"project": project, "decision": request, "task": task}


def is_git_delivery_retry(workflow: dict, task: dict) -> bool:
    """Return whether a human-blocked task may retry the FF-only delivery step."""
    return bool(
        workflow.get("status") == "blocked_waiting_human"
        and workflow.get("code_change")
        and not workflow.get("queue")
        and task.get("status") == "blocked_waiting_human"
        and task.get("next_action") == "Resolve Git fast-forward blocker"
        and task.get("blockers")
    )


def escalate_to_planner(project: str, reason: str) -> dict:
    """Preserve branch/session/worktree and replace only the active executor with planning."""
    from . import agent_flow as af

    state = af.load_state(project)
    workflow = state.get("workflow") or {}
    data = af.load_protocol(project)
    previous = copy.deepcopy(state.get("task") or {})
    workflow.update({
        "status": "planning", "escalated_from": previous.get("owner"),
        "escalation_reason": reason, "preserved_task": previous,
    })
    owner = planner_owner(data)
    task = {
        "id": f"{workflow.get('id', previous.get('id'))}-REPLAN", "title": f"Replan: {workflow.get('title', previous.get('title'))}",
        "owner": owner, "status": "active", "stage": "planning", "route": "planner",
        "next_action": f"Inspect the preserved work and propose a revised milestone plan. Reason: {reason}",
        "acceptance": ["Preserve useful existing work", "Return an executable plan for human confirmation"],
        "verify_commands": [], "rule_tags": ["planning"], "last_output": "", "blockers": [],
        "decision_ids": [], "cli_sessions": {}, "required_role": "planner", "required_capabilities": [],
    }
    state["workflow"] = workflow
    state["task"] = task
    af.save_state(project, state)
    return {"project": project, "workflow": workflow, "task": task}


def advance_after_completion(project: str, state: dict, completed_task: dict) -> tuple[dict, str] | None:
    """Advance a confirmed non-goal workflow and finalize its Git delivery."""
    from . import agent_flow as af

    workflow = state.get("workflow") or {}
    if workflow.get("status") != "active":
        return None
    protocol = af.load_protocol(project)
    strict = leases.is_strict(protocol)
    if not any(item.get("id") == completed_task.get("id") for item in workflow.setdefault("completed", [])):
        workflow["completed"].append({
            "id": completed_task.get("id"), "title": completed_task.get("title"),
            "owner": completed_task.get("owner"), "output": completed_task.get("last_output", "")[-1000:],
            "execution": copy.deepcopy(completed_task.get("execution", {})),
        })
    if completed_task.get("stage") == "implementation":
        workflow["last_implementation"] = copy.deepcopy(completed_task)
        if strict and workflow.get("code_change"):
            try:
                candidate = git_flow.checkpoint_branch(
                    af.task_workspace(project, state), workflow["id"], workflow.get("git") or {},
                    message=f"feat: checkpoint {completed_task.get('id')}",
                )
            except git_flow.GitFlowBlocked as exc:
                completed_task.update({
                    "status": "blocked_waiting_human", "next_action": "Resolve task checkpoint blocker",
                    "blockers": [str(exc)],
                })
                workflow["status"] = "blocked_waiting_human"
                state["workflow"] = workflow
                state["task"] = completed_task
                _human_inbox(project, {
                    "type": "git_checkpoint_blocked", "task_id": workflow["id"], "reason": str(exc),
                })
                return completed_task, "blocked_waiting_human"
            workflow["candidate_commit"] = candidate
            workflow.setdefault("git", {})["candidate_commit"] = candidate
    if workflow.get("queue"):
        task = workflow["queue"].pop(0)
        if task.get("stage") == "review" and not task.get("verify_commands"):
            task["verify_commands"] = list(completed_task.get("verify_commands") or [])
            if completed_task.get("verification_policy"):
                task["verification_policy"] = completed_task["verification_policy"]
        if task.get("stage") == "review" and workflow.get("candidate_commit"):
            task["candidate_commit"] = workflow["candidate_commit"]
        state["workflow"] = workflow
        state["task"] = task
        return task, "advance"
    if workflow.get("code_change"):
        if strict:
            try:
                git_flow.assert_review_commit(
                    af.task_workspace(project, state), workflow.get("candidate_commit", "")
                )
            except git_flow.GitFlowBlocked as exc:
                completed_task.update({
                    "status": "blocked_waiting_human", "next_action": "Resolve review integrity violation",
                    "blockers": [str(exc)],
                })
                workflow["status"] = "blocked_waiting_human"
                state["workflow"] = workflow
                state["task"] = completed_task
                _human_inbox(project, {
                    "type": "review_integrity_violation", "task_id": workflow["id"], "reason": str(exc),
                })
                return completed_task, "blocked_waiting_human"
            workflow["status"] = "delivery_ready"
            workflow["delivery"] = {
                "status": "ready", "commit": workflow["candidate_commit"],
                "branch": (workflow.get("git") or {}).get("branch"),
                "target_branch": workflow.get("target_branch", "main"),
            }
            state["workflow"] = workflow
            state["task"] = completed_task
            return completed_task, "delivery_ready"
        try:
            delivery = git_flow.finalize_fast_forward(
                af.project_dir(project),
                workflow["id"], workflow.get("git") or {},
                message=f"feat: complete {workflow['id']}",
            )
            workflow["delivery"] = delivery
        except git_flow.GitFlowBlocked as exc:
            completed_task.update({
                "status": "blocked_waiting_human", "next_action": "Resolve Git fast-forward blocker",
                "blockers": [str(exc)],
            })
            workflow["status"] = "blocked_waiting_human"
            state["workflow"] = workflow
            state["task"] = completed_task
            _human_inbox(project, {
                "type": "git_fast_forward_blocked", "task_id": workflow["id"], "reason": str(exc),
            })
            af.notify(f"{project}: Git 快进合并等待处理", ring=True)
            return completed_task, "blocked_waiting_human"
    workflow["status"] = "done"
    workflow["completed_at"] = datetime.now().isoformat(timespec="seconds")
    state["workflow"] = workflow
    state["task"] = completed_task
    return completed_task, "complete"


def reject_review(project: str, reason: str) -> dict:
    """Return a rejected review to the original executor and preserved session."""
    from . import agent_flow as af

    state = af.load_state(project)
    workflow = state.get("workflow") or {}
    review = state.get("task") or {}
    implementation = copy.deepcopy(workflow.get("last_implementation") or {})
    if workflow.get("status") != "active" or review.get("stage") != "review" or not implementation:
        raise ValueError("current workflow is not in an independently reviewable state")
    count = int(workflow.get("review_rejections", 0)) + 1
    workflow["review_rejections"] = count
    archived_review = copy.deepcopy(review)
    archived_at = datetime.now().isoformat(timespec="seconds")
    for session in archived_review.get("cli_sessions", {}).values():
        session["status"] = "archived"
        session["archived_at"] = archived_at
    archived_review.update({"status": "rejected", "reason": reason, "archived_at": archived_at})
    workflow.setdefault("review_history", []).append(archived_review)
    rerun_review = copy.deepcopy(review)
    rerun_review.update({
        "id": f"{workflow['id']}-REVIEW-R{count + 1}", "status": "active",
        "last_output": "", "blockers": [], "cli_sessions": {},
    })
    workflow.setdefault("queue", []).insert(0, rerun_review)
    implementation.update({
        "status": "active", "next_action": f"Repair independent review findings: {reason}",
        "last_output": "\n".join(filter(None, [implementation.get("last_output", ""), f"Reviewer rejected: {reason}"])),
        "blockers": [],
    })
    state["workflow"] = workflow
    state["task"] = implementation
    af.save_state(project, state)
    af.append_comms(project, f"review rejected {workflow['id']}; original executor/session resumed: {reason[:500]}")
    return {"project": project, "workflow": workflow, "task": implementation}
