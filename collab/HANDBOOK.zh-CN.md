# 多 Agent 协作手册

> 本文件由 `AGENT_PROTOCOL.json` 单向生成，只供人类阅读，请勿手改。

## 全局原则

- **R001**（不可覆盖） `required/inherited/block`：每个 Agent 工作会话只通过 plow-whip start 进入；任务入口使用 submit，定时续作使用 whip --once。
- **R002**（不可覆盖） `required/inherited/block`：Agent 禁止用 rm 或 unlink 删除项目内容，删除项移入项目内 by_rm；框架仅可清理自身临时运行文件与定时任务产物。
- **R003**（不可覆盖） `required/inherited/require_approval`：除非用户明确授权，Agent 文件操作只能位于当前项目根目录；仅框架自有配置、运行状态与原生定时任务文件属于基础设施例外。
- **R004**（不可覆盖） `required/inherited/require_approval`：除非用户明确授权，禁止创建或委派临时嵌套子智能体；允许通过 Registry、Router、Task、Handoff 在已注册 plow-whip Agent 间接力。
- **R005** `required/inherited/block`：工作流状态只能通过 submit、task、handoff、plan、review、goal、automation 等 plow-whip 状态迁移命令更新，禁止直接编辑 AGENT_STATE.json。
- **R006** `important/inherited/warn`：启动只加载 Hot；Warm 仅加载定向信息；Cold 历史按需搜索。
- **R007** `important/inherited/warn`：自动续作仅处理超时的 active 任务；done/blocked 永不派发，未进展任务在有限租约后才重试。
- **R008** `important/inherited/warn`：每个 CLI 在一个任务中最多绑定一个自身生成的会话；持续恢复该会话，任务完成后归档。
- **R009** `required/inherited/verify`：代码修改任务必须配置验收命令并全部通过，且须完成独立 Reviewer 验收后才能交付；失败时保持 active 并继续修复。

## 项目原则

- **P001** `required/local/block`：Agent 启动上下文必须有界，禁止恢复多文件强制重复读取。
- **P002** `required/local/block`：AGENT_PROTOCOL.json 与 AGENT_STATE.json 是真源；中文 Markdown 和兼容文件均为派生视图。
- **P003** `required/local/block`：定时续作必须适配 macOS、Linux、Windows，并以带锁单次进程运行。

## 入口与状态迁移

- 人或外部工具通过 `submit` 投递任务；Agent 每次开始或恢复工作前通过 `start --agent ... --json` 获取有界上下文。
- 系统定时任务只运行带锁的 `whip --once`；模型仅在存在可恢复的超时 active Task 时由 Worker 调用。
- Agent 只能使用 `submit`、`task`、`handoff`、`plan`、`review`、`goal`、`automation` 等命令推进状态，框架内部通过 revision 与原子写维护 `AGENT_STATE.json`。
- 临时嵌套子智能体受 R004 限制；Registry 中已注册 Agent 之间的 Planner、实现、Reviewer、故障接力属于 plow-whip 编排。

## 无人值守闭环

1. 本地分类器将明确任务直接路由；复杂、模糊或无法确认的任务交给可配置 Planner。
2. Planner 只提交粗粒度里程碑；计划必须由人确认，确认后才恢复无人值守。
3. 代码任务在独立任务分支执行，完成验收命令后进入独立 Reviewer。
4. Reviewer 默认使用不同 Driver；资源不足时使用同一 CLI 的不同逻辑 Agent 与全新 Session。
5. 验收通过后推送任务分支，并仅以 fast-forward 更新目标分支；无法快进时暂停等待人工处理。
6. 网络或服务故障按 Driver 独立熔断；连续探测成功达到阈值后恢复原任务与会话。

## 编排默认值

| 配置 | 当前值 |
|---|---|
| 默认 Planner | `codex_cli` |
| 默认目标分支 | `main` |
| 系统调度间隔 | 60 秒 |
| 每种 Driver 最大并发 | 5 |
| 实现失败重试上限 | 3 |
| 熔断恢复连续成功次数 | 3 |
| 新项目无人值守 | 默认开启 |
| 计划确认 | 必须由人确认 |
| Git 交付 | 独立任务分支、独立 Reviewer、fast-forward only |

## 文件真源

| 领域 | 真源 |
|---|---|
| 规则、Registry、编排配置 | `collab/AGENT_PROTOCOL.json` |
| 当前 Task、Workflow、Session 绑定 | `collab/AGENT_STATE.json` |
| Simple-tasker 完整持久会话 | `collab/memory/sessions/<task>_simple_tasker.jsonl` |
| CLI 熔断与 Worker 进程登记 | 框架运行目录中的 `health.json`、`workers.json` |
| 分支与远端交付结果 | Git refs 与远端仓库 |
| 中文手册、Agent 阵容表、兼容 Markdown | 派生视图，不是真源 |

## Agent 阵容

| Agent | Roles | Driver | Capabilities | Assignment |
|---|---|---|---|---|
| `codex` | planner, coordinator, reviewer | codex_cli | * | — |
| `cursor` | implementation, reviewer | zellij | * | 桌面打工仔 |
| `cursor_cli` | planner, implementation, reviewer | cursor_cli | * | CLI 打工仔 |
| `codex_cli` | planner, implementation, reviewer | codex_cli | * | — |
| `reviewer` | reviewer | file | review | Review implementation and risks |
| `goal-planner` | planner | codex_cli | e2e-plan | Plan coarse goals |
| `e2e-worker` | e2e-worker | cursor_cli | e2e-readonly | Run read-only implementation acceptance |
| `e2e-worker-backup` | e2e-worker | codex_cli | e2e-readonly | Take over failed E2E worker runs |
| `e2e-auditor` | e2e-auditor | codex_cli | e2e-review | Independently accept completed E2E work |

> `goal-planner` 仅保留兼容；默认规划使用 `orchestration.default_planner`，除非任务明确指定。

> `simple-tasker` 是内置按需 Agent：首次命中简单任务路由时自动注册；未注册不表示 Driver 不受支持。

> `file` Driver 只负责 inbox/人工接管，不构成端到端无人值守；自动 Reviewer 会选择可执行 CLI Driver。

## 密钥与网络边界

- Codex/Cursor 可使用 Desktop 登录或只保存环境变量名称的 Key Pool；真实 Key 不写入项目、状态或日志。
- DeepSeek Key 只从 `DEEPSEEK_API_KEY` 或编号环境变量读取；仅记录后四位与哈希组成的脱敏标识。
- Simple-tasker 在项目沙箱内读写、测试并持久化本地 JSONL Session；禁止自行提交、推送、合并或越出项目。
- 国内网络、海外出口、TLS 与 Provider 分开探测；全局海外网络故障暂停外部 CLI，单 Provider 故障只暂停对应 Driver。
