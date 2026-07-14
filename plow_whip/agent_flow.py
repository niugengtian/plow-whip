#!/usr/bin/env python3
"""
plow-whip — Multi-Agent Collaboration State Machine Engine
多Agent协作工具 · 耕田之鞭

Usage:
    plow-whip --project <name> status              # View project status
    plow-whip --project <name> handoff --output …   # Handoff to next agent
    plow-whip --project <name> init                 # Initialize new project
    plow-whip --project <name> session --agent …    # View agent session
    plow-whip --project <name> rotate --agent …     # Rotate agent session
    plow-whip --project <name> sessions-overview    # All sessions overview
    plow-whip sync                                  # Sync framework updates
    plow-whip list                                  # List all projects
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime

# ── Configuration ──────────────────────────────────────────────────────────────

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.environ.get("PLOW_WHIP_CONFIG_DIR", os.path.join(os.path.expanduser("~"), ".plow-whip"))
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

ROTATE_MAX_LINES = 100
ROTATE_MAX_KB = 8

HOT_TOKEN_BUDGET = 1200
WARM_TOKEN_BUDGET = 4000
HOT_MEMORY_FILES = [
    "AGENT_STATE.json",
]
MACHINE_RULE_FILES = ["AGENT_PROTOCOL.json"]
WARM_MEMORY_FILES = [
    "AGENT_COMMS.md",
]
MEMORY_TEMPLATE_MAP = [
    ("DECISIONS.md.tpl", "DECISIONS.md"),
]

# Collab file rotation thresholds (stricter — these are shared files)
COLLAB_FILE_MAX_LINES = 80
COLLAB_FILE_MAX_KB = 6
COLLAB_KEEP_RECENT_LINES = 30  # When truncating, keep the most recent N lines

# Files tracked for auto-rotation in collab/
TRACKED_COLLAB_FILES = [
    "AGENT_COMMS.md",
    "memory/DECISIONS.md",
    "memory/CHANGELOG.md",
    "memory/CURRENT_STATUS.md",
    "memory/NEXT_ACTION.md",
    "memory/ROADMAP.md",
]

from . import rotation as rot
from . import protocol as proto
from . import routing
from . import leases
from .io_utils import atomic_write_json, atomic_write_text, file_lock

# Agent mention patterns for activity detection
AGENT_PATTERNS = ["cursor", "cursor_cli", "qoder", "qoder_cli", "codex", "codex_cli", "@cursor", "@qoder", "@codex", "handoff", "plow-whip"]

DEFAULT_AGENTS = ["codex", "cursor", "cursor_cli", "codex_cli", "simple-tasker"]
AGENT_LABEL = {
    "cursor": "Cursor Desktop (替代 Qoder CN Desktop)",
    "cursor_cli": "Cursor CLI (替代 Qoder CLI)",
    "qoder": "Qoder CN Desktop (停用)",
    "qoder_cli": "Qoder CLI (停用)",
    "codex": "Codex Desktop (PM+架构师)",
    "codex_cli": "Codex CLI (Code Owner)",
    "simple-tasker": "Simple Tasker (DeepSeek V4 Flash)",
}
AGENT_EMOJI = {
    "cursor": "🟣",
    "cursor_cli": "🟪",
    "qoder": "🔵",
    "qoder_cli": "🔷",
    "codex": "🟢",
    "codex_cli": "🟩",
    "simple-tasker": "⚡",
}


def load_config():
    """Load config from ~/.plow-whip/config.json, create default if missing."""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {"projects_dir": "", "agents": DEFAULT_AGENTS}
    cfg.setdefault("agents", DEFAULT_AGENTS)
    cfg.setdefault("agent_meta", {})
    return cfg


def save_config(cfg):
    atomic_write_json(CONFIG_FILE, cfg)


def get_projects_dir():
    cfg = load_config()
    d = cfg.get("projects_dir", "")
    if not d:
        print("Error: projects_dir not configured.", file=sys.stderr)
        print(f"Run: plow-whip configure --projects-dir /path/to/projects", file=sys.stderr)
        sys.exit(1)
    return d


def get_agents():
    cfg = load_config()
    agents = cfg.get("agents", DEFAULT_AGENTS)
    for agent in agents:
        validate_identifier(agent, "agent")
    return agents


def get_agent_meta(agent=None):
    meta = load_config().get("agent_meta", {})
    return meta.get(agent, {}) if agent else meta


def get_agent_label(agent):
    role = get_agent_meta(agent).get("role")
    return role or AGENT_LABEL.get(agent, agent)


def get_agent_assignment(agent):
    return get_agent_meta(agent).get("assignment", "")


# ── Path Resolution ────────────────────────────────────────────────────────────

def project_dir(project):
    validate_identifier(project, "project")
    return os.path.join(get_projects_dir(), project)


def task_workspace(project, state=None):
    """Resolve the active strict-task worktree; legacy work stays in the project checkout."""
    state = state or load_state(project)
    ref = ((state.get("workflow") or {}).get("git") or {}).get("workspace_ref")
    if not ref:
        return project_dir(project)
    root = os.path.realpath(os.path.join(CONFIG_DIR, "worktrees"))
    candidate = os.path.realpath(os.path.join(root, ref))
    if os.path.commonpath([root, candidate]) != root:
        raise ValueError("task workspace reference escapes the runtime root")
    return candidate


def validate_identifier(value, label="identifier"):
    if not isinstance(value, str) or not value.strip() or value in (".", ".."):
        raise ValueError(f"invalid {label}: {value!r}")
    if "\x00" in value or "/" in value or "\\" in value:
        raise ValueError(f"invalid {label}: path separators are not allowed")
    return value


def project_collab_dir(project):
    return os.path.join(project_dir(project), "collab")


def project_memory_dir(project):
    return os.path.join(project_collab_dir(project), "memory")


def conversations_dir(project):
    return os.path.join(project_collab_dir(project), "conversations")


def state_file(project):
    return os.path.join(project_collab_dir(project), "AGENT_STATE.json")


def comms_file(project):
    return os.path.join(project_collab_dir(project), "AGENT_COMMS.md")


def conventions_agent_file(project):
    return os.path.join(project_collab_dir(project), "CONVENTIONS.agent.md")


def conventions_human_file(project):
    return os.path.join(project_collab_dir(project), "CONVENTIONS.md")


def protocol_file(project):
    return proto.protocol_path(project_dir(project))


def handbook_file(project):
    return proto.handbook_path(project_dir(project))


def load_protocol(project):
    data = proto.load(project_dir(project))
    leases.verify_protocol(CONFIG_DIR, project, data)
    return data


def get_project_agents(project):
    if os.path.exists(protocol_file(project)):
        try:
            return proto.enabled_agents(load_protocol(project))
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    return get_agents()


# ── Templates ──────────────────────────────────────────────────────────────────

def template_dir():
    return os.path.join(PACKAGE_DIR, "templates")


def render_template(template_name, project):
    """Render a template file with {PROJECT_NAME} substitution."""
    tpl_path = os.path.join(template_dir(), template_name)
    if not os.path.exists(tpl_path):
        return None
    with open(tpl_path, encoding="utf-8") as f:
        content = f.read()
    return content.replace("{PROJECT_NAME}", project)


def write_rendered(target_path, template_name, project):
    """Render template and write to target path."""
    content = render_template(template_name, project)
    if content is not None:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    return False


def _read_text_file(path, default=""):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


def _write_compat_conventions(project):
    """Keep legacy filenames as tiny pointers; agents read AGENT_PROTOCOL.json."""
    agent_text = (
        "# Agent conventions compatibility pointer\n\n"
        "Canonical machine rules: `AGENT_PROTOCOL.json`.\n"
        f"Startup: `plow-whip --project {project} start --agent <agent> --json`.\n"
    )
    human_text = (
        "# 多 Agent 协作约定\n\n"
        "完整中文说明见 `HANDBOOK.zh-CN.md`；本文件仅为旧工具兼容入口。\n"
    )
    for path, content in ((conventions_agent_file(project), agent_text), (conventions_human_file(project), human_text)):
        if _read_text_file(path) != content:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)


# ── Notifications ──────────────────────────────────────────────────────────────

def apple_string(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def notify(message, ring=False):
    if ring or sys.platform != "darwin":
        print("\a", end="")
    if sys.platform != "darwin":
        return
    script = f"display notification {apple_string(message)} with title {apple_string('🪢 plow-whip')}"
    subprocess.run(
        ["osascript", "-e", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


# ── State Read/Write ───────────────────────────────────────────────────────────

def default_state(project):
    agents = (
        proto.schedulable_agents(load_protocol(project))
        if os.path.exists(protocol_file(project))
        else [agent for agent in get_agents() if agent != "codex"]
    )
    first_agent = agents[0] if agents else "agent"
    return {
        "current_agent": first_agent,
        "revision": 0,
        "phase": "initialization",
        "status": "in_progress",
        "project_id": project,
        "project_root": ".",
        "task_context": {"day": 0, "topic": "", "project_dir": ""},
        "assigned_agent": first_agent,
        "blockers": [],
        "last_wake_hash": "",
        "automation_enabled": True,
        "last_dispatch_id": "",
        "last_wake_status": "",
        "last_woken_at": "",
        "wake_count": 0,
        "last_output": "",
        "files_changed": [],
        "verify_commands": [],
        "next_action": f"{first_agent} starts requirements analysis for {project}",
        "task": {
            "id": "T-001",
            "title": "Project initialization",
            "goal": f"Initialize {project}",
            "owner": first_agent,
            "status": "active",
            "next_action": f"{first_agent} starts requirements analysis for {project}",
            "acceptance": [],
            "verify_commands": [],
            "rule_tags": [],
            "last_output": "",
            "blockers": [],
            "decision_ids": [],
            "cli_sessions": {},
            "placeholder": True,
        },
        "goal": None,
        "workflow": None,
        "goal_queue": [],
        "goal_history": [],
        "updated_at": "",
    }


def load_state(project):
    sf = state_file(project)
    if not os.path.exists(sf):
        print(f"Error: project '{project}' not found. Run: plow-whip --project {project} init", file=sys.stderr)
        sys.exit(1)
    with open(sf, encoding="utf-8") as f:
        state = json.load(f)
    if os.path.exists(protocol_file(project)):
        leases.verify_state(CONFIG_DIR, project, state, load_protocol(project))
    interaction = (state.get("workflow") or {}).get("interaction") or {}
    legacy_thread_id = interaction.pop("thread_id", None)
    if legacy_thread_id and "thread_ref" not in interaction:
        from .codex_desktop import thread_ref

        interaction["thread_ref"] = thread_ref(legacy_thread_id)
    base = default_state(project)
    for key in base:
        if key not in state:
            state[key] = base[key]
    task = state.setdefault("task", {})
    task.setdefault("id", "T-001")
    task.setdefault("title", state.get("next_action") or "Current task")
    if "goal" not in task and "goal_id" not in task:
        task["goal"] = task.get("title", "Current task")
    task.setdefault("owner", state.get("assigned_agent") or state.get("current_agent"))
    task.setdefault("status", {"in_progress": "active"}.get(state.get("status"), state.get("status", "active")))
    task.setdefault("next_action", state.get("next_action", ""))
    task.setdefault("acceptance", [])
    task.setdefault("verify_commands", state.get("verify_commands", []))
    task.setdefault("rule_tags", [])
    task.setdefault("last_output", state.get("last_output", ""))
    task.setdefault("blockers", state.get("blockers", []))
    task.setdefault("decision_ids", [])
    task.setdefault("cli_sessions", {})
    if task.get("id") == "T-001" and task.get("title") == "Project initialization":
        task.setdefault("placeholder", True)
    goal = state.get("goal") or {}
    if goal.get("id"):
        for milestone in [task, *goal.get("queue", [])]:
            if milestone.get("goal") == goal.get("text"):
                milestone.pop("goal", None)
                milestone["goal_id"] = goal["id"]
    _apply_task_to_legacy_fields(state)
    state.setdefault("project_id", project)
    state.setdefault("project_root", ".")
    state.setdefault("task_context", {}).pop("project_path", None)
    return state


def write_state(project, state, touch=True):
    interaction = (state.get("workflow") or {}).get("interaction") or {}
    legacy_thread_id = interaction.pop("thread_id", None)
    if legacy_thread_id and "thread_ref" not in interaction:
        from .codex_desktop import thread_ref

        interaction["thread_ref"] = thread_ref(legacy_thread_id)
    _apply_task_to_legacy_fields(state)
    if touch:
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    state.setdefault("task_context", {}).pop("project_path", None)
    if os.path.exists(protocol_file(project)):
        state.pop("agents", None)
        state.pop("agent_meta", None)
    sf = state_file(project)
    with file_lock(sf + ".lock"):
        disk_revision = 0
        if os.path.exists(sf):
            with open(sf, encoding="utf-8") as f:
                disk_state = json.load(f)
            if os.path.exists(protocol_file(project)):
                leases.verify_state(CONFIG_DIR, project, disk_state, load_protocol(project))
            disk_revision = disk_state.get("revision", 0)
            if state.get("revision", 0) != disk_revision:
                raise RuntimeError(f"state revision conflict: expected {state.get('revision', 0)}, found {disk_revision}")
        state["revision"] = disk_revision + 1
        if os.path.exists(protocol_file(project)):
            leases.sign_state(CONFIG_DIR, project, state, load_protocol(project))
        atomic_write_json(sf, state)


def save_state(project, state):
    write_state(project, state, touch=True)


def _apply_task_to_legacy_fields(state):
    """Derive old report fields from the canonical task object."""
    task = state.get("task") or {}
    if not task:
        return
    owner = task.get("owner") or state.get("current_agent") or "agent"
    state["current_agent"] = owner
    state["assigned_agent"] = owner
    state["status"] = {"active": "in_progress"}.get(task.get("status"), task.get("status", "in_progress"))
    state["next_action"] = task.get("next_action", "")
    state["last_output"] = task.get("last_output", "")
    state["blockers"] = task.get("blockers", [])
    state["verify_commands"] = task.get("verify_commands", [])


def ensure_project_path(project, state=None, write=True):
    """Drop legacy absolute paths; resolve them from local config at runtime."""
    state = state or load_state(project)
    ctx = state.setdefault("task_context", {})
    changed = "project_path" in ctx
    ctx.pop("project_path", None)
    if changed and write:
        write_state(project, state, touch=False)
    return changed


def append_comms(project, message):
    path = comms_file(project)
    if not os.path.exists(path):
        return
    timestamp = datetime.now().isoformat(timespec="seconds")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n### [system] {timestamp}\n\n{message}\n")
    _archive_collab_file(project, "AGENT_COMMS.md", topic="AGENT_COMMS", quiet=True)


# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_configure(args):
    """Configure plow-whip settings."""
    cfg = load_config()
    if args.projects_dir:
        cfg["projects_dir"] = os.path.abspath(args.projects_dir)
    if args.agents:
        cfg["agents"] = args.agents
        cfg.setdefault("agent_meta", {})
    save_config(cfg)
    print(f"\n🪢 plow-whip configured:")
    print(f"   projects_dir: {cfg.get('projects_dir', '(not set)')}")
    print(f"   agents: {cfg.get('agents', DEFAULT_AGENTS)}")
    print(f"   config: {CONFIG_FILE}\n")


def write_agent_manifest(project):
    path = os.path.join(project_collab_dir(project), "AGENTS.md")
    lines = [f"# Agents — {project}", "", "| Agent | Roles | Driver | Schedulable | Capabilities | Assignment |", "|---|---|---|---|---|---|"]
    protocol = load_protocol(project) if os.path.exists(protocol_file(project)) else None
    for agent in get_project_agents(project):
        meta = protocol.get("agents", {}).get(agent, {}) if protocol else {}
        roles = ", ".join(meta.get("roles") or [meta.get("role") or get_agent_label(agent)])
        capabilities = ", ".join(meta.get("capabilities") or []) or "—"
        assignment = meta.get("assignment") or get_agent_assignment(agent) or "—"
        schedulable = "yes" if meta.get("schedulable", True) else "no"
        lines.append(f"| `{agent}` | {roles} | {meta.get('driver', 'file')} | {schedulable} | {capabilities} | {assignment} |")
    atomic_write_text(path, "\n".join(lines) + "\n")


def cmd_agent(args, project=None):
    cfg = load_config()
    cfg.setdefault("agent_meta", {})
    if args.action == "list":
        if project and os.path.exists(protocol_file(project)):
            data = load_protocol(project)
            for agent in proto.enabled_agents(data):
                meta = data["agents"][agent]
                suffix = f" — {meta.get('assignment')}" if meta.get("assignment") else ""
                print(f"{agent}: {meta.get('role', agent)}{suffix}")
        else:
            for agent in cfg.get("agents", DEFAULT_AGENTS):
                assignment = cfg["agent_meta"].get(agent, {}).get("assignment", "")
                suffix = f" — {assignment}" if assignment else ""
                print(f"{agent}: {get_agent_label(agent)}{suffix}")
        return

    if project and os.path.exists(state_file(project)):
        if os.path.exists(protocol_file(project)):
            load_protocol(project)
        data = proto.ensure(project_dir(project), project, get_agents(), get_agent_meta())
        old = data.setdefault("agents", {}).get(args.name, {})
        meta = {
            "role": args.role or old.get("role") or args.name,
            "roles": getattr(args, "roles", None) or old.get("roles"),
            "capabilities": getattr(args, "capabilities", None) if getattr(args, "capabilities", None) is not None else old.get("capabilities"),
            "driver": getattr(args, "driver", None) or old.get("driver"),
            "priority": getattr(args, "priority", None) if getattr(args, "priority", None) is not None else old.get("priority", 50),
            "cost_tier": getattr(args, "cost_tier", None) or old.get("cost_tier", "medium"),
            "assignment": args.assignment if args.assignment is not None else old.get("assignment", ""),
            "enabled": True,
            "schedulable": getattr(args, "schedulable", None) if getattr(args, "schedulable", None) is not None else old.get("schedulable", True),
        }
        data.setdefault("agents", {})[args.name] = proto.normalize_agent(args.name, meta)
        proto.save(project_dir(project), data)
        proto.write_handbook(project_dir(project), data)
        agent_dir = os.path.join(conversations_dir(project), args.name)
        os.makedirs(agent_dir, exist_ok=True)
        current = os.path.join(agent_dir, "current.md")
        if not os.path.exists(current):
            _write_session_template(args.name, project, current)
        write_agent_manifest(project)
        append_comms(project, f"Agent 更新：`{args.name}` = {meta.get('role', args.name)}；作业：{meta.get('assignment', '—')}。")
    else:
        if args.name not in cfg.get("agents", []):
            cfg.setdefault("agents", []).append(args.name)
        meta = cfg["agent_meta"].setdefault(args.name, {})
        if args.role:
            meta["role"] = args.role
        for field in ("roles", "capabilities", "driver", "priority", "cost_tier"):
            value = getattr(args, field, None)
            if value is not None:
                meta[field] = value
        if args.assignment:
            meta["assignment"] = args.assignment
        save_config(cfg)

    print(f"agent saved: {args.name}")


def cmd_status(project):
    state = load_state(project)
    ensure_project_path(project, state)
    agent = state["current_agent"]
    status_map = {"in_progress": "In Progress", "done": "Done", "blocked": "Blocked"}
    status_text = status_map.get(state["status"], state["status"])
    ctx = state.get("task_context", {})

    print(f"\n📂 Project: {project}")
    print(f"🤖 Current turn: {agent} ({state['phase']} — {status_text})")
    if ctx.get("day"):
        print(f"📅 Task: Day {ctx['day']} — {ctx.get('topic', '')}")
    if ctx.get("project_dir"):
        print(f"📁 Code: {ctx['project_dir']}")
    print(f"📍 Path: {ctx.get('project_path', project_dir(project))}")
    print(f"📌 Last output: {state['last_output'] or '(none)'}")
    files = state.get("files_changed", [])
    if files:
        print(f"📝 Changed files: {', '.join(files)}")
    cmds = state.get("verify_commands", [])
    if cmds:
        print("🧪 Verify commands:")
        for c in cmds:
            print(f"   $ {c}")
    print(f"➡️  Next: {state['next_action'] or '(none)'}")
    print(f"🕐 Updated: {state['updated_at'] or '(never)'}")
    print()



def _needs_rotation(project, agent):
    """Check if an agent's current.md exceeds rotation thresholds."""
    curr = os.path.join(conversations_dir(project), agent, "current.md")
    if not os.path.exists(curr):
        return False, 0, 0
    size = os.path.getsize(curr)
    with open(curr, encoding="utf-8") as f:
        line_count = len(f.readlines())
    return (line_count > ROTATE_MAX_LINES or size > ROTATE_MAX_KB * 1024), line_count, size


