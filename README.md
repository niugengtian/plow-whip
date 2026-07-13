# plow-whip（耕田之鞭）

**让多个 AI Agent 围绕同一个项目状态持续交付，而不是靠聊天记录猜测上下文。**

plow-whip 是一个面向本地开发项目的多 Agent 协作状态机与无人值守调度器。它把任务、规则、交接、验收和执行证据保存在项目内，通过确定性路由连接 Codex CLI、Cursor CLI、zellij 或文件 inbox。适合同时使用多个 Agent、需要跨会话接力，或希望长任务能在系统定时器下自动续作的开发者和团队。

它解决的不是“如何再调用一个模型”，而是协作过程中的几个具体问题：

- **上下文漂移**：启动只返回当前任务、有效规则和定向信息，不要求每轮重读整套文档。
- **职责混乱**：Registry 描述长期角色与能力，Router 确定性选人，Driver 负责实际执行。
- **状态失真**：任务进度、下一步、阻塞和验收结果原子写入一个状态真源。
- **自动化中断**：系统原生 scheduler 定期运行一次性扫描，只恢复明确启用且需要续作的任务。
- **结果难追责**：每次投递记录逻辑 owner、实际 executor、Driver、dispatch ID 和精简失败证据。

> 当前版本为 `0.1.0`（Alpha）。plow-whip 是本地编排与状态管理工具，不是托管式 Agent 平台；无人值守能力取决于本机 CLI、认证、权限和系统定时器是否可用。

<a id="contents"></a>
## 导航

