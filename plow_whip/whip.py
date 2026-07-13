#!/usr/bin/env python3
"""
whip.py — 耕田之鞭：主动驱动 AI agent 干活

核心功能：
  1. 扫描所有项目状态，找出当前轮到谁
  2. 检测"摸鱼"（stale）：轮到某 agent 但长时间无动作
  3. 生成可执行的"鞭策指令"（actionable prompt）
  4. 使用 --once 供系统原生调度器周期执行

用法:
    plow-whip whip                        # 扫一遍，输出谁该干活
    plow-whip whip --json                 # JSON 格式输出（给脚本用）
    plow-whip whip --agent codex          # 只鞭策指定 agent
    plow-whip whip --stale-minutes 30     # 超过30分钟算摸鱼
    plow-whip whip --once                 # 单次、加锁、适合系统调度
"""

import json
import os
import hashlib
import contextlib
import io
import sys
import uuid
from datetime import datetime, timedelta

from . import agent_flow as af
from .dispatch import dispatch, available_channels


# ── 诊断 ─────────────────────────────────────────────────────────────────────

STALE_THRESHOLD_MINUTES = 60  # 默认超过60分钟算摸鱼
WAKE_RETRY_MINUTES = 30  # 投递后任务仍无进展时允许再次续作
QUEUED_WAKE_RETRY_MINUTES = 1  # 未实际执行的投递仅等待一个调度周期
MAX_UNCHANGED_WAKE_ATTEMPTS = 3
MAX_QUEUED_WAKE_ATTEMPTS = 6

STATUS_LABEL = {
    "in_progress": "进行中",
    "done": "已完成",
    "blocked": "阻塞中",
}


def _parse_updated_at(ts: str):
    """安全解析 ISO 时间戳，返回 timezone-aware datetime。"""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        # 如果是 naive datetime，加上本地时区
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt
    except (ValueError, TypeError):
        return None


def _is_stale(state: dict, threshold_minutes: int) -> bool:
    """判断项目是否过期（轮到该 agent 但长时间无动作）。"""
    if state.get("status") in ("done", "blocked"):
        return False
    updated = _parse_updated_at(state.get("updated_at", ""))
    if updated is None:
        return True  # 从未更新过 → 也算摸鱼
    return datetime.now().astimezone() - updated > timedelta(minutes=threshold_minutes)


def _staleness_info(state: dict) -> str:
    """返回距离上次更新的可读时间描述。"""
    updated = _parse_updated_at(state.get("updated_at", ""))
    if updated is None:
        return "从未更新"
    delta = datetime.now().astimezone() - updated
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}秒前"
    if total_seconds < 3600:
        return f"{total_seconds // 60}分钟前"
    if total_seconds < 86400:
        return f"{total_seconds // 3600}小时前"
    return f"{total_seconds // 86400}天前"


# ── 核心扫描 ──────────────────────────────────────────────────────────────────

def scan_all_projects(stale_minutes: int = STALE_THRESHOLD_MINUTES) -> list:
    """
    扫描所有项目，返回每个项目的诊断结果。

    返回列表，每项包含:
      project, current_agent, status, phase, stale, staleness_info,
      next_action, task_context
    """
    projects_dir = af.get_projects_dir()
    if not os.path.isdir(projects_dir):
        return []

    results = []
    for name in sorted(os.listdir(projects_dir)):
        pdir = os.path.join(projects_dir, name)
        sf = af.state_file(name)
        if not os.path.isdir(pdir) or not os.path.exists(sf):
            continue
        if name in ("by_rm", "archive"):
            continue

        state = af.load_state(name)
        af.ensure_project_path(name, state)
        agent = state.get("current_agent", "unknown")
        status = state.get("status", "unknown")
        stale = _is_stale(state, stale_minutes)

        results.append({
            "project": name,
            "current_agent": agent,
            "status": status,
            "phase": state.get("phase", ""),
            "stale": stale,
            "staleness_info": _staleness_info(state),
            "next_action": state.get("next_action", ""),
            "task": state.get("task", {}),
            "task_context": state.get("task_context", {}),
            "project_path": af.project_dir(name),
            "updated_at": state.get("updated_at", ""),
            "zellij_tab": state.get("zellij_tab"),
        })

    return results