def estimate_tokens(text):
    """Cheap token estimate good enough for budget alarms."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _recent_signal_lines(content, limit=8):
    """Keep recent useful lines from a session without carrying the whole session."""
    signals = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if (
            stripped.startswith(("-", "*", "###"))
            or any(marker in lower for marker in ("done", "next", "block", "decision", "output", "todo", "完成", "下一步", "阻塞", "决策"))
        ):
            signals.append(stripped)
    return signals[-limit:]


def generate_carry_forward(project, agent, content, summary=None):
    """Build a compact deterministic handoff summary for archived sessions."""
    state = load_state(project)
    lines = [
        "## Carry Forward",
        f"- Current goal: {state.get('next_action') or '(none)'}",
        f"- Last output: {state.get('last_output') or '(none)'}",
        f"- Phase/status: {state.get('phase', '')} / {state.get('status', '')}",
    ]
    blockers = state.get("blockers") or []
    lines.append(f"- Blockers: {', '.join(blockers) if blockers else '(none)'}")
    files = state.get("files_changed") or []
    lines.append(f"- Files touched: {', '.join(files) if files else '(none)'}")
    verify = state.get("verify_commands") or []
    lines.append(f"- Verify: {', '.join(verify) if verify else '(none)'}")
    if summary:
        lines.append(f"- Human summary: {summary}")
    signals = _recent_signal_lines(content)
    if signals:
        lines.append("")
        lines.append("### Recent Signals")
        lines.extend(f"- {_clamp_text(line, 220)}" for line in signals)
    lines.append("")
    return "\n".join(lines)


def check_and_rotate_agent(project, agent, topic=None):
    """Auto-rotate an agent's session if it exceeds thresholds.
    Returns True if rotation was performed, False otherwise."""
    needs, lines, size = _needs_rotation(project, agent)
    if not needs:
        return False

    curr = os.path.join(conversations_dir(project), agent, "current.md")
    with open(curr, encoding="utf-8") as f:
        content = f.read()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_topic = (topic or "auto_session").replace(" ", "_").replace("/", "-")[:40]
    archive_name = f"{timestamp}_{safe_topic}.md"
    archive_path = os.path.join(conversations_dir(project), agent, archive_name)

    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(f"# Archived Session: {safe_topic}\n")
        f.write(f"**AI:** {agent}\n")
        f.write(f"**Archived:** {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"**Project:** {project}\n")
        f.write(f"**Trigger:** auto-rotation ({lines} lines, {size / 1024:.1f}KB)\n\n")
        f.write(generate_carry_forward(project, agent, content))
        f.write("\n")
        f.write("## Original Content\n\n")
        f.write(content)

    _write_session_template(agent, project, curr)
    print(f"🔄 Auto-rotated: {agent} ({lines} lines, {size / 1024:.1f}KB → {archive_name})")
    notify(f"[auto-rotated] {agent}: {safe_topic}")
    return True


def auto_rotate_all_agents(project):
    """Check and rotate all agents + collab files in a project. Returns summary dict."""
    rotated_agents = []
    for agent in get_project_agents(project):
        if check_and_rotate_agent(project, agent, topic="auto_daemon"):
            rotated_agents.append(agent)
    rotated_files = auto_rotate_collab_files(project)
    return {"agents": rotated_agents, "files": rotated_files}


# ── Collab File Auto-Rotation ──────────────────────────────────────────────────

def _collab_file_path(project, rel_path):
    """Get absolute path for a collab file."""
    return os.path.join(project_collab_dir(project), rel_path)


def _file_needs_rotation(filepath, max_lines=None, max_kb=None):
    """Check if a file exceeds rotation thresholds."""
    if not os.path.exists(filepath):
        return False, 0, 0
    max_lines = max_lines or COLLAB_FILE_MAX_LINES
    max_kb = max_kb or COLLAB_FILE_MAX_KB
    size = os.path.getsize(filepath)
    with open(filepath, encoding="utf-8") as f:
        line_count = len(f.readlines())
    return (line_count > max_lines or size > max_kb * 1024), line_count, size


def _archive_collab_file(project, rel_path, topic=None, quiet=False):
    """Archive a collab file: keep recent content, move old content to archive.
    Returns True if archived, False otherwise."""
    filepath = _collab_file_path(project, rel_path)
    if not os.path.exists(filepath):
        return False

    if rel_path == "AGENT_COMMS.md":
        needs, stats = rot.comms_needs_block_rotation(
            filepath, COLLAB_FILE_MAX_LINES, COLLAB_FILE_MAX_KB, rot.COMMS_KEEP_RECENT_BLOCKS
        )
        if not needs:
            return False
        keep_blocks = rot.COMMS_KEEP_RECENT_BLOCKS
        if stats["lines"] > COLLAB_FILE_MAX_LINES or stats["bytes"] > COLLAB_FILE_MAX_KB * 1024:
            with open(filepath, encoding="utf-8") as f:
                preamble, blocks = rot.split_message_blocks(f.read())
            for candidate in range(min(keep_blocks, len(blocks) - 1), 0, -1):
                kept = preamble + "\n\n" + "\n\n".join(blocks[-candidate:])
                if len(kept.splitlines()) <= COLLAB_FILE_MAX_LINES and len(kept.encode("utf-8")) <= COLLAB_FILE_MAX_KB * 1024:
                    keep_blocks = candidate
                    break
            else:
                keep_blocks = 1
        archive_dir = os.path.join(project_memory_dir(project), "sessions")
        archived, archive_path = rot.archive_comms_by_blocks(
            project,
            filepath,
            archive_dir,
            keep_blocks=keep_blocks,
            topic=topic or "AGENT_COMMS",
        )
        if archived and not quiet:
            print(
                f"🔄 Archived: {rel_path} ({stats['lines']}L, {stats['blocks']} blocks "
                f"→ keep {keep_blocks})"
            )
        return archived

    needs, lines, size = _file_needs_rotation(filepath)
    if not needs:
        return False

    with open(filepath, encoding="utf-8") as f:
        all_lines = f.readlines()

    # Split: old content (to archive) + recent content (to keep)
    keep_n = min(COLLAB_KEEP_RECENT_LINES, len(all_lines))
    old_lines = all_lines[:-keep_n]
    recent_lines = all_lines[-keep_n:]

    if not old_lines:
        return False  # Nothing to archive

    # Create archive file
    archive_dir = os.path.join(project_memory_dir(project), "sessions")
    os.makedirs(archive_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_name = rel_path.replace("/", "_").replace(".md", "")
    safe_topic = (topic or safe_name).replace(" ", "_")[:40]
    archive_name = f"{timestamp}_{safe_topic}.md"
    archive_path = os.path.join(archive_dir, archive_name)

    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(f"# Archived: {rel_path}\n")
        f.write(f"**Project:** {project}\n")
        f.write(f"**Archived:** {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"**Trigger:** auto-rotation ({lines} lines, {size / 1024:.1f}KB)\n")
        f.write(f"**Lines archived:** {len(old_lines)}\n\n")
        f.write("## Archived Content\n\n")
        f.writelines(old_lines)

    # Rewrite original with only recent content
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"<!-- Previous content archived to: {archive_path} -->\n")
        f.writelines(recent_lines)

    if not quiet:
        print(f"🔄 Archived: {rel_path} ({lines}L → keep {keep_n}L, archived {len(old_lines)}L)")
    return True


def auto_rotate_collab_files(project):
    """Check and rotate all tracked collab files. Returns list of rotated file names."""
    rotated = []
    for rel_path in TRACKED_COLLAB_FILES:
        if _archive_collab_file(project, rel_path, topic=rel_path.replace("/", "_")):
            rotated.append(rel_path)
    return rotated


def list_collab_projects():
    """Return sorted project names that have collab/ under projects_dir."""
    projects_dir = get_projects_dir()
    if not os.path.isdir(projects_dir):
        return []
    return sorted(
        name
        for name in os.listdir(projects_dir)
        if name not in ("by_rm", "archive")
        and os.path.isdir(os.path.join(projects_dir, name, "collab"))
    )


def build_rotation_health(project):
    """Scan rotation thresholds without loading full file bodies into prompts."""
    agent_rows = []
    for agent in get_project_agents(project):
        needs, lines, size = _needs_rotation(project, agent)
        agent_rows.append(
            {
                "agent": agent,
                "lines": lines,
                "bytes": size,
                "needs_rotation": needs,
            }
        )

    collab_rows = []
    for rel_path in TRACKED_COLLAB_FILES:
        fpath = _collab_file_path(project, rel_path)
        if rel_path == "AGENT_COMMS.md":
            needs, stats = rot.comms_needs_block_rotation(
                fpath, COLLAB_FILE_MAX_LINES, COLLAB_FILE_MAX_KB, rot.COMMS_KEEP_RECENT_BLOCKS
            )
            collab_rows.append(
                {
                    "file": rel_path,
                    "lines": stats.get("lines", 0),
                    "bytes": stats.get("bytes", 0),
                    "blocks": stats.get("blocks", 0),
                    "needs_rotation": needs,
                }
            )
        else:
            needs, lines, size = _file_needs_rotation(fpath)
            collab_rows.append(
                {
                    "file": rel_path,
                    "lines": lines,
                    "bytes": size,
                    "blocks": None,
                    "needs_rotation": needs,
                }
            )

    budget = build_memory_budget(project)
    needs_enforcement = any(r["needs_rotation"] for r in agent_rows + collab_rows)
    if not budget["hot"]["ok"] or not budget["warm"]["ok"]:
        needs_enforcement = True

    return {
        "project": project,
        "agents": agent_rows,
        "collab_files": collab_rows,
        "budget": {
            "hot_ok": budget["hot"]["ok"],
            "warm_ok": budget["warm"]["ok"],
            "hot_tokens": budget["hot"]["tokens"],
            "warm_tokens": budget["warm"]["tokens"],
        },
        "needs_enforcement": needs_enforcement,
    }


def enforce_project_rotation(project, budget_aware=True):
    """Rotate all overdue agent sessions and collab files. Returns summary dict."""
    summary = {"project": project, "agents": [], "files": [], "passes": 0}

    def _run_pass():
        rotated_agents = []
        for agent in get_project_agents(project):
            if check_and_rotate_agent(project, agent, topic="enforce_rotation"):
                rotated_agents.append(agent)
        rotated_files = auto_rotate_collab_files(project)
        return rotated_agents, rotated_files

    for _ in range(2):
        agents, files = _run_pass()
        if agents or files:
            summary["passes"] += 1
            summary["agents"].extend(agents)
            summary["files"].extend(files)
        else:
            break

    if budget_aware:
        budget = build_memory_budget(project)
        if not budget["warm"]["ok"]:
            agents, files = _run_pass()
            if agents or files:
                summary["passes"] += 1
                summary["agents"].extend(agents)
                summary["files"].extend(files)

    summary["health"] = build_rotation_health(project)
    summary["ok"] = not summary["health"]["needs_enforcement"]
    return summary


def format_rotation_health(report):
    lines = [
        f"# Rotation Health — {report['project']}",
        "",
        f"needs_enforcement: {'YES' if report['needs_enforcement'] else 'NO'}",
        f"budget: hot {'OK' if report['budget']['hot_ok'] else 'OVER'} "
        f"({report['budget']['hot_tokens']} tok), "
        f"warm {'OK' if report['budget']['warm_ok'] else 'OVER'} "
        f"({report['budget']['warm_tokens']} tok)",
        "",
        "## Agent Sessions",
    ]
    for row in report["agents"]:
        flag = "ROTATE" if row["needs_rotation"] else "ok"
        lines.append(
            f"- {row['agent']}: {row['lines']}L, {row['bytes'] / 1024:.1f}KB — {flag}"
        )
    lines.append("")
    lines.append("## Collab Files")
    for row in report["collab_files"]:
        flag = "ROTATE" if row["needs_rotation"] else "ok"
        extra = f", {row['blocks']} blocks" if row.get("blocks") is not None else ""
        lines.append(
            f"- {row['file']}: {row['lines']}L{extra}, {row['bytes'] / 1024:.1f}KB — {flag}"
        )
    lines += [
        "",
        "Enforce:",
        f"- plow-whip --project {report['project']} memory-rotate",
        f"- plow-whip --project {report['project']} memory-budget --enforce-rotate",
        f"- plow-whip scheduler install --interval 60",
    ]
    return "\n".join(lines) + "\n"


def scan_md_activity(project):
    """Scan all .md files in collab/, analyze last 10 lines for agent activity.
    Returns list of dicts with file info and activity detection."""
    collab_dir = project_collab_dir(project)
    if not os.path.isdir(collab_dir):
        return []

    results = []
    for root, dirs, files in os.walk(collab_dir):
        # Skip conversations/ (handled separately) and archive dirs
        dirs[:] = [d for d in dirs if d not in ("conversations", "sessions", "archive", "sprints", "adr")]
        for fname in files:
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, collab_dir)

            with open(fpath, encoding="utf-8") as f:
                all_lines = f.readlines()

            total_lines = len(all_lines)
            size = os.path.getsize(fpath)
            last_10 = all_lines[-10:] if len(all_lines) >= 10 else all_lines
            last_10_text = "".join(last_10).lower()

            # Detect agent activity in last 10 lines
            active_agents = set()
            for pattern in AGENT_PATTERNS:
                if pattern.lower() in last_10_text:
                    active_agents.add(pattern)

            needs, _, _ = _file_needs_rotation(fpath)
            is_tracked = rel in TRACKED_COLLAB_FILES

            results.append({
                "file": rel,
                "lines": total_lines,
                "size_kb": round(size / 1024, 1),
                "needs_rotation": needs,
                "is_tracked": is_tracked,
                "active_agents": sorted(active_agents),
                "is_active": len(active_agents) > 0,
            })

    # Sort: active + untracked first (candidates for tracking), then by size desc
    results.sort(key=lambda r: (not r["is_active"] or r["is_tracked"], -r["lines"]))
    return results




def _show_tail_and_judge(project, rel_path, info):
    """Show last 20 lines of an untracked file and judge if it should be tracked."""
    collab_dir = project_collab_dir(project)
    fpath = os.path.join(collab_dir, rel_path)
    if not os.path.exists(fpath):
        return

    with open(fpath, encoding="utf-8") as f:
        all_lines = f.readlines()

    tail_n = min(20, len(all_lines))
    tail_lines = all_lines[-tail_n:]

    print(f"     ── 最后 {tail_n} 行 ──")
    for line in tail_lines:
        stripped = line.rstrip()
        if stripped:
            print(f"       | {stripped[:80]}")

    # Judgment: is this a living document or a reference doc?
    tail_text = "".join(tail_lines).lower()
    
    # Signs of a living document (frequently updated, should be tracked)
    living_signs = ["handoff", "update", "progress", "status", "todo", "next",
                    "done", "block", "sprint", "day ", "更新", "进度", "交接"]
    living_hits = sum(1 for s in living_signs if s in tail_text)

    # Signs of a reference doc (stable, should NOT be tracked)
    ref_signs = ["convention", "rule", "policy", "约定", "规范", "规则",
                 "template", "模板", "guide", "指南", "reference", "参考"]
    ref_hits = sum(1 for s in ref_signs if s in tail_text)

    if ref_hits > living_hits:
        print(f"     📖 判断: 参考文档（不宜轮转）")
    elif living_hits >= 2:
        print(f"     📝 判断: 活跃协作文档 → 建议加入 TRACKED_COLLAB_FILES")
    else:
        print(f"     ❓ 判断: 不确定，需人工确认")


def cmd_memory_rotate(project, args):
    """memory-rotate subcommand: check and rotate all collab/memory files."""
    scan_only = getattr(args, "scan", False)

    if scan_only:
        # Just scan and report
        print(f"\n🔍 Scanning .md activity (project: {project}):\n")
        results = scan_md_activity(project)
        if not results:
            print("  No .md files found.")
            return

        print(f"  {'File':35s} {'Lines':>6s} {'Size':>7s} {'Rotate':>7s} {'Tracked':>8s} {'Active Agents'}")
        print(f"  {'─' * 35} {'─' * 6} {'─' * 7} {'─' * 7} {'─' * 8} {'─' * 20}")
        for r in results:
            rot = "🔴 YES" if r["needs_rotation"] else "🟢 ok"
            trk = "✅" if r["is_tracked"] else "❌"
            agents = ", ".join(r["active_agents"][:3]) if r["active_agents"] else "—"
            print(f"  {r['file']:35s} {r['lines']:>6d} {r['size_kb']:>6.1f}KB {rot:>7s} {trk:>8s} {agents}")

        # Highlight untracked but active files with tail + judgment
        untracked_active = [r for r in results if not r["is_tracked"] and r["is_active"]]
        if untracked_active:
            print(f"\n  ⚠️  {len(untracked_active)} untracked but active .md file(s):")
            for r in untracked_active:
                print(f"\n     → {r['file']} ({r['lines']}L, {r['size_kb']}KB, agents: {', '.join(r['active_agents'][:3])})")
                _show_tail_and_judge(project, r["file"], r)
        print()
        return

    # Full rotation mode
    print(f"\n🔄 Memory rotation (project: {project}):\n")

    # 1. Rotate agent sessions
    print("  ── Agent Sessions ──")
    for agent in get_project_agents(project):
        needs, lines, size = _needs_rotation(project, agent)
        status = "🔴" if needs else "🟢"
        print(f"  {status} {agent:12s} — {lines} lines, {size / 1024:.1f}KB")
    rotated_agents = []
    for agent in get_project_agents(project):
        if check_and_rotate_agent(project, agent, topic="memory_rotate"):
            rotated_agents.append(agent)
    if rotated_agents:
        print(f"  ✅ Rotated: {', '.join(rotated_agents)}")
    else:
        print(f"  ✅ No agent sessions need rotation")

    # 2. Rotate collab files
    print(f"\n  ── Collab/Memory Files ──")
    for rel_path in TRACKED_COLLAB_FILES:
        fpath = _collab_file_path(project, rel_path)
        needs, lines, size = _file_needs_rotation(fpath)
        status = "🔴" if needs else "🟢"
        print(f"  {status} {rel_path:30s} — {lines} lines, {size / 1024:.1f}KB")
    rotated_files = auto_rotate_collab_files(project)
    if rotated_files:
        print(f"  ✅ Rotated: {', '.join(rotated_files)}")
    else:
        print(f"  ✅ No collab files need rotation")

    # 3. Scan for new active files
    print(f"\n  ── Activity Scan ──")
    results = scan_md_activity(project)
    untracked_active = [r for r in results if not r["is_tracked"] and r["is_active"]]
    if untracked_active:
        print(f"  ⚠️  {len(untracked_active)} active but untracked file(s):")
        for r in untracked_active:
            print(f"\n     → {r['file']} ({r['lines']}L, {r['size_kb']}KB, agents: {', '.join(r['active_agents'][:3])})")
            # Show last 20 lines for decision
            _show_tail_and_judge(project, r["file"], r)
    else:
        print(f"  ✅ All active .md files are tracked")
    print()


def _memory_file_stats(project, rel_path):
    path = os.path.join(project_collab_dir(project), rel_path)
    if not os.path.exists(path):
        return {"file": rel_path, "exists": False, "bytes": 0, "tokens": 0, "lines": 0}
    size = os.path.getsize(path)
    return {
        "file": rel_path,
        "exists": True,
        "bytes": size,
        "tokens": max(1, (size + 3) // 4),
        "lines": None,
    }


def _cold_memory_stats(project):
    roots = [
        os.path.join(project_memory_dir(project), "sessions"),
        os.path.join(conversations_dir(project)),
    ]
    total_bytes = 0
    files = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        for base, _dirs, names in os.walk(root):
            for name in names:
                if name == "current.md":
                    continue
                path = os.path.join(base, name)
                if os.path.isfile(path):
                    files += 1
                    total_bytes += os.path.getsize(path)
    return {"files": files, "bytes": total_bytes, "tokens_estimate": max(0, (total_bytes + 3) // 4)}


def build_memory_budget(project):
    state = load_state(project)
    payload = json.dumps(build_start_pack(project, state.get("current_agent")), ensure_ascii=False, separators=(",", ":"))
    hot_files = [{"file": "start --json payload", "exists": True, "bytes": len(payload.encode()), "tokens": (len(payload) + 3) // 4, "lines": None}]
    warm_files = [_memory_file_stats(project, rel) for rel in WARM_MEMORY_FILES]
    hot_tokens = sum(item["tokens"] for item in hot_files)
    warm_tokens = sum(item["tokens"] for item in warm_files)
    return {
        "project": project,
        "budgets": {"hot": HOT_TOKEN_BUDGET, "warm": WARM_TOKEN_BUDGET},
        "hot": {"tokens": hot_tokens, "ok": hot_tokens <= HOT_TOKEN_BUDGET, "files": hot_files},
        "warm": {"tokens": warm_tokens, "ok": warm_tokens <= WARM_TOKEN_BUDGET, "files": warm_files},
        "machine": {"files": [_memory_file_stats(project, rel) for rel in ["AGENT_STATE.json", *MACHINE_RULE_FILES]]},
        "cold": _cold_memory_stats(project),
    }


def format_memory_budget(report):
    lines = [
        f"# Memory Budget — {report['project']}",
        "",
        f"Hot: {report['hot']['tokens']} / {report['budgets']['hot']} tokens {'OK' if report['hot']['ok'] else 'OVER'}",
    ]
    for item in report["hot"]["files"]:
        status = "missing" if not item["exists"] else f"{item['tokens']} tok, {item['bytes']} bytes"
        lines.append(f"- {item['file']}: {status}")

    lines += [
        "",
        f"Warm: {report['warm']['tokens']} / {report['budgets']['warm']} tokens {'OK' if report['warm']['ok'] else 'OVER'}",
    ]
    for item in report["warm"]["files"]:
        status = "missing" if not item["exists"] else f"{item['tokens']} tok, {item['bytes']} bytes"
        lines.append(f"- {item['file']}: {status}")

    cold = report["cold"]
    lines += [
        "",
        "Machine truth (compiled before model context):",
        *[f"- {item['file']}: {item['tokens']} tok, {item['bytes']} bytes" for item in report["machine"]["files"]],
        "",
        "Cold:",
        f"- archived files: {cold['files']}",
        f"- stored size: {cold['bytes'] / 1024:.1f}KB",
        "",
        "Rule:",
        "- Hot should stay tiny enough to read every wakeup.",
        "- Warm contains only targeted messages included by start.",
        "- Cold is searched/restored, not read wholesale.",
    ]

    over = []
    if not report["hot"]["ok"]:
        over.append("Hot exceeds budget: keep AGENT_STATE.task single-purpose and compact protocol summaries.")
    if not report["warm"]["ok"]:
        over.append("Warm exceeds budget: rotate handled AGENT_COMMS blocks.")
    if over:
        lines += ["", "Actions"] + [f"- {item}" for item in over]
    return "\n".join(lines) + "\n"


def cmd_rotation_health(project, args):
    health = build_rotation_health(project)
    if getattr(args, "enforce", False):
        summary = enforce_project_rotation(project)
        health = summary["health"]
        if not getattr(args, "json", False):
            if summary["agents"] or summary["files"]:
                print(
                    f"✂️  Rotation enforced: agents={summary['agents'] or '—'}, "
                    f"files={summary['files'] or '—'}"
                )
            else:
                print("✅ No rotation needed.")
    if getattr(args, "json", False):
        print(json.dumps(health, ensure_ascii=False, indent=2))
    else:
        print(format_rotation_health(health), end="")


def cmd_memory_budget(project, args):
    if getattr(args, "enforce_rotate", False):
        summary = enforce_project_rotation(project)
        if not getattr(args, "json", False):
            if summary["agents"] or summary["files"]:
                print(
                    f"✂️  Rotation enforced: agents={summary['agents'] or '—'}, "
                    f"files={summary['files'] or '—'}"
                )
            else:
                print("✅ No rotation needed.")
    report = build_memory_budget(project)
    if getattr(args, "json", False):
        payload = report
        if getattr(args, "enforce_rotate", False):
            payload = {"budget": report, "rotation": build_rotation_health(project)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_memory_budget(report), end="")
        if not report["hot"]["ok"] or not report["warm"]["ok"]:
            print("\nTip: run memory-budget --enforce-rotate to archive overdue files.\n")


def cmd_handoff(project, args):
    state = load_state(project)
    ensure_project_path(project, state)
    current = state["current_agent"]
    agents = proto.schedulable_agents(load_protocol(project))
    idx = agents.index(current) if current in agents else 0
    requested_agent = getattr(args, "to", None)
    if requested_agent and requested_agent not in agents:
        print(f"Error: unknown agent '{requested_agent}'. Run: plow-whip agent set {requested_agent}", file=sys.stderr)
        sys.exit(1)
    next_agent = requested_agent or agents[(idx + 1) % len(agents)]

    state["current_agent"] = next_agent
    state["assigned_agent"] = next_agent
    if args.phase:
        state["phase"] = args.phase
    state["status"] = args.status
    state["last_output"] = args.output
    state["next_action"] = args.next or ""
    state["blockers"] = getattr(args, "blockers", None) or []
    next_status = "blocked" if args.status == "blocked" else ("active" if (args.next or "").strip() else "done")
    state["task"] = {
        "id": state.get("task", {}).get("id", "T-001"),
        "title": state.get("task", {}).get("title") or state.get("next_action") or "Current task",
        "owner": next_agent,
        "status": next_status,
        "next_action": args.next or "",
        "last_output": args.output,
        "blockers": state["blockers"],
        "decision_ids": state.get("task", {}).get("decision_ids", []),
    }
    if args.day is not None:
        state.setdefault("task_context", {})["day"] = args.day
    if args.topic:
        state.setdefault("task_context", {})["topic"] = args.topic
    if args.project_dir:
        state.setdefault("task_context", {})["project_dir"] = args.project_dir
    if args.files:
        state["files_changed"] = args.files
    if args.verify:
        state["verify_commands"] = args.verify

    save_state(project, state)
    print(f"\n✅ Handoff: {current} → {next_agent} (project: {project})")
    print(f"📋 Phase: {state['phase']}")
    print(f"📌 Output: {args.output}")
    print(f"➡️  {next_agent} please: {state['next_action'] or '(check status)'}")
    print()
    notify(f"[{project}] {current} → {next_agent}", ring=True)

    # ── Auto-rotate: check the agent that just finished ──
    handoff_topic = args.topic or state.get("phase", "handoff")
    rotated = check_and_rotate_agent(project, current, topic=f"handoff_{handoff_topic}")
    if rotated:
        print(f"   ✂️  {current}'s session auto-rotated (exceeded threshold)")
    else:
        needs, lines, size = _needs_rotation(project, current)
        print(f"   📝 {current} session: {lines} lines, {size / 1024:.1f}KB — no rotation needed")

    rotated_files = auto_rotate_collab_files(project)
    if rotated_files:
        print(f"   ✂️  Collab rotated: {', '.join(rotated_files)}")


def cmd_reset(project):
    save_state(project, default_state(project))
    print(f"\n🔄 Project '{project}' state reset.\n")


def cmd_init(project, args=None):
    """Initialize a new project with full collab/ structure."""
    pcd = project_collab_dir(project)
    sf = state_file(project)
    if os.path.exists(sf):
        print(f"Project '{project}' already exists: {sf}")
        return

    # Create collab/ directory structure
    os.makedirs(pcd, exist_ok=True)
    os.makedirs(os.path.join(pcd, "conversations"), exist_ok=True)
    for agent in get_agents():
        agent_dir = os.path.join(pcd, "conversations", agent)
        os.makedirs(agent_dir, exist_ok=True)
        _write_session_template(agent, project, os.path.join(agent_dir, "current.md"))

    # Create memory/ directory
    mem_dir = project_memory_dir(project)
    os.makedirs(mem_dir, exist_ok=True)
    os.makedirs(os.path.join(mem_dir, "sessions"), exist_ok=True)

    # Render templates
    for tpl_name, out_name in MEMORY_TEMPLATE_MAP:
        write_rendered(os.path.join(mem_dir, out_name), f"memory/{tpl_name}", project)

    # Canonical machine protocol + derived human handbook.
    data = proto.ensure(project_dir(project), project, get_agents(), get_agent_meta())
    leases.pin_protocol(CONFIG_DIR, project, data)
    proto.write_handbook(project_dir(project))
    _write_compat_conventions(project)
    write_agent_manifest(project)

    # Create AGENT_COMMS.md
    write_rendered(comms_file(project), "AGENT_COMMS.md.tpl", project)

    # Create state file
    save_state(project, default_state(project))
    append_comms(project, f"项目初始化：当前路径为 `{project_dir(project)}`。")

    # A real user installation is unattended by default. Test/embedded config
    # directories do not mutate the host scheduler.
    default_config_dir = os.path.join(os.path.expanduser("~"), ".plow-whip")
    configured_dir = os.environ.get("PLOW_WHIP_CONFIG_DIR")
    if os.path.abspath(CONFIG_DIR) in {
        os.path.abspath(default_config_dir),
        os.path.abspath(configured_dir) if configured_dir else "",
    }:
        try:
            from . import scheduler

            if not scheduler.status(CONFIG_DIR).get("installed"):
                scheduler.install(CONFIG_DIR, interval=60, auto_crack=True)
        except (OSError, ValueError):
            # State remains automation-enabled; scheduler doctor/repair reports
            # a host-specific installation failure without corrupting init.
            pass

    print(f"\n🆕 Project '{project}' initialized!")
    print(f"   collab/: {pcd}")
    print(f"   memory/: {mem_dir}")
    print(f"   state:   {sf}")
    print(f"   Run: plow-whip --project {project} start --agent {default_state(project)['current_agent']} --json\n")


def cmd_new(project, args):
    """Create a new project directory and initialize plow-whip collaboration files."""
    pdir = project_dir(project)
    os.makedirs(pdir, exist_ok=True)
    cmd_init(project, args)

    first_action = (getattr(args, "first_action", None) or "").strip()
    owner = (getattr(args, "owner", None) or "").strip()
    if first_action or owner:
        state = load_state(project)
        task = state.setdefault("task", {})
        if owner:
            if owner not in proto.schedulable_agents(load_protocol(project)):
                print(f"Error: owner '{owner}' is unknown or non-schedulable; use submit for control-plane intake", file=sys.stderr)
                sys.exit(1)
            task["owner"] = owner
        if first_action:
            task["title"] = first_action
            task["next_action"] = first_action
            task["status"] = "active"
            task["placeholder"] = False
        save_state(project, state)
        append_comms(project, f"新项目一键接入：owner=`{state['current_agent']}`；next=`{state['next_action']}`。")

    print(f"   Next lightweight context:")
    print(f"   plow-whip --project {project} start --agent {load_state(project)['current_agent']} --json\n")


def _ensure_plow_whip_structure(project):
    """Create any missing collaboration files without overwriting existing work."""
    pcd = project_collab_dir(project)
    os.makedirs(pcd, exist_ok=True)
    os.makedirs(conversations_dir(project), exist_ok=True)
    seed_agents = get_agents()
    seed_meta = get_agent_meta()
    if os.path.exists(state_file(project)) and not os.path.exists(protocol_file(project)):
        try:
            with open(state_file(project), encoding="utf-8") as f:
                legacy_state = json.load(f)
            seed_agents = legacy_state.get("agents") or seed_agents
            seed_meta = legacy_state.get("agent_meta") or seed_meta
        except (OSError, json.JSONDecodeError, RuntimeError):
            pass
    protocol_existed = os.path.exists(protocol_file(project))
    if protocol_existed:
        load_protocol(project)
    data = proto.ensure(project_dir(project), project, seed_agents, seed_meta)
    leases.pin_protocol(CONFIG_DIR, project, data)
    proto.write_handbook(project_dir(project))
    for agent in get_project_agents(project):
        agent_dir = os.path.join(conversations_dir(project), agent)
        os.makedirs(agent_dir, exist_ok=True)
        current = os.path.join(agent_dir, "current.md")
        if not os.path.exists(current):
            _write_session_template(agent, project, current)

    mem_dir = project_memory_dir(project)
    os.makedirs(mem_dir, exist_ok=True)
    os.makedirs(os.path.join(mem_dir, "sessions"), exist_ok=True)

    for tpl_name, out_name in MEMORY_TEMPLATE_MAP:
        target = os.path.join(mem_dir, out_name)
        if not os.path.exists(target):
            write_rendered(target, f"memory/{tpl_name}", project)

    _write_compat_conventions(project)
    write_agent_manifest(project)
    if not os.path.exists(comms_file(project)):
        write_rendered(comms_file(project), "AGENT_COMMS.md.tpl", project)
    if not os.path.exists(state_file(project)):
        save_state(project, default_state(project))
        append_comms(project, f"plow-whip repair：机制缺失，已自动接入；当前路径 `{project_dir(project)}`。")
    else:
        try:
            write_state(project, load_state(project), touch=False)
        except (OSError, json.JSONDecodeError):
            pass


def _state_protocol_issues(project):
    issues = []
    try:
        data = load_protocol(project)
        agents = proto.enabled_agents(data)
        proto.effective_rules(data)
        issues.extend(proto.semantic_issues(data))
    except (OSError, json.JSONDecodeError, ValueError, leases.ProtocolIntegrityError) as exc:
        return [f"invalid AGENT_PROTOCOL.json: {exc}"]
    try:
        with open(handbook_file(project), encoding="utf-8") as f:
            handbook = f.read()
        if handbook != proto.render_handbook(data):
            issues.append("derived handbook drifted from AGENT_PROTOCOL.json")
    except OSError:
        issues.append("derived handbook is missing")
    try:
        with open(state_file(project), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid AGENT_STATE.json: {exc}"]
    try:
        leases.verify_state(CONFIG_DIR, project, raw, data)
    except leases.StateIntegrityError as exc:
        return [str(exc)]
    task = raw.get("task")
    if not isinstance(task, dict):
        return ["AGENT_STATE.json has no canonical task object"]
    owner = task.get("owner")
    if owner not in agents:
        issues.append(f"task owner '{owner}' is not an enabled project agent")
    expected = {
        "current_agent": owner,
        "assigned_agent": owner,
        "status": {"active": "in_progress"}.get(task.get("status"), task.get("status", "in_progress")),
        "next_action": task.get("next_action", ""),
        "last_output": task.get("last_output", ""),
        "blockers": task.get("blockers", []),
    }
    drift = [key for key, value in expected.items() if raw.get(key) != value]
    if drift:
        issues.append("derived state fields drifted: " + ", ".join(drift))
    return issues


REPAIRABLE_ISSUE_PREFIXES = (
    "protocol schema drifted",
    "inherited global rules drifted",
    "project rules need normalization",
    "protocol defaults missing",
    "derived handbook drifted",
    "derived handbook is missing",
    "derived state fields drifted",
)


def _issues_are_repairable(issues):
    return bool(issues) and all(issue.startswith(REPAIRABLE_ISSUE_PREFIXES) for issue in issues)


def build_doctor_report(project):
    """Validate project structure plus canonical/derived semantic consistency."""
    required = [
        ("collab/", project_collab_dir(project)),
        ("collab/AGENT_STATE.json", state_file(project)),
        ("collab/AGENT_COMMS.md", comms_file(project)),
        ("collab/AGENT_PROTOCOL.json", protocol_file(project)),
        ("collab/memory/DECISIONS.md", os.path.join(project_memory_dir(project), "DECISIONS.md")),
        ("collab/memory/sessions/", os.path.join(project_memory_dir(project), "sessions")),
    ]
    optional = [
        ("collab/AGENTS.md", os.path.join(project_collab_dir(project), "AGENTS.md")),
        ("collab/HANDBOOK.zh-CN.md", handbook_file(project)),
        ("collab/CONVENTIONS.agent.md", conventions_agent_file(project)),
        ("collab/CONVENTIONS.md", conventions_human_file(project)),
    ]
    try:
        doctor_agents = get_project_agents(project)
    except leases.ProtocolIntegrityError:
        doctor_agents = get_agents()
    for agent in doctor_agents:
        required.append((f"collab/conversations/{agent}/current.md", os.path.join(conversations_dir(project), agent, "current.md")))
    checks = [{"name": name, "path": path, "ok": os.path.exists(path)} for name, path in required]
    optional_checks = [{"name": name, "path": path, "ok": os.path.exists(path)} for name, path in optional]
    missing = [item for item in checks if not item["ok"]]
    issues = _state_protocol_issues(project) if not missing else []
    repairable = bool(missing) or _issues_are_repairable(issues)
    return {
        "project": project,
        "project_path": project_dir(project),
        "ok": not missing and not issues,
        "checks": checks,
        "optional_checks": optional_checks,
        "missing": missing,
        "issues": issues,
        "next": (
            f"Run plow-whip --project {project} start --agent <agent> --json."
            if not missing and not issues
            else f"Run plow-whip --project {project} repair, then doctor again."
            if repairable
            else "Restore or fix the canonical JSON file reported above; repair will not overwrite it."
        ),
    }


def format_doctor_report(report):
    lines = [
        f"# plow-whip Doctor — {report['project']}",
        "",
        f"project_path: {report['project_path']}",
        f"status: {'OK' if report['ok'] else 'MISSING'}",
        "",
        "## Checks",
    ]
    for item in report["checks"]:
        lines.append(f"- {'OK' if item['ok'] else 'MISS'} {item['name']}")
    if report.get("optional_checks"):
        lines += ["", "## Derived / Compatibility Views"]
        for item in report["optional_checks"]:
            lines.append(f"- {'OK' if item['ok'] else 'REBUILD'} {item['name']}")
    if report.get("issues"):
        lines += ["", "## Issues"] + [f"- {issue}" for issue in report["issues"]]
    lines += ["", "## Next", report["next"]]
    return "\n".join(lines) + "\n"


def cmd_doctor(project, args):
    if getattr(args, "repair", False):
        try:
            _ensure_plow_whip_structure(project)
            if not getattr(args, "skip_rotate", False):
                enforce_project_rotation(project)
        except (OSError, json.JSONDecodeError, ValueError, RuntimeError):
            pass
    report = build_doctor_report(project)
    health = build_rotation_health(project) if report["ok"] else None
    report["rotation"] = health
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_doctor_report(report), end="")
        if health and health["needs_enforcement"]:
            print(format_rotation_health(health), end="")


def cmd_repair(project, args):
    error = None
    try:
        _ensure_plow_whip_structure(project)
    except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        error = str(exc)
    report = build_doctor_report(project)
    payload = {"project": project, "repaired": report["ok"], "error": error, "doctor": report}
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Repaired plow-whip structure for {project}.")


def _read_text(path, default=""):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


def _clamp_text(text, max_chars):
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n... [truncated]"


START_PACK_MAX_CHARS = 16000


def _bounded_start_value(value, depth=0):
    """Bound arbitrary task metadata while keeping the startup schema intact."""
    if isinstance(value, str):
        return _clamp_text(value, 300)
    if isinstance(value, list):
        return [_bounded_start_value(item, depth + 1) for item in value[:10]]
    if isinstance(value, dict):
        if depth >= 3:
            return {"summary": "nested startup metadata omitted"}
        return {
            str(key): _bounded_start_value(item, depth + 1)
            for key, item in list(value.items())[:30]
        }
    return value


def _bounded_start_task(raw_task):
    task = _bounded_start_value(dict(raw_task or {}))
    for field, limit in (("title", 500), ("goal", 1200), ("next_action", 2000), ("last_output", 1000)):
        if field in raw_task:
            raw_value = (raw_task.get(field) or "").strip()
            if field == "last_output" and len(raw_value) > limit:
                task[field] = "... [truncated]\n" + raw_value[-limit:]
            else:
                task[field] = _clamp_text(raw_value, limit)
            if task[field] != (raw_task.get(field) or "").strip():
                task[f"{field}_truncated"] = True
    task.pop("verification", None)
    return task


def _latest_targeted_blocks(markdown, agent, limit=3):
    """Return recent AGENT_COMMS blocks aimed at or written by agent."""
    blocks = re.split(r"(?=^### \[)", markdown, flags=re.MULTILINE)
    wanted = []
    agent_tag = f"@{agent}"
    agent_heading = f"### [{agent}]"
    for block in blocks:
        if not block.strip().startswith("### ["):
            continue
        if agent_tag in block or block.startswith(agent_heading):
            wanted.append(block.strip())
    if not wanted:
        wanted = [line.strip() for line in markdown.splitlines() if agent_tag in line]
    if not wanted:
        # Fall back to the latest real message blocks, not the whole board.
        wanted = [b.strip() for b in blocks if b.strip().startswith("### [")]
    return wanted[-limit:]


def cmd_context_pack(project, args):
    pack = build_start_pack(project, getattr(args, "agent", None))
    if getattr(args, "json", False):
        print(json.dumps(pack, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(pack, ensure_ascii=False, indent=2))


def build_start_pack(project, agent=None):
    """Return the complete, bounded machine startup payload."""
    report = build_doctor_report(project)
    if not report["ok"]:
        repairable = bool(report["missing"]) or _issues_are_repairable(report.get("issues", []))
        return {
            "ready": False,
            "project": project,
            "missing": [item["name"] for item in report["missing"]],
            "issues": report.get("issues", []),
            "action_required": "repair" if repairable else "fix_canonical_json",
            "command": f"plow-whip --project {project} repair" if repairable else None,
        }
    data = load_protocol(project)
    agents = proto.enabled_agents(data)
    state = load_state(project)
    agent = agent or state.get("current_agent")
    if agent not in agents:
        return {
            "ready": False,
            "project": project,
            "agent": agent,
            "action_required": "select_enabled_agent",
            "enabled_agents": agents,
        }
    raw_task = dict(state.get("task", {}))
    authorization = {"mode": "worker", "execution_allowed": True, "reason": "legacy_project"}
    if leases.is_strict(data):
        try:
            lease = leases.validate(CONFIG_DIR, project, state, data, agent=agent)
            authorization = {
                "mode": "worker", "execution_allowed": True,
                "lease_id": (raw_task.get("execution") or {}).get("lease", {}).get("id"),
                "dispatch_id": lease.get("dispatch_id"),
            }
        except leases.LeaseDenied as exc:
            authorization = {"mode": "observer", "execution_allowed": False, "reason": str(exc)}
    task = _bounded_start_task(raw_task)
    task["owner"] = state.get("assigned_agent") or task.get("owner")
    raw_next = state.get("next_action", raw_task.get("next_action", ""))
    task["next_action"] = _clamp_text(raw_next, 2000)
    if task["next_action"] != (raw_next or "").strip():
        task["next_action_truncated"] = True
    raw_output = (state.get("last_output", raw_task.get("last_output", "")) or "").strip()
    task["last_output"] = (
        "... [truncated]\n" + raw_output[-1000:]
        if len(raw_output) > 1000 else raw_output
    )
    if task["last_output"] != raw_output:
        task["last_output_truncated"] = True
    task["blockers"] = _bounded_start_value(state.get("blockers", raw_task.get("blockers", [])))
    messages = [
        _clamp_text(message, 1000)
        for message in _latest_targeted_blocks(_read_text(comms_file(project)), agent, limit=6)
        if f"@{agent}" in message and "启动自检确认" not in message
    ][-3:]
    rule_pack = proto.compiled_rules(data, agent, raw_task)
    goal = state.get("goal") or None
    goal_view = None if not goal else {
        "id": goal.get("id"),
        "text": _clamp_text(goal.get("text", ""), 1200),
        "status": goal.get("status"),
        "context_summary": goal.get("context_summary", "")[:1200],
        "progress": f"{len(goal.get('completed', []))}/{goal.get('total', 0)}",
    }
    pack = {
        "ready": True,
        "project": project,
        "project_path": task_workspace(project, state) if authorization["execution_allowed"] else project_dir(project),
        "agent": agent,
        "authorization": authorization,
        "task": task,
        "goal": goal_view,
        **rule_pack,
        "messages": messages,
        "decision_ids": task.get("decision_ids", []),
        "memory": data.get("memory", {}),
    }
    if authorization["execution_allowed"]:
        pack["writeback"] = {
            "progress": f"plow-whip --project {project} task progress --output '...' --next '...'",
            "complete": f"plow-whip --project {project} task complete --output '...'",
            "handoff": f"plow-whip --project {project} handoff --to <agent> --output '...' --next '...'",
            "goal_plan": f"plow-whip --project {project} goal plan --context-summary '...' --plan-json '[{{...}}]'",
            "plan_propose": f"plow-whip --project {project} plan propose --context-summary '...' --plan-json '[{{...}}]'",
        }
    if "planning" in raw_task.get("rule_tags", []):
        pack["routing_catalog"] = routing.planner_catalog(data)
    pack["rules_meta"]["startup_payload_max_chars"] = START_PACK_MAX_CHARS
    payload_chars = len(json.dumps(pack, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    pack["rules_meta"]["startup_payload_chars"] = payload_chars
    payload_chars = len(json.dumps(pack, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    pack["rules_meta"]["startup_payload_chars"] = payload_chars
    if payload_chars > START_PACK_MAX_CHARS:
        return {
            "ready": False,
            "project": project,
            "agent": agent,
            "action_required": "reduce_startup_payload",
            "startup_payload_chars": payload_chars,
            "startup_payload_max_chars": START_PACK_MAX_CHARS,
        }
    return pack


def cmd_start(project, args):
    payload = build_start_pack(project, getattr(args, "agent", None))
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_submit(project, args):
    from . import tasking
    from . import codex_desktop

    payload = tasking.submit(
        project,
        args.text,
        requested_cli=getattr(args, "cli", None),
        planner=getattr(args, "planner", None),
        target_branch=getattr(args, "target_branch", None),
        source=getattr(args, "source", "current_session"),
        interaction=codex_desktop.interaction(project),
        replace=getattr(args, "replace", False),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_plan(project, args):
    from . import tasking

    if args.action == "propose":
        payload = tasking.propose_plan(project, args.context_summary, json.loads(args.plan_json))
    elif args.action == "confirm":
        payload = tasking.confirm_plan(project)
    elif args.action == "reject":
        payload = tasking.reject_plan(project, args.reason)
    else:
        state = load_state(project)
        payload = {"project": project, "workflow": state.get("workflow"), "task": state.get("task")}
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_review(project, args):
    from . import tasking

    payload = tasking.reject_review(project, args.reason)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_decision(project, args):
    from . import tasking

    if args.action == "request":
        payload = tasking.request_decision(project, args.summary, args.option, args.recommended)
    elif args.action == "answer":
        payload = tasking.answer_decision(project, args.choice, args.note or "")
    else:
        state = load_state(project)
        payload = {
            "project": project,
            "decision": (state.get("task") or {}).get("decision_request"),
            "task_id": (state.get("task") or {}).get("id"),
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_automation(project, args):
    state = load_state(project)
    if args.action in ("enable", "disable"):
        state["automation_enabled"] = args.action == "enable"
        save_state(project, state)
    from . import scheduler

    print(json.dumps({
        "project": project, "automation_enabled": state.get("automation_enabled", True),
        "scheduler": scheduler.status(CONFIG_DIR),
    }, ensure_ascii=False, indent=2))


def cmd_health(args):
    from . import health

    if args.action == "probe":
        drivers = [args.driver] if args.driver else list(health.DRIVERS)
        payload = {driver: health.probe_driver(driver) for driver in drivers}
    else:
        payload = health.load(CONFIG_DIR)
        payload["deepseek_keys"] = health.safe_key_refs()
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def run_task_verification(project, commands, task=None):
    """Run the task's explicit acceptance commands and stop at first failure."""
    results = []
    workspace = task_workspace(project)
    for command in commands:
        if (task or {}).get("verification_policy") == "sandboxed":
            from .simple_tasker import SimpleTasker

            try:
                sandboxed = SimpleTasker(
                    workspace, (task or {}).get("id", "verification"),
                    session_dir=os.path.join(project_memory_dir(project), "sessions"),
                ).run_command(command)
            except ValueError as exc:
                sandboxed = {"returncode": 126, "output": f"sandbox rejected verification command: {exc}"}
            result = {"command": command, "returncode": sandboxed["returncode"], "output": sandboxed["output"][-2000:]}
        else:
            completed = subprocess.run(
                command,
                cwd=workspace,
                shell=True,
                capture_output=True,
                text=True,
                check=False,
            )
            result = {
                "command": command,
                "returncode": completed.returncode,
                "output": (completed.stdout + completed.stderr).strip()[-2000:],
            }
        results.append(result)
        if result["returncode"]:
            break
    return results


