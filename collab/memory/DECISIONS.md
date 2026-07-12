# Decisions Log

| ID | Date | Decision | Status |
|----|------|----------|--------|
| D-001 | — | Project initialized with plow-whip | Accepted |
| D-002 | 2026-07-09 | 禁止 `rm`，所有删除改为 `mv` 到 `by_rm/` 并加时间戳 | Accepted |
| D-003 | 2026-07-09 | 新会话强制 plow-whip 启动自检 + 全员留言板确认 | Accepted |
| D-004 | 2026-07-09 | CLI 后台并行驱使 + tail 盯梢（禁止同步 whip --crack 阻塞） | Accepted |
| D-005 | 2026-07-09 | `plow-whip drive` 命令：Codex 驱使 cursor_cli / codex_cli | Accepted |
| D-006 | 2026-07-12 | `context-pack` 优先启动 + `new` 一键项目接入 | Accepted |
| D-007 | 2026-07-12 | `memory-budget` 零正文预算仪表 + 轮转 `Carry Forward` 摘要 | Accepted |
| D-008 | 2026-07-12 | P-1 零号启动协议：先 `doctor --repair`，再进入项目工作 | Accepted |
| D-009 | 2026-07-12 | 禁止 Agent 自行开子智能体，除非用户临时明确指定 | Accepted |
| D-010 | 2026-07-12 | 项目边界原则：禁止越过当前项目目录修改全局配置或其他项目 | Accepted |
| D-011 | 2026-07-12 | CONVENTIONS 分层：sync 只推全局原则，项目原则优先 | Accepted |

## D-002: Safe file deletion via by_rm archive

- **Date:** 2026-07-09
- **Author:** Human（沿用 JobBrain / codex_qoder_collab 既有约定）
- **Status:** ✅ Accepted
- **Rule:** 【P0 最高优先级，CONVENTIONS.md 最前】禁止使用 `rm` / `rm -rf`。所有"删除"改为 `mv` 到项目根 `by_rm/<原目录>/<原文件名>_YYYYMMDD_HHMMSS.<后缀>`
- **Reason:** 防止不可逆误删；可追溯、可审计、可恢复
- **Trade-off:** 占用磁盘 vs 安全性
- **Alternatives:** git 恢复、trash CLI — 均不如 by_rm 直观且与 plow-whip 框架一致
- **Enforcement:** `CONVENTIONS.md` 【P0】；`by_rm/` 在 `.gitignore`；框架代码 `qoder_session._safe_remove()` / `cmd_archive()` 已遵循

## D-003: Mandatory plow-whip startup self-check for all agents

- **Date:** 2026-07-09
- **Author:** Human
- **Status:** ✅ Accepted
- **Rule:** 任何 AI Agent（含 Desktop 与 CLI）开启新会话时，必须先读 `CONVENTIONS.md`（含 P0），再读 `AGENT_STATE.json`、`AGENT_COMMS.md`、`memory/NEXT_ACTION.md`；写入会话记忆；在 `AGENT_COMMS.md` Acknowledgments 区回复确认
- **Reason:** 确保全员接入 plow-whip，防止 Agent 跳过约定直接干活
- **Trade-off:** 启动多几步 vs 协作一致性
- **Enforcement:** `CONVENTIONS.md` §6；`AGENT_COMMS.md` 全员公告；未确认视为未接入

## D-004: CLI parallel dispatch with tail-only monitoring

- **Date:** 2026-07-09
- **Author:** Human
- **Status:** ✅ Accepted
- **Rule:** 编排者驱使 `cursor_cli` / `codex_cli` 时：后台并行启动 + 日志落 `/tmp/plow-whip-logs/`；`sleep 60 && tail -10` 盯结果；成功看 `AGENT_COMMS.md` Acknowledgments + `status`；失败 `tail -50` 往上翻
- **Reason:** `whip --crack` 同步调用 codex_cli 会阻塞 30min；编排者读全文日志浪费 token
- **Anti-pattern:** 同步 `whip --crack` 无 `--channel file` 驱使多个 CLI
- **Enforcement:** `CONVENTIONS.md` §4 CLI 驱使与结果盯梢

## D-005: plow-whip drive command for Desktop → CLI orchestration

- **Date:** 2026-07-09
- **Author:** Human + cursor Desktop
- **Status:** ✅ Accepted
- **Rule:** Codex Desktop 用 `plow-whip --project X drive cursor_cli --next "..." --from-agent codex` 驱使 CLI；默认 inbox + 后台 CLI；`drive --status` 盯结果
- **Module:** `plow_whip/drive.py`

