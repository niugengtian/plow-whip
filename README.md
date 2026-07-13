# plow-whip（耕田之鞭）

面向多 Agent 项目的低 token 协作状态机。核心约束是：**一个协议真源、一个状态真源、一个启动入口**。

## 安装

要求 Python 3.10+。

```bash
python3 -m pip install -e .
plow-whip configure --projects-dir /absolute/path/to/projects --agents codex codex_cli reviewer
```

## 唯一启动入口

Agent 进入项目只运行：

```bash
plow-whip --project MyProject start --agent codex --json
```

返回当前任务、有效规则、定向消息、相关决策 ID 和回写命令。正常情况下不再读取完整 Markdown。

规则按四个维度管理：`scope=global|project`、`priority=required|important`、`origin=local|inherited|derived`、`enforcement=block|require_approval|verify|warn|inform`。`start --json` 每次返回全部 `mandatory_rules`，并按 Agent 与任务 `rule_tags` 返回命中的 `important_rules`；需要完整细则时只返回定向 `required_context`。派生启动规则保留 `derived_from` 和 `source_origin`，`rules_meta.effective_hash` 用于识别规则是否变化。

日常接力不重复读取 `CONVENTIONS.md` 或 `CONVENTIONS.agent.md`；安全约束由编译后的启动规则包承载。首次进入、规则哈希变化、命中高风险规则或专项审计时，才按 `required_context` 读取真源中的指定细则。

如果结构缺失：

```bash
plow-whip --project MyProject doctor --json   # 只读检查
plow-whip --project MyProject repair --json   # 显式修复
```

`doctor --repair` 仍作为旧调用兼容入口，但不会隐式轮转内容。

`doctor` 同时校验协议 JSON、状态 JSON、task owner 和派生字段一致性；损坏的 canonical JSON 不会被 repair 静默覆盖。

## 项目文件

```text
collab/
├── AGENT_PROTOCOL.json   # 英文机器协议与项目 Agent 阵容，唯一协议真源
├── HANDBOOK.zh-CN.md     # 从协议单向生成的中文手册
├── AGENT_STATE.json      # 当前任务与运行状态，唯一状态真源
├── AGENTS.md             # 从协议派生的人类阵容表
├── AGENT_COMMS.md        # 近期定向消息，写入后自动检查轮转
├── CONVENTIONS.agent.md  # 旧工具兼容指针
├── CONVENTIONS.md        # 旧工具兼容指针
├── conversations/        # Agent 会话与归档
└── memory/               # 决策和历史归档（Cold）
```

新项目的必要 memory 结构只有 `DECISIONS.md` 与 `sessions/`；旧版 `NEXT_ACTION.md`、`CURRENT_STATUS.md`、`ROADMAP.md` 可继续保留，但不参与启动和 doctor 就绪判定。

全局配置只为新项目提供默认 Agent 阵容。项目初始化后，阵容以项目自己的 `AGENT_PROTOCOL.json` 为准。

## Registry、Router 与 Driver

Agent 名称只表示长期职责身份，不再绑定 Cursor 或 Codex。项目 Registry 为每个逻辑 Agent 保存：

```json
{
  "backend-primary": {
    "role": "Backend Owner",
    "roles": ["backend", "implementation"],
    "capabilities": ["python", "api"],
    "driver": "cursor_cli",
    "priority": 80,
    "cost_tier": "low",
    "assignment": "Own backend delivery",
    "enabled": true
  }
}
```

可执行 Driver 是封闭集合：`cursor_cli`、`codex_cli`、`zellij`、`file`。Router 只根据 `role + capabilities + enabled + priority + cost_tier + driver availability` 做确定性选择；同优先级保持 Registry 顺序。旧 v3 配置会无损迁移为 v4：旧 `role` 推导为稳定的 `roles` 标签，并补齐 Driver、优先级和成本档。

```bash
plow-whip --project MyProject agent set backend-primary \
  --role "Backend Owner" --roles backend implementation \
  --capabilities python api --driver cursor_cli \
  --priority 80 --cost-tier low --assignment "Own backend delivery"

plow-whip --project MyProject agent set backend-backup \
  --role "Backend Backup" --roles backend implementation \
  --capabilities python api --driver codex_cli --priority 70
```

执行结果明确区分 `logical_owner`、`executor` 和 `driver`。主 Agent 的 Driver 不可用或执行失败时，只在同职责、满足同能力的 Agent 中接力，不会把后端任务误派给设计或审计 Agent。
父调度器会把这份精简执行证据（含 `fallback_errors`，不含大段模型输出）回写到对应 PLAN、当前任务或已完成 milestone，避免 scheduler 下一轮覆盖后失去追责链。

## 原子任务状态