def _archive_task_sessions(project, task):
    if not task.get("cli_sessions"):
        return
    validate_identifier(task.get("id", "task"), "task")
    atomic_write_json(os.path.join(project_memory_dir(project), "sessions", f"{task['id']}_cli_sessions.json"), {
        "project": project,
        "task_id": task.get("id"),
        "title": task.get("title"),
        "final_output": task.get("last_output", ""),
        "archived_at": datetime.now().isoformat(timespec="seconds"),
        "cli_sessions": task.get("cli_sessions", {}),
    })


def _goal_record(goal_id, text, owner=None, status="planning"):
    return {
        "id": goal_id, "text": text, "status": status, "preferred_owner": owner,
        "context_summary": "", "total": 0, "completed": [], "queue": [],
    }


def _planning_task(goal, owner):
    return {
        "id": f"{goal['id']}-PLAN", "title": "Plan goal milestones", "goal_id": goal["id"],
        "owner": owner, "status": "active",
        "next_action": "Create 1-7 coarse milestones from the goal using roles/capabilities. If the goal already supplies enough detail, do not scan the repository or run tests during planning; inspect only named files needed to fill a concrete gap. The last milestone must be independent final acceptance.",
        "acceptance": [
            "Plan contains 1-7 coarse independently verifiable milestones",
            "No broad repository read or test run when the supplied goal is sufficient",
            "Last milestone performs independent final acceptance",
        ],
        "verify_commands": [], "rule_tags": ["planning"], "last_output": "", "blockers": [],
        "decision_ids": [], "cli_sessions": {}, "required_role": "planner", "required_capabilities": [],
    }


