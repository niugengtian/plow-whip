#!/usr/bin/env python3
"""
inbox_watcher.py — Qoder CN 会话唤醒器

监听 inbox 文件变化，当有新任务时：
1. 终端打印醒目提示
2. 发送 macOS 通知
3. 把 Qoder CN 拉到前台
4. 显示任务内容

用法:
    python3 -m plow_whip.inbox_watcher --agent qoder
    python3 -m plow_whip.inbox_watcher --agent qoder --interval 5
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

INBOX_DIR = os.path.join(os.path.expanduser("~"), ".plow-whip", "inbox")


def get_inbox_file(agent: str) -> str:
    return os.path.join(INBOX_DIR, f"{agent}.json")


def read_inbox(agent: str) -> list:
    inbox_file = get_inbox_file(agent)
    if not os.path.exists(inbox_file):
        return []
    try:
        with open(inbox_file, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def bring_qoder_to_foreground():
    """把 Qoder CN 拉到前台。"""
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "Qoder CN" to activate'],
            capture_output=True, timeout=3,
        )
        return True
    except (subprocess.TimeoutExpired, Exception):
        return False


def send_notification(agent: str, task: dict):
    """发送 macOS 通知。"""
    project = task.get("project", "unknown")
    prompt = task.get("prompt", "新任务")[:100]
    message = f"[{project}] {prompt}"
    
    script = f'display notification "{message}" with title "🪢 耕田之鞭 — {agent} 被鞭了!" sound name "Glass"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass


def print_alert(agent: str, task: dict):
    """在终端打印醒目提示。"""
    now = datetime.now().strftime("%H:%M:%S")
    project = task.get("project", "unknown")
    prompt = task.get("prompt", "新任务")
    
    print()
    print("=" * 60)
    print(f"  🪢  上 帝 之 鞭  —  {agent} 挨鞭了！")
    print("=" * 60)
    print(f"  时间: {now}")
    print(f"  项目: {project}")
    print(f"  任务: {prompt}")
    print("=" * 60)
    print()
    print("  👉 请在 Qoder CN 中切换到对应会话开始工作")
    print()


def watch_inbox(agent: str, interval: float):
    """持续监听 inbox 文件。"""
    inbox_file = get_inbox_file(agent)
    last_mtime = 0
    
    print(f"🪢 耕田之鞭 — 监听 {agent} 的 inbox")
    print(f"   文件: {inbox_file}")
    print(f"   间隔: {interval} 秒")
    print(f"   Ctrl+C 退出")
    print()
    
    try:
        while True:
            if os.path.exists(inbox_file):
                mtime = os.path.getmtime(inbox_file)
                if mtime != last_mtime:
                    last_mtime = mtime
                    tasks = read_inbox(agent)
                    pending = [t for t in tasks if t.get("status") == "pending"]
                    
                    if pending:
                        task = pending[0]  # 取第一个待办
                        print_alert(agent, task)
                        send_notification(agent, task)
                        bring_qoder_to_foreground()
            
            time.sleep(interval)
    
    except KeyboardInterrupt:
        print(f"\n👋 已停止监听 {agent}")


def main():
    parser = argparse.ArgumentParser(description="Qoder CN 会话唤醒器")
    parser.add_argument("--agent", default="qoder", help="Agent 名称 (default: qoder)")
    parser.add_argument("--interval", type=float, default=5, help="检查间隔秒数 (default: 5)")
    args = parser.parse_args()
    
    watch_inbox(args.agent, args.interval)


if __name__ == "__main__":
    main()