- [核心模型](#core-model)
- [快速上手：完成首个可验证任务](#quick-start)
- [真实无人值守如何工作](#unattended)
- [可靠性设计与边界](#reliability)
- [主要能力](#capabilities)
- [项目内的数据](#project-data)
- [完整命令索引](#command-index)
- [开发、测试与贡献](#contributing)

<a id="core-model"></a>
## 核心模型

plow-whip 将“谁负责”与“用什么执行”分开：

```text
Goal（人给出的交付目标）
  └─ Plan（1–7 个粗粒度里程碑）
      └─ Task（当前唯一原子工作单元）
          ├─ Registry：有哪些长期 Agent，它们的角色、能力和 Driver
          ├─ Router：按角色、能力、优先级、成本和可用性确定性选人
          ├─ Driver：codex_cli / cursor_cli / zellij / file
          └─ State：进度、下一步、验收命令、会话与投递生命周期
```

两个 canonical 真源和一个启动入口约束整个协作过程：

| 真源 | 作用 |
|---|---|
| `collab/AGENT_PROTOCOL.json` | 机器协议、有效规则与项目 Registry |
| `collab/AGENT_STATE.json` | 当前 Goal、Task、owner、进度、会话和投递状态 |
| `plow-whip ... start --json` | Agent 的唯一启动入口；从真源生成最小执行包 |

Agent 名称代表长期职责身份，不绑定某个模型或客户端。一次执行可以同时区分：

- `logical_owner`：任务责任人；
- `executor`：本轮实际执行者；
- `driver`：承载执行的通道。

主 Agent 的 Driver 不可用或执行失败时，Router 只会在职责相同、能力满足的候选 Agent 中选择后备，不会把后端任务误派给设计或审计角色。

<a id="quick-start"></a>
## 快速上手：完成首个可验证任务

要求 Python 3.10+。当前仓库可用 editable install：

```bash
git clone https://github.com/niugengtian/plow-whip.git
cd plow-whip
python3 -m pip install -e .
```

### 1. 配置项目目录和默认 Agent

```bash
plow-whip configure \
  --projects-dir /absolute/path/to/projects \
  --agents codex codex_cli reviewer
```

全局配置只为新项目提供默认阵容；初始化后，以各项目的 `collab/AGENT_PROTOCOL.json` 为准。

### 2. 初始化已有项目

假设 `/absolute/path/to/projects/MyProject` 已存在：

```bash
plow-whip --project MyProject init
plow-whip --project MyProject doctor --json
```

也可以让 plow-whip 创建项目目录：

```bash
plow-whip --project MyProject new \
  --owner codex \
  --first-action "完成首个可验证任务"
```

结构缺失时使用显式修复：

```bash
plow-whip --project MyProject repair --json
```

`doctor --repair` 是兼容入口。`doctor` 会校验协议、状态、task owner 和派生字段一致性；损坏的 canonical JSON 不会被 repair 静默覆盖。

### 3. 创建任务并唤醒 Agent

```bash
plow-whip --project MyProject task start \
  --id T-001 \
  --title "建立健康检查" \
  --goal "交付一个可验证的健康检查" \
  --owner codex \
  --next "实现健康检查并运行测试" \
  --acceptance "健康检查返回成功" "测试通过" \
  --verify "python3 -m unittest"

plow-whip --project MyProject start --agent codex --json
```

`start --json` 返回当前任务、有效规则、定向消息、相关决策 ID 和准确的回写命令。Agent 正常启动无需重读完整 Markdown。

### 4. 回写进度并完成验收

```bash
plow-whip --project MyProject task progress \
  --output "健康检查已实现" \
  --next "运行验收并完成任务"

plow-whip --project MyProject task complete \
  --output "健康检查与测试已完成"
```

`task complete` 自动执行任务的全部 `verify_commands`。全部通过才将任务设为 `done`；失败时任务保持 `active`，失败命令成为新的 `next_action`，供后续修复。用以下命令确认最终状态：

```bash
plow-whip --project MyProject status
```

<a id="unattended"></a>
## 真实无人值守如何工作

无人值守不是“定时提醒人运行一条命令”。plow-whip 的完整自动续作链路是：

```text
goal start（项目明确 opt-in）
  → 系统 scheduler 每次启动一个有锁的短进程
  → 只读探针筛选 stale active 任务
  → Router 选择可执行 Agent 与 Driver
  → CLI Driver 真实执行并恢复同一会话
  → 父调度器回写 running / completed / failed
  → task complete 执行验收命令
  → 通过后推进下一里程碑，否则保持 active 等待有界重试
```

安装跨平台用户级定时任务：

```bash
# 先预览，不写系统配置
plow-whip scheduler install --interval 300 --dry-run

# 明确开启无人值守续作
plow-whip scheduler install --interval 300 --auto-continue

plow-whip scheduler status
```

`goal start` 会为当前项目设置 `automation_enabled=true`。`--auto-continue` 只派发明确 opt-in 的项目；普通 active、blocked 和 done 项目不会因全局定时器而执行。

| 系统 | 原生机制 | 用户级配置位置 |
|---|---|---|
| macOS | launchd | `~/Library/LaunchAgents/com.plow-whip.scheduler.plist` |
| Linux | systemd user timer | `~/.config/systemd/user/plow-whip.{service,timer}` |
| Windows | Task Scheduler | 当前用户任务 `PlowWhipScheduler` |

调度器固定已安装的 `plow-whip` 绝对入口并写入稳定 PATH，不依赖交互式 shell。周期调度由操作系统负责；每次运行都是短生命周期、跨平台单实例锁保护的 `whip --once`。

### 自动化的真实边界

- `codex_cli` 和 `cursor_cli` Driver 可以在 CLI 已安装、已认证且权限充足时真实执行任务。
- `zellij` 依赖可用的本地会话；`file` 只写 inbox，必须由外部客户端接管，不能单独称为端到端无人值守。
- Desktop Agent 没有可执行通道时，只进入 inbox 或系统通知等待接管。
- `blocked` 和 `done` 永不自动派发。任务需要外部凭据、人工审批或产品决策时，应明确 block，而不是绕过边界。
- API Key 池只对认证、额度、限流、连接超时和服务端错误做有界切换；代码失败或任务没有推进不会换 Key。
- plow-whip 不代替操作系统权限、密钥管理、沙箱、代码审查或部署审批。

<a id="reliability"></a>
## 可靠性设计与边界

### 原子状态与并发保护

`task start|progress|block|complete` 原子更新 `AGENT_STATE.json`，并使用 revision 防止旧写入覆盖新进度。任务状态、下一步、阻塞和验收结果不会分散在多份留言里。

### 有界恢复，不无限烧 token

默认探针只读状态、时间戳、任务 ID 和状态，不加载 skills、协议正文、任务正文、消息或历史。仅发现 stale 或状态不一致且允许恢复时，才加载该项目的任务上下文。已被实际接管但没有推进的同一动作使用 30 分钟租约，最多自动尝试 3 次；queued/failed 投递使用 1 分钟租约，最多尝试 6 次。达到上限后暂停自动派发。

### 验收驱动推进

任务有验证命令时，只有全部通过才能完成。Goal 的最后一个里程碑必须显式设置 `final_acceptance=true`，并由未参与实现且可执行的 Agent 独立验收，否则计划会被拒绝。

### 可追踪的投递生命周期

每次 dispatch 都有唯一 ID，并记录：

```json
{
  "task_id": "T-001",
  "dispatch_id": "DP-...",
  "logical_owner": "backend-primary",
  "executor": "backend-backup",
  "driver": "codex_cli",
  "status": "queued | accepted | running | completed | failed"
}
```

父调度器将精简执行证据和 `fallback_errors` 回写到当前任务或对应里程碑，不保存大段模型输出到启动 payload。过长的 `last_output` 在启动包中只返回末尾 1000 字符，完整值仍在状态真源中。

### 不承诺的事项

- 不保证模型输出正确；验收命令、独立审查和项目权限仍是必要防线。
- 不保证所有任务可自动解决；缺凭据、环境故障或需要人类判断时会停留在 active/blocked。
- 不提供云端高可用控制面；状态保存在本地项目与用户级配置中，备份和同步由使用者负责。
- Alpha 阶段的协议和 CLI 仍可能演进；升级前应运行测试，并保留项目状态备份。

<a id="capabilities"></a>
## 主要能力

### Registry、Router 与 Driver

项目 Registry 为每个逻辑 Agent 保存角色、能力、Driver、优先级、成本档和当前 assignment：

```bash
plow-whip --project MyProject agent set backend-primary \
  --role "Backend Owner" \
  --roles backend implementation \
  --capabilities python api \
  --driver cursor_cli \
  --priority 80 \
  --cost-tier low \
  --assignment "Own backend delivery"

plow-whip --project MyProject agent set backend-backup \
  --role "Backend Backup" \
  --roles backend implementation \
  --capabilities python api \
  --driver codex_cli \
  --priority 70
```

Router 根据 `roles + capabilities + enabled + priority + cost_tier + driver availability` 确定性选择；同优先级保持 Registry 顺序。可执行 Driver 是封闭集合：`codex_cli`、`cursor_cli`、`zellij`、`file`。旧 schema v3 配置可迁移为 v4，补齐稳定角色标签和执行字段。

### Goal 与粗粒度里程碑

人可以只提交目标：

```bash
plow-whip --project MyProject goal start "交付可上线的登录功能"
```

系统选择具备 `planner` 角色且 Driver 可执行的 Agent。Planner 第一次理解项目后提交 1–7 个粗粒度里程碑：

```bash
plow-whip --project MyProject goal plan \
  --context-summary "后续接力所需的压缩项目上下文" \
  --plan-json '[{"title":"实现并测试登录","role":"backend","capabilities":["api"],"acceptance":["测试通过"]},{"title":"最终集成验收","role":"reviewer","capabilities":["review"],"acceptance":["全量验收通过"],"final_acceptance":true}]'
```

Planner 通常写角色和能力，由 Router 绑定实际 owner；仅在用户明确指定时写 `owner`。每个里程碑内部完成理解、实现、测试和文档，不把每个动作拆成独立小任务。

已有 active Goal 时再次 `goal start` 会进入 FIFO 队列；只有 `--replace` 才显式替换，并把旧 Goal 写入历史：

```bash
plow-whip --project MyProject goal start "下一个交付目标"
plow-whip --project MyProject goal start "紧急目标" --replace
```

### Handoff 与三层记忆

```bash
plow-whip --project MyProject handoff \
  --to reviewer \
  --status in_progress \
  --output "实现完成" \
  --next "复核改动并运行测试"
```

handoff 同步更新 owner、任务状态、下一步、输出与阻塞，并检查会话和协作文件轮转。

- **Hot**：`AGENT_STATE.json`，每次启动加载。
- **Warm**：定向消息和当前交接，按 Agent 筛选。
- **Cold**：完整决策、历史消息和会话归档，按需搜索。

```bash
plow-whip --project MyProject memory-budget --json
plow-whip --project MyProject rotation-health --json
plow-whip --project MyProject memory-rotate
```

### CLI 认证与 Key 池

`codex_cli` 和 `cursor_cli` 默认复用各自 Desktop/CLI 登录。配置只保存环境变量名，不保存真实 Key：

```bash
plow-whip cli-auth status
plow-whip cli-auth mode codex_cli desktop

plow-whip cli-auth add codex_cli \
  --name api-1 \
  --env OPENAI_KEY_1 \
  --model MODEL_ID
plow-whip cli-auth select codex_cli --name api-1
plow-whip cli-auth failover codex_cli on
plow-whip cli-auth mode codex_cli pool
```

Pool 模式先用 active profile，再按配置顺序尝试其他可用 profile。空池明确失败，不回退到其他账号；模型随 profile 切换。

### CLI 会话生命周期

每个任务可分别绑定一个 `cursor_cli` 和一个 `codex_cli` 会话。会话 ID 由 CLI 生成，plow-whip 只记录、恢复和归档：

- Cursor CLI 首次执行从 stream JSON 的 `system/init.session_id` 取 ID，后续使用 `--resume=<session_id>`。
- Codex CLI 首次执行从 `exec --json` 捕获 session/thread ID，后续使用 `exec resume <session_id>`。
- 同一任务、同一 CLI 若返回不同 ID，立即失败，避免产生第二条上下文链。
- `task complete` 将任务的 CLI 会话标记为 `archived`；下一任务从空会话开始。

<a id="project-data"></a>
## 项目内的数据

```text
collab/
├── AGENT_PROTOCOL.json   # 机器协议与项目 Registry；协议真源
├── HANDBOOK.zh-CN.md     # 从协议单向生成的中文手册
├── AGENT_STATE.json      # 当前任务与运行状态；状态真源
├── AGENTS.md             # 从协议派生的人类阵容表
├── AGENT_COMMS.md        # 近期定向消息，自动检查轮转
├── CONVENTIONS.agent.md  # 旧工具兼容指针
├── CONVENTIONS.md        # 旧工具兼容指针
├── conversations/        # Agent 会话与归档
└── memory/
    ├── DECISIONS.md      # 持久决策
    └── sessions/         # 历史会话与执行证据
```

新项目就绪所需的 memory 结构只有 `DECISIONS.md` 和 `sessions/`。旧版 `NEXT_ACTION.md`、`CURRENT_STATUS.md`、`ROADMAP.md` 可保留，但不参与启动和 doctor 就绪判定。

规则包含四个维度：`scope=global|project`、`priority=required|important`、`origin=local|inherited|derived`、`enforcement=block|require_approval|verify|warn|inform`。`start --json` 返回全部 mandatory rules，并根据 Agent 和任务 `rule_tags` 返回相关 important rules；只有需要完整细则时才给出定向 `required_context`。`rules_meta.effective_hash` 用于识别规则变化。

<a id="command-index"></a>
## 完整命令索引

所有项目命令都可使用全局参数 `--project PROJECT`（或 `-p PROJECT`）。以 `plow-whip <command> --help` 查看参数细节。

### 安装、配置与项目生命周期

| 命令 | 用途 |
|---|---|
| `configure` | 设置项目根目录和新项目默认 Agent |
| `list` | 列出活跃项目 |
| `init` | 在已有项目中初始化 plow-whip |
| `new` | 创建新项目并初始化，可设置 owner 和首个动作 |
| `status` | 查看项目状态 |
| `doctor` | 只读检查机制与 canonical 数据，可用兼容参数 `--repair` |
| `repair` | 显式创建或修复缺失结构 |
| `reset` | 重置项目状态 |
| `archive` | 归档已完成项目 |
| `sync` | 将框架派生模板同步到已配置项目 |

### Agent、启动与路由

| 命令 | 用途 |
|---|---|
| `agent list` | 列出项目 Registry |
| `agent set` | 设置角色、能力、Driver、优先级、成本和 assignment |
| `start` | 返回完整但有界的最小启动 payload；Agent 唯一启动入口 |
| `context-pack` | `start` 的废弃兼容别名 |
| `drive` | 通过 Registry 中配置的 Driver 执行指定逻辑 Agent |
| `permit` | allow/ask/reject 或检查投递权限 |
| `bind-tab` | 将项目绑定到 zellij tab |

### 任务、目标与交接

| 命令 | 用途 |
|---|---|
| `task start` | 创建当前原子任务及其验收、验证和规则标签 |
| `task progress` | 原子回写产出、下一步，并可更新验收与验证命令 |
| `task block` | 写入阻塞原因并暂停自动派发 |
| `task complete` | 运行验证命令；通过后完成任务并推进 Goal |
| `goal start` | 提交或排队一个交付目标；可用 `--replace` 显式替换 |
| `goal plan` | 提交 1–7 个粗粒度里程碑和压缩上下文 |
| `goal status` | 查看当前 Goal 与进度 |
| `handoff` | 将当前工作、证据、下一步和 owner 一次性交接 |

### 调度、恢复与投递生命周期

| 命令 | 用途 |
|---|---|
| `whip` | 扫描项目；`--once` 为有锁单次运行，`--crack` 才实际派发 |
| `scheduler install` | 安装用户级定时任务；`--auto-continue` 开启 opt-in 自动续作 |
| `scheduler status` | 查看定时任务状态 |
| `scheduler run` | 立即执行一次 scheduler 工作负载 |
| `scheduler start` / `stop` | 启动或停止原生定时任务 |
| `scheduler logs` | 查看 scheduler 日志 |
| `scheduler doctor` / `repair` | 检查或修复 scheduler 配置 |
| `scheduler uninstall` | 卸载用户级定时任务 |
| `inbox list` | 查看某 Agent 的文件投递 |
| `inbox update` | 按 dispatch ID 更新 queued/accepted/running/completed/failed |

### 会话与记忆

| 命令 | 用途 |
|---|---|
| `session` | 查看指定 Agent 的会话状态 |
| `rotate` | 轮转指定 Agent 的当前会话 |
| `sessions-overview` | 查看全部会话概况 |
| `watch` | 轮询项目状态变化 |
| `memory-budget` | 查看 Hot/Warm/Cold token 预算 |
| `memory-rotate` | 扫描或轮转协作记忆文件 |
| `rotation-health` | 检查会话和文件轮转阈值 |

### CLI 认证与辅助能力

| 命令 | 用途 |
|---|---|
| `cli-auth status` | 查看 Codex/Cursor CLI 认证模式和 profile |
| `cli-auth mode` | 在 `desktop` 与 `pool` 间切换 |
| `cli-auth add` / `remove` | 添加或删除仅引用环境变量的 Key profile |
| `cli-auth select` | 选择 active profile |
| `cli-auth failover` | 开关有界认证故障切换 |
| `brain` | 使用可选 DeepSeek Brain 处理简单任务 |

常用参数的精确真源是当前 CLI：

```bash
plow-whip --help
plow-whip task start --help
plow-whip scheduler install --help
plow-whip drive --help
```

<a id="contributing"></a>
## 开发、测试与贡献

欢迎提交可复现的问题、文档修正和小而明确的 Pull Request：<https://github.com/niugengtian/plow-whip>。

本地开发：

```bash
git clone https://github.com/niugengtian/plow-whip.git
cd plow-whip
python3 -m pip install -e '.[dev]'
python3 -m unittest discover -s tests -q
python3 -m plow_whip.agent_flow --help
```

贡献时请遵守以下边界：

1. 以 `AGENT_PROTOCOL.json` 和 `AGENT_STATE.json` 为 canonical 数据，不增加并行真源。
2. 新命令或参数必须同步更新 CLI 帮助、测试和本 README 的命令索引。
3. 调度改动必须保持 macOS、Linux、Windows 原生支持，以及有锁的一次性执行模型。
4. 无人值守相关改动需要覆盖 opt-in、blocked/done 不派发、租约、重试上限和验收失败路径。
5. PR 请说明行为变化、验证命令和兼容性影响；Bug 报告请包含平台、Python 版本、复现步骤和脱敏日志。

更多实现背景见 [架构说明](docs/architecture.md)、[核心概念](docs/concepts.md) 和 [接入指南](docs/onboarding.md)。若这些文档与 CLI 或 canonical schema 冲突，以代码、测试和机器协议为准。

## License

[MIT](LICENSE)
