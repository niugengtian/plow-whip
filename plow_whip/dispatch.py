#!/usr/bin/env python3
"""
dispatch.py — 鞭子本体：将任务投递给指定 AI agent

三种投递方式（按优先级）：
  1. zellij  — 直接往共享终端注入命令（实时，agent 立即看到）
  2. file    — 写入任务收件箱文件（agent 启动时读取）
  3. notify  — macOS 通知（最后手段，提醒人来转达）

用法:
    from plow_whip.dispatch import dispatch
    dispatch("codex", project="MyProject", prompt="实现登录功能")
"""

import json
import os
import subprocess
import sys
from datetime import datetime

from . import agent_flow as af
from .brain import Brain, classify_complexity


# ── 配置 ─────────────────────────────────────────────────────────────────────

ZELLIJ_SESSION = "shared"
INBOX_DIR = os.path.join(os.path.expanduser("~"), ".plow-whip", "inbox")


def _ensure_inbox():
    os.makedirs(INBOX_DIR, exist_ok=True)

# ── 权限控制 ──────────────────────────────────────────────────────────────────

# 权限模式
PERMISSION_MODES = {
    "allow": "本次允许",
    "allow_n": "后面 N 个允许",
    "ask": "本次需要询问",
    "ask_n": "后面 N 个需要询问",
    "reject": "本次拒绝",
}

# 权限状态（存储在 ~/.plow-whip/permissions.json）
_permission_state = {
    "allow_remaining": 0,  # 剩余允许次数
    "ask_remaining": 0,    # 剩余询问次数
}

def _load_permissions():
    """加载权限状态"""
    global _permission_state
    perm_file = os.path.join(os.path.expanduser("~"), ".plow-whip", "permissions.json")
    if os.path.exists(perm_file):
        try:
            with open(perm_file, encoding="utf-8") as f:
                _permission_state.update(json.load(f))
        except (json.JSONDecodeError, IOError):
            pass
    return _permission_state

def _save_permissions():
    """保存权限状态"""
    perm_dir = os.path.join(os.path.expanduser("~"), ".plow-whip")
    os.makedirs(perm_dir, exist_ok=True)
    perm_file = os.path.join(perm_dir, "permissions.json")
    with open(perm_file, "w", encoding="utf-8") as f:
        json.dump(_permission_state, f, ensure_ascii=False, indent=2)

def set_permission(mode: str, count: int = 1):
    """
    设置权限模式
    
    参数:
      mode: allow | allow_n | ask | ask_n | reject
      count: 用于 allow_n / ask_n 的次数
    """
    _load_permissions()
    if mode == "allow":
        _permission_state["allow_remaining"] = 1
        _permission_state["ask_remaining"] = 0
    elif mode == "allow_n":
        _permission_state["allow_remaining"] = count
        _permission_state["ask_remaining"] = 0
    elif mode == "ask":
        _permission_state["allow_remaining"] = 0
        _permission_state["ask_remaining"] = 1
    elif mode == "ask_n":
        _permission_state["allow_remaining"] = 0
        _permission_state["ask_remaining"] = count
    elif mode == "reject":
        _permission_state["allow_remaining"] = 0
        _permission_state["ask_remaining"] = 0
    _save_permissions()
    return {"mode": mode, "count": count}

def check_permission() -> dict:
    """
    检查当前权限状态
    
    返回:
      {"action": "allow" | "ask" | "reject", "reason": str}
    """
    _load_permissions()
    
    if _permission_state["allow_remaining"] > 0:
        _permission_state["allow_remaining"] -= 1
        _save_permissions()
        return {"action": "allow", "reason": f"允许（剩余 {_permission_state['allow_remaining']} 次）"}
    
    if _permission_state["ask_remaining"] > 0:
        _permission_state["ask_remaining"] -= 1
        _save_permissions()
        return {"action": "ask", "reason": f"需要询问（剩余 {_permission_state['ask_remaining']} 次）"}
    
    # 默认需要询问
    return {"action": "ask", "reason": "默认需要确认"}




