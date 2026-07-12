#!/usr/bin/env python3
"""
drive.py — Desktop 编排者驱使 CLI agent（供 Codex / Cursor Desktop 调用）

用法:
    plow-whip --project MyProject drive cursor_cli --next "实现登录 API"
    plow-whip --project MyProject drive cursor_cli --status
    plow-whip --project MyProject drive codex_cli --next "写单元测试"

默认后台启动 CLI + 写入 inbox 双保险；立即返回 log 路径与盯梢命令。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime

from . import agent_flow as af
from .dispatch import _dispatch_file, dispatch, read_inbox

DEFAULT_LOG_DIR = "/tmp/plow-whip-logs"
CLI_AGENTS = ("cursor_cli", "codex_cli")


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
) -> str:
    """生成给 CLI agent 的短 prompt（附路径，不贴全文）。"""
    project_path = project_path or resolve_project_path(project)
    role_hint = {
        "cursor_cli": "Cursor CLI 打工仔",
        "codex_cli": "Codex CLI Code Owner",
    }.get(target_agent, target_agent)

    lines = [
        f"plow-whip drive: {project} 项目 — {requested_by} 请你（{target_agent} / {role_hint}）执行。",
        "",
        f"任务: {task.strip()}",
        "",
        "启动自检（若未做）:",
        f"  1. 优先运行: python3 -m plow_whip.agent_flow --project {project} context-pack --agent {target_agent}",
        f"  2. 读 {project_path}/collab/CONVENTIONS.md（先遵守 P0 by_rm，禁止 rm）",
        f"  3. 只在 context-pack 不够时读 AGENT_COMMS.md / DECISIONS.md 原文",
        "",
        "完成后:",
        "  1. 写进度到 collab/AGENT_COMMS.md",
        "  2. plow-whip handoff（如需要）",
        f"  3. 清空 ~/.plow-whip/inbox/{target_agent}.json",
    ]
    return "\n".join(lines)


def _log_path(log_dir: str, agent: str) -> str:
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(log_dir, f"{agent}_{ts}.log")


def _cursor_cli_cmd(project_path: str, prompt: str) -> list[str] | None:
    agent_bin = shutil.which("cursor-agent")
    if not agent_bin:
        return None
    return [
        agent_bin, "--print", "--force", "--trust",
        "--workspace", project_path, prompt,
    ]


def _codex_cli_cmd(project_path: str, prompt: str) -> list[str]:
    npx = shutil.which("npx") or "npx"
    return [
        npx, "-y", "@openai/codex",
        "-a", "never",
        "-s", "workspace-write",
        "-C", project_path,
        "exec", "--ephemeral",
        prompt,
    ]


def spawn_cli_background(
    target_agent: str,
    project: str,
    prompt: str,
    log_dir: str = DEFAULT_LOG_DIR,
    project_path: str | None = None,
) -> dict:
    """后台启动 CLI 进程，日志写入文件。立即返回，不阻塞。"""
    project_path = project_path or resolve_project_path(project)
    if not os.path.isdir(project_path):
        return {"success": False, "detail": f"项目路径不存在: {project_path}"}

    log_file = _log_path(log_dir, target_agent)
    if target_agent == "cursor_cli":
        cmd = _cursor_cli_cmd(project_path, prompt)
        if not cmd:
            return {"success": False, "detail": "cursor-agent 未安装"}
    elif target_agent == "codex_cli":
        cmd = _codex_cli_cmd(project_path, prompt)
    else:
        return {"success": False, "detail": f"不支持的 CLI agent: {target_agent}"}

    with open(log_file, "ab") as lf:
        lf.write(f"=== plow-whip drive start {datetime.now().isoformat()} ===\n".encode())
    log_fd = open(log_file, "ab")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=project_path,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        log_fd.close()
        return {"success": False, "detail": str(exc)}

    # 父进程关闭 fd；子进程已继承
    log_fd.close()

    return {
        "success": True,
        "pid": proc.pid,
        "log_file": log_file,
        "cmd": " ".join(cmd[:4]) + " ...",
    }


def cmd_drive(project: str, args) -> None:
    """drive 子命令：驱使 cursor_cli / codex_cli。"""
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
    prompt = build_drive_prompt(
        project, target, task, requested_by=requested_by, project_path=project_path,
    )
    channel = getattr(args, "channel", "auto") or "auto"
    log_dir = getattr(args, "log_dir", DEFAULT_LOG_DIR) or DEFAULT_LOG_DIR
    foreground = getattr(args, "foreground", False)

    print(f"\n🪢 plow-whip drive → {target} (project: {project})")
    print(f"📋 任务: {task[:120]}{'...' if len(task) > 120 else ''}")
    print(f"📤 请求方: {requested_by}")
    print(f"📍 路径: {project_path}\n")

    # 1) 始终写 inbox（Codex 可只靠 file 通道派活）
    inbox_result = _dispatch_file(target, prompt, project)
    print(f"  [{'OK' if inbox_result['success'] else 'FAIL'}] inbox: {inbox_result['detail']}")

    spawn_result = None
    if channel in ("auto", "cursor_cli", "codex_cli") and not foreground:
        if channel == "auto" or channel == target:
            spawn_result = spawn_cli_background(
                target, project, prompt, log_dir, project_path=project_path,
            )
            if spawn_result["success"]:
                print(f"  [OK] background: pid={spawn_result['pid']}")
                print(f"       log: {spawn_result['log_file']}")
            else:
                print(f"  [WARN] background: {spawn_result['detail']}")

    if foreground:
        force = target if channel in ("auto", target) else channel
        if force in ("cursor_cli", "codex_cli"):
            result = dispatch(target, project, prompt, force_channel=force)
            status = "OK" if result["success"] else "FAIL"
            print(f"  [{status}] foreground/{result['channel']}: {result['detail']}")
            if result.get("output"):
                print(result["output"][:500])
        else:
            print(f"  [SKIP] foreground 需要 cursor_cli 或 codex_cli 通道")

    print("\n📌 盯结果（省 token）:")
    print(f"  sleep 60 && tail -10 {log_dir}/{target}_*.log 2>/dev/null | tail -10")
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
    pending = [t for t in read_inbox(target_agent) if t.get("status") == "pending"]
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
