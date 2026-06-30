#!/usr/bin/env python3
"""
brain.py — 廉价大脑：用 DeepSeek API 处理简单任务

设计理念：
  - 简单任务（代码生成、格式化、搜索、模板）→ DeepSeek（便宜）
  - 复杂任务（架构决策、PM 规划、深度 debug）→ 上报给主 Agent（贵）

用法:
    from plow_whip.brain import Brain
    brain = Brain()
    result = brain.think("生成一个 Python 函数，计算斐波那契数列")
    if result["routed"] == "deepseek":
        print(result["output"])
    else:
        print("需要上报给主 Agent")
"""

import json
import os
import re
import sys
import time
from datetime import datetime

try:
    import httpx
except ImportError:
    httpx = None


# ── 配置 ─────────────────────────────────────────────────────────────────────

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT = 60  # 秒

# 复杂度分类关键词
COMPLEX_KEYWORDS = [
    "架构", "设计模式", "重构", "迁移", "性能优化",
    "安全", "权限", "认证", "加密",
    "数据库", "schema", "migration",
    "API 设计", "接口设计", "协议",
    "调试", "debug", "排查", "诊断",
    "规划", "PRD", "需求分析", "Sprint",
    "决策", "trade-off", "权衡",
]

SIMPLE_KEYWORDS = [
    "生成", "创建", "写一个", "实现",
    "格式化", "转换", "解析",
    "搜索", "查找", "匹配",
    "模板", "示例", "样例",
    "修复", "bug fix", "patch",
    "测试", "单元测试", "test case",
    "文档", "注释", "README",
    "翻译", "解释", "说明",
]


# ── API Key 加载 ──────────────────────────────────────────────────────────────

def _load_api_key() -> str:
    """
    加载 DeepSeek API Key，优先级：
    1. 环境变量 DEEPSEEK_API_KEY
    2. ~/.config/deepseek/env 文件
    """
    # 1. 环境变量
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key

    # 2. 配置文件
    config_path = os.path.expanduser("~/.config/deepseek/env")
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("export DEEPSEEK_API_KEY="):
                    # 提取引号内的值
                    match = re.search(r'["\'](.+?)["\']', line)
                    if match:
                        return match.group(1)
    return ""


# ── 复杂度分类器 ──────────────────────────────────────────────────────────────

def classify_complexity(task: str) -> dict:
    """
    判断任务复杂度。
    返回 {"level": "simple"|"complex", "reason": str, "score": int}
    """
    task_lower = task.lower()

    # 统计复杂关键词
    complex_hits = sum(1 for kw in COMPLEX_KEYWORDS if kw.lower() in task_lower)
    simple_hits = sum(1 for kw in SIMPLE_KEYWORDS if kw.lower() in task_lower)

    # 评分：复杂关键词权重更高
    score = complex_hits * 2 - simple_hits

    # 长度也是指标：超长任务通常更复杂
    if len(task) > 500:
        score += 2
    if len(task) > 1000:
        score += 2

    # 包含代码块通常是复杂任务
    if "```" in task or "def " in task or "class " in task:
        score += 1

    if score >= 2:
        return {"level": "complex", "reason": f"复杂关键词 {complex_hits} 个", "score": score}
    else:
        return {"level": "simple", "reason": f"简单关键词 {simple_hits} 个", "score": score}


# ── Brain 类 ──────────────────────────────────────────────────────────────────

