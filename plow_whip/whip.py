#!/usr/bin/env python3
"""
whip.py — 上帝之鞭：主动驱动 AI agent 干活

核心功能：
  1. 扫描所有项目状态，找出当前轮到谁
  2. 检测"摸鱼"（stale）：轮到某 agent 但长时间无动作
  3. 生成可执行的"鞭策指令"（actionable prompt）
  4. 支持 --daemon 持续监控并周期性鞭策

用法:
    plow-whip whip                        # 扫一遍，输出谁该干活
    plow-whip whip --json                 # JSON 格式输出（给脚本用）
    plow-whip whip --agent codex          # 只鞭策指定 agent
    plow-whip whip --stale-minutes 30     # 超过30分钟算摸鱼
    plow-whip whip --daemon               # 持续监控模式
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta

from . import agent_flow as af
from .dispatch import dispatch, available_channels


# ── 诊断 ─────────────────────────────────────────────────────────────────────

STALE_THRESHOLD_MINUTES = 60  # 默认超过60分钟算摸鱼

STATUS_LABEL = {
    "in_progress": "进行中",
    "done": "已完成",
    "blocked": "阻塞中",
}


def _parse_updated_at(ts: str):
    """安全解析 ISO 时间戳。"""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
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
            "task_context": state.get("task_context", {}),
            "updated_at": state.get("updated_at", ""),
            "zellij_tab": state.get("zellij_tab"),
        })

    return results


def filter_by_agent(results: list, agent: str) -> list:
    """只保留指定 agent 的项目。"""
    return [r for r in results if r["current_agent"] == agent]


def filter_active(results: list) -> list:
    """只保留未完成的项目。"""
    return [r for r in results if r["status"] != "done"]


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

    lines.append(f"请立即执行: {next_action}")
    lines.append("")
    lines.append(f"快速恢复命令:")
    lines.append(f"  plow-whip --project {project} status")
    lines.append(f"  plow-whip --project {project} handoff --output '...' --next '...'")

    return "\n".join(lines)


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
    daemon_interval = getattr(args, "interval", 300)  # 默认5分钟
    crack = getattr(args, "crack", False)
    auto_crack = getattr(args, "auto_crack", False)
    force_channel = getattr(args, "channel", None)

    if auto_crack:
        _auto_crack_loop(stale_minutes, target_agent, daemon_interval, force_channel)
        return

    results = scan_all_projects(stale_minutes)
    results = filter_active(results)

    if target_agent:
        results = filter_by_agent(results, target_agent)

    if not results:
        print("所有项目已完成，无人需要鞭策。")
        return

    if crack:
        _crack(results, force_channel)
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
        label = af.AGENT_LABEL.get(agent, agent)
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


def _crack(results: list, force_channel: str = None):
    """
    抽鞭子！将任务投递给每个摸鱼的 agent。
    对每个 stale 项目，生成 prompt 并通过 dispatch 投递。
    """
    stale = [r for r in results if r.get("stale")]
    if not stale:
        print("没有摸鱼项目，无需抽鞭。")
        return

    print(f"\n  抽鞭！目标: {len(stale)} 个摸鱼项目\n")
    for r in stale:
        agent = r["current_agent"]
        project = r["project"]
        prompt = generate_whip_prompt(r)

        # 显示可用通道
        channels = available_channels(agent)
        channel_str = ", ".join(channels)
        print(f"  [{project}] -> {agent} (通道: {channel_str})")

        # 投递（如果项目绑定了 zellij tab，精准投递）
        zellij_tab = r.get("zellij_tab")
        result = dispatch(agent, project, prompt, force_channel, target_tab=zellij_tab)
        status = "OK" if result["success"] else "FAIL"
        print(f"    [{status}] {result['channel']}: {result['detail']}")
        print()


def _auto_crack_loop(stale_minutes, target_agent, interval, force_channel):
    """
    自动挥舞模式：持续扫描，发现摸鱼就自动抽鞭。
    这是真正的"上帝之鞭" — 不需要人介入，自动驱动 agent 干活。
    """
    print("  上帝之鞭 — 自动挥舞模式")
    print(f"   摸鱼阈值: {stale_minutes} 分钟")
    print(f"   轮询间隔: {interval} 秒")
    if target_agent:
        print(f"   目标: 只鞭策 {target_agent}")
    if force_channel:
        print(f"   通道: 强制 {force_channel}")
    print("   Ctrl+C 收起鞭子\n")

    try:
        while True:
            results = scan_all_projects(stale_minutes)
            results = filter_active(results)
            if target_agent:
                results = filter_by_agent(results, target_agent)

            stale = [r for r in results if r["stale"]]
            if stale:
                now = datetime.now().strftime("%H:%M:%S")
                print(f"\n[{now}] 发现 {len(stale)} 个摸鱼项目，抽鞭中...")
                _crack(stale, force_channel)
            else:
                now = datetime.now().strftime("%H:%M:%S")
                print(f"[{now}] 暂无摸鱼项目，鞭子休息中")

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n  上帝之鞭已收起")


def _daemon_loop(stale_minutes, target_agent, as_json, interval):
    """持续监控模式。"""
    print("  上帝之鞭 — 持续监控模式")
    print(f"   摸鱼阈值: {stale_minutes} 分钟")
    print(f"   轮询间隔: {interval} 秒")
    if target_agent:
        print(f"   目标: 只鞭策 {target_agent}")
    print("   Ctrl+C 退出\n")

    try:
        while True:
            results = scan_all_projects(stale_minutes)
            results = filter_active(results)
            if target_agent:
                results = filter_by_agent(results, target_agent)

            stale = [r for r in results if r["stale"]]
            if stale:
                if as_json:
                    print(json.dumps(stale, ensure_ascii=False, indent=2))
                else:
                    now = datetime.now().strftime("%H:%M:%S")
                    print(f"\n[{now}] 发现 {len(stale)} 个摸鱼项目:")
                    for r in stale:
                        msg = generate_notification(r)
                        print(f"   {msg}")
                        af.notify(msg, ring=False)
            else:
                now = datetime.now().strftime("%H:%M:%S")
                print(f"[{now}] 暂无摸鱼项目")

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n  上帝之鞭已收起")