## D-006: Context-pack first and one-command project onboarding

- **Date:** 2026-07-12
- **Author:** Human + Codex
- **Status:** ✅ Accepted
- **Rule:** Agent 启动优先运行 `plow-whip --project X context-pack --agent <agent>`，只在上下文包不够时读 `AGENT_COMMS.md` / `DECISIONS.md` 原文；新项目用 `plow-whip --project X new --owner <agent> --first-action "..."`
- **Reason:** 把 Hot 层编译成最小上下文包，减少重复读全文 Markdown；让新项目一键继承 plow-whip 协作规则
- **Modules:** `plow_whip/agent_flow.py`, `plow_whip/drive.py`

## D-007: Memory budget meter and Carry Forward rotation summaries

- **Date:** 2026-07-12
- **Author:** Human + Codex
- **Status:** ✅ Accepted
- **Rule:** 用 `plow-whip --project X memory-budget` 查看 Hot/Warm/Cold 预算；该命令只用文件大小估算 token，不读取正文。会话轮转归档必须写入 `## Carry Forward`，下个会话优先读继承摘要，不读完整历史。
- **Reason:** 让三层记忆预算可见，避免 Hot/Warm 悄悄变胖；轮转后保留可执行上下文，减少冷历史读取。
- **Modules:** `plow_whip/agent_flow.py`

## D-008: P-1 plow-whip mechanism self-check before any project work

- **Date:** 2026-07-12
- **Author:** Human + Codex
- **Status:** ✅ Accepted
- **Rule:** 任何 Agent 进入项目、打开新会话、接到任务或准备改文件前，第一件事必须运行 `plow-whip --project X doctor --repair`；如果 plow-whip 机制不存在或缺文件，先建立/补齐。第二件事是遵守 `CONVENTIONS.md`，再继续 context-pack 和任务执行。
- **Reason:** 确保所有项目和新会话都被 plow-whip 接管，避免 Agent 绕开状态机、记忆层、留言板和安全删除规则。
- **Modules:** `plow_whip/agent_flow.py`, `plow_whip/templates/CONVENTIONS.md.tpl`


## D-009: No self-spawned subagents without explicit user instruction

- **Date:** 2026-07-12
- **Author:** Human
- **Status:** ✅ Accepted
- **Rule:** 除非用户在当前任务中临时明确指定，否则任何 Agent 不允许自行创建、调用、委派或并行启动子智能体。所有跨 Agent 作业必须通过 plow-whip 的三层记忆、留言板、状态机和 handoff/drive 机制接力。
- **Reason:** 防止子智能体绕过 `doctor --repair -> context-pack -> CONVENTIONS.md`，产生 Hot/Warm/Cold 三层记忆断层、token 成本失控和责任边界不清。
- **Enforcement:** `CONVENTIONS.md` 【P-0.5】；`plow_whip/templates/CONVENTIONS.md.tpl`；如用户临时允许子智能体，输出必须回填到当前 agent 会话记忆、`AGENT_COMMS.md` 或 handoff 记录。


## D-010: Project boundary principle

- **Date:** 2026-07-12
- **Author:** Human + Codex
- **Status:** ✅ Accepted
- **Rule:** 除非用户在当前任务中明确指定路径，否则 Agent 只能读取和修改当前项目根目录内的文件。禁止擅自修改 `~/.plow-whip/config.json`、其他项目目录、其他项目 `collab/`、其他项目 agent 会话文件或全局 agent 阵容。
- **Reason:** 防止一个项目的 agent 阵容污染另一个项目，避免误删或误改其他项目会话造成三层记忆断层。
- **Enforcement:** `CONVENTIONS.md` 【P-0.75】；`plow_whip/templates/CONVENTIONS.md.tpl`；发现项目外风险时只报告，不代改；误触项目外文件时必须立即停止、说明并恢复。


## D-011: Layered conventions sync

- **Date:** 2026-07-12
- **Author:** Human + Codex
- **Status:** ✅ Accepted
- **Rule:** `CONVENTIONS.md` 分为全局原则和项目原则。`plow-whip sync` 只能更新 `<!-- plow-whip:global-principles:start/end -->` 包住的全局原则块；项目原则保留在标记外，并优先于全局原则。
- **Reason:** 全局安全/协作原则需要跨项目传播，但每个项目的角色、业务约束、会话规则和本地决策不能被框架同步覆盖。
- **Enforcement:** `cmd_sync()` 使用 `write_conventions(..., sync_global_only=True)`；未分层旧文件会被非破坏式迁移，旧内容保留为项目原则。
