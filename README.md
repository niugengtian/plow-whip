# 🪢 Plow-Whip — 耕田之鞭

**Multi-Agent Collaboration Framework / 多Agent协作工具**

[English](#english) | [中文](#中文)

---

<a id="english"></a>
## 🇬🇧 English

### What is Plow-Whip?

Plow-Whip is a lightweight framework for orchestrating multiple AI coding agents (Codex, Cursor, Qoder, ChatGPT, etc.) on a single project. It provides:

- **State Machine** — Turn-based handoff between AI agents with full context
- **Conversation Rotation** — Like log rotation for AI conversations; keeps context small and focused
- **Multi-Layer Memory** — Hot/Warm/Cold memory architecture so each agent starts with just 1-2KB instead of 100K+ tokens
- **Template System** — Define collaboration rules once, sync to all projects
- **Traceability** — Every decision, every handoff, every AI output is recorded

### Why?

When you use 3-4 AI tools on one project, context gets lost. ChatGPT tells Codex one thing, Qoder says another, and nobody remembers what was decided last Tuesday.

Plow-Whip solves this with:
1. **Project files as source of truth** — Chat logs are ephemeral; files persist
2. **Single-direction responsibility** — Each AI has a clear role; no conflicting instructions
3. **Automatic context compression** — 100K tokens → 2KB summary via conversation rotation

### Quick Start

```bash
pip install plow-whip

# Initialize a new project
plow-whip init --projects-dir ~/my-projects --project MyProject

# Daily usage
plow-whip status --project MyProject
plow-whip handoff --project MyProject --output "Done X" --next "Do Y"
plow-whip rotate --project MyProject --agent qoder --topic "Feature A" --summary "Implemented A"
plow-whip sync  # Push framework updates to all projects
```

### Architecture

```
plow-whip/                    ← This framework (one copy, globally)
├── plow_whip/
│   ├── agent_flow.py         ← State machine + rotation engine
│   ├── templates/            ← Project templates
│   └── adr/                  ← Framework-level ADRs

MyProject/
└── collab/                   ← Project instance (per project)
    ├── AGENT_STATE.json      ← Current turn
    ├── AGENT_COMMS.md        ← Message board
    ├── CONVENTIONS.md        ← Rules (from template)
    ├── conversations/        ← Per-agent session files
    └── memory/               ← Multi-layer project memory
```

### Core Concepts

| Concept | Description |
|---------|-------------|
| **State Machine** | Agents take turns. Handoff carries full context. |
| **Conversation Rotation** | Like logrotate — archive old context, start fresh. |
| **Multi-Layer Memory** | Hot (status+next) → Warm (roadmap+decisions) → Cold (archive) |
| **Sync** | Update framework → propagate to all projects. |

### License

MIT

---

<a id="中文"></a>
## 🇨🇳 中文

### 耕田之鞭是什么？

耕田之鞭是一个轻量级框架，用于在一个项目上协调多个 AI 编程助手（Codex、Cursor、Qoder、ChatGPT 等）。它提供：

- **状态机** — AI 之间的轮次交接，携带完整上下文
- **会话轮转** — 类似 log rotation，保持上下文精简聚焦
- **多层记忆** — Hot/Warm/Cold 三层架构，每个 AI 启动只需读 1-2KB 而非 100K+ tokens
- **模板系统** — 协作规则定义一次，同步到所有项目
- **全链路溯源** — 每个决策、每次交接、每条 AI 输出都有记录

### 为什么需要它？

当你在一个项目上同时用 3-4 个 AI 工具时，上下文会丢失。ChatGPT 告诉 Codex 一件事，Qoder 说了另一件事，没人记得上周二决定了什么。

耕田之鞭的解决方案：
1. **项目文件是唯一真相** — 聊天记录是临时的，文件是持久的
2. **单向职责** — 每个 AI 角色明确，不存在矛盾指令
3. **自动上下文压缩** — 100K tokens → 2KB 摘要，通过会话轮转实现

### 快速开始

```bash
pip install plow-whip

# 初始化新项目
plow-whip init --projects-dir ~/my-projects --project MyProject

# 日常使用
plow-whip status --project MyProject          # 查看状态
plow-whip handoff --project MyProject ...     # 交接轮次
plow-whip rotate --project MyProject ...      # 会话轮转
plow-whip sync                                # 同步框架更新
```

### 架构

```
plow-whip/                    ← 框架（全局一份）
├── plow_whip/
│   ├── agent_flow.py         ← 状态机 + 轮转引擎
│   ├── templates/            ← 项目模板
│   └── adr/                  ← 框架级 ADR

MyProject/
└── collab/                   ← 项目实例（每个项目一份）
    ├── AGENT_STATE.json      ← 当前轮次
    ├── AGENT_COMMS.md        ← 留言板
    ├── CONVENTIONS.md        ← 协作规则（从模板生成）
    ├── conversations/        ← 每个 AI 的会话文件
    └── memory/               ← 多层项目记忆
```

### 核心概念

| 概念 | 说明 |
|------|------|
| **状态机** | AI 轮流工作，交接时携带完整上下文 |
| **会话轮转** | 类似 logrotate — 归档旧上下文，重新开始 |
| **多层记忆** | Hot（状态+待办）→ Warm（路线图+决策）→ Cold（归档）|
| **同步** | 更新框架 → 传播到所有项目 |

### 许可

MIT
