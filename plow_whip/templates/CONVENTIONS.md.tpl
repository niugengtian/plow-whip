# 多 Agent 协作约定

> **核心原则**：Desktop 决策，CLI 干活，留言板沟通，鞭子驱赶。

---

## 【P-1】零号启动协议（plow-whip 机制自检）— 所有 Agent 进入项目第一件事

**任何 Agent 进入项目、打开新会话、接到任务或准备改文件前，第一件事必须检查 plow-whip 机制是否存在。**

```bash
plow-whip --project {PROJECT_NAME} doctor --repair
```

规则：

- 如果 `collab/`、`AGENT_STATE.json`、`AGENT_COMMS.md`、`CONVENTIONS.md`、`memory/` 或自己的 `conversations/<agent>/current.md` 缺失，先用 `doctor --repair` 建立。
- 机制建立前，不读业务代码、不改文件、不执行任务。
- 机制存在后，第二件事是遵守本文件全部规则，尤其是 P0 by_rm 和 context-pack 启动流程。
- `doctor` 只检查结构存在性，不读取正文；这是零 token/低成本自检。

决策记录：`memory/DECISIONS.md` D-008

---

## 【P-0.5】子智能体边界 — 禁止 Agent 自行开子智能体

**除非用户在当前任务中临时明确指定，否则任何 Agent 不允许自行创建、调用、委派或并行启动子智能体来完成工作。**

规则：

- 默认执行者只能是 `AGENT_STATE.json`、`AGENTS.md` 和 plow-whip handoff/drive 明确登记的 agent。
- Agent 不得为了提速、拆分任务、评审、搜索或“多角度分析”自行开 subagent / worker / parallel agent。
- 如用户临时指定可用子智能体，该子智能体输出必须回填到当前 agent 的会话记忆、`AGENT_COMMS.md` 或 handoff 记录，不能形成独立记忆孤岛。
- 所有跨 Agent 作业必须通过 plow-whip 的三层记忆、留言板、状态机和 handoff/drive 机制接力。

原因：

- 防止绕过 `doctor --repair -> context-pack -> CONVENTIONS.md` 启动协议。
- 防止子智能体只持有临时上下文，导致 Hot/Warm/Cold 三层记忆断层。
- 防止 token 成本失控和责任边界不清。

决策记录：`memory/DECISIONS.md` D-009

---

## 【P-0.75】项目边界原则 — 禁止越过当前项目目录

**除非用户在当前任务中明确指定路径，否则 Agent 只能读取和修改当前项目根目录内的文件。**

规则：

- 当前项目根目录是 `AGENT_STATE.json` 的 `task_context.project_path` 或用户当前指定的项目路径。
- 禁止擅自修改 `~/.plow-whip/config.json`、其他项目目录、其他项目的 `collab/`、其他项目 agent 的会话文件或全局 agent 阵容。
- 发现全局配置或其他项目状态看起来“不对”时，只能报告风险和建议命令，不能代替用户修改。
- 如用户明确要求跨项目或全局配置操作，必须在回复中点名目标路径和影响范围，并只执行该范围内的操作。
- 任何误触项目外文件时，必须立即停止、说明触碰了什么、恢复原状，并记录到当前项目的 `AGENT_COMMS.md` / 决策记录。

原因：

- 防止一个项目的 agent 阵容污染另一个项目。
- 防止误删、误改其他项目会话，造成 plow-whip 三层记忆断层。
- 让每个项目的记忆、状态机和 agent 边界保持独立。

决策记录：`memory/DECISIONS.md` D-010

---

## 【P0】文件删除约定（by_rm）— 最高优先级，所有 Agent 首先遵守

**禁止使用 `rm` / `rm -rf` 直接删除文件或目录。**

所有"删除"操作改为移动到项目根目录下的 `by_rm/` 目录，并追加时间戳重命名：

