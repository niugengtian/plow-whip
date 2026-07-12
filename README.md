# 🪢 plow-whip

> **v1.0.0** — 多 Agent 协作的鞭策引擎 / Multi-Agent Collaboration Whip Engine

[English](#english) | [中文](#chinese)

---

## 中文

### 简介

plow-whip（耕田之鞭）是一个多 Agent 协作框架，管理**可配置 Agent 阵容**，通过 **whip（耕田之鞭）** 驱动摸鱼 Agent，用 **DeepSeek 廉价大脑** 处理简单任务，实现高效项目交付。

默认情况下，Agent 不允许自行创建或调用子智能体；除非用户在当前任务中临时明确指定，所有跨 Agent 作业都必须通过 plow-whip 的三层记忆、留言板、状态机和 handoff/drive 机制接力，避免记忆断层。

默认情况下，Agent 只能读取和修改当前项目根目录内的文件；不得擅自修改全局配置、其他项目目录、其他项目 agent 会话或全局 agent 阵容。

### 架构

```
┌─────────────────────────────────────────────────┐
│                    plow-whip                     │
├──────────────┬──────────────┬───────────────────┤
│   whip.py    │   brain.py   │    dispatch.py    │
│  (耕田之鞭)  │ (DeepSeek大脑)│   (投递通道)      │
├──────────────┴──────────────┴───────────────────┤
│               Configurable Agents                │
│  planner / builder / reviewer / ...              │
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

# 新建项目并一键接入 plow-whip 规则
plow-whip --project MyProject new --owner codex --first-action "clarify requirements"

# 查看状态
plow-whip --project MyProject status

# 进入项目/新会话第一步：检查 plow-whip 机制，缺失则建立
plow-whip --project MyProject doctor --repair

# 生成最小唤醒上下文包，避免读全文
plow-whip --project MyProject context-pack --agent codex

# 检查 Hot/Warm/Cold 记忆 token 预算
plow-whip --project MyProject memory-budget

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
| `new` | 新建项目目录并一键初始化 plow-whip 规则 |
| `status` | 查看项目状态 |
| `doctor` | 检查 plow-whip 机制是否存在，`--repair` 可补齐缺失结构 |
| `handoff` | 交接给下一个或指定 Agent（自动轮转会话） |
| `context-pack` | 生成最小唤醒上下文包，优先给 Agent 读 |
| `memory-budget` | 检查 Hot/Warm/Cold token 预算，不读正文 |
| `whip` | 耕田之鞭 — 驱动摸鱼 Agent |
| `brain` | DeepSeek 廉价大脑 — 简单任务直接完成 |
| `memory-rotate` | 自动轮转所有记忆文件 |
| `rotate` | 手动轮转会话 |
| `permit` | 设置投递权限 |
| `agent` | 查看或修改 Agent 角色和作业分配 |
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

`whip --crack` 只发送项目路径和关键协作文件路径；同一个任务未变化时会用 `last_wake_hash` 跳过重复投递。

```bash
plow-whip --project MyProject handoff --to builder --output "done" --next "review this" --blockers "none"
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
| `cursor_cli` | 唤醒 Cursor CLI |
| `codex_cli` | 唤醒 Codex CLI |
| `brain` | DeepSeek 处理简单任务 |
| `file` | 写入任务收件箱 |
| `notify` | macOS 通知 |

### Agent 阵容

Agent 名称、角色、作业分配都来自 `~/.plow-whip/config.json`：

```bash
plow-whip configure --projects-dir ~/projects --agents planner builder reviewer
plow-whip agent set planner --role "PM / Architect" --assignment "Break work into tasks"
plow-whip agent set builder --role "Code Owner" --assignment "Implement scoped tasks"
plow-whip agent list
```

新项目初始化时会生成独立的 `collab/AGENTS.md`、`AGENT_STATE.json`、`AGENT_COMMS.md` 和约定文件。

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

plow-whip is a multi-agent collaboration framework that manages a **configurable agent lineup**, drives idle agents with the **whip**, and handles simple tasks with **DeepSeek brain**.

### Architecture

```
┌─────────────────────────────────────────────────┐
│                    plow-whip                     │
├──────────────┬──────────────┬───────────────────┤
│   whip.py    │   brain.py   │    dispatch.py    │
│  (The Plow Whip)  │ (DeepSeek)   │    (Dispatch)     │
├──────────────┴──────────────┴───────────────────┤
│               Configurable Agents                │
│  planner / builder / reviewer / ...              │
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

# Create a new project with plow-whip rules
plow-whip --project MyProject new --owner codex --first-action "clarify requirements"

# Check status
plow-whip --project MyProject status

# First step when entering a project/session: verify or repair plow-whip
plow-whip --project MyProject doctor --repair

# Print a minimal wakeup context pack
plow-whip --project MyProject context-pack --agent codex

# Check Hot/Warm/Cold memory token budgets
plow-whip --project MyProject memory-budget

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
| `new` | Create a project and initialize plow-whip rules |
| `status` | View project status |
| `doctor` | Check plow-whip mechanism; `--repair` fills missing structure |
| `handoff` | Handoff to next or specific agent (auto-rotates session) |
| `context-pack` | Print minimal wakeup context for an agent |
| `memory-budget` | Check Hot/Warm/Cold token budgets without reading content |
| `whip` | The Plow Whip — drive idle agents |
| `brain` | DeepSeek brain for simple tasks |
| `memory-rotate` | Auto-rotate all memory files |
| `rotate` | Manual session rotation |
| `permit` | Set dispatch permissions |
| `agent` | List or edit agent roles and assignments |

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
| `cursor_cli` | Wake Cursor CLI |
| `codex_cli` | Wake Codex CLI |
| `brain` | DeepSeek processing |
| `file` | Task inbox write |
| `notify` | macOS notification |

### Agent Lineup

Agent names, roles, and assignments come from `~/.plow-whip/config.json`:

```bash
plow-whip configure --projects-dir ~/projects --agents planner builder reviewer
plow-whip agent set planner --role "PM / Architect" --assignment "Break work into tasks"
plow-whip agent set builder --role "Code Owner" --assignment "Implement scoped tasks"
plow-whip agent list
```

Each initialized project gets its own `collab/AGENTS.md`, `AGENT_STATE.json`, `AGENT_COMMS.md`, and conventions.

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
