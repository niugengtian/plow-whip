# Framework-owned collab template

This directory is the single initial-template source owned by plow-whip.

- `init`, `new`, and `doctor --repair` read templates from here.
- Managed projects receive only a generated `collab/` runtime directory.
- Managed projects must not copy, override, or modify `collab.template/`.
- Project-specific rules belong in that project's generated `AGENT_PROTOCOL.json`
  or project configuration, never in this framework template.

`AGENT_PROTOCOL.json`, `AGENT_STATE.json`, `AGENTS.md`, and
`HANDBOOK.zh-CN.md` contain project identity or runtime data and are generated
by plow-whip code. The files below seed only static collaboration content.