def _planner_owner(data, preferred=None):
    from .tasking import planner_owner

    return planner_owner(data, preferred)


def _activate_goal(state, data, goal):
    goal["status"] = "planning"
    owner = _planner_owner(data, goal.pop("preferred_owner", None))
    state["goal"] = goal
    state["task"] = _planning_task(goal, owner)
    state["phase"] = goal["id"]
    state["automation_enabled"] = True


def cmd_goal(project, args):
    state = load_state(project)
    data = proto.ensure(project_dir(project), project, get_agents(), get_agent_meta())
    if args.action == "start":
        goal_id = f"G-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        goal = _goal_record(goal_id, args.text, getattr(args, "owner", None))
        active_goal = (state.get("goal") or {}).get("status") in ("planning", "active")
        active_task = state.get("task", {}).get("status") == "active" and state.get("task", {}).get("id") != "T-001"
        if (active_goal or active_task) and not getattr(args, "replace", False):
            goal["status"] = "queued"
            state.setdefault("goal_queue", []).append(goal)
            state["automation_enabled"] = True
            save_state(project, state)
            append_comms(project, f"goal {goal_id} queued: {args.text}")
            print(json.dumps({"project": project, "action": "queued", "goal": goal, "queue_length": len(state["goal_queue"])}, ensure_ascii=False, indent=2))
            return
        if getattr(args, "replace", False) and state.get("goal"):
            replaced = dict(state["goal"])
            replaced["status"] = "replaced"
            state.setdefault("goal_history", []).append(replaced)
        _activate_goal(state, data, goal)
    elif args.action == "plan":
        goal = state.get("goal") or {}
        if goal.get("status") != "planning":
            raise ValueError("goal is not waiting for a milestone plan")
        raw = json.loads(args.plan_json)
        if not isinstance(raw, list) or not 1 <= len(raw) <= 7:
            raise ValueError("goal plan must contain 1 to 7 milestones")
        if not raw[-1].get("final_acceptance"):
            raise ValueError("last milestone must declare final_acceptance=true")
        milestones = []
        implementation_owners = set()
        for index, item in enumerate(raw, 1):
            final = index == len(raw)
            role = proto.normalize_role(str(item.get("role") or ("reviewer" if final else "implementation")))
            capabilities = list(dict.fromkeys(
                str(value).strip().lower() for value in item.get("capabilities", []) if str(value).strip()
            ))
            owner = item.get("owner")
            if owner and owner not in proto.schedulable_agents(data):
                raise ValueError(f"milestone owner is unknown or non-schedulable: {owner}")
            if not owner:
                owner = routing.select_agent(
                    data, role=role, capabilities=capabilities,
                    exclude_agents=implementation_owners if final else None,
                    driver_available=lambda driver: driver in ("codex_cli", "cursor_cli", "zellij"),
                )
            if not owner:
                raise ValueError(f"no executable agent matches role={role} capabilities={capabilities}")
            if final and owner in implementation_owners:
                independent_owner = routing.select_agent(
                    data, role=role, capabilities=capabilities,
                    exclude_agents=implementation_owners,
                    driver_available=lambda driver: driver in ("codex_cli", "cursor_cli", "zellij"),
                )
                if independent_owner:
                    owner = independent_owner
            if final and implementation_owners and owner in implementation_owners:
                raise ValueError("final acceptance requires an independent executable agent")
            title = str(item.get("title", "")).strip()
            acceptance = item.get("acceptance") or []
            if not title or not acceptance:
                raise ValueError("each milestone needs title and acceptance")
            milestones.append({
                "id": f"{goal['id']}-M{index}", "title": title, "goal_id": goal["id"], "owner": owner,
                "status": "active", "next_action": item.get("next_action") or title,
                "acceptance": acceptance, "verify_commands": item.get("verify_commands") or [],
                "rule_tags": item.get("rule_tags") or [], "last_output": "", "blockers": [],
                "decision_ids": [], "cli_sessions": {}, "final": final,
                "required_role": role, "required_capabilities": capabilities,
                "independent_acceptance": final and owner not in implementation_owners,
            })
            if not final:
                implementation_owners.add(owner)
        goal.update({"status": "active", "context_summary": args.context_summary[:1200], "total": len(milestones), "queue": milestones[1:]})
        state["task"] = milestones[0]
    else:
        payload = {"project": project, "goal": state.get("goal"), "task": state.get("task")}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    save_state(project, state)
    append_comms(project, f"goal {state['goal']['id']} {args.action}: {state['goal']['text']}")
    print(json.dumps({"project": project, "action": args.action, "goal": state["goal"], "task": state["task"]}, ensure_ascii=False, indent=2))


