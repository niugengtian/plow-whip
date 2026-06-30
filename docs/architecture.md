# Architecture / 架构说明

## Overview

Plow-whip separates concerns into two layers:

```
Framework Layer (plow-whip/)     ← One copy, globally shared
├── Engine (agent_flow.py)        ← State machine + rotation + sync
├── Templates                     ← CONVENTIONS, memory, state templates
└── ADRs                          ← Framework-level decisions

Project Layer (<project>/collab/) ← One per project
├── State (AGENT_STATE.json)      ← Current turn, phase, context
├── Comms (AGENT_COMMS.md)        ← Inter-agent message board
├── Conventions                   ← Rules (rendered from template)
├── Conversations/                ← Per-agent session files
└── Memory/                       ← Multi-layer project memory
```

## State Machine

The state machine tracks whose turn it is. Each handoff:
1. Reads current state from AGENT_STATE.json
2. Switches to next agent (round-robin)
3. Updates phase, output, next action
4. Sends macOS notification (if available)

```
Human → Qoder → Codex → Qoder → Human → ...
```

## Multi-Layer Memory

| Layer | Files | Purpose | Load Time |
|-------|-------|---------|-----------|
| Hot | PROJECT + STATUS + NEXT_ACTION | Current state | <30s |
| Warm | ROADMAP + DECISIONS + Sprint | Context | <2min |
| Cold | Archive / Sessions | History | On demand |

Each agent starts by reading the Hot Layer (<30s), then loads more only if needed.

## Conversation Rotation

Like log rotation for AI conversations:

```
current.md (active, small)
    ↓ [exceeds 100 lines or 8KB]
    ↓ rotate
    ↓
YYYYMMDD_topic.md (archived, with summary header)
current.md (fresh, empty template)
```

Result: 100K tokens → 2KB summary. Next session reads 2KB instead of 100K.

## Sync Mechanism

```
Framework updated (new template version)
    ↓
plow-whip sync
    ↓
For each project with collab/:
    Render template with {PROJECT_NAME}
    Write to project's collab/ directory
```

---

## 中文

### 架构概览

耕田之鞭将关注点分为两层：

- **框架层**：全局一份，包含引擎、模板、框架级决策
- **项目层**：每个项目一份，包含状态、留言板、会话、记忆

### 状态机

跟踪当前轮次。每次交接：读取状态 → 切换到下一个 Agent → 更新产出和下一步。

### 多层记忆

Hot（热）→ Warm（温）→ Cold（冷）。每个 Agent 启动只读热层（<30秒），按需加载更多。

### 会话轮转

类似 log rotation。current.md 超过阈值时归档为带摘要的文件，重建空模板。压缩比约 98%。

### 同步机制

框架更新后，`plow-whip sync` 遍历所有项目，用模板重新渲染并写入。