```bash
plow-whip --project MyProject task start \
  --id T-12 --title "Implement API" --goal "Ship a working API" \
  --owner codex_cli --next "Add endpoint" \
  --acceptance "endpoint returns 200" "tests pass" \
  --verify "python -m unittest" --rule-tags api database

plow-whip --project MyProject task progress --output "Endpoint added" --next "Run tests"
plow-whip --project MyProject task progress --output "Plan ready" \
  --acceptance "endpoint returns 200" --verify "python -m unittest" --next "Implement"
plow-whip --project MyProject task block --output "Cannot deploy" --blockers "missing credential"
plow-whip --project MyProject task complete --output "Implementation finished"
```

这些命令原子更新 `AGENT_STATE.json`，避免状态、下一步和留言互相脱节。`task complete` 会自动执行任务的 `verify_commands`：全部通过才进入 `done`；失败则保持 `active`，把失败命令写成新的 `next_action`，由 scheduler 在租约到期后继续派发修复。启动 payload 中过长的 `last_output` 只返回末尾 1000 字符，完整结果仍保留在状态真源中。

## 人只给目标

```bash
plow-whip --project MyProject goal start "交付可上线的登录功能"
```

系统通过 Registry 选择具备 `planner` 角色且 Driver 可执行的 Agent，让规划 Agent 只在第一次理解项目并提交 1–7 个粗粒度里程碑：

```bash
plow-whip --project MyProject goal plan \
  --context-summary "复用后续接力所需的压缩项目上下文" \
  --plan-json '[{"title":"实现并测试登录","role":"backend","capabilities":["api"],"acceptance":["测试通过"]},{"title":"最终集成验收","role":"reviewer","capabilities":["review"],"acceptance":["全量验收通过"],"final_acceptance":true}]'
```

Planner 通常只写角色和能力，由 Router 绑定实际 owner；只有用户明确指定某个 Agent 时才写 `owner`。每个里程碑内部自行完成读代码、实现、测试和文档，不拆成独立小任务。`task complete` 验收成功后自动切换到下一个里程碑；失败则停留修复。最后一个里程碑必须显式声明 `final_acceptance=true`，而且必须由未参与实现的可执行 Agent 独立验收，否则计划会被拒绝。

启动包只返回当前任务、最多 1200 字符的目标上下文和完成进度，不返回完整队列或 Agent 清单。只有 PLAN 任务会收到去重后的角色/能力目录，因此 Registry 从 10 个扩到 100 个同类 Agent，也不会把 100 份配置烧进模型上下文。

已有 active Goal 时再次 `goal start` 会进入 FIFO 队列，不会静默覆盖；当前 Goal 完成后自动激活下一个。只有明确使用 `--replace` 才会替换，并把旧 Goal 写入 `goal_history`：

```bash
plow-whip --project MyProject goal start "下一个交付目标"
plow-whip --project MyProject goal start "紧急目标" --replace
```

`goal start` 同时为当前项目设置 `automation_enabled=true`。开启 `scheduler install --auto-continue` 后，定时器只派发明确 opt-in 的项目；其他普通 active、blocked 或 done 项目不会因为全局定时器而被驱动。

## Handoff

```bash
plow-whip --project MyProject handoff \
  --to reviewer --status in_progress --output "Implementation done" --next "Review changes"
```

handoff 同步更新 owner、任务状态、下一步、输出与阻塞，并检查会话和协作文件轮转。

## 三层记忆

- Hot：`AGENT_STATE.json`，每次启动加载。
- Warm：定向消息和当前交接，按 Agent 筛选。
- Cold：完整决策、历史消息和会话归档，只按需搜索。

`DECISIONS.md` 不再计入 Warm。

```bash
plow-whip --project MyProject memory-budget --json
plow-whip --project MyProject rotation-health --json
plow-whip --project MyProject memory-rotate
```

## Whip

单次、安全、适合系统定时器的运行方式：

```bash
plow-whip whip --once --json
plow-whip whip --once --auto-rotate --crack --json
```

`--once` 使用跨平台单实例锁。自动续作每 5 分钟检查新进展；排队/失败 5 分钟后可重试，运行中的相同动作保留 30 分钟租约。同一动作最多自动尝试 3 次，防止无限消耗 token。

旧 `--daemon` 与 `--auto-crack` 不再创建永久 Python 循环，只执行一次兼容扫描；周期运行统一交给系统 scheduler。

探针和任务严格分层：默认 `--once` 只直读 `AGENT_STATE.json` 的状态、时间戳、任务 ID/状态，不加载 skills、协议正文、任务正文、消息或历史，也不轮转文件。只有发现 stale/状态不一致，并且显式启用 `--crack` 恢复时，才只为异常项目加载任务上下文。自动派发必须显式使用 `--crack`。

Codex Desktop 定时唤醒只执行 `plow-whip whip --once --json`：`recovery_projects` 为空立即结束；非空才进入对应项目的恢复入口。探针输出用 `mode=probe`、`context_loaded=false`、`model_invoked=false` 明确声明边界。