```
原路径:  src/old_module.py
移动到:  by_rm/src/old_module_20260709_165000.py
```

### 命名规则

- 格式：`原文件名_YYYYMMDD_HHMMSS.原后缀`
- 保留原始目录结构（`by_rm/<原目录>/...`）
- 时间戳取操作时刻

### 示例

```bash
# ❌ 禁止
rm src/old_module.py
rm -rf src/old_module/

# ✅ 正确
mkdir -p by_rm/src
mv src/old_module.py by_rm/src/old_module_20260709_165000.py
```

### 原因

- **可追溯**：任何"删除"都能找回
- **可审计**：时间戳记录何时移除
- **安全**：避免误删导致不可逆损失
- **框架一致**：`qoder_session._safe_remove()`、`cmd_archive()` 均使用 `by_rm/`

### 其他规则

- `by_rm/` 已加入 `.gitignore`，不提交 git
- `plow-whip whip` 扫描项目时会跳过 `by_rm/` 目录
- 人类确认不再需要后，由人类手动清空 `by_rm/`（Agent 不执行永久删除）
- 决策记录：`memory/DECISIONS.md` D-002

---

## 1. 角色分工

以 `AGENTS.md` 为准。每个项目初始化时会从 `~/.plow-whip/config.json` 快照当前 agent 阵容、角色和作业分配。

命名边界：`cursor` 指 Cursor Desktop；`cursor_cli` 指 Cursor CLI。Qoder CN Desktop / Qoder CLI 已停用，不在轮转里。

修改方式：

```bash
plow-whip agent list
plow-whip agent set builder --role "Code Owner" --assignment "Implement scoped tasks and report checks"
plow-whip --project {PROJECT_NAME} agent set reviewer --role "Reviewer" --assignment "Review implementation and risks"
```

`--project` 存在时，会同步更新该项目的 `AGENT_STATE.json`、`AGENTS.md` 和留言板。

---

## 2. 沟通机制

### 留言板 (AGENT_COMMS.md)

**职责**：AI 间即时沟通，负责人发指令，执行者汇报进度。

**格式**：
```markdown
### [planner] 2026-07-01 — 任务标题
@reviewer 请审查 XXX
@builder 请实现 YYY

### [reviewer] 2026-07-01 — 审查结果
审查完成，发现 N 个问题：...

### [builder] 2026-07-01 — 实现汇报
已实现 YYY，代码在：...
```

**规则**：
- 发指令时必须 @指定 agent
- agent 完成后必须写回留言板
- 保留最近 5 条，更早的归档

### 状态机 (AGENT_STATE.json)

**职责**：追踪当前轮到谁干活。

**字段**：
```json
{
  "current_agent": "任意已配置 agent 名",
  "assigned_agent": "当前被分配的 agent",
  "status": "in_progress | done | blocked",
  "agents": ["planner", "builder", "reviewer"],
  "agent_meta": {"builder": {"role": "Code Owner", "assignment": "实现任务"}},
  "blockers": [],
  "files_changed": [],
  "verify_commands": [],
  "last_wake_hash": "防重复唤醒",
  "wake_count": 0,
  "updated_at": "2026-07-01T15:00:00"
}
```

**轮转规则**：
- 默认按 `agents` 列表顺序轮转。
- 指定接收方：`plow-whip --project {PROJECT_NAME} handoff --to builder --output "..." --next "..."`
- 记录阻塞：`plow-whip --project {PROJECT_NAME} handoff --status blocked --blockers "缺少 API key" --output "..." --next "..."`

---

## 3. 工作流程

### 标准流程

```
1. Human 提需求 → 负责人 agent
2. 负责人分析需求 → 写留言板 → @指定执行/审查 agent
3. plow-whip 检测到 stale → 唤醒对应 agent
4. agent 读留言板 → 执行任务 → 写回留言板 → handoff
5. 负责人看结果 → 验收 → 分配下一个任务
6. 循环直到完成
```

