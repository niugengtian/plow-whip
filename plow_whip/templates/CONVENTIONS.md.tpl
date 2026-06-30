# 多 Agent 协作约定（v3.1 — 角色修正版）

> **核心原则**：Desktop 决策，CLI 干活，留言板沟通，鞭子驱赶。

---

## 1. 角色分工

| 角色 | Agent 名 | 工具 | 职责 | 能被 plow-whip 驱动？ |
|------|----------|------|------|----------------------|
| **PM + 架构师** | `qoder` | Qoder CN (Desktop) | 需求分析、架构设计、任务分配、Sprint 规划；**用户主对话窗口** | ❌ 用户直接对话 |
| **审查官** | `qoder_cli` | Qoder CLI | 架构审查、测试验收、代码质量检查 | ✅ 能被驱动 |
| **学习搭子** | `codex` | Codex (Desktop/Web) | **当前闲置**，陪用户学习，不参与开发循环 | ❌ 暂不参与 |
| **代码手** | `codex_cli` | Codex CLI | 代码实现、改文件、写功能（Code Owner） | ✅ 能被驱动 |
| **监工** | — | plow-whip | 检测摸鱼、自动唤醒、会话轮转、自动轮转 | — |

**关键区分：**
- `qoder` = 用户主对话窗口（就是你现在聊天的 AI），不需要驱动
- `qoder_cli` + `codex_cli` = 两个 CLI agent，都能被 plow-whip 鞭子抽着干活
- `codex` = 暂时闲置，随时可重新激活

### 单向职责原则

```
qoder     → 只回答"做什么"和"为什么做"，禁止写代码
qoder_cli → 只负责"做得对不对"（审查/测试/验收），禁止需求决策
codex_cli → 只负责"怎么做"（代码实现），禁止改变需求或产品方向
codex     → 闲置中
```

---

## 2. 沟通机制

### 留言板 (AGENT_COMMS.md)

**职责**：AI 间即时沟通，qoder 发指令，CLI 汇报进度。

**格式**：
```markdown
### [qoder] 2026-07-01 — 任务标题
@qoder_cli 请审查 XXX
@codex_cli 请实现 YYY

### [qoder_cli] 2026-07-01 — 审查结果
审查完成，发现 N 个问题：...

### [codex_cli] 2026-07-01 — 实现汇报
已实现 YYY，代码在：...
```

**规则**：
- qoder 发指令时必须 @指定 CLI
- CLI 完成后必须写回留言板
- 保留最近 5 条，更早的归档

### 状态机 (AGENT_STATE.json)

**职责**：追踪当前轮到谁干活。

**字段**：
```json
{
  "current_agent": "qoder | qoder_cli | codex | codex_cli",
  "status": "in_progress | done | blocked",
  "updated_at": "2026-07-01T15:00:00"
}
```

**轮转规则**：
- `qoder` 分配任务后 → handoff 给 `qoder_cli`（审查）或 `codex_cli`（实现）
- CLI 完成后 → handoff 回 `qoder`（汇报验收）
- `codex` 当前不参与轮转

---

## 3. 工作流程

### 标准流程

```
1. Human 提需求 → qoder（直接对话）
2. qoder 分析需求 → 写留言板 → @codex_cli 实现 / @qoder_cli 审查
3. plow-whip 检测到 stale → 唤醒对应 CLI
4. CLI 读留言板 → 执行任务 → 写回留言板 → handoff
5. qoder 看结果 → 验收 → 分配下一个任务
6. 循环直到完成
```

### CLI 执行规则

**收到任务后**：
1. 读 AGENT_STATE.json 确认轮到自己
2. 读 AGENT_COMMS.md 看 qoder 指令
3. 执行任务
4. 写回 AGENT_COMMS.md（进度 + 结果）
5. 更新 AGENT_STATE.json（handoff）

**遇到问题**：
- 技术卡住 → 写留言板 @qoder 求助
- 需求不清 → 写留言板 @qoder 确认
- 完成 → 写留言板 @qoder 汇报

---

## 4. plow-whip 鞭策机制

### 摸鱼检测

```bash
# 手动检查
plow-whip whip

# 自动挥舞 + 自动轮转（推荐）
plow-whip whip --auto-crack --auto-rotate --interval 300
```

**判定标准**：
- `qoder_cli` 或 `codex_cli` 超过阈值时间没更新 → 摸鱼
- `status == "done"` 或 `"blocked"` → 不算摸鱼
- `codex` 闲置中，不参与摸鱼检测

### 会话轮转（自动）

```bash
# 手动轮转
plow-whip --project JobBrain rotate --agent qoder_cli --topic "主题" --summary "摘要"

# 自动轮转（集成在 daemon 中）
plow-whip whip --auto-crack --auto-rotate
```

**规则**：
- `current.md` 超过 100 行或 8KB → 自动归档
- handoff 完成时 → 自动检查交出方会话，超限则轮转
- 归档文件：`conversations/<agent>/YYYYMMDD_HHMMSS_<topic>.md`

---

## 5. 文件职责

| 文件 | 职责 | 谁写 |
|------|------|------|
| `AGENT_COMMS.md` | 即时沟通（谁说了什么） | 所有 agent |
| `AGENT_STATE.json` | 状态机（现在轮到谁） | 所有 agent |
| `CURRENT_STATUS.md` | 项目状态（进度摘要） | qoder |
| `NEXT_ACTION.md` | 下一步动作 | qoder |
| `DECISIONS.md` | 决策记录 | qoder |
| `conversations/<agent>/current.md` | 会话上下文（自动轮转） | 各 agent |

---

## 6. 会话启动规则

### qoder (Desktop) 启动时

1. 读 `CURRENT_STATUS.md` — 项目进度
2. 读 `AGENT_COMMS.md` 最近 3 条 — 有没有 CLI 汇报
3. 读 `AGENT_STATE.json` — 现在轮到谁
4. 决策：分配任务或等 CLI 汇报

### CLI 启动时（被 whip 唤醒）

1. 读 `AGENT_STATE.json` — 确认轮到自己
2. 读 `AGENT_COMMS.md` — 看 qoder 指令
3. 执行任务
4. 写回留言板 + handoff
5. 清空 `~/.plow-whip/inbox/<自己>.json`

---

## 7. 一句话总结

> **qoder 决策，qoder_cli 审查验收，codex_cli 写码，鞭子驱赶，会话自动轮转。codex 学习去。**

---

*本约定由 qoder (Qoder CN Desktop) 维护，所有 CLI 必须遵守。*