def cmd_task(project, args):
    state = load_state(project)
    task = state.setdefault("task", {})
    action = args.action
    git_delivery_retry = False
    protected_human_pause = False
    if action == "complete" and task.get("status") == "blocked_waiting_human":
        from . import tasking

        git_delivery_retry = tasking.is_git_delivery_retry(state.get("workflow") or {}, task)
        protected_human_pause = not git_delivery_retry
    if action == "start":
        task_id = getattr(args, "task_id", None) or f"T-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        owner = getattr(args, "owner", None) or state.get("current_agent")
        if owner not in get_project_agents(project):
            print(f"Error: unknown project agent '{owner}'", file=sys.stderr)
            sys.exit(1)
        if owner not in proto.schedulable_agents(load_protocol(project)):
            print(
                f"Error: '{owner}' is a non-schedulable control plane; use `plow-whip --project {project} submit ...` instead.",
                file=sys.stderr,
            )
            sys.exit(2)
        task = {
            "id": task_id,
            "title": args.title,
            "goal": getattr(args, "goal", None) or args.title,
            "owner": owner,
            "status": "active",
            "next_action": args.next or args.title,
            "acceptance": getattr(args, "acceptance", None) or [],
            "verify_commands": getattr(args, "verify", None) or [],
            "rule_tags": getattr(args, "rule_tags", None) or [],
            "last_output": "",
            "blockers": [],
            "decision_ids": getattr(args, "decisions", None) or [],
            "cli_sessions": {},
        }
        state["current_agent"] = owner
        state["assigned_agent"] = owner
        state["phase"] = task_id
    else:
        if not protected_human_pause and getattr(args, "output", None) is not None:
            task["last_output"] = args.output
        if not protected_human_pause and getattr(args, "next", None) is not None:
            task["next_action"] = args.next
        if not protected_human_pause and getattr(args, "acceptance", None) is not None:
            task["acceptance"] = args.acceptance
        if not protected_human_pause and getattr(args, "verify", None) is not None:
            task["verify_commands"] = args.verify
        if not protected_human_pause and getattr(args, "rule_tags", None) is not None:
            task["rule_tags"] = args.rule_tags
        if protected_human_pause:
            action = "blocked_waiting_human"
        elif action == "block":
            task["status"] = "blocked"
            task["blockers"] = getattr(args, "blockers", None) or []
            if leases.is_strict(load_protocol(project)):
                leases.revoke(task, "task blocked")
        elif action == "complete":
            if git_delivery_retry:
                state["workflow"]["status"] = "active"
            requires_verification = (
                (state.get("workflow") or {}).get("code_change")
                and task.get("stage") == "implementation"
                and not task.get("verify_commands")
            )
            verification = ([{
                "command": "<verify_commands required>", "returncode": 2,
                "output": "Code-changing tasks must declare and pass at least one verification command.",
            }] if requires_verification else run_task_verification(project, task.get("verify_commands", []), task))
            failed = next((item for item in verification if item["returncode"]), None)
            task["verification"] = verification
            if failed:
                task["status"] = "active"
                task["next_action"] = f"Fix verification failure: {failed['command']}"
                task["blockers"] = []
                task["last_output"] = "\n".join(filter(None, [
                    task.get("last_output", ""),
                    f"Verification failed ({failed['returncode']}): {failed['command']}",
                    failed["output"],
                ]))
            else:
                task["status"] = "done"
                task["next_action"] = ""
                task["blockers"] = []
                archived_at = datetime.now().isoformat(timespec="seconds")
                for session in task.setdefault("cli_sessions", {}).values():
                    session["status"] = "archived"
                    session["archived_at"] = archived_at
        else:
            task["status"] = "active"
    completed_task = task
    if action == "complete" and task.get("status") == "done" and leases.is_strict(load_protocol(project)):
        leases.revoke(task, "task execution finished")
    goal = state.get("goal") or {}
    workflow_handled = False
    if action == "complete" and task.get("status") == "done" and (state.get("workflow") or {}).get("status") == "active":
        from . import tasking

        _archive_task_sessions(project, task)
        advanced = tasking.advance_after_completion(project, state, task)
        if advanced:
            task, action = advanced
            workflow_handled = True
    if not workflow_handled and action == "complete" and task.get("status") == "done" and goal.get("status") == "active":
        goal.setdefault("completed", []).append({
            "id": task.get("id"), "title": task.get("title"), "owner": task.get("owner"),
            "output": task.get("last_output", "")[-500:],
        })
        _archive_task_sessions(project, task)
        if goal.get("queue"):
            task = goal["queue"].pop(0)
            action = "advance"
        else:
            goal["status"] = "done"
            if state.get("goal_queue"):
                state.setdefault("goal_history", []).append(goal)
                next_goal = state["goal_queue"].pop(0)
                _activate_goal(state, load_protocol(project), next_goal)
                task = state["task"]
                action = "advance_goal"
            else:
                state["goal"] = goal
        if action != "advance_goal":
            state["goal"] = goal
    elif not workflow_handled and action == "complete" and task.get("status") == "done" and state.get("goal_queue"):
        next_goal = state["goal_queue"].pop(0)
        _activate_goal(state, load_protocol(project), next_goal)
        task = state["task"]
        action = "advance_goal"
    state["task"] = task
    state["status"] = {"active": "in_progress"}.get(task.get("status"), task.get("status", "in_progress"))
    state["next_action"] = task.get("next_action", "")
    state["last_output"] = task.get("last_output", "")
    state["blockers"] = task.get("blockers", [])
    state["current_agent"] = task.get("owner", state.get("current_agent"))
    state["assigned_agent"] = state["current_agent"]
    save_state(project, state)
    if action == "complete" and completed_task.get("status") == "done" and not goal:
        _archive_task_sessions(project, completed_task)
    append_comms(project, f"task {task.get('id')} {action}: {task.get('last_output') or task.get('next_action') or task.get('title', '')}")
    payload = {"project": project, "action": action, "task": task}
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Task {task.get('id')} -> {task.get('status')}")


