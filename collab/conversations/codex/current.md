# Codex Session — plow-whip

**AI:** Codex Desktop (PM + Architect)
**Started:** 2026-07-08
**Topic:** —

## Previous
- (none, new session)

## Current Tasks
- T-016 one-task/one-session CLI lifecycle implemented; final verification in progress.

## Key Decisions
- 2026-07-09: New Codex sessions must start by reading `CONVENTIONS.md` first, especially P0 by_rm: no `rm`; move removals to `by_rm/` with timestamp.
- 2026-07-09: Startup self-check also requires `AGENT_STATE.json`, `AGENT_COMMS.md`, and `memory/NEXT_ACTION.md`, then an Acknowledgments reply in `AGENT_COMMS.md`.
- 2026-07-13: D-012 replaces repeated startup reads with `start --json`, makes `AGENT_PROTOCOL.json` canonical, and moves decisions to Cold.
- 2026-07-13: D-013 makes task state canonical, fixes queued inbox delivery, reduces required legacy files, and validates canonical JSON in doctor.
- 2026-07-13: D-014 adds optimistic state revisions, atomic writes, locked inbox updates, leased automatic continuation, and stable native scheduler execution.
- 2026-07-13: D-016 binds one CLI-generated Cursor/Codex session per task, resumes it, and archives it on completion.

## Outputs
- Added atomic task commands, project-local roster truth, automatic message rotation, dispatch IDs, scheduler-safe `whip --once`, and native macOS/Linux/Windows scheduler support.
- Fixed inbox queued-to-accepted delivery, same-second archive overwrite, scheduler config propagation, and canonical-state drift.
- Installed and verified the macOS launchd job with `--auto-continue`; compacted machine protocol so Hot is 1066/1200 estimated tokens.
- Added streaming Cursor/Codex session-ID capture, exact resume commands, duplicate-session rejection, and durable Cold session archives.
