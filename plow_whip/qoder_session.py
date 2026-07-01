#!/usr/bin/env python3
"""
Qoder CN IDE Session Manager
============================

针对 Qoder CN (https://qoder.cn) IDE 的会话历史管理模块。

背景
----
Qoder CN 是一款基于 AI 的桌面 IDE，采用 JSONL 格式存储会话历史。
随着对话增长，上下文会膨胀导致性能下降。本模块提供：

1. **自动轮转** - 超阈值会话自动切割归档（配合 launchd 定时任务）
2. **安全切割** - JSON 对象边界检测，保证不切半条记录
3. **索引检索** - SQLite + FTS5 全文搜索历史会话
4. **回退机制** - 归档文件可合并回活跃会话

会话文件位置
-----------
Qoder CN 将会话存储在：
    ~/.qoder-cn/cache/projects/<project-hash>/conversation-history/task-<id>.jsonl

每条记录格式：
    {"role": "user/assistant", "message": {"content": [...]}}

使用方式
--------
命令行：
    python -m plow_whip.qoder_session rotate    # 轮转超阈值会话
    python -m plow_whip.qoder_session archives  # 列出所有归档
    python -m plow_whip.qoder_session rollback --task <id>  # 回退
    python -m plow_whip.qoder_session search <keyword>      # 搜索

Python API：
    from plow_whip.qoder_session import QoderSessionManager
    mgr = QoderSessionManager()
    mgr.run_rotation()  # 执行轮转
    mgr.search_sessions("Sprint")  # 搜索

建议 Qoder CN 提供的 API
----------------------
希望 Qoder CN 官方能提供会话操作 API，让开发者可以用代码管理会话，
而不是直接操作底层 JSONL 文件。详见 docs/qoder-cn-api-suggestion.md
"""

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple


