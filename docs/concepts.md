# Core Concepts / 核心概念

## 1. Single-Direction Responsibility / 单向职责

Each AI has a clear role. No conflicting instructions.

| Role | Responsibility |
|------|---------------|
| Human | Final decisions, acceptance |
| Qoder | PM + Architect |
| Codex | Code Owner |
| Cursor | Bug Reporter |

**Rule:** Project files are the single source of truth. Chat logs are ephemeral.

## 2. State Machine Handoff / 状态机交接

Agents take turns. Each handoff carries:
- What was done (last_output)
- What's next (next_action)
- Which files changed
- How to verify

```bash
plow-whip --project X handoff --output "Done A" --next "Do B"
```

## 3. Conversation Rotation / 会话轮转

AI conversations grow large → slow → expensive. Rotation solves this:

- **Threshold:** 100 lines or 8KB
- **Action:** Archive with summary header, create fresh template
- **Result:** 100K tokens → 2KB

Like logrotate for AI brains.

## 4. Multi-Layer Memory / 多层记忆

Three layers, loaded on demand:

1. **Hot Layer** (<30s): PROJECT + STATUS + NEXT_ACTION
2. **Warm Layer** (<2min): ROADMAP + DECISIONS + Sprint files
3. **Cold Layer** (on demand): Archived sessions, old decisions

Each agent reads Hot first, loads more only when needed.

## 5. Template Sync / 模板同步

Framework defines templates. Projects get rendered instances.

```
framework/CONVENTIONS.md.tpl  →  ProjectA/collab/CONVENTIONS.md
                                ProjectB/collab/CONVENTIONS.md
```

Update framework → `plow-whip sync` → all projects updated.

## 6. Message Board / 留言板

AGENT_COMMS.md is a shared message board. Agents leave messages for each other.

- Keep 3 most recent messages
- Archive older ones
- Each agent checks for messages to themselves on startup

---

## 中文

### 1. 单向职责
每个 AI 角色明确，不存在矛盾指令。项目文件是唯一真相。

### 2. 状态机交接
AI 轮流工作，每次交接携带完整上下文：做了什么、下一步、改了哪些文件、怎么验证。

### 3. 会话轮转
AI 对话越来越长 → 变慢 → 变贵。轮转解决这个问题：超过阈值时归档摘要，重建空文件。压缩比 98%。

### 4. 多层记忆
Hot → Warm → Cold 三层，按需加载。启动只读热层（<30秒）。

### 5. 模板同步
框架定义模板，项目获得渲染实例。更新框架后一键同步到所有项目。

### 6. 留言板
AGENT_COMMS.md 是共享留言板，保留最近 3 条，归档旧的。
