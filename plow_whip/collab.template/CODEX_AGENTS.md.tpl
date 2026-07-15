<!-- plow-whip:codex-instructions:start -->
## plow-whip control contract

This repository uses plow-whip. Before doing project work in Codex Desktop, run:

```bash
plow-whip --project {PROJECT_NAME} start --agent codex --json
```

- Treat `collab/AGENT_PROTOCOL.json` and `collab/AGENT_STATE.json` as canonical truth.
- Use plow-whip commands for task intake and state transitions; never edit state JSON directly.
- As controller, persist the dispatch/`awaiting_receipt`, then end the turn. Never babysit child sessions with `wait` or `read_thread` polling.
- Reconcile only the current execution receipt. Ignore duplicate and stale receipts; never redispatch completed work.
- Follow the bounded rules and verification commands returned by `start --json`.
<!-- plow-whip:codex-instructions:end -->
