# plow-whip（耕田之鞭）

**让多个 AI Agent 围绕同一个项目状态持续交付，而不是靠聊天记录猜测上下文。**

plow-whip 是一个面向本地开发项目的多 Agent 协作状态机与无人值守调度器。它把任务、规则、交接、验收和执行证据保存在项目内，通过确定性路由连接 Codex CLI、Cursor CLI、DeepSeek simple-tasker、zellij 或文件 inbox。适合同时使用多个 Agent、需要跨会话接力，或希望任务能在系统定时器下自动测试、审查并交付到 Git 的开发者和团队。

它解决的不是“如何再调用一个模型”，而是协作过程中的几个具体问题：

- **上下文漂移**：启动只返回当前任务、有效规则和定向信息，不要求每轮重读整套文档。
- **职责混乱**：Registry 描述长期角色与能力，Router 确定性选人，Driver 负责实际执行。
- **状态失真**：任务进度、下一步、阻塞和验收结果原子写入一个状态真源。
- **会话越权**：新项目默认使用签名租约；无租约会话只有 observer 权限，不能机器回写或进入交付。
- **自动化中断**：系统原生 scheduler 定期运行一次性扫描，只恢复明确启用且需要续作的任务。
- **Token 成本失控**：状态探针和任务分类完全在本地完成；明确的小任务可交给持久化的 DeepSeek simple-tasker。
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
Submit（严格模式由绑定的人机控制面进入：macOS 默认 Codex Desktop，其他系统为交互式终端；旧模式可由其他入口进入）
  └─ 本地零 Token 分类：direct / simple / needs_planner
      ├─ direct：明确且有界，直接交给指定 CLI
      ├─ simple：交给文件持久化的 DeepSeek simple-tasker
      └─ needs_planner：Codex CLI 规划，必须由人确认里程碑
          └─ Task（当前唯一原子工作单元）
          ├─ Registry：有哪些长期 Agent，它们的角色、能力、Driver 和 schedulable
          ├─ Router：按角色、能力、优先级、成本和可用性确定性选人
          ├─ Driver：codex_cli / cursor_cli / simple_tasker / zellij / file
          └─ State：进度、下一步、PID、验收、会话、熔断与 Git 生命周期
```

两个 canonical 真源和一个启动入口约束整个协作过程：

| 真源 | 作用 |
|---|---|
| `collab/AGENT_PROTOCOL.json` | 机器协议、有效规则与项目 Registry |
| `collab/AGENT_STATE.json` | 当前 Workflow、Task、owner、进度、会话和投递状态；严格项目带 authority 完整性签名 |
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
  --owner codex_cli \
  --first-action "完成首个可验证任务"
```

结构缺失时使用显式修复：

```bash
plow-whip --project MyProject repair --json
```

`doctor --repair` 是兼容入口。`doctor` 会校验协议、状态、task owner、派生字段和 authority 签名；损坏或被直接篡改的 canonical JSON 不会被 repair 静默覆盖。

由当前版本 `init/new` 创建的项目默认写入 `enforcement.mode=strict` 和独立 `protocol_epoch`。升级前已经存在、且协议中没有 `enforcement` 的项目继续按旧模式运行，不会被自动迁移。

### 3. 提交任务

```bash
plow-whip --project MyProject submit "实现健康检查并运行测试"
```

提交后先进行本地分类，不调用模型。明确指定 `--cli cursor_cli` 的有界任务直接执行；高置信度小任务交给 `simple-tasker`；宽泛或模糊任务交给默认 Planner `codex_cli`。Planner 只能提出计划：

```bash
plow-whip --project MyProject plan status
plow-whip --project MyProject plan confirm
```

计划未确认时状态为 `blocked_waiting_human`，scheduler 不会执行。确认后恢复无人值守。旧的 `task start` 和 `goal` 命令仍保留兼容。

严格项目的 `submit`、计划确认、决策答复、自动化开关等控制命令必须来自当前绑定的人机控制面。macOS 校验 Codex Desktop Thread 与原生 App 父进程链；Linux/Windows 绑定真实交互式终端会话。项目只公开不可逆引用；后台 CLI Worker 的标准流被重定向，既不继承 Desktop 身份也不能取得交互终端身份。首次控制动作完成绑定；macOS 更换 Desktop 会话时先显式执行 `desktop sync`。

