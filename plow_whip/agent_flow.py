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

# Agent mention patterns for activity detection
AGENT_PATTERNS = ["qoder", "qoder_cli", "codex", "codex_cli", "@qoder", "@codex", "handoff", "plow-whip"]

DEFAULT_AGENTS = ["qoder", "qoder_cli", "codex", "codex_cli"]
AGENT_LABEL = {"qoder": "Qoder CN (PM+架构师)", "qoder_cli": "Qoder CLI (审查+验收)", "codex": "Codex (闲置/学习)", "codex_cli": "Codex CLI (Code Owner)"}
AGENT_EMOJI = {
    "qoder": "🔵",
    "qoder_cli": "🔷",
    "codex": "🟢",
    "codex_cli": "🟩",
}


def load_config():
    """Load config from ~/.plow-whip/config.json, create default if missing."""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"projects_dir": "", "agents": DEFAULT_AGENTS}


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


# ── Path Resolution ────────────────────────────────────────────────────────────

def project_collab_dir(project):
    return os.path.join(get_projects_dir(), project, "collab")


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
    return {
        "current_agent": "qoder",
        "phase": "initialization",
        "status": "in_progress",
        "task_context": {"day": 0, "topic": "", "project_dir": ""},
        "last_output": "",
        "files_changed": [],
        "verify_commands": [],
        "next_action": f"Qoder starts requirements analysis for {project}",
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
    return state


def save_state(project, state):
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    sf = state_file(project)
    with open(sf, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_configure(args):
    """Configure plow-whip settings."""
    cfg = load_config()
    if args.projects_dir:
        cfg["projects_dir"] = os.path.abspath(args.projects_dir)
    if args.agents:
        cfg["agents"] = args.agents
    save_config(cfg)
    print(f"\n🪢 plow-whip configured:")
    print(f"   projects_dir: {cfg.get('projects_dir', '(not set)')}")
    print(f"   agents: {cfg.get('agents', DEFAULT_AGENTS)}")
    print(f"   config: {CONFIG_FILE}\n")


def cmd_status(project):
    state = load_state(project)
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

        # Highlight untracked but active files
        untracked_active = [r for r in results if not r["is_tracked"] and r["is_active"]]
        if untracked_active:
            print(f"\n  ⚠️  {len(untracked_active)} untracked but active .md file(s):")
            for r in untracked_active:
                print(f"     → {r['file']} (agents: {', '.join(r['active_agents'][:3])})")
            print(f"     Consider adding to TRACKED_COLLAB_FILES for auto-rotation.")
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
            print(f"     → {r['file']} ({r['lines']}L, agents: {', '.join(r['active_agents'][:3])})")
        print(f"     Run with --scan to review and decide.")
    else:
        print(f"  ✅ All active .md files are tracked")
    print()


def cmd_handoff(project, args):
    state = load_state(project)
    current = state["current_agent"]
    agents = get_agents()
    idx = agents.index(current) if current in agents else 0
    next_agent = agents[(idx + 1) % len(agents)]

    state["current_agent"] = next_agent
    if args.phase:
        state["phase"] = args.phase
    state["status"] = args.status
    state["last_output"] = args.output
    state["next_action"] = args.next or ""
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
    memory_templates = [
        ("PROJECT.md.tpl", "PROJECT.md"),
        ("CURRENT_STATUS.md.tpl", "CURRENT_STATUS.md"),
        ("NEXT_ACTION.md.tpl", "NEXT_ACTION.md"),
        ("ROADMAP.md.tpl", "ROADMAP.md"),
        ("DECISIONS.md.tpl", "DECISIONS.md"),
    ]
    for tpl_name, out_name in memory_templates:
        write_rendered(os.path.join(mem_dir, out_name), f"memory/{tpl_name}", project)

    # Render CONVENTIONS.md
    write_rendered(os.path.join(pcd, "CONVENTIONS.md"), "CONVENTIONS.md.tpl", project)

    # Create AGENT_COMMS.md
    write_rendered(comms_file(project), "AGENT_COMMS.md.tpl", project)

    # Create state file
    save_state(project, default_state(project))

    print(f"\n🆕 Project '{project}' initialized!")
    print(f"   collab/: {pcd}")
    print(f"   memory/: {mem_dir}")
    print(f"   state:   {sf}")
    print(f"   Run: plow-whip --project {project} handoff --output 'Start' --next 'First step'\n")


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
    role_map = {
        "qoder": "Qoder (PM + Architect)",
        "codex": "Codex (Code Owner)",
        "cursor": "Cursor (Bug Reporter)",
    }
    role = role_map.get(agent, agent)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {role.split(' ')[0]} Session — {project}\n\n")
        f.write(f"**AI:** {role}\n")
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
        if write_rendered(conventions_path, "CONVENTIONS.md.tpl", p):
            updated.append("CONVENTIONS.md")
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

    # status
    sub.add_parser("status", help="View project status")

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

    # reset
    sub.add_parser("reset", help="Reset project state")

    # init
    sub.add_parser("init", help="Initialize new project")

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

    # memory-rotate — check and rotate all collab/memory files
    mr = sub.add_parser("memory-rotate", help="Check and rotate all collab/memory files")
    mr.add_argument("--scan", action="store_true", help="Scan only, show activity without rotating")

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


    # permit 子命令
    permit_parser = sub.add_parser("permit", help="Set dispatch permission")
    permit_parser.add_argument("action", choices=["allow", "allow_n", "ask", "ask_n", "reject", "check"],
                               help="Permission mode")
    permit_parser.add_argument("--count", type=int, default=1,
                               help="Number of tasks (for allow_n / ask_n)")
    permit_parser.set_defaults(func=cmd_permit)

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
    elif args.command == "handoff":
        cmd_handoff(project, args)
    elif args.command == "reset":
        cmd_reset(project)
    elif args.command == "init":
        cmd_init(project, args)
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
    elif args.command == "memory-rotate":
        cmd_memory_rotate(project, args)


if __name__ == "__main__":
    raise SystemExit(main())


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