## 跨平台定时任务

```bash
# 先预览，不写系统配置
plow-whip scheduler install --interval 300 --dry-run

# 安装用户级定时任务，默认不自动派发 Agent
plow-whip scheduler install --interval 300

# 明确允许无人值守续作（--auto-crack 是兼容别名）
plow-whip scheduler install --interval 300 --auto-continue

plow-whip scheduler status
plow-whip scheduler run
plow-whip scheduler start
plow-whip scheduler stop
plow-whip scheduler logs
plow-whip scheduler doctor
plow-whip scheduler repair
plow-whip scheduler uninstall
```

平台映射：

| 系统 | 原生机制 | 配置位置 |
|---|---|---|
| macOS | launchd | `~/Library/LaunchAgents/com.plow-whip.scheduler.plist` |
| Linux | systemd user timer | `~/.config/systemd/user/plow-whip.{service,timer}` |
| Windows | Task Scheduler | 当前用户任务 `PlowWhipScheduler` |

调度器优先固定已安装的 `plow-whip` 绝对入口，并写入稳定的工具 PATH，不依赖交互式 shell 环境。

自动续作只扫描超时的 `active` 任务，`blocked` 和 `done` 不会派发。CLI/Brain 通道可真正无人值守执行；Desktop Agent 无可执行通道时只进入 inbox/系统通知，等待对应客户端接管。每次派发都有租约和 dispatch ID，不会无限高频重试。

## 投递生命周期

每次 dispatch 都带：

```json
{
  "task_id": "T-12",
  "dispatch_id": "DP-...",
  "status": "queued | accepted | running | completed | failed"
}
```

## CLI 认证与 Key 池

`codex_cli` 和 `cursor_cli` 默认使用各自 Desktop/CLI 已登录账号。配置只保存环境变量名称，不保存真实 Key：

```bash
plow-whip cli-auth status
plow-whip cli-auth mode codex_cli desktop
plow-whip cli-auth mode cursor_cli desktop

plow-whip cli-auth add codex_cli --name api-1 --env OPENAI_KEY_1 --model MODEL_ID
plow-whip cli-auth add cursor_cli --name api-1 --env CURSOR_KEY_1 --model MODEL_ID
plow-whip cli-auth select codex_cli --name api-1
plow-whip cli-auth failover codex_cli on
plow-whip cli-auth mode codex_cli pool
```

Pool 模式按 active profile 优先，再按配置顺序尝试其他可用 profile。只对认证、额度、限流、连接超时和服务端错误执行有界切换；代码失败或任务未推进不会换 Key。模型随 profile 切换。空池不会回退到其他账号，而是明确失败。

当前执行 Driver 包括 `codex_cli`、`cursor_cli`、`zellij` 和 `file`；通知和可选 Brain 属于投递兜底，不是 Agent 身份。逻辑 owner 优先使用自己的 Driver；遇到不可用、中断或网络故障时，同一轮由 Router 选择另一个同职责、同能力且 Driver 不同的 Agent 代跑，任务 owner、规则和验收命令不变。没有 Driver 实际执行时才记为 `queued` 并写入文件 inbox；这类投递只占用一个 scheduler 周期，不会被当成已完成。

父调度器负责 CLI Driver 的 `running/completed/failed` lifecycle 回写，子 Agent 不重复写 inbox。Desktop/file 通道由外部接管时，仍可按 dispatch ID 更新单条任务，不需要清空整个 inbox：

```bash
plow-whip inbox list --agent codex_cli
plow-whip inbox update --agent codex_cli --dispatch-id DP-123 \
  --status running
plow-whip inbox update --agent codex_cli --dispatch-id DP-123 \
  --status completed --output "tests passed"
```

## CLI 会话生命周期

每个任务可以分别绑定一个 `cursor_cli` 会话和一个 `codex_cli` 会话；会话 ID 必须由 CLI 生成，plow-whip 只负责记录、恢复和归档：

```json
{
  "cli_sessions": {
    "cursor_cli": {"session_id": "...", "status": "active"},
    "codex_cli": {"session_id": "...", "status": "active"}
  }
}
```

- Cursor CLI 首次运行使用 `--output-format stream-json`，从 `system/init.session_id` 取 ID；后续使用 `--resume=<session_id>`。
- Codex CLI 首次运行使用 `exec --json` 捕获 session/thread ID；后续使用 `exec resume <session_id>`。
- 同一任务、同一 CLI 如果返回不同 ID，立即失败，禁止产生第二个会话。
- `task complete` 将该任务的全部 CLI 会话标记为 `archived`；下一任务从空 `cli_sessions` 开始。

## 验证

```bash
python3 -m unittest discover -s tests
python3 -m plow_whip.agent_flow --help
```

## License

MIT