class QoderSessionManager:
    """Qoder IDE 会话管理器"""
    
    DEFAULT_CONFIG = {
        "ide_session_dirs": [],  # 手动指定的会话目录
        "auto_discover": True,   # 是否自动扫描
        "rotation_config": {
            "line_threshold": 100,       # 行数阈值
            "size_threshold_kb": 20,     # 大小阈值
            "keep_recent_lines": 50,     # 保留最近行数
        },
        "index_db": "~/.plow-whip/session_index.db",
    }
    
    def __init__(self, config_path: str = "~/.plow-whip/qoder_sessions.yaml"):
        self.config_path = Path(config_path).expanduser()
        self.config = self._load_config()
        self.index_db = Path(self.config["index_db"]).expanduser()
        self._init_index_db()
    
    def _load_config(self) -> dict:
        """加载配置文件，不存在则创建默认配置"""
        if not self.config_path.exists():
            self._create_default_config()
        
        # 尝试 YAML 解析，失败则用 JSON
        try:
            import yaml
            with open(self.config_path, 'r') as f:
                return yaml.safe_load(f) or self.DEFAULT_CONFIG.copy()
        except ImportError:
            # PyYAML 未安装，尝试 JSON
            try:
                with open(self.config_path, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                return self.DEFAULT_CONFIG.copy()
    
    def _create_default_config(self):
        """创建默认配置文件"""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, 'w') as f:
            json.dump(self.DEFAULT_CONFIG, f, indent=2)
    
    def _init_index_db(self):
        """初始化 SQLite 索引库"""
        self.index_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.index_db))
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                summary TEXT,
                keywords TEXT,
                UNIQUE(task_id, file_path, start_line)
            )
        """)
        
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
                summary, keywords
            )
        """)
        
        conn.commit()
        conn.close()
    
    def discover_sessions(self) -> List[Path]:
        """自动发现所有项目的会话目录"""
        discovered = []
        
        # 扫描 ~/.qoder*/cache/projects/*/conversation-history/
        home = Path.home()
        for qoder_dir in home.glob(".qoder*"):
            projects_dir = qoder_dir / "cache" / "projects"
            if projects_dir.exists():
                for project_dir in projects_dir.iterdir():
                    history_dir = project_dir / "conversation-history"
                    if history_dir.exists() and history_dir.is_dir():
                        discovered.append(history_dir)
        
        # 合并手动配置的目录
        for dir_path in self.config.get("ide_session_dirs", []):
            path = Path(dir_path).expanduser()
            if path.exists() and path.is_dir():
                if path not in discovered:
                    discovered.append(path)
        
        return discovered
    
    def find_active_sessions(self) -> List[Tuple[Path, int]]:
        """查找需要轮转的活跃会话文件"""
        active = []
        session_dirs = self.discover_sessions()
        
        for session_dir in session_dirs:
            # 递归查找所有 task-*.jsonl 文件
            for task_file in session_dir.rglob("task-*.jsonl"):
                # 跳过 archive 和 by_rm 目录
                path_str = str(task_file)
                if "archive" in path_str or "by_rm" in path_str:
                    continue
                
                line_count = self._count_lines(task_file)
                size_kb = task_file.stat().st_size / 1024
                
                threshold = self.config["rotation_config"]
                if (line_count > threshold["line_threshold"] or 
                    size_kb > threshold["size_threshold_kb"]):
                    active.append((task_file, line_count))
        
        return active
    
    def _count_lines(self, file_path: Path) -> int:
        """统计文件行数"""
        with open(file_path, 'r', encoding='utf-8') as f:
            return sum(1 for _ in f)
    
    def rotate_session(self, task_file: Path) -> Optional[Path]:
        """
        切割归档单个会话文件
        
        Returns:
            归档文件路径，如果不需要轮转则返回 None
        """
        threshold = self.config["rotation_config"]
        keep_lines = threshold["keep_recent_lines"]
        
        # 读取所有行
        with open(task_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        total_lines = len(lines)
        size_kb = task_file.stat().st_size / 1024
        
        # 检查是否需要轮转（行数或大小任一超过阈值）
        needs_rotation = (
            total_lines > threshold["line_threshold"] or 
            size_kb > threshold["size_threshold_kb"]
        )
        
        if not needs_rotation:
            return None
        
        # 确定切割点
        if total_lines > threshold["line_threshold"]:
            # 行数超限：保留最后 keep_lines 行
            split_line = total_lines - keep_lines
        else:
            # 大小超限但行数不多：估算需要保留多少行
            avg_line_size = size_kb / total_lines  # KB per line
            target_lines = int(threshold["size_threshold_kb"] * 0.8 / avg_line_size)  # 留 20% 余量
            split_line = max(1, total_lines - min(target_lines, keep_lines))
        
        # 确保不切半条 JSON 记录
        # 从 split_line 位置向前找到完整的 JSON 对象边界
        split_line = self._find_json_boundary(lines, split_line)
        
        # 提取要归档的部分
        archive_lines = lines[:split_line]
        recent_lines = lines[split_line:]
        
        # 生成归档文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        task_id = task_file.stem  # e.g., "task-037"
        archive_name = f"{task_id}_{timestamp}.jsonl"
        archive_dir = task_file.parent / "archive"
        archive_dir.mkdir(exist_ok=True)
        archive_file = archive_dir / archive_name
        
        # 写入归档文件
        with open(archive_file, 'w', encoding='utf-8') as f:
            f.writelines(archive_lines)
        
        # 更新活跃文件（只保留最近部分）
        with open(task_file, 'w', encoding='utf-8') as f:
            f.writelines(recent_lines)
        
        # 生成摘要并建立索引
        summary = self._generate_summary(archive_lines)
        self._index_session(
            task_id=task_id,
            file_path=str(archive_file),
            start_line=1,
            end_line=len(archive_lines),
            summary=summary,
        )
        
        print(f"✓ Rotated {task_file.name}: {total_lines} → {len(recent_lines)} lines")
        print(f"  Archived to: {archive_file}")
        
        return archive_file
    
    def _find_json_boundary(self, lines: List[str], target_line: int) -> int:
        """
        找到 JSON 对象的完整边界
        
        从 target_line 向前搜索，确保不切半条 JSON 记录
        """
        # 如果目标行超过总行数，返回总行数（不需要切割）
        if target_line >= len(lines):
            return len(lines)
        
        # 逐行检查是否是完整的 JSON 对象
        for i in range(target_line - 1, max(0, target_line - 100), -1):
            line = lines[i].strip()
            if line.startswith('{') and line.endswith('}'):
                return i + 1
        
        # 如果没找到，就按目标行切割（可能不完整）
        return target_line
    
    def _generate_summary(self, lines: List[str]) -> str:
        """
        从 JSONL 行生成摘要
            
        提取关键信息：用户问题、主要操作、结果
        """
        import re
            
        user_queries = []
        assistant_highlights = []
        tool_names = set()
            
        for line in lines:
            try:
                record = json.loads(line.strip())
                role = record.get("role", "")
                message = record.get("message", {})
                content = message.get("content", []) if isinstance(message, dict) else record.get("content", [])
                    
                # 提取文本内容
                msg_text = ""
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            msg_text = item.get("text", "")
                            break
                elif isinstance(content, str):
                    msg_text = content
                    
                if not msg_text:
                    continue
                    
                # 提取用户问题（从 <user_query> 标签中）
                if role == "user" and len(user_queries) < 3:
                    query_match = re.search(r'<user_query>\s*(.*?)\s*</user_query>', msg_text, re.DOTALL)
                    if query_match:
                        query_text = query_match.group(1).strip()
                        # 截取前 80 字符
                        clean_query = query_text[:80].replace('\n', ' ')
                        user_queries.append(clean_query)
                    
                # 提取 assistant 关键信息（前几条非工具调用文本）
                if role == "assistant" and len(assistant_highlights) < 2:
                    # 跳过纯工具调用
                    if len(msg_text.strip()) > 20:
                        clean_text = msg_text[:100].replace('\n', ' ')
                        assistant_highlights.append(clean_text)
                    
                # 提取工具调用
                if role == "assistant":
                    tool_calls = message.get("tool_calls", []) if isinstance(message, dict) else []
                    if not tool_calls:
                        tool_calls = record.get("tool_calls", [])
                    for call in tool_calls:
                        func = call.get("function", {})
                        name = func.get("name", "")
                        if name:
                            tool_names.add(name)
                
            except (json.JSONDecodeError, AttributeError):
                continue
            
        # 组合摘要
        parts = []
        if user_queries:
            parts.append(f"Q: {user_queries[0]}")
        if assistant_highlights and not user_queries:
            parts.append(f"A: {assistant_highlights[0]}")
        if tool_names:
            tools_str = ', '.join(sorted(tool_names)[:5])
            parts.append(f"Tools: {tools_str}")
            
        return " | ".join(parts) if parts else "No summary available"
    
    def _index_session(self, task_id: str, file_path: str, 
                       start_line: int, end_line: int, summary: str):
        """将会话元数据存入索引库"""
        conn = sqlite3.connect(str(self.index_db))
        cursor = conn.cursor()
        
        # 提取关键词
        keywords = self._extract_keywords(summary)
        
        cursor.execute("""
            INSERT OR IGNORE INTO sessions 
            (task_id, file_path, start_line, end_line, created_at, summary, keywords)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id,
            file_path,
            start_line,
            end_line,
            datetime.now().isoformat(),
            summary,
            keywords,
        ))
        
        conn.commit()
        conn.close()
    
    def _extract_keywords(self, summary: str) -> str:
        """从摘要中提取关键词"""
        # 简单实现：提取常见技术关键词
        keywords = []
        tech_terms = [
            "Sprint", "DateTime", "middleware", "Service", "router",
            "test", "migration", "API", "frontend", "backend",
            "codex_cli", "qoder", "plow-whip", "Git", "commit"
        ]
        
        for term in tech_terms:
            if term.lower() in summary.lower():
                keywords.append(term)
        
        return ", ".join(keywords)
    
    def search_sessions(self, query: str) -> List[dict]:
        """
        搜索会话
        
        Args:
            query: 搜索关键词
            
        Returns:
            匹配的会话列表
        """
        conn = sqlite3.connect(str(self.index_db))
        cursor = conn.cursor()
        
        # 使用 LIKE 进行模糊搜索（更简单可靠）
        search_pattern = f"%{query}%"
        cursor.execute("""
            SELECT task_id, file_path, start_line, end_line, summary, keywords
            FROM sessions
            WHERE summary LIKE ? OR keywords LIKE ? OR task_id LIKE ?
            ORDER BY id DESC
            LIMIT 50
        """, (search_pattern, search_pattern, search_pattern))
        
        results = []
        for row in cursor.fetchall():
            results.append({
                "task_id": row[0],
                "file_path": row[1],
                "start_line": row[2],
                "end_line": row[3],
                "summary": row[4],
                "keywords": row[5],
            })
        
        conn.close()
        return results
    
    def restore_context(self, file_path: str, start_line: int, 
                        context_lines: int = 20) -> str:
        """
        从归档文件中恢复上下文
        
        Args:
            file_path: 归档文件路径
            start_line: 起始行号
            context_lines: 上下文行数（默认 20，最大 200）
            
        Returns:
            提取的 JSONL 内容字符串
        """
        context_lines = min(context_lines, 200)  # 限制最大 200 行
        
        with open(file_path, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()
        
        # 计算实际范围
        actual_start = max(0, start_line - 1)
        actual_end = min(len(all_lines), actual_start + context_lines)
        
        # 提取指定行
        selected_lines = all_lines[actual_start:actual_end]
        
        return "".join(selected_lines)
    
    def list_archives(self, task_id: Optional[str] = None) -> List[dict]:
        """
        列出所有归档文件
        
        Args:
            task_id: 可选，按 task_id 过滤
            
        Returns:
            归档信息列表 [{task_id, file_path, lines, size_kb, created_at}]
        """
        archives = []
        
        for session_dir in self.discover_sessions():
            archive_dir = session_dir / "archive"
            if not archive_dir.exists():
                # 也检查各 task 子目录下的 archive
                for adir in session_dir.rglob("archive"):
                    if adir.is_dir():
                        archive_dir = adir
                        break
            
            for archive_file in session_dir.rglob("archive/task-*.jsonl"):
                # 跳过 by_rm 目录
                if "by_rm" in str(archive_file):
                    continue
                if task_id and not archive_file.name.startswith(f"{task_id}_"):
                    continue
                
                line_count = self._count_lines(archive_file)
                size_kb = archive_file.stat().st_size / 1024
                # 从文件名提取时间戳
                parts = archive_file.stem.split("_")
                ts = parts[-1] if len(parts) >= 2 else "unknown"
                
                archives.append({
                    "task_id": archive_file.parent.parent.stem,
                    "file_path": str(archive_file),
                    "file_name": archive_file.name,
                    "lines": line_count,
                    "size_kb": round(size_kb, 1),
                    "timestamp": ts,
                })
        
        # 按时间戳降序
        archives.sort(key=lambda x: x["timestamp"], reverse=True)
        return archives
    
    def rollback(self, archive_path: str) -> bool:
        """
        回退：将归档文件合并回活跃文件
        
        逻辑：归档内容（旧）+ 当前活跃内容（新）→ 合并写入活跃文件
        归档文件移至 by_rm 安全删除
        
        Args:
            archive_path: 归档文件的绝对路径
            
        Returns:
            是否成功
        """
        archive_file = Path(archive_path)
        if not archive_file.exists():
            print(f"✗ Archive not found: {archive_file}")
            return False
        
        # 从归档路径推导活跃文件位置
        # archive: .../task-XXX/archive/task-XXX_timestamp.jsonl
        # active:  .../task-XXX/task-XXX.jsonl
        task_dir = archive_file.parent.parent  # task-XXX/
        task_id = task_dir.stem
        active_file = task_dir / f"{task_id}.jsonl"
        
        # 读取归档内容和活跃内容
        with open(archive_file, 'r', encoding='utf-8') as f:
            archive_lines = f.readlines()
        
        active_lines = []
        if active_file.exists():
            with open(active_file, 'r', encoding='utf-8') as f:
                active_lines = f.readlines()
        
        # 合并：归档（旧）在前，活跃（新）在后
        merged = archive_lines + active_lines
        
        # 写入活跃文件
        with open(active_file, 'w', encoding='utf-8') as f:
            f.writelines(merged)
        
        # 安全移除归档文件（移至 by_rm）
        self._safe_remove(archive_file)
        
        # 从索引库中移除记录
        conn = sqlite3.connect(str(self.index_db))
        conn.execute("DELETE FROM sessions WHERE file_path = ?", (str(archive_file),))
        conn.commit()
        conn.close()
        
        print(f"✓ Rolled back {task_id}: {len(archive_lines)} + {len(active_lines)} = {len(merged)} lines")
        print(f"  Restored to: {active_file}")
        
        return True
    
    def rollback_latest(self, task_id: str) -> bool:
        """回退指定 task 的最近一次归档"""
        archives = self.list_archives(task_id)
        if not archives:
            print(f"✗ No archives found for {task_id}")
            return False
        
        latest = archives[0]  # 最近一次
        return self.rollback(latest["file_path"])
    
    def _safe_remove(self, file_path: Path):
        """安全移除文件（移至 by_rm 目录）"""
        # 在项目目录下找 by_rm，否则在文件同级创建
        by_rm_dir = None
        for session_dir in self.discover_sessions():
            candidate = session_dir.parent.parent / "by_rm"
            if candidate.exists():
                by_rm_dir = candidate
                break
        
        if not by_rm_dir:
            by_rm_dir = file_path.parent.parent.parent / "by_rm"
        
        by_rm_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = by_rm_dir / f"{file_path.stem}_{timestamp}{file_path.suffix}"
        file_path.rename(dest)
    
    def run_rotation(self):
        """执行批量轮转"""
        print(" Scanning for sessions to rotate...")
        
        active_sessions = self.find_active_sessions()
        if not active_sessions:
            print("✓ No sessions need rotation.")
            return
        
        print(f"Found {len(active_sessions)} session(s) exceeding threshold.\n")
        
        for task_file, line_count in active_sessions:
            self.rotate_session(task_file)
        
        print(f"\n✓ Rotation complete.")


def main():
    """CLI 入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Qoder IDE Session Manager")
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    
    # rotate 命令
    rotate_parser = subparsers.add_parser("rotate", help="Rotate oversized sessions")
    rotate_parser.set_defaults(func=lambda args: manager.run_rotation())
    
    # search 命令
    search_parser = subparsers.add_parser("search", help="Search archived sessions")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--lines", type=int, default=20, 
                               help="Context lines to restore (default: 20, max: 200)")
    
    def handle_search(args):
        results = manager.search_sessions(args.query)
        if not results:
            print("No matching sessions found.")
            return
        
        print(f"Found {len(results)} matching session(s):\n")
        for i, result in enumerate(results, 1):
            print(f"{i}. [{result['task_id']}] {result['summary'][:60]}...")
            print(f"   File: {result['file_path']}")
            print(f"   Lines: {result['start_line']}-{result['end_line']}")
            print()
        
        # 交互式选择
        choice = input("Enter session number to restore context (or Enter to skip): ").strip()
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(results):
                selected = results[idx]
                context = manager.restore_context(
                    selected['file_path'],
                    selected['start_line'],
                    args.lines
                )
                print("\n--- Restored Context ---")
                print(context)
                print("--- End of Context ---\n")
    
    search_parser.set_defaults(func=handle_search)
    
    # rollback 命令
    rollback_parser = subparsers.add_parser("rollback", help="Rollback: merge archive back to active file")
    rollback_parser.add_argument("--task", help="Task ID to rollback (e.g., task-037)")
    rollback_parser.add_argument("--file", help="Specific archive file path to rollback")
    
    def handle_rollback(args):
        if args.file:
            manager.rollback(args.file)
        elif args.task:
            manager.rollback_latest(args.task)
        else:
            # 列出所有归档，让用户选
            archives = manager.list_archives()
            if not archives:
                print("No archives found.")
                return
            print(f"Found {len(archives)} archive(s):\n")
            for i, a in enumerate(archives, 1):
                print(f"{i}. [{a['task_id']}] {a['lines']} lines / {a['size_kb']}KB | {a['timestamp']}")
                print(f"   {a['file_name']}")
            print("\nUse --task <id> or --file <path> to rollback.")
    
    rollback_parser.set_defaults(func=handle_rollback)
    
    # archives 命令
    archives_parser = subparsers.add_parser("archives", help="List all archived sessions")
    archives_parser.add_argument("--task", help="Filter by task ID")
    
    def handle_archives(args):
        archives = manager.list_archives(args.task)
        if not archives:
            print("No archives found.")
            return
        print(f"Found {len(archives)} archive(s):\n")
        for a in archives:
            print(f"  [{a['task_id']}] {a['lines']:>4} lines | {a['size_kb']:>6}KB | {a['timestamp']} | {a['file_name']}")
    
    archives_parser.set_defaults(func=handle_archives)
    
    # discover 命令
    discover_parser = subparsers.add_parser("discover", help="Discover session directories")
    discover_parser.set_defaults(func=lambda args: print_discovered(manager))
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    manager = QoderSessionManager()
    args.func(args)


def print_discovered(manager: QoderSessionManager):
    """打印发现的会话目录"""
    dirs = manager.discover_sessions()
    if not dirs:
        print("No session directories found.")
        return
    
    print(f"Discovered {len(dirs)} session director(y/ies):\n")
    for d in dirs:
        print(f"  {d}")


if __name__ == "__main__":
    main()