def probe_all_projects(stale_minutes: int = STALE_THRESHOLD_MINUTES) -> list:
    """Read only state-machine fields; never hydrate task text or collaboration context."""
    projects_dir = af.get_projects_dir()
    if not os.path.isdir(projects_dir):
        return []

    results = []
    for name in sorted(os.listdir(projects_dir)):
        pdir = os.path.join(projects_dir, name)
        sf = af.state_file(name)
        if name in ("by_rm", "archive") or not os.path.isdir(pdir) or not os.path.exists(sf):
            continue
        try:
            with open(sf, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            results.append({
                "project": name,
                "current_agent": "unknown",
                "status": "invalid_state",
                "task_id": "",
                "task_status": "unknown",
                "updated_at": "",
                "stale": True,
                "needs_recovery": True,
                "reason": f"state_error:{type(exc).__name__}",
            })
            continue

        task = state.get("task") or {}
        status = state.get("status", "unknown")
        task_status = task.get("status", "unknown")
        try:
            agent_meta = af.load_protocol(name).get("agents", {}).get(task.get("owner") or state.get("current_agent"), {})
            schedulable = agent_meta.get("schedulable", True)
        except (OSError, ValueError, json.JSONDecodeError):
            schedulable = False
        expected_status = {"active": "in_progress"}.get(task_status, task_status)
        stale = _is_stale(state, stale_minutes)
        mismatch = task_status != "unknown" and status != expected_status
        results.append({
            "project": name,
            "current_agent": state.get("current_agent", "unknown"),
            "status": status,
            "task_id": task.get("id", ""),
            "task_status": task_status,
            "automation_enabled": bool(state.get("automation_enabled", True)),
            "schedulable": schedulable,
            "updated_at": state.get("updated_at", ""),
            "stale": stale,
            "needs_recovery": schedulable and (stale or mismatch),
            "reason": "control_plane" if not schedulable else "state_mismatch" if mismatch else "stale" if stale else "healthy",
        })
    return results


def _load_recovery_project(project: str, stale_minutes: int, probe: dict) -> dict:
    """Hydrate one anomalous project only after the zero-context probe trips."""
    state = af.load_state(project)
    af.ensure_project_path(project, state)
    result = {
        "project": project,
        "current_agent": state.get("current_agent", "unknown"),
        "status": state.get("status", "unknown"),
        "phase": state.get("phase", ""),
        "stale": _is_stale(state, stale_minutes),
        "staleness_info": _staleness_info(state),
        "next_action": state.get("next_action", ""),
        "task": state.get("task", {}),
        "task_context": state.get("task_context", {}),
        "project_path": af.project_dir(project),
        "updated_at": state.get("updated_at", ""),
        "zellij_tab": state.get("zellij_tab"),
        "needs_recovery": probe.get("needs_recovery", False),
        "reason": probe.get("reason", ""),
    }
    return result


def filter_by_agent(results: list, agent: str) -> list:
    """只保留指定 agent 的项目。"""
    return [r for r in results if r["current_agent"] == agent]


def filter_active(results: list) -> list:
    """只保留可继续工作的项目；done/blocked 都不可自动派发。"""
    return [
        r for r in results
        if r["status"] not in ("done", "blocked", "blocked_waiting_human")
        and r.get("schedulable", True)
    ]


# ── 鞭策指令生成 ──────────────────────────────────────────────────────────────

def generate_whip_prompt(result: dict) -> str:
    """
    为单个项目生成一段可执行的鞭策指令文本。
    这段话会告诉 agent：你是谁、该干什么、怎么开始。
    """
    project = result["project"]
    agent = result["current_agent"]
    phase = result.get("phase", "")
    next_action = result.get("next_action", "查看状态并继续工作")
    ctx = result.get("task_context", {})
    staleness = result.get("staleness_info", "")
    project_path = result.get("project_path") or ctx.get("project_path") or af.project_dir(project)

    lines = [
        f"【鞭策指令】项目: {project}",
        f"当前轮次: {agent}",
        f"阶段: {phase}",
        f"上次更新: {staleness}",
    ]

    if ctx.get("day"):
        lines.append(f"任务: Day {ctx['day']} — {ctx.get('topic', '')}")
    if ctx.get("project_dir"):
        lines.append(f"代码: {ctx['project_dir']}")
    lines.append(f"项目路径: {project_path}")

    lines.append(f"请立即执行: {next_action}")
    lines.append("")
    lines.append("唯一启动命令:")
    lines.append(f"  plow-whip --project {project} start --agent {agent} --json")

    return "\n".join(lines)


def _wake_hash(result: dict) -> str:
    payload = {
        "project": result.get("project"),
        "agent": result.get("current_agent"),
        "phase": result.get("phase"),
        "next_action": result.get("next_action"),
        "project_path": result.get("project_path") or result.get("task_context", {}).get("project_path"),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _wake_lease_active(state: dict, wake_hash: str, retry_minutes: int = WAKE_RETRY_MINUTES) -> bool:
    """Suppress duplicate wakes briefly, but never strand an unfinished task forever."""
    if state.get("last_wake_hash") != wake_hash:
        return False
    last_woken = _parse_updated_at(state.get("last_woken_at", ""))
    if last_woken is None:
        return False
    if state.get("last_wake_status") in ("queued", "failed"):
        retry_minutes = min(retry_minutes, QUEUED_WAKE_RETRY_MINUTES)
    return datetime.now().astimezone() - last_woken < timedelta(minutes=retry_minutes)


def _record_wake(project: str, wake_hash: str, result: dict, dispatch_id: str) -> bool:
    """Merge wake metadata without allowing a concurrent Agent write to be lost."""
    for _ in range(3):
        state = af.load_state(project)
        same_action = state.get("last_wake_hash") == wake_hash
        state["last_wake_hash"] = wake_hash
        state["last_dispatch_id"] = result.get("dispatch_id", dispatch_id)
        state["last_wake_status"] = result.get("status", "accepted")
        state["last_woken_at"] = datetime.now().isoformat(timespec="seconds")
        state["wake_count"] = int(state.get("wake_count", 0)) + 1 if same_action else 1
        try:
            af.write_state(project, state, touch=False)
            return True
        except RuntimeError:
            continue
    return False


def generate_notification(result: dict) -> str:
    """生成简短的 macOS 通知文本。"""
    project = result["project"]
    agent = result["current_agent"]
    stale_tag = " 摸鱼!" if result.get("stale") else ""
    return f"[{project}] 轮到 {agent}{stale_tag} — {result.get('next_action', '继续工作')}"


# ── 命令入口 ──────────────────────────────────────────────────────────────────

def cmd_whip(args):
    """whip 子命令主入口。"""
    stale_minutes = getattr(args, "stale_minutes", None) or STALE_THRESHOLD_MINUTES
    target_agent = getattr(args, "agent", None)
    as_json = getattr(args, "json", False)
    daemon = getattr(args, "daemon", False)
    crack = getattr(args, "crack", False)
    auto_crack = getattr(args, "auto_crack", False)
    force_channel = getattr(args, "channel", None)

    auto_rotate = getattr(args, 'auto_rotate', False)
    use_brain = getattr(args, 'brain', False)

    if getattr(args, "once", False) or auto_crack or daemon:
        if auto_crack or daemon:
            print("warning: continuous whip flags are deprecated; install the native scheduler for repetition", file=sys.stderr)
        payload = run_once(
            stale_minutes=stale_minutes,
            target_agent=target_agent,
            crack=crack or auto_crack,
            force_channel=force_channel,
            auto_rotate=auto_rotate,
            force=getattr(args, "force", False),
            use_brain=use_brain,
            opt_in_only=getattr(args, "opt_in_only", False),
        )
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"whip once: {payload['status']} ({len(payload.get('projects', []))} projects)")
        return

    results = scan_all_projects(stale_minutes)
    results = filter_active(results)

    if target_agent:
        results = filter_by_agent(results, target_agent)

    if not results:
        if as_json:
            print("[]")
        else:
            print("所有项目已完成，无人需要鞭策。")
        return

    force = getattr(args, "force", False)

    if crack:
        _crack(results, force_channel, force=force)
        return

    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    _print_whip_report(results)


def _print_whip_report(results: list):
    """打印人类可读的鞭策报告。"""
    # 按 agent 分组
    by_agent = {}
    for r in results:
        by_agent.setdefault(r["current_agent"], []).append(r)

    print("\n" + "=" * 60)
    print("  上 帝 之 鞭 — AI 鞭策报告")
    print("=" * 60)

    for agent, projects in sorted(by_agent.items()):
        emoji = af.AGENT_EMOJI.get(agent, "?")
        label = af.get_agent_label(agent)
        print(f"\n{emoji} {label}（{len(projects)} 个项目）")
        print("-" * 40)

        for r in projects:
            stale_flag = " !! 摸鱼!" if r["stale"] else " [OK]"
            status_text = STATUS_LABEL.get(r["status"], r["status"])
            print(f"  {r['project']}{stale_flag}")
            print(f"     阶段: {r['phase']}  |  状态: {status_text}  |  更新: {r['staleness_info']}")
            if r.get("next_action"):
                print(f"     -> {r['next_action']}")

            # 生成鞭策指令
            if r["stale"]:
                prompt = generate_whip_prompt(r)
                print()
                for line in prompt.split("\n"):
                    print(f"     {line}")
                print()

    # 汇总
    stale_count = sum(1 for r in results if r["stale"])
    print(f"\n{'=' * 60}")
    print(f"  共 {len(results)} 个活跃项目，{stale_count} 个需要鞭策")
    print(f"{'=' * 60}\n")

    # macOS 通知
    if stale_count > 0:
        af.notify(f"{stale_count} 个项目需要鞭策!", ring=True)


def _crack(results: list, force_channel: str = None, force: bool = False, use_brain: bool = False, quiet: bool = False):
    """
    抽鞭子！将任务投递给每个摸鱼的 agent。
    对每个 stale 项目，生成 prompt 并通过 dispatch 投递。
    """
    stale = [r for r in results if r.get("stale") or r.get("needs_recovery") or force]
    if not stale:
        if not quiet:
            print("没有摸鱼项目，无需抽鞭。")
        return []

    if not quiet:
        print(f"\n  抽鞭！目标: {len(stale)} 个摸鱼项目\n")
    outcomes = []
    for r in stale:
        agent = r["current_agent"]
        project = r["project"]
        wake_hash = _wake_hash(r)
        state = af.load_state(project)
        retry_limit = (
            MAX_QUEUED_WAKE_ATTEMPTS
            if state.get("last_wake_status") in ("queued", "failed")
            else MAX_UNCHANGED_WAKE_ATTEMPTS
        )
        if (
            not force
            and state.get("last_wake_hash") == wake_hash
            and int(state.get("wake_count", 0)) >= retry_limit
        ):
            outcomes.append({"project": project, "agent": agent, "status": "paused_retry_limit"})
            continue
        if not force and _wake_lease_active(state, wake_hash):
            outcomes.append({"project": project, "agent": agent, "status": "skipped_duplicate"})
            if not quiet:
                print(f"  [{project}] -> {agent} SKIP: same task already woken")
            continue
        prompt = generate_whip_prompt(r)
        dispatch_id = f"DP-{uuid.uuid4().hex[:12]}"

        # 显示可用通道
        channels = available_channels(agent, project)
        channel_str = ", ".join(channels)
        if not quiet:
            print(f"  [{project}] -> {agent} (通道: {channel_str})")

        # 投递（如果项目绑定了 zellij tab，精准投递）
        zellij_tab = r.get("zellij_tab")
        dispatch_kwargs = {"target_tab": zellij_tab}
        if use_brain:
            dispatch_kwargs["use_brain"] = True
        result = dispatch(agent, project, prompt, force_channel, dispatch_id=dispatch_id, **dispatch_kwargs)
        status = "OK" if result["success"] else "FAIL"
        if not quiet:
            print(f"    [{status}] {result['channel']}: {result['detail']}")
        outcomes.append({"project": project, "agent": agent, **result})
        if result["success"]:
            if not _record_wake(project, wake_hash, result, dispatch_id):
                outcomes[-1]["wake_recorded"] = False
        if not quiet:
            print()
    return outcomes


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _acquire_once_lock():
    run_dir = os.path.join(af.CONFIG_DIR, "run")
    lock_dir = os.path.join(run_dir, "whip.lock")
    os.makedirs(run_dir, exist_ok=True)
    try:
        os.mkdir(lock_dir)
    except FileExistsError:
        pid_path = os.path.join(lock_dir, "pid")
        try:
            with open(pid_path, encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            pid = None
        if pid and _pid_alive(pid):
            return None
        if os.path.isfile(pid_path):
            os.unlink(pid_path)
        try:
            os.rmdir(lock_dir)
            os.mkdir(lock_dir)
        except OSError:
            return None
    with open(os.path.join(lock_dir, "pid"), "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return lock_dir


def _release_once_lock(lock_dir):
    if not lock_dir:
        return
    pid_path = os.path.join(lock_dir, "pid")
    if os.path.isfile(pid_path):
        os.unlink(pid_path)
    try:
        os.rmdir(lock_dir)
    except OSError:
        pass


def run_once(stale_minutes=STALE_THRESHOLD_MINUTES, target_agent=None, crack=False,
             force_channel=None, auto_rotate=False, force=False, use_brain=False, opt_in_only=False):
    """Run a zero-context probe; hydrate task context only for explicit recovery."""
    lock_dir = _acquire_once_lock()
    if not lock_dir:
        return {"status": "skipped_already_running", "projects": [], "dispatches": []}
    try:
        rotations = []
        desktop_sync = []
        with contextlib.redirect_stdout(io.StringIO()):
            from . import codex_desktop

            for project in af.list_collab_projects():
                desktop_sync.append(codex_desktop.sync(project, allow_env=False))
            if auto_rotate:
                for project in af.list_collab_projects():
                    summary = af.auto_rotate_all_agents(project)
                    if summary["agents"] or summary["files"]:
                        rotations.append(summary)
            results = filter_active(probe_all_projects(stale_minutes))
            if target_agent:
                results = filter_by_agent(results, target_agent)
            recovery_probes = [r for r in results if r.get("needs_recovery") or force]
            if opt_in_only and not force:
                recovery_probes = [r for r in recovery_probes if r.get("automation_enabled")]
            supervision = None
            if crack:
                from . import supervisor

                supervision = supervisor.dispatch_projects([item["project"] for item in recovery_probes])
            dispatches = supervision.get("workers", []) if supervision else []
        payload = {
            "status": "ok",
            "mode": "probe",
            "context_loaded": False,
            "model_invoked": False,
            "ran_at": datetime.now().isoformat(timespec="seconds"),
            "projects": results,
            "stale_projects": [r["project"] for r in results if r.get("stale")],
            "recovery_projects": [r["project"] for r in recovery_probes],
            "dispatches": dispatches,
            "supervision": supervision,
            "rotations": rotations,
            "desktop_sync": desktop_sync,
        }
        try:
            os.makedirs(af.CONFIG_DIR, exist_ok=True)
            af.atomic_write_json(os.path.join(af.CONFIG_DIR, "scheduler-last-run.json"), payload)
        except OSError:
            pass
        return payload
    finally:
        _release_once_lock(lock_dir)
