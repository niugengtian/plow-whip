# cursor Session — plow-whip

**AI:** Cursor Desktop (替代 Qoder CN Desktop)
**Assignment:** 桌面打工仔
**Started:** 2026-07-09
**Topic:** —

## Previous
- Replaces Qoder CN Desktop for active desktop work.

## Current Tasks
- (check NEXT_ACTION.md)

## Key Decisions
- D-002: 禁止 `rm`，删除改 `mv` 到 `by_rm/` + 时间戳（2026-07-09）
- D-003: 新会话强制 plow-whip 启动自检 + 全员留言板确认（2026-07-09）

## Startup Protocol (mandatory)
1. Read CONVENTIONS.md (P0 by_rm first)
2. Read AGENT_STATE.json / AGENT_COMMS.md / NEXT_ACTION.md
3. Write to conversations/<agent>/current.md
4. Reply in AGENT_COMMS.md Acknowledgments

## CLI Orchestration (D-004, cursor as orchestrator)
- Drive cursor_cli/codex_cli in background, logs to /tmp/plow-whip-logs/
- Poll: sleep 60 && tail -10; success = Acknowledgments post; fail = tail -50
- Never block on synchronous whip --crack for codex_cli

## Outputs
- (outputs will be appended here)
