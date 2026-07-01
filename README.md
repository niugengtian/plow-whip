# 🪢 plow-whip

> **v1.0.0** — 多 Agent 协作的鞭策引擎 / Multi-Agent Collaboration Whip Engine

[English](#english) | [中文](#chinese)

---

## 中文

### 简介

plow-whip（耕田之鞭）是一个多 Agent 协作框架，管理 **4 Agent 阵容**，通过 **whip（耕田之鞭）** 驱动摸鱼 Agent，用 **DeepSeek 廉价大脑** 处理简单任务，实现高效项目交付。

### 架构

```
┌─────────────────────────────────────────────────┐
│                    plow-whip                     │
├──────────────┬──────────────┬───────────────────┤
│   whip.py    │   brain.py   │    dispatch.py    │
│  (耕田之鞭)  │ (DeepSeek大脑)│   (投递通道)      │
├──────────────┴──────────────┴───────────────────┤
│               4 Agent 阵容                       │
│  🔵 qoder (PM)  │  🔷 qoder_cli (审查)          │
│  🟢 codex (学习) │  🟩 codex_cli (开发)          │
├─────────────────────────────────────────────────┤
│            memory-rotate 自动轮转                │
│    Hot → Warm → Cold 多层记忆                   │
└─────────────────────────────────────────────────┘
```

### 快速开始

```bash
# 安装
pip install plow-whip

# 初始化项目
plow-whip --project MyProject init

# 查看状态
plow-whip --project MyProject status

# 挥舞耕田之鞭 — 扫描摸鱼 Agent
plow-whip whip

# 实际投递 + 启用 DeepSeek 大脑
plow-whip whip --crack --brain

# 持续自动挥舞 + 自动轮转
plow-whip whip --auto-crack --auto-rotate

# 使用 DeepSeek 处理简单任务
plow-whip brain "写一个 Python 函数判断回文"

# 一次检查所有记忆文件健康状态
plow-whip --project MyProject memory-rotate
```

### 核心命令

| 命令 | 功能 |
|------|------|
| `init` | 初始化项目（collab/ 目录 + 模板） |
| `status` | 查看项目状态 |
| `handoff` | 交接给下一个 Agent（自动轮转会话） |
| `whip` | 耕田之鞭 — 驱动摸鱼 Agent |
| `brain` | DeepSeek 廉价大脑 — 简单任务直接完成 |
| `memory-rotate` | 自动轮转所有记忆文件 |
| `rotate` | 手动轮转会话 |
| `permit` | 设置投递权限 |
| `watch` | 监控项目状态变化 |
| `bind-tab` | 绑定项目到 zellij tab |

### 耕田之鞭 (whip)

```bash
plow-whip whip                    # 扫描报告：谁在摸鱼
plow-whip whip --crack            # 抽鞭！实际投递任务
plow-whip whip --auto-crack       # 持续自动挥舞
plow-whip whip --daemon           # 持续监控模式
plow-whip whip --auto-rotate      # 自动轮转超限会话
plow-whip whip --brain            # 简单任务交给 DeepSeek
```

### DeepSeek 大脑 (brain)

```bash
plow-whip brain "写一个排序算法"   # 简单 → DeepSeek 1.7s 完成
plow-whip brain "设计微服务架构"   # 复杂 → 建议上报主 Agent
```

复杂度自动分类：关键词匹配 + 长度权重 + 代码块检测

### 投递通道

| 通道 | 说明 |
|------|------|
| `zellij` | 注入共享终端 |
| `qoder_cli` | 唤醒 Qoder CLI |
| `codex_cli` | 唤醒 Codex CLI |
| `brain` | DeepSeek 处理简单任务 |
| `file` | 写入任务收件箱 |
| `notify` | macOS 通知 |

### 4 Agent 阵容

| Agent | 角色 | 可被 whip 驱动 |
|-------|------|---------------|
| 🔵 qoder | PM + 架构师 | ❌ 主对话窗口 |
| 🔷 qoder_cli | 审查 + 验收 | ✅ |
| 🟢 codex | 闲置/学习 | ❌ |
| 🟩 codex_cli | Code Owner | ✅ |

### 自动轮转

- **Agent 会话**: 100行/8KB → 归档 + 重建模板
- **Collab 文件**: 80行/6KB → 保留最新30行
- **触发点**: handoff、whip daemon、memory-rotate

### 许可

MIT

---

<a id="english"></a>
## English

### Introduction

plow-whip is a multi-agent collaboration framework that manages a **4-agent lineup**, drives idle agents with the **whip**, and handles simple tasks with **DeepSeek brain**.

### Architecture

```
┌─────────────────────────────────────────────────┐
│                    plow-whip                     │
├──────────────┬──────────────┬───────────────────┤
│   whip.py    │   brain.py   │    dispatch.py    │
│  (The Plow Whip)  │ (DeepSeek)   │    (Dispatch)     │
├──────────────┴──────────────┴───────────────────┤
│               4 Agent Lineup                     │
│  🔵 qoder (PM)   │  🔷 qoder_cli (Review)       │
│  🟢 codex (Idle)  │  🟩 codex_cli (Dev)         │
├─────────────────────────────────────────────────┤
│            memory-rotate                         │
│    Hot → Warm → Cold Memory Layers              │
└─────────────────────────────────────────────────┘
```

### Quick Start

```bash
# Install
pip install plow-whip

# Initialize project
plow-whip --project MyProject init

# Check status
plow-whip --project MyProject status

# Crack the plow-whip
plow-whip whip --crack --brain

# Daemon mode with auto-rotate
plow-whip whip --auto-crack --auto-rotate

# Use DeepSeek for simple tasks
plow-whip brain "write a palindrome checker"
```

### Core Commands

| Command | Description |
|---------|-------------|
| `init` | Initialize project |
| `status` | View project status |
| `handoff` | Handoff to next agent (auto-rotates session) |
| `whip` | The Plow Whip — drive idle agents |
| `brain` | DeepSeek brain for simple tasks |
| `memory-rotate` | Auto-rotate all memory files |
| `rotate` | Manual session rotation |
| `permit` | Set dispatch permissions |

### The Plow Whip

```bash
plow-whip whip                    # Scan: who is slacking?
plow-whip whip --crack            # Crack! Dispatch tasks
plow-whip whip --auto-crack       # Continuous auto-dispatch
plow-whip whip --daemon           # Daemon monitoring mode
plow-whip whip --auto-rotate      # Auto-rotate oversized sessions
plow-whip whip --brain            # Simple tasks → DeepSeek
```

### DeepSeek Brain

```bash
plow-whip brain "write a sort function"    # Simple → DeepSeek 1.7s
plow-whip brain "design microservices"     # Complex → escalate
```

Auto-classification: keyword matching + length weight + code block detection

### Dispatch Channels

| Channel | Description |
|---------|-------------|
| `zellij` | Shared terminal injection |
| `qoder_cli` | Wake Qoder CLI |
| `codex_cli` | Wake Codex CLI |
| `brain` | DeepSeek processing |
| `file` | Task inbox write |
| `notify` | macOS notification |

### 4-Agent Lineup

| Agent | Role | Whip-drivable |
|-------|------|---------------|
| 🔵 qoder | PM + Architect | ❌ Main dialog |
| 🔷 qoder_cli | Review + Accept | ✅ |
| 🟢 codex | Idle/Learning | ❌ |
| 🟩 codex_cli | Code Owner | ✅ |

### Auto-Rotation

- **Agent sessions**: 100 lines/8KB → archive + rebuild
- **Collab files**: 80 lines/6KB → keep latest 30 lines
- **Triggers**: handoff, whip daemon, memory-rotate

### License

MIT

### Qoder CN IDE Session Manager

针对 [Qoder CN](https://qoder.cn) IDE 的会话历史管理模块。

**背景**：Qoder CN 采用 JSONL 格式存储会话历史，随着对话增长会导致上下文膨胀。
本模块提供自动轮转、安全切割、索引检索和回退机制。

```bash
# 轮转超阈值会话（配合 launchd 定时任务）
python -m plow_whip.qoder_session rotate

# 列出所有归档
python -m plow_whip.qoder_session archives

# 回退最近一次归档
python -m plow_whip.qoder_session rollback --task task-037

# 搜索历史会话
python -m plow_whip.qoder_session search "Sprint"
```

**Python API**：
```python
from plow_whip.qoder_session import QoderSessionManager

mgr = QoderSessionManager()
mgr.run_rotation()                    # 执行轮转
results = mgr.search_sessions("API")  # 搜索历史
mgr.rollback_latest("task-037")       # 回退
```

**自动轮转配置**（launchd）：
- 脚本位置：`~/.plow-whip/qoder_session_manager.py`
- 配置文件：`~/.plow-whip/qoder_sessions.yaml`
- 执行频率：每 30 分钟

详见 [docs/qoder-cn-api-suggestion.md](docs/qoder-cn-api-suggestion.md) 了解我们对 Qoder CN 官方提供会话 API 的建议。

