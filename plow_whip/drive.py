#!/usr/bin/env python3
"""
drive.py — 让逻辑 Agent 通过其注册的执行 Driver 工作

用法:
    plow-whip --project MyProject drive backend-primary --next "实现登录 API"
    plow-whip --project MyProject drive backend-primary --status

统一通过 dispatch 启动或恢复任务绑定的 CLI 会话，并写入 inbox 生命周期。
"""

from __future__ import annotations

import os
import sys
import uuid

from . import agent_flow as af
from .dispatch import dispatch, read_inbox

DEFAULT_LOG_DIR = "/tmp/plow-whip-logs"
def resolve_project_path(project: str, override: str | None = None) -> str:
    """解析项目根路径：--project-path > 配置目录 > cwd > 环境变量。"""
    if override:
        return os.path.abspath(override)
    computed = af.project_dir(project)
    if os.path.isdir(computed):
        return computed
    cwd = os.getcwd()
    if os.path.isfile(os.path.join(cwd, "collab", "AGENT_STATE.json")):
        return cwd
    env_path = os.environ.get("PLOW_WHIP_PROJECT_PATH", "").strip()
    if env_path and os.path.isdir(env_path):
        return os.path.abspath(env_path)
    return computed


def build_drive_prompt(
    project: str,
    target_agent: str,
    task: str,
    requested_by: str = "codex",
    project_path: str | None = None,
    dispatch_id: str | None = None,
) -> str:
    """生成给 CLI agent 的短 prompt（附路径，不贴全文）。"""
    project_path = project_path or resolve_project_path(project)
    role_hint = target_agent
    if os.path.exists(af.protocol_file(project)):
        role_hint = af.load_protocol(project).get("agents", {}).get(target_agent, {}).get("role", target_agent)
    dispatch_id = dispatch_id or "<dispatch-id>"

    lines = [
        f"plow-whip drive: {project} 项目 — {requested_by} 请你（{target_agent} / {role_hint}）执行。",
        "",
        f"任务: {task.strip()}",
        "",
        "唯一启动入口:",
        f"  python3 -m plow_whip.agent_flow --project {project} start --agent {target_agent} --json",
        "",
        "完成后:",
        f"  1. plow-whip --project {project} task progress --output '...' --next '...'",
        f"     代码任务还要通过 task progress --verify '<command>' 写入真实验收命令。",
        f"  2. 当前里程碑完成时运行 plow-whip --project {project} task complete --output '...'（系统验收并自动接力）",
        f"  3. 若当前任务是 PLAN：普通 Task 使用 start --json 返回的 plan_propose；旧 Goal 仅使用 goal_plan",
        "  投递 lifecycle 由父调度器回写，不要在子 Agent 内重复更新 inbox。",
    ]
    try:
        current_task = af.load_state(project).get("task", {})
    except (OSError, SystemExit):
        current_task = {}
    if current_task.get("stage") == "review":
        lines += [
            "",
            "独立 Reviewer 规则:",
            "  - 只读审查，不要修改实现文件。",
            f"  - 验收通过：plow-whip --project {project} task complete --output 'approved: ...'",
            f"  - 验收拒绝：plow-whip --project {project} review reject --reason '...'",
            "  - 拒绝后系统会恢复原执行器的原 Session 修复。",
        ]
    return "\n".join(lines)


def spawn_cli_background(
    target_agent: str,
    project: str,
    prompt: str,
    log_dir: str = DEFAULT_LOG_DIR,
    project_path: str | None = None,
) -> dict:
    """Legacy entrypoint: direct spawning would bypass task/session binding."""
    return {
        "success": False,
        "detail": "direct background spawn disabled; use dispatch() so the task session ID is captured",
    }


