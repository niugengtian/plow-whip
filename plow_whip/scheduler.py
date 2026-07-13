"""Install plow-whip as a native per-user scheduled job."""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape

from .io_utils import atomic_write_json, atomic_write_text


JOB_NAME = "com.plow-whip.scheduler"
AUTO_CONTINUE_STALE_MINUTES = 1


def runtime_path() -> str:
    """Build a stable scheduler PATH without transient shell/runtime entries."""
    candidates = []
    for executable in ("plow-whip", "codex", "cursor-agent", "cursor", "npx"):
        resolved = shutil.which(executable)
        if resolved:
            candidates.append(os.path.dirname(os.path.abspath(resolved)))
    candidates.extend(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    return os.pathsep.join(dict.fromkeys(candidates))


def platform_name(system: str | None = None) -> str:
    value = (system or platform.system()).lower()
    return {"darwin": "macos", "linux": "linux", "windows": "windows"}.get(value, value)


def scheduler_paths(config_dir: str, system: str | None = None) -> dict:
    kind = platform_name(system)
    home = os.path.expanduser("~")
    if kind == "macos":
        job = os.path.join(home, "Library", "LaunchAgents", f"{JOB_NAME}.plist")
    elif kind == "linux":
        job = os.path.join(home, ".config", "systemd", "user", "plow-whip.service")
    else:
        job = os.path.join(config_dir, "scheduler-task.json")
    return {
        "platform": kind,
        "job": job,
        "timer": os.path.join(home, ".config", "systemd", "user", "plow-whip.timer") if kind == "linux" else None,
        "state": os.path.join(config_dir, "scheduler.json"),
        "stdout": os.path.join(config_dir, "logs", "scheduler.stdout.log"),
        "stderr": os.path.join(config_dir, "logs", "scheduler.stderr.log"),
    }


def command(auto_crack: bool = True) -> list[str]:
    executable = shutil.which("plow-whip")
    cmd = ([os.path.abspath(executable)] if executable else [sys.executable, "-m", "plow_whip.agent_flow"])
    cmd.extend(["whip", "--once", "--json"])
    if auto_crack:
        cmd.extend(["--crack", "--opt-in-only", "--stale-minutes", str(AUTO_CONTINUE_STALE_MINUTES)])
    return cmd


def render(config_dir: str, interval: int = 60, auto_crack: bool = True, system: str | None = None) -> dict:
    if interval < 1:
        raise ValueError("interval must be at least 1 second")
    paths = scheduler_paths(config_dir, system)
    cmd = command(auto_crack)
    path_env = runtime_path()
    if paths["platform"] == "macos":
        args = "".join(f"\n      <string>{escape(item)}</string>" for item in cmd)
        content = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{JOB_NAME}</string>
  <key>ProgramArguments</key><array>{args}
    </array>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><true/>
  <key>EnvironmentVariables</key><dict>
    <key>PLOW_WHIP_CONFIG_DIR</key><string>{escape(config_dir)}</string>
    <key>PATH</key><string>{escape(path_env)}</string>
  </dict>
  <key>StandardOutPath</key><string>{escape(paths['stdout'])}</string>
  <key>StandardErrorPath</key><string>{escape(paths['stderr'])}</string>
</dict></plist>
'''
    elif paths["platform"] == "linux":
        quoted = " ".join(shlex.quote(item) for item in cmd)
        content = {
            "service": f"[Unit]\nDescription=plow-whip scheduled scan\n\n[Service]\nType=oneshot\nEnvironment=PLOW_WHIP_CONFIG_DIR={shlex.quote(config_dir)}\nEnvironment=PATH={shlex.quote(path_env)}\nExecStart={quoted}\nStandardOutput=append:{paths['stdout']}\nStandardError=append:{paths['stderr']}\n",
            "timer": f"[Unit]\nDescription=Run plow-whip every {interval} seconds\n\n[Timer]\nOnBootSec=60\nOnUnitActiveSec={interval}\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n",
        }
    else:
        inner = f'set "PLOW_WHIP_CONFIG_DIR={config_dir}" && {subprocess.list2cmdline(cmd)} >> "{paths["stdout"]}" 2>> "{paths["stderr"]}"'
        content = {
            "task_name": "PlowWhipScheduler",
            "command": "cmd.exe",
            "arguments": ["/C", inner],
            "interval_seconds": interval,
        }
    return {
        **paths,
        "interval_seconds": interval,
        "auto_continue": auto_crack,
        "auto_crack": auto_crack,
        "content": content,
    }


def install(config_dir: str, interval: int = 60, auto_crack: bool = True, system: str | None = None, dry_run: bool = False) -> dict:
    spec = render(config_dir, interval, auto_crack, system)
    if dry_run:
        return {**spec, "installed": False, "dry_run": True}
    os.makedirs(os.path.join(config_dir, "logs"), exist_ok=True)
    os.makedirs(os.path.dirname(spec["job"]), exist_ok=True)
    if spec["platform"] == "macos":
        atomic_write_text(spec["job"], spec["content"])
        subprocess.run(["launchctl", "unload", spec["job"]], capture_output=True, check=False)
        result = subprocess.run(["launchctl", "load", spec["job"]], capture_output=True, text=True, check=False)
    elif spec["platform"] == "linux":
        atomic_write_text(spec["job"], spec["content"]["service"])
        atomic_write_text(spec["timer"], spec["content"]["timer"])
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)
        result = subprocess.run(["systemctl", "--user", "enable", "--now", "plow-whip.timer"], capture_output=True, text=True, check=False)
    else:
        task = spec["content"]
        action = subprocess.list2cmdline([task["command"], *task["arguments"]])
        minutes = max(1, (interval + 59) // 60)
        result = subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", str(minutes), "/TN", task["task_name"], "/TR", action], capture_output=True, text=True, check=False)
        atomic_write_json(spec["job"], task)
    state = {k: spec[k] for k in ("platform", "job", "timer", "interval_seconds", "auto_continue", "auto_crack")}
    state.update({
        "installed": result.returncode == 0,
        "enabled": result.returncode == 0,
        "returncode": result.returncode,
        "detail": (result.stderr or result.stdout or "").strip(),
    })
    atomic_write_json(spec["state"], state)
    return state


def status(config_dir: str, system: str | None = None) -> dict:
    paths = scheduler_paths(config_dir, system)
    state = {}
    if os.path.exists(paths["state"]):
        with open(paths["state"], encoding="utf-8") as f:
            state = json.load(f)
    last_run = None
    last_run_path = os.path.join(config_dir, "scheduler-last-run.json")
    if os.path.exists(last_run_path):
        try:
            with open(last_run_path, encoding="utf-8") as f:
                last_run = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    installed = os.path.exists(paths["job"])
    return {
        **paths,
        **state,
        "installed": installed,
        "enabled": bool(state.get("enabled", state.get("installed", installed))),
        "last_run": last_run,
    }


def _control(config_dir: str, action: str, system: str | None = None) -> dict:
    paths = scheduler_paths(config_dir, system)
    if paths["platform"] == "macos":
        verb = "load" if action == "start" else "unload"
        cmd = ["launchctl", verb, paths["job"]]
    elif paths["platform"] == "linux":
        cmd = ["systemctl", "--user", action, "plow-whip.timer"]
    else:
        cmd = ["schtasks", "/Change", "/TN", "PlowWhipScheduler", "/ENABLE" if action == "start" else "/DISABLE"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        state = {}
        if os.path.exists(paths["state"]):
            try:
                with open(paths["state"], encoding="utf-8") as f:
                    state = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass
        state["enabled"] = action == "start"
        atomic_write_json(paths["state"], state)
    return {**paths, "action": action, "success": result.returncode == 0, "returncode": result.returncode, "detail": (result.stderr or result.stdout or "").strip()}


def start(config_dir: str, system: str | None = None) -> dict:
    return _control(config_dir, "start", system)


def stop(config_dir: str, system: str | None = None) -> dict:
    return _control(config_dir, "stop", system)


def logs(config_dir: str, lines: int = 40, system: str | None = None) -> dict:
    paths = scheduler_paths(config_dir, system)
    output = {}
    for key in ("stdout", "stderr"):
        try:
            with open(paths[key], encoding="utf-8", errors="replace") as f:
                output[key] = "".join(f.readlines()[-lines:])
        except OSError:
            output[key] = ""
    return {**paths, "lines": lines, "logs": output}


def doctor(config_dir: str, system: str | None = None) -> dict:
    current = status(config_dir, system)
    executable = command()[0]
    log_dir = os.path.dirname(current["stdout"])
    log_dir_writable = False
    if os.path.isdir(log_dir):
        try:
            log_dir_writable = bool(os.stat(log_dir).st_mode & 0o222)
        except OSError:
            pass
    checks = {
        "job_exists": os.path.exists(current["job"]),
        "executable_exists": os.path.exists(executable),
        "state_exists": os.path.exists(current["state"]),
        "log_dir_writable": log_dir_writable,
    }
    return {**current, "checks": checks, "ok": all(checks.values())}


def repair(config_dir: str, system: str | None = None) -> dict:
    current = status(config_dir, system)
    return install(
        config_dir,
        interval=int(current.get("interval_seconds", 60)),
        auto_crack=bool(current.get("auto_continue", current.get("auto_crack", False))),
        system=system,
    )


def uninstall(config_dir: str, system: str | None = None) -> dict:
    paths = scheduler_paths(config_dir, system)
    if paths["platform"] == "macos" and os.path.exists(paths["job"]):
        subprocess.run(["launchctl", "unload", paths["job"]], capture_output=True, check=False)
    elif paths["platform"] == "linux":
        subprocess.run(["systemctl", "--user", "disable", "--now", "plow-whip.timer"], capture_output=True, check=False)
    elif paths["platform"] == "windows":
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", "PlowWhipScheduler"], capture_output=True, check=False)
    for path in (paths["job"], paths["timer"], paths["state"]):
        if path and os.path.isfile(path):
            os.unlink(path)
    return {**paths, "installed": False}