class Brain:
    """DeepSeek 廉价大脑"""

    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or _load_api_key()
        self.model = model or os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL)
        self._client = None

    @property
    def available(self) -> bool:
        """检查 Brain 是否可用（有 key 且有 httpx）"""
        return bool(self.api_key) and httpx is not None

    def _get_client(self):
        if self._client is None:
            if httpx is None:
                raise RuntimeError("httpx 未安装，请运行: pip install httpx")
            self._client = httpx.Client(timeout=DEFAULT_TIMEOUT)
        return self._client

    def think(self, task: str, context: str = "") -> dict:
        """
        让 Brain 思考一个任务。

        参数:
          task: 任务描述
          context: 可选的上下文信息

        返回:
          {
            "routed": "deepseek" | "escalate",
            "complexity": {"level": "simple"|"complex", "reason": str, "score": int},
            "output": str,  # 如果 routed=deepseek
            "reason": str,  # 如果 routed=escalate
          }
        """
        # 1. 分类复杂度
        complexity = classify_complexity(task)

        if complexity["level"] == "complex":
            return {
                "routed": "escalate",
                "complexity": complexity,
                "reason": f"任务复杂度 {complexity['score']}，需要主 Agent 处理: {complexity['reason']}",
            }

        # 2. 简单任务 → DeepSeek
        if not self.available:
            return {
                "routed": "escalate",
                "complexity": complexity,
                "reason": "DeepSeek API 不可用（无 key 或缺少 httpx）",
            }

        try:
            output = self._call_deepseek(task, context)
            return {
                "routed": "deepseek",
                "complexity": complexity,
                "output": output,
            }
        except Exception as e:
            return {
                "routed": "escalate",
                "complexity": complexity,
                "reason": f"DeepSeek 调用失败: {e}",
            }

    def _call_deepseek(self, task: str, context: str) -> str:
        """调用 DeepSeek API"""
        system_prompt = (
            "你是一个高效的代码助手，负责快速完成简单编程任务。\n"
            "要求：\n"
            "- 直接给出答案或代码，不要过多解释\n"
            "- 代码要可运行、有基本错误处理\n"
            "- 如果任务不明确，给出最合理的默认实现\n"
        )

        messages = [{"role": "system", "content": system_prompt}]

        user_content = task
        if context:
            user_content = f"上下文:\n{context}\n\n任务:\n{task}"
        messages.append({"role": "user", "content": user_content})

        client = self._get_client()
        response = client.post(
            DEEPSEEK_API_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": messages,
                "temperature": 0.3,  # 低温度，更确定
                "max_tokens": 2000,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    def quick_code(self, description: str, language: str = "python") -> dict:
        """快速生成代码片段"""
        task = f"用 {language} 实现: {description}\n只返回代码，不要解释。"
        return self.think(task)

    def review_code(self, code: str, focus: str = "正确性") -> dict:
        """快速代码审查"""
        task = f"审查以下代码，重点关注{focus}:\n```{code}```\n列出发现的问题（如有），并给出修复建议。"
        # 代码审查通常较复杂，但如果代码短小可以处理
        if len(code) < 200:
            return self.think(task)
        else:
            return {
                "routed": "escalate",
                "complexity": {"level": "complex", "reason": "代码较长，需要主 Agent 审查", "score": 3},
                "reason": "代码量较大，建议主 Agent 处理",
            }


# ── CLI 接口 ──────────────────────────────────────────────────────────────────

def cmd_brain(args):
    """brain 子命令：用 DeepSeek 处理简单任务"""
    brain = Brain()

    if not brain.available:
        if not brain.api_key:
            print("❌ DeepSeek API Key 未配置")
            print("   请设置环境变量 DEEPSEEK_API_KEY 或创建 ~/.config/deepseek/env")
        if httpx is None:
            print("❌ httpx 未安装")
            print("   请运行: pip install httpx")
        sys.exit(1)

    task = args.task
    context = getattr(args, "context", "") or ""

    # 分类
    complexity = classify_complexity(task)
    print(f"\n🧠 Brain 分析:")
    print(f"   复杂度: {complexity['level']} (score={complexity['score']})")
    print(f"   原因: {complexity['reason']}")
    print()

    if complexity["level"] == "complex" and not getattr(args, "force", False):
        print("⚠️  任务较复杂，建议上报给主 Agent")
        print("   使用 --force 强制用 DeepSeek 处理")
        return

    # 执行
    print("🤔 DeepSeek 思考中...")
    start = time.time()
    result = brain.think(task, context)
    elapsed = time.time() - start

    if result["routed"] == "deepseek":
        print(f"\n✅ DeepSeek 完成 ({elapsed:.1f}s):\n")
        print(result["output"])
        print(f"\n---\n💰 成本: ~{len(result['output']) / 1000 * 0.00014:.4f} USD (DeepSeek 价格)")
    else:
        print(f"\n❌ 无法处理: {result['reason']}")


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main():
    """独立运行入口"""
    import argparse
    parser = argparse.ArgumentParser(description="🧠 DeepSeek 廉价大脑")
    parser.add_argument("task", help="任务描述")
    parser.add_argument("--context", help="上下文信息")
    parser.add_argument("--force", action="store_true", help="强制用 DeepSeek 处理复杂任务")
    args = parser.parse_args()
    cmd_brain(args)


if __name__ == "__main__":
    main()