### 4. Worker 回写进度并完成验收

以下命令只供 scheduler 签发租约后启动的 Worker 使用。Codex Desktop 或其他无租约会话执行 `start` 时会得到 `authorization.mode=observer`，启动包不会包含 `writeback`：

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
submit（项目默认启用 automation）
  → 系统 scheduler 每 60 秒启动一个有锁的零 Token 短进程
  → 原子领取 Task，签发绑定 Owner/Driver/dispatch/epoch 的短期租约
  → 后台 Worker 只在任务独立 worktree 中执行，并绑定一个 CLI Session
  → 实现完成后运行 verify_commands
  → 提交候选 commit；独立 Reviewer（默认不同 CLI）验收准确 SHA
  → Reviewer 拒绝则恢复原执行器的原 Session 修复
  → scheduler 发布进程推送任务分支
  → 已配置受保护合并身份时 fast-forward；否则提示人工合并
  → scheduler 检测远端目标分支包含已验收 SHA 后完成 Workflow
```

安装跨平台用户级定时任务：

```bash
# 先预览，不写系统配置
plow-whip scheduler install --dry-run

# 默认每 60 秒自动续作
plow-whip scheduler install

plow-whip scheduler status
```

新项目默认 `automation_enabled=true`，首次真实用户初始化还会确保本机 scheduler 已安装。可以使用 `automation disable|enable|status` 控制单个项目。`blocked`、`blocked_waiting_human` 和 `done` 永不派发。

| 系统 | 原生机制 | 用户级配置位置 |
|---|---|---|
| macOS | launchd | `~/Library/LaunchAgents/com.plow-whip.scheduler.plist` |
| Linux | systemd user timer | `~/.config/systemd/user/plow-whip.{service,timer}` |
| Windows | Task Scheduler | 当前用户任务 `PlowWhipScheduler` |

调度器固定已安装的 `plow-whip` 绝对入口并写入稳定 PATH，不依赖交互式 shell。周期调度由操作系统负责；每次运行都是短生命周期、跨平台单实例锁保护的 `whip --once`。

### 自动化的真实边界

- `codex_cli` 和 `cursor_cli` Driver 可以在 CLI 已安装、已认证且权限充足时真实执行任务。
- `simple_tasker` 复用现有 DeepSeek Brain API 客户端，但增加项目沙箱工具、Task 级 JSONL 会话、断点恢复和自动上下文压缩；生产 Key 只读取环境变量。
- `zellij` 依赖可用的本地会话且无法安全继承 Worker 租约，因此只保留旧模式兼容；严格模式只调度可携带租约的 CLI Driver。`file` 只写 inbox，必须由外部客户端接管，不能单独称为端到端无人值守。
- Desktop Agent 没有可执行通道时，只进入 inbox 或系统通知等待接管。
- 新项目默认使用本机人机控制面：macOS 是 Codex Desktop，Linux/Windows 是首次绑定的交互式终端；可 `submit`、查看状态、确认计划和答复决策，但没有 Worker 租约。
- 默认 Cursor Desktop 只是不可调度的观察身份；本版没有它的可信会话授权适配器，因此不能在严格项目中提交、确认或答复决策。Cursor CLI 仍可由 scheduler 持租约执行任务。
- 签名执行租约保持默认启用：状态和日志只保存不可逆 lease ID，不保存 Token；scheduler 可续期活跃租约元数据。状态 HMAC、协议 authority pin 和原生父进程链实现保留在代码中，但默认不调用；只有显式设置 `enforcement.reserved_hardening` 才启用这些同用户加固层。
- macOS 控制面绑定当前 Codex Desktop Thread；Linux/Windows 绑定第一个交互式终端。旧入口丢失后，新交互终端可显式执行 `desktop rebind`，无需旧终端确认；授权日志同时记录旧、新不可逆引用。该边界防止过期会话和误操作，不防御同一 OS 用户下蓄意恶意的 CLI 进程。
- Worker worktree 的 push URL 被禁用；发布由 scheduler 父进程从控制 checkout 完成。目标分支自动更新默认关闭，未配置受保护身份时只推送 `plow/*` 分支；人工合并冲突期间保留 `awaiting_human_merge`，推送完成后由 scheduler 自动对账。
- `blocked` 和 `done` 永不自动派发。任务需要外部凭据、人工审批或产品决策时，应明确 block，而不是绕过边界。
- API Key/Profile 池只对认证、额度和限流错误切换。网络或服务异常打开对应 CLI 的独立熔断器，不累计 Task 重试；连续三次无 Token 探测成功后自动恢复。
- 海外出口探测包含 DNS、国内网络、`curl ifconfig.me`、TLS 和 Provider 端点。全局海外网络中断会熔断全部外部 CLI；单个 Provider 故障只暂停对应 CLI。
- plow-whip 不代替操作系统权限、密钥管理、沙箱、代码审查或部署审批。

<a id="reliability"></a>
## 可靠性设计与边界

### 原子状态与并发保护

`task progress|block|complete` 在严格项目中必须同时通过租约签名、Task、Owner、dispatch ID、租约代数、有效期和 `protocol_epoch` 校验。JSON/schema 与语义校验、revision 冲突检测、原子替换和审计日志保持在默认热路径；状态 HMAC 与 authority pin 属于保留但默认关闭的可选层。

`start` 对无租约会话仍返回有界只读上下文，但标记为 `observer` 且不返回写回命令。旧线程即使保留历史指令，也无法重放过期租约。

### 有界恢复，不无限烧 token

默认探针只读状态、时间戳、任务 ID、PID、Session、revision、Git 和熔断状态，不消耗模型 token。每个 Task 同时只有一个有效租约、一个 Worker 和一个 `active_session`；旧 Session 只进入 archive。Git-backed recovery snapshot 不依赖 CLI 原始 transcript，可跨会话、跨 CLI、跨电脑继续。`start --json` 使用紧凑英文 JSON，正常预算 600、硬上限 1200 token，规则最多 8 条。

撤销或替换时先立即撤销旧租约并标记 `stopping`，再发送 TERM。scheduler 至少等待 30 秒；进程仍存活才发送 KILL，且 PID 消失前不会启动替代 Worker。会话估算 3000 token 软轮换、4000 token 硬轮换，文件安全阈值 16384 bytes，carry-forward 最多 300 token；完整历史保持 cold、按需读取。

### Controller 回执与前台可打断性

controller 是项目当前绑定的协调会话，不等同于固定 PM 名称：存在唯一 PM/协调角色会话时优先绑定，否则使用当前人类对话。controller 只做原子派发并记录 `awaiting_receipt`，随后立即结束 turn；禁止在前台用 `wait` 或 `read_thread` 循环 babysit 执行会话。人的消息在短派发事务结束后立即处理，不通过中止半个派发来抢占。

执行结果先持久化，再写入 `~/.plow-whip/logs/controller-receipts.jsonl` 的追加式私有回执。scheduler 用任务 ID、dispatch ID 和 lease 代次拒绝旧回执，重复投递幂等；断网或重启后会重扫未消费回执。controller 读取结果引用并运行 `controller consume` 后才算消费完成。controller 正忙时在 20 分钟内最多唤醒三次，仍失败才恢复或新建同控制角色会话。CLI 假死判断只使用 PID、状态、时间戳和日志增量；有限探测后只携证据通知 controller，不向模型发送探测消息，也不自行重派。

### 验收驱动推进

任务有验证命令时，只有全部通过才能进入 Reviewer。每个候选 SHA 固定两次独立 Review，使用固定 blocking checklist，Reviewer 不修改候选；开放式非阻塞发现进入 backlog。两个结论冲突时只允许一次 adjudication，裁决必须返回 pass 或 block，不进入无限评审循环。

### Git 交付原子性

严格代码任务从目标分支创建带原始 Task ID 摘要的 `plow/<task-id>-<digest>`，并在本机配置目录建立带项目和 Task 摘要的独立 linked worktree；路径规范化碰撞或工作树属于另一仓库时直接阻塞，控制 checkout 不再切换任务分支。实现结束先提交候选 SHA，Reviewer 只能验收该 SHA；Reviewer 改动文件或 HEAD 变化会触发完整性阻塞。

发布进程先推送任务分支。仅当 `orchestration.auto_merge_protected=true` 且远端允许受保护发布身份时，才尝试 fast-forward 目标分支；否则进入 `awaiting_human_merge`。人工合并后 scheduler 自动验证 `origin/<target>` 包含准确 SHA 并标记 `delivered`，系统绝不自动 rebase。

### 必须由人决断的分裂

Worker 遇到二选一或必然分裂时使用：

```bash
plow-whip --project MyProject decision request \
  --summary "选择持久化方案" \
  --option SQLite \
  --option PostgreSQL \
  --recommended SQLite
```

系统撤销当前任务租约、写入 `human_inbox.jsonl`，并只冻结该 Task。人类控制面答复后，scheduler 签发新一代租约续接：

```bash
plow-whip --project MyProject decision answer \
  --choice SQLite \
  --note "当前是单机小规模负载"
```

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

项目 Registry 为每个逻辑 Agent 保存角色、能力、Driver、`schedulable`、优先级、成本档和当前 assignment：

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

Router 根据 `roles + capabilities + enabled + schedulable + priority + cost_tier + driver availability` 确定性选择；同优先级保持 Registry 顺序。`codex` 固定为 `schedulable=false` 的 Codex Desktop 控制面；默认 `cursor` 是 `schedulable=false` 的观察身份。两者都不会成为 Task owner、执行器、CLI Session、重试或故障切换目标。macOS 使用绑定的 Codex Desktop 会话通过严格项目的人工控制授权，Linux/Windows 使用绑定的交互式终端；`codex_cli` 与 `cursor_cli` 才是执行 Driver。项目可以显式修改 Registry，但严格项目的机器回写仍必须持有租约。

### Codex Desktop 对话同步

只有当 `CODEX_INTERNAL_ORIGINATOR_OVERRIDE` 明确等于 `Codex Desktop` 时，`submit` 才会用 `CODEX_THREAD_ID` 注册或替换 Desktop checkpoint。Codex CLI、scheduler 和仅设置 `CODEX_THREAD_ID` 的进程不会改变控制线程。显式查看或同步：

```bash
plow-whip --project MyProject desktop status
plow-whip --project MyProject desktop sync
```

同步完全在本地解析 Codex JSONL，不调用模型、不消耗 Token。它用本机 plow-whip 配置目录中的字节 checkpoint 去重；若受管沙箱不允许写用户配置，则回退到 Git 忽略的 `collab/.runtime/codex-desktop-sync.json`。同步只追加 user 文本和 assistant 的 commentary/final_answer 文本（兼容旧 final）到 `collab/conversations/codex/current.md`；developer、system、reasoning、tool、图片及其他内容全部排除。原始 thread ID 仅保存在这个本机私有 checkpoint；workflow、AGENT_STATE、scheduler JSON、status 和日志只记录不可逆的 `thread_ref`。checkpoint 不进入 canonical/Hot 或版本库，也不保存被过滤内容或本机源文件绝对路径。scheduler 每轮先续同步已绑定线程，再使用原有阈值轮转 `current.md`。

### Task Planner 与粗粒度里程碑

`submit` 的本地分类器只把高置信度任务直接派发。无法确认或范围过宽时，默认由 `codex_cli` 规划；项目可在 `AGENT_PROTOCOL.json.orchestration.default_planner` 指定其他 CLI。Planner 使用：

```bash
plow-whip --project MyProject plan propose \
  --context-summary "后续接力所需的压缩项目上下文" \
  --plan-json '[{"title":"实现并测试登录","role":"backend","acceptance":["测试通过"]},{"title":"最终集成验收","role":"reviewer","acceptance":["全量验收通过"],"final_acceptance":true}]'
```

提交计划只会触发人类确认门，不会立刻执行。`goal-planner` 不再参与默认路由。

### 兼容 Goal（仅旧模式）

严格项目统一使用 `submit` 和 `plan`，执行 `goal start` 或 `goal plan` 会直接拒绝，避免旧 Goal 绕过独立 worktree、精确 SHA 验收和受控发布。仅旧模式项目保留以下兼容入口：

```bash
plow-whip --project MyProject goal start "交付可上线的登录功能"
```

旧 `goal` 工作流仍可使用，但其默认 Planner 同样遵循 `orchestration.default_planner`，不会再按 `goal-planner` 的旧优先级选人：

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

DeepSeek 只读取环境变量，可配置一个或多个 Key：

```bash
export DEEPSEEK_API_KEY='...'
export DEEPSEEK_API_KEY_02='...'
export DEEPSEEK_MODEL='deepseek-v4-flash'  # 可选
plow-whip health status
```

任务和日志只记录类似 `deepseek/01/****A7F2/fp-82c91a` 的脱敏标识，不保存真实 Key，也不读取 `.env` 或 `~/.config/deepseek/env`。

### CLI 会话生命周期

每个任务可分别绑定一个 `cursor_cli`、`codex_cli` 和 `simple_tasker` 会话。CLI 会话 ID 由各 CLI 生成；simple-tasker 使用本地文件 Session：

- Cursor CLI 首次执行从 stream JSON 的 `system/init.session_id` 取 ID，后续使用 `--resume=<session_id>`。
- Codex CLI 首次执行从 `exec --json` 捕获 session/thread ID，后续使用 `exec resume <session_id>`。
- 同一任务、同一 CLI 若返回不同 ID，立即失败，避免产生第二条上下文链。
- `collab/memory/sessions/<task>_simple_tasker.jsonl` 追加消息、工具调用、命令结果和检查点；上下文压缩不删除完整历史。
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
├── human_inbox.jsonl     # 计划确认、任务分裂与人工合并等可恢复的人类入口
├── CONVENTIONS.agent.md  # 旧工具兼容指针
├── CONVENTIONS.md        # 旧工具兼容指针
├── conversations/        # Agent 会话与归档
└── memory/
    ├── DECISIONS.md      # 持久决策
    └── sessions/         # 历史会话与执行证据
```

新项目就绪所需的 memory 结构只有 `DECISIONS.md` 和 `sessions/`。旧版 `NEXT_ACTION.md`、`CURRENT_STATUS.md`、`ROADMAP.md` 可保留，但不参与启动和 doctor 就绪判定。

本机私有运行目录还保存签名租约密钥、可选 authority pin、控制面绑定、授权审计 `logs/authorization.jsonl` 和 `worktrees/<project>/<task>/`。这些文件不进入项目仓库；状态只记录不可逆 lease ID 与相对 `workspace_ref`。authority pin 只有显式启用保留加固时才参与加载。

协议规则仍保留 scope、priority、origin、enforcement 四个维度。模型启动包只携带最多 8 条 `{id, action, on_violation}`；租约、PID、scheduler、Git、revision 和状态校验由本地代码执行，不把这些检查折算成 prompt token。

`plow-whip` 自身发布到 `main` 时可显式标记 release branch；只有这一最终合并会触发 release gate。闸门复核 startup/recovery 预算，并要求专用公开仓库 `niugengtian/plow-whip-e2e` 的 `stable-minimal-task` 完成 submit → scheduler claim → implementation → controlled writeback → 两次 review → push → fast-forward → done；临时 target/task 分支成功后删除，测试仓库 `main` 不被修改。

发布 Task 必须在 `submit` 时显式加 `--release-branch`；该标记会进入 Git workflow。GitHub E2E 完成后，Worker 通过受租约保护的 `task progress --release-gate-report '<JSON>'` 写入运行 ID、candidate/main SHA 和临时分支证据，并在公开 E2E 仓库留下指向 candidate 的 `plow-whip-e2e/<run_id>` 证据 tag。发布进程在最终 fast-forward `main` 前会实时查询 GitHub，核对该 tag、确认 E2E 仓库 `main` 未变且两个临时分支已删除。未标记的普通 Task 不触发该闸门。

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
| `desktop sync` / `status` | 增量同步或查看 Codex Desktop 文本镜像状态 |

### Agent、启动与路由

| 命令 | 用途 |
|---|---|
| `agent list` | 列出项目 Registry |
| `agent set` | 设置角色、能力、Driver、schedulable、优先级、成本和 assignment |
| `start` | 返回完整但有界的最小启动 payload；Agent 唯一启动入口 |
| `context-pack` | `start` 的废弃兼容别名 |
| `drive` | 旧项目可直接驱动；严格项目拒绝把它作为执行入口，统一使用 `submit` + scheduler |
| `permit` | allow/ask/reject 或检查投递权限 |
| `bind-tab` | 将项目绑定到 zellij tab |

### 任务、目标与交接

| 命令 | 用途 |
|---|---|
| `submit` | 本地零 Token 分类并提交 direct/simple/planner Workflow |
| `plan propose` | Planner 提交 1–7 个里程碑，随后进入人工确认门 |
| `plan confirm` / `reject` / `status` | 确认、退回或查看非 Goal 计划 |
| `review reject` | 独立 Reviewer 拒绝并恢复原执行器 Session 修复 |
| `automation enable` / `disable` / `status` | 控制单个项目的无人值守开关 |
| `task start` | 旧项目兼容命令；严格项目拒绝并引导使用 `submit` |
| `task progress` | 原子回写产出、下一步，并可更新验收与验证命令 |
| `task block` | 写入阻塞原因并暂停自动派发 |
| `task complete` | 运行验证命令；通过后完成任务并推进 Goal |
| `decision request` | Worker 撤销当前租约并提交 2–3 个必须由人选择的方案 |
| `decision answer` / `status` | 控制面答复或查看决策；答复后 scheduler 重新签发租约 |
| `goal start` | 仅旧模式：提交或排队一个交付目标；严格模式使用 `submit` |
| `goal plan` | 仅旧模式：提交 1–7 个粗粒度里程碑和压缩上下文 |
| `goal status` | 查看旧模式 Goal 与进度；严格模式只保留只读查看 |
| `handoff` | 将当前工作、证据、下一步和 owner 一次性交接 |

### 调度、恢复与投递生命周期

| 命令 | 用途 |
|---|---|
| `whip` | 扫描项目；`--once` 为有锁单次运行，`--crack` 才实际派发 |
| `scheduler install` | 安装默认 60 秒、自动续作的用户级定时任务 |
| `scheduler status` | 查看定时任务状态 |
| `scheduler run` | 立即执行一次 scheduler 工作负载 |
| `scheduler start` / `stop` | 启动或停止原生定时任务 |
| `scheduler logs` | 查看 scheduler 日志 |
| `scheduler doctor` / `repair` | 检查或修复 scheduler 配置 |
| `scheduler uninstall` | 卸载用户级定时任务 |
| `health status` | 查看各 CLI 独立熔断状态和脱敏 DeepSeek Key 槽位 |
| `health probe` | 执行 DNS、国内/海外出口、TLS、Provider 与 CLI 可用性探测 |
| `inbox list` | 查看某 Agent 的文件投递 |
| `inbox update` | 按 dispatch ID 更新 queued/accepted/running/completed/failed |
| `controller status` | 查看当前项目尚未消费的 controller 回执 |
| `controller consume --event-id <id>` | controller 读取持久化结果后幂等确认消费 |

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
| `brain` | `simple-tasker` DeepSeek 客户端的兼容单次入口 |

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
4. 无人值守相关改动需要覆盖项目开关、人工阻塞/done 不派发、Task 原子性、熔断、重试上限和验收失败路径。
5. PR 请说明行为变化、验证命令和兼容性影响；Bug 报告请包含平台、Python 版本、复现步骤和脱敏日志。

更多实现背景见 [架构说明](docs/architecture.md)、[核心概念](docs/concepts.md) 和 [接入指南](docs/onboarding.md)。若这些文档与 CLI 或 canonical schema 冲突，以代码、测试和机器协议为准。

## License

[MIT](LICENSE)
