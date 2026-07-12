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
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".plow-whip")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

ROTATE_MAX_LINES = 100
ROTATE_MAX_KB = 8

HOT_TOKEN_BUDGET = 1200
WARM_TOKEN_BUDGET = 4000
HOT_MEMORY_FILES = [
    "AGENT_STATE.json",
    "memory/NEXT_ACTION.md",
    "memory/CURRENT_STATUS.md",
]
WARM_MEMORY_FILES = [
    "AGENT_COMMS.md",
    "memory/ROADMAP.md",
    "memory/DECISIONS.md",
]
MEMORY_TEMPLATE_MAP = [
    ("PROJECT.md.tpl", "PROJECT.md"),
    ("CURRENT_STATUS.md.tpl", "CURRENT_STATUS.md"),
    ("NEXT_ACTION.md.tpl", "NEXT_ACTION.md"),
    ("ROADMAP.md.tpl", "ROADMAP.md"),
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

GLOBAL_CONVENTIONS_BEGIN = "<!-- plow-whip:global-principles:start -->"
GLOBAL_CONVENTIONS_END = "<!-- plow-whip:global-principles:end -->"

# Agent mention patterns for activity detection
AGENT_PATTERNS = ["cursor", "cursor_cli", "qoder", "qoder_cli", "codex", "codex_cli", "@cursor", "@qoder", "@codex", "handoff", "plow-whip"]

DEFAULT_AGENTS = ["codex", "cursor", "cursor_cli", "codex_cli"]
AGENT_LABEL = {
    "cursor": "Cursor Desktop (替代 Qoder CN Desktop)",
    "cursor_cli": "Cursor CLI (替代 Qoder CLI)",
    "qoder": "Qoder CN Desktop (停用)",
    "qoder_cli": "Qoder CLI (停用)",
    "codex": "Codex Desktop (PM+架构师)",
    "codex_cli": "Codex CLI (Code Owner)",
}
AGENT_EMOJI = {
    "cursor": "🟣",
    "cursor_cli": "🟪",
    "qoder": "🔵",
    "qoder_cli": "🔷",
    "codex": "🟢",
    "codex_cli": "🟩",
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
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


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
    return cfg.get("agents", DEFAULT_AGENTS)


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
    return os.path.join(get_projects_dir(), project)


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


def extract_global_conventions_block(content):
    """Return the marked global conventions block, including markers."""
    start = content.find(GLOBAL_CONVENTIONS_BEGIN)
    end = content.find(GLOBAL_CONVENTIONS_END)
    if start == -1 or end == -1 or end < start:
        return None
    end += len(GLOBAL_CONVENTIONS_END)
    return content[start:end]


def write_conventions(target_path, project, sync_global_only=False):
    """Write CONVENTIONS.md.

    Init writes the full template. Sync updates only the marked global block so
    project-specific principles outside the block remain local and take priority.
    Legacy unmarked files are migrated without dropping their existing content.
    """
    rendered = render_template("CONVENTIONS.md.tpl", project)
    if rendered is None:
        return False

    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    if not sync_global_only or not os.path.exists(target_path):
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(rendered)
        return True

    with open(target_path, encoding="utf-8") as f:
        current = f.read()

    new_block = extract_global_conventions_block(rendered)
    old_block = extract_global_conventions_block(current)
    if not new_block:
        return False

    if old_block:
        updated = current.replace(old_block, new_block, 1)
    else:
        preserved = current.rstrip()
        updated = (
            rendered.rstrip()
            + "\n\n## 项目原则（从旧 CONVENTIONS.md 保留，优先于全局原则）\n\n"
            + "> 这是 sync 从未分层的旧文件中保留下来的项目内容。请按项目需要整理；本区内容优先于上方全局原则。\n\n"
            + preserved
            + "\n"
        )

    with open(target_path, "w", encoding="utf-8") as f:
        f.write(updated)
    return updated != current


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
    agents = get_agents()
    first_agent = agents[0] if agents else "agent"
    return {
        "current_agent": first_agent,
        "phase": "initialization",
        "status": "in_progress",
        "task_context": {"day": 0, "topic": "", "project_dir": "", "project_path": project_dir(project)},
        "agents": agents,
        "agent_meta": get_agent_meta(),
        "assigned_agent": first_agent,
        "blockers": [],
        "last_wake_hash": "",
        "last_woken_at": "",
        "wake_count": 0,
        "last_output": "",
        "files_changed": [],
        "verify_commands": [],
        "next_action": f"{first_agent} starts requirements analysis for {project}",
        "updated_at": "",
    }


def load_state(project):
    sf = state_file(project)
    if not os.path.exists(sf):
        print(f"Error: project '{project}' not found. Run: plow-whip --project {project} init", file=sys.stderr)
        sys.exit(1)
    with open(sf, encoding="utf-8") as f:
        state = json.load(f)
    base = default_state(project)
    for key in base:
        if key not in state:
            state[key] = base[key]
    ensure_project_path(project, state, write=False)
    return state


def write_state(project, state, touch=True):
    if touch:
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    sf = state_file(project)
    with open(sf, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def save_state(project, state):
    write_state(project, state, touch=True)


def ensure_project_path(project, state=None, write=True):
    state = state or load_state(project)
    ctx = state.setdefault("task_context", {})
    path = project_dir(project)
    changed = ctx.get("project_path") != path
    ctx["project_path"] = path
    if changed and write:
        write_state(project, state, touch=False)
        append_comms(project, f"目录更新：项目当前路径为 `{path}`。whip / inbox 后续按此路径唤醒。")
    return changed


def append_comms(project, message):
    path = comms_file(project)
    if not os.path.exists(path):
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n- {datetime.now().isoformat(timespec='seconds')} — {message}\n")


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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Agents — {project}\n\n")
        f.write("| Agent | Role | Assignment |\n")
        f.write("|---|---|---|\n")
        for agent in get_agents():
            role = get_agent_label(agent)
            assignment = get_agent_assignment(agent) or "—"
            f.write(f"| `{agent}` | {role} | {assignment} |\n")


def cmd_agent(args, project=None):
    cfg = load_config()
    cfg.setdefault("agent_meta", {})
    if args.action == "list":
        for agent in cfg.get("agents", DEFAULT_AGENTS):
            assignment = cfg["agent_meta"].get(agent, {}).get("assignment", "")
            suffix = f" — {assignment}" if assignment else ""
            print(f"{agent}: {get_agent_label(agent)}{suffix}")
        return

    if args.name not in cfg.get("agents", []):
        cfg.setdefault("agents", []).append(args.name)
    meta = cfg["agent_meta"].setdefault(args.name, {})
    if args.role:
        meta["role"] = args.role
    if args.assignment:
        meta["assignment"] = args.assignment
    save_config(cfg)

    if project and os.path.exists(state_file(project)):
        state = load_state(project)
        state["agents"] = cfg["agents"]
        state["agent_meta"] = cfg["agent_meta"]
        write_state(project, state, touch=False)
        write_agent_manifest(project)
        append_comms(project, f"Agent 更新：`{args.name}` = {meta.get('role', args.name)}；作业：{meta.get('assignment', '—')}。")

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

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
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
    for agent in get_agents():
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


def _archive_collab_file(project, rel_path, topic=None):
    """Archive a collab file: keep recent content, move old content to archive.
    Returns True if archived, False otherwise."""
    filepath = _collab_file_path(project, rel_path)
    if not os.path.exists(filepath):
        return False

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

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
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

    print(f"🔄 Archived: {rel_path} ({lines}L → keep {keep_n}L, archived {len(old_lines)}L)")
    return True


def auto_rotate_collab_files(project):
    """Check and rotate all tracked collab files. Returns list of rotated file names."""
    rotated = []
    for rel_path in TRACKED_COLLAB_FILES:
        if _archive_collab_file(project, rel_path, topic=rel_path.replace("/", "_")):
            rotated.append(rel_path)
    return rotated


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
    for agent in get_agents():
        needs, lines, size = _needs_rotation(project, agent)
        status = "🔴" if needs else "🟢"
        print(f"  {status} {agent:12s} — {lines} lines, {size / 1024:.1f}KB")
    rotated_agents = []
    for agent in get_agents():
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
    hot_files = [_memory_file_stats(project, rel) for rel in HOT_MEMORY_FILES]
    warm_files = [_memory_file_stats(project, rel) for rel in WARM_MEMORY_FILES]
    hot_tokens = sum(item["tokens"] for item in hot_files)
    warm_tokens = sum(item["tokens"] for item in warm_files)
    return {
        "project": project,
        "budgets": {"hot": HOT_TOKEN_BUDGET, "warm": WARM_TOKEN_BUDGET},
        "hot": {"tokens": hot_tokens, "ok": hot_tokens <= HOT_TOKEN_BUDGET, "files": hot_files},
        "warm": {"tokens": warm_tokens, "ok": warm_tokens <= WARM_TOKEN_BUDGET, "files": warm_files},
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
        "Cold:",
        f"- archived files: {cold['files']}",
        f"- stored size: {cold['bytes'] / 1024:.1f}KB",
        "",
        "Rule:",
        "- Hot should stay tiny enough to read every wakeup.",
        "- Warm is loaded only when context-pack is not enough.",
        "- Cold is searched/restored, not read wholesale.",
    ]

    over = []
    if not report["hot"]["ok"]:
        over.append("Hot exceeds budget: move detail into Warm/Cold and keep NEXT_ACTION single-task.")
    if not report["warm"]["ok"]:
        over.append("Warm exceeds budget: rotate AGENT_COMMS/DECISIONS or archive old roadmap detail.")
    if over:
        lines += ["", "Actions"] + [f"- {item}" for item in over]
    return "\n".join(lines) + "\n"


def cmd_memory_budget(project, args):
    report = build_memory_budget(project)
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_memory_budget(report), end="")


def cmd_handoff(project, args):
    state = load_state(project)
    ensure_project_path(project, state)
    current = state["current_agent"]
    agents = get_agents()
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
    os.makedirs(os.path.join(mem_dir, "adr"), exist_ok=True)
    os.makedirs(os.path.join(mem_dir, "sessions"), exist_ok=True)
    os.makedirs(os.path.join(mem_dir, "sprints", "active"), exist_ok=True)
    os.makedirs(os.path.join(mem_dir, "sprints", "archive"), exist_ok=True)

    # Render templates
    for tpl_name, out_name in MEMORY_TEMPLATE_MAP:
        write_rendered(os.path.join(mem_dir, out_name), f"memory/{tpl_name}", project)

    # Render CONVENTIONS.md
    write_conventions(os.path.join(pcd, "CONVENTIONS.md"), project)
    write_agent_manifest(project)

    # Create AGENT_COMMS.md
    write_rendered(comms_file(project), "AGENT_COMMS.md.tpl", project)

    # Create state file
    save_state(project, default_state(project))
    append_comms(project, f"项目初始化：当前路径为 `{project_dir(project)}`。")

    print(f"\n🆕 Project '{project}' initialized!")
    print(f"   collab/: {pcd}")
    print(f"   memory/: {mem_dir}")
    print(f"   state:   {sf}")
    print(f"   Run: plow-whip --project {project} handoff --output 'Start' --next 'First step'\n")


def cmd_new(project, args):
    """Create a new project directory and initialize plow-whip collaboration files."""
    pdir = project_dir(project)
    os.makedirs(pdir, exist_ok=True)
    cmd_init(project, args)

    first_action = (getattr(args, "first_action", None) or "").strip()
    owner = (getattr(args, "owner", None) or "").strip()
    if first_action or owner:
        state = load_state(project)
        if owner:
            if owner not in get_agents():
                print(f"Error: unknown owner '{owner}'. Run: plow-whip agent set {owner}", file=sys.stderr)
                sys.exit(1)
            state["current_agent"] = owner
            state["assigned_agent"] = owner
        if first_action:
            state["next_action"] = first_action
        save_state(project, state)
        append_comms(project, f"新项目一键接入：owner=`{state['current_agent']}`；next=`{state['next_action']}`。")

    print(f"   Next lightweight context:")
    print(f"   plow-whip --project {project} context-pack --agent {load_state(project)['current_agent']}\n")


def _ensure_plow_whip_structure(project):
    """Create any missing collaboration files without overwriting existing work."""
    pcd = project_collab_dir(project)
    os.makedirs(pcd, exist_ok=True)
    os.makedirs(conversations_dir(project), exist_ok=True)
    for agent in get_agents():
        agent_dir = os.path.join(conversations_dir(project), agent)
        os.makedirs(agent_dir, exist_ok=True)
        current = os.path.join(agent_dir, "current.md")
        if not os.path.exists(current):
            _write_session_template(agent, project, current)

    mem_dir = project_memory_dir(project)
    os.makedirs(mem_dir, exist_ok=True)
    os.makedirs(os.path.join(mem_dir, "adr"), exist_ok=True)
    os.makedirs(os.path.join(mem_dir, "sessions"), exist_ok=True)
    os.makedirs(os.path.join(mem_dir, "sprints", "active"), exist_ok=True)
    os.makedirs(os.path.join(mem_dir, "sprints", "archive"), exist_ok=True)

    for tpl_name, out_name in MEMORY_TEMPLATE_MAP:
        target = os.path.join(mem_dir, out_name)
        if not os.path.exists(target):
            write_rendered(target, f"memory/{tpl_name}", project)

    conventions = os.path.join(pcd, "CONVENTIONS.md")
    if not os.path.exists(conventions):
        write_conventions(conventions, project)
    if not os.path.exists(os.path.join(pcd, "AGENTS.md")):
        write_agent_manifest(project)
    if not os.path.exists(comms_file(project)):
        write_rendered(comms_file(project), "AGENT_COMMS.md.tpl", project)
    if not os.path.exists(state_file(project)):
        save_state(project, default_state(project))
        append_comms(project, f"plow-whip doctor --repair：机制缺失，已自动接入；当前路径 `{project_dir(project)}`。")


def build_doctor_report(project):
    """Check whether a project is wired into plow-whip without reading file bodies."""
    required = [
        ("collab/", project_collab_dir(project)),
        ("collab/AGENT_STATE.json", state_file(project)),
        ("collab/AGENT_COMMS.md", comms_file(project)),
        ("collab/AGENTS.md", os.path.join(project_collab_dir(project), "AGENTS.md")),
        ("collab/CONVENTIONS.md", os.path.join(project_collab_dir(project), "CONVENTIONS.md")),
        ("collab/memory/PROJECT.md", os.path.join(project_memory_dir(project), "PROJECT.md")),
        ("collab/memory/CURRENT_STATUS.md", os.path.join(project_memory_dir(project), "CURRENT_STATUS.md")),
        ("collab/memory/NEXT_ACTION.md", os.path.join(project_memory_dir(project), "NEXT_ACTION.md")),
        ("collab/memory/ROADMAP.md", os.path.join(project_memory_dir(project), "ROADMAP.md")),
        ("collab/memory/DECISIONS.md", os.path.join(project_memory_dir(project), "DECISIONS.md")),
        ("collab/memory/sessions/", os.path.join(project_memory_dir(project), "sessions")),
    ]
    for agent in get_agents():
        required.append((f"collab/conversations/{agent}/current.md", os.path.join(conversations_dir(project), agent, "current.md")))
    checks = [{"name": name, "path": path, "ok": os.path.exists(path)} for name, path in required]
    missing = [item for item in checks if not item["ok"]]
    return {
        "project": project,
        "project_path": project_dir(project),
        "ok": not missing,
        "checks": checks,
        "missing": missing,
        "next": "Run context-pack, then obey CONVENTIONS.md." if not missing else "Run doctor --repair before doing project work.",
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
    lines += ["", "## Next", report["next"]]
    return "\n".join(lines) + "\n"


def cmd_doctor(project, args):
    if getattr(args, "repair", False):
        _ensure_plow_whip_structure(project)
    report = build_doctor_report(project)
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_doctor_report(report), end="")


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


def build_context_pack(project, agent=None, max_comms=3, max_next_chars=1200):
    """Compile the smallest useful context an agent should read on wakeup."""
    state = load_state(project)
    ensure_project_path(project, state)
    agent = agent or state.get("current_agent")
    ctx = state.get("task_context", {})
    pdir = project_dir(project)
    comms = _read_text(comms_file(project))
    next_action_text = _read_text(os.path.join(project_memory_dir(project), "NEXT_ACTION.md"))
    blocks = _latest_targeted_blocks(comms, agent, limit=max_comms)

    return {
        "project": project,
        "project_path": pdir,
        "agent": agent,
        "phase": state.get("phase", ""),
        "status": state.get("status", ""),
        "updated_at": state.get("updated_at", ""),
        "next_action": state.get("next_action", ""),
        "last_output": state.get("last_output", ""),
        "blockers": state.get("blockers", []),
        "files_changed": state.get("files_changed", []),
        "verify_commands": state.get("verify_commands", []),
        "task_context": {
            "day": ctx.get("day"),
            "topic": ctx.get("topic", ""),
            "project_dir": ctx.get("project_dir", ""),
        },
        "read_first": [
            os.path.join(pdir, "collab", "CONVENTIONS.md"),
            os.path.join(pdir, "collab", "AGENT_STATE.json"),
            os.path.join(pdir, "collab", "memory", "NEXT_ACTION.md"),
        ],
        "read_if_needed": [
            os.path.join(pdir, "collab", "AGENT_COMMS.md"),
            os.path.join(pdir, "collab", "memory", "CURRENT_STATUS.md"),
            os.path.join(pdir, "collab", "memory", "DECISIONS.md"),
        ],
        "next_action_file_excerpt": _clamp_text(next_action_text, max_next_chars),
        "recent_relevant_messages": blocks,
    }


def format_context_pack(pack):
    lines = [
        f"# plow-whip Context Pack — {pack['project']}",
        "",
        f"- agent: {pack['agent']}",
        f"- phase: {pack['phase']}",
        f"- status: {pack['status']}",
        f"- updated_at: {pack['updated_at'] or '(never)'}",
        f"- project_path: {pack['project_path']}",
        "",
        "## Current Task",
        pack["next_action"] or "(none)",
        "",
        "## Last Output",
        pack["last_output"] or "(none)",
    ]
    if pack["blockers"]:
        lines += ["", "## Blockers"] + [f"- {b}" for b in pack["blockers"]]
    if pack["files_changed"]:
        lines += ["", "## Files Changed"] + [f"- {f}" for f in pack["files_changed"]]
    if pack["verify_commands"]:
        lines += ["", "## Verify"] + [f"- {c}" for c in pack["verify_commands"]]

    lines += [
        "",
        "## Read First",
    ] + [f"- {p}" for p in pack["read_first"]]

    lines += [
        "",
        "## Read If Needed",
    ] + [f"- {p}" for p in pack["read_if_needed"]]

    if pack["next_action_file_excerpt"]:
        lines += ["", "## NEXT_ACTION Excerpt", pack["next_action_file_excerpt"]]
    if pack["recent_relevant_messages"]:
        lines += ["", "## Recent Relevant Messages"]
        for msg in pack["recent_relevant_messages"]:
            lines.append(_clamp_text(msg, 1600))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def cmd_context_pack(project, args):
    agent = getattr(args, "agent", None)
    max_comms = getattr(args, "max_comms", 3) or 3
    pack = build_context_pack(project, agent=agent, max_comms=max_comms)
    if getattr(args, "json", False):
        print(json.dumps(pack, ensure_ascii=False, indent=2))
    else:
        print(format_context_pack(pack), end="")


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
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
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

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
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
    role = get_agent_label(agent)
    assignment = get_agent_assignment(agent)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {agent} Session — {project}\n\n")
        f.write(f"**AI:** {role}\n")
        if assignment:
            f.write(f"**Assignment:** {assignment}\n")
        f.write(f"**Started:** {datetime.now().strftime('%Y-%m-%d')}\n")
        f.write(f"**Topic:** —\n\n")
        f.write(f"## Previous\n- (none, new session)\n\n")
        f.write(f"## Current Tasks\n- (check NEXT_ACTION.md)\n\n")
        f.write(f"## Key Decisions\n- (decisions will be appended here)\n\n")
        f.write(f"## Outputs\n- (outputs will be appended here)\n")


def cmd_sessions_overview(project):
    print(f"\n📊 Session overview (project: {project}):\n")
    for agent in get_agents():
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
        # Sync CONVENTIONS.md
        conventions_path = os.path.join(projects_dir, p, "collab", "CONVENTIONS.md")
        if write_conventions(conventions_path, p, sync_global_only=True):
            updated.append("CONVENTIONS.md(global)")
        # Note: memory files are project-specific, NOT synced
        status = "✅ " + ", ".join(updated) if updated else "⚪ no changes"
        print(f"  {p:20s} {status}")
    print()


# ── Entry Point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="🪢 plow-whip — Multi-Agent Collaboration Framework / 耕田之鞭"
    )
    parser.add_argument("--project", "-p", help="Target project name")

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
    agent_set.add_argument("--assignment", help="Current assignment")

    # status
    sub.add_parser("status", help="View project status")

    # doctor
    doctor_parser = sub.add_parser("doctor", help="Check plow-whip mechanism and optionally repair it")
    doctor_parser.add_argument("--repair", action="store_true", help="Create missing plow-whip files without overwriting")
    doctor_parser.add_argument("--json", action="store_true", help="Output JSON")

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
    cp = sub.add_parser("context-pack", help="Print a minimal wakeup context pack")
    cp.add_argument("--agent", help="Target agent (default: current_agent)")
    cp.add_argument("--max-comms", type=int, default=3, help="Recent relevant message blocks (default 3)")
    cp.add_argument("--json", action="store_true", help="Output JSON")

    # memory-rotate — check and rotate all collab/memory files
    mr = sub.add_parser("memory-rotate", help="Check and rotate all collab/memory files")
    mr.add_argument("--scan", action="store_true", help="Scan only, show activity without rotating")

    # memory-budget — token budget for Hot/Warm/Cold memory
    mb = sub.add_parser("memory-budget", help="Show Hot/Warm/Cold token budget")
    mb.add_argument("--json", action="store_true", help="Output JSON")

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
    whip_parser.add_argument("--auto-crack", action="store_true", help="Auto-crack mode: continuously scan and dispatch")
    whip_parser.add_argument("--channel", choices=["zellij", "file", "notify"], help="Force specific dispatch channel")
    whip_parser.add_argument("--daemon", action="store_true", help="Continuous monitoring mode")
    whip_parser.add_argument("--interval", type=int, default=300, help="Daemon poll interval in seconds (default 300)")
    whip_parser.add_argument("--force", action="store_true", help="Force dispatch even if project is not stale")
    whip_parser.add_argument("--auto-rotate", action="store_true", help="Auto-rotate sessions that exceed size thresholds")
    whip_parser.add_argument("--brain", action="store_true", help="Use DeepSeek brain for simple tasks before dispatching")

    # drive — Desktop 驱使 CLI（Codex / Cursor 编排者）
    drive_parser = sub.add_parser("drive", help="Drive cursor_cli / codex_cli (for Desktop orchestrators)")
    drive_parser.add_argument("target_agent", choices=["cursor_cli", "codex_cli"], help="CLI agent to drive")
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

    # configure and list and sync don't need --project
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
    if args.command == "permit":
        cmd_permit(args)
        return

    # Other commands need --project
    if not args.project:
        print("Error: --project required", file=sys.stderr)
        print("Example: plow-whip --project MyProject status", file=sys.stderr)
        sys.exit(1)

    project = args.project

    if args.command == "bind-tab":
        cmd_bind_tab(project, args.tab, args.name)
        return

    if args.command == "status":
        cmd_status(project)
    elif args.command == "doctor":
        cmd_doctor(project, args)
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
