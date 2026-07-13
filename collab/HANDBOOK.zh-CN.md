# 多 Agent 协作手册

> 本文件由 `AGENT_PROTOCOL.json` 单向生成，只供人类阅读，请勿手改。

## 全局原则

- **R001**（不可覆盖） `required/inherited/block`：只通过 plow-whip start 进入项目。
- **R002**（不可覆盖） `required/inherited/block`：禁止 rm；删除内容移动到项目内 by_rm。
- **R003**（不可覆盖） `required/inherited/require_approval`：除非用户明确授权，否则只读写当前项目根目录。
- **R004**（不可覆盖） `required/inherited/require_approval`：除非用户明确授权，否则禁止委派子智能体。
- **R005** `required/inherited/block`：通过 task 或 handoff 更新工作；AGENT_STATE.json 是运行状态真源。
- **R006** `important/inherited/warn`：启动只加载 Hot；Warm 仅加载定向信息；Cold 历史按需搜索。
- **R007** `important/inherited/warn`：自动续作仅处理超时的 active 任务；done/blocked 永不派发，未进展任务在有限租约后才重试。
- **R008** `important/inherited/warn`：每个 CLI 在一个任务中最多绑定一个自身生成的会话；持续恢复该会话，任务完成后归档。
- **R009** `required/inherited/verify`：配置了验收命令的任务只有全部通过才算完成；失败时保持 active 并继续修复。

## 项目原则

- **P001** `required/local/block`：Agent 启动上下文必须有界，禁止恢复多文件强制重复读取。
- **P002** `required/local/block`：AGENT_PROTOCOL.json 与 AGENT_STATE.json 是真源；中文 Markdown 和兼容文件均为派生视图。
- **P003** `required/local/block`：定时续作必须适配 macOS、Linux、Windows，并以带锁单次进程运行。

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