# ── 检测可用通道 ─────────────────────────────────────────────────────────────

def _zellij_available() -> bool:
    """检查 zellij 是否可用且 shared 会话存在。"""
    try:
        result = subprocess.run(
            ["zellij", "list-sessions"],
            capture_output=True, text=True, timeout=3,
        )
        return ZELLIJ_SESSION in result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _agent_cli_available(agent: str) -> bool:
    """检查 agent 的 CLI 是否可用。"""
    cli_map = {
        "codex": "codex",
        "codex_cli": "codex",
        "cursor": "cursor",
        "qoder": "qoderclicn",
        "qoder_cli": "qoderclicn",
    }
    cmd = cli_map.get(agent)
    if not cmd:
        return False
    try:
        subprocess.run(
            ["which", cmd],
            capture_output=True, timeout=3,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def available_channels(agent: str) -> list:
    """返回指定 agent 当前可用的投递通道列表（按优先级）。"""
    channels = []
    
    # CLI 通道（最优，真正自动唤醒）
    if agent in ("qoder", "qoder_cli") and _agent_cli_available("qoder"):
        channels.append("qoder_cli")
    if agent in ("codex", "codex_cli") and _agent_cli_available("codex"):
        channels.append("codex_cli")
    
    # zellij 通道（qoder 专属）
    if agent in ("qoder", "qoder_cli") and _zellij_available():
        channels.append("zellij")
    
    # 通用 CLI 通道
    if _agent_cli_available(agent):
        channels.append("cli")
    
    # Brain 通道（简单任务直接完成）
    channels.append("brain")

    # 兜底通道
    channels.append("file")
    channels.append("notify")
    return channels


# ── 投递方法 ──────────────────────────────────────────────────────────────────

def _dispatch_zellij(prompt: str, project: str, target_tab: int = None) -> dict:
    """
    通过 zellij 注入命令到共享终端。
    如果指定 target_tab，先切换到对应 tab 再注入。
    """
    # 构造注入的命令
    header = f"echo '=== 上帝之鞭 === 项目: {project} ==='"
    status_cmd = f"python3 -m plow_whip.agent_flow --project {project} status"
    prompt_echo = f"echo '{prompt}'"
    full_command = f"{header} && {status_cmd} && {prompt_echo}"

    try:
        # 如果指定了 tab，先切换
        if target_tab is not None:
            subprocess.run(
                ["zellij", "action", "go-to-tab", str(target_tab)],
                capture_output=True, text=True, timeout=3,
            )

        result = subprocess.run(
            ["zellij", "action", "write-chars", full_command + "\n"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            tab_info = f" (tab {target_tab})" if target_tab else ""
            return {"success": True, "channel": "zellij", "detail": f"命令已注入到 shared 会话{tab_info}"}
        return {"success": False, "channel": "zellij", "detail": f"zellij 返回 {result.returncode}: {result.stderr}"}
    except subprocess.TimeoutExpired:
        return {"success": False, "channel": "zellij", "detail": "zellij 超时"}
    except FileNotFoundError:
        return {"success": False, "channel": "zellij", "detail": "zellij 未安装"}


def _dispatch_qoder_cli(prompt: str, project: str, max_turns: int = 20, timeout: int = 300) -> dict:
    """
    通过 qoderclicn Print 模式直接唤醒 Qoder CLI 执行任务。
    这是真正的自动唤醒——不需要人点，脚本直接调。
    """
    # 获取项目路径
    projects_dir = af.get_projects_dir()
    project_path = os.path.join(projects_dir, project)
    
    if not os.path.isdir(project_path):
        return {"success": False, "channel": "qoder_cli", "detail": f"项目路径不存在: {project_path}"}
    
    # 构造 prompt 指令
    full_prompt = f"""plow-whip wakeup: {project} 项目被上帝之鞭唤醒。

请执行以下任务:
{prompt}

完成后:
1. 更新 AGENT_STATE.json (handoff)
2. 写进度到 AGENT_COMMS.md
3. 清空 ~/.plow-whip/inbox/qoder.json 中对应任务"""
    
    cmd = [
        "qoderclicn",
        "-p", full_prompt,
        "-w", project_path,
        "--yolo",
        "--max-turns", str(max_turns),
    ]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=timeout,  # 默认 5 分钟
        )
        if result.returncode == 0:
            return {
                "success": True,
                "channel": "qoder_cli",
                "detail": f"Qoder CLI 已在 {project_path} 执行任务",
                "output": result.stdout[:500] if result.stdout else "",
            }
        return {
            "success": False,
            "channel": "qoder_cli",
            "detail": f"qoderclicn 返回 {result.returncode}: {result.stderr[:200]}",
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "channel": "qoder_cli", "detail": "qoderclicn 超时 (5分钟)"}
    except FileNotFoundError:
        return {"success": False, "channel": "qoder_cli", "detail": "qoderclicn 未安装"}


def _dispatch_codex_cli(prompt: str, project: str, max_turns: int = 20, timeout: int = 1800) -> dict:
    """
    通过 codex CLI Print 模式直接唤醒 Codex 执行任务。
    timeout 默认 1800 秒（30 分钟），Sprint 级任务需要足够时间。
    """
    # 获取项目路径
    projects_dir = af.get_projects_dir()
    project_path = os.path.join(projects_dir, project)
    
    if not os.path.isdir(project_path):
        return {"success": False, "channel": "codex_cli", "detail": f"项目路径不存在: {project_path}"}
    
    # 构造 prompt 指令
    full_prompt = f"""plow-whip wakeup: {project} 项目被上帝之鞭唤醒。

请执行以下任务:
{prompt}

完成后:
1. 更新 AGENT_STATE.json (handoff)
2. 写进度到 AGENT_COMMS.md
3. 清空 ~/.plow-whip/inbox/codex.json 中对应任务"""
    
    cmd = [
        "npx", "@openai/codex",
        "-a", "never",       # 不自动审批
        "-s", "workspace-write",  # 沙箱模式
        "-C", project_path,  # 工作目录
        "exec", "--ephemeral",  # 一次性执行
        full_prompt,
    ]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=timeout,  # 默认 5 分钟
        )
        if result.returncode == 0:
            return {
                "success": True,
                "channel": "codex_cli",
                "detail": f"Codex CLI 已在 {project_path} 执行任务",
                "output": result.stdout[:500] if result.stdout else "",
            }
        return {
            "success": False,
            "channel": "codex_cli",
            "detail": f"codex 返回 {result.returncode}: {result.stderr[:200]}",
        }
    except subprocess.TimeoutExpired:
        timeout_min = timeout // 60
        return {"success": False, "channel": "codex_cli", "detail": f"codex 超时 ({timeout_min}分钟)"}
    except FileNotFoundError:
        return {"success": False, "channel": "codex_cli", "detail": "npx 或 @openai/codex 未安装"}




def _dispatch_brain(agent: str, prompt: str, project: str) -> dict:
    """
    用 DeepSeek Brain 处理简单任务。
    如果任务简单，Brain 直接完成；否则返回失败让其他通道接手。
    """
    brain = Brain()
    if not brain.available:
        return {"success": False, "channel": "brain", "detail": "DeepSeek 不可用"}

    # 构造带项目上下文的 task
    projects_dir = af.get_projects_dir()
    project_path = os.path.join(projects_dir, project)
    context = f"项目: {project}\n路径: {project_path}\n当前 agent: {agent}"

    result = brain.think(prompt, context)

    if result["routed"] == "deepseek":
        # Brain 完成了任务！把结果写入 agent 的会话和 inbox
        output = result["output"]
        complexity = result["complexity"]

        # 写入 agent 的会话记录
        conv_dir = af.conversations_dir(project)
        agent_dir = os.path.join(conv_dir, agent)
        if os.path.isdir(agent_dir):
            brain_log = os.path.join(agent_dir, "brain_results.md")
            with open(brain_log, "a", encoding="utf-8") as f:
                f.write(f"\n## Brain Result ({datetime.now().strftime('%H:%M:%S')})\n")
                f.write(f"**Task:** {prompt[:100]}...\n")
                f.write(f"**Complexity:** {complexity['level']} (score={complexity['score']})\n\n")
                f.write(output)
                f.write("\n\n---\n")

        return {
            "success": True,
            "channel": "brain",
            "detail": f"DeepSeek 完成 (score={complexity['score']})",
            "output": output,
        }
    else:
        return {"success": False, "channel": "brain", "detail": result["reason"]}

def _dispatch_file(agent: str, prompt: str, project: str) -> dict:
    """
    写入任务收件箱文件。
    agent 启动时会读取 ~/.plow-whip/inbox/<agent>.json
    """
    _ensure_inbox()
    inbox_file = os.path.join(INBOX_DIR, f"{agent}.json")

    # 读取现有任务（如有）
    tasks = []
    if os.path.exists(inbox_file):
        try:
            with open(inbox_file, encoding="utf-8") as f:
                tasks = json.load(f)
        except (json.JSONDecodeError, IOError):
            tasks = []

    # 追加新任务
    tasks.append({
        "project": project,
        "prompt": prompt,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "pending",
    })

    with open(inbox_file, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)
        f.write("\n")

    return {"success": True, "channel": "file", "detail": f"任务已写入 {inbox_file}"}


def _dispatch_notify(agent: str, prompt: str, project: str) -> dict:
    """发送 macOS 通知。"""
    message = f"[{project}] 轮到 {agent} — 请查看任务"
    af.notify(message, ring=True)
    return {"success": True, "channel": "notify", "detail": "macOS 通知已发送"}


# ── 主入口 ────────────────────────────────────────────────────────────────────

def dispatch(agent: str, project: str, prompt: str, force_channel: str = None, **kwargs) -> dict:
    """
    将任务投递给指定 agent。

    参数:
      agent: 目标 agent 名称（qoder/codex/cursor）
      project: 项目名称
      prompt: 可执行的鞭策指令文本
      force_channel: 强制使用指定通道（zellij/file/notify）

    返回:
      {"success": bool, "channel": str, "detail": str}
    """
    use_brain = kwargs.pop("use_brain", False)

    if force_channel:
        channels = [force_channel]
    else:
        channels = available_channels(agent)
        # Brain 通道只在显式启用时使用
        if not use_brain:
            channels = [ch for ch in channels if ch != "brain"]

    for ch in channels:
        if ch == "brain":
            result = _dispatch_brain(agent, prompt, project)
        elif ch == "qoder_cli":
            max_turns = kwargs.get("max_turns", 20)
            timeout = kwargs.get("timeout", 300)
            result = _dispatch_qoder_cli(prompt, project, max_turns, timeout)
        elif ch == "codex_cli":
            max_turns = kwargs.get("max_turns", 20)
            timeout = kwargs.get("timeout", 1800)
            result = _dispatch_codex_cli(prompt, project, max_turns, timeout)
        elif ch == "zellij":
            target_tab = kwargs.get("target_tab")
            result = _dispatch_zellij(prompt, project, target_tab)
        elif ch == "file":
            result = _dispatch_file(agent, prompt, project)
        elif ch == "notify":
            result = _dispatch_notify(agent, prompt, project)
        else:
            continue

        if result["success"]:
            return result

    return {"success": False, "channel": "none", "detail": "所有通道均失败"}


def read_inbox(agent: str) -> list:
    """读取指定 agent 的任务收件箱。"""
    _ensure_inbox()
    inbox_file = os.path.join(INBOX_DIR, f"{agent}.json")
    if not os.path.exists(inbox_file):
        return []
    try:
        with open(inbox_file, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def clear_inbox(agent: str):
    """清空指定 agent 的任务收件箱。"""
    _ensure_inbox()
    inbox_file = os.path.join(INBOX_DIR, f"{agent}.json")
    if os.path.exists(inbox_file):
        os.remove(inbox_file)