def cmd_drive(project: str, args) -> None:
    """Drive any registered logical Agent through its configured Driver."""
    target = args.target_agent

    if getattr(args, "status", False):
        cmd_drive_status(project, target, getattr(args, "log_dir", DEFAULT_LOG_DIR))
        return

    task = (getattr(args, "next", None) or "").strip()
    if getattr(args, "prompt_file", None):
        with open(args.prompt_file, encoding="utf-8") as f:
            task = f.read().strip()
    if not task:
        state = af.load_state(project)
        task = (state.get("next_action") or "").strip()
    if not task:
        print("错误: 请提供 --next、--prompt-file，或确保 AGENT_STATE.json 有 next_action", file=sys.stderr)
        sys.exit(1)

    requested_by = getattr(args, "from_agent", None) or "codex"
    project_path = resolve_project_path(project, getattr(args, "project_path", None))
    dispatch_id = f"DP-{uuid.uuid4().hex[:12]}"
    prompt = build_drive_prompt(
        project, target, task, requested_by=requested_by, project_path=project_path, dispatch_id=dispatch_id,
    )
    channel = getattr(args, "channel", "auto") or "auto"

    from . import codex_desktop

    foreground = bool(getattr(args, "foreground", False))
    if foreground and codex_desktop.authorize_control(project):
        print(
            "错误: controller 禁止前台 babysit 执行会话；请移除 --foreground，派发落盘后立即结束 turn。",
            file=sys.stderr,
        )
        raise SystemExit(2)

    print(f"\n🪢 plow-whip drive → {target} (project: {project})")
    print(f"📋 任务: {task[:120]}{'...' if len(task) > 120 else ''}")
    print(f"📤 请求方: {requested_by}")
    print(f"📍 路径: {project_path}\n")

    if foreground:
        force = None if channel == "auto" else channel
        result = dispatch(
            target, project, prompt, force_channel=force, dispatch_id=dispatch_id,
        )
    else:
        state = af.load_state(project)
        task = state.get("task") or {}
        if task.get("owner") != target:
            print(
                f"错误: 当前 Task owner={task.get('owner')}，不能短路派发给 {target}；请先用 handoff/submit 更新任务真源。",
                file=sys.stderr,
            )
            raise SystemExit(2)
        driver = af.load_protocol(project).get("agents", {}).get(target, {}).get("driver", "file")
        if driver in ("codex_cli", "cursor_cli", "simple_tasker"):
            from . import supervisor

            outcome = supervisor.dispatch_projects([project])
            result = next(
                (item for item in outcome.get("workers", []) if item.get("project") == project),
                {"status": "queued"},
            )
            result = {
                "success": result.get("status") in ("started", "skipped_running"),
                "channel": driver,
                "detail": result.get("status", "queued"),
                **result,
            }
        else:
            result = dispatch(
                target, project, prompt, force_channel="file", dispatch_id=dispatch_id,
            )
    status = "OK" if result["success"] else "FAIL"
    print(f"  [{status}] {result['channel']}: {result['detail']}")
    if result.get("session_id"):
        print(f"       session: {result['session_id']}")
    if result.get("output"):
        print(result["output"][-500:])

    print("\n📌 盯结果（省 token）:")
    print(f"  plow-whip inbox list --agent {target}")
    print(f"  plow-whip --project {project} drive {target} --status")
    print(f"  plow-whip --project {project} status")
    print()


def cmd_drive_status(project: str, target_agent: str, log_dir: str = DEFAULT_LOG_DIR) -> None:
    """查看 CLI 驱使结果：日志尾部 + inbox + 状态机。"""
    print(f"\n👀 drive status — {target_agent} (project: {project})\n")

    # 最新日志
    if os.path.isdir(log_dir):
        logs = sorted(
            [f for f in os.listdir(log_dir) if f.startswith(f"{target_agent}_") and f.endswith(".log")],
            reverse=True,
        )
        if logs:
            latest = os.path.join(log_dir, logs[0])
            print(f"📝 最新日志: {latest}")
            try:
                with open(latest, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                for line in lines[-10:]:
                    print(f"   {line.rstrip()}")
            except OSError as exc:
                print(f"   (无法读取: {exc})")
        else:
            print(f"📝 无日志: {log_dir}/{target_agent}_*.log")
    else:
        print(f"📝 日志目录不存在: {log_dir}")

    # inbox
    pending = [t for t in read_inbox(target_agent) if t.get("status") in ("pending", "queued")]
    print(f"\n📥 inbox pending: {len(pending)} 条")

    # 状态机
    sf = af.state_file(project)
    if os.path.exists(sf):
        state = af.load_state(project)
        print(f"🤖 current_agent: {state.get('current_agent')}")
        print(f"   status: {state.get('status')}")
        print(f"   next_action: {(state.get('next_action') or '')[:80]}")

    comms = os.path.join(af.project_collab_dir(project), "AGENT_COMMS.md")
    if os.path.exists(comms):
        print(f"\n💬 检查 Acknowledgments:")
        print(f"   grep '{target_agent}' {comms}")
    print()