### 省 token 唤醒规则

- whip 只发送项目路径和关键文件路径，不粘贴全文。
- 被唤醒的 agent 先读 `CONVENTIONS.md`、`AGENT_STATE.json`、`AGENT_COMMS.md`、`memory/NEXT_ACTION.md`。
- 同一个 `next_action` 没变时，`last_wake_hash` 会阻止重复投递。

### 历史 BUG 规则

- 修 BUG 前先在 `collab/` 里搜症状、文件名、函数名，确认有没有远古同类问题。
- 修复要落在共同根因；只补当前入口会复活旧 BUG 时，不算完成。
- 发现复发/远古 BUG，写回 `AGENT_COMMS.md`，必要时补到 `memory/DECISIONS.md`。

### CLI 执行规则

**收到任务后**：
1. 读 CONVENTIONS.md **【P0】by_rm 约定** — 确认禁止 `rm`
2. 读 AGENT_STATE.json 确认轮到自己
3. 读 AGENT_COMMS.md 看指令
4. 执行任务
5. 写回 AGENT_COMMS.md（进度 + 结果）
6. 更新 AGENT_STATE.json（handoff）

**遇到问题**：
- 技术卡住 → 写留言板 @负责人 求助
- 需求不清 → 写留言板 @负责人 确认
- 完成 → 写留言板 @负责人 汇报

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
- 当前轮到的 agent 超过阈值时间没更新 → 摸鱼
- `status == "done"` 或 `"blocked"` → 不算摸鱼
- Qoder CN Desktop / Qoder CLI 停用，不参与摸鱼检测

### 会话轮转（自动）

```bash
# 手动轮转
plow-whip --project JobBrain rotate --agent builder --topic "主题" --summary "摘要"

# 自动轮转（集成在 daemon 中）
plow-whip whip --auto-crack --auto-rotate
```

**规则**：
- `current.md` 超过 100 行或 8KB → 自动归档
- 归档文件必须包含 `## Carry Forward`，下个会话优先读继承摘要
- handoff 完成时 → 自动检查交出方会话，超限则轮转
- 归档文件：`conversations/<agent>/YYYYMMDD_HHMMSS_<topic>.md`

### 三层记忆预算

```bash
plow-whip --project {PROJECT_NAME} memory-budget
```

`memory-budget` 只用文件大小估算 token，不读取正文。Hot 层应该每次唤醒都能读；Warm 层只在 context-pack 不够时读；Cold 层只搜索/恢复片段，禁止整层通读。

---

### CLI 驱使与结果盯梢（cursor / codex 编排者必读）

> **教训**：`whip --crack` 对 `codex_cli` 会同步阻塞（默认超时 30 分钟），编排者应**后台并行驱使 + 只看尾部结果**，不要傻等。

#### 驱使命令（后台 + 日志）

```bash
PROJECT="/path/to/project"
PROMPT="任务内容（短 prompt，附文件路径，不贴全文）"
LOG_DIR="/tmp/plow-whip-logs"
mkdir -p "$LOG_DIR"

# cursor_cli — 真唤醒
cursor-agent --print --force --trust \
  --workspace "$PROJECT" "$PROMPT" \
  > "$LOG_DIR/cursor_cli.log" 2>&1 &

# codex_cli — 真唤醒（Codex 现随 ChatGPT.app 分发）
/Applications/ChatGPT.app/Contents/Resources/codex -a never -s workspace-write -C "$PROJECT" \
  exec --ephemeral "$PROMPT" \
  > "$LOG_DIR/codex_cli.log" 2>&1 &
```

**多个 CLI 可同时后台跑**，编排者用并行 `sleep + tail` 盯梢。

#### 盯结果三板斧（省 token）

