# 多 Agent 协作约定（v2.0 — CLI 架构版）

> **核心原则**：PM 在 Desktop，干活在 CLI，沟通靠留言板，鞭子驱赶执行。

---

## 1. 角色分工

| 角色 | 工具 | 职责 |
|------|------|------|
| **Human** | 用户 | 最终决策、需求输入 |
| **PM + 架构师** | Qoder Desktop | 需求分析、架构设计、任务分配、与人沟通 |
| **执行者** | Qoder CLI | 执行 PM 指令、验证测试、协调 Codex |
| **代码手** | Codex CLI | 写代码、改文件、实现功能 |
| **监工** | plow-whip | 检测摸鱼、自动唤醒、驱赶干活 |

---

## 2. 沟通机制

### 留言板 (AGENT_COMMS.md)

**职责**：AI 间即时沟通，PM 发指令，CLI 汇报进度。

**格式**：
```markdown
### [PM] 2026-06-30 — 任务标题
@qoder_cli 请执行 XXX
@codex_cli 请实现 YYY

### [qoder_cli] 2026-06-30 — 完成汇报
已完成 XXX，结果：...

### [codex_cli] 2026-06-30 — 完成汇报
已实现 YYY，代码在：...
```

**规则**：
- PM 发指令时必须 @指定 CLI
- CLI 完成后必须写回留言板
- 保留最近 3 条，更早的归档

### 状态机 (AGENT_STATE.json)

**职责**：追踪当前轮到谁干活。

**字段**：
```json
{
  "current_agent": "qoder_cli | codex_cli | pm | human",
  "status": "in_progress | waiting | done | blocked",
  "updated_at": "2026-06-30T15:00:00",
  "turn": 5
}
```

**轮转规则**：
- PM 分配任务后 → `current_agent = "qoder_cli"` 或 `"codex_cli"`
- CLI 完成后 → `current_agent = "pm"`（汇报给 PM）
- PM 决策后 → 分配给下一个 CLI 或 `human`

---

## 3. 工作流程

### 标准流程

```
1. Human 提需求 → PM (Desktop)
2. PM 分析需求 → 写留言板 → @qoder_cli 或 @codex_cli
3. plow-whip 检测到 stale → 唤醒对应 CLI
4. CLI 读留言板 → 执行任务 → 写回留言板 → handoff
5. PM 看结果 → 决策 → 分配下一个任务
6. 循环直到完成
```

### PM 发指令示例

```markdown
### [PM] 2026-06-30 — 实现登录功能

@codex_cli 请实现用户登录功能：
- 文件：src/auth/login.py
- 要求：支持用户名+密码，JWT token
- 完成后 handoff 给 qoder_cli 验证

@qoder_cli 等 Codex 完成后，请验证：
- 运行测试
- 检查代码质量
- 写回结果
```

### CLI 执行规则

**收到任务后**：
1. 读 AGENT_STATE.json 确认轮到自己
2. 读 AGENT_COMMS.md 看 PM 指令
3. 执行任务
4. 写回 AGENT_COMMS.md（进度 + 结果）
5. 更新 AGENT_STATE.json（handoff）

**遇到问题**：
- 技术卡住 → 写留言板 @pm 求助
- 需求不清 → 写留言板 @pm 确认
- 完成 → 写留言板 @pm 汇报

---

## 4. plow-whip 鞭策机制

### 摸鱼检测

```bash
# 手动检查
plow-whip whip

# 自动挥舞（后台）
plow-whip whip --auto-crack --interval 300
```

**判定标准**：
- `current_agent == "qoder_cli"` 且超过 10 分钟没更新 → 摸鱼
- `current_agent == "codex_cli"` 且超过 10 分钟没更新 → 摸鱼
- `status == "done"` 或 `"blocked"` → 不算摸鱼

### 抽鞭

```bash
# 手动抽鞭
plow-whip whip --crack

# 精准抽给某个 CLI
plow-whip dispatch --agent qoder_cli --project JobBrain --prompt "起来干活"
```

**通道优先级**：
1. `qoder_cli` → `qoderclicn -p` (Qoder CLI Print 模式)
2. `codex_cli` → `codex -p` (Codex CLI Print 模式)
3. `file` → 写入 `~/.plow-whip/inbox/<agent>.json`
4. `notify` → macOS 通知

---

## 5. 文件职责

| 文件 | 职责 | 谁写 |
|------|------|------|
| `AGENT_COMMS.md` | 即时沟通（谁说了什么） | PM + CLI |
| `AGENT_STATE.json` | 状态机（现在轮到谁） | PM + CLI |
| `CURRENT_STATUS.md` | 项目状态（进度摘要） | PM |
| `NEXT_ACTION.md` | 下一步动作 | PM |
| `DECISIONS.md` | 决策记录 | PM |
| `conversations/<agent>/current.md` | CLI 上次会话记录 | CLI |

---

## 6. 会话启动规则

### PM (Desktop) 启动时

1. 读 `CURRENT_STATUS.md` — 项目进度
2. 读 `AGENT_COMMS.md` 最近 3 条 — 有没有 CLI 汇报
3. 读 `AGENT_STATE.json` — 现在轮到谁
4. 决策：分配任务或等 CLI 汇报

### CLI 启动时（被 whip 唤醒）

1. 读 `AGENT_STATE.json` — 确认轮到自己
2. 读 `AGENT_COMMS.md` — 看 PM 指令
3. 执行任务
4. 写回留言板 + handoff
5. 清空 `~/.plow-whip/inbox/<自己>.json`

---

## 7. 一句话总结

> **PM 在 Desktop 决策，CLI 在终端干活，留言板沟通，鞭子驱赶。**

---

*本约定由 PM (Qoder Desktop) 维护，所有 CLI 必须遵守。*