def cmd_list():
    projects_dir = get_projects_dir()
    if not os.path.isdir(projects_dir):
        print("No projects directory configured.")
        return
    projects = sorted(
        d for d in os.listdir(projects_dir)
        if os.path.isdir(os.path.join(projects_dir, d, "collab"))
    )
    if not projects:
        print("No active projects.")
        return

    print(f"\n📋 Active projects ({len(projects)}):\n")
    for p in projects:
        sf = state_file(p)
        if os.path.exists(sf):
            state = load_state(p)
            agent = state["current_agent"]
            status_map = {"in_progress": "In Progress", "done": "Done", "blocked": "Blocked"}
            status = status_map.get(state["status"], state["status"])
            ctx = state.get("task_context", {})
            topic = ctx.get("topic", "")
            print(f"  🤖 {p:20s} {topic:20s} {agent} — {status}")
        else:
            print(f"  ⚪ {p:20s} (not initialized)")
    print()


def cmd_archive(project):
    pcd = project_collab_dir(project)
    if not os.path.isdir(pcd):
        print(f"Project '{project}' not found.", file=sys.stderr)
        sys.exit(1)

    state = load_state(project)
    if state["status"] != "done":
        print(f"Project '{project}' status is '{state['status']}', only 'done' can be archived.", file=sys.stderr)
        sys.exit(1)

    projects_dir = get_projects_dir()
    archive_dir = os.path.join(projects_dir, "by_rm", "plow-whip-archive")
    os.makedirs(archive_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    archive_name = f"{project}_{timestamp}.tar.gz"
    archive_path = os.path.join(archive_dir, archive_name)

    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(pcd, arcname=f"{project}/collab")

    shutil.move(pcd, os.path.join(archive_dir, f"{project}_{timestamp}"))
    print(f"\n📦 Archived: {project}")
    print(f"   Archive: {archive_path}")
    print(f"   Last output: {state['last_output']}")
    print()



def cmd_bind_tab(project: str, tab_index: int, tab_name: str = None):
    """Bind a project to a specific zellij tab for targeted whip dispatch."""
    sf = state_file(project)
    if not os.path.exists(sf):
        print(f"Error: project '{project}' not found.", file=sys.stderr)
        sys.exit(1)
    with open(sf, encoding="utf-8") as f:
        state = json.load(f)
    state["zellij_tab"] = tab_index
    if tab_name:
        state.setdefault("task_context", {})["tab_name"] = tab_name
    save_state(project, state)
    name_info = f" ({tab_name})" if tab_name else ""
    print(f"\n📌 Project '{project}' bound to zellij tab {tab_index}{name_info}")
    print(f"   Whip will now target this tab when dispatching to {project}\n")


def cmd_watch(project, args):
    sf = state_file(project)
    interval = max(args.interval, 0.2)
    last_mtime = os.path.getmtime(sf) if os.path.exists(sf) else 0
    print(f"👀 Watching project '{project}'... interval {interval:g}s, Ctrl+C to stop")
    try:
        while True:
            time.sleep(interval)
            mtime = os.path.getmtime(sf) if os.path.exists(sf) else 0
            if mtime == last_mtime:
                continue
            last_mtime = mtime
            state = load_state(project)
            agent = state["current_agent"]
            status_map = {"in_progress": "In Progress", "done": "Done", "blocked": "Blocked"}
            status = status_map.get(state["status"], state["status"])
            summary = f"[{project}] {agent}: {state['phase']} — {status}"
            notify(summary)
            if not args.quiet:
                print(f"\n🔔 State change: {summary}")
                print(f"📌 Last output: {state['last_output'] or '(none)'}")
                print(f"➡️  Next: {state['next_action'] or '(none)'}")
    except KeyboardInterrupt:
        print(f"\n👋 Stopped watching '{project}'")


def cmd_session(project, agent):
    curr = os.path.join(conversations_dir(project), agent, "current.md")
    if not os.path.exists(curr):
        print(f"❗ {agent}'s current.md not found.")
        return
    size = os.path.getsize(curr)
    with open(curr, encoding="utf-8") as f:
        lines = f.readlines()
    line_count = len(lines)
    needs_rotate = line_count > ROTATE_MAX_LINES or size > ROTATE_MAX_KB * 1024
    status = "🔴 Needs rotation" if needs_rotate else "🟢 OK"
    print(f"\n📝 {agent} current session: {curr}")
    print(f"   Lines: {line_count} / {ROTATE_MAX_LINES}")
    print(f"   Size: {size / 1024:.1f}KB / {ROTATE_MAX_KB}KB")
    print(f"   Status: {status}")
    print()


def cmd_rotate(project, agent, args):
    conv_dir = conversations_dir(project)
    curr = os.path.join(conv_dir, agent, "current.md")
    if not os.path.exists(curr):
        print(f"❗ {agent}'s current.md not found, creating.")
        os.makedirs(os.path.join(conv_dir, agent), exist_ok=True)
        _write_session_template(agent, project, curr)
        return

    with open(curr, encoding="utf-8") as f:
        content = f.read()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    topic = args.topic or "session"
    topic_safe = topic.replace(" ", "_").replace("/", "-")[:40]
    archive_name = f"{timestamp}_{topic_safe}.md"
    archive_path = os.path.join(conv_dir, agent, archive_name)

    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(f"# Archived Session: {topic}\n")
        f.write(f"**AI:** {agent}\n")
        f.write(f"**Archived:** {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"**Project:** {project}\n\n")
        if args.summary:
            f.write(f"## Summary\n{args.summary}\n\n")
        f.write(generate_carry_forward(project, agent, content, summary=args.summary))
        f.write("\n")
        f.write("## Original Content\n\n")
        f.write(content)

    _write_session_template(agent, project, curr)

    size = os.path.getsize(archive_path)
    print(f"\n🔄 Rotated: {agent} (project: {project})")
    print(f"   Archived: {archive_path} ({size / 1024:.1f}KB)")
    print(f"   New current.md created")
    print()
    notify(f"[session rotated] {agent}: {topic}")


def _write_session_template(agent, project, path):
    """Write a fresh current.md for an agent."""
    meta = {}
    if os.path.exists(protocol_file(project)):
        meta = load_protocol(project).get("agents", {}).get(agent, {})
    role = meta.get("role") or get_agent_label(agent)
    assignment = meta.get("assignment") or get_agent_assignment(agent)
    lines = [f"# {agent} Session — {project}", "", f"**AI:** {role}"]
    if assignment:
        lines.append(f"**Assignment:** {assignment}")
    lines += [
        f"**Started:** {datetime.now().strftime('%Y-%m-%d')}", "**Topic:** —", "",
        "## Previous", "- (none, new session)", "", "## Current Tasks",
        "- (check the start --json payload)", "", "## Key Decisions",
        "- (decisions will be appended here)", "", "## Outputs",
        "- (outputs will be appended here)", "",
    ]
    atomic_write_text(path, "\n".join(lines))


def cmd_sessions_overview(project):
    print(f"\n📊 Session overview (project: {project}):\n")
    for agent in get_project_agents(project):
        curr = os.path.join(conversations_dir(project), agent, "current.md")
        if not os.path.exists(curr):
            print(f"  ⚪ {agent:8s} — not initialized")
            continue
        size = os.path.getsize(curr)
        with open(curr, encoding="utf-8") as f:
            lines = len(f.readlines())
        needs_rotate = lines > ROTATE_MAX_LINES or size > ROTATE_MAX_KB * 1024
        icon = "🔴" if needs_rotate else "🟢"
        print(f"  {icon} {agent:8s} — {lines} lines, {size / 1024:.1f}KB")
    print()


def cmd_sync():
    """Sync framework templates to all projects."""
    projects_dir = get_projects_dir()
    if not os.path.isdir(projects_dir):
        print("No projects directory configured.", file=sys.stderr)
        sys.exit(1)

    projects = sorted(
        d for d in os.listdir(projects_dir)
        if os.path.isdir(os.path.join(projects_dir, d, "collab"))
        and d != "by_rm"
    )
    if not projects:
        print("No projects with collab/ found.")
        return

    print(f"\n🔄 Syncing framework templates to {len(projects)} project(s):\n")
    for p in projects:
        updated = []
        try:
            proto.ensure(project_dir(p), p, get_agents(), get_agent_meta())
            if proto.write_handbook(project_dir(p)):
                updated.append("HANDBOOK.zh-CN.md")
            _write_compat_conventions(p)
            write_agent_manifest(p)
        except OSError as exc:
            print(f"  {p:20s} SKIP: {exc}")
            continue
        # Note: memory files are project-specific, NOT synced
        status = "✅ " + ", ".join(updated) if updated else "⚪ no changes"
        print(f"  {p:20s} {status}")
    print()


def cmd_desktop(project, args):
    from . import codex_desktop

    payload = codex_desktop.sync(project) if args.action == "sync" else codex_desktop.status(project)
    if args.action == "sync" and payload.get("status") == "synced":
        check_and_rotate_agent(project, "codex", topic="desktop_sync")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_scheduler(args):
    from . import scheduler

    auto_continue = bool(getattr(args, "auto_continue", False) or getattr(args, "auto_crack", False))

    if args.action == "install":
        payload = scheduler.install(CONFIG_DIR, args.interval, auto_continue, dry_run=args.dry_run)
    elif args.action == "uninstall":
        payload = scheduler.uninstall(CONFIG_DIR)
    elif args.action == "start":
        payload = scheduler.start(CONFIG_DIR)
    elif args.action == "stop":
        payload = scheduler.stop(CONFIG_DIR)
    elif args.action == "logs":
        payload = scheduler.logs(CONFIG_DIR, args.lines)
    elif args.action == "doctor":
        payload = scheduler.doctor(CONFIG_DIR)
    elif args.action == "repair":
        payload = scheduler.repair(CONFIG_DIR)
    elif args.action == "run":
        from .whip import run_once
        payload = run_once(stale_minutes=args.stale_minutes, crack=auto_continue, auto_rotate=True, opt_in_only=auto_continue)
    else:
        payload = scheduler.status(CONFIG_DIR)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_inbox(args):
    from .dispatch import read_inbox, update_inbox_task

    if args.action == "list":
        payload = read_inbox(args.agent)
    else:
        try:
            payload = update_inbox_task(args.agent, args.dispatch_id, args.status, args.output or "")
        except (KeyError, ValueError) as exc:
            payload = {"updated": False, "error": str(exc)}
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _machine_write_action(args) -> str | None:
    """Return the lease-protected operation represented by parsed CLI args."""
    command = getattr(args, "command", None)
    action = getattr(args, "action", None)
    if command == "task" and action in ("start", "progress", "block", "complete"):
        return f"task.{action}"
    if command == "handoff":
        return "handoff"
    if command == "plan" and action == "propose":
        return "plan.propose"
    if command == "review" and action == "reject":
        return "review.reject"
    if command == "decision" and action == "request":
        return "decision.request"
    if command == "goal" and action == "plan":
        return "goal.plan"
    if command == "drive":
        return "drive"
    return None


def _human_control_action(args) -> str | None:
    command = getattr(args, "command", None)
    action = getattr(args, "action", None)
    if command == "submit":
        return "submit"
    if command == "plan" and action in ("confirm", "reject"):
        return f"plan.{action}"
    if command == "decision" and action == "answer":
        return "decision.answer"
    if command == "automation" and action in ("enable", "disable"):
        return f"automation.{action}"
    if command in ("reset", "archive", "repair", "init", "new"):
        return command
    if command == "goal" and action == "start":
        return "goal.start"
    return None


def _require_machine_lease(project: str, args) -> None:
    operation = _machine_write_action(args)
    data = load_protocol(project)
    if not leases.is_strict(data):
        return
    control = _human_control_action(args)
    if control and os.environ.get(leases.TOKEN_ENV):
        raise leases.LeaseDenied(f"worker lease cannot authorize human control operation {control}")
    if not operation:
        return
    if operation in ("task.start", "drive"):
        raise leases.LeaseDenied(
            f"{operation} is not an execution entry in strict mode; submit work and let the scheduler issue a lease"
        )
    state = load_state(project)
    leases.validate(CONFIG_DIR, project, state, data)


# ── Entry Point ────────────────────────────────────────────────────────────────

def main():
    def positive_int(value):
        number = int(value)
        if number < 1:
            raise argparse.ArgumentTypeError("must be at least 1")
        return number

    def project_name(value):
        try:
            return validate_identifier(value, "project")
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    parser = argparse.ArgumentParser(
        description="🪢 plow-whip — Multi-Agent Collaboration Framework / 耕田之鞭"
    )
    parser.add_argument("--project", "-p", type=project_name, help="Target project name")

    sub = parser.add_subparsers(dest="command")

    # configure
    cfg_parser = sub.add_parser("configure", help="Configure plow-whip")
    cfg_parser.add_argument("--projects-dir", help="Root directory for projects")
    cfg_parser.add_argument("--agents", nargs="+", help="Agent names")

    # list
    sub.add_parser("list", help="List all active projects")

    # agent
    agent_parser = sub.add_parser("agent", help="List or edit agents")
    agent_sub = agent_parser.add_subparsers(dest="action")
    agent_sub.required = True
    agent_sub.add_parser("list", help="List configured agents")
    agent_set = agent_sub.add_parser("set", help="Set an agent role or assignment")
    agent_set.add_argument("name", help="Agent name")
    agent_set.add_argument("--role", help="Role label")
    agent_set.add_argument("--roles", nargs="+", help="Stable routing role tags")
    agent_set.add_argument("--capabilities", nargs="*", help="Capability tags")
    agent_set.add_argument("--driver", choices=["codex_cli", "cursor_cli", "simple_tasker", "zellij", "file", "control"], help="Execution driver")
    agent_set.add_argument("--schedulable", action=argparse.BooleanOptionalAction, help="Allow scheduler/task ownership")
    agent_set.add_argument("--priority", type=int, help="Routing priority")
    agent_set.add_argument("--cost-tier", choices=["low", "medium", "high"], help="Token/cost tier")
    agent_set.add_argument("--assignment", help="Current assignment")

    # status
    sub.add_parser("status", help="View project status")

    # doctor
    doctor_parser = sub.add_parser("doctor", help="Check plow-whip mechanism and optionally repair it")
    doctor_parser.add_argument("--repair", action="store_true", help="Create missing plow-whip files without overwriting")
    doctor_parser.add_argument("--skip-rotate", action="store_true", help="With --repair, skip automatic rotation enforcement")
    doctor_parser.add_argument("--json", action="store_true", help="Output JSON")

    repair_parser = sub.add_parser("repair", help="Explicitly create or repair missing plow-whip structure")
    repair_parser.add_argument("--json", action="store_true", help="Output JSON")

    start_parser = sub.add_parser("start", help="Return the complete minimal startup payload")
    start_parser.add_argument("--agent", help="Target agent")
    start_parser.add_argument("--json", action="store_true", help="Output JSON")

    submit_parser = sub.add_parser("submit", help="Submit and locally route a new unattended task")
    submit_parser.add_argument("text", help="Task description")
    submit_parser.add_argument("--cli", choices=["codex_cli", "cursor_cli", "simple_tasker", "codex", "cursor", "deepseek"])
    submit_parser.add_argument("--planner", help="Configured planner agent (default codex_cli)")
    submit_parser.add_argument("--target-branch", help="Fast-forward delivery target (default main)")
    submit_parser.add_argument("--source", default="current_session", help="Interaction source identifier")
    submit_parser.add_argument("--replace", action="store_true", help="Deliberately replace current active work")
    desktop_parser = sub.add_parser("desktop", help="Sync or inspect the local Codex Desktop conversation")
    desktop_parser.add_argument("action", choices=["sync", "status"])

    plan_parser = sub.add_parser("plan", help="Propose or confirm a non-goal milestone workflow")
    plan_sub = plan_parser.add_subparsers(dest="action", required=True)
    plan_propose = plan_sub.add_parser("propose")
    plan_propose.add_argument("--context-summary", required=True)
    plan_propose.add_argument("--plan-json", required=True)
    plan_sub.add_parser("confirm")
    plan_reject = plan_sub.add_parser("reject")
    plan_reject.add_argument("--reason", required=True)
    plan_sub.add_parser("status")

    review_parser = sub.add_parser("review", help="Independent reviewer lifecycle")
    review_sub = review_parser.add_subparsers(dest="action", required=True)
    review_reject = review_sub.add_parser("reject")
    review_reject.add_argument("--reason", required=True)

    decision_parser = sub.add_parser("decision", help="Pause for or answer a bounded human decision")
    decision_sub = decision_parser.add_subparsers(dest="action", required=True)
    decision_request = decision_sub.add_parser("request")
    decision_request.add_argument("--summary", required=True)
    decision_request.add_argument("--option", action="append", required=True)
    decision_request.add_argument("--recommended")
    decision_answer = decision_sub.add_parser("answer")
    decision_answer.add_argument("--choice", required=True)
    decision_answer.add_argument("--note")
    decision_sub.add_parser("status")

    automation_parser = sub.add_parser("automation", help="Enable, disable, or inspect unattended execution")
    automation_sub = automation_parser.add_subparsers(dest="action", required=True)
    for automation_action in ("enable", "disable", "status"):
        automation_sub.add_parser(automation_action)

    task_parser = sub.add_parser("task", help="Atomically update the current task")
    task_sub = task_parser.add_subparsers(dest="action", required=True)
    task_start = task_sub.add_parser("start")
    task_start.add_argument("--id", dest="task_id")
    task_start.add_argument("--title", required=True)
    task_start.add_argument("--goal")
    task_start.add_argument("--owner")
    task_start.add_argument("--next")
    task_start.add_argument("--acceptance", nargs="*")
    task_start.add_argument("--verify", nargs="*")
    task_start.add_argument("--rule-tags", nargs="*")
    task_start.add_argument("--decisions", nargs="*")
    for action in ("progress", "block", "complete"):
        item = task_sub.add_parser(action)
        item.add_argument("--output", required=True)
        item.add_argument("--next")
        if action == "progress":
            item.add_argument("--acceptance", nargs="*")
            item.add_argument("--verify", nargs="*")
            item.add_argument("--rule-tags", nargs="*")
        item.add_argument("--json", action="store_true")
        if action == "block":
            item.add_argument("--blockers", nargs="+", required=True)
    task_start.add_argument("--json", action="store_true")

    goal_parser = sub.add_parser("goal", help="Start and run a coarse milestone goal")
    goal_sub = goal_parser.add_subparsers(dest="action", required=True)
    goal_start = goal_sub.add_parser("start")
    goal_start.add_argument("text")
    goal_start.add_argument("--owner")
    goal_start.add_argument("--replace", action="store_true", help="Explicitly replace active work instead of queueing")
    goal_plan = goal_sub.add_parser("plan")
    goal_plan.add_argument("--context-summary", required=True)
    goal_plan.add_argument("--plan-json", required=True)
    goal_sub.add_parser("status")

    # handoff
    hp = sub.add_parser("handoff", help="Handoff to next agent")
    hp.add_argument("--phase", help="Phase name")
    hp.add_argument("--status", choices=["in_progress", "done", "blocked"], default="done")
    hp.add_argument("--output", required=True, help="Output summary")
    hp.add_argument("--next", help="Next step")
    hp.add_argument("--day", type=int, help="Current day")
    hp.add_argument("--topic", help="Current topic")
    hp.add_argument("--project-dir", help="Code directory")
    hp.add_argument("--files", nargs="+", help="Changed files")
    hp.add_argument("--verify", nargs="+", help="Verify commands")
    hp.add_argument("--to", help="Assign next turn to a specific agent")
    hp.add_argument("--blockers", nargs="+", help="Known blockers")

    # reset
    sub.add_parser("reset", help="Reset project state")

    # init
    sub.add_parser("init", help="Initialize new project")

    # new
    np = sub.add_parser("new", help="Create a new project and initialize plow-whip")
    np.add_argument("--first-action", help="Initial next_action for the project")
    np.add_argument("--owner", help="Initial owner/current agent")

    # archive
    sub.add_parser("archive", help="Archive completed project")

    # watch
    wp = sub.add_parser("watch", help="Watch project state changes")
    wp.add_argument("--interval", type=float, default=3, help="Poll interval (default 3s)")
    wp.add_argument("--quiet", action="store_true", help="Quiet mode")

    # session
    sp = sub.add_parser("session", help="View agent session status")
    sp.add_argument("--agent", required=True, help="Agent name")

    # rotate
    rp = sub.add_parser("rotate", help="Rotate agent session")
    rp.add_argument("--agent", required=True, help="Agent name")
    rp.add_argument("--topic", help="Session topic")
    rp.add_argument("--summary", help="Session summary")

    # sessions-overview
    sub.add_parser("sessions-overview", help="All sessions overview")

    # context-pack
    cp = sub.add_parser("context-pack", help="Deprecated alias for start")
    cp.add_argument("--agent", help="Target agent (default: current_agent)")
    cp.add_argument("--max-comms", type=int, default=3, help="Recent relevant message blocks (default 3)")
    cp.add_argument("--json", action="store_true", help="Output JSON")

    # memory-rotate — check and rotate all collab/memory files
    mr = sub.add_parser("memory-rotate", help="Check and rotate all collab/memory files")
    mr.add_argument("--scan", action="store_true", help="Scan only, show activity without rotating")

    # memory-budget — token budget for Hot/Warm/Cold memory
    mb = sub.add_parser("memory-budget", help="Show Hot/Warm/Cold token budget")
    mb.add_argument("--enforce-rotate", action="store_true", help="Rotate overdue sessions/files when budget exceeded")
    mb.add_argument("--json", action="store_true", help="Output JSON")

    rh = sub.add_parser("rotation-health", help="Show rotation thresholds and overdue files")
    rh.add_argument("--enforce", action="store_true", help="Rotate overdue sessions/files immediately")
    rh.add_argument("--json", action="store_true", help="Output JSON")

    # brain — DeepSeek 廉价大脑
    brain_parser = sub.add_parser("brain", help="DeepSeek brain for simple tasks")
    brain_parser.add_argument("task", help="Task description")
    brain_parser.add_argument("--context", help="Additional context")
    brain_parser.add_argument("--force", action="store_true", help="Force DeepSeek for complex tasks")

    # sync
    sub.add_parser("sync", help="Sync framework templates to all projects")

    # bind-tab
    btp = sub.add_parser("bind-tab", help="Bind project to zellij tab")
    btp.add_argument("--tab", type=int, required=True, help="Tab index (1-based)")
    btp.add_argument("--name", help="Tab name (for display)")

    # whip — plow-whip / 耕田之鞭
    whip_parser = sub.add_parser("whip", help="plow-whip — actively drive agents to work")
    whip_parser.add_argument("--agent", help="Target specific agent to whip")
    whip_parser.add_argument("--stale-minutes", type=int, help="Stale threshold in minutes (default 60)")
    whip_parser.add_argument("--json", action="store_true", help="Output as JSON")
    whip_parser.add_argument("--crack", action="store_true", help="CRACK! Actually dispatch tasks to agents")
    whip_parser.add_argument("--auto-crack", action="store_true", help="Deprecated: run one crack pass; use scheduler for repetition")
    whip_parser.add_argument("--channel", choices=["codex_cli", "cursor_cli", "simple_tasker", "zellij", "file", "notify"], help="Force specific dispatch channel")
    whip_parser.add_argument("--daemon", action="store_true", help="Deprecated: run one scan; use scheduler for repetition")
    whip_parser.add_argument("--interval", type=int, default=300, help="Deprecated compatibility option")
    whip_parser.add_argument("--force", action="store_true", help="Force dispatch even if project is not stale")
    whip_parser.add_argument("--auto-rotate", action="store_true", help="Auto-rotate sessions that exceed size thresholds")
    whip_parser.add_argument("--brain", action="store_true", help="Use DeepSeek brain for simple tasks before dispatching")
    whip_parser.add_argument("--once", action="store_true", help="Run one locked scheduler-safe pass and exit")
    whip_parser.add_argument("--opt-in-only", action="store_true", help="Dispatch only projects with automation enabled")

    scheduler_parser = sub.add_parser("scheduler", help="Manage native per-user scheduling")
    scheduler_sub = scheduler_parser.add_subparsers(dest="action", required=True)
    scheduler_install = scheduler_sub.add_parser("install")
    scheduler_install.add_argument("--interval", type=positive_int, default=60)
    scheduler_install.add_argument("--auto-crack", action="store_true")
    scheduler_install.add_argument("--auto-continue", action="store_true", help="Automatically resume stale active tasks")
    scheduler_install.add_argument("--dry-run", action="store_true")
    scheduler_install.set_defaults(auto_continue=True)
    scheduler_status = scheduler_sub.add_parser("status")
    scheduler_run = scheduler_sub.add_parser("run")
    scheduler_run.add_argument("--auto-crack", action="store_true")
    scheduler_run.add_argument("--auto-continue", action="store_true", help="Automatically resume stale active tasks")
    scheduler_run.add_argument("--stale-minutes", type=int, default=1)
    scheduler_run.set_defaults(auto_continue=True)
    scheduler_sub.add_parser("start")
    scheduler_sub.add_parser("stop")
    scheduler_logs = scheduler_sub.add_parser("logs")
    scheduler_logs.add_argument("--lines", type=int, default=40)
    scheduler_sub.add_parser("doctor")
    scheduler_sub.add_parser("repair")
    scheduler_sub.add_parser("uninstall")

    health_parser = sub.add_parser("health", help="Inspect zero-token CLI/network circuit health")
    health_sub = health_parser.add_subparsers(dest="action", required=True)
    health_sub.add_parser("status")
    health_probe = health_sub.add_parser("probe")
    health_probe.add_argument("--driver", choices=["codex_cli", "cursor_cli", "simple_tasker"])

    inbox_parser = sub.add_parser("inbox", help="Inspect or update dispatch lifecycle")
    inbox_sub = inbox_parser.add_subparsers(dest="action", required=True)
    inbox_list = inbox_sub.add_parser("list")
    inbox_list.add_argument("--agent", required=True)
    inbox_update = inbox_sub.add_parser("update")
    inbox_update.add_argument("--agent", required=True)
    inbox_update.add_argument("--dispatch-id", required=True)
    inbox_update.add_argument("--status", required=True, choices=["queued", "accepted", "running", "completed", "failed"])
    inbox_update.add_argument("--output")

    cli_auth_parser = sub.add_parser("cli-auth", help="Manage Desktop auth or referenced API key pools")
    cli_auth_sub = cli_auth_parser.add_subparsers(dest="action", required=True)
    cli_auth_status = cli_auth_sub.add_parser("status")
    cli_auth_status.add_argument("agent", nargs="?", choices=["codex_cli", "cursor_cli"])
    cli_auth_mode = cli_auth_sub.add_parser("mode")
    cli_auth_mode.add_argument("agent", choices=["codex_cli", "cursor_cli"])
    cli_auth_mode.add_argument("mode", choices=["desktop", "pool"])
    cli_auth_add = cli_auth_sub.add_parser("add")
    cli_auth_add.add_argument("agent", choices=["codex_cli", "cursor_cli"])
    cli_auth_add.add_argument("--name", required=True)
    cli_auth_add.add_argument("--env", required=True, help="Environment variable containing the key")
    cli_auth_add.add_argument("--model")
    for action in ("remove", "select"):
        item = cli_auth_sub.add_parser(action)
        item.add_argument("agent", choices=["codex_cli", "cursor_cli"])
        item.add_argument("--name", required=True)
    cli_auth_failover = cli_auth_sub.add_parser("failover")
    cli_auth_failover.add_argument("agent", choices=["codex_cli", "cursor_cli"])
    cli_auth_failover.add_argument("value", choices=["on", "off"])

    # drive — Desktop 驱使 CLI（Codex / Cursor 编排者）
    drive_parser = sub.add_parser("drive", help="Drive any registered logical agent through its configured driver")
    drive_parser.add_argument("target_agent", help="Registered logical agent to drive")
    drive_parser.add_argument("--next", help="Task description")
    drive_parser.add_argument("--prompt-file", help="Read task from file")
    drive_parser.add_argument("--from-agent", default="codex", help="Requester agent name (default: codex)")
    drive_parser.add_argument("--channel", choices=["auto", "file", "cursor_cli", "codex_cli"], default="auto",
                              help="Dispatch channel (default: auto = inbox + background CLI)")
    drive_parser.add_argument("--foreground", action="store_true", help="Run CLI in foreground (blocks)")
    drive_parser.add_argument("--status", action="store_true", help="Check last drive result (tail log + inbox)")
    drive_parser.add_argument("--log-dir", default="/tmp/plow-whip-logs", help="Background CLI log directory")
    drive_parser.add_argument("--project-path", help="Override project root path (when config projects_dir is wrong)")

    # permit 子命令
    permit_parser = sub.add_parser("permit", help="Set dispatch permission")
    permit_parser.add_argument("action", choices=["allow", "allow_n", "ask", "ask_n", "reject", "check"],
                               help="Permission mode")
    permit_parser.add_argument("--count", type=int, default=1,
                               help="Number of tasks (for allow_n / ask_n)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # configure, list, sync, scheduler don't need --project
    if args.command == "configure":
        cmd_configure(args)
        return
    if args.command == "list":
        cmd_list()
        return
    if args.command == "agent":
        cmd_agent(args, project=args.project)
        return
    if args.command == "brain":
        from .brain import cmd_brain
        cmd_brain(args)
        return
    if args.command == "sync":
        cmd_sync()
        return
    if args.command == "whip":
        from .whip import cmd_whip
        cmd_whip(args)
        return
    if args.command == "scheduler":
        cmd_scheduler(args)
        return
    if args.command == "health":
        cmd_health(args)
        return
    if args.command == "inbox":
        cmd_inbox(args)
        return
    if args.command == "cli-auth":
        from .cli_auth import cmd
        cmd(args)
        return
    if args.command == "permit":
        cmd_permit(args)
        return

    # Other commands need --project
    if not args.project:
        print("Error: --project required", file=sys.stderr)
        print("Example: plow-whip --project MyProject status", file=sys.stderr)
        sys.exit(1)

    project = args.project

    try:
        _require_machine_lease(project, args)
    except leases.LeaseDenied as exc:
        leases.audit(
            CONFIG_DIR, project, "lease_denied",
            operation=_machine_write_action(args), detail=str(exc),
        )
        print(json.dumps({
            "success": False,
            "status": "lease_denied",
            "project": project,
            "operation": _machine_write_action(args),
            "detail": str(exc),
        }, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(3)

    if args.command == "bind-tab":
        cmd_bind_tab(project, args.tab, args.name)
        return

    if args.command == "status":
        cmd_status(project)
    elif args.command == "doctor":
        cmd_doctor(project, args)
    elif args.command == "repair":
        cmd_repair(project, args)
    elif args.command == "start":
        cmd_start(project, args)
    elif args.command == "submit":
        cmd_submit(project, args)
    elif args.command == "desktop":
        cmd_desktop(project, args)
    elif args.command == "plan":
        cmd_plan(project, args)
    elif args.command == "review":
        cmd_review(project, args)
    elif args.command == "decision":
        cmd_decision(project, args)
    elif args.command == "automation":
        cmd_automation(project, args)
    elif args.command == "task":
        cmd_task(project, args)
    elif args.command == "goal":
        cmd_goal(project, args)
    elif args.command == "handoff":
        cmd_handoff(project, args)
    elif args.command == "reset":
        cmd_reset(project)
    elif args.command == "init":
        cmd_init(project, args)
    elif args.command == "new":
        cmd_new(project, args)
    elif args.command == "archive":
        cmd_archive(project)
    elif args.command == "watch":
        cmd_watch(project, args)
    elif args.command == "session":
        cmd_session(project, args.agent)
    elif args.command == "rotate":
        cmd_rotate(project, args.agent, args)
    elif args.command == "sessions-overview":
        cmd_sessions_overview(project)
    elif args.command == "context-pack":
        cmd_context_pack(project, args)
    elif args.command == "memory-rotate":
        cmd_memory_rotate(project, args)
    elif args.command == "memory-budget":
        cmd_memory_budget(project, args)
    elif args.command == "rotation-health":
        cmd_rotation_health(project, args)
    elif args.command == "drive":
        from .drive import cmd_drive
        cmd_drive(project, args)


# ── permit 命令 ──────────────────────────────────────────────────────────────

def cmd_permit(args):
    """处理 permit 子命令：设置 dispatch 权限"""
    from .dispatch import set_permission, check_permission, PERMISSION_MODES

    if args.action == "check":
        result = check_permission()
        print(f"当前权限: {result['action']} — {result['reason']}")
        return

    mode = args.action  # allow | allow_n | ask | ask_n | reject
    count = getattr(args, "count", 1) or 1

    if mode in ("allow_n", "ask_n") and count < 1:
        print("错误: count 必须 >= 1")
        return

    result = set_permission(mode, count)
    print(f"✅ 权限已设置: {PERMISSION_MODES.get(mode, mode)}")
    if mode in ("allow_n", "ask_n"):
        print(f"   次数: {count}")


if __name__ == "__main__":
    raise SystemExit(main())
