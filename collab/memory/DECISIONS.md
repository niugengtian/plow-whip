<!-- Previous content archived to: /Users/niugengtian/work/plow-whip/collab/memory/sessions/20260713_101222_692226_memory_DECISIONS.md.md -->
- **Structure:** New projects require only canonical state/protocol, comms, decisions, sessions, and enabled Agent sessions. Human/legacy views are rebuildable and old status/roadmap files are optional.
- **Safety:** Doctor validates JSON content, enabled task ownership, and derived-state drift. Repair normalizes safe derived data but never overwrites corrupt canonical JSON.
- **Rotation:** Framework comms are proper message blocks and archive names include microseconds to prevent same-second overwrite.

## D-014: Crash-safe state and leased automatic continuation

- **Date:** 2026-07-13
- **Author:** Human + Codex
- **Status:** ✅ Accepted
- **Concurrency:** Canonical state uses monotonically increasing `revision`, atomic replacement, and a portable short-lived lock. A stale Agent writer must fail instead of overwriting newer work. Inbox read-modify-write uses the same lock discipline.
- **Continuation:** Native scheduler `--auto-continue` resumes only stale `active` tasks. `done` and `blocked` are excluded. An unchanged task has a 30-minute wake lease: duplicate scheduler ticks are suppressed, but unfinished work becomes retryable after lease expiry.
- **Execution:** Scheduler binds the installed `plow-whip` absolute entrypoint and a stable tool PATH. CLI and Brain channels can complete unattended work; Desktop-only delivery remains queued/accepted until a client takes it.
- **Files:** `AGENT_PROTOCOL.json` schema v2 is compact machine JSON; `HANDBOOK.zh-CN.md` is its Chinese human view. The framework project's Hot layer must remain within 1200 estimated tokens.
- **Legacy:** Permanent Python daemon loops and the obsolete Markdown-source `conventions_sync` path are retired; periodic work belongs to launchd, systemd user timers, or Windows Task Scheduler.

## D-015: Zero-model scheduler supervision

- **Date:** 2026-07-13
- **Status:** ✅ Accepted
- **Rule:** Native scheduling reads state, inbox lifecycle and local process status without creating an Agent or model session. Running work is never duplicated; done and blocked work are excluded; unchanged actions stop after a bounded retry limit.
- **Failure handling:** A CLI exit without business-state progress is failure. Hard timeout terminates the process group. File fallback preserves the executable-channel error instead of hiding it.

## D-016: One task, one session per CLI

- **Date:** 2026-07-13
- **Status:** ✅ Accepted
- **Rule:** Cursor CLI and Codex CLI generate their own session IDs. plow-whip atomically binds at most one session from each CLI to the current task and must resume that exact ID for every continuation.
- **Cursor:** Capture `system/init.session_id` from `stream-json`; continue with `--resume=<session_id>`.
- **Codex:** Capture the session/thread ID from `exec --json`; continue with `exec resume <session_id>`. Persistent task sessions must not use `--ephemeral`.
- **Archive:** `task complete` marks bound CLI sessions archived and writes a durable Cold-layer `<task_id>_cli_sessions.json`; a new task starts with an empty session map.