| 优先级 | 检查什么 | 命令 |
|--------|----------|------|
| 1 | CLI 是否跑完 | `sleep 60 && tail -10 /tmp/plow-whip-logs/<agent>.log` |
| 2 | 任务是否落地 | `grep -A8 "Acknowledgments" collab/AGENT_COMMS.md \| tail -15` |
| 3 | 状态机是否更新 | `plow-whip --project <名> status` |

**判定**：
- `tail` 末尾无 error / exit 0 迹象 → 继续看留言板和 status
- `Acknowledgments` 出现该 agent 确认帖 → ✅ 成功
- `~/.plow-whip/inbox/<agent>.json` 中 `status: pending` 清空 → ✅ 已消费
- 失败 → `tail -50` 往上翻定位，修完再驱

#### 轻量轮询（不读全文日志）

```bash
# 每 60 秒只看各 CLI 最后 10 行
sleep 60 && for a in cursor_cli codex_cli; do
  echo "=== $a ===" && tail -10 "/tmp/plow-whip-logs/$a.log" 2>/dev/null
done

# 或盯状态机变化（零 token）
plow-whip --project {PROJECT_NAME} watch --interval 30
```

#### 通道选择

| Agent | 推荐驱使 | 避免 |
|-------|----------|------|
| `cursor_cli` | `cursor-agent --print` 后台 | 同步 `whip --crack` 阻塞编排者 |
| `codex_cli` | `/Applications/ChatGPT.app/Contents/Resources/codex exec` 后台 | 同上（1800s 超时） |
| `codex` / `reviewer` Desktop | `whip --crack --channel file` 写 inbox | 盲目 `--crack` 无 `--channel` |

决策记录：`memory/DECISIONS.md` D-004

---

## 5. 文件职责

| 文件 | 职责 | 谁写 |
|------|------|------|
| `AGENT_COMMS.md` | 即时沟通（谁说了什么） | 所有 agent |
| `AGENT_STATE.json` | 状态机（现在轮到谁） | 所有 agent |
| `CURRENT_STATUS.md` | 项目状态（进度摘要） | codex |
| `NEXT_ACTION.md` | 下一步动作 | codex |
| `DECISIONS.md` | 决策记录 | codex |
| `conversations/<agent>/current.md` | 会话上下文（自动轮转） | 各 agent |

---

## 6. 会话启动规则

> **【强制】任何 Agent（Desktop + CLI）开启新会话时，必须先读本文件并完成自检。详见 `AGENT_COMMS.md` 全员公告与 Acknowledgments 确认区。**

### 通用启动自检（所有 Agent 必须执行）

1. 先运行 `plow-whip --project {PROJECT_NAME} doctor --repair` — 确认 plow-whip 机制存在，缺失则建立
2. 再运行 `plow-whip --project {PROJECT_NAME} context-pack --agent <自己>` — 读最小上下文包
3. 读 `CONVENTIONS.md` **【P-1 + P0】约定** — 确认先自检机制、禁止 `rm`
4. 只在 context-pack 不够时读 `AGENT_COMMS.md` / `DECISIONS.md` 原文
5. 写入自己的会话记忆（`conversations/<agent>/current.md`）
6. 在 `AGENT_COMMS.md` **Acknowledgments** 区回复确认（新会话首次）

### codex (Desktop) 启动时

在通用自检之后：

1. 读 `CURRENT_STATUS.md` — 项目进度
2. 决策：分配任务或等其他 agent 汇报

### CLI 启动时（被 whip 唤醒）

在通用自检之后：

1. 优先按 `context-pack` 里的 `Current Task` 执行任务
2. 写回留言板 + handoff
3. 清空 `~/.plow-whip/inbox/<自己>.json`

---

## 7. 一句话总结

> **P0：禁止 `rm`，删除改 `mv` 到 `by_rm/`。角色在 AGENTS.md，状态在 AGENT_STATE.json，沟通在 AGENT_COMMS.md，鞭子负责唤醒和轮转。**

---

*本约定由项目负责人维护，所有 agent 必须遵守。*
