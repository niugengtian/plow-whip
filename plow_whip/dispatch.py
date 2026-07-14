#!/usr/bin/env python3
"""
dispatch.py — 将逻辑 Agent 解析成可执行 Driver，并按同职责候选故障接力

用法:
    from plow_whip.dispatch import dispatch
    dispatch("codex", project="MyProject", prompt="实现登录功能")
"""

from __future__ import annotations

import json
import contextlib
import io
import os
import shutil
import shlex
import signal
import subprocess
import sys
import threading
import uuid
from datetime import datetime

from . import agent_flow as af
from . import routing
from . import leases
from .brain import Brain, classify_complexity
from .simple_tasker import SimpleTasker
from .io_utils import atomic_write_json, file_lock


# ── 配置 ─────────────────────────────────────────────────────────────────────

ZELLIJ_SESSION = "shared"
INBOX_DIR = os.path.join(os.environ.get("PLOW_WHIP_CONFIG_DIR", os.path.join(os.path.expanduser("~"), ".plow-whip")), "inbox")


def _ensure_inbox():
    os.makedirs(INBOX_DIR, exist_ok=True)


def _inbox_file(agent: str) -> str:
    af.validate_identifier(agent, "agent")
    return os.path.join(INBOX_DIR, f"{agent}.json")

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
    atomic_write_json(perm_file, _permission_state)

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


def _driver_available(driver: str) -> bool:
    """Check one execution driver without invoking a model."""
    if driver == "codex_cli":
        return bool(shutil.which("codex") or shutil.which("npx"))
    if driver == "cursor_cli":
        return bool(shutil.which("cursor-agent"))
    if driver == "simple_tasker":
        return Brain().available
    if driver == "zellij":
        return _zellij_available()
    if driver == "notify":
        return sys.platform == "darwin"
    return driver in ("file", "brain")


def _agent_cli_available(agent: str) -> bool:
    """Compatibility wrapper for callers/tests using legacy agent names."""
    return _driver_available({"codex": "codex_cli"}.get(agent, agent))


def _execution_routes(agent: str, project: str | None = None) -> list[dict]:
    if project and os.path.exists(af.protocol_file(project)):
        task = af.load_state(project).get("task", {})
        return routing.execution_routes(
            af.load_protocol(project), agent, _driver_available,
            role=task.get("required_role"), capabilities=task.get("required_capabilities"),
        )
    legacy = {
        "cursor_cli": ["cursor_cli", "codex_cli"],
        "codex_cli": ["codex_cli", "cursor_cli"],
        "cursor": ["zellij"],
    }
    return [
        {"agent": agent, "driver": driver}
        for driver in legacy.get(agent, []) if _driver_available(driver)
    ]


def available_channels(agent: str, project: str | None = None) -> list:
    """返回指定 agent 当前可用的投递通道列表（按优先级）。"""
    channels = [item["driver"] for item in _execution_routes(agent, project)]
    
    # Brain 通道（简单任务直接完成）
    channels.append("brain")

    # 兜底通道
    if "file" not in channels:
        channels.append("file")
    if sys.platform == "darwin":
        channels.append("notify")
    return channels


# ── 投递方法 ──────────────────────────────────────────────────────────────────

def _dispatch_zellij(prompt: str, project: str, target_tab: int = None) -> dict:
    """
    通过 zellij 注入命令到共享终端。
    如果指定 target_tab，先切换到对应 tab 再注入。
    """
    # 构造注入的命令
    header = f"printf '%s\\n' {shlex.quote(f'=== 耕田之鞭 === 项目: {project} ===')}"
    status_cmd = f"python3 -m plow_whip.agent_flow --project {shlex.quote(project)} status"
    prompt_echo = f"printf '%s\\n' {shlex.quote(prompt)}"
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


def _task_marker(state: dict) -> str:
    task = dict(state.get("task", {}))
    task.pop("cli_sessions", None)
    task.pop("execution", None)
    task.pop("attempts", None)
    return json.dumps(task, ensure_ascii=False, sort_keys=True)


def _existing_cli_session(project: str, agent: str) -> dict | None:
    return af.load_state(project).get("task", {}).get("cli_sessions", {}).get(agent)


