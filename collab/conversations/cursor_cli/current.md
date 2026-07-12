# cursor_cli Session — plow-whip

**AI:** Cursor CLI (替代 Qoder CLI)
**Assignment:** CLI 打工仔
**Started:** 2026-07-09
**Topic:** D-003 启动自检

## Previous
- Replaces Qoder CLI for active CLI work.

## Current Tasks
- D-003 启动自检：已读 `collab/CONVENTIONS.md`（含 P0 by_rm）、`collab/AGENT_STATE.json`、`collab/AGENT_COMMS.md`、`collab/memory/NEXT_ACTION.md`。

## Key Decisions
- 启动先读 CONVENTIONS.md P0（禁止 `rm`，改 `mv` 到 `by_rm/`）
- CLI 驱使：后台并行 + `tail -10` 盯梢（D-004）
- cursor-agent 在 Cursor IDE 内嵌调用会 socket hang up；需外部 Terminal 驱使

## Outputs
- 2026-07-09：D-003 确认帖写入 `AGENT_COMMS.md`（cursor-agent 环境故障，由 cursor Desktop 编排者代落盘）