def _record_cli_session(project: str, agent: str, session_id: str, route: dict | None = None) -> dict:
    """Persist one CLI-generated session ID per task and CLI."""
    for _ in range(3):
        state = af.load_state(project)
        task = state.setdefault("task", {})
        sessions = task.setdefault("cli_sessions", {})
        existing = sessions.get(agent)
        if existing:
            if existing.get("session_id") != session_id:
                raise RuntimeError(
                    f"task {task.get('id')} already owns {agent} session {existing.get('session_id')}"
                )
            return existing
        sessions[agent] = {
            "session_id": session_id,
            "status": "active",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "auth_profile": (route or {}).get("name", "desktop"),
            "model": (route or {}).get("model"),
        }
        task.setdefault("execution", {})["session_id"] = session_id
        try:
            af.save_state(project, state)
            return sessions[agent]
        except RuntimeError:
            continue
    raise RuntimeError(f"could not persist {agent} session after concurrent state updates")


def _event_session_id(event: dict) -> str | None:
    return (
        event.get("session_id")
        or event.get("thread_id")
        or (event.get("thread") or {}).get("id")
    )


def _record_running_pid(project: str, driver: str, dispatch_id: str | None, pid: int) -> None:
    """Persist the real CLI PID while it is running, preserving the wrapper PID."""
    for _ in range(5):
        state = af.load_state(project)
        task = state.get("task") or {}
        execution = task.setdefault("execution", {})
        if execution.get("dispatch_id") != dispatch_id:
            raise RuntimeError(f"task execution claim changed before {driver} pid={pid} started")
        execution.update({
            "dispatch_id": dispatch_id, "driver": driver, "cli_pid": pid,
            "status": "running", "cli_started_at": datetime.now().isoformat(timespec="seconds"),
        })
        try:
            af.save_state(project, state)
            return
        except RuntimeError:
            continue
    raise RuntimeError(f"could not persist running {driver} pid after concurrent state updates")


def _terminate_process_group(process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (AttributeError, ProcessLookupError, PermissionError):
        try:
            process.terminate()
        except (AttributeError, ProcessLookupError):
            pass


def _run_streaming_cli(cmd: list[str], cwd: str, timeout: int, on_event, on_start=None, env=None) -> dict:
    """Consume JSONL without creating another Agent or probing the model."""
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
        env=env,
    )
    if on_start:
        try:
            on_start(process.pid)
        except Exception:
            _terminate_process_group(process)
            process.wait()
            raise
    timed_out = threading.Event()

    def stop():
        timed_out.set()
        _terminate_process_group(process)
        def force_kill():
            if process.poll() is None:
                try:
                    process.kill()
                except (AttributeError, ProcessLookupError):
                    pass
        killer = threading.Timer(5, force_kill)
        killer.daemon = True
        killer.start()

    timer = threading.Timer(timeout, stop)
    timer.daemon = True
    timer.start()
    lines = []
    try:
        try:
            for raw in process.stdout or ():
                lines.append(raw)
                try:
                    on_event(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    pass
            process.wait()
        except Exception:
            _terminate_process_group(process)
            process.wait()
            raise
    finally:
        timer.cancel()
    return {
        "pid": process.pid,
        "returncode": process.returncode,
        "timed_out": timed_out.is_set(),
        "output": "".join(lines),
    }


def _retryable_cli_failure(result: dict) -> bool:
    if result.get("timed_out"):
        return True
    output = result.get("output", "").lower()
    markers = (
        "socket hang up", "timed out", "timeout", "rate limit", "rate_limit",
        "quota", "insufficient_quota", "unauthorized", "invalid api key",
        "invalid_api_key", "authentication", "401", "403", "429",
        "500", "502", "503", "504", "connection reset",
    )
    return result.get("returncode") != 0 and any(marker in output for marker in markers)


def _candidate_env(agent: str, candidate: dict, extra_env: dict | None = None) -> dict:
    env = os.environ.copy()
    env.pop("CODEX_THREAD_ID", None)
    env.pop("PLOW_WHIP_CONTROL_TOKEN", None)
    env["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"] = "Codex CLI" if agent == "codex_cli" else "Cursor CLI"
    key_name = "CODEX_API_KEY" if agent == "codex_cli" else "CURSOR_API_KEY"
    if candidate.get("secret"):
        env[key_name] = candidate["secret"]
    else:
        env.pop(key_name, None)
    env.update(extra_env or {})
    return env


def _run_cli_candidates(
    agent: str, build_command, cwd: str, timeout: int, on_event, on_start,
    active: dict, extra_env: dict | None = None,
) -> dict:
    from .cli_auth import candidates

    routes = candidates(agent)
    if not routes:
        return {"config_error": "API key pool has no available profiles", "attempts": []}
    attempts = []
    for index, route in enumerate(routes):
        active.clear()
        active.update(route)
        result = _run_streaming_cli(
            build_command(route), cwd, timeout, on_event, on_start,
            env=_candidate_env(agent, route, extra_env),
        )
        attempts.append({"profile": route["name"], "model": route.get("model"), "returncode": result["returncode"]})
        result.update({"auth_profile": route["name"], "model": route.get("model"), "attempts": attempts})
        if result["returncode"] == 0 or index == len(routes) - 1 or not _retryable_cli_failure(result):
            return result
    return result


def _dispatch_cursor_cli(
    prompt: str,
    project: str,
    max_turns: int = 20,
    timeout: int = 300,
    agent: str | None = None,
    dispatch_id: str | None = None,
    lease_token: str | None = None,
) -> dict:
    """通过 Cursor CLI 唤醒 Cursor 执行任务。"""
    initial_state = af.load_state(project)
    project_path = af.task_workspace(project, initial_state)
    if not os.path.isdir(project_path):
        return {"success": False, "channel": "cursor_cli", "detail": f"项目路径不存在: {project_path}"}

    full_prompt = f"""plow-whip wakeup: {project} 项目被耕田之鞭唤醒。

请执行以下任务:
{prompt}

完成后:
1. 使用 plow-whip task progress 或 handoff 更新状态
2. 当前里程碑完成时使用 plow-whip task complete；投递 lifecycle 由父调度器回写"""

    cursor_bin = shutil.which("cursor-agent")
    if not cursor_bin:
        return {"success": False, "channel": "cursor_cli", "detail": "cursor-agent 未安装"}
    session = _existing_cli_session(project, "cursor_cli")

    try:
        initial_marker = _task_marker(initial_state)
        initial_task_id = (initial_state.get("task") or {}).get("id")
        captured = {"session_id": session.get("session_id") if session else None}
        active_route = {}

        def build_command(route):
            cmd = [cursor_bin, "--print", "--force", "--output-format", "stream-json"]
            if route.get("model"):
                cmd.extend(["--model", route["model"]])
            if captured["session_id"]:
                cmd.append(f"--resume={captured['session_id']}")
            cmd.append(full_prompt)
            return cmd

        def on_event(event):
            session_id = _event_session_id(event)
            if not session_id:
                return
            if captured["session_id"] and captured["session_id"] != session_id:
                raise RuntimeError(f"cursor_cli resumed as unexpected session {session_id}")
            if not captured["session_id"]:
                _record_cli_session(project, "cursor_cli", session_id, active_route)
                captured["session_id"] = session_id

        def on_start(pid):
            _record_running_pid(project, "cursor_cli", dispatch_id, pid)
            if agent and dispatch_id:
                update_inbox_task(agent, dispatch_id, "running", f"pid={pid}")
        result = _run_cli_candidates(
            "cursor_cli", build_command, project_path, timeout, on_event, on_start, active_route,
            {leases.TOKEN_ENV: lease_token} if lease_token else None,
        )
        if result.get("config_error"):
            return {"success": False, "channel": "cursor_cli", "detail": result["config_error"]}
        if result["timed_out"]:
            return {"success": False, "channel": "cursor_cli", "detail": "cursor 超时并已终止", "pid": result["pid"], "attempts": result["attempts"]}
        if result["returncode"] == 0 and not captured["session_id"]:
            return {"success": False, "channel": "cursor_cli", "detail": "cursor 成功退出但未返回 session_id", "pid": result["pid"]}
        if result["returncode"] == 0:
            current_state = af.load_state(project)
            if _task_marker(current_state) == initial_marker:
                return {"success": False, "channel": "cursor_cli", "detail": "cursor 正常退出但任务状态未推进", "pid": result["pid"], "session_id": captured["session_id"]}
            current_task = current_state.get("task") or {}
            if current_task.get("id") == initial_task_id and any(
                item.get("returncode") for item in current_task.get("verification", [])
            ):
                return {
                    "success": False, "channel": "cursor_cli", "detail": "verification failed; resume same session",
                    "pid": result["pid"], "session_id": captured["session_id"], "attempts": result["attempts"],
                }
            return {
                "success": True,
                "channel": "cursor_cli",
                "detail": f"Cursor CLI 已在 {project_path} 执行任务",
                "output": result["output"][-500:],
                "pid": result["pid"],
                "session_id": captured["session_id"],
                "auth_profile": result["auth_profile"],
                "model": result.get("model"),
                "attempts": result["attempts"],
            }
        return {
            "success": False,
            "channel": "cursor_cli",
            "detail": f"cursor 返回 {result['returncode']}: {result['output'][-200:]}",
            "pid": result["pid"],
            "auth_profile": result["auth_profile"],
            "model": result.get("model"),
            "attempts": result["attempts"],
        }
    except (FileNotFoundError, RuntimeError) as exc:
        if isinstance(exc, RuntimeError):
            return {"success": False, "channel": "cursor_cli", "detail": str(exc)}
        return {"success": False, "channel": "cursor_cli", "detail": "cursor-agent 未安装"}


def _dispatch_codex_cli(
    prompt: str,
    project: str,
    max_turns: int = 20,
    timeout: int = 1800,
    agent: str | None = None,
    dispatch_id: str | None = None,
    lease_token: str | None = None,
) -> dict:
    """
    通过 codex CLI Print 模式直接唤醒 Codex 执行任务。
    timeout 默认 1800 秒（30 分钟），Sprint 级任务需要足够时间。
    """
    # 获取项目路径
    initial_state = af.load_state(project)
    project_path = af.task_workspace(project, initial_state)
    
    if not os.path.isdir(project_path):
        return {"success": False, "channel": "codex_cli", "detail": f"项目路径不存在: {project_path}"}
    
    # 构造 prompt 指令
    full_prompt = f"""plow-whip wakeup: {project} 项目被耕田之鞭唤醒。

请执行以下任务:
{prompt}

完成后:
1. 使用 plow-whip task progress 或 handoff 更新状态
2. 当前里程碑完成时使用 plow-whip task complete；投递 lifecycle 由父调度器回写"""
    
    codex_bin = shutil.which("codex")
    if codex_bin:
        command = [codex_bin]
    elif shutil.which("npx"):
        command = [shutil.which("npx"), "-y", "@openai/codex"]
    else:
        return {"success": False, "channel": "codex_cli", "detail": "codex 或 npx 未安装"}
    prefix = [
        "-a", "never",       # 不自动审批
        "-s", "workspace-write",  # 沙箱模式
        "-C", project_path,  # 工作目录
        "exec",
    ]
    session = _existing_cli_session(project, "codex_cli")
    initial_marker = _task_marker(initial_state)
    initial_task_id = (initial_state.get("task") or {}).get("id")
    try:
        captured = {"session_id": session.get("session_id") if session else None}
        active_route = {}

        def build_command(route):
            cmd = list(command)
            options = list(prefix[:-1])
            if route.get("model"):
                options.extend(["--model", route["model"]])
            options.extend(["exec", "--skip-git-repo-check"])
            if captured["session_id"]:
                cmd.extend(options + ["resume", "--json", captured["session_id"], full_prompt])
            else:
                cmd.extend(options + ["--json", full_prompt])
            return cmd

        def on_event(event):
            session_id = _event_session_id(event)
            if not session_id:
                return
            if captured["session_id"] and captured["session_id"] != session_id:
                raise RuntimeError(f"codex_cli resumed as unexpected session {session_id}")
            if not captured["session_id"]:
                _record_cli_session(project, "codex_cli", session_id, active_route)
                captured["session_id"] = session_id

        def on_start(pid):
            _record_running_pid(project, "codex_cli", dispatch_id, pid)
            if agent and dispatch_id:
                update_inbox_task(agent, dispatch_id, "running", f"pid={pid}")
        result = _run_cli_candidates(
            "codex_cli", build_command, project_path, timeout, on_event, on_start, active_route,
            {leases.TOKEN_ENV: lease_token} if lease_token else None,
        )
        if result.get("config_error"):
            return {"success": False, "channel": "codex_cli", "detail": result["config_error"]}
        if result["timed_out"]:
            return {
                "success": False,
                "channel": "codex_cli",
                "detail": f"codex 超时并已终止 pid={result['pid']}",
                "pid": result["pid"],
                "attempts": result["attempts"],
            }
        if result["returncode"] == 0 and not captured["session_id"]:
            return {"success": False, "channel": "codex_cli", "detail": "codex 成功退出但未返回 session_id", "pid": result["pid"]}
        if result["returncode"] == 0:
            current_state = af.load_state(project)
            if _task_marker(current_state) == initial_marker:
                return {
                    "success": False,
                    "channel": "codex_cli",
                    "detail": f"codex pid={result['pid']} 正常退出但任务状态未推进",
                    "pid": result["pid"],
                    "session_id": captured["session_id"],
                }
            current_task = current_state.get("task") or {}
            if current_task.get("id") == initial_task_id and any(
                item.get("returncode") for item in current_task.get("verification", [])
            ):
                return {
                    "success": False, "channel": "codex_cli", "detail": "verification failed; resume same session",
                    "pid": result["pid"], "session_id": captured["session_id"], "attempts": result["attempts"],
                }
            return {
                "success": True,
                "channel": "codex_cli",
                "detail": f"Codex CLI 已在 {project_path} 执行任务",
                "output": result["output"][-500:],
                "pid": result["pid"],
                "session_id": captured["session_id"],
                "auth_profile": result["auth_profile"],
                "model": result.get("model"),
                "attempts": result["attempts"],
            }
        return {
            "success": False,
            "channel": "codex_cli",
            "detail": f"codex pid={result['pid']} 返回 {result['returncode']}: {result['output'][-200:]}",
            "pid": result["pid"],
            "auth_profile": result["auth_profile"],
            "model": result.get("model"),
            "attempts": result["attempts"],
        }
    except (FileNotFoundError, RuntimeError) as exc:
        if isinstance(exc, RuntimeError):
            return {"success": False, "channel": "codex_cli", "detail": str(exc)}
        return {"success": False, "channel": "codex_cli", "detail": "codex 或 npx 未安装"}




def _dispatch_brain(agent: str, prompt: str, project: str) -> dict:
    """
    用 DeepSeek Brain 处理简单任务。
    如果任务简单，Brain 直接完成；否则返回失败让其他通道接手。
    """
    brain = Brain()
    if not brain.available:
        return {"success": False, "channel": "brain", "detail": "DeepSeek 不可用"}

    # 构造带项目上下文的 task
    project_path = af.project_dir(project)
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


def _dispatch_simple_tasker(agent: str, prompt: str, project: str) -> dict:
    """Run or resume one file-persisted simple-tasker session."""
    from types import SimpleNamespace
    from . import tasking

    state = af.load_state(project)
    task = state.get("task") or {}
    task_id = task.get("id", "T-001")
    workspace = af.task_workspace(project, state)
    runner = SimpleTasker(
        workspace, task_id,
        session_dir=os.path.join(af.project_memory_dir(project), "sessions"),
    )
    context = json.dumps({
        "project": project,
        "acceptance": task.get("acceptance", []),
        "verify_commands": task.get("verify_commands", []),
        "branch": (state.get("workflow") or {}).get("git", {}).get("branch"),
    }, ensure_ascii=False)
    try:
        result = runner.run(task.get("next_action") or task.get("title") or prompt, context)
    except Exception as exc:
        return {
            "success": False, "channel": "simple_tasker",
            "detail": f"{type(exc).__name__}: {exc}",
            "session_id": f"simple-tasker:{task_id}",
        }
    state = af.load_state(project)
    if state.get("task", {}).get("id") == task_id:
        session = state["task"].setdefault("cli_sessions", {}).setdefault("simple_tasker", {})
        session.update({
            "session_id": result.get("session_id"), "status": result.get("status"),
            "created_at": session.get("created_at") or datetime.now().isoformat(timespec="seconds"),
            "key_ref": result.get("key_ref"), "session_file": os.path.relpath(
                result.get("session_file", runner.session_path), af.project_dir(project)
            ),
        })
        af.save_state(project, state)

    if result.get("status") == "needs_planner":
        tasking.escalate_to_planner(project, result.get("reason") or "simple-tasker requested planning")
        return {
            "success": True, "channel": "simple_tasker", "detail": "simple-tasker escalated to planner",
            "output": result.get("reason", ""), "session_id": result.get("session_id"), "key_ref": result.get("key_ref"),
        }
    if result.get("status") != "completed":
        return {
            "success": False, "channel": "simple_tasker", "detail": result.get("reason", "simple-tasker failed"),
            "session_id": result.get("session_id"), "key_ref": result.get("key_ref"),
        }

    if (state.get("workflow") or {}).get("code_change") and not result.get("verify_commands"):
        return {
            "success": False, "channel": "simple_tasker",
            "detail": "simple-tasker completed without executable verify_commands",
            "session_id": result.get("session_id"), "key_ref": result.get("key_ref"),
        }

    state = af.load_state(project)
    if state.get("task", {}).get("id") == task_id and result.get("verify_commands"):
        state["task"]["verify_commands"] = list(result["verify_commands"])
        af.save_state(project, state)
    with contextlib.redirect_stdout(io.StringIO()):
        af.cmd_task(project, SimpleNamespace(action="complete", output=result.get("summary", "completed"), next=None, json=True))
    current = af.load_state(project)
    if current.get("task", {}).get("id") == task_id and current.get("task", {}).get("status") == "active":
        return {
            "success": False, "channel": "simple_tasker", "detail": "verification failed; resume same session",
            "output": current["task"].get("last_output", ""), "session_id": result.get("session_id"), "key_ref": result.get("key_ref"),
        }
    runner.archive()
    return {
        "success": True, "channel": "simple_tasker", "detail": "DeepSeek simple-tasker completed",
        "output": result.get("summary", ""), "session_id": result.get("session_id"), "key_ref": result.get("key_ref"),
    }

def _dispatch_file(agent: str, prompt: str, project: str, dispatch_id: str = None, task_id: str = None) -> dict:
    """
    写入任务收件箱文件。
    agent 启动时会读取 ~/.plow-whip/inbox/<agent>.json
    """
    _ensure_inbox()
    inbox_file = _inbox_file(agent)

    dispatch_id = dispatch_id or f"DP-{uuid.uuid4().hex[:12]}"
    task_id = task_id or (af.load_state(project).get("task", {}).get("id", "T-001") if os.path.exists(af.state_file(project)) else "T-001")
    record = {
        "dispatch_id": dispatch_id,
        "task_id": task_id,
        "project": project,
        "project_path": af.project_dir(project),
        "prompt": prompt,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "queued",
    }
    with file_lock(inbox_file + ".lock"):
        tasks = read_inbox(agent)
        for index, task in enumerate(tasks):
            if task.get("dispatch_id") == dispatch_id:
                tasks[index] = {**task, **record}
                break
        else:
            tasks.append(record)
        atomic_write_json(inbox_file, tasks)

    return {"success": True, "channel": "file", "detail": f"任务已写入 {inbox_file}", "dispatch_id": dispatch_id, "task_id": task_id, "status": "queued"}


def update_inbox_task(agent: str, dispatch_id: str, status: str, output: str = "") -> dict:
    allowed = {"queued", "accepted", "running", "completed", "failed"}
    if status not in allowed:
        raise ValueError(f"invalid dispatch status: {status}")
    _ensure_inbox()
    inbox_file = _inbox_file(agent)
    with file_lock(inbox_file + ".lock"):
        tasks = read_inbox(agent)
        for task in tasks:
            if task.get("dispatch_id") == dispatch_id:
                task["status"] = status
                task[f"{status}_at"] = datetime.now().isoformat(timespec="seconds")
                if output:
                    task["output"] = output
                atomic_write_json(inbox_file, tasks)
                return task
    raise KeyError(f"dispatch not found: {dispatch_id}")


def _dispatch_notify(agent: str, prompt: str, project: str) -> dict:
    """发送 macOS 通知。"""
    message = f"[{project}] 轮到 {agent} — 请查看任务"
    af.notify(message, ring=True)
    return {"success": True, "channel": "notify", "detail": "macOS 通知已发送"}


def _record_execution(project: str, task_id: str | None, result: dict) -> None:
    """Persist a compact audit trail without copying model output into Hot context."""
    if not task_id or not os.path.exists(af.state_file(project)):
        return
    state = af.load_state(project)
    execution = {
        key: result[key]
        for key in (
            "dispatch_id", "logical_owner", "executor", "driver", "status", "session_id",
            "pid", "returncode", "auth_profile", "key_ref", "attempts", "fallback_errors",
        )
        if result.get(key) is not None
    }
    goal = state.get("goal") or {}
    workflow = state.get("workflow") or {}
    target = next((item for item in workflow.get("completed", []) if item.get("id") == task_id), None)
    if target is None:
        target = next((item for item in goal.get("completed", []) if item.get("id") == task_id), None)
    if target is not None:
        target["execution"] = {**target.get("execution", {}), **execution}
    elif task_id.endswith("-PLAN") and goal.get("id") and task_id == f"{goal['id']}-PLAN":
        goal["planning_execution"] = execution
        state["goal"] = goal
    elif state.get("task", {}).get("id") == task_id:
        state["task"]["execution"] = {**state["task"].get("execution", {}), **execution}
    else:
        return
    af.save_state(project, state)


# ── 主入口 ────────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _temporary_lease(token: str | None):
    previous = os.environ.get(leases.TOKEN_ENV)
    if token:
        os.environ[leases.TOKEN_ENV] = token
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(leases.TOKEN_ENV, None)
        else:
            os.environ[leases.TOKEN_ENV] = previous

def dispatch(agent: str, project: str, prompt: str, force_channel: str = None, **kwargs) -> dict:
    """
    将任务投递给指定 agent。

    参数:
      agent: 目标 agent 名称（cursor/codex/cursor_cli）
      project: 项目名称
      prompt: 可执行的鞭策指令文本
      force_channel: 强制使用指定通道（zellij/file/notify）

    返回:
      {"success": bool, "channel": str, "detail": str}
    """
    if os.path.exists(af.protocol_file(project)):
        meta = af.load_protocol(project).get("agents", {}).get(agent, {})
        if not meta.get("schedulable", True):
            return {
                "success": False, "channel": "none", "status": "rejected_control_plane",
                "detail": f"{agent} is a non-schedulable control plane; use submit to assign a schedulable Agent",
            }
    use_brain = kwargs.pop("use_brain", False)
    dispatch_id = kwargs.pop("dispatch_id", None) or f"DP-{uuid.uuid4().hex[:12]}"
    lease_token = kwargs.pop("lease_token", None) or os.environ.get(leases.TOKEN_ENV)
    task_id = kwargs.pop("task_id", None)
    if task_id is None and os.path.exists(af.state_file(project)):
        task_id = af.load_state(project).get("task", {}).get("id", "T-001")
    strict = os.path.exists(af.protocol_file(project)) and leases.is_strict(af.load_protocol(project))
    ledger = _dispatch_file(agent, prompt, project, dispatch_id=dispatch_id, task_id=task_id)

    if force_channel:
        channels = [force_channel]
    else:
        routes = _execution_routes(agent, project)
        channels = available_channels(agent, project)
        # Brain 通道只在显式启用时使用
        if not use_brain:
            channels = [ch for ch in channels if ch != "brain"]

    failures = []
    for ch in channels:
        route = next((item for item in routes if item["driver"] == ch), {"agent": agent, "driver": ch}) if not force_channel else {"agent": agent, "driver": ch}
        if ch == "zellij" and strict:
            failures.append({
                "channel": ch,
                "detail": "strict workers require a lease-capable CLI driver; zellij is legacy-only",
            })
            continue
        if ch in ("cursor_cli", "codex_cli", "simple_tasker") and task_id:
            from . import supervisor

            existing = False
            if lease_token:
                try:
                    payload = leases.validate(
                        af.CONFIG_DIR, project, af.load_state(project), af.load_protocol(project),
                        token=lease_token, agent=agent,
                    )
                    existing = payload.get("dispatch_id") == dispatch_id
                except leases.LeaseDenied:
                    existing = False
            if not existing:
                claim = supervisor.claim_task(project, task_id, dispatch_id, ch, agent)
                if not claim["claimed"]:
                    result = {
                        "success": False, "channel": ch,
                        "detail": claim.get("detail", "same task already has a live executor"),
                        "dispatch_id": dispatch_id, "task_id": task_id, "status": "skipped_running",
                    }
                    try:
                        update_inbox_task(agent, dispatch_id, "failed", result["detail"])
                    except KeyError:
                        pass
                    return result
                lease_token = claim.get("lease_token") or lease_token
        if ch == "brain":
            result = _dispatch_brain(agent, prompt, project)
        elif ch == "simple_tasker":
            with _temporary_lease(lease_token):
                result = _dispatch_simple_tasker(agent, prompt, project)
        elif ch == "cursor_cli":
            max_turns = kwargs.get("max_turns", 20)
            timeout = kwargs.get("timeout", 300)
            result = _dispatch_cursor_cli(prompt, project, max_turns, timeout, agent, dispatch_id, lease_token)
        elif ch == "codex_cli":
            max_turns = kwargs.get("max_turns", 20)
            timeout = kwargs.get("timeout", 1800)
            result = _dispatch_codex_cli(prompt, project, max_turns, timeout, agent, dispatch_id, lease_token)
        elif ch == "zellij":
            target_tab = kwargs.get("target_tab")
            result = _dispatch_zellij(prompt, project, target_tab)
        elif ch == "file":
            result = ledger
        elif ch == "notify":
            result = _dispatch_notify(agent, prompt, project)
        else:
            continue

        if result["success"]:
            lifecycle = "completed" if ch in ("brain", "simple_tasker", "cursor_cli", "codex_cli", "cli") else "queued" if ch == "file" else "accepted"
            if lifecycle != "queued" or failures:
                try:
                    output = result.get("output", "")
                    if failures:
                        output = json.dumps({"fallback_errors": failures}, ensure_ascii=False)
                    update_inbox_task(agent, dispatch_id, lifecycle, output)
                except KeyError:
                    pass
            result.update({"dispatch_id": dispatch_id, "task_id": task_id, "status": lifecycle})
            result.update({"logical_owner": agent, "executor": route["agent"], "driver": ch})
            if failures:
                result["fallback_errors"] = failures
            _record_execution(project, task_id, result)
            return result
        failures.append({"channel": ch, "detail": result.get("detail", "failed")})

    try:
        update_inbox_task(agent, dispatch_id, "failed", "all channels failed")
    except KeyError:
        pass
    detail = "; ".join(f"{item['channel']}: {item['detail']}" for item in failures)
    result = {
        "success": False,
        "channel": "none",
        "detail": detail or "所有通道均失败",
        "failures": failures,
        "dispatch_id": dispatch_id,
        "task_id": task_id,
        "status": "failed",
    }
    _record_execution(project, task_id, result)
    return result


def read_inbox(agent: str) -> list:
    """读取指定 agent 的任务收件箱。"""
    _ensure_inbox()
    inbox_file = _inbox_file(agent)
    if not os.path.exists(inbox_file):
        return []
    try:
        with open(inbox_file, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def clear_inbox(agent: str):
    """Legacy helper: preserve the ledger file but clear its records."""
    _ensure_inbox()
    inbox_file = _inbox_file(agent)
    with file_lock(inbox_file + ".lock"):
        atomic_write_json(inbox_file, [])
